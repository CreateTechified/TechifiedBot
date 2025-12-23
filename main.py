import discord
from discord.ext import commands
import os
from dotenv import load_dotenv

load_dotenv()
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix=".", intents=intents, auto_sync_commands=True)
presence = discord.Game("modpack release soon??")

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} ({bot.user.id})")
    await bot.change_presence(status=discord.Status.online, activity=presence)

@bot.command()
async def ping(ctx):
    await ctx.send("Pong!")

bot.run(os.getenv("TOKEN"))
