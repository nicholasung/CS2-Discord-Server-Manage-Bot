"""Loads config.json and exposes it as a simple object.

Search order for the config file:
  1. $CS2BOT_CONFIG
  2. ./config.json (working directory)

DISCORD_TOKEN and RCON_PASSWORD environment variables override the
values in the file, so secrets can be kept out of it entirely.
"""

import json
import os
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
        self.steamcmd = s.get("steamcmd", "/usr/games/steamcmd")
        self.app_id = int(s.get("app_id", 730))
        self.launch_script = s.get("launch_script", "/home/steam/Desktop/start_cs2.sh")
        _cwd = s.get("launch_cwd")
        self.launch_cwd = Path(_cwd) if _cwd else Path(self.launch_script).parent
        self.start_timeout = int(s.get("start_timeout_seconds", 180))
        self.stop_timeout = int(s.get("stop_timeout_seconds", 30))
        self.log_buffer_lines = int(s.get("log_buffer_lines", 2000))
        self.startup_markers = s.get("startup_markers", ["Host activate"])

        u = raw.get("update", {})
        self.daily_hour = int(u.get("daily_hour", 6))
        self.daily_minute = int(u.get("daily_minute", 30))
        self.recovery_interval_hours = int(u.get("recovery_interval_hours", 1))

        self.default_map = raw.get("default_map", "de_dust2")
        self.gamemodes = raw.get("gamemodes", {})
        self.state_file = Path(raw.get("state_file", "state.json"))

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
