import discord
from discord.ext import commands
import json
import os

TAG_FILES_DIR = "tag_files"

ADMIN_ROLE_ID = 1222456633511378965
MODERATOR_ROLE_ID = 1421877616272605326
OWNER_ROLE_ID = 1286650794053210122
ALLOWED_ROLE_IDS = {ADMIN_ROLE_ID, MODERATOR_ROLE_ID, OWNER_ROLE_ID}

REPORTED_WARNING = "⚠️ **This Tag has been reported for potential rule violations**"


class TagReportView(discord.ui.View):

    def __init__(self, guild_id: int, tag_name: str):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.tag_name = tag_name
        self.force_delete.custom_id = f"tagreport_delete:{guild_id}:{tag_name}"
        self.clear_report.custom_id = f"tagreport_clear:{guild_id}:{tag_name}"

    @staticmethod
    def _is_staff(interaction: discord.Interaction) -> bool:
        member = interaction.user
        if not isinstance(member, discord.Member):
            return False
        role_ids = {r.id for r in member.roles}
        return bool(role_ids & ALLOWED_ROLE_IDS)

    @discord.ui.button(label="Force Delete Tag", style=discord.ButtonStyle.danger, custom_id="tagreport_delete")
    async def force_delete(self, button: discord.ui.Button, interaction: discord.Interaction):
        if not self._is_staff(interaction):
            await interaction.response.send_message("❌ You don't have permission to do that.", ephemeral=True)
            return

        db = interaction.client.tag_db
        async with db.execute(
            "SELECT attachments FROM tags WHERE guild = ? AND name = ?",
            (self.guild_id, self.tag_name)
        ) as cursor:
            row = await cursor.fetchone()

        if row is None:
            await interaction.response.send_message(f"❌ Tag `{self.tag_name}` no longer exists.", ephemeral=True)
            return

        attachments_json = row[0]
        if attachments_json:
            for path in json.loads(attachments_json):
                if os.path.exists(path):
                    os.remove(path)

        await db.execute("DELETE FROM tags WHERE guild = ? AND name = ?", (self.guild_id, self.tag_name))
        await db.execute(
            "DELETE FROM tag_aliases WHERE guild = ? AND original_name = ?", (self.guild_id, self.tag_name)
        )
        await db.commit()

        for item in self.children:
            item.disabled = True

        embed = interaction.message.embeds[0] if interaction.message.embeds else None
        if embed:
            embed.color = discord.Color.dark_grey()
            embed.title = "🗑️ Tag Deleted"

        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send(
            f"🗑️ Tag `{self.tag_name}` has been force-deleted by {interaction.user.mention}."
        )

    @discord.ui.button(label="Clear Report", style=discord.ButtonStyle.success, custom_id="tagreport_clear")
    async def clear_report(self, button: discord.ui.Button, interaction: discord.Interaction):
        if not self._is_staff(interaction):
            await interaction.response.send_message("❌ You don't have permission to do that.", ephemeral=True)
            return

        db = interaction.client.tag_db
        await db.execute(
            "UPDATE tags SET reported = 0 WHERE guild = ? AND name = ?",
            (self.guild_id, self.tag_name)
        )
        await db.commit()

        for item in self.children:
            item.disabled = True

        embed = interaction.message.embeds[0] if interaction.message.embeds else None
        if embed:
            embed.color = discord.Color.green()
            embed.title = "✅ Report Cleared"

        await interaction.response.edit_message(embed=embed, view=self)


class TagSystem(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def cog_unload(self):
        self.bot.loop.create_task(self.bot.tag_db.close())

    # ---------- lookups ----------

    async def get_tag_direct(self, guild_id: int, name: str):
        """Looks up a real tag row only (does not resolve aliases)."""
        async with self.bot.tag_db.execute(
            "SELECT id, content, attachments, creator, attachments_size, reported "
            "FROM tags WHERE guild = ? AND name = ?",
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
        """True if name is used by either a real tag or an alias."""
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
    def _delete_files(attachments_json):
        if attachments_json:
            for path in json.loads(attachments_json):
                if os.path.exists(path):
                    os.remove(path)

    # ---------- commands ----------

    @commands.group(name="tag", invoke_without_command=True, aliases=["t"])
    async def tag(self, ctx, name: str = None):
        if name is None:
            await ctx.send(
                "Usage: `.tag <name>`, `.tag add <name> <content>`, `.tag remove <name>`, "
                "`.tag alias <original> <alias>`, `.tag list [@user]`, `.tag listall`, "
                "`.tag usage [@user]` (alias: `.tag storage`)"
            )
            return

        row = await self.get_tag(ctx.guild.id, name)
        if row is None:
            await ctx.send(f"❌ Tag `{name}` doesn't exist.")
            return

        _, content, attachments_json, _, _, reported = row
        files = []
        if attachments_json:
            for path in json.loads(attachments_json):
                if os.path.exists(path):
                    files.append(discord.File(path))

        display_content = content or None
        if reported:
            display_content = f"{REPORTED_WARNING}\n{display_content}" if display_content else REPORTED_WARNING

        await ctx.send(content=display_content, files=files if files else None)

    @tag.command(name="add")
    async def tag_add(self, ctx, name: str, *, content: str = None):
        if await self.name_taken(ctx.guild.id, name):
            await ctx.send(f"❌ Tag `{name}` already exists")
            return

        attachments = ctx.message.attachments
        if not content and not attachments:
            await ctx.send("❌ You need to provide text content and/or attach an image.")
            return

        for a in attachments:
            if a.size > 10 * 1024 * 1024:
                await ctx.send("❌ Your files are too powerful! Or, atleast that's what Discord says. Use a link instead. Thanks!")
                return

        new_size = sum(a.size for a in attachments) if attachments else 0
        if new_size:
            current_usage = await self.get_user_usage(ctx.guild.id, ctx.author.id)
            limit_mb = 5 * 1024 if ctx.author.guild_permissions.manage_guild else 50
            limit_bytes = limit_mb * 1024 * 1024
            if current_usage + new_size > limit_bytes:
                remaining = limit_bytes - current_usage
                await ctx.send(
                    f"❌ Storage limit exceeded. You have **{remaining / (1024 * 1024):.1f} MB** left "
                    f"of your {limit_mb:.0f} MB limit, but these attachments total **{new_size / (1024 * 1024):.1f} MB**."
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
            guild_dir = os.path.join(TAG_FILES_DIR, str(ctx.guild.id))
            os.makedirs(guild_dir, exist_ok=True)
            saved_paths = []
            for i, attachment in enumerate(attachments):
                safe_filename = f"{tag_id}_{i}_{attachment.filename}"
                path = os.path.join(guild_dir, safe_filename)
                await attachment.save(path)
                saved_paths.append(path)

            await self.bot.tag_db.execute(
                "UPDATE tags SET attachments = ?, attachments_size = ? WHERE id = ?",
                (json.dumps(saved_paths), new_size, tag_id)
            )
            await self.bot.tag_db.commit()

        await ctx.send(f"✅ Tag `{name}` added.")

    @tag.command(name="alias")
    async def tag_alias(self, ctx, original: str, alias: str):
        orig_row = await self.get_tag_direct(ctx.guild.id, original)
        if orig_row is None:
            if await self.get_alias(ctx.guild.id, original) is not None:
                await ctx.send(f"❌ `{original}` is itself an alias — alias the original tag it points to instead.")
            else:
                await ctx.send(f"❌ Tag `{original}` doesn't exist.")
            return

        if await self.name_taken(ctx.guild.id, alias):
            await ctx.send(f"❌ Tag `{alias}` already exists")
            return

        await self.bot.tag_db.execute(
            "INSERT INTO tag_aliases (name, guild, original_name, creator) VALUES (?, ?, ?, ?)",
            (alias, ctx.guild.id, original, ctx.author.id)
        )
        await self.bot.tag_db.commit()

        await ctx.send(f"✅ `{alias}` is now an alias for `{original}`.")

    @tag.command(name="remove")
    async def tag_remove(self, ctx, name: str):
        direct_row = await self.get_tag_direct(ctx.guild.id, name)
        if direct_row is not None:
            _, _, attachments_json, creator_id, _, _ = direct_row
            if ctx.author.id != creator_id:
                await ctx.send(
                    "❌ You can only remove tags you created. "
                    "Staff should use `/forcetag remove` to remove someone else's tag."
                )
                return

            self._delete_files(attachments_json)
            await self.bot.tag_db.execute(
                "DELETE FROM tags WHERE guild = ? AND name = ?", (ctx.guild.id, name)
            )
            await self.bot.tag_db.execute(
                "DELETE FROM tag_aliases WHERE guild = ? AND original_name = ?", (ctx.guild.id, name)
            )
            await self.bot.tag_db.commit()
            await ctx.send(f"✅ Tag `{name}` removed.")
            return

        original_name = await self.get_alias(ctx.guild.id, name)
        if original_name is not None:
            async with self.bot.tag_db.execute(
                "SELECT creator FROM tag_aliases WHERE guild = ? AND name = ?", (ctx.guild.id, name)
            ) as cursor:
                alias_row = await cursor.fetchone()
            alias_creator = alias_row[0] if alias_row else None

            if ctx.author.id != alias_creator:
                await ctx.send(
                    "❌ You can only remove aliases you created. "
                    "Staff should use `/forcetag remove` to remove someone else's alias."
                )
                return

            await self.bot.tag_db.execute(
                "DELETE FROM tag_aliases WHERE guild = ? AND name = ?", (ctx.guild.id, name)
            )
            await self.bot.tag_db.commit()
            await ctx.send(f"✅ Alias `{name}` removed.")
            return

        await ctx.send(f"❌ Tag `{name}` doesn't exist.")

    @tag.command(name="list")
    async def tag_list(self, ctx, member: discord.Member = None):
        target = member or ctx.author

        async with self.bot.tag_db.execute(
            "SELECT name FROM tags WHERE guild = ? AND creator = ? ORDER BY name",
            (ctx.guild.id, target.id)
        ) as cursor:
            rows = await cursor.fetchall()

        if not rows:
            who = "You haven't" if target == ctx.author else f"{target.display_name} hasn't"
            await ctx.send(f"{who} created any tags in this server yet.")
            return

        names = ", ".join(f"`{row[0]}`" for row in rows)
        possessive = "Your" if target == ctx.author else f"{target.display_name}'s"
        embed = discord.Embed(
            title=f"📑 {possessive} tags in {ctx.guild.name}",
            description=names,
            color=discord.Color.blurple()
        )
        embed.set_footer(text=f"{len(rows)} tag(s)")
        await ctx.send(embed=embed)

    @tag.command(name="listall")
    async def tag_listall(self, ctx):
        async with self.bot.tag_db.execute(
            "SELECT name FROM tags WHERE guild = ? ORDER BY name", (ctx.guild.id,)
        ) as cursor:
            rows = await cursor.fetchall()

        if not rows:
            await ctx.send("No tags exist in this server yet.")
            return

        names = ", ".join(f"`{row[0]}`" for row in rows)
        embed = discord.Embed(
            title=f"📑 All tags in {ctx.guild.name}",
            description=names,
            color=discord.Color.blurple()
        )
        embed.set_footer(text=f"{len(rows)} tag(s)")
        await ctx.send(embed=embed)

    @tag.command(name="usage", aliases=["storage"])
    async def tag_usage(self, ctx, member: discord.Member = None):
        target = member or ctx.author
        usage = await self.get_user_usage(ctx.guild.id, target.id)
        used_mb = usage / (1024 * 1024)
        limit_mb = 5 * 1024 if target.guild_permissions.manage_guild else 50
        who = "You have" if target == ctx.author else f"{target.display_name} has"
        await ctx.send(f"📦 {who} used **{used_mb:.2f} MB** of the **{limit_mb:.0f} MB** tag storage limit.")


def setup(bot):
    bot.add_cog(TagSystem(bot))