"""Auto-scroll box in every chat header: scrolling up unchecks it, back to the bottom re-checks it."""
import asyncio
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_autoscroll_follows_scrolling(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path))
    asyncio.set_event_loop(asyncio.new_event_loop())
    from orchestrator.core.session_manager import SessionManager
    from orchestrator.storage.db import Database
    from orchestrator.ui.chat_view import ChatView
    from orchestrator.ui.chat_widgets import UserMessage

    db = Database(tmp_path / "t.db")
    p = db.add_project(str(tmp_path), "p")
    s = db.create_session(p.id, "claude", "claude-opus-5-5", "medium", title="T")
    db.add_message(s.id, "user", "text", {"text": "hello"})
    v = ChatView(db, SessionManager(db))
    v.resize(700, 500)
    v.show()
    v.show_session(s.id)
    for i in range(40):
        v._add(UserMessage(f"message {i}"), False)
    for _ in range(5):
        qapp.processEvents()
    bar = v.scroll.verticalScrollBar()
    box = v.autoScroll
    assert box.isChecked() and bar.maximum() > 0, (bar.maximum(), v.scroll.viewport().height(), v.column.height())
    assert bar.value() == bar.maximum()

    # new content while checked: stays pinned to the newest message
    v._add(UserMessage("new one"), False)
    qapp.processEvents()
    assert bar.value() == bar.maximum()

    # scroll up: box unchecks, and new content no longer moves the view
    bar.setValue(0)
    assert not box.isChecked()
    v._add(UserMessage("another"), False)
    qapp.processEvents()
    assert bar.value() == 0 and not box.isChecked()

    # scroll back down to the newest: box re-checks
    bar.setValue(bar.maximum())
    assert box.isChecked()

    # unticking by hand keeps the view where it is; ticking jumps to the bottom
    bar.setValue(0)
    box.setChecked(True)
    assert bar.value() == bar.maximum()
    v.deleteLater()
