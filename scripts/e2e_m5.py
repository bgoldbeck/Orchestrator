"""Real end-to-end for M5 (cheap models; uses the user's engine logins).

    .venv\\Scripts\\python scripts\\e2e_m5.py claude|codex     # planner engine

1. Seeds a real chat (Codex GPT-6 Luna) that creates notes.txt = "alpha", then summarizes it with the
   cheap model (real call).
2. Runs an orchestration whose planner must find that chat with find_sessions and continue it
   (reuse card auto-accepts after the countdown), delegate a second task with no model (Auto pick),
   and ask the user a question (answered by this script).
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
GOAL = ("Two things, in parallel:\n"
        "A) notes.txt must contain exactly 'alpha beta'. An earlier chat created notes.txt: call find_sessions "
        "first and continue that chat by passing its session_ref as resume_session_id to delegate.\n"
        "B) Create readme.txt containing exactly 'docs'. For this task leave engine, model and effort empty so the "
        "app picks them.\n"
        "When both reports are in, call ask_user with the question 'Ship it?' and options yes and no, then finish "
        "with a two-line summary that includes the answer.")


def main() -> int:
    which = sys.argv[1] if len(sys.argv) > 1 else "claude"
    home = Path(tempfile.mkdtemp(prefix="orch-e2e5-"))
    base = Path(os.environ.get("ORCH_E2E_ROOT", r"D:\OrchestratorE2E"))
    base.mkdir(parents=True, exist_ok=True)
    proj_dir = base / f"m5-{which}-{os.getpid()}"
    proj_dir.mkdir()  # plain mkdir: mkdtemp on Windows sets an owner-only ACL
    subprocess.run(["git", "init", "-q", str(proj_dir)], check=True)
    os.environ["ORCHESTRATOR_HOME"] = str(home)
    os.environ["ORCHESTRATOR_INDEX_CLAUDE"] = str(home / "none")
    os.environ["ORCHESTRATOR_INDEX_CODEX"] = str(home / "none")

    import qasync
    from PySide6.QtWidgets import QApplication

    from orchestrator.core import orchestration
    from orchestrator.core.orchestration import OrchestrationManager
    from orchestrator.core.session_manager import SessionManager
    from orchestrator.core.summaries import Summarizer
    from orchestrator.storage.db import Database

    app = QApplication(sys.argv)
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    db = Database()
    db.set_setting("summaries.mode", "model")
    db.set_setting("orchestrate.confirm", "countdown")
    orchestration.COUNTDOWN_SEC = 4
    proj = db.add_project(str(proj_dir), "e2e5")
    mgr = SessionManager(db)
    om = OrchestrationManager(db, mgr)
    sm = Summarizer(db, mgr, delay_ms=10_000_000)  # summarize explicitly below
    mgr.approval_requested.connect(lambda sid, req, fut: fut.set_result(True))  # stands in for "Allow"
    answered: list[str] = []

    def on_question(oid, qid):
        q = db.question(qid)
        answered.append(q.text)
        loop.call_later(1.0, lambda: om.answer(qid, "yes"))

    om.question_asked.connect(on_question)

    def read(name):
        p = proj_dir / name
        try:
            return p.read_text(encoding="utf-8-sig").strip() if p.exists() else None
        except OSError as exc:
            return f"<unreadable: {exc}>"

    async def run() -> int:
        t0 = time.time()
        seed = db.create_session(proj.id, "codex", "gpt-6-luna", "low")
        mgr.send(seed.id, "Create a file notes.txt in this folder containing exactly the word alpha. Nothing else.")
        while mgr.is_running(seed.id) or db.session(seed.id).status in ("idle", "running"):
            await asyncio.sleep(1)
            if time.time() - t0 > 240:
                break
        print(f"seed chat: {db.session(seed.id).status}, notes.txt={read('notes.txt')!r}, "
              f"engine id={db.session(seed.id).engine_session_id}")
        await sm.summarize(seed.id)
        s = db.session(seed.id)
        print("summary:", (s.summary or "").replace("\n", " | ")[:300])
        print("keywords:", s.keywords)

        o = om.start(proj.id, GOAL, *PLANNERS[which], max_parallel=4)
        t1 = time.time()
        last = ""
        while time.time() - t1 < 900:
            await asyncio.sleep(2)
            orch = db.orchestration(o.id)
            snap = f"orch={orch.status} " + " ".join(f"t{t.id}={t.status}" for t in db.tasks(o.id))
            if snap != last:
                print(f"[{time.time() - t1:5.0f}s] {snap}", flush=True)
                last = snap
            if orch.status != "running" and not mgr.is_running(o.session_id):
                break
        orch = db.orchestration(o.id)
        tasks = db.tasks(o.id)
        tools = [m.get("name") for m in db.messages(o.session_id) if m["kind"] == "tool"]
        print(f"orchestration: {orch.status} after {time.time() - t1:.0f}s; planner tools: {tools}")
        for t in tasks:
            r = t.result or {}
            print(f"  task {t.id} [{t.engine} {t.model} {t.effort}] {t.status} reused={t.reused} auto={t.auto_picked}"
                  f" session={t.session_id} reason={t.pick_reason!r}")
            print(f"     report: {r.get('status')} {r.get('files_changed')} {(r.get('summary') or '')[:120]!r}")
        final = [m["text"] for m in db.messages(o.session_id) if m["role"] == "assistant" and m["kind"] == "text"]
        print("planner final:", (final[-1] if final else "")[:300].replace("\n", " | "))
        print("questions asked:", answered)
        notes, readme = read("notes.txt"), read("readme.txt")
        print("files:", {"notes.txt": notes, "readme.txt": readme})
        reused = [t for t in tasks if t.reused and t.session_id == seed.id]
        auto = [t for t in tasks if t.auto_picked and (t.pick_reason or "").startswith(("Auto:", "Escalated"))]
        checks = {
            "summary written": bool(s.summary and s.keywords),
            "find_sessions called": any(n and "find_sessions" in n for n in tools),
            "reused seed chat": bool(reused),
            "auto-picked task with reason": bool(auto),
            "ask_user answered": bool(answered) and any(n and "ask_user" in n for n in tools),
            "notes.txt": notes == "alpha beta",
            "readme.txt": readme == "docs",
            "orchestration done": orch.status == "done",
        }
        for k, v in checks.items():
            print(f"  {'ok ' if v else 'BAD'} {k}")
        ok = all(checks.values())
        print("E2E", "PASS" if ok else "FAIL")
        om.bridge.stop()
        return 0 if ok else 1

    with loop:
        return loop.run_until_complete(run())


if __name__ == "__main__":
    raise SystemExit(main())
