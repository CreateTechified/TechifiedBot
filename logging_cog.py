import io
from datetime import timedelta

import discord
from discord.ext import commands
from discord.commands import SlashCommandGroup, Option

from slash_cog import is_admin
from log_utils import ADMIN_LOG_CHANNEL_ID, BOT_LOG_CHANNEL_ID, clip, consume_quiet, log_admin, log_admin_action

IGNORED_CHANNEL_IDS = {ADMIN_LOG_CHANNEL_ID, BOT_LOG_CHANNEL_ID}

NEW_ACCOUNT_WARNING = timedelta(days=7)

# key -> label.
LOG_TYPES = {
    "message_delete": "🗑️ Message deletes",
    "message_edit": "✏️ Message edits",
    "bulk_delete": "🧹 Bulk message deletes",
    "member_join": "📥 Member joins",
    "member_leave": "📤 Member leaves",
}
LOG_TYPE_CHOICES = [discord.OptionChoice(label, key) for key, label in LOG_TYPES.items()]


class EventLogs(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._disabled = {}

    # ---------- toggles ----------

    async def _get_disabled(self, guild_id: int) -> set:
        cached = self._disabled.get(guild_id)
        if cached is not None:
            return cached

        async with self.bot.tag_db.execute(
            "SELECT log_type FROM log_settings WHERE guild = ? AND enabled = 0", (guild_id,)
        ) as cursor:
            rows = await cursor.fetchall()

        disabled = {row[0] for row in rows}
        self._disabled[guild_id] = disabled
        return disabled

    async def _is_enabled(self, guild_id: int, log_type: str) -> bool:
        return log_type not in await self._get_disabled(guild_id)

    logs_group = SlashCommandGroup("logs", "Turn individual log types on or off")

    @logs_group.command(name="toggle", description="Turn a type of log on or off")
    @is_admin()
    async def logs_toggle(
        self, ctx,
        log_type: Option(str, "Which log to toggle", choices=LOG_TYPE_CHOICES),
    ):
        disabled = await self._get_disabled(ctx.guild.id)
        enable = log_type in disabled

        await self.bot.tag_db.execute(
            "INSERT INTO log_settings (guild, log_type, enabled) VALUES (?, ?, ?) "
            "ON CONFLICT(guild, log_type) DO UPDATE SET enabled = excluded.enabled",
            (ctx.guild.id, log_type, int(enable))
        )
        await self.bot.tag_db.commit()

        if enable:
            disabled.discard(log_type)
        else:
            disabled.add(log_type)

        label = LOG_TYPES[log_type]
        await ctx.respond(f"{label} logging is now **{'ON ✅' if enable else 'OFF ❌'}**.")
        await log_admin_action(
            self.bot, "⚙️ Log Setting Changed",
            discord.Color.green() if enable else discord.Color.red(), ctx.author,
            fields={"Log": label, "Now": "ON" if enable else "OFF"}, channel=ctx.channel
        )

    @logs_group.command(name="status", description="See which logs are on or off")
    @is_admin()
    async def logs_status(self, ctx):
        disabled = await self._get_disabled(ctx.guild.id)
        lines = [
            f"{label}: {'❌ OFF' if key in disabled else '✅ ON'}"
            for key, label in LOG_TYPES.items()
        ]
        embed = discord.Embed(
            title="📋 Log settings", description="\n".join(lines), color=discord.Color.blurple()
        )
        await ctx.respond(embed=embed)

    # ---------- messages ----------

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload):
        if payload.guild_id is None or payload.channel_id in IGNORED_CHANNEL_IDS:
            return

        if consume_quiet(payload.message_id):
            return

        if not await self._is_enabled(payload.guild_id, "message_delete"):
            return

        msg = payload.cached_message
        if msg is not None and msg.author.bot:
            return

        embed = discord.Embed(
            title="🗑️ Message Deleted", color=discord.Color.red(), timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="Channel", value=f"<#{payload.channel_id}>", inline=True)

        if msg is None:
            embed.description = (
                "*This message wasn't in my cache (it's old, or was sent before the bot restarted), "
                "so the author and content are unknown.*"
            )
        else:
            embed.description = clip(msg.content, 4000) if msg.content else "*No text content*"
            embed.set_author(name=str(msg.author), icon_url=msg.author.display_avatar.url)
            embed.add_field(name="Author", value=f"{msg.author.mention} (`{msg.author.id}`)", inline=True)
            embed.add_field(name="Sent", value=discord.utils.format_dt(msg.created_at, style="R"), inline=True)
            if msg.attachments:
                files = "\n".join(f"[{a.filename}]({a.url})" for a in msg.attachments)
                embed.add_field(name="Attachments", value=clip(files, 1024), inline=False)

        embed.set_footer(text=f"Message ID: {payload.message_id}")
        await log_admin(self.bot, embed=embed)

    @commands.Cog.listener()
    async def on_message_edit(self, before, after):
        if after.guild is None or after.author.bot or after.channel.id in IGNORED_CHANNEL_IDS:
            return

        if before.content == after.content:
            return

        if not await self._is_enabled(after.guild.id, "message_edit"):
            return

        embed = discord.Embed(
            title="✏️ Message Edited",
            description=f"[Jump to message]({after.jump_url}) in {after.channel.mention}",
            color=discord.Color.gold(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_author(name=str(after.author), icon_url=after.author.display_avatar.url)
        embed.add_field(name="Before", value=clip(before.content or "*empty*", 1024), inline=False)
        embed.add_field(name="After", value=clip(after.content or "*empty*", 1024), inline=False)
        embed.add_field(name="Author", value=f"{after.author.mention} (`{after.author.id}`)", inline=False)
        embed.set_footer(text=f"Message ID: {after.id}")
        await log_admin(self.bot, embed=embed)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload):
        if payload.guild_id is None or payload.channel_id in IGNORED_CHANNEL_IDS:
            return

        if not await self._is_enabled(payload.guild_id, "bulk_delete"):
            return

        cached = sorted(payload.cached_messages, key=lambda m: m.created_at)

        lines = []
        for m in cached:
            stamp = m.created_at.strftime("%Y-%m-%d %H:%M:%S")
            text = m.content or "[no text]"
            if m.attachments:
                text += " " + " ".join(a.url for a in m.attachments)
            lines.append(f"[{stamp} UTC] {m.author} ({m.author.id}): {text}")

        embed = discord.Embed(
            title="🧹 Bulk Message Delete", color=discord.Color.dark_red(), timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="Channel", value=f"<#{payload.channel_id}>", inline=True)
        embed.add_field(name="Messages deleted", value=str(len(payload.message_ids)), inline=True)
        embed.add_field(name="Saved in transcript", value=str(len(cached)), inline=True)
        if len(cached) < len(payload.message_ids):
            embed.set_footer(text="Messages that weren't in my cache can't be included in the transcript.")

        kwargs = {"embed": embed}
        if lines:
            buffer = io.BytesIO("\n".join(lines).encode("utf-8"))
            kwargs["file"] = discord.File(fp=buffer, filename="deleted_messages.txt")

        await log_admin(self.bot, **kwargs)

    # ---------- members ----------

    @commands.Cog.listener()
    async def on_member_join(self, member):
        if not await self._is_enabled(member.guild.id, "member_join"):
            return

        created = member.created_at
        embed = discord.Embed(
            title="🤖 Bot Added" if member.bot else "📥 Member Joined",
            description=f"{member.mention} (`{member.id}`)",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(
            name="Account created",
            value=f"{discord.utils.format_dt(created, style='F')} ({discord.utils.format_dt(created, style='R')})",
            inline=False
        )
        if discord.utils.utcnow() - created < NEW_ACCOUNT_WARNING:
            embed.add_field(name="⚠️ New account", value="This account is less than 7 days old.", inline=False)
        embed.set_footer(text=f"Member #{member.guild.member_count}")
        await log_admin(self.bot, embed=embed)

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        if not await self._is_enabled(member.guild.id, "member_leave"):
            return

        embed = discord.Embed(
            title="📤 Member Left",
            description=f"{member.mention} (`{member.id}`)",
            color=discord.Color.dark_orange(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        if member.joined_at:
            embed.add_field(
                name="Joined",
                value=f"{discord.utils.format_dt(member.joined_at, style='F')} "
                      f"({discord.utils.format_dt(member.joined_at, style='R')})",
                inline=False
            )
        roles = [r.mention for r in reversed(member.roles) if not r.is_default()]
        embed.add_field(name=f"Roles ({len(roles)})", value=clip(", ".join(roles), 1024) if roles else "None", inline=False)
        embed.set_footer(text=f"Members now: {member.guild.member_count}")
        await log_admin(self.bot, embed=embed)


def setup(bot):
    bot.add_cog(EventLogs(bot))