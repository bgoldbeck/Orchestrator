# M0 Notes — engine spikes (2026-11-17)

Goal: prove Claude Code and Codex can be driven from Python with streaming + resume. No UI.

## Versions

| Tool | Version |
|---|---|
| Python | 3.13.14 (Store build on PATH; 3.12 also installed at `%LOCALAPPDATA%\Programs\Python\Python312`) |
| venv | `.venv` built from Python 3.13.14 |
| claude (installed CLI) | 2.1.263 (`C:\Users\bgold\.local\bin\claude.exe`) |
| claude-agent-sdk | 0.2.184 (bundles its own CLI: `_bundled/claude.exe` 2.1.327) |
| codex | codex-cli 0.166.0 (npm shim `%APPDATA%\npm\codex.cmd`) |
| git | 2.55.0.windows.1 |

## Status

| Spike | Turn 1 | Resume | Result |
|---|---|---|---|
| `spikes/spike_codex.py` | PING | PING, same thread id | **OK** |
| `spikes/spike_claude.py` | `Not logged in · Please run /login` | — | **BLOCKED on auth** |

## Claude Code (claude_agent_sdk)

### API (inspected from installed package, not guessed)

- `query(*, prompt: str | AsyncIterable[dict], options: ClaudeAgentOptions | None, transport=None)` → async iterator of
  `UserMessage | AssistantMessage | SystemMessage | ResultMessage | StreamEvent | RateLimitEvent | ConversationResetMessage`.
- `ClaudeSDKClient` also exported (long-lived bidirectional client; use for the real app when we need interrupts / multi-turn in one process).
- Relevant `ClaudeAgentOptions` fields: `cwd`, `model`, `resume`, `session_id`, `continue_conversation`, `fork_session`,
  `max_turns`, `allowed_tools`, `disallowed_tools`, `permission_mode` (`default|acceptEdits|plan|bypassPermissions|dontAsk|auto`),
  `include_partial_messages` (token-level `StreamEvent`), `setting_sources` (`[]` = ignore user/project settings),
  `effort`, `thinking`, `cli_path`, `env`, `stderr` callback, `can_use_tool`, `hooks`, `max_budget_usd`.
- Session helpers exported: `list_sessions`, `get_session_info`, `get_session_messages`, `rename_session`, `tag_session`,
  `fork_session`, `delete_session`, `project_key_for_directory` — useful for the history sidebar instead of parsing JSONL ourselves.

### Spike call

```python
opts = ClaudeAgentOptions(cwd=str(SANDBOX), model="haiku", resume=None, max_turns=1,
                          allowed_tools=[], setting_sources=[], include_partial_messages=True)
async for msg in query(prompt="Reply with the word PING and nothing else.", options=opts): ...
# resume: same options with resume=<session_id from SystemMessage(init).data["session_id"] or ResultMessage.session_id>
```

Expected stream (per SDK types; not yet observed live because of auth):
`SystemMessage(subtype="init", data.session_id)` → `StreamEvent` (`event.type`: message_start / content_block_start / content_block_delta / content_block_stop / message_delta / message_stop) → `AssistantMessage(content=[TextBlock...])` → `ResultMessage(session_id, subtype, is_error, num_turns, total_cost_usd, duration_ms)`.

### What broke

- Both the installed `claude` CLI and the SDK-bundled CLI return `Not logged in · Please run /login` when run from a shell on this machine.
- `~/.claude/.credentials.json` exists but contains only `mcpOAuth` entries — no `claudeAiOauth` login.
- The Claude Code I'm running in (desktop app) authenticates through host-injected env (`ANTHROPIC_BASE_URL`, `CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH`, etc.), which child processes can't use as a standalone login.
- **Fix (owner action):** in a normal terminal run `claude` then `/login` (subscription), **or** `claude setup-token` and set `CLAUDE_CODE_OAUTH_TOKEN`, **or** set `ANTHROPIC_API_KEY`. Then rerun `.venv\Scripts\python spikes\spike_claude.py` from a plain terminal (not from inside a Claude session, which leaks `CLAUDECODE` / `CLAUDE_CODE_*` / `ANTHROPIC_BASE_URL` into the child).
- For the app: decide whether to pass `cli_path` to the user's installed `claude.exe` (shares its login/settings, version tracks user updates) or rely on the bundled one (pinned version). Both read the same `~/.claude` credentials.

## Codex (`codex exec --json`)

### Working commands

```
# turn 1 (prompt on stdin via "-", avoids .cmd quoting on Windows)
codex exec --json --skip-git-repo-check -s read-only -C <sandbox> -c model_reasoning_effort="low" -
# resume (no -C / -s on resume; cwd = subprocess cwd)
codex exec resume --json --skip-git-repo-check -c model_reasoning_effort="low" <thread_id> -
```

- Model: `-m <MODEL>` (left default in spike). Reasoning effort: `-c model_reasoning_effort="low|medium|high"`.
- Sandbox: `-s read-only|workspace-write|danger-full-access`; `--approve-for-me` routes approvals to auto-review.
- Other useful: `-o <file>` (last message), `--output-schema <file>`, `--ephemeral` (no session file), `--add-dir`, `codex exec fork`.
- From Python on Windows: `shutil.which("codex")` → `codex.CMD`; `subprocess.Popen([...], text=True, encoding="utf-8")`, read stdout line by line.

### JSONL events seen

| `type` | keys | notes |
|---|---|---|
| `thread.started` | `thread_id`, `type` | thread id = resume id; same id on resume |
| `turn.started` | `type` | |
| `item.completed` | `item`, `type` | `item.type`: `agent_message` (`text`), `error` (`message`) |
| `turn.completed` | `type`, `usage` | usage: `input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`, `reasoning_output_tokens` |

- No `item.started` / delta events for a plain text reply — `exec --json` gives whole items, not token streaming. Tool calls (command_execution, file_change, etc.) not exercised yet.
- Two `item.type=error` items each run are config warnings (`features.rmcp_client` in `~/.codex/config.toml` is ignored) — non-fatal; app should show these as warnings, not failures.
- Exit 0 on success; stderr has a few log lines.

## Auth findings (no values printed)

| Check | Result |
|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | unset |
| `ANTHROPIC_API_KEY` | unset |
| `~/.claude/.credentials.json` | exists, **no Claude login** (MCP OAuth only) |
| `OPENAI_API_KEY` / `CODEX_HOME` | unset |
| `~/.codex/auth.json` | exists (Codex works) |

## Session file formats (keys only, no content)

### Claude: `%USERPROFILE%\.claude\projects\<project-key>\<session-uuid>.jsonl`

- Project key = cwd with `:` `\` → `-` (e.g. `D:\PrintPantry` → `D--PrintPantry`); SDK has `project_key_for_directory()`.
- Filename = session id. Every line has `type` + `sessionId`.
- Line types: `user`, `assistant`, `attachment`, `queue-operation`, `last-prompt`, `api-request`, `api-request-blob`, `api-request-shape`, `atis-latch`, `tools-used`, `custom-title`, `agent-name`.
- `user` / `assistant` carry `uuid`, `parentUuid` (tree), `cwd`, `gitBranch`, `timestamp`, `version`, `entrypoint`, `isSidechain`, `message`.
- Title: `{"type":"custom-title","customTitle":...,"sessionId":...}` lines (last one wins); `agent-name` lines also present.

### Codex: `%USERPROFILE%\.codex\sessions\YYYY\MM\DD\rollout-<ISO-ts>-<uuid>.jsonl`

- Every line: `{type, timestamp, ordinal, payload}` (`response_item` also has `metadata`).
- Line types: `session_meta`, `turn_context`, `event_msg`, `response_item`, `world_state`, `token_usage_record`.
- `session_meta.payload`: `id`, `session_id`, `cwd`, `cli_version`, `model_provider`, `originator`, `source`, `thread_source`, `git`, `timestamp`, ...
- `turn_context.payload`: `cwd`, `model`, `effort`, `sandbox_policy`, `approval_policy`, `turn_id`, ...
- `event_msg.payload.type`: `task_started`, `task_complete`, `item_completed`, `token_count`, `thread_settings_applied`.
- `response_item.payload.type`: `message`, `additional_tools`.
- Title: not in rollout file; `~/.codex/session_index.jsonl` lines `{id, thread_name, updated_at}`.

## Recommendation: `codex exec --json` vs `codex app-server`

- `exec --json` is enough for M1: simple, stateless per turn, resume by thread id works, easy to parse.
- Limits: no token deltas, no mid-turn approvals (must pre-pick sandbox / `--approve-for-me`), one process per turn, interrupt = kill process.
- Plan: build the engine adapter behind an interface using `exec --json` now; spike `codex app-server` (JSON-RPC over stdio, long-lived, streaming deltas + approval requests) in M1/M2 when the UI needs live streaming and approvals. Same for Claude: `query()` for one-shot, `ClaudeSDKClient` for interactive sessions.
