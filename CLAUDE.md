# CLAUDE.md — Nethor (dev rules)

Personal Discord assistant on the Claude Agent SDK. Plain git, no Perforce. Local only until the owner says push.
This file is for developing the bot. The bot's own persona and rules live in `agent_home/CLAUDE.md`; the running agent never sees this file (`CLAUDE_CODE_DISABLE_CLAUDE_MDS=1`, rules passed via the system prompt).

## Run

- Python: `.venv/Scripts/python -I -X utf8` (Windows) / `.venv/bin/python` (Linux).
- Tests: `.venv/Scripts/python -I -X utf8 -m pytest`
- Console chat (no Discord): `.venv/Scripts/python -I -X utf8 -m assistant --console`
- Bot: `scripts/run.ps1` (restart loop) or `python -m assistant`.

## Rules

- Never commit `.env`, `CLAUDE.local.md`, `data/`, `logs/`. No secrets in any tracked file.
- Never set `ANTHROPIC_API_KEY`: the bot must run on the claude.ai login (connectors). The code strips it.
- `permissions.py` is default deny. Adding a tool = add it to the table + a test in `tests/test_permissions.py`. Read the tool's real input schema first (ask a session to print it); never guess field names.
- Functions ≤ 50 lines, type hints, minimal comments (why, not what). Keep logic in small pure functions so it stays unit-testable.
- Errors: never swallowed. Log with context to `logs/assistant.log`, short ⚠️ line to the user.
- Don't run `scripts/install-task.ps1` or change power settings without the owner asking.
