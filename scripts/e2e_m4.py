"""Real end-to-end orchestration with both engines (cheap models). Uses the user's engine logins.

    .venv\\Scripts\\python scripts\\e2e_m4.py claude     # planner on Claude Haiku
    .venv\\Scripts\\python scripts\\e2e_m4.py codex      # planner on Codex GPT-6 Luna

The goal makes the planner delegate one task to each engine. Checks: both files written,
both tasks done with parsed reports, the planner finished with a summary.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PLANNERS = {"claude": ("claude", "claude-haiku-5-5", "low"), "codex": ("codex", "gpt-6-luna", "low")}
GOAL = ("Create two small files in the project folder, one per sub-agent, in parallel:\n"
        "1) hello.txt containing exactly the word hello - delegate this to engine codex, model gpt-6-luna, effort low.\n"
        "2) world.txt containing exactly the word world - delegate this to engine claude, model claude-haiku-5-5, "
        "effort low.\nWait for both reports, then summarize in two lines.")


def main() -> int:
    which = sys.argv[1] if len(sys.argv) > 1 else "claude"
    home = Path(tempfile.mkdtemp(prefix="orch-e2e-"))
    # Not under %TEMP% and not via mkdtemp: both give owner-only ACLs, and files the Codex sandbox
    # user creates there are then unreadable for the app.
    base = Path(os.environ.get("ORCH_E2E_ROOT", r"D:\OrchestratorE2E"))
    base.mkdir(parents=True, exist_ok=True)
    proj_dir = base / f"proj-{os.getpid()}-{int(time.time())}"
    proj_dir.mkdir()  # plain mkdir: mkdtemp on Windows sets an owner-only ACL
    subprocess.run(["git", "init", "-q", str(proj_dir)], check=True)
    os.environ["ORCHESTRATOR_HOME"] = str(home)
    os.environ["ORCHESTRATOR_INDEX_CLAUDE"] = str(home / "none")
    os.environ["ORCHESTRATOR_INDEX_CODEX"] = str(home / "none")

    import qasync
    from PySide6.QtWidgets import QApplication

    from orchestrator.core.orchestration import OrchestrationManager
    from orchestrator.core.session_manager import SessionManager
    from orchestrator.storage.db import Database

    app = QApplication(sys.argv)
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    db = Database()
    proj = db.add_project(str(proj_dir), "e2e")
    mgr = SessionManager(db)
    om = OrchestrationManager(db, mgr)
    approvals: list[str] = []

    def on_approval(sid, req, fut):  # stands in for the user clicking Allow
        approvals.append(f"{db.session(sid).title}: {req.tool} {req.detail}")
        fut.set_result(True)

    mgr.approval_requested.connect(on_approval)
    events: list[str] = []
    mgr.event.connect(lambda sid, ev: events.append(f"{sid}:{type(ev).__name__}:{getattr(ev, 'name', '')}")
                      if type(ev).__name__ in ("ToolCall", "Error") else None)

    async def run() -> int:
        t0 = time.time()
        o = om.start(proj.id, GOAL, *PLANNERS[which], max_parallel=4)
        print(f"planner {which}: session {o.session_id}, orchestration {o.id}")
        last = ""
        while time.time() - t0 < 900:
            await asyncio.sleep(2)
            orch = db.orchestration(o.id)
            snap = f"orch={orch.status} planner={'run' if mgr.is_running(o.session_id) else 'idle'} " + " ".join(
                f"t{t.id}={t.status}/{'run' if t.session_id and mgr.is_running(t.session_id) else 'idle'}"
                for t in db.tasks(o.id))
            if snap != last:
                print(f"[{time.time() - t0:5.0f}s] {snap}", flush=True)
                last = snap
            if orch.status != "running" and not mgr.is_running(o.session_id):
                break
        orch = db.orchestration(o.id)
        tasks = db.tasks(o.id)
        print(f"orchestration status: {orch.status} after {time.time() - t0:.0f}s")
        for t in tasks:
            r = t.result or {}
            print(f"  task {t.id} [{t.engine} {t.model} {t.effort}] {t.status}: {t.title}")
            print(f"     report: status={r.get('status')} files={r.get('files_changed')} "
                  f"missing={r.get('report_missing', False)} summary={(r.get('summary') or '')[:120]!r}")
        planner_tools = [m.get("name") for m in db.messages(o.session_id) if m["kind"] == "tool"]
        print("planner tool calls:", planner_tools)
        final = [m["text"] for m in db.messages(o.session_id) if m["role"] == "assistant" and m["kind"] == "text"]
        print("planner final:", (final[-1] if final else "")[:400].replace("\n", " | "))
        errs = [m["text"] for m in db.messages(o.session_id) if m["kind"] == "error"]
        if errs:
            print("planner errors:", errs[-3:])
        print("approvals answered:", approvals)
        for t in tasks:
            s = db.session(t.session_id)
            print(f"  -- task {t.id} session {s.id} status={s.status} engine_id={s.engine_session_id}")
            for m in db.messages(s.id)[-6:]:
                print("     ", m["role"], m["kind"], str(m.get("text") or m.get("name") or m.get("output") or "")[:200]
                      .replace("\n", " | "))
        for m in db.messages(o.session_id)[-8:]:
            print("  planner:", m["role"], m["kind"], str(m.get("text") or m.get("name") or "")[:160].replace("\n", " | "),
                  str(m.get("output") or "")[:200].replace("\n", " | "))

        def read(name):
            p = proj_dir / name
            try:
                return p.read_text(encoding="utf-8-sig").strip() if p.exists() else None
            except OSError as exc:
                return f"<unreadable: {exc}>"
        hello, world = read("hello.txt"), read("world.txt")
        print("files:", {"hello.txt": hello, "world.txt": world})
        engines = {t.engine for t in tasks if t.status == "done"}
        ok = (orch.status == "done" and hello == "hello" and world == "world" and engines == {"claude", "codex"}
              and all(t.result and not t.result.get("report_missing") for t in tasks)
              and any(n and "delegate" in n for n in planner_tools) and any(n and "wait_for" in n for n in planner_tools))
        print("E2E", "PASS" if ok else "FAIL")
        om.bridge.stop()
        return 0 if ok else 1

    with loop:
        return loop.run_until_complete(run())


if __name__ == "__main__":
    raise SystemExit(main())
