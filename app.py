import time
import logging
from pathlib import Path
from datetime import timedelta
import os
import asyncio

import discord
from discord.ext import tasks
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


class StatusWatcher(discord.Client):
    def __init__(self, **kwargs):
        super().__init__(intents=intents, **kwargs)
        # store last known status for each monitored bot
        self.last_status: dict[int, str] = {}
        # store last time each user used /watch (cooldown)
        self.watch_cooldown: dict[int, float] = {}
        # store last time each user used !restart (cooldown)
        self.restart_cooldown: dict[int, float] = {}
        # global anti-spam: timestamp until which restart is considered active
        self.global_restart_until: float = 0.0

    # --------------------------------
    # Helpers
    # --------------------------------
    @staticmethod
    def make_channel_name(member: discord.Member) -> str:
        """
        Build a channel name from member.display_name.
        Use display name (nickname) not the global username.
        """
        base = member.display_name.strip()
        if not base:
            base = f"user-{member.id}"
        # spaces -> hyphens, lower for consistency
        name = base.replace(" ", "-").lower()
        # Discord limit ~100 chars, keep it shorter
        return name[:90]

    async def send_log_message(self, text: str):
        """Send important error/info to a dedicated log channel if configured."""
        if LOG_CHANNEL_ID == 0:
            return
        channel = self.get_channel(LOG_CHANNEL_ID)
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.send(f"⚠️ {text[:1900]}")
            except Exception as e:
                log.exception(f"Failed to send log message to log channel: {e}")

    async def send_restart_webhook(self) -> bool:
        if not RESTART_WEBHOOK_URL:
            await self.send_log_message(
                "RESTART_WEBHOOK_URL is not set; !restart webhook message was skipped."
            )
            return False

        bot_mention = self.user.mention if self.user else ""
        payload = {
            "content": f"restart {bot_mention}".strip(),
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

    async def restart_countdown(self, message: discord.Message, end_ts: int):
        try:
            await message.channel.send(
                "🔄 تم إرسال طلب إعادة التشغيل.\n"
                f"⏳ المتوقع الانتهاء: <t:{end_ts}:R>\n"
                f"{message.author.mention} من فضلك استنى لحد الوقت ده قبل ما تستخدم `!restart` تاني."
            )
        except Exception as e:
            log.exception(f"Failed to send countdown message: {e}")
            await self.send_log_message(f"Failed to send countdown message: {e}")
            return

    # --------------------------------
    # Events
    # --------------------------------
    async def on_ready(self):
        log.info(f"✅ Logged in as {self.user} (ID: {self.user.id})")
        log.info(f"Monitoring bots: {MONITORED_BOT_IDS}")

        guild = self.get_guild(GUILD_ID)
        if guild is None:
            log.warning(f"⚠️ Bot is not in guild {GUILD_ID}")
        else:
            log.info(f"✅ Connected to guild: {guild.name} ({guild.id})")

        channel = self.get_channel(STATUS_CHANNEL_ID)
        if isinstance(channel, discord.TextChannel):
            embed = discord.Embed(
                title="Status Watcher Online",
                description="✅ Status watcher bot started and is now monitoring configured bots.",
                color=discord.Color.green(),
            )
            embed.set_footer(text="Status Watcher")
            await channel.send(embed=embed)

        # start background loops if not already running
        if not self.check_status_loop.is_running():
            self.check_status_loop.start()

    async def on_message(self, message: discord.Message):
        # ignore bots (including this watcher)
        if message.author.bot:
            return

        content_raw = message.content.strip()
        content = content_raw.lower()

        guild = self.get_guild(GUILD_ID) or message.guild
        if guild is None:
            return

        # ----------------- /watch (with cooldown) -----------------
        if content in ("/watch", "!watch", "watch"):
            now = time.time()
            last = self.watch_cooldown.get(message.author.id, 0)
            if now - last < 10:  # 10 seconds cooldown
                remaining = int(10 - (now - last))
                await message.channel.send(
                    f"⏳ Please wait {remaining} more second(s) before using `/watch` again."
                )
                return
            self.watch_cooldown[message.author.id] = now

            if not MONITORED_BOT_IDS:
                await message.channel.send("ℹ️ No monitored bots are configured.")
                return

            offline_states = {"offline", "invisible", "not_in_guild"}

            embed = discord.Embed(
                title="Monitored Bots Status",
                description="Current presence for all configured bots:",
                color=discord.Color.blurple(),
            )
            embed.set_footer(text="Status Watcher • /watch")

            for bot_id in MONITORED_BOT_IDS:
                member = guild.get_member(bot_id)

                if member is None:
                    current_status = "not_in_guild"
                else:
                    current_status = str(member.status)

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

            await message.channel.send(embed=embed)
            return

        # ----------------- !restart (webhook + lightweight countdown) -----------------
        if content == "!restart":
            now = time.time()

            if now < self.global_restart_until:
                await message.channel.send(
                    "⚠️ فيه Restart شغال بالفعل.\n"
                    f"⏳ المتوقع الانتهاء: <t:{int(self.global_restart_until)}:R>"
                )
                return

            last = self.restart_cooldown.get(message.author.id, 0)
            if now - last < 5 * 60:
                await message.channel.send(
                    f"⏳ {message.author.mention} you already requested a restart. Please wait a bit."
                )
                return

            ok = await self.send_restart_webhook()
            if not ok:
                await message.channel.send(
                    "⚠️ حصلت مشكلة وأنا ببعت طلب الـ Restart. جرّب تاني بعد شوية."
                )
                return

            end_ts = int(now) + 5 * 60
            self.global_restart_until = float(end_ts)
            self.restart_cooldown[message.author.id] = now

            await self.restart_countdown(message, end_ts)
            return

        # ----------------- /bot-status (admins only) -----------------
        if content in ("/bot-status", "!bot-status", "bot-status"):
            if not message.author.guild_permissions.administrator:
                await message.channel.send(
                    "⛔ This command is for admins only."
                )
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
                if member is None:
                    current_status = "not_in_guild"
                else:
                    current_status = str(member.status)

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

            await message.channel.send(embed=embed)
            return

    # --------------------------
    # Monitoring loop for bots
    # --------------------------
    @tasks.loop(seconds=CHECK_INTERVAL)
    async def check_status_loop(self):
        guild = self.get_guild(GUILD_ID)
        if guild is None:
            log.warning("Guild not found, skipping bot status check cycle.")
            return

        channel = self.get_channel(STATUS_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            log.warning("Status channel not found, skipping bot status messages.")
            return

        offline_states = {"offline", "invisible", "not_in_guild"}

        for bot_id in MONITORED_BOT_IDS:
            member = guild.get_member(bot_id)

            if member is None:
                # bot is not in guild or not visible
                current_status = "not_in_guild"
            else:
                # online / offline / idle / dnd / invisible
                current_status = str(member.status)

            previous_status = self.last_status.get(bot_id)

            # first time we see this bot -> just store the status
            if previous_status is None:
                self.last_status[bot_id] = current_status
                log.info(f"Initial status for {bot_id}: {current_status}")
                continue

            # no change
            if current_status == previous_status:
                continue

            # there is a change
            self.last_status[bot_id] = current_status
            await self.handle_status_change(
                channel, bot_id, previous_status, current_status, offline_states
            )

    @check_status_loop.before_loop
    async def before_check_status_loop(self):
        await self.wait_until_ready()
        log.info("Starting bot status check loop...")

    # --------------------------
    # Handle status changes
    # --------------------------
    async def handle_status_change(
        self,
        channel: discord.TextChannel,
        bot_id: int,
        old_status: str,
        new_status: str,
        offline_states: set,
    ):
        bot_mention = f"<@{bot_id}>"

        is_now_offline = new_status in offline_states
        was_offline = old_status in offline_states

        # Went ONLINE
        if not is_now_offline and was_offline:
            log.info(f"Bot {bot_id} went ONLINE: {old_status} -> {new_status}")

            embed = discord.Embed(
                title="Bot Online",
                description=f"🟢 {bot_mention} is now **Online**.",
                color=discord.Color.green(),
            )
            embed.set_footer(text="Status Watcher")
            await channel.send(embed=embed)

        # Went OFFLINE
        elif is_now_offline and not was_offline:
            log.info(f"Bot {bot_id} went OFFLINE: {old_status} -> {new_status}")

            # build admin mentions (if configured)
            admin_mentions = (
                " ".join(f"<@{admin_id}>" for admin_id in ADMIN_IDS)
                if ADMIN_IDS
                else ""
            ).strip()

            # content (outside embed) → this will actually ping
            if admin_mentions:
                content = f"{admin_mentions} 🔴 {bot_mention} is now **Offline / Sleeping**."
            else:
                content = f"🔴 {bot_mention} is now **Offline / Sleeping**."

            # embed just for nice formatting (no need ل mentions جوه الوصف)
            embed = discord.Embed(
                title="Bot Offline",
                description="A monitored bot just went offline.",
                color=discord.Color.red(),
            )
            embed.set_footer(text="Status Watcher")

            await channel.send(content=content, embed=embed)

        # Other transitions (idle/dnd/online<->idle) are ignored
        else:
            log.info(
                f"Ignored minor state change: {bot_id} {old_status} -> {new_status}"
            )
            return


# --------------------------
# Run the bot
# --------------------------
def main():
    client = StatusWatcher()
    client.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
