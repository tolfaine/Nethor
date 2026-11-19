from pathlib import Path

import pytest

from assistant.config import ConfigError, config_warnings, load_config, strip_api_keys

BASE = {
    "ASSISTANT_CALENDAR_ID": "cal@group.calendar.google.com",
    "NOTION_ASSISTANT_PAGE_ID": "a" * 32,
    "NOTION_SESSIONS_PAGE_ID": "b" * 32,
}
DISCORD = {**BASE, "DISCORD_BOT_TOKEN": "x", "ALLOWED_USER_IDS": "123, 456"}


def test_console_mode_needs_no_discord_settings(tmp_path: Path) -> None:
    config = load_config(BASE, console=True, root=tmp_path)
    assert config.discord_bot_token is None
    assert config.assistant_name == "Nethor" and config.tz.key == "Europe/Oslo"
    assert config.model == "claude-sonnet-5-5" and not config.oauth_token_set


def test_discord_mode_parses_allowlist(tmp_path: Path) -> None:
    config = load_config({**DISCORD, "CLAUDE_CODE_OAUTH_TOKEN": "t"}, console=False, root=tmp_path)
    assert config.allowed_user_ids == frozenset({123, 456}) and config.oauth_token_set


def test_missing_vars_are_all_named() -> None:
    with pytest.raises(ConfigError) as err:
        load_config({"ASSISTANT_CALENDAR_ID": "c"}, console=False)
    message = str(err.value)
    for name in ("NOTION_ASSISTANT_PAGE_ID", "NOTION_SESSIONS_PAGE_ID", "DISCORD_BOT_TOKEN", "ALLOWED_USER_IDS"):
        assert name in message
    assert "ASSISTANT_CALENDAR_ID" not in message


def test_blank_values_count_as_missing() -> None:
    with pytest.raises(ConfigError, match="ASSISTANT_CALENDAR_ID"):
        load_config({**BASE, "ASSISTANT_CALENDAR_ID": "  "}, console=True)


def test_bad_user_ids_named() -> None:
    with pytest.raises(ConfigError, match="ALLOWED_USER_IDS"):
        load_config({**DISCORD, "ALLOWED_USER_IDS": "123,bob"}, console=False)


def test_bad_time_zone_named() -> None:
    with pytest.raises(ConfigError, match="ASSISTANT_TZ"):
        load_config({**BASE, "ASSISTANT_TZ": "Mars/Olympus"}, console=True)


def test_api_keys_are_stripped() -> None:
    environ = {"ANTHROPIC_API_KEY": "k", "OTHER": "1"}
    assert strip_api_keys(environ) == ["ANTHROPIC_API_KEY"]
    assert environ == {"OTHER": "1"}


def test_guild_and_forum_ids_default_off(tmp_path: Path) -> None:
    config = load_config(DISCORD, console=False, root=tmp_path)
    assert config.allowed_guild_ids == frozenset() and config.forum_channel_ids == frozenset()


def test_guild_and_forum_ids_parse(tmp_path: Path) -> None:
    env = {**DISCORD, "ALLOWED_GUILD_IDS": "1; 2", "FORUM_CHANNEL_IDS": "30,40"}
    config = load_config(env, console=False, root=tmp_path)
    assert config.allowed_guild_ids == frozenset({1, 2}) and config.forum_channel_ids == frozenset({30, 40})


@pytest.mark.parametrize("name", ["ALLOWED_GUILD_IDS", "FORUM_CHANNEL_IDS"])
def test_bad_guild_or_forum_ids_named(name: str) -> None:
    with pytest.raises(ConfigError, match=name):
        load_config({**DISCORD, name: "12,#nethor"}, console=False)


def test_memory_page_id_is_optional_and_warned_about(tmp_path: Path) -> None:
    config = load_config(BASE, console=True, root=tmp_path)
    assert config.targets.notion_memory_page_id == ""
    assert any("NOTION_MEMORY_PAGE_ID" in w and "memory is off" in w for w in config_warnings(config))


def test_memory_page_id_parses(tmp_path: Path) -> None:
    dashed = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
    config = load_config({**BASE, "NOTION_MEMORY_PAGE_ID": dashed}, console=True, root=tmp_path)
    assert config.targets.notion_memory_page_id == dashed
    assert not any("NOTION_MEMORY_PAGE_ID" in w for w in config_warnings(config))


def test_bad_memory_page_id_named() -> None:
    with pytest.raises(ConfigError, match="NOTION_MEMORY_PAGE_ID"):
        load_config({**BASE, "NOTION_MEMORY_PAGE_ID": "Memory"}, console=True)
