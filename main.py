import discord
from discord.ext import bridge
import os
from dotenv import load_dotenv

load_dotenv()
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = bridge.Bot(command_prefix="!", intents=intents)
presence = discord.Game("modpack release soon??")
bot.auto_sync_commands = True

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} ({bot.user.id})")
    await bot.change_presence(status=discord.Status.online, activity=presence)

@bot.bridge_command()
async def ping(ctx):
    await ctx.respond("Pong!", ephemeral=False)

# Register extensions within these comments! Below is code to unbreak it.
bot.load_extension("tag")
# Register extensions within these comments! Below is code to unbreak it.
bot.sync_commands()
bot.run(os.getenv("TOKEN"))
