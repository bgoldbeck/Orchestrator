"""M4 orchestrator core: tools, queueing, reports, bridge (fake engines; no network)."""
import asyncio
from pathlib import Path
import json
import os

import pytest
from PySide6.QtCore import QObject, Signal

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from orchestrator.core.orchestration import (  # noqa: E402
    CLOSING,
    OrchestrationError,
    OrchestrationManager,
    build_report,
    parse_report,
)
from orchestrator.storage.db import Database  # noqa: E402


class FakeMgr(QObject):
    status_changed = Signal(int, str)
    session_updated = Signal(int)

    def __init__(self, db):
        super().__init__()
        self.db = db
        self.sent = []
        self.busy = set()
        self.run_options = {}
        self.mcp_provider = None

    def is_running(self, sid):
        return sid in self.busy

    def send(self, sid, text, policy=None):
        if sid in self.busy:
            return False
        self.sent.append((sid, text))
        self.db.add_message(sid, "user", "text", {"text": text})
        self.busy.add(sid)
        self.db.update_session(sid, status="running")
        self.status_changed.emit(sid, "running")
        return True

    def finish(self, sid, text, status="done"):
        # Like SessionManager: the final status is emitted while the turn task is still running.
        self.db.add_message(sid, "assistant", "text", {"text": text})
        self.db.update_session(sid, status=status)
        self.status_changed.emit(sid, status)
        self.busy.discard(sid)

    async def stop(self, sid):
        self.db.update_session(sid, status="idle")
        self.status_changed.emit(sid, "idle")
        self.busy.discard(sid)

    def live_text(self, sid):
        return ""

    def live_tools(self, sid):
        return []


REPORT = """All done.

```report
status: done
summary: Added the greeting function.
files_changed: hello.py, tests/test_hello.py
open_questions: none
```"""


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "t.db")
    proj = db.add_project(str(tmp_path), "proj")
    (tmp_path / "sub").mkdir()
    mgr = FakeMgr(db)
    om = OrchestrationManager(db, mgr)
    return db, proj, mgr, om


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def loop():
    lp = asyncio.new_event_loop()
    asyncio.set_event_loop(lp)
    yield lp
    lp.close()


def test_parse_report_variants():
    r = parse_report(REPORT)
    assert r == {"status": "done", "summary": "Added the greeting function.",
                 "files_changed": ["hello.py", "tests/test_hello.py"], "open_questions": []}
    loose = parse_report("Text\nstatus: partial\nsummary: half\nfiles_changed: none\nopen_questions: which db?")
    assert loose["status"] == "partial" and loose["open_questions"] == ["which db?"]
    assert parse_report("no report here") is None
    assert parse_report("```report\nstatus: done | partial | failed\nsummary: x\n```")["status"] == "done"


def test_start_creates_planner_with_mcp_and_prompt(env):
    db, proj, mgr, om = env
    o = om.start(proj.id, "Add a hello script", "codex", "gpt-6-luna", "low", max_parallel=2)
    assert o.session_id and mgr.sent[0][0] == o.session_id
    prompt = mgr.sent[0][1]
    assert "Add a hello script" in prompt and "gpt-6.2-sol" in prompt and "up to 2" in prompt.lower()
    cfg = om.mcp_for(o.session_id)["orchestrator"]
    assert cfg["args"] == ["-m", "orchestrator.mcp_server"] and cfg["env"]["ORCH_ORCHESTRATION_ID"] == str(o.id)
    assert om.mcp_for(999) is None
    assert mgr.run_options[o.session_id]["sandbox"] == "read-only"
    om.bridge.stop()


def test_start_refuses_disabled_model(env):
    db, proj, mgr, om = env
    db.set_model_enabled("codex", "gpt-6-luna", False)
    with pytest.raises(OrchestrationError, match="disabled"):
        om.start(proj.id, "goal", "codex", "gpt-6-luna", "low")


def test_delegate_validation(env):
    db, proj, mgr, om = env
    o = om.start(proj.id, "g", "claude", "claude-haiku-5-5", "low")
    with pytest.raises(OrchestrationError, match="Unknown engine"):
        om.delegate(o.id, "x", engine="gpt")
    db.set_model_enabled("claude", "claude-opus-5-5", False)
    with pytest.raises(OrchestrationError, match="disabled"):
        om.delegate(o.id, "x", engine="claude", model="claude-opus-5-5")
    with pytest.raises(OrchestrationError, match="Unknown model"):
        om.delegate(o.id, "x", engine="codex", model="gpt-99")
    with pytest.raises(OrchestrationError, match="Effort"):
        om.delegate(o.id, "x", engine="claude", model="claude-haiku-5-5", effort="max")
    with pytest.raises(OrchestrationError, match="inside the project"):
        om.delegate(o.id, "x", cwd="..")
    r = om.delegate(o.id, "write hello.py", cwd="sub", title="Hello")
    assert r["engine"] == "claude" and r["model"] and r["status"] == "running"
    t = db.task(r["task_id"])
    assert t.cwd.endswith("sub") and db.session(t.session_id).parent_id == o.session_id
    sub_prompt = mgr.sent[-1][1]
    assert sub_prompt.startswith("You are a sub-agent") and sub_prompt.endswith(CLOSING)
    assert mgr.run_options[t.session_id]["claude_mode"] == "acceptEdits"
    db.set_model_enabled("claude", None, False)  # whole engine off: no fallback to another model
    with pytest.raises(OrchestrationError, match="no enabled models"):
        om.delegate(o.id, "x", engine="claude")


def test_queue_wait_report_and_followup(env):
    db, proj, mgr, om = env
    o = om.start(proj.id, "g", "codex", "gpt-6-luna", "low", max_parallel=1)
    a = om.delegate(o.id, "task a", title="A")["task_id"]
    b = om.delegate(o.id, "task b", title="B")["task_id"]
    assert db.task(a).status == "running" and db.task(b).status == "queued"  # max parallel 1
    part = run(om.wait(o.id, [a, b], 0.05))
    assert part["done"] is False and set(part["pending"]) == {a, b}
    mgr.finish(db.task(a).session_id, REPORT)
    assert db.task(a).status == "done" and db.task(b).status == "running"  # queue moves on
    mgr.finish(db.task(b).session_id, "I could not do it, no report")  # missing report: fallback
    res = run(om.wait(o.id, [a, b], 5))
    assert res["done"] is True
    ra, rb = (t["report"] for t in res["tasks"])
    assert ra["files_changed"] == ["hello.py", "tests/test_hello.py"] and ra["summary"].startswith("Added")
    assert rb["report_missing"] and "could not" in rb["summary"]
    # follow-up continues the same chat and resets the report
    r = om.send_to_task(o.id, a, "also add a docstring")
    assert r["status"] == "running" and mgr.sent[-1][0] == db.task(a).session_id
    with pytest.raises(OrchestrationError, match="still running"):
        om.send_to_task(o.id, a, "again")
    mgr.finish(db.task(a).session_id, REPORT.replace("Added the greeting function.", "Docstring added."))
    assert db.task(a).result["summary"] == "Docstring added."
    with pytest.raises(OrchestrationError, match="No task"):
        run(om.handle(o.id, "status", {"task_id": 999}))


def test_wait_unblocks_when_task_finishes(env, loop):
    db, proj, mgr, om = env
    o = om.start(proj.id, "g", "codex", "gpt-6-luna", "low")
    a = om.delegate(o.id, "task a")["task_id"]
    loop.call_later(0.05, lambda: mgr.finish(db.task(a).session_id, REPORT))
    res = run(om.wait(o.id, [a], 10))
    assert res["done"] and res["tasks"][0]["status"] == "done"


def test_stop_retry_and_stop_all(env):
    db, proj, mgr, om = env
    o = om.start(proj.id, "g", "codex", "gpt-6-luna", "low", max_parallel=1)
    a = om.delegate(o.id, "task a")["task_id"]
    b = om.delegate(o.id, "task b")["task_id"]
    run(om.stop_task(a))
    assert db.task(a).status == "stopped" and db.task(b).status == "running"
    old = db.task(a).session_id
    om.retry_task(a)
    assert db.task(a).session_id != old and db.task(a).status == "queued"
    run(om.stop_all(o.id))
    assert db.orchestration(o.id).status == "stopped"
    assert {db.task(a).status, db.task(b).status} == {"stopped"}
    with pytest.raises(OrchestrationError, match="stopped"):
        om.delegate(o.id, "more")


def test_planner_done_marks_orchestration_done(env):
    db, proj, mgr, om = env
    o = om.start(proj.id, "g", "codex", "gpt-6-luna", "low")
    a = om.delegate(o.id, "task a")["task_id"]
    mgr.finish(db.task(a).session_id, REPORT)
    mgr.finish(o.session_id, "Summary for the user.")
    assert db.orchestration(o.id).status == "done"


def test_bridge_roundtrip_through_mcp_client(env, loop, monkeypatch):
    from orchestrator import mcp_server

    db, proj, mgr, om = env
    o = om.start(proj.id, "g", "codex", "gpt-6-luna", "low")
    cfg = om.mcp_for(o.session_id)["orchestrator"]["env"]
    for k, v in cfg.items():
        monkeypatch.setenv(k, v)
    res = run(mcp_server.bridge_call("delegate", {"instructions": "hi", "title": "Hi"}, timeout=10))
    assert res["task_id"] and res["status"] == "running"
    st = run(mcp_server.bridge_call("status", {"task_id": 0}, timeout=10))
    assert [t["title"] for t in st["tasks"]] == ["Hi"]
    monkeypatch.setenv("ORCH_TOKEN", "wrong")
    with pytest.raises(RuntimeError, match="bad token"):
        run(mcp_server.bridge_call("status", {}, timeout=10))
    om.bridge.stop()


def test_mcp_server_exposes_tools():
    from orchestrator.mcp_server import build_server

    async def names():
        return sorted(t.name for t in await build_server().list_tools())

    assert asyncio.get_event_loop().run_until_complete(names()) == ["ask_user", "delegate", "find_projects", "find_sessions", "send_to_task", "suggest_preference",
                                                                     "task_status", "wait_for"]


def test_engine_mcp_config_shapes():
    from orchestrator.engines.base import TurnRequest
    from orchestrator.engines.claude import claude_mcp_options
    from orchestrator.engines.codex import build_codex_cmd

    mcp = {"orchestrator": {"command": r"C:\py\pythonw.exe", "args": ["-m", "orchestrator.mcp_server"],
                            "env": {"ORCH_BRIDGE": "127.0.0.1:5"}, "tool_timeout_sec": 600}}
    servers, env, allowed = claude_mcp_options(mcp)
    assert servers["orchestrator"]["type"] == "stdio" and env == {"MCP_TOOL_TIMEOUT": "600000"}
    assert allowed == ["mcp__orchestrator"]
    from pathlib import Path

    cmd = build_codex_cmd("codex", TurnRequest("p", Path("."), "m", "low", mcp=mcp))
    joined = " ".join(cmd)
    assert r"mcp_servers.orchestrator.command='C:\py\pythonw.exe'" in joined
    assert "mcp_servers.orchestrator.default_tools_approval_mode='approve'" in joined
    assert "mcp_servers.orchestrator.tool_timeout_sec=600" in joined


def test_build_report_collects_edited_files(tmp_path):
    db = Database(tmp_path / "t.db")
    p = db.add_project(str(tmp_path), "p")
    s = db.create_session(p.id, "codex", "m", "low")
    db.add_message(s.id, "user", "text", {"text": "go"})
    db.add_message(s.id, "tool", "tool", {"name": "edit", "summary": "add a.py", "output": "add: a.py", "is_error": False})
    db.add_message(s.id, "assistant", "text", {"text": "```report\nstatus: done\nsummary: ok\nfiles_changed: none\n```"})
    rep = build_report(db, s.id, "done")
    assert rep["files_changed"] == ["a.py"] and json.dumps(rep)


def test_worktree_per_task_when_two_write_same_repo(tmp_path, monkeypatch):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in (["init", "-q"], ["config", "user.email", "t@example.invalid"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True)
    (repo / "a.txt").write_text("a")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    db = Database(tmp_path / "t.db")
    proj = db.add_project(str(repo), "repo")
    mgr = FakeMgr(db)
    om = OrchestrationManager(db, mgr)
    o = om.start(proj.id, "g", "codex", "gpt-6-luna", "low", max_parallel=2)
    first = om.delegate(o.id, "one")["task_id"]
    assert db.task(first).worktree is None  # setting off by default
    db.set_setting("orchestrate.worktrees", True)
    second = om.delegate(o.id, "two")["task_id"]
    t2 = db.task(second)
    assert t2.worktree and (Path(t2.worktree) / "a.txt").exists()
    assert db.session(t2.session_id).cwd == t2.cwd and om.task_info(t2)["branch"] == f"orchestrator/task-{second}"
    listed = subprocess.run(["git", "-C", str(repo), "worktree", "list"], capture_output=True, text=True).stdout
    assert Path(t2.worktree).name in listed
    om.bridge.stop()
