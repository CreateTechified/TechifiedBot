import re

import discord
from discord.ext import commands
from discord.commands import SlashCommandGroup, Option

# --- CONFIGURATION ---
APPLICATION_CHANNEL_ID = 1318925028586291283

ADMIN_ROLE_ID = 1222456633511378965
MODERATOR_ROLE_ID = 1421877616272605326
OWNER_ROLE_ID = 1286650794053210122
STAFF_ROLE_IDS = {ADMIN_ROLE_ID, MODERATOR_ROLE_ID, OWNER_ROLE_ID}

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")

STATUS_STYLE = {
    "pending": (discord.Color.gold(), "📝 Whitelist Application"),
    "accepted": (discord.Color.green(), "✅ Accepted"),
    "denied": (discord.Color.red(), "❌ Rejected"),
}


def is_staff_member(user) -> bool:
    if not isinstance(user, discord.Member):
        return False
    return bool({role.id for role in user.roles} & STAFF_ROLE_IDS)


def build_application_embed(app_id, user, server_name, mc_username, reason):
    color, title = STATUS_STYLE["pending"]
    embed = discord.Embed(title=title, color=color, timestamp=discord.utils.utcnow())
    embed.add_field(name="Applicant", value=f"{user.mention} (`{user.id}`)", inline=False)
    embed.add_field(name="Server", value=f"`{server_name}`", inline=True)
    embed.add_field(name="Minecraft username", value=f"`{mc_username}`", inline=True)
    embed.add_field(name="Reason", value=reason[:1024], inline=False)
    embed.set_footer(text=f"Application #{app_id}")
    return embed


async def finalize_application(interaction: discord.Interaction, view, new_status: str, reason: str = None):
    db = interaction.client.tag_db
    message = interaction.message

    async with db.execute(
        "SELECT id, user_id, server_name, status FROM applications WHERE message_id = ?",
        (message.id,)
    ) as cursor:
        row = await cursor.fetchone()

    if row is None:
        await interaction.response.send_message("❌ I have no record of this application.", ephemeral=True)
        return

    app_id, user_id, server_name, status = row

    cursor = await db.execute(
        "UPDATE applications SET status = ?, reviewer_id = ?, reviewed_at = ? "
        "WHERE id = ? AND status = 'pending'",
        (new_status, interaction.user.id, discord.utils.utcnow().isoformat(), app_id)
    )
    await db.commit()
    changed = cursor.rowcount
    await cursor.close()

    if not changed:
        await interaction.response.send_message(
            f"⚠️ This application was already **{status}**.", ephemeral=True
        )
        return

    embed = message.embeds[0] if message.embeds else discord.Embed()
    color, title = STATUS_STYLE[new_status]
    embed.color = color
    embed.title = title
    embed.add_field(
        name="Accepted by" if new_status == "accepted" else "Denied by",
        value=interaction.user.mention, inline=False
    )
    if reason:
        embed.add_field(name="Reason", value=reason[:1024], inline=False)

    for item in view.children:
        item.disabled = True
    await interaction.response.edit_message(embed=embed, view=view)

    try:
        applicant = await interaction.client.fetch_user(user_id)
        if new_status == "accepted":
            text = f"✅ Your whitelist application for **{server_name}** was accepted!"
        else:
            text = f"❌ Your whitelist application for **{server_name}** was denied."
            if reason:
                text += f"\n**Reason:** {reason}"
        await applicant.send(text)
    except (discord.Forbidden, discord.HTTPException):
        pass


# ---------- application form (popup) ----------

class ApplicationModal(discord.ui.Modal):
    def __init__(self, cog, server_name: str):
        super().__init__(title=f"Whitelist: {server_name}"[:45])
        self.cog = cog
        self.server_name = server_name

        # PLACEHOLDER QUESTIONS - i couldn't think of anything else :DD
        self.username_input = discord.ui.InputText(
            label="Minecraft username",
            placeholder="Your in-game name",
            style=discord.InputTextStyle.short,
            min_length=3, max_length=16, required=True,
        )
        self.reason_input = discord.ui.InputText(
            label="Why do you want to join?",
            style=discord.InputTextStyle.paragraph,
            max_length=1000, required=True,
        )
        self.add_item(self.username_input)
        self.add_item(self.reason_input)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        username = (self.username_input.value or "").strip()
        reason = (self.reason_input.value or "").strip()

        if not USERNAME_RE.match(username):
            await interaction.followup.send(
                "❌ That isn't a valid Minecraft username (3-16 letters, numbers or underscores).",
                ephemeral=True
            )
            return

        db = self.cog.bot.tag_db
        guild_id = interaction.guild_id

        async with db.execute(
            "SELECT 1 FROM mc_servers WHERE guild = ? AND name = ?", (guild_id, self.server_name)
        ) as cursor:
            if await cursor.fetchone() is None:
                await interaction.followup.send(
                    f"❌ `{self.server_name}` isn't a tracked server anymore.", ephemeral=True
                )
                return

        async with db.execute(
            "SELECT id FROM applications WHERE guild = ? AND user_id = ? AND server_name = ? "
            "AND status = 'pending'",
            (guild_id, interaction.user.id, self.server_name)
        ) as cursor:
            existing = await cursor.fetchone()
        if existing is not None:
            await interaction.followup.send(
                f"⚠️ You already have a pending application (#{existing[0]}) for `{self.server_name}`.",
                ephemeral=True
            )
            return

        channel = self.cog.bot.get_channel(APPLICATION_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.cog.bot.fetch_channel(APPLICATION_CHANNEL_ID)
            except discord.HTTPException:
                channel = None
        if channel is None:
            await interaction.followup.send(
                "❌ I can't reach the applications channel right now. Please tell a staff member.",
                ephemeral=True
            )
            return

        cursor = await db.execute(
            "INSERT INTO applications (guild, user_id, server_name, mc_username, reason, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (guild_id, interaction.user.id, self.server_name, username, reason,
             discord.utils.utcnow().isoformat())
        )
        await db.commit()
        app_id = cursor.lastrowid
        await cursor.close()

        embed = build_application_embed(app_id, interaction.user, self.server_name, username, reason)
        try:
            msg = await channel.send(
                embed=embed, view=ReviewView(),
                allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.HTTPException:
            await db.execute("DELETE FROM applications WHERE id = ?", (app_id,))
            await db.commit()
            await interaction.followup.send(
                "❌ I couldn't post your application. Please tell a staff member.", ephemeral=True
            )
            return

        await db.execute("UPDATE applications SET message_id = ? WHERE id = ?", (msg.id, app_id))
        await db.commit()

        await interaction.followup.send(
            f"✅ Application #{app_id} for `{self.server_name}` submitted! Staff will review it soon.",
            ephemeral=True
        )


# ---------- choosing a server ----------

class ServerSelectView(discord.ui.View):

    def __init__(self, cog, names, owner_id=None):
        super().__init__(timeout=120)
        self.cog = cog
        self.owner_id = owner_id

        select = discord.ui.Select(
            placeholder="Choose a server to apply for...",
            options=[discord.SelectOption(label=name) for name in names[:25]],
        )
        select.callback = self._picked
        self.select = select
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.owner_id is not None and interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "❌ This menu isn't for you. Run `.apply` yourself.", ephemeral=True
            )
            return False
        return True

    async def _picked(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ApplicationModal(self.cog, self.select.values[0]))


class StartApplicationView(discord.ui.View):

    def __init__(self, cog, server_name: str, owner_id: int):
        super().__init__(timeout=120)
        self.cog = cog
        self.server_name = server_name
        self.owner_id = owner_id
        self.apply_button.label = f"Apply for {server_name}"[:80]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "❌ This button isn't for you. Run `.apply` yourself.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Apply", style=discord.ButtonStyle.success, emoji="📝")
    async def apply_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.send_modal(ApplicationModal(self.cog, self.server_name))


# ---------- persistent views ----------

class ApplyPanelView(discord.ui.View):

    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Apply for Whitelist", style=discord.ButtonStyle.success,
        emoji="📝", custom_id="application_panel_apply"
    )
    async def apply(self, button: discord.ui.Button, interaction: discord.Interaction):
        names = await self.cog._server_names(interaction.guild_id)
        if not names:
            await interaction.response.send_message(
                "❌ There are no servers open for applications right now.", ephemeral=True
            )
            return

        if len(names) == 1:
            await interaction.response.send_modal(ApplicationModal(self.cog, names[0]))
            return

        await interaction.response.send_message(
            "Which server do you want to apply for?",
            view=ServerSelectView(self.cog, names, owner_id=interaction.user.id),
            ephemeral=True
        )


class DenyReasonModal(discord.ui.Modal):
    def __init__(self, parent_view: "ReviewView"):
        super().__init__(title="Deny Application")
        self.parent_view = parent_view
        self.reason_input = discord.ui.InputText(
            label="Reason (optional)",
            style=discord.InputTextStyle.paragraph,
            placeholder="Shown to the applicant",
            required=False, max_length=500,
        )
        self.add_item(self.reason_input)

    async def callback(self, interaction: discord.Interaction):
        reason = (self.reason_input.value or "").strip() or None
        await finalize_application(interaction, self.parent_view, "denied", reason)


class ReviewView(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, custom_id="application_accept")
    async def accept(self, button: discord.ui.Button, interaction: discord.Interaction):
        if not is_staff_member(interaction.user):
            await interaction.response.send_message("❌ You don't have permission to do that.", ephemeral=True)
            return
        await finalize_application(interaction, self, "accepted")

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, custom_id="application_deny")
    async def deny(self, button: discord.ui.Button, interaction: discord.Interaction):
        if not is_staff_member(interaction.user):
            await interaction.response.send_message("❌ You don't have permission to do that.", ephemeral=True)
            return
        await interaction.response.send_modal(DenyReasonModal(self))


# ---------- the cog ----------

class Applications(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        bot.add_view(ApplyPanelView(self))
        bot.add_view(ReviewView())

    application_group = SlashCommandGroup("application", "Whitelist application system (admin only)")

    async def _server_names(self, guild_id: int):
        async with self.bot.tag_db.execute(
            "SELECT name FROM mc_servers WHERE guild = ? ORDER BY name", (guild_id,)
        ) as cursor:
            rows = await cursor.fetchall()
        return [row[0] for row in rows]

    @commands.command(name="apply")
    @commands.guild_only()
    async def apply_prefix(self, ctx, server: str = None):
        names = await self._server_names(ctx.guild.id)
        if not names:
            await ctx.send("❌ There are no servers open for applications right now.")
            return

        if server:
            server = server.strip().lower()
            if server not in names:
                available = ", ".join(f"`{n}`" for n in names)
                await ctx.send(f"❌ No server named `{server}`. Available: {available}")
                return
        elif len(names) == 1:
            server = names[0]

        if server:
            await ctx.send(
                f"Click the button below to apply for `{server}`.",
                view=StartApplicationView(self, server, ctx.author.id)
            )
        else:
            await ctx.send(
                "Which server do you want to apply for?",
                view=ServerSelectView(self, names, owner_id=ctx.author.id)
            )

    @application_group.command(name="panel", description="Post a permanent 'Apply' button in a channel")
    async def application_panel(
        self, ctx,
        channel: Option(discord.TextChannel, "Channel to post in (defaults to here)", required=False, default=None),
    ):
        target = channel or ctx.channel
        embed = discord.Embed(
            title="📝 Whitelist Applications",
            description="Click the button below to apply for access to one of our servers.",
            color=discord.Color.blurple()
        )
        try:
            await target.send(embed=embed, view=ApplyPanelView(self))
        except discord.Forbidden:
            await ctx.respond(f"❌ I can't send messages in {target.mention}.", ephemeral=True)
            return

        await ctx.respond(f"✅ Application panel posted in {target.mention}.", ephemeral=True)


def setup(bot):
    bot.add_cog(Applications(bot))