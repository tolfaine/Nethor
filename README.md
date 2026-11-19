# Nethor

A personal assistant you talk to on Discord. It runs on your own PC (or a small Linux box) on the **Claude Agent SDK with your claude.ai login**, so it uses your claude.ai connectors and your subscription, with no API key needed.

- **Reminders**: "remind me at 18:00 to call mum" becomes an event with a popup on your own Google Calendar.
- **To-dos**: "add buy milk" goes to Todoist.
- **Memory**: "remember that…" goes on one lean Notion page that every new conversation reads first.
- **Web**: search and fetch for everything else.
- **Where you talk to it**: DMs, posts in a private forum, or a thread it opens when you @mention it. Private channels get full Nethor; channels others can read get a web-only public mode.
- **Safe by default**: routine actions run in Claude Code auto mode; deletions, invitations and RSVPs ask you with ✅/❌ buttons; Gmail, Drive, files and shell are blocked.
- **Slash commands**: `/status`, `/context`, `/model`, `/effort`, `/fast`, `/new`, `/delete`, `/remote` (open a conversation in the Claude app).

Requirements: Python 3.12+, a claude.ai account with the Google Calendar, Notion and Todoist connectors, a Discord bot in a private server of yours.

> Usage counts against your claude.ai plan's limits, shared with your other Claude sessions.

## How it works

- Four places, one Claude session per conversation, resumed after restarts (`data/state.json`):

  | Where | Mode | Session key | Lifetime | Approvals |
  |---|---|---|---|---|
  | DM | private | `dm:<user id>` | fresh at 04:00 (`ASSISTANT_TZ`), after 6 h idle, or `!new`; the old one first tidies the Memory page and appends a 3-line summary to the Notion `Sessions` page | buttons in the DM |
  | Post in a `FORUM_CHANNEL_IDS` forum (no @mention needed) | private | `thread:<post id>` | lives as long as the post: no 04:00 reset; after 6 h idle it does the wrap-up (Memory + summary) and frees the process but keeps the session, so the next message resumes it; `!new` = wrap-up + fresh session. The first turn gets the post title and tags as its topic | buttons in the post |
  | Thread Nethor opened on an @mention in an `ALLOWED_GUILD_IDS` channel **only you can see** | private | `thread:<thread id>` | like a forum post: no 04:00 reset, 6 h idle = wrap-up + suspend, `!new` = wrap-up + fresh. The first turn gets `#channel — channel description` as its topic | buttons in the thread |
  | Thread Nethor opened on an @mention in any other `ALLOWED_GUILD_IDS` channel | **public** | `pthread:<thread id>` | like a forum post but never a wrap-up: 6 h idle = suspend, `!new` = fresh | none |

- **In a server**, outside the forums, Nethor answers an @mention (of it or its role) or a reply to its message, from an `ALLOWED_USER_IDS` author, by opening a thread on that message (named after its first ~80 characters, archived after 7 days idle) and answering inside it. In that thread every message from an `ALLOWED_USER_IDS` author reaches it, no mention needed; each new mention in the channel opens a new thread with its own conversation. In a thread Nethor didn't open, it needs a mention once, answers in that same thread (never a thread in a thread) and from then on treats it like its own. A failed thread creation (missing permission) gets a one-line ⚠️ in the channel and an ERROR in the log. What it can do depends on who can read the channel:
  - **Private channel**: `@everyone` can't view it and every human who can view it is in `ALLOWED_USER_IDS` (bots don't count). A thread is judged by its parent channel; a private thread's members must also be allowlisted or bots. Full Nethor: all tools, the normal rules and session-start ritual, approvals in the thread.
  - **Public channel**: anything else. Web search and web fetch only, every claude.ai connector off and denied, rules from `agent_home/PUBLIC.md` (no memory, no personal data). Ask it about your calendar there and it tells you to use a DM, the forum or a private channel.
  - The check runs on every message and **fails closed**: member list not loaded yet (e.g. right after startup), Server Members Intent off, a channel type it can't judge, or any error → public. The reason is logged with each routing decision (`privacy=…`). Channels that can't have threads (voice, stage) are ignored with a WARNING.
  - A private thread whose parent channel opens up becomes a separate public conversation (`pthread:`, WARNING in the log). Replies, queued answers and approvals still owed to the private conversation go to your DM instead of the thread.
  - Forum posts in `FORUM_CHANNEL_IDS` always stay `thread:` conversations, whatever the forum's permissions.
  - v3 channel-level conversations (`priv:`/`pub:<channel id>`) are no longer created; old records load and expire on their own.
- Only `ALLOWED_USER_IDS` can trigger it anywhere; everyone else, and every other server, is ignored and logged.
- Any conversation idle for 30 min has its `claude` process closed (the session is kept and resumes on the next message), so many posts and threads don't pile up processes.
- **Memory**: one Notion page `Memory` (`NOTION_MEMORY_PAGE_ID`) with the headings About me, People, Preferences, Routines & places, Projects & goals, Other; one short English line per fact. Every new private session reads it first (and nothing else); "remember that …" edits it in place (an existing line about the same thing is changed, not duplicated; false lines are deleted); each wrap-up tidies it. The `Sessions` page is read only when you ask about past conversations. Notion's built-in Memory (`memory`) is retired and not used. Empty `NOTION_MEMORY_PAGE_ID` = memory off (WARNING at startup).
- Routing: has a time → 15-min event "⏰ …" on Nethor's calendar (`ASSISTANT_CALENDAR_ID`) with a popup at the time · to-do without time → Todoist · "remember that …" → the Memory page · unclear → it asks.
- Permissions (`src/assistant/permissions.py`, default deny): reads, Todoist task edits, events on Nethor's calendar, edits of the Memory and Sessions pages and new pages under Assistant/Sessions run automatically. Private conversations run in Claude Code **auto mode**: writes to other calendars and Notion create/edit/move anywhere get no hook decision, so the auto-mode classifier approves routine ones and blocks risky ones (if it can't decide, you're asked). Deletions (events, Todoist tasks, Notion pages), invites, RSVPs, page updates that would delete child pages, and new pages in the built-in `memory` always need your ✅/❌ in the DM, forum post or private thread (10 min, then denied; only you can click). Everything else (Gmail, Drive, files, shell, …) is blocked and hidden from the model.
- Usage limit hit: you get `⏳ limit hit, back at HH:MM`; the message is queued (`data/pending.json`) and answered automatically after the reset. It never falls back to an API key.
- Logs: `logs/assistant.log` (rotating): every routing decision (key, mode, reason), every ignored trigger, every tool call with its tier and mode, every approval outcome, every slash command, each turn's model, fast-mode state and speed, every error.

## Setup (Windows, first time)

1. **Python env** (Python 3.12+), from the repo folder:
   ```powershell
   py -3 -m venv .venv
   .venv\Scripts\python -m pip install -e ".[dev]"
   ```
2. **Claude login**: `claude` CLI logged in with your claude.ai account (the SDK ships its own CLI and reuses that login). Leave `CLAUDE_CODE_OAUTH_TOKEN` empty: a `claude setup-token` token is inference-only and the claude.ai connectors don't connect with it (probe prints `FAIL`). Never set `ANTHROPIC_API_KEY` either (it drops the connectors; the bot strips it anyway).
3. **claude.ai connectors**: Google Calendar, Notion and Todoist connected at claude.ai → Settings → Connectors. Check with:
   ```powershell
   .venv\Scripts\python -I -X utf8 scripts\probe.py --auth-check
   ```
   It must print `PASS`. The bot disables the other connectors for its own folder (`agent_home`) only; your other Claude Code projects are unaffected.
4. **A Google Calendar for Nethor**: Google Calendar → Other calendars → + → Create new calendar (e.g. `Nethor`). Then its Settings → Integrate calendar → copy **Calendar ID** (`…@group.calendar.google.com`) → `ASSISTANT_CALENDAR_ID`. The bot sets a popup at 0 min on each reminder.
5. **Notion**: a page `Assistant` with two child pages, `Sessions` and `Memory`. Give `Memory` a one-line intro and the headings `## About me`, `## People`, `## Preferences`, `## Routines & places`, `## Projects & goals`, `## Other` (Nethor adds one bullet per fact under them). Copy each page's 32-character id from its URL → `NOTION_ASSISTANT_PAGE_ID`, `NOTION_SESSIONS_PAGE_ID`, `NOTION_MEMORY_PAGE_ID`. Notion's built-in Memory (parent `memory`) is not used.
6. **Discord app**:
   - https://discord.com/developers/applications → New Application → name `Nethor`.
   - Bot tab → Reset Token → copy into `.env` as `DISCORD_BOT_TOKEN`. Turn **Message Content Intent** and **Server Members Intent** on (the second tells private channels from public ones; without it Discord refuses the login and the bot exits with code 2, so `run.ps1` stops instead of restarting). Turn **Public Bot** off.
   - OAuth2 → URL Generator → scopes `bot` + `applications.commands`, bot permissions View Channels, Send Messages, Send Messages in Threads, Create Public Threads, Manage Threads, Read Message History → open the URL → add it to a private server of yours (DMs need a shared server).
   - Discord app → Settings → Advanced → Developer Mode on → right-click your name → Copy User ID → `ALLOWED_USER_IDS`.
   - **Server permissions** for the bot (Server Settings → Roles → the `Nethor` role, or per channel): View Channels, Send Messages, **Send Messages in Threads** (forum posts and mention threads are threads; Send Messages alone doesn't work there), **Create Public Threads** (an @mention opens a thread on that message; without it Nethor posts `⚠️ … Create Public Threads …` in the channel instead), **Manage Threads** (only for `/delete`; without it the thread stays and the reply says so), Read Message History. The developer portal's "Bot Permissions" calculator only builds an invite link; it doesn't change a bot that's already in your server.
7. **Forum and public mode** (optional; leave the ids empty for DM-only):
   - Create a forum channel, e.g. `#nethor` (Create Channel → Forum). Edit Channel → Permissions → make it private: remove View Channel from `@everyone`, add yourself and the `Nethor` role with View Channel, Send Messages in Threads, Read Message History (and Manage Threads for `/delete`).
   - Developer Mode on → right-click the forum → Copy Channel ID → `FORUM_CHANNEL_IDS`. Right-click the server icon → Copy Server ID → `ALLOWED_GUILD_IDS`.
   - New post in the forum = new conversation; the title and tags become its topic. @Nethor in any other channel of that server = a new thread on your message, private mode if only you (and bots) can see the channel, public mode otherwise; the channel's name and description become its topic. For a private channel: Edit Channel → Permissions → remove View Channel from `@everyone`, add yourself and the `Nethor` role (View Channel, Send Messages, Send Messages in Threads, Create Public Threads, Read Message History). Anyone else with access, admins included, makes it public.
8. **`.env`**: `copy .env.example .env` and fill it in. Missing values are named on startup. `.env`, `data/` and `logs/` are git-ignored; never commit them.
9. **Try it in the terminal** (no Discord needed): `.venv\Scripts\python -I -X utf8 -m assistant --console`
10. **Run the bot**: `powershell -ExecutionPolicy Bypass -File scripts\run.ps1` (restart loop, backoff 5 s → 5 min, `logs\run.log`). DM the bot. To run it without a window: `Start-Process powershell -WindowStyle Hidden -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','scripts\run.ps1'`. Don't start it as a Claude Code background task: Claude Code stops those when memory runs low.
11. **Start at logon** (recommended): `powershell -ExecutionPolicy Bypass -File scripts\install-task.ps1` registers the Task Scheduler task `Nethor`; start it now with `Start-ScheduledTask -TaskName Nethor`. Set Windows power to never sleep on AC if the PC should answer all day.

## Commands

- `!new` — in a DM, forum post or private thread: wrap up (tidy Memory, save a summary), start a fresh session. In a public thread: just start fresh. Inside a thread it only resets that thread's conversation; the thread keeps answering without a mention.
- Console only: `!quit`.

### Slash commands

Type `/` in a DM with Nethor, a forum post, or a thread Nethor is talking in. Only you see the reply, and only `ALLOWED_USER_IDS` may use them (anyone else gets "not allowed", logged as a WARNING). In a plain channel, or a thread Nethor isn't in yet, they answer "use this inside a thread, forum post or DM". A command acts on the conversation a message in the same place would reach, with the same private/public check.

| Command | What it does |
|---|---|
| `/status` | One line: model · effort · fast mode · context use · private/public · when the conversation started · `remote on (<state>)` / `remote off` (private conversations). |
| `/context` | Context use with the biggest categories (messages, system tools, …). |
| `/model <Opus 5.5 \| Sonnet 5.5 \| Haiku 5.5 \| Fable 5.2>` | Switches the model at once, no restart. Fable uses far more of your plan's limits. |
| `/effort <low \| medium \| high \| xhigh \| max>` | Restarts the conversation's `claude` process with the new effort; the conversation continues where it was. |
| `/fast <on \| off>` | Fast mode. It only runs on Opus 5.5 and Fable 5.2: `/fast on` on another model switches the conversation to Opus 5.5 and says so; `/fast off` keeps the model. Restarts the process like `/effort`. |
| `/new` | Same as `!new`. |
| `/delete` | In a thread or forum post: asks "Delete this thread and its conversation? ✅/❌" (only you can click; no answer in 2 min = cancelled). ✅ ends the conversation for good (private: the `!new` wrap-up first; public: none), drops its queued messages and deletes the Discord thread or post. In a DM it refuses (Discord can't delete DMs; use `/new`). Needs **Manage Threads**. |
| `/remote [on \| off]` | Connects this conversation to Claude Code **Remote Control** and replies with its link, so you can open it in claude.ai or the Claude app (listed as `Nethor · <thread or post name>` / `Nethor · DM`). `on` (the default) when it's already on shows the same link again; `off` disconnects it. DMs, forum posts and private threads only; a public thread refuses ("Remote Control is only for private conversations"). |

- Defaults: `ASSISTANT_MODEL` (Sonnet 5.5), medium effort, fast off. Choices last **one conversation**: anything that starts fresh (`!new`, `/new`, a new thread or post, the 04:00 DM reset, a public reset) starts on the defaults again; idle suspend, the 30-min process close and a bot restart keep them (`data/state.json`).
- A change sent while Nethor is still replying applies right after that reply, never in the middle of it. `/delete` likewise waits for the running reply and says so.
- `/delete` outcomes: a failed wrap-up still deletes, and a DM tells you the summary wasn't saved. Without **Manage Threads** the conversation is already ended but the thread stays, and the ephemeral reply asks for the permission. A thread that went from private to public has both conversations ended. Each delete is logged with keys, user, wrap-up and delete outcome.
- Fast mode may still answer at standard speed. `/status` shows what the CLI reported: `fast on (last reply: standard)`, `fast on (last reply: fast)`, or `fast on, but the CLI says off: <reason>`. It never claims a fast reply the CLI didn't report.
- `/remote`, what to know:
  - It **uploads that conversation's history to claude.ai** (your account), and what you type in the app goes into the same conversation. Replies to messages typed in the app show in the app only, not in Discord.
  - The app can answer the ✅/❌ approval prompts too (whoever answers first wins; both are you), and it can change the model or permission mode or interrupt a reply. Nethor switches the conversation **back to auto mode before every reply it sends in Discord**, and logs a WARNING when the app had left it in another mode.
  - The link lives in that conversation's `claude` process. While Remote Control is on, the 30-min process close skips it; an idle suspend, a restart or an `/effort`/`/fast` change ends the process, and the next one re-attaches to the same entry (`/status` says `remote on (paused)` until your next message there). A re-attach that fails is logged and doesn't hold up the reply; `/remote on` then starts a new link.
  - It uses a private Agent SDK call (the Python SDK has no public Remote Control API yet), so an SDK upgrade can break it; `/remote` then says it isn't supported by this SDK version.
  - It is on demand and lasts one conversation, like `/model`: `!new`, `/new`, `/delete` and the 04:00 DM reset turn it off first.
- Context numbers are live while the conversation's process runs; otherwise `/status` and `/context` show the last known numbers with "(paused; refreshed on your next message)".
- Registration: on every start the commands sync to each `ALLOWED_GUILD_IDS` server (they appear at once) and globally for DMs. The DM commands can take a while to appear the first time (restart the Discord app if they don't show). A sync failure is logged and the bot keeps running; a server sync failing with `403 Missing Access` means the bot was invited without command rights: re-open the OAuth2 URL with scopes `bot` + `applications.commands`. Every command is logged with key, user, change and outcome.

## Security and privacy

- All secrets live in `.env` (Discord bot token, ids); it is git-ignored, and `.env.example` lists the names only. Never commit `.env`, `data/` (conversation ids, queued messages) or `logs/` (message excerpts, tool inputs).
- Nethor acts with your claude.ai login and your connectors. Keep `ALLOWED_USER_IDS` to yourself, keep **Public Bot** off, and only add it to servers you control.
- Anything that reaches a public thread (`pthread:`) can be read by everyone in that channel; that mode has no personal tools on purpose.
- Found a security problem? Please open a private security advisory on the repository instead of a public issue.

## Development

- Tests: `.venv\Scripts\python -I -X utf8 -m pytest`
- Dev rules: `CLAUDE.md`. The assistant's persona and rules: `agent_home/CLAUDE.md` (DMs, forum posts, private threads) and `agent_home/PUBLIC.md` (public threads); its name comes from `ASSISTANT_NAME`.
- Adding a tool: read its real input schema, add it to the table in `permissions.py`, add a test.
- The Python package is `assistant` (`python -m assistant`).

## Move to Linux

1. Copy the repo (without `.venv`, `data/` optional to keep sessions, `logs/`) to `~/Nethor`.
2. `python3 -m venv .venv && .venv/bin/pip install -e .`
3. Login: run `claude` once on the box and complete `/login` with your claude.ai account (on a headless box it prints a URL to open on another device and asks for the code). Do NOT use `claude setup-token`: its token has no connectors. Run `.venv/bin/python -I -X utf8 scripts/probe.py --auth-check` → `PASS`.
   Sessions are stored by the CLI under `~/.claude/projects/` for the `agent_home` path; they don't carry over between machines, so the first message there starts a fresh session (the Notion Memory page carries over).
4. `cp deploy/nethor.service ~/.config/systemd/user/ && systemctl --user daemon-reload && systemctl --user enable --now nethor`
5. `loginctl enable-linger "$USER"` so it runs without you logged in. Logs: `journalctl --user -u nethor -f` and `logs/assistant.log`.
6. Stop the Windows copy (Task Scheduler → disable `Nethor`): two bots on one token both answer.

## License

MIT, see [LICENSE](LICENSE).
