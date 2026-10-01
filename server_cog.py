import discord
from discord.ext import commands
from discord.commands import SlashCommandGroup, Option

from page_embeds import send_paged
from mc_utils import (
    get_api_key, lookup_profile,
    whitelist_add as ws_whitelist_add,
    whitelist_remove as ws_whitelist_remove,
    whitelist_names as ws_whitelist_names,
)

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

NO_KEY_MESSAGE = (
    "❌ No WhitelistSync API key is configured. Add `WLS_TOKEN=...` to the bot's `.env` and restart it."
)


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


class AddServerModal(discord.ui.Modal):

    def __init__(self, cog, name: str, hide_ip: bool):
        super().__init__(title=f"Add server: {name}"[:45])
        self.cog = cog
        self.server_name = name
        self.hide_ip = hide_ip

        self.host_input = discord.ui.InputText(
            label="Server IP / address",
            placeholder="e.g. play.example.com",
            style=discord.InputTextStyle.short,
            required=True,
            max_length=253,
        )
        self.port_input = discord.ui.InputText(
            label="Port (optional)",
            placeholder=f"Leave empty to use {DEFAULT_MC_PORT}",
            style=discord.InputTextStyle.short,
            required=False,
            max_length=5,
        )
        self.add_item(self.host_input)
        self.add_item(self.port_input)

    async def callback(self, interaction: discord.Interaction):
        host = (self.host_input.value or "").strip()
        port_text = (self.port_input.value or "").strip()

        if not port_text and host.count(":") == 1:
            maybe_host, _, maybe_port = host.partition(":")
            if maybe_host and maybe_port.isdigit():
                host, port_text = maybe_host, maybe_port

        if not host or any(c.isspace() for c in host):
            await interaction.response.send_message(
                "❌ That doesn't look like a valid address.", ephemeral=True
            )
            return

        if port_text:
            if not port_text.isdigit() or not (1 <= int(port_text) <= 65535):
                await interaction.response.send_message(
                    "❌ The port must be a number between 1 and 65535.", ephemeral=True
                )
                return
            port = int(port_text)
        else:
            port = DEFAULT_MC_PORT

        if await self.cog._get_server(interaction.guild_id, self.server_name) is not None:
            await interaction.response.send_message(
                f"❌ A server named `{self.server_name}` is already tracked.", ephemeral=True
            )
            return

        await self.cog.bot.tag_db.execute(
            "INSERT INTO mc_servers (guild, name, host, port, creator, hide_ip) VALUES (?, ?, ?, ?, ?, ?)",
            (interaction.guild_id, self.server_name, host, port, interaction.user.id, int(self.hide_ip))
        )
        await self.cog.bot.tag_db.commit()

        shown = "address hidden" if self.hide_ip else f"`{host}:{port}`"
        await interaction.response.send_message(
            f"✅ Now tracking `{self.server_name}` ({shown}). Check it with `.mcstatus {self.server_name}`."
        )


class ServerManagement(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    whitelist_group = SlashCommandGroup("whitelist", "Manage the network-wide whitelist (staff only)")
    server_group = SlashCommandGroup("server", "Manage the Minecraft server(s) tracked in this Discord server")

    @staticmethod
    async def _reply(ctx, *args, **kwargs):
        if isinstance(ctx, discord.ApplicationContext):
            return await ctx.respond(*args, **kwargs)
        return await ctx.send(*args, **kwargs)

    # ---------- server tracking helpers (used by /mcstatus only) ----------

    async def _get_server(self, guild_id: int, name: str):
        async with self.bot.tag_db.execute(
            "SELECT host, port, hide_ip FROM mc_servers WHERE guild = ? AND name = ?",
            (guild_id, name)
        ) as cursor:
            return await cursor.fetchone()

    async def _list_servers(self, guild_id: int):
        async with self.bot.tag_db.execute(
            "SELECT name, host, port, hide_ip FROM mc_servers WHERE guild = ? ORDER BY name",
            (guild_id,)
        ) as cursor:
            return await cursor.fetchall()

    async def server_name_autocomplete(self, ctx: discord.AutocompleteContext):
        guild_id = ctx.interaction.guild_id
        if guild_id is None:
            return []
        servers = await self._list_servers(guild_id)
        typed = (ctx.value or "").lower()
        return [server[0] for server in servers if typed in server[0].lower()][:25]

    # ---------- /server (config) ----------

    @server_group.command(name="add", description="Track a new Minecraft server (staff only)")
    @is_staff()
    async def server_add(
        self, ctx,
        name: Option(str, "A short name for this server, e.g. 'survival'"),
        hide_ip: Option(bool, "Hide the address in /mcstatus and the server list", required=False, default=False),
    ):
        name = name.strip().lower()

        if not name:
            await ctx.respond("❌ Give the server a name.", ephemeral=True)
            return

        if await self._get_server(ctx.guild.id, name) is not None:
            await ctx.respond(
                f"❌ A server named `{name}` is already tracked. Remove it first or pick a different name.",
                ephemeral=True
            )
            return

        await ctx.send_modal(AddServerModal(self, name, hide_ip))

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

    @server_group.command(name="update", description="Update a tracked server's address, port and/or visibility (staff only)")
    @is_staff()
    async def server_update(
        self, ctx,
        name: Option(str, "Server name to update", autocomplete=server_name_autocomplete),
        host: Option(str, "New server address", required=False, default=None),
        port: Option(int, "New server port", required=False, default=None),
        hide_ip: Option(bool, "True to hide the address publicly, False to show it", required=False, default=None),
    ):
        name = name.strip().lower()
        existing = await self._get_server(ctx.guild.id, name)
        if existing is None:
            await ctx.respond(f"❌ No tracked server named `{name}`.", ephemeral=True)
            return

        if host is None and port is None and hide_ip is None:
            await ctx.respond(
                "❌ Provide a new host, port and/or hide_ip setting to update.", ephemeral=True
            )
            return

        old_host, old_port, old_hide_ip = existing
        new_host = host.strip() if host else old_host
        new_port = port if port is not None else old_port
        new_hide_ip = bool(old_hide_ip) if hide_ip is None else hide_ip

        await self.bot.tag_db.execute(
            "UPDATE mc_servers SET host = ?, port = ?, hide_ip = ? WHERE guild = ? AND name = ?",
            (new_host, new_port, int(new_hide_ip), ctx.guild.id, name)
        )
        await self.bot.tag_db.commit()

        shown = "address hidden" if new_hide_ip else f"`{new_host}:{new_port}`"
        await ctx.respond(f"✅ Updated `{name}` ({shown}).")

    async def _send_server_list(self, ctx):
        servers = await self._list_servers(ctx.guild.id)

        if not servers:
            await self._reply(ctx, "No servers are tracked here yet. An admin can add one with `/server add`.")
            return

        items = [
            f"`{name}` — " + ("*address hidden*" if hide_ip else f"`{host}:{port}`")
            for name, host, port, hide_ip in servers
        ]
        await send_paged(
            ctx, f"🖥️ Minecraft servers in {ctx.guild.name}",
            items, discord.Color.blurple(), noun="server(s)"
        )

    @server_group.command(name="list", description="List every Minecraft server tracked in this Discord server")
    async def server_list(self, ctx):
        await self._send_server_list(ctx)

    @commands.command(name="servers")
    @commands.guild_only()
    async def servers_prefix(self, ctx):
        await self._send_server_list(ctx)

    # ---------- mcstatus ----------

    async def _resolve_single_server(self, guild_id: int, name):
        servers = await self._list_servers(guild_id)
        if not servers:
            return None, "❌ No servers are tracked here yet. An admin can add one with `/server add`."

        if name:
            name = name.strip().lower()
            match = next((s for s in servers if s[0] == name), None)
            if match is None:
                names = ", ".join(f"`{s[0]}`" for s in servers)
                return None, f"❌ No tracked server named `{name}`. Available: {names}"
            return match, None

        if len(servers) == 1:
            return servers[0], None

        names = ", ".join(f"`{s[0]}`" for s in servers)
        return None, f"❌ Multiple servers are tracked here — specify one: {names}"

    async def _mcstatus_embed(self, guild_id: int, name):
        if JavaServer is None:
            return None, "❌ The `mcstatus` package isn't installed on the bot host, so I can't check server status."

        match, error = await self._resolve_single_server(guild_id, name)
        if error:
            return None, error

        server_name, host, port, hide_ip = match

        try:
            mc_server = JavaServer.lookup(f"{host}:{port}")
            status = await mc_server.async_status()
        except Exception:
            embed = discord.Embed(
                title=f"🔴 {server_name}",
                description="This server appears to be offline or unreachable.",
                color=discord.Color.red()
            )
            if not hide_ip:
                embed.add_field(name="Address", value=f"`{host}:{port}`")
            return embed, None

        description = status.description
        motd = description.to_plain() if hasattr(description, "to_plain") else str(description)
        motd = motd.strip()[:256] if motd else None

        embed = discord.Embed(title=f"🟢 {server_name}", description=motd, color=discord.Color.green())
        if not hide_ip:
            embed.add_field(name="Address", value=f"`{host}:{port}`", inline=True)
        embed.add_field(name="Version", value=status.version.name, inline=True)
        embed.add_field(name="Players", value=f"{status.players.online}/{status.players.max}", inline=True)

        if status.players.sample:
            sample_names = ", ".join(p.name for p in status.players.sample[:10])
            embed.add_field(name="Online now", value=sample_names, inline=False)

        latency = getattr(status, "latency", None)
        if latency is not None:
            embed.set_footer(text=f"Ping: {round(latency)} ms")

        return embed, None

    @discord.slash_command(name="mcstatus", description="Check whether a tracked Minecraft server is online")
    async def mcstatus(
        self, ctx,
        name: Option(
            str, "Server name (see /server list); optional if only one server is tracked",
            required=False, default=None, autocomplete=server_name_autocomplete
        ),
    ):
        await ctx.defer()

        embed, error = await self._mcstatus_embed(ctx.guild.id, name)
        if error:
            await ctx.respond(error)
            return

        await ctx.respond(embed=embed)

    @commands.command(name="mcstatus")
    @commands.guild_only()
    async def mcstatus_prefix(self, ctx, name: str = None):
        async with ctx.typing():
            embed, error = await self._mcstatus_embed(ctx.guild.id, name)

        if error:
            await ctx.send(error)
            return

        await ctx.send(embed=embed)

    # ---------- whitelist (global: one WhitelistSync key for everything) ----------

    @whitelist_group.command(name="list", description="List all whitelisted players")
    @is_staff()
    async def whitelist_list(self, ctx):
        if not get_api_key():
            await ctx.respond(NO_KEY_MESSAGE, ephemeral=True)
            return

        await ctx.defer()

        names = await ws_whitelist_names()
        if names is None:
            await ctx.respond("❌ WhitelistSync API error while reading the whitelist.")
            return

        if not names:
            await ctx.respond("*No players whitelisted.*")
            return

        await send_paged(
            ctx, "📑 Whitelisted players",
            [f"`{name}`" for name in sorted(names)],
            discord.Color.blurple(), noun="player(s)"
        )

    @whitelist_group.command(name="add", description="Add a player to the whitelist")
    @is_staff()
    async def whitelist_add(
        self, ctx,
        name: Option(str, "Minecraft username"),
    ):
        if not get_api_key():
            await ctx.respond(NO_KEY_MESSAGE, ephemeral=True)
            return

        await ctx.defer()

        state, canonical, player_uuid = await lookup_profile(name)
        if state == "not_found":
            await ctx.respond(f"❌ Couldn't find a Minecraft account named `{name}`.")
            return
        if state == "error":
            await ctx.respond("❌ I couldn't reach Mojang to check that name. Try again in a minute.")
            return

        ok, code = await ws_whitelist_add(player_uuid)
        if not ok:
            detail = f" (API returned {code})" if code else ""
            await ctx.respond(f"❌ Failed to whitelist `{canonical}`{detail}.")
            return

        await ctx.respond(f"✅ Whitelisted `{canonical}`.")

    @whitelist_group.command(name="remove", description="Remove a player from the whitelist")
    @is_staff()
    async def whitelist_remove(
        self, ctx,
        name: Option(str, "Minecraft username"),
    ):
        if not get_api_key():
            await ctx.respond(NO_KEY_MESSAGE, ephemeral=True)
            return

        await ctx.defer()

        state, canonical, player_uuid = await lookup_profile(name)
        if state == "not_found":
            await ctx.respond(f"❌ Couldn't find a Minecraft account named `{name}`.")
            return
        if state == "error":
            await ctx.respond("❌ I couldn't reach Mojang to check that name. Try again in a minute.")
            return

        ok, code = await ws_whitelist_remove(player_uuid)
        if not ok:
            detail = f" (API returned {code})" if code else ""
            await ctx.respond(f"❌ Failed to unwhitelist `{canonical}`{detail}.")
            return

        await ctx.respond(f"✅ Unwhitelisted `{canonical}`.")

    @whitelist_group.command(name="check", description="Check whether a player is whitelisted")
    @is_staff()
    async def whitelist_check(
        self, ctx,
        name: Option(str, "Minecraft username"),
    ):
        if not get_api_key():
            await ctx.respond(NO_KEY_MESSAGE, ephemeral=True)
            return

        await ctx.defer()

        names = await ws_whitelist_names()
        if names is None:
            await ctx.respond("❌ WhitelistSync API error while reading the whitelist.")
            return

        if name.lower() in names:
            await ctx.respond(f"✅ `{name}` is whitelisted.")
        else:
            await ctx.respond(f"❌ `{name}` is **not** whitelisted.")


def setup(bot):
    bot.add_cog(ServerManagement(bot))