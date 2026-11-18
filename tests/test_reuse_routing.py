"""M5: summaries, find_sessions, reuse proposals, auto routing, escalation, ask_user (fake engines)."""
import asyncio
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from orchestrator.core import routing  # noqa: E402
from orchestrator.core.orchestration import OrchestrationError, OrchestrationManager  # noqa: E402
from orchestrator.core.summaries import (  # noqa: E402
    Summarizer,
    find_sessions,
    heuristic_summary,
    parse_model_summary,
)
from orchestrator.storage.db import Database  # noqa: E402
from tests.test_orchestration import REPORT, FakeMgr  # noqa: E402

FAILED = "```report\nstatus: failed\nsummary: Could not find the bug.\nfiles_changed: none\nopen_questions: none\n```"


@pytest.fixture(autouse=True)
def loop():
    lp = asyncio.new_event_loop()
    asyncio.set_event_loop(lp)
    yield lp
    lp.close()


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "t.db")
    proj = db.add_project(str(tmp_path), "proj")
    mgr = FakeMgr(db)
    om = OrchestrationManager(db, mgr)
    yield db, proj, mgr, om
    om.bridge.stop()


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def chat(db, pid, title, user, agent, engine="codex", age_days=0.0, summary=None, keywords=None):
    s = db.create_session(pid, engine, "gpt-6.2-sol" if engine == "codex" else "claude-sonnet-5-5", "medium",
                          title=title)
    db.add_message(s.id, "user", "text", {"text": user})
    db.add_message(s.id, "assistant", "text", {"text": agent})
    db.update_session(s.id, last_active=time.time() - age_days * 86400, status="done")
    if summary is not None:
        db.update_session(s.id, summary=summary, keywords=keywords)
    return s


# ---------------------------------------------------------------- summaries + search
def test_heuristic_and_model_summary_parsing(tmp_path):
    db = Database(tmp_path / "t.db")
    p = db.add_project(str(tmp_path), "p")
    s = chat(db, p.id, "Fix login bug", "The login form rejects valid passwords in auth.py",
             "Fixed the bcrypt compare in auth.py; tests pass.")
    summary, kw = heuristic_summary(db, s)
    assert summary.startswith("Asked: The login form") and "auth.py" in kw and "login" in kw
    body, kws = parse_model_summary("User wanted X.\nDid Y in a.py.\n**Keywords:** Login, auth.py , bcrypt")
    assert body == "User wanted X.\nDid Y in a.py." and kws == "login, auth.py, bcrypt"


def test_summarizer_heuristic_mode_stores_summary(env):
    db, proj, mgr, om = env
    db.set_setting("summaries.mode", "heuristic")
    s = chat(db, proj.id, "Add dark mode", "Add a dark mode toggle to settings.vue", "Added the toggle.")
    sm = Summarizer(db, mgr, delay_ms=0)
    run(sm.summarize(s.id))
    s2 = db.session(s.id)
    assert s2.summary and s2.summary_updated_at and "settings.vue" in s2.keywords


def test_find_sessions_ranks_by_match_project_and_recency(tmp_path):
    db = Database(tmp_path / "t.db")
    a = db.add_project(str(tmp_path / "a"), "a")
    b = db.add_project(str(tmp_path / "b"), "b")
    old = chat(db, a.id, "Fix login bug", "x", "y", age_days=40, summary="Fixed login redirect.", keywords="login, auth")
    new = chat(db, a.id, "Login page styles", "x", "y", age_days=1, summary="CSS for login.", keywords="login, css")
    other = chat(db, b.id, "Fix login bug in API", "x", "y", age_days=1, summary="API login.", keywords="login, api")
    chat(db, a.id, "Unrelated", "x", "y", summary="Docs only.", keywords="docs")
    res = find_sessions(db, "fix the login bug", project_id=a.id)
    refs = [r["session_ref"] for r in res]
    assert f"s:{old.id}" in refs and f"s:{new.id}" in refs and f"s:{other.id}" in refs and len(res) == 3
    top = res[0]
    assert "matches" in top["why"] and top["title"].startswith("Fix login bug")
    assert "same project" in next(r for r in res if r["session_ref"] == f"s:{old.id}")["why"]


# ---------------------------------------------------------------- routing
def test_route_uses_only_allowed_models_and_gives_reason(tmp_path):
    db = Database(tmp_path / "t.db")
    easy = routing.route(db, "Fix a typo in README.md")
    assert easy.effort == "low" and easy.reason.startswith("Auto:") and "small task" in easy.reason
    hard = routing.route(db, "Investigate the intermittent race condition in the job scheduler")
    assert hard.engine == "claude" and hard.model == "claude-opus-5-5" and hard.effort == "high"
    big = routing.route(db, "Refactor the whole storage layer and migrate every caller to the new API " * 30)
    assert big.engine == "codex" and big.effort == "high"
    db.set_model_enabled("claude", "claude-opus-5-5", False)  # strongest Claude off: next one, never Opus
    hard2 = routing.route(db, "Investigate the intermittent race condition")
    assert hard2.model != "claude-opus-5-5" and hard2.engine == "claude"
    db.set_model_enabled("claude", None, False)
    assert routing.route(db, "Investigate the race").engine == "codex"  # engine off: other engine
    assert routing.route(db, "x", engine="claude") is None
    db.set_model_enabled("codex", None, False)
    assert routing.route(db, "x") is None and routing.route_planner(db, "x") is None


def test_escalate_steps_effort_then_model(tmp_path):
    db = Database(tmp_path / "t.db")
    e1 = routing.escalate(db, "claude", "claude-haiku-5-5", "low")
    assert (e1.model, e1.effort) == ("claude-haiku-5-5", "medium") and "low → medium" in e1.reason
    e2 = routing.escalate(db, "claude", "claude-haiku-5-5", "high")
    assert e2.model == "claude-fable-5-2" and e2.effort == "high"  # next up in strength order
    order = [m.id for m in routing.strength_order(db.models("codex"))]
    assert order[-1] == "gpt-6-luna" and order[0] == "gpt-6.2-sol"
    assert routing.escalate(db, "claude", "claude-opus-5-5", "high") is None  # already strongest
    db.set_model_enabled("claude", "claude-fable-5-2", False)
    assert routing.escalate(db, "claude", "claude-haiku-5-5", "high").model == "claude-sonnet-5-5"


# ---------------------------------------------------------------- orchestration flows
def test_auto_pick_reason_on_task_and_planner_auto(env):
    db, proj, mgr, om = env
    o = om.start(proj.id, "Investigate why the cache is flaky", "auto", "", "")
    assert o.engine == "claude" and o.model == "claude-opus-5-5" and o.pick_reason.startswith("Auto planner")
    r = om.delegate(o.id, "Fix a typo in README.md")
    t = db.task(r["task_id"])
    assert t.auto_picked and t.effort == "low" and t.pick_reason.startswith("Auto:") and r["pick_reason"]
    r2 = om.delegate(o.id, "Do it", engine="codex", model="gpt-6-luna", effort="low", reason="cheap and fast")
    t2 = db.task(r2["task_id"])
    assert not t2.auto_picked and t2.pick_reason == "cheap and fast"


def test_reuse_proposal_accept_fresh_and_pick(env):
    db, proj, mgr, om = env
    db.set_setting("orchestrate.confirm", "always_ask")
    old = chat(db, proj.id, "Fix login bug", "login fails", "fixed", engine="claude", summary="login", keywords="login")
    other = chat(db, proj.id, "Login styles", "css", "done", engine="codex", summary="login css", keywords="login")
    o = om.start(proj.id, "g", "codex", "gpt-6-luna", "low")
    found = run(om.handle(o.id, "find_sessions", {"query": "login bug"}))
    assert found["candidates"][0]["session_ref"] == f"s:{old.id}"
    r = om.delegate(o.id, "Fix the login bug again", resume_session_id=f"s:{old.id}")
    t = db.task(r["task_id"])
    assert t.status == "proposed" and t.engine == "claude" and t.proposal["kind"] == "reuse"
    assert r["status"] == "proposed" and "confirm" in r["note"]
    om.resolve_proposal(t.id, "accept")
    t = db.task(t.id)
    assert t.reused and t.session_id == old.id and t.status == "running"
    assert db.session(old.id).parent_id == o.session_id
    assert mgr.sent[-1][1].startswith("New task from the orchestrator")  # continued chat: no intro
    # Start fresh keeps a new chat
    r2 = om.delegate(o.id, "Login once more", resume_session_id=f"s:{old.id}")
    om.resolve_proposal(r2["task_id"], "fresh")
    t2 = db.task(r2["task_id"])
    assert not t2.reused and t2.session_id != old.id and t2.status == "running"
    # Pick another: switches to that chat and its engine
    r3 = om.delegate(o.id, "Login css", resume_session_id=f"s:{old.id}")
    om.resolve_proposal(r3["task_id"], "pick", other.id)
    t3 = db.task(r3["task_id"])
    assert t3.session_id == other.id and t3.engine == "codex" and t3.reused
    with pytest.raises(OrchestrationError, match="No session"):
        om.delegate(o.id, "x", resume_session_id="s:9999")


def test_countdown_auto_accepts(env, loop, monkeypatch):
    from orchestrator.core import orchestration

    db, proj, mgr, om = env
    monkeypatch.setattr(orchestration, "COUNTDOWN_SEC", 0.05)
    old = chat(db, proj.id, "Fix login bug", "x", "y", engine="claude")
    o = om.start(proj.id, "g", "codex", "gpt-6-luna", "low")
    tid = om.delegate(o.id, "again", resume_session_id=f"s:{old.id}")["task_id"]
    assert db.task(tid).status == "proposed" and db.task(tid).proposal["deadline"]
    run(asyncio.sleep(0.2))
    assert db.task(tid).status == "running" and db.task(tid).session_id == old.id


def test_change_pick_before_start(env):
    db, proj, mgr, om = env
    db.set_setting("orchestrate.confirm", "always_ask")
    o = om.start(proj.id, "g", "codex", "gpt-6-luna", "low")
    tid = om.delegate(o.id, "Fix a typo")["task_id"]
    assert db.task(tid).status == "proposed"
    om.change_pick(tid, "claude", "claude-sonnet-5-5", "medium")
    db.set_model_enabled("claude", "claude-haiku-5-5", False)
    with pytest.raises(Exception, match="disabled"):
        om.change_pick(tid, "claude", "claude-haiku-5-5", "low")
    om.resolve_proposal(tid, "accept")
    t = db.task(tid)
    assert (t.engine, t.model, t.status) == ("claude", "claude-sonnet-5-5", "running")
    assert t.pick_reason.startswith("Changed by you") and db.session(t.session_id).model == "claude-sonnet-5-5"


def test_failed_auto_pick_escalates_once(env):
    db, proj, mgr, om = env
    o = om.start(proj.id, "g", "codex", "gpt-6-luna", "low")
    tid = om.delegate(o.id, "Fix a typo in README.md", engine="claude")["task_id"]
    t = db.task(tid)
    assert (t.model, t.effort) == ("claude-haiku-5-5", "low")  # cheapest enabled Claude
    mgr.finish(t.session_id, FAILED)
    run(asyncio.sleep(0))  # retry starts on the next loop pass
    t = db.task(tid)
    assert t.status == "running" and t.escalated and t.effort == "medium" and "Escalated" in t.pick_reason
    assert mgr.sent[-1][0] == t.session_id and mgr.sent[-1][1].startswith("Your previous attempt did not succeed")
    mgr.finish(t.session_id, FAILED)  # second failure: no more retries
    run(asyncio.sleep(0))
    t = db.task(tid)
    assert t.status == "failed" and t.result["escalated"].startswith("Escalated")
    # planner-picked tasks are not escalated by the app
    t2 = om.delegate(o.id, "x", engine="codex", model="gpt-6-luna", effort="low")["task_id"]
    mgr.finish(db.task(t2).session_id, FAILED)
    assert db.task(t2).status == "failed" and not db.task(t2).escalated


def test_ask_user_waits_and_returns_answer(env, loop):
    db, proj, mgr, om = env
    o = om.start(proj.id, "g", "codex", "gpt-6-luna", "low")
    asked = []
    om.question_asked.connect(lambda oid, qid: asked.append(qid))
    first = run(om.handle(o.id, "ask", {"question": "Which DB?", "options": ["sqlite", "postgres"], "timeout": 0.05}))
    assert first == {"answered": False, "question_id": asked[0]}
    assert db.open_questions(o.id)[0].options == ["sqlite", "postgres"]
    loop.call_later(0.05, lambda: om.answer(asked[0], "postgres"))
    second = run(om.handle(o.id, "ask", {"question": "Which DB?", "question_id": asked[0], "timeout": 5}))
    assert second["answered"] and second["answer"] == "postgres" and not db.open_questions(o.id)
    with pytest.raises(OrchestrationError, match="empty"):
        run(om.handle(o.id, "ask", {"question": " ", "timeout": 0}))


def test_prompt_mentions_guide_and_tools(env):
    db, proj, mgr, om = env
    db.set_setting("routing.guide", "- custom: use Haiku for everything")
    o = om.start(proj.id, "g", "codex", "gpt-6-luna", "low")
    prompt = mgr.sent[0][1]
    assert "find_sessions" in prompt and "ask_user" in prompt and "custom: use Haiku" in prompt
    assert om.delegate(o.id, "done task", engine="codex", model="gpt-6-luna")["status"] == "running"
    mgr.finish(db.task(1).session_id, REPORT)
