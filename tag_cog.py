import asyncio
import discord
from discord.ext import commands
import aiosqlite
import json
import os

TAG_FILES_DIR = "tag_files"

class TagSystem(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        asyncio.create_task(self.cog_load())

    async def cog_load(self):
        self.bot.tag_db = await aiosqlite.connect("tags.db")
        await self.bot.tag_db.execute(
            """CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                content TEXT,
                attachments TEXT,
                guild INTEGER NOT NULL,
                creator INTEGER NOT NULL,
                UNIQUE(name, guild)
            )"""
        )
        await self.bot.tag_db.commit()

    def cog_unload(self):
        self.bot.loop.create_task(self.bot.tag_db.close())

    async def get_tag(self, guild_id: int, name: str):
        async with self.bot.tag_db.execute(
            "SELECT id, content, attachments FROM tags WHERE guild = ? AND name = ?",
            (guild_id, name)
        ) as cursor:
            return await cursor.fetchone()

    @commands.group(name="tag", invoke_without_command=True)
    async def tag(self, ctx, name: str = None):
        if name is None:
            await ctx.send(
                "Usage: `.tag <name>`, `.tag add <name> <content>`, `.tag remove <name>`, "
                "`.tag list [@user]`, `.tag listall`"
            )
            return

        row = await self.get_tag(ctx.guild.id, name)
        if row is None:
            await ctx.send(f"❌ Tag `{name}` doesn't exist.")
            return

        _, content, attachments_json = row
        files = []
        if attachments_json:
            for path in json.loads(attachments_json):
                if os.path.exists(path):
                    files.append(discord.File(path))

        await ctx.send(content=content or None, files=files if files else None)

    @tag.command(name="add")
    async def tag_add(self, ctx, name: str, *, content: str = None):
        existing = await self.get_tag(ctx.guild.id, name)
        if existing is not None:
            await ctx.send(f"❌ Tag `{name}` already exists")
            return

        if not content and not ctx.message.attachments:
            await ctx.send("❌ You need to provide text content and/or attach an image.")
            return

        cursor = await self.bot.tag_db.execute(
            "INSERT INTO tags (name, content, attachments, guild, creator) VALUES (?, ?, ?, ?, ?)",
            (name, content, None, ctx.guild.id, ctx.author.id)
        )
        await self.bot.tag_db.commit()
        tag_id = cursor.lastrowid

        # Save attachments to disk if any
        saved_paths = []
        if ctx.message.attachments:
            guild_dir = os.path.join(TAG_FILES_DIR, str(ctx.guild.id))
            os.makedirs(guild_dir, exist_ok=True)
            for i, attachment in enumerate(ctx.message.attachments):
                safe_filename = f"{tag_id}_{i}_{attachment.filename}"
                path = os.path.join(guild_dir, safe_filename)
                await attachment.save(path)
                saved_paths.append(path)

            await self.bot.tag_db.execute(
                "UPDATE tags SET attachments = ? WHERE id = ?",
                (json.dumps(saved_paths), tag_id)
            )
            await self.bot.tag_db.commit()

        await ctx.send(f"✅ Tag `{name}` added.")

    @tag.command(name="remove")
    async def tag_remove(self, ctx, name: str):
        row = await self.get_tag(ctx.guild.id, name)
        if row is None:
            await ctx.send(f"❌ Tag `{name}` doesn't exist.")
            return

        tag_id, _, attachments_json = row

        is_creator = await self._is_creator(ctx.guild.id, name, ctx.author.id)
        if not (ctx.author.guild_permissions.manage_messages or is_creator):
            await ctx.send("❌ You don't have permission to remove this tag.")
            return

        # Delete stored files
        if attachments_json:
            for path in json.loads(attachments_json):
                if os.path.exists(path):
                    os.remove(path)

        await self.bot.tag_db.execute(
            "DELETE FROM tags WHERE guild = ? AND name = ?", (ctx.guild.id, name)
        )
        await self.bot.tag_db.commit()

        await ctx.send(f"✅ Tag `{name}` removed.")

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

    async def _is_creator(self, guild_id, name, user_id):
        async with self.bot.tag_db.execute(
            "SELECT creator FROM tags WHERE guild = ? AND name = ?", (guild_id, name)
        ) as cursor:
            row = await cursor.fetchone()
            return row is not None and row[0] == user_id


def setup(bot):
    bot.add_cog(TagSystem(bot))