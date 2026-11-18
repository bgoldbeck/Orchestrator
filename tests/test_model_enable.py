"""Disabled models are excluded everywhere (Settings › Models › Enabled)."""
import asyncio
import os
import sqlite3

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from orchestrator.core import model_registry as reg  # noqa: E402
from orchestrator.storage.db import Database  # noqa: E402
from tests.test_scheduler import FakeManager, new_schedule  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "t.db")


def test_migration_defaults_enabled(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)  # a models table from before the column existed
    con.execute("CREATE TABLE models(engine TEXT NOT NULL, id TEXT NOT NULL, label TEXT NOT NULL, "
                "efforts_json TEXT NOT NULL, sort INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(engine, id))")
    con.execute("INSERT INTO models VALUES('codex','m1','M1','[\"low\"]',0)")
    con.commit()
    con.close()
    d = Database(path)
    assert [(m.id, m.enabled) for m in d.models()] == [("m1", True)]


def test_registry_allowed_default_pick_check(db):
    db.set_model_enabled("codex", "gpt-6.2-sol", False)  # the codex default
    ids = [m.id for m in reg.allowed(db, "codex")]
    assert "gpt-6.2-sol" not in ids and ids
    assert not reg.is_allowed(db, "codex", "gpt-6.2-sol")
    assert reg.default(db, "codex")[0] == ids[0]  # falls to first allowed, never the disabled default
    assert reg.pick(db, "codex", "gpt-6.2-sol", "high")[1] == ids[0]
    with pytest.raises(reg.ModelDisabled, match="disabled"):
        reg.check(db, "codex", "gpt-6.2-sol")
    with pytest.raises(reg.ModelDisabled, match="Unknown model"):
        reg.check(db, "codex", "made-up", strict=True)
    assert reg.is_allowed(db, "codex", "from-old-log")  # unlisted id from an imported log: not strict
    db.set_model_enabled("codex", None, False)
    assert not reg.engine_available(db, "codex") and reg.available_engines(db) == ["claude"]
    assert reg.pick(db, "codex", "", "")[0] == "claude"  # whole engine off: other engine
    with pytest.raises(reg.ModelDisabled, match="no enabled models"):
        reg.check(db, "codex", "")
    db.set_model_enabled("claude", None, False)
    assert reg.pick(db, "codex", "", "") is None


def test_save_models_keeps_enabled(db):
    ms = db.models()
    ms[0].enabled = False
    db.save_models(ms)
    assert db.models()[0].enabled is False and all(m.enabled for m in db.models()[1:])


def test_session_manager_refuses_disabled_model(db, tmp_path):
    from orchestrator.core.session_manager import SessionManager

    p = db.add_project(str(tmp_path), "p")
    s = db.create_session(p.id, "claude", "claude-haiku-5-5", "low")
    db.set_model_enabled("claude", "claude-haiku-5-5", False)
    assert SessionManager(db).send(s.id, "hi") is False
    assert db.messages(s.id) == []  # nothing recorded, nothing run


def test_schedule_with_disabled_model_is_skipped_and_flagged(db, tmp_path):
    from orchestrator.core.scheduler import ScheduleRunner

    p = db.add_project(str(tmp_path), "p")
    mgr = FakeManager()
    runner = ScheduleRunner(db, mgr, f"sqlite:///{(tmp_path / 'j.db').as_posix()}")
    blocked = []
    runner.run_blocked.connect(lambda sid, why: blocked.append((sid, why)))
    sched = new_schedule(db, p, model="gpt-6.2-sol")
    db.set_model_enabled("codex", "gpt-6.2-sol", False)
    asyncio.run(runner.fire(sched))
    run = db.runs(sched)[0]
    assert run.status == "skipped" and run.note.startswith("Model disabled")
    assert mgr.sent == [] and blocked and blocked[0][0] == sched  # never run, never swapped; toast signal
    # A resumed chat whose model was disabled is skipped too.
    s = db.create_session(p.id, "claude", "claude-opus-5-5", "high")
    db.set_model_enabled("claude", "claude-opus-5-5", False)
    sched2 = new_schedule(db, p, engine="claude", model="claude-sonnet-5-5", new_or_resume="resume", session_id=s.id)
    asyncio.run(runner.fire(sched2))
    assert db.runs(sched2)[0].status == "skipped" and mgr.sent == []


def test_model_pill_hides_disabled(qapp, db):
    from orchestrator.ui.chat_view import ModelPill

    db.set_model_enabled("claude", "claude-fable-5-2", False)
    db.set_model_enabled("codex", None, False)
    pill = ModelPill(db)
    pill.set_value("claude", "claude-sonnet-5-5", "medium", locked=False)
    texts = [a.text() for a in pill.build_menu().actions()]
    assert "Fable 5.2" not in texts and "Sonnet 5.5" in texts
    codex = next(a for a in pill.build_menu().actions() if a.text().startswith("Codex"))
    assert not codex.isEnabled() and "no enabled models" in codex.text()
    pill.set_value("claude", "claude-fable-5-2", "low", locked=True)
    assert "(disabled)" in pill.text()


def test_chat_with_disabled_model_warns_and_blocks_send(qapp, db, tmp_path):
    from orchestrator.ui.chat_view import ChatView

    class Mgr(FakeManager):
        from PySide6.QtCore import Signal as _S
        event = _S(int, object)
        approval_requested = _S(int, object, object)

        def live_tools(self, sid):
            return []

        def live_text(self, sid):
            return ""

    p = db.add_project(str(tmp_path), "p")
    s = db.create_session(p.id, "claude", "claude-opus-5-5", "high")
    mgr = Mgr()
    view = ChatView(db, mgr)
    view.show_session(s.id)
    assert view.model_warn.isHidden()
    db.set_model_enabled("claude", "claude-opus-5-5", False)
    view.refresh_models()
    assert not view.model_warn.isHidden() and "disabled" in view.model_warn.text()
    view._send("do it")
    assert mgr.sent == [] and view.prompt.edit.toPlainText() == "do it"  # kept for after picking a model
    view._on_model_changed("claude", "claude-sonnet-5-5", "medium")
    view.refresh_models()
    assert view.model_warn.isHidden()
    view._send("do it")
    assert mgr.sent and mgr.sent[0][1] == "do it"


def test_schedule_dialog_lists_only_allowed(qapp, db, tmp_path):
    from PySide6.QtWidgets import QWidget

    from orchestrator.ui.schedule_dialog import ScheduleDialog

    p = db.add_project(str(tmp_path), "p")
    db.set_model_enabled("codex", "gpt-6.1-terra", False)
    host = QWidget()
    host.resize(900, 700)
    dlg = ScheduleDialog(db, host)
    dlg.engine.setCurrentIndex(dlg.engine.findData("codex"))
    ids = [dlg.model.itemData(i) for i in range(dlg.model.count())]
    assert "gpt-6.1-terra" not in ids and ids
    # Editing a schedule whose model was disabled: shown as disabled, Save refused.
    sched = db.schedule(new_schedule(db, p, model="gpt-6.1-terra"))
    dlg2 = ScheduleDialog(db, host, sched)
    assert dlg2.model.currentData() == "gpt-6.1-terra" and "(disabled)" in dlg2.model.currentText()
    assert dlg2.validate() is False and "disabled" in dlg2.error.text()


def test_run_once_starts_on_allowed_model_and_refuses_disabled(qapp, db, tmp_path):
    from orchestrator.core.scheduler import ScheduleRunner
    from orchestrator.ui.run_once import RunOnceView

    p = db.add_project(str(tmp_path), "p")
    db.set_setting("last", ["codex", "gpt-6.2-sol", "medium"])
    db.set_model_enabled("codex", "gpt-6.2-sol", False)
    runner = ScheduleRunner(db, FakeManager(), f"sqlite:///{(tmp_path / 'j.db').as_posix()}")
    view = RunOnceView(db, runner, {"project_id": p.id})
    assert reg.is_allowed(db, view.pill.engine, view.pill.model, strict=True)
    view.pill.set_value("codex", "gpt-6.2-sol", "low", locked=False)
    view.prompt.setPlainText("x")
    view.picker.select("In 1 hour")
    view._schedule()
    assert db.schedules() == [] and "disabled" in view.summary.text()


def test_settings_enabled_column_and_bulk(qapp, db):
    from PySide6.QtCore import Qt

    from orchestrator.ui.settings_page import ModelsCard

    card = ModelsCard(db)
    changed = []
    card.changed.connect(lambda: changed.append(1))
    assert card.table.horizontalHeaderItem(0).text() == "Enabled"
    card.table.item(0, 0).setCheckState(Qt.Unchecked)  # takes effect right away
    first = db.models()[0]
    assert first.enabled is False and changed
    card._set_all("codex", False)
    assert not reg.engine_available(db, "codex") and "Codex has no enabled models" in card.warn.text()
    card._set_all("codex", True)
    assert reg.engine_available(db, "codex")
