"""Orchestrate: give a goal, a planner chat splits it into sub-agent tasks; live board of their cards."""
from __future__ import annotations

import asyncio
import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QButtonGroup, QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    LineEdit,
    MessageBoxBase,
    PillPushButton,
    RadioButton,
    SubtitleLabel,
    CaptionLabel,
    CheckBox,
    ComboBox,
    FluentIcon as FIF,
    InfoBar,
    InfoBarPosition,
    PlainTextEdit,
    PrimaryPushButton,
    PushButton,
    SmoothScrollArea,
    SpinBox,
    StrongBodyLabel,
    TransparentPushButton,
    TransparentToolButton,
)

from orchestrator.core import model_registry
from orchestrator.core.catalog import ENGINES
from orchestrator.core.orchestration import ACTIVE, OrchestrationError, OrchestrationManager, strip_report
from orchestrator.core.session_manager import SessionManager
from orchestrator.storage.db import Database, Orchestration, Task
from orchestrator.ui.chat_view import ModelPill
from orchestrator.ui.theme import STATUS_COLORS, engine_badge, palette
from orchestrator.ui_text import relative, usage_text

TASK_COLORS = {"proposed": STATUS_COLORS["waiting"], "queued": STATUS_COLORS["idle"], "running": STATUS_COLORS["running"],
               "waiting": STATUS_COLORS["waiting"], "done": STATUS_COLORS["done"],
               "failed": STATUS_COLORS["error"], "stopped": STATUS_COLORS["idle"],
               "error": STATUS_COLORS["error"]}
TASK_LABELS = {"proposed": "Needs OK", "queued": "Queued", "running": "Running", "waiting": "Waiting on you", "done": "Done",
               "failed": "Failed", "stopped": "Stopped", "error": "Failed"}


def fmt_elapsed(sec: float) -> str:
    sec = int(max(0, sec))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m {sec % 60:02d}s"
    return f"{sec // 3600}h {(sec % 3600) // 60:02d}m"


def status_pill(status: str, parent: QWidget) -> QLabel:
    col = TASK_COLORS.get(status, "#888")
    lb = QLabel(f"●  {TASK_LABELS.get(status, status)}", parent)
    lb.setObjectName("statusPill")
    lb.setStyleSheet(f"QLabel#statusPill {{ color:{col}; background:transparent; border:1px solid {col};"
                     f" border-radius:9px; padding:1px 9px; font-size:8.5pt; }}")
    return lb


def model_line(db: Database, engine: str, model: str | None, effort: str | None) -> str:
    return f"{ENGINES.get(engine, engine)} · {model_registry.label(db, engine, model or '')} · {effort or 'default'}"


class TaskCard(QFrame):
    openChat = Signal(int)

    def __init__(self, page: "OrchestratePage", t: Task) -> None:
        super().__init__(page.board)
        self.page, self.tid, self.sid = page, t.id, t.session_id
        p = palette()
        self.setObjectName("taskCard")
        border = TASK_COLORS["waiting"] if t.status == "waiting" else p["border"]
        self.setStyleSheet(f"#taskCard {{ background:{p['card']}; border:1px solid {border}; border-radius:12px; }}")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 12, 12)
        lay.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(10)
        badge = QLabel(self)
        badge.setPixmap(engine_badge(t.engine, 24))
        badge.setFixedSize(24, 24)
        top.addWidget(badge, 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(0)
        title = StrongBodyLabel(t.title, self)
        title.setWordWrap(True)
        col.addWidget(title)
        self.sub = CaptionLabel("", self)
        self.sub.setStyleSheet(f"color:{p['muted']};")
        col.addWidget(self.sub)
        top.addLayout(col, 1)
        top.addWidget(status_pill(t.status, self), 0, Qt.AlignTop)
        lay.addLayout(top)

        if t.pick_reason:
            tags = []
            if t.reused:
                tags.append("♻ reused")
            if t.escalated:
                tags.append("⤴ escalated")
            elif t.auto_picked:
                tags.append("auto")
            why = CaptionLabel(("[" + " · ".join(tags) + "]  " if tags else "") + t.pick_reason, self)
            why.setObjectName("pickReason")
            why.setWordWrap(True)
            why.setStyleSheet(f"color:{p['muted']}; padding-left:34px; font-style:italic;")
            lay.addWidget(why)

        self.last = CaptionLabel("", self)
        self.last.setStyleSheet(f"color:{p['faint']}; padding-left:34px;")
        self.last.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        lay.addWidget(self.last)

        self.proposal = None
        if t.status == "proposed":
            self.proposal = self._proposal_box(t)
            lay.addWidget(self.proposal)
            self.last.hide()

        rep = t.result or {}
        if t.status in ("done", "failed", "stopped") and rep:
            summ = BodyLabel(rep.get("summary") or "", self)
            summ.setWordWrap(True)
            summ.setStyleSheet(f"color:{p['text']}; padding-left:34px;")
            lay.addWidget(summ)
            extra = []
            if rep.get("files_changed"):
                files = rep["files_changed"]
                extra.append("Files: " + ", ".join(files[:4]) + (f" +{len(files) - 4}" if len(files) > 4 else ""))
            if rep.get("open_questions"):
                extra.append("Questions: " + "; ".join(rep["open_questions"][:2]))
            if t.worktree:
                extra.append(f"Worktree: {t.worktree}")
            for e in extra:
                lb = CaptionLabel(e, self)
                lb.setWordWrap(True)
                lb.setStyleSheet(f"color:{p['muted']}; padding-left:34px;")
                lay.addWidget(lb)
            self.last.hide()

        self.approval = QWidget(self)
        al = QHBoxLayout(self.approval)
        al.setContentsMargins(34, 2, 0, 0)
        self.approvalText = CaptionLabel("", self.approval)
        self.approvalText.setStyleSheet(f"color:{TASK_COLORS['waiting']};")
        self.approvalText.setWordWrap(True)
        allow = PrimaryPushButton("Allow", self.approval)
        deny = PushButton("Deny", self.approval)
        allow.clicked.connect(lambda: self._answer(True))
        deny.clicked.connect(lambda: self._answer(False))
        al.addWidget(self.approvalText, 1)
        al.addWidget(allow)
        al.addWidget(deny)
        self.approval.hide()
        lay.addWidget(self.approval)

        btns = QHBoxLayout()
        btns.setContentsMargins(26, 0, 0, 0)
        btns.setSpacing(2)
        openb = TransparentPushButton(FIF.CHAT, "Open chat", self)
        openb.clicked.connect(lambda: self.openChat.emit(self.sid) if self.sid else None)
        btns.addWidget(openb)
        if t.status not in ("proposed", "queued"):
            diffb = TransparentPushButton(FIF.DOCUMENT, "Changes", self)
            diffb.setToolTip("Git changes in this task's folder" + (" (its worktree)" if t.worktree else ""))
            diffb.clicked.connect(lambda: self._diff())
            btns.addWidget(diffb)
        if t.status in ("proposed", "queued"):
            self.pick = ModelPill(page.db, self)
            self.pick.set_value(t.engine, t.model or "", t.effort or "", locked=bool(t.reused))
            self.pick.setToolTip("Change the engine, model or effort before it starts")
            self.pick.changed.connect(lambda e, m, ef: self._change(e, m, ef))
            btns.addWidget(self.pick)
        if t.status in ACTIVE:
            stop = TransparentPushButton(FIF.PAUSE, "Stop", self)
            stop.clicked.connect(lambda: asyncio.ensure_future(page.om.stop_task(self.tid)))
            btns.addWidget(stop)
        else:
            retry = TransparentPushButton(FIF.SYNC, "Retry", self)
            retry.clicked.connect(lambda: page.om.retry_task(self.tid))
            btns.addWidget(retry)
        btns.addStretch(1)
        lay.addLayout(btns)
        self.refresh(t)

    def _proposal_box(self, t: Task) -> QWidget:
        p = palette()
        prop = t.proposal or {}
        box = QFrame(self)
        box.setObjectName("proposal")
        box.setStyleSheet(f"#proposal {{ background:{p['warn_bg']}; border-radius:8px; }}")
        bl = QVBoxLayout(box)
        bl.setContentsMargins(12, 8, 8, 8)
        bl.setSpacing(6)
        if prop.get("kind") == "reuse":
            s = self.page.db.session(int(prop.get("session_id") or 0))
            when = relative(s.last_active) if s else ""
            text = (f"Will continue “{s.title}” ({ENGINES.get(s.engine, s.engine)}, {when})" if s
                    else "Will continue an earlier chat")
            buttons = [("Use it", "accept", True), ("Start fresh", "fresh", False), ("Pick another", "pick", False)]
        else:
            text = f"Auto picked {model_line(self.page.db, t.engine, t.model, t.effort)}. Start it, or change the model first."
            buttons = [("Start", "accept", True)]
        self.proposalText = BodyLabel(text, box)
        self.proposalText.setWordWrap(True)
        bl.addWidget(self.proposalText)
        row = QHBoxLayout()
        row.setSpacing(6)
        for label, action, primary in buttons:
            b = (PrimaryPushButton if primary else PushButton)(label, box)
            b.clicked.connect(lambda _=False, a=action: self._resolve(a))
            row.addWidget(b)
        row.addStretch(1)
        self.countdown = CaptionLabel("", box)
        self.countdown.setStyleSheet(f"color:{p['muted']};")
        row.addWidget(self.countdown)
        bl.addLayout(row)
        return box

    def _resolve(self, action: str) -> None:
        t = self.page.db.task(self.tid)
        if not t:
            return
        if action == "pick":
            cands = [c for c in (t.proposal or {}).get("candidates", [])]
            dlg = PickSessionDialog(cands, self.window())
            if not dlg.exec() or dlg.choice is None:
                return
            self.page.om.resolve_proposal(self.tid, "pick", dlg.choice)
        else:
            self.page.om.resolve_proposal(self.tid, action)

    def _diff(self) -> None:
        from orchestrator.ui.diff_view import DiffDialog

        t = self.page.db.task(self.tid)
        if t:
            DiffDialog(t.worktree or t.cwd or "", self.window(), f"Changes · {t.title}").exec()

    def _change(self, engine: str, model: str, effort: str) -> None:
        try:
            self.page.om.change_pick(self.tid, engine, model, effort)
        except Exception as exc:  # noqa: BLE001 - disabled model etc.
            InfoBar.warning("Can't use that model", str(exc), parent=self.window(),
                            position=InfoBarPosition.TOP_RIGHT, duration=4000)

    def refresh(self, t: Task) -> None:
        if self.proposal is not None and (t.proposal or {}).get("deadline"):
            left = max(0, int(round(t.proposal["deadline"] - time.time())))
            self.countdown.setText(f"Starts in {left}s")
        elif self.proposal is not None:
            self.countdown.setText("Waiting for you")
        end = t.finished_at or time.time()
        el = fmt_elapsed(end - t.started_at) if t.started_at else "not started"
        u = usage_text(self.page.db.usage(t.session_id), short=True) if t.session_id else ""
        self.sub.setText(f"{model_line(self.page.db, t.engine, t.model, t.effort)}  ·  {el}" + (f"  ·  {u}" if u else ""))
        if t.status in ("running", "waiting", "queued") and t.session_id:
            line = self.page.om.last_line(t.session_id) if t.status != "queued" else "Waiting for a free slot"
            self.last.setText(line or "Starting…")
        pend = self.page.pending.get(t.session_id or -1)
        if pend and not pend[1].done():
            req = pend[0]
            self.approvalText.setText(f"Wants to use {req.tool}" + (f": {req.detail}" if req.detail else ""))
            self.approval.show()
            self.last.hide()
        else:
            self.approval.hide()
            if t.status in ("queued", "running", "waiting"):
                self.last.show()

    def _answer(self, ok: bool) -> None:
        pend = self.page.pending.pop(self.sid or -1, None)
        if pend and not pend[1].done():
            pend[1].set_result(ok)
        self.approval.hide()


class PickSessionDialog(MessageBoxBase):
    """Pick another earlier chat to continue (candidates from find_sessions)."""

    def __init__(self, candidates: list[dict], parent: QWidget) -> None:
        super().__init__(parent)
        self.choice: int | None = None
        self.viewLayout.addWidget(SubtitleLabel("Continue which chat?", self))
        self.group = QButtonGroup(self)
        p = palette()
        if not candidates:
            self.viewLayout.addWidget(BodyLabel("No other matching chats were found.", self))
        for i, c in enumerate(candidates):
            rb = RadioButton(f"{c['title']}  ·  {c['engine_label']}  ·  {c['last_active']}", self)
            self.group.addButton(rb, i)
            self.viewLayout.addWidget(rb)
            why = CaptionLabel(f"{c['why']}\n{(c.get('summary') or '')[:160]}", self)
            why.setWordWrap(True)
            why.setStyleSheet(f"color:{p['muted']}; padding-left:28px;")
            self.viewLayout.addWidget(why)
        if candidates:
            self.group.button(0).setChecked(True)
        self.cands = candidates
        self.yesButton.setText("Continue it")
        self.cancelButton.setText("Cancel")
        self.widget.setMinimumWidth(520)

    def validate(self) -> bool:  # noqa: D401 - MessageBoxBase hook
        i = self.group.checkedId()
        if i < 0 or i >= len(self.cands):
            return False
        self.choice = int(self.cands[i]["session_ref"].split(":", 1)[1])
        return True


class OrchestratePage(SmoothScrollArea):
    openChat = Signal(int)

    def __init__(self, db: Database, mgr: SessionManager, om: OrchestrationManager, parent: QWidget | None = None):
        super().__init__(parent)
        self.db, self.mgr, self.om = db, mgr, om
        self.current: int | None = None  # orchestration id shown on the board
        self.pending: dict[int, tuple] = {}  # session id -> (NeedsApproval, future)
        self.cards: dict[int, TaskCard] = {}
        self.setObjectName("orchestratePage")
        self.setWidgetResizable(True)
        self.setStyleSheet("QScrollArea { background:transparent; border:none; }")
        body = QWidget()
        body.setStyleSheet("background:transparent;")
        self.setWidget(body)
        outer = QHBoxLayout(body)
        outer.setContentsMargins(0, 0, 0, 0)
        self.column = QWidget(body)
        self.column.setMaximumWidth(1100)
        outer.addStretch(1)
        outer.addWidget(self.column, 100)
        outer.addStretch(1)
        lay = QVBoxLayout(self.column)
        lay.setContentsMargins(36, 28, 36, 36)
        lay.setSpacing(14)
        p = palette()

        head = QHBoxLayout()
        title = QLabel("Orchestrate", self.column)
        f = QFont("Georgia")
        f.setStyleHint(QFont.Serif)
        f.setPointSize(26)
        title.setFont(f)
        title.setStyleSheet(f"color:{p['text']}; background:transparent;")
        head.addWidget(title)
        head.addStretch(1)
        self.project = ComboBox(self.column)
        self.project.setMinimumWidth(200)
        self.project.currentIndexChanged.connect(self._project_changed)
        head.addWidget(self.project)
        self.history = ComboBox(self.column)
        self.history.setMinimumWidth(260)
        self.history.currentIndexChanged.connect(self._history_changed)
        head.addWidget(self.history)
        self.newRun = PushButton(FIF.ADD, "New run", self.column)
        self.newRun.clicked.connect(lambda: self.history.setCurrentIndex(0))
        head.addWidget(self.newRun)
        lay.addLayout(head)

        # composer
        self.composer = QFrame(self.column)
        self.composer.setObjectName("composer")
        self.composer.setStyleSheet(f"#composer {{ background:{p['card']}; border:1px solid {p['border']}; border-radius:14px; }}")
        cl = QVBoxLayout(self.composer)
        cl.setContentsMargins(16, 14, 16, 12)
        cl.setSpacing(10)
        self.goal = PlainTextEdit(self.composer)
        self.goal.setPlaceholderText("What should get done? The planner splits it into tasks and runs sub-agents "
                                     "(Claude or Codex) in this project, then reports back.")
        self.goal.setFixedHeight(96)
        cl.addWidget(self.goal)
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(CaptionLabel("Planner", self.composer))
        self.pill = ModelPill(db, self.composer)
        self.pill.changed.connect(lambda e, m, ef: self.pill.set_value(e, m, ef, False))
        row.addWidget(self.pill)
        self.autoPlanner = CheckBox("Auto", self.composer)
        self.autoPlanner.setToolTip("Let the app pick the planner's engine and strength from the goal (enabled models only)")
        self.autoPlanner.setChecked(bool(db.get_setting("orchestrate.auto_planner", False)))
        self.autoPlanner.toggled.connect(self._auto_toggled)
        row.addWidget(self.autoPlanner)
        row.addSpacing(8)
        row.addWidget(CaptionLabel("Max parallel", self.composer))
        self.maxPar = SpinBox(self.composer)
        self.maxPar.setRange(1, 8)
        self.maxPar.setValue(int(db.get_setting("orchestrate.max_parallel", 4)))
        self.maxPar.valueChanged.connect(lambda v: db.set_setting("orchestrate.max_parallel", int(v)))
        row.addWidget(self.maxPar)
        self.worktrees = CheckBox("Git worktree per task", self.composer)
        self.worktrees.setToolTip("When two sub-agents write in the same git repo at once, give each its own "
                                  "worktree and branch (orchestrator/task-N). Off by default.")
        self.worktrees.setChecked(bool(db.get_setting("orchestrate.worktrees", False)))
        self.worktrees.toggled.connect(lambda v: db.set_setting("orchestrate.worktrees", bool(v)))
        row.addWidget(self.worktrees)
        row.addStretch(1)
        self.startBtn = PrimaryPushButton(FIF.PLAY, "Start", self.composer)
        self.startBtn.clicked.connect(self._start)
        row.addWidget(self.startBtn)
        cl.addLayout(row)
        self.composerError = CaptionLabel("", self.composer)
        self.composerError.setStyleSheet(f"color:{STATUS_COLORS['error']};")
        self.composerError.hide()
        cl.addWidget(self.composerError)
        lay.addWidget(self.composer)

        # planner strip
        self.planner = QFrame(self.column)
        self.planner.setObjectName("planner")
        self.planner.setStyleSheet(f"#planner {{ background:{p['card']}; border:1px solid {p['border']}; border-radius:14px; }}")
        pl = QVBoxLayout(self.planner)
        pl.setContentsMargins(16, 12, 12, 12)
        pl.setSpacing(4)
        ptop = QHBoxLayout()
        ptop.setSpacing(10)
        self.plannerBadge = QLabel(self.planner)
        self.plannerBadge.setFixedSize(28, 28)
        ptop.addWidget(self.plannerBadge, 0, Qt.AlignTop)
        pc = QVBoxLayout()
        pc.setSpacing(0)
        self.goalLabel = StrongBodyLabel("", self.planner)
        self.goalLabel.setWordWrap(True)
        pc.addWidget(self.goalLabel)
        self.plannerSub = CaptionLabel("", self.planner)
        self.plannerSub.setStyleSheet(f"color:{p['muted']};")
        pc.addWidget(self.plannerSub)
        ptop.addLayout(pc, 1)
        self.plannerStatus = QWidget(self.planner)
        QHBoxLayout(self.plannerStatus).setContentsMargins(0, 0, 0, 0)
        ptop.addWidget(self.plannerStatus, 0, Qt.AlignTop)
        self.openPlanner = TransparentToolButton(FIF.CHAT, self.planner)
        self.openPlanner.setToolTip("Open the planner chat")
        self.openPlanner.clicked.connect(self._open_planner)
        ptop.addWidget(self.openPlanner, 0, Qt.AlignTop)
        self.stopAll = PushButton(FIF.CLOSE, "Stop all", self.planner)
        self.stopAll.clicked.connect(self._stop_all)
        ptop.addWidget(self.stopAll, 0, Qt.AlignTop)
        pl.addLayout(ptop)
        self.plannerLast = CaptionLabel("", self.planner)
        self.plannerLast.setStyleSheet(f"color:{p['faint']}; padding-left:38px;")
        self.plannerLast.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.plannerLast.setWordWrap(True)
        pl.addWidget(self.plannerLast)
        self.plannerWhy = CaptionLabel("", self.planner)
        self.plannerWhy.setStyleSheet(f"color:{p['muted']}; padding-left:38px; font-style:italic;")
        pl.addWidget(self.plannerWhy)
        self.questions = QWidget(self.planner)
        self.qlay = QVBoxLayout(self.questions)
        self.qlay.setContentsMargins(38, 6, 0, 0)
        self.qlay.setSpacing(8)
        pl.addWidget(self.questions)
        self._q_shown: tuple = ()
        lay.addWidget(self.planner)

        self.boardHead = CaptionLabel("", self.column)
        self.boardHead.setStyleSheet(f"color:{p['muted']}; font-weight:600;")
        lay.addWidget(self.boardHead)
        self.board = QWidget(self.column)
        self.grid = QHBoxLayout(self.board)  # masonry: one vertical stack per column
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(12)
        self.cols: list[QVBoxLayout] = []
        for _ in range(2):
            v = QVBoxLayout()
            v.setSpacing(12)
            v.addStretch(1)
            self.grid.addLayout(v, 1)
            self.cols.append(v)
        lay.addWidget(self.board)
        self.emptyBoard = BodyLabel("No tasks yet. The planner is reading the goal and will delegate shortly.", self.column)
        self.emptyBoard.setStyleSheet(f"color:{p['muted']};")
        lay.addWidget(self.emptyBoard)
        lay.addStretch(1)

        self._rebuild_timer = QTimer(self)
        self._rebuild_timer.setSingleShot(True)
        self._rebuild_timer.timeout.connect(self.render_board)
        self._tick = QTimer(self)
        self._tick.timeout.connect(self._refresh_live)
        self._tick.start(1000)
        om.changed.connect(self._on_changed)
        om.task_changed.connect(lambda oid, _t: self._on_changed(oid))
        om.question_asked.connect(self._on_question)
        mgr.approval_requested.connect(self._on_approval)
        self.reload()
        self._auto_toggled(self.autoPlanner.isChecked())

    # ---- data
    def reload(self) -> None:
        cur = self.project.currentData()
        self.project.blockSignals(True)
        self.project.clear()
        for pr in self.db.projects():
            if not pr.auto:
                self.project.addItem(pr.name, userData=pr.id)
        if not self.project.count():
            self.project.addItem("Add a project first", userData=None)
        want = cur or self.db.get_setting("orchestrate.project")
        self.project.setCurrentIndex(max(0, self.project.findData(want)))
        self.project.blockSignals(False)
        last = self.db.get_setting("orchestrate.planner") or ["claude", *self.db.default_model("claude")]
        choice = model_registry.pick(self.db, *last)
        if choice:
            self.pill.set_value(*choice, locked=False)
        self._project_changed()

    def _project_changed(self, *_a) -> None:
        pid = self.project.currentData()
        if pid:
            self.db.set_setting("orchestrate.project", pid)
        runs = self.db.orchestrations(pid) if pid else []
        self.history.blockSignals(True)
        self.history.clear()
        self.history.addItem("New run", userData=None)
        for o in runs:
            g = " ".join(o.goal.split())
            self.history.addItem(f"{g[:38] + '…' if len(g) > 38 else g}  ·  {relative(o.created_at)}", userData=o.id)
        keep = self.current if self.current in [o.id for o in runs] else (runs[0].id if runs else None)
        self.history.setCurrentIndex(max(0, self.history.findData(keep)) if keep else 0)
        self.history.blockSignals(False)
        self.show_run(keep)

    def _history_changed(self, *_a) -> None:
        self.show_run(self.history.currentData())

    def show_run(self, oid: int | None) -> None:
        self.current = oid
        self.composer.setVisible(oid is None)
        self.newRun.setVisible(oid is not None)
        self.composerError.hide()
        self.render_board()

    def render_board(self) -> None:
        for c in self.cards.values():
            c.setParent(None)
            c.deleteLater()
        self.cards.clear()
        orch = self.db.orchestration(self.current) if self.current else None
        self.planner.setVisible(bool(orch))
        self.boardHead.setVisible(bool(orch))
        self.board.setVisible(bool(orch))
        if not orch:
            self.emptyBoard.hide()
            return
        tasks = self.db.tasks(orch.id)
        self._render_planner(orch)
        n_act = sum(1 for t in tasks if t.status in ACTIVE)
        self._q_shown = ()  # questions box is rebuilt with the strip
        n_done = sum(1 for t in tasks if t.status == "done")
        self.boardHead.setText(f"SUB-AGENTS  ·  {len(tasks)} tasks  ·  {n_act} active  ·  {n_done} done  ·  "
                               f"max {orch.max_parallel} at once")
        self.emptyBoard.setVisible(not tasks)
        if not tasks and orch.status != "running":
            self.emptyBoard.setText("This run ended without delegating any tasks. Open the planner chat for details.")
        cols = 1 if self.viewport().width() < 820 else 2
        for i, c in enumerate(self.cols):
            self.grid.setStretch(i, 1 if i < cols else 0)
        for i, t in enumerate(tasks):
            card = TaskCard(self, t)
            card.openChat.connect(self.openChat.emit)
            self.cards[t.id] = card
            col = self.cols[i % cols]
            col.insertWidget(col.count() - 1, card)

    def _render_planner(self, orch: Orchestration) -> None:
        self.plannerBadge.setPixmap(engine_badge(orch.engine, 28))
        self.goalLabel.setText(orch.goal)
        running = bool(orch.session_id and self.mgr.is_running(orch.session_id))
        s = self.db.session(orch.session_id) if orch.session_id else None
        asking = bool(self.db.open_questions(orch.id))
        status = "waiting" if (s and s.status == "waiting") or asking else ("running" if running else
                                                                  {"done": "done", "error": "failed"}.get(orch.status, orch.status))
        if orch.status == "running" and not running and any(t.status in ACTIVE for t in self.db.tasks(orch.id)):
            status = "running"
        lay = self.plannerStatus.layout()
        while lay.count():
            w = lay.takeAt(0).widget()
            if w:
                w.deleteLater()
        lay.addWidget(status_pill(status, self.plannerStatus))
        end = orch.finished_at or time.time()
        sids = [orch.session_id] if orch.session_id else []
        sids += [t.session_id for t in self.db.tasks(orch.id) if t.session_id]
        u = usage_text(self.db.usage(sids), short=True)
        self.plannerSub.setText(f"Planner · {model_line(self.db, orch.engine, orch.model, orch.effort)}  ·  "
                                f"started {relative(orch.created_at)}  ·  {fmt_elapsed(end - orch.created_at)}"
                                + (f"  ·  run total {u}" if u else ""))
        self.stopAll.setEnabled(self.om.is_active(orch.id))
        self.plannerLast.setText(self._planner_line(orch))
        self.plannerWhy.setText(orch.pick_reason or "")
        self.plannerWhy.setVisible(bool(orch.pick_reason))
        self._render_questions(orch)

    def _render_questions(self, orch: Orchestration) -> None:
        qs = self.db.open_questions(orch.id)
        key = tuple(q.id for q in qs)
        if key == self._q_shown:
            return
        self._q_shown = key
        while self.qlay.count():
            w = self.qlay.takeAt(0).widget()
            if w:
                w.deleteLater()
        p = palette()
        for q in qs:
            box = QFrame(self.questions)
            box.setObjectName("question")
            box.setStyleSheet(f"#question {{ background:{p['warn_bg']}; border:1px solid {STATUS_COLORS['waiting']};"
                              f" border-radius:8px; }}")
            bl = QVBoxLayout(box)
            bl.setContentsMargins(12, 8, 8, 8)
            head = CaptionLabel("THE PLANNER ASKS", box)
            head.setStyleSheet(f"color:{STATUS_COLORS['waiting']}; font-weight:600;")
            bl.addWidget(head)
            txt = BodyLabel(q.text, box)
            txt.setWordWrap(True)
            bl.addWidget(txt)
            row = QHBoxLayout()
            row.setSpacing(6)
            for opt in q.options:
                b = PillPushButton(opt, box)
                b.clicked.connect(lambda _=False, qid=q.id, o=opt: self.om.answer(qid, o))
                row.addWidget(b)
            edit = LineEdit(box)
            edit.setPlaceholderText("Type an answer…")
            send = PrimaryPushButton("Answer", box)
            send.clicked.connect(lambda _=False, qid=q.id, e=edit: self.om.answer(qid, e.text()))
            edit.returnPressed.connect(lambda qid=q.id, e=edit: self.om.answer(qid, e.text()))
            row.addWidget(edit, 1)
            row.addWidget(send)
            bl.addLayout(row)
            self.qlay.addWidget(box)

    def _planner_line(self, orch: Orchestration) -> str:
        if not orch.session_id:
            return ""
        pend = self.pending.get(orch.session_id)
        if pend and not pend[1].done():
            return f"Waiting on you: wants to use {pend[0].tool}. Open the planner chat to answer."
        if self.db.open_questions(orch.id):
            return "Waiting on you: the planner asked a question below."
        if self.mgr.is_running(orch.session_id):
            return self.om.last_line(orch.session_id) or "Thinking…"
        msgs = [m for m in self.db.messages(orch.session_id) if m["role"] == "assistant" and m["kind"] == "text"]
        if msgs:
            text = strip_report(msgs[-1]["text"])
            return text[:400] + ("…" if len(text) > 400 else "")
        return ""

    def _refresh_live(self) -> None:
        if not self.isVisible() or not self.current:
            return
        orch = self.db.orchestration(self.current)
        if not orch:
            return
        self._render_planner(orch)
        for tid, card in self.cards.items():
            t = self.db.task(tid)
            if t and (t.status in ACTIVE):
                card.refresh(t)

    def _on_changed(self, oid: int) -> None:
        if oid == self.current:
            self._rebuild_timer.start(120)
        elif self.isVisible():
            pid = self.project.currentData()
            o = self.db.orchestration(oid)
            if o and o.project_id == pid and self.history.findData(oid) < 0:
                self.current = oid
                self._project_changed()

    def _on_question(self, oid: int, qid: int) -> None:
        self._on_changed(oid)
        q = self.db.question(qid)
        if q and self.isVisible():
            InfoBar.warning("The planner asks", q.text[:160], parent=self.window(),
                            position=InfoBarPosition.TOP_RIGHT, duration=6000)

    def _on_approval(self, sid: int, req, fut) -> None:
        t = self.db.task_for_session(sid)
        orch = self.db.orchestration_for_session(sid)
        if not t and not orch:
            return
        self.pending[sid] = (req, fut)
        fut.add_done_callback(lambda _f, s=sid: (self.pending.pop(s, None), self._rebuild_timer.start(50)))
        oid = t.orchestration_id if t else orch.id
        if oid == self.current:
            self._rebuild_timer.start(50)
        if self.isVisible():
            InfoBar.warning("Sub-agent needs you", f"{(t.title if t else 'The planner')} wants to use {req.tool}",
                            parent=self.window(), position=InfoBarPosition.TOP_RIGHT, duration=5000)

    # ---- actions
    def _start(self) -> None:
        pid = self.project.currentData()
        goal = self.goal.toPlainText().strip()
        try:
            if self.autoPlanner.isChecked():
                o = self.om.start(pid or 0, goal, "auto", "", "", self.maxPar.value())
            else:
                o = self.om.start(pid or 0, goal, self.pill.engine, self.pill.model, self.pill.effort,
                                  self.maxPar.value())
        except OrchestrationError as exc:
            self.composerError.setText(str(exc))
            self.composerError.show()
            return
        self.db.set_setting("orchestrate.planner", [self.pill.engine, self.pill.model, self.pill.effort])
        self.goal.clear()
        self.current = o.id
        self._project_changed()

    def _auto_toggled(self, on: bool) -> None:
        self.db.set_setting("orchestrate.auto_planner", bool(on))
        self.pill.setEnabled(not on)

    def _open_planner(self) -> None:
        o = self.db.orchestration(self.current) if self.current else None
        if o and o.session_id:
            self.openChat.emit(o.session_id)

    def _stop_all(self) -> None:
        if self.current:
            asyncio.ensure_future(self.om.stop_all(self.current))

    def resizeEvent(self, e) -> None:  # noqa: N802
        super().resizeEvent(e)
        if self.cards:
            self._rebuild_timer.start(80)
