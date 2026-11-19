"""Queue rules (dependencies, shared files, usage-limit pause, silent), checks, worktree merge/review, inbox."""
import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from orchestrator.core import inbox, limits, queue_rules, review, usage  # noqa: E402
from orchestrator.core.orchestration import OrchestrationError, OrchestrationManager, parse_report  # noqa: E402
from orchestrator.storage.db import Database  # noqa: E402
from tests.test_orchestration import REPORT, FakeMgr  # noqa: E402

PY = f'"{sys.executable}"'


@pytest.fixture(autouse=True)
def loop():
    lp = asyncio.new_event_loop()
    asyncio.set_event_loop(lp)
    limits.forget_cache()
    yield lp
    lp.close()


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def settle(om, tid, timeout=20.0):
    """Let background delivery (checks / merge on a worker thread) finish."""
    end = time.time() + timeout
    while time.time() < end and om.db.task(tid).status == "checking":
        run(asyncio.sleep(0.05))
    return om.db.task(tid)


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "t.db")
    db.set_setting("orchestrate.confirm", "never")
    db.set_setting("global.max_parallel", 8)
    db.set_setting(usage.CLAUDE_KEY, {})
    proj = db.add_project(str(tmp_path), "proj")
    mgr = FakeMgr(db)
    om = OrchestrationManager(db, mgr)
    o = om.start(proj.id, "goal", "claude", "claude-haiku-5-5", "low", max_parallel=4)  # planner stays running
    yield db, proj, mgr, om, o
    om.bridge.stop()


def delegate(om, o, text, **kw):
    kw.setdefault("engine", "claude")
    kw.setdefault("model", "claude-haiku-5-5")
    return om.delegate(o.id, text, **kw)["task_id"]


def finish(mgr, db, tid, text=REPORT, status="done"):
    mgr.finish(db.task(tid).session_id, text, status)


# ------------------------------------------------------------------ dependencies
def test_after_waits_then_starts(env):
    db, proj, mgr, om, o = env
    a = delegate(om, o, "build api")
    res = om.delegate(o.id, "build ui", engine="claude", model="claude-haiku-5-5", after=[a])
    b = res["task_id"]
    assert db.task(b).status == "queued" and db.task(b).depends_on == [a]
    assert f"T-{a}" in res["waiting"]
    assert queue_rules.why_waiting(db, db.task(b), {}) == f"Waiting for T-{a} to finish"
    finish(mgr, db, a)
    assert db.task(a).status == "done" and db.task(b).status == "running"


def test_after_rejects_foreign_task(env):
    db, proj, mgr, om, o = env
    with pytest.raises(OrchestrationError, match="No task"):
        om.delegate(o.id, "x", engine="claude", model="claude-haiku-5-5", after=[999])


def test_failed_dependency_holds_and_run_anyway(env):
    db, proj, mgr, om, o = env
    a = delegate(om, o, "build api")
    b = delegate(om, o, "build ui", after=[a])
    finish(mgr, db, a, "```report\nstatus: failed\nsummary: broke\n```", "done")
    t = db.task(b)
    assert t.status == "queued" and t.hold and t.hold["kind"] == "dep_failed"
    items = inbox.build(db, om)
    held = next(i for i in items if i.kind == "held")
    assert held.task_id == b and held.recommended.id == "stop"
    run(inbox.perform(db, om, held, "run_anyway"))
    assert db.task(b).status == "running" and db.task(b).hold is None and db.task(b).depends_on == []


# ------------------------------------------------------------------ shared files
def test_shared_files_never_run_together(env):
    db, proj, mgr, om, o = env
    a = delegate(om, o, "edit login", files=["src/login.py"])
    b = delegate(om, o, "edit login tests", files=["./src/login.py", "tests/x.py"])
    c = delegate(om, o, "other", files=["src/other.py"])
    assert db.task(a).status == "running" and db.task(c).status == "running"
    assert db.task(b).status == "queued"
    assert "src/login.py" in queue_rules.why_waiting(db, db.task(b), {})
    finish(mgr, db, a)
    assert db.task(b).status == "running"


# ------------------------------------------------------------------ usage limit
def test_limit_pauses_and_ignore(env):
    db, proj, mgr, om, o = env
    db.set_setting(usage.CLAUDE_KEY, {"five_hour": {"status": "rejected", "resets_at": time.time() + 3600,
                                                    "at": time.time()}})
    t = delegate(om, o, "work")
    assert db.task(t).status == "queued"
    assert "5-hour limit reached" in queue_rules.why_waiting(db, db.task(t), om.paused())
    item = next(i for i in inbox.build(db, om) if i.kind == "paused")
    assert item.engine == "claude" and item.unblocks == 1
    run(inbox.perform(db, om, item, "ignore_limit"))
    assert db.task(t).status == "running"


def test_limit_off_switch(env):
    db, proj, mgr, om, o = env
    db.set_setting(usage.CLAUDE_KEY, {"five_hour": {"status": "rejected", "resets_at": time.time() + 60}})
    db.set_setting(limits.SETTING, False)
    assert delegate(om, o, "work") and om.paused() == {}


def test_task_stopped_by_limit_is_requeued(env):
    db, proj, mgr, om, o = env
    t = delegate(om, o, "work")
    db.set_setting(usage.CLAUDE_KEY, {"five_hour": {"status": "rejected", "resets_at": time.time() + 3600}})
    finish(mgr, db, t, "hit the limit", "error")
    assert db.task(t).status == "queued"
    db.set_setting(usage.CLAUDE_KEY, {})
    om._tick()
    assert db.task(t).status == "running" and "usage limit stopped" in mgr.sent[-1][1]


def test_codex_limit_from_logs():
    raw = {"primary": {"used_percent": 100, "resets_at": time.time() + 600}, "secondary": {"used_percent": 20}}
    p = limits._codex(time.time(), reader=lambda: (raw, time.time()))
    assert p and p.engine == "codex" and p.window == "5-hour"
    raw["primary"]["used_percent"] = 99
    assert limits._codex(time.time(), reader=lambda: (raw, time.time())) is None


# ------------------------------------------------------------------ silent
def test_silent_after_minutes(env):
    db, proj, mgr, om, o = env
    t = delegate(om, o, "slow")
    db.update_task(t, started_at=time.time() - 30 * 60)
    assert om.silent_minutes(db.task(t)) >= 20
    assert any(i.kind == "silent" and i.task_id == t for i in inbox.build(db, om))
    om._on_event(db.task(t).session_id, object())  # output arrived
    assert om.silent_minutes(db.task(t)) == 0
    db.set_setting("orchestrate.silent_min", 0)
    om._activity.clear()
    assert om.silent_minutes(db.task(t)) == 0


# ------------------------------------------------------------------ report, criteria
def test_report_assumptions_and_criteria(env):
    db, proj, mgr, om, o = env
    rep = parse_report("```report\nstatus: done\nsummary: ok\nfiles_changed: a.py\nassumptions: used UTC; kept API\n"
                       "criteria: 1 met; 2 not met: no test\nopen_questions: none\n```")
    assert rep["assumptions"] == ["used UTC", "kept API"] and rep["criteria"].startswith("1 met")
    t = delegate(om, o, "do it", done_when=["tests pass", "README updated"])
    prompt = mgr.sent[-1][1]
    assert "Done when (fixed" in prompt and "2. README updated" in prompt and "criteria:" in prompt
    assert "assumptions:" in prompt
    with pytest.raises(OrchestrationError, match="at most"):
        delegate(om, o, "x", done_when=[str(i) for i in range(11)])
    assert om.task_info(db.task(t))["done_when"] == ["tests pass", "README updated"]


# ------------------------------------------------------------------ checks
def test_checks_pass_and_fail(env):
    db, proj, mgr, om, o = env
    review.set_check_command(db, proj.id, f'{PY} -c "print(42)"')
    a = delegate(om, o, "good")
    finish(mgr, db, a)
    t = settle(om, a)
    assert t.status == "done" and t.checks["ok"] and "42" in t.checks["tail"] and t.result["checks"]["ok"]
    review.set_check_command(db, proj.id, f'{PY} -c "import sys; print(\'boom\'); sys.exit(3)"')
    b = delegate(om, o, "bad")
    finish(mgr, db, b)
    t = settle(om, b)
    assert t.status == "failed" and t.checks["exit"] == 3 and "tests failed" in t.result["summary"]


# ------------------------------------------------------------------ worktree merge
def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@t")
    git(r, "config", "user.name", "t")
    (r / "a.txt").write_text("one\n")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "init")
    return r


def _worktree_task(db, o, repo, tid_title="change"):
    t = db.add_task(o.id, "claude", "claude-haiku-5-5", "low", tid_title, tid_title, str(repo))
    dest = repo.parent / f"wt{t.id}"
    git(repo, "worktree", "add", "-q", "-b", f"orchestrator/task-{t.id}", str(dest))
    db.update_task(t.id, worktree=str(dest), cwd=str(dest), status="done")
    return db.task(t.id), dest


def test_merge_and_cleanup(env, repo):
    db, proj, mgr, om, o = env
    t, wt = _worktree_task(db, o, repo)
    (wt / "b.txt").write_text("new\n")
    res = review.merge(t)
    assert res.state == "merged", res.note
    assert (repo / "b.txt").read_text() == "new\n"
    assert not wt.exists() and "orchestrator/task" not in git(repo, "branch")


def test_merge_conflict_leaves_checkout(env, repo):
    db, proj, mgr, om, o = env
    t, wt = _worktree_task(db, o, repo)
    (wt / "a.txt").write_text("theirs\n")
    (repo / "a.txt").write_text("ours\n")
    git(repo, "commit", "-qam", "ours")
    res = review.merge(t)
    assert res.state == "conflict"
    assert (repo / "a.txt").read_text() == "ours\n" and not (Path(repo) / ".git" / "MERGE_HEAD").exists()
    assert wt.exists()


def test_review_mode_waits_then_merges(env, repo):
    db, proj, mgr, om, o = env
    db.set_setting(review.REVIEW_SETTING, True)
    t, wt = _worktree_task(db, o, repo)
    db.update_task(t.id, status="checking")
    (wt / "c.txt").write_text("x\n")
    run(om._deliver(t.id, {"status": "done", "summary": "ok", "assumptions": ["kept names"],
                           "files_changed": [], "open_questions": []}, ""))
    t = db.task(t.id)
    assert t.status == "done" and t.merge_state == "review" and not (repo / "c.txt").exists()
    item = next(i for i in inbox.build(db, om) if i.kind == "review")
    assert item.recommended.id == "merge" and "kept names" in item.detail
    from orchestrator.core import board as B

    assert B.column_of(t)[0] == "review"
    run(inbox.perform(db, om, item, "merge"))
    t = db.task(t.id)
    assert t.merge_state == "merged" and (repo / "c.txt").exists() and t.worktree is None


def test_auto_merge_when_review_off(env, repo):
    db, proj, mgr, om, o = env
    t, wt = _worktree_task(db, o, repo)
    db.update_task(t.id, status="checking")
    (wt / "d.txt").write_text("x\n")
    run(om._deliver(t.id, {"status": "done", "summary": "ok", "files_changed": [], "open_questions": []}, ""))
    t = db.task(t.id)
    assert t.merge_state == "merged" and (repo / "d.txt").exists() and "Merged into main" in t.result["merge"]


def test_discard_holds_dependents(env, repo):
    db, proj, mgr, om, o = env
    t, wt = _worktree_task(db, o, repo)
    db.update_task(t.id, merge_state="review")
    child = delegate(om, o, "next", after=[t.id])
    assert db.task(child).status == "queued"
    run(om.discard_task(t.id))
    assert db.task(t.id).merge_state == "discarded" and not wt.exists()
    assert db.task(child).hold and "discarded" in db.task(child).hold["text"]


# ------------------------------------------------------------------ inbox
def test_question_recommended_first(env):
    db, proj, mgr, om, o = env
    q = db.add_question(o.id, "Which db?", ["SQLite", "Postgres"], "Postgres")
    item = next(i for i in inbox.build(db, om) if i.kind == "question")
    assert item.recommended.label == "Postgres" and item.free_text and item.question_id == q.id
    run(inbox.perform(db, om, item, "answer", "SQLite please"))
    assert db.question(q.id).answer == "SQLite please"
    assert not any(i.kind == "question" for i in inbox.build(db, om))


# ------------------------------------------------------------------ UI
def test_inbox_and_board_pages_render(env, repo):
    from PySide6.QtWidgets import QApplication

    from orchestrator.ui.board_page import BoardCardWidget, BoardPage
    from orchestrator.ui.inbox_page import InboxCard, InboxPage

    QApplication.instance() or QApplication([])
    db, proj, mgr, om, o = env
    db.add_question(o.id, "Which db?", ["SQLite", "Postgres"], "Postgres")
    t, wt = _worktree_task(db, o, repo)
    db.update_task(t.id, merge_state="review", checks={"ok": True, "sec": 1.2, "exit": 0, "cmd": "x", "tail": ""})
    a = delegate(om, o, "first")
    delegate(om, o, "second", after=[a])
    page = InboxPage(db, om)
    kinds = [c.item.kind for c in page.findChildren(InboxCard)]
    assert "question" in kinds and "review" in kinds
    counts = []
    page.countChanged.connect(counts.append)
    page.reload()
    assert counts and counts[-1] == len(kinds)
    board = BoardPage(db, om)
    board.reload()
    lines = [c.line.text() for c in board.findChildren(BoardCardWidget)]
    assert any(f"waiting for T-{a} to finish" in x for x in lines)
    assert any("waiting for your review" in x for x in lines)


def test_settings_page_has_new_controls(env):
    from PySide6.QtWidgets import QApplication

    from orchestrator.ui.settings_page import SettingsPage

    QApplication.instance() or QApplication([])
    db, proj, mgr, om, o = env
    review.set_check_command(db, proj.id, "pytest -q")
    page = SettingsPage(db)
    assert page.checkCmd.text() == "pytest -q"
    page.checkCmd.setText("npm test")
    page.checkCmd.editingFinished.emit()
    assert review.check_command(db, proj.id) == "npm test"
    page.silentMin.setValue(5)
    assert db.get_setting("orchestrate.silent_min") == 5
