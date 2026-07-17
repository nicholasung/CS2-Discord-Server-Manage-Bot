#!/usr/bin/env bash
# Repair ownership and tighten permissions to least privilege across
# everything the cs2bot stack touches: every file owned by the service user
# with owner-only access (u=rwX,go=), and the two password-carrying files
# (config.json, the launch script) locked to 0600/0700.
#
#   sudo scripts/fix_ownership.sh <service-user> [config.json]
#
# <service-user> is the account the systemd unit runs the bot as (User= in
# systemd/cs2bot.service, e.g. niccs2). config.json defaults to
# $CS2BOT_CONFIG, then ./config.json; every path is derived from it with the
# same defaults as cs2bot/config.py. A relative state_file is resolved
# against config.json's directory, matching the systemd WorkingDirectory
# layout.
#
# Use case: a steamcmd/bootstrap run under the wrong account (sudo/root)
# leaves files the service user can't write, which later surfaces as
# steamcmd exit 254 or a stuck app state like 0x626 mid-update. Stop the
# bot first (sudo systemctl stop cs2bot) so nothing is mid-write while
# ownership changes under it.

set -euo pipefail

die() { echo "error: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "must run as root: sudo $0 <service-user> [config.json]"
[[ $# -ge 1 && $# -le 2 ]] || die "usage: sudo $0 <service-user> [config.json]"

user=$1
[[ $user != root ]] || die "the service user must not be root"
id -u "$user" >/dev/null 2>&1 || die "no such user: $user"
group=$(id -gn "$user")
home=$(getent passwd "$user" | cut -d: -f6)

cfg=${2:-${CS2BOT_CONFIG:-config.json}}
[[ -f $cfg ]] || die "config not found: $cfg (pass its path as the 2nd argument)"
cfg=$(realpath -- "$cfg")

# Emit "kind|path" lines: recursive dirs, secret files, required executables.
readarray -t entries < <(python3 - "$cfg" <<'PY'
import json, sys
from pathlib import Path

cfg = Path(sys.argv[1])
raw = json.loads(cfg.read_text(encoding="utf-8"))
s = raw.get("server", {})

install = Path(s.get("install_dir", "/home/steam/cs2"))
lib = Path(s["steam_library"]) if s.get("steam_library") else install
launch = Path(s.get("launch_script", "/home/steam/Desktop/start_cs2.sh"))
state = Path(raw.get("state_file", "state.json"))
if not state.is_absolute():
    state = cfg.parent / state

lines = [
    ("dir", lib), ("dir", install), ("dir", cfg.parent), ("dir", state.parent),
    ("file0600", cfg),
    ("file0700", launch),
    ("exec", install / "game" / "bin" / "linuxsteamrt64" / "cs2"),
]
steamcmd = s.get("steamcmd", "steamcmd")
if "/" in steamcmd:
    lines += [("dir", Path(steamcmd).parent), ("exec", Path(steamcmd))]
for kind, p in lines:
    print(f"{kind}|{p.resolve()}")
PY
)
[[ ${#entries[@]} -gt 0 ]] || die "could not read paths from $cfg"

dirs=("$home/.steam")  # steamcmd's own state; unwritable here = exit 254
files0600=() files0700=() execs=()
for e in "${entries[@]}"; do
    kind=${e%%|*} path=${e#*|}
    case $kind in
        dir) dirs+=("$path") ;;
        file0600) files0600+=("$path") ;;
        file0700) files0700+=("$path") ;;
        exec) execs+=("$path") ;;
    esac
done

# Drop dirs already covered by another target so each tree is walked once;
# processing shortest paths first guarantees parents come before children.
readarray -t sorted < <(printf '%s\n' "${dirs[@]}" | awk '{ print length, $0 }' | sort -n | cut -d' ' -f2-)
targets=()
for d in "${sorted[@]}"; do
    [[ -d $d ]] || { echo "skip (no such directory): $d"; continue; }
    covered=false
    for t in "${targets[@]}"; do
        [[ $d == "$t" || $d == "$t"/* ]] && { covered=true; break; }
    done
    $covered || targets+=("$d")
done

for t in "${targets[@]}"; do
    echo "-> $user:$group, u=rwX,go=  $t"
    # -h: re-own symlinks themselves, never what they point at, so links to
    # content outside these trees can't pull foreign files into the chown.
    chown -Rh "$user:$group" -- "$t"
    chmod -R u=rwX,g=,o= -- "$t"
done

fix_file() { # <path> <mode>
    [[ -f $1 ]] || { echo "skip (no such file): $1"; return 0; }
    chown "$user:$group" -- "$1"
    chmod "$2" -- "$1"
    echo "-> $user:$group, $2  $1"
}
for f in "${files0600[@]}"; do fix_file "$f" 0600; done
for f in "${files0700[@]}"; do fix_file "$f" 0700; done

# chmod u=rwX can only keep exec bits that exist; these two must have them.
for x in "${execs[@]}"; do
    [[ -f $x ]] && { chmod u+x -- "$x"; echo "-> u+x  $x"; }
done

echo "done. restart the bot with: sudo systemctl start cs2bot"
