"""Discord bot that owns and supervises the CS2 dedicated server.

One process does everything:
  * launches/supervises the CS2 server as a child (see ServerManager)
  * serves role-gated slash commands
      Admin role: /restart, /map, /gamemode, /reinstall-plugins, /status
      User role:  recognized, but has no commands yet — add them under the
                  user_only() check when the time comes.
  * runs the daily steamcmd update and the hourly plugin-recovery loops

There is no systemd timer and no separate updater process: because the bot
owns the CS2 process, updates and restarts have to go through it.
"""

import asyncio
import datetime as dt
import logging

import discord
from discord import app_commands
from discord.ext import tasks

from . import updater
from .config import load_config
from .rcon import rcon_exec
from .server import ServerManager

log = logging.getLogger("cs2bot")
cfg = load_config()


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

        # bring the server up as soon as the bot starts
        await self.manager.start()
        healthy = await self.manager.wait_healthy()
        state = updater.load_state(cfg)
        state["broken"] = not healthy
        updater.save_state(cfg, state)

        daily_update_loop.start()
        recovery_loop.start()

    async def close(self):
        daily_update_loop.cancel()
        recovery_loop.cancel()
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


# ---------------- admin commands ----------------

@bot.tree.command(name="restart", description="Restart the CS2 server (admin)")
@admin_only()
async def restart(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    healthy = await bot.manager.restart()
    state = updater.load_state(cfg)
    state["broken"] = not healthy
    updater.save_state(cfg, state)
    if healthy:
        await interaction.followup.send("🔄 Server restarted and healthy.")
    else:
        await interaction.followup.send(
            "⚠️ Server was restarted but did not report healthy within the timeout. "
            "Check the logs / `/status`."
        )


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


@bot.tree.command(
    name="reinstall-plugins",
    description="Force-reinstall Metamod/CSSharp/MatchZy and restart (admin)",
)
@admin_only()
async def reinstall_plugins(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    healthy = await updater.perform_plugin_reinstall(cfg, bot.manager, notify)
    if healthy:
        await interaction.followup.send("🧩 Plugins reinstalled; server healthy.")
    else:
        await interaction.followup.send(
            "⚠️ Plugins reinstalled but the server did not report healthy within the timeout. "
            "Check the logs / `/status`."
        )


@bot.tree.command(name="status", description="Show server status (admin)")
@admin_only()
async def status(interaction: discord.Interaction):
    state = updater.load_state(cfg)
    running = "🟢 running" if bot.manager.is_running else "🔴 stopped"
    broken = " (flagged **broken** — recovery loop active)" if state["broken"] else ""
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
        await updater.perform_daily_update(cfg, bot.manager, notify)
    except Exception:
        log.exception("daily update loop failed")


@tasks.loop(hours=cfg.recovery_interval_hours)
async def recovery_loop():
    try:
        await updater.perform_recovery(cfg, bot.manager, notify)
    except Exception:
        log.exception("recovery loop failed")


@daily_update_loop.before_loop
@recovery_loop.before_loop
async def _wait_ready():
    await bot.wait_until_ready()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not cfg.token or cfg.token == "YOUR_BOT_TOKEN":
        raise SystemExit("Discord token missing: set it in config.json or DISCORD_TOKEN")
    bot.run(cfg.token, log_handler=None)


if __name__ == "__main__":
    main()
