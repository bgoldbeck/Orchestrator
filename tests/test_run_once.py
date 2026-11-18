import os
import time
from datetime import datetime

import pytest

from orchestrator.core.scheduler import quick_times

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_quick_times_evening_and_morning():
    morning = datetime(2026, 11, 18, 7, 10)
    labels = [l for l, _ in quick_times(morning)]
    assert labels == ["In 15 min", "In 1 hour", "Tonight 9 PM", "Tomorrow 8 AM"]
    t = dict(quick_times(morning))
    assert t["Tonight 9 PM"] == datetime(2026, 11, 18, 21, 0)
    assert t["Tomorrow 8 AM"] == datetime(2026, 11, 19, 8, 0)
    late = dict(quick_times(datetime(2026, 11, 18, 20, 45)))
    assert "Tonight 9 PM" not in late  # too close / past: hidden
    assert late["In 15 min"] == datetime(2026, 11, 18, 21, 0)


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_run_once_view_schedules_continue_chat(qapp, tmp_path):
    from orchestrator.core.scheduler import ScheduleRunner
    from orchestrator.storage.db import Database
    from orchestrator.ui.run_once import RunOnceView
    from tests.test_scheduler import FakeManager

    db = Database(tmp_path / "t.db")
    proj = db.add_project(str(tmp_path), "proj")
    chat = db.create_session(proj.id, "claude", "claude-haiku-5-5", "low")
    runner = ScheduleRunner(db, FakeManager(), f"sqlite:///{(tmp_path / 'j.db').as_posix()}")
    view = RunOnceView(db, runner, {"prompt": "Check CI", "engine": "claude", "model": "claude-haiku-5-5",
                                    "effort": "low", "project_id": proj.id, "session_id": chat.id})
    got = []
    view.scheduled.connect(got.append)
    view.group.button(0).setChecked(True)  # In 15 min
    view._schedule()
    s = db.schedule(got[0])
    assert s.kind == "once" and s.new_or_resume == "resume" and s.session_id == chat.id
    assert s.engine == "claude" and s.permission == "read-only" and s.prompt == "Check CI"
    assert 14 * 60 <= s.run_at - time.time() <= 16 * 60
    view.cont.setChecked(False)  # new chat instead
    assert view.data()["new_or_resume"] == "new" and view.data()["session_id"] is None


def test_run_once_rejects_empty_prompt(qapp, tmp_path):
    from orchestrator.core.scheduler import ScheduleRunner
    from orchestrator.storage.db import Database
    from orchestrator.ui.run_once import RunOnceView
    from tests.test_scheduler import FakeManager

    db = Database(tmp_path / "t.db")
    db.add_project(str(tmp_path), "proj")
    view = RunOnceView(db, ScheduleRunner(db, FakeManager(), f"sqlite:///{(tmp_path / 'j.db').as_posix()}"), {})
    view._schedule()
    assert db.schedules() == [] and "prompt" in view.summary.text()
