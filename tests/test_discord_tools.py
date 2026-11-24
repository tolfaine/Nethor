import asyncio
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import discord
import pytest

from assistant import discord_tools
from assistant.discord_tools import (
    MAX_CHANNELS, SERVER_NAME, normalize_name, parse_items, run_create_channels, run_delete_channels,
    run_rename_channels, same_name, unique_names, validate_renames, validate_request,
)
from assistant.permissions import DISCORD, OWN_SERVERS
from discord_fakes import GUILD_ID, ChannelsClient, FakeTextChannel

CATEGORY_ID = 222


class FakeCategory(discord.CategoryChannel):
    """isinstance-compatible stand-in that records create_text_channel calls."""

    def __init__(self, guild_id: int, fail: set[str] | None = None) -> None:
        self.created: list[str] = []
        self._fail = fail or set()
        self._guild = SimpleNamespace(id=guild_id)
        self._id = CATEGORY_ID

    @property
    def guild(self) -> Any:  # type: ignore[override]
        return self._guild

    @property
    def id(self) -> int:  # type: ignore[override]
        return self._id

    async def create_text_channel(self, name: str, **kwargs: Any) -> Any:  # type: ignore[override]
        if name in self._fail:
            raise discord.HTTPException(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")
        self.created.append(name)
        return SimpleNamespace(name=name, id=1000 + len(self.created))


class FakeClient:
    def __init__(self, category: Any) -> None:
        self._category = category

    def get_channel(self, channel_id: int) -> Any:
        return self._category if channel_id == CATEGORY_ID else None

    async def fetch_channel(self, channel_id: int) -> Any:
        raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Channel")


def run(client: Any, args: dict[str, Any]) -> dict[str, Any]:
    return asyncio.run(run_create_channels(client, frozenset({GUILD_ID}), args))


def test_normalize_name() -> None:
    assert normalize_name("  My Big  Project! ") == "my-big-project"
    assert normalize_name("a" * 150) == "a" * 100
    assert normalize_name("!!!") == ""


@pytest.mark.parametrize(("raw", "expected"), [
    ("🎮 Gaming", "🎮-gaming"),
    ("📁・Work", "📁・work"),
    ("home_lab / Notes", "home_lab-notes"),
    ("🔥", "🔥"),
])
def test_normalize_name_keeps_emoji(raw: str, expected: str) -> None:
    assert normalize_name(raw) == expected


def test_unique_names_drops_empty_and_duplicates() -> None:
    assert unique_names(["Work", "work", "", "Home Lab"]) == ["work", "home-lab"]


@pytest.mark.parametrize("args", [
    {"category_id": "abc", "names": ["x"]},
    {"category_id": "1", "names": "x"},
    {"category_id": "1", "names": []},
    {"category_id": "1", "names": [f"c{i}" for i in range(MAX_CHANNELS + 1)]},
])
def test_validate_request_rejects(args: dict[str, Any]) -> None:
    assert isinstance(validate_request(args), str)


def test_validate_request_accepts() -> None:
    assert validate_request({"category_id": " 123 ", "names": ["A b"]}) == (123, ["a-b"])


def test_creates_one_channel_per_name() -> None:
    category = FakeCategory(GUILD_ID)
    result = run(FakeClient(category), {"category_id": str(CATEGORY_ID), "names": ["Work", "Home"]})
    assert category.created == ["work", "home"]
    assert not result.get("is_error")


def test_partial_failure_is_reported_per_name() -> None:
    category = FakeCategory(GUILD_ID, fail={"home"})
    result = run(FakeClient(category), {"category_id": str(CATEGORY_ID), "names": ["Work", "Home"]})
    text = result["content"][0]["text"]
    assert category.created == ["work"]
    assert result["is_error"] and "✅ #work" in text and "⚠️ home" in text


def test_category_in_unlisted_server_is_refused() -> None:
    category = FakeCategory(guild_id=999)
    result = run(FakeClient(category), {"category_id": str(CATEGORY_ID), "names": ["x"]})
    assert result["is_error"] and category.created == []


def test_unknown_category_is_refused() -> None:
    result = run(FakeClient(None), {"category_id": str(CATEGORY_ID), "names": ["x"]})
    assert result["is_error"]


def rename(client: Any, *items: tuple[str, str, str]) -> dict[str, Any]:
    renames = [{"channel_id": i, "current_name": c, "new_name": n} for i, c, n in items]
    return asyncio.run(run_rename_channels(client, frozenset({GUILD_ID}), {"renames": renames}))


def delete(client: Any, *items: tuple[str, str]) -> dict[str, Any]:
    channels = [{"channel_id": i, "name": n} for i, n in items]
    return asyncio.run(run_delete_channels(client, frozenset({GUILD_ID}), {"channels": channels}))


def text_of(result: dict[str, Any]) -> str:
    return result["content"][0]["text"]


@pytest.mark.parametrize("args", [
    {"renames": []},
    {"renames": "x"},
    {"renames": [{"channel_id": "abc", "current_name": "a", "new_name": "b"}]},
    {"renames": [{"channel_id": "1", "current_name": "a"}]},
    {"renames": [{"channel_id": "1", "current_name": "a", "new_name": "!!!"}]},
    {"renames": [{"channel_id": "1", "current_name": "a", "new_name": "b"}] * 2},
    {"renames": [{"channel_id": str(i), "current_name": "a", "new_name": "b"} for i in range(MAX_CHANNELS + 1)]},
])
def test_validate_renames_rejects(args: dict[str, Any]) -> None:
    assert isinstance(validate_renames(args), str)


def test_validate_renames_normalizes_new_name() -> None:
    args = {"renames": [{"channel_id": " 5 ", "current_name": "work", "new_name": "💼 Work"}]}
    assert validate_renames(args) == [(5, "work", "💼-work")]


def test_parse_items_for_delete() -> None:
    assert parse_items({"channels": [{"channel_id": "7", "name": "x"}]}, "channels", ("name",)) == [(7, "x")]
    assert isinstance(parse_items({"channels": [{"channel_id": "7"}]}, "channels", ("name",)), str)


def test_same_name_ignores_hash_and_case() -> None:
    assert same_name(" #Work ", "work")
    assert not same_name("home", "work")


def test_renames_each_channel_with_emoji() -> None:
    work, home = FakeTextChannel(1, "work"), FakeTextChannel(2, "home")
    result = rename(ChannelsClient(work, home), ("1", "work", "💼 Work"), ("2", "#home", "🏠 home"))
    assert (work.name, home.name) == ("💼-work", "🏠-home")
    assert not result.get("is_error") and "✅ #work → #💼-work" in text_of(result)


def test_rename_skips_a_channel_whose_name_does_not_match() -> None:
    work = FakeTextChannel(1, "work")
    result = rename(ChannelsClient(work), ("1", "general", "x"))
    assert work.name == "work" and result["is_error"] and "skipped" in text_of(result)


def test_rename_reports_failures_per_channel() -> None:
    ok, bad = FakeTextChannel(1, "ok"), FakeTextChannel(2, "bad", fail=True)
    result = rename(ChannelsClient(ok, bad), ("1", "ok", "ok2"), ("2", "bad", "bad2"))
    assert ok.name == "ok2" and bad.name == "bad"
    assert result["is_error"] and "✅ #ok" in text_of(result) and "⚠️ #bad" in text_of(result)


def test_rename_times_out_on_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(discord_tools, "EDIT_TIMEOUT_SECONDS", 0.01)
    slow = FakeTextChannel(1, "slow", hang=True)
    result = rename(ChannelsClient(slow), ("1", "slow", "fast"))
    assert slow.name == "slow" and result["is_error"] and "rate-limiting" in text_of(result)


@pytest.mark.parametrize("channel", [FakeTextChannel(1, "x", guild_id=999), FakeCategory(GUILD_ID)])
def test_rename_and_delete_refuse_other_servers_and_non_text_channels(channel: Any) -> None:
    client = ChannelsClient(channel) if isinstance(channel, FakeTextChannel) else FakeClient(channel)
    channel_id = str(channel.id)
    assert rename(client, (channel_id, "x", "y"))["is_error"]
    assert delete(client, (channel_id, "x"))["is_error"]
    assert getattr(channel, "deleted", False) is False


def test_deletes_only_matching_channels() -> None:
    old, keep = FakeTextChannel(1, "old"), FakeTextChannel(2, "keep")
    result = delete(ChannelsClient(old, keep), ("1", "old"), ("2", "something-else"))
    assert old.deleted and not keep.deleted
    assert "🗑️ #old deleted" in text_of(result) and "skipped" in text_of(result) and result["is_error"]


def test_delete_failure_is_reported() -> None:
    bad = FakeTextChannel(1, "bad", fail=True)
    result = delete(ChannelsClient(bad), ("1", "bad"))
    assert not bad.deleted and result["is_error"] and "⚠️ #bad" in text_of(result)


def test_unknown_channel_is_refused() -> None:
    assert rename(ChannelsClient(), ("9", "x", "y"))["is_error"]
    assert delete(ChannelsClient(), ("9", "x"))["is_error"]


def set_topics(client: Any, *items: tuple[str, str, str]) -> dict[str, Any]:
    topics = [{"channel_id": i, "current_name": c, "topic": t} for i, c, t in items]
    return asyncio.run(discord_tools.run_set_channel_topics(client, frozenset({GUILD_ID}), {"topics": topics}))


def test_sets_and_clears_topics() -> None:
    work, home = FakeTextChannel(1, "work", topic="old"), FakeTextChannel(2, "home", topic="old")
    result = set_topics(ChannelsClient(work, home), ("1", "#work", " Day job stuff "), ("2", "home", ""))
    assert (work.topic, home.topic) == ("Day job stuff", "")
    assert not result.get("is_error") and "✅ #work: topic set" in text_of(result) and "topic cleared" in text_of(result)


def test_topic_skips_mismatched_name_and_reports_failures() -> None:
    work, bad = FakeTextChannel(1, "work", topic="keep"), FakeTextChannel(2, "bad", fail=True)
    result = set_topics(ChannelsClient(work, bad), ("1", "general", "x"), ("2", "bad", "y"))
    assert work.topic == "keep" and result["is_error"]
    assert "skipped" in text_of(result) and "⚠️ #bad" in text_of(result)


def test_topic_times_out_on_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(discord_tools, "EDIT_TIMEOUT_SECONDS", 0.01)
    slow = FakeTextChannel(1, "slow", hang=True)
    assert "rate-limiting" in text_of(set_topics(ChannelsClient(slow), ("1", "slow", "x")))


@pytest.mark.parametrize("args", [
    {"topics": []},
    {"topics": [{"channel_id": "1", "current_name": "a"}]},
    {"topics": [{"channel_id": "1", "current_name": "a", "topic": "t" * 1025}]},
])
def test_validate_topics_rejects(args: dict[str, Any]) -> None:
    assert isinstance(discord_tools.validate_topics(args), str)


def test_uncached_channel_is_fetched_for_rename() -> None:
    work = FakeTextChannel(1, "work")
    rename(ChannelsClient(fetchable=(work,)), ("1", "work", "💼 work"))
    assert work.name == "💼-work"


def test_server_registers_all_ten_tools() -> None:
    ctx = discord_tools.DiscordContext(lambda: ChannelsClient(), frozenset({GUILD_ID}), ZoneInfo("Europe/Oslo"),
                                       _no_channel)
    names = [t.name for t in _tool_names(ctx)]
    assert sorted(names) == sorted({"create_channels", "rename_channels", "set_channel_topics", "delete_channels",
                                    "list_channels",
                                    "list_archived_threads", "read_messages", "get_reactions", "react", "send_file"})
    assert discord_tools.build_discord_server(ctx)["name"] == SERVER_NAME


async def _no_channel() -> Any:
    raise AssertionError("not used")


def _tool_names(ctx: Any) -> list[Any]:
    return discord_tools._channel_tools(ctx) + discord_tools._read_tools(ctx) + discord_tools._write_tools(ctx)


def test_server_name_matches_permission_prefix() -> None:
    assert SERVER_NAME in OWN_SERVERS
    assert DISCORD == f"mcp__{SERVER_NAME}__"
    assert discord_tools.MAX_CHANNELS == 25
