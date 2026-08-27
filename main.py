import asyncio

asyncio.set_event_loop(asyncio.new_event_loop())
import discord
from discord.ext import commands
import os
import aiosqlite
from dotenv import load_dotenv

from tag_cog import TagReportView

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(
    command_prefix=".",
    intents=intents,
    auto_sync_commands=True
)

presence = discord.Game("modpack release soon??")

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} ({bot.user.id})")
    await bot.change_presence(status=discord.Status.online)

@bot.command()
async def ping(ctx):
    await ctx.send("Pong!")

async def setup_database(bot):
    bot.tag_db = await aiosqlite.connect("tags.db")

    await bot.tag_db.execute(
        """CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            content TEXT,
            attachments TEXT,
            attachments_size INTEGER NOT NULL DEFAULT 0,
            reported INTEGER NOT NULL DEFAULT 0,
            guild INTEGER NOT NULL,
            creator INTEGER NOT NULL,
            UNIQUE(name, guild)
        )"""
    )
    for migration in (
        "ALTER TABLE tags ADD COLUMN attachments_size INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE tags ADD COLUMN reported INTEGER NOT NULL DEFAULT 0",
    ):
        try:
            await bot.tag_db.execute(migration)
        except aiosqlite.OperationalError:
            pass

    await bot.tag_db.execute(
        """CREATE TABLE IF NOT EXISTS tag_aliases (
            name TEXT NOT NULL,
            guild INTEGER NOT NULL,
            original_name TEXT NOT NULL,
            creator INTEGER NOT NULL,
            UNIQUE(name, guild)
        )"""
    )

    await bot.tag_db.execute(
        """CREATE TABLE IF NOT EXISTS warnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            moderator_id INTEGER NOT NULL,
            reason TEXT,
            timestamp TEXT NOT NULL
        )"""
    )

    await bot.tag_db.commit()

async def register_persistent_views(bot):
    async with bot.tag_db.execute("SELECT guild, name FROM tags WHERE reported = 1") as cursor:
        rows = await cursor.fetchall()
    for guild_id, name in rows:
        bot.add_view(TagReportView(guild_id, name))

async def main():
    async with bot:
        await setup_database(bot)
        await register_persistent_views(bot)

        bot.load_extension('help_cog')
        bot.load_extension('tag_cog')
        bot.load_extension('server_cog')
        bot.load_extension('slash_cog')

        token = os.getenv("DSC_TOKEN")
        if not token:
            print("❌ ERROR: No DSC_TOKEN found in .env file!")
            return
        await bot.start(token)

asyncio.run(main())