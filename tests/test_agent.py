from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from claude_agent_sdk import (
    AssistantMessage, PermissionResultAllow, PermissionResultDeny, RateLimitEvent, RateLimitInfo, ResultMessage,
    SystemMessage, TextBlock, ToolPermissionContext,
)

from assistant.agent import (
    ApprovalRequest, RateLimitTracker, build_public_system_prompt, build_system_prompt, client_change, disallowed_tools,
    init_info, make_can_use_tool, make_pre_tool_hook, public_disallowed_tools, to_turn_result,
)
from assistant.config import Config
from assistant.permissions import CALENDAR, KEPT_SERVERS, NOTION, Targets
from assistant.settings import ConversationSettings, Effective

TARGETS = Targets("cal@group.calendar.google.com", "a" * 32, "b" * 32, "c" * 32)
REPO_HOME = Path(__file__).resolve().parents[1] / "agent_home"


def result(is_error: bool = False, status: int | None = None, text: str | None = "done") -> ResultMessage:
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=is_error, num_turns=1,
                         session_id="s1", result=text, api_error_status=status)


def limit_event(status: str, resets_at: int | None) -> RateLimitEvent:
    return RateLimitEvent(RateLimitInfo(status=status, resets_at=resets_at), uuid="u", session_id="s1")  # type: ignore[arg-type]


def test_rejected_event_with_error_result_is_limited_with_reset_time() -> None:
    tracker = RateLimitTracker()
    tracker.observe(limit_event("rejected", 1_900_000_000))
    final = result(is_error=True, text=None)
    tracker.observe(final)
    turn = to_turn_result(final, [], tracker)
    assert turn.limited and turn.resets_at == 1_900_000_000.0


def test_http_429_result_is_limited_without_reset_time() -> None:
    tracker = RateLimitTracker()
    final = result(is_error=True, status=429, text=None)
    tracker.observe(final)
    turn = to_turn_result(final, [], tracker)
    assert turn.limited and turn.resets_at is None


def test_assistant_rate_limit_error_is_limited() -> None:
    tracker = RateLimitTracker()
    tracker.observe(AssistantMessage(content=[], model="m", error="rate_limit"))
    assert tracker.is_limited(result())


def test_warning_or_served_overage_is_not_limited() -> None:
    tracker = RateLimitTracker()
    tracker.observe(limit_event("allowed_warning", 1_900_000_000))
    assert not tracker.is_limited(result())
    tracker.observe(limit_event("rejected", 1_900_000_000))
    assert not tracker.is_limited(result())  # rejected but the turn was still served


def test_turn_result_text_and_errors() -> None:
    ok = to_turn_result(result(text="⏰ Added"), ["thinking aloud"], RateLimitTracker())
    assert ok.text == "⏰ Added" and ok.error is None and ok.session_id == "s1"
    fallback = to_turn_result(result(text=None), ["a", "b"], RateLimitTracker())
    assert fallback.text == "a\n\nb"
    failed = to_turn_result(result(is_error=True, status=500), [], RateLimitTracker())
    assert failed.error and "500" in failed.error and not failed.limited


def test_disallowed_hides_unlisted_tools_and_other_servers() -> None:
    servers = [
        {"name": "claude.ai Google Calendar", "status": "connected",
         "tools": [{"name": "create_event"}, {"name": "list_events"}]},
        {"name": "claude.ai Notion", "status": "connected",
         "tools": [{"name": "notion-fetch"}, {"name": "notion-create-comment"}]},
        {"name": "claude.ai Gmail", "status": "connected", "tools": [{"name": "send_message"}]},
    ]
    assert disallowed_tools(servers) == ["mcp__claude_ai_Gmail", NOTION + "notion-create-comment"]


async def test_hook_returns_tier_decision() -> None:
    hook = make_pre_tool_hook(TARGETS, "u")
    out = await hook({"tool_name": CALENDAR + "delete_event", "tool_input": {"eventId": "e"}}, "t1", {})
    assert out["hookSpecificOutput"]["permissionDecision"] == "ask"
    out = await hook({"tool_name": "Bash", "tool_input": {"command": "dir"}}, "t2", {})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


async def test_hook_leaves_auto_tier_to_the_auto_mode_classifier() -> None:
    hook = make_pre_tool_hook(TARGETS, "u")
    out = await hook({"tool_name": CALENDAR + "create_event", "tool_input": {"summary": "x"}}, "t1", {})
    assert out == {}  # no decision: the CLI's auto-mode classifier decides


def test_private_clients_run_in_auto_mode_public_do_not(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)
    assert agent._options("dm:1", None).permission_mode == "auto"
    assert agent._options("pthread:9", None).permission_mode == "default"


class FakeApprover:
    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.requests: list[ApprovalRequest] = []

    async def approve(self, request: ApprovalRequest) -> bool:
        self.requests.append(request)
        return self.answer


async def test_can_use_tool_routes_ask_through_approver() -> None:
    ctx = ToolPermissionContext(tool_use_id="t")
    yes, no = FakeApprover(True), FakeApprover(False)
    delete = CALENDAR + "delete_event"
    assert isinstance(await make_can_use_tool(TARGETS, "u", yes)(delete, {"eventId": "e"}, ctx), PermissionResultAllow)
    assert isinstance(await make_can_use_tool(TARGETS, "u", no)(delete, {"eventId": "e"}, ctx), PermissionResultDeny)
    assert yes.requests[0].tool_name == delete
    assert isinstance(await make_can_use_tool(TARGETS, "u", yes)("Bash", {}, ctx), PermissionResultDeny)
    assert len(yes.requests) == 1  # deny tier never asks


def test_system_prompt_injects_name_and_targets(tmp_path: Path) -> None:
    config = Config(None, False, frozenset(), "Nethor", ZoneInfo("Europe/Oslo"), "m", TARGETS, root=tmp_path)
    prompt = build_system_prompt(config, "# Rules\nBe brief.")
    assert prompt.startswith("Your name is Nethor.")
    assert "Be brief." in prompt and TARGETS.calendar_id in prompt and "Europe/Oslo" in prompt


async def test_run_turn_replaces_a_session_the_cli_no_longer_has(tmp_path: Path, monkeypatch) -> None:
    from claude_agent_sdk import ClaudeSDKError

    import assistant.agent as agent_mod

    (tmp_path / "agent_home").mkdir()
    (tmp_path / "agent_home" / "CLAUDE.md").write_text("rules", encoding="utf-8")
    (tmp_path / "agent_home" / "PUBLIC.md").write_text("public rules", encoding="utf-8")
    config = Config(None, False, frozenset(), "N", ZoneInfo("Europe/Oslo"), "m", TARGETS, root=tmp_path)
    agent = agent_mod.Agent(config, FakeApprover(False))
    resumed: list[str | None] = []

    async def fake_connect(user_key: str, session_id: str | None, settings: object = None) -> object:
        resumed.append(session_id)
        if session_id:
            raise ClaudeSDKError(f"No conversation found with session ID: {session_id}")
        return agent_mod._UserClient(client=None, session_id=None, applied=agent.effective(None))  # type: ignore[arg-type]

    async def fake_exchange(client: object, prompt: str) -> agent_mod.TurnResult:
        return agent_mod.TurnResult("pong", "fresh")

    monkeypatch.setattr(agent, "_connect", fake_connect)
    monkeypatch.setattr(agent_mod, "_exchange", fake_exchange)
    result = await agent.run_turn("u", "ping", "gone-session")
    assert result.session_id == "fresh" and resumed == ["gone-session", None]


SERVERS = [
    {"name": "claude.ai Google Calendar", "status": "connected", "tools": [{"name": "list_events"}]},
    {"name": "claude.ai Notion", "status": "connected", "tools": [{"name": "notion-fetch"}]},
    {"name": "claude.ai Todoist", "status": "connected", "tools": [{"name": "find-tasks"}]},
    {"name": "claude.ai Gmail", "status": "connected", "tools": [{"name": "send_message"}]},
]


def make_agent(tmp_path: Path):  # type: ignore[no-untyped-def]
    import assistant.agent as agent_mod

    home = tmp_path / "agent_home"
    home.mkdir(exist_ok=True)
    (home / "CLAUDE.md").write_text("PRIVATE RULES", encoding="utf-8")
    (home / "PUBLIC.md").write_text("PUBLIC RULES", encoding="utf-8")
    config = Config(None, False, frozenset(), "Nethor", ZoneInfo("Europe/Oslo"), "m", TARGETS, root=tmp_path)
    agent = agent_mod.Agent(config, FakeApprover(True))
    agent._disallowed = disallowed_tools(SERVERS)
    agent._public_disallowed = public_disallowed_tools(SERVERS)
    return agent


def test_public_disallows_every_connector_server() -> None:
    assert public_disallowed_tools(SERVERS) == ["mcp__claude_ai_Gmail", "mcp__claude_ai_Google_Calendar",
                                                "mcp__claude_ai_Notion", "mcp__claude_ai_Todoist"]


def test_profiles_by_key(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)
    for key in ("dm:1", "thread:7", "priv:9", "console"):
        profile = agent.profile(key)
        assert profile.mode == "private" and "PRIVATE RULES" in profile.system_prompt
        assert profile.kept_servers == KEPT_SERVERS and profile.disallowed == ["mcp__claude_ai_Gmail"]
    public = agent.profile("pub:8")
    assert public.mode == "public" and public.tools == ["WebSearch", "WebFetch"] and public.kept_servers == ()
    assert "PUBLIC RULES" in public.system_prompt and "PRIVATE RULES" not in public.system_prompt
    assert public.disallowed == public_disallowed_tools(SERVERS)


def test_public_options_carry_the_public_surface(tmp_path: Path) -> None:
    options = make_agent(tmp_path)._options("pub:8", None)
    assert options.tools == ["WebSearch", "WebFetch"]
    assert "mcp__claude_ai_Notion" in options.disallowed_tools
    assert "PUBLIC RULES" in str(options.system_prompt)


def test_public_prompt_has_no_private_ids(tmp_path: Path) -> None:
    config = Config(None, False, frozenset(), "Nethor", ZoneInfo("Europe/Oslo"), "m", TARGETS, root=tmp_path)
    prompt = build_public_system_prompt(config, "rules")
    assert prompt.startswith("Your name is Nethor.") and "other people" in prompt
    for secretish in (TARGETS.calendar_id, TARGETS.notion_assistant_page_id, TARGETS.notion_sessions_page_id,
                      TARGETS.notion_memory_page_id):
        assert secretish not in prompt


class FakeSDKClient:
    toggled: list[tuple[str, bool]] = []
    start_status = "connected"
    last_options: object = None

    def __init__(self, options: object) -> None:
        self.options = options
        FakeSDKClient.last_options = options
        self.servers = [{**s, "status": FakeSDKClient.start_status} for s in SERVERS]

    async def connect(self) -> None:
        pass

    async def get_mcp_status(self) -> dict[str, object]:
        return {"mcpServers": self.servers}

    async def toggle_mcp_server(self, name: str, enabled: bool) -> None:
        FakeSDKClient.toggled.append((name, enabled))
        for server in self.servers:
            if server["name"] == name:
                server["status"] = "connected" if enabled else "disabled"

    async def disconnect(self) -> None:
        pass


def use_fake_client(monkeypatch, start_status: str = "connected") -> None:  # type: ignore[no-untyped-def]
    import assistant.agent as agent_mod

    monkeypatch.setattr(agent_mod, "ClaudeSDKClient", FakeSDKClient)
    FakeSDKClient.toggled = []
    FakeSDKClient.start_status = start_status


async def test_private_connect_only_turns_unused_connectors_off(tmp_path: Path, monkeypatch) -> None:
    use_fake_client(monkeypatch)
    await make_agent(tmp_path)._connect("dm:1", None)
    assert FakeSDKClient.toggled == [("claude.ai Gmail", False)]


async def test_private_connect_re_enables_connectors_saved_as_disabled(tmp_path: Path, monkeypatch) -> None:
    use_fake_client(monkeypatch, start_status="disabled")
    await make_agent(tmp_path)._connect("thread:7", None)
    assert sorted(FakeSDKClient.toggled) == sorted((name, True) for name in KEPT_SERVERS)


async def test_public_connect_never_toggles_and_drops_connectors_by_env(tmp_path: Path, monkeypatch) -> None:
    use_fake_client(monkeypatch)
    await make_agent(tmp_path)._connect("pub:8", None)
    assert FakeSDKClient.toggled == []
    assert FakeSDKClient.last_options.env["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"  # type: ignore[attr-defined]


async def test_private_options_keep_claude_ai_connectors(tmp_path: Path) -> None:
    options = make_agent(tmp_path)._options("dm:1", None)
    assert "ENABLE_CLAUDEAI_MCP_SERVERS" not in options.env


async def test_public_hook_denies_private_tools_and_logs_mode(caplog) -> None:
    caplog.set_level("INFO", logger="assistant.tools")
    hook = make_pre_tool_hook(TARGETS, "pub:8", "public")
    out = await hook({"tool_name": CALENDAR + "list_events", "tool_input": {}}, "t1", {})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    out = await hook({"tool_name": "WebSearch", "tool_input": {"query": "q"}}, "t2", {})
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert "mode=public key=pub:8" in caplog.text


async def test_public_can_use_tool_never_asks() -> None:
    approver = FakeApprover(True)
    can_use = make_can_use_tool(TARGETS, "pub:8", approver, "public")
    ctx = ToolPermissionContext(tool_use_id="t")
    assert isinstance(await can_use(CALENDAR + "delete_event", {"eventId": "e"}, ctx), PermissionResultDeny)
    assert approver.requests == []


async def test_discover_re_enables_disabled_connectors_before_listing_tools(tmp_path: Path, monkeypatch) -> None:
    use_fake_client(monkeypatch, start_status="disabled")
    await make_agent(tmp_path).discover()
    assert sorted(FakeSDKClient.toggled) == sorted((name, True) for name in KEPT_SERVERS)


async def test_live_keys_track_open_clients(tmp_path: Path, monkeypatch) -> None:
    use_fake_client(monkeypatch)
    agent = make_agent(tmp_path)
    agent._clients["thread:7"] = await agent._connect("thread:7", None)
    assert agent.live_keys() == ["thread:7"]


def test_system_prompt_names_the_memory_page_and_the_session_start_rule(tmp_path: Path) -> None:
    config = Config(None, False, frozenset(), "Nethor", ZoneInfo("Europe/Oslo"), "m", TARGETS, root=tmp_path)
    prompt = build_system_prompt(config, (REPO_HOME / "CLAUDE.md").read_text(encoding="utf-8"))
    assert f"- Notion Memory page id: {TARGETS.notion_memory_page_id}" in prompt
    start = prompt[prompt.index("## Session start"):]
    start = start[:start.index("\n## ", 1)]
    assert "Memory page" in start and "notion-fetch" in start
    assert "Sessions page" in start and "`memory`" in start  # both named only to say: don't fetch them


def test_private_rules_document_the_update_content_recipe() -> None:
    rules = (REPO_HOME / "CLAUDE.md").read_text(encoding="utf-8")
    for needle in ("notion-update-page", "update_content", "content_updates", "old_str", "new_str"):
        assert needle in rules
    assert "notion-memory-search" not in rules and 'parent` = `"memory"`' not in rules


def test_system_prompt_says_memory_is_off_without_a_page(tmp_path: Path) -> None:
    targets = Targets("cal", "a" * 32, "b" * 32)
    config = Config(None, False, frozenset(), "Nethor", ZoneInfo("Europe/Oslo"), "m", targets, root=tmp_path)
    assert "- Notion Memory page id: none (memory is off" in build_system_prompt(config, "rules")


def test_public_rules_have_no_memory_page_or_ids() -> None:
    rules = (REPO_HOME / "PUBLIC.md").read_text(encoding="utf-8")
    assert "Memory page" not in rules and "Sessions" not in rules


# --- v5: per-conversation settings -----------------------------------------------------------------------------------

OPUS_FAST = ConversationSettings("claude-opus-5-5", None, True)
USAGE = {"totalTokens": 46_000, "maxTokens": 1_000_000, "percentage": 4.6,
         "categories": [{"name": "Messages", "tokens": 30_000, "color": "x"}]}


class TurnSDKClient(FakeSDKClient):
    """Fake client that also answers turns, switches models and reports context."""

    instances: list[TurnSDKClient] = []
    init_data: dict[str, object] = {}
    usage: dict[str, object] = {}

    def __init__(self, options: object) -> None:
        super().__init__(options)
        self.models: list[str | None] = []
        self.disconnected = False
        TurnSDKClient.instances.append(self)

    async def set_model(self, model: str | None = None) -> None:
        self.models.append(model)

    async def get_context_usage(self) -> dict[str, object]:
        return USAGE

    async def query(self, prompt: str) -> None:
        self.prompt = prompt

    async def receive_messages(self):  # type: ignore[no-untyped-def]
        yield SystemMessage("init", dict(TurnSDKClient.init_data))
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                            session_id="s1", result="hi", usage=dict(TurnSDKClient.usage))

    async def disconnect(self) -> None:
        self.disconnected = True


def use_turn_client(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import assistant.agent as agent_mod

    monkeypatch.setattr(agent_mod, "ClaudeSDKClient", TurnSDKClient)
    TurnSDKClient.instances = []
    TurnSDKClient.init_data = {"model": "m", "fast_mode_state": "on"}
    TurnSDKClient.usage = {"speed": "standard"}


def test_client_change_rules() -> None:
    base = Effective("claude-sonnet-5-5", "medium", False)
    assert client_change(base, base) == "keep"
    assert client_change(base, Effective("claude-opus-5-5", "medium", False)) == "set_model"
    assert client_change(base, Effective("claude-sonnet-5-5", "high", False)) == "reconnect"
    assert client_change(base, Effective("claude-opus-5-5", "medium", True)) == "reconnect"


def test_options_carry_model_effort_and_fast_only_when_fast(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)
    plain = agent._options("dm:1", None)
    assert (plain.model, plain.effort, plain.settings) == ("m", "medium", None)
    chosen = agent._options("dm:1", "s1", ConversationSettings("claude-haiku-5-5", "max"))
    assert (chosen.model, chosen.effort, chosen.settings, chosen.resume) == ("claude-haiku-5-5", "max", None, "s1")
    fast = agent._options("thread:7", None, OPUS_FAST)
    assert (fast.model, fast.settings) == ("claude-opus-5-5", '{"fastMode": true}')


async def test_model_change_switches_the_live_client(tmp_path: Path, monkeypatch) -> None:
    use_turn_client(monkeypatch)
    agent = make_agent(tmp_path)
    await agent.run_turn("dm:1", "hi", None)
    await agent.apply_settings("dm:1", "s1", ConversationSettings("claude-opus-5-5"))
    assert len(TurnSDKClient.instances) == 1 and TurnSDKClient.instances[0].models == ["claude-opus-5-5"]
    await agent.run_turn("dm:1", "again", "s1", ConversationSettings("claude-opus-5-5"))
    assert len(TurnSDKClient.instances) == 1 and TurnSDKClient.instances[0].models == ["claude-opus-5-5"]


@pytest.mark.parametrize("settings", [ConversationSettings(None, "high"), OPUS_FAST])
async def test_effort_or_fast_change_reconnects_with_resume(tmp_path: Path, monkeypatch,
                                                            settings: ConversationSettings) -> None:
    use_turn_client(monkeypatch)
    agent = make_agent(tmp_path)
    await agent.run_turn("dm:1", "hi", None)
    await agent.apply_settings("dm:1", "s1", settings)
    first, second = TurnSDKClient.instances
    assert first.disconnected and second.options.resume == "s1"  # type: ignore[attr-defined]
    assert second.options.effort == agent.effective(settings).effort  # type: ignore[attr-defined]
    await agent.run_turn("dm:1", "again", "s1", settings)
    assert len(TurnSDKClient.instances) == 2


async def test_settings_without_a_live_client_wait_for_the_next_connect(tmp_path: Path, monkeypatch) -> None:
    use_turn_client(monkeypatch)
    agent = make_agent(tmp_path)
    await agent.apply_settings("dm:1", "s1", OPUS_FAST)
    assert TurnSDKClient.instances == []
    await agent.run_turn("dm:1", "hi", "s1", OPUS_FAST)
    assert TurnSDKClient.instances[0].options.settings == '{"fastMode": true}'  # type: ignore[attr-defined]


async def test_turn_captures_fast_state_speed_model_and_context(tmp_path: Path, monkeypatch, caplog) -> None:
    use_turn_client(monkeypatch)
    TurnSDKClient.init_data = {"model": "claude-opus-5-5", "fast_mode_state": "off",
                               "fast_mode_disabled_reason": "sdk_opt_in_required"}
    caplog.set_level("INFO", logger="assistant.agent")
    turn = await make_agent(tmp_path).run_turn("dm:1", "hi", None)
    assert (turn.model, turn.fast_state, turn.fast_reason, turn.speed) == (
        "claude-opus-5-5", "off", "sdk_opt_in_required", "standard")
    assert turn.context is not None and (turn.context.total, turn.context.categories) == (46_000,
                                                                                          (("Messages", 30_000),))
    assert "model=claude-opus-5-5" in caplog.text and "speed=standard" in caplog.text


async def test_live_context_usage(tmp_path: Path, monkeypatch) -> None:
    use_turn_client(monkeypatch)
    agent = make_agent(tmp_path)
    assert await agent.context_usage("dm:1") is None
    await agent.run_turn("dm:1", "hi", None)
    usage = await agent.context_usage("dm:1")
    assert usage is not None and usage.percentage == 4.6


async def test_context_failure_is_logged_and_the_turn_still_counts(tmp_path: Path, monkeypatch, caplog) -> None:
    use_turn_client(monkeypatch)

    async def broken(self: object) -> dict[str, object]:
        raise RuntimeError("no usage")

    monkeypatch.setattr(TurnSDKClient, "get_context_usage", broken)
    turn = await make_agent(tmp_path).run_turn("dm:1", "hi", None)
    assert turn.text == "hi" and turn.context is None and "context usage unavailable key=dm:1" in caplog.text


def test_init_info_reads_only_init_messages() -> None:
    info = init_info(SystemMessage("init", {"fast_mode_state": "on"}))
    assert info is not None and info.fast_state == "on"
    assert init_info(SystemMessage("status", {})) is None and init_info(result()) is None


async def test_failed_live_model_switch_reconnects(tmp_path: Path, monkeypatch, caplog) -> None:
    use_turn_client(monkeypatch)
    agent = make_agent(tmp_path)
    await agent.run_turn("dm:1", "hi", None)

    async def refuse(self: object, model: str | None = None) -> None:
        raise RuntimeError("no switch")

    monkeypatch.setattr(TurnSDKClient, "set_model", refuse)
    await agent.apply_settings("dm:1", "s1", ConversationSettings("claude-opus-5-5"))
    second = TurnSDKClient.instances[1]
    assert len(TurnSDKClient.instances) == 2 and second.options.model == "claude-opus-5-5"  # type: ignore[attr-defined]
    assert "set_model failed key=dm:1" in caplog.text
