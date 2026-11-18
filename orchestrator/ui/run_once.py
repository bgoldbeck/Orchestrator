"""'Run once' popover: the quick way to schedule a single prompt for later."""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QDate, QDateTime, QTime, Qt, Signal
from PySide6.QtWidgets import QButtonGroup, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CheckBox,
    ComboBox,
    FastCalendarPicker,
    Flyout,
    FlyoutAnimationType,
    FlyoutViewBase,
    HyperlinkButton,
    InfoBar,
    InfoBarPosition,
    PillPushButton,
    PlainTextEdit,
    PrimaryPushButton,
    StrongBodyLabel,
    TimePicker,
)

from orchestrator.core.scheduler import ScheduleRunner, fmt_when, quick_times
from orchestrator.core.session_manager import POLICIES
from orchestrator.storage.db import Database
from orchestrator.ui.chat_view import ModelPill
from orchestrator.ui.theme import palette
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

        chips = QHBoxLayout()
        chips.setSpacing(6)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.times = quick_times()
        for i, (label, _dt) in enumerate(self.times):
            b = PillPushButton(label, self)
            b.setCheckable(True)
            self.group.addButton(b, i)
            chips.addWidget(b)
        self.customBtn = PillPushButton("Custom…", self)
        self.customBtn.setCheckable(True)
        self.group.addButton(self.customBtn, len(self.times))
        chips.addWidget(self.customBtn)
        chips.addStretch(1)
        lay.addLayout(chips)
        self.group.buttonToggled.connect(lambda *_: self._update())

        self.customRow = QWidget(self)
        cr = QHBoxLayout(self.customRow)
        cr.setContentsMargins(0, 0, 0, 0)
        self.date = FastCalendarPicker(self.customRow)
        self.time = TimePicker(self.customRow)
        d = self.times[1][1] if len(self.times) > 1 else datetime.now()
        self.date.setDate(QDate(d.year, d.month, d.day))
        self.time.setTime(QTime(d.hour, d.minute))
        self.date.dateChanged.connect(lambda _x: self._update())
        self.time.timeChanged.connect(lambda _x: self._update())
        cr.addWidget(self.date)
        cr.addWidget(self.time)
        cr.addStretch(1)
        self.customRow.hide()
        lay.addWidget(self.customRow)

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
        self.go.clicked.connect(self._schedule)
        foot.addWidget(self.repeat)
        foot.addStretch(1)
        foot.addWidget(self.go)
        lay.addLayout(foot)

        self.group.button(1 if len(self.times) > 1 else 0).setChecked(True)
        self._update()

    def when(self) -> datetime | None:
        i = self.group.checkedId()
        if i < 0:
            return None
        if i == len(self.times):
            return QDateTime(self.date.getDate(), self.time.getTime()).toPython()
        return self.times[i][1]

    def _update(self) -> None:
        self.customRow.setVisible(self.customBtn.isChecked())
        w = self.when()
        bad = not w or w <= datetime.now()
        self.summary.setText("Pick a time in the future" if bad else
                             f"Runs {fmt_when(w.timestamp())} · then moves to history")
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


def open_run_once(window, target: QWidget, db: Database, runner: ScheduleRunner, prefill: dict,
                  on_repeat=None) -> Flyout:
    view = RunOnceView(db, runner, prefill)
    fly = Flyout.make(view, target, window, FlyoutAnimationType.DROP_DOWN if target.y() < 200
                      else FlyoutAnimationType.PULL_UP)

    def done(sid: int) -> None:
        fly.close()
        s = db.schedule(sid)
        InfoBar.success("Scheduled", f"“{s.name}” runs {fmt_when(s.run_at)}", parent=window,
                        position=InfoBarPosition.TOP_RIGHT, duration=3500)

    def repeat(d: dict) -> None:
        fly.close()
        if on_repeat:
            on_repeat(d)

    view.scheduled.connect(done)
    view.repeatRequested.connect(repeat)
    view.prompt.setFocus()
    return fly
