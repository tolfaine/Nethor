import json
from pathlib import Path

import pytest

from assistant.agent import TurnResult
from assistant.pending import PendingQueue
from assistant.sessions import (
    CLIENT_IDLE_SECONDS, IDLE_SECONDS, NEW_COMMAND, Expiry, Origin, SessionManager, SessionRecord, SessionStore,
    WRAP_UP, idle_clients, session_expiry, wrap_up_prompt,
)
from test_sessions import HOUR, OSLO, Clock, FakeAgent, ts

POST = Origin("private", 700, 111)
GENERAL = Origin("public", 800, 111)
TOPIC = 'Conversation topic (this Discord forum post): "Plan Lisbon trip"'
CONTEXT = "<<<UNTRUSTED CHANNEL CONTENT: ...>>>\nAna: hi\n<<<END UNTRUSTED CHANNEL CONTENT>>>"


def rec(started: float, last: float, **kw: object) -> SessionRecord:
    return SessionRecord("s1", started_at=started, last_active=last, **kw)  # type: ignore[arg-type]


def test_forum_has_no_daily_reset() -> None:
    record = rec(ts(2026, 11, 17, 22), ts(2026, 11, 18, 3, 30))
    assert session_expiry("thread:7", record, ts(2026, 11, 18, 4, 30), OSLO) is None
    assert session_expiry("dm:1", record, ts(2026, 11, 18, 4, 30), OSLO) == (Expiry.ROLLOVER, "daily")


def test_forum_idle_suspends_once() -> None:
    now = ts(2026, 11, 18, 12)
    record = rec(now - 7 * HOUR, now - IDLE_SECONDS)
    assert session_expiry("thread:7", record, now, OSLO) == (Expiry.SUSPEND, "idle")
    record.wrapped_up = True
    assert session_expiry("thread:7", record, now + 10 * HOUR, OSLO) is None
    assert session_expiry("thread:7", SessionRecord(), now, OSLO) is None


def test_public_drops_on_idle_and_daily() -> None:
    now = ts(2026, 11, 18, 12)
    assert session_expiry("pub:8", rec(now - 7 * HOUR, now - IDLE_SECONDS), now, OSLO) == (Expiry.DROP, "idle")
    assert session_expiry("pub:8", rec(ts(2026, 11, 18, 3), now - 60), now, OSLO) == (Expiry.DROP, "daily")
    assert session_expiry("pub:8", rec(now - HOUR, now - 60), now, OSLO) is None


def test_private_channel_lives_like_a_dm() -> None:
    now = ts(2026, 11, 18, 12)
    assert session_expiry("priv:9", rec(now - 7 * HOUR, now - IDLE_SECONDS), now, OSLO) == (Expiry.ROLLOVER, "idle")
    assert session_expiry("priv:9", rec(ts(2026, 11, 18, 3), now - 60), now, OSLO) == (Expiry.ROLLOVER, "daily")
    assert session_expiry("priv:9", rec(now - HOUR, now - 60), now, OSLO) is None

def test_dm_lifetime_unchanged_across_spring_forward() -> None:
    record = rec(ts(2026, 3, 28, 23), ts(2026, 3, 29, 3, 30))
    assert session_expiry("dm:1", record, ts(2026, 3, 29, 3, 50), OSLO) is None
    assert session_expiry("dm:1", record, ts(2026, 3, 29, 4, 1), OSLO) == (Expiry.ROLLOVER, "daily")
    assert session_expiry("console", record, ts(2026, 3, 29, 4, 1), OSLO) == (Expiry.ROLLOVER, "daily")


def test_idle_clients_pure() -> None:
    now = 10_000.0
    records = {"a": SessionRecord(last_active=now - CLIENT_IDLE_SECONDS - 1), "b": SessionRecord(last_active=now - 60)}
    assert idle_clients(["a", "b", "gone"], lambda k: records.get(k, SessionRecord()), now) == ["a", "gone"]


@pytest.fixture
def env(tmp_path: Path) -> tuple[SessionManager, FakeAgent, Clock, SessionStore, PendingQueue]:
    agent, clock = FakeAgent(), Clock(ts(2026, 11, 18, 12))
    store, queue = SessionStore(tmp_path / "state.json"), PendingQueue(tmp_path / "pending.json")
    return SessionManager(agent, store, queue, OSLO, "sessions-page", clock=clock, memory_page_id="memory-page"), agent, clock, store, queue


async def nothing(key: str, text: str) -> None:
    raise AssertionError("nothing to deliver")


async def test_forum_idle_wraps_up_suspends_and_resumes(env) -> None:
    manager, agent, clock, store, _ = env
    await manager.handle("thread:7", "flights?", POST, TOPIC)
    clock.now = ts(2026, 11, 19, 5)  # past 04:00 and 6 h idle
    await manager.tick(nothing)
    assert WRAP_UP in agent.calls[1][1] and agent.closed == ["thread:7"]
    record = store.get("thread:7")
    assert record.session_id == "sess-1" and record.wrapped_up and record.channel_id == 700
    clock.now += 5 * 3600
    await manager.tick(nothing)
    assert len(agent.calls) == 2  # no second wrap-up
    await manager.handle("thread:7", "and hotels?", POST, TOPIC)
    assert agent.calls[2][2] == "sess-1" and TOPIC not in agent.calls[2][1]
    assert not store.get("thread:7").wrapped_up


async def test_forum_topic_only_on_first_turn_and_after_new(env) -> None:
    manager, agent, clock, store, _ = env
    await manager.handle("thread:7", "flights?", POST, TOPIC)
    assert agent.calls[0][1].startswith("[Wed 2026-11-18 12:00 Europe/Oslo] " + TOPIC)
    assert agent.calls[0][1].endswith("\n\nflights?")
    await manager.handle("thread:7", "hotels?", POST, TOPIC)
    assert TOPIC not in agent.calls[1][1]
    assert await manager.handle("thread:7", NEW_COMMAND, POST, TOPIC) == "🆕 Fresh session."
    assert WRAP_UP in agent.calls[2][1] and store.get("thread:7").session_id is None
    await manager.handle("thread:7", "restart", POST, TOPIC)
    assert TOPIC in agent.calls[3][1] and agent.calls[3][2] is None


async def test_public_idle_and_new_drop_without_wrap_up(env) -> None:
    manager, agent, clock, store, _ = env
    await manager.handle("pub:8", "weather?", GENERAL, None, CONTEXT)
    clock.now += IDLE_SECONDS
    await manager.tick(nothing)
    assert len(agent.calls) == 1 and "pub:8" in agent.closed and store.get("pub:8").session_id is None
    await manager.handle("pub:8", "again", GENERAL, None, CONTEXT)
    assert await manager.handle("pub:8", NEW_COMMAND, GENERAL) == "🆕 Fresh session."
    assert len(agent.calls) == 2 and store.get("pub:8").session_id is None


async def test_private_channel_new_wraps_up_like_a_dm(env) -> None:
    manager, agent, _, store, _ = env
    hidden = Origin("private", 900, 111)
    await manager.handle("priv:900", "my calendar?", hidden, None, CONTEXT)
    assert CONTEXT in agent.calls[0][1] and store.get("priv:900").channel_id == 900
    assert await manager.handle("priv:900", NEW_COMMAND, hidden) == "🆕 Fresh session."
    assert WRAP_UP in agent.calls[1][1] and store.get("priv:900").session_id is None

async def test_public_daily_reset_on_next_mention(env) -> None:
    manager, agent, clock, _, _ = env
    await manager.handle("pub:8", "hi", GENERAL, None, CONTEXT)
    clock.now = ts(2026, 11, 19, 4, 30)
    await manager.handle("pub:8", "morning", GENERAL, None, CONTEXT)
    assert agent.calls[1][2] is None and len(agent.calls) == 2  # fresh session, no wrap-up turn


async def test_public_prompt_has_context_every_turn(env) -> None:
    manager, agent, _, _, _ = env
    await manager.handle("pub:8", "summarise the above", GENERAL, None, CONTEXT)
    await manager.handle("pub:8", "and now?", GENERAL, None, CONTEXT)
    first = agent.calls[0][1]
    assert first.index(CONTEXT) < first.index("Message to you:") < first.index("summarise the above")
    assert CONTEXT in agent.calls[1][1] and "Message to you:\n\nand now?" in agent.calls[1][1]


async def test_origin_is_stored_and_survives_a_new_session(env) -> None:
    manager, agent, _, store, _ = env
    agent.results = [TurnResult("ok", "first")]
    await manager.handle("thread:7", "hi", POST, TOPIC)
    record = store.get("thread:7")
    assert (record.mode, record.channel_id, record.owner_id, record.session_id) == ("private", 700, 111, "first")
    agent.results = [TurnResult("ok", "second")]  # CLI replaced the session
    await manager.handle("thread:7", "again", POST)
    assert (store.get("thread:7").session_id, store.get("thread:7").channel_id) == ("second", 700)


async def test_idle_clients_are_closed_but_sessions_kept(env) -> None:
    manager, agent, clock, store, _ = env
    await manager.handle("thread:7", "a", POST)
    clock.now += 20 * 60
    await manager.handle("pub:8", "b", GENERAL, None, CONTEXT)
    clock.now += 11 * 60
    await manager.tick(nothing)
    assert agent.closed == ["thread:7"] and agent.live_keys() == ["pub:8"]
    assert store.get("thread:7").session_id == "sess-1"


async def test_busy_client_is_not_closed(env) -> None:
    manager, agent, clock, _, _ = env
    await manager.handle("dm:1", "a")
    clock.now += CLIENT_IDLE_SECONDS + 60
    async with manager.lock("dm:1"):
        await manager._close_idle_clients()
    assert agent.closed == []


async def test_queued_public_message_keeps_its_context_and_goes_back_to_the_channel(env) -> None:
    manager, agent, clock, _, queue = env
    reset = clock.now + HOUR
    agent.results = [TurnResult("", None, limited=True, resets_at=reset)]
    assert (await manager.handle("pub:8", "summarise", GENERAL, None, CONTEXT)).startswith("⏳")
    assert queue.items[0].preamble.startswith(CONTEXT) and queue.items[0].text == "summarise"
    delivered: list[tuple[str, str]] = []

    async def deliver(key: str, text: str) -> None:
        delivered.append((key, text))

    clock.now = reset + 60
    await manager.tick(deliver)
    late = agent.calls[1][1]
    assert CONTEXT in late and late.endswith("because of a usage limit) summarise")
    assert delivered == [("pub:8", "ok")]


def test_state_migration_on_a_v1_copy(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    v1 = {"users": {"123456789012345678": {"session_id": "s", "started_at": 1.0, "last_active": 2.0}}}
    path.write_text(json.dumps(v1), encoding="utf-8")
    store = SessionStore(path)
    assert store.keys() == ["dm:123456789012345678"]
    record = store.get("dm:123456789012345678")
    assert (record.session_id, record.owner_id, record.mode) == ("s", 123456789012345678, "private")
    on_disk = json.loads(path.read_text(encoding="utf-8"))["users"]
    assert list(on_disk) == ["dm:123456789012345678"]
    assert SessionStore(path).get("dm:123456789012345678") == record  # idempotent


def test_state_load_ignores_unknown_fields_and_forces_mode_from_key(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"users": {"pub:8": {"session_id": "s", "mode": "private", "later": 1}}}),
                    encoding="utf-8")
    assert SessionStore(path).get("pub:8").mode == "public"
    path.write_text(json.dumps({"users": {"priv:9": {"session_id": "s", "mode": "public"}}}), encoding="utf-8")
    assert SessionStore(path).get("priv:9").mode == "private"


PTHREAD = Origin("public", 43, 111)
CHANNEL_THREAD = Origin("private", 42, 111)
CHANNEL_TOPIC = "Conversation topic: #plans — Weekend plans"


def test_public_thread_idle_pauses_without_wrap_up_and_no_daily_reset() -> None:
    now = ts(2026, 11, 18, 12)
    assert session_expiry("pthread:43", rec(now - 7 * HOUR, now - IDLE_SECONDS), now, OSLO) == (Expiry.PAUSE, "idle")
    assert session_expiry("pthread:43", rec(ts(2026, 11, 18, 3), now - 60), now, OSLO) is None
    paused = rec(now - 7 * HOUR, now - IDLE_SECONDS, wrapped_up=True)
    assert session_expiry("pthread:43", paused, now, OSLO) is None


async def test_public_thread_pauses_and_resumes_without_wrap_up(env) -> None:
    manager, agent, clock, store, _ = env
    await manager.handle("pthread:43", "weather?", PTHREAD, CHANNEL_TOPIC)
    clock.now += IDLE_SECONDS
    await manager.tick(nothing)
    assert len(agent.calls) == 1 and agent.closed == ["pthread:43"]
    assert store.get("pthread:43").session_id == "sess-1" and store.get("pthread:43").wrapped_up
    await manager.handle("pthread:43", "and tomorrow?", PTHREAD, CHANNEL_TOPIC)
    assert agent.calls[1][2] == "sess-1" and CHANNEL_TOPIC not in agent.calls[1][1]


async def test_channel_thread_idle_wraps_up_and_suspends(env) -> None:
    manager, agent, clock, store, _ = env
    await manager.handle("thread:42", "picnic?", CHANNEL_THREAD, CHANNEL_TOPIC)
    assert agent.calls[0][1].startswith("[Wed 2026-11-18 12:00 Europe/Oslo] " + CHANNEL_TOPIC)
    clock.now = ts(2026, 11, 19, 5)
    await manager.tick(nothing)
    assert WRAP_UP in agent.calls[1][1] and "memory-page" in agent.calls[1][1] and agent.closed == ["thread:42"]
    assert store.get("thread:42").session_id == "sess-1" and store.get("thread:42").wrapped_up


@pytest.mark.parametrize(("key", "origin", "wrap_ups"), [("thread:42", CHANNEL_THREAD, 1), ("pthread:43", PTHREAD, 0)])
async def test_new_in_a_thread_starts_fresh_and_keeps_the_thread_known(env, key: str, origin: Origin,
                                                                       wrap_ups: int) -> None:
    manager, agent, _, store, _ = env
    await manager.handle(key, "hi", origin, CHANNEL_TOPIC)
    assert await manager.handle(key, NEW_COMMAND, origin, CHANNEL_TOPIC) == "🆕 Fresh session."
    assert sum(WRAP_UP in call[1] for call in agent.calls) == wrap_ups
    record = store.get(key)
    assert record.session_id is None and record.channel_id == origin.channel_id and record.owner_id == 111
    await manager.handle(key, "again", origin, CHANNEL_TOPIC)
    assert CHANNEL_TOPIC in agent.calls[-1][1] and agent.calls[-1][2] is None


def test_wrap_up_prompt_tidies_memory_and_appends_to_sessions() -> None:
    prompt = wrap_up_prompt("sess-id", "mem-id")
    assert prompt.startswith(WRAP_UP) and "mem-id" in prompt and "sess-id" in prompt
    assert "update_content" in prompt and "duplicates" in prompt and 'position {"type": "end"}' in prompt
    assert prompt.index("mem-id") < prompt.index("sess-id")
    off = wrap_up_prompt("sess-id", "")
    assert "Memory" not in off and "1. Append" in off


def test_old_channel_records_still_load(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    old = {"pub:8": {"session_id": "s", "channel_id": 8, "owner_id": 111, "mode": "public"},
           "priv:9": {"session_id": "t", "channel_id": 9, "owner_id": 111, "wrapped_up": False}}
    path.write_text(json.dumps({"users": old}), encoding="utf-8")
    store = SessionStore(path)
    assert sorted(store.keys()) == ["priv:9", "pub:8"]
    assert (store.get("pub:8").mode, store.get("priv:9").mode) == ("public", "private")
    assert store.get("priv:9").channel_id == 9
