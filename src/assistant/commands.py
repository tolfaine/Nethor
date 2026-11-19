"""Slash commands /status /context /model /effort /fast /new /delete /remote: owner-only, ephemeral, per conversation."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Any, Literal

import discord
from discord import app_commands

from assistant.keys import DM, PUBLIC, THREAD, THREAD_KINDS, key_id, key_kind, make_key, mode_for_key
from assistant.remote import PRIVATE_ONLY
from assistant.sessions import Origin
from assistant.settings import (
    DEFAULT_EFFORT, EFFORTS, MODELS, Change, SettingError, format_context, format_status,
)
from assistant.views import ApprovalView, Wording

if TYPE_CHECKING:
    from assistant.bot import AssistantBot, Route

log = logging.getLogger("assistant.commands")

NOT_ALLOWED = "⛔ Only Nethor's owner can use this."
NOT_HERE = "Use this inside a thread, forum post or DM where Nethor is talking."
FAILED = "⚠️ That didn't work; details are in the log."
REPLY_LIMIT = 2000
DELETE_PROMPT = "Delete this thread and its conversation? ✅/❌"
DELETE_WORDING = Wording("Delete", "Cancel", "✅ deleting…", "❌ cancelled, nothing changed")
DELETE_CONFIRM_SECONDS = 2 * 60
DELETE_TIMED_OUT = "⌛ timed out after 2 min: cancelled, nothing changed"
DM_NOT_DELETABLE = "DMs can't be deleted; use /new to start fresh."
DELETE_WAITS = "⏳ A reply is running: the thread is deleted right after it."
DELETE_FORBIDDEN = ("⚠️ I couldn't delete this thread: I need the **Manage Threads** permission here. "
                    "The thread is still there; its conversation has ended.")
DELETE_FAILED = "⚠️ I couldn't delete this thread; details are in the log. Its conversation has ended."
SUMMARY_NOT_SAVED = "⚠️ The deleted conversation's summary wasn't saved to Notion; details are in the log."
DeleteOutcome = Literal["deleted", "already gone", "forbidden", "failed"]
# Global commands only in the bot's DM; servers get their own instant copy, so no command shows twice there.
DM_CONTEXT = app_commands.AppCommandContext(guild=False, dm_channel=True, private_channel=False)
GUILD_CONTEXT = app_commands.AppCommandContext(guild=True, dm_channel=False, private_channel=False)

Work = Callable[["Route"], Awaitable[str]]
Callback = Callable[..., Awaitable[None]]


def model_choices(default_model: str) -> list[app_commands.Choice[str]]:
    return [app_commands.Choice(name=f"{m.label} (default)" if m.model_id == default_model else m.label,
                                value=m.model_id) for m in MODELS]


def effort_choices() -> list[app_commands.Choice[str]]:
    return [app_commands.Choice(name=f"{e} (default)" if e == DEFAULT_EFFORT else e, value=e) for e in EFFORTS]


FAST_CHOICES = [app_commands.Choice(name="on", value="on"), app_commands.Choice(name="off", value="off")]


def origin_of(route: Route) -> Origin:
    return Origin(route.mode, route.channel_id, route.owner_id)


def delete_refusal(key: str) -> str | None:
    kind = key_kind(key)
    if kind == DM:
        return DM_NOT_DELETABLE
    return None if kind in THREAD_KINDS else NOT_HERE


def remote_refusal(key: str) -> str | None:
    if key_kind(key) in (DM, THREAD):
        return None
    return PRIVATE_ONLY if mode_for_key(key) == PUBLIC else NOT_HERE


def remote_name(assistant: str, key: str, channel: Any) -> str:
    """How the conversation is listed in claude.ai: the thread or forum post name, or DM."""
    where = "DM" if key_kind(key) == DM else (getattr(channel, "name", None) or "thread")
    return f"{assistant} · {where}"


def sibling_keys(key: str) -> list[str]:
    """The thread's other conversation: a private thread that went public has both `thread:` and `pthread:`."""
    thread_id = key_id(key)
    if thread_id is None:
        return []
    return [other for kind in THREAD_KINDS if (other := make_key(kind, thread_id)) != key]


def delete_report(outcome: DeleteOutcome, summary_saved: bool) -> tuple[str | None, bool]:
    """(note for the user or None, whether the thread is gone)."""
    gone = outcome in ("deleted", "already gone")
    note = None if gone else (DELETE_FORBIDDEN if outcome == "forbidden" else DELETE_FAILED)
    if not summary_saved:
        note = f"{note}\n{SUMMARY_NOT_SAVED}" if note else SUMMARY_NOT_SAVED
    return note, gone


async def delete_thread(channel: Any) -> DeleteOutcome:
    channel_id = getattr(channel, "id", "?")
    try:
        await channel.delete()
    except discord.NotFound:
        log.warning("thread delete: already gone channel=%s", channel_id)
        return "already gone"
    except discord.Forbidden as exc:
        log.error("thread delete refused channel=%s status=%s code=%s: %s (needs Manage Threads)", channel_id,
                  exc.status, exc.code, exc.text)
        return "forbidden"
    except discord.HTTPException:
        log.exception("thread delete failed channel=%s", channel_id)
        return "failed"
    return "deleted"


class CommandHandler:
    def __init__(self, bot: AssistantBot) -> None:
        self._bot = bot

    def _manager(self) -> Any:
        if self._bot.manager is None:
            raise RuntimeError("AssistantBot.manager is not set")
        return self._bot.manager

    async def status(self, interaction: discord.Interaction) -> None:
        async def work(route: Route) -> str:
            return format_status(await self._manager().status(route.key), time.time())

        await self.run(interaction, "status", "", work)

    async def context(self, interaction: discord.Interaction) -> None:
        async def work(route: Route) -> str:
            return format_context(await self._manager().status(route.key))

        await self.run(interaction, "context", "", work)

    async def change(self, interaction: discord.Interaction, change: Change) -> None:
        async def work(route: Route) -> str:
            try:
                return await self._manager().apply_setting(route.key, change, origin_of(route))
            except SettingError as exc:
                return f"⚠️ {exc}"

        await self.run(interaction, change.kind, change.describe(), work)

    async def new(self, interaction: discord.Interaction) -> None:
        async def work(route: Route) -> str:
            return await self._manager().new_conversation(route.key, origin_of(route))

        await self.run(interaction, "new", "", work)

    async def remote(self, interaction: discord.Interaction, state: str) -> None:
        admitted = await self._admit(interaction, "remote")
        if admitted is None:
            return
        channel, route, reason = admitted

        async def work(route: Route) -> str:
            refusal = remote_refusal(route.key)
            if refusal is not None:
                return refusal
            if state == "off":
                return await self._manager().disable_remote(route.key)
            name = remote_name(self._bot.config.assistant_name, route.key, channel)
            return await self._manager().enable_remote(route.key, name, origin_of(route))

        await self._finish(interaction, "remote", f"remote={state}", route, reason, work)

    async def run(self, interaction: discord.Interaction, name: str, change: str, work: Work) -> None:
        admitted = await self._admit(interaction, name)
        if admitted is None:
            return
        _, route, reason = admitted
        await self._finish(interaction, name, change, route, reason, work)

    async def _finish(self, interaction: discord.Interaction, name: str, change: str, route: Route, reason: str,
                      work: Work) -> None:
        user = interaction.user.id
        try:
            text = await work(route)
        except Exception:
            log.exception("command failed command=%s key=%s user=%s change=%s", name, route.key, user, change)
            text = FAILED
        log.info("command=%s key=%s user=%s change=%s reason=%s outcome=%r", name, route.key, user, change or "-",
                 reason, text[:300])
        await self._reply(interaction, text)

    async def _admit(self, interaction: discord.Interaction, name: str) -> tuple[Any, Route, str] | None:
        """Allowlist, defer, find the conversation; None = already answered with a refusal."""
        user = interaction.user.id
        if user not in self._bot.config.allowed_user_ids:
            log.warning("command refused command=%s user=%s name=%s channel=%s: not on ALLOWED_USER_IDS", name, user,
                        interaction.user, interaction.channel_id)
            await interaction.response.send_message(NOT_ALLOWED, ephemeral=True)
            return None
        # Anything below may fetch a channel or (re)connect a CLI process: past Discord's 3 s reply window.
        await interaction.response.defer(ephemeral=True, thinking=True)
        channel, route, reason = await self._bot.command_target(interaction)
        if route is None:
            log.info("command not in a conversation command=%s user=%s channel=%s reason=%s", name, user,
                     interaction.channel_id, reason)
            await self._reply(interaction, NOT_HERE)
            return None
        return channel, route, reason

    async def _reply(self, interaction: discord.Interaction, text: str) -> bool:
        try:
            await interaction.followup.send(text[:REPLY_LIMIT], ephemeral=True)
        except discord.HTTPException:
            log.exception("command reply failed channel=%s text=%r", interaction.channel_id, text[:200])
            return False
        return True

    async def delete(self, interaction: discord.Interaction) -> None:
        admitted = await self._admit(interaction, "delete")
        if admitted is None:
            return
        channel, route, _ = admitted
        refusal = delete_refusal(route.key)
        if refusal is not None:
            log.info("command=delete key=%s user=%s outcome=refused (%s)", route.key, interaction.user.id,
                     key_kind(route.key))
            await self._reply(interaction, refusal)
            return
        if await self._confirm_delete(interaction, route.key):
            await self._delete(interaction, channel, route.key)

    async def _confirm_delete(self, interaction: discord.Interaction, key: str) -> bool:
        user = interaction.user.id
        view = ApprovalView(user, DELETE_CONFIRM_SECONDS, DELETE_WORDING)
        try:
            message = await interaction.followup.send(DELETE_PROMPT, view=view, ephemeral=True, wait=True)
        except discord.HTTPException:
            log.exception("delete confirmation failed key=%s user=%s", key, user)
            return False
        if await view.wait():
            log.info("command=delete key=%s user=%s outcome=timed out", key, user)
            try:
                await message.edit(content=f"{DELETE_PROMPT}\n→ {DELETE_TIMED_OUT}", view=None)
            except discord.HTTPException:
                log.exception("delete timeout note failed key=%s user=%s", key, user)
            return False
        if not view.approved:
            log.info("command=delete key=%s user=%s outcome=cancelled", key, user)
        return bool(view.approved)

    async def _delete(self, interaction: discord.Interaction, channel: Any, key: str) -> None:
        manager, user = self._manager(), interaction.user.id
        keys = [key, *(other for other in sibling_keys(key) if manager.has_state(other))]
        if manager.busy(keys):
            await self._reply(interaction, DELETE_WAITS)
        try:
            async with manager.holding(keys):
                wrap_ups = [await manager.end_locked(k) for k in keys]
                outcome = await delete_thread(channel)
        except Exception:
            log.exception("command failed command=delete keys=%s user=%s", keys, user)
            await self._tell(interaction, FAILED, thread_gone=False)
            return
        wrap_up = "failed" if "failed" in wrap_ups else ("saved" if "saved" in wrap_ups else "skipped")
        log.info("command=delete keys=%s user=%s wrap_up=%s delete=%s", keys, user, wrap_up, outcome)
        note, gone = delete_report(outcome, wrap_up != "failed")
        if note is not None:
            await self._tell(interaction, note, gone)

    async def _tell(self, interaction: discord.Interaction, text: str, thread_gone: bool) -> None:
        """An ephemeral note needs the thread and a live interaction token; otherwise it goes to the DM."""
        if not thread_gone and await self._reply(interaction, text):
            return
        try:
            user = await self._bot.fetch_user(interaction.user.id)
            await (await user.create_dm()).send(text)
        except discord.HTTPException:
            log.exception("DM note failed user=%s text=%r", interaction.user.id, text[:200])


def _callbacks(handler: CommandHandler, default_model: str) -> list[tuple[str, str, Callback]]:
    async def status(interaction: discord.Interaction) -> None:
        await handler.status(interaction)

    async def context(interaction: discord.Interaction) -> None:
        await handler.context(interaction)

    @app_commands.describe(model="Model for this conversation")
    @app_commands.choices(model=model_choices(default_model))
    async def model(interaction: discord.Interaction, model: str) -> None:
        await handler.change(interaction, Change("model", model))

    @app_commands.describe(level="How hard it thinks")
    @app_commands.choices(level=effort_choices())
    async def effort(interaction: discord.Interaction, level: str) -> None:
        await handler.change(interaction, Change("effort", level))

    @app_commands.describe(state="Fast mode on or off")
    @app_commands.choices(state=FAST_CHOICES)
    async def fast(interaction: discord.Interaction, state: str) -> None:
        await handler.change(interaction, Change("fast", state))

    async def new(interaction: discord.Interaction) -> None:
        await handler.new(interaction)

    async def delete(interaction: discord.Interaction) -> None:
        await handler.delete(interaction)

    @app_commands.describe(state="on (default) shows the link; off disconnects it")
    @app_commands.choices(state=FAST_CHOICES)
    async def remote(interaction: discord.Interaction, state: str = "on") -> None:
        await handler.remote(interaction, state)

    return [
        ("status", "Model, effort, fast mode and context use of this conversation", status),
        ("context", "What fills this conversation's context", context),
        ("model", "Switch this conversation's model", model),
        ("effort", "Set this conversation's effort", effort),
        ("fast", "Fast mode on or off (Opus 5.5 / Fable 5.2)", fast),
        ("new", "Start a fresh conversation here (like !new)", new),
        ("delete", "Delete this thread or forum post and end its conversation", delete),
        ("remote", "Open this private conversation in claude.ai or the Claude app (Remote Control)", remote),
    ]


def build_commands(handler: CommandHandler, default_model: str,
                   contexts: app_commands.AppCommandContext) -> list[app_commands.Command[Any, ..., None]]:
    return [app_commands.Command(name=name, description=description, callback=callback, allowed_contexts=contexts)
            for name, description, callback in _callbacks(handler, default_model)]


def install_commands(tree: app_commands.CommandTree[Any], handler: CommandHandler, guild_ids: Iterable[int],
                     default_model: str) -> None:
    for command in build_commands(handler, default_model, DM_CONTEXT):
        tree.add_command(command)
    for guild_id in guild_ids:
        for command in build_commands(handler, default_model, GUILD_CONTEXT):
            tree.add_command(command, guild=discord.Object(guild_id))


async def sync_commands(tree: app_commands.CommandTree[Any], guild_ids: Iterable[int]) -> None:
    """Server commands appear at once; DM (global) ones can take a while the first time. Failures only log."""
    scopes: list[int | None] = [*sorted(guild_ids), None]
    for guild_id in scopes:
        where = f"guild={guild_id}" if guild_id is not None else "global (DMs)"
        try:
            synced = await tree.sync(guild=discord.Object(guild_id) if guild_id is not None else None)
        except Exception:
            log.exception("slash command sync failed %s", where)
            continue
        log.info("synced %d slash commands %s: %s", len(synced), where, sorted(c.name for c in synced))
