import discord
from discord.ext import bridge
import os
from dotenv import load_dotenv

load_dotenv()
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = bridge.Bot(command_prefix="!", intents=intents, auto_sync_commands=True)
presence = discord.Game("modpack release soon??")

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} ({bot.user.id})")
    await bot.change_presence(status=discord.Status.online, activity=presence)

@bot.bridge_command()
async def ping(ctx):
    await ctx.respond("Pong!", ephemeral=False)

bot.load_extension("tag")
bot.run(os.getenv("TOKEN"))
