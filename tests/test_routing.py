from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from typing import Any
from zoneinfo import ZoneInfo

import discord
import pytest

from assistant.agent import ApprovalRequest
from assistant.bot import AssistantBot, DiscordApprover, Route, classify, clean_text, resolve_destination, route
from assistant.config import Config
from assistant.permissions import Targets
from assistant.sessions import Origin, SessionRecord

BOT_ID, OWNER, STRANGER = 999, 111, 222
GUILD, OTHER_GUILD, FORUM, TEXT_PARENT = 500, 501, 600, 601
BOT = NS(id=BOT_ID)
TARGETS = Targets("c", "a" * 32, "b" * 32)
CONFIG = Config(None, False, frozenset({OWNER}), "Nethor", ZoneInfo("Europe/Oslo"), "m", TARGETS,
                allowed_guild_ids=frozenset({GUILD}), forum_channel_ids=frozenset({FORUM}))
DM_ONLY = Config(None, False, frozenset({OWNER}), "Nethor", ZoneInfo("Europe/Oslo"), "m", TARGETS)


def person(uid: int, bot: bool = False, name: str = "Whit") -> NS:
    return NS(id=uid, bot=bot, display_name=name)


def dm_channel() -> NS:
    return NS(id=10, type=discord.ChannelType.private)


def thread(tid: int = 700, parent: int = FORUM, archived: bool = False, tags: tuple[str, ...] = ("travel",)) -> NS:
    return NS(id=tid, type=discord.ChannelType.public_thread, parent_id=parent, archived=archived,
              name="Plan Lisbon trip", applied_tags=[NS(name=t) for t in tags])


def text_channel(cid: int = 800) -> NS:
    return NS(id=cid, type=discord.ChannelType.text)


def msg(channel: NS, author: int | NS = OWNER, content: str = "hi", mid: int = 1, guild: int = GUILD,
        mentions: tuple[NS, ...] = (), roles: tuple[NS, ...] = (), reply_to: NS | None = None) -> NS:
    return NS(id=mid, channel=channel, author=person(author) if isinstance(author, int) else author,
              content=content, guild=None if channel.type == discord.ChannelType.private else NS(id=guild),
              mentions=list(mentions), role_mentions=list(roles),
              reference=NS(resolved=reply_to) if reply_to else None)


def test_dm_routes_privately_like_v1() -> None:
    assert route(msg(dm_channel()), BOT, CONFIG) == Route("dm:111", "private", 10, OWNER)
    assert route(msg(dm_channel()), BOT, DM_ONLY) == Route("dm:111", "private", 10, OWNER)


def test_forum_starter_message_gets_topic() -> None:
    got = route(msg(thread(), mid=700), BOT, CONFIG)
    assert got is not None
    assert (got.key, got.mode, got.channel_id, got.owner_id) == ("thread:700", "private", 700, OWNER)
    assert got.topic == 'Conversation topic (this Discord forum post): "Plan Lisbon trip" · tags: travel'


def test_forum_reply_and_archived_post_route_to_the_same_key() -> None:
    reply = route(msg(thread(), mid=701), BOT, CONFIG)
    archived = route(msg(thread(archived=True), mid=702), BOT, CONFIG)
    assert reply is not None and archived is not None and reply.key == archived.key == "thread:700"


def test_forum_post_without_tags() -> None:
    got = route(msg(thread(tags=())), BOT, CONFIG)
    assert got is not None and got.topic == 'Conversation topic (this Discord forum post): "Plan Lisbon trip"'


def test_fetched_channel_overrides_partial_channel() -> None:
    partial = NS(id=700, type=None)
    got = route(msg(partial), BOT, CONFIG, channel=thread())
    assert got is not None and got.key == "thread:700"


def test_thread_under_non_forum_parent_falls_through_to_mentions() -> None:
    other = thread(tid=710, parent=TEXT_PARENT)
    assert classify(msg(other), BOT, CONFIG) == (None, "not addressed")
    got = route(msg(other, mentions=(BOT,)), BOT, CONFIG)
    assert got is not None and (got.key, got.mode, got.channel_id, got.thread_name) == ("pthread:710", "public", 710,
                                                                                        None)


def test_mention_in_allowed_guild_is_public() -> None:
    got, reason = classify(msg(text_channel(), mentions=(BOT,)), BOT, CONFIG)
    assert got is not None and (got.key, got.mode, got.channel_id) == ("pthread:1", "public", 1) and reason == "mention"


def test_reply_to_nethor_counts_as_mention() -> None:
    mine = NS(author=NS(id=BOT_ID))
    got, reason = classify(msg(text_channel(), reply_to=mine), BOT, CONFIG)
    assert got is not None and got.key == "pthread:1" and reason == "reply to Nethor"
    someone = NS(author=NS(id=STRANGER))
    assert route(msg(text_channel(), reply_to=someone), BOT, CONFIG) is None
    assert route(msg(text_channel(), reply_to=NS(id=5)), BOT, CONFIG) is None  # deleted message: no author


def test_bot_managed_role_mention_counts() -> None:
    role = NS(id=77, tags=NS(bot_id=BOT_ID))
    other_role = NS(id=78, tags=None)
    assert route(msg(text_channel(), roles=(role,)), BOT, CONFIG) is not None
    assert route(msg(text_channel(), roles=(other_role,)), BOT, CONFIG) is None


def test_no_mention_is_ignored() -> None:
    assert classify(msg(text_channel()), BOT, CONFIG) == (None, "not addressed")


def test_non_allowlisted_author_ignored_everywhere() -> None:
    assert classify(msg(dm_channel(), STRANGER), BOT, CONFIG) == (None, "DM from non-allowlisted user")
    assert classify(msg(thread(), STRANGER), BOT, CONFIG) == (None, "forum post by non-allowlisted user")
    assert classify(msg(text_channel(), STRANGER, mentions=(BOT,)), BOT, CONFIG) == (
        None, "mention by non-allowlisted user")


def test_guild_not_allowlisted_is_ignored() -> None:
    got, reason = classify(msg(text_channel(), guild=OTHER_GUILD, mentions=(BOT,)), BOT, CONFIG)
    assert got is None and "ALLOWED_GUILD_IDS" in reason


def test_empty_lists_turn_features_off() -> None:
    assert route(msg(thread()), BOT, DM_ONLY) is None
    assert route(msg(text_channel(), mentions=(BOT,)), BOT, DM_ONLY) is None


def test_own_and_other_bot_messages_ignored() -> None:
    assert classify(msg(dm_channel(), person(BOT_ID, bot=True)), BOT, CONFIG) == (None, "bot message")
    assert classify(msg(thread(), person(OWNER, bot=True)), BOT, CONFIG) == (None, "bot message")


def test_clean_text_strips_own_mentions_only() -> None:
    role = NS(id=77, tags=NS(bot_id=BOT_ID))
    assert clean_text(msg(text_channel(), content=f"<@{BOT_ID}> what's up"), BOT_ID) == "what's up"
    assert clean_text(msg(text_channel(), content=f"hey <@!{BOT_ID}>"), BOT_ID) == "hey"
    assert clean_text(msg(text_channel(), content="<@&77> sum up", roles=(role,)), BOT_ID) == "sum up"
    assert clean_text(msg(text_channel(), content="ask <@5>"), BOT_ID) == "ask <@5>"








class FakeChannel:
    def __init__(self, cid: int, parent_id: int | None = None) -> None:
        self.id = cid
        self.sent: list[tuple[str, dict[str, Any]]] = []
        if parent_id is not None:
            self.parent_id = parent_id

    async def send(self, content: str, **kwargs: Any) -> NS:
        self.sent.append((content, kwargs))
        view = kwargs.get("view")
        if view is not None:
            view.approved = True
            view.stop()
        return NS(content=content)

    @asynccontextmanager
    async def typing(self) -> Any:
        yield




def test_destination_per_mode() -> None:
    assert resolve_destination("dm:111", SessionRecord()).__dict__ == {"dm": True, "target_id": 111, "owner_id": 111}
    forum = resolve_destination("thread:700", SessionRecord(channel_id=700, owner_id=OWNER))
    assert (forum.dm, forum.target_id, forum.owner_id) == (False, 700, OWNER)
    public = resolve_destination("pub:800", SessionRecord())  # record dropped at 04:00: the key still knows
    assert (public.dm, public.target_id, public.owner_id) == (False, 800, None)
    with pytest.raises(LookupError):
        resolve_destination("console", SessionRecord())


class FakeManager:
    def __init__(self, records: dict[str, SessionRecord]) -> None:
        self.records = records
        self.handled: list[tuple[Any, ...]] = []

    def record(self, key: str) -> SessionRecord:
        return self.records.get(key, SessionRecord())

    async def handle(self, *args: Any) -> str:
        self.handled.append(args)
        return "answer"


def make_bot(records: dict[str, SessionRecord], channels: dict[int, FakeChannel]) -> tuple[AssistantBot, FakeManager]:
    bot = AssistantBot(CONFIG)
    manager = FakeManager(records)
    bot.manager = manager  # type: ignore[assignment]
    bot._connection.user = NS(id=BOT_ID)  # type: ignore[assignment]
    dms: dict[int, FakeChannel] = {}

    async def fetch_user(uid: int) -> NS:
        async def create_dm() -> FakeChannel:
            return dms.setdefault(uid, FakeChannel(uid))
        return NS(id=uid, create_dm=create_dm)

    async def fetch_channel(cid: int) -> FakeChannel:
        return channels[cid]

    bot.fetch_user = fetch_user  # type: ignore[method-assign]
    bot.fetch_channel = fetch_channel  # type: ignore[method-assign]
    bot.get_channel = lambda cid: None  # type: ignore[method-assign]
    bot.dms = dms  # type: ignore[attr-defined]
    return bot, manager


async def test_deliver_goes_to_dm_post_or_channel() -> None:
    post, general = FakeChannel(700, parent_id=FORUM), FakeChannel(800)
    bot, _ = make_bot({"thread:700": SessionRecord("s", channel_id=700, owner_id=OWNER)}, {700: post, 800: general})
    await bot.deliver("dm:111", "late dm")
    await bot.deliver("thread:700", "late post")
    await bot.deliver("pub:800", "late public")
    assert bot.dms[111].sent[0][0] == "late dm"  # type: ignore[attr-defined]
    assert post.sent[0][0] == "late post" and general.sent[0][0] == "late public"


async def test_forum_approval_buttons_go_into_the_post_for_the_owner() -> None:
    post = FakeChannel(700, parent_id=FORUM)
    bot, _ = make_bot({"thread:700": SessionRecord("s", channel_id=700, owner_id=OWNER)}, {700: post})
    approved = await DiscordApprover(bot).approve(ApprovalRequest("thread:700", "delete_event", {"eventId": "e"}, "r"))
    view = post.sent[0][1]["view"]
    assert approved and view.owner_id == OWNER and not bot.dms  # type: ignore[attr-defined]


async def test_dm_approval_still_goes_to_dm() -> None:
    bot, _ = make_bot({}, {})
    assert await DiscordApprover(bot).approve(ApprovalRequest("dm:111", "delete_event", {}, "r"))
    assert bot.dms[111].sent[0][1]["view"].owner_id == OWNER  # type: ignore[attr-defined]


async def test_approval_without_known_owner_raises() -> None:
    bot, _ = make_bot({}, {700: FakeChannel(700)})
    with pytest.raises(LookupError):
        await DiscordApprover(bot).approve(ApprovalRequest("thread:700", "delete_event", {}, "r"))


def live_message(channel: FakeChannel, ctype: discord.ChannelType, content: str, **extra: Any) -> NS:
    channel.type = ctype  # type: ignore[attr-defined]
    for name, value in extra.items():
        setattr(channel, name, value)
    base = msg(channel, content=content, mentions=(BOT,))  # type: ignore[arg-type]
    base.to_reference = lambda fail_if_not_exists: NS(ref=base.id, strict=fail_if_not_exists)
    return base



async def test_on_message_forum_passes_topic_and_no_context() -> None:
    post = FakeChannel(700)
    bot, manager = make_bot({}, {})
    message = live_message(post, discord.ChannelType.public_thread, "flights?", parent_id=FORUM, name="Lisbon",
                           applied_tags=[])
    await bot.on_message(message)
    key, text, origin, topic = manager.handled[0]
    assert (key, origin) == ("thread:700", Origin("private", 700, OWNER))
    assert topic is not None and '"Lisbon"' in topic and post.sent == [("answer", {})]
