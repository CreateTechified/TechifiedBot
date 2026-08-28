import time
import difflib
from collections import defaultdict
from datetime import timedelta

import discord
from discord.ext import commands

# --- CONFIGURATION ---
HONEYPOT_CHANNEL_ID = 1512206288631627927
HONEYPOT_LOG_CHANNEL_ID = 1472650884906221771

SPAM_WINDOW_SECONDS = 3.0
SPAM_MESSAGE_THRESHOLD = 3
SPAM_SIMILARITY_RATIO = 0.85
BASE_TIMEOUT_SECONDS = 10


class AntiSpam(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        if not HONEYPOT_CHANNEL_ID or not HONEYPOT_LOG_CHANNEL_ID:
            print("⚠️ WARNING: HONEYPOT_CHANNEL_ID / HONEYPOT_LOG_CHANNEL_ID not configured in automod_cog.py!")

        self.message_log = defaultdict(list)
        self.warning_counts = defaultdict(int)

    @staticmethod
    def _is_similar(a: str, b: str) -> bool:
        if not a and not b:
            return True
        if not a or not b:
            return False
        return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio() >= SPAM_SIMILARITY_RATIO

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not message.guild:
            return

        if message.channel.id == HONEYPOT_CHANNEL_ID:
            await self._handle_honeypot(message)
            return

        await self._handle_spam_check(message)

    # ---------- honeypot ----------

    async def _handle_honeypot(self, message):
        member = message.author
        if member.guild_permissions.manage_messages:
            return

        try:
            await message.delete()
        except discord.HTTPException:
            pass

        try:
            await member.kick(reason="Posted in honeypot channel")
            kicked = True
        except (discord.Forbidden, discord.HTTPException):
            kicked = False

        log_channel = self.bot.get_channel(HONEYPOT_LOG_CHANNEL_ID)
        if log_channel:
            embed = discord.Embed(
                title="🍯 Honeypot Triggered",
                description=(
                    f"**User:** {member.mention} (`{member.id}`)\n"
                    f"**Channel:** {message.channel.mention}\n"
                    f"**Kicked:** {'✅ Yes' if kicked else '❌ Failed (check bot permissions/role position)'}"
                ),
                color=discord.Color.dark_red()
            )
            if message.content:
                embed.add_field(name="Message content", value=message.content[:1024], inline=False)
            embed.timestamp = discord.utils.utcnow()
            await log_channel.send(embed=embed)

    # ---------- spam burst detection ----------

    async def _handle_spam_check(self, message):
        member = message.author
        if member.guild_permissions.manage_messages:
            return

        user_id = member.id
        now = time.monotonic()

        history = self.message_log[user_id]
        history.append((now, message.content))
        history[:] = [(t, c) for t, c in history if now - t <= SPAM_WINDOW_SECONDS]

        similar_count = sum(1 for _, c in history if self._is_similar(c, message.content))
        if similar_count < SPAM_MESSAGE_THRESHOLD:
            return

        self.message_log[user_id] = []

        self.warning_counts[user_id] += 1
        count = self.warning_counts[user_id]

        try:
            await message.channel.send(
                f"⚠️ {member.mention}, please slow down — you're sending messages too quickly. "
                f"(Warning {count}/3)",
                delete_after=10
            )
        except discord.HTTPException:
            pass

        if count < 3:
            return

        duration = BASE_TIMEOUT_SECONDS * (2 ^ (count - 3))
        try:
            until = discord.utils.utcnow() + timedelta(seconds=duration)
            await member.timeout(until, reason=f"Automated spam detection (warning #{count})")
            await message.channel.send(
                f"🔇 {member.mention} has been timed out for {duration} second(s) for repeated spam."
            )
        except (discord.Forbidden, discord.HTTPException):
            pass


def setup(bot):
    bot.add_cog(AntiSpam(bot))