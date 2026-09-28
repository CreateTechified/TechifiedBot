import io

import discord
from discord.ext import commands
from discord.commands import SlashCommandGroup, Option

from slash_cog import is_admin
from page_embeds import send_paged

MAX_TRIGGER_LENGTH = 100
MAX_REPLY_LENGTH = 2000


class AutoReply(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._cache = {}

    wss_group = SlashCommandGroup("wss", "Automatic reply triggers (admin only)")

    async def _get_replies(self, guild_id: int):
        cached = self._cache.get(guild_id)
        if cached is not None:
            return cached

        async with self.bot.tag_db.execute(
            "SELECT trigger_text, reply_text FROM auto_replies "
            "WHERE guild = ? ORDER BY LENGTH(trigger_text) DESC",
            (guild_id,)
        ) as cursor:
            rows = await cursor.fetchall()

        self._cache[guild_id] = rows
        return rows

    # ---------- commands ----------

    @wss_group.command(name="reply", description="Make the bot reply whenever a message contains some text. Do NOT abuse this :D")
    @is_admin()
    async def wss_reply(
        self, ctx,
        when_someone_says: Option(str, "Text to look for anywhere in a message (not case sensitive)"),
        reply_with: Option(str, "What the bot should reply with"),
    ):
        trigger = when_someone_says.strip().lower()
        reply = reply_with.strip()

        if not trigger or not reply:
            await ctx.respond("❌ Both fields need some text.", ephemeral=True)
            return

        if len(trigger) > MAX_TRIGGER_LENGTH:
            await ctx.respond(f"❌ The trigger can be at most {MAX_TRIGGER_LENGTH} characters.", ephemeral=True)
            return

        if len(reply) > MAX_REPLY_LENGTH:
            await ctx.respond(f"❌ The reply can be at most {MAX_REPLY_LENGTH} characters.", ephemeral=True)
            return

        await self.bot.tag_db.execute(
            "INSERT OR REPLACE INTO auto_replies (guild, trigger_text, reply_text, creator) VALUES (?, ?, ?, ?)",
            (ctx.guild.id, trigger, reply, ctx.author.id)
        )
        await self.bot.tag_db.commit()
        self._cache.pop(ctx.guild.id, None)

        await ctx.respond(
            f"✅ Whenever a message contains `{trigger}`, I'll reply with:\n> {reply}",
            allowed_mentions=discord.AllowedMentions.none()
        )

    @wss_group.command(name="remove", description="Remove an auto-reply trigger")
    @is_admin()
    async def wss_remove(
        self, ctx,
        trigger: Option(str, "The trigger text to remove (not case sensitive)"),
    ):
        trigger = trigger.strip().lower()

        cursor = await self.bot.tag_db.execute(
            "DELETE FROM auto_replies WHERE guild = ? AND trigger_text = ?",
            (ctx.guild.id, trigger)
        )
        await self.bot.tag_db.commit()
        removed = cursor.rowcount
        await cursor.close()

        if not removed:
            await ctx.respond(f"❌ Trigger `{trigger}` doesn't exists.", ephemeral=True)
            return

        self._cache.pop(ctx.guild.id, None)
        await ctx.respond(f"`{trigger}` Trigger Removed ✅")

    @wss_group.command(name="list", description="List every auto-reply trigger in the server")
    @is_admin()
    async def wss_list(self, ctx):
        async with self.bot.tag_db.execute(
            "SELECT trigger_text FROM auto_replies WHERE guild = ? ORDER BY trigger_text",
            (ctx.guild.id,)
        ) as cursor:
            rows = await cursor.fetchall()

        if not rows:
            await ctx.respond("No auto-reply triggers exist in this server yet.")
            return

        await send_paged(
            ctx, f"💬 Auto-reply triggers in {ctx.guild.name}",
            [f"`{row[0]}`" for row in rows],
            discord.Color.blurple(), noun="trigger(s)"
        )

    @wss_group.command(name="listlog", description="Get every trigger and its reply as a downloadable .txt file")
    @is_admin()
    async def wss_listlog(self, ctx):
        await ctx.defer()

        async with self.bot.tag_db.execute(
            "SELECT trigger_text, reply_text, creator FROM auto_replies WHERE guild = ? ORDER BY trigger_text",
            (ctx.guild.id,)
        ) as cursor:
            rows = await cursor.fetchall()

        if not rows:
            await ctx.respond("No auto-reply triggers exist in this server yet.")
            return

        blocks = [
            f"trigger: {trigger}\ncreator: {creator}\nreply: {reply}"
            for trigger, reply, creator in rows
        ]
        content = "\n\n---\n\n".join(blocks)
        buffer = io.BytesIO(content.encode("utf-8"))
        file = discord.File(fp=buffer, filename="auto_replies.txt")

        await ctx.respond(f"💬 {len(rows)} trigger(s) in {ctx.guild.name}:", file=file)

    # ---------- listener ----------

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not message.guild or not message.content:
            return

        replies = await self._get_replies(message.guild.id)
        if not replies:
            return

        content = message.content.lower()
        reply = next((r for trigger, r in replies if trigger in content), None)
        if reply is None:
            return

        ctx = await self.bot.get_context(message)
        if ctx.valid:
            return

        try:
            await message.reply(
                reply,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.HTTPException:
            pass


def setup(bot):
    bot.add_cog(AutoReply(bot))