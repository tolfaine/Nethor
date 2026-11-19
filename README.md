# Nethor

A personal assistant you talk to on Discord. It runs on your own PC (or a small Linux box) on the **Claude Agent SDK with your claude.ai login**, so it uses your claude.ai connectors and your subscription, with no API key needed.

- **Reminders**: "remind me at 18:00 to call mum" becomes an event with a popup on your own Google Calendar.
- **To-dos**: "add buy milk" goes to Todoist.
- **Memory**: "remember that…" goes on one lean Notion page that every new conversation reads first.
- **Web**: search and fetch for everything else.
- **Where you talk to it**: DMs, posts in a private forum, or a thread it opens when you @mention it. Private channels get full Nethor; channels others can read get a web-only public mode.
- **Safe by default**: routine actions run in Claude Code auto mode; deletions, invitations and RSVPs ask you with ✅/❌ buttons; Gmail, Drive, files and shell are blocked.
- **Slash commands**: `/status`, `/context`, `/model`, `/effort`, `/fast`, `/new`, `/delete`, `/remote` (open a conversation in the Claude app).

Requirements: Python 3.12+, a claude.ai account with the Google Calendar, Notion and Todoist connectors, a Discord bot in a private server of yours. Step by step: [Getting started](#getting-started).

> Usage counts against your claude.ai plan's limits, shared with your other Claude sessions.

## Getting started

From `git clone` to "Nethor answered my DM", on Windows. Do the steps in order: each one gives you something a later one needs. Steps 4 to 9 hand you a token and some ids; paste them into a Notepad window as you go (don't save it inside the repo) and move them into `.env` in step 10.

### 1. Prerequisites

- Windows 10 or 11. Linux works too: see [Move to Linux](#move-to-linux) for the commands that differ.
- Python 3.12 or newer (check with `py --version`).
- Git.
- The Claude Code CLI, logged in with your claude.ai account: install it ([Claude Code docs](https://code.claude.com/docs)), run `claude`, type `/login`, sign in, then `/exit`. Nethor's SDK ships its own CLI and reuses this login.
- A Discord account.
- A Google account (for Google Calendar), a Notion account and a Todoist account.

No API key is involved: usage counts against your claude.ai plan's limits.

### 2. Get the code

1. Clone the repo:
   ```powershell
   git clone https://github.com/tolfaine/Nethor.git
   ```
2. Go into it (every later command runs from this folder):
   ```powershell
   cd Nethor
   ```
3. Create the Python environment:
   ```powershell
   py -3 -m venv .venv
   ```
4. Install Nethor and its test tools:
   ```powershell
   .venv\Scripts\python -m pip install -e ".[dev]"
   ```

### 3. Connect the claude.ai connectors

1. On claude.ai open **Settings → Connectors**.
2. Connect **Google Calendar**, **Notion** and **Todoist** (sign in to each when asked).
3. Check that Nethor sees all three (it can take a minute or two):
   ```powershell
   .venv\Scripts\python -I -X utf8 scripts\probe.py --auth-check
   ```
   The last line must be `PASS`. `FAIL, not connected: [...]` names the connector to fix.

> ⚠️ Leave `CLAUDE_CODE_OAUTH_TOKEN` empty and never set `ANTHROPIC_API_KEY`. A `claude setup-token` token is inference-only, so the connectors don't connect and the probe prints `FAIL`; an API key drops them too (the bot and the probe ignore it anyway).

Nethor turns your other connectors off for its own folder (`agent_home`) only; your other Claude Code projects are unaffected.

### 4. Create the Discord bot

You add the bot to a server of yours in the last bullet. No server of your own yet? Do [step 5](#5-a-private-discord-server) first.

1. Open https://discord.com/developers/applications → **New Application**.
2. Name it (e.g. `Nethor`), accept the terms → **Create**.
3. Open the **Bot** tab. Optional: set an icon and a **Username**. The username is what people see in chats; the application name is separate.
4. **Reset Token** → confirm → copy the token. It is shown once and becomes `DISCORD_BOT_TOKEN`. Treat it like a password: anyone who has it can act as your bot. Lost it? Reset it again; the old one stops working.
5. Same page, **Privileged Gateway Intents**: turn on **Message Content Intent** and **Server Members Intent** → **Save Changes**. Server Members is how Nethor tells private channels from public ones; with either intent off Discord refuses the login, the bot exits with code 2 and `run.ps1` stops.
6. Same page: turn **Public Bot** off → **Save Changes**, so only you can add it to servers. If Discord says a private application can't have a default authorization link: **Installation** tab → **Install Link** → **None** → **Save Changes**, then turn **Public Bot** off again.
7. **OAuth2** tab → **URL Generator**: tick the scopes **bot** and **applications.commands**.
8. Under **Bot Permissions** tick **View Channels**, **Send Messages**, **Send Messages in Threads**, **Create Public Threads**, **Manage Threads** and **Read Message History**.
9. Copy the generated URL at the bottom, open it in your browser, pick your server → **Continue** → **Authorize**.

The **Bot Permissions** calculator on the **Bot** page only builds an invite link; it doesn't change a bot that's already in a server. To change permissions later, see [Discord permissions reference](#discord-permissions-reference).

### 5. A private Discord server

Skip this if you already have a server of your own. You need one because Discord only lets you DM a bot you share a server with.

1. In the Discord app, click **+** (**Add a Server**) at the bottom of the server list.
2. **Create My Own** → **For me and my friends**.
3. Name it → **Create**.
4. Came here from step 4? Go back and finish it.

### 6. Copy your Discord ids

Discord ids are long numbers like `123456789012345678`.

1. Discord app → **User Settings** (the gear next to your name) → **Advanced** → turn **Developer Mode** on.
2. Right-click your own name (in a message or the member list) → **Copy User ID** → this is `ALLOWED_USER_IDS`.
3. Optional, to talk to Nethor in server channels: right-click the server icon → **Copy Server ID** → this is `ALLOWED_GUILD_IDS`. It also makes the slash commands show up in that server right away.

### 7. Optional: a private forum

A forum gives you one conversation per topic: every new post is its own conversation, and its title and tags become the topic. Skip this for DMs only.

1. In your server, **Create Channel** → **Forum** → name it (e.g. `nethor`) → **Create Channel**.
2. Right-click the forum → **Edit Channel** → **Permissions**.
3. Remove **View Channel** from `@everyone`.
4. Add yourself and the bot's role (Discord names it after the bot) with **View Channel**, **Send Messages in Threads**, **Read Message History** and **Manage Threads** allowed → **Save Changes**.
5. Right-click the forum → **Copy Channel ID** → this is `FORUM_CHANNEL_IDS`.

### 8. Google Calendar

Nethor puts every reminder on a calendar of its own, as a 15-minute "⏰ …" event with a popup at the time.

1. On Google Calendar, in the left sidebar: **Other calendars** → **+** → **Create new calendar**.
2. Name it (e.g. `Nethor`) → **Create calendar**.
3. Under **My calendars**, hover the new calendar → the three dots → **Settings and sharing**.
4. Scroll to **Integrate calendar** → copy the **Calendar ID** (`…@group.calendar.google.com`) → this is `ASSISTANT_CALENDAR_ID`.

### 9. Notion

1. Create a page named `Assistant`.
2. Inside it, create two child pages: `Sessions` and `Memory`.
3. In `Memory`, write a one-line intro, then add these six headings (type `## ` and the name). Nethor adds one bullet per fact under them:
   ```
   ## About me
   ## People
   ## Preferences
   ## Routines & places
   ## Projects & goals
   ## Other
   ```
4. On each of the three pages: **Share** → **Copy link**. The page id is the 32 characters at the end of the link, e.g. `https://www.notion.so/Memory-0123456789abcdef0123456789abcdef` → `0123456789abcdef0123456789abcdef`.
5. Note them as `NOTION_ASSISTANT_PAGE_ID`, `NOTION_SESSIONS_PAGE_ID` and `NOTION_MEMORY_PAGE_ID`.

Notion's built-in Memory (`memory`) is not used.

### 10. Fill in `.env`

1. Create your `.env` from the template:
   ```powershell
   copy .env.example .env
   ```
2. Open it:
   ```powershell
   notepad .env
   ```
3. Fill in the values below, save, and close Notepad (and the scratch window from steps 4 to 9 without saving).

| Variable | Needed? | Where it comes from | Format |
|---|---|---|---|
| `DISCORD_BOT_TOKEN` | Required for Discord (not for `--console`) | Step 4, **Reset Token** | one long string with two dots |
| `CLAUDE_CODE_OAUTH_TOKEN` | **Leave empty** | (see the warning in step 3) | empty |
| `ALLOWED_USER_IDS` | Required for Discord (not for `--console`) | Step 6, **Copy User ID** | `123456789012345678` (several: comma-separated) |
| `FORUM_CHANNEL_IDS` | Optional; empty = no forum | Step 7, **Copy Channel ID** | `123456789012345678` (several: comma-separated) |
| `ALLOWED_GUILD_IDS` | Optional; empty = no server channels | Step 6, **Copy Server ID** | `123456789012345678` (several: comma-separated) |
| `ASSISTANT_NAME` | Optional; default `Nethor` | Your choice; renaming = this + the Discord portal name | `Nethor` |
| `ASSISTANT_TZ` | Optional; default `Europe/Oslo`, **set yours** | Your IANA time zone; used for reminders, the 04:00 reset and "back at HH:MM" | `Europe/London`, `America/New_York` |
| `ASSISTANT_CALENDAR_ID` | Required | Step 8, **Calendar ID** | `…@group.calendar.google.com` |
| `NOTION_ASSISTANT_PAGE_ID` | Required | Step 9, `Assistant` page | `0123456789abcdef0123456789abcdef` |
| `NOTION_SESSIONS_PAGE_ID` | Required | Step 9, `Sessions` page | `0123456789abcdef0123456789abcdef` |
| `NOTION_MEMORY_PAGE_ID` | Optional; empty = memory off (startup WARNING) | Step 9, `Memory` page | `0123456789abcdef0123456789abcdef` |
| `ASSISTANT_MODEL` | Optional; default `claude-sonnet-5-5` | Claude model id | `claude-sonnet-5-5` |

A missing required value is named when Nethor starts. `.env` is git-ignored (so are `data/` and `logs/`); never commit it.

### 11. Try it without Discord

1. Start the terminal chat (it needs only the calendar and Notion values):
   ```powershell
   .venv\Scripts\python -I -X utf8 -m assistant --console
   ```
2. At `you>`, type a question, e.g. `what's on my calendar tomorrow?`. Actions that need your approval ask `[y/N]` right there.
3. Type `!quit` to exit (`!new` starts a fresh session).

### 12. Run the bot

1. Start it and leave the window open:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\run.ps1
   ```
   It restarts the bot when it exits (waiting 5 s, up to 5 min) and logs to `logs\`: `run.log` (starts and exits), `stdout-stderr.log` (console output, config errors), `assistant.log` (everything the bot does).
2. Or run it without a window, from the `Nethor` folder:
   ```powershell
   Start-Process powershell -WindowStyle Hidden -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','scripts\run.ps1'
   ```

Don't start it as a Claude Code background task: Claude Code stops those when memory runs low.

### 13. First test

1. In your server's member list, click the bot and send it a message: that opens your DM with it.
2. Send `what's on my calendar tomorrow?`. The first reply of a conversation takes a little longer (it reads the Memory page first).
3. Send `remind me in 5 minutes to stretch` → a "⏰ …" event on Nethor's calendar, with a popup in 5 minutes.
4. Send `add buy milk to my todo` → a task in Todoist.
5. Send `remember my favourite tea is genmaicha` → a new line on the Memory page.
6. Type `/` to see the [slash commands](#slash-commands).

### 14. Start automatically (recommended)

1. Stop the copy from step 12 (close its window): two copies on one token both answer.
2. Register the Task Scheduler task `Nethor`, which starts `run.ps1` hidden at logon (a normal, non-admin PowerShell is fine):
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\install-task.ps1
   ```
3. Start it now:
   ```powershell
   Start-ScheduledTask -TaskName Nethor
   ```
4. If the PC should answer all day: in the Windows power settings, set sleep to **Never** when plugged in.

To remove the task: `Unregister-ScheduledTask -TaskName Nethor -Confirm:$false`.

### Troubleshooting

| Problem | Fix |
|---|---|
| "The application did not respond" on a slash command, or no reply at all | The bot isn't running: check `logs\run.log`, then start it (step 12, or `Start-ScheduledTask -TaskName Nethor`). |
| The probe prints `FAIL` | Connect the named connector at claude.ai → **Settings → Connectors**; make sure `CLAUDE_CODE_OAUTH_TOKEN` is empty and `ANTHROPIC_API_KEY` isn't set. |
| The bot exits right away with code 2 | `logs\stdout-stderr.log` names the setting to fix (`Config error: …`), or **Server Members Intent** / **Message Content Intent** is off (step 4). `run.ps1` doesn't retry on code 2: start it again after the fix. |
| Slash commands missing in DMs | They can take a while to appear the first time; restart the Discord app. |
| A server sync fails with `403 Missing Access` (in `logs\assistant.log`) | The bot was invited without command rights: open the OAuth2 URL again with **bot** + **applications.commands** (step 4). |
| `⚠️ I can't open a thread here: give me the Create Public Threads permission in this channel.` | Give the bot's role **Create Public Threads** there ([reference](#discord-permissions-reference)). |
| `/delete` says it needs **Manage Threads** | Give the bot's role **Manage Threads** in that channel or forum. |
| Calendar questions refused in a channel | That channel is public (someone else can read it): use a DM, the forum or a channel only you can see. Right after startup every channel counts as public for a moment while the member list loads. |

### Discord permissions reference

Set them for the whole server in **Server Settings → Roles →** the bot's role, or per channel in **Edit Channel → Permissions**.

| Permission | Why Nethor needs it |
|---|---|
| **View Channels** | To see the channel at all. |
| **Send Messages** | To post in the channel (e.g. a ⚠️ when it can't open a thread). |
| **Send Messages in Threads** | Forum posts and mention threads are threads; **Send Messages** alone doesn't work there. |
| **Create Public Threads** | An @mention opens a thread on that message; without it Nethor posts `⚠️ … Create Public Threads …` in the channel instead. |
| **Manage Threads** | Only for `/delete`; without it the thread stays and the reply says so. |
| **Read Message History** | To read the messages it answers and the thread it's in. |

- **Private channel** (full Nethor, approvals in the thread): **Edit Channel → Permissions** → remove **View Channel** from `@everyone`, add yourself and the bot's role with **View Channel**, **Send Messages**, **Send Messages in Threads**, **Create Public Threads** and **Read Message History**. Anyone else with access, admins included, makes it public; bots don't count.
- Channels only answer in servers listed in `ALLOWED_GUILD_IDS`; forum posts in `FORUM_CHANNEL_IDS` always stay private conversations. The full rules are in [How it works](#how-it-works).

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
