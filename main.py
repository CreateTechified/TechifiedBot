import discord
from discord.ext import commands
import os
import asyncio
from dotenv import load_dotenv

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

bot = commands.Bot(
    command_prefix=".",
    intents=intents,
    loop=loop,
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
        token = os.getenv("TOKEN")
        if not token:
            print("❌ ERROR: No TOKEN found in .env file!")
            return
        await bot.start(token)

if __name__ == "__main__":
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        loop.run_until_complete(bot.close())
    finally:
        loop.close()
