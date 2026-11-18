"""Owns running turns, persists messages, and relays events to the UI via Qt signals."""
from __future__ import annotations

import asyncio
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from orchestrator.core import auth
from orchestrator.core.events import (
    Done,
    Error,
    NeedsApproval,
    Started,
    Text,
    ToolCall,
    ToolResult,
    Usage,
)
from orchestrator.engines.base import AgentAdapter, TurnRequest
from orchestrator.engines.claude import ClaudeAdapter
from orchestrator.engines.codex import CodexAdapter
from orchestrator.storage.db import Database


def make_title(prompt: str) -> str:
    t = " ".join(prompt.split())
    return t if len(t) <= 48 else t[:47].rstrip() + "…"


class SessionManager(QObject):
    event = Signal(int, object)  # session id, Event
    status_changed = Signal(int, str)  # idle | running | waiting | done | error
    session_updated = Signal(int)
    approval_requested = Signal(int, object, object)  # session id, NeedsApproval, asyncio.Future

    def __init__(self, db: Database, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.db = db
        self._tasks: dict[int, asyncio.Task] = {}
        self._adapters: dict[int, AgentAdapter] = {}

    def is_running(self, sid: int) -> bool:
        t = self._tasks.get(sid)
        return bool(t and not t.done())

    def _adapter(self, sid: int, engine: str) -> AgentAdapter:
        async def approve(req: NeedsApproval) -> bool:
            fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
            self._set_status(sid, "waiting")
            self.approval_requested.emit(sid, req, fut)
            try:
                return await fut
            finally:
                self._set_status(sid, "running")

        if engine == "claude":
            return ClaudeAdapter(approval_handler=approve)
        return CodexAdapter(approval_handler=approve, env=auth.codex_status().env)

    def _set_status(self, sid: int, status: str) -> None:
        self.db.update_session(sid, status=status)
        self.status_changed.emit(sid, status)

    def send(self, sid: int, text: str) -> None:
        if self.is_running(sid):
            return
        s = self.db.session(sid)
        if not s:
            return
        if not self.db.messages(sid):
            self.db.update_session(sid, title=make_title(text))
            self.session_updated.emit(sid)
        self.db.add_message(sid, "user", "text", {"text": text})
        self._tasks[sid] = asyncio.ensure_future(self._run(sid, text))

    async def stop(self, sid: int) -> None:
        a = self._adapters.get(sid)
        if a:
            await a.interrupt()

    async def _run(self, sid: int, prompt: str) -> None:
        s = self.db.session(sid)
        proj = self.db.project(s.project_id) if s else None
        if not s or not proj:
            return
        adapter = self._adapter(sid, s.engine)
        self._adapters[sid] = adapter
        self._set_status(sid, "running")
        req = TurnRequest(prompt=prompt, cwd=Path(proj.path), model=s.model or "", effort=s.effort or "",
                          resume_id=s.engine_session_id,
                          sandbox=self.db.get_setting("codex.sandbox", "workspace-write"))
        buf: list[str] = []
        pending_tools: dict[str, ToolCall] = {}
        final_status = "done"
        seen_warnings = {m["text"] for m in self.db.messages(sid) if m["kind"] == "error" and not m.get("fatal", True)}

        def flush_text() -> None:
            text = "".join(buf).strip()
            buf.clear()
            if text:
                self.db.add_message(sid, "assistant", "text", {"text": text})

        try:
            async for ev in adapter.run(req):
                if isinstance(ev, Started):
                    if ev.session_id and ev.session_id != s.engine_session_id:
                        self.db.update_session(sid, engine_session_id=ev.session_id)
                        s.engine_session_id = ev.session_id
                elif isinstance(ev, Text):
                    if ev.delta:
                        buf.append(ev.text)
                    else:
                        buf[:] = [ev.text]
                elif isinstance(ev, ToolCall):
                    flush_text()
                    pending_tools[ev.id] = ev
                elif isinstance(ev, ToolResult):
                    call = pending_tools.pop(ev.id, None)
                    self.db.add_message(sid, "tool", "tool", {
                        "id": ev.id, "name": (call.name if call else ev.name), "summary": call.summary if call else "",
                        "output": ev.output[-20000:], "is_error": ev.is_error})
                elif isinstance(ev, Error):
                    if not ev.fatal:
                        if ev.message in seen_warnings:
                            continue  # same config warning every turn: show once per chat
                        seen_warnings.add(ev.message)
                    flush_text()
                    self.db.add_message(sid, "system", "error", {"text": ev.message, "fatal": ev.fatal, "level": ev.kind})
                    if ev.fatal:
                        final_status = "error"
                elif isinstance(ev, Usage):
                    self.db.add_message(sid, "system", "usage", {
                        "input": ev.input_tokens, "output": ev.output_tokens, "cached": ev.cached_tokens, "cost": ev.cost_usd})
                elif isinstance(ev, Done):
                    flush_text()
                    if ev.is_error and final_status != "error":
                        final_status = "idle" if ev.result == "stopped" else "error"
                self.event.emit(sid, ev)
        except Exception as exc:  # never let a turn crash the app
            flush_text()
            msg = f"{type(exc).__name__}: {exc}"
            self.db.add_message(sid, "system", "error", {"text": msg, "fatal": True, "level": "error"})
            self.event.emit(sid, Error(msg))
            self.event.emit(sid, Done(is_error=True))
            final_status = "error"
        finally:
            flush_text()
            for call in pending_tools.values():
                self.db.add_message(sid, "tool", "tool", {"id": call.id, "name": call.name, "summary": call.summary,
                                                          "output": "", "is_error": True})
            self._adapters.pop(sid, None)
            self._set_status(sid, final_status)
