"""Persists where the pinned "how to join" embed lives, so bot.py can edit
it in place -- across restarts and whenever the server's online/offline
status changes -- instead of re-posting it.
"""

import json


def load(cfg) -> dict:
    try:
        return json.loads(cfg.join_board_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save(cfg, data: dict):
    cfg.join_board_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.join_board_file.write_text(json.dumps(data, indent=2), encoding="utf-8")


def clear(cfg):
    save(cfg, {})
