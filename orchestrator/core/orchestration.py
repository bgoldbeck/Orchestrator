"""Orchestrator v1: an orchestrator chat (Claude or Codex) runs sub-agents through MCP tools.

The tools live in `orchestrator.mcp_server`, a stdio MCP server the engine starts.
It forwards each call over a local TCP bridge (127.0.0.1, per-run secret token)
to OrchestrationManager, which owns the tasks, runs sub-agent chats through the
SessionManager and returns their structured reports.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import re
import secrets
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal

from orchestrator.core import model_registry
from orchestrator.core.catalog import ENGINES
from orchestrator.storage.db import Database, Orchestration, Task, app_dir

APP_ROOT = Path(__file__).resolve().parents[2]
WAIT_LIMIT = 240  # seconds one wait_for call may block (kept under the tool timeout below)
TOOL_TIMEOUT = 600
ACTIVE = ("queued", "running", "waiting")
FINISHED = ("done", "failed", "stopped")
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

CLOSING = """

When you are finished, end your final message with this report block, filled in:

```report
status: done | partial | failed
summary: <one to three sentences on what you did and the result>
files_changed: <comma-separated paths, or none>
open_questions: <questions or problems for the orchestrator, or none>
```"""

SUBAGENT_INTRO = ("You are a sub-agent started by an orchestrator. Work only on the task below, in the "
                  "current folder. Nobody can answer questions while you work: make reasonable choices and "
                  "list anything unclear under open_questions.\n\nTask:\n")

ORCH_PROMPT = """You are the orchestrator for the project folder {path}.

Goal from the user:
{goal}

You coordinate sub-agents with the `orchestrator` MCP tools:
- delegate(instructions, engine, model, effort, title, cwd) starts a sub-agent and returns a task_id at once.
- wait_for(task_ids) waits for tasks and returns their reports. If it says tasks are still pending, call it again.
- task_status(task_id) checks without waiting. send_to_task(task_id, text) sends a follow-up to a finished sub-agent.

How to work:
1. Look at the project briefly if you need to, then split the goal into a few clear tasks that can run in parallel.
2. Delegate them. A sub-agent starts with no context, so give complete instructions (which files, what result).
   Give each a short title. Up to {max_parallel} run at once; the rest wait in a queue.
3. Call wait_for on the task ids. Read each report. If something failed or is missing, send a follow-up or delegate a fix.
4. Do not edit files yourself. Sub-agents do the work.
5. Finish with a short summary for the user: what each task did, files changed, and open questions.

Engines and models you may use (only these ids):
{models}
If you leave engine and model empty: {engine} / {model}."""


# ---------------------------------------------------------------- reports
_REPORT_BLOCK = re.compile(r"```report\s*\n(.*?)```", re.S | re.I)
_FIELD = re.compile(r"^\s*(status|summary|files_changed|open_questions)\s*:\s*(.*)$", re.I)
EDIT_TOOLS = {"edit", "Edit", "Write", "MultiEdit", "NotebookEdit"}


def _split_list(text: str) -> list[str]:
    t = text.strip()
    if not t or t.lower() in ("none", "n/a", "-", "no"):
        return []
    return [x.strip(" `") for x in re.split(r",|;|\n", t) if x.strip(" `")]


def parse_report(text: str) -> dict[str, Any] | None:
    """Parse the ```report block a sub-agent ends with (last one wins). None if missing."""
    blocks = _REPORT_BLOCK.findall(text or "")
    body = blocks[-1] if blocks else None
    if body is None:
        # Tolerate a report without the fence: "status: ..." lines at the end.
        tail = (text or "").strip().splitlines()[-8:]
        if not any(_FIELD.match(line) and line.strip().lower().startswith("status") for line in tail):
            return None
        body = "\n".join(tail)
    out: dict[str, str] = {}
    key = None
    for line in body.splitlines():
        m = _FIELD.match(line)
        if m:
            key = m.group(1).lower()
            out[key] = m.group(2).strip()
        elif key and line.strip():
            out[key] += "\n" + line.strip()
    if "status" not in out:
        return None
    status = out["status"].lower().split()[0].strip("|*") if out["status"] else "done"
    if status not in ("done", "partial", "failed"):
        status = "done" if "done" in out["status"].lower() else "partial"
    return {"status": status, "summary": out.get("summary", "").strip(),
            "files_changed": _split_list(out.get("files_changed", "")),
            "open_questions": _split_list(out.get("open_questions", ""))}


def strip_report(text: str) -> str:
    return _REPORT_BLOCK.sub("", text or "").strip()


def _files_from_tools(msgs: list[dict]) -> list[str]:
    files: list[str] = []
    for m in msgs:
        if m.get("kind") != "tool" or m.get("name") not in EDIT_TOOLS or m.get("is_error"):
            continue
        out = m.get("output") or ""
        found = re.findall(r"^(?:add|update|delete|create|modify)\w*:\s*(.+)$", out, re.M)
        if not found and m.get("summary"):
            found = [p.split(" ", 1)[-1] for p in m["summary"].split(", ")]
        for f in found:
            f = f.strip()
            if f and f not in files:
                files.append(f)
    return files


def build_report(db: Database, sid: int, session_status: str) -> dict[str, Any]:
    """The task's result from its latest turn: the parsed report, with fallbacks."""
    msgs = db.messages(sid)
    last_user = max((i for i, m in enumerate(msgs) if m["role"] == "user"), default=-1)
    turn = msgs[last_user + 1:]
    texts = [m["text"] for m in turn if m["role"] == "assistant" and m["kind"] == "text"]
    final = texts[-1] if texts else ""
    errors = [m["text"] for m in turn if m["kind"] == "error" and m.get("fatal", True)]
    rep = parse_report(final)
    if rep is None:
        summary = strip_report(final)
        rep = {"status": "done" if session_status == "done" else "failed",
               "summary": (summary[-600:] if summary else (errors[-1] if errors else "No reply.")),
               "files_changed": [], "open_questions": [], "report_missing": True}
    if session_status == "error" and rep["status"] == "done":
        rep["status"] = "failed"
    for f in _files_from_tools(turn):
        if f not in rep["files_changed"] and not any(f.endswith(x) or x.endswith(f) for x in rep["files_changed"]):
            rep["files_changed"].append(f)
    if errors:
        rep["errors"] = errors[-3:]
    return rep


# ---------------------------------------------------------------- bridge
class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        bridge: Bridge = self.server.bridge  # type: ignore[attr-defined]
        try:
            req = json.loads(self.rfile.readline(1_000_000) or b"{}")
            if not hmac.compare_digest(str(req.get("token", "")), bridge.token):
                resp = {"ok": False, "error": "bad token"}
            else:
                args = req.get("args") or {}
                fut = asyncio.run_coroutine_threadsafe(
                    bridge.handler(int(req.get("orch") or 0), str(req.get("op")), args), bridge.loop)
                resp = {"ok": True, "result": fut.result(timeout=float(args.get("timeout", 0) or 0) + 90)}
        except Exception as exc:  # noqa: BLE001 - reported to the agent as a tool error
            resp = {"ok": False, "error": str(exc) or type(exc).__name__}
        try:
            self.wfile.write((json.dumps(resp) + "\n").encode())
        except OSError:
            pass


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False


class Bridge:
    """Local TCP endpoint the MCP server calls. Requests run on the app's asyncio loop."""

    def __init__(self, handler) -> None:
        self.handler = handler
        self.token = secrets.token_hex(16)
        self.loop: asyncio.AbstractEventLoop | None = None
        self._server: _Server | None = None

    @property
    def address(self) -> str:
        assert self._server
        host, port = self._server.server_address[:2]
        return f"{host}:{port}"

    def start(self) -> None:
        if self._server:
            return
        self.loop = asyncio.get_event_loop()
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.bridge = self  # type: ignore[attr-defined]
        threading.Thread(target=self._server.serve_forever, name="orch-bridge", daemon=True).start()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


def mcp_python() -> str:
    """pythonw.exe next to the running interpreter: no console window when an engine starts the server."""
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if w.exists() else exe)


# ---------------------------------------------------------------- manager
class OrchestrationError(Exception):
    pass


class OrchestrationManager(QObject):
    changed = Signal(int)  # orchestration id (anything about it or its tasks)
    task_changed = Signal(int, int)  # orchestration id, task id

    def __init__(self, db: Database, mgr, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.db = db
        self.mgr = mgr
        self.bridge = Bridge(self.handle)
        self._events: dict[int, asyncio.Event] = {}
        self._stopping: set[int] = set()
        mgr.mcp_provider = self.mcp_for
        mgr.status_changed.connect(self._on_status)

    # ---- orchestrations
    def start(self, project_id: int, goal: str, engine: str, model: str, effort: str,
              max_parallel: int = 4) -> Orchestration:
        goal = goal.strip()
        if not goal:
            raise OrchestrationError("Write a goal first.")
        proj = self.db.project(project_id)
        if not proj:
            raise OrchestrationError("Pick a project.")
        try:
            model_registry.check(self.db, engine, model, strict=True)
        except model_registry.ModelDisabled as exc:
            raise OrchestrationError(str(exc)) from exc
        self.bridge.start()
        orch = self.db.add_orchestration(project_id, goal, engine, model, effort, max(1, int(max_parallel)))
        title = "Orchestrate · " + " ".join(goal.split())[:40]
        s = self.db.create_session(project_id, engine, model, effort, title=title)
        self.db.update_orchestration(orch.id, session_id=s.id)
        self.mgr.run_options[s.id] = {"sandbox": "read-only"}  # the planner reads; sub-agents write
        self.mgr.send(s.id, self.orchestrator_prompt(orch.id))
        self.db.update_session(s.id, title=title)
        self.mgr.session_updated.emit(s.id)
        self.changed.emit(orch.id)
        return self.db.orchestration(orch.id)  # type: ignore[return-value]

    def orchestrator_prompt(self, oid: int) -> str:
        orch = self.db.orchestration(oid)
        proj = self.db.project(orch.project_id) if orch else None
        lines = []
        for e in ENGINES:
            for m in model_registry.allowed(self.db, e):
                lines.append(f"- engine {e}: model {m.id} ({m.label}; efforts {', '.join(m.efforts)})")
        if not lines:
            lines.append("- (none enabled)")
        d = model_registry.default(self.db, orch.engine) if orch else None
        return ORCH_PROMPT.format(path=proj.path if proj else "?", goal=orch.goal if orch else "",
                                  max_parallel=orch.max_parallel if orch else 4, models="\n".join(lines),
                                  engine=orch.engine if orch else "", model=d[0] if d else "none")

    def mcp_for(self, sid: int) -> dict | None:
        orch = self.db.orchestration_for_session(sid)
        if not orch:
            return None
        self.bridge.start()
        return {"orchestrator": {
            "command": mcp_python(), "args": ["-m", "orchestrator.mcp_server"],
            "env": {"ORCH_BRIDGE": self.bridge.address, "ORCH_TOKEN": self.bridge.token,
                    "ORCH_ORCHESTRATION_ID": str(orch.id), "ORCH_WAIT_LIMIT": str(WAIT_LIMIT),
                    "PYTHONPATH": str(APP_ROOT)},
            "tool_timeout_sec": TOOL_TIMEOUT}}

    def is_active(self, oid: int) -> bool:
        orch = self.db.orchestration(oid)
        if not orch:
            return False
        planner = bool(orch.session_id and self.mgr.is_running(orch.session_id))
        return planner or any(t.status in ACTIVE for t in self.db.tasks(oid))

    # ---- tool calls from the MCP server
    async def handle(self, oid: int, op: str, args: dict[str, Any]) -> Any:
        orch = self.db.orchestration(oid)
        if not orch:
            raise OrchestrationError(f"Unknown orchestration {oid}.")
        if op == "delegate":
            return self.delegate(oid, **{k: args.get(k, "") for k in (
                "instructions", "engine", "model", "effort", "title", "cwd", "resume_session_id")})
        if op == "status":
            tid = int(args.get("task_id") or 0)
            if tid:
                return self.task_info(self._own_task(oid, tid))
            return {"tasks": [self.task_info(t) for t in self.db.tasks(oid)]}
        if op == "wait":
            return await self.wait(oid, [int(x) for x in args.get("task_ids") or []],
                                   float(args.get("timeout") or WAIT_LIMIT))
        if op == "send":
            return self.send_to_task(oid, int(args.get("task_id") or 0), str(args.get("text") or ""))
        raise OrchestrationError(f"Unknown operation {op!r}.")

    def _own_task(self, oid: int, tid: int) -> Task:
        t = self.db.task(tid)
        if not t or t.orchestration_id != oid:
            raise OrchestrationError(f"No task {tid} in this orchestration. Use task_status() to list tasks.")
        return t

    def delegate(self, oid: int, instructions: str, engine: str = "", model: str = "", effort: str = "",
                 title: str = "", cwd: str = "", resume_session_id: str = "") -> dict[str, Any]:
        orch = self.db.orchestration(oid)
        if not orch or orch.status != "running":
            raise OrchestrationError("This orchestration was stopped; no new tasks can start.")
        if not instructions.strip():
            raise OrchestrationError("instructions is empty.")
        proj = self.db.project(orch.project_id)
        resumed = None
        if resume_session_id:
            resumed = next((self.db.session_by_engine_id(e, resume_session_id) for e in ENGINES
                            if self.db.session_by_engine_id(e, resume_session_id)), None)
            if not resumed:
                raise OrchestrationError(f"No session with id {resume_session_id!r} is known.")
            engine = resumed.engine
            model = model or resumed.model or ""
        engine = (engine or orch.engine).strip().lower()
        if engine not in ENGINES:
            raise OrchestrationError(f"Unknown engine {engine!r}. Use one of: {', '.join(ENGINES)}.")
        if not model:
            d = model_registry.default(self.db, engine)
            if not d:
                raise OrchestrationError(model_registry.unavailable_message(engine))
            model, effort = d[0], effort or d[1]
        try:  # disabled or unknown models are refused, never swapped
            model_registry.check(self.db, engine, model, strict=True)
        except model_registry.ModelDisabled as exc:
            raise OrchestrationError(str(exc)) from exc
        efforts = next((m.efforts for m in model_registry.allowed(self.db, engine) if m.id == model), [])
        if effort and efforts and effort not in efforts:
            raise OrchestrationError(f"Effort {effort!r} is not available for {model}. Use one of: {', '.join(efforts)}.")
        effort = effort or (efforts[min(1, len(efforts) - 1)] if efforts else "")
        work = Path(proj.path)
        if cwd:
            target = (work / cwd).resolve() if not Path(cwd).is_absolute() else Path(cwd).resolve()
            if not str(target).lower().startswith(str(work.resolve()).lower()) or not target.is_dir():
                raise OrchestrationError(f"cwd must be an existing folder inside the project ({proj.path}).")
            work = target
        title = " ".join((title or instructions).split())[:60]
        task = self.db.add_task(oid, engine, model, effort, title, instructions.strip(), str(work))
        if resumed:
            sid = resumed.id
            self.db.update_session(sid, parent_id=orch.session_id, model=model, effort=effort)
        else:
            sid = self.db.create_session(orch.project_id, engine, model, effort, parent_id=orch.session_id,
                                         title=title, cwd=str(work)).id
        self.db.update_task(task.id, session_id=sid)
        self._events[task.id] = asyncio.Event()
        self.mgr.session_updated.emit(sid)
        self.pump(oid)
        t = self.db.task(task.id)
        return {"task_id": t.id, "status": t.status, "engine": engine, "model": model, "effort": effort,
                "note": "Running in the background. Call wait_for([task_id]) to get its report."}

    def pump(self, oid: int) -> None:
        """Start queued tasks while under the orchestration's max parallel."""
        orch = self.db.orchestration(oid)
        if not orch:
            return
        tasks = self.db.tasks(oid)
        running = sum(1 for t in tasks if t.status in ("running", "waiting"))
        for t in tasks:
            if running >= orch.max_parallel:
                break
            if t.status == "queued":
                if self._start_task(t):
                    running += 1
        self.changed.emit(oid)

    def _start_task(self, t: Task) -> bool:
        sid = t.session_id
        if not sid:
            return False
        work = self._maybe_worktree(t)
        if work:
            self.db.update_session(sid, cwd=work)
        self.mgr.run_options[sid] = {"claude_mode": "acceptEdits", "sandbox": "workspace-write"}
        first = not self.db.messages(sid)
        prompt = (SUBAGENT_INTRO + t.instructions if first else t.instructions) + CLOSING
        if not self.mgr.send(sid, prompt):
            self._finish(t.id, "failed", {"status": "failed", "summary": "Could not start the sub-agent "
                                          "(its model may be disabled, or the chat is busy).",
                                          "files_changed": [], "open_questions": []})
            return False
        s = self.db.session(sid)
        if s and s.title != t.title:
            self.db.update_session(sid, title=t.title)
        self.db.update_task(t.id, status="running", started_at=time.time(), finished_at=None)
        self.task_changed.emit(t.orchestration_id, t.id)
        return True

    def _maybe_worktree(self, t: Task) -> str | None:
        """Per-task git worktree when the setting is on and another task is writing in the same repo."""
        if t.worktree:
            return t.worktree
        if not self.db.get_setting("orchestrate.worktrees", False):
            return None
        repo = _git_root(t.cwd or "")
        if not repo:
            return None
        others = [o for o in self.db.tasks(t.orchestration_id)
                  if o.id != t.id and o.status in ("running", "waiting") and not o.worktree
                  and _git_root(o.cwd or "") == repo]
        if not others:
            return None
        dest = app_dir() / "worktrees" / f"{Path(repo).name}-task{t.id}"
        branch = f"orchestrator/task-{t.id}"
        r = subprocess.run(["git", "-C", repo, "worktree", "add", "-b", branch, str(dest)],
                           capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
        if r.returncode != 0:
            return None
        rel = Path(t.cwd or repo).resolve().relative_to(Path(repo).resolve()) if t.cwd else Path(".")
        work = str((dest / rel).resolve())
        self.db.update_task(t.id, worktree=str(dest), cwd=work)
        return work

    def _on_status(self, sid: int, status: str) -> None:
        orch = self.db.orchestration_for_session(sid)
        if orch:  # the planner chat itself
            if status == "running" and orch.status != "running":  # user continued the planner chat
                self.db.update_orchestration(orch.id, status="running", finished_at=None)
            if status in ("done", "error", "idle") and orch.status == "running":
                if not any(t.status in ACTIVE for t in self.db.tasks(orch.id)):
                    self.db.update_orchestration(orch.id, status={"done": "done", "error": "error"}.get(status, "stopped"),
                                                 finished_at=time.time())
            self.changed.emit(orch.id)
            return
        t = self.db.task_for_session(sid)
        if not t or t.status in FINISHED and status in ("done", "error", "idle"):
            return
        if status in ("running", "waiting"):
            if t.status != status:
                self.db.update_task(t.id, status=status)
                self.task_changed.emit(t.orchestration_id, t.id)
                self.changed.emit(t.orchestration_id)
            return
        if status in ("done", "error", "idle"):
            if t.id in self._stopping or status == "idle":
                self._stopping.discard(t.id)
                rep = build_report(self.db, sid, "error")
                rep["status"] = "failed"
                rep["summary"] = "Stopped before it finished. " + rep.get("summary", "")
                self._finish(t.id, "stopped", rep)
            else:
                rep = build_report(self.db, sid, status)
                self._finish(t.id, "failed" if rep["status"] == "failed" else "done", rep)

    def _finish(self, tid: int, status: str, result: dict) -> None:
        t = self.db.task(tid)
        if not t:
            return
        self.db.update_task(tid, status=status, result=result, finished_at=time.time())
        ev = self._events.setdefault(tid, asyncio.Event())
        ev.set()
        self.task_changed.emit(t.orchestration_id, tid)
        self.pump(t.orchestration_id)
        orch = self.db.orchestration(t.orchestration_id)
        planner = self.db.session(orch.session_id) if orch and orch.session_id else None
        if (orch and orch.status == "running" and planner and planner.status not in ("running", "waiting")
                and not any(x.status in ACTIVE for x in self.db.tasks(orch.id))):
            self.db.update_orchestration(orch.id, status="done", finished_at=time.time())
        if orch:
            self.changed.emit(orch.id)

    def task_info(self, t: Task) -> dict[str, Any]:
        s = self.db.session(t.session_id) if t.session_id else None
        end = t.finished_at or time.time()
        info: dict[str, Any] = {
            "task_id": t.id, "title": t.title, "engine": t.engine, "model": t.model, "effort": t.effort,
            "status": t.status, "elapsed_sec": round(end - t.started_at) if t.started_at else 0,
        }
        if t.worktree:
            info["worktree"] = t.worktree
            info["branch"] = f"orchestrator/task-{t.id}"
        if s and s.engine_session_id:
            info["session_id"] = s.engine_session_id
        if t.status in FINISHED:
            info["report"] = t.result
        elif t.status in ("running", "waiting") and t.session_id:
            info["last_output"] = self.last_line(t.session_id)
            if t.status == "waiting":
                info["note"] = "Waiting for the user to approve a tool."
        return info

    def last_line(self, sid: int) -> str:
        live = self.mgr.live_text(sid).strip()
        if live:
            return live.splitlines()[-1][-160:]
        tools = self.mgr.live_tools(sid)
        if tools:
            return f"{tools[-1].name}: {tools[-1].summary}"[:160]
        for m in reversed(self.db.messages(sid)):
            if m["role"] == "assistant" and m["kind"] == "text":
                lines = strip_report(m["text"]).splitlines()
                return (lines[-1] if lines else "")[-160:]
            if m["kind"] == "tool":
                return f"{m.get('name', '')}: {m.get('summary', '')}"[:160]
        return ""

    async def wait(self, oid: int, task_ids: list[int], timeout: float) -> dict[str, Any]:
        if not task_ids:
            task_ids = [t.id for t in self.db.tasks(oid)]
        tasks = [self._own_task(oid, tid) for tid in task_ids]
        deadline = time.monotonic() + max(0.0, min(timeout, WAIT_LIMIT))
        pending = [t.id for t in tasks if t.status not in FINISHED]
        while pending:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            evs = [self._events.setdefault(tid, asyncio.Event()) for tid in pending]
            waiters = [asyncio.ensure_future(e.wait()) for e in evs]
            try:
                await asyncio.wait(waiters, timeout=left, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for w in waiters:
                    w.cancel()
            pending = [tid for tid in pending if self.db.task(tid).status not in FINISHED]
        infos = [self.task_info(self.db.task(tid)) for tid in task_ids]
        return {"done": not pending, "tasks": infos, "pending": pending}

    def send_to_task(self, oid: int, tid: int, text: str) -> dict[str, Any]:
        t = self._own_task(oid, tid)
        if not text.strip():
            raise OrchestrationError("text is empty.")
        if t.status in ACTIVE:
            raise OrchestrationError(f"Task {tid} is still {t.status}. Call wait_for([{tid}]) first.")
        orch = self.db.orchestration(oid)
        if not orch or orch.status != "running":
            raise OrchestrationError("This orchestration was stopped.")
        self._events[tid] = asyncio.Event()
        self.db.update_task(tid, status="queued", result=None, instructions=t.instructions + "\n\nFollow-up:\n" + text)
        t = self.db.task(tid)
        sid = t.session_id
        self.mgr.run_options[sid] = {"claude_mode": "acceptEdits", "sandbox": "workspace-write"}
        if not self.mgr.send(sid, text.strip() + CLOSING):
            self._finish(tid, "failed", {"status": "failed", "summary": "Could not send the follow-up.",
                                         "files_changed": [], "open_questions": []})
            return self.task_info(self.db.task(tid))
        self.db.update_task(tid, status="running", started_at=time.time(), finished_at=None)
        self.task_changed.emit(oid, tid)
        self.changed.emit(oid)
        return {"task_id": tid, "status": "running", "note": "Call wait_for to get the new report."}

    # ---- UI actions
    async def stop_task(self, tid: int) -> None:
        t = self.db.task(tid)
        if not t:
            return
        if t.status == "queued":
            self._finish(tid, "stopped", {"status": "failed", "summary": "Stopped before it started.",
                                          "files_changed": [], "open_questions": []})
        elif t.status in ("running", "waiting") and t.session_id:
            self._stopping.add(tid)
            await self.mgr.stop(t.session_id)

    def retry_task(self, tid: int) -> None:
        """Run the task again in a fresh chat with the same instructions and model."""
        t = self.db.task(tid)
        if not t or t.status in ACTIVE:
            return
        orch = self.db.orchestration(t.orchestration_id)
        if not orch:
            return
        if not model_registry.is_allowed(self.db, t.engine, t.model or "", strict=True):
            self._finish(tid, "failed", {"status": "failed", "summary": model_registry.disabled_message(
                self.db, t.engine, t.model or ""), "files_changed": [], "open_questions": []})
            return
        if orch.status != "running":
            self.db.update_orchestration(orch.id, status="running", finished_at=None)
        s = self.db.create_session(orch.project_id, t.engine, t.model or "", t.effort or "", parent_id=orch.session_id,
                                   title=t.title, cwd=t.cwd)
        self._events[tid] = asyncio.Event()
        self.db.update_task(tid, session_id=s.id, status="queued", result=None, started_at=None, finished_at=None,
                            worktree=None)
        self.mgr.session_updated.emit(s.id)
        self.pump(orch.id)

    async def stop_all(self, oid: int) -> None:
        orch = self.db.orchestration(oid)
        if not orch:
            return
        self.db.update_orchestration(oid, status="stopped", finished_at=time.time())
        if orch.session_id and self.mgr.is_running(orch.session_id):
            await self.mgr.stop(orch.session_id)
        for t in self.db.tasks(oid):
            if t.status in ACTIVE:
                await self.stop_task(t.id)
        self.changed.emit(oid)


def _git_root(path: str) -> str | None:
    if not path or not Path(path).is_dir():
        return None
    r = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                       creationflags=CREATE_NO_WINDOW)
    return str(Path(r.stdout.strip())) if r.returncode == 0 and r.stdout.strip() else None
