from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock
from claude_agent_sdk._internal.query import Query

import assistant.agent as agent_mod
from assistant.commands import NOT_HERE
from assistant.permissions import KEPT_SERVERS
from assistant.remote import (
    NOT_CONNECTED, PRIVATE_ONLY, REMOTE_ALREADY_OFF, REMOTE_OFF, UNSUPPORTED, RemoteError, RemoteInfo, RemoteLink,
    RemoteSpec, answers, bridge_state_of, control_sender, parse_link, permission_mode_of, remote_request,
    send_remote_control,
)
from assistant.sessions import IDLE_SECONDS, Origin, SessionRecord, idle_clients
from assistant.settings import ConversationSettings, describe_remote
from test_agent import USAGE, make_agent
from test_commands import interaction, live, reply_of  # noqa: F401  (fixture)
from test_routing import OWNER, dm_channel, text_channel, thread
from test_sessions import ts

KEY = "thread:7"
DM_KEY = f"dm:{OWNER}"
POST_KEY = "thread:700"


# --- pure helpers ----------------------------------------------------------------------------------------------------

def test_request_with_and_without_reattach_id() -> None:
    assert remote_request(True, RemoteSpec("Nethor · DM")) == {
        "subtype": "remote_control", "enabled": True, "name": "Nethor · DM", "keep_session_on_exit": True}
    assert remote_request(True, RemoteSpec("Nethor · Trip", "cse_1")) == {
        "subtype": "remote_control", "enabled": True, "name": "Nethor · Trip", "reattach_session_id": "cse_1",
        "keep_session_on_exit": True}
    assert remote_request(True, RemoteSpec(reattach_id="cse_1")) == {
        "subtype": "remote_control", "enabled": True, "reattach_session_id": "cse_1", "keep_session_on_exit": True}
    assert remote_request(False) == {"subtype": "remote_control", "enabled": False}


@pytest.mark.parametrize(("client", "error"), [
    (NS(), UNSUPPORTED), (NS(_query=None), NOT_CONNECTED), (NS(_query=NS()), UNSUPPORTED),
    (NS(_query=NS(_send_control_request=lambda request: None)), UNSUPPORTED),
])
def test_missing_private_api_is_a_clear_error(client: Any, error: str) -> None:
    with pytest.raises(RemoteError, match=error):
        control_sender(client)


def test_installed_sdk_still_has_the_private_api() -> None:
    assert control_sender(NS(_query=Query.__new__(Query)))


async def test_cli_errors_become_remote_errors() -> None:
    async def send(request: dict[str, Any], timeout: float = 60.0) -> dict[str, Any]:
        raise Exception("Remote Control initialization failed")

    with pytest.raises(RemoteError, match="Remote Control initialization failed"):
        await send_remote_control(NS(_query=NS(_send_control_request=send)), remote_request(False))
    with pytest.raises(RemoteError, match="no session_url"):
        parse_link({"bridge_session_id": "cse_1"})


def result_for(message_id: str | None, text: str) -> ResultMessage:
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                         session_id="s1", result=text, user_message_uuid=message_id,
                         user_message_uuids=[message_id] if message_id else None)


def test_result_belongs_to_our_prompt_by_uuid() -> None:
    assert answers(result_for("ours", "x"), "ours") and not answers(result_for("app", "x"), "ours")
    merged = ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                           session_id="s1", user_message_uuid="ours", user_message_uuids=["app", "ours"])
    assert answers(merged, "ours") and answers(result_for(None, "old CLI"), "ours")


def test_bridge_state_and_permission_mode_messages() -> None:
    assert bridge_state_of(SystemMessage("bridge_state", {"state": "connected", "bridge_epoch": 1})) == "connected"
    assert bridge_state_of(SystemMessage("status", {"permissionMode": "auto"})) is None
    assert permission_mode_of(SystemMessage("status", {"status": None, "permissionMode": "default"})) == "default"
    assert permission_mode_of(SystemMessage("init", {"permissionMode": "auto"})) == "auto"
    assert permission_mode_of(SystemMessage("bridge_state", {"state": "ready"})) is None


def test_status_words() -> None:
    assert describe_remote(False, True, "connected") == "remote off"
    assert describe_remote(True, True, "connected") == "remote on (connected)"
    assert describe_remote(True, False, "connected") == "remote on (paused)"
    assert describe_remote(True, True, None) == "remote on"


def test_remote_conversations_keep_their_client_when_idle() -> None:
    records = {"a": SessionRecord("s", last_active=0.0), "b": SessionRecord("s", last_active=0.0, remote=True)}
    assert idle_clients(["a", "b"], records.__getitem__, 10_000.0) == ["a"]


# --- agent: a fake CLI with one message stream -------------------------------------------------------------------

class FakeQuery:
    def __init__(self, client: StreamClient) -> None:
        self.client = client

    async def _send_control_request(self, request: dict[str, Any], timeout: float = 60.0) -> dict[str, Any]:
        self.client.requests.append(request)
        self.client.events.append(("control", request["enabled"]))
        failure = StreamClient.failure
        if failure and (failure != "reattach" or "reattach_session_id" in request):
            raise Exception("Remote Control initialization failed")
        if not request["enabled"]:
            return {}
        bridge = request.get("reattach_session_id") or "cse_new"
        return {"session_url": f"https://claude.ai/code/{bridge}", "connect_url": "c", "environment_id": "",
                "bridge_epoch": 1, "bridge_session_id": bridge}


class StreamClient:
    instances: list[StreamClient] = []
    failure: str | None = None

    def __init__(self, options: Any) -> None:
        self.options = options
        self.stream: asyncio.Queue[Any] = asyncio.Queue()
        self.requests: list[dict[str, Any]] = []
        self.events: list[tuple[str, Any]] = []
        self.before_reply: list[Any] = []
        self.disconnected = False
        self._query = FakeQuery(self)
        StreamClient.instances.append(self)

    async def connect(self) -> None:
        pass

    async def get_mcp_status(self) -> dict[str, Any]:
        return {"mcpServers": [{"name": name, "status": "connected", "tools": []} for name in KEPT_SERVERS]}

    async def get_server_info(self) -> dict[str, Any]:
        return {"current_permission_mode": "auto"}

    async def set_permission_mode(self, mode: str) -> None:
        self.events.append(("mode", mode))

    async def get_context_usage(self) -> dict[str, Any]:
        return USAGE

    async def query(self, prompt: Any) -> None:
        message = [m async for m in prompt][0]
        text = message["message"]["content"]
        self.events.append(("query", text))
        for item in self.before_reply:
            self.stream.put_nowait(item)
        self.before_reply = []
        self.stream.put_nowait(SystemMessage("init", {"permissionMode": "auto"}))
        self.stream.put_nowait(result_for(message["uuid"], f"re: {text}"))

    async def receive_messages(self) -> Any:
        while (item := await self.stream.get()) is not None:
            yield item

    async def disconnect(self) -> None:
        self.disconnected = True
        self.stream.put_nowait(None)


def app_turn(text: str) -> list[Any]:
    return [SystemMessage("init", {"permissionMode": "auto"}),
            AssistantMessage(content=[TextBlock(text)], model="m"), result_for("app-uuid", text)]


async def settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


@pytest.fixture
def agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(agent_mod, "ClaudeSDKClient", StreamClient)
    StreamClient.instances, StreamClient.failure = [], None
    return make_agent(tmp_path)


async def test_reconnect_re_attaches_with_the_saved_id_and_forces_auto_first(agent: Any) -> None:
    turn = await agent.run_turn(KEY, "hi", "s1", None, RemoteSpec(reattach_id="cse_saved"))
    client = StreamClient.instances[0]
    assert client.requests == [{"subtype": "remote_control", "enabled": True, "reattach_session_id": "cse_saved",
                                "keep_session_on_exit": True}]
    assert client.events == [("control", True), ("mode", "auto"), ("query", "hi")] and turn.text == "re: hi"
    assert agent.remote_info(KEY) == RemoteInfo(RemoteLink("https://claude.ai/code/cse_saved", "cse_saved"), None)
    await agent.run_turn(KEY, "again", "s1", None, RemoteSpec(reattach_id="cse_saved"))
    assert len(client.requests) == 1 and client.events[-2:] == [("mode", "auto"), ("query", "again")]
    await agent.close_all()


async def test_non_remote_turn_never_touches_remote_control_or_the_mode(agent: Any) -> None:
    turn = await agent.run_turn(KEY, "hi", None)
    assert StreamClient.instances[0].events == [("query", "hi")] and turn.text == "re: hi"
    assert agent.remote_info(KEY) == RemoteInfo(None, None)


async def test_reattach_failure_is_logged_and_the_turn_still_runs(agent: Any, caplog) -> None:
    StreamClient.failure = "always"
    turn = await agent.run_turn(KEY, "hi", "s1", None, RemoteSpec(reattach_id="cse_saved"))
    assert turn.text == "re: hi" and agent.remote_info(KEY) == RemoteInfo(None, "failed")
    assert "remote control re-attach failed key=thread:7 bridge=cse_saved" in caplog.text
    await agent.run_turn(KEY, "again", "s1", None, RemoteSpec(reattach_id="cse_saved"))
    assert len(StreamClient.instances[0].requests) == 1  # once per client, not per turn


async def test_app_turns_are_drained_between_and_skipped_during_a_turn(agent: Any, caplog) -> None:
    caplog.set_level(logging.INFO)
    await agent.run_turn(KEY, "hi", None, None, RemoteSpec())
    client = StreamClient.instances[0]
    for message in app_turn("typed in the app"):
        client.stream.put_nowait(message)
    await settle()
    assert client.stream.qsize() == 0 and "app turn done key=thread:7" in caplog.text
    client.before_reply = app_turn("another app reply")
    turn = await agent.run_turn(KEY, "from discord", "s1", None, RemoteSpec())
    assert turn.text == "re: from discord" and "skipped another turn's result" in caplog.text
    await agent.close_all()


async def test_bridge_state_and_app_mode_changes_are_tracked(agent: Any, caplog) -> None:
    await agent.run_turn(KEY, "hi", None, None, RemoteSpec())
    client = StreamClient.instances[0]
    client.stream.put_nowait(SystemMessage("bridge_state", {"state": "connected", "bridge_epoch": 1}))
    client.stream.put_nowait(SystemMessage("status", {"status": None, "permissionMode": "bypassPermissions"}))
    await settle()
    assert agent.remote_info(KEY).state == "connected"
    await agent.run_turn(KEY, "go", "s1", None, RemoteSpec())
    assert "was in permission mode bypassPermissions" in caplog.text
    assert client.events[-2:] == [("mode", "auto"), ("query", "go")]
    client.stream.put_nowait(SystemMessage("bridge_state", {"state": "failed", "detail": "x", "failure_kind": "t"}))
    await settle()
    assert agent.remote_info(KEY).state == "failed" and "bridge_state key=thread:7 state=failed" in caplog.text
    await agent.close_all()


async def test_dead_stream_fails_the_turn_over_to_a_new_client_that_re_attaches(agent: Any) -> None:
    await agent.run_turn(KEY, "hi", None, None, RemoteSpec())
    StreamClient.instances[0].stream.put_nowait(None)  # the CLI process went away
    await settle()
    turn = await agent.run_turn(KEY, "again", "s1", None, RemoteSpec(reattach_id="cse_new"))
    assert turn.text == "re: again" and len(StreamClient.instances) == 2
    assert StreamClient.instances[1].requests[0]["reattach_session_id"] == "cse_new"
    await agent.close_all()


async def test_enable_twice_is_one_request_and_the_same_link(agent: Any) -> None:
    first = await agent.enable_remote(DM_KEY, None, None, RemoteSpec("Nethor · DM"))
    again = await agent.enable_remote(DM_KEY, None, None, RemoteSpec("Nethor · DM"))
    client = StreamClient.instances[0]
    assert first == again and first.link.session_url == "https://claude.ai/code/cse_new"
    assert client.requests == [remote_request(True, RemoteSpec("Nethor · DM"))]
    await agent.close_all()


async def test_enable_falls_back_to_a_new_bridge_when_the_old_one_is_gone(agent: Any, caplog) -> None:
    StreamClient.failure = "reattach"
    info = await agent.enable_remote(KEY, "s1", None, RemoteSpec("Nethor · Trip", "cse_old"))
    assert info.link is not None and info.link.bridge_session_id == "cse_new"
    assert [r.get("reattach_session_id") for r in StreamClient.instances[0].requests] == ["cse_old", None]
    assert "starting a new one" in caplog.text
    await agent.close_all()


async def test_enable_failure_raises_the_cli_reason(agent: Any) -> None:
    StreamClient.failure = "always"
    with pytest.raises(RemoteError, match="initialization failed"):
        await agent.enable_remote(KEY, None, None, RemoteSpec("Nethor · Trip"))


async def test_disable_sends_enabled_false_and_a_failure_closes_the_client(agent: Any, caplog) -> None:
    await agent.enable_remote(KEY, None, None, RemoteSpec())
    await agent.disable_remote(KEY)
    assert StreamClient.instances[0].requests[-1] == {"subtype": "remote_control", "enabled": False}
    assert agent.remote_info(KEY) == RemoteInfo(None, None)
    await agent.enable_remote(KEY, None, None, RemoteSpec())
    StreamClient.failure = "always"
    await agent.disable_remote(KEY)
    assert KEY not in agent.live_keys() and StreamClient.instances[0].disconnected
    assert "remote control off failed key=thread:7" in caplog.text


async def test_settings_reconnect_re_attaches(agent: Any) -> None:
    await agent.run_turn(KEY, "hi", None, None, RemoteSpec())
    await agent.apply_settings(KEY, "s1", ConversationSettings(None, "high"), RemoteSpec(reattach_id="cse_new"))
    first, second = StreamClient.instances
    assert first.disconnected and second.requests[0]["reattach_session_id"] == "cse_new"
    await agent.close_all()


# --- /remote and the conversation lifecycle ----------------------------------------------------------------------

async def run_remote(bot: Any, inter: Any, *state: str) -> str:
    command = bot.tree.get_command("remote")
    assert command is not None
    await command.callback(inter, *state)
    return reply_of(inter)


async def test_on_in_a_dm_stores_the_link_and_replies_with_it(live) -> None:  # noqa: F811
    bot, manager, agent = live
    reply = await run_remote(bot, interaction(dm_channel(), guild=None))
    record = manager.record(DM_KEY)
    assert "https://claude.ai/code/dm:" in reply and reply.startswith("🔗 Remote Control is on")
    assert (record.remote, record.bridge_session_id, record.remote_state) == (True, "cse_1", "connected")
    assert record.remote_url == f"https://claude.ai/code/{DM_KEY}" and agent.enabled[0][1].name == "Nethor · DM"


async def test_on_in_a_forum_post_is_named_after_it_and_again_shows_the_same_link(live) -> None:  # noqa: F811
    bot, manager, agent = live
    await run_remote(bot, interaction(thread(tid=700)))
    assert agent.enabled[0][1] == RemoteSpec("Nethor · Plan Lisbon trip", None)
    again = await run_remote(bot, interaction(thread(tid=700)), "on")
    assert again == f"🔗 Remote Control is already on: https://claude.ai/code/{POST_KEY}"
    assert agent.enabled[1][1].reattach_id == "cse_1"


async def test_public_thread_and_plain_channel_are_refused(live) -> None:  # noqa: F811
    bot, manager, agent = live
    await manager.handle("pthread:710", "hi", Origin("public", 710, OWNER))
    assert await run_remote(bot, interaction(thread(tid=710, parent=601))) == PRIVATE_ONLY
    assert await run_remote(bot, interaction(text_channel())) == NOT_HERE
    assert agent.enabled == []


async def test_failure_is_one_line_and_logged(live, caplog) -> None:  # noqa: F811
    bot, manager, agent = live
    agent.remote_error = "Remote Control initialization failed"
    reply = await run_remote(bot, interaction(dm_channel(), guild=None))
    assert reply == "⚠️ Remote Control didn't start: Remote Control initialization failed"
    assert not manager.record(DM_KEY).remote and f"remote control on failed key={DM_KEY}" in caplog.text


async def test_off_clears_and_off_again_says_so(live) -> None:  # noqa: F811
    bot, manager, agent = live
    await run_remote(bot, interaction(dm_channel(), guild=None))
    assert await run_remote(bot, interaction(dm_channel(), guild=None), "off") == REMOTE_OFF
    record = manager.record(DM_KEY)
    assert (record.remote, record.bridge_session_id, record.remote_url, record.remote_state) == (False, None, None,
                                                                                                 None)
    assert agent.disabled == [DM_KEY]
    assert await run_remote(bot, interaction(dm_channel(), guild=None), "off") == REMOTE_ALREADY_OFF


async def test_turns_pass_the_saved_bridge_and_store_its_state(live) -> None:  # noqa: F811
    _, manager, agent = live
    await manager.handle(KEY, "before", Origin("private", 7, OWNER))
    await manager.enable_remote(KEY, "Nethor · x")
    agent.remote[KEY] = RemoteInfo(RemoteLink("https://claude.ai/code/cse_2", "cse_2"), "failed")
    await manager.handle(KEY, "after", Origin("private", 7, OWNER))
    assert agent.turn_remotes == [None, RemoteSpec(reattach_id="cse_1")]
    record = manager.record(KEY)
    assert (record.remote_state, record.bridge_session_id, record.remote_url) == (
        "failed", "cse_2", "https://claude.ai/code/cse_2")


async def test_new_clears_it_with_or_without_a_session(live) -> None:  # noqa: F811
    _, manager, agent = live
    origin = Origin("private", 7, OWNER)
    await manager.enable_remote(KEY, "Nethor · x", origin)
    await manager.new_conversation(KEY, origin)
    assert agent.disabled == [KEY] and not manager.record(KEY).remote
    await manager.handle(KEY, "hi", origin)
    await manager.enable_remote(KEY, "Nethor · x", origin)
    await manager.new_conversation(KEY, origin)
    assert agent.disabled == [KEY, KEY] and not manager.record(KEY).remote and manager.record(KEY).channel_id == 7


async def test_delete_and_daily_rollover_clear_it(live) -> None:  # noqa: F811
    _, manager, agent = live
    await manager.handle(KEY, "hi", Origin("private", 7, OWNER))
    await manager.enable_remote(KEY, "Nethor · x")
    async with manager.holding([KEY]):
        await manager.end_locked(KEY)
    assert agent.disabled == [KEY] and KEY not in manager._store.keys()
    await manager.handle(DM_KEY, "hi", Origin("private", 10, OWNER))
    await manager.enable_remote(DM_KEY, "Nethor · DM")
    manager._clock.now = ts(2026, 11, 19, 4, 5)

    async def deliver(key: str, text: str) -> None:
        raise AssertionError("nothing to deliver")

    await manager.tick(deliver)
    assert agent.disabled == [KEY, DM_KEY] and DM_KEY not in manager._store.keys()


async def test_suspend_keeps_it_on_and_the_next_turn_re_attaches(live) -> None:  # noqa: F811
    _, manager, agent = live
    origin = Origin("private", 7, OWNER)
    await manager.handle(KEY, "hi", origin)
    await manager.enable_remote(KEY, "Nethor · x", origin)
    manager._clock.now += IDLE_SECONDS

    async def deliver(key: str, text: str) -> None:
        raise AssertionError("nothing to deliver")

    await manager.tick(deliver)
    assert KEY in agent.closed and agent.disabled == [] and manager.record(KEY).remote
    await manager.handle(KEY, "back", origin)
    assert agent.turn_remotes[-1] == RemoteSpec(reattach_id="cse_1")


async def test_status_shows_remote(live) -> None:  # noqa: F811
    bot, manager, agent = live
    status = bot.tree.get_command("status")
    assert status is not None
    inter = interaction(dm_channel(), guild=None)
    await status.callback(inter)
    assert reply_of(inter).endswith("not started yet · remote off")
    await run_remote(bot, interaction(dm_channel(), guild=None))
    inter = interaction(dm_channel(), guild=None)
    await status.callback(inter)
    assert "remote on (connected)" in reply_of(inter)
    await agent.close_session(DM_KEY)
    assert (await manager.status(DM_KEY)).remote == "remote on (paused)"
    assert (await manager.status("pthread:710")).remote is None
