"""Claude Code Remote Control for one conversation: the control request, its replies, and the stream pump."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from claude_agent_sdk import CLIConnectionError, ResultMessage, SystemMessage

log = logging.getLogger("assistant.remote")

REMOTE_TIMEOUT_SECONDS = 120
REATTACH_TIMEOUT_SECONDS = 30
UNSUPPORTED = "Remote Control isn't supported by this SDK version"
NOT_CONNECTED = "Claude Code isn't connected for this conversation"
PRIVATE_ONLY = "Remote Control is only for private conversations."
REMOTE_ON = ("🔗 Remote Control is on: {url}\nOpen it in claude.ai or the Claude app; it carries this conversation's "
             "history. Nethor switches back to auto mode before each reply here.")
REMOTE_ALREADY_ON = "🔗 Remote Control is already on: {url}"
REMOTE_OFF = "Remote Control: off."
REMOTE_ALREADY_OFF = "Remote Control is already off."
REMOTE_FAILED = "⚠️ Remote Control didn't start: {reason}"
FAILED_STATE = "failed"


class RemoteError(Exception):
    pass


@dataclass(frozen=True)
class RemoteSpec:
    """`name` is shown in the app; `reattach_id` is a saved bridge_session_id to reconnect to."""

    name: str | None = None
    reattach_id: str | None = None


@dataclass(frozen=True)
class RemoteLink:
    session_url: str
    bridge_session_id: str | None


@dataclass(frozen=True)
class RemoteInfo:
    link: RemoteLink | None
    state: str | None


def remote_request(enabled: bool, spec: RemoteSpec | None = None) -> dict[str, Any]:
    if not enabled:
        return {"subtype": "remote_control", "enabled": False}
    spec = spec or RemoteSpec()
    request: dict[str, Any] = {"subtype": "remote_control", "enabled": True}
    if spec.name:
        request["name"] = spec.name
    if spec.reattach_id:
        request["reattach_session_id"] = spec.reattach_id
    request["keep_session_on_exit"] = True
    return request


def _takes(function: Callable[..., Any], names: tuple[str, ...]) -> bool:
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        return False
    return all(name in parameters for name in names)


def control_sender(client: Any) -> Callable[..., Any]:
    """The SDK's private `_query._send_control_request(request, timeout=...)`; there is no public API for it."""
    if not hasattr(client, "_query"):
        raise RemoteError(UNSUPPORTED)
    query = client._query
    if query is None:
        raise RemoteError(NOT_CONNECTED)
    send = getattr(query, "_send_control_request", None)
    if send is None or not _takes(send, ("request", "timeout")):
        raise RemoteError(UNSUPPORTED)
    return send  # type: ignore[no-any-return]


async def send_remote_control(client: Any, request: dict[str, Any],
                              timeout: float = REMOTE_TIMEOUT_SECONDS) -> dict[str, Any]:
    send = control_sender(client)
    try:
        response = await send(request, timeout=timeout)
    except Exception as exc:
        raise RemoteError(str(exc) or type(exc).__name__) from exc
    return response if isinstance(response, dict) else {}


def parse_link(response: dict[str, Any]) -> RemoteLink:
    url = response.get("session_url")
    if not isinstance(url, str) or not url:
        raise RemoteError(f"no session_url in the reply (keys: {sorted(response)})")
    bridge = response.get("bridge_session_id")
    return RemoteLink(url, bridge if isinstance(bridge, str) and bridge else None)


def bridge_state_of(message: object) -> str | None:
    if isinstance(message, SystemMessage) and message.subtype == "bridge_state":
        state = message.data.get("state")
        return str(state) if state else None
    return None


def permission_mode_of(message: object) -> str | None:
    """`system/status` reports every permission-mode change; `system/init` the mode a turn runs in."""
    if isinstance(message, SystemMessage) and message.subtype in ("status", "init"):
        mode = message.data.get("permissionMode")
        return str(mode) if mode else None
    return None


def answers(result: ResultMessage, message_id: str) -> bool:
    """Whether this result is for our prompt; an app-typed turn has its own uuid. No uuids (old CLI) = ours."""
    if result.user_message_uuids:
        return message_id in result.user_message_uuids
    if result.user_message_uuid:
        return result.user_message_uuid == message_id
    return True


def log_bridge_state(user_key: str, message: SystemMessage) -> None:
    data = message.data
    level = logging.WARNING if data.get("state") in (FAILED_STATE, "policy_disabled") else logging.INFO
    log.log(level, "bridge_state key=%s state=%s detail=%s failure_kind=%s recovered=%s epoch=%s", user_key,
            data.get("state"), data.get("detail"), data.get("failure_kind"), data.get("recovered"),
            data.get("bridge_epoch"))


Observer = Callable[[object], None]


class MessagePump:
    """Reads a Remote Control client's stream all the time.

    Turns typed in the app write to the same stream; unread, they would fill the SDK's 100-message buffer and
    stall the CLI. A Discord turn takes the messages only while it runs.
    """

    def __init__(self, client: Any, user_key: str, observe: Observer) -> None:
        self._client = client
        self._user_key = user_key
        self._observe = observe
        self._waiting: asyncio.Queue[object] | None = None
        self._failure: BaseException | None = None
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            async for message in self._client.receive_messages():
                self._observe(message)
                if self._waiting is not None:
                    self._waiting.put_nowait(message)
                elif isinstance(message, ResultMessage):
                    log.info("app turn done key=%s session=%s error=%s reply=%r", self._user_key,
                             message.session_id, message.is_error, (message.result or "")[:200])
        except Exception as exc:
            log.exception("message stream failed key=%s", self._user_key)
            self._fail(exc)
            return
        self._fail(CLIConnectionError("the CLI's message stream ended"))

    def _fail(self, exc: BaseException) -> None:
        self._failure = exc
        if self._waiting is not None:
            self._waiting.put_nowait(exc)

    def begin_turn(self) -> None:
        """Before the prompt is sent, so none of the turn's messages can be missed."""
        self._waiting = asyncio.Queue()
        if self._failure is not None:
            self._waiting.put_nowait(self._failure)

    def end_turn(self) -> None:
        self._waiting = None

    async def turn_messages(self) -> AsyncIterator[object]:
        queue = self._waiting
        if queue is None:
            raise RuntimeError("turn_messages() without begin_turn()")
        while True:
            item = await queue.get()
            if isinstance(item, BaseException):
                raise item
            yield item

    async def stop(self) -> None:
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._fail(CLIConnectionError("the client was closed"))


def remote_on_text(url: str, already: bool) -> str:
    return (REMOTE_ALREADY_ON if already else REMOTE_ON).format(url=url)
