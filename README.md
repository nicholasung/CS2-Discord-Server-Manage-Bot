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

## Configuration (`config.json`)

| Key | Meaning |
|---|---|
| `discord.token` | Bot token (or set `DISCORD_TOKEN` env var and leave blank) |
| `discord.guild_id` | Your server's ID — makes slash commands register instantly |
| `discord.admin_roles` / `user_roles` | Role names (or role IDs as strings) |
| `discord.status_channel_id` | Optional channel ID for update/failure/recovery notices |
| `rcon.*` | RCON host/port/password (or `RCON_PASSWORD` env var) |
| `server.install_dir` | CS2 install root (contains `game/` and `steamapps/`) |
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
| `gamemodes` | Map of mode name → list of RCON commands; `/gamemode` appends a map change |
| `join.host` | Public IP/hostname players connect to; `/join` refuses to post a board until this is set |
| `join.port` | Game port players connect to (default `27015`; separate from `rcon.port` if you split them) |
| `join.password` | Optional `sv_password`, included in the join board's connect string if set |
| `join.refresh_seconds` | How often the pinned join board is checked for an online/offline change (default 60) |
| `join.board_file` | Small JSON file tracking the pinned board's channel/message ID (defaults next to `state_file`) |
| `state_file` | Small JSON file: last buildid, installed plugin versions, broken flag, plugins_disabled flag |

> **Startup markers matter.** Pick strings you *know* appear in your server's stdout on a good boot and *don't* on a failed one — e.g. a CounterStrikeSharp load line plus a "host activate" style line. If they're too loose the bot may think a broken start succeeded; too strict and it may flag a healthy start as broken. Watch a normal boot's output once and choose accordingly. `startup_markers_no_plugins` should only contain CS2-level lines (no CounterStrikeSharp/MatchZy strings) since those never appear during a no-plugin fallback launch.

## How the update / recovery cycle behaves

```
daily HH:MM  players online? → defer (recheck every player_check_interval_seconds), notify ⏸️/▶️
             stop server → steamcmd app_update 730
             ├─ steamcmd failed → restart on old build (same plugin mode as before), notify ⚠️
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
