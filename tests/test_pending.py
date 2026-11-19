from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from assistant.pending import (
    FALLBACK_RETRY_SECONDS, RESET_GRACE_SECONDS, PendingQueue, limit_message, next_retry_at,
)


def test_queue_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "pending.json"
    queue = PendingQueue(path)
    first = queue.add("u1", "remind me at 18 to call mum ⏰", now=100.0, retry_at=200.0)
    second = queue.add("u2", "add milk", now=50.0, retry_at=150.0)
    reloaded = PendingQueue(path)
    assert reloaded.items == [first, second]
    assert [i.item_id for i in reloaded.due(175.0)] == [second.item_id]
    assert [i.item_id for i in reloaded.due(300.0)] == [second.item_id, first.item_id]
    reloaded.reschedule(second.item_id, 400.0)
    reloaded.remove(first.item_id)
    final = PendingQueue(path).items
    assert len(final) == 1 and final[0].retry_at == 400.0 and final[0].attempts == 1
    assert PendingQueue(path).earliest_retry() == 400.0


def test_missing_file_is_empty_queue(tmp_path: Path) -> None:
    queue = PendingQueue(tmp_path / "nope.json")
    assert queue.items == [] and queue.earliest_retry() is None


def test_retry_after_reset_time() -> None:
    assert next_retry_at(resets_at=1_000.0, now=500.0) == 1_000.0 + RESET_GRACE_SECONDS


def test_retry_fallback_without_or_with_stale_reset() -> None:
    assert next_retry_at(None, now=500.0) == 500.0 + FALLBACK_RETRY_SECONDS
    assert next_retry_at(400.0, now=500.0) == 500.0 + FALLBACK_RETRY_SECONDS


def test_limit_message_uses_local_time() -> None:
    summer = datetime(2026, 7, 1, 16, 30, tzinfo=timezone.utc).timestamp()
    winter = datetime(2026, 12, 1, 16, 30, tzinfo=timezone.utc).timestamp()
    assert limit_message(winter, ZoneInfo("Europe/Oslo")) == "⏳ limit hit, back at 17:30"
    assert limit_message(summer, ZoneInfo("Europe/Oslo")) == "⏳ limit hit, back at 18:30"


def test_v1_pending_keys_migrate_to_dm(tmp_path: Path) -> None:
    import json

    path = tmp_path / "pending.json"
    item = {"user_key": "123456789012345678", "text": "add milk", "queued_at": 1.0, "retry_at": 2.0,
            "attempts": 0, "item_id": "abc"}
    path.write_text(json.dumps({"items": [item, {**item, "user_key": "pub:8", "item_id": "def"}]}), encoding="utf-8")
    items = PendingQueue(path).items
    assert [i.user_key for i in items] == ["dm:123456789012345678", "pub:8"]
    assert items[0].preamble == ""
