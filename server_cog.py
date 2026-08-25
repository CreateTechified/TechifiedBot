import os

import discord
import requests
import uuid
from discord.ext import commands


class ServerManagement(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.whitelistsync_api_key = os.getenv("WLS_TOKEN")
        if not self.whitelistsync_api_key:
            print("❌ ERROR: No WLS_TOKEN found in .env file!")
            return
        self.headers = {
            "X-API-KEY": self.whitelistsync_api_key or ""
        }

    @commands.group(name="whitelist", invoke_without_command=True)
    @commands.has_permissions(manage_guild=True)
    async def whitelist(self, ctx):
        if ctx.author.guild_permissions.manage_guild:
            print("hi")

    @whitelist.command(name="list")
    @commands.has_permissions(manage_guild=True)
    async def whitelist_list(self, ctx):
        whitelist = requests.get("https://whitelistsync.com/api/whitelist", headers=self.headers).json()
        usernames = [player["name"] for player in whitelist if "name" in player]
        names = ", ".join(f"`{player}`" for player in usernames)
        embed = discord.Embed(
            title=f"📑 All whitelisted players",
            description=names,
            color=discord.Color.blurple()
        )
        embed.set_footer(text=f"{len(whitelist)} player(s)")
        await ctx.send(embed=embed)

    @whitelist.command(name="add")
    @commands.has_permissions(manage_guild=True)
    async def whitelist_add(self, ctx, name: str):
        mojang = requests.get(f"https://api.minecraftservices.com/minecraft/profile/lookup/name/{name}").json()
        inuuid = str(uuid.UUID(mojang["id"]))
        requests.post("https://whitelistsync.com/api/whitelist", headers=self.headers, json={"uuid": inuuid})
        await ctx.send(f"✅ Whitelisted user `{name}`.")

    @whitelist.command(name="remove")
    @commands.has_permissions(manage_guild=True)
    async def whitelist_remove(self, ctx, name: str):
        mojang = requests.get(f"https://api.minecraftservices.com/minecraft/profile/lookup/name/{name}").json()
        inuuid = str(uuid.UUID(mojang["id"]))
        requests.delete(f"https://whitelistsync.com/api/whitelist/{inuuid}", headers=self.headers)
        await ctx.send(f"✅ Unwhitelisted user `{name}`.")

def setup(bot):
    bot.add_cog(ServerManagement(bot))