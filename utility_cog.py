import discord
from discord.ext import commands


class Utility(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @staticmethod
    async def _get_first_message(channel):
        async for message in channel.history(limit=1, oldest_first=True):
            return message
        return None

    @discord.slash_command(name="firstmessage", description="Jump to the first message in this channel")
    async def firstmessage_slash(self, ctx):
        await ctx.defer()

        first = await self._get_first_message(ctx.channel)
        if first is None:
            await ctx.respond("❌ This channel has no messages.")
            return

        await ctx.respond(f"📍 First message in {ctx.channel.mention}: {first.jump_url}")

    @commands.command(name="firstmessage")
    async def firstmessage_prefix(self, ctx):
        first = await self._get_first_message(ctx.channel)
        if first is None:
            await ctx.send("❌ This channel has no messages.")
            return

        await ctx.send(f"📍 First message in {ctx.channel.mention}: {first.jump_url}")


def setup(bot):
    bot.add_cog(Utility(bot))