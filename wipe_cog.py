import asyncio
import os
import shutil
from datetime import timedelta

import discord
from discord.ext import commands, tasks
from discord.commands import Option, SlashCommandGroup

from slash_cog import is_admin, ADMIN_LOG_CHANNEL_ID

# Idk if this is a good idea or not..

WIPEALL_USER_IDS = {872693345510637589, 1023288254671376424}

TAG_FILES_DIR = "tag_files"
FINAL_CONFIRM_TIMEOUT = 30
WIPEALL_DELAY_HOURS = 12
WIPEALL_DELAY = timedelta(hours=WIPEALL_DELAY_HOURS)

# NOT included: tickets, help threads and AFK.
SECTORS = {
    "larp": "LARP stats and restrictions",
    "tags": "Tags, aliases and attachment files",
    "wss": "Auto-reply triggers",
    "warnings": "Member warnings",
    "applications": "Whitelist application records",
    "servers": "Tracked Minecraft servers (incl. API keys)",
}

SECTOR_CHOICES = [discord.OptionChoice(desc, key) for key, desc in SECTORS.items()]


def bullet_list(lines):
    return "\n".join(f"- {line}" for line in lines)


def fmt_time(dt) -> str:
    if dt is None:
        return "an unknown time"
    return f"{discord.utils.format_dt(dt, style='F')} ({discord.utils.format_dt(dt, style='R')})"


# ---------- confirmation ----------

class PhraseModal(discord.ui.Modal):

    def __init__(self, title: str, phrase: str, on_confirm):
        super().__init__(title=title[:45])
        self.phrase = phrase
        self.on_confirm = on_confirm
        self.phrase_input = discord.ui.InputText(
            label=f"Type {phrase} to confirm"[:45],
            placeholder=phrase,
            style=discord.InputTextStyle.short,
            required=True,
            max_length=50,
        )
        self.add_item(self.phrase_input)

    async def callback(self, interaction: discord.Interaction):
        if (self.phrase_input.value or "").strip() != self.phrase:
            await interaction.response.send_message(
                f"❌ That didn't match `{self.phrase}`. "
                "Nothing was wiped.",
                ephemeral=True
            )
            return
        await self.on_confirm(interaction)


class WipeAllFinalView(discord.ui.View):

    def __init__(self, cog, owner_id: int):
        super().__init__(timeout=FINAL_CONFIRM_TIMEOUT)
        self.cog = cog
        self.owner_id = owner_id
        self.message = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id or interaction.user.id not in WIPEALL_USER_IDS:
            await interaction.response.send_message("❌ This isn't for you.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(content="⌛ Timed out. Nothing was scheduled.", view=None)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Yes, schedule the wipe", style=discord.ButtonStyle.danger, emoji="💥")
    async def confirm(self, button: discord.ui.Button, interaction: discord.Interaction):
        self.stop()
        await interaction.response.edit_message(content="⏳ Scheduling...", view=None)
        await self.cog._schedule_wipeall(interaction)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, button: discord.ui.Button, interaction: discord.Interaction):
        self.stop()
        await interaction.response.edit_message(content="🛑 Cancelled. Nothing was scheduled.", view=None)


# ---------- the cog ----------

class Wipe(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.check_scheduled.start()

    def cog_unload(self):
        self.check_scheduled.cancel()

    wipe_group = SlashCommandGroup("wipe", "Wipe bot data")

    # ---------- wiping logic ----------

    async def _delete(self, table: str, guild_id: int) -> int:
        cursor = await self.bot.tag_db.execute(f"DELETE FROM {table} WHERE guild = ?", (guild_id,))
        count = cursor.rowcount
        await cursor.close()
        return count

    async def _wipe_db(self, sector: str, guild_id: int):
        if sector == "larp":
            scores = await self._delete("larp_scores", guild_id)
            restrictions = await self._delete("larp_timeouts", guild_id)
            return [f"LARP: {scores} score(s), {restrictions} restriction/offense record(s)"]
        if sector == "tags":
            tags = await self._delete("tags", guild_id)
            aliases = await self._delete("tag_aliases", guild_id)
            return [f"Tags: {tags} tag(s), {aliases} alias(es), attachment files"]
        if sector == "wss":
            triggers = await self._delete("auto_replies", guild_id)
            return [f"Auto-replies: {triggers} trigger(s)"]
        if sector == "warnings":
            warnings = await self._delete("warnings", guild_id)
            return [f"Warnings: {warnings} warning(s)"]
        if sector == "applications":
            apps = await self._delete("applications", guild_id)
            return [f"Applications: {apps} record(s)"]
        if sector == "servers":
            servers = await self._delete("mc_servers", guild_id)
            return [f"Servers: {servers} tracked server(s)"]
        return []

    async def _post_wipe(self, sector: str, guild_id: int):
        if sector == "tags":
            path = os.path.join(TAG_FILES_DIR, str(guild_id))
            await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)
        elif sector == "wss":
            cog = self.bot.get_cog("AutoReply")
            if cog is not None:
                cog._cache.pop(guild_id, None)

    async def run_wipe(self, sectors, guild_id: int, claim_schedule: bool = False):
        db = self.bot.tag_db
        lines = []
        try:
            if claim_schedule:
                cursor = await db.execute("DELETE FROM scheduled_wipes WHERE guild = ?", (guild_id,))
                claimed = cursor.rowcount
                await cursor.close()
                if not claimed:
                    await db.rollback()
                    return None

            for sector in sectors:
                lines.extend(await self._wipe_db(sector, guild_id))
            await db.commit()
        except Exception:
            await db.rollback()
            raise

        for sector in sectors:
            await self._post_wipe(sector, guild_id)
        return lines

    async def _log(self, guild, title: str, lines, moderator_id=None, color=None):
        channel = self.bot.get_channel(ADMIN_LOG_CHANNEL_ID)
        if channel is None:
            return
        embed = discord.Embed(
            title=title,
            description=bullet_list(lines) or None,
            color=color or discord.Color.dark_red(),
            timestamp=discord.utils.utcnow()
        )
        if moderator_id is not None:
            embed.add_field(name="Moderator", value=f"<@{moderator_id}> (`{moderator_id}`)", inline=False)
        if guild is not None:
            embed.set_footer(text=guild.name)
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass

    # ---------- scheduled full wipe: db helpers ----------

    async def _get_schedule(self, guild_id: int):
        async with self.bot.tag_db.execute(
            "SELECT run_at, scheduled_by FROM scheduled_wipes WHERE guild = ?", (guild_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return discord.utils.parse_time(row[0]), row[1]

    async def _schedule(self, guild_id: int, user_id: int):
        now = discord.utils.utcnow()
        run_at = now + WIPEALL_DELAY
        cursor = await self.bot.tag_db.execute(
            "INSERT OR IGNORE INTO scheduled_wipes (guild, run_at, scheduled_by, created_at) "
            "VALUES (?, ?, ?, ?)",
            (guild_id, run_at.isoformat(), user_id, now.isoformat())
        )
        await self.bot.tag_db.commit()
        inserted = cursor.rowcount
        await cursor.close()
        return run_at if inserted else None

    # ---------- scheduled full wipe: the timer ----------

    @tasks.loop(minutes=1)
    async def check_scheduled(self):
        async with self.bot.tag_db.execute(
            "SELECT guild, run_at, scheduled_by FROM scheduled_wipes"
        ) as cursor:
            rows = await cursor.fetchall()

        now = discord.utils.utcnow()
        for guild_id, run_at, scheduled_by in rows:
            due = discord.utils.parse_time(run_at)
            if due is None or due > now:
                continue
            try:
                await self._execute_scheduled(guild_id, scheduled_by)
            except Exception as e:
                print(f"Scheduled wipe error (guild {guild_id}): {e}")

    @check_scheduled.before_loop
    async def before_check_scheduled(self):
        await self.bot.wait_until_ready()

    async def _execute_scheduled(self, guild_id: int, scheduled_by: int):
        lines = await self.run_wipe(list(SECTORS), guild_id, claim_schedule=True)
        if lines is None:
            return  # cancelled just before it ran

        await self._log(
            self.bot.get_guild(guild_id), "💥 Scheduled wipe executed: ALL", lines, scheduled_by
        )

    # ---------- /wipe data <sector> ----------

    async def _confirmed_sector(self, interaction: discord.Interaction, sector: str):
        await interaction.response.defer(ephemeral=True)
        try:
            lines = await self.run_wipe([sector], interaction.guild_id)
        except Exception as e:
            print(f"Wipe error ({sector}): {e}")
            await interaction.followup.send(
                "❌ Something went wrong while wiping, so nothing was changed.", ephemeral=True
            )
            return

        await interaction.followup.send(f"🧹 **Wipe complete.**\n{bullet_list(lines)}", ephemeral=True)
        await self._log(interaction.guild, f"🧹 Wiped: {sector}", lines, interaction.user.id)

    @wipe_group.command(name="data", description="Permanently wipe one category of bot data")
    @is_admin()
    async def wipe_data(
        self, ctx,
        sector: Option(str, "What to wipe", choices=SECTOR_CHOICES),
    ):
        phrase = f"WIPE {sector.upper()}"

        async def on_confirm(interaction: discord.Interaction):
            await self._confirmed_sector(interaction, sector)

        await ctx.send_modal(PhraseModal(f"Confirm: {phrase}", phrase, on_confirm))

    # ---------- /wipe cancel ----------

    @wipe_group.command(name="cancel", description="Cancel the scheduled full wipe")
    @is_admin()
    async def wipe_cancel(self, ctx):
        schedule = await self._get_schedule(ctx.guild.id)
        if schedule is None:
            await ctx.respond("❌ There's no wipe scheduled.", ephemeral=True)
            return

        run_at, scheduled_by = schedule
        await self.bot.tag_db.execute("DELETE FROM scheduled_wipes WHERE guild = ?", (ctx.guild.id,))
        await self.bot.tag_db.commit()

        await ctx.respond("✅ The scheduled wipe was cancelled. Nothing was wiped.", ephemeral=True)
        await self._log(
            ctx.guild, "🛑 Scheduled wipe cancelled",
            [f"It was scheduled by <@{scheduled_by}> to run {fmt_time(run_at)}."],
            ctx.author.id, color=discord.Color.green()
        )

    # ---------- /wipe all (scheduled) ----------

    @wipe_group.command(
        name="all", description=f"Schedule a wipe of ALL bot data, {WIPEALL_DELAY_HOURS} hours from now"
    )
    @is_admin()
    async def wipe_all(self, ctx):
        if ctx.author.id not in WIPEALL_USER_IDS:
            await ctx.respond("❌ You don't have permission to schedule a full wipe.", ephemeral=True)
            return

        schedule = await self._get_schedule(ctx.guild.id)
        if schedule is not None:
            run_at, _ = schedule
            await ctx.respond(
                f"⚠️ A full wipe is already scheduled to run {fmt_time(run_at)}. "
                "Use `/wipe cancel` to stop it.",
                ephemeral=True
            )
            return

        await ctx.send_modal(PhraseModal("Confirm: WIPE ALL", "WIPE ALL", self._wipeall_first_step))

    async def _wipeall_first_step(self, interaction: discord.Interaction):
        if interaction.user.id not in WIPEALL_USER_IDS:
            await interaction.response.send_message("❌ You don't have permission to do that.", ephemeral=True)
            return

        view = WipeAllFinalView(self, interaction.user.id)
        await interaction.response.send_message(
            f"⚠️ **Final confirmation.** This will schedule a wipe of **everything** for this server, "
            f"to run in **{WIPEALL_DELAY_HOURS} hours**:\n"
            + bullet_list(SECTORS.values())
            + "\n\nYou can stop it any time before then with `/wipe cancel`. "
              f"Press the button within {FINAL_CONFIRM_TIMEOUT} seconds, or nothing will be scheduled.",
            view=view, ephemeral=True
        )
        view.message = await interaction.original_response()

    async def _schedule_wipeall(self, interaction: discord.Interaction):
        try:
            run_at = await self._schedule(interaction.guild_id, interaction.user.id)
        except Exception as e:
            print(f"Schedule error (all): {e}")
            await interaction.edit_original_response(
                content="❌ Something went wrong, so nothing was scheduled.", view=None
            )
            return

        if run_at is None:
            schedule = await self._get_schedule(interaction.guild_id)
            when = fmt_time(schedule[0]) if schedule else "soon"
            await interaction.edit_original_response(
                content=f"⚠️ A full wipe is already scheduled to run {when}. Use `/wipe cancel` to stop it.",
                view=None
            )
            return

        await interaction.edit_original_response(
            content=(
                f"⏳ **Full wipe scheduled** to run {fmt_time(run_at)}.\n"
                "Use `/wipe cancel` any time before then to stop it."
            ),
            view=None
        )
        await self._log(
            interaction.guild, "⏳ Full wipe scheduled",
            [f"Runs {fmt_time(run_at)}",
             "Wipes: " + ", ".join(SECTORS),
             "Stop it with `/wipe cancel`"],
            interaction.user.id, color=discord.Color.orange()
        )


def setup(bot):
    bot.add_cog(Wipe(bot))