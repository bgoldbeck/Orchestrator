"""Schedule UI: readable buttons at any width, easy time picking, plain-words times."""
import os
import re
import time
from datetime import datetime, timedelta

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_when_picker_defaults_to_in_5_hours(qapp):
    from orchestrator.ui.when_picker import WhenPicker, time_slots

    w = WhenPicker()
    labels = [b.text() for b in w.buttons.values()]
    assert labels[:2] == ["In 1 hour", "In 5 hours"] and labels[-1] == "Custom"
    assert "Tomorrow 8 AM" in labels
    assert w.buttons["In 5 hours"].isChecked()
    delta = (w.when() - datetime.now()).total_seconds()
    assert 5 * 3600 - 120 <= delta <= 5 * 3600 + 60
    assert w.words.text().endswith("in 5 hours")
    for b in w.buttons.values():  # labels never elided
        assert b.minimumWidth() >= b.sizeHint().width()
    slots = time_slots()
    assert len(slots) == 96 and slots[0][0] == "12:00 AM" and slots[54][0] == "1:30 PM"


def test_when_picker_custom_time(qapp):
    from orchestrator.ui.when_picker import WhenPicker

    w = WhenPicker()
    target = (datetime.now() + timedelta(days=2)).replace(hour=18, minute=30, second=0, microsecond=0)
    w.set_when(target)
    assert w.buttons["Custom"].isChecked() and not w.customRow.isHidden()
    assert w.when() == target and w.time.currentText() == "6:30 PM"
    # "in 2 days", "in 3 days" or "in 47 hours" depending on the time of day the test runs
    assert re.search(r"at 6:30 PM, in \d+ (days|hours)$", w.words.text())


def test_schedules_page_fits_narrow_window(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    from orchestrator.core.scheduler import ScheduleRunner
    from orchestrator.storage.db import Database
    from orchestrator.ui.schedules_page import SchedulesPage
    from tests.test_scheduler import FakeManager, new_schedule

    db = Database(tmp_path / "t.db")
    proj = db.add_project(str(tmp_path), "proj")
    new_schedule(db, proj, prompt="A long prompt " * 20)
    new_schedule(db, proj, name="Later", kind="once", cron=None, run_at=time.time() + 3600, missed="skip")
    page = SchedulesPage(db, ScheduleRunner(db, FakeManager(), f"sqlite:///{(tmp_path / 'j.db').as_posix()}"))
    page.resize(640, 700)
    page.show()
    qapp.processEvents()
    for b in (page.repeatBtn, page.onceBtn):
        assert b.minimumWidth() >= b.sizeHint().width()
    assert page.widget().width() <= page.viewport().width()  # no sideways scrolling
    page.close()


def test_schedule_dialog_once_uses_picker(qapp, tmp_path):
    from PySide6.QtWidgets import QWidget

    from orchestrator.storage.db import Database
    from orchestrator.ui.schedule_dialog import ScheduleDialog

    db = Database(tmp_path / "t.db")
    db.add_project(str(tmp_path), "proj")
    host = QWidget()
    host.resize(900, 800)
    dlg = ScheduleDialog(db, host)
    dlg.when.setCurrentItem("once")
    dlg._update_when()
    dlg.name.setText("x")
    dlg.prompt.setPlainText("do it")
    assert dlg.validate() and dlg.result_data["kind"] == "once"
    assert 5 * 3600 - 120 <= dlg.result_data["run_at"] - time.time() <= 5 * 3600 + 60
