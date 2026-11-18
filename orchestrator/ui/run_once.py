"""'Run once' popover: the quick way to schedule a single prompt for later."""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CheckBox,
    ComboBox,
    Flyout,
    FlyoutAnimationType,
    FlyoutViewBase,
    HyperlinkButton,
    InfoBar,
    InfoBarPosition,
    PlainTextEdit,
    PrimaryPushButton,
    StrongBodyLabel,
)

from orchestrator.core import model_registry
from orchestrator.core.scheduler import ScheduleRunner, plain_when
from orchestrator.core.session_manager import POLICIES
from orchestrator.storage.db import Database
from orchestrator.ui.chat_view import ModelPill
from orchestrator.ui.theme import palette
from orchestrator.ui.when_picker import WhenPicker
from orchestrator.ui_text import short_title


class RunOnceView(FlyoutViewBase):
    scheduled = Signal(int)  # schedule id
    repeatRequested = Signal(dict)

    def __init__(self, db: Database, runner: ScheduleRunner, prefill: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.db = db
        self.runner = runner
        self.prefill = prefill
        muted = f"color:{palette()['muted']};"
        self.setFixedWidth(460)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(10)

        head = QHBoxLayout()
        head.addWidget(StrongBodyLabel("Run once", self))
        head.addStretch(1)
        last = db.get_setting("last") or ["codex", *db.default_model("codex")]
        engine = prefill.get("engine") or last[0]
        model = prefill.get("model") or (last[1] if engine == last[0] else db.default_model(engine)[0])
        effort = prefill.get("effort") or (last[2] if engine == last[0] else db.default_model(engine)[1])
        if not prefill.get("session_id"):  # new chat: start from an allowed model
            engine, model, effort = model_registry.pick(db, engine, model, effort) or (engine, model, effort)
        self.pill = ModelPill(db, self)
        self.pill.set_value(engine, model, effort, locked=bool(prefill.get("session_id")))
        self.pill.changed.connect(lambda e, m, f: self.pill.set_value(e, m, f, self.pill.engine_locked))
        head.addWidget(self.pill)
        lay.addLayout(head)

        self.prompt = PlainTextEdit(self)
        self.prompt.setPlaceholderText("What should run later?")
        self.prompt.setPlainText(prefill.get("prompt", ""))
        self.prompt.setFixedHeight(84)
        lay.addWidget(self.prompt)

        self.picker = WhenPicker(self)  # In 1 hour / In 5 hours (default) / Tonight / Tomorrow / Custom
        self.picker.changed.connect(self._update)
        lay.addWidget(self.picker)

        opts = QHBoxLayout()
        self.cont = CheckBox("Continue this chat", self)
        self.cont.setVisible(bool(prefill.get("session_id")))
        self.cont.setChecked(bool(prefill.get("session_id")))
        self.project = ComboBox(self)
        for p in db.projects():
            self.project.addItem(p.name, userData=p.id)
        if prefill.get("project_id"):
            self.project.setCurrentIndex(max(0, self.project.findData(prefill["project_id"])))
        self.project.setVisible(not prefill.get("session_id"))
        self.perm = ComboBox(self)
        for k, v in POLICIES.items():
            self.perm.addItem(v["label"], userData=k)
        self.perm.setToolTip("Nobody is there to click Allow, so tools outside this policy are denied.")
        opts.addWidget(self.cont)
        opts.addWidget(self.project, 1)
        opts.addStretch(1)
        opts.addWidget(self.perm)
        lay.addLayout(opts)

        self.summary = CaptionLabel("", self)
        self.summary.setStyleSheet(muted)
        lay.addWidget(self.summary)

        foot = QHBoxLayout()
        self.repeat = HyperlinkButton("", "Repeat…", self)
        self.repeat.setToolTip("Run on a schedule instead (every weekday, every hour, cron…)")
        self.repeat.clicked.connect(self._repeat)
        self.go = PrimaryPushButton("Schedule", self)
        self.go.setMinimumWidth(self.go.sizeHint().width())
        self.go.clicked.connect(self._schedule)
        foot.addWidget(self.repeat)
        foot.addStretch(1)
        foot.addWidget(self.go)
        lay.addLayout(foot)

        self._update()

    def when(self) -> datetime | None:
        return self.picker.when()

    def _update(self) -> None:
        w = self.when()
        bad = not w or w <= datetime.now()
        self.summary.setText("Pick a time in the future" if bad else "Then it moves to history on the Schedules page.")
        self.go.setEnabled(not bad)
        self.adjustSize()

    def data(self) -> dict:
        resume = bool(self.prefill.get("session_id")) and self.cont.isChecked()
        text = self.prompt.toPlainText().strip()
        return {
            "name": short_title(text, 40) or "Run once",
            "project_id": self.prefill.get("project_id") if self.prefill.get("session_id") else self.project.currentData(),
            "prompt": text,
            "engine": self.pill.engine, "model": self.pill.model, "effort": self.pill.effort,
            "kind": "once", "cron": None, "run_at": (self.when() or datetime.now()).timestamp(),
            "new_or_resume": "resume" if resume else "new",
            "session_id": self.prefill.get("session_id") if resume else None,
            "permission": self.perm.currentData(), "missed": "run_once", "enabled": True,
        }

    def _schedule(self) -> None:
        d = self.data()
        if not model_registry.is_allowed(self.db, d["engine"], d["model"] or ""):
            self.summary.setText(model_registry.disabled_message(self.db, d["engine"], d["model"] or ""))
            return
        if not d["prompt"]:
            self.summary.setText("Write the prompt first")
            return
        if not d["project_id"]:
            self.summary.setText("Add a project folder first")
            return
        sid = self.db.save_schedule(d)
        self.runner.sync(self.db.schedule(sid))
        self.scheduled.emit(sid)

    def _repeat(self) -> None:
        d = self.data()
        d.update(kind="cron", cron="0 8 * * mon-fri", run_at=None)
        self.repeatRequested.emit(d)


def keep_inside(fly, window) -> None:
    """Shift a flyout left so it does not hang over the window's right edge (buttons near the edge)."""
    from PySide6.QtCore import QTimer

    def fix() -> None:
        try:
            right = window.mapToGlobal(window.rect().topRight()).x() - 8
            if fly.x() + fly.width() > right:
                fly.move(max(window.mapToGlobal(window.rect().topLeft()).x() + 8, right - fly.width()), fly.y())
        except RuntimeError:  # closed before the animation finished
            pass

    QTimer.singleShot(0, fix)
    QTimer.singleShot(260, fix)  # after the drop-down animation settles


def open_run_once(window, target: QWidget, db: Database, runner: ScheduleRunner, prefill: dict,
                  on_repeat=None) -> Flyout:
    view = RunOnceView(db, runner, prefill)
    fly = Flyout.make(view, target, window, FlyoutAnimationType.DROP_DOWN if target.y() < 200
                      else FlyoutAnimationType.PULL_UP)
    keep_inside(fly, window)

    def done(sid: int) -> None:
        fly.close()
        s = db.schedule(sid)
        InfoBar.success("Scheduled", f"“{s.name}” runs {plain_when(datetime.fromtimestamp(s.run_at))}", parent=window,
                        position=InfoBarPosition.TOP_RIGHT, duration=3500)

    def repeat(d: dict) -> None:
        fly.close()
        if on_repeat:
            on_repeat(d)

    view.scheduled.connect(done)
    view.repeatRequested.connect(repeat)
    view.prompt.setFocus()
    return fly
