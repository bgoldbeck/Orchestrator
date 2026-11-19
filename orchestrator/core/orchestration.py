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

from PySide6.QtCore import QObject, QTimer, Signal, SignalInstance

from orchestrator.core import concurrency, limits, model_registry, queue_rules, review, routing
from orchestrator.core.project_pick import find_projects
from orchestrator.core.catalog import ENGINES
from orchestrator.core.summaries import find_sessions
from orchestrator.storage.db import Database, Orchestration, Task, app_dir

APP_ROOT = Path(__file__).resolve().parents[2]
WAIT_LIMIT = 240  # seconds one wait_for call may block (kept under the tool timeout below)
TOOL_TIMEOUT = 600
ACTIVE = ("proposed", "queued", "running", "waiting", "checking")
CONFIRM_MODES = {"countdown": "Start after a short countdown", "always_ask": "Always ask me", "never": "Never ask"}
COUNTDOWN_SEC = 10
SILENT_SETTING = "orchestrate.silent_min"  # minutes without output before a running task counts as silent
SILENT_DEFAULT = 20
TICK_MS = 60_000  # re-check limits (a reset starts paused tasks) and silent tasks once a minute
MAX_CRITERIA = 10
FINISHED = ("done", "failed", "stopped")
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

CLOSING = """

When you are finished, end your final message with this report block, filled in:

```report
status: done | partial | failed
summary: <one to three sentences on what you did and the result>
files_changed: <comma-separated paths, or none>
assumptions: <choices you made that the user did not specify, or none>
open_questions: <questions or problems for the orchestrator, or none>
```"""

CRITERIA_CLOSING = """
criteria: <for each "done when" item, its number and met / not met, e.g. 1 met; 2 not met: why>"""

SUBAGENT_INTRO = ("You are a sub-agent started by an orchestrator. Work only on the task below, in the "
                  "current folder. Nobody can answer questions while you work: make reasonable choices and "
                  "list anything unclear under open_questions.\n\nTask:\n")

ORCH_PROMPT = """You are the orchestrator (planner) inside Orchestrator, the user's Windows desktop app that runs Claude
Code and Codex agents. The user is talking to you from the app's Orchestrate page. When they say "the app" or "this
app" they mean Orchestrator: use the `app` tools (about_app, get_usage, list_models, set_default_model,
list_preferences, set_model_preference, list_settings, set_setting, ...) to read or change it instead of searching for
it on disk. This run works in the project folder {path}.

Goal from the user:
{goal}

You coordinate sub-agents with the `orchestrator` MCP tools:
- find_sessions(query) searches earlier chats by summary. Use it first: continuing a chat that already worked on
  the same thing is cheaper than starting fresh.
- find_projects(query) lists the user's project folders that match a description. A task may run in another
  project than this one: pass that project's name as `project` to delegate.
- delegate(instructions, engine, model, effort, title, cwd, resume_session_id, reason, project, after, done_when,
  files) starts a sub-agent and returns a task_id at once. after = task ids that must finish first (the task waits in
  the queue until they are done). done_when = 1-10 short, checkable criteria, fixed once the task starts; the report
  marks each met or not. files = paths the task will change, when you know them: two tasks that share a file never
  run at the same time. Pass resume_session_id (a session_ref like "s:12") to continue an earlier chat; the user sees a
  reuse card and may choose to start fresh instead.
- wait_for(task_ids) waits for tasks and returns their reports. If it says tasks are still pending, call it again.
- task_status(task_id) checks without waiting. send_to_task(task_id, text) sends a follow-up to a finished sub-agent.
- ask_user(question, options) asks the user and waits for the answer. Use it only when you truly cannot decide.
- suggest_preference(task_type, keywords, engine, model, effort, notes): when the user says they prefer a model for a
  kind of task ("prefer Opus for complex coding"), call this; the app offers to save it as a rule. It does not wait.
- The `app` MCP tools are the app itself: about_app, get_usage (plan limits left, reset times, spend per model),
  list_models, set_default_model, model preference rules, settings, appearance and schedules. Use them when the user
  asks about the app, usage or models, or asks you to change them ("coding use GPT 6.2 medium" ->
  set_model_preference). Each change shows the user an Undo button.

How to work:
1. Look at the project briefly if you need to, call find_sessions, then split the goal into a few clear tasks that
   can run in parallel.
2. Delegate them. A sub-agent starts with no context, so give complete instructions (which files, what result).
   Give each a short title. Up to {max_parallel} run at once (fewer if the app's global limit is reached); the rest
   wait in a queue and start on their own.
3. Call wait_for on the task ids. Read each report, including its assumptions and, when present, "checks" (the
   project's tests, run by the app on the delivered work: a failed run makes the task failed; send a fix). If something failed or is missing, send a follow-up or delegate a fix.
   A task that failed on an automatically picked model is retried once by the app on a stronger setting.
4. Do not edit files yourself. Sub-agents do the work.
5. Finish with a short summary for the user: what each task did, files changed, and open questions.

Choosing engine, model and effort: leave them empty and the app picks from the enabled models (Auto) with a reason,
or choose yourself using this routing guide and say why in `reason`:
{guide}

Engines and models you may use (only these ids; strongest first per engine):
{models}"""


# ---------------------------------------------------------------- reports
_REPORT_BLOCK = re.compile(r"```report\s*\n(.*?)```", re.S | re.I)
_FIELD = re.compile(r"^\s*(status|summary|files_changed|assumptions|criteria|open_questions)\s*:\s*(.*)$", re.I)
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
    rep = {"status": status, "summary": out.get("summary", "").strip(),
           "files_changed": _split_list(out.get("files_changed", "")),
           "open_questions": _split_list(out.get("open_questions", ""))}
    if _split_list(out.get("assumptions", "")):
        rep["assumptions"] = _split_list(out["assumptions"])
    if out.get("criteria", "").strip():
        rep["criteria"] = out["criteria"].strip()
    return rep


def closing_for(t: Task) -> str:
    """The report instructions for a task; with fixed "done when" criteria, ask for each to be marked."""
    if not t.done_when:
        return CLOSING
    return CLOSING[:-4] + CRITERIA_CLOSING + "\n```"


def criteria_block(done_when: list[str]) -> str:
    if not done_when:
        return ""
    lines = "\n".join(f"{i}. {c}" for i, c in enumerate(done_when, 1))
    return "\n\nDone when (fixed, do not change; the result is checked against these):\n" + lines


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


def mcp_command() -> tuple[str, list[str]]:
    """How an engine starts the MCP server: the built Orchestrator.exe re-runs itself with --mcp-server."""
    if getattr(sys, "frozen", False):
        return sys.executable, ["--mcp-server"]
    return mcp_python(), ["-m", "orchestrator.mcp_server"]


# ---------------------------------------------------------------- manager
class OrchestrationError(Exception):
    pass


class OrchestrationManager(QObject):
    changed = Signal(int)  # orchestration id (anything about it or its tasks)
    task_changed = Signal(int, int)  # orchestration id, task id
    question_asked = Signal(int, int)  # orchestration id, question id

    def __init__(self, db: Database, mgr, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.db = db
        self.mgr = mgr
        self.bridge = Bridge(self.handle)
        self._events: dict[int, asyncio.Event] = {}
        self._stopping: set[int] = set()
        self._answers: dict[int, asyncio.Event] = {}
        self._retry_text: dict[int, str] = {}  # task id -> follow-up text for an escalated retry
        self.rule_offers: dict[int, list[dict]] = {}  # orchestration id -> offered preference rules (unsaved)
        self._jobs: set = set()
        self._merge_lock = asyncio.Lock()
        self._limit_override: dict[str, float] = {}  # engine -> reset time the user chose to ignore
        self._activity: dict[int, float] = {}  # session id -> last event time
        self._snooze: dict[int, float] = {}  # task id -> "keep waiting" pressed at
        self._silent: set[int] = set()
        mgr.mcp_provider = self.mcp_for
        if isinstance(getattr(mgr, "event", None), SignalInstance):  # output from any chat = a sign of life
            mgr.event.connect(self._on_event)
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        mgr.status_changed.connect(self._on_status)
        concurrency.hub.freed.connect(self.pump_all)

    # ---- orchestrations
    def start(self, project_id: int, goal: str, engine: str, model: str, effort: str,
              max_parallel: int = 4, project_reason: str | None = None) -> Orchestration:
        goal = goal.strip()
        if not goal:
            raise OrchestrationError("Write a goal first.")
        proj = self.db.project(project_id)
        if not proj:
            raise OrchestrationError("Pick a project.")
        pick_reason = None
        if engine == "auto":
            pick = routing.route_planner(self.db, goal)
            if not pick:
                raise OrchestrationError("No models are enabled. Turn one on in Settings › Models.")
            engine, model, effort, pick_reason = pick.engine, pick.model, pick.effort, pick.reason
        try:
            model_registry.check(self.db, engine, model, strict=True)
        except model_registry.ModelDisabled as exc:
            raise OrchestrationError(str(exc)) from exc
        self.bridge.start()
        orch = self.db.add_orchestration(project_id, goal, engine, model, effort, max(1, int(max_parallel)))
        if pick_reason:
            self.db.update_orchestration(orch.id, pick_reason=pick_reason)
        if project_reason:
            self.db.update_orchestration(orch.id, project_reason=project_reason)
        title = "Orchestrate · " + " ".join(goal.split())[:40]
        s = self.db.create_session(project_id, engine, model, effort, title=title)
        self.db.update_orchestration(orch.id, session_id=s.id)
        self.mgr.run_options[s.id] = {"sandbox": "read-only"}  # the planner reads; sub-agents write
        self.mgr.send(s.id, self.orchestrator_prompt(orch.id))
        self.db.update_session(s.id, title=title)
        self.mgr.session_updated.emit(s.id)
        from orchestrator.core import preferences

        draft = preferences.parse_preference(self.db, goal)  # "prefer X for Y" in the goal: offer a rule
        if draft:
            self.offer_rule(orch.id, draft)
        self.changed.emit(orch.id)
        return self.db.orchestration(orch.id)  # type: ignore[return-value]

    # ---- preference rules offered from chat
    def offer_rule(self, oid: int, rule) -> dict:
        from orchestrator.core import preferences

        offers = self.rule_offers.setdefault(oid, [])
        same = next((o for o in offers if o["rule"].name.lower() == rule.name.lower()
                     and o["rule"].model == rule.model), None)
        if same:
            return same
        offer = {"id": rule.id, "rule": rule, "text": f"{rule.name} → {preferences.describe(self.db, rule)}",
                 "warning": preferences.image_warning(self.db, rule)}
        offers.append(offer)
        self.changed.emit(oid)
        return offer

    def resolve_offer(self, oid: int, offer_id: str, save: bool) -> None:
        from orchestrator.core import preferences

        offers = self.rule_offers.get(oid, [])
        offer = next((o for o in offers if o["id"] == offer_id), None)
        if not offer:
            return
        offers.remove(offer)
        if save:
            preferences.add_rule(self.db, offer["rule"])
        self.changed.emit(oid)

    def suggest_preference(self, oid: int, args: dict[str, Any]) -> dict[str, Any]:
        from orchestrator.core import preferences

        engine = str(args.get("engine") or "").strip().lower()
        model = str(args.get("model") or "").strip()
        if not engine and model:
            engine = next((m.engine for m in self.db.models() if m.id == model), "")
        if engine not in ENGINES or not any(m.id == model for m in self.db.models(engine)):
            raise OrchestrationError(f"Unknown model {model!r}. Use an id from the model list in your instructions.")
        task = str(args.get("task_type") or "").strip()
        if not task:
            raise OrchestrationError("task_type is empty.")
        kws = args.get("keywords") or []
        kws = preferences.split_keywords(kws) if isinstance(kws, str) else [str(k) for k in kws]
        rule = preferences.Rule(name=task, keywords=kws or [task.lower()], engine=engine, model=model,
                                effort=str(args.get("effort") or ""), notes=str(args.get("notes") or "Saved from chat"))
        offer = self.offer_rule(oid, rule)
        return {"offered": True, "rule": offer["text"], "warning": offer["warning"] or None,
                "note": "The user sees a 'Save rule' offer in the app. Carry on with the goal."}

    def orchestrator_prompt(self, oid: int) -> str:
        orch = self.db.orchestration(oid)
        proj = self.db.project(orch.project_id) if orch else None
        lines = []
        for e in ENGINES:
            for m in routing.strength_order(model_registry.allowed(self.db, e)):
                lines.append(f"- engine {e}: model {m.id} ({m.label}; efforts {', '.join(m.efforts)})")
        if not lines:
            lines.append("- (none enabled)")
        return ORCH_PROMPT.format(path=proj.path if proj else "?", goal=orch.goal if orch else "",
                                  max_parallel=orch.max_parallel if orch else 4, models="\n".join(lines),
                                  guide=routing.planner_guide(self.db))

    def mcp_for(self, sid: int) -> dict | None:
        orch = self.db.orchestration_for_session(sid)
        if not orch:
            return None
        self.bridge.start()
        command, args = mcp_command()
        env = {"ORCH_BRIDGE": self.bridge.address, "ORCH_TOKEN": self.bridge.token,
               "ORCH_ORCHESTRATION_ID": str(orch.id), "ORCH_WAIT_LIMIT": str(WAIT_LIMIT)}
        if not getattr(sys, "frozen", False):
            env["PYTHONPATH"] = str(APP_ROOT)
        return {"orchestrator": {"command": command, "args": args, "env": env, "tool_timeout_sec": TOOL_TIMEOUT}}

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
            kw = {k: args.get(k, "") for k in (
                "instructions", "engine", "model", "effort", "title", "cwd", "resume_session_id", "reason", "project")}
            kw.update({k: args.get(k) for k in ("after", "done_when", "files")})
            return self.delegate(oid, **kw)
        if op == "find_projects":
            return find_projects(self.db, str(args.get("query") or ""))
        if op == "suggest_preference":
            return self.suggest_preference(oid, args)
        if op == "find_sessions":
            return self.find(oid, str(args.get("query") or ""), str(args.get("project") or ""))
        if op == "ask":
            return await self.ask(oid, str(args.get("question") or ""), list(args.get("options") or []),
                                  int(args.get("question_id") or 0), float(args.get("timeout") or WAIT_LIMIT),
                                  str(args.get("recommended") or ""))
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
                 title: str = "", cwd: str = "", resume_session_id: str = "", reason: str = "",
                 project: str = "", after: Any = None, done_when: Any = None,
                 files: Any = None) -> dict[str, Any]:
        orch = self.db.orchestration(oid)
        if not orch or orch.status != "running":
            raise OrchestrationError("This orchestration was stopped; no new tasks can start.")
        if not instructions.strip():
            raise OrchestrationError("instructions is empty.")
        deps = self._deps(oid, after)
        criteria = self._criteria(done_when)
        files = [str(f).strip() for f in (_as_list(files)) if str(f).strip()][:50]
        proj = self.db.project(orch.project_id)
        if project:
            proj = self.project_ref(project)
        resumed = self.resolve_session(resume_session_id) if resume_session_id else None
        if resume_session_id and not resumed:
            raise OrchestrationError(f"No session {resume_session_id!r} is known. Use a session_ref from find_sessions.")
        if resumed:
            engine = resumed.engine  # reuse keeps that session's engine
            if not model and resumed.model and model_registry.is_allowed(self.db, engine, resumed.model, strict=True):
                model, reason = resumed.model, reason or "Continues the earlier chat on its model"
        engine = engine.strip().lower()
        if engine and engine not in ENGINES:
            raise OrchestrationError(f"Unknown engine {engine!r}. Use one of: {', '.join(ENGINES)}.")
        auto = not model
        if auto:  # Auto: pick from enabled models only, with a reason
            pick = routing.route(self.db, instructions, engine=engine, prefer=orch.engine)
            if not pick:
                raise OrchestrationError(model_registry.unavailable_message(engine) if engine else
                                         "No models are enabled. Turn one on in Settings › Models.")
            engine, model, effort = pick.engine, pick.model, effort or pick.effort
            reason = pick.reason
        engine = engine or orch.engine
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
        self.db.update_task(task.id, pick_reason=reason or "Chosen by the planner", auto_picked=int(auto),
                            depends_on=deps, done_when=criteria, files=files)
        sid = self.db.create_session(proj.id, engine, model, effort, parent_id=orch.session_id,
                                     title=title, cwd=str(work)).id
        self.db.update_task(task.id, session_id=sid)
        self._events[task.id] = asyncio.Event()
        mode = self.db.get_setting("orchestrate.confirm", "countdown")
        if resumed:  # reuse card: Use it / Start fresh / Pick another
            cands = find_sessions(self.db, instructions, orch.project_id, exclude={sid}, limit=5)
            self._propose(task.id, {"kind": "reuse", "session_id": resumed.id, "candidates": cands}, mode)
        elif auto and mode == "always_ask":  # the user confirms the automatic pick
            self._propose(task.id, {"kind": "pick"}, mode)
        self.mgr.session_updated.emit(sid)
        self.pump(oid)
        t = self.db.task(task.id)
        res = {"task_id": t.id, "status": t.status, "engine": t.engine, "model": t.model, "effort": t.effort,
               "pick_reason": t.pick_reason,
               "note": "Running in the background. Call wait_for([task_id]) to get its report."}
        if t.status == "proposed":
            res["note"] = ("Waiting for the user to confirm on the board (reuse or model pick); it then starts "
                           "on its own. Call wait_for([task_id]) as usual.")
        elif t.status == "queued":
            res["waiting"] = queue_rules.why_waiting(self.db, t)
            res["note"] = "Queued: " + res["waiting"] + ". It starts on its own; call wait_for([task_id])."
        return res

    def _deps(self, oid: int, after: Any) -> list[int]:
        out = []
        for x in _as_list(after):
            try:
                tid = int(str(x).strip().lstrip("T-").lstrip("t-"))
            except ValueError:
                raise OrchestrationError(f"after: {x!r} is not a task id.") from None
            self._own_task(oid, tid)
            if tid not in out:
                out.append(tid)
        return out

    @staticmethod
    def _criteria(done_when: Any) -> list[str]:
        items = [" ".join(str(c).split()) for c in _as_list(done_when) if str(c).strip()]
        if len(items) > MAX_CRITERIA:
            raise OrchestrationError(f"done_when: at most {MAX_CRITERIA} criteria.")
        return [c[:300] for c in items]

    def project_ref(self, ref: str):
        """A project by name (case-insensitive) or folder path; raises a helpful error otherwise."""
        ref = (ref or "").strip()
        projects = self.db.projects()
        p = next((p for p in projects if p.name.lower() == ref.lower()), None)
        if not p:
            norm = lambda x: str(Path(x).resolve()).lower()  # noqa: E731
            p = next((p for p in projects if Path(ref).is_absolute() and norm(p.path) == norm(ref)), None)
        if not p:
            names = ", ".join(p.name for p in projects[:12]) or "(none)"
            raise OrchestrationError(f"No project {ref!r}. Use find_projects, or one of: {names}.")
        return p

    # ---- reuse / pick proposals
    def resolve_session(self, ref: str):
        ref = (ref or "").strip()
        m = re.fullmatch(r"(?:s:)?(\d+)", ref)
        if m:
            return self.db.session(int(m.group(1)))
        return next((s for s in (self.db.session_by_engine_id(e, ref) for e in ENGINES) if s), None)

    def _propose(self, tid: int, proposal: dict, mode: str) -> None:
        if mode == "countdown":
            proposal["deadline"] = time.time() + COUNTDOWN_SEC
            loop = asyncio.get_event_loop()
            loop.call_later(COUNTDOWN_SEC, lambda: self._auto_accept(tid))
        self.db.update_task(tid, status="proposed", proposal=proposal)

    def _auto_accept(self, tid: int) -> None:
        t = self.db.task(tid)
        if t and t.status == "proposed" and (t.proposal or {}).get("deadline"):
            self.resolve_proposal(tid, "accept")

    def resolve_proposal(self, tid: int, action: str, session_id: int | None = None) -> None:
        """action: accept (use the proposed chat / pick) | fresh (new chat) | pick (continue `session_id`)."""
        t = self.db.task(tid)
        if not t or t.status != "proposed":
            return
        prop = t.proposal or {}
        target = None
        if prop.get("kind") == "reuse":
            if action == "accept":
                target = prop.get("session_id")
            elif action == "pick":
                target = session_id
        if target:
            s = self.db.session(int(target))
            orch = self.db.orchestration(t.orchestration_id)
            if s and orch:
                model, effort = t.model, t.effort
                if s.engine != t.engine or not model_registry.is_allowed(self.db, s.engine, model or "", strict=True):
                    keep = s.model if model_registry.is_allowed(self.db, s.engine, s.model or "", strict=True) else None
                    d = (keep, s.effort) if keep else model_registry.default(self.db, s.engine)
                    if not d:
                        self._finish(tid, "failed", {"status": "failed", "summary": model_registry.unavailable_message(
                            s.engine), "files_changed": [], "open_questions": []})
                        return
                    model, effort = d
                old = t.session_id
                self.db.update_session(s.id, parent_id=orch.session_id, model=model, effort=effort)
                self.db.update_task(tid, session_id=s.id, engine=s.engine, model=model, effort=effort, reused=1,
                                    pick_reason=f"Continues “{s.title}” ({ENGINES.get(s.engine, s.engine)})")
                if old and old != s.id and not self.db.messages(old):
                    self.db.delete_session(old)
        self.db.update_task(tid, status="queued", proposal=None)
        self.task_changed.emit(t.orchestration_id, tid)
        self.pump(t.orchestration_id)

    def change_pick(self, tid: int, engine: str, model: str, effort: str) -> None:
        """User override on the card before the task starts."""
        t = self.db.task(tid)
        if not t or t.status not in ("proposed", "queued"):
            return
        model_registry.check(self.db, engine, model, strict=True)
        self.db.update_task(tid, engine=engine, model=model, effort=effort, auto_picked=0,
                            pick_reason=f"Changed by you: {model_registry.label(self.db, engine, model)} · {effort}")
        if t.session_id and not self.db.messages(t.session_id):
            self.db.update_session(t.session_id, engine=engine, model=model, effort=effort)
        self.task_changed.emit(t.orchestration_id, tid)
        self.changed.emit(t.orchestration_id)

    # ---- find_sessions / ask_user
    def find(self, oid: int, query: str, project: str = "") -> dict[str, Any]:
        orch = self.db.orchestration(oid)
        pid = orch.project_id if orch else None
        if project:
            p = next((p for p in self.db.projects() if p.name.lower() == project.lower()), None)
            pid = p.id if p else pid
        own = {t.session_id for t in self.db.tasks(oid) if t.session_id}
        res = find_sessions(self.db, query, pid, exclude=own)
        return {"candidates": res, "note": ("Pass a session_ref as resume_session_id to delegate to continue it."
                                            if res else "No earlier chat matches; start fresh.")}

    async def ask(self, oid: int, question: str, options: list[str], qid: int, timeout: float,
                  recommended: str = "") -> dict[str, Any]:
        q = self.db.question(qid) if qid else None
        if q and q.orchestration_id != oid:
            q = None
        if q is None:
            if not question.strip():
                raise OrchestrationError("question is empty.")
            opts = [str(o) for o in options][:6]
            rec = recommended.strip() or None
            if rec and rec not in opts:
                opts = [rec, *opts][:6]
            q = self.db.add_question(oid, question.strip(), opts, rec)
            self.question_asked.emit(oid, q.id)
            self.changed.emit(oid)
        ev = self._answers.setdefault(q.id, asyncio.Event())
        if q.answer is None:
            try:
                await asyncio.wait_for(ev.wait(), timeout=max(0.0, min(timeout, WAIT_LIMIT)))
            except asyncio.TimeoutError:
                pass
        q = self.db.question(q.id)
        if q.answer is None:
            return {"answered": False, "question_id": q.id}
        return {"answered": True, "question_id": q.id, "answer": q.answer}

    def answer(self, qid: int, text: str) -> None:
        q = self.db.question(qid)
        if not q or q.answer is not None or not text.strip():
            return
        self.db.answer_question(qid, text.strip())
        self._answers.setdefault(qid, asyncio.Event()).set()
        self.changed.emit(q.orchestration_id)

    def pump(self, oid: int) -> None:
        """Start queued tasks (this orchestration's and any other waiting ones) as limits allow."""
        self.pump_all()
        self.changed.emit(oid)

    def pump_all(self) -> None:
        """Start queued tasks in global queue order while both the task's orchestration limit
        ("Run up to N tasks at once") and the app-wide limit (Settings) have room."""
        free = concurrency.free_slots(self.db)
        running: dict[int, int] = {}
        touched: set[int] = set()
        paused = self.paused()
        wt_on = bool(self.db.get_setting("orchestrate.worktrees", False))
        for t in self.db.queued_tasks():
            if free <= 0:
                break
            orch = self.db.orchestration(t.orchestration_id)
            if not orch:
                continue
            state, dt = queue_rules.dep_state(self.db, t)
            if state == "failed" and not t.hold:  # its input failed: ask the user instead of starting blind
                self.db.update_task(t.id, hold={"kind": "dep_failed", "task": dt.id,
                                                "text": f"Waiting for you: its dependency T-{dt.id} {dt.status}"})
                touched.add(orch.id)
                continue
            if queue_rules.start_blocker(self.db, self.db.task(t.id), paused, wt_on, _git_root):
                continue
            if t.orchestration_id not in running:
                running[t.orchestration_id] = sum(1 for x in self.db.tasks(orch.id) if x.status in ("running", "waiting"))
            if running[t.orchestration_id] >= orch.max_parallel:
                continue
            if self._start_task(t):
                running[t.orchestration_id] += 1
                free -= 1
                touched.add(orch.id)
        for oid in touched:
            self.changed.emit(oid)

    def queue_positions(self) -> dict[int, int]:
        """Task id -> 1-based place in the global queue (what starts next is #1)."""
        return {t.id: i for i, t in enumerate(self.db.queued_tasks(), 1)}

    def move_task(self, tid: int, delta: int) -> None:
        """Move a queued task earlier (delta < 0) or later in the global queue."""
        q = self.db.queued_tasks()
        idx = next((i for i, t in enumerate(q) if t.id == tid), None)
        if idx is None:
            return
        j = max(0, min(len(q) - 1, idx + (1 if delta > 0 else -1)))
        if j == idx:
            return
        a, b = q[idx], q[j]
        pa = a.queue_pos if a.queue_pos is not None else a.created_at
        pb = b.queue_pos if b.queue_pos is not None else b.created_at
        if pa == pb:
            pb += 0.001 if j > idx else -0.001
        self.db.update_task(a.id, queue_pos=pb)
        self.db.update_task(b.id, queue_pos=pa)
        for oid in {a.orchestration_id, b.orchestration_id}:
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
        follow = self._retry_text.pop(t.id, None)
        crit, close = criteria_block(t.done_when), closing_for(t)
        if first:
            prompt = SUBAGENT_INTRO + t.instructions + crit + close
        elif follow:
            prompt = follow + close
        elif t.reused:  # continuing an earlier chat: it has context, send just the task
            prompt = "New task from the orchestrator:\n" + t.instructions + crit + close
        else:
            prompt = t.instructions + crit + close
        if not self.mgr.send(sid, prompt):
            self._finish(t.id, "failed", {"status": "failed", "summary": "Could not start the sub-agent "
                                          "(its model may be disabled, or the chat is busy).",
                                          "files_changed": [], "open_questions": []})
            return False
        s = self.db.session(sid)
        if s and s.title != t.title:
            self.db.update_session(sid, title=t.title)
        self.db.update_task(t.id, status="running", started_at=time.time(), finished_at=None, reviewed_at=None)
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
                if rep["status"] == "failed" and self._requeue_on_limit(t, rep):
                    return
                if rep["status"] == "failed" and self._escalate(t, rep):
                    return
                if t.escalated:
                    rep["escalated"] = t.pick_reason
                final = "failed" if rep["status"] == "failed" else "done"
                proj = self._project_of(t)
                cmd = review.check_command(self.db, proj.id) if (proj and final == "done") else ""
                if cmd or (t.worktree and final == "done"):
                    self.db.update_task(t.id, status="checking")
                    self.task_changed.emit(t.orchestration_id, t.id)
                    self._spawn(self._deliver(t.id, rep, cmd))
                else:
                    self._finish(t.id, final, rep)

    # ---- delivery: checks, then merge
    def _project_of(self, t: Task):
        orch = self.db.orchestration(t.orchestration_id)
        if not orch:
            return None
        projects = sorted(self.db.projects(), key=lambda p: -len(p.path))
        cwd = str(Path(t.cwd or "").resolve()).lower() if t.cwd and not t.worktree else ""
        hit = next((p for p in projects if cwd and cwd.startswith(str(Path(p.path).resolve()).lower())), None)
        return hit or self.db.project(orch.project_id)

    def _spawn(self, coro) -> None:
        fut = asyncio.ensure_future(coro)
        self._jobs.add(fut)
        fut.add_done_callback(self._jobs.discard)

    async def _deliver(self, tid: int, rep: dict, cmd: str) -> None:
        t = self.db.task(tid)
        if not t:
            return
        if cmd:
            where = t.worktree or t.cwd or ""
            proj = self._project_of(t)
            if not t.worktree and proj:
                where = _git_root(proj.path) or proj.path
            checks = await asyncio.to_thread(review.run_checks, cmd, where)
            self.db.update_task(tid, checks=checks)
            rep["checks"] = {k: checks[k] for k in ("ok", "cmd", "exit", "sec", "tail")}
            if not checks["ok"]:
                rep["status"] = "failed"
                rep["summary"] = f"The project's tests failed after this task (`{cmd}`, exit {checks['exit']}). " + \
                    (rep.get("summary") or "")
                self._finish(tid, "failed", rep)
                return
        t = self.db.task(tid)
        if t.worktree:
            if review.review_first(self.db):
                await asyncio.to_thread(review.commit_worktree, t)
                self.db.update_task(tid, merge_state="review",
                                    merge_note="Waiting for your review before it is merged.")
                rep["merge"] = "Waiting for the user's review before it is merged (branch " + review.branch_of(t) + ")."
            else:
                res = await self._merge(t)
                rep["merge"] = res.note
        self._finish(tid, "done", rep)

    async def _merge(self, t: Task) -> review.MergeResult:
        async with self._merge_lock:  # one merge at a time
            res = await asyncio.to_thread(review.merge, t)
        self.db.update_task(t.id, merge_state=res.state, merge_note=res.note)
        if res.state == "merged":
            self.db.update_task(t.id, worktree=None, reviewed_at=time.time())
        return res

    async def merge_task(self, tid: int) -> review.MergeResult | None:
        """User pressed Merge (Review mode, or again after a conflict)."""
        t = self.db.task(tid)
        if not t or not t.worktree or t.merge_state not in ("review", "conflict", "failed"):
            return None
        res = await self._merge(t)
        self.task_changed.emit(t.orchestration_id, tid)
        self.changed.emit(t.orchestration_id)
        concurrency.slot_freed()  # tasks that wait for this one may start now
        return res

    async def discard_task(self, tid: int) -> None:
        t = self.db.task(tid)
        if not t or not t.worktree:
            return
        res = await asyncio.to_thread(review.discard, t)
        self.db.update_task(tid, merge_state=res.state, merge_note=res.note, worktree=None,
                            reviewed_at=time.time())
        for d in queue_rules.dependents(self.db, tid):  # what built on it should not start without the user
            self.db.update_task(d.id, hold={"kind": "dep_failed", "task": tid,
                                            "text": f"Waiting for you: its dependency T-{tid} was discarded"})
        self.task_changed.emit(t.orchestration_id, tid)
        self.changed.emit(t.orchestration_id)

    def run_anyway(self, tid: int) -> None:
        """Start a held task without the dependency that failed."""
        t = self.db.task(tid)
        if not t or not t.hold:
            return
        bad = (t.hold or {}).get("task")
        self.db.update_task(tid, hold=None, depends_on=[d for d in t.depends_on if d != bad])
        self.pump(t.orchestration_id)

    # ---- usage limit pause
    def paused(self) -> dict:
        now = time.time()
        p = limits.paused(self.db, now)
        return {e: v for e, v in p.items() if self._limit_override.get(e, 0) < v.resets_at}

    def ignore_limit(self, engine: str) -> None:
        """Start tasks for `engine` anyway until this limit resets."""
        p = limits.paused(self.db).get(engine)
        if p:
            self._limit_override[engine] = p.resets_at
        self.pump_all()

    def _requeue_on_limit(self, t: Task, rep: dict) -> bool:
        """The plan limit stopped this task mid-turn: put it back in the queue instead of failing it."""
        limits.forget_cache()
        if t.engine not in self.paused():
            return False
        orch = self.db.orchestration(t.orchestration_id)
        if not orch or orch.status != "running":
            return False
        self._retry_text[t.id] = "The usage limit stopped your last attempt. Continue the task where you left off."
        self.db.update_task(t.id, status="queued", started_at=None)
        self.task_changed.emit(t.orchestration_id, t.id)
        self.changed.emit(t.orchestration_id)
        concurrency.slot_freed()
        return True

    # ---- silent tasks
    def _on_event(self, sid: int, _ev) -> None:
        self._activity[sid] = time.time()

    def silent_minutes(self, t: Task, now: float | None = None) -> int:
        """Minutes without any output for a running task, when over the setting (0 = not silent)."""
        limit = int(self.db.get_setting(SILENT_SETTING, SILENT_DEFAULT) or 0)
        if limit <= 0 or t.status != "running" or not t.started_at:
            return 0
        now = now or time.time()
        last = max(t.started_at, self._activity.get(t.session_id or 0, 0), self._snooze.get(t.id, 0))
        mins = int((now - last) // 60)
        return mins if mins >= limit else 0

    def keep_waiting(self, tid: int) -> None:
        self._snooze[tid] = time.time()
        t = self.db.task(tid)
        if t:
            self.changed.emit(t.orchestration_id)

    def _tick(self) -> None:
        self.pump_all()  # a reset limit lets paused tasks start
        silent = {t.id for t in self.db.tasks_running() if self.silent_minutes(t)}
        for tid in silent ^ self._silent:
            t = self.db.task(tid)
            if t:
                self.task_changed.emit(t.orchestration_id, tid)
                self.changed.emit(t.orchestration_id)
        self._silent = silent

    def _escalate(self, t: Task, rep: dict) -> bool:
        """An automatically picked task failed: retry once, same chat, more effort or a stronger model."""
        if t.escalated or not t.auto_picked or not self.db.get_setting("orchestrate.escalate", True):
            return False
        orch = self.db.orchestration(t.orchestration_id)
        if not orch or orch.status != "running":
            return False
        pick = routing.escalate(self.db, t.engine, t.model or "", t.effort or "")
        if not pick:
            return False
        self.db.update_task(t.id, model=pick.model, effort=pick.effort, escalated=1, pick_reason=pick.reason,
                            status="queued")
        self.db.update_session(t.session_id, model=pick.model, effort=pick.effort)
        follow = ("Your previous attempt did not succeed: " + (rep.get("summary") or "no details") +
                  "\nTry again and finish the task.")
        self.db.update_task(t.id, instructions=t.instructions + "\n\n" + follow)
        self._retry_text[t.id] = follow
        self.task_changed.emit(t.orchestration_id, t.id)
        # The failed turn is still finishing (its final status is emitted from inside it): start the
        # retry on the next loop pass, when the chat is free again.
        asyncio.get_event_loop().call_soon(self.pump, t.orchestration_id)
        return True

    def _finish(self, tid: int, status: str, result: dict) -> None:
        t = self.db.task(tid)
        if not t:
            return
        self.db.update_task(tid, status=status, result=result, finished_at=time.time())
        ev = self._events.setdefault(tid, asyncio.Event())
        ev.set()
        self.task_changed.emit(t.orchestration_id, tid)
        concurrency.slot_freed()  # queued tasks here or elsewhere, and waiting scheduled runs, may start now
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
            "pick_reason": t.pick_reason,
        }
        if t.reused:
            info["reused"] = True
        if t.escalated:
            info["escalated"] = True
        if t.status == "proposed":
            info["note"] = "Waiting for the user to confirm on the board."
        if t.status == "queued":
            info["waiting"] = queue_rules.why_waiting(self.db, t, self.paused())
        if t.status == "checking":
            info["note"] = "Finished; the app is running the project's tests / merging."
        if t.depends_on:
            info["after"] = t.depends_on
        if t.done_when:
            info["done_when"] = t.done_when
        if t.merge_state:
            info["merge"] = {"state": t.merge_state, "note": t.merge_note}
        if self.silent_minutes(t):
            info["note"] = f"No output for {self.silent_minutes(t)} minutes."
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
        if not self.mgr.send(sid, text.strip() + closing_for(t)):
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
        if t.status in ("queued", "proposed"):
            self.db.update_task(tid, hold=None)
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
                            worktree=None, checks=None, merge_state=None, merge_note=None, hold=None)
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


def _as_list(v: Any) -> list:
    if v is None or v == "":
        return []
    if isinstance(v, (list, tuple)):
        return list(v)
    return [x for x in re.split(r"[,;\n]", str(v)) if x.strip()]
