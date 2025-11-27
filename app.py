import os
import time
import logging
from pathlib import Path
from datetime import timedelta

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
        # timestamps of room creations (for last 24h stats)
        self.room_creation_times: list[float] = []

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

    async def ensure_member_room(
        self,
        guild: discord.Guild,
        category: discord.CategoryChannel,
        member: discord.Member,
        existing_names: set[str] | None = None,
    ) -> bool:
        """
        Ensure that a given member has a personal room inside the given category.
        Returns True if a room was created, False otherwise.
        Applies all ignore rules:
        - skip bots
        - skip admins
        - skip members with IGNORE_ROLE_NAMES
        """
        # skip bots
        if member.bot:
            return False

        # skip admins
        if member.guild_permissions.administrator:
            return False

        # skip members who have roles we want to ignore (L or bot)
        member_role_names = {r.name for r in member.roles}
        if member_role_names & IGNORE_ROLE_NAMES:
            return False

        # compute channel name
        channel_name = self.make_channel_name(member)

        if existing_names is None:
            existing_names = {
                ch.name
                for ch in category.channels
                if isinstance(ch, discord.TextChannel)
            }

        # already has a room with this name
        if channel_name in existing_names:
            return False

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

        # SS role (if exists)
        ss_role = discord.utils.get(guild.roles, name=SS_ROLE_NAME)

        try:
            channel = await category.create_text_channel(
                name=channel_name,
                overwrites=overwrites,
                reason="Auto-create personal room for bot usage",
            )
            existing_names.add(channel.name)
            self.room_creation_times.append(time.time())

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
                    await self.send_log_message(
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

            log.info(f"Created personal room '{channel.name}' for {member} ({member.id})")
            return True

        except Exception as e:
            log.exception(f"Failed to create channel for {member} ({member.id}): {e}")
            await self.send_log_message(
                f"Failed to create channel for {member} ({member.id}): {e}"
            )
            return False

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
        if not self.archive_old_rooms_loop.is_running():
            self.archive_old_rooms_loop.start()

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

        # ----------------- /room -----------------
        if content in ("/room", "!room", "room"):
            category = self.get_channel(SS_CLASS_CATEGORY_ID)
            if not isinstance(category, discord.CategoryChannel):
                await message.channel.send(
                    "⚠️ Room category is not configured correctly."
                )
                return

            # Admin: trigger full scan immediately
            if message.author.guild_permissions.administrator:
                await message.channel.send(
                    "🛠 Running full room scan now (admin-triggered `/room`)."
                )

                existing_names = {
                    ch.name
                    for ch in category.channels
                    if isinstance(ch, discord.TextChannel)
                }
                created_count = 0
                for member in guild.members:
                    created = await self.ensure_member_room(
                        guild, category, member, existing_names
                    )
                    if created:
                        created_count += 1

                await message.channel.send(
                    f"✅ Room scan finished. Created **{created_count}** new room(s)."
                )
            else:
                # Normal user: only ensure their own room
                existing_names = {
                    ch.name
                    for ch in category.channels
                    if isinstance(ch, discord.TextChannel)
                }
                created = await self.ensure_member_room(
                    guild, category, message.author, existing_names
                )
                if created:
                    await message.channel.send(
                        "✅ Your personal room has been created under `SS-Class`."
                    )
                else:
                    await message.channel.send(
                        "ℹ️ You already have a room or you are not eligible for one."
                    )
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

            # SS-Class rooms count
            ss_category = self.get_channel(SS_CLASS_CATEGORY_ID)
            rooms_count = 0
            if isinstance(ss_category, discord.CategoryChannel):
                rooms_count = sum(
                    1
                    for ch in ss_category.channels
                    if isinstance(ch, discord.TextChannel)
                )

            embed.add_field(
                name="Current SS-Class rooms",
                value=str(rooms_count),
                inline=True,
            )

            # rooms created in last 24h
            now = time.time()
            last_24h = sum(
                1 for t in self.room_creation_times if now - t <= 24 * 3600
            )
            embed.add_field(
                name="Rooms created last 24h",
                value=str(last_24h),
                inline=True,
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
                content = f"{admin_mentions} is now **Offline / Sleeping**."
            else:
                content = f"{bot_mention} is now **Offline / Sleeping**."

            # embed just for nice formatting (no need ل mentions جوه الوصف)
            embed = discord.Embed(
                title=f"🔴 Bot Offline {bot_mention",
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
    # User room loop (every N hours)
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

        existing_names = {
            ch.name for ch in category.channels if isinstance(ch, discord.TextChannel)
        }

        created_count = 0

        for member in guild.members:
            created = await self.ensure_member_room(
                guild, category, member, existing_names
            )
            if created:
                created_count += 1

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
    # Archive old rooms loop (daily)
    # --------------------------
    @tasks.loop(hours=24)
    async def archive_old_rooms_loop(self):
        await self.wait_until_ready()

        if SS_ARCHIVE_CATEGORY_ID == 0:
            # no archive category configured
            return

        guild = self.get_guild(GUILD_ID)
        if guild is None:
            log.warning("Guild not found, skipping archive check cycle.")
            return

        src_category = self.get_channel(SS_CLASS_CATEGORY_ID)
        dst_category = self.get_channel(SS_ARCHIVE_CATEGORY_ID)

        if not isinstance(src_category, discord.CategoryChannel):
            log.warning(
                f"Source category with ID {SS_CLASS_CATEGORY_ID} not found or not a category."
            )
            return
        if not isinstance(dst_category, discord.CategoryChannel):
            log.warning(
                f"Archive category with ID {SS_ARCHIVE_CATEGORY_ID} not found or not a category."
            )
            return

        now = discord.utils.utcnow()
        threshold = now - timedelta(days=ARCHIVE_INACTIVE_DAYS)

        archived_count = 0

        for channel in src_category.channels:
            if not isinstance(channel, discord.TextChannel):
                continue

            # get last message in channel
            last_message = None
            try:
                async for msg in channel.history(limit=1, oldest_first=False):
                    last_message = msg
                    break
            except Exception as e:
                log.exception(
                    f"Failed to fetch history for channel {channel.name} ({channel.id}): {e}"
                )
                await self.send_log_message(
                    f"Failed to fetch history for channel {channel.name} ({channel.id}): {e}"
                )
                continue

            if last_message is not None:
                last_ts = last_message.created_at
            else:
                # no messages at all -> use channel creation time
                last_ts = channel.created_at

            if last_ts >= threshold:
                # still active within last ARCHIVE_INACTIVE_DAYS
                continue

            # move channel to archive category
            try:
                await channel.edit(
                    category=dst_category,
                    reason=f"Inactive for {ARCHIVE_INACTIVE_DAYS} days, moving to archive.",
                )
                archived_count += 1
                log.info(
                    f"Archived channel {channel.name} ({channel.id}) due to inactivity."
                )
            except Exception as e:
                log.exception(
                    f"Failed to archive channel {channel.name} ({channel.id}): {e}"
                )
                await self.send_log_message(
                    f"Failed to archive channel {channel.name} ({channel.id}): {e}"
                )

        if archived_count > 0:
            await self.send_log_message(
                f"Archived {archived_count} old SS-Class channel(s) due to inactivity."
            )
        log.info(
            f"Archive check finished. Archived {archived_count} channel(s) from SS-Class."
        )

    @archive_old_rooms_loop.before_loop
    async def before_archive_old_rooms_loop(self):
        await self.wait_until_ready()
        log.info(
            f"Starting archive old rooms loop (runs daily, archives after {ARCHIVE_INACTIVE_DAYS} days of inactivity)..."
        )


# --------------------------
# Run the bot
# --------------------------
def main():
    client = StatusWatcher()
    client.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()

