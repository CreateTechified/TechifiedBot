"""Shared logging helpers for the bot.

Two log channels:

  ADMIN_LOG_CHANNEL_ID - things admins/mods do: moderator actions, config changes, plus
                         message edits/deletes and members joining/leaving :)
  BOT_LOG_CHANNEL_ID   - things the bot does: help thread cleanup, tickets,
                         applications, tag reports, auto-timeouts, startup...
"""
import discord

ADMIN_LOG_CHANNEL_ID = 1561998095271858216
BOT_LOG_CHANNEL_ID = 1472650884906221771

NO_MENTIONS = discord.AllowedMentions.none()


def clip(text, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


_QUIET_LIMIT = 500
_quiet_ids = set()


def mark_quiet(message_id: int):
    if len(_quiet_ids) >= _QUIET_LIMIT:
        _quiet_ids.clear()
    _quiet_ids.add(message_id)


def consume_quiet(message_id: int) -> bool:
    if message_id in _quiet_ids:
        _quiet_ids.discard(message_id)
        return True
    return False


# ---------- sending ----------

async def _get_channel(client, channel_id: int):
    channel = client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except discord.HTTPException:
            return None
    return channel


async def send_log(client, channel_id: int, *args, **kwargs):
    channel = await _get_channel(client, channel_id)
    if channel is None:
        print(f"⚠️ Log channel {channel_id} wasn't found or isn't accessible.")
        return None

    kwargs.setdefault("allowed_mentions", NO_MENTIONS)
    try:
        return await channel.send(*args, **kwargs)
    except discord.HTTPException as e:
        print(f"⚠️ Couldn't send a log message to {channel_id}: {e}")
        return None


async def log_admin(client, *args, **kwargs):
    return await send_log(client, ADMIN_LOG_CHANNEL_ID, *args, **kwargs)


async def log_bot(client, *args, **kwargs):
    return await send_log(client, BOT_LOG_CHANNEL_ID, *args, **kwargs)


# ---------- ready-made embeds ----------

async def log_admin_action(client, title, color, moderator, target=None, reason=None, fields=None, channel=None):
    embed = discord.Embed(title=title, color=color, timestamp=discord.utils.utcnow())
    embed.add_field(name="Moderator", value=f"{moderator.mention} (`{moderator.id}`)", inline=False)

    if target is not None:
        target_id = getattr(target, "id", None)
        shown = getattr(target, "mention", None) or f"`{target}`"
        embed.add_field(
            name="Target",
            value=shown + (f" (`{target_id}`)" if target_id is not None else ""),
            inline=False
        )

    if reason:
        embed.add_field(name="Reason", value=clip(reason, 1024), inline=False)

    for name, value in (fields or {}).items():
        embed.add_field(name=name, value=clip(value, 1024), inline=False)

    channel_name = getattr(channel, "name", None)
    if channel_name:
        embed.set_footer(text=f"in #{channel_name}")

    return await log_admin(client, embed=embed)


async def log_bot_event(client, title, color, description=None, fields=None):
    embed = discord.Embed(
        title=title,
        description=clip(description, 4000) if description else None,
        color=color,
        timestamp=discord.utils.utcnow()
    )
    for name, value in (fields or {}).items():
        embed.add_field(name=name, value=clip(value, 1024), inline=False)

    return await log_bot(client, embed=embed)