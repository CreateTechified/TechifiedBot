import asyncio
asyncio.set_event_loop(asyncio.new_event_loop())
import discord
from discord.ext import commands
import os
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
    await bot.change_presence(status=discord.Status.online, activity=presence)

@bot.command()
async def ping(ctx):
    await ctx.send("Pong!")

async def main():
    async with bot:
        bot.load_extension('help_cog')
        token = os.getenv("TOKEN")
        if not token:
            print("❌ ERROR: No TOKEN found in .env file!")
            return
        await bot.start(token)

asyncio.run(main())
