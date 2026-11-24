"""In-process MCP tool that lets the agent create Discord text channels. Every call is approval-gated (ASK tier)."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

import discord
from claude_agent_sdk import create_sdk_mcp_server, tool

log = logging.getLogger("assistant.discord_tools")

SERVER_NAME = "nethor_discord"
MAX_CHANNELS = 25
MAX_NAME_LENGTH = 100

_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "category_id": {"type": "string", "description": "Id of the Discord category to create the channels in."},
        "names": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": MAX_CHANNELS,
            "description": "Channel names, one text channel is created per name.",
        },
    },
    "required": ["category_id", "names"],
}


def normalize_name(name: str) -> str:
    """Discord text-channel style: lowercase, words joined by single hyphens, at most 100 characters."""
    slug = re.sub(r"[^\w-]+", "-", name.strip().lower())
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
    """Return (category_id, channel names), or a short message saying what is wrong."""
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


def _text(message: str, *, error: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"content": [{"type": "text", "text": message}]}
    if error:
        result["is_error"] = True
    return result


async def _create_one(category: discord.CategoryChannel, name: str) -> str:
    try:
        channel = await category.create_text_channel(name, reason="Created by the assistant (approved by the owner)")
    except discord.DiscordException as exc:
        log.exception("create_text_channel failed: name=%s category=%s", name, category.id)
        return f"⚠️ {name}: {exc}"
    log.info("created channel name=%s id=%s category=%s", channel.name, channel.id, category.id)
    return f"✅ #{channel.name} (id {channel.id})"


def _find_category(client: discord.Client, category_id: int,
                   allowed_guilds: frozenset[int]) -> discord.CategoryChannel | str:
    category = client.get_channel(category_id)
    if not isinstance(category, discord.CategoryChannel):
        return f"{category_id} is not a category the bot can see."
    if category.guild.id not in allowed_guilds:
        return "That category is in a server that is not on the allow list."
    return category


async def run_create_channels(client: discord.Client, allowed_guilds: frozenset[int],
                              args: dict[str, Any]) -> dict[str, Any]:
    checked = validate_request(args)
    if isinstance(checked, str):
        return _text(f"⚠️ {checked}", error=True)
    category_id, names = checked
    category = _find_category(client, category_id, allowed_guilds)
    if isinstance(category, str):
        return _text(f"⚠️ {category}", error=True)
    lines = [await _create_one(category, name) for name in names]
    return _text("\n".join(lines), error=any(line.startswith("⚠️") for line in lines))


def build_discord_server(get_client: Callable[[], discord.Client], allowed_guilds: frozenset[int]) -> Any:
    """A fresh server per agent client: an SDK server instance is bound to one connection."""

    @tool("create_channels", "Create one Discord text channel per name inside a category. The owner is asked to "
          "approve first.", _INPUT_SCHEMA)
    async def create_channels(args: dict[str, Any]) -> dict[str, Any]:
        return await run_create_channels(get_client(), allowed_guilds, args)

    return create_sdk_mcp_server(SERVER_NAME, tools=[create_channels])
