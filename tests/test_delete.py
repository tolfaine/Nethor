from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace as NS
from typing import Any

import discord
import pytest

from assistant import commands
from assistant.agent import TurnResult
from assistant.commands import (
    DELETE_FAILED, DELETE_FORBIDDEN, DELETE_PROMPT, DELETE_WAITS, DM_NOT_DELETABLE, NOT_ALLOWED, NOT_HERE,
    SUMMARY_NOT_SAVED, delete_refusal, delete_report,
)
from assistant.sessions import WRAP_UP, Origin
from assistant.views import ApprovalView
from test_commands import FakeResponse, live  # noqa: F401  (fixture)
from test_private_channel import THREAD_ID, hidden, thread_in
from test_routing import OWNER, STRANGER, dm_channel, text_channel, thread

PRIVATE_KEY = f"thread:{THREAD_ID}"
PUBLIC_ID = 710
PUBLIC_KEY = f"pthread:{PUBLIC_ID}"


class FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content
        self.edits: list[dict[str, Any]] = []

    async def edit(self, **kwargs: Any) -> None:
        self.edits.append(kwargs)


class Clicker:
    """Followup whose confirm view gets ✅, ❌ or no click at all (then it times out)."""

    def __init__(self, click: str, fail_notes: bool = False) -> None:
        self.click = click
        self.fail_notes = fail_notes
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.views: list[ApprovalView] = []
        self.messages: list[FakeMessage] = []

    async def send(self, content: str, **kwargs: Any) -> FakeMessage:
        view = kwargs.get("view")
        if view is None and self.fail_notes:
            raise discord.HTTPException(NS(status=404, reason="Not Found"), "Unknown Webhook")
        self.sent.append((content, kwargs))
        message = FakeMessage(content)
        if view is not None:
            self.views.append(view)
            self.messages.append(message)
            await self._answer(view, message)
        return message

    async def _answer(self, view: ApprovalView, message: FakeMessage) -> None:
        if self.click == "timeout":
            view._start_listening_from_store(NS(remove_view=lambda v: None))  # type: ignore[arg-type]
            return
        button = view.approve if self.click == "approve" else view.deny
        await button.callback(NS(user=NS(id=OWNER), message=message, response=NS(edit_message=message.edit)))


def confirm_interaction(channel: Any, click: str = "approve", user: int = OWNER, guild: int | None = 500,
                        fail_notes: bool = False) -> NS:
    return NS(user=NS(id=user), channel=channel, channel_id=channel.id, guild_id=guild, response=FakeResponse(),
              followup=Clicker(click, fail_notes))


def notes(inter: NS) -> list[str]:
    return [content for content, kwargs in inter.followup.sent if "view" not in kwargs]


def deletable(channel: Any, events: list[Any], error: Exception | None = None) -> Any:
    async def delete() -> None:
        events.append("delete")
        if error is not None:
            raise error

    channel.delete = delete
    return channel


def trace(manager: Any, agent: Any) -> list[Any]:
    """Order of wrap-up turn, client close, record drop, queue drop (thread.delete is added by `deletable`)."""
    events: list[Any] = []
    run_turn, close_session = agent.run_turn, agent.close_session
    store_drop, queue_drop = manager._store.drop, manager._queue.drop_key

    async def traced_turn(key: str, prompt: str, *args: Any, **kwargs: Any) -> TurnResult:
        events.append(("wrap-up" if WRAP_UP in prompt else "turn", key))
        return await run_turn(key, prompt, *args, **kwargs)

    async def traced_close(key: str) -> None:
        events.append(("close", key))
        await close_session(key)

    def traced_store_drop(key: str) -> None:
        events.append(("drop", key))
        store_drop(key)

    def traced_queue_drop(key: str) -> int:
        events.append(("pending", key))
        return queue_drop(key)

    agent.run_turn, agent.close_session = traced_turn, traced_close
    manager._store.drop, manager._queue.drop_key = traced_store_drop, traced_queue_drop
    return events


async def run_delete(bot: Any, inter: NS) -> None:
    command = bot.tree.get_command("delete")
    assert command is not None
    await command.callback(inter)


async def private_thread(manager: Any) -> Any:
    await manager.handle(PRIVATE_KEY, "hi", Origin("private", THREAD_ID, OWNER))
    return thread_in(hidden())


async def public_thread(manager: Any) -> Any:
    await manager.handle(PUBLIC_KEY, "hi", Origin("public", PUBLIC_ID, OWNER))
    return thread(tid=PUBLIC_ID, parent=601)


def ended(manager: Any, agent: Any, key: str) -> bool:
    return key not in manager._store.keys() and key in agent.closed and key not in agent.live


# --- pure helpers ------------------------------------------------------------------------------------------------

def test_refusal_per_key_kind() -> None:
    assert delete_refusal(f"dm:{OWNER}") == DM_NOT_DELETABLE and "/new" in DM_NOT_DELETABLE
    assert delete_refusal(PRIVATE_KEY) is None and delete_refusal(PUBLIC_KEY) is None
    assert delete_refusal("priv:800") == NOT_HERE and delete_refusal("pub:800") == NOT_HERE


def test_report_per_outcome() -> None:
    assert delete_report("deleted", True) == (None, True)
    assert delete_report("already gone", False) == (SUMMARY_NOT_SAVED, True)
    assert delete_report("forbidden", True) == (DELETE_FORBIDDEN, False) and "Manage Threads" in DELETE_FORBIDDEN
    note, gone = delete_report("failed", False)
    assert not gone and note is not None and note.startswith(DELETE_FAILED) and SUMMARY_NOT_SAVED in note


# --- confirm view --------------------------------------------------------------------------------------------------

async def test_confirm_view_is_owner_only_with_delete_wording() -> None:
    view = ApprovalView(OWNER, 120, commands.DELETE_WORDING)
    assert view.timeout == 120 and (view.approve.label, view.deny.label) == ("Delete", "Cancel")
    assert await view.interaction_check(NS(user=NS(id=OWNER)))  # type: ignore[arg-type]
    assert not await view.interaction_check(NS(user=NS(id=STRANGER)))  # type: ignore[arg-type]


async def test_approval_view_keeps_its_defaults() -> None:
    view = ApprovalView(OWNER)
    assert view.timeout == 10 * 60 and (view.approve.label, view.deny.label) == ("Approve", "Deny")


async def test_timeout_cancels_and_says_so(live, monkeypatch, caplog) -> None:  # noqa: F811
    bot, manager, agent = live
    monkeypatch.setattr(commands, "DELETE_CONFIRM_SECONDS", 0.01)
    channel = await private_thread(manager)
    events = trace(manager, agent)
    deletable(channel, events)
    inter = confirm_interaction(channel, "timeout")
    with caplog.at_level(logging.INFO):
        await run_delete(bot, inter)
    edit = inter.followup.messages[0].edits[-1]
    assert edit["view"] is None and "timed out" in edit["content"] and "cancelled" in edit["content"]
    assert events == [] and PRIVATE_KEY in manager._store.keys()
    assert f"command=delete key={PRIVATE_KEY} user={OWNER} outcome=timed out" in caplog.text


# --- confirmed ---------------------------------------------------------------------------------------------------

async def test_private_delete_wraps_up_then_closes_drops_and_deletes(live, caplog) -> None:  # noqa: F811
    bot, manager, agent = live
    channel = await private_thread(manager)
    manager._queue.add(PRIVATE_KEY, "queued", 0, 0)
    manager._queue.add(f"dm:{OWNER}", "other", 0, 0)
    events = trace(manager, agent)
    deletable(channel, events)
    inter = confirm_interaction(channel)
    with caplog.at_level(logging.INFO):
        await run_delete(bot, inter)
    prompt, kwargs = inter.followup.sent[0]
    assert prompt == DELETE_PROMPT and kwargs["ephemeral"] and inter.followup.views[0].timeout == 120
    assert events == [("wrap-up", PRIVATE_KEY), ("close", PRIVATE_KEY), ("drop", PRIVATE_KEY),
                      ("pending", PRIVATE_KEY), "delete"]
    assert ended(manager, agent, PRIVATE_KEY) and [i.user_key for i in manager._queue.items] == [f"dm:{OWNER}"]
    assert notes(inter) == [] and not bot.dms  # type: ignore[attr-defined]
    assert f"command=delete keys=['{PRIVATE_KEY}'] user={OWNER} wrap_up=saved delete=deleted" in caplog.text


async def test_public_delete_has_no_wrap_up(live, caplog) -> None:  # noqa: F811
    bot, manager, agent = live
    channel = await public_thread(manager)
    events = trace(manager, agent)
    deletable(channel, events)
    with caplog.at_level(logging.INFO):
        await run_delete(bot, confirm_interaction(channel))
    assert events == [("close", PUBLIC_KEY), ("drop", PUBLIC_KEY), ("pending", PUBLIC_KEY), "delete"]
    assert ended(manager, agent, PUBLIC_KEY) and "wrap_up=skipped delete=deleted" in caplog.text


async def test_thread_that_went_public_ends_both_conversations(live) -> None:  # noqa: F811
    bot, manager, agent = live
    channel = await private_thread(manager)
    channel.parent.permissions_for = lambda obj: NS(view_channel=True)
    await manager.handle(f"pthread:{THREAD_ID}", "hi", Origin("public", THREAD_ID, OWNER))
    events = trace(manager, agent)
    deletable(channel, events)
    await run_delete(bot, confirm_interaction(channel))
    assert [e for e in events if e[0] == "wrap-up"] == [("wrap-up", PRIVATE_KEY)] and events[-1] == "delete"
    assert ended(manager, agent, PRIVATE_KEY) and ended(manager, agent, f"pthread:{THREAD_ID}")


async def test_cancel_changes_nothing(live) -> None:  # noqa: F811
    bot, manager, agent = live
    channel = await private_thread(manager)
    events = trace(manager, agent)
    deletable(channel, events)
    inter = confirm_interaction(channel, "deny")
    await run_delete(bot, inter)
    assert events == [] and manager.record(PRIVATE_KEY).session_id and PRIVATE_KEY in agent.live
    assert "cancelled" in inter.followup.messages[0].edits[-1]["content"]


async def test_busy_conversation_is_deleted_right_after_the_reply(live) -> None:  # noqa: F811
    bot, manager, agent = live
    channel = await private_thread(manager)
    events = trace(manager, agent)
    deletable(channel, events)
    inter = confirm_interaction(channel)
    lock = manager.lock(PRIVATE_KEY)
    await lock.acquire()
    task = asyncio.create_task(run_delete(bot, inter))
    for _ in range(50):
        await asyncio.sleep(0)
    assert notes(inter) == [DELETE_WAITS] and events == []
    lock.release()
    await task
    assert events[-1] == "delete" and ended(manager, agent, PRIVATE_KEY)


async def test_wrap_up_failure_still_deletes_and_says_so_in_the_dm(live, caplog) -> None:  # noqa: F811
    bot, manager, agent = live
    channel = await private_thread(manager)
    agent.results = [TurnResult("", "sess-1", error="boom")]
    events = trace(manager, agent)
    deletable(channel, events)
    with caplog.at_level(logging.INFO):
        await run_delete(bot, confirm_interaction(channel))
    assert events[-1] == "delete" and ended(manager, agent, PRIVATE_KEY)
    assert bot.dms[OWNER].sent[-1][0] == SUMMARY_NOT_SAVED  # type: ignore[attr-defined]
    assert "wrap_up=failed delete=deleted" in caplog.text


async def test_forbidden_asks_for_manage_threads_and_conversation_stays_closed(live, caplog) -> None:  # noqa: F811
    bot, manager, agent = live
    channel = await private_thread(manager)
    events = trace(manager, agent)
    deletable(channel, events, discord.Forbidden(NS(status=403, reason="Forbidden"), "Missing Permissions"))
    inter = confirm_interaction(channel)
    with caplog.at_level(logging.INFO):
        await run_delete(bot, inter)
    assert notes(inter) == [DELETE_FORBIDDEN] and ended(manager, agent, PRIVATE_KEY)
    assert inter.followup.sent[-1][1] == {"ephemeral": True} and not bot.dms  # type: ignore[attr-defined]
    assert "delete=forbidden" in caplog.text and "Manage Threads" in caplog.text


async def test_note_falls_back_to_the_dm_when_the_followup_fails(live) -> None:  # noqa: F811
    bot, manager, agent = live
    channel = await private_thread(manager)
    deletable(channel, [], discord.Forbidden(NS(status=403, reason="Forbidden"), "Missing Permissions"))
    await run_delete(bot, confirm_interaction(channel, fail_notes=True))
    assert bot.dms[OWNER].sent[-1][0] == DELETE_FORBIDDEN  # type: ignore[attr-defined]


# --- refused -----------------------------------------------------------------------------------------------------

async def test_dm_is_refused_and_nothing_ends(live) -> None:  # noqa: F811
    bot, manager, agent = live
    await manager.handle(f"dm:{OWNER}", "hi", Origin("private", 10, OWNER))
    inter = confirm_interaction(dm_channel(), guild=None)
    await run_delete(bot, inter)
    assert notes(inter) == [DM_NOT_DELETABLE] and not inter.followup.views
    assert manager.record(f"dm:{OWNER}").session_id and agent.closed == []


@pytest.mark.parametrize("channel", [text_channel(), thread_in(hidden())])
async def test_plain_channel_or_thread_without_conversation_is_refused(live, channel: Any) -> None:  # noqa: F811
    bot, _, agent = live
    inter = confirm_interaction(channel)
    await run_delete(bot, inter)
    assert notes(inter) == [NOT_HERE] and not inter.followup.views and agent.closed == []


async def test_stranger_is_refused_and_logged(live, caplog) -> None:  # noqa: F811
    bot, manager, agent = live
    channel = await private_thread(manager)
    inter = confirm_interaction(channel, user=STRANGER)
    await run_delete(bot, inter)
    assert inter.response.sent == [(NOT_ALLOWED, {"ephemeral": True})] and not inter.followup.sent
    assert f"command refused command=delete user={STRANGER}" in caplog.text and agent.closed == []


# --- queue -------------------------------------------------------------------------------------------------------

async def test_queued_message_dropped_while_waiting_is_not_answered(live) -> None:  # noqa: F811
    _, manager, agent = live
    manager._queue.add(PRIVATE_KEY, "late", 0, 0)
    delivered: list[str] = []

    async def deliver(key: str, text: str) -> None:
        delivered.append(text)

    lock = manager.lock(PRIVATE_KEY)
    await lock.acquire()
    tick = asyncio.create_task(manager.tick(deliver))
    for _ in range(20):
        await asyncio.sleep(0)
    assert manager._queue.drop_key(PRIVATE_KEY) == 1
    lock.release()
    await tick
    assert agent.calls == [] and delivered == []
