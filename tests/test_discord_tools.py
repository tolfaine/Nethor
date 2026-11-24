import asyncio
from types import SimpleNamespace
from typing import Any

import discord
import pytest

from assistant import discord_tools
from assistant.discord_tools import (
    MAX_CHANNELS, SERVER_NAME, normalize_name, run_create_channels, unique_names, validate_request,
)
from assistant.permissions import DISCORD, OWN_SERVERS

GUILD_ID = 111
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


def run(client: Any, args: dict[str, Any]) -> dict[str, Any]:
    return asyncio.run(run_create_channels(client, frozenset({GUILD_ID}), args))


def test_normalize_name() -> None:
    assert normalize_name("  My Big  Project! ") == "my-big-project"
    assert normalize_name("a" * 150) == "a" * 100
    assert normalize_name("!!!") == ""


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


def test_server_name_matches_permission_prefix() -> None:
    assert SERVER_NAME in OWN_SERVERS
    assert DISCORD == f"mcp__{SERVER_NAME}__"
    assert discord_tools.MAX_CHANNELS == 25
