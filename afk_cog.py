import discord
from discord.ext import commands
from discord.commands import Option

AFK_PREFIX = "[AFK] "
MAX_NICK_LENGTH = 32


class AFK(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ---------- db helpers ----------

    async def _get_afk(self, guild_id: int, user_id: int):
        async with self.bot.tag_db.execute(
            "SELECT message, since, original_nick, tagged_nick FROM afk_status "
            "WHERE guild = ? AND user_id = ?",
            (guild_id, user_id)
        ) as cursor:
            return await cursor.fetchone()

    async def _set_afk(self, guild_id, user_id, message, since_iso, original_nick, tagged_nick):
        await self.bot.tag_db.execute(
            "INSERT OR REPLACE INTO afk_status (guild, user_id, message, since, original_nick, tagged_nick) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (guild_id, user_id, message, since_iso, original_nick, tagged_nick)
        )
        await self.bot.tag_db.commit()

    async def _clear_afk(self, guild_id: int, user_id: int):
        await self.bot.tag_db.execute(
            "DELETE FROM afk_status WHERE guild = ? AND user_id = ?", (guild_id, user_id)
        )
        await self.bot.tag_db.commit()

    # ---------- shared logic ----------

    async def _go_afk(self, member: discord.Member, message: str):
        since = discord.utils.utcnow()
        base_name = member.nick or member.name
        tagged_nick = (AFK_PREFIX + base_name)[:MAX_NICK_LENGTH]

        await self._set_afk(member.guild.id, member.id, message, since.isoformat(), member.nick, tagged_nick)

        try:
            await member.edit(nick=tagged_nick, reason="Went AFK")
        except (discord.Forbidden, discord.HTTPException):
            pass

        return since

    async def _return_from_afk(self, member: discord.Member, original_nick):
        await self._clear_afk(member.guild.id, member.id)
        try:
            await member.edit(nick=original_nick, reason="No longer AFK")
        except (discord.Forbidden, discord.HTTPException):
            pass

    # ---------- commands ----------

    @discord.slash_command(name="afk", description="Mark yourself as AFK")
    async def afk_slash(
        self, ctx,
        afk_message: Option(str, "Your AFK message", required=False, default="AFK"),
    ):
        since = await self._go_afk(ctx.author, afk_message.strip() or "AFK")
        await ctx.respond(
            f"You're now AFK: `{afk_message}` - since {discord.utils.format_dt(since, style='R')}",
            allowed_mentions=discord.AllowedMentions.none()
        )

    @commands.command(name="afk")
    async def afk_prefix(self, ctx, *, message: str = "AFK"):
        since = await self._go_afk(ctx.author, message.strip() or "AFK")
        await ctx.send(
            f"You're now AFK: `{message}` - since {discord.utils.format_dt(since, style='R')}",
            allowed_mentions=discord.AllowedMentions.none()
        )

    # ---------- listeners ----------

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not message.guild:
            return

        ctx = await self.bot.get_context(message)
        if ctx.valid:
            return

        guild_id = message.guild.id

        # Welcome back to the basement
        row = await self._get_afk(guild_id, message.author.id)
        if row is not None:
            _, _, original_nick, _ = row
            await self._return_from_afk(message.author, original_nick)
            try:
                await message.reply(
                    f"Welcome back `{message.author.display_name}`! I removed your AFK :)",
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none()
                )
            except discord.HTTPException:
                pass

        # Let me sleep in peace bruh
        seen = set()
        lines = []
        for mentioned in message.mentions:
            if mentioned.id == message.author.id or mentioned.id in seen:
                continue
            seen.add(mentioned.id)

            m_row = await self._get_afk(guild_id, mentioned.id)
            if m_row is None:
                continue
            afk_message, since_str, _, _ = m_row
            since_dt = discord.utils.parse_time(since_str)
            since_text = discord.utils.format_dt(since_dt, style="R") if since_dt else "some time ago"
            lines.append(f"💤 `{mentioned.display_name}` is AFK: `{afk_message}` - since {since_text}")

        if lines:
            try:
                await message.reply(
                    "\n".join(lines),
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none()
                )
            except discord.HTTPException:
                pass

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        if before.nick == after.nick:
            return

        row = await self._get_afk(after.guild.id, after.id)
        if row is None:
            return

        _, _, _, tagged_nick = row
        if after.nick == tagged_nick:
            return

        await self._clear_afk(after.guild.id, after.id)


def setup(bot):
    bot.add_cog(AFK(bot))