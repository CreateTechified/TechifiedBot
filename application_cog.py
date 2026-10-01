import asyncio
import re
from datetime import timedelta

import discord
from discord.ext import commands
from discord.commands import SlashCommandGroup, Option

from mc_utils import lookup_profile, whitelist_add

# --- CONFIGURATION ---
APPLICATION_CHANNEL_ID = 1318925028586291283

ADMIN_ROLE_ID = 1222456633511378965
MODERATOR_ROLE_ID = 1421877616272605326
OWNER_ROLE_ID = 1286650794053210122
STAFF_ROLE_IDS = {ADMIN_ROLE_ID, MODERATOR_ROLE_ID, OWNER_ROLE_ID}

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")

DM_ANSWER_TIMEOUT = 300
MAX_REASON_LENGTH = 1000

REAPPLY_COOLDOWN = timedelta(hours=24)

# For future pine (because i'm a dumbass): IT ONLY WORKS WITH DM APPLICATIONS.
QUESTIONS = [
    "What's your Minecraft username?",
    "Why do you want to join?",
]
TOTAL_QUESTIONS = len(QUESTIONS)


def question_prompt(n: int) -> str:
    return f"**Question {n}/{TOTAL_QUESTIONS}:** {QUESTIONS[n - 1]}"


STATUS_STYLE = {
    "pending": (discord.Color.gold(), "📝 Whitelist Application"),
    "accepted": (discord.Color.green(), "✅ Application Accepted"),
    "denied": (discord.Color.red(), "❌ Application Rejected"),
}

# Application statuses:
#   pending      - waiting for staff
#   denied       - staff said no
#   accepted     - staff said yes, waiting for the applicant to confirm their mc username
#   whitelisting - (briefly) being added to the whitelist
#   whitelisted  - done

# A BOT RESTART WILL END ANY IN-PROGRESS DM SESSIONS!!
ACTIVE_DM_SESSIONS = set()
_background_tasks = set()


def spawn(coro):
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def is_staff_member(user) -> bool:
    if not isinstance(user, discord.Member):
        return False
    return bool({role.id for role in user.roles} & STAFF_ROLE_IDS)


def build_application_embed(app_id, user, server_name, mc_username, reason, is_test=False):
    color, title = STATUS_STYLE["pending"]
    if is_test:
        title = "🧪 [TEST] " + title
    embed = discord.Embed(title=title, color=color, timestamp=discord.utils.utcnow())
    embed.add_field(name="Applicant", value=f"{user.mention} (`{user.id}`)", inline=False)
    embed.add_field(name="Server", value=f"`{server_name}`", inline=True)
    embed.add_field(name="Minecraft username", value=f"`{mc_username}`", inline=True)
    embed.add_field(name="Reason", value=reason[:1024], inline=False)
    embed.set_footer(text=f"Application #{app_id}")
    return embed


def build_summary_embed(app_id, server_name, mc_username, reason):
    embed = discord.Embed(
        title="📝 Your application", color=discord.Color.green(), timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="Server", value=f"`{server_name}`", inline=True)
    embed.add_field(name="Minecraft username", value=f"`{mc_username}`", inline=True)
    embed.add_field(name="Why do you want to join?", value=reason[:1024], inline=False)
    embed.set_footer(text=f"Application #{app_id}")
    return embed


async def notify_staff(client, application_message_id, text):
    channel = client.get_channel(APPLICATION_CHANNEL_ID)
    if channel is None:
        return
    try:
        await channel.get_partial_message(application_message_id).reply(
            text, mention_author=False, allowed_mentions=discord.AllowedMentions.none()
        )
    except discord.HTTPException:
        pass


# ---------- DM question engine ----------

class DMCancelled(Exception):
    pass


class DMTimedOut(Exception):
    pass


async def dm_ask(bot, channel, user_id, prompt, validate):
    await channel.send(prompt)

    def check(m):
        return m.author.id == user_id and m.channel.id == channel.id

    while True:
        try:
            msg = await bot.wait_for("message", check=check, timeout=DM_ANSWER_TIMEOUT)
        except asyncio.TimeoutError:
            raise DMTimedOut()

        text = (msg.content or "").strip()
        if text.lower() == "cancel":
            raise DMCancelled()

        error, value = await validate(text)
        if error:
            await channel.send(error)
            continue
        return value


async def validate_username_answer(text):
    if not USERNAME_RE.match(text):
        return ("❌ That isn't a valid Minecraft username (3-16 letters, numbers or underscores). "
                "Try again, or type `cancel`."), None

    state, canonical, _ = await lookup_profile(text)
    if state == "not_found":
        return (f"❌ There's no Minecraft account named `{text}`. "
                "Check the spelling and try again, or type `cancel`."), None
    if state == "ok":
        return None, (canonical, True)
    return None, (text, False)  # Mojang unreachable: accept, but mark as unverified.. just in case


async def validate_reason_answer(text):
    if not text:
        return "❌ Please send your answer as text. Try again, or type `cancel`.", None
    if len(text) > MAX_REASON_LENGTH:
        return (f"❌ That's {len(text)} characters, but the limit is {MAX_REASON_LENGTH}. "
                "Please shorten it and send it again."), None
    return None, text


# ---------- submitting an application (shared by the popup and DM flows) ----------

async def get_pending_application(db, guild_id, user_id, server_name):
    async with db.execute(
        "SELECT id FROM applications WHERE guild = ? AND user_id = ? AND server_name = ? "
        "AND status = 'pending' AND is_test = 0",
        (guild_id, user_id, server_name)
    ) as cursor:
        row = await cursor.fetchone()
    return row[0] if row else None


async def get_closed_message(db, guild_id):
    async with db.execute("SELECT closed FROM app_settings WHERE guild = ?", (guild_id,)) as cursor:
        row = await cursor.fetchone()
    if row and row[0]:
        return "🔒 Whitelist applications are currently closed. Check back later!"
    return None


async def is_prefix_apply_disabled(db, guild_id):
    async with db.execute(
        "SELECT prefix_apply_disabled FROM app_settings WHERE guild = ?", (guild_id,)
    ) as cursor:
        row = await cursor.fetchone()
    return bool(row and row[0])


async def get_application_block(db, guild_id, user_id, server_name):
    closed = await get_closed_message(db, guild_id)
    if closed:
        return closed

    pending = await get_pending_application(db, guild_id, user_id, server_name)
    if pending is not None:
        return f"⚠️ You already have a pending application (#{pending}) for `{server_name}`."

    async with db.execute(
        "SELECT 1 FROM applications WHERE guild = ? AND user_id = ? AND server_name = ? "
        "AND status IN ('accepted', 'whitelisting', 'whitelisted') AND is_test = 0 LIMIT 1",
        (guild_id, user_id, server_name)
    ) as cursor:
        if await cursor.fetchone() is not None:
            return (f"✅ Your application for `{server_name}` was already accepted. "
                    "If you want to get an alt whitelisted, contact staff.")

    async with db.execute(
        "SELECT reviewed_at FROM applications WHERE guild = ? AND user_id = ? AND server_name = ? "
        "AND status = 'denied' AND is_test = 0 ORDER BY reviewed_at DESC LIMIT 1",
        (guild_id, user_id, server_name)
    ) as cursor:
        row = await cursor.fetchone()

    if row and row[0]:
        reviewed = discord.utils.parse_time(row[0])
        if reviewed is not None:
            retry_at = reviewed + REAPPLY_COOLDOWN
            if retry_at > discord.utils.utcnow():
                return (f"⏳ Your last application for `{server_name}` was rejected. "
                        f"You can apply again {discord.utils.format_dt(retry_at, style='R')}.")
    return None


async def submit_application(bot, user, guild_id, server_name, username, reason, verified, test=False):
    db = bot.tag_db

    async with db.execute(
        "SELECT 1 FROM mc_servers WHERE guild = ? AND name = ?", (guild_id, server_name)
    ) as cursor:
        if await cursor.fetchone() is None:
            return f"❌ `{server_name}` isn't a tracked server anymore.", None

    if not test:
        block = await get_application_block(db, guild_id, user.id, server_name)
        if block:
            return block, None

    channel = bot.get_channel(APPLICATION_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(APPLICATION_CHANNEL_ID)
        except discord.HTTPException:
            channel = None
    if channel is None:
        return "❌ I can't reach the applications channel right now. Please tell a staff member.", None

    cursor = await db.execute(
        "INSERT INTO applications (guild, user_id, server_name, mc_username, reason, status, created_at, is_test) "
        "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
        (guild_id, user.id, server_name, username, reason, discord.utils.utcnow().isoformat(), int(test))
    )
    await db.commit()
    app_id = cursor.lastrowid
    await cursor.close()

    embed = build_application_embed(app_id, user, server_name, username, reason, test)
    if not verified:
        embed.add_field(
            name="⚠️ Username not verified",
            value="Mojang couldn't be reached, so this name hasn't been checked yet.",
            inline=False
        )
    try:
        msg = await channel.send(
            embed=embed, view=ReviewView(), allowed_mentions=discord.AllowedMentions.none()
        )
    except discord.HTTPException:
        await db.execute("DELETE FROM applications WHERE id = ?", (app_id,))
        await db.commit()
        return "❌ I couldn't post your application. Please tell a staff member.", None

    await db.execute("UPDATE applications SET message_id = ? WHERE id = ?", (msg.id, app_id))
    await db.commit()
    return None, app_id


async def run_dm_application(bot, user, channel, guild_id, server_name, test=False):
    try:
        try:
            username, verified = await dm_ask(
                bot, channel, user.id, question_prompt(1), validate_username_answer
            )
            reason = await dm_ask(bot, channel, user.id, question_prompt(2), validate_reason_answer)
        except DMCancelled:
            await channel.send("🛑 Application cancelled. You can start again any time with `.apply`.")
            return
        except DMTimedOut:
            await channel.send("⌛ I stopped waiting for an answer, so the application was cancelled. "
                               "You can start again any time with `.apply`.")
            return

        error, app_id = await submit_application(
            bot, user, guild_id, server_name, username, reason, verified, test
        )
        if error:
            await channel.send(error)
            return

        await channel.send(
            f"✅ Your application for `{server_name}` has been submitted! Staff will review it soon. "
            f"Here's a copy of your answers:",
            embed=build_summary_embed(app_id, server_name, username, reason)
        )
    except discord.HTTPException:
        pass
    finally:
        ACTIVE_DM_SESSIONS.discard(user.id)


# ---------- staff review ----------

async def finalize_application(interaction: discord.Interaction, view, new_status: str, reason: str = None):
    db = interaction.client.tag_db
    message = interaction.message

    async with db.execute(
        "SELECT id, user_id, server_name, mc_username, status, is_test FROM applications WHERE message_id = ?",
        (message.id,)
    ) as cursor:
        row = await cursor.fetchone()

    if row is None:
        await interaction.response.send_message("❌ I have no record of this application.", ephemeral=True)
        return

    app_id, user_id, server_name, mc_username, status, is_test = row

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
    embed.title = ("🧪 [TEST] " if is_test else "") + title
    embed.add_field(
        name="Accepted by" if new_status == "accepted" else "Denied by",
        value=interaction.user.mention, inline=False
    )
    if reason:
        embed.add_field(name="Reason", value=reason[:1024], inline=False)

    for item in view.children:
        item.disabled = True
    await interaction.response.edit_message(embed=embed, view=view)

    if new_status == "accepted":
        sent = await send_accept_dm(interaction.client, user_id, app_id, server_name, mc_username)
        if not sent:
            await notify_staff(
                interaction.client, message.id,
                "⚠️ I couldn't DM the applicant (their DMs are probably closed), so they haven't been "
                "asked to confirm their username. Whitelist them manually with `/whitelist add`."
            )
    else:
        try:
            applicant = await interaction.client.fetch_user(user_id)
            text = f"❌ Your whitelist application for **{server_name}** was rejected."
            if reason:
                text += f"\n**Reason:** {reason}"
            await applicant.send(text)
        except (discord.Forbidden, discord.HTTPException):
            pass


# ---------- username confirmation (after acceptance) ----------

async def send_accept_dm(client, user_id, app_id, server_name, mc_username) -> bool:
    try:
        user = await client.fetch_user(user_id)
        msg = await user.send(
            f"🎉 Your whitelist application for **{server_name}** was accepted!\n\n"
            f"Before I whitelist you: is `{mc_username}` your actual Minecraft username?",
            view=UsernameConfirmView()
        )
    except (discord.Forbidden, discord.HTTPException):
        return False

    db = client.tag_db
    await db.execute("UPDATE applications SET confirm_message_id = ? WHERE id = ?", (msg.id, app_id))
    await db.commit()
    return True


async def complete_whitelist(client, app_id: int, username: str):
    """Verifies the account, whitelists it, and records it.

    Returns (status, text):
      ("ok", canonical_username)  - whitelisted
      ("retry", error_text)       - failed, the user can try again
      ("done", text)              - this application was already completed
    """
    db = client.tag_db

    state, canonical, player_uuid = await lookup_profile(username)
    if state == "not_found":
        return "retry", f"❌ There's no Minecraft account named `{username}`."
    if state == "error":
        return "retry", "❌ I couldn't reach Mojang to check that name. Please try again in a minute."

    cursor = await db.execute(
        "UPDATE applications SET status = 'whitelisting' WHERE id = ? AND status = 'accepted'", (app_id,)
    )
    await db.commit()
    claimed = cursor.rowcount
    await cursor.close()
    if not claimed:
        return "done", "⚠️ This application has already been completed."

    async with db.execute("SELECT is_test FROM applications WHERE id = ?", (app_id,)) as cursor:
        test_row = await cursor.fetchone()
    if test_row and test_row[0]:
        ok, code = True, None
    else:
        ok, code = await whitelist_add(player_uuid)
    if not ok:
        await db.execute("UPDATE applications SET status = 'accepted' WHERE id = ?", (app_id,))
        await db.commit()
        detail = f" (error {code})" if code else ""
        return "retry", f"❌ The whitelist service didn't accept that{detail}. Try again, or tell a staff member."

    await db.execute(
        "UPDATE applications SET status = 'whitelisted', final_username = ?, whitelisted_at = ? WHERE id = ?",
        (canonical, discord.utils.utcnow().isoformat(), app_id)
    )
    await db.commit()
    return "ok", canonical


async def _get_by_confirm_message(db, message_id):
    async with db.execute(
        "SELECT id, server_name, mc_username, message_id, status FROM applications "
        "WHERE confirm_message_id = ?",
        (message_id,)
    ) as cursor:
        return await cursor.fetchone()


TEST_NOTE = " 🧪 *Test application: nothing was actually whitelisted.*"


async def test_note(db, app_id):
    async with db.execute("SELECT is_test FROM applications WHERE id = ?", (app_id,)) as cursor:
        row = await cursor.fetchone()
    return TEST_NOTE if row and row[0] else ""


async def remove_confirm_buttons(user, message_id):
    try:
        dm = user.dm_channel or await user.create_dm()
        await dm.get_partial_message(message_id).edit(view=None)
    except discord.HTTPException:
        pass


class CorrectUsernameModal(discord.ui.Modal):
    def __init__(self, app_id, applied_name, confirm_message_id, application_message_id):
        super().__init__(title="Your Minecraft username")
        self.app_id = app_id
        self.applied_name = applied_name
        self.confirm_message_id = confirm_message_id
        self.application_message_id = application_message_id
        self.username_input = discord.ui.InputText(
            label="Your actual Minecraft username",
            style=discord.InputTextStyle.short,
            min_length=3, max_length=16, required=True,
        )
        self.add_item(self.username_input)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        username = (self.username_input.value or "").strip()

        if not USERNAME_RE.match(username):
            await interaction.followup.send(
                "❌ That isn't a valid Minecraft username (3-16 letters, numbers or underscores). "
                "Press the button to try again.", ephemeral=True
            )
            return

        status, result = await complete_whitelist(interaction.client, self.app_id, username)
        if status != "ok":
            suffix = "\nPress the button to try again." if status == "retry" else ""
            await interaction.followup.send(f"{result}{suffix}", ephemeral=True)
            return

        note = await test_note(interaction.client.tag_db, self.app_id)
        done_text = f"✅ You're whitelisted as `{result}`! See you in game.{note}"
        await interaction.edit_original_response(view=None)
        await remove_confirm_buttons(interaction.user, self.confirm_message_id)  # removes Yes/No buttons
        await interaction.followup.send(done_text)
        await notify_staff(
            interaction.client, self.application_message_id,
            f"✅ Whitelisted `{result}` (applied as `{self.applied_name}`, corrected by the applicant).{note}"
        )


async def run_dm_correction(bot, user, channel, app_id, applied_name, confirm_message_id, application_message_id):
    """Asks for the correct username in DMs, whitelists it, and finishes up :thumbsup:"""

    async def validate(text):
        if not USERNAME_RE.match(text):
            return ("❌ That isn't a valid Minecraft username (3-16 letters, numbers or underscores). "
                    "Try again, or type `cancel`."), None
        status, result = await complete_whitelist(bot, app_id, text)
        if status == "retry":
            return f"{result}\nTry again, or type `cancel`.", None
        return None, (status, result)

    try:
        try:
            status, result = await dm_ask(
                bot, channel, user.id,
                "What's your actual Minecraft username? (Type `cancel` to stop.)", validate
            )
        except DMCancelled:
            await channel.send("🛑 Cancelled. Press **No, change it** on the message above whenever you're ready.")
            return
        except DMTimedOut:
            await channel.send("⌛ I stopped waiting for an answer. Press **No, change it** on the message "
                               "above whenever you're ready.")
            return

        if status == "done":
            await channel.send(result)
            return

        note = await test_note(bot.tag_db, app_id)
        done_text = f"✅ You're whitelisted as `{result}`! See you in game.{note}"
        await remove_confirm_buttons(user, confirm_message_id)
        await channel.send(done_text)
        await notify_staff(
            bot, application_message_id,
            f"✅ Whitelisted `{result}` (applied as `{applied_name}`, corrected by the applicant).{note}"
        )
    except discord.HTTPException:
        pass
    finally:
        ACTIVE_DM_SESSIONS.discard(user.id)


class CorrectMethodView(discord.ui.View):

    def __init__(self, app_id, applied_name, confirm_message_id, application_message_id):
        super().__init__(timeout=300)
        self.app_id = app_id
        self.applied_name = applied_name
        self.confirm_message_id = confirm_message_id
        self.application_message_id = application_message_id

    @discord.ui.button(label="Fill out a popup", style=discord.ButtonStyle.primary, emoji="📋")
    async def popup(self, button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.send_modal(CorrectUsernameModal(
            self.app_id, self.applied_name, self.confirm_message_id, self.application_message_id
        ))

    @discord.ui.button(label="Type it in chat", style=discord.ButtonStyle.secondary, emoji="💬")
    async def chat(self, button: discord.ui.Button, interaction: discord.Interaction):
        user = interaction.user
        if user.id in ACTIVE_DM_SESSIONS:
            await interaction.response.send_message(
                "⚠️ You already have a question in progress. Answer it (or type `cancel`) first.",
                ephemeral=True
            )
            return
        ACTIVE_DM_SESSIONS.add(user.id)

        channel = user.dm_channel or await user.create_dm()
        await interaction.response.edit_message(
            content="💬 Okay! I'll ask for it in a new message below.", view=None
        )
        spawn(run_dm_correction(
            interaction.client, user, channel, self.app_id, self.applied_name,
            self.confirm_message_id, self.application_message_id
        ))


class UsernameConfirmView(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Yes, that's me", style=discord.ButtonStyle.success, custom_id="application_username_yes"
    )
    async def yes(self, button: discord.ui.Button, interaction: discord.Interaction):
        db = interaction.client.tag_db
        row = await _get_by_confirm_message(db, interaction.message.id)
        if row is None:
            await interaction.response.send_message("❌ I have no record of this application.", ephemeral=True)
            return

        app_id, server_name, mc_username, application_message_id, status = row
        await interaction.response.defer()

        result_status, result = await complete_whitelist(interaction.client, app_id, mc_username)
        if result_status != "ok":
            await interaction.followup.send(result, ephemeral=True)
            return

        note = await test_note(db, app_id)
        await interaction.edit_original_response(view=None)  # just remove the damn buttons
        await interaction.followup.send(f"✅ You're whitelisted as `{result}`! See you in game.{note}")
        await notify_staff(interaction.client, application_message_id, f"✅ Whitelisted `{result}`.{note}")

    @discord.ui.button(
        label="No, change it", style=discord.ButtonStyle.secondary, custom_id="application_username_no"
    )
    async def no(self, button: discord.ui.Button, interaction: discord.Interaction):
        db = interaction.client.tag_db
        row = await _get_by_confirm_message(db, interaction.message.id)
        if row is None:
            await interaction.response.send_message("❌ I have no record of this application.", ephemeral=True)
            return

        app_id, _, mc_username, application_message_id, status = row
        if status != "accepted":
            await interaction.response.send_message("⚠️ This application has already been completed.", ephemeral=True)
            return

        await interaction.response.send_message(
            "How would you like to enter your username?",
            view=CorrectMethodView(app_id, mc_username, interaction.message.id, application_message_id)
        )

# ---------- application form (popup) ----------

class ApplicationModal(discord.ui.Modal):
    def __init__(self, cog, server_name: str, test: bool = False):
        super().__init__(title=f"Whitelist: {server_name}"[:45])
        self.cog = cog
        self.server_name = server_name
        self.test = test

        self.username_input = discord.ui.InputText(
            label="Minecraft username",
            placeholder="Your in-game name",
            style=discord.InputTextStyle.short,
            min_length=3, max_length=16, required=True,
        )
        self.reason_input = discord.ui.InputText(
            label="Why do you want to join?",
            style=discord.InputTextStyle.paragraph,
            max_length=MAX_REASON_LENGTH, required=True,
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

        state, canonical, _ = await lookup_profile(username)
        if state == "not_found":
            await interaction.followup.send(
                f"❌ There's no Minecraft account named `{username}`. Check the spelling and apply again.",
                ephemeral=True
            )
            return
        verified = state == "ok"
        if verified:
            username = canonical

        error, app_id = await submit_application(
            self.cog.bot, interaction.user, interaction.guild_id, self.server_name,
            username, reason, verified, self.test
        )
        if error:
            await interaction.followup.send(error, ephemeral=True)
            return

        await interaction.followup.send(
            f"✅ Your application for `{self.server_name}` has been submitted! Staff will review it soon. "
            f"Here's a copy of your answers:",
            embed=build_summary_embed(app_id, self.server_name, username, reason),
            ephemeral=True
        )


# ---------- choosing how to apply ----------

class MethodChoiceView(discord.ui.View):
    """Popup or DM questions. Just so no one thinks it's a scam :)"""

    def __init__(self, cog, server_name: str, owner_id=None, test: bool = False):
        super().__init__(timeout=300)
        self.cog = cog
        self.server_name = server_name
        self.owner_id = owner_id
        self.test = test

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.owner_id is not None and interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "❌ This isn't for you. Run `.apply` yourself.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Fill out a popup", style=discord.ButtonStyle.primary, emoji="📋")
    async def popup(self, button: discord.ui.Button, interaction: discord.Interaction):
        block = None if self.test else await get_application_block(
            self.cog.bot.tag_db, interaction.guild_id, interaction.user.id, self.server_name
        )
        if block:
            await interaction.response.send_message(block, ephemeral=True)
            return
        await interaction.response.send_modal(ApplicationModal(self.cog, self.server_name, self.test))

    @discord.ui.button(label="Answer in DMs", style=discord.ButtonStyle.secondary, emoji="💬")
    async def dms(self, button: discord.ui.Button, interaction: discord.Interaction):
        user = interaction.user
        if user.id in ACTIVE_DM_SESSIONS:
            await interaction.response.send_message(
                "⚠️ You already have a question in progress in your DMs. Answer it (or type `cancel`) first.",
                ephemeral=True
            )
            return
        ACTIVE_DM_SESSIONS.add(user.id)

        await interaction.response.defer()

        block = None if self.test else await get_application_block(
            self.cog.bot.tag_db, interaction.guild_id, user.id, self.server_name
        )
        if block:
            ACTIVE_DM_SESSIONS.discard(user.id)
            await interaction.followup.send(block, ephemeral=True)
            return

        try:
            channel = await user.create_dm()
            await channel.send(
                f"📬 **Whitelist application for `{self.server_name}`**{' 🧪 *(test mode)*' if self.test else ''}\n"
                f"I'll ask you {TOTAL_QUESTIONS} questions, one at a time. Just reply to each in this chat.\n"
                f"Type `cancel` at any time to stop. If you don't answer for "
                f"{DM_ANSWER_TIMEOUT // 60} minutes, I'll cancel automatically."
            )
        except (discord.Forbidden, discord.HTTPException):
            ACTIVE_DM_SESSIONS.discard(user.id)
            await interaction.followup.send(
                "❌ I couldn't DM you. Turn on DMs from server members and try again, "
                "or use the popup instead.", ephemeral=True
            )
            return

        await interaction.edit_original_response(
            content="📬 Check your DMs! I've sent you the first question.", view=None
        )
        spawn(run_dm_application(self.cog.bot, user, channel, interaction.guild_id, self.server_name, self.test))


async def send_method_choice(interaction: discord.Interaction, cog, server_name: str, test: bool = False):
    await interaction.response.send_message(
        f"How would you like to apply for `{server_name}`?",
        view=MethodChoiceView(cog, server_name, owner_id=interaction.user.id, test=test),
        ephemeral=True
    )


# ---------- choosing a server ----------

class ServerSelectView(discord.ui.View):

    def __init__(self, cog, names, owner_id=None, test: bool = False):
        super().__init__(timeout=120)
        self.cog = cog
        self.owner_id = owner_id
        self.test = test

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
        await send_method_choice(interaction, self.cog, self.select.values[0], self.test)


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
        closed = await get_closed_message(interaction.client.tag_db, interaction.guild_id)
        if closed:
            await interaction.response.send_message(closed, ephemeral=True)
            return

        names = await self.cog._server_names(interaction.guild_id)
        if not names:
            await interaction.response.send_message(
                "❌ There are no servers open for applications right now.", ephemeral=True
            )
            return

        if len(names) == 1:
            await send_method_choice(interaction, self.cog, names[0])
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
        bot.add_view(UsernameConfirmView())

    application_group = SlashCommandGroup("application", "Whitelist application system (admin only)")

    async def _server_names(self, guild_id: int):
        async with self.bot.tag_db.execute(
            "SELECT name FROM mc_servers WHERE guild = ? ORDER BY name", (guild_id,)
        ) as cursor:
            rows = await cursor.fetchall()
        return [row[0] for row in rows]

    async def server_name_autocomplete(self, ctx: discord.AutocompleteContext):
        names = await self._server_names(ctx.interaction.guild_id)
        typed = (ctx.value or "").lower()
        return [n for n in names if typed in n.lower()][:25]

    async def _start_apply(self, ctx, server=None, test=False):
        slash = isinstance(ctx, discord.ApplicationContext)

        async def reply(*args, **kwargs):
            if slash:
                return await ctx.respond(*args, ephemeral=True, **kwargs)
            return await ctx.send(*args, **kwargs)

        if not test:
            closed = await get_closed_message(self.bot.tag_db, ctx.guild.id)
            if closed:
                await reply(closed)
                return

        names = await self._server_names(ctx.guild.id)
        if not names:
            await reply("❌ There are no servers open for applications right now.")
            return

        if server:
            server = server.strip().lower()
            if server not in names:
                available = ", ".join(f"`{n}`" for n in names)
                await reply(f"❌ No server named `{server}`. Available: {available}")
                return
        elif len(names) == 1:
            server = names[0]

        prefix = "🧪 **Test mode:** nothing will actually be whitelisted.\n" if test else ""
        if server:
            await reply(
                f"{prefix}How would you like to apply for `{server}`?",
                view=MethodChoiceView(self, server, ctx.author.id, test)
            )
        else:
            await reply(
                f"{prefix}Which server do you want to apply for?",
                view=ServerSelectView(self, names, owner_id=ctx.author.id, test=test)
            )

    @commands.command(name="apply")
    @commands.guild_only()
    async def apply_prefix(self, ctx, server: str = None):
        if await is_prefix_apply_disabled(self.bot.tag_db, ctx.guild.id):
            return  # silently ignored: members must use the panel button
        await self._start_apply(ctx, server)

    @discord.slash_command(name="apply", description="Apply for a whitelist (admin only). Use test=True to try it safely")
    async def apply_slash(
        self, ctx,
        server: Option(str, "Server to apply for", required=False, default=None, autocomplete=server_name_autocomplete),
        test: Option(bool, "Test mode: skips all checks and never actually whitelists", required=False, default=False),
    ):
        await self._start_apply(ctx, server, test)

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

    @application_group.command(name="toggle", description="Open or close whitelist applications for everyone")
    async def application_toggle(
        self, ctx,
        enabled: Option(bool, "True to open applications, False to close them"),
    ):
        await self.bot.tag_db.execute(
            "INSERT INTO app_settings (guild, closed) VALUES (?, ?) "
            "ON CONFLICT(guild) DO UPDATE SET closed = excluded.closed",
            (ctx.guild.id, int(not enabled))
        )
        await self.bot.tag_db.commit()

        if enabled:
            await ctx.respond("✅ Whitelist applications are now **open**.", ephemeral=True)
        else:
            await ctx.respond(
                "🔒 Whitelist applications are now **closed** for everyone. "
                "`/apply` with `test: True` still works for testing.",
                ephemeral=True
            )

    @application_group.command(name="prefixapply", description="Turn the .apply command on or off (panel button keeps working)")
    async def application_prefixapply(
        self, ctx,
        enabled: Option(bool, "True to let .apply work, False to make the bot ignore it"),
    ):
        await self.bot.tag_db.execute(
            "INSERT INTO app_settings (guild, prefix_apply_disabled) VALUES (?, ?) "
            "ON CONFLICT(guild) DO UPDATE SET prefix_apply_disabled = excluded.prefix_apply_disabled",
            (ctx.guild.id, int(not enabled))
        )
        await self.bot.tag_db.commit()

        if enabled:
            await ctx.respond("✅ `.apply` is now **enabled**.", ephemeral=True)
        else:
            await ctx.respond(
                "🔕 `.apply` is now **disabled**. The bot will ignore it, so members have to use the "
                "apply panel button (post one with `/application panel`).",
                ephemeral=True
            )


def setup(bot):
    bot.add_cog(Applications(bot))