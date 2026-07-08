"""Fetch and install the three plugins: Metamod:Source, CounterStrikeSharp,
MatchZy. Everything is downloaded to memory and extracted straight into
<install_dir>/game/csgo — nothing is written to temp storage.
"""

import io
import json
import logging
import os
import re
import tarfile
import urllib.request
import zipfile
from pathlib import Path

log = logging.getLogger("cs2bot.plugins")

MMS_BASE = "https://mms.alliedmods.net/mmsdrop/2.0/"
CSSHARP_REPO = "roflmuffin/CounterStrikeSharp"
MATCHZY_REPO = "shobhit-pathak/MatchZy"

PLUGINS = ("metamod", "cssharp", "matchzy")


def _http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "cs2bot-updater"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def _github_latest(repo: str, pick):
    rel = json.loads(_http_get(f"https://api.github.com/repos/{repo}/releases/latest"))
    for asset in rel.get("assets", []):
        if pick(asset["name"]):
            return rel["tag_name"], asset["browser_download_url"]
    raise RuntimeError(f"no matching release asset found for {repo}")


def _pick_cssharp(name: str) -> bool:
    n = name.lower()
    return "with-runtime" in n and "linux" in n and n.endswith(".zip")


def _pick_matchzy(name: str) -> bool:
    n = name.lower()
    return n.startswith("matchzy") and "cssharp" not in n and n.endswith(".zip")


def latest_versions() -> dict:
    """Latest available version identifier for each plugin (no install)."""
    metamod = _http_get(MMS_BASE + "mmsource-latest-linux").decode().strip()
    cssharp, _ = _github_latest(CSSHARP_REPO, _pick_cssharp)
    matchzy, _ = _github_latest(MATCHZY_REPO, _pick_matchzy)
    return {"metamod": metamod, "cssharp": cssharp, "matchzy": matchzy}


def _extract_zip(data: bytes, dest: Path):
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        zf.extractall(dest)
        # zipfile drops unix permissions; restore them (dotnet/.so need +x)
        for info in zf.infolist():
            mode = info.external_attr >> 16
            if mode:
                os.chmod(dest / info.filename, mode)


def install(name: str, csgo_dir: Path) -> str:
    """Install the latest release of one plugin. Returns the version installed."""
    if name == "metamod":
        filename = _http_get(MMS_BASE + "mmsource-latest-linux").decode().strip()
        log.info("installing metamod %s", filename)
        data = _http_get(MMS_BASE + filename)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            tf.extractall(csgo_dir)
        return filename

    repo, pick = {
        "cssharp": (CSSHARP_REPO, _pick_cssharp),
        "matchzy": (MATCHZY_REPO, _pick_matchzy),
    }[name]
    tag, url = _github_latest(repo, pick)
    log.info("installing %s %s", name, tag)
    _extract_zip(_http_get(url), csgo_dir)
    return tag


def patch_gameinfo(csgo_dir: Path):
    """CS2 updates rewrite gameinfo.gi, which removes the Metamod entry.
    Re-insert it if missing."""
    gi = csgo_dir / "gameinfo.gi"
    text = gi.read_text(encoding="utf-8")
    if "csgo/addons/metamod" in text:
        return
    patched, n = re.subn(
        r"^(\s*)Game\s+csgo\s*$",
        r"\1Game\tcsgo/addons/metamod\n\1Game\tcsgo",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if n == 0:
        raise RuntimeError("could not find 'Game csgo' entry in gameinfo.gi")
    gi.write_text(patched, encoding="utf-8")
    log.info("patched gameinfo.gi with metamod entry")
