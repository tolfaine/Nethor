from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from assistant.agent import TurnResult
from assistant.pending import PendingQueue
from assistant.sessions import (
    AFTER_TURN, IDLE_SECONDS, NEW_COMMAND, Origin, SessionManager, SessionRecord, SessionStore, settings_of,
)
from assistant.settings import (
    DEFAULT_EFFORT, FABLE_WARNING, Change, ContextSnapshot, ConversationSettings, Effective, FastInfo, SettingError,
    StatusView, apply_change, describe_fast, format_age, format_context, format_status, format_tokens,
)
from test_sessions import HOUR, OSLO, Clock, FakeAgent, ts

SONNET, OPUS, HAIKU, FABLE = "claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5", "claude-fable-5-2"
DM = Origin("private", 10, 111)
POST = Origin("private", 700, 111)
PUBLIC = Origin("public", 43, 111)
USAGE = {"totalTokens": 46_000, "maxTokens": 1_000_000, "rawMaxTokens": 1_000_000, "percentage": 4.6,
         "model": SONNET, "categories": [
             {"name": "Messages", "tokens": 30_000, "color": "x"},
             {"name": "System tools", "tokens": 12_000, "color": "x"},
             {"name": "Free space", "tokens": 900_000, "color": "x"},
             {"name": "MCP tools", "tokens": 5_000, "color": "x", "isDeferred": True},
             {"name": "System prompt", "tokens": 4_000, "color": "x"},
             {"name": "Empty", "tokens": 0, "color": "x"}]}
SNAPSHOT = ContextSnapshot.from_usage(USAGE)


def change(kind: str, value: str) -> Change:
    return Change(kind, value)  # type: ignore[arg-type]


# --- pure settings ---------------------------------------------------------------------------------------------------

def test_defaults_come_from_config_model_and_medium_effort() -> None:
    assert ConversationSettings().effective(SONNET) == Effective(SONNET, "medium", False)
    assert ConversationSettings(HAIKU, "max").effective(SONNET) == Effective(HAIKU, "max", False)
    assert DEFAULT_EFFORT == "medium"


@pytest.mark.parametrize(("kind", "value"), [("model", "gpt-9"), ("effort", "extreme"), ("fast", "maybe"),
                                             ("colour", "red")])
def test_bad_values_are_refused(kind: str, value: str) -> None:
    with pytest.raises(SettingError):
        apply_change(ConversationSettings(), change(kind, value), SONNET)


def test_model_and_effort_changes() -> None:
    new, note = apply_change(ConversationSettings(), change("model", OPUS), SONNET)
    assert new == ConversationSettings(OPUS, None, False) and note == "Model: Opus 5.5."
    new, note = apply_change(new, change("effort", "xhigh"), SONNET)
    assert new == ConversationSettings(OPUS, "xhigh", False) and note == "Effort: xhigh."


def test_fable_reply_warns_about_limits() -> None:
    _, note = apply_change(ConversationSettings(), change("model", FABLE), SONNET)
    assert note.splitlines() == ["Model: Fable 5.2.", FABLE_WARNING]


def test_fast_on_a_model_without_fast_switches_to_opus() -> None:
    new, note = apply_change(ConversationSettings(), change("fast", "on"), SONNET)
    assert new == ConversationSettings(OPUS, None, True)
    assert "Switched to Opus 5.5" in note and "standard speed" in note
    haiku, _ = apply_change(ConversationSettings(HAIKU), change("fast", "on"), SONNET)
    assert haiku.model == OPUS


@pytest.mark.parametrize("model", [OPUS, FABLE])
def test_fast_on_keeps_a_model_that_has_fast(model: str) -> None:
    new, note = apply_change(ConversationSettings(model), change("fast", "on"), SONNET)
    assert new == ConversationSettings(model, None, True) and "Switched" not in note


def test_fast_on_pins_the_default_model_when_it_has_fast() -> None:
    new, _ = apply_change(ConversationSettings(), change("fast", "on"), OPUS)
    assert new == ConversationSettings(OPUS, None, True)


def test_fast_off_keeps_the_model() -> None:
    new, note = apply_change(ConversationSettings(OPUS, "high", True), change("fast", "off"), SONNET)
    assert new == ConversationSettings(OPUS, "high", False) and note == "Fast mode: off."


def test_model_without_fast_turns_fast_off_and_says_so() -> None:
    new, note = apply_change(ConversationSettings(OPUS, None, True), change("model", SONNET), SONNET)
    assert new == ConversationSettings(SONNET, None, False) and "Fast mode is now off" in note
    kept, _ = apply_change(ConversationSettings(OPUS, None, True), change("model", FABLE), SONNET)
    assert kept.fast


# --- formatting ------------------------------------------------------------------------------------------------------

def test_token_and_age_formatting() -> None:
    assert [format_tokens(n) for n in (950, 46_000, 200_000, 1_000_000, 1_500_000)] == ["950", "46k", "200k", "1M",
                                                                                         "1.5M"]
    assert [format_age(s) for s in (30, 300, 2 * HOUR + 60, 3 * 86400)] == ["just now", "5 min ago", "2 h ago",
                                                                            "3 d ago"]


def test_context_snapshot_keeps_the_biggest_used_categories() -> None:
    assert SNAPSHOT == ContextSnapshot(46_000, 1_000_000, 4.6, (("Messages", 30_000), ("System tools", 12_000),
                                                                ("System prompt", 4_000)))


def test_fast_description_never_claims_a_fast_reply_without_usage_saying_so() -> None:
    assert describe_fast(False, FastInfo("on", None, "fast")) == "fast off"
    assert describe_fast(True, FastInfo()) == "fast on (no reply yet)"
    assert describe_fast(True, FastInfo("on", None, "standard")) == "fast on (last reply: standard)"
    assert describe_fast(True, FastInfo("on", None, "fast")) == "fast on (last reply: fast)"
    assert describe_fast(True, FastInfo("off", "model_not_allowed")) == "fast on, but the CLI says off: model_not_allowed"


def view(**kw: object) -> StatusView:
    base: dict[str, object] = {"effective": Effective(SONNET, "medium", False), "fast_info": FastInfo(),
                               "context": SNAPSHOT, "freshness": "live", "mode": "private",
                               "started_at": ts(2026, 11, 18, 10)}
    base.update(kw)
    return StatusView(**base)  # type: ignore[arg-type]


NOON = ts(2026, 11, 18, 12)


def test_status_line_live() -> None:
    assert format_status(view(), NOON) == ("Sonnet 5.5 · medium · fast off · context 5% (46k/1M) · private · "
                                           "started 2 h ago")


def test_status_paused_busy_and_new() -> None:
    paused = format_status(view(freshness="paused"), NOON)
    assert paused.splitlines()[1] == "(paused; refreshed on your next message)"
    assert "a reply is running" in format_status(view(freshness="busy"), NOON)
    fresh = format_status(view(context=None, started_at=None, freshness="paused"), NOON)
    assert fresh == "Sonnet 5.5 · medium · fast off · context n/a (no reply yet) · private · not started yet"
    fast = format_status(view(effective=Effective(OPUS, "high", True), fast_info=FastInfo("on", None, "standard")),
                         NOON)
    assert fast.startswith("Opus 5.5 · high · fast on (last reply: standard) · ")


def test_context_breakdown() -> None:
    assert format_context(view(freshness="paused")).splitlines() == [
        "Context 5% (46k/1M) · Sonnet 5.5", "- Messages: 30k", "- System tools: 12k", "- System prompt: 4k",
        "(paused; refreshed on your next message)"]
    assert format_context(view(context=None)) == "Context n/a (no reply yet) · Sonnet 5.5"


# --- manager -------------------------------------------------------------------------------------------------------

@pytest.fixture
def env(tmp_path: Path) -> tuple[SessionManager, FakeAgent, Clock, SessionStore]:
    agent, clock = FakeAgent(), Clock(ts(2026, 11, 18, 12))
    store, queue = SessionStore(tmp_path / "state.json"), PendingQueue(tmp_path / "pending.json")
    manager = SessionManager(agent, store, queue, OSLO, "sessions-page", clock=clock, memory_page_id="memory-page",
                             default_model=SONNET)
    return manager, agent, clock, store


async def nothing(key: str, text: str) -> None:
    raise AssertionError("nothing to deliver")


async def test_settings_reach_the_next_turn_and_persist(env, tmp_path: Path) -> None:
    manager, agent, _, store = env
    await manager.handle("dm:111", "hi", DM)
    assert await manager.apply_setting("dm:111", change("effort", "high"), DM) == "Effort: high."
    assert agent.applied == [("dm:111", "sess-1", ConversationSettings(None, "high", False))]
    await manager.handle("dm:111", "again", DM)
    assert agent.turn_settings[-1] == ConversationSettings(None, "high", False)
    assert settings_of(SessionStore(tmp_path / "state.json").get("dm:111")) == ConversationSettings(None, "high")


async def test_setting_before_the_first_message_is_kept(env) -> None:
    manager, agent, _, store = env
    await manager.apply_setting("thread:700", change("model", OPUS), POST)
    assert store.get("thread:700").channel_id == 700
    await manager.handle("thread:700", "hi", POST, "topic")
    assert agent.turn_settings[0] == ConversationSettings(OPUS) and "topic" in agent.calls[0][1]


@pytest.mark.parametrize(("key", "origin"), [("dm:111", DM), ("thread:700", POST), ("pthread:43", PUBLIC)])
async def test_new_resets_settings(env, key: str, origin: Origin) -> None:
    manager, agent, _, store = env
    await manager.handle(key, "hi", origin)
    await manager.apply_setting(key, change("model", HAIKU), origin)
    assert await manager.handle(key, NEW_COMMAND, origin) == "🆕 Fresh session."
    assert settings_of(store.get(key)) == ConversationSettings()
    await manager.handle(key, "again", origin)
    assert agent.turn_settings[-1] == ConversationSettings()


async def test_slash_new_resets_even_without_a_session(env) -> None:
    manager, _, _, store = env
    await manager.apply_setting("dm:111", change("fast", "on"), DM)
    assert await manager.new_conversation("dm:111", DM) == "🆕 Fresh session."
    assert settings_of(store.get("dm:111")) == ConversationSettings()


async def test_daily_dm_reset_starts_on_defaults(env) -> None:
    manager, agent, clock, _ = env
    await manager.handle("dm:111", "hi", DM)
    await manager.apply_setting("dm:111", change("model", OPUS), DM)
    clock.now = ts(2026, 11, 19, 5)
    await manager.tick(nothing)
    await manager.handle("dm:111", "morning", DM)
    assert agent.turn_settings[-1] == ConversationSettings()


async def test_public_reset_starts_on_defaults(env) -> None:
    manager, agent, clock, _ = env
    await manager.handle("pub:8", "hi", Origin("public", 8, 111))
    await manager.apply_setting("pub:8", change("effort", "low"), Origin("public", 8, 111))
    clock.now += IDLE_SECONDS
    await manager.tick(nothing)
    await manager.handle("pub:8", "again", Origin("public", 8, 111))
    assert agent.turn_settings[-1] == ConversationSettings()


async def test_suspend_idle_close_and_restart_keep_settings(env, tmp_path: Path) -> None:
    manager, agent, clock, _ = env
    await manager.handle("thread:700", "hi", POST)
    await manager.apply_setting("thread:700", change("model", OPUS), POST)
    clock.now += 31 * 60
    await manager.tick(nothing)  # 30-min process close
    clock.now = ts(2026, 11, 19, 5)
    await manager.tick(nothing)  # idle suspend with wrap-up
    assert agent.closed and agent.turn_settings[-1] == ConversationSettings(OPUS)  # the wrap-up used it too
    restarted = SessionManager(agent, SessionStore(tmp_path / "state.json"), PendingQueue(tmp_path / "p.json"), OSLO,
                               "sessions-page", clock=clock, default_model=SONNET)
    await restarted.handle("thread:700", "back", POST)
    assert agent.turn_settings[-1] == ConversationSettings(OPUS) and agent.calls[-1][2] == "sess-1"


async def test_command_during_a_turn_waits_for_the_lock(env) -> None:
    manager, agent, _, store = env
    await manager.handle("dm:111", "hi", DM)
    lock = manager.lock("dm:111")
    await lock.acquire()
    reply = await manager.apply_setting("dm:111", change("model", OPUS), DM)
    assert reply == f"Model: Opus 5.5.\n{AFTER_TURN}"
    await asyncio.sleep(0)
    assert agent.applied == [] and store.get("dm:111").model is None
    lock.release()
    await asyncio.sleep(0.01)
    assert store.get("dm:111").model == OPUS and agent.applied[0][2] == ConversationSettings(OPUS)


async def test_new_during_a_turn_waits_for_the_lock(env) -> None:
    manager, agent, _, store = env
    await manager.handle("dm:111", "hi", DM)
    await manager.lock("dm:111").acquire()
    assert (await manager.new_conversation("dm:111", DM)).endswith(AFTER_TURN)
    assert store.get("dm:111").session_id == "sess-1"
    manager.lock("dm:111").release()
    await asyncio.sleep(0.01)
    assert store.get("dm:111").session_id is None


async def test_bad_setting_raises_before_waiting(env) -> None:
    manager, _, _, _ = env
    with pytest.raises(SettingError):
        await manager.apply_setting("dm:111", change("effort", "huge"), DM)


async def test_turn_info_is_stored(env) -> None:
    manager, agent, _, store = env
    agent.results = [TurnResult("ok", "s1", fast_state="on", speed="standard", context=SNAPSHOT)]
    await manager.handle("dm:111", "hi", DM)
    record = store.get("dm:111")
    assert (record.fast_state, record.fast_reason, record.last_speed) == ("on", None, "standard")
    assert (record.context_pct, record.context_tokens, record.context_max) == (4.6, 46_000, 1_000_000)
    assert record.context_categories == {"Messages": 30_000, "System tools": 12_000, "System prompt": 4_000}


async def test_model_or_fast_change_clears_the_old_fast_report(env) -> None:
    manager, agent, _, store = env
    agent.results = [TurnResult("ok", "s1", fast_state="on", speed="standard")]
    await manager.handle("dm:111", "hi", DM)
    await manager.apply_setting("dm:111", change("effort", "high"), DM)
    assert store.get("dm:111").fast_state == "on"
    await manager.apply_setting("dm:111", change("fast", "on"), DM)
    record = store.get("dm:111")
    assert (record.fast_state, record.last_speed, record.model, record.fast) == (None, None, OPUS, True)


async def test_status_live_paused_and_busy(env) -> None:
    manager, agent, _, store = env
    agent.results = [TurnResult("ok", "s1", context=SNAPSHOT)]
    await manager.handle("dm:111", "hi", DM)
    agent.context = ContextSnapshot(60_000, 1_000_000, 6.0)
    live = await manager.status("dm:111")
    assert live.freshness == "live" and live.context == agent.context and store.get("dm:111").context_tokens == 60_000
    agent.live.clear()
    paused = await manager.status("dm:111")
    assert paused.freshness == "paused" and paused.context is not None and paused.context.total == 60_000
    agent.live.add("dm:111")
    async with manager.lock("dm:111"):
        busy = await manager.status("dm:111")
    assert busy.freshness == "busy"
    agent.context = None
    assert (await manager.status("dm:111")).freshness == "stale"


async def test_status_of_a_new_conversation(env) -> None:
    manager, _, _, _ = env
    status = await manager.status("thread:9")
    assert status.started_at is None and status.context is None and status.effective == Effective(SONNET, "medium",
                                                                                                  False)


def test_old_state_files_load_with_default_settings(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    v4 = {"thread:7": {"session_id": "s", "started_at": 1.0, "last_active": 2.0, "mode": "private", "channel_id": 7,
                       "owner_id": 111, "wrapped_up": False}}
    path.write_text(json.dumps({"users": v4}), encoding="utf-8")
    record = SessionStore(path).get("thread:7")
    assert settings_of(record) == ConversationSettings() and record.context_tokens is None
    assert record == SessionRecord("s", 1.0, 2.0, "private", 7, 111)
