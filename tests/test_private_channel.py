from __future__ import annotations

from types import SimpleNamespace as NS
from typing import Any

import discord

from assistant.agent import ApprovalRequest
from assistant.bot import DiscordApprover, channel_privacy, classify, is_private_channel, route
from assistant.sessions import SessionRecord
from test_routing import BOT, BOT_ID, CONFIG, FORUM, GUILD, OWNER, STRANGER, FakeChannel, make_bot, msg, person, thread

ALLOWED = CONFIG.allowed_user_ids
HIDDEN, THREAD_ID = 900, 901
OTHER_BOT = 333


def member(uid: int, bot: bool = False) -> NS:
    return NS(id=uid, bot=bot)


def guild(members: list[NS], chunked: bool = True) -> NS:
    by_id = {m.id: m for m in members}
    return NS(id=GUILD, chunked=chunked, default_role=NS(id=GUILD), get_member=by_id.get)


def make_private(channel: Any, viewers: tuple[int, ...] = (OWNER,), everyone_can_view: bool = False,
                 chunked: bool = True, ctype: discord.ChannelType = discord.ChannelType.text) -> Any:
    members = [member(uid) for uid in viewers] + [member(BOT_ID, bot=True), member(OTHER_BOT, bot=True)]
    channel.type = ctype
    channel.guild = guild(members, chunked)
    channel.members = members
    channel.permissions_for = lambda obj: NS(view_channel=everyone_can_view)
    return channel


def hidden(**kwargs: Any) -> NS:
    return make_private(NS(id=HIDDEN), **kwargs)


def thread_in(parent: NS, private: bool = False, thread_members: tuple[int, ...] = ()) -> NS:
    ctype = discord.ChannelType.private_thread if private else discord.ChannelType.public_thread
    return NS(id=THREAD_ID, type=ctype, parent_id=parent.id, parent=parent, guild=parent.guild,
              members=[NS(id=uid) for uid in thread_members])


def test_everyone_can_view_is_not_private() -> None:
    verdict = channel_privacy(hidden(everyone_can_view=True), ALLOWED)
    assert not verdict.private and "@everyone" in verdict.reason


def test_hidden_with_only_owner_and_bots_is_private() -> None:
    assert is_private_channel(hidden(), ALLOWED)


def test_hidden_with_another_human_is_not_private() -> None:
    verdict = channel_privacy(hidden(viewers=(OWNER, STRANGER)), ALLOWED)
    assert not verdict.private and str(STRANGER) in verdict.reason


def test_unknown_or_empty_member_cache_is_not_private() -> None:
    assert not is_private_channel(hidden(chunked=False), ALLOWED)
    assert not is_private_channel(hidden(viewers=()), ALLOWED)
    no_members = hidden()
    del no_members.members
    assert not is_private_channel(no_members, ALLOWED)


def test_unsupported_types_are_not_private() -> None:
    assert not is_private_channel(hidden(ctype=discord.ChannelType.voice), ALLOWED)
    assert not is_private_channel(NS(id=1, type=discord.ChannelType.text), ALLOWED)  # no guild


def test_thread_under_private_parent_is_private() -> None:
    assert is_private_channel(thread_in(hidden()), ALLOWED)
    assert not is_private_channel(thread_in(hidden(viewers=(OWNER, STRANGER))), ALLOWED)
    orphan = thread_in(hidden())
    orphan.parent = None
    assert not is_private_channel(orphan, ALLOWED)


def test_private_thread_members_must_be_allowlisted_or_bots() -> None:
    assert is_private_channel(thread_in(hidden(), private=True, thread_members=(OWNER, OTHER_BOT)), ALLOWED)
    assert not is_private_channel(thread_in(hidden(), private=True, thread_members=(OWNER, STRANGER)), ALLOWED)


def test_check_error_is_logged_and_not_private(caplog) -> None:
    broken = hidden()

    def boom(obj: object) -> NS:
        raise RuntimeError("cache exploded")

    broken.permissions_for = boom
    verdict = channel_privacy(broken, ALLOWED)
    assert not verdict.private and "RuntimeError" in verdict.reason
    assert f"privacy check failed channel={HIDDEN}" in caplog.text


def test_mention_in_private_channel_routes_privately() -> None:
    got, reason = classify(msg(hidden(), mentions=(BOT,)), BOT, CONFIG)
    assert got is not None and (got.key, got.mode, got.channel_id, got.owner_id) == ("thread:1", "private", 1, OWNER)
    assert reason == "mention in a private channel"
    in_thread = route(msg(thread_in(hidden()), mentions=(BOT,)), BOT, CONFIG)
    assert in_thread is not None and (in_thread.key, in_thread.channel_id) == (f"thread:{THREAD_ID}", THREAD_ID)


def test_private_channel_still_needs_a_mention_and_an_allowlisted_author() -> None:
    assert classify(msg(hidden()), BOT, CONFIG) == (None, "not addressed")
    assert route(msg(hidden(), STRANGER, mentions=(BOT,)), BOT, CONFIG) is None
    assert route(msg(hidden(), person(OWNER, bot=True), mentions=(BOT,)), BOT, CONFIG) is None


def test_channel_that_became_public_starts_public_threads() -> None:
    channel = hidden()
    assert route(msg(channel, mentions=(BOT,)), BOT, CONFIG).key == "thread:1"  # type: ignore[union-attr]
    channel.permissions_for = lambda obj: NS(view_channel=True)
    got = route(msg(channel, mentions=(BOT,)), BOT, CONFIG)
    assert got is not None and (got.key, got.mode) == ("pthread:1", "public") and "@everyone" in got.note


def test_forum_post_in_private_forum_stays_thread() -> None:
    post = thread()
    post.parent = make_private(NS(id=FORUM), ctype=discord.ChannelType.forum)
    post.guild = post.parent.guild
    got = route(msg(post), BOT, CONFIG)
    assert got is not None and got.key == "thread:700"





def private_bot(still_private: bool) -> tuple[Any, FakeChannel]:
    channel = make_private(FakeChannel(HIDDEN), everyone_can_view=not still_private)
    bot, _ = make_bot({f"priv:{HIDDEN}": SessionRecord("s", channel_id=HIDDEN, owner_id=OWNER)}, {HIDDEN: channel})
    return bot, channel


async def test_queued_reply_goes_to_channel_while_private() -> None:
    bot, channel = private_bot(still_private=True)
    await bot.deliver(f"priv:{HIDDEN}", "late")
    assert channel.sent[0][0] == "late" and not bot.dms


async def test_queued_reply_falls_back_to_dm_when_no_longer_private(caplog) -> None:
    bot, channel = private_bot(still_private=False)
    await bot.deliver(f"priv:{HIDDEN}", "late")
    assert channel.sent == []
    dm_text = bot.dms[OWNER].sent[0][0]
    assert dm_text.endswith("late") and f"<#{HIDDEN}>" in dm_text
    assert "no longer private" in caplog.text


async def test_approval_in_private_channel_and_dm_fallback() -> None:
    request = ApprovalRequest(f"priv:{HIDDEN}", "delete_event", {"eventId": "e"}, "r")
    bot, channel = private_bot(still_private=True)
    assert await DiscordApprover(bot).approve(request)
    assert channel.sent[0][1]["view"].owner_id == OWNER and not bot.dms
    bot, channel = private_bot(still_private=False)
    assert await DiscordApprover(bot).approve(request)
    sent = bot.dms[OWNER].sent[0]
    assert channel.sent == [] and sent[1]["view"].owner_id == OWNER and "no longer private" in sent[0]
