from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any

import discord
import pytest

from assistant.bot import AssistantBot, conversation_key_for
from assistant.commands import NOT_ALLOWED, NOT_HERE, sync_commands
from assistant.pending import PendingQueue
from assistant.sessions import Origin, SessionManager, SessionRecord, SessionStore
from test_private_channel import THREAD_ID, hidden, thread_in
from test_routing import CONFIG, GUILD, OTHER_GUILD, OWNER, STRANGER, dm_channel, make_bot, text_channel, thread
from test_sessions import OSLO, Clock, FakeAgent, ts

NAMES = ["context", "delete", "effort", "fast", "model", "new", "remote", "status"]


def known(*ids: int) -> Any:
    return lambda channel_id: channel_id in ids


# --- key resolution ------------------------------------------------------------------------------------------------

def test_dm_command_acts_on_the_dm_conversation() -> None:
    got, reason = conversation_key_for(dm_channel(), OWNER, None, CONFIG, known())
    assert got is not None and (got.key, got.mode, reason) == (f"dm:{OWNER}", "private", "dm")


def test_forum_post_command_needs_no_earlier_message() -> None:
    got, _ = conversation_key_for(thread(tid=700), OWNER, GUILD, CONFIG, known())
    assert got is not None and got.key == "thread:700" and got.topic is not None


def test_nethor_thread_command_rechecks_privacy() -> None:
    parent = hidden()
    private = thread_in(parent)
    got, _ = conversation_key_for(private, OWNER, GUILD, CONFIG, known(THREAD_ID))
    assert got is not None and (got.key, got.mode) == (f"thread:{THREAD_ID}", "private")
    parent.permissions_for = lambda obj: NS(view_channel=True)
    got, _ = conversation_key_for(private, OWNER, GUILD, CONFIG, known(THREAD_ID))
    assert got is not None and (got.key, got.mode) == (f"pthread:{THREAD_ID}", "public")


def test_public_nethor_thread_is_pthread() -> None:
    got, _ = conversation_key_for(thread(tid=710, parent=601), OWNER, GUILD, CONFIG, known(710))
    assert got is not None and got.key == "pthread:710"


@pytest.mark.parametrize(("channel", "conversations"), [(text_channel(), (800,)), (thread_in(hidden()), ())])
def test_plain_channel_or_foreign_thread_has_no_conversation(channel: NS, conversations: tuple[int, ...]) -> None:
    got, reason = conversation_key_for(channel, OWNER, GUILD, CONFIG, known(*conversations))
    assert got is None and reason == "command outside a conversation"


def test_other_server_and_strangers_are_refused() -> None:
    got, reason = conversation_key_for(thread(tid=710, parent=601), OWNER, OTHER_GUILD, CONFIG, known(710))
    assert got is None and "ALLOWED_GUILD_IDS" in reason
    for channel in (dm_channel(), thread(tid=700), thread(tid=710, parent=601)):
        assert conversation_key_for(channel, STRANGER, GUILD, CONFIG, known(710))[0] is None
    assert conversation_key_for(None, OWNER, GUILD, CONFIG, known())[1] == "command in an unknown channel"


# --- registration ----------------------------------------------------------------------------------------------------

def test_commands_register_for_dms_globally_and_per_server_without_connecting() -> None:
    bot, _ = make_bot({}, {})
    global_cmds = {c.name: c for c in bot.tree.get_commands()}
    guild_cmds = {c.name: c for c in bot.tree.get_commands(guild=discord.Object(GUILD))}
    assert sorted(global_cmds) == sorted(guild_cmds) == NAMES
    assert global_cmds["status"].to_dict(bot.tree)["contexts"] == [discord.app_commands.AppCommandContext.DM_CHANNEL]
    assert guild_cmds["status"].to_dict(bot.tree)["contexts"] == [discord.app_commands.AppCommandContext.GUILD]


def test_command_options_and_choices() -> None:
    bot, _ = make_bot({}, {})
    payload = {c.name: c.to_dict(bot.tree) for c in bot.tree.get_commands()}
    model = payload["model"]["options"][0]
    assert model["required"] and [c["value"] for c in model["choices"]] == [
        "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-2"]
    assert [c["value"] for c in payload["effort"]["options"][0]["choices"]] == ["low", "medium", "high", "xhigh", "max"]
    assert "medium (default)" in [c["name"] for c in payload["effort"]["options"][0]["choices"]]
    assert [c["value"] for c in payload["fast"]["options"][0]["choices"]] == ["on", "off"]
    assert payload["status"]["options"] == [] and payload["new"]["options"] == []


async def test_sync_failure_is_logged_and_does_not_raise(caplog) -> None:
    calls: list[Any] = []

    async def sync(guild: Any = None) -> list[NS]:
        calls.append(guild)
        if guild is not None:
            raise discord.HTTPException(NS(status=403, reason="Forbidden"), "Missing Access")
        return [NS(name="status")]

    with caplog.at_level(logging.INFO):
        await sync_commands(NS(sync=sync), [GUILD])  # type: ignore[arg-type]
    assert calls[0].id == GUILD and calls[1] is None
    assert f"slash command sync failed guild={GUILD}" in caplog.text and "synced 1 slash commands global" in caplog.text


# --- handler -------------------------------------------------------------------------------------------------------

class FakeResponse:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.deferred: dict[str, Any] | None = None

    async def send_message(self, content: str, **kwargs: Any) -> None:
        self.sent.append((content, kwargs))

    async def defer(self, **kwargs: Any) -> None:
        self.deferred = kwargs


class FakeFollowup:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def send(self, content: str, **kwargs: Any) -> None:
        self.sent.append((content, kwargs))


def interaction(channel: Any, user: int = OWNER, guild: int | None = GUILD) -> NS:
    return NS(user=NS(id=user), channel=channel, channel_id=channel.id, guild_id=guild, response=FakeResponse(),
              followup=FakeFollowup())


def reply_of(inter: NS) -> str:
    assert inter.followup.sent and inter.followup.sent[-1][1] == {"ephemeral": True}
    return inter.followup.sent[-1][0]


@pytest.fixture
def live(tmp_path: Path) -> tuple[AssistantBot, SessionManager, FakeAgent]:
    agent = FakeAgent()
    store, queue = SessionStore(tmp_path / "state.json"), PendingQueue(tmp_path / "pending.json")
    manager = SessionManager(agent, store, queue, OSLO, "sessions", clock=Clock(ts(2026, 11, 18, 12)),
                             default_model="claude-sonnet-5-5")
    bot, _ = make_bot({}, {})
    bot.manager = manager
    return bot, manager, agent


def handler_of(bot: AssistantBot) -> Any:
    command = bot.tree.get_command("status")
    assert command is not None
    return command.callback


async def test_stranger_is_refused_ephemerally_and_logged(live, caplog) -> None:
    bot, _, _ = live
    inter = interaction(dm_channel(), user=STRANGER)
    await handler_of(bot)(inter)
    assert inter.response.sent == [(NOT_ALLOWED, {"ephemeral": True})] and inter.response.deferred is None
    assert f"command refused command=status user={STRANGER}" in caplog.text


async def test_plain_channel_gets_use_it_inside_a_thread(live) -> None:
    bot, _, _ = live
    inter = interaction(text_channel())
    await handler_of(bot)(inter)
    assert inter.response.deferred == {"ephemeral": True, "thinking": True} and reply_of(inter) == NOT_HERE


async def test_status_in_a_dm(live, caplog) -> None:
    bot, manager, _ = live
    await manager.handle(f"dm:{OWNER}", "hi", Origin("private", 10, OWNER))
    inter = interaction(dm_channel(), guild=None)
    with caplog.at_level(logging.INFO):
        await handler_of(bot)(inter)
    assert reply_of(inter).startswith("Sonnet 5.5 · medium · fast off · context n/a")
    assert f"command=status key=dm:{OWNER} user={OWNER}" in caplog.text


async def test_model_fast_and_new_in_a_nethor_thread(live, caplog) -> None:
    bot, manager, agent = live
    nethor = thread_in(hidden())
    key, origin = f"thread:{THREAD_ID}", Origin("private", THREAD_ID, OWNER)
    await manager.handle(key, "hi", origin)
    model_cmd, fast_cmd, new_cmd = (bot.tree.get_command(n) for n in ("model", "fast", "new"))
    inter = interaction(nethor)
    with caplog.at_level(logging.INFO):
        await fast_cmd.callback(inter, "on")  # type: ignore[union-attr]
    assert "Switched to Opus 5.5" in reply_of(inter) and agent.applied[-1][2].fast
    assert f"command=fast key={key} user={OWNER} change=fast=on" in caplog.text
    inter = interaction(nethor)
    await model_cmd.callback(inter, "claude-fable-5-2")  # type: ignore[union-attr]
    assert "Fable uses far more" in reply_of(inter)
    inter = interaction(nethor)
    await new_cmd.callback(inter)  # type: ignore[union-attr]
    assert reply_of(inter).startswith("🆕") and manager.settings(key).model is None
    assert manager.record(key).channel_id == THREAD_ID  # the thread stays a conversation


async def test_command_failure_is_logged_and_answered(live, caplog) -> None:
    bot, manager, _ = live

    async def boom(key: str) -> Any:
        raise RuntimeError("store broke")

    manager.status = boom  # type: ignore[method-assign]
    inter = interaction(dm_channel(), guild=None)
    await handler_of(bot)(inter)
    assert reply_of(inter).startswith("⚠️") and f"command failed command=status key=dm:{OWNER}" in caplog.text


async def test_uncached_thread_is_fetched(live) -> None:
    bot, manager, _ = live
    nethor = thread_in(hidden())
    manager._store.put(f"thread:{THREAD_ID}", SessionRecord("s", channel_id=THREAD_ID, owner_id=OWNER))
    fetched: list[int] = []

    async def fetch_channel(cid: int) -> Any:
        fetched.append(cid)
        return nethor

    bot.fetch_channel = fetch_channel  # type: ignore[method-assign]
    inter = interaction(discord.PartialMessageable(state=bot._connection, id=THREAD_ID))
    await bot.tree.get_command("context").callback(inter)  # type: ignore[union-attr]
    assert fetched == [THREAD_ID] and reply_of(inter).startswith("Context n/a")
