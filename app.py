import os
import asyncio
import logging
from pathlib import Path

import discord
from discord.ext import tasks
from dotenv import load_dotenv
import aiohttp

# --------------------------
# إعداد اللوج
# --------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
)

log = logging.getLogger("status_watcher")

# --------------------------
# قراءة .env (لو موجود)
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

# IDs البوتات اللي هتتراقب
MONITORED_BOT_IDS = [
    int(x.strip()) for x in os.getenv("MONITORED_BOT_IDS", "").split(",") if x.strip()
]

# IDs الأدمنز اللي هيتمنشنوا لو بوت بقى Offline
ADMIN_IDS = [
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()
]

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "30"))

if not DISCORD_TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN is not set. "
        "Set it in a local .env file for development, or as an Environment Variable on Railway."
    )

# --------------------------
# إعداد البوت (intents)
# --------------------------
intents = discord.Intents.default()
intents.members = True  # عشان نجيب حالة البوتات
intents.presences = True  # presence (online/offline)
intents.guilds = True


class StatusWatcher(discord.Client):
    def __init__(self, **kwargs):
        super().__init__(intents=intents, **kwargs)
        # نخزن آخر حالة لكل بوت
        self.last_status = {}
        self.session = None

    async def setup_hook(self):
        # Session للويبهوك
        self.session = aiohttp.ClientSession()
        # نبدأ اللوب بعد ما البوت يجهز
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
        if channel:
            await channel.send("✅ **Status watcher bot started.**")

    async def close(self):
        # نغلق جلسة الويبهوك
        if self.session:
            await self.session.close()
        await super().close()

    # --------------------------
    # لوب المراقبة
    # --------------------------
    @tasks.loop(seconds=CHECK_INTERVAL)
    async def check_status_loop(self):
        guild = self.get_guild(GUILD_ID)
        if guild is None:
            log.warning("Guild not found, skipping check cycle.")
            return

        channel = self.get_channel(STATUS_CHANNEL_ID)
        if channel is None:
            log.warning("Status channel not found, skipping messages.")
            return

        for bot_id in MONITORED_BOT_IDS:
            member = guild.get_member(bot_id)
            if member is None:
                # البوت مش في السيرفر أو مش متشاف
                current_status = "not_in_guild"
            else:
                # status ممكن يكون online / offline / idle / dnd / invisible
                current_status = str(member.status)  # تحويل لنص

            previous_status = self.last_status.get(bot_id)

            # أول مرة نشوفه → بس نخزن الحالة
            if previous_status is None:
                self.last_status[bot_id] = current_status
                log.info(f"Initial status for {bot_id}: {current_status}")
                continue

            # لو مفيش تغيير → skip
            if current_status == previous_status:
                continue

            # حصل تغيير
            self.last_status[bot_id] = current_status
            await self.handle_status_change(
                channel, bot_id, previous_status, current_status
            )

    @check_status_loop.before_loop
    async def before_check_status(self):
        await self.wait_until_ready()
        log.info("Starting status check loop...")

    # --------------------------
    # إرسال الرسائل عند التغيير
    # --------------------------
    async def handle_status_change(
        self,
        channel: discord.TextChannel,
        bot_id: int,
        old_status: str,
        new_status: str,
    ):
        bot_mention = f"<@{bot_id}>"

        # هنعتبر كل الحالات غير offline = Online
        is_now_offline = (
            new_status == "offline"
            or new_status == "invisible"
            or new_status == "not_in_guild"
        )
        was_offline = (
            old_status == "offline"
            or old_status == "invisible"
            or old_status == "not_in_guild"
        )

        # لو بقى Online بعد ما كان Offline
        if not is_now_offline and was_offline:
            msg = f"🟢 البوت {bot_mention} رجع **Online** (الحالة الجديدة: `{new_status}`)."
        # لو بقى Offline
        elif is_now_offline and not was_offline:
            admin_mentions = (
                " ".join(f"<@{admin_id}>" for admin_id in ADMIN_IDS)
                if ADMIN_IDS
                else ""
            )
            msg = (
                f"🔴 البوت {bot_mention} بقى **Offline/Sleep** "
                f"(الحالة الجديدة: `{new_status}`). {admin_mentions}".strip()
            )
        else:
            # تغيير بين idle/dnd/online → نكتب رسالة أبسط
            msg = (
                f"ℹ️ حالة البوت {bot_mention} اتغيرت من `{old_status}` "
                f"إلى `{new_status}`."
            )

        log.info(msg)
        # إرسال للقناة
        await channel.send(msg)

        # إرسال كمان للويبهوك (اختياري)
        if WEBHOOK_URL:
            try:
                payload = {"content": msg, "allowed_mentions": {"parse": ["users"]}}
                async with self.session.post(WEBHOOK_URL, json=payload) as resp:
                    if resp.status >= 400:
                        text = await resp.text()
                        log.warning(f"Webhook error {resp.status}: {text}")
            except Exception as e:
                log.exception(f"Failed to POST to webhook: {e}")


# --------------------------
# تشغيل البوت
# --------------------------
def main():
    client = StatusWatcher()
    client.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
