import asyncio
asyncio.set_event_loop(asyncio.new_event_loop())
import discord
from discord.ext import commands
import os
from dotenv import load_dotenv
import aiosqlite

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
    await bot.change_presence(status=discord.Status.online, activity=presence)
    bot.db = await aiosqlite.connect("tags.db")
    async with bot.db.cursor() as cursor:
        await cursor.execute("CREATE TABLE IF NOT EXISTS tags (name TEXT, content TEXT, guild INTEGER, creator INTEGER)")

@bot.command()
async def ping(ctx):
    await ctx.send("Pong!")

@bot.command(name="createtag", description="Creates a new tag")
async def create_tag(ctx: commands.Context, name: str, *, content: str):
    async with bot.db.cursor() as cursor:
        await cursor.execute("SELECT content FROM tags WHERE guild = ? AND name = ?", (ctx.guild.id, name))
        data = await cursor.fetchone()
        if data is None: 
            await cursor.execute("INSERT INTO tags (name, content, guild, creator) VALUES (?, ?, ?, ?)", (name, content, ctx.guild.id, ctx.author.id))
            await ctx.send("✅ Tag created successfully!")
        if data:
            await ctx.send("❌ Tag already exists!")

        await bot.db.commet()

async def main():
    async with bot:
        bot.load_extension('help_cog')
        token = os.getenv("TOKEN")
        if not token:
            print("❌ ERROR: No TOKEN found in .env file!")
            return
        await bot.start(token)

asyncio.run(main())
