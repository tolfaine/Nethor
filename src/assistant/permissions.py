"""Tool permission tiers: a pure, table-driven check over (tool_name, input). Default deny."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from assistant.keys import PRIVATE, PUBLIC, Mode

CALENDAR = "mcp__claude_ai_Google_Calendar__"
NOTION = "mcp__claude_ai_Notion__"
TODOIST = "mcp__claude_ai_Todoist__"

KEPT_SERVERS = ("claude.ai Google Calendar", "claude.ai Notion", "claude.ai Todoist")
PUBLIC_TOOLS = ("WebSearch", "WebFetch")

_HEX32 = re.compile(r"[0-9a-f]{32}")


class Tier(StrEnum):
    ALLOW = "allow"
    AUTO = "auto"  # no hook decision: Claude Code's auto-mode classifier decides
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True)
class Decision:
    tier: Tier
    reason: str


@dataclass(frozen=True)
class Targets:
    """The only write targets that are auto-approved."""

    calendar_id: str
    notion_assistant_page_id: str
    notion_sessions_page_id: str
    notion_memory_page_id: str = ""  # empty = memory off


Rule = Tier | Callable[[Mapping[str, Any], Targets], Decision]


def normalize_notion_id(value: object) -> str | None:
    """Return the 32-hex Notion id inside an id (with or without dashes) or a page URL."""
    if not isinstance(value, str):
        return None
    matches = _HEX32.findall(value.lower().replace("-", ""))
    return matches[-1] if matches else None


def _same_id(value: object, expected: str) -> bool:
    found = normalize_notion_id(value)
    return found is not None and found == normalize_notion_id(expected)


def _calendar_write(tool_input: Mapping[str, Any], targets: Targets) -> Decision:
    calendar_id = tool_input.get("calendarId")
    if tool_input.get("attendees"):
        return Decision(Tier.ASK, "event has attendees, would send invitations")
    if not isinstance(calendar_id, str) or not calendar_id.strip():
        return Decision(Tier.AUTO, "no calendarId, would write to the primary calendar")
    if calendar_id.strip().casefold() != targets.calendar_id.strip().casefold():
        return Decision(Tier.AUTO, f"write to calendar {calendar_id!r}, not the Assistant calendar")
    return Decision(Tier.ALLOW, "Assistant calendar")


def _parse_parent(parent: object) -> object:
    # The model sometimes passes the parent object as a JSON string.
    if isinstance(parent, str) and parent.strip().startswith("{"):
        try:
            return json.loads(parent)
        except json.JSONDecodeError:
            return parent
    return parent


def _notion_create(tool_input: Mapping[str, Any], targets: Targets) -> Decision:
    parent = _parse_parent(tool_input.get("parent"))
    if parent == "memory":
        return Decision(Tier.ASK, "built-in Notion Memory is retired; facts go on the Memory page")
    if isinstance(parent, Mapping):
        page_id = parent.get("page_id")
        allowed = (targets.notion_assistant_page_id, targets.notion_sessions_page_id)
        if any(_same_id(page_id, expected) for expected in allowed):
            return Decision(Tier.ALLOW, "under the Assistant page")
    return Decision(Tier.AUTO, f"create pages under {parent!r}")


def _notion_update(tool_input: Mapping[str, Any], targets: Targets) -> Decision:
    page_id = tool_input.get("page_id")
    own = {"Sessions page": targets.notion_sessions_page_id, "Memory page": targets.notion_memory_page_id}
    name = next((name for name, expected in own.items() if _same_id(page_id, expected)), None)
    if tool_input.get("allow_deleting_content"):
        return Decision(Tier.ASK, f"would delete child pages or databases of {name or repr(page_id)}")
    if name is None:
        return Decision(Tier.AUTO, f"update page {page_id!r}")
    return Decision(Tier.ALLOW, name)


def _names(prefix: str, names: str) -> list[str]:
    return [prefix + name for name in names.split()]


def _build_table() -> dict[str, Rule]:
    table: dict[str, Rule] = {}
    auto = (
        _names(CALENDAR, "list_calendars list_events search_events get_event suggest_time")
        + _names(
            TODOIST,
            "find-tasks find-tasks-by-date find-projects find-sections find-labels find-completed-tasks "
            "get-overview user-info fetch-object search fetch add-tasks complete-tasks uncomplete-tasks "
            "update-tasks reschedule-tasks",
        )
        + _names(NOTION, "notion-search notion-fetch notion-get-self")
        + ["WebSearch", "WebFetch"]
    )
    # Deletes and anything that messages other people still ask the owner, even in auto mode.
    ask = _names(CALENDAR, "delete_event respond_to_event") + _names(TODOIST, "delete-object")
    ask += _names(NOTION, "notion-delete-pages")
    table.update({name: Tier.ALLOW for name in auto})
    table.update({name: Tier.ASK for name in ask})
    table[NOTION + "notion-move-pages"] = Tier.AUTO
    table[CALENDAR + "create_event"] = _calendar_write
    table[CALENDAR + "update_event"] = _calendar_write
    table[NOTION + "notion-create-pages"] = _notion_create
    table[NOTION + "notion-update-page"] = _notion_update
    return table


TABLE: dict[str, Rule] = _build_table()


def decide(tool_name: str, tool_input: Mapping[str, Any] | None, targets: Targets, mode: Mode = PRIVATE) -> Decision:
    if mode == PUBLIC:
        # Others can read a public channel: web only, and never an approval prompt.
        if tool_name in PUBLIC_TOOLS:
            return Decision(Tier.ALLOW, "public web tool")
        return Decision(Tier.DENY, "not allowed in public mode")
    rule = TABLE.get(tool_name)
    if rule is None:
        return Decision(Tier.DENY, "not on the allow list")
    if isinstance(rule, Tier):
        return Decision(rule, "table")
    return rule(tool_input or {}, targets)


def is_listed(tool_name: str) -> bool:
    """True when a tool can ever run; unlisted tools are hidden from the model entirely."""
    return tool_name in TABLE


def server_prefix(server_name: str) -> str:
    """Model-facing tool prefix for an MCP server, e.g. 'claude.ai Notion' -> 'mcp__claude_ai_Notion__'."""
    return "mcp__" + re.sub(r"[^A-Za-z0-9_-]", "_", server_name) + "__"
