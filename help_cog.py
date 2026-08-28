import discord
from discord.ext import commands, tasks
import re
from datetime import datetime, timezone, timedelta

class CommunityHelp(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.kdan = re.compile(
            r"\[(?P<filename>.*?)]\(<?https://p\.kdan\.dev/(?P<id>[a-zA-Z0-9]+)>?\)")
        self.gnomebot = re.compile(
            r"\[(?P<filename>.*?)]\(<?https://gnomebot\.dev/paste/mclogs/(?P<id>[a-zA-Z0-9]+)>?\)")
        self.mclogs = re.compile(
            r"\[(?P<filename>.*?)]\(<?https://mclo\.gs/(?P<id>[a-zA-Z0-9]+)>?\)")

        # --- CONFIGURATION ---
        self.HELP_CHANNEL_ID = 1425955723552100362
        self.STAFF_LOG_CHANNEL_ID = 1472650884906221771
        self.cooldowns = commands.CooldownMapping.from_cooldown(1, 60.0, commands.BucketType.user)

        # Start the cleanup loop
        self.cleanup_old_threads.start()

    def cog_unload(self):
        self.cleanup_old_threads.cancel()

    @tasks.loop(hours=12)
    async def cleanup_old_threads(self):
        staff_log = self.bot.get_channel(self.STAFF_LOG_CHANNEL_ID)
        now = datetime.now(timezone.utc)
        threshold = timedelta(days=7)

        async with self.bot.tag_db.execute(
            "SELECT thread_id, closed_at FROM help_threads WHERE closed = 1 AND closed_at IS NOT NULL"
        ) as cursor:
            rows = await cursor.fetchall()

        for thread_id, closed_at in rows:
            try:
                closed_dt = datetime.fromisoformat(closed_at)
            except (TypeError, ValueError):
                continue

            if now - closed_dt <= threshold:
                continue

            thread_name = f"id {thread_id}"
            try:
                thread = self.bot.get_channel(thread_id) or await self.bot.fetch_channel(thread_id)
                thread_name = thread.name
                await thread.delete()
            except discord.NotFound:
                pass
            except discord.HTTPException:
                continue

            await self.bot.tag_db.execute("DELETE FROM help_threads WHERE thread_id = ?", (thread_id,))
            await self.bot.tag_db.commit()

            if staff_log:
                await staff_log.send(
                    f"🗑️ **Auto-Cleanup**: Permanently deleted thread `{thread_name}` (closed 7+ days ago)."
                )

    @cleanup_old_threads.before_loop
    async def before_cleanup(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or message.channel.id != self.HELP_CHANNEL_ID:
            return

        content = message.content
        kdan_matches = list(self.kdan.finditer(content))
        gnome_matches = list(self.gnomebot.finditer(content))
        mclogs_matches = list(self.mclogs.finditer(content))
        attachments = message.attachments
        is_admin = message.author.guild_permissions.manage_messages

        if not kdan_matches and not gnome_matches and not mclogs_matches and not attachments:
            return

        bucket = self.cooldowns.get_bucket(message)
        if bucket.update_rate_limit() and not is_admin:
            return

        log_list = []
        clean_description = content

        for match in kdan_matches:
            fname = match.group('filename')
            log_id = match.group('id')
            clean_url = f"https://p.kdan.dev/{log_id}"
            log_list.append(f"🔗 **{fname}**: [View Log]({clean_url})")
            clean_description = clean_description.replace(match.group(0), "")

        for match in gnome_matches:
            fname = match.group('filename')
            log_id = match.group('id')
            if any(log_id in entry for entry in log_list):
                continue
            clean_url = f"https://p.kdan.dev/{log_id}"
            log_list.append(f"🔗 **{fname}**: [View Log]({clean_url})")
            clean_description = clean_description.replace(match.group(0), "")

        for match in mclogs_matches:
            fname = match.group('filename')
            log_id = match.group('id')
            if any(log_id in entry for entry in log_list):
                continue
            clean_url = f"https://p.kdan.dev/{log_id}"
            log_list.append(f"🔗 **{fname}**: [View Log]({clean_url})")
            clean_description = clean_description.replace(match.group(0), "")

        # Remove Crash Assistant boilerplate text
        if "The logs have been uploaded" in clean_description:
            clean_description = clean_description.split("The logs have been uploaded")[0]

        # Build Embed
        embed = discord.Embed(
            description=clean_description.strip() or "*User provided logs.*",
            color=discord.Color.brand_green()
        )
        embed.set_author(name=f"Support Request: {message.author.display_name}",
                         icon_url=message.author.display_avatar.url)

        if log_list:
            embed.add_field(name="📑 Identified Logs", value="\n".join(log_list), inline=False)
        if attachments:
            files_val = "\n".join([f"📁 `{a.filename}`" for a in attachments])
            embed.add_field(name="📎 Attached Files", value=files_val, inline=False)

        try:
            master_msg = await message.channel.send(
                content=f"🛠️ {message.author.mention}, a support thread has been opened.",
                embed=embed
            )
            thread = await master_msg.create_thread(name=f"❓｜{message.author.display_name}")

            await self.bot.tag_db.execute(
                "INSERT OR REPLACE INTO help_threads (thread_id, requester_id, closed, closed_at) "
                "VALUES (?, ?, 0, NULL)",
                (thread.id, message.author.id)
            )
            await self.bot.tag_db.commit()

            if attachments:
                files_to_send = [await a.to_file() for a in attachments]
                await thread.send("**Original attachments:**", files=files_to_send)

            await thread.send(
                f"Discussion for {message.author.mention}. Use `.close` once resolved "
                f"(staff can use `/forceclose`)."
            )
            await message.delete()
        except discord.HTTPException as e:
            print(f"Error: {e}")

    # ---------- closing / reopening threads ----------
    # pretty sure theres a better way to do this too, eh

    def _resolve_thread(self, channel):
        if not isinstance(channel, discord.Thread) or channel.parent_id != self.HELP_CHANNEL_ID:
            return None
        return channel

    async def _get_thread_row(self, thread):
        async with self.bot.tag_db.execute(
            "SELECT requester_id, closed FROM help_threads WHERE thread_id = ?", (thread.id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return thread.owner_id, 0
        return row

    @commands.Cog.listener(name="on_message")
    async def enforce_closed_thread(self, message):
        if message.author.bot:
            return

        thread = self._resolve_thread(message.channel)
        if thread is None:
            return

        requester_id, closed = await self._get_thread_row(thread)
        if not closed:
            return

        is_requester = message.author.id == requester_id
        is_staff = isinstance(message.author, discord.Member) and message.author.guild_permissions.manage_messages
        if is_requester or is_staff:
            return

        try:
            await message.delete()
        except discord.HTTPException:
            pass

        try:
            await thread.send(
                f"❌ {message.author.mention} This thread is closed, if you want to reopen run `.reopen` instead.",
                delete_after=10
            )
        except discord.HTTPException:
            pass

    async def _finish_close(self, thread, respond):
        closed_at = discord.utils.utcnow().isoformat()
        await self.bot.tag_db.execute(
            "UPDATE help_threads SET closed = 1, closed_at = ? WHERE thread_id = ?",
            (closed_at, thread.id)
        )
        await self.bot.tag_db.commit()

        new_name = thread.name.replace("❓", "✅") if "❓" in thread.name else thread.name
        await respond(
            "✅ **Issue marked resolved.** Only the original poster and staff can post here now — "
            "run `.reopen` to open it back up. This thread will be permanently deleted in 7 days."
        )
        try:
            await thread.edit(name=new_name, archived=True, locked=False)
        except discord.HTTPException:
            pass

    @commands.command(name="close")
    async def close_thread(self, ctx):
        thread = self._resolve_thread(ctx.channel)
        if thread is None:
            return

        requester_id, _ = await self._get_thread_row(thread)
        if ctx.author.id != requester_id:
            await ctx.send(
                "❌ Only the person who opened this thread can `.close` it. "
                "Staff should use `/forceclose` instead."
            )
            return

        await self._finish_close(thread, ctx.send)

    @discord.slash_command(name="forceclose", description="Force-close any support thread (staff only)")
    async def forceclose_thread(self, ctx: discord.ApplicationContext):
        thread = self._resolve_thread(ctx.channel)
        if thread is None:
            await ctx.respond("❌ This command can only be used inside a support thread.", ephemeral=True)
            return

        if not ctx.author.guild_permissions.manage_messages:
            await ctx.respond("❌ You don't have permission to use this command.", ephemeral=True)
            return

        await ctx.defer()
        await self._finish_close(thread, ctx.respond)

    @commands.command(name="reopen")
    async def reopen_thread(self, ctx):
        thread = self._resolve_thread(ctx.channel)
        if thread is None:
            return

        requester_id, closed = await self._get_thread_row(thread)
        is_staff = ctx.author.guild_permissions.manage_messages
        if ctx.author.id != requester_id and not is_staff:
            await ctx.send("❌ Only the person who opened this thread (or staff) can reopen it.")
            return

        if not closed:
            await ctx.send("This thread isn't closed.")
            return

        await self.bot.tag_db.execute(
            "UPDATE help_threads SET closed = 0, closed_at = NULL WHERE thread_id = ?", (thread.id,)
        )
        await self.bot.tag_db.commit()

        new_name = thread.name.replace("✅", "❓") if "✅" in thread.name else thread.name
        try:
            await thread.edit(name=new_name, archived=False, locked=False)
        except discord.HTTPException:
            pass

        await ctx.send("🔓 **This thread has been reopened.** Anyone can message here again.")

# LEAVE THIS NON-ASYNC! IT CRASHES!!!
def setup(bot):
    bot.add_cog(CommunityHelp(bot))