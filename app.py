import os
import asyncio
import logging
from pathlib import Path

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
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")

# IDs of the bots to monitor (comma-separated in env)
MONITORED_BOT_IDS = [
    int(x.strip()) for x in os.getenv("MONITORED_BOT_IDS", "").split(",") if x.strip()
]

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "30"))

if not DISCORD_TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN is not set. "
        "Set it in a local .env file for development, or as an Environment Variable on Railway."
    )

# --------------------------
# Discord intents
# --------------------------
intents = discord.Intents.default()
intents.members = True      # needed to get members/bots
intents.presences = True    # needed for presence (online/offline)
intents.guilds = True


class StatusWatcher(discord.Client):
    def __init__(self, **kwargs):
        super().__init__(intents=intents, **kwargs)
        # store last known status for each monitored bot
        self.last_status: dict[int, str] = {}
        self.session: aiohttp.ClientSession | None = None

    async def setup_hook(self):
        # HTTP session for webhook
        self.session = aiohttp.ClientSession()
        # start the monitoring loop after the bot is ready
        self.check_status_loop.start()

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

    async def close(self):
        if self.session:
            await self.session.close()
        await super().close()

    # --------------------------
    # Monitoring loop
    # --------------------------
    @tasks.loop(seconds=CHECK_INTERVAL)
    async def check_status_loop(self):
        guild = self.get_guild(GUILD_ID)
        if guild is None:
            log.warning("Guild not found, skipping check cycle.")
            return

        channel = self.get_channel(STATUS_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            log.warning("Status channel not found, skipping messages.")
            return

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
                channel, bot_id, previous_status, current_status
            )

    @check_status_loop.before_loop
    async def before_check_status(self):
        await self.wait_until_ready()
        log.info("Starting status check loop...")

    # --------------------------
    # Handle status changes
    # --------------------------
    async def handle_status_change(
        self,
        channel: discord.TextChannel,
        bot_id: int,
        old_status: str,
        new_status: str,
    ):
        bot_mention = f"<@{bot_id}>"

        def pretty_status(status: str) -> str:
            if status == "not_in_guild":
                return "Not in guild / unreachable"
            return status.capitalize()

        # consider anything that is not offline as "online-ish"
        is_now_offline = new_status in ("offline", "invisible", "not_in_guild")
        was_offline = old_status in ("offline", "invisible", "not_in_guild")

        # choose style depending on transition
        if not is_now_offline and was_offline:
            # went ONLINE
            title = "Bot is back online"
            emoji = "🟢"
            color = discord.Color.green()
            description = (
                f"{emoji} {bot_mention} is now **Online**.\n"
                f"New status: `{pretty_status(new_status)}`"
            )
        elif is_now_offline and not was_offline:
            # went OFFLINE
            title = "Bot went offline"
            emoji = "🔴"
            color = discord.Color.red()
            description = (
                f"{emoji} {bot_mention} is now **Offline / Sleeping**.\n"
                f"New status: `{pretty_status(new_status)}`"
            )
        else:
            # other transitions (idle <-> dnd <-> online)
            title = "Bot status changed"
            emoji = "🟡"
            color = discord.Color.yellow()
            description = (
                f"{emoji} {bot_mention} changed status.\n"
                f"Old: `{pretty_status(old_status)}` → New: `{pretty_status(new_status)}`"
            )

        # log
        log.info(f"{title}: {bot_mention} {old_status} -> {new_status}")

        # create embed
        embed = discord.Embed(
            title=title,
            description=description,
            color=color,
        )
        embed.add_field(
            name="Previous status",
            value=f"`{pretty_status(old_status)}`",
            inline=True,
        )
        embed.add_field(
            name="Current status",
            value=f"`{pretty_status(new_status)}`",
            inline=True,
        )
        embed.set_footer(text="Status Watcher")

        # send to Discord channel
        await channel.send(embed=embed)

        # also send a simple text line to the webhook (optional)
        if WEBHOOK_URL and self.session:
            try:
                webhook_message = f"{emoji} {bot_mention} status changed: `{old_status}` → `{new_status}`"
                payload = {
                    "content": webhook_message,
                    "allowed_mentions": {"parse": ["users"]},
                }
                async with self.session.post(WEBHOOK_URL, json=payload) as resp:
                    if resp.status >= 400:
                        text = await resp.text()
                        log.warning(f"Webhook error {resp.status}: {text}")
            except Exception as e:
                log.exception(f"Failed to POST to webhook: {e}")


# --------------------------
# Run the bot
# --------------------------
def main():
    client = StatusWatcher()
    client.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
