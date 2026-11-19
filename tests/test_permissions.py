import json
from pathlib import Path

import pytest

from assistant.permissions import (
    CALENDAR, NOTION, TABLE, TODOIST, Targets, Tier, decide, normalize_notion_id, server_prefix,
)

CAL_ID = "abc123@group.calendar.google.com"
ASSISTANT_PAGE = "195de9221179449fab8075a27c979105"
SESSIONS_PAGE = "2a1de9221179449fab8075a27c979999"
MEMORY_PAGE = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
TARGETS = Targets(CAL_ID, ASSISTANT_PAGE, SESSIONS_PAGE, MEMORY_PAGE)
DISCOVERED = json.loads((Path(__file__).parent / "fixtures" / "discovered_tools.json").read_text())

AUTO = [CALENDAR + n for n in "list_calendars list_events search_events get_event suggest_time".split()] + [
    TODOIST + n for n in (
        "find-tasks find-tasks-by-date find-projects find-sections find-labels find-completed-tasks get-overview "
        "user-info fetch-object search fetch add-tasks complete-tasks uncomplete-tasks update-tasks reschedule-tasks"
    ).split()
] + [NOTION + n for n in "notion-search notion-fetch notion-get-self".split()] + [
    "WebSearch", "WebFetch"]
ASK = [CALENDAR + "delete_event", CALENDAR + "respond_to_event", TODOIST + "delete-object",
       NOTION + "notion-delete-pages"]
DENY = ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "Task", "Skill", "mcp__claude_ai_Gmail__send_message",
        "mcp__claude_ai_Google_Drive__search_files", "mcp__claude_ai_Atlassian__createJiraIssue",
        NOTION + "notion-memory-search", NOTION + "notion-create-comment", NOTION + "notion-duplicate-page", NOTION + "notion-get-users",
        TODOIST + "add-projects", TODOIST + "project-management", TODOIST + "add-reminders", "mcp__unknown__x", ""]


def tier(name: str, tool_input: dict | None = None) -> Tier:
    return decide(name, tool_input or {}, TARGETS).tier


@pytest.mark.parametrize("name", AUTO)
def test_auto_tools_allowed(name: str) -> None:
    assert tier(name) is Tier.ALLOW


@pytest.mark.parametrize("name", ASK)
def test_ask_tools(name: str) -> None:
    assert tier(name, {"calendarId": CAL_ID}) is Tier.ASK


def test_moving_notion_pages_is_left_to_auto_mode() -> None:
    assert tier(NOTION + "notion-move-pages", {"page_or_database_ids": ["a"], "new_parent": {}}) is Tier.AUTO


@pytest.mark.parametrize("name", DENY)
def test_everything_else_denied(name: str) -> None:
    assert tier(name) is Tier.DENY


def test_table_names_exist_in_real_connector_tool_list() -> None:
    assert sorted(set(TABLE) - set(DISCOVERED)) == []


def test_every_discovered_tool_outside_the_table_is_denied() -> None:
    for name in set(DISCOVERED) - set(TABLE):
        assert tier(name) is Tier.DENY, name


@pytest.mark.parametrize("tool", ["create_event", "update_event"])
def test_calendar_write_only_auto_on_assistant_calendar(tool: str) -> None:
    name = CALENDAR + tool
    assert tier(name, {"calendarId": CAL_ID, "eventId": "e1"}) is Tier.ALLOW
    assert tier(name, {"calendarId": CAL_ID.upper()}) is Tier.ALLOW
    assert tier(name, {"calendarId": "primary"}) is Tier.AUTO
    assert tier(name, {"calendarId": "me@gmail.com"}) is Tier.AUTO
    assert tier(name, {}) is Tier.AUTO  # no calendarId = primary calendar


def test_calendar_write_with_attendees_asks() -> None:
    assert tier(CALENDAR + "create_event", {"calendarId": CAL_ID, "attendees": [{"email": "x@y.z"}]}) is Tier.ASK
    assert tier(CALENDAR + "update_event", {"attendees": [{"email": "x@y.z"}]}) is Tier.ASK  # primary + invites


def test_notion_create_pages_parent_rules() -> None:
    name = NOTION + "notion-create-pages"
    assert tier(name, {"parent": "memory", "pages": []}) is Tier.ASK  # built-in Notion Memory is retired
    assert tier(name, {"parent": {"page_id": MEMORY_PAGE}}) is Tier.AUTO
    assert tier(name, {"parent": {"type": "page_id", "page_id": ASSISTANT_PAGE}}) is Tier.ALLOW
    dashed = "2a1de922-1179-449f-ab80-75a27c979999"
    assert tier(name, {"parent": {"page_id": dashed}}) is Tier.ALLOW
    assert tier(name, {"parent": json.dumps({"page_id": SESSIONS_PAGE})}) is Tier.ALLOW
    assert tier(name, {"parent": {"page_id": "f" * 32}}) is Tier.AUTO
    assert tier(name, {"parent": {"database_id": ASSISTANT_PAGE}}) is Tier.AUTO
    assert tier(name, {"parent": {"teamspace_id": "t1"}}) is Tier.AUTO
    assert tier(name, {"pages": []}) is Tier.AUTO  # no parent = new workspace page


def test_notion_update_page_only_auto_on_sessions_page() -> None:
    name = NOTION + "notion-update-page"
    assert tier(name, {"page_id": SESSIONS_PAGE, "command": "insert_content"}) is Tier.ALLOW
    url = f"https://www.notion.so/Sessions-{SESSIONS_PAGE}"
    assert tier(name, {"page_id": url, "command": "insert_content"}) is Tier.ALLOW
    assert tier(name, {"page_id": ASSISTANT_PAGE, "command": "replace_content"}) is Tier.AUTO
    assert tier(name, {"page_id": "memory"}) is Tier.AUTO


def test_normalize_notion_id() -> None:
    assert normalize_notion_id("2A1DE922-1179-449F-AB80-75A27C979999") == SESSIONS_PAGE
    assert normalize_notion_id("not an id") is None
    assert normalize_notion_id(None) is None


def test_server_prefix_matches_real_names() -> None:
    assert server_prefix("claude.ai Google Calendar") == CALENDAR
    assert server_prefix("claude.ai Notion") == NOTION
    assert server_prefix("claude.ai Todoist") == TODOIST


PUBLIC_DENIED = sorted((set(TABLE) | set(DISCOVERED) | set(DENY)) - {"WebSearch", "WebFetch"})


@pytest.mark.parametrize("name", PUBLIC_DENIED)
def test_public_mode_denies_every_connector_and_builtin(name: str) -> None:
    decision = decide(name, {"calendarId": CAL_ID, "parent": "memory", "page_id": SESSIONS_PAGE}, TARGETS, "public")
    assert decision.tier is Tier.DENY


@pytest.mark.parametrize("name", ["WebSearch", "WebFetch"])
def test_public_mode_allows_web_tools(name: str) -> None:
    assert decide(name, {"query": "x"}, TARGETS, "public").tier is Tier.ALLOW


def test_public_mode_never_asks() -> None:
    assert all(decide(name, {}, TARGETS, "public").tier is not Tier.ASK for name in ASK)
    assert all(decide(name, {}, TARGETS, "private").tier is Tier.ASK for name in ASK)


def test_public_denies_cover_all_three_kept_connectors() -> None:
    prefixes = {name.split("__")[1] for name in PUBLIC_DENIED if name.startswith("mcp__")}
    assert {"claude_ai_Google_Calendar", "claude_ai_Notion", "claude_ai_Todoist"} <= prefixes


def test_notion_update_page_auto_on_memory_page() -> None:
    name = NOTION + "notion-update-page"
    edit = {"page_id": MEMORY_PAGE, "command": "update_content",
            "content_updates": [{"old_str": "## People", "new_str": "## People\n- Sister: Ana"}]}
    assert tier(name, edit) is Tier.ALLOW
    assert tier(name, {**edit, "page_id": "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"}) is Tier.ALLOW
    assert tier(name, {**edit, "page_id": "f" * 32}) is Tier.AUTO


def test_notion_update_page_deleting_child_pages_asks() -> None:
    name = NOTION + "notion-update-page"
    for page in (MEMORY_PAGE, SESSIONS_PAGE, "f" * 32):
        assert tier(name, {"page_id": page, "command": "replace_content", "new_str": "x",
                           "allow_deleting_content": True}) is Tier.ASK


def test_memory_page_off_never_matches_an_empty_id() -> None:
    off = Targets(CAL_ID, ASSISTANT_PAGE, SESSIONS_PAGE)
    assert decide(NOTION + "notion-update-page", {"page_id": "", "command": "update_content"}, off).tier is Tier.AUTO
    assert decide(NOTION + "notion-update-page", {"page_id": MEMORY_PAGE, "command": "update_content"},
                  off).tier is Tier.AUTO
