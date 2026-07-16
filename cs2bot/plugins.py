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

# Operator-edited files that plugin release zips also ship as templates
# (paths relative to game/csgo, as stored in the zips). Once one of these
# exists on disk it is never overwritten by a plugin (re)install -- daily
# updates and recovery reinstalls would otherwise reset admin lists to the
# upstream template. A first install still extracts the template, since
# nothing exists yet. Add more paths here to protect other local edits.
PRESERVED_CONFIGS = frozenset({
    "addons/counterstrikesharp/configs/admins.json",
    "addons/counterstrikesharp/configs/admin_groups.json",
    "addons/counterstrikesharp/configs/core.json",
    "cfg/MatchZy/admins.json",
    "cfg/MatchZy/whitelist.cfg",
})


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
        members = []
        for info in zf.infolist():
            name = os.path.normpath(info.filename)
            if name in PRESERVED_CONFIGS and (dest / name).exists():
                log.info("keeping existing %s (local edits preserved)", name)
                continue
            members.append(info)
        zf.extractall(dest, members=members)
        # zipfile drops unix permissions; restore them (dotnet/.so need +x).
        # Always OR in owner rwx/rw on top of whatever the archive stored:
        # some release zips carry restrictive or missing unix attrs for
        # directories, and CounterStrikeSharp needs to create files (its log,
        # configs) under here at runtime. Trusting the stored mode verbatim
        # can lock the extracting/running user out of its own install -- and
        # since plugins get reinstalled on every recovery attempt, that would
        # keep re-clobbering any manual permission fix on the next retry.
        for info in members:
            path = dest / info.filename
            mode = info.external_attr >> 16
            owner_bits = 0o700 if info.is_dir() else 0o600
            os.chmod(path, mode | owner_bits)


def extract_tar(tf: tarfile.TarFile, dest: Path):
    """extractall with the same guarantees _extract_zip enforces for zips:
    the extracting user always keeps rw(x) on what it extracted, and
    ownership is never taken from the archive -- an unfiltered extractall
    running as root (easy to hit on a first-run bootstrap) chowns files to
    whatever uids the upstream build machine stored, leaving them unwritable
    by the bot's own user on the next update. The stdlib 'data' filter
    (3.12+, and the PEP 706 backports to 3.8.17/3.9.17/3.10.12/3.11.4) does
    both; older pythons get a manual mode fix-up instead."""
    if hasattr(tarfile, "data_filter"):
        tf.extractall(dest, filter="data")
        return
    tf.extractall(dest)
    for m in tf.getmembers():
        if not (m.isfile() or m.isdir()):
            continue
        try:
            os.chmod(dest / m.name, m.mode | (0o700 if m.isdir() else 0o600))
        except OSError:
            pass


def install(name: str, csgo_dir: Path) -> str:
    """Install the latest release of one plugin. Returns the version installed."""
    if name == "metamod":
        filename = _http_get(MMS_BASE + "mmsource-latest-linux").decode().strip()
        log.info("installing metamod %s", filename)
        data = _http_get(MMS_BASE + filename)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            extract_tar(tf, csgo_dir)
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
    Re-insert it if missing, per the official install guide
    (https://cs2.metamodsource.net/): as the very first SearchPaths entry,
    immediately before Game_LowViolence, so addons are searched before any
    other content path."""
    gi = csgo_dir / "gameinfo.gi"
    text = gi.read_text(encoding="utf-8")
    if "csgo/addons/metamod" in text:
        return
    patched, n = re.subn(
        r"^(\s*)Game_LowViolence(\s+)",
        r"\1Game\2csgo/addons/metamod\n\n\1Game_LowViolence\2",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if n == 0:
        # No Game_LowViolence entry in this install; fall back to inserting
        # immediately before "Game csgo" instead.
        patched, n = re.subn(
            r"^(\s*)Game\s+csgo\s*$",
            r"\1Game\tcsgo/addons/metamod\n\1Game\tcsgo",
            text,
            count=1,
            flags=re.MULTILINE,
        )
    if n == 0:
        raise RuntimeError(
            "could not find 'Game_LowViolence' or 'Game csgo' entry in gameinfo.gi"
        )
    gi.write_text(patched, encoding="utf-8")
    log.info("patched gameinfo.gi with metamod entry")


def unpatch_gameinfo(csgo_dir: Path):
    """Remove the Metamod search-path entry inserted by patch_gameinfo(),
    so CS2 launches without loading Metamod (and therefore CounterStrikeSharp
    and MatchZy, which load through it) at all. This is how the no-plugin
    fallback launch is implemented: one edit disables the whole chain
    without touching any plugin files, and patch_gameinfo() re-adds the
    entry cleanly on the next normal start. No-op if already absent."""
    gi = csgo_dir / "gameinfo.gi"
    text = gi.read_text(encoding="utf-8")
    if "csgo/addons/metamod" not in text:
        return
    patched, n = re.subn(
        r"^[ \t]*Game[ \t]+csgo/addons/metamod[ \t]*\n\n?",
        "",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if n == 0:
        raise RuntimeError("could not find the metamod entry to remove in gameinfo.gi")
    gi.write_text(patched, encoding="utf-8")
    log.info("unpatched gameinfo.gi; plugins disabled for this launch")
