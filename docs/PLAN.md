# Orchestrator Desktop App: Plan

Status: plan only, no code yet. Date: 2026-11-18.

## 1. What it is

A local desktop app that drives **Claude Code** and **Codex** from one window. It feels like the Claude and Codex apps (projects on the left, chats in the middle), and adds three things neither app gives you on its own:

1. Both agents side by side, with a model and reasoning-effort picker per chat.
2. Scheduled prompts that run in the background from the system tray.
3. An **orchestrator**: a main agent that splits a task, hands pieces to Claude and Codex sub-sessions, watches them, collects their results, and reuses old sessions when they already have the right context.

Out of scope: web UI, port forwarding, Tailscale, cloud sync. Regular claude.ai chats and Claude cloud Projects are not reachable from local tools, so they are not part of this app.

## 2. Tech stack

| Piece | Choice | Why |
|---|---|---|
| Language | Python 3.12+ | Both engines have good Python paths |
| UI | PySide6 (Qt 6) | Native desktop, tray support, mature |
| Look | PyQt-Fluent-Widgets (PySide6 build), dark/light | Modern Windows 11 style out of the box. Fallback: qdarktheme |
| Async | qasync (asyncio inside the Qt event loop) | Stream many sessions without freezing UI |
| Claude engine | Claude Agent SDK for Python (`claude-agent-sdk`) | Streams messages, supports resume, model, cwd, permissions, hooks |
| Codex engine | `codex exec --json` subprocess (JSONL events); `codex app-server` as upgrade path | Stable, scriptable, supports resume |
| Storage | SQLite (via `sqlite3` or SQLModel) + app folder | Single file, no server |
| Scheduler | APScheduler (AsyncIOScheduler, SQLite job store) | Cron and one-shot jobs that survive restarts |
| Secrets | `keyring` (Windows Credential Manager / macOS Keychain) | Tokens never in plain files |
| Packaging | PyInstaller or Nuitka | One installer per OS |

## 3. Architecture

```
+------------------------- Qt UI (PySide6 + Fluent) --------------------------+
|  Sidebar (projects/chats) | Chat view | Orchestrator board | Schedules | Settings |
+-------------------------------------+---------------------------------------+
                                      | Qt signals (events in, commands out)
+-------------------------------------v---------------------------------------+
|                               Core (asyncio)                                |
|  SessionManager   Orchestrator   Scheduler   SessionIndexer   AuthManager   |
+--------+-----------------+-------------------------+------------------------+
         |                 |                         |
   +-----v-----+     +-----v------+          +-------v--------+
   | Claude    |     | Codex      |          | SQLite + files |
   | adapter   |     | adapter    |          | (app data)     |
   | Agent SDK |     | codex exec |          +----------------+
   +-----------+     +------------+
         |                 |
  ~/.claude/projects   ~/.codex/sessions   (read for import + resume)
```

**Key idea: one `AgentAdapter` interface.** Both engines are wrapped so the rest of the app never cares which is which:

```
start(prompt, cwd, model, effort, resume_id=None) -> stream of Events
send(text)          # follow-up turn in same session
interrupt()
Events: Started(session_id) | Text | ToolCall | ToolResult | NeedsApproval | Usage | Done(result) | Error
```

- `SessionManager` owns all running sessions, their state (running / done / failed / waiting on user), and pipes events to the UI.
- `SessionIndexer` scans `~/.claude/projects/**/*.jsonl` and `~/.codex/sessions/**` and keeps an index (title, cwd, last time, engine, summary) in SQLite. Watches folders for changes.
- `Orchestrator` is itself an agent session on **either engine** (Claude or Codex, or Auto), with extra tools served by a local MCP server (see section 6).
- `Scheduler` fires saved prompts into `SessionManager`.
- `AuthManager` loads tokens from keyring into each subprocess's environment.

## 4. Screens

**Main window (three panes)**

```
+--------------+-------------------------------------------+-----------------+
| PROJECTS     |  Chat title           [Claude v][Opus..v][High v]  [...]  |
|  > Silo      |-------------------------------------------+  RIGHT PANEL    |
|    chat A  * |  messages, streaming text,                 |  (toggle)       |
|    chat B    |  collapsible tool calls, diffs,           |  - Sub-agents   |
|  > Website   |  approval prompts inline                  |  - Files touched|
|              |                                           |  - Usage        |
| + New chat   |-------------------------------------------+                 |
| Schedules    |  [ prompt box ................. ] [Send]  |                 |
| Settings     |                                           |                 |
+--------------+-------------------------------------------+-----------------+
```

- **Sidebar:** projects (a project = a folder on disk), chats under each, with engine icon and a status dot. Search. Imported old sessions show here too.
- **Chat view:** streaming output, markdown, code blocks, collapsible tool calls, inline "Allow / Deny" for permission requests, stop button.
- **Model picker:** engine (Claude / Codex), model, effort. Shown as one label like "Opus - high" or "GPT 6.2 Sol - medium". Model list is editable in settings, so new models need no code change. Picker also has an **Auto** option, where the orchestrator chooses model and effort per task (see 6.5).
- **Orchestrator board:** one card per sub-agent: engine, model, task, status, elapsed time, last line of output, buttons (open chat, stop, retry). Cards update live.
- **Schedules:** list of scheduled prompts with next run time, last result, enable toggle.
- **Settings:** accounts and tokens, default models, model list, permission defaults, theme, start with OS / tray behavior.
- **Tray:** icon with count of running jobs, notifications when a job finishes, fails, or needs you. Closing the window keeps the app running in tray.

## 5. Data storage

App folder: `%APPDATA%/Orchestrator` (Windows) or `~/Library/Application Support/Orchestrator` (Mac) or `~/.local/share/orchestrator` (Linux).

SQLite tables (first cut):

- `projects(id, name, path, created_at)`
- `sessions(id, project_id, engine, engine_session_id, title, model, effort, status, summary, summary_updated_at, source[app|imported], last_active)`
- `messages(id, session_id, role, kind, content_json, ts)`: app's own copy for fast display; the engine's own log stays the source of truth for resume
- `orchestrations(id, project_id, goal, status, created_at)`
- `tasks(id, orchestration_id, session_id, engine, model, effort, pick_reason, auto_picked[bool], instructions, status, result, reused[bool], created_at, finished_at)`
- `schedules(id, project_id, prompt, engine, model, effort, cron, new_or_resume, session_id, enabled)`
- `runs(id, schedule_id, session_id, status, started_at, finished_at)`
- `models(engine, id, label, efforts_json)`

Tokens: keyring only. Nothing secret in SQLite.

## 6. Features in detail

### 6.1 Chats with Claude and Codex
- Claude: Agent SDK `ClaudeSDKClient` with options for `model`, `cwd`, `resume`, `permission_mode`, allowed tools. Effort maps to Claude's thinking/effort setting.
- Codex: `codex exec --json -m <model> -c model_reasoning_effort=<low|medium|high> -C <cwd> "<prompt>"`, follow-ups via `codex exec resume <id> --json "<text>"`. Parse JSONL events into the common Event types.
- Permissions: default to "ask", shown inline. Per-project option to auto-allow edits. Codex sandbox mode set per project.

### 6.2 Import and resume old sessions
- Index both folders on startup and on change. Show them under the matching project (match by `cwd`).
- Opening one renders history from the jsonl. Typing continues it via resume (SDK `resume=` / `codex exec resume`).
- Read only. Never edit the engines' own files.

### 6.3 Auth that does not log out
- Claude: run `claude setup-token` once, store result in keyring, pass as `CLAUDE_CODE_OAUTH_TOKEN` to each Claude session. Fallback: `ANTHROPIC_API_KEY`.
- Codex: use the existing `codex login` (ChatGPT sign-in, refresh token in `~/.codex/auth.json`, which Codex refreshes itself). Fallback: `OPENAI_API_KEY`.
- Settings page shows each account as "OK / expired / missing" with a "Fix" button that runs the right login command in a terminal.

### 6.4 Scheduled prompts
- Cron or one-shot ("every weekday 8 AM", "tomorrow 3 PM"). Each can start a new chat or continue a chosen one.
- Runs from the tray even when the window is closed. App can start at login.
- Missed runs (PC asleep): run once on wake, configurable.
- Result lands as a normal chat plus a notification.

### 6.5 Orchestrator
The main agent can run on **Claude or Codex**, any model and effort, or **Auto** (it picks for itself). Its custom tools live in a small **local MCP server** (stdio, started by the app) that talks to the app core, so both engines get the same tools: Claude via the Agent SDK `mcp_servers` option, Codex via its `mcp_servers` config (`-c` overrides or a generated config). The tools:

| Tool | Does |
|---|---|
| `find_sessions(query, project)` | Searches session summaries, returns best candidates with why |
| `delegate(engine, model, effort, instructions, resume_session_id?)` | Starts or resumes a sub-session, returns a task id right away |
| `task_status(task_id?)` | Status of one or all tasks |
| `wait_for(task_ids, timeout)` | Blocks until done, returns results |
| `send_to_task(task_id, text)` | Follow-up to a running or finished sub-agent |
| `ask_user(question)` | Shows a question in the UI, returns answer |

Flow:
1. User gives a goal. Orchestrator plans and calls `find_sessions` first.
2. If an old session fits, it proposes reuse. The UI shows a **reuse card**: "Will continue *Fix login bug* (Codex, 2 days ago). [Use it] [Start fresh] [Pick another]". Default accepts after a short countdown, or waits if the user set "always ask".
3. It delegates tasks; they run in parallel. Board shows live status.
4. Each sub-agent ends with a short structured report (done / not done, what changed, files, open questions). The adapter adds a closing instruction so reports are consistent.
5. Results go back to the orchestrator as tool results; it decides next steps or summarizes to the user.
6. If a sub-agent needs approval or asks a question, its card turns "waiting on you" and the tray notifies.

**Session summaries** (powers reuse): after each session goes idle, a cheap model (e.g. Haiku) writes a 3 to 5 line summary plus keywords and stores it. `find_sessions` ranks by keyword match + project + recency (later: embeddings, if needed).

**Auto model and effort choice.** In `delegate`, engine, model and effort are optional. If left out, the orchestrator picks them itself based on the task:
- A **routing guide** in settings (editable) tells it what each model is good at and costs, e.g. "small edits, renames: Sonnet low", "hard debugging, architecture: Opus high", "large refactors, long autonomous coding: Codex high", "quick lookups, summaries: Haiku".
- It weighs task difficulty, size, whether the session is being reused (reuse keeps that session's engine), and current usage left on each account (prefers the engine with more headroom when both fit).
- Every choice comes with a one-line reason on the sub-agent card ("Opus high: multi-file bug with unclear cause"). User can change it on the card before it starts or set the project to "always ask".
- If a cheap pick fails or gets stuck, the orchestrator may retry once at a higher effort or stronger model, and says so.
- The orchestrator's own engine, model and effort can also be "Auto": a short first pass on a light model reads the goal and picks the engine and strength for the planner (e.g. Codex high for a big coding job, Opus high for open-ended design). The pick and reason are shown at the top of the board, and the user can change it.
- Picks and outcomes are logged in `tasks` so the routing guide can be tuned later.

Limits: max parallel sub-agents (default 4), per-task timeout, and a "stop all" button.

## 7. Build order

Each milestone ends with something usable.

**M0. Spike (1 to 2 days).** Two tiny scripts: stream one Claude turn via Agent SDK, stream one Codex turn via `codex exec --json`, both with resume. Confirm current flags, event shapes, and token env vars. Output: notes that pin the adapter design.

**M1. MVP chat app.** Window, Fluent theme, projects = folders, new chat with Claude or Codex, streaming output, model + effort picker, stop button, SQLite storage, keyring auth. Single chat at a time is fine.

**M2. Import and resume.** Session indexer for both folders, sidebar shows old sessions, open and continue them. Multiple chats running at once with status dots.

**M3. Tray and schedules.** System tray, notifications, APScheduler with persisted jobs, schedule editor, run history, start at login.

**M4. Orchestrator v1.** Local MCP server for orchestrator tools, tested from both Claude and Codex. Orchestrator session (engine chosen by user) with `delegate`, `task_status`, `wait_for`. Board view with live cards. Structured reports back to orchestrator. No reuse yet.

**M5. Orchestrator v2: reuse and auto routing.** Session summaries, `find_sessions`, reuse card with override, `send_to_task`, `ask_user`. Auto model and effort choice with routing guide, reason shown on each card, escalate-on-failure.

**M6. Polish.** Diff viewer, usage meter per session, keyboard shortcuts, search across chats, installer builds.

## 8. Risks and open questions

- **CLI / SDK churn.** Flags and JSON event formats change between versions. Mitigation: all parsing lives in the two adapters; M0 pins versions; show engine version in settings.
- **`setup-token` scope.** Confirm the long-lived token works for the Agent SDK in the current version and how long it lasts.
- **Codex Python path.** `codex exec --json` is simplest. If approvals mid-run are needed for Codex, `codex app-server` (JSON-RPC over stdio) may be required. Decide in M0.
- **Usage limits.** Parallel sub-agents burn subscription limits fast. Show usage and cap parallel tasks.
- **Permissions on unattended runs.** Scheduled and orchestrated jobs cannot wait for a click forever. Need a clear per-project policy (read only / allow edits / ask and pause).
- **Two writers in one repo.** Claude and Codex editing the same folder at once can collide. Option: give each sub-agent its own git worktree, merge at the end. Decide in M4.
- **Session file formats are internal.** Reading `~/.claude/projects` and `~/.codex/sessions` is not an official API. Keep the parser tolerant and read only.
- **Decided:** Windows only for now (app folder `%APPDATA%/Orchestrator`, Windows Credential Manager, PyInstaller .exe). Keep paths via `pathlib` so Mac/Linux stay possible later.
- **Decided:** Orchestrator is not locked to Claude. It runs on Claude or Codex, picked by the user or by Auto. Needs M0/M4 check that Codex calls MCP tools reliably and handles long `wait_for` calls (tool timeouts).
