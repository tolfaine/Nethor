from __future__ import annotations

import logging
from zoneinfo import ZoneInfo

import discord
import pytest

import assistant.__main__ as entry
from assistant.config import Config
from assistant.permissions import Targets

TARGETS = Targets("c", "a" * 32, "b" * 32)
CONFIG = Config("token", False, frozenset({1}), "Nethor", ZoneInfo("Europe/Oslo"), "m", TARGETS)


def test_missing_members_intent_exits_2_with_portal_hint(caplog) -> None:
    with caplog.at_level(logging.ERROR):
        assert entry.fatal_exit_code(discord.PrivilegedIntentsRequired(None)) == 2
    assert "Server Members Intent" in caplog.text and "Developer Portal" in caplog.text


def test_other_fatal_errors_exit_1_with_traceback(caplog) -> None:
    assert entry.fatal_exit_code(RuntimeError("boom")) == 1
    assert "fatal error" in caplog.text and "RuntimeError: boom" in caplog.text


@pytest.mark.parametrize(("error", "code"), [(discord.PrivilegedIntentsRequired(None), 2), (RuntimeError("x"), 1)])
def test_main_maps_discord_start_errors(monkeypatch: pytest.MonkeyPatch, error: Exception, code: int) -> None:
    async def failing_discord(config: Config) -> None:
        raise error

    monkeypatch.setattr(entry, "load_from_environment", lambda console: (CONFIG, []))
    monkeypatch.setattr(entry, "setup_logging", lambda logs_dir, level: None)
    monkeypatch.setattr(entry, "run_discord", failing_discord)
    assert entry.main([]) == code
