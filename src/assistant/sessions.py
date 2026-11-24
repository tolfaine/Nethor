"""One session per conversation key: persistence, per-mode lifetime rules, and the message/maintenance flow."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager, nullcontext
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime, timedelta
from datetime import time as dtime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

from assistant.agent import TurnResult
from assistant.keys import DM, PRIVATE, PUBLIC, THREAD_KINDS, key_id, key_kind, migrate_key, mode_for_key
from assistant.pending import PendingItem, PendingQueue, limit_message, next_retry_at, write_json_atomic
from assistant.remote import (
    FAILED_STATE, REMOTE_ALREADY_OFF, REMOTE_FAILED, REMOTE_OFF, RemoteError, RemoteInfo, RemoteSpec, remote_on_text,
)
from assistant.settings import (
    DEFAULT_MODEL, Change, ContextSnapshot, ConversationSettings, FastInfo, Freshness, StatusView, apply_change,
    describe_remote,
)

log = logging.getLogger("assistant.sessions")

ROLLOVER_HOUR = 4
IDLE_SECONDS = 6 * 3600
CLIENT_IDLE_SECONDS = 30 * 60
TICK_SECONDS = 5 * 60
MAX_PENDING_ATTEMPTS = 8
NEW_COMMAND = "!new"
FRESH_REPLY = "🆕 Fresh session."
AFTER_TURN = "⏳ A reply is running: this applies right after it."

WRAP_UP = "[Automatic session wrap-up; the user is not waiting for this reply.]"
MEMORY_STEP = (
    "Tidy the Memory page (page_id {memory_page_id}; notion-update-page with command update_content, as in your "
    "rules): add durable facts about the user from this session under the right heading, edit the existing line "
    "instead of adding a duplicate, merge duplicates, delete lines that are stale or became false. One short English "
    "line per fact, never secrets. Skip if nothing changed."
)
SESSIONS_STEP = (
    "Append a dated 3-line summary of this session to the end of the Sessions page (notion-update-page, page_id "
    '{sessions_page_id}, command insert_content, position {{"type": "end"}}).'
)


def wrap_up_prompt(sessions_page_id: str, memory_page_id: str = "") -> str:
    """Without a Memory page, memory is off and the wrap-up only writes the Sessions summary."""
    steps = [MEMORY_STEP.format(memory_page_id=memory_page_id)] if memory_page_id else []
    steps.append(SESSIONS_STEP.format(sessions_page_id=sessions_page_id))
    numbered = "\n".join(f"{index}. {step}" for index, step in enumerate(steps, 1))
    return f"{WRAP_UP}\n{numbered}\nThen reply with one line saying what you saved."


Deliver = Callable[[str, str], Awaitable[None]]
Typing = Callable[[str], AbstractAsyncContextManager[Any]]  # "typing…" in a conversation while its turn runs
WrapUpOutcome = Literal["saved", "failed", "skipped"]


class TurnRunner(Protocol):
    async def run_turn(self, user_key: str, prompt: str, session_id: str | None,
                       settings: ConversationSettings | None = None, remote: RemoteSpec | None = None) -> TurnResult: ...

    async def close_session(self, user_key: str) -> None: ...

    def live_keys(self) -> list[str]: ...

    async def apply_settings(self, user_key: str, session_id: str | None, settings: ConversationSettings,
                             remote: RemoteSpec | None = None) -> None: ...

    async def context_usage(self, user_key: str) -> ContextSnapshot | None: ...

    def remote_info(self, user_key: str) -> RemoteInfo | None: ...

    async def enable_remote(self, user_key: str, session_id: str | None, settings: ConversationSettings | None,
                            spec: RemoteSpec) -> RemoteInfo: ...

    async def disable_remote(self, user_key: str) -> None: ...


@dataclass
class SessionRecord:
    """`model`/`effort`/`fast`/`remote` last one conversation; `context_*`, `fast_*`, `remote_state` are last known.

    `remote` = on Remote Control; `bridge_session_id` re-attaches it whenever a new CLI process starts.
    """

    session_id: str | None = None
    started_at: float = 0.0
    last_active: float = 0.0
    mode: str = PRIVATE
    channel_id: int | None = None
    owner_id: int | None = None
    wrapped_up: bool = False
    model: str | None = None
    effort: str | None = None
    fast: bool = False
    context_pct: float | None = None
    context_tokens: int | None = None
    context_max: int | None = None
    context_categories: dict[str, int] = field(default_factory=dict)
    fast_state: str | None = None
    fast_reason: str | None = None
    last_speed: str | None = None
    remote: bool = False
    bridge_session_id: str | None = None
    remote_url: str | None = None
    remote_state: str | None = None


def settings_of(record: SessionRecord) -> ConversationSettings:
    return ConversationSettings(record.model, record.effort, record.fast)


def with_settings(record: SessionRecord, settings: ConversationSettings) -> SessionRecord:
    """A model or fast change makes the stored fast-mode report stale."""
    updated = replace(record, model=settings.model, effort=settings.effort, fast=settings.fast)
    if (settings.model, settings.fast) != (record.model, record.fast):
        updated = replace(updated, fast_state=None, fast_reason=None, last_speed=None)
    return updated


def remote_spec(record: SessionRecord) -> RemoteSpec | None:
    return RemoteSpec(reattach_id=record.bridge_session_id) if record.remote else None


def without_remote(record: SessionRecord) -> SessionRecord:
    return replace(record, remote=False, bridge_session_id=None, remote_url=None, remote_state=None)


def with_remote_info(record: SessionRecord, info: RemoteInfo) -> SessionRecord:
    """A re-attach can hand out a new bridge (and link); a state is kept until a newer one arrives."""
    updated = replace(record, remote_state=info.state or record.remote_state)
    if info.link is not None:
        updated = replace(updated, bridge_session_id=info.link.bridge_session_id or record.bridge_session_id,
                          remote_url=info.link.session_url)
    return updated


def with_context(record: SessionRecord, context: ContextSnapshot) -> SessionRecord:
    return replace(record, context_pct=context.percentage, context_tokens=context.total, context_max=context.maximum,
                   context_categories=dict(context.categories))


def record_context(record: SessionRecord) -> ContextSnapshot | None:
    if record.context_tokens is None or record.context_max is None or record.context_pct is None:
        return None
    return ContextSnapshot(record.context_tokens, record.context_max, record.context_pct,
                           tuple(record.context_categories.items()))


def with_turn_info(record: SessionRecord, result: TurnResult) -> SessionRecord:
    if result.context is not None:
        record = with_context(record, result.context)
    if result.fast_state is not None:
        record = replace(record, fast_state=result.fast_state, fast_reason=result.fast_reason)
    if not result.limited and result.error is None:
        record = replace(record, last_speed=result.speed)
    return record


def freshness(live: bool, busy: bool, refreshed: bool) -> Freshness:
    if busy:
        return "busy"
    if not live:
        return "paused"
    return "live" if refreshed else "stale"


@dataclass(frozen=True)
class Origin:
    """Where a conversation lives: replies, approvals and late answers go back there."""

    mode: str
    channel_id: int | None
    owner_id: int | None


class Expiry(StrEnum):
    ROLLOVER = "rollover"  # wrap-up, then drop the session
    SUSPEND = "suspend"  # wrap-up, free the process, keep the session id to resume
    PAUSE = "pause"  # like SUSPEND without the wrap-up (public threads)
    DROP = "drop"  # close and forget, no wrap-up


KEEPS_SESSION = frozenset({Expiry.SUSPEND, Expiry.PAUSE})
NO_WRAP_UP = frozenset({Expiry.PAUSE, Expiry.DROP})


def last_boundary(now: float, tz: ZoneInfo, hour: int = ROLLOVER_HOUR) -> float:
    """Unix time of the most recent local `hour`:00 at or before `now` (wall clock, so DST-safe)."""
    local = datetime.fromtimestamp(now, tz)
    boundary = datetime.combine(local.date(), dtime(hour), tzinfo=tz)
    if boundary.timestamp() > now:
        boundary = datetime.combine(local.date() - timedelta(days=1), dtime(hour), tzinfo=tz)
    return boundary.timestamp()


def rollover_reason(record: SessionRecord, now: float, tz: ZoneInfo) -> str | None:
    if not record.session_id:
        return None
    if now - record.last_active >= IDLE_SECONDS:
        return "idle"
    if record.started_at < last_boundary(now, tz):
        return "daily"
    return None


def session_expiry(key: str, record: SessionRecord, now: float, tz: ZoneInfo) -> tuple[Expiry, str] | None:
    """Threads live as long as the thread (idle only suspends); DMs and legacy channels reset daily.

    Public conversations never get a wrap-up.
    """
    public = mode_for_key(key) == PUBLIC
    if key_kind(key) in THREAD_KINDS:
        idle = now - record.last_active >= IDLE_SECONDS
        if not (record.session_id and idle and not record.wrapped_up):
            return None
        return (Expiry.PAUSE if public else Expiry.SUSPEND), "idle"
    reason = rollover_reason(record, now, tz)
    if reason is None:
        return None
    return (Expiry.DROP if public else Expiry.ROLLOVER), reason


def idle_clients(live_keys: Iterable[str], record_of: Callable[[str], SessionRecord], now: float,
                 limit: float = CLIENT_IDLE_SECONDS) -> list[str]:
    """Remote Control conversations keep their process: the bridge to the app lives in it."""
    return [key for key in live_keys if now - record_of(key).last_active > limit and not record_of(key).remote]


def stamp(text: str, when: float, tz: ZoneInfo) -> str:
    return f"[{datetime.fromtimestamp(when, tz):%a %Y-%m-%d %H:%M} {tz.key}] {text}"


def build_preamble(topic: str | None, context: str | None) -> str:
    parts = [part for part in (topic, context) if part]
    if context:
        parts.append("Message to you:")
    return "\n\n".join(parts)


def join_prompt(preamble: str, text: str) -> str:
    return f"{preamble}\n\n{text}" if preamble else text


def _record_from(key: str, value: dict[str, Any]) -> SessionRecord:
    known = {f.name for f in fields(SessionRecord)}
    record = SessionRecord(**{k: v for k, v in value.items() if k in known})
    record.mode = mode_for_key(key)
    if record.owner_id is None and key_kind(key) == DM:
        record.owner_id = key_id(key)
    return record


class SessionStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._records, migrated = self._load()
        if migrated:
            log.info("migrated session keys to dm:<id>: %s", migrated)
            self._save()

    def _load(self) -> tuple[dict[str, SessionRecord], list[str]]:
        if not self._path.exists():
            return {}, []
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        records: dict[str, SessionRecord] = {}
        migrated: list[str] = []
        for key, value in raw.get("users", {}).items():
            new_key = migrate_key(key)
            if new_key != key:
                migrated.append(key)
            records[new_key] = _record_from(new_key, value)
        return records, migrated

    def _save(self) -> None:
        write_json_atomic(self._path, {"users": {k: asdict(v) for k, v in self._records.items()}})

    def get(self, user_key: str) -> SessionRecord:
        return self._records.get(user_key, SessionRecord(mode=mode_for_key(user_key)))

    def put(self, user_key: str, record: SessionRecord) -> None:
        self._records[user_key] = record
        self._save()

    def set_origin(self, user_key: str, origin: Origin) -> None:
        record = self.get(user_key)
        updated = replace(record, mode=origin.mode, channel_id=origin.channel_id, owner_id=origin.owner_id)
        if updated != record or user_key not in self._records:
            self.put(user_key, updated)

    def mark_wrapped_up(self, user_key: str) -> None:
        if user_key in self._records:
            self.put(user_key, replace(self._records[user_key], wrapped_up=True))

    def drop(self, user_key: str) -> None:
        self._records.pop(user_key, None)
        self._save()

    def keys(self) -> list[str]:
        return list(self._records)


class SessionManager:
    def __init__(self, agent: TurnRunner, store: SessionStore, queue: PendingQueue, tz: ZoneInfo,
                 sessions_page_id: str, clock: Callable[[], float] = time.time, memory_page_id: str = "",
                 default_model: str = DEFAULT_MODEL) -> None:
        self._agent = agent
        self._store = store
        self._queue = queue
        self._tz = tz
        self._rollover_prompt = wrap_up_prompt(sessions_page_id, memory_page_id)
        self._clock = clock
        self._default_model = default_model
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._limited_until = max(queue.earliest_retry() or 0.0, 0.0)
        self._deferred: set[asyncio.Task[None]] = set()

    def lock(self, user_key: str) -> asyncio.Lock:
        return self._locks[user_key]

    def record(self, user_key: str) -> SessionRecord:
        return self._store.get(user_key)

    async def handle(self, user_key: str, text: str, origin: Origin | None = None, topic: str | None = None,
                     context: str | None = None) -> str:
        """`topic` is used only on a session's first turn; `context` (an extra preamble) on every turn."""
        async with self.lock(user_key):
            try:
                return await self._handle_locked(user_key, text, origin, topic, context)
            except Exception as exc:
                log.exception("message failed key=%s text=%r", user_key, text[:200])
                return f"⚠️ Something went wrong ({type(exc).__name__}); details are in the log."

    async def _handle_locked(self, user_key: str, text: str, origin: Origin | None, topic: str | None,
                             context: str | None) -> str:
        if origin is not None:
            self._store.set_origin(user_key, origin)
        if text.strip().lower() == NEW_COMMAND:
            return await self._new_session(user_key, origin)
        await self._expire_if_due(user_key, force=True)
        fresh = not self._store.get(user_key).session_id
        preamble = build_preamble(topic if fresh else None, context)
        if self._clock() < self._limited_until:
            return self._enqueue(user_key, text, preamble)
        result = await self._turn(user_key, stamp(join_prompt(preamble, text), self._clock(), self._tz))
        if result.limited:
            return self._enqueue(user_key, text, preamble)
        return reply_text(result)

    def _enqueue(self, user_key: str, text: str, preamble: str) -> str:
        item = self._queue.add(user_key, text, self._clock(), self._limited_until, preamble)
        log.info("queued message key=%s item=%s retry_at=%s", user_key, item.item_id, item.retry_at)
        return limit_message(item.retry_at, self._tz)

    async def _turn(self, user_key: str, prompt: str) -> TurnResult:
        record = self._store.get(user_key)
        result = await self._agent.run_turn(user_key, prompt, record.session_id, settings_of(record),
                                            remote_spec(record))
        now = self._clock()
        if result.session_id and result.session_id != record.session_id:
            log.info("session started key=%s session=%s (was %s)", user_key, result.session_id, record.session_id)
            record = replace(record, session_id=result.session_id, started_at=now)
        record = with_turn_info(record, result)
        record.last_active = now
        record.wrapped_up = False
        if record.session_id:
            self._store.put(user_key, record)
        self._sync_remote(user_key)
        if result.limited:
            self._limited_until = next_retry_at(result.resets_at, now)
        return result

    def _sync_remote(self, user_key: str) -> None:
        record = self._store.get(user_key)
        info = self._agent.remote_info(user_key) if record.remote else None
        if info is not None and (updated := with_remote_info(record, info)) != record:
            self._store.put(user_key, updated)

    async def _new_session(self, user_key: str, origin: Origin | None) -> str:
        record = self._store.get(user_key)
        if not record.session_id:
            if record.remote:
                await self._agent.disable_remote(user_key)
            fresh = with_settings(without_remote(record), ConversationSettings())
            if fresh != record:
                self._store.put(user_key, fresh)
            return FRESH_REPLY
        action = Expiry.DROP if mode_for_key(user_key) == PUBLIC else Expiry.ROLLOVER
        saved = await self._expire(user_key, action, NEW_COMMAND, force=True)
        if origin is not None and key_kind(user_key) in THREAD_KINDS:
            # The record is what makes a thread answer without a mention, so the thread stays known.
            self._store.set_origin(user_key, origin)
        return FRESH_REPLY if saved else f"{FRESH_REPLY} ⚠️ Couldn't save the old session's summary."

    async def _expire_if_due(self, user_key: str, force: bool) -> None:
        expiry = session_expiry(user_key, self._store.get(user_key), self._clock(), self._tz)
        if expiry is not None:
            await self._expire(user_key, *expiry, force=force)

    async def _expire(self, user_key: str, action: Expiry, reason: str, force: bool) -> bool:
        """Without `force`, a failed wrap-up keeps everything for a retry on the next tick."""
        log.info("session end key=%s mode=%s action=%s reason=%s", user_key, mode_for_key(user_key), action, reason)
        ok = action in NO_WRAP_UP or await self._wrap_up(user_key, reason)
        if not ok and not force:
            return False
        # After the wrap-up, which re-attached a closed client: the request then reaches the bridge.
        if action not in KEEPS_SESSION and self._store.get(user_key).remote:
            await self._agent.disable_remote(user_key)
        await self._agent.close_session(user_key)
        if action not in KEEPS_SESSION:
            self._store.drop(user_key)
        elif ok:
            self._store.mark_wrapped_up(user_key)
        return ok

    async def _wrap_up(self, user_key: str, reason: str) -> bool:
        if self._clock() < self._limited_until:
            return False
        try:
            result = await self._turn(user_key, self._rollover_prompt)
        except Exception:
            log.exception("wrap-up failed key=%s reason=%s", user_key, reason)
            return False
        ok = not result.limited and result.error is None
        log.info("wrap-up key=%s ok=%s reply=%r", user_key, ok, result.text[:300])
        return ok

    def busy(self, user_keys: Iterable[str]) -> bool:
        return any(self.lock(key).locked() for key in user_keys)

    @asynccontextmanager
    async def holding(self, user_keys: Iterable[str]) -> AsyncIterator[None]:
        """Several per-key locks, always taken in sorted order."""
        async with AsyncExitStack() as stack:
            for key in sorted(set(user_keys)):
                await stack.enter_async_context(self.lock(key))
            yield

    def has_state(self, user_key: str) -> bool:
        return (user_key in self._store.keys() or user_key in self._agent.live_keys()
                or any(item.user_key == user_key for item in self._queue.items))

    async def end_locked(self, user_key: str) -> WrapUpOutcome:
        """Ends a conversation for good (its thread is being deleted). The caller holds the key's lock."""
        wraps = mode_for_key(user_key) == PRIVATE and bool(self._store.get(user_key).session_id)
        saved = await self._expire(user_key, Expiry.ROLLOVER if wraps else Expiry.DROP, "delete", force=True)
        dropped = self._queue.drop_key(user_key)
        if dropped:
            log.info("dropped %d queued message(s) key=%s", dropped, user_key)
        if not wraps:
            return "skipped"
        return "saved" if saved else "failed"

    def settings(self, user_key: str) -> ConversationSettings:
        return settings_of(self._store.get(user_key))

    async def apply_setting(self, user_key: str, change: Change, origin: Origin | None = None) -> str:
        """Never mid-turn: while a reply runs, the change waits for the per-key lock and the reply says so."""
        _, note = apply_change(self.settings(user_key), change, self._default_model)  # SettingError → caller
        return await self._now_or_after_turn(user_key, change.describe(),
                                             lambda: self._apply_locked(user_key, change, origin), note)

    async def _apply_locked(self, user_key: str, change: Change, origin: Origin | None) -> str:
        # A conversation due to end would drop the new setting on the next message; end it first.
        await self._expire_if_due(user_key, force=True)
        if origin is not None:
            self._store.set_origin(user_key, origin)
        record = self._store.get(user_key)
        updated, note = apply_change(settings_of(record), change, self._default_model)
        self._store.put(user_key, with_settings(record, updated))
        await self._agent.apply_settings(user_key, record.session_id, updated, remote_spec(record))
        self._sync_remote(user_key)
        return note

    async def enable_remote(self, user_key: str, name: str, origin: Origin | None = None) -> str:
        """Waits for a running reply: the answer is the link. Already on = the same link again."""
        async with self.lock(user_key):
            await self._expire_if_due(user_key, force=True)
            if origin is not None:
                self._store.set_origin(user_key, origin)
            record = self._store.get(user_key)
            spec = RemoteSpec(name, record.bridge_session_id)
            try:
                info = await self._agent.enable_remote(user_key, record.session_id, settings_of(record), spec)
            except RemoteError as exc:
                log.warning("remote control on failed key=%s bridge=%s: %s", user_key, record.bridge_session_id, exc)
                if record.remote:
                    self._store.put(user_key, replace(record, remote_state=FAILED_STATE))
                return REMOTE_FAILED.format(reason=exc)
            updated = with_remote_info(replace(record, remote=True, remote_state=None), info)
            self._store.put(user_key, updated)
        already = record.remote and record.remote_url == updated.remote_url
        return remote_on_text(updated.remote_url or "?", already)

    async def disable_remote(self, user_key: str) -> str:
        async def work() -> str:
            record = self._store.get(user_key)
            if not record.remote:
                return REMOTE_ALREADY_OFF
            await self._agent.disable_remote(user_key)
            self._store.put(user_key, without_remote(self._store.get(user_key)))
            return REMOTE_OFF

        return await self._now_or_after_turn(user_key, "remote=off", work, REMOTE_OFF)

    async def new_conversation(self, user_key: str, origin: Origin | None = None) -> str:
        async def work() -> str:
            if origin is not None:
                self._store.set_origin(user_key, origin)
            return await self._new_session(user_key, origin)

        return await self._now_or_after_turn(user_key, "new", work, "🆕 Starting fresh.")

    async def _now_or_after_turn(self, user_key: str, what: str, work: Callable[[], Awaitable[str]],
                                 note: str) -> str:
        lock = self.lock(user_key)
        if not lock.locked():
            async with lock:
                return await work()
        task = asyncio.create_task(self._after_turn(user_key, what, work))
        self._deferred.add(task)
        task.add_done_callback(self._deferred.discard)
        log.info("command waits for the running reply key=%s command=%s", user_key, what)
        return f"{note}\n{AFTER_TURN}"

    async def _after_turn(self, user_key: str, what: str, work: Callable[[], Awaitable[str]]) -> None:
        async with self.lock(user_key):
            try:
                outcome = await work()
            except Exception:
                log.exception("deferred command failed key=%s command=%s", user_key, what)
                return
        log.info("deferred command applied key=%s command=%s outcome=%r", user_key, what, outcome[:200])

    async def status(self, user_key: str) -> StatusView:
        """Live numbers when the client is up and idle; otherwise the last known ones."""
        record = self._store.get(user_key)
        lock = self.lock(user_key)
        live, busy = user_key in self._agent.live_keys(), lock.locked()
        context = record_context(record)
        refreshed = False
        if live and not busy:
            async with lock:
                fresh = await self._agent.context_usage(user_key)
            if fresh is not None:
                context, refreshed = fresh, True
                if user_key in self._store.keys():
                    self._store.put(user_key, with_context(self._store.get(user_key), fresh))
        return StatusView(settings_of(record).effective(self._default_model),
                          FastInfo(record.fast_state, record.fast_reason, record.last_speed), context,
                          freshness(live, busy, refreshed), mode_for_key(user_key),
                          record.started_at if record.session_id else None, self._remote_text(user_key, live))

    def _remote_text(self, user_key: str, live: bool) -> str | None:
        """Public conversations can't use Remote Control, so their status doesn't mention it."""
        if mode_for_key(user_key) == PUBLIC:
            return None
        record = self._store.get(user_key)
        info = self._agent.remote_info(user_key) if live and record.remote else None
        return describe_remote(record.remote, live, (info.state if info else None) or record.remote_state)

    async def tick(self, deliver: Deliver, typing: Typing | None = None) -> None:
        for user_key in self._store.keys():
            async with self.lock(user_key):
                await self._expire_if_due(user_key, force=False)
        await self._close_idle_clients()
        for item in self._queue.due(self._clock()):
            if not await self._answer_pending(item, deliver, typing):
                break

    async def _close_idle_clients(self) -> None:
        """Caps live CLI processes; the session id stays, so the next message resumes it."""
        for user_key in idle_clients(self._agent.live_keys(), self._store.get, self._clock()):
            lock = self.lock(user_key)
            if lock.locked():
                continue
            async with lock:
                log.info("closing idle client key=%s", user_key)
                await self._agent.close_session(user_key)

    async def _answer_pending(self, item: PendingItem, deliver: Deliver, typing: Typing | None = None) -> bool:
        """Answer one queued message; False when still rate limited."""
        async with self.lock(item.user_key):
            if not self._queue.has(item.item_id):
                return True  # dropped (thread deleted) while this waited for the lock
            late =(f"(sent at {datetime.fromtimestamp(item.queued_at, self._tz):%H:%M}, answered late "
                    f"because of a usage limit) {item.text}")
            try:
                async with typing(item.user_key) if typing else nullcontext():
                    result = await self._turn(item.user_key, stamp(join_prompt(item.preamble, late), self._clock(),
                                                                    self._tz))
            except Exception:
                log.exception("pending answer failed item=%s attempts=%d", item.item_id, item.attempts)
                return await self._retry_or_give_up(item, deliver)
            if result.limited:
                self._queue.reschedule(item.item_id, self._limited_until)
                return False
            self._queue.remove(item.item_id)
        await _safe_deliver(deliver, item.user_key, reply_text(result))
        return True

    async def _retry_or_give_up(self, item: PendingItem, deliver: Deliver) -> bool:
        if item.attempts + 1 < MAX_PENDING_ATTEMPTS:
            self._queue.reschedule(item.item_id, next_retry_at(None, self._clock()))
            return True
        self._queue.remove(item.item_id)
        log.error("gave up on pending item=%s text=%r", item.item_id, item.text[:200])
        await _safe_deliver(deliver, item.user_key, f"⚠️ Gave up answering: {item.text[:200]}")
        return True

    def seconds_until_next_tick(self) -> float:
        earliest = self._queue.earliest_retry()
        if earliest is None:
            return TICK_SECONDS
        return min(TICK_SECONDS, max(5.0, earliest - self._clock()))


def reply_text(result: TurnResult) -> str:
    if result.error:
        prefix = f"{result.text}\n" if result.text else ""
        return f"{prefix}⚠️ The assistant hit an error; details are in the log."
    return result.text


async def _safe_deliver(deliver: Deliver, user_key: str, text: str) -> None:
    try:
        await deliver(user_key, text)
    except Exception:
        log.exception("delivery failed key=%s text=%r", user_key, text[:200])


async def run_maintenance_loop(manager: SessionManager, deliver: Deliver, typing: Typing | None = None) -> None:
    while True:
        try:
            await manager.tick(deliver, typing)
        except Exception:
            log.exception("maintenance tick failed")
        await asyncio.sleep(manager.seconds_until_next_tick())
