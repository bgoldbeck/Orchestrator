"""Orchestrate: give a goal, a planner chat splits it into sub-agent tasks; live board of their cards."""
from __future__ import annotations

import asyncio
import os
import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QButtonGroup,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    Action,
    BodyLabel,
    DropDownPushButton,
    HyperlinkButton,
    IconWidget,
    RoundMenu,
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
    StrongBodyLabel,
    TransparentPushButton,
    TransparentToolButton,
)

from orchestrator.core import model_registry, project_pick
from orchestrator.core.catalog import ENGINES
from orchestrator.core.orchestration import ACTIVE, OrchestrationError, OrchestrationManager, strip_report
from orchestrator.core.session_manager import SessionManager
from orchestrator.storage.db import Database, Orchestration, Task
from orchestrator.ui.chat_view import ModelPill
from orchestrator.ui.number_box import NumberBox
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


def status_pill(status: str, parent: QWidget, label: str | None = None) -> QLabel:
    col = TASK_COLORS.get(status, "#888")
    lb = QLabel(f"●  {label or TASK_LABELS.get(status, status)}", parent)
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
        pos = page.qpos.get(t.id)
        top.addWidget(status_pill(t.status, self, f"Queued (#{pos})" if t.status == "queued" and pos else None),
                      0, Qt.AlignTop)
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
        openb.setToolTip("Opens this agent's chat next to the orchestrator's chat")
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
            prow = QHBoxLayout()
            prow.setContentsMargins(26, 0, 0, 0)
            prow.addWidget(self.pick)
            prow.addStretch(1)
            lay.addLayout(prow)
        if t.status == "queued" and t.id in page.qpos:
            up = TransparentToolButton(FIF.UP, self)
            up.setToolTip("Move earlier in the queue")
            up.clicked.connect(lambda: page.om.move_task(self.tid, -1))
            down = TransparentToolButton(FIF.DOWN, self)
            down.setToolTip("Move later in the queue")
            down.clicked.connect(lambda: page.om.move_task(self.tid, +1))
            up.setEnabled(page.qpos[t.id] > 1)
            down.setEnabled(page.qpos[t.id] < len(page.qpos))
            btns.addWidget(up)
            btns.addWidget(down)
        if t.status in ACTIVE:
            stop = TransparentPushButton(FIF.CLOSE if t.status == "queued" else FIF.PAUSE,
                                         "Cancel" if t.status == "queued" else "Stop", self)
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
            pos = self.page.qpos.get(t.id)
            line = (self.page.om.last_line(t.session_id) if t.status != "queued" else
                    f"Waiting for a free slot · #{pos} in the queue" if pos else "Waiting for a free slot")
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
    projectsChanged = Signal()

    def __init__(self, db: Database, mgr: SessionManager, om: OrchestrationManager, parent: QWidget | None = None):
        super().__init__(parent)
        self.db, self.mgr, self.om = db, mgr, om
        self.current: int | None = None  # orchestration id shown on the board
        self.pending: dict[int, tuple] = {}  # session id -> (NeedsApproval, future)
        self.cards: dict[int, TaskCard] = {}
        self.qpos: dict[int, int] = {}  # task id -> place in the global queue
        self.runner = None  # ScheduleRunner, set by the main window (Run later / Repeat)
        self._auto_choice = None  # ProjectMatch shown in the "Working in" line
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
        tcol = QVBoxLayout()
        tcol.setSpacing(2)
        title = QLabel("Orchestrate", self.column)
        f = QFont("Georgia")
        f.setStyleHint(QFont.Serif)
        f.setPointSize(26)
        title.setFont(f)
        title.setStyleSheet(f"color:{p['text']}; background:transparent;")
        tcol.addWidget(title)
        explain = CaptionLabel("An orchestration is one goal: a planner splits it into tasks and hands them to "
                               "Claude or Codex sub-agents, then reports back.", self.column)
        explain.setWordWrap(True)
        explain.setStyleSheet(f"color:{p['muted']};")
        head.addLayout(tcol)
        head.addStretch(1)
        self.project = ComboBox(self.column)
        self.project.setMinimumWidth(200)
        self.project.currentIndexChanged.connect(self._project_changed)
        head.addWidget(self.project)
        self.history = ComboBox(self.column)
        self.history.setMinimumWidth(260)
        self.history.currentIndexChanged.connect(self._history_changed)
        head.addWidget(self.history)
        self.newRun = PushButton(FIF.ADD, "New orchestration", self.column)
        self.newRun.setToolTip("Start another goal (this one keeps running)")
        self.newRun.clicked.connect(lambda: self.history.setCurrentIndex(0))
        head.addWidget(self.newRun)
        lay.addLayout(head)
        lay.addWidget(explain)

        # no projects yet: explain what a project is, with the button right here
        self.noProjects = QFrame(self.column)
        self.noProjects.setObjectName("noProjects")
        self.noProjects.setStyleSheet(f"#noProjects {{ background:{p['card']}; border:1px solid {p['border']}; border-radius:14px; }}")
        npl = QVBoxLayout(self.noProjects)
        npl.setContentsMargins(24, 28, 24, 28)
        npl.setSpacing(8)
        ic = IconWidget(FIF.FOLDER_ADD, self.noProjects)
        ic.setFixedSize(36, 36)
        npl.addWidget(ic, 0, Qt.AlignHCenter)
        npl.addWidget(StrongBodyLabel("Add a project to start", self.noProjects), 0, Qt.AlignHCenter)
        nps = BodyLabel("A project is just a folder on your computer, like a code repository. The planner and its "
                        "sub-agents read and change files inside it.", self.noProjects)
        nps.setWordWrap(True)
        nps.setAlignment(Qt.AlignCenter)
        nps.setStyleSheet(f"color:{p['muted']};")
        npl.addWidget(nps)
        self.addProjectBtn = PrimaryPushButton(FIF.FOLDER_ADD, "Add project folder", self.noProjects)
        self.addProjectBtn.clicked.connect(self.add_project)
        npl.addWidget(self.addProjectBtn, 0, Qt.AlignHCenter)
        self.noProjects.hide()
        lay.addWidget(self.noProjects)

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
        row.addWidget(BodyLabel("Run up to", self.composer))
        self.maxPar = NumberBox(self.composer)
        self.maxPar.setObjectName("maxParallel")
        self.maxPar.setToolTip("How many of this goal's tasks may run at the same time. The rest wait in a queue. "
                               "The whole app also has a limit (Settings › Orchestrator).")
        self.maxPar.setRange(1, 8)
        self.maxPar.setValue(int(db.get_setting("orchestrate.max_parallel", 4)))
        self.maxPar.valueChanged.connect(lambda v: db.set_setting("orchestrate.max_parallel", int(v)))
        row.addWidget(self.maxPar)
        row.addWidget(BodyLabel("tasks at once", self.composer))
        self.worktrees = CheckBox("Git worktree per task", self.composer)
        self.worktrees.setToolTip("When two sub-agents write in the same git repo at once, give each its own "
                                  "worktree and branch (orchestrator/task-N). Off by default.")
        self.worktrees.setChecked(bool(db.get_setting("orchestrate.worktrees", False)))
        self.worktrees.toggled.connect(lambda v: db.set_setting("orchestrate.worktrees", bool(v)))
        row.addWidget(self.worktrees)
        row.addStretch(1)
        cl.addLayout(row)
        row = QHBoxLayout()
        row.setSpacing(10)
        self.startBtn = PrimaryPushButton(FIF.PLAY, "Run now", self.composer)
        self.startBtn.clicked.connect(self._start)
        self.laterBtn = DropDownPushButton(FIF.DATE_TIME, "Later", self.composer)
        later = RoundMenu(parent=self.laterBtn)
        later.addAction(Action(FIF.HISTORY, "Run once later…", triggered=lambda: self._schedule("once")))
        later.addAction(Action(FIF.SYNC, "Repeat…", triggered=lambda: self._schedule("repeat")))
        self.laterBtn.setMenu(later)
        for b in (self.startBtn, self.laterBtn):
            b.setMinimumWidth(b.sizeHint().width())
        # Auto project: where this goal will run, and why
        self.whereRow = QWidget(self.composer)
        wr = QHBoxLayout(self.whereRow)
        wr.setContentsMargins(0, 0, 0, 0)
        wr.setSpacing(6)
        self.whereLabel = CaptionLabel("", self.whereRow)
        self.whereLabel.setObjectName("whereLabel")
        self.whereLabel.setWordWrap(True)
        self.whereLabel.setStyleSheet(f"color:{p['muted']};")
        self.whereChange = HyperlinkButton("", "Change", self.whereRow)
        self.whereChange.clicked.connect(self._change_project_menu)
        wr.addWidget(self.whereLabel)
        wr.addWidget(self.whereChange)
        wr.addStretch(1)
        self.whereRow.hide()
        row.addWidget(self.whereRow, 1)
        self.whereSpacer = QWidget(self.composer)
        row.addWidget(self.whereSpacer, 1)
        row.addWidget(self.laterBtn)
        row.addWidget(self.startBtn)
        cl.addLayout(row)
        # Auto project unsure: ask with the top matches
        self.askProject = QFrame(self.composer)
        self.askProject.setObjectName("askProject")
        self.askProject.setStyleSheet(f"#askProject {{ background:{p['warn_bg']}; border:1px solid {STATUS_COLORS['waiting']};"
                                      " border-radius:8px; }")
        self.askLay = QVBoxLayout(self.askProject)
        self.askLay.setContentsMargins(12, 8, 8, 8)
        self.askProject.hide()
        cl.addWidget(self.askProject)
        self._where_timer = QTimer(self)
        self._where_timer.setSingleShot(True)
        self._where_timer.timeout.connect(self._update_where)
        self.goal.textChanged.connect(lambda: self._where_timer.start(350))
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
        mine = [pr for pr in self.db.projects() if not pr.auto]
        if mine:
            self.project.addItem("Auto (pick from goal)", userData="auto")
        for pr in mine:
            self.project.addItem(pr.name, userData=pr.id)
        has = bool(mine)
        self.noProjects.setVisible(not has)
        for w in (self.project, self.history):
            w.setVisible(has)
        want = cur or self.db.get_setting("orchestrate.project", "auto")
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
        auto = pid == "auto"
        runs = self.db.orchestrations(None if auto else pid) if pid else []
        self.history.blockSignals(True)
        self.history.clear()
        self.history.addItem("New orchestration", userData=None)
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
        has = self.project.currentData() is not None
        self.composer.setVisible(oid is None and has)
        self.newRun.setVisible(oid is not None and has)
        self.composerError.hide()
        self.askProject.hide()
        self._update_where()
        self.render_board()

    # ---- projects
    def add_project(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose a project folder")
        if not path:
            return
        pr = self.db.add_project(path, os.path.basename(os.path.normpath(path)) or path)
        self.db.set_setting("orchestrate.project", pr.id)
        self.projectsChanged.emit()
        self.reload()

    def _update_where(self) -> None:
        """Auto project: show where the goal will run (or that the app will ask)."""
        self._auto_choice = None
        if self.project.currentData() != "auto" or self.current is not None:
            self._where_visible(False)
            return
        goal = self.goal.toPlainText().strip()
        if not goal:
            self.whereLabel.setText("Working in: picked from the goal when you run it.")
            self.whereChange.setText("Choose")
            self._where_visible(True)
            return
        best, _m = self._auto_best(goal)
        if best:
            self._auto_choice = best
            self.whereLabel.setText(f"Working in: {best.project.name} — because {best.reason}")
            self.whereChange.setText("Change")
        else:
            self.whereLabel.setText("Working in: not sure yet. I'll ask which project when you run it.")
            self.whereChange.setText("Choose")
        self._where_visible(True)

    def _where_visible(self, on: bool) -> None:
        self.whereRow.setVisible(on)
        self.whereSpacer.setVisible(not on)

    def _change_project_menu(self) -> None:
        menu = RoundMenu(parent=self)
        for pr in self.db.projects():
            if pr.auto:
                continue
            menu.addAction(Action(FIF.FOLDER, pr.name, triggered=lambda _=False, i=pr.id: self._set_project(i)))
        menu.addSeparator()
        menu.addAction(Action(FIF.FOLDER_ADD, "Browse folder…", triggered=self.add_project))
        menu.exec(self.whereChange.mapToGlobal(self.whereChange.rect().bottomLeft()))

    def _set_project(self, pid: int) -> None:
        self.project.setCurrentIndex(max(0, self.project.findData(pid)))

    def _ask_project(self, matches, then) -> None:
        """Low confidence: ask which project, with the top 3 and Browse folder."""
        while self.askLay.count():
            it = self.askLay.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()
            elif it.layout():
                lay = it.layout()
                while lay.count():
                    x = lay.takeAt(0).widget()
                    if x:
                        x.deleteLater()
        head = CaptionLabel("WHICH PROJECT?", self.askProject)
        head.setStyleSheet(f"color:{STATUS_COLORS['waiting']}; font-weight:600;")
        self.askLay.addWidget(head)
        txt = BodyLabel("I couldn't tell from the goal which folder this is for." if matches else
                        "No project matched the goal. Pick one or browse to a folder.", self.askProject)
        txt.setWordWrap(True)
        self.askLay.addWidget(txt)
        row = QHBoxLayout()
        row.setSpacing(6)
        options = [m.project for m in matches[:3]] or [p for p in self.db.projects() if not p.auto][:3]
        for pr in options:
            b = PillPushButton(pr.name, self.askProject)
            b.setToolTip(pr.path)
            b.clicked.connect(lambda _=False, i=pr.id: (self.askProject.hide(), then(i, "you picked it")))
            row.addWidget(b)
        browse = PushButton(FIF.FOLDER, "Browse folder…", self.askProject)

        def pick_folder() -> None:
            path = QFileDialog.getExistingDirectory(self, "Choose a project folder")
            if path:
                pr = self.db.add_project(path, os.path.basename(os.path.normpath(path)) or path)
                self.projectsChanged.emit()
                self.askProject.hide()
                then(pr.id, "you picked the folder")

        browse.clicked.connect(pick_folder)
        row.addWidget(browse)
        row.addStretch(1)
        cancel = TransparentPushButton("Cancel", self.askProject)
        cancel.clicked.connect(self.askProject.hide)
        row.addWidget(cancel)
        self.askLay.addLayout(row)
        self.askProject.show()

    def _resolve_project(self, then) -> None:
        """Calls then(project_id, reason) now, or after asking the user (Auto and unsure)."""
        pid = self.project.currentData()
        if pid != "auto":
            then(pid, None)
            return
        goal = self.goal.toPlainText().strip()
        if not goal:
            self.composerError.setText("Write a goal first.")
            self.composerError.show()
            return
        best, matches = self._auto_best(goal)
        if best:
            then(best.project.id, best.reason)
        else:
            self._ask_project(matches, then)

    def _auto_best(self, goal: str):
        matches = project_pick.rank_projects(self.db, goal)
        best = project_pick.confident(matches)
        mine = [p for p in self.db.projects() if not p.auto]
        if not best and len(mine) == 1:  # only one project: nothing to choose between
            best = project_pick.ProjectMatch(mine[0], 0.0, "it's your only project")
        return best, matches

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
        self.qpos = self.om.queue_positions()
        self._render_planner(orch)
        n_act = sum(1 for t in tasks if t.status in ACTIVE)
        self._q_shown = ()  # questions box is rebuilt with the strip
        n_done = sum(1 for t in tasks if t.status == "done")
        n_q = sum(1 for t in tasks if t.status == "queued")
        self.boardHead.setText(f"SUB-AGENTS  ·  {len(tasks)} tasks  ·  {n_act - n_q} active  ·  {n_q} queued  ·  "
                               f"{n_done} done  ·  up to {orch.max_parallel} at once")
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
        proj = self.db.project(orch.project_id)
        why = []
        if proj:
            why.append(f"Working in {proj.name}" + (f" — because {orch.project_reason}" if orch.project_reason else ""))
        if orch.pick_reason:
            why.append(orch.pick_reason)
        self.plannerWhy.setText("\n".join(why))
        self.plannerWhy.setVisible(bool(why))
        self._render_questions(orch)

    def _render_questions(self, orch: Orchestration) -> None:
        qs = self.db.open_questions(orch.id)
        offers = list(self.om.rule_offers.get(orch.id, []))
        key = (tuple(q.id for q in qs), tuple(o["id"] for o in offers))
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
        for o in offers:
            self.qlay.addWidget(self._offer_box(orch.id, o))

    def _offer_box(self, oid: int, offer: dict) -> QFrame:
        """'Save as a preference rule?' for a 'prefer X for Y' the user said."""
        p = palette()
        box = QFrame(self.questions)
        box.setObjectName("ruleOffer")
        box.setStyleSheet(f"#ruleOffer {{ background:{p['card']}; border:1px solid {p['border']}; border-radius:8px; }}")
        bl = QVBoxLayout(box)
        bl.setContentsMargins(12, 8, 8, 8)
        head = CaptionLabel("SAVE AS A PREFERENCE RULE?", box)
        head.setStyleSheet(f"color:{p['muted']}; font-weight:600;")
        bl.addWidget(head)
        row = QHBoxLayout()
        txt = BodyLabel(offer["text"], box)
        txt.setWordWrap(True)
        row.addWidget(txt, 1)
        no = PushButton("Dismiss", box)
        yes = PrimaryPushButton("Save rule", box)
        yes.setObjectName("saveRule")
        for b in (no, yes):
            b.setMinimumWidth(b.sizeHint().width())
        no.clicked.connect(lambda _=False: self.om.resolve_offer(oid, offer["id"], False))
        yes.clicked.connect(lambda _=False: self._save_offer(oid, offer))
        row.addWidget(no)
        row.addWidget(yes)
        bl.addLayout(row)
        if offer.get("warning"):
            w = CaptionLabel("⚠ " + offer["warning"], box)
            w.setWordWrap(True)
            w.setStyleSheet(f"color:{STATUS_COLORS['waiting']};")
            bl.addWidget(w)
        return box

    def _save_offer(self, oid: int, offer: dict) -> None:
        self.om.resolve_offer(oid, offer["id"], True)
        InfoBar.success("Rule saved", offer["text"] + " — edit it in Settings › Orchestrator", parent=self.window(),
                        position=InfoBarPosition.TOP_RIGHT, duration=4000)

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
        self.composerError.hide()
        self._resolve_project(self._start_in)

    def _start_in(self, pid: int, project_reason: str | None) -> None:
        goal = self.goal.toPlainText().strip()
        try:
            if self.autoPlanner.isChecked():
                o = self.om.start(pid or 0, goal, "auto", "", "", self.maxPar.value(), project_reason=project_reason)
            else:
                o = self.om.start(pid or 0, goal, self.pill.engine, self.pill.model, self.pill.effort,
                                  self.maxPar.value(), project_reason=project_reason)
        except OrchestrationError as exc:
            self.composerError.setText(str(exc))
            self.composerError.show()
            return
        self.db.set_setting("orchestrate.planner", [self.pill.engine, self.pill.model, self.pill.effort])
        self.goal.clear()
        self.current = o.id
        self._project_changed()

    def _schedule(self, kind: str) -> None:
        """Run once later / Repeat: save it as a scheduled orchestration (the Schedules page lists it)."""
        if self.runner is None:
            InfoBar.warning("Schedules are off", "The scheduler is not running in this window.", parent=self.window(),
                            position=InfoBarPosition.TOP_RIGHT, duration=4000)
            return
        goal = self.goal.toPlainText().strip()
        if not goal:
            self.composerError.setText("Write a goal first.")
            self.composerError.show()
            return
        self._resolve_project(lambda pid, why: self._schedule_in(kind, pid, goal))

    def _schedule_in(self, kind: str, pid: int, goal: str) -> None:
        dlg = OrchestrationScheduleDialog(kind, goal, self.window())
        if not dlg.exec():
            return
        data = {"name": "Orchestrate · " + " ".join(goal.split())[:40], "project_id": pid, "prompt": goal,
                "engine": self.pill.engine, "model": self.pill.model, "effort": self.pill.effort,
                "new_or_resume": "new", "permission": "read-only", "missed": "run_once", "enabled": True,
                "target": "orchestration",
                "options": {"max_parallel": self.maxPar.value(), "auto_planner": self.autoPlanner.isChecked()}}
        data.update(dlg.schedule_fields())
        sid = self.db.save_schedule(data)
        s = self.db.schedule(sid)
        self.runner.sync(s)
        self.runner.changed.emit()
        self.goal.clear()
        InfoBar.success("Scheduled", f"{dlg.summary()}. It's listed on the Schedules page.", parent=self.window(),
                        position=InfoBarPosition.TOP_RIGHT, duration=5000)

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


class OrchestrationScheduleDialog(MessageBoxBase):
    """When to run an orchestration later: once (time chips / date + time) or on repeat (presets)."""

    def __init__(self, kind: str, goal: str, parent: QWidget) -> None:
        super().__init__(parent)
        from orchestrator.core.scheduler import CRON_PRESETS
        from orchestrator.ui.when_picker import WhenPicker

        self.kind = kind
        self.viewLayout.addWidget(SubtitleLabel("Run once later" if kind == "once" else "Repeat", self))
        g = " ".join(goal.split())
        cap = CaptionLabel(g[:140] + ("…" if len(g) > 140 else ""), self)
        cap.setWordWrap(True)
        cap.setStyleSheet(f"color:{palette()['muted']};")
        self.viewLayout.addWidget(cap)
        if kind == "once":
            self.picker = WhenPicker(self)
            self.viewLayout.addWidget(self.picker)
        else:
            self.repeat = ComboBox(self)
            for label, expr in CRON_PRESETS:
                if expr != "* * * * *":
                    self.repeat.addItem(label, userData=expr)
            self.repeat.setCurrentIndex(max(0, self.repeat.findData("0 8 * * mon-fri")))
            self.viewLayout.addWidget(self.repeat)
        self.yesButton.setText("Schedule")
        self.widget.setMinimumWidth(460)

    def schedule_fields(self) -> dict:
        if self.kind == "once":
            return {"kind": "once", "run_at": self.picker.when().timestamp(), "cron": None}
        return {"kind": "cron", "cron": self.repeat.currentData(), "run_at": None}

    def summary(self) -> str:
        if self.kind == "once":
            from orchestrator.core.scheduler import plain_when

            return "Runs " + plain_when(self.picker.when())
        return "Repeats: " + self.repeat.currentText()
