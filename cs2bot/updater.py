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

from . import plugins

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

async def perform_daily_update(cfg, manager, notify) -> None:
    """Stop server, run steamcmd, and if CS2 changed (or we're recovering
    from a broken state) update plugins, then start and verify."""
    state = load_state(cfg)
    old_build = read_buildid(cfg)

    await manager.stop()
    ok = await asyncio.to_thread(_run_steamcmd_blocking, cfg)
    if not ok:
        # steamcmd failed; bring the server back on the old build and move on
        await manager.start()
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

    await manager.start()
    healthy = await manager.wait_healthy()
    state["broken"] = not healthy
    save_state(cfg, state)

    if healthy and changed:
        await notify(f"✅ CS2 updated to build {new_build}; plugins refreshed, server healthy.")
    elif not healthy:
        await notify(
            f"❌ CS2 update to build {new_build} left the server unable to start. "
            "Recovery loop will retry plugin updates hourly until it's back."
        )


async def perform_recovery(cfg, manager, notify) -> None:
    """Hourly: only acts when the server is flagged broken. Installs any
    plugin release newer than what's recorded, then restarts and verifies."""
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

    healthy = await manager.restart()
    state["broken"] = not healthy
    save_state(cfg, state)

    if healthy:
        await notify(f"✅ Server recovered after updating: {', '.join(stale)}.")
    else:
        log.error("still unhealthy after updating %s", ", ".join(stale))
