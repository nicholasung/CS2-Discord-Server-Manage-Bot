"""Loads config.json and exposes it as a simple object.

Search order for the config file:
  1. $CS2BOT_CONFIG
  2. ./config.json (working directory)

DISCORD_TOKEN and RCON_PASSWORD environment variables override the
values in the file, so secrets can be kept out of it entirely.
"""

import json
import os
import shutil
from pathlib import Path


class Config:
    def __init__(self, raw: dict):
        self.raw = raw

        d = raw.get("discord", {})
        self.token = os.environ.get("DISCORD_TOKEN") or d.get("token", "")
        self.guild_id = int(d.get("guild_id") or 0)
        self.admin_roles = [str(r) for r in d.get("admin_roles", ["Admin"])]
        self.user_roles = [str(r) for r in d.get("user_roles", ["User"])]
        self.status_channel_id = int(d.get("status_channel_id") or 0)

        r = raw.get("rcon", {})
        self.rcon_host = r.get("host", "127.0.0.1")
        self.rcon_port = int(r.get("port", 27015))
        self.rcon_password = os.environ.get("RCON_PASSWORD") or r.get("password", "")

        s = raw.get("server", {})
        self.install_dir = Path(s.get("install_dir", "/home/steam/cs2"))
        # The Steam *library root* that holds this game -- the dir whose
        # steamapps/appmanifest_<app_id>.acf describes the install. steamcmd's
        # force_install_dir, the buildid read, and scratch cleanup all key off
        # it; game files / plugins use install_dir. Pointing force_install_dir
        # at the dir that already has the manifest is what makes steamcmd
        # update in place instead of writing a duplicate install.
        #   - Standard Steam library: the game lives at
        #     <steam_library>/steamapps/common/<name>, so steam_library is a
        #     few levels ABOVE install_dir (e.g. install_dir
        #     ".../steamapps/common/Counter Strike Global Offensive",
        #     steam_library ".../Steam").
        #   - Flat force_install_dir install: same dir as install_dir -- which
        #     is the default when this is left blank.
        _lib = s.get("steam_library")
        self.steam_library = Path(_lib) if _lib else self.install_dir
        # Resolved via PATH by default; override with an absolute path only
        # if steamcmd isn't on PATH. subprocess handles PATH lookup for a
        # bare name, so no hard-coded install location is assumed.
        self.steamcmd = s.get("steamcmd", "steamcmd")
        # Where steamcmd keeps its appcache (appcache/appinfo.vdf) -- the stale
        # metadata the /force-update repair clears to break a stuck "already up
        # to date" update. Unlike the game files, appcache lives next to
        # steamcmd's own bootstrap, NOT under force_install_dir, and its
        # location varies (steamcmd's install dir, or the per-user ~/.steam /
        # ~/.local/share/Steam copy the Debian wrapper makes). The repair
        # already searches those usual spots; set this to pin an unusual one.
        # Accepts a single path string or a list of them; searched first.
        _data = s.get("steamcmd_data_dirs", [])
        if isinstance(_data, str):
            _data = [_data] if _data else []
        self.steamcmd_data_dirs = [Path(p) for p in _data]
        self.app_id = int(s.get("app_id", 730))
        self.launch_script = s.get("launch_script", "/home/steam/Desktop/start_cs2.sh")
        _cwd = s.get("launch_cwd")
        self.launch_cwd = Path(_cwd) if _cwd else Path(self.launch_script).parent
        self.start_timeout = int(s.get("start_timeout_seconds", 180))
        self.stop_timeout = int(s.get("stop_timeout_seconds", 30))
        self.log_buffer_lines = int(s.get("log_buffer_lines", 2000))
        self.startup_markers = s.get("startup_markers", ["Host activate"])
        # Used when launching without plugins (see the no-plugin fallback in
        # updater.py): a CS2-only line, since plugin markers like a
        # CounterStrikeSharp load line will never appear in that mode.
        self.startup_markers_no_plugins = s.get("startup_markers_no_plugins", ["Host activate"])
        # Niceness for the CS2 child process (0-19; higher = lower priority).
        # Keeps the game server from starving the bot's event loop of CPU
        # time when it's under load, which otherwise shows up as Discord
        # commands timing out while the server is running.
        self.server_nice = int(s.get("nice", 10))
        # Interactive console: CS2 runs inside this tmux session so a real
        # terminal can attach and type into it directly, while the bot still
        # taps its output for health checks. "auto" tries a few common GUI
        # terminal emulators in order; set explicitly to pin one, or leave
        # unset entirely on headless boxes (falls back to logging the
        # manual `tmux attach` command).
        self.tmux_session = s.get("tmux_session", "cs2-server")
        self.terminal_emulator = s.get("terminal_emulator", "auto")

        u = raw.get("update", {})
        self.daily_hour = int(u.get("daily_hour", 6))
        self.daily_minute = int(u.get("daily_minute", 30))
        self.recovery_interval_hours = int(u.get("recovery_interval_hours", 1))
        # How often to recheck player count while an update/restart is
        # deferred waiting for the server to empty out.
        self.player_check_interval_seconds = int(u.get("player_check_interval_seconds", 60))
        # Custom-content symlinks to (re)create after any successful update, so
        # maps/cfgs linked into the install dir survive (a `validate` can strip
        # files it doesn't recognize). Each entry is
        # {"link": <path>, "target": <path>}.
        self.symlinks = [dict(s) for s in u.get("symlinks", [])]

        j = raw.get("join", {})
        self.join_host = j.get("host", "")
        self.join_port = int(j.get("port", 27015))
        self.join_password = j.get("password", "")
        # How often the pinned join-board embed is checked for an online/
        # offline change and re-edited in place; see joinboard.py.
        self.join_refresh_seconds = int(j.get("refresh_seconds", 60))

        self.default_map = raw.get("default_map", "de_dust2")
        self.gamemodes = raw.get("gamemodes", {})
        self.state_file = Path(raw.get("state_file", "state.json"))
        self.join_board_file = Path(j.get("board_file") or self.state_file.with_name("join_board.json"))

    @property
    def csgo_dir(self) -> Path:
        return self.install_dir / "game" / "csgo"

    def validate(self):
        """Fail fast and loud on bad paths, instead of dying deep inside
        asyncio.create_subprocess_exec with a confusing traceback."""
        problems = []
        if not Path(self.launch_script).is_file():
            problems.append(f"server.launch_script does not exist: {self.launch_script}")
        elif not os.access(self.launch_script, os.R_OK):
            problems.append(f"server.launch_script is not readable: {self.launch_script}")
        if not self.launch_cwd.is_dir():
            problems.append(f"server.launch_cwd does not exist: {self.launch_cwd}")
        if not self.install_dir.is_dir():
            problems.append(f"server.install_dir does not exist: {self.install_dir}")
        if not self.steam_library.is_dir():
            problems.append(f"server.steam_library does not exist: {self.steam_library}")
        if not shutil.which("tmux"):
            problems.append("tmux is not installed or not on PATH (required to run the CS2 console)")
        if not shutil.which(self.steamcmd):
            problems.append(f"server.steamcmd not found (not on PATH, or bad path): {self.steamcmd}")
        if problems:
            raise SystemExit(
                "Invalid config.json (check for typo'd keys — unknown keys are silently "
                "ignored and fall back to placeholder defaults):\n  - " + "\n  - ".join(problems)
            )


def load_config() -> Config:
    path = os.environ.get("CS2BOT_CONFIG") or "config.json"
    with open(path, "r", encoding="utf-8") as f:
        cfg = Config(json.load(f))
    cfg.validate()
    return cfg
