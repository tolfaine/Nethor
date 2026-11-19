from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from assistant.agent import TurnResult
from assistant.pending import PendingQueue
from assistant.remote import RemoteError, RemoteInfo, RemoteLink, RemoteSpec
from assistant.settings import ContextSnapshot, ConversationSettings
from assistant.sessions import (
    IDLE_SECONDS, NEW_COMMAND, WRAP_UP, SessionManager, SessionRecord, SessionStore, last_boundary, rollover_reason,
)

OSLO = ZoneInfo("Europe/Oslo")
HOUR = 3600.0


def ts(y: int, mo: int, d: int, h: int, mi: int = 0) -> float:
    return datetime(y, mo, d, h, mi, tzinfo=OSLO).timestamp()


def test_boundary_is_today_0400_after_four_and_yesterday_before() -> None:
    assert last_boundary(ts(2026, 11, 18, 10), OSLO) == ts(2026, 11, 18, 4)
    assert last_boundary(ts(2026, 11, 18, 3, 59), OSLO) == ts(2026, 11, 17, 4)
    assert last_boundary(ts(2026, 11, 18, 4), OSLO) == ts(2026, 11, 18, 4)


def test_boundary_on_spring_forward_day_is_0400_cest() -> None:
    boundary = last_boundary(ts(2026, 3, 29, 12), OSLO)
    assert datetime.fromtimestamp(boundary, timezone.utc) == datetime(2026, 3, 29, 2, tzinfo=timezone.utc)
    assert boundary - last_boundary(ts(2026, 3, 28, 12), OSLO) == 23 * HOUR


def test_boundary_on_fall_back_day_is_0400_cet() -> None:
    boundary = last_boundary(ts(2026, 10, 25, 12), OSLO)
    assert datetime.fromtimestamp(boundary, timezone.utc) == datetime(2026, 10, 25, 3, tzinfo=timezone.utc)
    assert boundary - last_boundary(ts(2026, 10, 24, 12), OSLO) == 25 * HOUR


def test_daily_rollover_across_spring_forward_night() -> None:
    started = ts(2026, 3, 28, 23)
    record = SessionRecord("s1", started_at=started, last_active=ts(2026, 3, 29, 3, 30))
    assert rollover_reason(record, ts(2026, 3, 29, 3, 50), OSLO) is None
    assert rollover_reason(record, ts(2026, 3, 29, 4, 1), OSLO) == "daily"


def test_idle_counts_real_hours_across_fall_back() -> None:
    # 00:30 CEST -> 05:30 CET is 5 wall-clock hours but 6 real hours.
    last = datetime(2026, 10, 24, 22, 30, tzinfo=timezone.utc).timestamp()
    now = datetime(2026, 10, 25, 4, 30, tzinfo=timezone.utc).timestamp()
    record = SessionRecord("s1", started_at=ts(2026, 10, 25, 0, 10), last_active=last)
    assert now - last == IDLE_SECONDS
    assert rollover_reason(record, now, OSLO) == "idle"


def test_no_rollover_without_session_or_when_fresh() -> None:
    now = ts(2026, 11, 18, 12)
    assert rollover_reason(SessionRecord(), now, OSLO) is None
    assert rollover_reason(SessionRecord("s", started_at=now - HOUR, last_active=now - 60), now, OSLO) is None


def test_store_round_trip(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "state.json")
    record = SessionRecord("abc", 1.0, 2.0, mode="private", channel_id=5, owner_id=42)
    store.put("dm:42", record)
    assert SessionStore(tmp_path / "state.json").get("dm:42") == record
    store.drop("dm:42")
    assert SessionStore(tmp_path / "state.json").get("dm:42") == SessionRecord()


class FakeAgent:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self.results: list[TurnResult] = []
        self.closed: list[str] = []
        self.live: set[str] = set()
        self.turn_settings: list[ConversationSettings | None] = []
        self.applied: list[tuple[str, str | None, ConversationSettings]] = []
        self.context: ContextSnapshot | None = None
        self.turn_remotes: list[RemoteSpec | None] = []
        self.remote: dict[str, RemoteInfo] = {}
        self.enabled: list[tuple[str, RemoteSpec]] = []
        self.disabled: list[str] = []
        self.remote_error: str | None = None

    async def run_turn(self, user_key: str, prompt: str, session_id: str | None,
                       settings: ConversationSettings | None = None, remote: RemoteSpec | None = None) -> TurnResult:
        self.calls.append((user_key, prompt, session_id))
        self.turn_settings.append(settings)
        self.turn_remotes.append(remote)
        self.live.add(user_key)
        return self.results.pop(0) if self.results else TurnResult("ok", session_id or "sess-1")

    async def apply_settings(self, user_key: str, session_id: str | None, settings: ConversationSettings,
                             remote: RemoteSpec | None = None) -> None:
        self.applied.append((user_key, session_id, settings))

    def remote_info(self, user_key: str) -> RemoteInfo | None:
        return self.remote.get(user_key) if user_key in self.live else None

    async def enable_remote(self, user_key: str, session_id: str | None, settings: ConversationSettings | None,
                            spec: RemoteSpec) -> RemoteInfo:
        self.enabled.append((user_key, spec))
        if self.remote_error is not None:
            raise RemoteError(self.remote_error)
        self.live.add(user_key)
        known = self.remote.get(user_key)
        info = known or RemoteInfo(RemoteLink(f"https://claude.ai/code/{user_key}", spec.reattach_id or "cse_1"),
                                   "connected")
        self.remote[user_key] = info
        return info

    async def disable_remote(self, user_key: str) -> None:
        self.disabled.append(user_key)
        self.remote.pop(user_key, None)

    async def context_usage(self, user_key: str) -> ContextSnapshot | None:
        return self.context if user_key in self.live else None

    async def close_session(self, user_key: str) -> None:
        self.closed.append(user_key)
        self.live.discard(user_key)

    def live_keys(self) -> list[str]:
        return sorted(self.live)


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def setup(tmp_path: Path) -> tuple[SessionManager, FakeAgent, Clock, PendingQueue, SessionStore]:
    agent, clock = FakeAgent(), Clock(ts(2026, 11, 18, 12))
    store, queue = SessionStore(tmp_path / "state.json"), PendingQueue(tmp_path / "pending.json")
    return SessionManager(agent, store, queue, OSLO, "sessions-page", clock=clock, memory_page_id="memory-page"), agent, clock, queue, store


async def test_message_starts_and_resumes_session(setup) -> None:
    manager, agent, clock, _, store = setup
    assert await manager.handle("u", "hi") == "ok"
    assert store.get("u").session_id == "sess-1"
    clock.now += 60
    await manager.handle("u", "again")
    assert agent.calls[1][2] == "sess-1"
    assert agent.calls[1][1].startswith("[Wed 2026-11-18 12:01 Europe/Oslo] again")


async def test_rate_limit_queues_then_answers_after_reset(setup) -> None:
    manager, agent, clock, queue, _ = setup
    reset = clock.now + 2 * HOUR
    agent.results = [TurnResult("", None, limited=True, resets_at=reset)]
    assert await manager.handle("u", "remind me") == "⏳ limit hit, back at 14:00"
    assert await manager.handle("u", "and this") == "⏳ limit hit, back at 14:00"
    assert len(agent.calls) == 1 and len(queue.items) == 2
    delivered: list[tuple[str, str]] = []

    async def deliver(user_key: str, text: str) -> None:
        delivered.append((user_key, text))

    await manager.tick(deliver)
    assert delivered == []
    clock.now = reset + 60
    await manager.tick(deliver)
    assert delivered == [("u", "ok"), ("u", "ok")]
    assert queue.items == []
    assert "answered late" in agent.calls[1][1] and agent.calls[1][1].endswith("remind me")


async def test_new_command_wraps_up_and_drops_session(setup) -> None:
    manager, agent, _, _, store = setup
    await manager.handle("u", "hi")
    assert await manager.handle("u", NEW_COMMAND) == "🆕 Fresh session."
    assert WRAP_UP in agent.calls[1][1] and "sessions-page" in agent.calls[1][1] and "memory-page" in agent.calls[1][1]
    assert store.get("u").session_id is None and agent.closed == ["u"]


async def test_background_rollover_keeps_session_when_wrap_up_is_limited(setup) -> None:
    manager, agent, clock, _, store = setup
    await manager.handle("u", "hi")
    clock.now = ts(2026, 11, 19, 4, 5)
    agent.results = [TurnResult("", "sess-1", limited=True, resets_at=clock.now + HOUR)]

    async def deliver(user_key: str, text: str) -> None:
        raise AssertionError("nothing to deliver")

    await manager.tick(deliver)
    assert store.get("u").session_id == "sess-1"
    clock.now += 2 * HOUR
    await manager.tick(deliver)
    assert store.get("u").session_id is None


async def test_agent_exception_gives_short_warning(setup) -> None:
    manager, agent, _, _, _ = setup

    async def boom(*args: object) -> TurnResult:
        raise RuntimeError("cli died")

    agent.run_turn = boom  # type: ignore[method-assign]
    reply = await manager.handle("u", "hi")
    assert reply.startswith("⚠️") and "RuntimeError" in reply


def test_fall_back_day_length_sanity() -> None:
    day = timedelta(hours=25).total_seconds()
    assert ts(2026, 10, 26, 0) - ts(2026, 10, 25, 0) == day
