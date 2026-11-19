"""Messages that hit the rate limit, persisted in data/pending.json and answered after the reset."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from assistant.keys import migrate_key

FALLBACK_RETRY_SECONDS = 15 * 60
RESET_GRACE_SECONDS = 30


@dataclass
class PendingItem:
    user_key: str
    text: str
    queued_at: float
    retry_at: float
    attempts: int = 0
    item_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    preamble: str = ""


def next_retry_at(resets_at: float | None, now: float) -> float:
    """Retry just after the reported reset; without a usable reset time, every 15 min."""
    if resets_at is not None and resets_at > now:
        return resets_at + RESET_GRACE_SECONDS
    return now + FALLBACK_RETRY_SECONDS


def limit_message(retry_at: float, tz: ZoneInfo) -> str:
    return f"⏳ limit hit, back at {datetime.fromtimestamp(retry_at, tz):%H:%M}"


def write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


class PendingQueue:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._items: list[PendingItem] = self._load()

    def _load(self) -> list[PendingItem]:
        if not self._path.exists():
            return []
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        items = [PendingItem(**item) for item in raw.get("items", [])]
        for item in items:
            item.user_key = migrate_key(item.user_key)
        return items

    def _save(self) -> None:
        write_json_atomic(self._path, {"items": [asdict(item) for item in self._items]})

    @property
    def items(self) -> list[PendingItem]:
        return list(self._items)

    def add(self, user_key: str, text: str, now: float, retry_at: float, preamble: str = "") -> PendingItem:
        item = PendingItem(user_key=user_key, text=text, queued_at=now, retry_at=retry_at, preamble=preamble)
        self._items.append(item)
        self._save()
        return item

    def due(self, now: float) -> list[PendingItem]:
        return sorted((i for i in self._items if i.retry_at <= now), key=lambda i: i.queued_at)

    def remove(self, item_id: str) -> None:
        self._items = [i for i in self._items if i.item_id != item_id]
        self._save()

    def has(self, item_id: str) -> bool:
        return any(i.item_id == item_id for i in self._items)

    def drop_key(self, user_key: str) -> int:
        kept = [i for i in self._items if i.user_key != user_key]
        dropped = len(self._items) - len(kept)
        if dropped:
            self._items = kept
            self._save()
        return dropped

    def reschedule(self, item_id: str, retry_at: float) -> None:
        for item in self._items:
            if item.item_id == item_id:
                item.retry_at = retry_at
                item.attempts += 1
        self._save()

    def earliest_retry(self) -> float | None:
        return min((i.retry_at for i in self._items), default=None)
