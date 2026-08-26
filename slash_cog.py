import discord
from discord.ext import commands
from discord.commands import SlashCommandGroup, Option
from datetime import timedelta
import json
import os

TAG_FILES_DIR = "tag_files"
MAX_USER_STORAGE_BYTES = 50 * 1024 * 1024  # 50 MB

ADMIN_ROLE_ID = 1222456633511378965
MODERATOR_ROLE_ID = 1421877616272605326
OWNER_ROLE_ID = 1286650794053210122
ALLOWED_ROLE_IDS = {ADMIN_ROLE_ID, MODERATOR_ROLE_ID, OWNER_ROLE_ID}


def is_staff():
    async def predicate(ctx: discord.ApplicationContext) -> bool:
        member = ctx.author
        if not isinstance(member, discord.Member):
            await ctx.respond("❌ This command can only be used in a server.", ephemeral=True)
            return False

        role_ids = {role.id for role in member.roles}
        if role_ids & ALLOWED_ROLE_IDS:
            return True

        await ctx.respond("❌ You don't have permission to use this command.", ephemeral=True)
        return False

    return commands.check(predicate)


class SlashCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        await self.bot.tag_db.execute(
            """CREATE TABLE IF NOT EXISTS warnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                moderator_id INTEGER NOT NULL,
                reason TEXT,
                timestamp TEXT NOT NULL
            )"""
        )
        await self.bot.tag_db.commit()

    # ---------- shared helpers (mirrors tag_cog.py) ----------

    async def get_tag_direct(self, guild_id: int, name: str):
        async with self.bot.tag_db.execute(
            "SELECT id, content, attachments, creator, attachments_size FROM tags WHERE guild = ? AND name = ?",
            (guild_id, name)
        ) as cursor:
            return await cursor.fetchone()

    async def get_alias(self, guild_id: int, name: str):
        async with self.bot.tag_db.execute(
            "SELECT original_name FROM tag_aliases WHERE guild = ? AND name = ?",
            (guild_id, name)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

    async def get_tag(self, guild_id: int, name: str):
        """Resolves aliases transparently. Use this for viewing a tag."""
        row = await self.get_tag_direct(guild_id, name)
        if row is not None:
            return row
        original_name = await self.get_alias(guild_id, name)
        if original_name is not None:
            return await self.get_tag_direct(guild_id, original_name)
        return None

    async def name_taken(self, guild_id: int, name: str) -> bool:
        if await self.get_tag_direct(guild_id, name) is not None:
            return True
        if await self.get_alias(guild_id, name) is not None:
            return True
        return False

    async def get_user_usage(self, guild_id: int, user_id: int) -> int:
        async with self.bot.tag_db.execute(
            "SELECT COALESCE(SUM(attachments_size), 0) FROM tags WHERE guild = ? AND creator = ?",
            (guild_id, user_id)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

    @staticmethod
    def _collect_attachments(*attachments):
        return [a for a in attachments if a is not None]

    async def _save_attachments(self, guild_id: int, tag_id: int, attachments):
        saved_paths = []
        if attachments:
            guild_dir = os.path.join(TAG_FILES_DIR, str(guild_id))
            os.makedirs(guild_dir, exist_ok=True)
            for i, attachment in enumerate(attachments):
                safe_filename = f"{tag_id}_{i}_{attachment.filename}"
                path = os.path.join(guild_dir, safe_filename)
                await attachment.save(path)
                saved_paths.append(path)
        return saved_paths

    @staticmethod
    def _delete_files(attachments_json):
        if attachments_json:
            for path in json.loads(attachments_json):
                if os.path.exists(path):
                    os.remove(path)

    @staticmethod
    def _can_moderate(ctx, target: discord.Member):
        """Returns an error string if the action should be blocked, else None."""
        author_role_ids = {r.id for r in ctx.author.roles}
        target_role_ids = {r.id for r in target.roles}

        if target.id == ctx.author.id:
            return "❌ You can't target yourself."
        if target.id == ctx.bot.user.id:
            return "❌ You can't target the bot."
        if OWNER_ROLE_ID in target_role_ids and OWNER_ROLE_ID not in author_role_ids:
            return "❌ You can't moderate the owner."
        if ADMIN_ROLE_ID in target_role_ids and not (author_role_ids & {ADMIN_ROLE_ID, OWNER_ROLE_ID}):
            return "❌ You can't moderate an admin."
        return None

    # ---------- /tag (staff-only mirrors of the . commands) ----------

    tag_group = SlashCommandGroup("tag", "View and manage tags (staff only)")

    @tag_group.command(name="view", description="View a tag")
    @is_staff()
    async def tag_view(self, ctx, name: Option(str, "The tag name")):
        row = await self.get_tag(ctx.guild.id, name)
        if row is None:
            await ctx.respond(f"❌ Tag `{name}` doesn't exist.", ephemeral=True)
            return

        _, content, attachments_json, _, _ = row
        files = []
        if attachments_json:
            for path in json.loads(attachments_json):
                if os.path.exists(path):
                    files.append(discord.File(path))

        await ctx.respond(content=content or None, files=files if files else [])

    @tag_group.command(name="add", description="Add a new tag")
    @is_staff()
    async def tag_add(
        self, ctx,
        name: Option(str, "The tag name"),
        content: Option(str, "The tag's text content", required=False, default=None),
        attachment1: Option(discord.Attachment, "Image or GIF", required=False, default=None),
        attachment2: Option(discord.Attachment, "Image or GIF", required=False, default=None),
        attachment3: Option(discord.Attachment, "Image or GIF", required=False, default=None),
    ):
        if await self.name_taken(ctx.guild.id, name):
            await ctx.respond(f"❌ Tag `{name}` already exists", ephemeral=True)
            return

        attachments = self._collect_attachments(attachment1, attachment2, attachment3)
        if not content and not attachments:
            await ctx.respond("❌ You need to provide text content and/or attach an image.", ephemeral=True)
            return

        new_size = sum(a.size for a in attachments) if attachments else 0
        if new_size:
            current_usage = await self.get_user_usage(ctx.guild.id, ctx.author.id)
            if current_usage + new_size > MAX_USER_STORAGE_BYTES:
                remaining = MAX_USER_STORAGE_BYTES - current_usage
                await ctx.respond(
                    f"❌ Storage limit exceeded. You have **{remaining / (1024 * 1024):.1f} MB** left "
                    f"of your 50 MB limit, but these attachments total **{new_size / (1024 * 1024):.1f} MB**.",
                    ephemeral=True
                )
                return

        cursor = await self.bot.tag_db.execute(
            "INSERT INTO tags (name, content, attachments, attachments_size, guild, creator) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, content, None, 0, ctx.guild.id, ctx.author.id)
        )
        await self.bot.tag_db.commit()
        tag_id = cursor.lastrowid

        if attachments:
            saved_paths = await self._save_attachments(ctx.guild.id, tag_id, attachments)
            await self.bot.tag_db.execute(
                "UPDATE tags SET attachments = ?, attachments_size = ? WHERE id = ?",
                (json.dumps(saved_paths), new_size, tag_id)
            )
            await self.bot.tag_db.commit()

        await ctx.respond(f"`{name}` Tag Added ✅")

    @tag_group.command(name="list", description="List tags created by a user (defaults to yourself)")
    @is_staff()
    async def tag_list(
        self, ctx,
        member: Option(discord.Member, "User whose tags to list", required=False, default=None)
    ):
        target = member or ctx.author

        async with self.bot.tag_db.execute(
            "SELECT name FROM tags WHERE guild = ? AND creator = ? ORDER BY name",
            (ctx.guild.id, target.id)
        ) as cursor:
            rows = await cursor.fetchall()

        if not rows:
            who = "You haven't" if target == ctx.author else f"{target.display_name} hasn't"
            await ctx.respond(f"{who} created any tags in this server yet.", ephemeral=True)
            return

        names = ", ".join(f"`{row[0]}`" for row in rows)
        possessive = "Your" if target == ctx.author else f"{target.display_name}'s"
        embed = discord.Embed(
            title=f"📑 {possessive} tags in {ctx.guild.name}",
            description=names,
            color=discord.Color.blurple()
        )
        embed.set_footer(text=f"{len(rows)} tag(s)")
        await ctx.respond(embed=embed)

    @tag_group.command(name="listall", description="List every tag in the server")
    @is_staff()
    async def tag_listall(self, ctx):
        async with self.bot.tag_db.execute(
            "SELECT name FROM tags WHERE guild = ? ORDER BY name", (ctx.guild.id,)
        ) as cursor:
            rows = await cursor.fetchall()

        if not rows:
            await ctx.respond("No tags exist in this server yet.", ephemeral=True)
            return

        names = ", ".join(f"`{row[0]}`" for row in rows)
        embed = discord.Embed(
            title=f"📑 All tags in {ctx.guild.name}",
            description=names,
            color=discord.Color.blurple()
        )
        embed.set_footer(text=f"{len(rows)} tag(s)")
        await ctx.respond(embed=embed)

    @tag_group.command(name="usage", description="Check a user's tag storage usage")
    @is_staff()
    async def tag_usage(
        self, ctx,
        member: Option(discord.Member, "User to check (defaults to yourself)", required=False, default=None)
    ):
        target = member or ctx.author
        usage = await self.get_user_usage(ctx.guild.id, target.id)
        used_mb = usage / (1024 * 1024)
        limit_mb = MAX_USER_STORAGE_BYTES / (1024 * 1024)
        who = "You have" if target == ctx.author else f"{target.display_name} has"
        await ctx.respond(f"📦 {who} used **{used_mb:.2f} MB** of the **{limit_mb:.0f} MB** tag storage limit.")

    # ---------- /forcetag ----------

    forcetag_group = SlashCommandGroup("forcetag", "Forcefully remove or overwrite any tag (staff only)")

    @forcetag_group.command(name="remove", description="Force-remove any tag or alias, regardless of who created it")
    @is_staff()
    async def forcetag_remove(self, ctx, name: Option(str, "The tag or alias name to remove")):
        direct_row = await self.get_tag_direct(ctx.guild.id, name)
        if direct_row is not None:
            _, _, attachments_json, _, _ = direct_row
            self._delete_files(attachments_json)

            await self.bot.tag_db.execute(
                "DELETE FROM tags WHERE guild = ? AND name = ?", (ctx.guild.id, name)
            )
            await self.bot.tag_db.execute(
                "DELETE FROM tag_aliases WHERE guild = ? AND original_name = ?", (ctx.guild.id, name)
            )
            await self.bot.tag_db.commit()
            await ctx.respond(f"`{name}` Tag Removed ✅")
            return

        if await self.get_alias(ctx.guild.id, name) is not None:
            await self.bot.tag_db.execute(
                "DELETE FROM tag_aliases WHERE guild = ? AND name = ?", (ctx.guild.id, name)
            )
            await self.bot.tag_db.commit()
            await ctx.respond(f"`{name}` Alias Removed ✅")
            return

        await ctx.respond(f"❌ Tag `{name}` doesn't exist.", ephemeral=True)

    @forcetag_group.command(name="modify", description="Overwrite a tag's content and/or attachments")
    @is_staff()
    async def forcetag_modify(
        self, ctx,
        name: Option(str, "The tag name to modify"),
        text: Option(str, "New text content", required=False, default=None),
        attachment1: Option(discord.Attachment, "New image/GIF", required=False, default=None),
        attachment2: Option(discord.Attachment, "New image/GIF", required=False, default=None),
        attachment3: Option(discord.Attachment, "New image/GIF", required=False, default=None),
    ):
        row = await self.get_tag_direct(ctx.guild.id, name)
        if row is None:
            await ctx.respond(f"❌ Tag `{name}` doesn't exist.", ephemeral=True)
            return

        tag_id, old_content, old_attachments_json, creator_id, old_size = row
        attachments = self._collect_attachments(attachment1, attachment2, attachment3)

        if text is None and not attachments:
            await ctx.respond("❌ Provide new text and/or a new attachment to modify the tag.", ephemeral=True)
            return

        new_content = text if text is not None else old_content

        if attachments:
            new_size = sum(a.size for a in attachments)
            current_usage = await self.get_user_usage(ctx.guild.id, creator_id)
            projected = current_usage - old_size + new_size
            if projected > MAX_USER_STORAGE_BYTES:
                remaining = MAX_USER_STORAGE_BYTES - (current_usage - old_size)
                await ctx.respond(
                    f"❌ This would exceed the tag creator's 50 MB storage limit. "
                    f"They have **{remaining / (1024 * 1024):.1f} MB** available for this tag, "
                    f"but the new attachments total **{new_size / (1024 * 1024):.1f} MB**.",
                    ephemeral=True
                )
                return

            self._delete_files(old_attachments_json)
            saved_paths = await self._save_attachments(ctx.guild.id, tag_id, attachments)
            new_attachments_json = json.dumps(saved_paths) if saved_paths else None
            await self.bot.tag_db.execute(
                "UPDATE tags SET content = ?, attachments = ?, attachments_size = ? WHERE id = ?",
                (new_content, new_attachments_json, new_size, tag_id)
            )
        else:
            await self.bot.tag_db.execute(
                "UPDATE tags SET content = ? WHERE id = ?",
                (new_content, tag_id)
            )

        await self.bot.tag_db.commit()
        await ctx.respond(f"`{name}` Tag Modified ✅")

    # ---------- moderation ----------

    @discord.slash_command(name="warn", description="Warn a member")
    @is_staff()
    async def warn(
        self, ctx,
        member: Option(discord.Member, "Member to warn"),
        reason: Option(str, "Reason for the warning", required=False, default="No reason provided"),
    ):
        block = self._can_moderate(ctx, member)
        if block:
            await ctx.respond(block, ephemeral=True)
            return

        await self.bot.tag_db.execute(
            "INSERT INTO warnings (guild, user_id, moderator_id, reason, timestamp) VALUES (?, ?, ?, ?, ?)",
            (ctx.guild.id, member.id, ctx.author.id, reason, discord.utils.utcnow().isoformat())
        )
        await self.bot.tag_db.commit()

        try:
            await member.send(f"⚠️ You were warned in **{ctx.guild.name}**: {reason}")
        except discord.Forbidden:
            pass

        await ctx.respond(f"⚠️ {member.mention} has been warned. Reason: {reason}")

    @discord.slash_command(name="mute", description="Voice-mute or unmute a member (they can still type)")
    @is_staff()
    async def mute(
        self, ctx,
        member: Option(discord.Member, "Member to mute/unmute"),
        state: Option(bool, "True to mute, False to unmute", default=True),
        reason: Option(str, "Reason", required=False, default=None),
    ):
        block = self._can_moderate(ctx, member)
        if block:
            await ctx.respond(block, ephemeral=True)
            return

        try:
            await member.edit(mute=state, reason=reason)
        except discord.Forbidden:
            await ctx.respond("❌ I don't have permission to do that.", ephemeral=True)
            return

        action = "voice-muted" if state else "voice-unmuted"
        await ctx.respond(f"🔇 {member.mention} has been {action}." + (f" Reason: {reason}" if reason else ""))

    @discord.slash_command(name="timeout", description="Timeout a member (full communication block) or clear it")
    @is_staff()
    async def timeout(
        self, ctx,
        member: Option(discord.Member, "Member to timeout"),
        duration_minutes: Option(int, "Duration in minutes (0 to remove timeout)", default=10),
        reason: Option(str, "Reason", required=False, default=None),
    ):
        block = self._can_moderate(ctx, member)
        if block:
            await ctx.respond(block, ephemeral=True)
            return

        try:
            if duration_minutes <= 0:
                await member.timeout(None, reason=reason)
                await ctx.respond(f"⏱️ Timeout removed for {member.mention}.")
            else:
                until = discord.utils.utcnow() + timedelta(minutes=duration_minutes)
                await member.timeout(until, reason=reason)
                await ctx.respond(
                    f"⏱️ {member.mention} has been timed out for {duration_minutes} minute(s)."
                    + (f" Reason: {reason}" if reason else "")
                )
        except discord.Forbidden:
            await ctx.respond("❌ I don't have permission to do that.", ephemeral=True)

    @discord.slash_command(name="kick", description="Kick a member")
    @is_staff()
    async def kick(
        self, ctx,
        member: Option(discord.Member, "Member to kick"),
        reason: Option(str, "Reason", required=False, default="No reason provided"),
    ):
        block = self._can_moderate(ctx, member)
        if block:
            await ctx.respond(block, ephemeral=True)
            return

        try:
            await member.kick(reason=reason)
        except discord.Forbidden:
            await ctx.respond("❌ I don't have permission to do that.", ephemeral=True)
            return

        await ctx.respond(f"👢 {member.mention} has been kicked. Reason: {reason}")

    @discord.slash_command(name="ban", description="Ban a member")
    @is_staff()
    async def ban(
        self, ctx,
        member: Option(discord.Member, "Member to ban"),
        reason: Option(str, "Reason", required=False, default="No reason provided"),
        delete_message_days: Option(int, "Days of messages to delete (0-7)", required=False, default=0),
    ):
        block = self._can_moderate(ctx, member)
        if block:
            await ctx.respond(block, ephemeral=True)
            return

        try:
            await member.ban(reason=reason, delete_message_days=delete_message_days)
        except discord.Forbidden:
            await ctx.respond("❌ I don't have permission to do that.", ephemeral=True)
            return

        await ctx.respond(f"🔨 {member.mention} has been banned. Reason: {reason}")


def setup(bot):
    bot.add_cog(SlashCommands(bot))