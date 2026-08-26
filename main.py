import asyncio
import re

asyncio.set_event_loop(asyncio.new_event_loop())
import discord
from discord.ext import commands
import os
import aiosqlite
from dotenv import load_dotenv

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

@bot.command()
async def neofetch(ctx):
    try:
        process = await asyncio.create_subprocess_exec(
            "fastfetch", "--pipe", "false",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            await ctx.send(
                f"Error:\n```\n{stderr.decode().strip() or 'Unknown error'}\n```"
            )
            return
        output = stdout.decode("utf-8", errors="replace").strip()
        output = re.sub(r"\x1b\[m", "\x1b[0m", output)
        if len(output) > 1900:
            output = output[:1900] + "\n... [Truncated]"
        await ctx.send(f"```ansi\n{output}\n```")
    except FileNotFoundError:
        await ctx.send("Error: `fastfetch` is not installed or in PATH.")

async def setup_database(bot):
    bot.tag_db = await aiosqlite.connect("tags.db")

    await bot.tag_db.execute(
        """CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            content TEXT,
            attachments TEXT,
            attachments_size INTEGER NOT NULL DEFAULT 0,
            guild INTEGER NOT NULL,
            creator INTEGER NOT NULL,
            UNIQUE(name, guild)
        )"""
    )
    try:
        await bot.tag_db.execute(
            "ALTER TABLE tags ADD COLUMN attachments_size INTEGER NOT NULL DEFAULT 0"
        )
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

async def main():
    async with bot:
        await setup_database(bot)

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