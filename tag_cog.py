import discord
from discord.ext import commands
import json
import os

TAG_FILES_DIR = "tag_files"

class TagSystem(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def cog_unload(self):
        self.bot.loop.create_task(self.bot.tag_db.close())

    # ---------- lookups ----------

    async def get_tag_direct(self, guild_id: int, name: str):
        """Looks up a real tag row only (does not resolve aliases)."""
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

        _, content, attachments_json, _, _ = row
        files = []
        if attachments_json:
            for path in json.loads(attachments_json):
                if os.path.exists(path):
                    files.append(discord.File(path))

        await ctx.send(content=content or None, files=files if files else None)

    @tag.command(name="add")
    async def tag_add(self, ctx, name: str, *, content: str = None):
        if await self.name_taken(ctx.guild.id, name):
            await ctx.send(f"❌ Tag `{name}` already exists")
            return

        attachments = ctx.message.attachments
        if not content and not attachments:
            await ctx.send("❌ You need to provide text content and/or attach an image.")
            return

        new_size = sum(a.size for a in attachments) if attachments else 0
        if new_size:
            current_usage = await self.get_user_usage(ctx.guild.id, ctx.author.id)
            limit_mb = 5000 if ctx.author.has_permission.manage_guild else 50
            if current_usage + new_size > limit_mb:
                remaining = limit_mb - current_usage
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
            _, _, attachments_json, creator_id, _ = direct_row
            is_creator = ctx.author.id == creator_id
            if not (ctx.author.guild_permissions.manage_messages or is_creator):
                await ctx.send("❌ You don't have permission to remove this tag.")
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

            if not (ctx.author.guild_permissions.manage_messages or ctx.author.id == alias_creator):
                await ctx.send("❌ You don't have permission to remove this alias.")
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
        limit_mb = 5000 if ctx.author.has_permission.manage_guild else 50
        who = "You have" if target == ctx.author else f"{target.display_name} has"
        await ctx.send(f"📦 {who} used **{used_mb:.2f} MB** of the **{limit_mb:.0f} MB** tag storage limit.")


def setup(bot):
    bot.add_cog(TagSystem(bot))