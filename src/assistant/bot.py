"""Discord front end: routes DMs, forum posts and threads opened on @mentions; approvals, chunked replies."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

import discord

from discord import app_commands

from assistant.agent import ApprovalRequest
from assistant.commands import CommandHandler, install_commands, sync_commands
from assistant.config import Config
from assistant.keys import (
    DM, PRIVATE, PRIVATE_CHANNEL, PUBLIC, PUBLIC_THREAD, THREAD, THREAD_KINDS, Mode, key_id, key_kind, make_key,
)
from assistant.sessions import Origin, SessionManager, SessionRecord, run_maintenance_loop
from assistant.views import ApprovalView

log = logging.getLogger("assistant.bot")

DISCORD_LIMIT = 2000
FENCE = "```"
# Room for a reopened fence header plus the closing fence added at a chunk break.
_FENCE_RESERVE = 64

THREAD_TYPES = frozenset({discord.ChannelType.public_thread, discord.ChannelType.private_thread,
                          discord.ChannelType.news_thread})
# Message.create_thread only works on these.
THREADABLE_TYPES = frozenset({discord.ChannelType.text, discord.ChannelType.news})
# Channels whose `members` means "can view"; a voice channel's `members` means "connected", so it never counts.
VIEWER_LIST_TYPES = frozenset({discord.ChannelType.text, discord.ChannelType.news, discord.ChannelType.forum})
FOLLOW_UP = "Nethor thread follow-up"
# Ordinary channel traffic; logged at debug so the log isn't every message in the server.
QUIET_REASONS = frozenset({"bot message", "not addressed", f"{FOLLOW_UP} by non-allowlisted user"})

THREAD_ARCHIVE_MINUTES = 10080
THREAD_NAME_CHARS = 80
DEFAULT_THREAD_NAME = "Nethor"
_MENTION_TOKEN = re.compile(r"<(?:@[!&]?|#)\d+>")
THREAD_FORBIDDEN = "⚠️ I can't open a thread here: give me the **Create Public Threads** permission in this channel."
THREAD_FAILED = "⚠️ I couldn't open a thread here; details are in the log."
REROUTED_NOTE = "🔒 <#{channel_id}> is no longer private, so this came to your DM instead.\n"


def _is_fence(line: str) -> bool:
    return line.strip().startswith(FENCE)


def _split_long(line: str, size: int) -> list[str]:
    return [line[i:i + size] for i in range(0, len(line), size)] or [""]


def chunk_message(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    """Split into <= limit pieces on line breaks; a code fence cut by a break is closed and reopened."""
    if not text.strip():
        return []
    lines = [piece for line in text.split("\n") for piece in _split_long(line, limit - _FENCE_RESERVE)]
    chunks: list[str] = []
    current = ""
    fence: str | None = None
    for line in lines:
        after = (None if fence else line.strip()) if _is_fence(line) else fence
        candidate = f"{current}\n{line}" if current else line
        closer = f"\n{FENCE}" if after else ""
        if current and len(candidate) + len(closer) > limit:
            chunks.append(current + (f"\n{FENCE}" if fence else ""))
            candidate = f"{fence}\n{line}" if fence else line
        current = candidate
        fence = after
    if current.strip():
        chunks.append(current + (f"\n{FENCE}" if fence else ""))
    return [piece for chunk in chunks for piece in _split_long(chunk, limit)]


async def send_chunks(channel: Any, text: str, reference: discord.MessageReference | None = None) -> None:
    for index, chunk in enumerate(chunk_message(text) or ["(empty reply)"]):
        if index == 0 and reference is not None:
            await channel.send(chunk, reference=reference, mention_author=False)
        else:
            await channel.send(chunk)


@dataclass(frozen=True)
class Route:
    """`thread_name` set = open a thread on the message first; key and channel_id then name that thread."""

    key: str
    mode: Mode
    channel_id: int
    owner_id: int
    topic: str | None = None
    note: str = field(default="", compare=False)
    thread_name: str | None = None


def addressed_by(message: Any, bot_id: int) -> str | None:
    """How a guild message addresses Nethor: a user mention, its managed role, or a reply to its message."""
    if any(user.id == bot_id for user in message.mentions):
        return "mention"
    if any(role.tags is not None and role.tags.bot_id == bot_id for role in message.role_mentions):
        return "role mention"
    resolved = message.reference.resolved if message.reference else None
    if getattr(getattr(resolved, "author", None), "id", None) == bot_id:
        return "reply to Nethor"
    return None


def format_topic(title: str, tags: list[str]) -> str:
    tag_text = f" · tags: {', '.join(tags)}" if tags else ""
    return f'Conversation topic (this Discord forum post): "{title}"{tag_text}'


def _defang(text: str) -> str:
    # Text others wrote must not be able to forge a new line or a delimiter in the prompt.
    return text.replace("<<<", "‹‹‹").replace(">>>", "›››").replace("\r", "").replace("\n", " ↵ ")


def format_channel_topic(name: str, description: str | None) -> str:
    text = _defang(description.strip()) if description and description.strip() else "no description"
    return f"Conversation topic: #{_defang(name)} — {text}"


def channel_topic(channel: Any) -> str:
    """A thread is described by its parent channel."""
    parent = getattr(channel, "parent", None) if getattr(channel, "type", None) in THREAD_TYPES else None
    source = parent or channel
    return format_channel_topic(getattr(source, "name", None) or "unknown", getattr(source, "topic", None))


def thread_name(text: str, limit: int = THREAD_NAME_CHARS) -> str:
    name = " ".join(_MENTION_TOKEN.sub("", text).split())
    if len(name) > limit:
        name = name[:limit].rstrip() + "…"
    return name or DEFAULT_THREAD_NAME


def _forum_route(channel: Any, author_id: int) -> Route:
    tags = [tag.name for tag in getattr(channel, "applied_tags", [])]
    return Route(make_key(THREAD, channel.id), PRIVATE, channel.id, author_id, format_topic(channel.name, tags))


def _no_conversation(channel_id: int) -> bool:
    return False


def _conversation_route(thread_id: int, judged: Any, topic_source: Any, author_id: int, how: str, config: Config,
                        name: str | None = None) -> tuple[Route, str]:
    """`judged` decides private vs public: the thread itself, or the channel a new thread will open in."""
    privacy = channel_privacy(judged, config.allowed_user_ids)
    topic = channel_topic(topic_source)
    where = "thread" if name is None else "channel"
    if privacy.private:
        route = Route(make_key(THREAD, thread_id), PRIVATE, thread_id, author_id, topic, privacy.reason, name)
        return route, f"{how} in a private {where}"
    route = Route(make_key(PUBLIC_THREAD, thread_id), PUBLIC, thread_id, author_id, topic,
                  f"not private: {privacy.reason}", name)
    return route, how if name is not None else f"{how} in a public thread"


def _guild_route(message: Any, bot_id: int, config: Config, channel: Any,
                 has_conversation: Callable[[int], bool]) -> tuple[Route | None, str]:
    in_thread = channel.type in THREAD_TYPES
    how = addressed_by(message, bot_id) or (FOLLOW_UP if in_thread and has_conversation(channel.id) else None)
    if how is None:
        return None, "not addressed"
    if message.guild is None or message.guild.id not in config.allowed_guild_ids:
        return None, f"{how} in a server not on ALLOWED_GUILD_IDS"
    if message.author.id not in config.allowed_user_ids:
        return None, f"{how} by non-allowlisted user"
    if in_thread:
        return _conversation_route(channel.id, channel, channel, message.author.id, how, config)
    if channel.type not in THREADABLE_TYPES:
        return None, f"{how} in a {channel.type} channel, which can't have threads"
    name = thread_name(clean_text(message, bot_id))
    # A thread opened on a message gets that message's id.
    return _conversation_route(message.id, channel, channel, message.author.id, how, config, name)


def _direct_route(channel: Any, author_id: int, config: Config) -> tuple[Route | None, str] | None:
    """DMs and forum posts are conversations by themselves; None = neither."""
    allowed = author_id in config.allowed_user_ids
    if channel.type == discord.ChannelType.private:
        route = Route(make_key(DM, author_id), PRIVATE, channel.id, author_id)
        return (route, "dm") if allowed else (None, "DM from non-allowlisted user")
    if channel.type in THREAD_TYPES and getattr(channel, "parent_id", None) in config.forum_channel_ids:
        if not allowed:
            return None, "forum post by non-allowlisted user"
        return _forum_route(channel, author_id), "forum post"
    return None


def classify(message: Any, bot_user: Any, config: Config, channel: Any = None,
             has_conversation: Callable[[int], bool] = _no_conversation) -> tuple[Route | None, str]:
    """Pure routing decision plus its reason. `channel` overrides message.channel when it had to be fetched.

    `has_conversation(thread_id)`: the thread already is a conversation, so it needs no mention.
    """
    channel = channel or message.channel
    author = message.author
    if author.bot or author.id == bot_user.id:
        return None, "bot message"
    direct = _direct_route(channel, author.id, config)
    if direct is not None:
        return direct
    return _guild_route(message, bot_user.id, config, channel, has_conversation)


def conversation_key_for(channel: Any, user_id: int, guild_id: int | None, config: Config,
                         has_conversation: Callable[[int], bool]) -> tuple[Route | None, str]:
    """The conversation a slash command in `channel` acts on, by the same rules as a message there.

    Unlike a message, a command never starts a conversation in a channel or a thread Nethor isn't in yet.
    """
    if channel is None:
        return None, "command in an unknown channel"
    direct = _direct_route(channel, user_id, config)
    if direct is not None:
        return direct
    if user_id not in config.allowed_user_ids:
        return None, "command by non-allowlisted user"
    if getattr(channel, "type", None) not in THREAD_TYPES or not has_conversation(channel.id):
        return None, "command outside a conversation"
    if guild_id not in config.allowed_guild_ids:
        return None, "command in a server not on ALLOWED_GUILD_IDS"
    return _conversation_route(channel.id, channel, channel, user_id, "command", config)


def route(message: Any, bot_user: Any, config: Config, channel: Any = None,
          has_conversation: Callable[[int], bool] = _no_conversation) -> Route | None:
    return classify(message, bot_user, config, channel, has_conversation)[0]


def clean_text(message: Any, bot_id: int) -> str:
    """Strip Nethor's own mention (user or managed role) from the text it is asked."""
    role_ids = [role.id for role in message.role_mentions if role.tags is not None and role.tags.bot_id == bot_id]
    pattern = "|".join([rf"<@!?{bot_id}>"] + [rf"<@&{role_id}>" for role_id in role_ids])
    return re.sub(pattern, "", message.content).strip()


@dataclass(frozen=True)
class Destination:
    dm: bool
    target_id: int
    owner_id: int | None


def resolve_destination(key: str, record: SessionRecord) -> Destination:
    """Where replies and approvals for a key go: the owner's DM for `dm:`, the stored channel otherwise."""
    if key_kind(key) == DM:
        user_id = key_id(key) or record.owner_id
        if user_id is None:
            raise LookupError(f"no Discord user for conversation key {key!r}")
        return Destination(True, user_id, user_id)
    channel_id = record.channel_id or key_id(key)
    if channel_id is None:
        raise LookupError(f"no Discord channel for conversation key {key!r}")
    return Destination(False, channel_id, record.owner_id)


@dataclass(frozen=True)
class Privacy:
    private: bool
    reason: str


def _viewers_privacy(channel: Any, allowed: frozenset[int]) -> Privacy:
    ctype = getattr(channel, "type", None)
    if ctype not in VIEWER_LIST_TYPES:
        return Privacy(False, f"unsupported channel type {ctype}")
    guild = getattr(channel, "guild", None)
    if guild is None:
        return Privacy(False, "no guild")
    if channel.permissions_for(guild.default_role).view_channel:
        return Privacy(False, "@everyone can view")
    if not guild.chunked:
        return Privacy(False, "member list not loaded (guild not chunked; Server Members Intent off?)")
    members = getattr(channel, "members", None)
    humans = [m.id for m in members or () if not m.bot]
    if not humans:
        return Privacy(False, "no members known")
    others = sorted(uid for uid in humans if uid not in allowed)
    if others:
        return Privacy(False, f"{len(others)} non-allowlisted member(s) can view: {others[:5]}")
    return Privacy(True, f"hidden, viewers={sorted(humans)}")


def _thread_members_privacy(thread: Any, allowed: frozenset[int]) -> Privacy | None:
    """Private-thread members already seen (discord.py caches only some) must be allowlisted or bots."""
    for thread_member in getattr(thread, "members", None) or ():
        if thread_member.id in allowed:
            continue
        member = thread.guild.get_member(thread_member.id)
        if member is None:
            return Privacy(False, f"unknown thread member {thread_member.id}")
        if not member.bot:
            return Privacy(False, f"thread member {thread_member.id} not allowlisted")
    return None


def _privacy(channel: Any, allowed: frozenset[int]) -> Privacy:
    if getattr(channel, "type", None) not in THREAD_TYPES:
        return _viewers_privacy(channel, allowed)
    parent = getattr(channel, "parent", None)
    if parent is None:
        return Privacy(False, "thread parent unknown")
    if channel.type == discord.ChannelType.private_thread:
        blocked = _thread_members_privacy(channel, allowed)
        if blocked is not None:
            return blocked
    verdict = _viewers_privacy(parent, allowed)
    return replace(verdict, reason=f"parent {parent.id}: {verdict.reason}")


def channel_privacy(channel: Any, allowed: frozenset[int]) -> Privacy:
    """Private iff @everyone can't view it and every human who can is allowlisted. Fails closed."""
    try:
        return _privacy(channel, allowed)
    except Exception as exc:
        log.exception("privacy check failed channel=%s, treating it as not private", getattr(channel, "id", "?"))
        return Privacy(False, f"check failed: {type(exc).__name__}")


def is_private_channel(channel: Any, allowed: frozenset[int]) -> bool:
    return channel_privacy(channel, allowed).private


def guards_privacy(key: str, channel: Any, forum_ids: frozenset[int]) -> bool:
    """Private conversations outside the forums must still be private before anything is sent there.

    A channel without a known parent counts as outside the forums (fails closed to the owner's DM).
    """
    kind = key_kind(key)
    if kind == PRIVATE_CHANNEL:
        return True
    return kind == THREAD and getattr(channel, "parent_id", None) not in forum_ids


def rerouted_note(key: str, destination: Destination) -> str:
    """Non-empty when a private conversation's output was moved to the owner's DM."""
    if key_kind(key) in (PRIVATE_CHANNEL, THREAD) and destination.dm:
        return REROUTED_NOTE.format(channel_id=key_id(key))
    return ""


class DiscordApprover:
    def __init__(self, bot: AssistantBot) -> None:
        self._bot = bot

    async def approve(self, request: ApprovalRequest) -> bool:
        channel, destination = await self._bot.channel_for(request.user_key)
        if destination.owner_id is None:
            raise LookupError(f"no owner recorded for {request.user_key!r}, can't check who clicks")
        view = ApprovalView(destination.owner_id)
        summary = request.summary().replace(FENCE, "'''")
        note = rerouted_note(request.user_key, destination)
        message = await channel.send(f"{note}🔐 Approve this action? (10 min, then denied)\n{FENCE}\n{summary}\n{FENCE}",
                                     view=view)
        timed_out = await view.wait()
        if timed_out:
            await message.edit(content=f"{message.content}\n→ ⌛ timed out, denied", view=None)
            return False
        return bool(view.approved)


def _log_route(message: Any, route: Route | None, reason: str) -> None:
    where = f"author={message.author.id} channel={message.channel.id}"
    if route is not None:
        note = f" privacy={route.note}" if route.note else ""
        opens = " new_thread=yes" if route.thread_name is not None else ""
        log.info("route key=%s mode=%s reason=%s %s%s%s", route.key, route.mode, reason, where, opens, note)
    elif reason in QUIET_REASONS:
        log.debug("ignored reason=%s %s", reason, where)
    else:
        log.warning("ignored trigger reason=%s %s name=%s", reason, where, message.author)


async def _send_warning(channel: Any, text: str) -> None:
    try:
        await channel.send(text)
    except discord.HTTPException:
        log.exception("couldn't post a warning channel=%s text=%r", getattr(channel, "id", "?"), text)


class AssistantBot(discord.Client):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.dm_messages = True
        intents.members = True  # privileged: who can view a channel decides private vs public mode
        super().__init__(intents=intents)
        self.config = config
        self.manager: SessionManager | None = None
        self._maintenance: asyncio.Task[None] | None = None
        self._private_threads: set[int] = set()
        self._opened_warned: set[int] = set()
        self.tree: app_commands.CommandTree[AssistantBot] = app_commands.CommandTree(self)
        install_commands(self.tree, CommandHandler(self), config.allowed_guild_ids, config.model)
        self._sync: asyncio.Task[None] | None = None

    async def setup_hook(self) -> None:
        if self.manager is None:
            raise RuntimeError("AssistantBot.manager must be set before start()")
        self._maintenance = asyncio.create_task(run_maintenance_loop(self.manager, self.deliver))
        self._sync = asyncio.create_task(sync_commands(self.tree, self.config.allowed_guild_ids))

    async def on_ready(self) -> None:
        log.info("logged in as %s (id %s)", self.user, self.user.id if self.user else "?")

    async def _resolved_channel(self, message: discord.Message) -> Any:
        return await self._full_channel(message.channel, message.channel.id, message.author.id)

    async def _full_channel(self, channel: Any, channel_id: int | None, author_id: int) -> Any:
        # A thread reopened from the archive can arrive before discord.py has cached it.
        if (channel is None or isinstance(channel, discord.PartialMessageable)) and channel_id is not None:
            try:
                return await self.fetch_channel(channel_id)
            except discord.HTTPException:
                log.exception("fetch_channel failed channel=%s author=%s, routing without it", channel_id, author_id)
        return channel

    async def command_target(self, interaction: discord.Interaction) -> tuple[Any, Route | None, str]:
        """The (fetched) channel a slash command ran in, and the conversation it acts on."""
        channel = await self._full_channel(interaction.channel, interaction.channel_id, interaction.user.id)
        route, reason = conversation_key_for(channel, interaction.user.id, interaction.guild_id, self.config,
                                             self._has_conversation)
        return channel, route, reason

    def _has_conversation(self, channel_id: int) -> bool:
        assert self.manager is not None
        return any(self.manager.record(make_key(kind, channel_id)).channel_id is not None for kind in THREAD_KINDS)

    async def on_message(self, message: discord.Message) -> None:
        if self.user is None or message.author.bot:
            return
        channel = await self._resolved_channel(message)
        route, reason = classify(message, self.user, self.config, channel, self._has_conversation)
        _log_route(message, route, reason)
        if route is None:
            return
        text = clean_text(message, self.user.id)
        if not text:
            await message.channel.send("I can only read text messages for now.")
            return
        if route.thread_name is not None:
            started = await self._start_thread(message, route)
            if started is None:
                return
            route, channel = started
        self._track_privacy(route)
        await self._answer(route, text, channel)

    async def _start_thread(self, message: discord.Message, route: Route) -> tuple[Route, Any] | None:
        assert route.thread_name is not None
        try:
            thread = await message.create_thread(name=route.thread_name, auto_archive_duration=THREAD_ARCHIVE_MINUTES)
        except discord.HTTPException as exc:
            forbidden = isinstance(exc, discord.Forbidden)
            log.error("thread creation failed channel=%s message=%s author=%s status=%s code=%s: %s%s",
                      message.channel.id, message.id, message.author.id, exc.status, exc.code, exc.text,
                      " (needs Create Public Threads)" if forbidden else "", exc_info=not forbidden)
            await _send_warning(message.channel, THREAD_FORBIDDEN if forbidden else THREAD_FAILED)
            return None
        if thread.id != route.channel_id:
            log.warning("thread id %s differs from message id %s, keying on the thread", thread.id, message.id)
        key = make_key(key_kind(route.key), thread.id)
        log.info("thread opened key=%s name=%r channel=%s", key, route.thread_name, message.channel.id)
        return replace(route, key=key, channel_id=thread.id, thread_name=None), thread

    def _track_privacy(self, route: Route) -> None:
        assert self.manager is not None
        kind = key_kind(route.key)
        if kind == THREAD:
            self._private_threads.add(route.channel_id)
            self._opened_warned.discard(route.channel_id)
        if kind != PUBLIC_THREAD:
            return
        known = self.manager.record(make_key(THREAD, route.channel_id)).channel_id is not None
        first_since_open = known and route.channel_id not in self._opened_warned
        if route.channel_id in self._private_threads or first_since_open:
            self._private_threads.discard(route.channel_id)
            self._opened_warned.add(route.channel_id)
            log.warning("thread=%s is no longer private (%s): answering in public mode as %s, apart from the "
                        "private conversation", route.channel_id, route.note, route.key)

    async def _answer(self, route: Route, text: str, channel: Any) -> None:
        assert self.manager is not None
        origin = Origin(route.mode, route.channel_id, route.owner_id)
        async with channel.typing():
            reply = await self.manager.handle(route.key, text, origin, route.topic)
        # Turns can run for minutes; a private thread's parent may have opened up meanwhile.
        target, destination = await self._guard_private(route.key, channel,
                                                        Destination(False, route.channel_id, route.owner_id))
        await send_chunks(target, rerouted_note(route.key, destination) + reply)

    async def _guard_private(self, key: str, channel: Any, destination: Destination) -> tuple[Any, Destination]:
        """A private conversation whose channel stopped being private gets nothing more; the owner's DM does."""
        if not guards_privacy(key, channel, self.config.forum_channel_ids):
            return channel, destination
        privacy = channel_privacy(channel, self.config.allowed_user_ids)
        if privacy.private:
            return channel, destination
        if destination.owner_id is None:
            raise LookupError(f"{key!r} is no longer private ({privacy.reason}) and has no owner to DM instead")
        log.warning("key=%s channel=%s is no longer private (%s): sending to the owner's DM user=%s", key,
                    destination.target_id, privacy.reason, destination.owner_id)
        user = await self.fetch_user(destination.owner_id)
        return await user.create_dm(), Destination(True, destination.owner_id, destination.owner_id)

    async def channel_for(self, key: str) -> tuple[Any, Destination]:
        if self.manager is None:
            raise RuntimeError("AssistantBot.manager is not set")
        destination = resolve_destination(key, self.manager.record(key))
        if destination.dm:
            user = await self.fetch_user(destination.target_id)
            return await user.create_dm(), destination
        channel = self.get_channel(destination.target_id) or await self.fetch_channel(destination.target_id)
        if not hasattr(channel, "send"):
            raise TypeError(f"channel {destination.target_id} for {key!r} can't receive messages")
        return await self._guard_private(key, channel, destination)

    async def deliver(self, user_key: str, text: str) -> None:
        channel, destination = await self.channel_for(user_key)
        await send_chunks(channel, rerouted_note(user_key, destination) + text)

    async def close(self) -> None:
        for task in (self._maintenance, self._sync):
            if task:
                task.cancel()
        await super().close()
