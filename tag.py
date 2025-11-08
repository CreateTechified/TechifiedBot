import discord
from discord.ext import bridge, commands
import os
import json
import re

TAG_FILE = "tags.json"
IMAGE_DIR = "tag_images"

def sanitize_description(text: str) -> str:
    text = re.sub(r"<@!?(\d+)>", "@user", text)
    text = re.sub(r"<#(\d+)>", "#channel", text)
    text = re.sub(r"<@&(\d+)>", "@role", text)
    return text

def load_tags():
    if os.path.exists(TAG_FILE):
        with open(TAG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_tags(tags):
    with open(TAG_FILE, "w", encoding="utf-8") as f:
        json.dump(tags, f, indent=4)

class TagCog(commands.Cog):  # ✅ Use commands.Cog instead of BridgeCog
    def __init__(self, bot):
        self.bot = bot
        self.tags = load_tags()
        os.makedirs(IMAGE_DIR, exist_ok=True)

    @bridge.bridge_group(name="t", description="Tag system", invoke_without_command=True)
    async def t(self, ctx, tag_name: str = None):
        """Main tag group: !t <tag|add|delete|list>"""
        if tag_name:
            tag = self.tags.get(tag_name)
            if not tag:
                return await ctx.respond("❌ Tag not found.")
            await ctx.respond(content=tag["description"], file=discord.File(tag["path"]))
        else:
            await ctx.respond("Usage: `!t <tag|add|delete|list>`")

    @t.command(name="add", description="Add a new tag")
    async def add_tag(self, ctx, name: str, *, description: str = None):
        if name in self.tags:
            return await ctx.respond("❌ Tag already exists.")

        attachments = ctx.message.attachments if ctx.message else ctx.attachments
        if not attachments:
            return await ctx.respond("❌ You must attach an image.")

        attachment = attachments[0]
        if not attachment.filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
            return await ctx.respond("❌ Invalid file type.")

        path = os.path.join(IMAGE_DIR, f"{name}_{attachment.filename}")
        await attachment.save(path)

        desc = sanitize_description(description or "")
        self.tags[name] = {"path": path, "description": desc}
        save_tags(self.tags)

        await ctx.respond(f"✅ Added tag `{name}` successfully!")

    @t.command(name="delete", description="Delete a tag")
    async def delete_tag(self, ctx, name: str):
        tag = self.tags.get(name)
        if not tag:
            return await ctx.respond("❌ Tag not found.")

        if os.path.exists(tag["path"]):
            os.remove(tag["path"])

        del self.tags[name]
        save_tags(self.tags)
        await ctx.respond(f"🗑️ Deleted tag `{name}`")

    @t.command(name="list", description="List all tags")
    async def list_tags(self, ctx):
        if not self.tags:
            return await ctx.respond("📭 No tags available.")
        tag_list = ", ".join(f"`{t}`" for t in self.tags)
        await ctx.respond(f"**Available tags:** {tag_list}")

async def setup(bot):
    await bot.add_cog(TagCog(bot))
