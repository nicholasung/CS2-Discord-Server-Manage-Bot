# CS2 Discord Server Manager Bot

Manages a CS2 dedicated server (Metamod:Source + CounterStrikeSharp + MatchZy) on a Linux VM from Discord, and keeps it updated.

**One process does everything.** The bot *owns* the CS2 server: it launches your existing `start_cs2.sh` as a child process, supervises it, serves the Discord commands, and runs the update/recovery loops internally. There are no systemd timers and no separate updater — because the bot owns the process, restarts and updates all go through it.

## What it does

- **Discord slash commands**, gated by Discord role:
  - **Admin role**: `/restart`, `/map <name|workshop-id>`, `/gamemode <mode> [map]`, `/status`
  - **User role**: recognized but has no commands yet (add them with the `user_only()` check in [cs2bot/bot.py](cs2bot/bot.py))
- **Daily update** (runs at a configurable time, default 06:30): stops the server, runs `steamcmd +login anonymous +app_update 730`, and — if the CS2 buildid changed — updates Metamod, CounterStrikeSharp, and MatchZy to their latest releases and re-patches `gameinfo.gi` (CS2 updates wipe the Metamod entry). Then it starts the server back up and verifies it came up healthy.
- **Hourly recovery**: if a start ever fails, the server is flagged *broken*. Every hour the bot checks whether any of the three plugins has published a newer release; when one has, it installs it, restarts, and re-verifies — repeating until the server is healthy again. When the server isn't broken, the hourly check is a no-op.
- **No logs on disk.** The bot captures the server's stdout into a bounded in-memory ring buffer (`log_buffer_lines`, default 2000) and judges startup health from that. Nothing is written to disk, so logs can't fill the drive. steamcmd/plugin output goes only to the systemd journal.

## Prerequisites on the VM

1. **Your launch script** (`start_cs2.sh` → `cs2.sh` with your preset launch options) must run the server in the **foreground** and print to stdout (the normal behavior for a dedicated server). The bot reads that stdout to detect a healthy start — you do **not** need `-condebug` or a `console.log`. If your preset options include `-condebug`, you can drop it to avoid CS2 writing its own `console.log`.
2. **Let the bot be the only launcher.** Once the bot runs, don't also double-click the script — otherwise you'd have two servers fighting for the port. The bot starts the server on boot and restarts it on demand.
3. **RCON enabled** on the server (`rcon_password` in your server cfg), reachable from the VM itself (`127.0.0.1:27015` by default). Used by `/map` and `/gamemode`.
4. **steamcmd** installed (`/usr/games/steamcmd` on Debian/Ubuntu).
5. **Python 3.10+**.

No sudo rule is needed — the bot signals its own child process, it doesn't call systemctl.

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
| `update.daily_hour` / `daily_minute` | Local time for the daily update run |
| `update.recovery_interval_hours` | How often to retry recovery while broken (default 1) |
| `gamemodes` | Map of mode name → list of RCON commands; `/gamemode` appends a map change |
| `state_file` | Small JSON file: last buildid, installed plugin versions, broken flag |

> **Startup markers matter.** Pick strings you *know* appear in your server's stdout on a good boot and *don't* on a failed one — e.g. a CounterStrikeSharp load line plus a "host activate" style line. If they're too loose the bot may think a broken start succeeded; too strict and it may flag a healthy start as broken. Watch a normal boot's output once and choose accordingly.

## How the update / recovery cycle behaves

```
daily HH:MM  stop server → steamcmd app_update 730
             ├─ steamcmd failed → restart on old build, notify ⚠️
             └─ ok → buildid changed (or was broken)?
                      ├─ no  → just start server back up
                      └─ yes → install latest metamod/cssharp/matchzy
                               → patch gameinfo.gi
                      → start → watch stdout for markers
                        ├─ markers seen → healthy (notify ✅ if updated)
                        └─ timeout/exit → flag broken (notify ❌)
hourly       not broken → no-op
             broken → any plugin released a newer version than installed?
                       ├─ no  → wait for next hour
                       └─ yes → install it → restart → verify
                                 └─ healthy → clear broken (notify ✅)
```

Plugin sources: Metamod:Source dev builds from `mms.alliedmods.net`, CounterStrikeSharp (`with-runtime` linux build) and MatchZy from their GitHub latest releases.

> Plugin archives are extracted over the existing install, refreshing plugin binaries but leaving files that only exist locally (e.g. your `admins.json`, MatchZy config edits) in place unless the release ships the same file.
