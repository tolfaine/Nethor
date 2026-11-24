import asyncio
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from assistant import discord_browse as browse
from assistant.discord_common import UNTRUSTED, DiscordContext, parse_limit
from discord_fakes import BOT_USER, GUILD_ID, ChannelsClient, FakeReaction, FakeTextChannel, http_error, message

OSLO = ZoneInfo("Europe/Oslo")


def ctx(client: Any) -> DiscordContext:
    async def conversation() -> Any:
        raise AssertionError("not used")

    return DiscordContext(lambda: client, frozenset({GUILD_ID}), OSLO, conversation)


def run(coro_fn: Any, client: Any, args: dict[str, Any]) -> dict[str, Any]:
    return asyncio.run(coro_fn(ctx(client), args))


def text_of(result: dict[str, Any]) -> str:
    return result["content"][0]["text"]


def test_format_message_has_local_time_id_and_markers() -> None:
    line = browse.format_message(message(5, "line one\nline two", "Sam", minute=7, reply_to=3, files=("a.png",),
                                         reactions=(("👍", 2),)), OSLO)
    assert line.startswith("[2026-11-24 23:07 · 5] Sam: line one ↵ line two")  # 22:07 UTC = 23:07 in Oslo
    assert "↪ reply to 3" in line and "📎 1 file(s): a.png" in line and "reactions: 👍 2" in line


def test_format_message_clips_long_text() -> None:
    line = browse.format_message(message(1, "x" * 5000), OSLO)
    assert "x" * (browse.MESSAGE_CHARS - 1) + "…" in line and "x" * browse.MESSAGE_CHARS not in line


def test_join_clipped_drops_whole_lines_and_says_so() -> None:
    joined = browse.join_clipped("head", ["a" * 40, "b" * 40, "c" * 40], limit=100)
    assert "a" * 40 in joined and "b" * 40 in joined and "c" * 40 not in joined
    assert joined.endswith("… 1 more lines cut: ask for fewer, or page with before/after.")


@pytest.mark.parametrize(("value", "expected"), [(None, 30), ("5", 5), (100, 100)])
def test_parse_limit_accepts(value: object, expected: int) -> None:
    assert parse_limit(value, 30, 100) == expected


@pytest.mark.parametrize("value", [0, 101, "abc", -1, True, 2.5])
def test_parse_limit_rejects(value: object) -> None:
    assert isinstance(parse_limit(value, 30, 100), str)


def test_read_messages_oldest_first_with_untrusted_header() -> None:
    channel = FakeTextChannel(1, "general", messages=[message(i, f"m{i}", minute=i) for i in range(1, 6)])
    result = run(browse.run_read_messages, ChannelsClient(channel), {"channel_id": "1", "limit": 3})
    text = text_of(result)
    assert text.startswith(UNTRUSTED) and "#general (id 1) in Home: 3 messages" in text
    assert text.index("m3") < text.index("m4") < text.index("m5") and "m2" not in text
    assert not result.get("is_error")


def test_read_messages_passes_before_and_after() -> None:
    channel = FakeTextChannel(1, "general", messages=[message(i, f"m{i}") for i in range(1, 10)])
    text = text_of(run(browse.run_read_messages, ChannelsClient(channel),
                       {"channel_id": "1", "before": "8", "after": "5"}))
    assert "m6" in text and "m7" in text and "m8" not in text and "m5" not in text


def test_read_messages_fetches_an_archived_thread() -> None:
    thread = FakeTextChannel(7, "old-thread", messages=[message(1, "archived hello")])
    assert "archived hello" in text_of(run(browse.run_read_messages, ChannelsClient(fetchable=(thread,)),
                                           {"channel_id": "7"}))


@pytest.mark.parametrize(("args", "client"), [
    ({"channel_id": "1"}, ChannelsClient(FakeTextChannel(1, "x", guild_id=555))),
    ({"channel_id": "1"}, ChannelsClient()),
    ({"channel_id": "1"}, ChannelsClient(SimpleNamespace(id=1, name="dm"))),
    ({"channel_id": "abc"}, ChannelsClient()),
    ({"channel_id": "1", "limit": 500}, ChannelsClient(FakeTextChannel(1, "x"))),
    ({"channel_id": "1", "before": "nope"}, ChannelsClient(FakeTextChannel(1, "x"))),
])
def test_read_messages_refuses(args: dict[str, Any], client: Any) -> None:
    assert run(browse.run_read_messages, client, args)["is_error"]


def test_read_messages_without_permission_says_which() -> None:
    channel = FakeTextChannel(1, "secret", history_error=http_error())
    result = run(browse.run_read_messages, ChannelsClient(channel), {"channel_id": "1"})
    assert result["is_error"] and "Read Message History" in text_of(result)


def test_list_archived_threads() -> None:
    old = SimpleNamespace(id=70, name="taxes-2025", archive_timestamp=message(1).created_at)
    channel = FakeTextChannel(1, "forum", archived=[old])
    text = text_of(run(browse.run_list_archived_threads, ChannelsClient(channel), {"channel_id": "1"}))
    assert "thread taxes-2025 (id 70) archived 2026-11-24" in text and "1 archived threads" in text


class FakeGuild:
    def __init__(self, guild_id: int, groups: list[tuple[Any, list[Any]]], threads: list[Any]) -> None:
        self.id, self.name, self.me = guild_id, f"Guild {guild_id}", BOT_USER
        self._groups, self._threads = groups, threads

    def by_category(self) -> list[tuple[Any, list[Any]]]:
        return self._groups

    async def active_threads(self) -> list[Any]:
        return self._threads


def test_list_channels_tree_and_only_allowed_guilds() -> None:
    lobby, work = FakeTextChannel(1, "lobby"), FakeTextChannel(2, "work", topic="Day job\nstuff")
    category = SimpleNamespace(id=10, name="Projects")
    thread = SimpleNamespace(id=20, name="q4-plan", parent_id=2)
    allowed = FakeGuild(GUILD_ID, [(None, [lobby]), (category, [work])], [thread])
    other = FakeGuild(555, [(None, [FakeTextChannel(3, "elsewhere", guild_id=555)])], [])
    text = text_of(run(browse.run_list_channels, ChannelsClient(guilds=(allowed, other)), {}))
    lines = text.splitlines()
    assert lines[1:] == [f"Server Guild {GUILD_ID} (id {GUILD_ID})", "  text #lobby (id 1)",
                         "  category Projects (id 10)", "    text #work (id 2) — topic: Day job stuff",
                         "      thread q4-plan (id 20)"]
    assert "elsewhere" not in text


def test_list_channels_refuses_unlisted_guild() -> None:
    other = FakeGuild(555, [], [])
    assert run(browse.run_list_channels, ChannelsClient(guilds=(other,)), {"guild_id": "555"})["is_error"]


def test_get_reactions_lists_who() -> None:
    msg = message(5)
    msg.reactions = [FakeReaction("👍", 3, ("Sam", "Alex"))]
    channel = FakeTextChannel(1, "general", messages=[msg])
    text = text_of(run(browse.run_get_reactions, ChannelsClient(channel), {"channel_id": "1", "message_id": "5"}))
    assert "👍 3: Sam, Alex, …" in text


def test_get_reactions_unknown_message() -> None:
    channel = FakeTextChannel(1, "general")
    assert run(browse.run_get_reactions, ChannelsClient(channel), {"channel_id": "1", "message_id": "5"})["is_error"]


def test_react_adds_and_reports_each_emoji() -> None:
    channel = FakeTextChannel(1, "general", reaction_fail={"<:gone:1>"})
    result = run(browse.run_react, ChannelsClient(channel),
                 {"channel_id": "1", "message_id": "5", "emojis": ["👍", "<:gone:1>"]})
    assert channel.partials[5].added == ["👍"]
    assert result["is_error"] and "✅ 👍 added" in text_of(result) and "⚠️ <:gone:1>" in text_of(result)


def test_react_remove_only_touches_own_reaction() -> None:
    channel = FakeTextChannel(1, "general")
    run(browse.run_react, ChannelsClient(channel), {"channel_id": "1", "message_id": "5", "emojis": ["👍"],
                                                    "remove": True})
    assert channel.partials[5].removed == [("👍", BOT_USER)]


@pytest.mark.parametrize("emojis", [[], "👍", [""], ["👍"] * (browse.EMOJI_MAX + 1)])
def test_react_rejects_bad_emoji_lists(emojis: Any) -> None:
    channel = FakeTextChannel(1, "general")
    result = run(browse.run_react, ChannelsClient(channel), {"channel_id": "1", "message_id": "5", "emojis": emojis})
    assert result["is_error"] and not channel.partials


def test_channel_without_messages_is_refused() -> None:
    category = SimpleNamespace(id=1, name="cat", guild=SimpleNamespace(id=GUILD_ID))  # no history(), like a category
    assert run(browse.run_read_messages, ChannelsClient(category), {"channel_id": "1"})["is_error"]
    assert run(browse.run_get_reactions, ChannelsClient(category), {"channel_id": "1", "message_id": "2"})["is_error"]
