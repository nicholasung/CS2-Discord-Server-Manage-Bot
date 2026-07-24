"""Discord bot that owns and supervises the CS2 dedicated server.

One process does everything:
  * launches/supervises the CS2 server as a child (see ServerManager)
  * serves role-gated slash commands
      Admin role: /join, /restart, /map, /gamemode, /update, /validate,
                  /force-update, /reinstall-plugins, /status
      User role:  recognized, but has no commands yet — add them under the
                  user_only() check when the time comes.
  * runs the daily steamcmd update and the hourly plugin-recovery loops
  * keeps a pinned "how to join" embed (posted by /join) in sync with
    whether the server is online -- see joinboard.py

There is no systemd timer and no separate updater process: because the bot
owns the CS2 process, updates and restarts have to go through it.
"""

import asyncio
import datetime as dt
import logging

import discord
from discord import app_commands
from discord.ext import tasks

from . import joinboard, updater
from .config import load_config
from .rcon import rcon_exec
from .server import ServerManager

log = logging.getLogger("cs2bot")
cfg = load_config()

# Serializes the flows that mutate game files between a stop and a start
# (the daily update and recovery loops, /update, /reinstall-plugins). The
# ServerManager lock only covers the process itself; without this, a manual
# update colliding with a scheduled one would interleave steamcmd and plugin
# writes between each other's stop/start.
_update_lock = asyncio.Lock()


def _has_any_role(member, wanted: list) -> bool:
    roles = getattr(member, "roles", [])
    return any(r.name in wanted or str(r.id) in wanted for r in roles)


def admin_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if _has_any_role(interaction.user, cfg.admin_roles):
            return True
        raise app_commands.CheckFailure("admin role required")
    return app_commands.check(predicate)


def user_only():
    """No commands use this yet; it exists so user-level commands can be
    added later with the same pattern as admin_only()."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if _has_any_role(interaction.user, cfg.admin_roles + cfg.user_roles):
            return True
        raise app_commands.CheckFailure("user role required")
    return app_commands.check(predicate)


class Cs2Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.manager = ServerManager(cfg)

    async def setup_hook(self):
        if cfg.guild_id:
            guild = discord.Object(id=cfg.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

        # bring the server up as soon as the bot starts, falling back to a
        # no-plugin launch if plugins don't come up healthy
        await updater.restart_with_fallback(cfg, self.manager)

        daily_update_loop.start()
        recovery_loop.start()
        join_board_loop.start()

    async def close(self):
        daily_update_loop.cancel()
        recovery_loop.cancel()
        join_board_loop.cancel()
        await self.manager.stop()
        await super().close()


bot = Cs2Bot()


async def notify(message: str):
    log.info("notify: %s", message)
    if not cfg.status_channel_id:
        return
    channel = bot.get_channel(cfg.status_channel_id)
    if channel is None:
        return
    try:
        await channel.send(message)
    except discord.DiscordException as e:
        log.warning("could not post status message: %s", e)


@bot.event
async def on_ready():
    log.info("Logged in as %s", bot.user)


@bot.tree.error
async def on_command_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.CheckFailure):
        msg = "You don't have permission to use this command."
    else:
        log.exception("command failed", exc_info=error)
        msg = f"Command failed: {error}"
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


async def _rcon(*commands: str) -> str:
    return await asyncio.to_thread(rcon_exec, cfg, *commands)


def _connect_string() -> str:
    connect = f"connect {cfg.join_host}:{cfg.join_port}"
    if cfg.join_password:
        connect += f"; password {cfg.join_password}"
    return connect


def _steam_link() -> str:
    link = f"steam://connect/{cfg.join_host}:{cfg.join_port}"
    if cfg.join_password:
        link += f"/{cfg.join_password}"
    return link


def _join_embed() -> discord.Embed:
    online = bot.manager.is_running
    embed = discord.Embed(
        title="🎮 CS2 Server",
        description=f"**Console:** `{_connect_string()}`\n**Steam:** {_steam_link()}",
        color=discord.Color.green() if online else discord.Color.red(),
    )
    embed.add_field(name="Status", value="🟢 Online" if online else "🔴 Offline")
    embed.timestamp = discord.utils.utcnow()
    embed.set_footer(text="Last updated")
    return embed


# ---------------- admin commands ----------------

@bot.tree.command(
    name="join",
    description="Post/refresh the pinned join-instructions board in this channel (admin)",
)
@admin_only()
async def join(interaction: discord.Interaction):
    if not cfg.join_host:
        await interaction.response.send_message(
            "⚠️ Join info isn't configured yet — set `join.host` in config.json.",
            ephemeral=True,
        )
        return

    # Retire any previous board (possibly in another channel) so there's
    # only ever one pinned join message at a time. Best-effort: if it's
    # already gone, or we lack permission, we just move on and post fresh.
    old = joinboard.load(cfg)
    if old.get("message_id"):
        old_channel = bot.get_channel(old["channel_id"])
        if old_channel is not None:
            try:
                old_message = await old_channel.fetch_message(old["message_id"])
                await old_message.delete()
            except discord.DiscordException:
                pass

    await interaction.response.send_message(embed=_join_embed())
    message = await interaction.original_response()
    try:
        await message.pin()
        pin_note = ""
    except discord.DiscordException as e:
        log.warning("could not pin join board message: %s", e)
        pin_note = " (couldn't pin it — check my Manage Messages permission)"

    joinboard.save(cfg, {
        "channel_id": message.channel.id,
        "message_id": message.id,
        "online": bot.manager.is_running,
    })
    await interaction.followup.send(f"📌 Join board posted{pin_note}.", ephemeral=True)

@bot.tree.command(name="restart", description="Restart the CS2 server (admin)")
@admin_only()
async def restart(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    state = updater.load_state(cfg)
    healthy = await updater.restart_with_fallback(cfg, bot.manager, state)
    if healthy and not state["plugins_disabled"]:
        await interaction.followup.send("🔄 Server restarted and healthy.")
    elif healthy:
        await interaction.followup.send(updater.with_debug_tail(
            "⚠️ Server restarted but plugins wouldn't start; running **without plugins**. "
            "Check the logs / `/status`.",
            bot.manager.last_failed_start_tail,
        ))
    else:
        await interaction.followup.send(updater.with_debug_tail(
            "⚠️ Server was restarted but did not report healthy within the timeout, even "
            "without plugins. Check the logs / `/status`.",
            bot.manager.last_failed_start_tail,
        ))


@bot.tree.command(name="map", description="Change the map (admin)")
@app_commands.describe(name="Map name (e.g. de_dust2) or a workshop ID")
@admin_only()
async def change_map(interaction: discord.Interaction, name: str):
    await interaction.response.defer(ephemeral=True)
    name = name.strip().split()[0]  # single token only; no command injection via rcon
    cmd = f"host_workshop_map {name}" if name.isdigit() else f"changelevel {name}"
    try:
        await _rcon(cmd)
        await interaction.followup.send(f"🗺️ Changing map: `{name}`")
    except Exception as e:
        await interaction.followup.send(f"❌ RCON error: {e}")


@bot.tree.command(name="gamemode", description="Change the gamemode (admin)")
@app_commands.describe(mode="Gamemode", map="Map to load after switching (optional)")
@app_commands.choices(
    mode=[app_commands.Choice(name=m, value=m) for m in sorted(cfg.gamemodes)]
)
@admin_only()
async def gamemode(interaction: discord.Interaction, mode: str, map: str = ""):
    await interaction.response.defer(ephemeral=True)
    commands = list(cfg.gamemodes[mode])
    target = (map or cfg.default_map).strip().split()[0]
    commands.append(
        f"host_workshop_map {target}" if target.isdigit() else f"changelevel {target}"
    )
    try:
        await _rcon(*commands)
        await interaction.followup.send(f"🎮 Gamemode `{mode}` set, loading `{target}`.")
    except Exception as e:
        await interaction.followup.send(f"❌ RCON error: {e}")


async def _run_manual_update(interaction: discord.Interaction, opening: str,
                             validate: bool, clear_caches: bool = False):
    """Shared body of /update, /validate, and /force-update: refuse if an
    update is already running, acknowledge immediately (the empty-server wait
    can run long past any deferral window), then run the update cycle under the
    lock."""
    if _update_lock.locked():
        await interaction.response.send_message(
            "⏳ An update or reinstall is already in progress; watch the status channel.",
            ephemeral=True,
        )
        return
    await interaction.response.send_message(opening, ephemeral=True)
    # Relay the updater's notifications while keeping the last one -- the
    # outcome -- to echo back to the invoking admin, who otherwise gets no
    # feedback at all when no status channel is configured.
    outcome: list[str] = []

    async def relay(message: str):
        outcome.append(message)
        await notify(message)

    async with _update_lock:
        await updater.perform_daily_update(
            cfg, bot.manager, relay, manual=True, validate=validate, clear_caches=clear_caches
        )
    if outcome:
        try:
            await interaction.followup.send(outcome[-1], ephemeral=True)
        except discord.DiscordException:
            pass  # token expires after 15 min; a slow download can outlive it


@bot.tree.command(
    name="update",
    description="Check for a CS2 update now and apply it — restarts the server (admin)",
)
@admin_only()
async def force_update(interaction: discord.Interaction):
    await _run_manual_update(
        interaction,
        "🔄 Checking for a CS2 update now. If players are online I'll wait for the "
        "server to empty first, then take it down, update, and restart. Progress is "
        "posted to the status channel.",
        validate=False,
    )


@bot.tree.command(
    name="validate",
    description="Verify & repair the CS2 install (steamcmd validate) — slow; restarts the server (admin)",
)
@admin_only()
async def validate_install(interaction: discord.Interaction):
    await _run_manual_update(
        interaction,
        "🔎 Validating the CS2 install: steamcmd re-hashes every game file and "
        "re-downloads anything damaged or missing — this can take a while. If "
        "players are online I'll wait for the server to empty first. Progress "
        "is posted to the status channel.",
        validate=True,
    )


@bot.tree.command(
    name="force-update",
    description="Fix a stuck update (clears steamcmd caches + validates) when clients get a version mismatch (admin)",
)
@admin_only()
async def force_update_repair(interaction: discord.Interaction):
    await _run_manual_update(
        interaction,
        "🧹 Forcing a CS2 update: clearing steamcmd's stale caches (the app-info/depot "
        "cache and install manifest) and re-validating. This fixes the case where steamcmd "
        "insists the server is up to date but connecting clients get a **version mismatch**. "
        "It re-downloads changed content and can take a while. If players are online I'll wait "
        "for the server to empty first. Progress is posted to the status channel.",
        validate=False,  # forced on internally alongside the cache clear
        clear_caches=True,
    )


@bot.tree.command(
    name="reinstall-plugins",
    description="Force-reinstall Metamod/CSSharp/MatchZy and restart (admin)",
)
@admin_only()
async def reinstall_plugins(interaction: discord.Interaction):
    if _update_lock.locked():
        await interaction.response.send_message(
            "⏳ An update is already in progress; try again once it finishes.",
            ephemeral=True,
        )
        return
    # Respond right away rather than deferring: with the empty-server wait this
    # can run long past the interaction's deferral window if players are on.
    await interaction.response.send_message(
        "🧩 Reinstalling plugins. If players are online I'll wait for the server to "
        "empty first, then take it down, reinstall, and restart. Progress is posted "
        "to the status channel.",
        ephemeral=True,
    )
    async with _update_lock:
        healthy = await updater.perform_plugin_reinstall(cfg, bot.manager, notify)
    if healthy:
        msg = "🧩 Plugins reinstalled; server healthy."
    else:
        msg = updater.with_debug_tail(
            "⚠️ Plugins reinstalled but the server did not report healthy within the timeout. "
            "Check the logs / `/status`.",
            bot.manager.last_failed_start_tail,
        )
    try:
        await interaction.followup.send(msg, ephemeral=True)
    except discord.DiscordException:
        pass  # token may have expired during a long player-wait; notify() already posted the outcome


@bot.tree.command(name="status", description="Show server status (admin)")
@admin_only()
async def status(interaction: discord.Interaction):
    state = updater.load_state(cfg)
    running = "🟢 running" if bot.manager.is_running else "🔴 stopped"
    if state["plugins_disabled"]:
        broken = " (⚠️ **running without plugins** — recovery loop active)"
    elif state["broken"]:
        broken = " (flagged **broken** — recovery loop active)"
    else:
        broken = ""
    build = state.get("buildid") or "unknown"
    versions = ", ".join(f"{k} {v}" for k, v in state.get("installed", {}).items()) or "unknown"
    await interaction.response.send_message(
        f"CS2 server: {running}{broken}\nBuild: `{build}`\nPlugins: {versions}",
        ephemeral=True,
    )


# ---------------- background loops ----------------

# discord.py treats a naive time as UTC; pin it to the machine's local zone
# so daily_hour/daily_minute mean local time.
_LOCAL_TZ = dt.datetime.now().astimezone().tzinfo


@tasks.loop(time=dt.time(hour=cfg.daily_hour, minute=cfg.daily_minute, tzinfo=_LOCAL_TZ))
async def daily_update_loop():
    try:
        async with _update_lock:
            await updater.perform_daily_update(cfg, bot.manager, notify)
    except Exception:
        log.exception("daily update loop failed")


@tasks.loop(hours=cfg.recovery_interval_hours)
async def recovery_loop():
    try:
        async with _update_lock:
            await updater.perform_recovery(cfg, bot.manager, notify)
    except Exception:
        log.exception("recovery loop failed")


@tasks.loop(seconds=cfg.join_refresh_seconds)
async def join_board_loop():
    """Keep the pinned join embed's online/offline status current. Only
    edits the message when that status actually changed, to avoid
    needless API calls every tick."""
    try:
        board = joinboard.load(cfg)
        if not board.get("message_id"):
            return
        online = bot.manager.is_running
        if board.get("online") == online:
            return
        channel = bot.get_channel(board["channel_id"])
        if channel is None:
            return
        try:
            message = await channel.fetch_message(board["message_id"])
            await message.edit(embed=_join_embed())
        except discord.NotFound:
            log.warning("join board message no longer exists; clearing it")
            joinboard.clear(cfg)
            return
        board["online"] = online
        joinboard.save(cfg, board)
    except Exception:
        log.exception("join board refresh failed")


@daily_update_loop.before_loop
@recovery_loop.before_loop
@join_board_loop.before_loop
async def _wait_ready():
    await bot.wait_until_ready()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not cfg.token or cfg.token == "YOUR_BOT_TOKEN":
        raise SystemExit("Discord token missing: set it in config.json or DISCORD_TOKEN")
    bot.run(cfg.token, log_handler=None)


if __name__ == "__main__":
    main()
