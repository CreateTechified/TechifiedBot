import json
import os
import re

import discord
from discord.ext import commands

TAG_FILE = "tags.json"
ATTACHMENT_DIR = "tag_attachments"
ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".txt", ".pdf"}

os.makedirs(ATTACHMENT_DIR, exist_ok=True)


def sanitize_description(text: str) -> str:
    """Remove mention syntax so tags can't ping users/roles/channels."""
    if not text:
        return ""
    text = re.sub(r"<@!?(\d+)>", "@user", text)
    text = re.sub(r"<#(\d+)>", "#channel", text)
    text = re.sub(r"<@&(\d+)>", "@role", text)
    return text


def load_tags() -> dict:
    if not os.path.exists(TAG_FILE):
        return {}

    with open(TAG_FILE, "r", encoding="utf-8") as file:
        data = json.load(file)

    if isinstance(data, dict):
        return data
    return {}


def save_tags(tags: dict) -> None:
    with open(TAG_FILE, "w", encoding="utf-8") as file:
        json.dump(tags, file, indent=4)


class TagCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.tags = load_tags()

    async def _send_tag(self, ctx: commands.Context, tag_name: str) -> None:
        tag = self.tags.get(tag_name.lower())
        if not tag:
            await ctx.send("❌ Tag not found.")
            return

        description = tag.get("description", "")
        path = tag.get("path")

        if path:
            if not os.path.exists(path):
                await ctx.send(f"❌ Tag `{tag_name}` found, but its attachment file is missing!")
                return
            await ctx.send(content=description or None, file=discord.File(path))
            return

        if description:
            await ctx.send(description)
            return

        await ctx.send("🤷 This tag is empty.")

    @commands.group(name="t", invoke_without_command=True)
    async def t_group(self, ctx: commands.Context, *, tag_name: str = None):
        """Tag system: .t <tag>, .t add, .t delete, .t list"""
        if tag_name is None:
            await ctx.send("Usage: `.t <tag_name>` or `.t <add|delete|list|view> ...`")
            return

        await self._send_tag(ctx, tag_name)

    @t_group.command(name="view")
    async def view_tag(self, ctx: commands.Context, *, tag_name: str):
        """Shows an existing tag."""
        await self._send_tag(ctx, tag_name)

    @t_group.command(name="add")
    async def add_tag(self, ctx: commands.Context, name: str, *, description: str = None):
        """Create a tag with text, attachment, or both."""
        name = name.lower()
        if name in self.tags:
            await ctx.send("❌ Tag already exists.")
            return

        cleaned_description = sanitize_description(description)
        path = None

        if ctx.message.attachments:
            attachment = ctx.message.attachments[0]
            extension = os.path.splitext(attachment.filename)[1].lower()
            if extension not in ALLOWED_EXTENSIONS:
                await ctx.send("❌ Invalid file type.")
                return

            path = os.path.join(ATTACHMENT_DIR, f"{name}{extension}")
            await attachment.save(path)

        if not cleaned_description and not path:
            await ctx.send("❌ Provide tag text, an attachment, or both.")
            return

        self.tags[name] = {"description": cleaned_description, "path": path}
        save_tags(self.tags)
        await ctx.send(f"✅ Added tag `{name}` successfully!")

    @t_group.command(name="delete")
    async def delete_tag(self, ctx: commands.Context, name: str):
        """Delete an existing tag."""
        name = name.lower()
        tag = self.tags.get(name)
        if not tag:
            await ctx.send("❌ Tag not found.")
            return

        path = tag.get("path")
        if path and os.path.exists(path):
            os.remove(path)

        del self.tags[name]
        save_tags(self.tags)
        await ctx.send(f"🗑️ Deleted tag `{name}`")

    @t_group.command(name="list")
    async def list_tags(self, ctx: commands.Context):
        """List all tags."""
        if not self.tags:
            await ctx.send("📭 No tags available.")
            return

        tag_list = ", ".join(f"`{tag}`" for tag in sorted(self.tags.keys()))
        response = f"**Available tags:** {tag_list}"
        if len(response) > 2000:
            response = f"{response[:1950]}... (list too long to display)"

        await ctx.send(response)


# Keep setup non-async to match extension loader expectations.
def setup(bot: commands.Bot):
    bot.add_cog(TagCog(bot))