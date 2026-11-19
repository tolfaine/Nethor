from __future__ import annotations

import logging
from types import SimpleNamespace as NS
from typing import Any

import discord
import pytest

from assistant.agent import ApprovalRequest
from assistant.bot import (
    DEFAULT_THREAD_NAME, THREAD_ARCHIVE_MINUTES, DiscordApprover, classify, format_channel_topic, guards_privacy,
    route, thread_name,
)
from assistant.sessions import Origin, SessionRecord
from test_private_channel import HIDDEN, THREAD_ID, hidden, make_private, thread_in
from test_routing import BOT, BOT_ID, CONFIG, FORUM, OWNER, STRANGER, FakeChannel, make_bot, msg, text_channel, thread

MENTION = f"<@{BOT_ID}>"
NO_DESCRIPTION = "Conversation topic: #unknown — no description"


def known(*ids: int) -> Any:
    return lambda channel_id: channel_id in ids


def named(channel: NS, name: str = "plans", topic: str | None = "Weekend plans") -> NS:
    channel.name, channel.topic = name, topic
    return channel


# --- thread names and topics -------------------------------------------------------------------------------------

def test_thread_name_strips_mentions_and_whitespace() -> None:
    assert thread_name(f"{MENTION}  plan a\npicnic <@&77> with <@5> in <#9>") == "plan a picnic with in"
    assert thread_name("  ") == DEFAULT_THREAD_NAME == "Nethor"
    assert thread_name(MENTION) == "Nethor"


def test_thread_name_is_trimmed_to_80_chars() -> None:
    name = thread_name("x" * 200)
    assert name == "x" * 80 + "…"
    assert thread_name("y" * 80) == "y" * 80


def test_channel_topic_line() -> None:
    assert format_channel_topic("plans", "Weekend plans") == "Conversation topic: #plans — Weekend plans"
    assert format_channel_topic("plans", None) == "Conversation topic: #plans — no description"
    assert format_channel_topic("plans", "  ") == "Conversation topic: #plans — no description"
    assert "\n" not in format_channel_topic("plans", "line one\nline two")


# --- routing -----------------------------------------------------------------------------------------------------

def test_mention_in_private_channel_starts_a_private_thread() -> None:
    message = msg(named(hidden()), content=f"{MENTION} plan a picnic", mid=42, mentions=(BOT,))
    got, reason = classify(message, BOT, CONFIG)
    assert got is not None and reason == "mention in a private channel"
    assert (got.key, got.mode, got.channel_id, got.owner_id) == ("thread:42", "private", 42, OWNER)
    assert got.thread_name == "plan a picnic"
    assert got.topic == "Conversation topic: #plans — Weekend plans"


def test_mention_in_public_channel_starts_a_public_thread() -> None:
    message = msg(text_channel(), content=f"{MENTION} weather in Oslo?", mid=43, mentions=(BOT,))
    got = route(message, BOT, CONFIG)
    assert got is not None and (got.key, got.mode, got.channel_id) == ("pthread:43", "public", 43)
    assert got.thread_name == "weather in Oslo?" and got.topic == NO_DESCRIPTION


def test_reply_to_nethor_in_a_channel_starts_a_thread() -> None:
    got = route(msg(text_channel(), mid=44, reply_to=NS(author=NS(id=BOT_ID))), BOT, CONFIG)
    assert got is not None and got.key == "pthread:44" and got.thread_name == "hi"


def test_message_in_a_nethor_thread_needs_no_mention() -> None:
    private = thread_in(hidden())
    got, reason = classify(msg(private), BOT, CONFIG, has_conversation=known(THREAD_ID))
    assert got is not None and got.key == f"thread:{THREAD_ID}" and got.thread_name is None
    assert "Nethor thread" in reason
    public = thread(tid=710, parent=601)
    got = route(msg(public), BOT, CONFIG, has_conversation=known(710))
    assert got is not None and (got.key, got.mode, got.thread_name) == ("pthread:710", "public", None)


def test_foreign_thread_needs_a_mention_and_answers_in_place() -> None:
    foreign = thread_in(hidden())
    assert classify(msg(foreign), BOT, CONFIG) == (None, "not addressed")
    got = route(msg(foreign, mentions=(BOT,)), BOT, CONFIG)
    assert got is not None and got.key == f"thread:{THREAD_ID}" and got.channel_id == THREAD_ID
    assert got.thread_name is None  # never a thread inside a thread


def test_thread_topic_comes_from_the_parent_channel() -> None:
    parent = named(hidden(), "trips", "Where we go next")
    got = route(msg(thread_in(parent), mentions=(BOT,)), BOT, CONFIG)
    assert got is not None and got.topic == "Conversation topic: #trips — Where we go next"


def test_non_allowlisted_author_is_ignored_in_channels_and_threads() -> None:
    assert route(msg(hidden(), STRANGER, mentions=(BOT,)), BOT, CONFIG) is None
    got, reason = classify(msg(thread_in(hidden()), STRANGER), BOT, CONFIG, has_conversation=known(THREAD_ID))
    assert got is None and "non-allowlisted" in reason


def test_thread_whose_parent_became_public_routes_to_pthread() -> None:
    parent = hidden()
    private = thread_in(parent)
    assert route(msg(private), BOT, CONFIG, has_conversation=known(THREAD_ID)).key == f"thread:{THREAD_ID}"  # type: ignore[union-attr]
    parent.permissions_for = lambda obj: NS(view_channel=True)
    got = route(msg(private), BOT, CONFIG, has_conversation=known(THREAD_ID))
    assert got is not None and got.key == f"pthread:{THREAD_ID}" and "@everyone" in got.note


def test_forum_post_routing_is_unchanged() -> None:
    got = route(msg(thread(), mid=700), BOT, CONFIG, has_conversation=known())
    assert got is not None and got.key == "thread:700" and got.thread_name is None
    assert got.topic is not None and "forum post" in got.topic


def test_channel_without_threads_is_ignored_with_a_reason() -> None:
    voice = NS(id=850, type=discord.ChannelType.voice)
    got, reason = classify(msg(voice, mentions=(BOT,)), BOT, CONFIG)
    assert got is None and "threads" in reason


def test_privacy_guard_covers_channel_threads_not_forum_posts() -> None:
    assert guards_privacy("thread:1", NS(parent_id=601), CONFIG.forum_channel_ids)
    assert guards_privacy("thread:1", NS(), CONFIG.forum_channel_ids)  # unknown parent: fail closed
    assert not guards_privacy("thread:1", NS(parent_id=FORUM), CONFIG.forum_channel_ids)
    assert guards_privacy("priv:1", NS(), CONFIG.forum_channel_ids)
    assert not guards_privacy("pthread:1", NS(parent_id=601), CONFIG.forum_channel_ids)
    assert not guards_privacy("dm:1", NS(), CONFIG.forum_channel_ids)


# --- on_message ----------------------------------------------------------------------------------------------------

class ThreadingMessage(NS):
    """A guild message whose create_thread returns `thread` or raises `error`."""

    async def create_thread(self, **kwargs: Any) -> Any:
        self.thread_args = kwargs
        if self.error is not None:
            raise self.error
        return self.thread


def thread_of(parent: Any, tid: int) -> FakeChannel:
    new = FakeChannel(tid)
    new.type, new.parent, new.parent_id = discord.ChannelType.public_thread, parent, parent.id  # type: ignore[attr-defined]
    new.guild = getattr(parent, "guild", None)  # type: ignore[attr-defined]
    return new


def mention_in(channel: FakeChannel, content: str, mid: int = 42, error: Exception | None = None) -> ThreadingMessage:
    base = msg(channel, content=content, mid=mid, mentions=(BOT,))  # type: ignore[arg-type]
    return ThreadingMessage(**vars(base), thread=thread_of(channel, mid), error=error)


def private_channel() -> FakeChannel:
    return make_private(named(FakeChannel(HIDDEN)))  # type: ignore[return-value]


def public_channel() -> FakeChannel:
    channel = named(FakeChannel(800), "general", None)
    channel.type = discord.ChannelType.text  # type: ignore[attr-defined]
    return channel  # type: ignore[return-value]


async def test_mention_creates_a_thread_and_answers_inside_it() -> None:
    channel = private_channel()
    bot, manager = make_bot({}, {})
    message = mention_in(channel, f"{MENTION} plan a picnic")
    await bot.on_message(message)
    assert THREAD_ARCHIVE_MINUTES == 10080
    assert message.thread_args == {"name": "plan a picnic", "auto_archive_duration": THREAD_ARCHIVE_MINUTES}
    key, text, origin, topic = manager.handled[0]
    assert (key, text, origin) == ("thread:42", "plan a picnic", Origin("private", 42, OWNER))
    assert topic == "Conversation topic: #plans — Weekend plans"
    assert message.thread.sent == [("answer", {})] and channel.sent == []


async def test_mention_in_public_channel_answers_in_a_public_thread() -> None:
    channel = public_channel()
    bot, manager = make_bot({}, {})
    message = mention_in(channel, f"{MENTION} weather?", mid=43)
    await bot.on_message(message)
    key, _, origin, topic = manager.handled[0]
    assert (key, origin, topic) == ("pthread:43", Origin("public", 43, OWNER), "Conversation topic: #general — "
                                                                                "no description")
    assert message.thread.sent == [("answer", {})]


async def test_thread_creation_forbidden_warns_in_the_channel(caplog) -> None:
    channel = private_channel()
    bot, manager = make_bot({}, {})
    error = discord.Forbidden(NS(status=403, reason="Forbidden"), "Missing Permissions")
    with caplog.at_level(logging.ERROR):
        await bot.on_message(mention_in(channel, f"{MENTION} plan a picnic", error=error))
    assert manager.handled == [] and len(channel.sent) == 1
    warning = channel.sent[0][0]
    assert warning.startswith("⚠️") and "Create Public Threads" in warning and "\n" not in warning
    assert f"channel={HIDDEN}" in caplog.text and "message=42" in caplog.text


async def test_thread_creation_http_error_warns_and_logs(caplog) -> None:
    channel = public_channel()
    bot, manager = make_bot({}, {})
    error = discord.HTTPException(NS(status=500, reason="Server Error"), "boom")
    await bot.on_message(mention_in(channel, f"{MENTION} hi", error=error))
    assert manager.handled == [] and channel.sent[0][0].startswith("⚠️")
    assert "thread creation failed" in caplog.text


async def test_follow_up_in_a_nethor_thread_without_mention_is_answered() -> None:
    parent = private_channel()
    nethor_thread = thread_of(parent, 42)
    bot, manager = make_bot({"thread:42": SessionRecord("s", channel_id=42, owner_id=OWNER)}, {})
    await bot.on_message(msg(nethor_thread, content="and drinks?"))  # type: ignore[arg-type]
    assert manager.handled[0][:2] == ("thread:42", "and drinks?") and nethor_thread.sent == [("answer", {})]


async def test_new_in_a_thread_keeps_routing_without_mention() -> None:
    parent = public_channel()
    public_thread = thread_of(parent, 43)
    bot, manager = make_bot({"pthread:43": SessionRecord(None, channel_id=43, owner_id=OWNER)}, {})
    await bot.on_message(msg(public_thread, content="!new"))  # type: ignore[arg-type]
    assert manager.handled[0][0] == "pthread:43"


async def test_parent_turning_public_warns_and_answers_publicly(caplog) -> None:
    parent = private_channel()
    nethor_thread = thread_of(parent, 42)
    bot, manager = make_bot({"thread:42": SessionRecord("s", channel_id=42, owner_id=OWNER)}, {})
    parent.permissions_for = lambda obj: NS(view_channel=True)  # type: ignore[attr-defined]
    with caplog.at_level(logging.WARNING):
        await bot.on_message(msg(nethor_thread, content="still there?"))  # type: ignore[arg-type]
    assert manager.handled[0][0] == "pthread:42" and manager.handled[0][2].mode == "public"
    assert "thread=42" in caplog.text and "no longer private" in caplog.text


async def test_reply_goes_to_dm_when_thread_parent_opened_during_the_turn() -> None:
    parent = private_channel()
    bot, manager = make_bot({}, {})
    message = mention_in(parent, f"{MENTION} my calendar?")

    async def handle(*args: Any) -> str:
        parent.permissions_for = lambda obj: NS(view_channel=True)  # type: ignore[attr-defined]
        return "secret answer"

    manager.handle = handle  # type: ignore[method-assign]
    await bot.on_message(message)
    assert message.thread.sent == []
    dm_text = bot.dms[OWNER].sent[0][0]  # type: ignore[attr-defined]
    assert "no longer private" in dm_text and dm_text.endswith("secret answer")


def thread_bot(still_private: bool) -> tuple[Any, FakeChannel]:
    parent = make_private(FakeChannel(HIDDEN), everyone_can_view=not still_private)
    nethor_thread = thread_of(parent, 42)
    bot, _ = make_bot({"thread:42": SessionRecord("s", channel_id=42, owner_id=OWNER)}, {42: nethor_thread})
    return bot, nethor_thread


@pytest.mark.parametrize("still_private", [True, False])
async def test_queued_reply_for_a_thread_follows_parent_privacy(still_private: bool) -> None:
    bot, nethor_thread = thread_bot(still_private)
    await bot.deliver("thread:42", "late")
    if still_private:
        assert nethor_thread.sent[0][0] == "late" and not bot.dms
    else:
        assert nethor_thread.sent == [] and bot.dms[OWNER].sent[0][0].endswith("late")


async def test_thread_approval_falls_back_to_dm_when_parent_opened() -> None:
    bot, nethor_thread = thread_bot(still_private=False)
    assert await DiscordApprover(bot).approve(ApprovalRequest("thread:42", "delete_event", {"eventId": "e"}, "r"))
    sent = bot.dms[OWNER].sent[0]
    assert nethor_thread.sent == [] and sent[1]["view"].owner_id == OWNER and "no longer private" in sent[0]


async def test_public_thread_delivery_goes_to_the_thread() -> None:
    nethor_thread = thread_of(public_channel(), 43)
    bot, _ = make_bot({"pthread:43": SessionRecord("s", channel_id=43, owner_id=OWNER)}, {43: nethor_thread})
    await bot.deliver("pthread:43", "late public")
    assert nethor_thread.sent[0][0] == "late public" and not bot.dms
