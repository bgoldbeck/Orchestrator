"""Board: all orchestration tasks across projects as a four-column board (In queue, Running, Review, Done).

Cards carry a project stripe, a short task id, the project tag, an engine letter, a time pill and the model.
Click a card to open its chat. Review cards get "Looks good" (moves to Done) and, for failures, "Retry".
"""
from __future__ import annotations

import asyncio
import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import (
    Action,
    CaptionLabel,
    ComboBox,
    FluentIcon as FIF,
    PrimaryPushButton,
    PushButton,
    RoundMenu,
    SmoothScrollArea,
    StrongBodyLabel,
    TitleLabel,
    TransparentToolButton,
)

from orchestrator.core import board as B
from orchestrator.core import model_registry
from orchestrator.storage.db import Database, Task
from orchestrator.ui.theme import ENGINE_COLORS, palette, ui_pt
from orchestrator.ui_text import relative

GROUP_TITLES = {"ready": "Ready now", "proposed": "Needs your OK"}
CHIP = {  # status chip on a card: (text, foreground, background)
    "ready": ("Ready now", "#ffffff", "#2b4c7e"),
    "proposed": ("Needs your OK", "#3a2a00", "#f5c451"),
    "waiting": ("Waiting on you", "#3a2a00", "#f5c451"),
    "failed": ("Failed", "#ffffff", "#c2413b"),
    "held": ("Held", "#ffffff", "#c2413b"),
    "silent": ("Silent", "#ffffff", "#718096"),
    "merge": ("Ready to merge", "#ffffff", "#2f855a"),
    "conflict": ("Merge problem", "#ffffff", "#c2413b"),
}
REFRESH_MS = 15_000


def project_color(db: Database, pid: int) -> str:
    from orchestrator.ui.projects_page import project_style

    p = db.project(pid)
    return project_style(p)[1] if p else "#94a3b8"


def _pill(text: str, parent: QWidget, fg: str, bg: str = "transparent", border: str | None = None,
          bold: bool = False) -> QLabel:
    lb = QLabel(text, parent)
    lb.setObjectName("pill")
    lb.setStyleSheet(f"QLabel#pill {{ color:{fg}; background:{bg}; border:1px solid {border or bg};"
                     f" border-radius:9px; padding:1px 8px; font-size:{ui_pt(8.5)}; {'font-weight:600;' if bold else ''} }}")
    return lb


def _engine_tag(engine: str, parent: QWidget) -> QLabel:
    col = ENGINE_COLORS.get(engine, "#888")
    lb = QLabel({"claude": "Claude", "codex": "Codex"}.get(engine, engine), parent)
    lb.setObjectName("engineTag")
    lb.setStyleSheet(f"QLabel#engineTag {{ color:{col}; border:1px solid {col}; border-radius:9px;"
                     f" padding:0px 7px; font-size:{ui_pt(8)}; font-weight:600; background:transparent; }}")
    return lb


def status_line(card: B.BoardCard, now: float | None = None, why: str = "") -> str:
    t = card.task
    now = now or time.time()
    if t.status == "queued":
        why = why or "waiting for a free agent"
        why = why[0].lower() + why[1:] if why else why
        return f"#{card.queue_pos} in line · {why}" if card.queue_pos else why
    if t.status == "checking":
        return "running the project's tests / merging"
    if t.merge_state == "review":
        return "finished · waiting for your review before merging"
    if t.merge_state == "conflict":
        return t.merge_note or "could not merge"
    if t.status == "proposed":
        return "waiting for you to start it"
    if t.status == "running":
        return f"running {B.time_text(t, now)}"
    if t.status == "waiting":
        return "needs your approval"
    when = relative(t.finished_at or t.created_at, now)
    if t.status == "failed":
        return f"failed {when}"
    if t.status == "stopped":
        return f"stopped {when}"
    return f"finished {when}" if card.column == "review" else f"done {when}"


class BoardCardWidget(QFrame):
    clicked = Signal(int)  # session id

    def __init__(self, page: "BoardPage", card: B.BoardCard) -> None:
        super().__init__()
        self.page, self.card = page, card
        t = card.task
        p = palette()
        stripe = project_color(page.db, card.project_id)
        self.setObjectName("boardCard")
        self.setCursor(Qt.PointingHandCursor if t.session_id else Qt.ArrowCursor)
        self.setStyleSheet(
            f"#boardCard {{ background:{p['card']}; border:1px solid {p['border']}; border-left:4px solid {stripe};"
            f" border-radius:8px; }} #boardCard:hover {{ background:{p['card_hover']}; }}")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 9, 10, 9)
        lay.setSpacing(4)

        top = QHBoxLayout()
        top.setSpacing(6)
        ident = CaptionLabel(f"T-{t.id}", self)
        ident.setStyleSheet(f"color:{p['muted']}; font-weight:600;")
        top.addWidget(ident)
        top.addStretch(1)
        proj = page.db.project(card.project_id)
        if proj:
            tag = CaptionLabel(proj.name, self)
            tag.setStyleSheet(f"color:{p['muted']};")
            tag.setToolTip(proj.path)
            top.addWidget(tag)
        top.addWidget(_engine_tag(t.engine, self))
        top.addWidget(_pill(B.time_text(t), self, p["text"], border=p["border"]))
        lay.addLayout(top)

        title = StrongBodyLabel(t.title, self)
        title.setWordWrap(True)
        lay.addWidget(title)

        model = model_registry.label(page.db, t.engine, t.model or "") if t.model else "Auto"
        meta = CaptionLabel(f"{model} · {t.effort or 'default'}" + ("  ·  continued chat" if t.reused else ""), self)
        meta.setStyleSheet(f"color:{p['muted']};")
        lay.addWidget(meta)

        bottom = QHBoxLayout()
        bottom.setSpacing(6)
        silent = page.om.silent_minutes(t) if page.om is not None else 0
        chip_key = ("held" if t.hold else "silent" if silent else "merge" if t.merge_state == "review"
                    else "conflict" if t.merge_state == "conflict"
                    else card.group if card.group in CHIP else t.status if t.status in CHIP else None)
        if chip_key:
            text, fg, bg = CHIP[chip_key]
            bottom.addWidget(_pill(text, self, fg, bg, bold=True))
        why = ""
        if t.status == "queued":
            from orchestrator.core import queue_rules

            why = queue_rules.why_waiting(page.db, t, page.om.paused() if page.om is not None else {})
        line = f"no output for {silent} min" if silent else status_line(card, why=why)
        self.line = CaptionLabel(line, self)
        self.line.setStyleSheet(f"color:{p['muted']};")
        bottom.addWidget(self.line, 1)
        lay.addLayout(bottom)

        summary = ((t.result or {}).get("summary") or "") if card.column in ("review", "done") else ""
        if card.column in ("review", "done"):
            from orchestrator.core import review

            extra = [review.checks_text(t.checks)] if t.checks else []
            if t.merge_state in ("merged", "discarded"):
                extra.append(t.merge_note or t.merge_state)
            if extra:
                ex = CaptionLabel("  ·  ".join(extra), self)
                ex.setStyleSheet(f"color:{'#c2413b' if t.checks and not t.checks.get('ok') else p['muted']};")
                ex.setWordWrap(True)
                lay.addWidget(ex)
        if summary:
            s = CaptionLabel(summary if len(summary) < 180 else summary[:177] + "…", self)
            s.setWordWrap(True)
            lay.addWidget(s)
        actions = self._actions(t)
        if actions:
            lay.addLayout(actions)

    def _actions(self, t: Task) -> QHBoxLayout | None:
        row = QHBoxLayout()
        row.setSpacing(6)
        om = self.page.om
        if self.card.column == "review" and t.merge_state in ("review", "conflict") and om is not None:
            mg = PrimaryPushButton(FIF.ACCEPT, "Merge" if t.merge_state == "review" else "Try merge again", self)
            mg.setToolTip("Merge this task's branch into your checkout, then delete the worktree and branch")
            mg.clicked.connect(lambda: asyncio.ensure_future(om.merge_task(t.id)))
            row.addWidget(mg)
            ds = PushButton(FIF.DELETE, "Discard", self)
            ds.setToolTip("Throw the changes away and delete the worktree and branch")
            ds.clicked.connect(lambda: asyncio.ensure_future(om.discard_task(t.id)))
            row.addWidget(ds)
            diff = TransparentToolButton(FIF.DOCUMENT, self)
            diff.setToolTip("Show the changes")
            diff.clicked.connect(lambda: self.page.show_diff(t.id))
            row.addWidget(diff)
        elif self.card.column == "review":
            ok = PushButton(FIF.ACCEPT, "Looks good", self)
            ok.setToolTip("Move it to Done")
            ok.clicked.connect(lambda: self.page.mark_reviewed(t.id))
            row.addWidget(ok)
            if t.status == "failed" and om is not None:
                retry = PushButton(FIF.SYNC, "Retry", self)
                retry.clicked.connect(lambda: self.page.retry(t.id))
                row.addWidget(retry)
            if (t.result or {}).get("files_changed"):
                diff = TransparentToolButton(FIF.DOCUMENT, self)
                diff.setToolTip("Show the changed files")
                diff.clicked.connect(lambda: self.page.show_diff(t.id))
                row.addWidget(diff)
        elif t.status == "proposed" and om is not None:
            go = PushButton(FIF.PLAY, "Start", self)
            go.clicked.connect(lambda: om.resolve_proposal(t.id, "accept"))
            row.addWidget(go)
        else:
            return None
        row.addStretch(1)
        return row

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802
        if e.button() == Qt.LeftButton and self.card.task.session_id:
            self.clicked.emit(self.card.task.session_id)
        elif e.button() == Qt.RightButton:
            self.page.card_menu(self.card, e.globalPosition().toPoint())
        super().mouseReleaseEvent(e)


class BoardColumn(QWidget):
    def __init__(self, title: str, parent: QWidget) -> None:
        super().__init__(parent)
        p = palette()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        head = QHBoxLayout()
        head.setSpacing(8)
        self.title = StrongBodyLabel(title, self)
        head.addWidget(self.title)
        self.count = CaptionLabel("0", self)
        self.count.setStyleSheet(f"color:{p['muted']};")
        head.addWidget(self.count)
        self.extra = QHBoxLayout()
        head.addLayout(self.extra)
        head.addStretch(1)
        lay.addLayout(head)
        self.scroll = SmoothScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("QScrollArea { background:transparent; border:none; }")
        self.body = QWidget()
        self.body.setStyleSheet("background:transparent;")
        self.list = QVBoxLayout(self.body)
        self.list.setContentsMargins(0, 0, 6, 0)
        self.list.setSpacing(8)
        self.list.addStretch(1)
        self.scroll.setWidget(self.body)
        lay.addWidget(self.scroll, 1)

    def clear(self) -> None:
        while self.list.count() > 1:
            w = self.list.takeAt(0).widget()
            if w:
                w.setParent(None)
                w.deleteLater()

    def add(self, w: QWidget) -> None:
        self.list.insertWidget(self.list.count() - 1, w)


class BoardPage(QWidget):
    openChat = Signal(int)

    def __init__(self, db: Database, om=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.db, self.om = db, om
        self.setObjectName("boardPage")
        self.collapsed: set[str] = set()
        self.board = B.Board()
        p = palette()
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 20, 28, 16)
        root.setSpacing(14)

        bar = QHBoxLayout()
        bar.setSpacing(10)
        bar.addWidget(TitleLabel("Board", self))
        self.agents = _pill("", self, p["text"], border=p["border"], bold=True)
        self.agents.setToolTip("Agents running now, out of the app-wide limit (Settings › Running agents)")
        bar.addWidget(self.agents)
        bar.addStretch(1)
        self.project = ComboBox(self)
        self.project.setMinimumWidth(200)
        self.project.currentIndexChanged.connect(lambda _i: self.reload())
        bar.addWidget(self.project)
        root.addLayout(bar)

        cols = QHBoxLayout()
        cols.setSpacing(16)
        self.cols: dict[str, BoardColumn] = {}
        for key in B.COLUMNS:
            c = BoardColumn(B.COLUMN_TITLES[key], self)
            self.cols[key] = c
            cols.addWidget(c, 1)
        self.reviewAll = TransparentToolButton(FIF.ACCEPT, self.cols["review"])
        self.reviewAll.setToolTip("Mark every finished task as looked at")
        self.reviewAll.setFixedSize(24, 24)
        self.reviewAll.clicked.connect(self.mark_all_reviewed)
        self.cols["review"].extra.addWidget(self.reviewAll)
        root.addLayout(cols, 1)

        self._fill_projects()
        if om is not None:
            om.changed.connect(lambda _oid: self._soon())
            om.task_changed.connect(lambda _oid, _tid: self._soon())
        self._debounce = QTimer(self, singleShot=True, interval=150, timeout=self.reload)
        self._tick = QTimer(self, interval=REFRESH_MS, timeout=self._refresh_if_visible)
        self._tick.start()
        self.reload()

    # ------------------------------------------------------------- data
    def _fill_projects(self) -> None:
        cur = self.project.currentData() if self.project.count() else None
        self.project.blockSignals(True)
        self.project.clear()
        self.project.addItem("All projects", userData=0)
        for p in self.db.projects():
            if not p.auto:
                self.project.addItem(p.name, userData=p.id)
        idx = max(0, self.project.findData(cur)) if cur else 0
        self.project.setCurrentIndex(idx)
        self.project.blockSignals(False)

    def _soon(self) -> None:
        if self.isVisible():
            self._debounce.start()

    def _refresh_if_visible(self) -> None:
        if self.isVisible():
            self.reload()

    def showEvent(self, e) -> None:  # noqa: N802
        self._fill_projects()
        self.reload()
        super().showEvent(e)

    def reload(self) -> None:
        pid = self.project.currentData() or None
        self.board = B.build_board(self.db, pid)
        b = self.board
        self.agents.setText(f"Agents {b.agents_busy} of {b.agents_limit}")
        for key, col in self.cols.items():
            col.clear()
            col.count.setText(str(b.count(key)))
            if key in B.GROUPS:
                for grp, title in B.GROUPS[key]:
                    cards = b.group(key, grp)
                    col.add(self._group_header(key, grp, title, len(cards)))
                    if f"{key}:{grp}" not in self.collapsed:
                        for c in cards:
                            col.add(self._card(c))
            else:
                for c in b.columns[key]:
                    col.add(self._card(c))
            if not b.columns[key]:
                col.add(self._empty(key))
        self.reviewAll.setVisible(any(c.task.status == "done" for c in b.columns["review"]))

    def _card(self, c: B.BoardCard) -> BoardCardWidget:
        w = BoardCardWidget(self, c)
        w.clicked.connect(self.openChat.emit)
        return w

    def _group_header(self, key: str, grp: str, title: str, n: int) -> QWidget:
        name = f"{key}:{grp}"
        arrow = "▸" if name in self.collapsed else "▾"
        b = QPushButton(f"{arrow}  {title.upper()}  ({n})", self)
        b.setObjectName("groupHead")
        b.setCursor(Qt.PointingHandCursor)
        b.setStyleSheet(f"QPushButton#groupHead {{ text-align:left; border:none; background:transparent;"
                        f" color:{palette()['muted']}; font-size:{ui_pt(8.5)}; font-weight:600; padding:4px 2px; }}"
                        f" QPushButton#groupHead:hover {{ color:{palette()['text']}; }}")
        b.clicked.connect(lambda: self._toggle(name))
        return b

    def _toggle(self, name: str) -> None:
        self.collapsed ^= {name}
        self.reload()

    def _empty(self, key: str) -> QWidget:
        text = {"queue": "Nothing waiting.", "running": "No agents running.",
                "review": "Nothing to review.", "done": "Finished tasks land here."}[key]
        lb = CaptionLabel(text, self)
        lb.setStyleSheet(f"color:{palette()['faint']}; padding:6px 2px;")
        return lb

    # ------------------------------------------------------------- actions
    def mark_reviewed(self, tid: int, reviewed: bool = True) -> None:
        B.mark_reviewed(self.db, tid, reviewed)
        self.reload()

    def mark_all_reviewed(self) -> None:
        B.mark_all_reviewed(self.db, self.project.currentData() or None)
        self.reload()

    def retry(self, tid: int) -> None:
        if self.om is not None:
            self.om.retry_task(tid)
        self.reload()

    def show_diff(self, tid: int) -> None:
        from orchestrator.ui.diff_view import DiffDialog

        t = self.db.task(tid)
        if t:
            DiffDialog(t.worktree or t.cwd or "", self.window(), f"Changes · {t.title}").exec()

    def card_menu(self, card: B.BoardCard, pos) -> None:
        t, om = card.task, self.om
        menu = RoundMenu(parent=self)
        if t.session_id:
            menu.addAction(Action(FIF.CHAT, "Open chat", triggered=lambda: self.openChat.emit(t.session_id)))
        if om is not None and t.status == "queued":
            menu.addAction(Action(FIF.UP, "Run sooner", triggered=lambda: om.move_task(t.id, -1)))
            menu.addAction(Action(FIF.DOWN, "Run later", triggered=lambda: om.move_task(t.id, 1)))
        if om is not None and t.hold:
            menu.addAction(Action(FIF.PLAY, "Run anyway", triggered=lambda: om.run_anyway(t.id)))
        if om is not None and t.status in ("queued", "proposed", "running", "waiting"):
            menu.addAction(Action(FIF.CLOSE, "Stop", triggered=lambda: asyncio.ensure_future(om.stop_task(t.id))))
        if card.column == "review":
            menu.addAction(Action(FIF.ACCEPT, "Looks good", triggered=lambda: self.mark_reviewed(t.id)))
        elif card.column == "done" and t.status in ("done", "failed"):
            menu.addAction(Action(FIF.RETURN, "Back to Review", triggered=lambda: self.mark_reviewed(t.id, False)))
        if om is not None and t.status in ("failed", "stopped", "done"):
            menu.addAction(Action(FIF.SYNC, "Run again", triggered=lambda: self.retry(t.id)))
        if menu.actions():
            menu.exec(pos)
