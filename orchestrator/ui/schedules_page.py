"""Schedules: Fluent cards with next run, last result, toggle and run history."""
from __future__ import annotations

import asyncio
from datetime import datetime

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt, QTimer, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    FluentIcon as FIF,
    HyperlinkButton,
    IconWidget,
    MessageBox,
    PrimaryPushButton,
    PushButton,
    SmoothScrollArea,
    StrongBodyLabel,
    SwitchButton,
    TitleLabel,
    ToolTipFilter,
    TransparentToolButton,
)

from orchestrator.core.catalog import ENGINES
from orchestrator.core.scheduler import ScheduleRunner, describe, fmt_when
from orchestrator.core.session_manager import POLICIES
from orchestrator.storage.db import Database, Schedule
from orchestrator.ui.schedule_dialog import ScheduleDialog
from orchestrator.ui.theme import ENGINE_COLORS, STATUS_COLORS, engine_badge, palette

RUN_COLORS = {"running": STATUS_COLORS["running"], "done": STATUS_COLORS["done"], "error": STATUS_COLORS["error"],
              "skipped": STATUS_COLORS["waiting"], "stopped": STATUS_COLORS["idle"]}


def chip(text: str, parent: QWidget, color: str | None = None) -> QLabel:
    p = palette()
    lb = QLabel(text, parent)
    fg = color or p["muted"]
    lb.setStyleSheet(f"QLabel {{ color:{fg}; background:{p['card_hover']}; border:1px solid {p['border']};"
                     f" border-radius:9px; padding:1px 8px; font-size:8.5pt; }}")
    return lb


class ScheduleCard(QFrame):
    openChat = Signal(int)

    def __init__(self, page: "SchedulesPage", s: Schedule) -> None:
        super().__init__(page)
        self.page = page
        self.s = s
        db = page.db
        p = palette()
        self.setObjectName("schedCard")
        self.setStyleSheet(f"#schedCard {{ background:{p['card']}; border:1px solid {p['border']}; border-radius:12px; }}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 14, 12)
        lay.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(10)
        badge = QLabel(self)
        badge.setPixmap(engine_badge(s.engine, 26))
        badge.setFixedSize(26, 26)
        title = QVBoxLayout()
        title.setSpacing(0)
        name = StrongBodyLabel(s.name, self)
        proj = db.project(s.project_id)
        model_label = next((m.label for m in db.models(s.engine) if m.id == s.model), s.model or "default")
        sub = CaptionLabel(f"{proj.name if proj else '?'}  ·  {ENGINES.get(s.engine, s.engine)} · {model_label} · "
                           f"{s.effort or 'default'}", self)
        sub.setStyleSheet(f"color:{p['muted']};")
        title.addWidget(name)
        title.addWidget(sub)
        top.addWidget(badge, 0, Qt.AlignTop)
        top.addLayout(title, 1)
        self.toggle = SwitchButton(self)
        self.toggle.setOnText("On")
        self.toggle.setOffText("Off")
        self.toggle.setChecked(s.enabled)
        self.toggle.checkedChanged.connect(self._toggled)
        top.addWidget(self.toggle, 0, Qt.AlignTop)
        for icon, tip, fn in ((FIF.PLAY, "Run now", self._run_now), (FIF.EDIT, "Edit", self._edit),
                              (FIF.DELETE, "Delete", self._delete)):
            b = TransparentToolButton(icon, self)
            b.setFixedSize(32, 32)
            b.setToolTip(tip)
            b.installEventFilter(ToolTipFilter(b))
            b.clicked.connect(fn)
            top.addWidget(b, 0, Qt.AlignTop)
        lay.addLayout(top)

        prompt = BodyLabel(s.prompt if len(s.prompt) < 220 else s.prompt[:217] + "…", self)
        prompt.setWordWrap(True)
        prompt.setStyleSheet(f"color:{p['text']}; padding-left:36px;")
        lay.addWidget(prompt)

        meta = QHBoxLayout()
        meta.setContentsMargins(36, 2, 0, 0)
        meta.setSpacing(8)
        meta.addWidget(chip(describe(s), self))
        meta.addWidget(chip(f"Permissions: {POLICIES.get(s.permission, {}).get('label', s.permission)}", self))
        meta.addWidget(chip("New chat each run" if s.new_or_resume == "new" else "Continues a chat", self))
        if s.missed == "skip":
            meta.addWidget(chip("Skips missed runs", self))
        meta.addStretch(1)
        lay.addLayout(meta)

        foot = QHBoxLayout()
        foot.setContentsMargins(36, 4, 0, 0)
        nxt = page.runner.next_run(s.id) if s.enabled else None
        paused = page.runner.paused
        nl = CaptionLabel(("Paused  ·  " if paused and s.enabled else "") +
                          (f"Next run {fmt_when(nxt)}" if nxt else ("Off" if not s.enabled else "No upcoming run")), self)
        nl.setStyleSheet(f"color:{p['muted']};")
        foot.addWidget(nl)
        foot.addStretch(1)
        runs = db.runs(s.id, 8)
        if runs:
            last = runs[0]
            dot = f'<span style="color:{RUN_COLORS.get(last.status, "#888")};">●</span>'
            ll = QLabel(f"{dot}&nbsp; Last run {last.status} · {fmt_when(last.started_at)}", self)
            ll.setStyleSheet(f"color:{p['muted']}; font-size:8.5pt;")
            foot.addWidget(ll)
            self.histBtn = HyperlinkButton("", "History", self)
            self.histBtn.setIcon(FIF.HISTORY)
            self.histBtn.clicked.connect(self._toggle_history)
            foot.addWidget(self.histBtn)
        lay.addLayout(foot)

        self.history = QWidget(self)
        hl = QVBoxLayout(self.history)
        hl.setContentsMargins(36, 4, 0, 0)
        hl.setSpacing(2)
        for r in runs:
            row = QHBoxLayout()
            dot = QLabel("●", self.history)
            dot.setStyleSheet(f"color:{RUN_COLORS.get(r.status, '#888')}; font-size:9pt;")
            when = CaptionLabel(datetime.fromtimestamp(r.started_at).strftime("%a %b %d, %H:%M"), self.history)
            when.setFixedWidth(140)
            dur = f"{int((r.finished_at - r.started_at))} s" if r.finished_at else "running…"
            st = CaptionLabel(f"{r.status} · {dur}" + (f" · {r.note}" if r.note else ""), self.history)
            st.setStyleSheet(f"color:{p['muted']};")
            row.addWidget(dot)
            row.addWidget(when)
            row.addWidget(st, 1)
            if r.session_id and db.session(r.session_id):
                link = HyperlinkButton("", "Open chat", self.history)
                link.clicked.connect(lambda _=False, sid=r.session_id: self.openChat.emit(sid))
                row.addWidget(link)
            hl.addLayout(row)
        self.history.setMaximumHeight(0)
        self.history.hide()
        lay.addWidget(self.history)
        self._anim = QPropertyAnimation(self.history, b"maximumHeight", self)
        self._anim.setDuration(200)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._open = s.id in page.expanded
        if self._open:
            self.history.setMaximumHeight(16777215)
            self.history.show()

    def _toggle_history(self) -> None:
        self._open = not self._open
        (self.page.expanded.add if self._open else self.page.expanded.discard)(self.s.id)
        self.history.show()
        self._anim.stop()
        self._anim.setStartValue(self.history.height() if self._open is False else 0)
        self._anim.setEndValue(self.history.sizeHint().height() if self._open else 0)
        self._anim.start()

    def _toggled(self, on: bool) -> None:
        self.page.db.save_schedule({"enabled": on}, self.s.id)
        self.page.runner.sync(self.page.db.schedule(self.s.id))
        self.page.reload_soon()

    def _run_now(self) -> None:
        asyncio.ensure_future(self.page.runner.fire(self.s.id))

    def _edit(self) -> None:
        self.page.edit(self.s)

    def _delete(self) -> None:
        box = MessageBox("Delete schedule?", f"“{self.s.name}” and its run history will be removed. "
                         "Chats it created are kept.", self.window())
        if box.exec():
            self.page.runner.remove(self.s.id)
            self.page.db.delete_schedule(self.s.id)
            self.page.reload()


class SchedulesPage(SmoothScrollArea):
    openChat = Signal(int)

    def __init__(self, db: Database, runner: ScheduleRunner, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.db = db
        self.runner = runner
        self.expanded: set[int] = set()
        self.setObjectName("schedulesPage")
        self.setWidgetResizable(True)
        self.setStyleSheet("QScrollArea { background:transparent; border:none; }")
        body = QWidget()
        body.setStyleSheet("background:transparent;")
        self.lay = QVBoxLayout(body)
        self.lay.setContentsMargins(36, 28, 36, 36)
        self.lay.setSpacing(12)
        head = QHBoxLayout()
        tcol = QVBoxLayout()
        tcol.setSpacing(2)
        tcol.addWidget(TitleLabel("Schedules", body))
        sub = CaptionLabel("Prompts that run on their own, even with the window closed (Orchestrator keeps running in the tray).", body)
        sub.setStyleSheet(f"color:{palette()['muted']};")
        tcol.addWidget(sub)
        head.addLayout(tcol, 1)
        self.pause = SwitchButton(body)
        self.pause.setOnText("Running")
        self.pause.setOffText("Paused")
        self.pause.setChecked(not runner.paused)
        self.pause.checkedChanged.connect(lambda on: runner.set_paused(not on))
        head.addWidget(self.pause)
        head.addSpacing(12)
        self.repeatBtn = PushButton(FIF.SYNC, "Repeat…", body)
        self.repeatBtn.setToolTip("Run on a schedule: every weekday, every hour, cron…")
        self.repeatBtn.clicked.connect(lambda: self.edit(None))
        self.onceBtn = PrimaryPushButton(FIF.DATE_TIME, "Run once", body)
        self.onceBtn.setToolTip("Run a prompt once, later: in 15 min, tonight, tomorrow morning…")
        self.onceBtn.clicked.connect(lambda: self.run_once({}, self.onceBtn))
        head.addWidget(self.repeatBtn)
        head.addWidget(self.onceBtn)
        self.lay.addLayout(head)
        self.lay.addSpacing(6)
        self.list = QVBoxLayout()
        self.list.setSpacing(12)
        self.lay.addLayout(self.list)
        self.lay.addStretch(1)
        self.setWidget(body)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.reload)
        runner.changed.connect(self.reload_soon)
        self._tick = QTimer(self)
        self._tick.timeout.connect(self.reload)
        self._tick.start(30_000)  # keep "next run in N min" fresh
        self.reload()

    def reload_soon(self) -> None:
        self._timer.start(150)

    def reload(self) -> None:
        self.pause.blockSignals(True)
        self.pause.setChecked(not self.runner.paused)
        self.pause.blockSignals(False)
        while self.list.count():
            it = self.list.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        everything = self.db.schedules()
        # One-shot jobs leave the active list once they have run; they live on in history.
        finished = [s for s in everything if s.kind == "once" and not s.enabled and self.db.runs(s.id, 1)]
        active = [s for s in everything if s not in finished]
        active.sort(key=lambda s: (not s.enabled, s.kind != "once", s.run_at or 0, s.name.lower()))
        if not everything:
            self.list.addWidget(self._empty())
            return
        if active:
            self.list.addWidget(self._section("Scheduled"))
        for s in active:
            card = ScheduleCard(self, s)
            card.openChat.connect(self.openChat.emit)
            self.list.addWidget(card)
        if finished:
            self.list.addWidget(self._section("Ran once"))
            self.list.addWidget(self._history(finished))

    def _section(self, text: str) -> QLabel:
        lb = CaptionLabel(text.upper(), self)
        lb.setStyleSheet(f"color:{palette()['muted']}; font-weight:600; letter-spacing:1px; padding-top:6px;")
        return lb

    def _history(self, items: list[Schedule]) -> QFrame:
        p = palette()
        box = QFrame(self)
        box.setObjectName("onceHist")
        box.setStyleSheet(f"#onceHist {{ background:{p['card']}; border:1px solid {p['border']}; border-radius:12px; }}")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(16, 8, 10, 8)
        lay.setSpacing(0)
        items = sorted(items, key=lambda s: -(self.db.runs(s.id, 1)[0].started_at))
        for s in items[:30]:
            r = self.db.runs(s.id, 1)[0]
            row = QHBoxLayout()
            row.setSpacing(10)
            dot = QLabel("●", box)
            dot.setStyleSheet(f"color:{RUN_COLORS.get(r.status, '#888')}; font-size:9pt;")
            badge = QLabel(box)
            badge.setPixmap(engine_badge(s.engine, 18))
            badge.setFixedSize(18, 18)
            name = BodyLabel(s.name, box)
            when = CaptionLabel(f"{r.status} · {fmt_when(r.started_at)}" + (f" · {r.note}" if r.note else ""), box)
            when.setStyleSheet(f"color:{p['muted']};")
            row.addWidget(dot)
            row.addWidget(badge)
            row.addWidget(name, 1)
            row.addWidget(when)
            if r.session_id and self.db.session(r.session_id):
                link = HyperlinkButton("", "Open chat", box)
                link.clicked.connect(lambda _=False, sid=r.session_id: self.openChat.emit(sid))
                row.addWidget(link)
            again = HyperlinkButton("", "Run again…", box)
            again.clicked.connect(lambda _=False, sc=s, w=again: self.run_once(self._prefill(sc), w))
            row.addWidget(again)
            w = QWidget(box)
            w.setLayout(row)
            w.setFixedHeight(40)
            lay.addWidget(w)
        return box

    @staticmethod
    def _prefill(s: Schedule) -> dict:
        return {"prompt": s.prompt, "engine": s.engine, "model": s.model, "effort": s.effort, "project_id": s.project_id}

    def run_once(self, prefill: dict, target: QWidget) -> None:
        if not self.db.projects():
            MessageBox("Add a project first", "Scheduled prompts run inside a project folder. Add one on the Chats page.",
                       self.window()).exec()
            return
        from orchestrator.ui.run_once import open_run_once

        def scheduled_repeat(d: dict) -> None:
            self.edit_prefilled(d)

        fly = open_run_once(self.window(), target, self.db, self.runner, prefill, on_repeat=scheduled_repeat)
        fly.view.scheduled.connect(lambda _sid: self.reload_soon())

    def edit_prefilled(self, data: dict) -> None:
        from orchestrator.storage.db import Schedule as S

        fake = S(0, data.get("name", ""), data.get("project_id") or 0, data.get("prompt", ""), data.get("engine", "codex"),
                 data.get("model"), data.get("effort"), "cron", data.get("cron") or "0 8 * * mon-fri", None,
                 data.get("new_or_resume", "new"), data.get("session_id"), data.get("permission", "read-only"),
                 "run_once", True)
        dlg = ScheduleDialog(self.db, self.window(), fake)
        if dlg.exec() and dlg.result_data:
            sid = self.db.save_schedule(dlg.result_data)
            self.runner.sync(self.db.schedule(sid))
            self.reload()

    def _empty(self) -> QWidget:
        w = QWidget(self)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 60, 0, 0)
        lay.setSpacing(8)
        ic = IconWidget(FIF.DATE_TIME, w)
        ic.setFixedSize(44, 44)
        t = StrongBodyLabel("No schedules yet", w)
        s = CaptionLabel("Use Run once to send a prompt later (tonight, tomorrow morning…), or Repeat… for every weekday, every hour, or cron.", w)
        s.setStyleSheet(f"color:{palette()['muted']};")
        for x in (ic, t, s):
            lay.addWidget(x, 0, Qt.AlignHCenter)
        return w

    def edit(self, s: Schedule | None) -> None:
        if not self.db.projects():
            MessageBox("Add a project first", "Schedules run inside a project folder. Add one on the Chats page.",
                       self.window()).exec()
            return
        dlg = ScheduleDialog(self.db, self.window(), s)
        if dlg.exec() and dlg.result_data:
            sid = self.db.save_schedule(dlg.result_data, s.id if s and s.id else None)
            self.runner.sync(self.db.schedule(sid))
            self.reload()
