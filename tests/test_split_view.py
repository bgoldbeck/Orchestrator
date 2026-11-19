"""Side by side: an agent's chat opens next to its orchestrator chat."""
import asyncio
import os

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
    w = MainWindow(db, SessionManager(db))
    w.resize(1400, 800)
    w.show()
    app.processEvents()
    yield w, db
    w.hide()


def test_open_task_chat_pairs_with_orchestrator(win):
    w, db = win
    p = db.add_project("/tmp/p", "p")
    planner = db.create_session(p.id, "claude", "claude-opus-5-5", "medium", title="Orchestrate")
    a = db.create_session(p.id, "codex", "gpt-6-luna", "low", parent_id=planner.id, title="Agent A")
    b = db.create_session(p.id, "claude", "claude-haiku-5-5", "low", parent_id=planner.id, title="Agent B")
    w.open_task_chat(a.id)
    page = w.chats.ensure()
    assert page.chat.session.id == planner.id
    assert page.side_sid == a.id
    assert not page.side.closeBtn.isHidden() and page.chat.closeBtn.isHidden()
    labels = [(lbl, s.id) for lbl, s in page.side.related_chats()]
    assert labels == [("Orchestrator", planner.id), ("Agent", b.id)]
    assert [s.id for _l, s in page.chat.related_chats()] == [a.id, b.id]
    page.side.sideBySideRequested.emit(b.id)
    assert page.side_sid == b.id
    page.close_side()
    assert page.side_sid is None


def test_plain_chat_opens_alone(win):
    w, db = win
    p = db.add_project("/tmp/q", "q")
    s = db.create_session(p.id, "claude", "claude-opus-5-5", "medium", title="Solo")
    w.open_task_chat(s.id)
    page = w.chats.ensure()
    assert page.chat.session.id == s.id and page.side_sid is None
    assert page.chat.pairBtn.isHidden()
