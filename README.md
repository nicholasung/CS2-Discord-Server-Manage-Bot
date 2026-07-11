# CS2 Discord Server Manager Bot

Manages a CS2 dedicated server (Metamod:Source + CounterStrikeSharp + MatchZy) on a Linux VM from Discord, and keeps it updated.

**One process does everything.** The bot *owns* the CS2 server: it launches your existing `start_cs2.sh` as a child process, supervises it, serves the Discord commands, and runs the update/recovery loops internally. There are no systemd timers and no separate updater — because the bot owns the process, restarts and updates all go through it.

## What it does

- **Discord slash commands**, gated by Discord role:
  - **Admin role**: `/join`, `/restart`, `/map <name|workshop-id>`, `/gamemode <mode> [map]`, `/reinstall-plugins`, `/status`
  - **User role**: recognized but has no commands yet (add them with the `user_only()` check in [cs2bot/bot.py](cs2bot/bot.py))
- **Pinned join board.** `/join` posts an embed with the connect string, `steam://` link, and online/offline status, then pins it in the channel it was run in. The bot keeps that same message edited in place (a background loop checks every `join.refresh_seconds`) as the server goes up or down, and running `/join` again retires the old board and posts a fresh one wherever you run it — so members always have one persistent, up-to-date place to see how to join.
- **Daily update** (runs at a configurable time, default 06:30): stops the server, runs `steamcmd +login anonymous +app_update 730`, and — if the CS2 buildid changed — updates Metamod, CounterStrikeSharp, and MatchZy to their latest releases and re-patches `gameinfo.gi` (CS2 updates wipe the Metamod entry). Then it starts the server back up and verifies it came up healthy.
- **No-plugin fallback**: if a CS2 update leaves the server unable to start with plugins (a common occurrence right after a game update, before plugin authors ship a compatible release), the bot automatically restarts it *without* Metamod/CSSharp/MatchZy — by removing the Metamod entry from `gameinfo.gi` — so the server stays up and playable on vanilla CS2 instead of sitting down entirely.
- **Hourly recovery**: if a start ever fails (with or without plugins), the server is flagged *broken*. Every hour the bot checks whether any of the three plugins has published a newer release; when one has, it installs it and tries restarting with plugins again — falling back to no-plugins once more if that release is still incompatible — repeating until a release actually works and the flag clears. When the server isn't broken, the hourly check is a no-op.
- **Won't interrupt an active game.** Before the daily update or an hourly recovery restart actually stops/restarts the server, the bot checks the current player count over RCON (`status`). If anyone's connected, it defers — rechecking every `update.player_check_interval_seconds` (default 60) — and only proceeds once the server is empty, posting a status-channel notice when it starts deferring and when it resumes. Manual admin commands (`/restart`, `/reinstall-plugins`) are not gated by this — they act immediately, since an admin invoking them has presumably already decided the disruption is warranted.
- **No logs on disk.** The bot captures the server's console output into a bounded in-memory ring buffer (`log_buffer_lines`, default 2000) and judges startup health from that. The only file involved is a transient named pipe on tmpfs (`/dev/shm`, RAM-backed) used to feed that buffer — nothing is ever written to real disk, so logs can't fill the drive. steamcmd/plugin output goes only to the systemd journal.
- **Interactive console.** CS2 runs inside a detached `tmux` session (`server.tmux_session`, default `cs2-server`), so you get a real, typeable console — not just log output — alongside the bot's own health monitoring of the same output. Attach anytime, including over SSH: `tmux attach -t cs2-server` (detach again with `Ctrl-b d` without stopping the server). If a display is available when the bot starts the server, it also best-effort opens a GUI terminal window already attached to that session. This is a convenience on top of, not a replacement for, RCON-based `/map`/`/gamemode` commands.
  > Running under systemd, a plain service has **no** `DISPLAY` — the GUI window won't appear unless you set it. `systemd/cs2bot.service` sets `DISPLAY`/`XAUTHORITY` for a typical single-seat auto-login X11 desktop; if that desktop is Wayland instead, swap them for the commented `WAYLAND_DISPLAY`/`XDG_RUNTIME_DIR` lines in the same file. Either way this only works if a real desktop session is actually running on the box (a monitor with auto-login, or something like VNC/x11vnc keeping one up) — a fully headless VM has nowhere for the window to appear, and `tmux attach` over SSH remains the only console access.

## Prerequisites on the VM

1. **Your launch script** (`start_cs2.sh` → `cs2.sh` with your preset launch options) must run the server in the **foreground** and print to stdout (the normal behavior for a dedicated server). The bot reads that stdout to detect a healthy start — you do **not** need `-condebug` or a `console.log`. If your preset options include `-condebug`, you can drop it to avoid CS2 writing its own `console.log`.
2. **Let the bot be the only launcher.** Once the bot runs, don't also double-click the script — otherwise you'd have two servers fighting for the port. The bot starts the server on boot and restarts it on demand.
3. **RCON enabled** on the server (`rcon_password` in your server cfg), reachable from the VM itself (`127.0.0.1:27015` by default). Used by `/map` and `/gamemode`.
4. **steamcmd** installed (`/usr/games/steamcmd` on Debian/Ubuntu).
5. **tmux** installed — the bot runs CS2 inside a tmux session so it has a real interactive console (`sudo apt install tmux` on Debian/Ubuntu). `config.py` fails fast at startup if it's missing.
6. **Python 3.10+**.

No sudo rule is needed — the bot signals the CS2 process tree directly (via the tmux pane's process group), it doesn't call systemctl.

## Discord application setup

1. Create an application at https://discord.com/developers/applications, add a **Bot**, copy the token.
2. No privileged intents are required.
3. Invite it with the `bot` + `applications.commands` scopes:
   `https://discord.com/oauth2/authorize?client_id=YOUR_APP_ID&scope=bot%20applications.commands&permissions=0`
4. In your Discord server create the roles named in `admin_roles` / `user_roles` (defaults: `Admin`, `User`).
5. (Optional) To get update/failure/recovery notices posted to a channel, set `status_channel_id` to that channel's ID.

## Install (on the VM)

```bash
git clone <this repo> /home/steam/cs2bot
cd /home/steam/cs2bot
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp config.example.json config.json
nano config.json    # token, guild_id, rcon password, launch_script path, paths
```

Run it directly to try it out:

```bash
CS2BOT_CONFIG=$PWD/config.json venv/bin/python -m cs2bot.bot
```

Or install the service so it starts on boot and manages the server:

```bash
sudo cp systemd/cs2bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cs2bot.service

systemctl status cs2bot
journalctl -u cs2bot -f      # bot + server + updater output all live here
```

### Alternative: launch in a GUI terminal via cron (no systemd)

If you'd rather see the bot's own console directly in a window on boot — instead of a background systemd service you check with `journalctl` — start it from that user's crontab instead. This needs the same auto-login desktop session as the [interactive console](#what-it-does) does for the CS2 terminal, since cron's `@reboot` jobs get no more of a desktop environment than a systemd service does: no `DISPLAY`, and they can fire before the desktop has even finished logging in.

```bash
crontab -e
```

Add a line like:

```cron
@reboot sleep 30 && DISPLAY=:0 XAUTHORITY=/home/niccs2/.Xauthority xterm -hold -e "cd /home/niccs2/Steam/cs2bot && while true; do CS2BOT_CONFIG=/home/niccs2/Steam/cs2bot/config.json venv/bin/python -m cs2bot.bot; sleep 5; done"
```

- `sleep 30` gives the desktop session time to finish logging in before cron fires — `@reboot` runs as soon as cron itself starts, which is often before that.
- `DISPLAY=:0` / `XAUTHORITY=...` — same values (and same X11-vs-Wayland caveat) as the `Environment=` lines in [`systemd/cs2bot.service`](systemd/cs2bot.service); swap to `WAYLAND_DISPLAY=wayland-0 XDG_RUNTIME_DIR=/run/user/<uid>` if that desktop is Wayland. `xterm` can be swapped for `gnome-terminal`, `konsole`, etc.
- `-hold` keeps the xterm window open after the bot process exits, so a crash or a bad `config.json` (e.g. `main()`'s missing-token check) leaves its error on screen instead of the window vanishing immediately.
- The `while true; do ...; sleep 5; done` loop is standing in for systemd's `Restart=on-failure`/`RestartSec=10` — cron's `@reboot` only fires once at boot, so without it a crashed bot would just sit there with `-hold` until someone notices and restarts it by hand.

Trade-offs versus the systemd unit: no automatic restart if the desktop session itself dies or the box is headless at boot (no session, no window, no bot), no `journalctl` integration, and the bot only starts once that user is logged into a desktop rather than at true boot. Use this if seeing the console matters more than those; otherwise prefer the systemd service and `tmux attach -t cs2-server` for the CS2 console when you need it.

## Configuration (`config.json`)

| Key | Meaning |
|---|---|
| `discord.token` | Bot token (or set `DISCORD_TOKEN` env var and leave blank) |
| `discord.guild_id` | Your server's ID — makes slash commands register instantly |
| `discord.admin_roles` / `user_roles` | Role names (or role IDs as strings) |
| `discord.status_channel_id` | Optional channel ID for update/failure/recovery notices |
| `rcon.*` | RCON host/port/password (or `RCON_PASSWORD` env var) |
| `server.install_dir` | The game directory — contains `game/csgo/`. In a standard Steam library this is `<steam_library>/steamapps/common/Counter Strike Global Offensive` |
| `server.steam_library` | The Steam **library root** holding the game (the dir with `steamapps/appmanifest_730.acf`), used for `force_install_dir`, the buildid read, and scratch cleanup. Leave `""` for a flat `force_install_dir` install (defaults to `install_dir`); for a standard library set it a few levels up, e.g. `/home/USER/Steam`. **Getting this wrong makes steamcmd write a duplicate install instead of updating in place** — see below |
| `server.launch_script` | Your existing launcher; the bot runs this to start the server |
| `server.launch_cwd` | Working dir for the launcher (defaults to the script's folder) |
| `server.start_timeout_seconds` | How long to wait for startup markers before calling a start failed |
| `server.stop_timeout_seconds` | Grace period before SIGTERM escalates to SIGKILL |
| `server.log_buffer_lines` | Size of the in-memory stdout ring buffer |
| `server.startup_markers` | Strings that must all appear in stdout for a start to count as healthy |
| `server.startup_markers_no_plugins` | Same, but used while running in the no-plugin fallback mode (should not include plugin-specific lines) |
| `server.tmux_session` | Name of the tmux session CS2 runs in (default `cs2-server`); `tmux attach -t <name>` for a live console |
| `server.terminal_emulator` | GUI terminal to auto-open attached to that session: `"auto"` tries a few common ones, or pin one (`gnome-terminal`, `xterm`, `konsole`, ...); no-op on a headless box |
| `update.daily_hour` / `daily_minute` | Local time for the daily update run |
| `update.recovery_interval_hours` | How often to retry recovery while broken (default 1) |
| `update.player_check_interval_seconds` | While a daily update/recovery restart is deferred for active players, how often to recheck (default 60) |
| `update.prune_paths` | Extra globs (relative to `install_dir`) deleted to free space on a disk-full update, on top of the always-safe scratch cleanup, before one automatic retry. For client-only content a dedicated server doesn't need. Empty (default) = scratch cleanup only. See [Disk-full recovery](#disk-full-recovery) |
| `update.symlinks` | Custom-content symlinks — `[{"link": ..., "target": ...}]` — re-created after any update/prune so maps/cfgs linked into the install dir survive. Never deleted by pruning |
| `gamemodes` | Map of mode name → list of RCON commands; `/gamemode` appends a map change |
| `join.host` | Public IP/hostname players connect to; `/join` refuses to post a board until this is set |
| `join.port` | Game port players connect to (default `27015`; separate from `rcon.port` if you split them) |
| `join.password` | Optional `sv_password`, included in the join board's connect string if set |
| `join.refresh_seconds` | How often the pinned join board is checked for an online/offline change (default 60) |
| `join.board_file` | Small JSON file tracking the pinned board's channel/message ID (defaults next to `state_file`) |
| `state_file` | Small JSON file: last buildid, installed plugin versions, broken flag, plugins_disabled flag |

> **Install layout / `steam_library`.** The bot updates CS2 with `steamcmd +force_install_dir <steam_library> +app_update 730`. steamcmd updates *in place* only when it points at the directory that already holds `steamapps/appmanifest_730.acf` — the **library root**. If `force_install_dir` points at the game folder instead (which has no manifest), steamcmd installs a *fresh duplicate copy* there rather than updating the real one — silently doubling disk usage and never actually patching the running server. So for a standard Steam library (`.../Steam/steamapps/common/Counter Strike Global Offensive`), set `install_dir` to the game folder and `steam_library` to the library root (`.../Steam`). For a self-contained `force_install_dir` install the two are the same dir; leave `steam_library` blank.

> **Startup markers matter.** Pick strings you *know* appear in your server's stdout on a good boot and *don't* on a failed one — e.g. a CounterStrikeSharp load line plus a "host activate" style line. If they're too loose the bot may think a broken start succeeded; too strict and it may flag a healthy start as broken. Watch a normal boot's output once and choose accordingly. `startup_markers_no_plugins` should only contain CS2-level lines (no CounterStrikeSharp/MatchZy strings) since those never appear during a no-plugin fallback launch.

## How the update / recovery cycle behaves

```
daily HH:MM  players online? → defer (recheck every player_check_interval_seconds), notify ⏸️/▶️
             stop server → steamcmd app_update 730
             ├─ steamcmd failed → classify:
             │    ├─ transient (content servers / 0x202) → retry w/ backoff (up to 3x)
             │    ├─ disk full → clear steamcmd scratch (+ prune_paths), retry once
             │    └─ still failing → restart on old build (same plugin mode), notify ⚠️ with reason
             └─ ok → buildid changed (or was broken)?
                      ├─ no  → just start server back up
                      └─ yes → install latest metamod/cssharp/matchzy
                               → patch gameinfo.gi
                      → start WITH plugins → watch stdout for markers
                        ├─ markers seen → healthy, clear broken (notify ✅ if updated)
                        └─ timeout/exit → restart WITHOUT plugins (unpatch gameinfo.gi)
                                           ├─ healthy → flag broken, notify ⚠️ (running vanilla)
                                           └─ still fails → flag broken, notify ❌
hourly       not broken → no-op
             broken → any plugin released a newer version than installed?
                       ├─ no  → wait for next hour
                       └─ yes → install it → players online? defer, notify ⏸️/▶️ → restart WITH plugins → verify
                                 ├─ healthy → clear broken, notify ✅
                                 └─ still fails → restart WITHOUT plugins again, stay broken, wait for next hour
```

Plugin sources: Metamod:Source dev builds from `mms.alliedmods.net`, CounterStrikeSharp (`with-runtime` linux build) and MatchZy from their GitHub latest releases.

> Plugin archives are extracted over the existing install, refreshing plugin binaries but leaving files that only exist locally (e.g. your `admins.json`, MatchZy config edits) in place unless the release ships the same file.

## Disk-full recovery

When `steamcmd` aborts an update for lack of space (it reports `Error! App '730' state is 0x202` and exits `8`, having transferred nothing), the bot frees room and retries the update once.

**By default (no config), it clears steamcmd's own scratch/staging dirs** — `<steam_library>/steamapps/downloading` and `.../temp`, i.e. partial downloads and temp files left by interrupted updates. These are never installed game content and steamcmd regenerates them, so this is always safe. It reclaims *stale* leftovers from earlier failed runs; it won't fix a fundamentally too-small disk (the retry re-downloads the current update's data).

**For a genuine shortage** — where the install itself no longer fits with headroom — you can additionally list game content to delete via `update.prune_paths`. This is opt-in because CS2 packs most assets (maps, models, sounds) into `pak01_*.vpk` archives the **server also needs**, so there's no large safe list to ship:

```jsonc
"update": {
  "prune_paths": [
    // ONLY paths you've confirmed a headless server boots and loads maps without.
    // e.g. client-only UI videos, if present on your build:
    "game/csgo/panorama/videos"
  ],
  "symlinks": [
    { "link": "/home/USER/cs2/game/csgo/maps/workshop", "target": "/home/USER/cs2-workshop-maps" }
  ]
}
```

- `prune_paths` are globs **relative to `install_dir`**. Verify each one against your own install before adding it — a wrong entry (e.g. a VPK the server needs) breaks map loading.
- **Safety rails:** pruning never removes a symlink, never removes a path listed in `symlinks`, and never touches anything outside `install_dir`. If nothing can be freed, the update just fails cleanly onto the old build — it won't loop.
- `symlinks` you've linked *into* the install dir (custom maps/cfgs) are re-created after every update or prune, so they self-heal if anything removes them.

> **Why the freed space stays freed:** the bot only ever runs a plain `app_update`, which trusts the local manifest and won't re-download files you deleted. Do **not** run `steamcmd +app_update 730 validate` against this install — a `validate` re-hashes everything and pulls the pruned content back, refilling the disk.
