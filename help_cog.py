import discord
from discord.ext import commands, tasks
import re
from datetime import datetime, timezone, timedelta


class CommunityHelp(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Precise regex for Crash Assistant's Gnomebot format
        self.gnome_markdown_regex = re.compile(
            r"\[(?P<filename>.*?)\]\(<?https://gnomebot\.dev/paste/mclogs/(?P<id>[a-zA-Z0-9]+)>?\)")
        # Regex for raw mclo.gs links
        self.mclogs_pattern = re.compile(r"mclo\.gs/(?P<id>[a-zA-Z0-9]+)")

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
        """Deletes threads marked with ✅ that are older than 7 days."""
        channel = self.bot.get_channel(self.HELP_CHANNEL_ID)
        staff_log = self.bot.get_channel(self.STAFF_LOG_CHANNEL_ID)
        if not channel:
            return

        now = datetime.now(timezone.utc)
        threshold = timedelta(days=7)

        async for thread in channel.archived_threads(limit=None):
            if "✅" in thread.name:
                archive_at = thread.archive_timestamp

                if archive_at and (now - archive_at > threshold):
                    try:
                        thread_name = thread.name
                        await thread.delete()
                        if staff_log:
                            await staff_log.send(
                                f"🗑️ **Auto-Cleanup**: Deleted resolved thread `{thread_name}` (Closed 7+ days ago).")
                    except discord.HTTPException:
                        pass

    @cleanup_old_threads.before_loop
    async def before_cleanup(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or message.channel.id != self.HELP_CHANNEL_ID:
            return

        content = message.content
        gnome_matches = list(self.gnome_markdown_regex.finditer(content))
        mclogs_matches = list(self.mclogs_pattern.finditer(content))
        attachments = message.attachments
        is_admin = message.author.guild_permissions.manage_messages

        # Logic: If no logs/attachments, ignore (let admins chat, skip users)
        if not gnome_matches and not mclogs_matches and not attachments:
            return

        # Cooldown: Admins bypass
        bucket = self.cooldowns.get_bucket(message)
        if bucket.update_rate_limit() and not is_admin:
            return

        log_list = []
        clean_description = content

        # 1. Process Crash Assistant / Gnomebot links
        for match in gnome_matches:
            fname = match.group('filename')
            log_id = match.group('id')
            clean_url = f"https://gnomebot.dev/paste/mclogs/{log_id}"
            log_list.append(f"🔗 **{fname}**: [View Log]({clean_url})")
            clean_description = clean_description.replace(match.group(0), "")

        # 2. Process and Convert raw mclo.gs links
        for match in mclogs_matches:
            m_id = match.group('id')
            # Avoid double-processing if the ID was already found in a Gnome link
            if any(m_id in entry for entry in log_list):
                continue

            clean_url = f"https://gnomebot.dev/paste/mclogs/{m_id}"
            log_list.append(f"🔗 **Converted Log**: [View on Gnomebot]({clean_url})")
            clean_description = re.sub(rf"https?://mclo\.gs/{m_id}", "", clean_description)

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

            if attachments:
                files_to_send = [await a.to_file() for a in attachments]
                await thread.send("**Original attachments:**", files=files_to_send)

            await thread.send(f"Discussion for {message.author.mention}. Use `.close` once resolved.")
            await message.delete()
        except discord.HTTPException as e:
            print(f"Error: {e}")

    @commands.command(name="close")
    async def close_thread(self, ctx):
        if not isinstance(ctx.channel, discord.Thread) or ctx.channel.parent_id != self.HELP_CHANNEL_ID:
            return

        if ctx.author.guild_permissions.manage_messages or ctx.channel.owner_id == ctx.author.id:
            try:
                starter_message = await ctx.channel.parent.fetch_message(ctx.channel.id)
                await starter_message.delete()
            except discord.NotFound:
                pass
            except discord.Forbidden:
                print("Bot lacks permission to delete the embed message.")
            except discord.HTTPException as e:
                print(f"Failed to delete embed: {e}")

            new_name = ctx.channel.name.replace("❓", "✅")
            await ctx.send(
                "✅ **Issue resolved. The main embed has been removed. This thread will be deleted in 7 days.**")
            await ctx.channel.edit(name=new_name, archived=True, locked=True)

# LEAVE THIS NON-ASYNC! IT CRASHES!!!
def setup(bot):
    bot.add_cog(CommunityHelp(bot))