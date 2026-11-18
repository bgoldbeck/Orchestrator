"""Create / edit a scheduled prompt."""
from __future__ import annotations

from datetime import datetime, timedelta

from PySide6.QtCore import QDate, QDateTime, QTime, Qt
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ComboBox,
    FastCalendarPicker,
    LineEdit,
    MessageBoxBase,
    PlainTextEdit,
    SegmentedWidget,
    SubtitleLabel,
    TimePicker,
)

from orchestrator.core import model_registry
from orchestrator.core.catalog import ENGINES
from orchestrator.core.scheduler import CRON_PRESETS, describe, fmt_when, next_fire
from orchestrator.core.session_manager import POLICIES
from orchestrator.storage.db import Database, Schedule
from orchestrator.ui.theme import palette

CUSTOM = "Custom cron…"


class ScheduleDialog(MessageBoxBase):
    def __init__(self, db: Database, parent: QWidget, sched: Schedule | None = None) -> None:
        super().__init__(parent)
        self.db = db
        self.sched = sched
        self.result_data: dict | None = None
        muted = f"color:{palette()['muted']};"

        self.viewLayout.addWidget(SubtitleLabel("Edit schedule" if sched and sched.id else "New repeating schedule", self))
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)
        row = 0

        def label(text: str) -> BodyLabel:
            lb = BodyLabel(text, self)
            lb.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            return lb

        self.name = LineEdit(self)
        self.name.setPlaceholderText("Morning repo digest")
        grid.addWidget(label("Name"), row, 0)
        grid.addWidget(self.name, row, 1)
        row += 1

        self.project = ComboBox(self)
        for p in db.projects():
            self.project.addItem(p.name + ("  (discovered)" if p.auto else ""), userData=p.id)
        self.project.currentIndexChanged.connect(self._fill_chats)
        grid.addWidget(label("Project"), row, 0)
        grid.addWidget(self.project, row, 1)
        row += 1

        self.prompt = PlainTextEdit(self)
        self.prompt.setPlaceholderText("What should the agent do each time?")
        self.prompt.setFixedHeight(96)
        grid.addWidget(label("Prompt"), row, 0, Qt.AlignTop)
        grid.addWidget(self.prompt, row, 1)
        row += 1

        eng = QHBoxLayout()
        self.engine = ComboBox(self)
        for k, v in ENGINES.items():
            self.engine.addItem(v if model_registry.engine_available(db, k) else f"{v} (no enabled models)", userData=k)
        self.model = ComboBox(self)
        self.effort = ComboBox(self)
        self.engine.currentIndexChanged.connect(self._fill_models)
        self.model.currentIndexChanged.connect(self._fill_efforts)
        eng.addWidget(self.engine)
        eng.addWidget(self.model, 1)
        eng.addWidget(self.effort)
        grid.addWidget(label("Engine"), row, 0)
        grid.addLayout(eng, row, 1)
        row += 1

        self.when = SegmentedWidget(self)
        self.when.addItem("cron", "Repeat")
        self.when.addItem("once", "Once")
        self.when.currentItemChanged.connect(lambda _k: self._update_when())
        grid.addWidget(label("When"), row, 0)
        grid.addWidget(self.when, row, 1)
        row += 1

        self.repeatBox = QWidget(self)
        rl = QHBoxLayout(self.repeatBox)
        rl.setContentsMargins(0, 0, 0, 0)
        self.preset = ComboBox(self.repeatBox)
        for lbl, expr in CRON_PRESETS:
            self.preset.addItem(lbl, userData=expr)
        self.preset.addItem(CUSTOM, userData="")
        self.cron = LineEdit(self.repeatBox)
        self.cron.setPlaceholderText("min hour day month weekday")
        self.cron.setFixedWidth(170)
        self.preset.currentIndexChanged.connect(self._preset_changed)
        self.cron.textChanged.connect(lambda _t: self._update_preview())
        rl.addWidget(self.preset, 1)
        rl.addWidget(self.cron)
        self.onceBox = QWidget(self)
        ol = QHBoxLayout(self.onceBox)
        ol.setContentsMargins(0, 0, 0, 0)
        self.date = FastCalendarPicker(self.onceBox)
        self.time = TimePicker(self.onceBox)
        soon = datetime.now() + timedelta(hours=1)
        self.date.setDate(QDate(soon.year, soon.month, soon.day))
        self.time.setTime(QTime(soon.hour, 0))
        self.date.dateChanged.connect(lambda _d: self._update_preview())
        self.time.timeChanged.connect(lambda _t: self._update_preview())
        ol.addWidget(self.date)
        ol.addWidget(self.time)
        ol.addStretch(1)
        whenBox = QVBoxLayout()
        whenBox.addWidget(self.repeatBox)
        whenBox.addWidget(self.onceBox)
        self.preview = CaptionLabel("", self)
        self.preview.setStyleSheet(muted)
        whenBox.addWidget(self.preview)
        grid.addLayout(whenBox, row, 1)
        row += 1

        tgt = QHBoxLayout()
        self.target = ComboBox(self)
        self.target.addItem("New chat each run", userData="new")
        self.target.addItem("Continue a chat", userData="resume")
        self.chat = ComboBox(self)
        self.target.currentIndexChanged.connect(lambda _i: self.chat.setEnabled(self.target.currentData() == "resume"))
        tgt.addWidget(self.target)
        tgt.addWidget(self.chat, 1)
        grid.addWidget(label("Runs in"), row, 0)
        grid.addLayout(tgt, row, 1)
        row += 1

        self.permission = ComboBox(self)
        for k, v in POLICIES.items():
            self.permission.addItem(v["label"], userData=k)
        perm_hint = CaptionLabel("Nobody is there to click Allow: tools outside this policy are denied.", self)
        perm_hint.setStyleSheet(muted)
        pv = QVBoxLayout()
        pv.setSpacing(2)
        pv.addWidget(self.permission)
        pv.addWidget(perm_hint)
        grid.addWidget(label("Permissions"), row, 0, Qt.AlignTop)
        grid.addLayout(pv, row, 1)
        row += 1

        self.missed = ComboBox(self)
        self.missed.addItem("Run once when the PC is back", userData="run_once")
        self.missed.addItem("Skip it", userData="skip")
        grid.addWidget(label("Missed runs"), row, 0)
        grid.addWidget(self.missed, row, 1)
        row += 1

        self.error = QLabel("", self)
        self.error.setStyleSheet("color:#f85149;")
        self.viewLayout.addLayout(grid)
        self.viewLayout.addWidget(self.error)
        self.widget.setMinimumWidth(640)
        self.yesButton.setText("Save")
        self.cancelButton.setText("Cancel")

        self._load()

    # ---- fill
    def _fill_models(self) -> None:
        engine = self.engine.currentData()
        self.model.clear()
        for m in model_registry.allowed(self.db, engine):
            self.model.addItem(m.label, userData=m.id)
        s = self.sched
        if s and s.engine == engine and s.model and self.model.findData(s.model) < 0:
            # Keep showing the saved (now disabled) model so it is never swapped silently.
            self.model.addItem(f"{model_registry.label(self.db, engine, s.model)} (disabled)", userData=s.model)
        d = model_registry.default(self.db, engine)
        idx = self.model.findData(d[0]) if d else -1
        self.model.setCurrentIndex(max(0, idx))
        self._fill_efforts()

    def _fill_efforts(self) -> None:
        mid = self.model.currentData()
        efforts = next((m.efforts for m in self.db.models(self.engine.currentData()) if m.id == mid), ["low", "medium", "high"])
        efforts = efforts or ["low", "medium", "high"]
        cur = self.effort.currentText()
        self.effort.clear()
        self.effort.addItems(efforts)
        self.effort.setCurrentText(cur if cur in efforts else ("low" if "low" in efforts else efforts[0]))

    def _fill_chats(self) -> None:
        self.chat.clear()
        pid = self.project.currentData()
        for s in self.db.sessions(pid) if pid else []:
            self.chat.addItem(f"{ENGINES.get(s.engine, s.engine)} · {s.title}", userData=s.id)
        if not self.chat.count():
            self.chat.addItem("No chats in this project yet", userData=None)

    def _preset_changed(self) -> None:
        expr = self.preset.currentData()
        if expr:
            self.cron.setText(expr)
        self.cron.setEnabled(not expr)
        self._update_preview()

    def _update_when(self) -> None:
        once = self.when.currentRouteKey() == "once"
        self.repeatBox.setVisible(not once)
        self.onceBox.setVisible(once)
        self._update_preview()

    def _run_at(self) -> float:
        d, t = self.date.getDate(), self.time.getTime()
        return QDateTime(d, t).toSecsSinceEpoch()

    def _update_preview(self) -> None:
        kind = self.when.currentRouteKey() or "cron"
        try:
            nxt = next_fire(kind, self.cron.text().strip(), self._run_at())
            text = describe({"kind": kind, "cron": self.cron.text().strip(), "run_at": self._run_at()})
            self.preview.setText(f"{text}   ·   next run {fmt_when(nxt)}" if nxt else f"{text}   ·   time is in the past")
        except Exception as exc:  # noqa: BLE001
            self.preview.setText(f"Invalid: {exc}")

    def _load(self) -> None:
        s = self.sched
        last = self.db.get_setting("last") or ["codex", *self.db.default_model("codex")]
        engine = s.engine if s else (model_registry.pick(self.db, *last) or last)[0]
        self.engine.setCurrentIndex(max(0, self.engine.findData(engine)))
        self._fill_models()
        if s:
            self.name.setText(s.name)
            self.project.setCurrentIndex(max(0, self.project.findData(s.project_id)))
            self.prompt.setPlainText(s.prompt)
            if s.model:
                self.model.setCurrentIndex(max(0, self.model.findData(s.model)))
            self._fill_efforts()
            if s.effort:
                self.effort.setCurrentText(s.effort)
            self.when.setCurrentItem(s.kind)
            if s.kind == "cron" and s.cron:
                idx = self.preset.findData(s.cron)
                self.preset.setCurrentIndex(idx if idx >= 0 else self.preset.count() - 1)
                self.cron.setText(s.cron)
            elif s.run_at:
                dt = datetime.fromtimestamp(s.run_at)
                self.date.setDate(QDate(dt.year, dt.month, dt.day))
                self.time.setTime(QTime(dt.hour, dt.minute))
            self.target.setCurrentIndex(max(0, self.target.findData(s.new_or_resume)))
            self.permission.setCurrentIndex(max(0, self.permission.findData(s.permission)))
            self.missed.setCurrentIndex(max(0, self.missed.findData(s.missed)))
        else:
            self.when.setCurrentItem("cron")
            self.preset.setCurrentIndex(self.preset.findData("0 8 * * mon-fri"))
        self._fill_chats()
        if s and s.session_id:
            self.chat.setCurrentIndex(max(0, self.chat.findData(s.session_id)))
        self.chat.setEnabled(self.target.currentData() == "resume")
        self._preset_changed()
        self._update_when()

    # ---- validate
    def validate(self) -> bool:  # called by MessageBoxBase on Save
        kind = self.when.currentRouteKey() or "cron"
        data = {
            "name": self.name.text().strip() or "Untitled schedule",
            "project_id": self.project.currentData(),
            "prompt": self.prompt.toPlainText().strip(),
            "engine": self.engine.currentData(),
            "model": self.model.currentData(),
            "effort": self.effort.currentText(),
            "kind": kind,
            "cron": self.cron.text().strip() if kind == "cron" else None,
            "run_at": self._run_at() if kind == "once" else None,
            "new_or_resume": self.target.currentData(),
            "session_id": self.chat.currentData() if self.target.currentData() == "resume" else None,
            "permission": self.permission.currentData(),
            "missed": self.missed.currentData(),
            "enabled": True if not self.sched else self.sched.enabled,
        }
        problem = ""
        if not data["project_id"]:
            problem = "Add a project folder first."
        elif not data["prompt"]:
            problem = "Write the prompt to run."
        elif data["new_or_resume"] == "resume" and not data["session_id"]:
            problem = "Pick the chat to continue."
        elif not model_registry.is_allowed(self.db, data["engine"], data["model"] or "", strict=True):
            problem = model_registry.disabled_message(self.db, data["engine"], data["model"] or "")
        else:
            try:
                nxt = next_fire(kind, data["cron"], data["run_at"])
                if kind == "once" and (not nxt or data["run_at"] < datetime.now().timestamp()):
                    problem = "That time is in the past."
            except Exception as exc:  # noqa: BLE001
                problem = str(exc)
        self.error.setText(problem)
        if problem:
            return False
        self.result_data = data
        return True
