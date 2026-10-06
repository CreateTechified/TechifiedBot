import asyncio
import re
from datetime import timedelta

import discord
from discord.ext import commands, tasks
from discord.commands import SlashCommandGroup, Option

from page_embeds import send_paged
from slash_cog import is_staff, ADMIN_ROLE_ID, MODERATOR_ROLE_ID, STAFF_ROLE_IDS

# --- CONFIGURATION ---
TICKET_CATEGORY_ID = 1556662805518884965
TICKET_LOG_CHANNEL_ID = 1472650884906221771

MAX_OPEN_PER_USER = 2
INACTIVITY_CLOSE = timedelta(hours=24)
INACTIVITY_REMIND = timedelta(hours=12)
DELETE_DELAY = timedelta(minutes=30)

URGENCY = {
    "low": ("🟢", "Low", discord.Color.green(), "Question or minor issue"),
    "medium": ("🟡", "Medium", discord.Color.gold(), "Something's wrong, but not that serious"),
    "high": ("🟠", "High", discord.Color.orange(), "Serious, need help soon"),
    "critical": ("🔴", "Critical", discord.Color.red(), "Emergency, needs staff right now"),
}
URGENCY_PINGS = {"high": {MODERATOR_ROLE_ID}, "critical": {MODERATOR_ROLE_ID, ADMIN_ROLE_ID}}
AUTO_CLOSE_DEFAULT = {"low": 1, "medium": 1, "high": 1, "critical": 0}

COLS_LIST = ["id", "channel_id", "user_id", "subject", "details", "urgency", "status",
             "last_activity", "claimed_by", "auto_close", "reminded", "delete_at"]
COLS = ", ".join(COLS_LIST)

NO_MENTIONS = discord.AllowedMentions.none()


def is_staff_member(member) -> bool:
    return isinstance(member, discord.Member) and bool({r.id for r in member.roles} & STAFF_ROLE_IDS)


def make_channel_name(ticket_id, user) -> str:
    clean = re.sub(r"[^a-z0-9_-]", "-", user.name.lower()) or str(user.id)
    return f"📦┃ticket-#{ticket_id}-{clean}"[:100]


# ---------- views / modals ----------

class TicketModal(discord.ui.Modal):
    def __init__(self, cog, urgency: str):
        super().__init__(title=f"New ticket ({URGENCY[urgency][1]} urgency)"[:45])
        self.cog = cog
        self.urgency = urgency
        self.subject_input = discord.ui.InputText(
            label="Reason", placeholder="Short summary of the issue",
            style=discord.InputTextStyle.short, max_length=100, required=True,
        )
        self.details_input = discord.ui.InputText(
            label="Details", placeholder="Explain what's going on. Include anything that might help us resolve your issue.",
            style=discord.InputTextStyle.paragraph, max_length=1000, required=True,
        )
        self.add_item(self.subject_input)
        self.add_item(self.details_input)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        error, channel = await self.cog.create_ticket(
            interaction.user, interaction.guild,
            self.subject_input.value.strip(), self.details_input.value.strip(), self.urgency
        )
        if error:
            await interaction.followup.send(error, ephemeral=True)
            return
        await interaction.followup.send(f"✅ Your ticket has been created: {channel.mention}", ephemeral=True)


class UrgencySelectView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=120)
        self.cog = cog
        select = discord.ui.Select(
            placeholder="How urgent is your issue?",
            options=[
                discord.SelectOption(label=label, value=key, emoji=emoji, description=desc)
                for key, (emoji, label, _, desc) in URGENCY.items()
            ],
        )
        select.callback = self._picked
        self.select = select
        self.add_item(select)

    async def _picked(self, interaction: discord.Interaction):
        await interaction.response.send_modal(TicketModal(self.cog, self.select.values[0]))


class TicketPanelView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Create Ticket", style=discord.ButtonStyle.success,
                       emoji="📩", custom_id="ticket_panel_create")
    async def create(self, button: discord.ui.Button, interaction: discord.Interaction):
        block = await self.cog._block(interaction.guild_id, interaction.user.id)
        if block:
            await interaction.response.send_message(block, ephemeral=True)
            return
        await interaction.response.send_message(
            "How urgent is your issue? Please only pick **High** or **Critical** for real emergencies, "
            "as those ping staff.",
            view=UrgencySelectView(self.cog), ephemeral=True
        )


class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Claim / Unclaim", style=discord.ButtonStyle.primary,
                       emoji="🙋", custom_id="ticket_claim")
    async def claim(self, button: discord.ui.Button, interaction: discord.Interaction):
        if not is_staff_member(interaction.user):
            await interaction.response.send_message("❌ Only staff can claim tickets.", ephemeral=True)
            return

        db = interaction.client.tag_db
        async with db.execute(
            "SELECT id, claimed_by FROM tickets WHERE channel_id = ? AND status != 'deleted'",
            (interaction.channel_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            await interaction.response.send_message("❌ I have no record of this ticket.", ephemeral=True)
            return

        ticket_id, claimed_by = row
        user = interaction.user
        if claimed_by and claimed_by != user.id:
            await interaction.response.send_message(
                f"⚠️ This ticket is already being handled by <@{claimed_by}>.",
                ephemeral=True, allowed_mentions=NO_MENTIONS
            )
            return

        new_owner = None if claimed_by == user.id else user.id
        await db.execute("UPDATE tickets SET claimed_by = ? WHERE id = ?", (new_owner, ticket_id))
        await db.commit()

        embed = interaction.message.embeds[0]
        value = user.mention if new_owner else "*Unassigned*"
        for i, field in enumerate(embed.fields):
            if field.name == "Handled by":
                embed.set_field_at(i, name="Handled by", value=value, inline=field.inline)
        await interaction.response.edit_message(embed=embed)

        text = (f"🙋 {user.mention} is now handling this ticket." if new_owner
                else f"↩️ {user.mention} is no longer handling this ticket.")
        await interaction.followup.send(text, allowed_mentions=NO_MENTIONS)


# ---------- the cog ----------

class Tickets(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.channels = None
        bot.add_view(TicketPanelView(self))
        bot.add_view(TicketControlView())
        self.maintenance.start()

    def cog_unload(self):
        self.maintenance.cancel()

    ticket_group = SlashCommandGroup("ticket", "Support tickets")

    # ---------- db helpers ----------

    async def _get(self, channel_id: int):
        async with self.bot.tag_db.execute(
            f"SELECT {COLS} FROM tickets WHERE channel_id = ? AND status != 'deleted'", (channel_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return dict(zip(COLS_LIST, row)) if row else None

    async def _is_ticket_channel(self, channel_id: int) -> bool:
        if self.channels is None:
            async with self.bot.tag_db.execute(
                "SELECT channel_id FROM tickets WHERE status IN ('open', 'closed')"
            ) as cursor:
                self.channels = {r[0] for r in await cursor.fetchall()}
        return channel_id in self.channels

    async def _block(self, guild_id: int, user_id: int):
        async with self.bot.tag_db.execute(
            "SELECT COUNT(*) FROM tickets WHERE guild = ? AND user_id = ? AND status = 'open'",
            (guild_id, user_id)
        ) as cursor:
            count = (await cursor.fetchone())[0]
        if count >= MAX_OPEN_PER_USER:
            return (f"⚠️ You already have {count} open ticket(s). Use those first, "
                    f"or close one with `.close` if it's resolved.")
        return None

    async def _log(self, title, color, text):
        channel = self.bot.get_channel(TICKET_LOG_CHANNEL_ID)
        if channel is None:
            return
        embed = discord.Embed(title=title, description=text, color=color, timestamp=discord.utils.utcnow())
        try:
            await channel.send(embed=embed, allowed_mentions=NO_MENTIONS)
        except discord.HTTPException:
            pass

    @staticmethod
    async def _rename(channel, name):
        if channel.name == name:
            return
        try:
            await asyncio.wait_for(channel.edit(name=name), timeout=5)
        except (asyncio.TimeoutError, discord.HTTPException):
            pass

    # ---------- creating ----------

    async def create_ticket(self, member, guild, subject, details, urgency):
        db = self.bot.tag_db

        block = await self._block(guild.id, member.id)
        if block:
            return block, None

        now = discord.utils.utcnow().isoformat()
        cursor = await db.execute(
            "INSERT INTO tickets (guild, user_id, subject, details, urgency, created_at, last_activity, auto_close) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (guild.id, member.id, subject, details, urgency, now, now, AUTO_CLOSE_DEFAULT[urgency])
        )
        await db.commit()
        ticket_id = cursor.lastrowid
        await cursor.close()

        emoji, label, color, _ = URGENCY[urgency]
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, embed_links=True,
                read_message_history=True, manage_channels=True
            ),
            member: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                attach_files=True, embed_links=True
            ),
        }
        for role_id in STAFF_ROLE_IDS:
            role = guild.get_role(role_id)
            if role:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True,
                    attach_files=True, embed_links=True
                )

        category = guild.get_channel(TICKET_CATEGORY_ID) if TICKET_CATEGORY_ID else None
        try:
            channel = await guild.create_text_channel(
                make_channel_name(ticket_id, member), category=category, overwrites=overwrites,
                topic=f"{emoji} {label} urgency | Ticket #{ticket_id} | opened by {member} ({member.id})",
                reason=f"Ticket #{ticket_id} opened by {member}"
            )
        except discord.HTTPException:
            await db.execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))
            await db.commit()
            return "❌ I couldn't create your ticket channel. Please tell a staff member.", None

        await db.execute("UPDATE tickets SET channel_id = ? WHERE id = ?", (channel.id, ticket_id))
        await db.commit()
        if self.channels is not None:
            self.channels.add(channel.id)

        embed = discord.Embed(
            title=f"{emoji} Ticket #{ticket_id}: {subject}"[:256],
            description=details, color=color, timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="Opened by", value=member.mention, inline=True)
        embed.add_field(name="Urgency", value=f"{emoji} {label}", inline=True)
        embed.add_field(name="Handled by", value="*Unassigned*", inline=True)
        footer = "Use .close once this is resolved"
        if AUTO_CLOSE_DEFAULT[urgency]:
            footer += " • Inactive tickets auto-close after 24h"
        embed.set_footer(text=footer)

        pings = " ".join(f"<@&{rid}>" for rid in URGENCY_PINGS.get(urgency, ()))
        try:
            await channel.send(f"{member.mention} {pings}".strip(), embed=embed, view=TicketControlView())
        except discord.HTTPException:
            pass

        await self._log(
            "📦 Ticket opened", color,
            f"**#{ticket_id}** {channel.mention} by {member.mention}\n**Urgency:** {emoji} {label}\n**Reason:** {subject}"
        )
        return None, channel

    # ---------- closing / reopening ----------

    async def _finish_close(self, channel, ticket, closed_by_id, send, text):
        await self.bot.tag_db.execute(
            "UPDATE tickets SET status = 'closed', closed_at = ?, closed_by = ?, delete_at = NULL WHERE id = ?",
            (discord.utils.utcnow().isoformat(), closed_by_id, ticket["id"])
        )
        await self.bot.tag_db.commit()
        await send(text, allowed_mentions=discord.AllowedMentions(users=True))
        await self._rename(channel, channel.name.replace("📦", "✅", 1))
        who = f"<@{closed_by_id}>" if closed_by_id else "auto-close (inactivity)"
        await self._log("✅ Ticket closed", discord.Color.dark_grey(), f"**#{ticket['id']}** {channel.mention} closed by {who}")

    @staticmethod
    def _closed_text(ticket_id):
        return (f"✅ **Ticket #{ticket_id} closed.** Only the OP and staff can post here now. "
                "Run `.reopen` to open it back up. This channel is kept for records; "
                "staff can schedule its removal with `/ticket delete`.")

    async def handle_close(self, ctx) -> bool:
        ticket = await self._get(ctx.channel.id)
        if ticket is None:
            return False
        if ticket["status"] == "closed":
            await ctx.send("This ticket is already closed.")
        elif ctx.author.id != ticket["user_id"]:
            await ctx.send(
                "❌ Only the person who opened this ticket can `.close` it. "
                "Staff should use `/forceclose` instead."
            )
        else:
            await self._finish_close(ctx.channel, ticket, ctx.author.id, ctx.send, self._closed_text(ticket["id"]))
        return True

    async def handle_forceclose(self, ctx) -> bool:
        ticket = await self._get(ctx.channel.id)
        if ticket is None:
            return False
        if not is_staff_member(ctx.author):
            await ctx.respond("❌ You don't have permission to use this command.", ephemeral=True)
        elif ticket["status"] == "closed":
            await ctx.respond("This ticket is already closed.", ephemeral=True)
        else:
            await ctx.defer()
            await self._finish_close(ctx.channel, ticket, ctx.author.id, ctx.respond, self._closed_text(ticket["id"]))
        return True

    async def _do_reopen(self, channel, ticket, send, reopened_by):
        await self.bot.tag_db.execute(
            "UPDATE tickets SET status = 'open', closed_at = NULL, closed_by = NULL, delete_at = NULL, "
            "last_activity = ?, reminded = 0 WHERE id = ?",
            (discord.utils.utcnow().isoformat(), ticket["id"])
        )
        await self.bot.tag_db.commit()
        await send(f"🔓 **Ticket #{ticket['id']} has been reopened.** Anyone with access can message here again.")
        await self._rename(channel, channel.name.replace("✅", "📦", 1))
        await self._log("🔓 Ticket reopened", discord.Color.green(),
                        f"**#{ticket['id']}** {channel.mention} reopened by {reopened_by.mention}")

    async def handle_reopen(self, ctx) -> bool:
        ticket = await self._get(ctx.channel.id)
        if ticket is None:
            return False
        if ctx.author.id != ticket["user_id"]:
            await ctx.send(
                "❌ Only the person who opened this ticket can `.reopen` it. "
                "Staff should use `/forcereopen` instead."
            )
        elif ticket["status"] != "closed":
            await ctx.send("This ticket isn't closed.")
        else:
            await self._do_reopen(ctx.channel, ticket, ctx.send, ctx.author)
        return True

    async def handle_forcereopen(self, ctx) -> bool:
        ticket = await self._get(ctx.channel.id)
        if ticket is None:
            return False
        if not is_staff_member(ctx.author):
            await ctx.respond("❌ You don't have permission to use this command.", ephemeral=True)
        elif ticket["status"] != "closed":
            await ctx.respond("This ticket isn't closed.", ephemeral=True)
        else:
            await ctx.defer()
            await self._do_reopen(ctx.channel, ticket, ctx.respond, ctx.author)
        return True

    # ---------- listener ----------

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not message.guild:
            return
        if not await self._is_ticket_channel(message.channel.id):
            return

        ticket = await self._get(message.channel.id)
        if ticket is None:
            return

        if ticket["status"] == "open":
            await self.bot.tag_db.execute(
                "UPDATE tickets SET last_activity = ?, reminded = 0 WHERE id = ?",
                (discord.utils.utcnow().isoformat(), ticket["id"])
            )
            await self.bot.tag_db.commit()
            return

        if is_staff_member(message.author):
            return
        ctx = await self.bot.get_context(message)
        if ctx.valid:
            return

        try:
            await message.delete()
        except discord.HTTPException:
            pass
        try:
            await message.channel.send(
                f"🔒 {message.author.mention} This ticket is closed, if you want to reopen it run `.reopen` instead.",
                delete_after=10
            )
        except discord.HTTPException:
            pass

    # ---------- auto-close / reminders / scheduled deletion ----------

    @tasks.loop(minutes=1)
    async def maintenance(self):
        async with self.bot.tag_db.execute(
            f"SELECT {COLS} FROM tickets WHERE status IN ('open', 'closed')"
        ) as cursor:
            rows = await cursor.fetchall()

        now = discord.utils.utcnow()
        for row in rows:
            ticket = dict(zip(COLS_LIST, row))
            try:
                await self._maintain(ticket, now)
            except Exception as e:
                print(f"Ticket maintenance error (#{ticket['id']}): {e}")

    @maintenance.before_loop
    async def before_maintenance(self):
        await self.bot.wait_until_ready()

    async def _maintain(self, ticket, now):
        if ticket["channel_id"] is None:
            return

        db = self.bot.tag_db
        channel = self.bot.get_channel(ticket["channel_id"])
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(ticket["channel_id"])
            except discord.NotFound:
                await db.execute("UPDATE tickets SET status = 'deleted' WHERE id = ?", (ticket["id"],))
                await db.commit()
                if self.channels is not None:
                    self.channels.discard(ticket["channel_id"])
                return
            except discord.HTTPException:
                return

        if ticket["status"] == "open":
            if not ticket["auto_close"]:
                return
            last = discord.utils.parse_time(ticket["last_activity"])
            idle = now - last
            if idle >= INACTIVITY_CLOSE:
                text = (f"⏰ <@{ticket['user_id']}> Ticket #{ticket['id']} was closed automatically "
                        "due to inactivity. Run `.reopen` if you still need help. This channel is kept for records.")
                await self._finish_close(channel, ticket, None, channel.send, text)
            elif idle >= INACTIVITY_REMIND and not ticket["reminded"]:
                close_at = discord.utils.format_dt(last + INACTIVITY_CLOSE, style="R")
                await channel.send(
                    f"⏳ <@{ticket['user_id']}> This ticket has been inactive for 12 hours and will be closed "
                    f"automatically {close_at} unless someone replies."
                )
                await db.execute("UPDATE tickets SET reminded = 1 WHERE id = ?", (ticket["id"],))
                await db.commit()
            return

        if ticket["delete_at"] and discord.utils.parse_time(ticket["delete_at"]) <= now:
            try:
                await channel.delete(reason=f"Ticket #{ticket['id']} deleted (scheduled)")
            except discord.Forbidden:
                await db.execute("UPDATE tickets SET delete_at = NULL WHERE id = ?", (ticket["id"],))
                await db.commit()
                await self._log("⚠️ Ticket deletion failed", discord.Color.red(),
                                f"I don't have permission to delete <#{ticket['channel_id']}> (ticket #{ticket['id']}).")
                return
            await db.execute("UPDATE tickets SET status = 'deleted' WHERE id = ?", (ticket["id"],))
            await db.commit()
            if self.channels is not None:
                self.channels.discard(ticket["channel_id"])
            await self._log("🗑️ Ticket deleted", discord.Color.dark_red(),
                            f"**#{ticket['id']}** (opened by <@{ticket['user_id']}>) was deleted.\n**Reason:** {ticket['subject']}")

    # ---------- per-ticket auto-close toggle ----------

    async def _set_autoclose(self, channel, enabled: bool):
        ticket = await self._get(channel.id)
        if ticket is None:
            return "❌ This command can only be used inside a ticket."
        if ticket["status"] != "open":
            return "❌ This ticket is closed."
        await self.bot.tag_db.execute(
            "UPDATE tickets SET auto_close = ?, last_activity = ?, reminded = 0 WHERE id = ?",
            (int(enabled), discord.utils.utcnow().isoformat(), ticket["id"])
        )
        await self.bot.tag_db.commit()
        if enabled:
            return "⏲️ Auto-close is now **on** for this ticket (closes after 24h of inactivity)."
        return "⏲️ Auto-close is now **off** for this ticket."

    @commands.command(name="autoclose")
    @commands.guild_only()
    async def autoclose_prefix(self, ctx, state: str = None):
        if not is_staff_member(ctx.author):
            return
        if state is None or state.lower() not in ("on", "off"):
            await ctx.send("Usage: `.autoclose on` or `.autoclose off` (inside a ticket).")
            return
        await ctx.send(await self._set_autoclose(ctx.channel, state.lower() == "on"))

    # ---------- slash commands ----------

    @ticket_group.command(name="panel", description="Post a permanent 'Create Ticket' button in a channel")
    @is_staff()
    async def ticket_panel(
        self, ctx,
        channel: Option(discord.TextChannel, "Channel to post in (defaults to here)", required=False, default=None),
    ):
        target = channel or ctx.channel
        embed = discord.Embed(
            title="📩 Support Tickets",
            description="Need help from staff? Click the button below to open a private ticket.\n"
                        "Don't open any tickets for no reason :)",
            color=discord.Color.blurple()
        )
        try:
            await target.send(embed=embed, view=TicketPanelView(self))
        except discord.Forbidden:
            await ctx.respond(f"❌ I can't send messages in {target.mention}.", ephemeral=True)
            return
        await ctx.respond(f"✅ Ticket panel posted in {target.mention}.", ephemeral=True)

    @ticket_group.command(name="delete", description="Schedule this closed ticket's channel for deletion in 30 minutes")
    @is_staff()
    async def ticket_delete(self, ctx):
        ticket = await self._get(ctx.channel.id)
        if ticket is None:
            await ctx.respond("❌ This command can only be used inside a ticket.", ephemeral=True)
            return
        if ticket["status"] != "closed":
            await ctx.respond("❌ Close the ticket first (`.close` or `/forceclose`).", ephemeral=True)
            return
        if ticket["delete_at"]:
            await ctx.respond("⚠️ Deletion is already scheduled. Use `/ticket canceldelete` to stop it.", ephemeral=True)
            return

        delete_at = discord.utils.utcnow() + DELETE_DELAY
        await self.bot.tag_db.execute(
            "UPDATE tickets SET delete_at = ? WHERE id = ?", (delete_at.isoformat(), ticket["id"])
        )
        await self.bot.tag_db.commit()
        await ctx.respond(
            f"🗑️ This ticket will be **permanently deleted** {discord.utils.format_dt(delete_at, style='R')}. "
            "Run `/ticket canceldelete` (or `/forcereopen`) to stop it."
        )

    @ticket_group.command(name="canceldelete", description="Cancel a scheduled ticket deletion")
    @is_staff()
    async def ticket_canceldelete(self, ctx):
        ticket = await self._get(ctx.channel.id)
        if ticket is None or not ticket["delete_at"]:
            await ctx.respond("❌ No deletion is scheduled for this channel.", ephemeral=True)
            return
        await self.bot.tag_db.execute("UPDATE tickets SET delete_at = NULL WHERE id = ?", (ticket["id"],))
        await self.bot.tag_db.commit()
        await ctx.respond("✅ Scheduled deletion cancelled. The ticket stays closed and kept for records.")

    @ticket_group.command(name="autoclose", description="Turn auto-close on or off for this ticket")
    @is_staff()
    async def ticket_autoclose(
        self, ctx,
        enabled: Option(bool, "True = close after 24h of inactivity, False = never auto-close"),
    ):
        await ctx.respond(await self._set_autoclose(ctx.channel, enabled))

    @ticket_group.command(name="list", description="List open tickets, most urgent first")
    @is_staff()
    async def ticket_list(self, ctx):
        async with self.bot.tag_db.execute(
            "SELECT id, channel_id, subject, urgency, claimed_by FROM tickets "
            "WHERE guild = ? AND status = 'open' "
            "ORDER BY CASE urgency WHEN 'critical' THEN 3 WHEN 'high' THEN 2 WHEN 'medium' THEN 1 ELSE 0 END DESC, id ASC",
            (ctx.guild.id,)
        ) as cursor:
            rows = await cursor.fetchall()

        if not rows:
            await ctx.respond("✅ No open tickets.")
            return

        items = [
            f"{URGENCY[urgency][0]} <#{channel_id}> — {subject[:40]} — "
            + (f"<@{claimed_by}>" if claimed_by else "*unclaimed*")
            for _, channel_id, subject, urgency, claimed_by in rows
        ]
        await send_paged(ctx, "📦 Open tickets", items, discord.Color.blurple(), noun="ticket(s)")


def setup(bot):
    bot.add_cog(Tickets(bot))