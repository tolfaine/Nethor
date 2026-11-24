"""Read-side Discord tools: list channels and threads, read history, read and add reactions."""

from __future__ import annotations

from collections import defaultdict
from typing import Any
from zoneinfo import ZoneInfo

import discord

from assistant.discord_common import (
    UNTRUSTED, DiscordContext, error_result, lines_result, log, parse_id, parse_limit, resolve_channel, text_result,
)

HISTORY_DEFAULT = 30
HISTORY_MAX = 100
ARCHIVED_DEFAULT = 25
ARCHIVED_MAX = 50
MESSAGE_CHARS = 1000
TOPIC_CHARS = 120
RESULT_CHARS = 25_000
REACTORS = 20
EMOJI_MAX = 10


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def join_clipped(header: str, lines: list[str], limit: int = RESULT_CHARS) -> str:
    """header + lines, dropping whole lines past limit and saying how many were cut."""
    kept, size = [header], len(header)
    for index, line in enumerate(lines):
        if size + len(line) + 1 > limit:
            kept.append(f"… {len(lines) - index} more lines cut: ask for fewer, or page with before/after.")
            break
        kept.append(line)
        size += len(line) + 1
    return "\n".join(kept)


def format_message(message: Any, tz: ZoneInfo) -> str:
    when = message.created_at.astimezone(tz)
    text = clip(message.content.replace("\n", " ↵ "), MESSAGE_CHARS)
    parts = [f"[{when:%Y-%m-%d %H:%M} · {message.id}] {message.author.display_name}: {text}"]
    reference = getattr(message, "reference", None)
    if reference is not None and reference.message_id:
        parts.append(f"↪ reply to {reference.message_id}")
    if message.attachments:
        parts.append(f"📎 {len(message.attachments)} file(s): {', '.join(a.filename for a in message.attachments)}")
    if message.embeds and not message.content:
        parts.append(f"[{len(message.embeds)} embed(s)]")
    if message.reactions:
        parts.append("reactions: " + " ".join(f"{r.emoji} {r.count}" for r in message.reactions))
    return "  ".join(parts)


def _visible(channel: Any, me: Any) -> bool:
    try:
        return bool(channel.permissions_for(me).view_channel)
    except Exception:  # noqa: BLE001 - a channel type without permissions; show it rather than hide it
        return True


def format_channel_tree(guild: Any, threads: list[Any]) -> list[str]:
    """One server: categories in Discord order, their channels, active threads under each parent."""
    by_parent: dict[int, list[Any]] = defaultdict(list)
    for thread in threads:
        by_parent[thread.parent_id].append(thread)
    lines = [f"Server {guild.name} (id {guild.id})"]
    for category, channels in guild.by_category():
        if category is not None:
            lines.append(f"  category {category.name} (id {category.id})")
        indent = "    " if category is not None else "  "
        for channel in channels:
            hidden = "" if _visible(channel, guild.me) else " [bot can't view]"
            topic = getattr(channel, "topic", None)
            about = f" — topic: {clip(topic.replace(chr(10), ' '), TOPIC_CHARS)}" if topic else ""
            lines.append(f"{indent}{channel.type.name} #{channel.name} (id {channel.id}){hidden}{about}")
            lines.extend(f"{indent}  thread {t.name} (id {t.id})" for t in by_parent.get(channel.id, []))
    return lines


def _wanted_guilds(client: discord.Client, allowed: frozenset[int], args: dict[str, Any]) -> list[Any] | str:
    guilds = [g for g in client.guilds if g.id in allowed]
    if args.get("guild_id") in (None, ""):
        return guilds or "The bot isn't in any allowed server."
    guild_id = parse_id(args.get("guild_id"), "guild_id")
    if isinstance(guild_id, str):
        return guild_id
    chosen = [g for g in guilds if g.id == guild_id]
    return chosen or f"{guild_id} is not an allowed server the bot is in."


async def run_list_channels(ctx: DiscordContext, args: dict[str, Any]) -> dict[str, Any]:
    guilds = _wanted_guilds(ctx.get_client(), ctx.allowed_guilds, args)
    if isinstance(guilds, str):
        return error_result(guilds)
    lines: list[str] = []
    for guild in guilds:
        try:
            threads = await guild.active_threads()
        except discord.HTTPException as exc:
            log.exception("active_threads failed: guild=%s", guild.id)
            lines.append(f"⚠️ {guild.name}: couldn't list active threads ({exc})")
            threads = []
        lines.extend(format_channel_tree(guild, threads))
    log.info("list_channels guilds=%s", [g.id for g in guilds])
    return text_result(join_clipped(UNTRUSTED, lines))


async def _channel_arg(ctx: DiscordContext, args: dict[str, Any]) -> Any:
    channel_id = parse_id(args.get("channel_id"), "channel_id")
    if isinstance(channel_id, str):
        return channel_id
    return await resolve_channel(ctx.get_client(), channel_id, ctx.allowed_guilds)


async def run_list_archived_threads(ctx: DiscordContext, args: dict[str, Any]) -> dict[str, Any]:
    limit = parse_limit(args.get("limit"), ARCHIVED_DEFAULT, ARCHIVED_MAX)
    channel = await _channel_arg(ctx, args)
    for problem in (limit, channel):
        if isinstance(problem, str):
            return error_result(problem)
    if not hasattr(channel, "archived_threads"):
        return error_result(f"#{channel.name} can't have threads: give a text or forum channel.")
    try:
        threads = [t async for t in channel.archived_threads(limit=limit)]
    except discord.HTTPException as exc:
        log.exception("archived_threads failed: channel=%s", channel.id)
        return error_result(f"#{channel.name}: {exc}")
    lines = [f"thread {t.name} (id {t.id}) archived {t.archive_timestamp.astimezone(ctx.tz):%Y-%m-%d}" for t in threads]
    header = f"{UNTRUSTED}\n#{channel.name} (id {channel.id}): {len(threads)} archived threads, newest first"
    log.info("list_archived_threads channel=%s count=%d", channel.id, len(threads))
    return text_result(join_clipped(header, lines))


def _history_bounds(args: dict[str, Any]) -> dict[str, Any] | str:
    bounds: dict[str, Any] = {}
    for field in ("before", "after"):
        if args.get(field) in (None, ""):
            continue
        message_id = parse_id(args.get(field), field)
        if isinstance(message_id, str):
            return message_id
        bounds[field] = discord.Object(id=message_id)
    return bounds


async def run_read_messages(ctx: DiscordContext, args: dict[str, Any]) -> dict[str, Any]:
    limit = parse_limit(args.get("limit"), HISTORY_DEFAULT, HISTORY_MAX)
    bounds = _history_bounds(args)
    channel = await _channel_arg(ctx, args)
    for problem in (limit, bounds, channel):
        if isinstance(problem, str):
            return error_result(problem)
    if not hasattr(channel, "history"):
        return error_result(f"#{channel.name} has no messages of its own: read one of its threads or posts instead.")
    try:
        messages = [m async for m in channel.history(limit=limit, **bounds)]
    except discord.Forbidden:
        log.warning("read_messages forbidden: channel=%s", channel.id)
        return error_result(f"#{channel.name}: the bot lacks View Channel or Read Message History there.")
    except discord.HTTPException as exc:
        log.exception("history failed: channel=%s", channel.id)
        return error_result(f"#{channel.name}: {exc}")
    messages.sort(key=lambda m: m.id)
    header = (f"{UNTRUSTED}\n#{channel.name} (id {channel.id}) in {channel.guild.name}: "
              f"{len(messages)} messages, oldest first")
    log.info("read_messages channel=%s count=%d", channel.id, len(messages))
    return text_result(join_clipped(header, [format_message(m, ctx.tz) for m in messages]))


async def _message_arg(ctx: DiscordContext, args: dict[str, Any]) -> tuple[Any, int] | str:
    message_id = parse_id(args.get("message_id"), "message_id")
    channel = await _channel_arg(ctx, args)
    if isinstance(message_id, str) or isinstance(channel, str):
        return message_id if isinstance(message_id, str) else channel
    if not hasattr(channel, "fetch_message"):
        return f"#{channel.name} has no messages of its own."
    return channel, message_id


async def run_get_reactions(ctx: DiscordContext, args: dict[str, Any]) -> dict[str, Any]:
    target = await _message_arg(ctx, args)
    if isinstance(target, str):
        return error_result(target)
    channel, message_id = target
    try:
        message = await channel.fetch_message(message_id)
        lines = []
        for reaction in message.reactions:
            names = [user.display_name async for user in reaction.users(limit=REACTORS)]
            more = ", …" if reaction.count > len(names) else ""
            lines.append(f"{reaction.emoji} {reaction.count}: {', '.join(names)}{more}")
    except discord.NotFound:
        return error_result(f"No message {message_id} in #{channel.name}.")
    except discord.HTTPException as exc:
        log.exception("get_reactions failed: channel=%s message=%s", channel.id, message_id)
        return error_result(f"#{channel.name}: {exc}")
    header = f"{UNTRUSTED}\nReactions on {message_id} in #{channel.name}"
    return text_result(join_clipped(header, lines or ["(no reactions)"]))


def _emojis(args: dict[str, Any]) -> list[str] | str:
    emojis = args.get("emojis")
    if not isinstance(emojis, list) or not emojis or not all(isinstance(e, str) and e.strip() for e in emojis):
        return "emojis must be a non-empty list of emoji."
    if len(emojis) > EMOJI_MAX:
        return f"At most {EMOJI_MAX} emoji per call."
    return [e.strip() for e in emojis]


async def _react_one(message: Any, emoji: str, me: Any, remove: bool) -> str:
    try:
        if remove:
            await message.remove_reaction(emoji, me)
        else:
            await message.add_reaction(emoji)
    except discord.HTTPException as exc:
        log.exception("reaction failed: message=%s emoji=%s remove=%s", message.id, emoji, remove)
        return f"⚠️ {emoji}: {exc}"
    log.info("reaction %s: message=%s emoji=%s", "removed" if remove else "added", message.id, emoji)
    return f"✅ {emoji} {'removed' if remove else 'added'}"


async def run_react(ctx: DiscordContext, args: dict[str, Any]) -> dict[str, Any]:
    emojis = _emojis(args)
    target = await _message_arg(ctx, args)
    for problem in (emojis, target):
        if isinstance(problem, str):
            return error_result(problem)
    channel, message_id = target
    client = ctx.get_client()
    if client.user is None:
        return error_result("The bot isn't logged in yet.")
    message = channel.get_partial_message(message_id)
    remove = args.get("remove") is True
    return lines_result([await _react_one(message, emoji, client.user, remove) for emoji in emojis])
