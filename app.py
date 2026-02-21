import time
import logging
from pathlib import Path
import os
import asyncio
import json

import discord
from discord.ext import commands
from dotenv import load_dotenv
import aiohttp

# --------------------------
# Logging setup
# --------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("status_watcher")

# --------------------------
# Load .env locally (if exists)
# --------------------------
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    log.info(f"📄 .env file found at {env_path}, loading it...")
    load_dotenv(env_path)
else:
    log.info("ℹ️ No .env file found, relying on system environment variables only.")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
STATUS_CHANNEL_ID = int(os.getenv("STATUS_CHANNEL_ID", "0"))

# IDs of the bots to monitor (comma-separated in env)
# Example: MONITORED_BOT_IDS=1438492783752118353,1432266312138358784
MONITORED_BOT_IDS = [
    int(x.strip()) for x in os.getenv("MONITORED_BOT_IDS", "").split(",") if x.strip()
]

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "30"))

# Category for per-user rooms (SS-Class)
SS_CLASS_CATEGORY_ID = int(os.getenv("SS_CLASS_CATEGORY_ID", "1442883219971375321"))

# Archive category for old rooms
SS_ARCHIVE_CATEGORY_ID = int(os.getenv("SS_ARCHIVE_CATEGORY_ID", "0"))

# Role names that should be IGNORED when creating rooms (typically admins/bots helpers)
IGNORE_ROLE_NAMES = {"L", "bot"}

# Role name to give to users when a room is created
SS_ROLE_NAME = os.getenv("SS_ROLE_NAME", "SS")

# Interval (hours) for user-room check
USER_ROOM_CHECK_INTERVAL_HOURS = int(
    os.getenv("USER_ROOM_CHECK_INTERVAL_HOURS", "12")
)

# How many days of inactivity before archiving a room
ARCHIVE_INACTIVE_DAYS = int(os.getenv("ARCHIVE_INACTIVE_DAYS", "10"))

# Log channel for important errors
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))

RESTART_WEBHOOK_URL = "https://discord.com/api/webhooks/1468613841528033473/TUa9LqfYmb5msk0nwvu1ydZgv9e-67XauL7P7c4ple2vuao9Hj4D46qEa6byAC5gPDo6"
RESTART_NOTIFY_USER_ID = 1339222260904366092

# Admin IDs to mention when a monitored bot goes offline
ADMIN_IDS = [
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()
]

if not DISCORD_TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN is not set. "
        "Set it in a local .env file for development, or as an Environment Variable on Railway."
    )

if not MONITORED_BOT_IDS:
    log.warning(
        "No MONITORED_BOT_IDS configured. The watcher will run but not monitor any bots."
    )

# --------------------------
# Discord intents
# --------------------------
intents = discord.Intents.default()
intents.members = True          # needed to get members/bots
intents.presences = True        # needed for presence (online/offline)
intents.guilds = True
intents.message_content = True  # needed to read text commands


STATUS_CACHE_PATH = Path(__file__).parent / "last_status.json"


class StatusWatcherBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=("!", "/"), intents=intents)
        self.last_status: dict[int, str] = {}
        self.global_restart_until: float = 0.0
        self._load_last_status()

    def _load_last_status(self):
        try:
            if STATUS_CACHE_PATH.exists():
                data = json.loads(STATUS_CACHE_PATH.read_text(encoding="utf-8") or "{}")
                if isinstance(data, dict):
                    self.last_status = {int(k): str(v) for k, v in data.items()}
        except Exception as e:
            log.exception(f"Failed to load last_status cache: {e}")

    def _save_last_status(self):
        try:
            STATUS_CACHE_PATH.write_text(
                json.dumps(self.last_status, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            log.exception(f"Failed to save last_status cache: {e}")

    async def send_log_message(self, text: str):
        if LOG_CHANNEL_ID == 0:
            return
        channel = self.get_channel(LOG_CHANNEL_ID)
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.send(f"⚠️ {text[:1900]}")
            except Exception as e:
                log.exception(f"Failed to send log message to log channel: {e}")

    async def send_restart_webhook(
        self,
        target_mention: str,
        requested_by_mention: str,
    ) -> bool:
        if not RESTART_WEBHOOK_URL:
            await self.send_log_message(
                "RESTART_WEBHOOK_URL is not set; restart webhook message was skipped."
            )
            return False

        notify_mention = f"<@{RESTART_NOTIFY_USER_ID}>" if RESTART_NOTIFY_USER_ID else ""
        payload = {
            "content": f"restart {target_mention} | requested by {requested_by_mention} {notify_mention}".strip(),
            "allowed_mentions": {"parse": ["users", "roles", "everyone"]},
        }

        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(RESTART_WEBHOOK_URL, json=payload) as resp:
                    if resp.status not in (200, 204):
                        body = (await resp.text())[:1000]
                        raise RuntimeError(f"Webhook HTTP {resp.status}: {body}")
            return True
        except Exception as e:
            log.exception(f"Failed to send restart webhook: {e}")
            await self.send_log_message(f"Failed to send restart webhook: {e}")
            return False

    async def handle_status_change(
        self,
        channel: discord.TextChannel,
        bot_id: int,
        old_status: str,
        new_status: str,
        offline_states: set[str],
    ):
        bot_mention = f"<@{bot_id}>"
        is_now_offline = new_status in offline_states
        was_offline = old_status in offline_states

        if not is_now_offline and was_offline:
            log.info(f"Bot {bot_id} went ONLINE: {old_status} -> {new_status}")
            embed = discord.Embed(
                title="Bot Online",
                description=f"🟢 {bot_mention} is now **Online**.",
                color=discord.Color.green(),
            )
            embed.set_footer(text="Status Watcher")
            await channel.send(embed=embed)
            return

        if is_now_offline and not was_offline:
            log.info(f"Bot {bot_id} went OFFLINE: {old_status} -> {new_status}")

            admin_mentions = (
                " ".join(f"<@{admin_id}>" for admin_id in ADMIN_IDS)
                if ADMIN_IDS
                else ""
            ).strip()

            if admin_mentions:
                content = f"{admin_mentions} 🔴 {bot_mention} is now **Offline / Sleeping**."
            else:
                content = f"🔴 {bot_mention} is now **Offline / Sleeping**."

            embed = discord.Embed(
                title="Bot Offline",
                description="A monitored bot just went offline.",
                color=discord.Color.red(),
            )
            embed.set_footer(text="Status Watcher")
            await channel.send(content=content, embed=embed)
            return

        log.info(f"Ignored minor state change: {bot_id} {old_status} -> {new_status}")


bot = StatusWatcherBot()


@bot.event
async def on_ready():
    log.info(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    log.info(f"Monitoring bots: {MONITORED_BOT_IDS}")

    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        log.warning(f"⚠️ Bot is not in guild {GUILD_ID}")
    else:
        log.info(f"✅ Connected to guild: {guild.name} ({guild.id})")

    channel = bot.get_channel(STATUS_CHANNEL_ID)
    if isinstance(channel, discord.TextChannel):
        embed = discord.Embed(
            title="Status Watcher Online",
            description="✅ Status watcher bot started and is now monitoring configured bots.",
            color=discord.Color.green(),
        )
        embed.set_footer(text="Status Watcher")
        await channel.send(embed=embed)

    if guild is not None:
        for bot_id in MONITORED_BOT_IDS:
            member = guild.get_member(bot_id)
            current_status = "not_in_guild" if member is None else str(member.status)
            if bot.last_status.get(bot_id) is None:
                bot.last_status[bot_id] = current_status
        bot._save_last_status()


@bot.event
async def on_presence_update(before: discord.Member, after: discord.Member):
    if after.guild is None or after.guild.id != GUILD_ID:
        return
    if after.id not in MONITORED_BOT_IDS:
        return

    channel = bot.get_channel(STATUS_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        return

    offline_states = {"offline", "invisible", "not_in_guild"}

    old_status = bot.last_status.get(after.id)
    new_status = str(after.status)

    if old_status is None:
        bot.last_status[after.id] = new_status
        bot._save_last_status()
        return

    if new_status == old_status:
        return

    bot.last_status[after.id] = new_status
    bot._save_last_status()
    await bot.handle_status_change(channel, after.id, old_status, new_status, offline_states)


@bot.event
async def on_member_remove(member: discord.Member):
    if member.guild is None or member.guild.id != GUILD_ID:
        return
    if member.id not in MONITORED_BOT_IDS:
        return

    bot.last_status[member.id] = "not_in_guild"
    bot._save_last_status()
    await bot.send_log_message(f"Monitored bot removed from guild: {member} ({member.id})")


@bot.command(name="watch")
@commands.cooldown(1, 10, commands.BucketType.user)
async def watch_cmd(ctx: commands.Context):
    guild = bot.get_guild(GUILD_ID) or ctx.guild
    if guild is None:
        return

    if not MONITORED_BOT_IDS:
        await ctx.send("ℹ️ No monitored bots are configured.")
        return

    offline_states = {"offline", "invisible", "not_in_guild"}

    embed = discord.Embed(
        title="Monitored Bots Status",
        description="Current presence for all configured bots:",
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="Status Watcher • watch")

    for bot_id in MONITORED_BOT_IDS:
        member = guild.get_member(bot_id)
        current_status = "not_in_guild" if member is None else str(member.status)

        if current_status in offline_states:
            label = "Offline / Sleeping"
            emoji = "🔴"
        elif current_status == "online":
            label = "Online"
            emoji = "🟢"
        elif current_status == "idle":
            label = "Idle"
            emoji = "🌙"
        elif current_status == "dnd":
            label = "Do Not Disturb"
            emoji = "⛔"
        else:
            label = current_status.capitalize()
            emoji = "❔"

        bot_mention = f"<@{bot_id}>"
        embed.add_field(
            name=bot_mention,
            value=f"{emoji} **{label}** (`{current_status}`)",
            inline=False,
        )

    await ctx.send(embed=embed)


@bot.command(name="bot-status")
@commands.has_permissions(administrator=True)
async def bot_status_cmd(ctx: commands.Context):
    guild = bot.get_guild(GUILD_ID) or ctx.guild
    if guild is None:
        return

    embed = discord.Embed(
        title="Bot Status Summary",
        color=discord.Color.gold(),
    )

    offline_states = {"offline", "invisible", "not_in_guild"}
    offline_list = []
    online_list = []

    for bot_id in MONITORED_BOT_IDS:
        member = guild.get_member(bot_id)
        current_status = "not_in_guild" if member is None else str(member.status)
        mention = f"<@{bot_id}>"
        if current_status in offline_states:
            offline_list.append(f"{mention} (`{current_status}`)")
        else:
            online_list.append(f"{mention} (`{current_status}`)")

    embed.add_field(
        name="Online Bots",
        value="\n".join(online_list) if online_list else "None",
        inline=False,
    )
    embed.add_field(
        name="Offline Bots / Not reachable",
        value="\n".join(offline_list) if offline_list else "None",
        inline=False,
    )

    await ctx.send(embed=embed)


@bot.command(name="restart")
@commands.cooldown(1, 5 * 60, commands.BucketType.user)
async def restart_cmd(ctx: commands.Context, target: discord.Member = None):
    now = time.time()

    target_mention = (
        target.mention
        if target is not None
        else (bot.user.mention if bot.user is not None else "")
    )

    if now < bot.global_restart_until:
        msg = await ctx.send(
            "⚠️ A restart request is already in progress.\n"
            f"⏳ Expected finish: <t:{int(bot.global_restart_until)}:R>"
        )
        try:
            await ctx.message.delete(delay=30)
        except Exception:
            pass
        return

    ok = await bot.send_restart_webhook(
        target_mention=target_mention,
        requested_by_mention=ctx.author.mention,
    )
    if not ok:
        ctx.command.reset_cooldown(ctx)
        msg = await ctx.send(
            "⚠️ Failed to send the restart request (webhook error). Please try again later."
        )
        try:
            await ctx.message.delete(delay=30)
        except Exception:
            pass
        return

    end_ts = int(now) + 5 * 60
    bot.global_restart_until = float(end_ts)

    msg = await ctx.send(
        "🔄 Restart request sent.\n"
        f"🎯 Target: {target_mention}\n"
        f"⏳ Expected finish: <t:{end_ts}:R>\n"
        f"{ctx.author.mention} please wait until then before using `!restart` again."
    )

    try:
        await ctx.message.delete(delay=30)
    except Exception:
        pass


@watch_cmd.error
async def watch_cmd_error(ctx: commands.Context, error: Exception):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(
            f"⏳ Please wait {int(error.retry_after)} second(s) before using this command again."
        )
        return
    raise error


@restart_cmd.error
async def restart_cmd_error(ctx: commands.Context, error: Exception):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(
            f"⏳ Please wait {int(error.retry_after)} second(s) before using this command again."
        )
        return
    raise error


@bot_status_cmd.error
async def bot_status_cmd_error(ctx: commands.Context, error: Exception):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("⛔ This command is for admins only.")
        return
    raise error


# --------------------------
# Run the bot
# --------------------------
def main():
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
