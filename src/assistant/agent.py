"""Claude Agent SDK wrapper: one persistent client per conversation key, every tool call tier-checked."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol, cast

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ClaudeSDKError,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    RateLimitEvent,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolPermissionContext,
)

from assistant.config import Config
from assistant.keys import PRIVATE, PUBLIC, Mode, mode_for_key
from assistant.permissions import KEPT_SERVERS, PUBLIC_TOOLS, Decision, Targets, Tier, decide, is_listed, server_prefix
from assistant.remote import (
    FAILED_STATE, REATTACH_TIMEOUT_SECONDS, REMOTE_TIMEOUT_SECONDS, MessagePump, RemoteError, RemoteInfo, RemoteLink,
    RemoteSpec, answers, bridge_state_of, log_bridge_state, parse_link, permission_mode_of, remote_request,
    send_remote_control,
)
from assistant.settings import FAST_SETTINGS, ContextSnapshot, ConversationSettings, Effective

log = logging.getLogger("assistant.agent")
tool_log = logging.getLogger("assistant.tools")
cli_log = logging.getLogger("assistant.cli")

BUILTIN_TOOLS = ["WebSearch", "WebFetch"]
MAX_TURNS = 40
TURN_TIMEOUT_SECONDS = 20 * 60
SERVER_SETTLE_SECONDS = 60
# The agent's rules come only from agent_home/CLAUDE.md via the system prompt; never the dev CLAUDE.md above it.
CLI_ENV = {"CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
# Public clients drop claude.ai connectors per process: toggle_mcp_server persists per project in
# ~/.claude.json, so a public toggle-off would also switch them off for every private client.
PUBLIC_ENV = {**CLI_ENV, "ENABLE_CLAUDEAI_MCP_SERVERS": "false"}
MEMORY_OFF = "none (memory is off: skip the session-start fetch, save nothing, and say so if asked to remember)"


@dataclass(frozen=True)
class ApprovalRequest:
    user_key: str
    tool_name: str
    tool_input: dict[str, Any]
    reason: str

    def summary(self, limit: int = 1500) -> str:
        args = json.dumps(self.tool_input, ensure_ascii=False, indent=1)
        if len(args) > limit:
            args = args[:limit] + " …"
        return f"{self.tool_name.removeprefix('mcp__claude_ai_')}\nWhy asking: {self.reason}\n{args}"


class Approver(Protocol):
    async def approve(self, request: ApprovalRequest) -> bool: ...


@dataclass
class TurnResult:
    text: str
    session_id: str | None
    limited: bool = False
    resets_at: float | None = None
    error: str | None = None
    model: str | None = None
    fast_state: str | None = None
    fast_reason: str | None = None
    speed: str | None = None
    context: ContextSnapshot | None = None


class RateLimitTracker:
    """Collects rate-limit signals from one turn's message stream."""

    def __init__(self) -> None:
        self.resets_at: float | None = None
        self.rejected = False
        self.hard_limited = False

    def observe(self, message: object) -> None:
        if isinstance(message, RateLimitEvent):
            info = message.rate_limit_info
            if info.resets_at:
                self.resets_at = float(info.resets_at)
            self.rejected = self.rejected or info.status == "rejected"
        elif isinstance(message, AssistantMessage) and message.error == "rate_limit":
            self.hard_limited = True
        elif isinstance(message, ResultMessage) and message.is_error and message.api_error_status == 429:
            self.hard_limited = True

    def is_limited(self, result: ResultMessage | None) -> bool:
        # A "rejected" event can still be followed by a served turn (overage), so it alone is not enough.
        return self.hard_limited or (self.rejected and (result is None or result.is_error))


def log_tool_decision(user_key: str, mode: Mode, tool_name: str, tool_input: dict[str, Any],
                      decision: Decision) -> None:
    args = json.dumps(tool_input, ensure_ascii=False)[:500]
    tool_log.info("tool=%s tier=%s reason=%s mode=%s key=%s input=%s", tool_name, decision.tier, decision.reason,
                  mode, user_key, args)


def make_pre_tool_hook(targets: Targets, user_key: str,
                       mode: Mode = PRIVATE) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Runs for every tool call; 'ask' forces the call through can_use_tool, 'auto' leaves it to the auto-mode classifier."""

    async def hook(input_data: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
        name = str(input_data.get("tool_name", ""))
        tool_input = input_data.get("tool_input") or {}
        decision = decide(name, tool_input, targets, mode)
        log_tool_decision(user_key, mode, name, tool_input, decision)
        if decision.tier is Tier.AUTO:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision.tier.value,
                "permissionDecisionReason": decision.reason,
            }
        }

    return hook


def make_can_use_tool(targets: Targets, user_key: str, approver: Approver,
                      mode: Mode = PRIVATE) -> Callable[..., Awaitable[Any]]:
    async def can_use_tool(name: str, tool_input: dict[str, Any], context: ToolPermissionContext) -> Any:
        decision = decide(name, tool_input, targets, mode)
        if decision.tier is Tier.ALLOW:
            return PermissionResultAllow()
        if decision.tier is Tier.DENY:
            return PermissionResultDeny(message=f"Not permitted: {decision.reason}")
        request = ApprovalRequest(user_key, name, tool_input, decision.reason)
        try:
            approved = await approver.approve(request)
        except Exception:
            tool_log.exception("approval failed, denying: tool=%s key=%s", name, user_key)
            approved = False
        tool_log.info("approval tool=%s key=%s outcome=%s", name, user_key, "approved" if approved else "denied")
        if approved:
            return PermissionResultAllow()
        return PermissionResultDeny(message="The user declined this action. Do not retry it.")

    return can_use_tool


def disallowed_tools(servers: list[dict[str, Any]]) -> list[str]:
    """Hide every tool that can never run: less context, and denied by the CLI before any hook."""
    hidden: list[str] = []
    for server in servers:
        prefix = server_prefix(server["name"])
        if server["name"] not in KEPT_SERVERS:
            hidden.append(prefix.removesuffix("__"))
            continue
        hidden.extend(prefix + t["name"] for t in server.get("tools", []) if not is_listed(prefix + t["name"]))
    return sorted(hidden)


def public_disallowed_tools(servers: list[dict[str, Any]]) -> list[str]:
    """Public mode hides every connector, the kept ones included."""
    return sorted(server_prefix(server["name"]).removesuffix("__") for server in servers)


def build_system_prompt(config: Config, home_rules: str) -> str:
    t = config.targets
    return (
        f"Your name is {config.assistant_name}. You are a personal assistant talking to your owner over chat.\n\n"
        f"{home_rules.strip()}\n\n"
        "## Configuration\n"
        f"- Time zone: {config.tz.key}\n"
        f"- Assistant calendar id: {t.calendar_id}\n"
        f"- Notion Assistant page id: {t.notion_assistant_page_id}\n"
        f"- Notion Sessions page id: {t.notion_sessions_page_id}\n"
        f"- Notion Memory page id: {t.notion_memory_page_id or MEMORY_OFF}\n"
    )


def build_public_system_prompt(config: Config, public_rules: str) -> str:
    return (
        f"Your name is {config.assistant_name}. You are answering in a shared Discord channel that other people "
        "can read.\n\n"
        f"{public_rules.strip()}\n\n"
        "## Configuration\n"
        f"- Time zone: {config.tz.key}\n"
    )


@dataclass(frozen=True)
class Profile:
    """Per-mode tool surface: built-in tools, hidden tools, rules, and which connectors stay on."""

    mode: Mode
    tools: list[str]
    disallowed: list[str]
    system_prompt: str
    kept_servers: tuple[str, ...]


async def wait_for_servers(client: ClaudeSDKClient, timeout: float = SERVER_SETTLE_SECONDS) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    while True:
        servers: list[dict[str, Any]] = list((await client.get_mcp_status())["mcpServers"])
        kept = [s for s in servers if s["name"] in KEPT_SERVERS]
        if len(kept) == len(KEPT_SERVERS) and all(s["status"] != "pending" for s in kept):
            return servers
        if time.monotonic() > deadline:
            log.warning("connectors not ready after %ss: %s", timeout, [(s["name"], s["status"]) for s in kept])
            return servers
        await asyncio.sleep(1)


async def enable_kept_servers(client: ClaudeSDKClient) -> list[dict[str, Any]]:
    """Kept connectors on, the rest off; repairs a disabled state saved by an earlier client."""
    servers = await wait_for_servers(client)
    reenabled = []
    for server in servers:
        wanted = server["name"] in KEPT_SERVERS
        if wanted and server["status"] == "disabled":
            await client.toggle_mcp_server(server["name"], True)
            reenabled.append(server["name"])
        elif not wanted and server["status"] != "disabled":
            await client.toggle_mcp_server(server["name"], False)
    if reenabled:
        log.warning("re-enabled connectors saved as disabled: %s", reenabled)
        servers = await wait_for_servers(client)
    return servers


@dataclass
class _UserClient:
    """`remote` set = attached to Remote Control; `permission_mode` is the last one the CLI reported."""

    client: ClaudeSDKClient
    session_id: str | None
    applied: Effective
    remote: RemoteLink | None = None
    remote_tried: bool = False
    bridge_state: str | None = None
    permission_mode: str | None = None
    pump: MessagePump | None = None

    def observe(self, user_key: str, message: object) -> None:
        state = bridge_state_of(message)
        if state is not None:
            self.bridge_state = state
            log_bridge_state(user_key, cast(SystemMessage, message))
        self.permission_mode = permission_mode_of(message) or self.permission_mode


ClientChange = Literal["keep", "set_model", "reconnect"]


def client_change(applied: Effective, wanted: Effective) -> ClientChange:
    """Effort and fast mode are fixed when the CLI process starts; the model can switch live."""
    if (applied.effort, applied.fast) != (wanted.effort, wanted.fast):
        return "reconnect"
    return "set_model" if applied.model != wanted.model else "keep"


class Agent:
    def __init__(self, config: Config, approver: Approver) -> None:
        self._config = config
        self._approver = approver
        self._clients: dict[str, _UserClient] = {}
        self._disallowed: list[str] = []
        self._public_disallowed: list[str] = []
        home_rules = (config.agent_home / "CLAUDE.md").read_text(encoding="utf-8")
        public_rules = (config.agent_home / "PUBLIC.md").read_text(encoding="utf-8")
        self._system_prompt = build_system_prompt(config, home_rules)
        self._public_prompt = build_public_system_prompt(config, public_rules)

    def profile(self, user_key: str) -> Profile:
        if mode_for_key(user_key) == PUBLIC:
            return Profile(PUBLIC, list(PUBLIC_TOOLS), self._public_disallowed, self._public_prompt, ())
        return Profile(PRIVATE, list(BUILTIN_TOOLS), self._disallowed, self._system_prompt, KEPT_SERVERS)

    def effective(self, settings: ConversationSettings | None) -> Effective:
        return (settings or ConversationSettings()).effective(self._config.model)

    def _options(self, user_key: str, session_id: str | None,
                 settings: ConversationSettings | None = None) -> ClaudeAgentOptions:
        targets = self._config.targets
        profile = self.profile(user_key)
        hook = make_pre_tool_hook(targets, user_key, profile.mode)
        wanted = self.effective(settings)
        return ClaudeAgentOptions(
            tools=profile.tools,
            allowed_tools=[],
            disallowed_tools=profile.disallowed,
            system_prompt=profile.system_prompt,
            # Private: auto mode's classifier decides 'auto'-tier calls; public has no such calls.
            permission_mode="auto" if profile.mode == PRIVATE else "default",
            resume=session_id,
            model=wanted.model,
            effort=cast(Any, wanted.effort),
            settings=FAST_SETTINGS if wanted.fast else None,
            max_turns=MAX_TURNS,
            cwd=str(self._config.agent_home),
            env=dict(PUBLIC_ENV if profile.mode == PUBLIC else CLI_ENV),
            setting_sources=["project"],
            can_use_tool=make_can_use_tool(targets, user_key, self._approver, profile.mode),
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[hook], on_failure="block")]},  # type: ignore[list-item]
            stderr=lambda line: cli_log.info("%s", line.rstrip()),
        )

    async def discover(self) -> None:
        """Connect once without a query to learn the connector tool names to hide."""
        client = ClaudeSDKClient(self._options("discovery", None))
        await client.connect()
        try:
            servers = await enable_kept_servers(client)
        finally:
            await client.disconnect()
        self._disallowed = disallowed_tools(servers)
        self._public_disallowed = public_disallowed_tools(servers)
        kept_tools = sum(len(s.get("tools", [])) for s in servers if s["name"] in KEPT_SERVERS)
        log.info("discovered %d connector tools, hiding %d entries (public: %d servers)", kept_tools,
                 len(self._disallowed), len(self._public_disallowed))

    async def _connect(self, user_key: str, session_id: str | None,
                       settings: ConversationSettings | None = None) -> _UserClient:
        client = ClaudeSDKClient(self._options(user_key, session_id, settings))
        await client.connect()
        if mode_for_key(user_key) == PUBLIC:
            servers = list((await client.get_mcp_status())["mcpServers"])
            shown = [(s["name"], s["status"]) for s in servers]
        else:
            servers = await enable_kept_servers(client)
            shown = [(s["name"], s["status"]) for s in servers if s["name"] in KEPT_SERVERS]
        applied = self.effective(settings)
        log.info("client ready key=%s mode=%s resume=%s model=%s effort=%s fast=%s servers=%s", user_key,
                 mode_for_key(user_key), session_id, applied.model, applied.effort, applied.fast, shown)
        return _UserClient(client, session_id, applied)

    def live_keys(self) -> list[str]:
        return list(self._clients)

    async def _client_for(self, user_key: str, session_id: str | None,
                          settings: ConversationSettings | None = None) -> _UserClient:
        entry = self._clients.get(user_key)
        if entry is not None and entry.session_id == session_id:
            change = client_change(entry.applied, self.effective(settings))
            if change == "keep" or (change == "set_model" and await self._set_model(user_key, entry, settings)):
                return entry
            log.info("restarting client for new settings key=%s resume=%s", user_key, session_id)
        await self.close_session(user_key)
        entry = await self._connect(user_key, session_id, settings)
        self._clients[user_key] = entry
        return entry

    async def _set_model(self, user_key: str, entry: _UserClient, settings: ConversationSettings | None) -> bool:
        """False = the live switch failed and the caller reconnects instead."""
        wanted = self.effective(settings)
        try:
            await entry.client.set_model(wanted.model)
        except Exception:
            log.exception("set_model failed key=%s model=%s, reconnecting", user_key, wanted.model)
            return False
        log.info("model switched live key=%s %s -> %s", user_key, entry.applied.model, wanted.model)
        entry.applied = wanted
        return True

    async def apply_settings(self, user_key: str, session_id: str | None, settings: ConversationSettings,
                             remote: RemoteSpec | None = None) -> None:
        """A live client takes the new settings now; otherwise the next connect uses them."""
        if user_key in self._clients:
            entry = await self._client_for(user_key, session_id, settings)
            if remote is not None:
                await self._ensure_remote(user_key, entry, remote)

    async def context_usage(self, user_key: str) -> ContextSnapshot | None:
        entry = self._clients.get(user_key)
        return await _context_of(entry.client, user_key) if entry is not None else None

    async def run_turn(self, user_key: str, prompt: str, session_id: str | None,
                       settings: ConversationSettings | None = None, remote: RemoteSpec | None = None) -> TurnResult:
        """Retries once on a CLI failure; a session the CLI no longer has is replaced by a fresh one.

        `remote`: the conversation is on Remote Control, so a new client re-attaches and auto mode is forced back.
        """
        failures = 0
        while True:
            try:
                entry = await self._client_for(user_key, session_id, settings)
                if remote is not None:
                    await self._ensure_remote(user_key, entry, remote)
                    await self._force_auto(user_key, entry)
                async with asyncio.timeout(TURN_TIMEOUT_SECONDS):
                    result = await _exchange(entry, prompt)
            except (ClaudeSDKError, TimeoutError) as exc:
                await self.close_session(user_key)
                if session_id and is_missing_session(exc):
                    log.warning("session %s no longer exists, starting a fresh one key=%s", session_id, user_key)
                    session_id = None
                    continue
                failures += 1
                log.exception("turn failed key=%s failures=%d session=%s", user_key, failures, session_id)
                if failures >= 2:
                    raise
                continue
            entry.session_id = result.session_id
            if not result.limited:
                result.context = await _context_of(entry.client, user_key)
            return result

    async def close_session(self, user_key: str) -> None:
        """Ends the CLI process; a Remote Control bridge goes with it (its app entry stays for a re-attach)."""
        entry = self._clients.pop(user_key, None)
        if entry is None:
            return
        if entry.pump is not None:
            await entry.pump.stop()
        try:
            await entry.client.disconnect()
        except Exception:
            log.exception("disconnect failed key=%s", user_key)

    def remote_info(self, user_key: str) -> RemoteInfo | None:
        entry = self._clients.get(user_key)
        return RemoteInfo(entry.remote, entry.bridge_state) if entry is not None else None

    async def enable_remote(self, user_key: str, session_id: str | None, settings: ConversationSettings | None,
                            spec: RemoteSpec) -> RemoteInfo:
        """Connects if needed; already attached = the same link, no request. RemoteError on failure."""
        entry = await self._client_for(user_key, session_id, settings)
        if entry.remote is None:
            try:
                await self._attach(user_key, entry, spec, REMOTE_TIMEOUT_SECONDS)
            except RemoteError as exc:
                if spec.reattach_id is None:
                    raise
                log.warning("remote control re-attach failed key=%s bridge=%s (%s), starting a new one", user_key,
                            spec.reattach_id, exc)
                await self._attach(user_key, entry, replace(spec, reattach_id=None), REMOTE_TIMEOUT_SECONDS)
        return RemoteInfo(entry.remote, entry.bridge_state)

    async def disable_remote(self, user_key: str) -> None:
        """Without a live client there is no bridge; a failed request closes the client, which ends it."""
        entry = self._clients.get(user_key)
        if entry is None or entry.remote is None:
            log.info("remote control off key=%s: no live bridge (the app may still list it as disconnected)",
                     user_key)
            return
        try:
            await send_remote_control(entry.client, remote_request(False))
        except RemoteError:
            log.exception("remote control off failed key=%s bridge=%s, closing the client to end the bridge",
                          user_key, entry.remote.bridge_session_id)
            await self.close_session(user_key)
            return
        log.info("remote control off key=%s bridge=%s", user_key, entry.remote.bridge_session_id)
        entry.remote, entry.bridge_state = None, None

    async def _attach(self, user_key: str, entry: _UserClient, spec: RemoteSpec, timeout: float) -> None:
        entry.remote_tried = True
        if entry.permission_mode is None:
            entry.permission_mode = (await entry.client.get_server_info() or {}).get("current_permission_mode")
        response = await send_remote_control(entry.client, remote_request(True, spec), timeout)
        entry.remote = parse_link(response)
        if entry.pump is None:
            entry.pump = MessagePump(entry.client, user_key, lambda message: entry.observe(user_key, message))
        log.info("remote control on key=%s bridge=%s reattach=%s url=%s", user_key, entry.remote.bridge_session_id,
                 spec.reattach_id, entry.remote.session_url)

    async def _ensure_remote(self, user_key: str, entry: _UserClient, spec: RemoteSpec) -> None:
        """Re-attach once per client; a failure is logged and never blocks the turn."""
        if entry.remote is not None or entry.remote_tried:
            return
        try:
            await self._attach(user_key, entry, spec, REATTACH_TIMEOUT_SECONDS)
        except RemoteError as exc:
            entry.bridge_state = FAILED_STATE
            log.warning("remote control re-attach failed key=%s bridge=%s: %s", user_key, spec.reattach_id, exc)

    async def _force_auto(self, user_key: str, entry: _UserClient) -> None:
        """The app can switch modes (even to bypassPermissions); every reply here runs in auto mode."""
        if entry.permission_mode not in (None, "auto"):
            log.warning("remote conversation key=%s was in permission mode %s (changed from the app?); forcing auto",
                        user_key, entry.permission_mode)
        await entry.client.set_permission_mode("auto")
        entry.permission_mode = "auto"

    async def close_all(self) -> None:
        for user_key in list(self._clients):
            await self.close_session(user_key)


def is_missing_session(exc: BaseException) -> bool:
    return "No conversation found" in str(exc)


async def _context_of(client: ClaudeSDKClient, user_key: str) -> ContextSnapshot | None:
    """Context numbers are a nice-to-have: a failure is logged and the turn still counts."""
    try:
        return ContextSnapshot.from_usage(await client.get_context_usage())
    except Exception:
        log.warning("context usage unavailable key=%s", user_key, exc_info=True)
        return None


@dataclass(frozen=True)
class InitInfo:
    """From the turn's system/init message: the model and fast mode the CLI actually runs."""

    model: str | None = None
    fast_state: str | None = None
    fast_reason: str | None = None


def init_info(message: object) -> InitInfo | None:
    if not isinstance(message, SystemMessage) or message.subtype != "init":
        return None
    data = message.data
    return InitInfo(data.get("model"), data.get("fast_mode_state"), data.get("fast_mode_disabled_reason"))


async def _one_message(prompt: str, message_id: str) -> AsyncIterator[dict[str, Any]]:
    yield {"type": "user", "message": {"role": "user", "content": prompt}, "parent_tool_use_id": None,
           "uuid": message_id}


async def _exchange(entry: _UserClient, prompt: str) -> TurnResult:
    """The prompt carries a uuid so its result can be told apart from a turn typed in the Remote Control app."""
    message_id = str(uuid.uuid4())
    pump = entry.pump
    if pump is not None:
        pump.begin_turn()
    try:
        await entry.client.query(_one_message(prompt, message_id))
        source = pump.turn_messages() if pump is not None else entry.client.receive_messages()
        async with aclosing(source) as messages:
            return await _collect(messages, message_id)
    finally:
        if pump is not None:
            pump.end_turn()


async def _collect(messages: AsyncIterator[object], message_id: str) -> TurnResult:
    tracker, init = RateLimitTracker(), InitInfo()
    texts: list[str] = []
    async for message in messages:
        if isinstance(message, ResultMessage) and not answers(message, message_id):
            log.info("skipped another turn's result (Remote Control app) session=%s reply=%r", message.session_id,
                     (message.result or "")[:200])
            tracker, init = RateLimitTracker(), InitInfo()
            texts = []
            continue
        tracker.observe(message)
        init = init_info(message) or init
        if isinstance(message, AssistantMessage) and message.parent_tool_use_id is None:
            texts.extend(block.text for block in message.content if isinstance(block, TextBlock))
        elif isinstance(message, ResultMessage):
            return to_turn_result(message, texts, tracker, init)
    return to_turn_result(None, texts, tracker, init)


def to_turn_result(result: ResultMessage | None, texts: list[str], tracker: RateLimitTracker,
                   init: InitInfo | None = None) -> TurnResult:
    session_id = result.session_id if result else None
    if tracker.is_limited(result):
        log.warning("rate limited, resets_at=%s", tracker.resets_at)
        return TurnResult("", session_id, limited=True, resets_at=tracker.resets_at)
    if result is None:
        return TurnResult("", None, error="no result from the agent")
    init = init or InitInfo()
    speed = (result.usage or {}).get("speed")
    log.info("turn done session=%s model=%s fast=%s/%s speed=%s turns=%s cost_usd=%s denials=%s", result.session_id,
             init.model, init.fast_state, init.fast_reason, speed, result.num_turns, result.total_cost_usd,
             len(result.permission_denials or []))
    text = (result.result or "\n\n".join(texts)).strip()
    info: dict[str, Any] = {"model": init.model, "fast_state": init.fast_state, "fast_reason": init.fast_reason,
                            "speed": speed}
    if result.is_error:
        detail = f"{result.subtype} status={result.api_error_status} errors={result.errors}"
        log.error("turn error session=%s %s", result.session_id, detail)
        return TurnResult(text, session_id, error=detail, **info)
    return TurnResult(text or "(no reply)", session_id, **info)
