"""Orchestrate workspace: the planner chat stays in view; tasks open in the right pane, never the Chats page."""
import asyncio
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def win(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path))
    monkeypatch.setenv("ORCHESTRATOR_INDEX_CLAUDE", str(tmp_path / "none"))
    monkeypatch.setenv("ORCHESTRATOR_INDEX_CODEX", str(tmp_path / "none"))
    monkeypatch.setenv("ORCHESTRATOR_NO_USER_ENV", "1")
    app = QApplication.instance() or QApplication([])
    asyncio.set_event_loop(asyncio.new_event_loop())
    from orchestrator.core.session_manager import SessionManager
    from orchestrator.storage.db import Database
    from orchestrator.ui.main_window import MainWindow

    db = Database(tmp_path / "t.db")
    p = db.add_project(str(tmp_path), "proj")
    o = db.add_orchestration(p.id, "goal", "claude", "claude-opus-5-5", "medium", 2)
    ps = db.create_session(p.id, "claude", "claude-opus-5-5", "medium", title="Orchestrate · goal")
    db.update_orchestration(o.id, session_id=ps.id)
    tids = {}
    for status in ("running", "queued", "waiting", "done"):
        t = db.add_task(o.id, "codex", "gpt-6-luna", "low", status, status, p.path)
        s = db.create_session(p.id, "codex", "gpt-6-luna", "low", parent_id=ps.id, title=status)
        db.update_task(t.id, session_id=s.id, status=status, started_at=time.time() - 60)
        tids[status] = (t.id, s.id)
    db.set_setting("orchestrate.project", p.id)
    w = MainWindow(db, SessionManager(db))
    w.resize(1400, 800)
    w.show()
    app.processEvents()
    yield w, db, o, ps, tids
    w.hide()


def test_planner_stays_and_task_opens_in_pane(win):
    from orchestrator.ui.orchestrate_workspace import TaskRow, group_of

    w, db, o, ps, tids = win
    w.switchTo(w.orchestrate)
    ws = w.orchestrate.ensure()
    ws.history.setCurrentIndex(ws.history.findData(o.id))
    assert not ws.planner.isHidden() and ws.planner.session.id == ps.id
    assert ws.stack.currentWidget() is ws.tasks
    from orchestrator.ui.inbox_page import InboxCard

    rows = ws.tasks.findChildren(TaskRow)
    assert len(rows) == 2  # working + queued; Done is collapsed by default
    cards = ws.tasks.findChildren(InboxCard)  # the waiting task is a Needs you card with its buttons
    assert [c.item.task_id for c in cards] == [tids["waiting"][0]]
    assert [group_of(db.task(tid)) for tid, _ in tids.values()] == ["working", "queued", "waiting", "done"]
    ws.open_task(tids["running"][0])
    assert ws.stack.currentWidget() is ws.agent and ws.agent.session.id == tids["running"][1]
    assert w.stackedWidget.currentWidget() is w.orchestrate  # did not jump to Chats
    ws.agent.closeRequested.emit()
    assert ws.stack.currentWidget() is ws.tasks
    ws.page.openChat.emit(ps.id)  # the planner's own button just focuses the left pane
    assert ws.stack.currentWidget() is ws.tasks


def test_board_click_opens_task_in_workspace(win):
    w, db, o, ps, tids = win
    w.open_task_chat(tids["queued"][1])
    ws = w.orchestrate.ensure()
    assert w.stackedWidget.currentWidget() is w.orchestrate
    assert ws.page.current == o.id and ws.agent.session.id == tids["queued"][1]


def test_new_goal_hides_planner(win):
    w, db, o, ps, tids = win
    w.switchTo(w.orchestrate)
    ws = w.orchestrate.ensure()
    ws.history.setCurrentIndex(ws.history.findData(o.id))
    ws.new_goal()
    assert ws.body.currentWidget() is ws.page and ws.page.composer.isVisibleTo(ws.page)
    assert ws.runBtn.isHidden() and not ws.settingsBtn.isHidden()


def test_header_project_and_run(win):
    from orchestrator.ui.orchestrate_page import RunCard

    w, db, o, ps, tids = win
    w.switchTo(w.orchestrate)
    ws = w.orchestrate.ensure()
    ws.page.set_project(o.project_id)
    assert ws.page.current is None and ws.body.currentWidget() is ws.page  # switching project shows its home
    assert ws.projectBtn.name.text() == "proj" and "proj" in ws.page.heading.text()
    cards = ws.page.findChildren(RunCard)
    assert [c.oid for c in cards] == [o.id]
    cards[0].clicked.emit(o.id)
    assert ws.page.current == o.id and ws.body.currentWidget() is ws.split
    assert not ws.runBtn.isHidden() and ws.runBtn.text().startswith("goal")
    assert "Tasks · 4" == ws.sideTitle.text()
    menu = ws.tasks.task_menu(tids["done"][0])
    assert "Retry" in [a.text() for a in menu.actions()]
