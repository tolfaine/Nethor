"""isinstance-compatible stand-ins for the discord.py objects the nethor_discord tools touch."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import discord

GUILD_ID = 111
BOT_USER = SimpleNamespace(id=999, display_name="Nethor")


def http_error(cls: type[discord.HTTPException] = discord.Forbidden, status: int = 403,
               text: str = "Missing Permissions") -> discord.HTTPException:
    return cls(SimpleNamespace(status=status, reason=text), text)


def message(message_id: int, content: str = "hi", author: str = "Alex", *, minute: int = 0, reply_to: int | None = None,
            files: tuple[str, ...] = (), reactions: tuple[tuple[str, int], ...] = ()) -> Any:
    return SimpleNamespace(
        id=message_id, content=content, author=SimpleNamespace(display_name=author),
        created_at=datetime(2026, 11, 24, 22, minute, tzinfo=timezone.utc),
        reference=SimpleNamespace(message_id=reply_to) if reply_to else None,
        attachments=[SimpleNamespace(filename=f) for f in files], embeds=[],
        reactions=[FakeReaction(e, c) for e, c in reactions],
    )


class FakeReaction:
    def __init__(self, emoji: str, count: int, users: tuple[str, ...] = ()) -> None:
        self.emoji, self.count, self._users = emoji, count, users

    async def users(self, *, limit: int | None = None) -> AsyncIterator[Any]:
        for name in self._users[:limit]:
            yield SimpleNamespace(display_name=name)


class FakePartialMessage:
    def __init__(self, message_id: int, fail: set[str]) -> None:
        self.id, self._fail = message_id, fail
        self.added: list[str] = []
        self.removed: list[tuple[str, Any]] = []

    async def add_reaction(self, emoji: str) -> None:
        if emoji in self._fail:
            raise http_error(discord.HTTPException, 400, "Unknown Emoji")
        self.added.append(emoji)

    async def remove_reaction(self, emoji: str, member: Any) -> None:
        self.removed.append((emoji, member))


class FakeTextChannel(discord.TextChannel):
    """Records edit/delete calls; serves history, reactions and archived threads from plain lists."""

    def __init__(self, channel_id: int, name: str, guild_id: int = GUILD_ID, *, fail: bool = False,
                 hang: bool = False, messages: list[Any] | None = None, archived: list[Any] | None = None,
                 history_error: discord.HTTPException | None = None, reaction_fail: set[str] | None = None,
                 topic: str | None = None) -> None:
        self._id, self._name, self._topic = channel_id, name, topic
        self._guild = SimpleNamespace(id=guild_id, name="Home")
        self._fail, self._hang = fail, hang
        self.messages, self.archived = messages or [], archived or []
        self._history_error, self._reaction_fail = history_error, reaction_fail or set()
        self.deleted = False
        self.partials: dict[int, FakePartialMessage] = {}
        self.history_calls: list[dict[str, Any]] = []

    @property
    def id(self) -> int:  # type: ignore[override]
        return self._id

    @property
    def name(self) -> str:  # type: ignore[override]
        return self._name

    @property
    def guild(self) -> Any:  # type: ignore[override]
        return self._guild

    @property
    def type(self) -> discord.ChannelType:  # type: ignore[override]
        return discord.ChannelType.text

    def permissions_for(self, member: Any) -> Any:  # type: ignore[override]
        return SimpleNamespace(view_channel=True)

    @property
    def topic(self) -> str | None:  # type: ignore[override]
        return self._topic

    async def edit(self, *, name: str | None = None, **kwargs: Any) -> Any:  # type: ignore[override]
        if self._hang:
            await asyncio.sleep(10)
        if self._fail:
            raise http_error()
        if name is not None:
            self._name = name
        if "topic" in kwargs:
            self._topic = kwargs["topic"]
        return self

    async def delete(self, **kwargs: Any) -> None:  # type: ignore[override]
        if self._fail:
            raise http_error()
        self.deleted = True

    async def history(self, *, limit: int | None = 100, before: Any = None,  # type: ignore[override]
                      after: Any = None, **kwargs: Any) -> AsyncIterator[Any]:
        self.history_calls.append({"limit": limit, "before": before, "after": after})
        if self._history_error is not None:
            raise self._history_error
        picked = [m for m in self.messages if (before is None or m.id < before.id) and (after is None or m.id > after.id)]
        for item in sorted(picked, key=lambda m: m.id, reverse=True)[:limit]:
            yield item

    async def fetch_message(self, message_id: int, /) -> Any:  # type: ignore[override]
        for item in self.messages:
            if item.id == message_id:
                return item
        raise http_error(discord.NotFound, 404, "Unknown Message")

    def get_partial_message(self, message_id: int, /) -> Any:  # type: ignore[override]
        return self.partials.setdefault(message_id, FakePartialMessage(message_id, self._reaction_fail))

    async def archived_threads(self, *, limit: int | None = 100, **kwargs: Any) -> AsyncIterator[Any]:  # type: ignore[override]
        for thread in self.archived[:limit]:
            yield thread


class ChannelsClient:
    """get_channel from a dict; fetch_channel serves `fetchable` (uncached, e.g. archived threads) or 404s."""

    def __init__(self, *channels: Any, fetchable: tuple[Any, ...] = (), guilds: tuple[Any, ...] = ()) -> None:
        self._channels = {c.id: c for c in channels}
        self._fetchable = {c.id: c for c in fetchable}
        self.guilds = list(guilds)
        self.user = BOT_USER

    def get_channel(self, channel_id: int) -> Any:
        return self._channels.get(channel_id)

    async def fetch_channel(self, channel_id: int) -> Any:
        if channel_id in self._fetchable:
            return self._fetchable[channel_id]
        raise http_error(discord.NotFound, 404, "Unknown Channel")
