import asyncio
import os
import shutil
from datetime import timedelta

import discord
from discord.ext import commands, tasks
from discord.commands import Option, SlashCommandGroup

from slash_cog import ADMIN_LOG_CHANNEL_ID

# Idk if this is a good idea or not..

WIPEALL_USER_IDS = {872693345510637589, 1023288254671376424}

TAG_FILES_DIR = "tag_files"
FINAL_CONFIRM_TIMEOUT = 30

WIPE_DELAY_HOURS = 24
WIPE_DELAY = timedelta(hours=WIPE_DELAY_HOURS)
REMIND_HALF = WIPE_DELAY / 2
REMIND_FINAL = timedelta(hours=1)

ALL_KEY = "all"

# NOT included: tickets, help threads and AFK.
SECTORS = {
    "larp": "Larp stats and restrictions",
    "tags": "Tags, aliases and attachment files",
    "wss": "Auto-reply triggers (WSS)",
    "warnings": "Member warnings",
    "applications": "Whitelist application records",
    "servers": "Tracked Minecraft servers (incl. API keys)",
}

SECTOR_CHOICES = [discord.OptionChoice(desc, key) for key, desc in SECTORS.items()]
TARGET_CHOICES = [discord.OptionChoice("Everything (/wipe all)", ALL_KEY)] + SECTOR_CHOICES


def bullet_list(lines):
    return "\n".join(f"- {line}" for line in lines)


def fmt_time(dt) -> str:
    if dt is None:
        return "an unknown time"
    return f"{discord.utils.format_dt(dt, style='F')} ({discord.utils.format_dt(dt, style='R')})"


def is_wipe_user():
    async def predicate(ctx: discord.ApplicationContext) -> bool:
        if ctx.author.id in WIPEALL_USER_IDS:
            return True
        await ctx.respond("❌ You don't have permission to use /wipe commands.", ephemeral=True)
        return False

    return commands.check(predicate)


def sector_label(sector: str) -> str:
    return "Everything" if sector == ALL_KEY else SECTORS.get(sector, sector)


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
                "Nothing was scheduled.",
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

    async def run_wipe(self, sectors, guild_id: int, claim_job_id: int = None):
        db = self.bot.tag_db
        lines = []
        try:
            if claim_job_id is not None:
                cursor = await db.execute("DELETE FROM wipe_jobs WHERE id = ?", (claim_job_id,))
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

    # ---------- scheduled wipes: db helpers ----------

    async def _list_jobs(self, guild_id: int):
        async with self.bot.tag_db.execute(
            "SELECT sector, run_at, scheduled_by FROM wipe_jobs WHERE guild = ? ORDER BY run_at",
            (guild_id,)
        ) as cursor:
            rows = await cursor.fetchall()
        return [(sector, discord.utils.parse_time(run_at), by) for sector, run_at, by in rows]

    async def _get_job(self, guild_id: int, sector: str):
        async with self.bot.tag_db.execute(
            "SELECT run_at, scheduled_by FROM wipe_jobs WHERE guild = ? AND sector = ?",
            (guild_id, sector)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return discord.utils.parse_time(row[0]), row[1]

    async def _schedule(self, guild_id: int, sector: str, user_id: int):
        now = discord.utils.utcnow()
        run_at = now + WIPE_DELAY
        cursor = await self.bot.tag_db.execute(
            "INSERT OR IGNORE INTO wipe_jobs (guild, sector, run_at, scheduled_by, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (guild_id, sector, run_at.isoformat(), user_id, now.isoformat())
        )
        await self.bot.tag_db.commit()
        inserted = cursor.rowcount
        await cursor.close()
        return run_at if inserted else None

    # ---------- scheduled wipes: the timer ----------

    @tasks.loop(minutes=1)
    async def check_scheduled(self):
        async with self.bot.tag_db.execute(
            "SELECT id, guild, sector, run_at, scheduled_by, reminded_12, reminded_1 FROM wipe_jobs"
        ) as cursor:
            rows = await cursor.fetchall()

        now = discord.utils.utcnow()
        for job_id, guild_id, sector, run_at, scheduled_by, reminded_half, reminded_final in rows:
            due = discord.utils.parse_time(run_at)
            if due is None:
                continue
            remaining = due - now
            try:
                if remaining <= timedelta(0):
                    await self._execute_job(job_id, guild_id, sector, scheduled_by)
                elif remaining <= REMIND_FINAL and not reminded_final:
                    await self._remind(job_id, sector, due, scheduled_by, final=True)
                elif remaining <= REMIND_HALF and not reminded_half:
                    await self._remind(job_id, sector, due, scheduled_by, final=False)
            except Exception as e:
                print(f"Scheduled wipe error (guild {guild_id}, {sector}): {e}")

    @check_scheduled.before_loop
    async def before_check_scheduled(self):
        await self.bot.wait_until_ready()

    async def _execute_job(self, job_id: int, guild_id: int, sector: str, scheduled_by: int):
        sectors = list(SECTORS) if sector == ALL_KEY else [sector]
        lines = await self.run_wipe(sectors, guild_id, claim_job_id=job_id)
        if lines is None:
            return  # cancelled just before it ran

        await self._log(
            self.bot.get_guild(guild_id), f"💥 Scheduled wipe executed: {sector.upper()}",
            lines, scheduled_by
        )

    async def _remind(self, job_id: int, sector: str, due, scheduled_by: int, final: bool):
        await self.bot.tag_db.execute(
            "UPDATE wipe_jobs SET reminded_12 = 1, reminded_1 = ? WHERE id = ?", (int(final), job_id)
        )
        await self.bot.tag_db.commit()

        channel = self.bot.get_channel(ADMIN_LOG_CHANNEL_ID)
        if channel is None:
            return

        label = sector_label(sector)
        if final:
            title = f"🚨 Wipe in under 1 hour: {label}"
            color = discord.Color.red()
        else:
            hours = int(REMIND_HALF.total_seconds() // 3600)
            title = f"⏰ Wipe in {hours} hours: {label}"
            color = discord.Color.orange()

        embed = discord.Embed(
            title=title,
            description=f"This wipe runs {fmt_time(due)}.\nUse `/wipe cancel` to stop it.",
            color=color,
            timestamp=discord.utils.utcnow()
        )
        try:
            await channel.send(
                f"<@{scheduled_by}>", embed=embed,
                allowed_mentions=discord.AllowedMentions(users=True)
            )
        except discord.HTTPException:
            pass

    # ---------- /wipe data <sector> ----------

    @wipe_group.command(
        name="data", description=f"Schedule a wipe of one category of bot data, {WIPE_DELAY_HOURS} hours from now"
    )
    @is_wipe_user()
    async def wipe_data(
        self, ctx,
        sector: Option(str, "What to wipe", choices=SECTOR_CHOICES),
    ):
        existing = await self._get_job(ctx.guild.id, sector)
        if existing is not None:
            await ctx.respond(
                f"⚠️ A wipe of **{SECTORS[sector]}** is already scheduled to run {fmt_time(existing[0])}. "
                "Use `/wipe cancel` to stop it.",
                ephemeral=True
            )
            return

        phrase = f"WIPE {sector.upper()}"

        async def on_confirm(interaction: discord.Interaction):
            await self._schedule_sector(interaction, sector)

        await ctx.send_modal(PhraseModal(f"Confirm: {phrase}", phrase, on_confirm))

    async def _schedule_sector(self, interaction: discord.Interaction, sector: str):
        if interaction.user.id not in WIPEALL_USER_IDS:
            await interaction.response.send_message("❌ You don't have permission to do that.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            run_at = await self._schedule(interaction.guild_id, sector, interaction.user.id)
        except Exception as e:
            print(f"Schedule error ({sector}): {e}")
            await interaction.followup.send(
                "❌ Something went wrong, so nothing was scheduled.", ephemeral=True
            )
            return

        if run_at is None:
            job = await self._get_job(interaction.guild_id, sector)
            when = fmt_time(job[0]) if job else "soon"
            await interaction.followup.send(
                f"⚠️ A wipe of **{SECTORS[sector]}** is already scheduled to run {when}. "
                "Use `/wipe cancel` to stop it.",
                ephemeral=True
            )
            return

        await interaction.followup.send(
            f"⏳ **Wipe scheduled:** {SECTORS[sector]}, to run {fmt_time(run_at)}.\n"
            "Use `/wipe cancel` any time before then to stop it.",
            ephemeral=True
        )
        await self._log(
            interaction.guild, f"⏳ Wipe scheduled: {sector}",
            [f"Wipes: {SECTORS[sector]}",
             f"Runs {fmt_time(run_at)}",
             "Stop it with `/wipe cancel`"],
            interaction.user.id, color=discord.Color.orange()
        )

    # ---------- /wipe queue ----------

    @wipe_group.command(name="queue", description="Show all scheduled wipes")
    @is_wipe_user()
    async def wipe_queue(self, ctx):
        jobs = await self._list_jobs(ctx.guild.id)
        if not jobs:
            await ctx.respond("✅ No wipes are scheduled.", ephemeral=True)
            return

        embed = discord.Embed(
            title="⏳ Scheduled wipes", color=discord.Color.orange(), timestamp=discord.utils.utcnow()
        )
        for sector, run_at, scheduled_by in jobs:
            embed.add_field(
                name=sector_label(sector),
                value=f"Runs {fmt_time(run_at)}\nScheduled by <@{scheduled_by}>",
                inline=False
            )
        embed.set_footer(text=f"{len(jobs)} wipe(s) • use /wipe cancel to stop one")
        await ctx.respond(embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    # ---------- /wipe cancel ----------

    @wipe_group.command(name="cancel", description="Cancel a scheduled wipe")
    @is_wipe_user()
    async def wipe_cancel(
        self, ctx,
        target: Option(
            str, "Which scheduled wipe to cancel (optional if only one is pending)",
            choices=TARGET_CHOICES, required=False, default=None
        ),
    ):
        jobs = await self._list_jobs(ctx.guild.id)
        if not jobs:
            await ctx.respond("❌ There's no wipe scheduled.", ephemeral=True)
            return

        if target is None:
            if len(jobs) > 1:
                pending = bullet_list(
                    f"**{sector_label(s)}**, runs {fmt_time(r)}" for s, r, _ in jobs
                )
                await ctx.respond(
                    f"⚠️ Several wipes are scheduled. Pick one with the `target` option:\n{pending}",
                    ephemeral=True
                )
                return
            target = jobs[0][0]

        job = next((j for j in jobs if j[0] == target), None)
        if job is None:
            await ctx.respond(f"❌ No wipe of **{sector_label(target)}** is scheduled.", ephemeral=True)
            return

        _, run_at, scheduled_by = job
        await self.bot.tag_db.execute(
            "DELETE FROM wipe_jobs WHERE guild = ? AND sector = ?", (ctx.guild.id, target)
        )
        await self.bot.tag_db.commit()

        await ctx.respond(
            f"✅ The scheduled wipe of **{sector_label(target)}** was cancelled. Nothing was wiped.",
            ephemeral=True
        )
        await self._log(
            ctx.guild, f"🛑 Scheduled wipe cancelled: {target}",
            [f"It was scheduled by <@{scheduled_by}> to run {fmt_time(run_at)}."],
            ctx.author.id, color=discord.Color.green()
        )

    # ---------- /wipe all ----------

    @wipe_group.command(
        name="all", description=f"Schedule a wipe of ALL bot data, {WIPE_DELAY_HOURS} hours from now"
    )
    @is_wipe_user()
    async def wipe_all(self, ctx):
        existing = await self._get_job(ctx.guild.id, ALL_KEY)
        if existing is not None:
            await ctx.respond(
                f"⚠️ A full wipe is already scheduled to run {fmt_time(existing[0])}. "
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
            f"to run in **{WIPE_DELAY_HOURS} hours**:\n"
            + bullet_list(SECTORS.values())
            + "\n\nYou'll get reminders with 12 hours and 1 hour left, and can stop it any time "
              "before then with `/wipe cancel`. "
              f"Press the button within {FINAL_CONFIRM_TIMEOUT} seconds, or nothing will be scheduled.",
            view=view, ephemeral=True
        )
        view.message = await interaction.original_response()

    async def _schedule_wipeall(self, interaction: discord.Interaction):
        try:
            run_at = await self._schedule(interaction.guild_id, ALL_KEY, interaction.user.id)
        except Exception as e:
            print(f"Schedule error (all): {e}")
            await interaction.edit_original_response(
                content="❌ Something went wrong, so nothing was scheduled.", view=None
            )
            return

        if run_at is None:
            job = await self._get_job(interaction.guild_id, ALL_KEY)
            when = fmt_time(job[0]) if job else "soon"
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