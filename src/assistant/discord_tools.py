"""In-process MCP server nethor_discord: channel create/rename/delete here, read tools in discord_browse, files in
discord_files.

Create, rename and delete are approval-gated (ASK tier). Rename and delete items carry the channel's current name
next to its id so the approval prompt is readable, and an item whose name doesn't match the real channel is skipped.
"""

from __future__ import annotations

import asyncio
import re
import string
from typing import Any

import discord
from claude_agent_sdk import create_sdk_mcp_server, tool

from assistant import discord_browse, discord_files
from assistant.discord_common import DiscordContext, lines_result, log, resolve_channel, text_result

SERVER_NAME = "nethor_discord"
MAX_CHANNELS = 25
MAX_NAME_LENGTH = 100
MAX_TOPIC_LENGTH = 1024  # Discord's limit for a text channel topic
EDIT_TIMEOUT_SECONDS = 30
REASON = "By the assistant (approved by the owner)"
_SEPARATORS = re.compile(r"[\s" + re.escape(string.punctuation.replace("-", "").replace("_", "")) + r"]+")

_ID = {"type": "string", "description": "Numeric Discord channel id."}
_CURRENT = {"type": "string", "description": "The channel's current name, exactly as Discord shows it."}
_CREATE_SCHEMA = {
    "type": "object",
    "properties": {
        "category_id": {"type": "string", "description": "Id of the Discord category to create the channels in."},
        "names": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": MAX_CHANNELS,
                  "description": "Channel names, one text channel is created per name. Emoji are kept."},
    },
    "required": ["category_id", "names"],
}
_RENAME_SCHEMA = {
    "type": "object",
    "properties": {"renames": {"type": "array", "minItems": 1, "maxItems": MAX_CHANNELS, "items": {
        "type": "object",
        "properties": {"channel_id": _ID, "current_name": _CURRENT,
                       "new_name": {"type": "string", "description": "New name. Emoji are kept."}},
        "required": ["channel_id", "current_name", "new_name"],
    }}},
    "required": ["renames"],
}
_TOPIC_SCHEMA = {
    "type": "object",
    "properties": {"topics": {"type": "array", "minItems": 1, "maxItems": MAX_CHANNELS, "items": {
        "type": "object",
        "properties": {"channel_id": _ID, "current_name": _CURRENT,
                       "topic": {"type": "string", "maxLength": MAX_TOPIC_LENGTH,
                                 "description": "New channel topic (description). Empty string clears it."}},
        "required": ["channel_id", "current_name", "topic"],
    }}},
    "required": ["topics"],
}
_DELETE_SCHEMA = {
    "type": "object",
    "properties": {"channels": {"type": "array", "minItems": 1, "maxItems": MAX_CHANNELS, "items": {
        "type": "object",
        "properties": {"channel_id": _ID, "name": _CURRENT},
        "required": ["channel_id", "name"],
    }}},
    "required": ["channels"],
}
_LIST_SCHEMA = {
    "type": "object",
    "properties": {"guild_id": {"type": "string", "description": "Numeric server id; omit for every allowed server."}},
}
_ARCHIVED_SCHEMA = {
    "type": "object",
    "properties": {"channel_id": _ID, "limit": {"type": "integer", "minimum": 1,
                                               "maximum": discord_browse.ARCHIVED_MAX}},
    "required": ["channel_id"],
}
_MESSAGE_ID = {"type": "string", "description": "Numeric Discord message id."}
_READ_SCHEMA = {
    "type": "object",
    "properties": {
        "channel_id": _ID,
        "limit": {"type": "integer", "minimum": 1, "maximum": discord_browse.HISTORY_MAX,
                  "description": f"Messages to read (default {discord_browse.HISTORY_DEFAULT})."},
        "before": {**_MESSAGE_ID, "description": "Only messages older than this message id."},
        "after": {**_MESSAGE_ID, "description": "Only messages newer than this message id."},
    },
    "required": ["channel_id"],
}
_MESSAGE_SCHEMA = {
    "type": "object",
    "properties": {"channel_id": _ID, "message_id": _MESSAGE_ID},
    "required": ["channel_id", "message_id"],
}
_REACT_SCHEMA = {
    "type": "object",
    "properties": {
        "channel_id": _ID, "message_id": _MESSAGE_ID,
        "emojis": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": discord_browse.EMOJI_MAX},
        "remove": {"type": "boolean", "description": "true = remove your own reactions instead of adding."},
    },
    "required": ["channel_id", "message_id", "emojis"],
}
_FILE_SCHEMA = {
    "type": "object",
    "properties": {
        "filename": {"type": "string", "description": "File name with extension, e.g. notes.md."},
        "content": {"type": "string", "description": "The file's text. Give this or url, not both."},
        "url": {"type": "string", "description": "Public https link to download and attach. Give this or content."},
        "message": {"type": "string", "description": "Optional text posted with the file."},
    },
    "required": ["filename"],
}


def normalize_name(name: str) -> str:
    """Discord text-channel style: lowercase, spaces and ASCII punctuation become single hyphens, emoji are kept."""
    slug = _SEPARATORS.sub("-", name.strip().lower())
    return re.sub(r"-{2,}", "-", slug).strip("-")[:MAX_NAME_LENGTH]


def unique_names(names: list[str]) -> list[str]:
    """Normalized names in order, with empties and duplicates dropped."""
    seen: dict[str, None] = {}
    for name in names:
        slug = normalize_name(name)
        if slug:
            seen.setdefault(slug)
    return list(seen)


def validate_request(args: dict[str, Any]) -> tuple[int, list[str]] | str:
    """create_channels: (category_id, channel names), or a short message saying what is wrong."""
    raw_id = str(args.get("category_id", "")).strip()
    if not raw_id.isdigit():
        return "category_id must be a numeric Discord id."
    raw_names = args.get("names")
    if not isinstance(raw_names, list) or not all(isinstance(n, str) for n in raw_names):
        return "names must be a list of strings."
    names = unique_names(raw_names)
    if not names:
        return "No usable channel names."
    if len(names) > MAX_CHANNELS:
        return f"Too many channels: at most {MAX_CHANNELS} per call."
    return int(raw_id), names


def parse_items(args: dict[str, Any], key: str, fields: tuple[str, ...]) -> list[tuple[int, ...]] | str:
    """A list of {channel_id, *fields} objects as (id, *values) tuples, ids unique, or what is wrong."""
    items = args.get(key)
    if not isinstance(items, list) or not items:
        return f"{key} must be a non-empty list."
    if len(items) > MAX_CHANNELS:
        return f"Too many channels: at most {MAX_CHANNELS} per call."
    parsed: list[tuple[Any, ...]] = []
    for item in items:
        if not isinstance(item, dict) or not all(isinstance(item.get(f), str) for f in ("channel_id", *fields)):
            return f"Each {key} item needs string fields channel_id, {', '.join(fields)}."
        raw_id = item["channel_id"].strip()
        if not raw_id.isdigit():
            return f"channel_id {raw_id!r} is not a numeric Discord id."
        parsed.append((int(raw_id), *(item[f] for f in fields)))
    if len({p[0] for p in parsed}) != len(parsed):
        return "The same channel_id appears twice."
    return parsed


def validate_renames(args: dict[str, Any]) -> list[tuple[int, str, str]] | str:
    """rename_channels: (channel_id, current_name, normalized new name) per item, or what is wrong."""
    parsed = parse_items(args, "renames", ("current_name", "new_name"))
    if isinstance(parsed, str):
        return parsed
    renames = [(cid, current, normalize_name(new)) for cid, current, new in parsed]
    empty = [cid for cid, _, new in renames if not new]
    return f"New name for {empty[0]} is empty after cleaning." if empty else renames


def same_name(given: str, actual: str) -> bool:
    return given.strip().lstrip("#").casefold() == actual.casefold()


def _find_category(client: discord.Client, category_id: int,
                   allowed_guilds: frozenset[int]) -> discord.CategoryChannel | str:
    category = client.get_channel(category_id)
    if not isinstance(category, discord.CategoryChannel):
        return f"{category_id} is not a category the bot can see."
    if category.guild.id not in allowed_guilds:
        return "That category is in a server that is not on the allow list."
    return category


async def _find_text_channel(client: discord.Client, channel_id: int, expected: str,
                             allowed_guilds: frozenset[int]) -> discord.TextChannel | str:
    channel = await resolve_channel(client, channel_id, allowed_guilds)
    if isinstance(channel, str):
        return channel
    if not isinstance(channel, discord.TextChannel):
        return f"{channel_id} is not a text channel."
    if not same_name(expected, channel.name):
        return f"{channel_id} is #{channel.name}, not {expected!r}: skipped."
    return channel


async def _create_one(category: discord.CategoryChannel, name: str) -> str:
    try:
        channel = await category.create_text_channel(name, reason=REASON)
    except discord.DiscordException as exc:
        log.exception("create_text_channel failed: name=%s category=%s", name, category.id)
        return f"⚠️ {name}: {exc}"
    log.info("created channel name=%s id=%s category=%s", channel.name, channel.id, category.id)
    return f"✅ #{channel.name} (id {channel.id})"


async def _rename_one(channel: discord.TextChannel, new_name: str) -> str:
    old = channel.name
    try:
        # discord.py waits out a 429; renames are limited to 2 per channel per 10 min, so don't wait that long.
        await asyncio.wait_for(channel.edit(name=new_name, reason=REASON), EDIT_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        log.warning("rename timed out (rate limit?): id=%s old=%s new=%s", channel.id, old, new_name)
        return f"⚠️ #{old}: Discord is rate-limiting renames of this channel (2 per 10 min), try again later."
    except discord.DiscordException as exc:
        log.exception("rename failed: id=%s old=%s new=%s", channel.id, old, new_name)
        return f"⚠️ #{old}: {exc}"
    log.info("renamed channel id=%s old=%s new=%s", channel.id, old, new_name)
    return f"✅ #{old} → #{new_name}"


async def _set_topic_one(channel: discord.TextChannel, topic: str) -> str:
    try:
        # Topic edits share the channel-edit rate limit with renames; same bounded wait as _rename_one.
        await asyncio.wait_for(channel.edit(topic=topic, reason=REASON), EDIT_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        log.warning("topic edit timed out (rate limit?): id=%s name=%s", channel.id, channel.name)
        return f"⚠️ #{channel.name}: Discord is rate-limiting edits of this channel, try again in ~10 min."
    except discord.DiscordException as exc:
        log.exception("topic edit failed: id=%s name=%s", channel.id, channel.name)
        return f"⚠️ #{channel.name}: {exc}"
    log.info("set channel topic id=%s name=%s chars=%d", channel.id, channel.name, len(topic))
    return f"✅ #{channel.name}: topic {'set' if topic else 'cleared'}"


def validate_topics(args: dict[str, Any]) -> list[tuple[int, str, str]] | str:
    """set_channel_topics: (channel_id, current_name, topic) per item; an empty topic clears it."""
    parsed = parse_items(args, "topics", ("current_name", "topic"))
    if isinstance(parsed, str):
        return parsed
    topics = [(cid, current, topic.strip()) for cid, current, topic in parsed]
    too_long = [cid for cid, _, topic in topics if len(topic) > MAX_TOPIC_LENGTH]
    return f"Topic for {too_long[0]} is over {MAX_TOPIC_LENGTH} characters." if too_long else topics


async def run_set_channel_topics(client: discord.Client, allowed_guilds: frozenset[int],
                                 args: dict[str, Any]) -> dict[str, Any]:
    checked = validate_topics(args)
    if isinstance(checked, str):
        return text_result(f"⚠️ {checked}", error=True)
    lines = []
    for channel_id, current, topic in checked:
        channel = await _find_text_channel(client, channel_id, current, allowed_guilds)
        lines.append(f"⚠️ {channel}" if isinstance(channel, str) else await _set_topic_one(channel, topic))
    return lines_result(lines)


async def _delete_one(channel: discord.TextChannel) -> str:
    try:
        await channel.delete(reason=REASON)
    except discord.DiscordException as exc:
        log.exception("delete failed: id=%s name=%s", channel.id, channel.name)
        return f"⚠️ #{channel.name}: {exc}"
    log.info("deleted channel id=%s name=%s guild=%s", channel.id, channel.name, channel.guild.id)
    return f"🗑️ #{channel.name} deleted"


async def run_create_channels(client: discord.Client, allowed_guilds: frozenset[int],
                              args: dict[str, Any]) -> dict[str, Any]:
    checked = validate_request(args)
    if isinstance(checked, str):
        return text_result(f"⚠️ {checked}", error=True)
    category_id, names = checked
    category = _find_category(client, category_id, allowed_guilds)
    if isinstance(category, str):
        return text_result(f"⚠️ {category}", error=True)
    return lines_result([await _create_one(category, name) for name in names])


async def run_rename_channels(client: discord.Client, allowed_guilds: frozenset[int],
                              args: dict[str, Any]) -> dict[str, Any]:
    checked = validate_renames(args)
    if isinstance(checked, str):
        return text_result(f"⚠️ {checked}", error=True)
    lines = []
    for channel_id, current, new_name in checked:
        channel = await _find_text_channel(client, channel_id, current, allowed_guilds)
        lines.append(f"⚠️ {channel}" if isinstance(channel, str) else await _rename_one(channel, new_name))
    return lines_result(lines)


async def run_delete_channels(client: discord.Client, allowed_guilds: frozenset[int],
                              args: dict[str, Any]) -> dict[str, Any]:
    checked = parse_items(args, "channels", ("name",))
    if isinstance(checked, str):
        return text_result(f"⚠️ {checked}", error=True)
    lines = []
    for channel_id, name in checked:
        channel = await _find_text_channel(client, channel_id, name, allowed_guilds)
        lines.append(f"⚠️ {channel}" if isinstance(channel, str) else await _delete_one(channel))
    return lines_result(lines)


def _channel_tools(ctx: DiscordContext) -> list[Any]:
    @tool("create_channels", "Create one Discord text channel per name inside a category. The owner is asked to "
          "approve first.", _CREATE_SCHEMA)
    async def create_channels(args: dict[str, Any]) -> dict[str, Any]:
        return await run_create_channels(ctx.get_client(), ctx.allowed_guilds, args)

    @tool("rename_channels", "Rename Discord text channels. Give each channel's id and its current name; an item "
          "whose current name doesn't match is skipped. The owner is asked to approve first.", _RENAME_SCHEMA)
    async def rename_channels(args: dict[str, Any]) -> dict[str, Any]:
        return await run_rename_channels(ctx.get_client(), ctx.allowed_guilds, args)

    @tool("set_channel_topics", "Set or clear the topic (description) of Discord text channels. Give each channel's "
          "id and its current name; an item whose current name doesn't match is skipped. The owner is asked to "
          "approve first.", _TOPIC_SCHEMA)
    async def set_channel_topics(args: dict[str, Any]) -> dict[str, Any]:
        return await run_set_channel_topics(ctx.get_client(), ctx.allowed_guilds, args)

    @tool("delete_channels", "Permanently delete Discord text channels and their message history. Give each "
          "channel's id and its current name; an item whose name doesn't match is skipped. The owner is asked to "
          "approve first.", _DELETE_SCHEMA)
    async def delete_channels(args: dict[str, Any]) -> dict[str, Any]:
        return await run_delete_channels(ctx.get_client(), ctx.allowed_guilds, args)

    return [create_channels, rename_channels, set_channel_topics, delete_channels]


def _read_tools(ctx: DiscordContext) -> list[Any]:
    @tool("list_channels", "List the categories, channels and active threads (with ids) of the allowed Discord "
          "servers, or of one server.", _LIST_SCHEMA)
    async def list_channels(args: dict[str, Any]) -> dict[str, Any]:
        return await discord_browse.run_list_channels(ctx, args)

    @tool("list_archived_threads", "List the archived public threads (or forum posts) of a text or forum channel, "
          "newest first.", _ARCHIVED_SCHEMA)
    async def list_archived_threads(args: dict[str, Any]) -> dict[str, Any]:
        return await discord_browse.run_list_archived_threads(ctx, args)

    @tool("read_messages", "Read recent messages of a Discord channel, thread or forum post (oldest first, with "
          "message ids, attachments and reactions). Page with before/after message ids.", _READ_SCHEMA)
    async def read_messages(args: dict[str, Any]) -> dict[str, Any]:
        return await discord_browse.run_read_messages(ctx, args)

    @tool("get_reactions", "Show the reactions on one message: each emoji, its count and who reacted.",
          _MESSAGE_SCHEMA)
    async def get_reactions(args: dict[str, Any]) -> dict[str, Any]:
        return await discord_browse.run_get_reactions(ctx, args)

    return [list_channels, list_archived_threads, read_messages, get_reactions]


def _write_tools(ctx: DiscordContext) -> list[Any]:
    @tool("react", "Add your reactions to a message, or remove your own (remove: true). Unicode emoji or "
          "<:name:id> for a server emoji.", _REACT_SCHEMA)
    async def react(args: dict[str, Any]) -> dict[str, Any]:
        return await discord_browse.run_react(ctx, args)

    @tool("send_file", "Attach a file to your reply in this conversation: either text you write (content, e.g. a "
          "CSV or Markdown export) or a file from a public https link (url, e.g. an image). Max 8 MB.", _FILE_SCHEMA)
    async def send_file(args: dict[str, Any]) -> dict[str, Any]:
        return await discord_files.run_send_file(ctx, args)

    return [react, send_file]


def build_discord_server(ctx: DiscordContext) -> Any:
    """A fresh server per agent client: an SDK server instance is bound to one connection (and one conversation)."""
    tools = _channel_tools(ctx) + _read_tools(ctx) + _write_tools(ctx)
    return create_sdk_mcp_server(SERVER_NAME, tools=tools)
