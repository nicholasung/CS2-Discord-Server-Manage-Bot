"""Update orchestration, run in-process by the bot's background loops.

Because the bot owns the CS2 process, updating means: stop the server, let
steamcmd/plugin installs touch the files, then start it back up and confirm
it's healthy via ServerManager. There is no separate updater process and no
systemd timer — see the loops in bot.py.

State lives in one small JSON file: the last CS2 buildid, the installed
plugin versions, and a "broken" flag that the recovery loop watches.
"""

import asyncio
import collections
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import plugins, rcon

log = logging.getLogger("cs2bot.updater")


# ---------- state ----------

def load_state(cfg) -> dict:
    try:
        data = json.loads(cfg.state_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data.setdefault("broken", False)
    data.setdefault("installed", {})
    data.setdefault("buildid", "")
    data.setdefault("plugins_disabled", False)
    return data


def save_state(cfg, state: dict):
    cfg.state_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ---------- steamcmd ----------

def read_buildid(cfg) -> str:
    manifest = cfg.steam_library / "steamapps" / f"appmanifest_{cfg.app_id}.acf"
    try:
        m = re.search(r'"buildid"\s+"(\d+)"', manifest.read_text(encoding="utf-8"))
        return m.group(1) if m else ""
    except FileNotFoundError:
        return ""


# steamcmd retries transient content-server/network failures with backoff; a
# permanent failure like a full disk is reported at once without wasting them.
STEAMCMD_MAX_ATTEMPTS = 3
STEAMCMD_RETRY_BACKOFF_SECONDS = 30

# Output signatures that mean retrying won't help (the disk is full). Anything
# else that fails -- notably an update aborted pre-download in state 0x202 --
# is treated as a transient Steam content-server hiccup and retried.
_DISK_FULL_RE = re.compile(
    r"not enough disk space|disk write failure|no space left", re.IGNORECASE
)


def _steamcmd_cmd(cfg, validate: bool = False) -> list[str]:
    app_update = [str(cfg.app_id), "validate"] if validate else [str(cfg.app_id)]
    return [
        cfg.steamcmd,
        "+force_install_dir", str(cfg.steam_library),
        "+login", "anonymous",
        "+app_update", *app_update,
        "+quit",
    ]


def _run_steamcmd_once(cfg, validate: bool = False) -> tuple[int, str]:
    """One steamcmd invocation. Relays output live to stdout (-> journald, as
    before) while retaining the tail so a failure's real reason can be
    classified. Returns (returncode, tail_text)."""
    cmd = _steamcmd_cmd(cfg, validate)
    tail: collections.deque = collections.deque(maxlen=40)
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    )
    # Relay raw lines straight to stdout (not through logging) so the journald
    # volume matches the old inherited-fd behavior, while keeping the tail.
    for line in proc.stdout:
        sys.stdout.write(line)
        tail.append(line.rstrip("\n"))
    proc.wait()
    sys.stdout.flush()
    return proc.returncode, "\n".join(tail)


# steamcmd reports the app's manifest state on failure ("state is 0x626").
# The 0x20 bit means Files Missing: an update started, was interrupted before
# committing, and the manifest now records files as gone -- retrying a plain
# app_update can't repair that; clearing the half-applied download and
# re-running with `validate` can.
_APP_STATE_RE = re.compile(r"state is (0x[0-9a-fA-F]+)")
_STATE_FILES_MISSING = 0x20


def _classify_steamcmd_failure(returncode: int, output: str) -> tuple[str, str]:
    """Map a failed run to (human_reason, kind). kind drives what happens next:
    'disk_full' -> prune configured client-only content and retry (retrying
    alone won't help); 'files_missing' -> clear steamcmd's interrupted-download
    leftovers and retry once with validate; 'content_servers' / 'other' ->
    transient, retry with backoff."""
    if _DISK_FULL_RE.search(output):
        return "not enough disk space on the install volume", "disk_full"
    state = _APP_STATE_RE.search(output)
    if state and int(state.group(1), 16) & _STATE_FILES_MISSING:
        return (
            f"install is missing files (app state {state.group(1)}; interrupted update)",
            "files_missing",
        )
    if "0x202" in output:
        return "Steam content servers unavailable (update aborted, state 0x202)", "content_servers"
    if returncode == 254 or "needs to be online" in output.lower():
        # steamcmd's own startup self-update died before +login was even
        # parsed. Despite the message it prints, an unwritable steamcmd
        # state dir is as common a cause as an actually blocked connection.
        return (
            "steamcmd's startup self-update failed (exit 254; unwritable "
            "steamcmd state dir, or Steam client CDN unreachable)",
            "other",
        )
    return f"steamcmd exited with code {returncode}", "other"


# steamcmd's own scratch/staging dirs (relative to the Steam library root):
# partial downloads and temp files left by an interrupted update. Never
# installed game content, regenerated on demand, so always safe to clear when
# we need room -- done automatically on a disk-full failure, ahead of any user
# prune_paths.
_STEAMCMD_SCRATCH = ("steamapps/downloading", "steamapps/temp")


def _clear_scratch(cfg) -> None:
    """Remove steamcmd's partial-download/staging leftovers. Never installed
    game content and regenerated on demand, so always safe; the first repair
    step when steamcmd reports the install is missing files (a half-applied
    update sitting in steamapps/downloading is the usual cause)."""
    for rel in _STEAMCMD_SCRATCH:
        path = cfg.steam_library / rel
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
            log.info("cleared steamcmd scratch %s", path)


def _path_size(path: Path) -> int:
    """Bytes used by a file or (recursively) a directory, not following
    symlinks."""
    if path.is_file():
        return path.stat().st_size
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def _free_disk_space(cfg) -> int:
    """Make room for an update that failed for lack of space. Always clears
    steamcmd's scratch/staging dirs first (safe leftovers from interrupted
    runs, under the Steam library root), then deletes any configured
    prune_paths -- client-only content a dedicated server doesn't need, under
    the game dir. A plain app_update won't re-download pruned content, so the
    space stays freed (a `validate` would re-fetch it, which the bot runs only
    as the one-time files-missing repair in _run_steamcmd_blocking).
    Safety rails: never removes a symlink (would drop custom-content links),
    never a path listed in `symlinks`, and never anything outside the library
    root. Returns bytes freed."""
    lib_root = cfg.steam_library.resolve()
    protected = {os.path.realpath(s["link"]) for s in cfg.symlinks if s.get("link")}
    freed = 0
    # scratch dirs are relative to the library root; prune_paths (game content)
    # are relative to the game dir.
    targets = [(cfg.steam_library, p) for p in _STEAMCMD_SCRATCH]
    targets += [(cfg.install_dir, p) for p in cfg.prune_paths]
    for base, pattern in targets:
        try:
            matched = list(base.glob(pattern))
        except ValueError as e:
            log.warning("prune: bad pattern %r (use a path relative to its base): %s", pattern, e)
            continue
        if not matched:
            log.info("prune: nothing matched %r", pattern)
            continue
        for path in matched:
            if path.is_symlink():
                continue
            real = path.resolve()
            if lib_root not in real.parents:
                log.warning("prune: skipping %s (outside %s)", path, lib_root)
                continue
            if str(real) in protected:
                continue
            try:
                size = _path_size(path)
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                freed += size
                log.info("prune: removed %s (%.1f GB)", path, size / 1e9)
            except OSError as e:
                log.warning("prune: could not remove %s: %s", path, e)
    if freed:
        log.info("prune: freed %.1f GB total", freed / 1e9)
    return freed


def _ensure_symlinks(cfg) -> None:
    """Recreate any configured custom-content symlinks that have gone missing,
    so an update or a prune can never leave the server without its linked
    maps/cfgs. Existing links (and real files at the link path) are left
    alone."""
    for s in cfg.symlinks:
        link, target = s.get("link"), s.get("target")
        if not link or not target:
            continue
        link_p = Path(link)
        if link_p.is_symlink() or link_p.exists():
            continue
        try:
            link_p.parent.mkdir(parents=True, exist_ok=True)
            link_p.symlink_to(target)
            log.info("recreated symlink %s -> %s", link, target)
        except OSError as e:
            log.error("could not recreate symlink %s -> %s: %s", link, target, e)


# Discord rejects messages over this many characters; debug tails are
# trimmed (oldest lines first) to stay under it.
_DISCORD_MESSAGE_LIMIT = 2000


def with_debug_tail(message: str, detail: str) -> str:
    """Append `detail` to a status message as a code block so failure
    notifications carry the actual error output instead of only a one-line
    summary. Oldest lines are trimmed first to respect Discord's message
    length limit; the end of the output is where the real error lives."""
    detail = (detail or "").strip()
    if not detail:
        return message
    room = _DISCORD_MESSAGE_LIMIT - len(message) - len("\n```text\n\n```")
    if room <= 0:
        return message
    if len(detail) > room:
        detail = detail[-room:]
        nl = detail.find("\n")
        if nl != -1:
            detail = detail[nl + 1:]  # drop the line left partial by the cut
        if not detail:
            return message
    return f"{message}\n```text\n{detail}\n```"


def _run_steamcmd_blocking(cfg) -> tuple[bool, str, str]:
    """Run steamcmd, recovering where we can: transient (content-server/network)
    failures are retried with backoff; a disk-full failure triggers a one-time
    prune of configured client-only content, then a retry; a files-missing app
    state (interrupted update, e.g. 0x626) triggers a one-time scratch cleanup
    and a retry with `validate` so steamcmd re-verifies the whole install.
    Configured symlinks are re-verified after any run that touched files.
    Returns (ok, reason, debug); on failure `reason` is a short human-readable
    string and `debug` is the exact command, exit code, and output tail of the
    last attempt (both "" on success)."""
    log.info("running steamcmd app_update %s", cfg.app_id)
    reason = ""
    returncode, output, attempt = 0, "", 0
    pruned = False
    validate = False
    for attempt in range(1, STEAMCMD_MAX_ATTEMPTS + 1):
        returncode, output = _run_steamcmd_once(cfg, validate=validate)
        if returncode == 0:
            _ensure_symlinks(cfg)
            return True, "", ""
        reason, kind = _classify_steamcmd_failure(returncode, output)
        log.error(
            "steamcmd failed (attempt %d/%d): %s",
            attempt, STEAMCMD_MAX_ATTEMPTS, reason,
        )

        if kind == "files_missing" and not validate:
            validate = True
            _clear_scratch(cfg)
            log.info("cleared download leftovers; retrying with validate to repair the install")
            continue  # retry now; validate re-fetches whatever is missing

        if kind == "disk_full" and not pruned:
            pruned = True
            freed = _free_disk_space(cfg)
            _ensure_symlinks(cfg)
            if freed > 0:
                log.info("freed %.1f GB; retrying update immediately", freed / 1e9)
                continue  # retry now; the space problem should be gone
            log.error("nothing to free (no scratch leftovers, no prune_paths matched)")
            break

        transient = kind in ("content_servers", "other")
        if not transient or attempt == STEAMCMD_MAX_ATTEMPTS:
            break
        backoff = STEAMCMD_RETRY_BACKOFF_SECONDS * attempt
        log.info("retrying steamcmd in %ds", backoff)
        time.sleep(backoff)
    debug = (
        f"$ {' '.join(_steamcmd_cmd(cfg, validate))}\n"
        f"exit code {returncode} on attempt {attempt}/{STEAMCMD_MAX_ATTEMPTS}\n"
        f"--- steamcmd output tail ---\n{output}"
    )
    return False, reason, debug


def _install_plugins_blocking(cfg, names) -> dict:
    installed = {}
    for name in names:
        try:
            installed[name] = plugins.install(name, cfg.csgo_dir)
        except Exception as e:
            log.error("failed to install %s: %s", name, e)
    try:
        plugins.patch_gameinfo(cfg.csgo_dir)
    except Exception as e:
        log.error("gameinfo.gi patch failed: %s", e)
    return installed


# ---------- orchestration (async, called by bot loops) ----------

async def _wait_for_empty_server(cfg, manager, notify, action: str) -> None:
    """Block until no human players are connected before an automatic
    action that would disconnect them (stopping/restarting the server for
    an update). A no-op if the server isn't currently running -- nothing
    to disturb -- and gives up and proceeds anyway if the player count
    can't be determined (RCON down, etc.), rather than blocking forever on
    a check that may never succeed."""
    if not manager.is_running:
        return

    deferred = False
    while True:
        try:
            count = await asyncio.to_thread(rcon.player_count, cfg)
        except Exception as e:
            log.warning("could not check player count (%s); proceeding with %s", e, action)
            return
        if count == 0:
            if deferred:
                log.info("server empty; proceeding with %s", action)
                await notify(f"▶️ Server empty; proceeding with {action}.")
            return
        if not deferred:
            log.info("%d player(s) online; deferring %s until the server is empty", count, action)
            await notify(f"⏸️ {count} player(s) online; deferring {action} until the server is empty.")
            deferred = True
        await asyncio.sleep(cfg.player_check_interval_seconds)


async def restart_with_fallback(cfg, manager, state: dict | None = None) -> bool:
    """(Re)start the server, trying with plugins first. If that doesn't come
    up healthy -- e.g. a CS2 update broke Metamod/CSSharp/MatchZy
    compatibility -- fall back to a vanilla, no-plugin launch so the server
    stays playable while the recovery loop waits for a working plugin
    release. Persists the resulting broken/plugins_disabled flags before
    returning. Returns whether the server ended up running, with or without
    plugins."""
    if state is None:
        state = load_state(cfg)

    healthy = await manager.restart(plugins_enabled=True)
    if healthy:
        state["broken"] = False
        state["plugins_disabled"] = False
    else:
        log.warning("server unhealthy with plugins; falling back to a no-plugin launch")
        healthy = await manager.restart(plugins_enabled=False)
        state["broken"] = True
        state["plugins_disabled"] = healthy

    save_state(cfg, state)
    return healthy


async def perform_daily_update(cfg, manager, notify, manual: bool = False) -> None:
    """Stop server, run steamcmd, and if CS2 changed (or we're recovering
    from a broken state) update plugins, then start and verify -- falling
    back to a no-plugin launch if the update broke plugin compatibility.

    manual=True (the /update admin command) skips the wait-for-empty
    deferral -- like the other manual admin commands, the invoking admin
    has already decided the disruption is warranted -- and also reports
    the no-new-build outcome, which the scheduled daily run keeps silent
    to avoid a pointless notification every day."""
    state = load_state(cfg)
    old_build = read_buildid(cfg)

    if not manual:
        await _wait_for_empty_server(cfg, manager, notify, "the CS2 update")
    await manager.stop()
    ok, reason, debug = await asyncio.to_thread(_run_steamcmd_blocking, cfg)
    if not ok:
        # steamcmd failed; bring the server back on the old build, in
        # whatever plugin mode it was already in, and move on
        await manager.start(plugins_enabled=not state["plugins_disabled"])
        await manager.wait_healthy()
        await notify(with_debug_tail(
            f"⚠️ CS2 daily update failed ({reason}); server restarted on the existing build.",
            debug,
        ))
        return

    new_build = read_buildid(cfg)
    state["buildid"] = new_build
    changed = new_build != old_build

    if changed or state["broken"]:
        log.info("CS2 build %s -> %s; updating plugins", old_build, new_build)
        installed = await asyncio.to_thread(_install_plugins_blocking, cfg, plugins.PLUGINS)
        state["installed"].update(installed)

    healthy = await restart_with_fallback(cfg, manager, state)

    if not healthy:
        await notify(with_debug_tail(
            f"❌ CS2 update to build {new_build} left the server unable to start, even without plugins. "
            "Check the logs / `/status`.",
            manager.last_failed_start_tail,
        ))
    elif state["plugins_disabled"]:
        await notify(with_debug_tail(
            f"⚠️ CS2 updated to build {new_build}, but the plugins failed to start on it. Running "
            "**without plugins** until a compatible release is available; recovery loop will retry hourly.",
            manager.last_failed_start_tail,
        ))
    elif changed:
        await notify(f"✅ CS2 updated to build {new_build}; plugins refreshed, server healthy.")
    elif manual:
        await notify(
            f"✅ No new CS2 build (still `{new_build or 'unknown'}`); server restarted, healthy."
        )


async def perform_plugin_reinstall(cfg, manager, notify) -> bool:
    """Force-reinstall every plugin regardless of the recorded version, then
    restart and verify (falling back to no-plugin mode if that still doesn't
    come up healthy). Unlike perform_recovery (which only acts when a newer
    upstream release exists), this always re-extracts the current release --
    the way to unstick a bad or partial install without waiting for upstream
    to publish something new. Returns the health result of the restart."""
    state = load_state(cfg)

    await manager.stop()
    installed = await asyncio.to_thread(_install_plugins_blocking, cfg, plugins.PLUGINS)
    state["installed"].update(installed)

    healthy = await restart_with_fallback(cfg, manager, state)

    if healthy and not state["plugins_disabled"]:
        await notify("✅ Plugins reinstalled on request; server healthy.")
    elif healthy:
        await notify(with_debug_tail(
            "⚠️ Plugins reinstalled on request, but the server still won't start with them; "
            "running **without plugins**. Check the logs / `/status`.",
            manager.last_failed_start_tail,
        ))
    else:
        await notify(with_debug_tail(
            "❌ Plugins reinstalled on request but the server did not come up healthy, even "
            "without plugins. Check the logs / `/status`.",
            manager.last_failed_start_tail,
        ))
    return healthy


async def perform_recovery(cfg, manager, notify) -> None:
    """Hourly: only acts when the server is flagged broken. Installs any
    plugin release newer than what's recorded, then restarts and verifies.
    If that still isn't healthy, stays in (or drops into) the no-plugin
    fallback and waits for the next hour's release check."""
    state = load_state(cfg)
    if not state["broken"]:
        return

    log.info("server flagged broken; checking for newer plugin releases")
    try:
        latest = await asyncio.to_thread(plugins.latest_versions)
    except Exception as e:
        log.error("could not check plugin versions: %s", e)
        return

    stale = [n for n in plugins.PLUGINS if latest[n] != state["installed"].get(n)]
    if not stale:
        log.info("no newer plugin releases yet; will retry next hour")
        return

    log.info("newer releases available for: %s", ", ".join(stale))
    installed = await asyncio.to_thread(_install_plugins_blocking, cfg, stale)
    state["installed"].update(installed)

    await _wait_for_empty_server(cfg, manager, notify, "the plugin update")
    healthy = await restart_with_fallback(cfg, manager, state)

    if healthy and not state["broken"]:
        await notify(f"✅ Server recovered after updating: {', '.join(stale)}.")
    elif not healthy:
        log.error("still unhealthy after updating %s, even without plugins", ", ".join(stale))
    else:
        log.info("updated %s but still incompatible; staying in no-plugin fallback", ", ".join(stale))
