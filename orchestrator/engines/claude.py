"""Claude engine: drives Claude Code through claude-agent-sdk (ClaudeSDKClient).

The SDK runs on its own asyncio loop in a worker thread (Proactor loop, needed
for subprocesses on Windows); events are bridged back to the UI loop.
"""
from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from orchestrator.core import auth
from orchestrator.core.events import (
    Done,
    Error,
    Event,
    NeedsApproval,
    Started,
    Text,
    ToolCall,
    ToolResult,
    Usage,
)
from orchestrator.engines._threadbridge import ThreadBridge
from orchestrator.engines.base import AgentAdapter, TurnRequest

NOT_LOGGED_IN = (
    "Claude is not logged in. Run `claude setup-token` in a terminal, then paste the "
    "token in Settings › Accounts (or set CLAUDE_CODE_OAUTH_TOKEN)."
)
EFFORTS = {"low", "medium", "high", "xhigh", "max"}


def _short(text: str, n: int = 90) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def tool_summary(name: str, inp: dict[str, Any]) -> str:
    for key in ("command", "file_path", "path", "pattern", "url", "query", "description", "prompt"):
        if key in inp and inp[key]:
            return _short(inp[key])
    return _short(json.dumps(inp)) if inp else ""


def _result_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for c in content:
        if isinstance(c, dict):
            parts.append(c.get("text") or json.dumps(c))
        else:
            parts.append(str(c))
    return "\n".join(parts)


def _is_auth_error(text: str) -> bool:
    t = text.lower()
    return "not logged in" in t or "/login" in t or "invalid api key" in t or "authentication" in t


@dataclass
class ClaudeMapState:
    session_id: str | None = None
    started: bool = False
    tool_names: dict[str, str] = field(default_factory=dict)


def map_claude_message(msg: Any, st: ClaudeMapState) -> list[Event]:
    """Translate one claude_agent_sdk message into events (pure, testable)."""
    from claude_agent_sdk import (
        AssistantMessage,
        ResultMessage,
        StreamEvent,
        SystemMessage,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
    )

    out: list[Event] = []

    def ensure_started(sid: str | None) -> None:
        if sid and not st.started:
            st.started = True
            st.session_id = sid
            out.append(Started(session_id=sid))

    if isinstance(msg, SystemMessage):
        ensure_started(msg.data.get("session_id"))
    elif isinstance(msg, StreamEvent):
        ensure_started(msg.session_id)
        if msg.parent_tool_use_id:  # sub-agent chatter: skip in M1
            return out
        ev = msg.event or {}
        if ev.get("type") == "content_block_delta":
            d = ev.get("delta") or {}
            if d.get("type") == "text_delta" and d.get("text"):
                out.append(Text(text=d["text"], delta=True))
    elif isinstance(msg, AssistantMessage):
        ensure_started(msg.session_id)
        if msg.error == "authentication_failed":
            out.append(Error(NOT_LOGGED_IN, kind="auth"))
            return out
        if msg.parent_tool_use_id:
            return out
        for b in msg.content:
            if isinstance(b, TextBlock) and b.text:
                out.append(Text(text=b.text, delta=False))
            elif isinstance(b, ToolUseBlock):
                st.tool_names[b.id] = b.name
                out.append(ToolCall(id=b.id, name=b.name, summary=tool_summary(b.name, b.input), input=b.input))
    elif isinstance(msg, UserMessage):
        if isinstance(msg.content, list) and not msg.parent_tool_use_id:
            for b in msg.content:
                if isinstance(b, ToolResultBlock):
                    out.append(ToolResult(id=b.tool_use_id, output=_result_text(b.content),
                                          is_error=bool(b.is_error), name=st.tool_names.get(b.tool_use_id, "")))
    elif isinstance(msg, ResultMessage):
        ensure_started(msg.session_id)
        u = msg.usage or {}
        out.append(Usage(
            input_tokens=int(u.get("input_tokens", 0)) + int(u.get("cache_creation_input_tokens", 0)),
            output_tokens=int(u.get("output_tokens", 0)),
            cached_tokens=int(u.get("cache_read_input_tokens", 0)),
            cost_usd=msg.total_cost_usd,
        ))
        if msg.is_error:
            text = msg.result or "; ".join(msg.errors or []) or msg.subtype
            out.append(Error(NOT_LOGGED_IN if _is_auth_error(text) else text,
                             kind="auth" if _is_auth_error(text) else "error"))
        out.append(Done(is_error=msg.is_error, result=msg.result or ""))
    return out


def _child_env(token_env: dict[str, str]) -> dict[str, str]:
    return dict(token_env)


class ClaudeAdapter(AgentAdapter):
    engine = "claude"
    label = "Claude"

    def __init__(self, approval_handler=None, permission_mode: str = "default") -> None:
        super().__init__(approval_handler)
        self.permission_mode = permission_mode
        self._thread_loop: asyncio.AbstractEventLoop | None = None
        self._client = None
        self._stopped = False

    async def run(self, req: TurnRequest) -> AsyncIterator[Event]:
        status = auth.claude_status()
        if not status.ok:
            yield Error(NOT_LOGGED_IN, kind="auth")
            yield Done(is_error=True)
            return
        try:
            import claude_agent_sdk as sdk

            from orchestrator.engines.winproc import install_no_window_spawn

            install_no_window_spawn()  # claude.exe + its MCP servers: no console windows under pythonw
        except ImportError:
            yield Error("claude-agent-sdk is not installed.")
            yield Done(is_error=True)
            return

        ui_loop = asyncio.get_running_loop()
        handler = self.approval_handler
        bridge = ThreadBridge()
        self._stopped = False

        async def can_use_tool(tool_name: str, tool_input: dict, _ctx):
            if handler is None:
                return sdk.PermissionResultAllow()
            req_ev = NeedsApproval(id=f"perm-{id(tool_input)}", tool=tool_name,
                                   detail=tool_summary(tool_name, tool_input))
            fut = asyncio.run_coroutine_threadsafe(handler(req_ev), ui_loop)
            allowed = await asyncio.wrap_future(fut)
            if allowed:
                return sdk.PermissionResultAllow()
            return sdk.PermissionResultDeny(message="The user denied this action.")

        options = sdk.ClaudeAgentOptions(
            cwd=str(req.cwd),
            model=req.model or None,
            resume=req.resume_id,
            effort=req.effort if req.effort in EFFORTS else None,
            include_partial_messages=True,
            permission_mode=self.permission_mode,
            can_use_tool=can_use_tool,
            env=_child_env(status.env),
            stderr=lambda _line: None,
        )

        def produce(b: ThreadBridge) -> None:
            async def main() -> None:
                self._thread_loop = asyncio.get_running_loop()
                st = ClaudeMapState()
                async with sdk.ClaudeSDKClient(options=options) as client:
                    self._client = client
                    await client.query(req.prompt)
                    async for m in client.receive_response():
                        for ev in map_claude_message(m, st):
                            b.put(ev)

            if sys.platform == "win32":
                asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(main())
            finally:
                self._client = None
                self._thread_loop = None
                loop.close()

        bridge.start(produce)
        saw_done = False
        try:
            async for ev in bridge.items():
                if isinstance(ev, Done):
                    saw_done = True
                yield ev
        except Exception as exc:  # SDK errors (ResultError, ProcessError, CLINotFound...)
            text = str(exc)
            if self._stopped:
                yield Error("Stopped.", fatal=False, kind="warning")
            elif _is_auth_error(text):
                yield Error(NOT_LOGGED_IN, kind="auth")
            elif type(exc).__name__ == "CLINotFoundError":
                yield Error("Claude Code CLI not found.", kind="auth")
            else:
                yield Error(_short(text, 400))
            if not saw_done:
                yield Done(is_error=True)
            return
        if not saw_done:
            yield Done(is_error=self._stopped, result="stopped" if self._stopped else "")

    async def interrupt(self) -> None:
        loop, client = self._thread_loop, self._client
        if loop and client:
            self._stopped = True
            fut = asyncio.run_coroutine_threadsafe(client.interrupt(), loop)
            try:
                await asyncio.wait_for(asyncio.wrap_future(fut), timeout=5)
            except Exception:
                pass
