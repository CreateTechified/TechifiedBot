from datetime import timedelta

import discord
from discord.ext import commands
from discord.commands import SlashCommandGroup, Option
from discord.utils import utcnow

ADMIN_ROLE_ID = 1222456633511378965
MODERATOR_ROLE_ID = 1421877616272605326
OWNER_ROLE_ID = 1286650794053210122
STAFF_ROLE_IDS = {ADMIN_ROLE_ID, MODERATOR_ROLE_ID, OWNER_ROLE_ID}

STREAK_THRESHOLD = 5
STREAK_WINDOW_SECONDS = 5 * 60
BASE_RESTRICTION_MINUTES = 30
MAX_RESTRICTION_MINUTES = 28 * 24 * 60


def is_staff():
    async def predicate(ctx: discord.ApplicationContext) -> bool:
        member = ctx.author
        if not isinstance(member, discord.Member):
            await ctx.respond("❌ This command can only be used in a server.", ephemeral=True)
            return False

        role_ids = {role.id for role in member.roles}
        if role_ids & STAFF_ROLE_IDS:
            return True

        await ctx.respond("❌ You don't have permission to use this command.", ephemeral=True)
        return False

    return commands.check(predicate)


def format_duration(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} minute(s)"
    hours, rem_minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} hour(s)" + (f" {rem_minutes} minute(s)" if rem_minutes else "")
    days, rem_hours = divmod(hours, 24)
    return f"{days} day(s)" + (f" {rem_hours} hour(s)" if rem_hours else "")


class LarpBoard(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    larp_group = SlashCommandGroup("larp", "The LARP point board")

    # ---------- db helpers ----------

    async def _add_point(self, guild_id: int, user_id: int):
        await self.bot.tag_db.execute(
            "INSERT INTO larp_scores (guild, user_id, points) VALUES (?, ?, 1) "
            "ON CONFLICT(guild, user_id) DO UPDATE SET points = points + 1",
            (guild_id, user_id)
        )
        await self.bot.tag_db.commit()

    async def _get_state(self, guild_id: int, user_id: int):
        async with self.bot.tag_db.execute(
            "SELECT until, offense_count, streak_count, last_larp_at FROM larp_timeouts "
            "WHERE guild = ? AND user_id = ?",
            (guild_id, user_id)
        ) as cursor:
            row = await cursor.fetchone()

        if row is None:
            return None, 0, 0, None
        return row

    async def _clear_restriction(self, guild_id: int, user_id: int):
        await self.bot.tag_db.execute(
            "UPDATE larp_timeouts SET until = NULL WHERE guild = ? AND user_id = ?",
            (guild_id, user_id)
        )
        await self.bot.tag_db.commit()

    async def _update_streak(self, guild_id: int, user_id: int, streak_count: int, last_larp_at):
        await self.bot.tag_db.execute(
            "INSERT INTO larp_timeouts (guild, user_id, until, offense_count, streak_count, last_larp_at) "
            "VALUES (?, ?, NULL, 0, ?, ?) "
            "ON CONFLICT(guild, user_id) DO UPDATE SET "
            "streak_count = excluded.streak_count, last_larp_at = excluded.last_larp_at",
            (guild_id, user_id, streak_count, last_larp_at)
        )
        await self.bot.tag_db.commit()

    async def _apply_restriction(self, message: discord.Message, offense_count: int):
        guild_id = message.guild.id
        user_id = message.author.id

        minutes = min(BASE_RESTRICTION_MINUTES * (2 ** (offense_count - 1)), MAX_RESTRICTION_MINUTES)
        until = utcnow() + timedelta(minutes=minutes)

        await self.bot.tag_db.execute(
            "INSERT INTO larp_timeouts (guild, user_id, until, offense_count, streak_count, last_larp_at) "
            "VALUES (?, ?, ?, ?, 0, NULL) "
            "ON CONFLICT(guild, user_id) DO UPDATE SET "
            "until = excluded.until, offense_count = excluded.offense_count, "
            "streak_count = 0, last_larp_at = NULL",
            (guild_id, user_id, until.isoformat(), offense_count)
        )
        await self.bot.tag_db.commit()

        try:
            await message.channel.send(
                f"🛑 {message.author.mention} said **LARP** {STREAK_THRESHOLD} times in a row in under "
                f"{STREAK_WINDOW_SECONDS // 60} minutes! They can't say it again for "
                f"**{format_duration(minutes)}** (offense #{offense_count}). Any message with 'larp' will "
                f"be deleted until then.",
                allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.HTTPException:
            pass

    # ---------- listener ----------

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not message.guild or not message.content:
            return

        guild_id = message.guild.id
        user_id = message.author.id
        contains_larp = "larp" in message.content.lower()

        until_str, offense_count, streak_count, last_larp_at_str = await self._get_state(guild_id, user_id)

        until = discord.utils.parse_time(until_str) if until_str else None
        if until is not None:
            if until > utcnow():
                if contains_larp:
                    try:
                        await message.delete()
                    except discord.HTTPException:
                        pass
                    try:
                        await message.channel.send(
                            f"🚫 {message.author.mention}, you're LARP-restricted "
                            f"({discord.utils.format_dt(until, style='R')}). That message was deleted.",
                            delete_after=8, allowed_mentions=discord.AllowedMentions.none()
                        )
                    except discord.HTTPException:
                        pass
                return
            else:
                await self._clear_restriction(guild_id, user_id)

        if not contains_larp:
            return

        await self._add_point(guild_id, user_id)

        now = utcnow()
        last_larp_at = discord.utils.parse_time(last_larp_at_str) if last_larp_at_str else None
        if last_larp_at is not None and (now - last_larp_at).total_seconds() <= STREAK_WINDOW_SECONDS:
            new_streak = streak_count + 1
        else:
            new_streak = 1

        if new_streak >= STREAK_THRESHOLD:
            await self._apply_restriction(message, offense_count + 1)
        else:
            await self._update_streak(guild_id, user_id, new_streak, now.isoformat())

    # ---------- commands ----------

    @larp_group.command(name="leaderboard", description="See who has said LARP the most")
    async def larp_leaderboard(self, ctx):
        async with self.bot.tag_db.execute(
            "SELECT user_id, points FROM larp_scores WHERE guild = ? ORDER BY points DESC LIMIT 10",
            (ctx.guild.id,)
        ) as cursor:
            rows = await cursor.fetchall()

        if not rows:
            await ctx.respond("No one has said LARP yet. 👀")
            return

        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, (user_id, points) in enumerate(rows):
            prefix = medals[i] if i < len(medals) else f"`#{i + 1}`"
            lines.append(f"{prefix} <@{user_id}> — **{points}** point(s)")

        embed = discord.Embed(
            title=f"🎭 LARP Leaderboard — {ctx.guild.name}",
            description="\n".join(lines),
            color=discord.Color.purple()
        )
        await ctx.respond(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @larp_group.command(name="score", description="Check how many times someone has said LARP")
    async def larp_score(
        self, ctx,
        member: Option(discord.Member, "Member to check (defaults to yourself)", required=False, default=None),
    ):
        target = member or ctx.author

        async with self.bot.tag_db.execute(
            "SELECT points FROM larp_scores WHERE guild = ? AND user_id = ?",
            (ctx.guild.id, target.id)
        ) as cursor:
            row = await cursor.fetchone()

        points = row[0] if row else 0
        who = "You have" if target == ctx.author else f"{target.display_name} has"
        await ctx.respond(f"🎭 {who} said **LARP** {points} time(s).", allowed_mentions=discord.AllowedMentions.none())

    @larp_group.command(name="reset", description="Wipe a member's LARP points and offense history (staff only)")
    @is_staff()
    async def larp_reset(
        self, ctx,
        member: Option(discord.Member, "Member to reset"),
    ):
        await self.bot.tag_db.execute(
            "DELETE FROM larp_scores WHERE guild = ? AND user_id = ?", (ctx.guild.id, member.id)
        )
        await self.bot.tag_db.execute(
            "DELETE FROM larp_timeouts WHERE guild = ? AND user_id = ?", (ctx.guild.id, member.id)
        )
        await self.bot.tag_db.commit()

        await ctx.respond(
            f"✅ Reset LARP points, restriction, and offense history for {member.mention}.",
            allowed_mentions=discord.AllowedMentions.none()
        )

    @larp_group.command(name="unmute", description="Manually clear an active LARP restriction (staff only)")
    @is_staff()
    async def larp_unmute(
        self, ctx,
        member: Option(discord.Member, "Member to clear"),
    ):
        await self._clear_restriction(ctx.guild.id, member.id)

        await ctx.respond(
            f"✅ Cleared {member.mention}'s LARP restriction.",
            allowed_mentions=discord.AllowedMentions.none()
        )


def setup(bot):
    bot.add_cog(LarpBoard(bot))