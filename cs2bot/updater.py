"""Update orchestration, run in-process by the bot's background loops.

Because the bot owns the CS2 process, updating means: stop the server, let
steamcmd/plugin installs touch the files, then start it back up and confirm
it's healthy via ServerManager. There is no separate updater process and no
systemd timer — see the loops in bot.py.

State lives in one small JSON file: the last CS2 buildid, the installed
plugin versions, and a "broken" flag that the recovery loop watches.
"""

import asyncio
import json
import logging
import re
import subprocess

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
    manifest = cfg.install_dir / "steamapps" / f"appmanifest_{cfg.app_id}.acf"
    try:
        m = re.search(r'"buildid"\s+"(\d+)"', manifest.read_text(encoding="utf-8"))
        return m.group(1) if m else ""
    except FileNotFoundError:
        return ""


def _run_steamcmd_blocking(cfg) -> bool:
    cmd = [
        cfg.steamcmd,
        "+force_install_dir", str(cfg.install_dir),
        "+login", "anonymous",
        "+app_update", str(cfg.app_id),
        "+quit",
    ]
    log.info("running steamcmd app_update %s", cfg.app_id)
    # steamcmd output streams to our stdout -> journald; nothing goes to disk
    result = subprocess.run(cmd)
    if result.returncode != 0:
        log.error("steamcmd exited with %s", result.returncode)
        return False
    return True


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


async def perform_daily_update(cfg, manager, notify) -> None:
    """Stop server, run steamcmd, and if CS2 changed (or we're recovering
    from a broken state) update plugins, then start and verify -- falling
    back to a no-plugin launch if the update broke plugin compatibility."""
    state = load_state(cfg)
    old_build = read_buildid(cfg)

    await _wait_for_empty_server(cfg, manager, notify, "the CS2 update")
    await manager.stop()
    ok = await asyncio.to_thread(_run_steamcmd_blocking, cfg)
    if not ok:
        # steamcmd failed; bring the server back on the old build, in
        # whatever plugin mode it was already in, and move on
        await manager.start(plugins_enabled=not state["plugins_disabled"])
        await manager.wait_healthy()
        await notify("⚠️ CS2 daily update: steamcmd failed; server restarted on the existing build.")
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
        await notify(
            f"❌ CS2 update to build {new_build} left the server unable to start, even without plugins. "
            "Check the logs / `/status`."
        )
    elif state["plugins_disabled"]:
        await notify(
            f"⚠️ CS2 updated to build {new_build}, but the plugins failed to start on it. Running "
            "**without plugins** until a compatible release is available; recovery loop will retry hourly."
        )
    elif changed:
        await notify(f"✅ CS2 updated to build {new_build}; plugins refreshed, server healthy.")


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
        await notify(
            "⚠️ Plugins reinstalled on request, but the server still won't start with them; "
            "running **without plugins**. Check the logs / `/status`."
        )
    else:
        await notify(
            "❌ Plugins reinstalled on request but the server did not come up healthy, even "
            "without plugins. Check the logs / `/status`."
        )
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
