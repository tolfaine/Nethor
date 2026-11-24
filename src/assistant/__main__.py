"""Entry point: `python -m assistant` runs the Discord bot, `--console` chats in the terminal."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from assistant.agent import Agent, ApprovalRequest
from assistant.config import Config, ConfigError, config_warnings, load_from_environment
from assistant.pending import PendingQueue
from assistant.sessions import SessionManager, SessionStore, run_maintenance_loop

log = logging.getLogger("assistant")

CONSOLE_USER = "console"


def setup_logging(logs_dir: Path, console_level: int) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(logs_dir / "assistant.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(fmt)
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(fmt)
    stream_handler.setLevel(console_level)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers[:] = [file_handler, stream_handler]


def build_manager(config: Config, agent: Agent) -> SessionManager:
    return SessionManager(
        agent,
        SessionStore(config.data_dir / "state.json"),
        PendingQueue(config.data_dir / "pending.json"),
        config.tz,
        config.targets.notion_sessions_page_id,
        memory_page_id=config.targets.notion_memory_page_id,
        default_model=config.model,
    )


def _read_line(prompt: str) -> str | None:
    try:
        return input(prompt)
    except EOFError:
        return None


class ConsoleApprover:
    async def approve(self, request: ApprovalRequest) -> bool:
        answer = await asyncio.to_thread(_read_line, f"\n🔐 Approve this action?\n{request.summary()}\n[y/N] ")
        return (answer or "").strip().lower() in ("y", "yes")


async def _console_deliver(user_key: str, text: str) -> None:
    print(f"\n(late reply) {text}\n", flush=True)


async def run_console(config: Config) -> None:
    agent = Agent(config, ConsoleApprover())
    await agent.discover()
    manager = build_manager(config, agent)
    maintenance = asyncio.create_task(run_maintenance_loop(manager, _console_deliver))
    print(f"{config.assistant_name} console. !new = fresh session, !quit or Ctrl+Z/Ctrl+D = exit.", flush=True)
    try:
        while True:
            line = await asyncio.to_thread(_read_line, "you> ")
            if line is None or line.strip().lower() in ("!quit", "!exit"):
                break
            if line.strip():
                reply = await manager.handle(CONSOLE_USER, line.strip())
                print(f"{config.assistant_name}> {reply}", flush=True)
    finally:
        maintenance.cancel()
        await agent.close_all()


async def run_discord(config: Config) -> None:
    from assistant.bot import AssistantBot, DiscordApprover

    assert config.discord_bot_token is not None
    bot = AssistantBot(config)
    from assistant.discord_tools import SERVER_NAME, build_discord_server

    def own_servers() -> dict[str, Any]:
        return {SERVER_NAME: build_discord_server(lambda: bot, config.allowed_guild_ids)}

    agent = Agent(config, DiscordApprover(bot), own_servers)
    await agent.discover()
    bot.manager = build_manager(config, agent)
    try:
        async with bot:
            await bot.start(config.discord_bot_token)
    finally:
        await agent.close_all()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="assistant", description="Personal Discord assistant on the Claude Agent SDK")
    parser.add_argument("--console", action="store_true", help="chat in this terminal instead of Discord")
    args = parser.parse_args(argv)
    try:
        config, stripped = load_from_environment(console=args.console)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    setup_logging(config.logs_dir, logging.WARNING if args.console else logging.INFO)
    if stripped:
        log.warning("ignored %s: the assistant only runs on the claude.ai login", ", ".join(stripped))
    if config.oauth_token_set:
        log.warning("CLAUDE_CODE_OAUTH_TOKEN is set: setup-token tokens carry no claude.ai connectors; empty it in .env")
    for warning in config_warnings(config):
        log.warning("%s", warning)
    log.info("starting mode=%s name=%s model=%s tz=%s oauth_token=%s", "console" if args.console else "discord",
             config.assistant_name, config.model, config.tz.key, config.oauth_token_set)
    try:
        asyncio.run(run_console(config) if args.console else run_discord(config))
    except KeyboardInterrupt:
        log.info("stopped by Ctrl+C")
    except Exception as exc:
        return fatal_exit_code(exc)
    return 0


def fatal_exit_code(exc: BaseException) -> int:
    """2 = a setup problem restarting can't fix (run.ps1 and systemd stop on it), 1 = restart."""
    import discord

    if isinstance(exc, discord.PrivilegedIntentsRequired):
        log.error("Discord refused a privileged intent: enable Message Content Intent and Server Members Intent in "
                  "the Developer Portal → your app → Bot, then start the bot again (%s)", exc)
        return 2
    log.error("fatal error", exc_info=exc)
    return 1


if __name__ == "__main__":
    sys.exit(main())
