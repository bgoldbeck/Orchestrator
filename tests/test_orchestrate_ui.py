"""Orchestrate page board + sidebar nesting (offscreen, fake engines)."""
import asyncio
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from orchestrator.storage.db import Database  # noqa: E402
from tests.test_orchestration import REPORT, FakeMgr  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def env(qapp, tmp_path):
    from PySide6.QtCore import Signal

    from orchestrator.core.orchestration import OrchestrationManager

    class Mgr(FakeMgr):
        approval_requested = Signal(int, object, object)
        event = Signal(int, object)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    db = Database(tmp_path / "t.db")
    proj = db.add_project(str(tmp_path), "proj")
    mgr = Mgr(db)
    om = OrchestrationManager(db, mgr)
    yield db, proj, mgr, om, loop
    om.bridge.stop()
    loop.close()


def flush(qapp):
    from PySide6.QtCore import QCoreApplication, QEvent

    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    qapp.processEvents()


def test_board_cards_status_and_approval(env, qapp):
    from orchestrator.core.events import NeedsApproval
    from orchestrator.ui.orchestrate_page import OrchestratePage

    db, proj, mgr, om, loop = env
    page = OrchestratePage(db, mgr, om)
    page.resize(1200, 900)
    page.goal.setPlainText("")
    page._start()
    assert "goal" in page.composerError.text().lower()  # empty goal refused
    page.goal.setPlainText("Do two things")
    page._start()
    o = db.orchestrations(proj.id)[0]
    assert page.current == o.id and page.composer.isHidden() and page.planner.isVisibleTo(page)
    a = om.delegate(o.id, "first", title="First")["task_id"]
    b = om.delegate(o.id, "second", title="Second", engine="claude")["task_id"]
    page.render_board()
    flush(qapp)
    assert set(page.cards) == {a, b} and "2 tasks" in page.boardHead.text()
    # approval from a sub-agent shows on its card; Allow resolves the engine's future
    fut = loop.create_future()
    sid_b = db.task(b).session_id
    mgr.approval_requested.emit(sid_b, NeedsApproval("x", "Bash", "npm test"), fut)
    page.render_board()
    flush(qapp)
    card = page.cards[b]
    assert card.approval.isVisibleTo(card) and "Bash" in card.approvalText.text()
    card._answer(True)
    assert fut.done() and fut.result() is True
    mgr.finish(db.task(a).session_id, REPORT)
    page.render_board()
    flush(qapp)
    assert db.task(a).status == "done" and "1 done" in page.boardHead.text()
    texts = [w.text() for w in page.cards[a].findChildren(type(page.cards[a].sub))]
    assert any("hello.py" in t for t in texts)


def test_board_one_column_when_narrow(env, qapp):
    from orchestrator.ui.orchestrate_page import OrchestratePage

    db, proj, mgr, om, loop = env
    o = om.start(proj.id, "g", "codex", "gpt-6-luna", "low")
    for i in range(3):
        om.delegate(o.id, f"t{i}")
    page = OrchestratePage(db, mgr, om)
    page.resize(700, 800)
    page.show()
    page.show_run(o.id)
    flush(qapp)
    assert page.cols[0].count() - 1 == 3 and page.cols[1].count() - 1 == 0
    page.resize(1300, 800)
    page.render_board()
    flush(qapp)
    assert page.cols[0].count() - 1 == 2 and page.cols[1].count() - 1 == 1
    page.hide()


def test_sidebar_nests_subagent_chats(env, qapp):
    from orchestrator.ui.sidebar import Sidebar

    db, proj, mgr, om, loop = env
    mgr.any_running = lambda: False
    o = om.start(proj.id, "g", "codex", "gpt-6-luna", "low")
    tid = om.delegate(o.id, "child task", title="Child")["task_id"]
    sb = Sidebar(db, mgr)
    sb.reload()
    pi = sb.tree.topLevelItem(0)
    sec = pi.child(0)
    assert sec.data(0, 0x0100) == "section" and sec.data(0, 0x0101).endswith(":orch")
    planner = sec.child(0)
    assert planner.data(0, 0x0101) == o.session_id  # ROLE_ID
    assert planner.data(0, 0x0105) == "g"  # shown by its goal
    assert planner.childCount() == 1 and planner.child(0).text(0) == "Child"
    assert sb._find(db.task(tid).session_id) is planner.child(0)
    # leaving a sub-agent chat that has no messages yet must not delete it
    sid = db.task(tid).session_id
    sb.current_sid = sid
    sb.select(None)
    assert db.session(sid) is not None


def test_sidebar_sections_filters_and_history_limit(env, qapp):
    from types import SimpleNamespace

    from orchestrator.ui.sidebar import Sidebar

    db, proj, mgr, om, loop = env
    mgr.any_running = lambda: False
    mine = db.create_session(proj.id, "claude", "claude-opus-5-5", "medium", title="My chat")
    for i in range(5):
        info = SimpleNamespace(engine="claude", session_id=f"h{i}", title=f"Hist {i}", last_active=100.0 + i,
                               path=f"/x/{i}.jsonl", mtime=1.0, cwd=proj.path, model="", effort="",
                               origin="app" if i == 4 else "")
        db.upsert_imported(info, proj.id)
    sb = Sidebar(db, mgr)
    sb.set_filter("all")
    pi = sb.tree.topLevelItem(0)
    secs = {pi.child(i).data(0, 0x0101).split(":")[1]: pi.child(i) for i in range(pi.childCount())}
    assert list(secs) == ["mine", "history", "app"]
    assert secs["mine"].child(0).data(0, 0x0101) == mine.id
    hist = secs["history"]
    assert hist.data(0, 0x0104) == 4 and hist.childCount() == 4  # 3 newest + "Show 1 older…"
    assert hist.child(3).data(0, 0x0100) == "more"
    assert not secs["app"].isExpanded()  # Claude app / remote sessions start folded
    sb._clicked(hist.child(3), 0)
    pi = sb.tree.topLevelItem(0)
    assert pi.child(1).childCount() == 4  # all history now
    sb.set_filter("mine")
    pi = sb.tree.topLevelItem(0)
    assert pi.childCount() == 1 and pi.child(0).data(0, 0x0101).endswith(":mine")
    sb.set_filter("all")
    sb.search.setText("hist 1")
    sb.reload()
    pi = sb.tree.topLevelItem(0)
    visible = [pi.child(i) for i in range(pi.childCount()) if not pi.child(i).isHidden()]
    assert [v.data(0, 0x0101).split(":")[1] for v in visible] == ["history"]


def test_title_text_cleanup():
    from orchestrator.core.indexer import title_text

    assert title_text('@"C:\\u\\a.png" fix the lighting') == "fix the lighting"
    assert title_text("C:\\Users\\b\\Arcanium\nread player.log") == "read player.log"
    assert title_text("see &#34;build failed&#34;") == 'see "build failed"'
    assert title_text("D:\\Games\\shot.png") == "File: shot.png"
    from orchestrator.core.harness_text import clean

    assert clean('<wake reason="m"><message>why is my patch server down? <attachment kind="image" '
                 'path="a.png"/></message></wake>') == "why is my patch server down?"


def test_reuse_card_question_box_and_auto_planner(env, qapp):
    from qfluentwidgets import PillPushButton, PrimaryPushButton

    from orchestrator.ui.orchestrate_page import OrchestratePage

    db, proj, mgr, om, loop = env
    db.set_setting("routing.rules", [])
    db.set_setting("orchestrate.confirm", "always_ask")
    old = db.create_session(proj.id, "claude", "claude-sonnet-5-5", "medium", title="Fix login bug")
    db.add_message(old.id, "user", "text", {"text": "login"})
    page = OrchestratePage(db, mgr, om)
    page.resize(1200, 900)
    page.autoPlanner.setChecked(True)
    assert not page.pill.isEnabled()
    page.goal.setPlainText("Investigate the flaky login test")
    page._start()
    o = db.orchestrations(proj.id)[0]
    assert o.engine == "claude" and o.pick_reason.startswith("Auto planner")
    tid = om.delegate(o.id, "login again", resume_session_id=f"s:{old.id}")["task_id"]
    page.render_board()
    flush(qapp)
    card = page.cards[tid]
    assert card.proposal is not None and "Fix login bug" in card.proposalText.text()
    use = next(b for b in card.proposal.findChildren(PrimaryPushButton) if b.text() == "Use it")
    use.click()
    assert db.task(tid).reused and db.task(tid).session_id == old.id
    # planner question shows with option buttons; clicking one answers it
    q = db.add_question(o.id, "Ship it?", ["yes", "no"])
    page._q_shown = ()
    page._render_planner(db.orchestration(o.id))
    flush(qapp)
    yes = next(b for b in page.questions.findChildren(PillPushButton) if b.text() == "yes")
    yes.click()
    assert db.question(q.id).answer == "yes"
    page.autoPlanner.setChecked(False)
