import asyncio

asyncio.set_event_loop(asyncio.new_event_loop())
import discord
from discord.ext import commands
import os
import aiosqlite
from dotenv import load_dotenv

from tag_cog import TagReportView
from slash_cog import ADMIN_ROLE_IDS, ADMIN_OVERRIDE_USER_IDS

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

SLASH_DENIED_MESSAGE = (
    "❌ Slash commands are restricted to admins. "
    "Please use the `.` prefix commands instead (e.g. `.tag`, `.afk`, `.mcstatus`)."
)


def is_admin_member(user) -> bool:
    if user.id in ADMIN_OVERRIDE_USER_IDS:
        return True
    if not isinstance(user, discord.Member):
        return False
    return bool({role.id for role in user.roles} & ADMIN_ROLE_IDS)


class ReplyContext(commands.Context):

    async def send(self, *args, **kwargs):
        kwargs.setdefault("reference", self.message)
        kwargs.setdefault("mention_author", False)
        try:
            return await super().send(*args, **kwargs)
        except discord.HTTPException:
            kwargs.pop("reference", None)
            return await super().send(*args, **kwargs)


class TechifiedBot(commands.Bot):
    async def get_context(self, message, *, cls=ReplyContext):
        return await super().get_context(message, cls=cls)

    async def invoke_application_command(self, ctx):
        if not is_admin_member(ctx.author):
            await ctx.respond(SLASH_DENIED_MESSAGE, ephemeral=True)
            return
        await super().invoke_application_command(ctx)


bot = TechifiedBot(
    command_prefix=".",
    intents=intents,
    auto_sync_commands=True
)

presence = discord.Game("modpack release soon??")

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} ({bot.user.id})")
    await bot.change_presence(status=discord.Status.online)

@bot.command()
async def ping(ctx):
    await ctx.send("Pong!")

async def setup_database(bot):
    bot.tag_db = await aiosqlite.connect("tags.db")

    await bot.tag_db.execute(
        """CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            content TEXT,
            attachments TEXT,
            attachments_size INTEGER NOT NULL DEFAULT 0,
            reported INTEGER NOT NULL DEFAULT 0,
            guild INTEGER NOT NULL,
            creator INTEGER NOT NULL,
            UNIQUE(name, guild)
        )"""
    )
    for migration in (
        "ALTER TABLE tags ADD COLUMN attachments_size INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE tags ADD COLUMN reported INTEGER NOT NULL DEFAULT 0",
    ):
        try:
            await bot.tag_db.execute(migration)
        except aiosqlite.OperationalError:
            pass

    await bot.tag_db.execute(
        """CREATE TABLE IF NOT EXISTS tag_aliases (
            name TEXT NOT NULL,
            guild INTEGER NOT NULL,
            original_name TEXT NOT NULL,
            creator INTEGER NOT NULL,
            UNIQUE(name, guild)
        )"""
    )

    await bot.tag_db.execute(
        """CREATE TABLE IF NOT EXISTS tag_command_aliases (
            alias TEXT PRIMARY KEY,
            target_command TEXT NOT NULL,
            creator INTEGER NOT NULL
        )"""
    )

    await bot.tag_db.execute(
        """CREATE TABLE IF NOT EXISTS warnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            moderator_id INTEGER NOT NULL,
            reason TEXT,
            timestamp TEXT NOT NULL
        )"""
    )

    await bot.tag_db.execute(
        """CREATE TABLE IF NOT EXISTS help_threads (
            thread_id INTEGER PRIMARY KEY,
            requester_id INTEGER NOT NULL,
            closed INTEGER NOT NULL DEFAULT 0,
            closed_at TEXT
        )"""
    )
    for migration in (
        "ALTER TABLE help_threads ADD COLUMN closed INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE help_threads ADD COLUMN closed_at TEXT",
    ):
        try:
            await bot.tag_db.execute(migration)
        except aiosqlite.OperationalError:
            pass

    await bot.tag_db.execute(
        """CREATE TABLE IF NOT EXISTS auto_replies (
            guild INTEGER NOT NULL,
            trigger_text TEXT NOT NULL,
            reply_text TEXT NOT NULL,
            creator INTEGER NOT NULL,
            UNIQUE(guild, trigger_text)
        )"""
    )

    await bot.tag_db.execute(
        """CREATE TABLE IF NOT EXISTS mc_servers (
            guild INTEGER NOT NULL,
            name TEXT NOT NULL,
            host TEXT NOT NULL,
            port INTEGER NOT NULL DEFAULT 25565,
            creator INTEGER NOT NULL,
            hide_ip INTEGER NOT NULL DEFAULT 0,
            UNIQUE(guild, name)
        )"""
    )
    for migration in (
        "ALTER TABLE mc_servers ADD COLUMN hide_ip INTEGER NOT NULL DEFAULT 0",
    ):
        try:
            await bot.tag_db.execute(migration)
        except aiosqlite.OperationalError:
            pass

    await bot.tag_db.execute(
        """CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            server_name TEXT NOT NULL,
            mc_username TEXT NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            message_id INTEGER,
            created_at TEXT NOT NULL,
            reviewer_id INTEGER,
            reviewed_at TEXT,
            confirm_message_id INTEGER,
            final_username TEXT,
            whitelisted_at TEXT,
            is_test INTEGER NOT NULL DEFAULT 0
        )"""
    )
    for migration in (
        "ALTER TABLE applications ADD COLUMN confirm_message_id INTEGER",
        "ALTER TABLE applications ADD COLUMN final_username TEXT",
        "ALTER TABLE applications ADD COLUMN whitelisted_at TEXT",
        "ALTER TABLE applications ADD COLUMN is_test INTEGER NOT NULL DEFAULT 0",
    ):
        try:
            await bot.tag_db.execute(migration)
        except aiosqlite.OperationalError:
            pass

    await bot.tag_db.execute(
        """CREATE TABLE IF NOT EXISTS app_settings (
            guild INTEGER PRIMARY KEY,
            closed INTEGER NOT NULL DEFAULT 0,
            prefix_apply_disabled INTEGER NOT NULL DEFAULT 0
        )"""
    )
    for migration in (
        "ALTER TABLE app_settings ADD COLUMN prefix_apply_disabled INTEGER NOT NULL DEFAULT 0",
    ):
        try:
            await bot.tag_db.execute(migration)
        except aiosqlite.OperationalError:
            pass

    await bot.tag_db.execute(
        """CREATE TABLE IF NOT EXISTS larp_scores (
            guild INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            points INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild, user_id)
        )"""
    )

    await bot.tag_db.execute(
        """CREATE TABLE IF NOT EXISTS larp_timeouts (
            guild INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            until TEXT,
            offense_count INTEGER NOT NULL DEFAULT 0,
            streak_count INTEGER NOT NULL DEFAULT 0,
            last_larp_at TEXT,
            PRIMARY KEY (guild, user_id)
        )"""
    )
    for migration in (
        "ALTER TABLE larp_timeouts ADD COLUMN streak_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE larp_timeouts ADD COLUMN last_larp_at TEXT",
    ):
        try:
            await bot.tag_db.execute(migration)
        except aiosqlite.OperationalError:
            pass

    await bot.tag_db.execute(
        """CREATE TABLE IF NOT EXISTS afk_status (
            guild INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            since TEXT NOT NULL,
            original_nick TEXT,
            tagged_nick TEXT NOT NULL,
            PRIMARY KEY (guild, user_id)
        )"""
    )

    await bot.tag_db.commit()

async def register_persistent_views(bot):
    async with bot.tag_db.execute("SELECT guild, name FROM tags WHERE reported = 1") as cursor:
        rows = await cursor.fetchall()
    for guild_id, name in rows:
        bot.add_view(TagReportView(guild_id, name))

async def register_tag_command_aliases(bot):
    cog = bot.get_cog("TagSystem")
    if cog is None:
        return

    async with bot.tag_db.execute(
        "SELECT alias, target_command FROM tag_command_aliases"
    ) as cursor:
        rows = await cursor.fetchall()

    for alias, target in rows:
        target_command = cog.tag.all_commands.get(target)
        if target_command is not None:
            cog.tag.all_commands[alias] = target_command

async def main():
    async with bot:
        await setup_database(bot)
        await register_persistent_views(bot)

        bot.load_extension('help_cog')
        bot.load_extension('tag_cog')
        bot.load_extension('server_cog')
        bot.load_extension('slash_cog')
        bot.load_extension('automod_cog')
        bot.load_extension('wss_cog')
        bot.load_extension('larp_cog')
        bot.load_extension('utility_cog')
        bot.load_extension('afk_cog')
        bot.load_extension('application_cog')

        await register_tag_command_aliases(bot)

        token = os.getenv("DSC_TOKEN")
        if not token:
            print("❌ ERROR: No DSC_TOKEN found in .env file!")
            return
        await bot.start(token)

asyncio.run(main())