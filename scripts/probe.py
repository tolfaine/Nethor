"""Step 0 gate: does a headless Agent SDK session with only project settings get the claude.ai connectors?

--auth-check also sends one tiny turn and prints the CLI's reported auth source (apiKeySource).
"""

import asyncio
import os
import sys
import time
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, SystemMessage

ROOT = Path(__file__).resolve().parent.parent
AGENT_HOME = ROOT / "agent_home"
REQUIRED = ("claude.ai Google Calendar", "claude.ai Notion", "claude.ai Todoist")
WAIT_SECONDS = 90


async def wait_for_servers(client: ClaudeSDKClient) -> list[dict]:
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        servers = (await client.get_mcp_status())["mcpServers"]
        required = [s for s in servers if s["name"] in REQUIRED]
        settled = all(s["status"] != "pending" for s in required)
        if (settled and len(required) == len(REQUIRED)) or time.monotonic() > deadline:
            return servers
        await asyncio.sleep(2)


def report(servers: list[dict]) -> int:
    for server in sorted(servers, key=lambda s: s["name"]):
        tools = [t.get("name", "?") for t in server.get("tools", [])]
        marker = "*" if server["name"] in REQUIRED else " "
        print(f" {marker} {server['name']}: {server['status']} ({len(tools)} tools) {server.get('error', '')}")
        if server["name"] in REQUIRED:
            print("     ", ", ".join(sorted(tools)))
    ok = {s["name"] for s in servers if s["status"] == "connected" and s.get("tools")}
    missing = [name for name in REQUIRED if name not in ok]
    print("PASS" if not missing else f"FAIL, not connected: {missing}")
    return 1 if missing else 0


def load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(ROOT / ".env", override=False)


async def report_auth(client: ClaudeSDKClient, auth_check: bool) -> None:
    token_set = bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))
    print("CLAUDE_CODE_OAUTH_TOKEN set:", token_set, "(env token wins over the stored CLI login)" if token_set else "")
    account = (await client.get_server_info() or {}).get("account") or {}
    print("account:", {k: account.get(k) for k in ("apiProvider", "subscriptionType")})
    if not auth_check:
        return
    await client.query("Reply with exactly: OK")
    async for message in client.receive_response():
        if isinstance(message, SystemMessage) and message.subtype == "init":
            print("apiKeySource:", message.data.get("apiKeySource"))


async def main() -> int:
    load_env()
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        if os.environ.pop(name, None) is not None:
            print(f"ignored {name}: must run on the claude.ai login")
    options = ClaudeAgentOptions(
        tools=[],
        setting_sources=["project"],
        cwd=str(AGENT_HOME),
        model="claude-sonnet-5-5",
        permission_mode="dontAsk",
    )
    async with ClaudeSDKClient(options) as client:
        servers = await wait_for_servers(client)
        await report_auth(client, "--auth-check" in sys.argv)
    return report(servers)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
