import discord
from discord.ext import bridge, commands
import os
import json
import re

TAG_FILE = "tags.json"
IMAGE_DIR = "tag_images"

os.makedirs(IMAGE_DIR, exist_ok=True)

# -----------------------------
# Helpers
# -----------------------------
def sanitize_description(text: str) -> str:
    """Remove mentions so tags can't ping anyone"""
    if not text:
        return ""
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

# -----------------------------
# Tag Cog
# -----------------------------
class TagCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.tags = load_tags()

    # -----------------------------
    # Main Tag Group & Default invoke
    # -----------------------------
    @bridge.bridge_group(
        name="t",
        description="Tag system: add, delete, list, or show tags",
        invoke_without_command=True  # This is correct
    )
    async def t(self, ctx, tag_name: str = None):
        """
        This is the main group callback.
        It runs when 't' is used without a valid subcommand (like 'add', 'list', etc.)
        """
        # If a subcommand (like 'add') was used, let it run
        if ctx.invoked_subcommand is not None:
            return

        # If no subcommand AND no tag_name, show help
        if not tag_name:
            return await ctx.respond("Usage: `!t <tag_name>` to show a tag, or `!t <add|delete|list>` for other actions.")

        # --- This is the logic from your old 'default' command ---
        tag = self.tags.get(tag_name.lower())
        if not tag:
            return await ctx.respond("❌ Tag not found.")

        # --- FIX: Check if file exists before sending ---
        if not os.path.exists(tag["path"]):
            # You might want to auto-delete this broken tag entry here
            return await ctx.respond(f"❌ Tag `{tag_name}` found, but its image file is missing!")

        await ctx.respond(content=tag["description"], file=discord.File(tag["path"]))

    # -----------------------------
    # View a tag: /t view <tagname>
    # -----------------------------
    @t.command(name="view", description="Shows the content of an existing tag.")
    async def view_tag(self, ctx, tag_name: str):
        tag = self.tags.get(tag_name.lower())
        if not tag:
            return await ctx.respond("❌ Tag not found.")

        content = tag.get("description", "No description provided.")
        tag_path = tag.get("path")
        
        # Prepare file if a path exists and the file is present
        file = None
        if tag_path and os.path.exists(tag_path):
            file = discord.File(tag_path)
        elif tag_path:
            # Handle case where path exists but file is missing
            content += "\n\n⚠️ **Warning:** The attached image file is missing."

        if not content and not file:
            return await ctx.respond("🤷 This tag is empty.")
            
        # If no file, just send the description
        if not file:
            await ctx.respond(content=content)
        # If there is a file, send both
        else:
            await ctx.respond(content=content, file=file)

    # -----------------------------
    # Add a tag
    # -----------------------------
    @t.command(name="add")
    async def add_tag(self, ctx, name: str, *, description: str = None):
        name = name.lower()
        if name in self.tags:
            return await ctx.respond("❌ Tag already exists.")

        attachments = ctx.message.attachments if ctx.message else ctx.attachments
        if not attachments:
            return await ctx.respond("❌ You must attach an image.")

        attachment = attachments[0]
        if not attachment.filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
            return await ctx.respond("❌ Invalid file type.")

        # --- IMPROVEMENT: Cleaner file path ---
        file_extension = os.path.splitext(attachment.filename)[1]
        path = os.path.join(IMAGE_DIR, f"{name}{file_extension}")

        await attachment.save(path)

        desc = sanitize_description(description)
        self.tags[name] = {"path": path, "description": desc}
        save_tags(self.tags)

        await ctx.respond(f"✅ Added tag `{name}` successfully!")

    # -----------------------------
    # Delete a tag (This command was already good!)
    # -----------------------------
    @t.command(name="delete")
    async def delete_tag(self, ctx, name: str):
        name = name.lower()
        tag = self.tags.get(name)
        if not tag:
            return await ctx.respond("❌ Tag not found.")

        # Remove the image file
        if os.path.exists(tag["path"]):
            os.remove(tag["path"])

        del self.tags[name]
        save_tags(self.tags)
        await ctx.respond(f"🗑️ Deleted tag `{name}`")

    # -----------------------------
    # List all tags
    # -----------------------------
    @t.command(name="list")
    async def list_tags(self, ctx):
        if not self.tags:
            return await ctx.respond("📭 No tags available.")

        tag_list = ", ".join(f"`{t}`" for t in sorted(self.tags.keys()))
        response = f"**Available tags:** {tag_list}"

        # --- FIX: Prevent message over 2000 chars ---
        if len(response) > 2000:
            response = f"{response[:1950]}... (list too long to display)"

        await ctx.respond(response)

# -----------------------------
# Setup function for the cog
# -----------------------------
def setup(bot):
    bot.add_cog(TagCog(bot))
