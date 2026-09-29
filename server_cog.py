import os
import asyncio
import uuid
import discord
import requests
from discord.ext import commands
from discord.commands import SlashCommandGroup, Option

from page_embeds import send_paged

try:
    from mcstatus import JavaServer
except ImportError:
    JavaServer = None
    print("⚠️ WARNING: mcstatus is not installed! Run `pip install mcstatus` to enable /mcstatus.")

ADMIN_ROLE_ID = 1222456633511378965
MODERATOR_ROLE_ID = 1421877616272605326
OWNER_ROLE_ID = 1286650794053210122
ALLOWED_ROLE_IDS = {ADMIN_ROLE_ID, MODERATOR_ROLE_ID, OWNER_ROLE_ID}

DEFAULT_MC_PORT = 25565

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
    server_group = SlashCommandGroup("server", "Manage the Minecraft server(s) tracked in this Discord server")

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

    # ---------- multi-server config helpers ----------

    async def _get_server(self, guild_id: int, name: str):
        async with self.bot.tag_db.execute(
            "SELECT host, port FROM mc_servers WHERE guild = ? AND name = ?",
            (guild_id, name)
        ) as cursor:
            return await cursor.fetchone()

    async def _list_servers(self, guild_id: int):
        async with self.bot.tag_db.execute(
            "SELECT name, host, port FROM mc_servers WHERE guild = ? ORDER BY name",
            (guild_id,)
        ) as cursor:
            return await cursor.fetchall()

    async def server_name_autocomplete(self, ctx: discord.AutocompleteContext):
        guild_id = ctx.interaction.guild_id
        if guild_id is None:
            return []
        servers = await self._list_servers(guild_id)
        typed = (ctx.value or "").lower()
        return [name for name, _, _ in servers if typed in name.lower()][:25]

    # ---------- /server (config) ----------

    @server_group.command(name="add", description="Track a new Minecraft server (staff only)")
    @is_staff()
    async def server_add(
        self, ctx,
        name: Option(str, "A short name for this server, e.g. 'survival'"),
        host: Option(str, "Server address, e.g. play.example.com"),
        port: Option(int, "Server port", required=False, default=DEFAULT_MC_PORT),
    ):
        name = name.strip().lower()
        host = host.strip()

        if not name:
            await ctx.respond("❌ Give the server a name.", ephemeral=True)
            return

        if await self._get_server(ctx.guild.id, name) is not None:
            await ctx.respond(
                f"❌ A server named `{name}` is already tracked. Remove it first or pick a different name.",
                ephemeral=True
            )
            return

        await self.bot.tag_db.execute(
            "INSERT INTO mc_servers (guild, name, host, port, creator) VALUES (?, ?, ?, ?, ?)",
            (ctx.guild.id, name, host, port, ctx.author.id)
        )
        await self.bot.tag_db.commit()

        await ctx.respond(f"✅ Now tracking `{name}` (`{host}:{port}`). Check it with `/mcstatus {name}`.")

    @server_group.command(name="remove", description="Stop tracking a Minecraft server (staff only)")
    @is_staff()
    async def server_remove(
        self, ctx,
        name: Option(str, "Server name to remove", autocomplete=server_name_autocomplete),
    ):
        name = name.strip().lower()

        cursor = await self.bot.tag_db.execute(
            "DELETE FROM mc_servers WHERE guild = ? AND name = ?", (ctx.guild.id, name)
        )
        await self.bot.tag_db.commit()
        removed = cursor.rowcount
        await cursor.close()

        if not removed:
            await ctx.respond(f"❌ No tracked server named `{name}`.", ephemeral=True)
            return

        await ctx.respond(f"✅ Stopped tracking `{name}`.")

    @server_group.command(name="update", description="Update a tracked server's address and/or port (staff only)")
    @is_staff()
    async def server_update(
        self, ctx,
        name: Option(str, "Server name to update", autocomplete=server_name_autocomplete),
        host: Option(str, "New server address", required=False, default=None),
        port: Option(int, "New server port", required=False, default=None),
    ):
        name = name.strip().lower()
        existing = await self._get_server(ctx.guild.id, name)
        if existing is None:
            await ctx.respond(f"❌ No tracked server named `{name}`.", ephemeral=True)
            return

        if host is None and port is None:
            await ctx.respond("❌ Provide a new host and/or port to update.", ephemeral=True)
            return

        old_host, old_port = existing
        new_host = host.strip() if host else old_host
        new_port = port if port is not None else old_port

        await self.bot.tag_db.execute(
            "UPDATE mc_servers SET host = ?, port = ? WHERE guild = ? AND name = ?",
            (new_host, new_port, ctx.guild.id, name)
        )
        await self.bot.tag_db.commit()

        await ctx.respond(f"✅ Updated `{name}` to `{new_host}:{new_port}`.")

    @server_group.command(name="list", description="List every Minecraft server tracked in this Discord server")
    async def server_list(self, ctx):
        servers = await self._list_servers(ctx.guild.id)

        if not servers:
            await ctx.respond("No servers are tracked here yet. Staff can add one with `/server add`.")
            return

        await send_paged(
            ctx, f"🖥️ Minecraft servers in {ctx.guild.name}",
            [f"`{name}` — `{host}:{port}`" for name, host, port in servers],
            discord.Color.blurple(), noun="server(s)"
        )

    # ---------- /mcstatus ----------

    @discord.slash_command(name="mcstatus", description="Check whether a tracked Minecraft server is online")
    async def mcstatus(
        self, ctx,
        name: Option(
            str, "Server name (see /server list); optional if only one server is tracked",
            required=False, default=None, autocomplete=server_name_autocomplete
        ),
    ):
        await ctx.defer()

        if JavaServer is None:
            await ctx.respond("❌ The `mcstatus` package isn't installed on the bot host, so I can't check server status.")
            return

        servers = await self._list_servers(ctx.guild.id)
        if not servers:
            await ctx.respond("❌ No servers are tracked here yet. Staff can add one with `/server add`.")
            return

        if name:
            name = name.strip().lower()
            match = next((s for s in servers if s[0] == name), None)
            if match is None:
                names = ", ".join(f"`{s[0]}`" for s in servers)
                await ctx.respond(f"❌ No tracked server named `{name}`. Available: {names}")
                return
        elif len(servers) == 1:
            match = servers[0]
        else:
            names = ", ".join(f"`{s[0]}`" for s in servers)
            await ctx.respond(f"❌ Multiple servers are tracked here — specify one: {names}")
            return

        server_name, host, port = match

        try:
            mc_server = JavaServer.lookup(f"{host}:{port}")
            status = await mc_server.async_status()
        except Exception:
            embed = discord.Embed(
                title=f"🔴 {server_name}",
                description="This server appears to be offline or unreachable.",
                color=discord.Color.red()
            )
            embed.add_field(name="Address", value=f"`{host}:{port}`")
            await ctx.respond(embed=embed)
            return

        description = status.description
        motd = description.to_plain() if hasattr(description, "to_plain") else str(description)
        motd = motd.strip()[:256] if motd else None

        embed = discord.Embed(title=f"🟢 {server_name}", description=motd, color=discord.Color.green())
        embed.add_field(name="Address", value=f"`{host}:{port}`", inline=True)
        embed.add_field(name="Version", value=status.version.name, inline=True)
        embed.add_field(name="Players", value=f"{status.players.online}/{status.players.max}", inline=True)

        if status.players.sample:
            sample_names = ", ".join(p.name for p in status.players.sample[:10])
            embed.add_field(name="Online now", value=sample_names, inline=False)

        latency = getattr(status, "latency", None)
        if latency is not None:
            embed.set_footer(text=f"Ping: {round(latency)} ms")

        await ctx.respond(embed=embed)

    # ---------- whitelist ----------

    @whitelist_group.command(name="list", description="List all whitelisted players")
    @is_staff()
    async def whitelist_list(self, ctx):
        await ctx.defer()

        resp = await asyncio.to_thread(
            requests.get, "https://whitelistsync.com/api/whitelist", headers=self.headers
        )
        if resp.status_code != 200:
            await ctx.respond(f"❌ WhitelistSync API returned an error (status {resp.status_code}).")
            return

        try:
            whitelist = resp.json()
        except ValueError:
            await ctx.respond("❌ WhitelistSync returned an unexpected response. Check that WLS_TOKEN is set correctly.")
            return

        usernames = [player["name"] for player in whitelist if "name" in player]
        if not usernames:
            await ctx.respond("*No players whitelisted.*")
            return

        await send_paged(
            ctx, "📑 All whitelisted players",
            [f"`{name}`" for name in usernames],
            discord.Color.blurple(), noun="player(s)"
        )

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

    @whitelist_group.command(name="check", description="Check whether a player is whitelisted")
    @is_staff()
    async def whitelist_check(self, ctx, name: Option(str, "Minecraft username")):
        await ctx.defer()

        resp = await asyncio.to_thread(
            requests.get, "https://whitelistsync.com/api/whitelist", headers=self.headers
        )
        if resp.status_code != 200:
            await ctx.respond(f"❌ WhitelistSync API returned an error (status {resp.status_code}).")
            return

        try:
            whitelist = resp.json()
        except ValueError:
            await ctx.respond("❌ WhitelistSync returned an unexpected response. Check that WLS_TOKEN is set correctly.")
            return

        found = any(player.get("name", "").lower() == name.lower() for player in whitelist)
        if found:
            await ctx.respond(f"✅ `{name}` is whitelisted.")
        else:
            await ctx.respond(f"❌ `{name}` is **not** whitelisted.")


def setup(bot):
    bot.add_cog(ServerManagement(bot))