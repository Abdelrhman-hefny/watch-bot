import os
import logging
from pathlib import Path

import discord
from discord.ext import tasks
from dotenv import load_dotenv

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
# Example:
# MONITORED_BOT_IDS=1438492783752118353,1432266312138358784
MONITORED_BOT_IDS = [
    int(x.strip()) for x in os.getenv("MONITORED_BOT_IDS", "").split(",") if x.strip()
]

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "30"))

# Category for per-user rooms (SS-Class)
SS_CLASS_CATEGORY_ID = int(os.getenv("SS_CLASS_CATEGORY_ID", "1442883219971375321"))

# Role names that should be IGNORED when creating rooms
# (L, bot) as you requested
IGNORE_ROLE_NAMES = {"L", "bot"}

# Role name to give to users when a room is created
SS_ROLE_NAME = os.getenv("SS_ROLE_NAME", "SS")

# Interval (hours) for user-room check
USER_ROOM_CHECK_INTERVAL_HOURS = int(
    os.getenv("USER_ROOM_CHECK_INTERVAL_HOURS", "12")
)

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
intents.message_content = True  # needed to read /watch text commands


class StatusWatcher(discord.Client):
    def __init__(self, **kwargs):
        super().__init__(intents=intents, **kwargs)
        # store last known status for each monitored bot
        self.last_status: dict[int, str] = {}

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
        if not self.user_room_check_loop.is_running():
            self.user_room_check_loop.start()

    async def on_message(self, message: discord.Message):
        # ignore bots (including this watcher)
        if message.author.bot:
            return

        content = message.content.strip().lower()
        if content not in ("/watch", "!watch", "watch"):
            return

        # choose guild: prefer configured GUILD_ID, fallback to message.guild
        guild = self.get_guild(GUILD_ID) or message.guild
        if guild is None:
            await message.channel.send("⚠️ I could not determine the guild to inspect.")
            return

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
                current_status = str(member.status)  # online / offline / idle / dnd / invisible

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

            embed = discord.Embed(
                title="Bot Offline",
                description=f"🔴 {bot_mention} is now **Offline / Sleeping**.",
                color=discord.Color.red(),
            )
            embed.set_footer(text="Status Watcher")
            await channel.send(embed=embed)

        # Other transitions (idle/dnd/online<->idle) are ignored
        else:
            log.info(
                f"Ignored minor state change: {bot_id} {old_status} -> {new_status}"
            )
            return

    # --------------------------
    # User room loop (every 12h by default)
    # --------------------------
    @tasks.loop(hours=USER_ROOM_CHECK_INTERVAL_HOURS)
    async def user_room_check_loop(self):
        await self.wait_until_ready()

        guild = self.get_guild(GUILD_ID)
        if guild is None:
            log.warning("Guild not found, skipping user room check cycle.")
            return

        category = self.get_channel(SS_CLASS_CATEGORY_ID)
        if not isinstance(category, discord.CategoryChannel):
            log.warning(
                f"Category with ID {SS_CLASS_CATEGORY_ID} not found or not a category."
            )
            return

        # existing channel names in this category
        existing_names = {
            ch.name for ch in category.channels if isinstance(ch, discord.TextChannel)
        }

        # SS role (if exists)
        ss_role = discord.utils.get(guild.roles, name=SS_ROLE_NAME)
        if ss_role is None:
            log.warning(
                f"Role '{SS_ROLE_NAME}' not found in guild. "
                "Users will not receive this role automatically."
            )

        created_count = 0

        for member in guild.members:
            # skip bots
            if member.bot:
                continue

            # skip admins
            if member.guild_permissions.administrator:
                continue

            # skip members who have roles we want to ignore (L or bot)
            member_role_names = {r.name for r in member.roles}
            if member_role_names & IGNORE_ROLE_NAMES:
                continue

            # build intended channel name from display name
            channel_name = self.make_channel_name(member)

            # if they already have a channel in this category -> skip
            if channel_name in existing_names:
                continue

            # permissions: only this member + bot (and admins implicitly)
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                member: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                ),
                guild.me: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                ),
            }

            try:
                channel = await category.create_text_channel(
                    name=channel_name,
                    overwrites=overwrites,
                    reason="Auto-create personal room for bot usage",
                )
                existing_names.add(channel.name)
                created_count += 1

                # give SS role if they don't already have it
                if ss_role and ss_role not in member.roles:
                    try:
                        await member.add_roles(
                            ss_role, reason="Auto-assign SS role for personal room"
                        )
                    except Exception as e:
                        log.exception(
                            f"Failed to add SS role to {member} ({member.id}): {e}"
                        )

                # send welcome / commands
                await channel.send(
                    f"Hi {member.mention}! 👋\n\n"
                    "This is your personal channel to use the OCR bots.\n\n"
                    "**Useful commands:**\n"
                    "• `/clean` – clean pages.\n"
                    "• `/extract` – extract text with OCR.\n"
                    "• `/translate` – translate extracted text.\n"
                )

            except Exception as e:
                log.exception(
                    f"Failed to create channel for {member} ({member.id}): {e}"
                )

        log.info(
            f"User room check finished. Created {created_count} new channels in SS-Class."
        )

    @user_room_check_loop.before_loop
    async def before_user_room_check_loop(self):
        await self.wait_until_ready()
        log.info(
            f"Starting user room check loop every {USER_ROOM_CHECK_INTERVAL_HOURS} hours..."
        )


# --------------------------
# Run the bot
# --------------------------
def main():
    client = StatusWatcher()
    client.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
