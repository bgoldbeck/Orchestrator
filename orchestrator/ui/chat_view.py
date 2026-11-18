"""Center pane: header with model pill, message stream, prompt box."""
from __future__ import annotations

import asyncio

from PySide6.QtCore import QEvent, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QFont, QKeyEvent
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CheckableMenu,
    FluentIcon as FIF,
    IconWidget,
    MenuIndicatorType,
    PlainTextEdit,
    PrimaryPushButton,
    PrimaryToolButton,
    SmoothScrollArea,
    SubtitleLabel,
    TitleLabel,
    ToolButton,
    ToolTipFilter,
    TransparentDropDownPushButton,
)

from orchestrator.core.catalog import ENGINES
from orchestrator.core.indexer import load_history
from orchestrator.core.events import Done, Error, NeedsApproval, Text, ToolCall, ToolResult, Usage
from orchestrator.core.session_manager import SessionManager
from orchestrator.storage.db import Database, Session
from orchestrator.ui.chat_widgets import (
    ApprovalCard,
    AssistantMessage,
    NoticeCard,
    ToolCard,
    TypingDots,
    UsageLine,
    UserMessage,
    WarningLine,
    fade_in,
)
from orchestrator.ui.theme import UI_FONT, palette

CONTENT_MAX = 860


class PromptBox(QFrame):
    submitted = Signal(str)
    stopRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        p = palette()
        self.setObjectName("promptBox")
        self._style(False)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 10, 8)
        lay.setSpacing(4)
        self.edit = QPlainTextEdit(self)
        self.edit.setPlaceholderText("Ask anything, or describe a change…")
        self.edit.setFrameShape(QFrame.NoFrame)
        self.edit.setStyleSheet(f"QPlainTextEdit {{ background:transparent; border:none; padding:0; color:{p['text']};"
                                f" selection-background-color:{p['accent']}; }}")
        f = QFont(UI_FONT)
        f.setPointSizeF(10.5)
        self.edit.setFont(f)
        self.edit.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.edit.installEventFilter(self)
        self.edit.textChanged.connect(self._grow)
        self.edit.document().setDocumentMargin(2)
        lay.addWidget(self.edit)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.hint = CaptionLabel("Enter to send  ·  Shift+Enter for new line", self)
        self.hint.setStyleSheet(f"color:{p['faint']};")
        self.send = PrimaryToolButton(FIF.SEND, self)
        self.send.setFixedSize(34, 34)
        self.send.setToolTip("Send (Enter)")
        self.send.installEventFilter(ToolTipFilter(self.send))
        self.send.clicked.connect(self._on_button)
        row.addWidget(self.hint)
        row.addStretch(1)
        row.addWidget(self.send)
        lay.addLayout(row)
        self._running = False
        self._grow()

    def _style(self, focused: bool) -> None:
        p = palette()
        border = p["accent"] if focused else p["border"]
        self.setStyleSheet(f"#promptBox {{ background:{p['card']}; border:1px solid {border}; border-radius:14px; }}")

    def eventFilter(self, obj, e) -> bool:  # noqa: N802
        if obj is self.edit:
            if e.type() == QEvent.KeyPress:
                ke: QKeyEvent = e  # type: ignore[assignment]
                if ke.key() in (Qt.Key_Return, Qt.Key_Enter) and not (ke.modifiers() & Qt.ShiftModifier):
                    self._submit()
                    return True
            elif e.type() == QEvent.FocusIn:
                self._style(True)
            elif e.type() == QEvent.FocusOut:
                self._style(False)
        return super().eventFilter(obj, e)

    def _grow(self) -> None:
        doc_h = self.edit.document().size().height() * self.edit.fontMetrics().lineSpacing()
        self.edit.setFixedHeight(int(max(26, min(doc_h + 8, 200))))

    def _on_button(self) -> None:
        if self._running:
            self.stopRequested.emit()
        else:
            self._submit()

    def _submit(self) -> None:
        if self._running:
            return
        text = self.edit.toPlainText().strip()
        if text:
            self.edit.clear()
            self.submitted.emit(text)

    def set_running(self, running: bool) -> None:
        self._running = running
        self.send.setIcon(FIF.PAUSE_BOLD if running else FIF.SEND)
        self.send.setToolTip("Stop" if running else "Send (Enter)")
        self.hint.setText("Working…  click ■ to stop" if running else "Enter to send  ·  Shift+Enter for new line")

    def focus(self) -> None:
        self.edit.setFocus()


class ModelPill(TransparentDropDownPushButton):
    changed = Signal(str, str, str)  # engine, model, effort

    def __init__(self, db: Database, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.db = db
        self.engine, self.model, self.effort = "codex", "", ""
        self.engine_locked = False
        self.clicked.connect(self._open)
        self.setFixedHeight(32)

    def set_value(self, engine: str, model: str, effort: str, locked: bool) -> None:
        self.engine, self.model, self.effort, self.engine_locked = engine, model, effort, locked
        label = next((m.label for m in self.db.models(engine) if m.id == model), model or "default")
        self.setText(f"{ENGINES.get(engine, engine)}  ·  {label}  ·  {effort or 'default'}")

    def _open(self) -> None:
        menu = CheckableMenu(parent=self, indicatorType=MenuIndicatorType.RADIO)
        eg, mg, fg = QActionGroup(menu), QActionGroup(menu), QActionGroup(menu)
        for key, name in ENGINES.items():
            a = QAction(name, menu, checkable=True)
            a.setChecked(key == self.engine)
            a.setEnabled(not self.engine_locked or key == self.engine)
            a.triggered.connect(lambda _=False, k=key: self._pick_engine(k))
            eg.addAction(a)
            menu.addAction(a)
        menu.addSeparator()
        models = self.db.models(self.engine)
        for m in models:
            a = QAction(m.label, menu, checkable=True)
            a.setChecked(m.id == self.model)
            a.triggered.connect(lambda _=False, mid=m.id: self._pick(model=mid))
            mg.addAction(a)
            menu.addAction(a)
        menu.addSeparator()
        cur = next((m for m in models if m.id == self.model), None)
        efforts = cur.efforts if cur else ["low", "medium", "high"]
        sub = CheckableMenu("Reasoning effort", menu, indicatorType=MenuIndicatorType.RADIO)
        sub.setIcon(FIF.SPEED_HIGH)
        for ef in efforts:
            a = QAction(ef, sub, checkable=True)
            a.setChecked(ef == self.effort)
            a.triggered.connect(lambda _=False, e=ef: self._pick(effort=e))
            fg.addAction(a)
            sub.addAction(a)
        menu.addMenu(sub)
        menu.exec(self.mapToGlobal(self.rect().bottomLeft()))

    def _pick_engine(self, engine: str) -> None:
        if engine == self.engine:
            return
        model, effort = self.db.default_model(engine)
        self.changed.emit(engine, model, effort)

    def _pick(self, model: str | None = None, effort: str | None = None) -> None:
        model = model or self.model
        efforts = next((m.efforts for m in self.db.models(self.engine) if m.id == model), [])
        effort = effort or (self.effort if self.effort in efforts else (efforts[min(1, len(efforts) - 1)] if efforts else ""))
        self.changed.emit(self.engine, model, effort)


class EmptyState(QWidget):
    action = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignCenter)
        lay.setSpacing(10)
        self.icon = IconWidget(FIF.CHAT, self)
        self.icon.setFixedSize(44, 44)
        self.title = TitleLabel("", self)
        self.title.setAlignment(Qt.AlignCenter)
        self.sub = BodyLabel("", self)
        self.sub.setAlignment(Qt.AlignCenter)
        self.sub.setWordWrap(True)
        self.sub.setStyleSheet(f"color:{palette()['muted']};")
        self.btn = PrimaryPushButton("", self)
        self.btn.clicked.connect(self.action.emit)
        lay.addWidget(self.icon, 0, Qt.AlignCenter)
        lay.addWidget(self.title)
        lay.addWidget(self.sub)
        lay.addSpacing(6)
        lay.addWidget(self.btn, 0, Qt.AlignCenter)

    def show_state(self, icon, title: str, sub: str, button: str = "") -> None:
        self.icon.setIcon(icon)
        self.title.setText(title)
        self.sub.setText(sub)
        self.btn.setVisible(bool(button))
        self.btn.setText(button)


class ChatView(QWidget):
    newChatRequested = Signal()
    addProjectRequested = Signal()
    openSettingsRequested = Signal()

    def __init__(self, db: Database, manager: SessionManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.db = db
        self.mgr = manager
        self.session: Session | None = None
        self._current: AssistantMessage | None = None
        self._tools: dict[str, ToolCard] = {}
        self._dots: QWidget | None = None
        self._live = ""
        self._usage: Usage | None = None
        self._approvals: dict[int, list] = {}
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.timeout.connect(self._flush_render)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # header
        self.header = QFrame(self)
        self.header.setObjectName("chatHeader")
        self.header.setStyleSheet(f"#chatHeader {{ border-bottom:1px solid {palette()['border']}; }}")
        hl = QHBoxLayout(self.header)
        hl.setContentsMargins(24, 10, 16, 10)
        tl = QVBoxLayout()
        tl.setSpacing(0)
        self.title = SubtitleLabel("", self.header)
        self.title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.path = CaptionLabel("", self.header)
        self.path.setStyleSheet(f"color:{palette()['muted']};")
        tl.addWidget(self.title)
        tl.addWidget(self.path)
        hl.addLayout(tl, 1)
        self.pill = ModelPill(db, self.header)
        self.pill.changed.connect(self._on_model_changed)
        hl.addWidget(self.pill)
        root.addWidget(self.header)

        # stream
        self.scroll = SmoothScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet("QScrollArea { background:transparent; border:none; }")
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        holder = QWidget()
        holder.setStyleSheet("background:transparent;")
        hh = QHBoxLayout(holder)
        hh.setContentsMargins(24, 20, 24, 20)
        self.column = QWidget(holder)
        self.column.setMaximumWidth(CONTENT_MAX)
        self.stream = QVBoxLayout(self.column)
        self.stream.setContentsMargins(0, 0, 0, 0)
        self.stream.setSpacing(12)
        self.stream.addStretch(1)
        hh.addStretch(1)
        hh.addWidget(self.column, 100)
        hh.addStretch(1)
        self.scroll.setWidget(holder)
        self.scroll.verticalScrollBar().rangeChanged.connect(self._autoscroll)
        self._stick = True
        self.scroll.verticalScrollBar().valueChanged.connect(self._track_stick)
        root.addWidget(self.scroll, 1)

        self.empty = EmptyState(self)
        self.empty.action.connect(self._empty_action)
        root.addWidget(self.empty, 1)

        # prompt
        pw = QWidget(self)
        pl = QHBoxLayout(pw)
        pl.setContentsMargins(24, 4, 24, 18)
        self.prompt = PromptBox(pw)
        self.prompt.setMaximumWidth(CONTENT_MAX)
        pl.addStretch(1)
        pl.addWidget(self.prompt, 100)
        pl.addStretch(1)
        self.prompt.submitted.connect(self._send)
        self.prompt.stopRequested.connect(self._stop)
        root.addWidget(pw)
        self.prompt_wrap = pw

        manager.event.connect(self._on_event)
        manager.status_changed.connect(self._on_status)
        manager.approval_requested.connect(self._on_approval)
        manager.session_updated.connect(self._on_session_updated)
        self.show_session(None)

    # ---- state
    def _empty_action(self) -> None:
        if not self.db.projects():
            self.addProjectRequested.emit()
        else:
            self.newChatRequested.emit()

    def show_session(self, sid: int | None) -> None:
        self.session = self.db.session(sid) if sid else None
        self._clear()
        s = self.session
        has_projects = bool(self.db.projects())
        self.header.setVisible(bool(s))
        self.prompt_wrap.setVisible(bool(s))
        if not s:
            self.scroll.hide()
            self.empty.show()
            if has_projects:
                self.empty.show_state(FIF.CHAT, "Pick up where you left off",
                                      "Choose a chat on the left, or start a new one with Claude or Codex.", "New chat")
            else:
                self.empty.show_state(FIF.FOLDER_ADD, "Welcome to Orchestrator",
                                      "A project is a folder on disk. Add one to start chatting with Claude or Codex inside it.",
                                      "Add project folder")
            return
        proj = self.db.project(s.project_id)
        self.title.setText(s.title)
        where = s.cwd or (proj.path if proj else "")
        self.path.setText(where + ("   ·   imported from engine history" if s.source == "imported" else ""))
        self.pill.set_value(s.engine, s.model or "", s.effort or "", locked=bool(s.engine_session_id))
        msgs = self._history(s)
        if not msgs and not self.mgr.is_running(s.id):
            self.scroll.hide()
            self.empty.show()
            self.empty.show_state(FIF.ROBOT, f"New chat in {proj.name if proj else 'project'}",
                                  "Messages run in this project folder. Pick the engine, model and effort in the top right.")
        else:
            self.empty.hide()
            self.scroll.show()
            self._render_history(msgs)
        running = self.mgr.is_running(s.id)
        self.prompt.set_running(running)
        pending = [(r, f) for r, f in self._approvals.get(s.id, []) if not f.done()]
        self._approvals[s.id] = pending
        if running:
            # Switched back to a chat that is still working: restore its live state.
            self.empty.hide()
            self.scroll.show()
            for call in self.mgr.live_tools(s.id):
                card = ToolCard(call.name, call.summary)
                self._tools[call.id] = card
                self._add(card, False)
            live = self.mgr.live_text(s.id)
            if live:
                self._current = AssistantMessage(s.engine, self._engine_label(), live)
                self._live = live
                self._add(self._current, False)
        if running and not pending:
            self._show_dots()
        for r, f in pending:
            self.empty.hide()
            self.scroll.show()
            self._show_approval(r, f)
        self._stick = True
        QTimer.singleShot(0, self.prompt.focus)

    def _clear(self) -> None:
        self._current = None
        self._tools.clear()
        self._dots = None
        while self.stream.count() > 1:
            item = self.stream.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _add(self, w: QWidget, animate: bool = True) -> None:
        self.stream.insertWidget(self.stream.count() - 1, w)
        if animate:
            fade_in(w)

    def _engine_label(self) -> str:
        return ENGINES.get(self.session.engine, "") if self.session else ""

    HISTORY_LIMIT = 150

    def _history(self, s: Session) -> list[dict]:
        if s.source == "imported" and s.file_path:
            # Engine log is the source of truth for imported chats (read-only).
            return load_history(s.engine, s.file_path)
        return self.db.messages(s.id)

    def _render_history(self, msgs: list[dict]) -> None:
        if len(msgs) > self.HISTORY_LIMIT:
            note = CaptionLabel(f"Showing the last {self.HISTORY_LIMIT} of {len(msgs)} items", self.column)
            note.setAlignment(Qt.AlignCenter)
            note.setStyleSheet(f"color:{palette()['faint']};")
            self._add(note, False)
            msgs = msgs[-self.HISTORY_LIMIT:]
        for m in msgs:
            k = m["kind"]
            if m["role"] == "user":
                self._add(UserMessage(m["text"]), False)
            elif k == "text":
                self._add(AssistantMessage(self.session.engine, self._engine_label(), m["text"]), False)
            elif k == "tool":
                card = ToolCard(m.get("name", ""), m.get("summary", ""))
                card.set_result(m.get("output", ""), m.get("is_error", False))
                self._add(card, False)
            elif k == "error":
                kind = m.get("level") or ("warning" if not m.get("fatal", True) else "error")
                self._add(self._notice(m["text"], kind), False)
            elif k == "usage":
                self._add(UsageLine(m.get("input", 0), m.get("output", 0), m.get("cached", 0), m.get("cost")), False)

    def _notice(self, text: str, kind: str) -> NoticeCard:
        if kind == "auth":
            card = NoticeCard(text, "error", "Open Settings")
            card.action.connect(self.openSettingsRequested.emit)
            return card
        if kind == "warning":
            return WarningLine(text)
        return NoticeCard(text, "error")

    # ---- actions
    def _send(self, text: str) -> None:
        if not self.session:
            return
        self.empty.hide()
        self.scroll.show()
        self._stick = True
        self._add(UserMessage(text))
        self._current = None
        self.mgr.send(self.session.id, text)
        self._show_dots()

    def _stop(self) -> None:
        if self.session:
            asyncio.ensure_future(self.mgr.stop(self.session.id))

    def _on_model_changed(self, engine: str, model: str, effort: str) -> None:
        if not self.session:
            return
        self.db.update_session(self.session.id, engine=engine, model=model, effort=effort)
        self.db.set_setting("last", [engine, model, effort])
        self.session = self.db.session(self.session.id)
        self.pill.set_value(engine, model, effort, locked=bool(self.session.engine_session_id))
        self.mgr.session_updated.emit(self.session.id)

    def _on_session_updated(self, sid: int) -> None:
        if self.session and sid == self.session.id:
            self.session = self.db.session(sid)
            if self.session:
                self.title.setText(self.session.title)

    # ---- live events
    def _show_dots(self) -> None:
        if self._dots is None:
            w = QWidget()
            lay = QHBoxLayout(w)
            lay.setContentsMargins(30, 0, 0, 0)
            lay.addWidget(TypingDots(w))
            lay.addStretch(1)
            self._dots = w
            self._add(w)

    def _hide_dots(self) -> None:
        if self._dots is not None:
            self._dots.deleteLater()
            self.stream.removeWidget(self._dots)
            self._dots = None

    def _on_status(self, sid: int, status: str) -> None:
        if self.session and sid == self.session.id:
            self.prompt.set_running(status in ("running", "waiting"))

    def _on_event(self, sid: int, ev) -> None:
        if not self.session or sid != self.session.id:
            return
        if isinstance(ev, Text):
            if self._current is None:
                self._hide_dots()
                self._current = AssistantMessage(self.session.engine, self._engine_label())
                self._add(self._current)
                self._live = ""
            self._live = (self._live + ev.text) if ev.delta else ev.text
            if not self._render_timer.isActive():
                self._render_timer.start(40)
            if not ev.delta:
                self._flush_render()
        elif isinstance(ev, ToolCall):
            self._flush_render()
            self._current = None
            if ev.id in self._tools:
                return
            self._hide_dots()
            card = ToolCard(ev.name, ev.summary)
            self._tools[ev.id] = card
            self._add(card)
            self._show_dots()
        elif isinstance(ev, ToolResult):
            card = self._tools.get(ev.id)
            if card:
                card.set_result(ev.output, ev.is_error)
        elif isinstance(ev, Error):
            self._flush_render()
            self._hide_dots()
            self._current = None
            self._add(self._notice(ev.message, ev.kind))
            if ev.fatal is False and self.mgr.is_running(sid):
                self._show_dots()
        elif isinstance(ev, Usage):
            self._usage = ev
        elif isinstance(ev, Done):
            self._flush_render()
            self._hide_dots()
            u = self._usage
            if u:
                self._add(UsageLine(u.input_tokens, u.output_tokens, u.cached_tokens, u.cost_usd))
                self._usage = None
            self._current = None
            self._tools.clear()

    def _flush_render(self) -> None:
        self._render_timer.stop()
        if self._current is not None and self._current.text != self._live:
            self._current.set_text(self._live)

    def _on_approval(self, sid: int, req: NeedsApproval, fut) -> None:
        self._approvals.setdefault(sid, []).append((req, fut))
        if self.session and sid == self.session.id:
            self._show_approval(req, fut)

    def _show_approval(self, req: NeedsApproval, fut) -> None:
        self._flush_render()
        self._hide_dots()
        self._current = None
        card = ApprovalCard(self._engine_label(), req.tool, req.detail)

        def answer(ok: bool) -> None:
            if not fut.done():
                fut.set_result(ok)
            self._show_dots()

        card.answered.connect(answer)
        self._add(card)

    # ---- scrolling
    def _track_stick(self, v: int) -> None:
        bar = self.scroll.verticalScrollBar()
        self._stick = bar.maximum() - v < 60

    def _autoscroll(self, _min: int, mx: int) -> None:
        if self._stick:
            self.scroll.verticalScrollBar().setValue(mx)
