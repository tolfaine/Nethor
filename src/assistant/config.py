"""Environment loading and validation. Every problem names the variable to fix."""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from assistant.permissions import Targets, normalize_notion_id

ROOT = Path(__file__).resolve().parents[2]

# Auth must come from the claude.ai login; an API key would silently drop the connectors.
API_KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

ALWAYS_REQUIRED = ("ASSISTANT_CALENDAR_ID", "NOTION_ASSISTANT_PAGE_ID", "NOTION_SESSIONS_PAGE_ID")
DISCORD_REQUIRED = ("DISCORD_BOT_TOKEN", "ALLOWED_USER_IDS")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    discord_bot_token: str | None
    oauth_token_set: bool
    allowed_user_ids: frozenset[int]
    assistant_name: str
    tz: ZoneInfo
    model: str
    targets: Targets
    root: Path = ROOT
    allowed_guild_ids: frozenset[int] = frozenset()
    forum_channel_ids: frozenset[int] = frozenset()

    @property
    def agent_home(self) -> Path:
        return self.root / "agent_home"

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"


def _get(env: Mapping[str, str], name: str, default: str = "") -> str:
    return env.get(name, default).strip() or default


def _parse_ids(env: Mapping[str, str], name: str, what: str) -> frozenset[int]:
    parts = [p.strip() for p in _get(env, name).replace(";", ",").split(",") if p.strip()]
    bad = [p for p in parts if not p.isdigit()]
    if bad:
        raise ConfigError(f"{name} must be comma-separated numeric Discord {what} ids, got {bad}")
    return frozenset(int(p) for p in parts)


def _parse_tz(raw: str) -> ZoneInfo:
    try:
        return ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"ASSISTANT_TZ={raw!r} is not an IANA time zone (e.g. Europe/Oslo)") from exc


def _optional_notion_id(env: Mapping[str, str], name: str) -> str:
    raw = _get(env, name)
    if raw and normalize_notion_id(raw) is None:
        raise ConfigError(f"{name} must be a Notion page id (32 hex chars from the page URL) or empty, got {raw!r}")
    return raw


def config_warnings(config: Config) -> list[str]:
    """Settings that are valid but turn a feature off."""
    warnings = []
    if not config.targets.notion_memory_page_id:
        warnings.append("NOTION_MEMORY_PAGE_ID is empty: memory is off (nothing is loaded or saved; see .env.example)")
    return warnings


def missing_vars(env: Mapping[str, str], console: bool) -> list[str]:
    required = ALWAYS_REQUIRED if console else ALWAYS_REQUIRED + DISCORD_REQUIRED
    return [name for name in required if not _get(env, name)]


def load_config(env: Mapping[str, str], console: bool, root: Path = ROOT) -> Config:
    missing = missing_vars(env, console)
    if missing:
        raise ConfigError(f"Missing required setting(s) in .env: {', '.join(missing)} (see .env.example)")
    targets = Targets(
        calendar_id=_get(env, "ASSISTANT_CALENDAR_ID"),
        notion_assistant_page_id=_get(env, "NOTION_ASSISTANT_PAGE_ID"),
        notion_sessions_page_id=_get(env, "NOTION_SESSIONS_PAGE_ID"),
        notion_memory_page_id=_optional_notion_id(env, "NOTION_MEMORY_PAGE_ID"),
    )
    return Config(
        discord_bot_token=_get(env, "DISCORD_BOT_TOKEN") or None,
        oauth_token_set=bool(_get(env, "CLAUDE_CODE_OAUTH_TOKEN")),
        allowed_user_ids=_parse_ids(env, "ALLOWED_USER_IDS", "user"),
        assistant_name=_get(env, "ASSISTANT_NAME", "Nethor"),
        tz=_parse_tz(_get(env, "ASSISTANT_TZ", "Europe/Oslo")),
        model=_get(env, "ASSISTANT_MODEL", "claude-sonnet-5-5"),
        targets=targets,
        root=root,
        allowed_guild_ids=_parse_ids(env, "ALLOWED_GUILD_IDS", "server"),
        forum_channel_ids=_parse_ids(env, "FORUM_CHANNEL_IDS", "channel"),
    )


def strip_api_keys(environ: MutableMapping[str, str]) -> list[str]:
    """Remove API-key auth from the environment the CLI inherits; returns the names removed."""
    return [name for name in API_KEY_VARS if environ.pop(name, None) is not None]


def load_from_environment(console: bool, root: Path = ROOT) -> tuple[Config, list[str]]:
    from dotenv import load_dotenv

    load_dotenv(root / ".env", override=False)
    stripped = strip_api_keys(os.environ)
    return load_config(os.environ, console=console, root=root), stripped
