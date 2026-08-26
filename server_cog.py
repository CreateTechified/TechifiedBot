import os
import asyncio
import uuid
import discord
import requests
from discord.ext import commands
from discord.commands import SlashCommandGroup, Option

ADMIN_ROLE_ID = 1222456633511378965
MODERATOR_ROLE_ID = 1421877616272605326
OWNER_ROLE_ID = 1286650794053210122
ALLOWED_ROLE_IDS = {ADMIN_ROLE_ID, MODERATOR_ROLE_ID, OWNER_ROLE_ID}


def is_staff():
    async def predicate(ctx: discord.ApplicationContext) -> bool:
        member = ctx.author
        if not isinstance(member, discord.Member):
            await ctx.respond("❌ This command can only be used in a server.", ephemeral=True)
            return False

        role_ids = {role.id for role in member.roles}
        if role_ids & ALLOWED_ROLE_IDS:
            return True

        await ctx.respond("❌ You don't have permission to use this command.", ephemeral=True)
        return False

    return commands.check(predicate)


class ServerManagement(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.whitelistsync_api_key = os.getenv("WLS_TOKEN")
        if not self.whitelistsync_api_key:
            print("❌ ERROR: No WLS_TOKEN found in .env file!")
        self.headers = {
            "X-API-KEY": self.whitelistsync_api_key or ""
        }

    whitelist_group = SlashCommandGroup("whitelist", "Manage the Minecraft server whitelist (staff only)")

    async def _get_uuid(self, name: str):
        """Looks up a Mojang UUID for a username. Returns None if the account doesn't exist."""
        resp = await asyncio.to_thread(
            requests.get, f"https://api.minecraftservices.com/minecraft/profile/lookup/name/{name}"
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if "id" not in data:
            return None
        return str(uuid.UUID(data["id"]))

    @whitelist_group.command(name="list", description="List all whitelisted players")
    @is_staff()
    async def whitelist_list(self, ctx):
        await ctx.defer()

        resp = await asyncio.to_thread(
            requests.get, "https://whitelistsync.com/api/whitelist", headers=self.headers
        )
        whitelist = resp.json()
        usernames = [player["name"] for player in whitelist if "name" in player]
        names = ", ".join(f"`{player}`" for player in usernames) if usernames else "*No players whitelisted.*"

        embed = discord.Embed(
            title="📑 All whitelisted players",
            description=names,
            color=discord.Color.blurple()
        )
        embed.set_footer(text=f"{len(whitelist)} player(s)")
        await ctx.respond(embed=embed)

    @whitelist_group.command(name="add", description="Add a player to the whitelist")
    @is_staff()
    async def whitelist_add(self, ctx, name: Option(str, "Minecraft username")):
        await ctx.defer()

        player_uuid = await self._get_uuid(name)
        if player_uuid is None:
            await ctx.respond(f"❌ Couldn't find a Minecraft account named `{name}`.")
            return

        resp = await asyncio.to_thread(
            requests.post, "https://whitelistsync.com/api/whitelist",
            headers=self.headers, json={"uuid": player_uuid}
        )
        if resp.status_code >= 400:
            await ctx.respond(f"❌ Failed to whitelist `{name}` (API returned {resp.status_code}).")
            return

        await ctx.respond(f"✅ Whitelisted user `{name}`.")

    @whitelist_group.command(name="remove", description="Remove a player from the whitelist")
    @is_staff()
    async def whitelist_remove(self, ctx, name: Option(str, "Minecraft username")):
        await ctx.defer()

        player_uuid = await self._get_uuid(name)
        if player_uuid is None:
            await ctx.respond(f"❌ Couldn't find a Minecraft account named `{name}`.")
            return

        resp = await asyncio.to_thread(
            requests.delete, f"https://whitelistsync.com/api/whitelist/{player_uuid}", headers=self.headers
        )
        if resp.status_code >= 400:
            await ctx.respond(f"❌ Failed to unwhitelist `{name}` (API returned {resp.status_code}).")
            return

        await ctx.respond(f"✅ Unwhitelisted user `{name}`.")


def setup(bot):
    bot.add_cog(ServerManagement(bot))