"""Center pane: header with model pill, message stream, prompt box."""
from __future__ import annotations

import asyncio

from PySide6.QtCore import QEvent, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QKeyEvent, QKeySequence
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import (
    Action,
    BodyLabel,
    CaptionLabel,
    CheckableMenu,
    CheckBox,
    FluentIcon as FIF,
    IconWidget,
    InfoBar,
    InfoBarPosition,
    MenuIndicatorType,
    PlainTextEdit,
    PrimaryPushButton,
    PrimaryToolButton,
    RoundMenu,
    SmoothScrollArea,
    SubtitleLabel,
    TitleLabel,
    ToolButton,
    ToolTipFilter,
    TransparentDropDownPushButton,
    TransparentToolButton,
)

from orchestrator.core import model_registry
from orchestrator.core.catalog import ENGINES
from orchestrator.core.indexer import load_history
from orchestrator.core.events import ContextReset, Done, Error, NeedsApproval, Text, ToolCall, ToolResult, Usage
from orchestrator.core.session_manager import SessionManager
from orchestrator.core.prompt_display import split_prompt
from orchestrator.storage.db import Database, Session
from orchestrator.ui.chat_widgets import (
    ApprovalCard,
    ContextChip,
    ResetDivider,
    CodeBlock,
    AssistantMessage,
    NoticeCard,
    ToolCard,
    TypingDots,
    UsageLine,
    UserMessage,
    WarningLine,
    fade_in,
)
from orchestrator.ui import theme as T
from orchestrator.ui.appearance import OWN_FONTS, refont_in_place
from orchestrator.ui.theme import chat_font, palette, space
from orchestrator.ui_text import usage_text

CONTENT_MAX = 860


class PromptBox(QFrame):
    submitted = Signal(str)
    stopRequested = Signal()
    runLaterRequested = Signal()

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
        self.edit.setFont(chat_font())
        self.edit.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.edit.installEventFilter(self)
        self.edit.textChanged.connect(self._grow)
        self.edit.document().setDocumentMargin(2)
        self.edit.setAcceptDrops(False)  # drops land on the box (files become attachments)
        self.setAcceptDrops(True)
        self.attachments: list[str] = []
        self.chips = QWidget(self)
        self.chipsLay = QHBoxLayout(self.chips)
        self.chipsLay.setContentsMargins(0, 0, 0, 0)
        self.chipsLay.setSpacing(6)
        self.chips.hide()
        lay.addWidget(self.chips)
        lay.addWidget(self.edit)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.row = row
        self.hint = CaptionLabel("Enter to send  ·  Shift+Enter new line", self)
        self.hint.setStyleSheet(f"color:{p['faint']};")
        self.hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.send = PrimaryToolButton(FIF.SEND, self)
        self.send.setFixedSize(32, 32)
        self.send.setToolTip("Send (Enter)")
        self.send.installEventFilter(ToolTipFilter(self.send))
        self.send.clicked.connect(self._on_button)
        self.later = TransparentToolButton(FIF.DATE_TIME, self)
        self.later.setFixedSize(32, 32)
        self.later.setToolTip("Run once, later…")
        self.later.installEventFilter(ToolTipFilter(self.later))
        self.later.clicked.connect(self.runLaterRequested.emit)
        self.attach = TransparentToolButton(FIF.ADD, self)
        self.attach.setFixedSize(32, 32)
        self.attach.setToolTip("Attach files or images (or paste an image, or drop files here)")
        self.attach.installEventFilter(ToolTipFilter(self.attach))
        self.attach.clicked.connect(self._pick_files)
        row.addWidget(self.attach)
        row.addWidget(self.hint, 1)
        row.addWidget(self.later)
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
                if ke.matches(QKeySequence.Paste) and self._paste_files():
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
        if text or self.attachments:
            self.edit.clear()
            self.submitted.emit(text)

    # ---- attachments
    def add_attachments(self, paths: list[str]) -> list[str]:
        """Add files; returns the problems (missing, too big) for the caller to show."""
        from orchestrator.core.attachments import check

        problems = []
        for p in paths:
            why = check(p)
            if why:
                problems.append(why)
            elif p not in self.attachments:
                self.attachments.append(p)
        self._render_chips()
        return problems

    def take_attachments(self) -> list[str]:
        out, self.attachments = self.attachments, []
        self._render_chips()
        return out

    def _render_chips(self) -> None:
        from orchestrator.core.attachments import label

        while self.chipsLay.count():
            w = self.chipsLay.takeAt(0).widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        p = palette()
        for path in self.attachments:
            chip = QFrame(self.chips)
            chip.setObjectName("attChip")
            chip.setStyleSheet(f"#attChip {{ background:{p['inline_code']}; border-radius:8px; }}")
            cl = QHBoxLayout(chip)
            cl.setContentsMargins(8, 2, 2, 2)
            cl.setSpacing(2)
            lb = QLabel(f"📎 {label(path)}", chip)
            lb.setToolTip(path)
            lb.setStyleSheet(f"color:{p['text']}; background:transparent;")
            cl.addWidget(lb)
            x = TransparentToolButton(FIF.CLOSE, chip)
            x.setFixedSize(20, 20)
            x.setToolTip("Remove")
            x.clicked.connect(lambda _=False, path=path: self._remove(path))
            cl.addWidget(x)
            self.chipsLay.addWidget(chip)
        self.chipsLay.addStretch(1)
        self.chips.setVisible(bool(self.attachments))

    def _remove(self, path: str) -> None:
        if path in self.attachments:
            self.attachments.remove(path)
        self._render_chips()

    def _pick_files(self) -> None:
        from PySide6.QtWidgets import QFileDialog

        paths, _f = QFileDialog.getOpenFileNames(self, "Attach files")
        if paths:
            self.add_attachments(paths)

    def _paste_files(self) -> bool:
        """Ctrl+V with an image or copied files on the clipboard attaches them. Plain text pastes as usual."""
        from PySide6.QtWidgets import QApplication

        md = QApplication.clipboard().mimeData()
        if md is None:
            return False
        files = [u.toLocalFile() for u in md.urls() if u.isLocalFile()] if md.hasUrls() else []
        if files:
            self.add_attachments(files)
            return True
        if md.hasImage():
            from orchestrator.core.attachments import pasted_image_path

            img = QApplication.clipboard().image()
            if not img.isNull():
                path = pasted_image_path()
                img.save(str(path), "PNG")
                self.add_attachments([str(path)])
                return True
        return False

    def dragEnterEvent(self, e) -> None:  # noqa: N802
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e) -> None:  # noqa: N802
        files = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if files:
            self.add_attachments(files)
            e.acceptProposedAction()

    def set_running(self, running: bool) -> None:
        self._running = running
        self.send.setIcon(FIF.PAUSE_BOLD if running else FIF.SEND)
        self.send.setToolTip("Stop" if running else "Send (Enter)")
        self.hint.setText("Working…  click ■ to stop" if running else "Enter to send  ·  Shift+Enter new line")

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
        self.setFixedHeight(30)
        self.setToolTip("Engine · model · reasoning effort for the next message")
        self.installEventFilter(ToolTipFilter(self))

    def set_value(self, engine: str, model: str, effort: str, locked: bool) -> None:
        self.engine, self.model, self.effort, self.engine_locked = engine, model, effort, locked
        label = model_registry.label(self.db, engine, model)
        ok = model_registry.is_allowed(self.db, engine, model)
        self.setText(f"{ENGINES.get(engine, engine)} · {label}{'' if ok else ' (disabled)'} · {effort or 'default'}")
        self.setMinimumWidth(self.sizeHint().width())  # never cut the label

    def _open(self) -> None:
        menu = self.build_menu()
        menu.exec(self.mapToGlobal(self.rect().bottomLeft()))

    def build_menu(self) -> CheckableMenu:
        menu = CheckableMenu(parent=self, indicatorType=MenuIndicatorType.RADIO)
        eg, mg, fg = QActionGroup(menu), QActionGroup(menu), QActionGroup(menu)
        for key, name in ENGINES.items():
            avail = model_registry.engine_available(self.db, key)
            a = QAction(name if avail else f"{name} (no enabled models)", menu, checkable=True)
            a.setChecked(key == self.engine)
            a.setEnabled(avail and (not self.engine_locked or key == self.engine))
            a.triggered.connect(lambda _=False, k=key: self._pick_engine(k))
            eg.addAction(a)
            menu.addAction(a)
        menu.addSeparator()
        models = model_registry.allowed(self.db, self.engine)
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
        return menu

    def _pick_engine(self, engine: str) -> None:
        if engine == self.engine:
            return
        d = model_registry.default(self.db, engine)
        if d:
            self.changed.emit(engine, *d)

    def _pick(self, model: str | None = None, effort: str | None = None) -> None:
        model = model or self.model
        efforts = next((m.efforts for m in model_registry.allowed(self.db, self.engine) if m.id == model), [])
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
    runOnceRequested = Signal(dict, object)  # prefill, anchor widget
    newChatRequested = Signal()
    addProjectRequested = Signal()
    openSettingsRequested = Signal()
    sideBySideRequested = Signal(int)  # open this related chat in the side pane
    closeRequested = Signal()  # side pane's close button

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
        self._restyle_pending = False
        self.app = None  # AppControl: settings changed from chat (cards with Undo)
        self._applied_cards: dict[str, list] = {}
        from orchestrator.ui.appearance import hub

        hub.changed.connect(self.restyle)
        self.setProperty(OWN_FONTS, True)  # global refont leaves us alone: restyle() sizes chat/code text
        self._sizes = (T.LOOK["chat_size"], T.LOOK["code_size"], T.LOOK["font"], T.LOOK["code_font"])

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # header
        self.header = QFrame(self)
        self.header.setObjectName("chatHeader")
        self.header.setStyleSheet(f"#chatHeader {{ border-bottom:1px solid {palette()['border']}; }}")
        hl = QHBoxLayout(self.header)
        hl.setContentsMargins(24, 8, 16, 8)
        tl = QVBoxLayout()
        tl.setSpacing(0)
        self.fixed_title = ""  # e.g. "Planner" in the Orchestrate workspace, whose header already names the run
        self.title = SubtitleLabel("", self.header)
        self.title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.path = CaptionLabel("", self.header)
        self.path.setStyleSheet(f"color:{palette()['muted']};")
        tl.addWidget(self.title)
        tl.addWidget(self.path)
        hl.addLayout(tl, 1)
        self.usage = CaptionLabel("", self.header)
        self.usage.setObjectName("usageMeter")
        self.usage.setStyleSheet(f"color:{palette()['muted']}; padding-right:8px;")
        hl.addWidget(self.usage)
        self.autoScroll = CheckBox("Auto-scroll", self.header)
        self.autoScroll.setObjectName("autoScroll")
        self.autoScroll.setChecked(True)
        self.autoScroll.setToolTip("Keep the newest messages in view. Scrolling up turns it off; "
                                   "scrolling back to the bottom turns it on again.")
        self.autoScroll.installEventFilter(ToolTipFilter(self.autoScroll))
        self.autoScroll.toggled.connect(self._auto_scroll_toggled)
        hl.addWidget(self.autoScroll)
        self.ctxBtn = ToolButton(FIF.PIE_SINGLE, self.header)
        self.ctxBtn.setToolTip("What goes into each prompt (token breakdown, Lean mode)")
        self.ctxBtn.installEventFilter(ToolTipFilter(self.ctxBtn))
        self.ctxBtn.clicked.connect(self.show_context)
        hl.addWidget(self.ctxBtn)
        self.diffBtn = ToolButton(FIF.DOCUMENT, self.header)
        self.diffBtn.setToolTip("Changes (git diff of this chat's folder)  ·  Ctrl+D")
        self.diffBtn.installEventFilter(ToolTipFilter(self.diffBtn))
        self.diffBtn.clicked.connect(self.show_diff)
        hl.addWidget(self.diffBtn)
        self.pairBtn = ToolButton(FIF.LAYOUT, self.header)
        self.pairBtn.setToolTip("Side by side: open the orchestrator or one of its agents next to this chat")
        self.pairBtn.installEventFilter(ToolTipFilter(self.pairBtn))
        self.pairBtn.clicked.connect(self._pair_menu)
        self.pairBtn.hide()
        hl.addWidget(self.pairBtn)
        self.closeBtn = ToolButton(FIF.CLOSE, self.header)
        self.closeBtn.setToolTip("Close this side pane")
        self.closeBtn.installEventFilter(ToolTipFilter(self.closeBtn))
        self.closeBtn.clicked.connect(self.closeRequested.emit)
        self.closeBtn.hide()
        hl.addWidget(self.closeBtn)
        root.addWidget(self.header)

        # stream
        self.scroll = SmoothScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet("QScrollArea { background:transparent; border:none; }")
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        holder = QWidget()
        holder.setStyleSheet("background:transparent;")
        hh = QHBoxLayout(holder)
        hh.setContentsMargins(24, 16, 24, 16)
        self.column = QWidget(holder)
        self.column.setMaximumWidth(CONTENT_MAX)
        self.stream = QVBoxLayout(self.column)
        self.stream.setContentsMargins(0, 0, 0, 0)
        self.stream.setSpacing(space(10))
        self.stream.addStretch(1)
        hh.addStretch(1)
        hh.addWidget(self.column, 100)
        hh.addStretch(1)
        self.scroll.setWidget(holder)
        self.scroll.verticalScrollBar().rangeChanged.connect(self._autoscroll)
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
        self.pill = ModelPill(db, self.prompt)
        self.pill.changed.connect(self._on_model_changed)
        self.prompt.row.insertWidget(0, self.pill)
        self.model_warn = CaptionLabel("", pw)
        self.model_warn.setObjectName("modelWarning")
        self.model_warn.setWordWrap(True)
        self.model_warn.setStyleSheet("color:#f59e0b; padding-left:6px;")
        self.model_warn.hide()
        pl.removeWidget(self.prompt)
        pcol = QVBoxLayout()
        pcol.setSpacing(4)
        pcol.addWidget(self.model_warn)
        pcol.addWidget(self.prompt)
        pl.insertLayout(1, pcol, 100)
        self.prompt.stopRequested.connect(self._stop)
        self.prompt.runLaterRequested.connect(self._run_later)
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

    # ---- app settings changed from chat
    def set_app_control(self, ctl) -> None:
        self.app = ctl
        ctl.applied.connect(self._on_applied)
        ctl.undone.connect(self._on_undone)

    def _applied_card(self, summary: str, change_id: str | None, undone: bool = False):
        from orchestrator.ui.chat_widgets import AppliedCard

        ch = self.app.changes.get(change_id) if (self.app and change_id) else None
        card = AppliedCard(summary, change_id, self._undo if ch else None, undone or bool(ch and ch.undone))
        self._applied_cards.setdefault(change_id or "", []).append(card)
        return card

    def _undo(self, change_id: str) -> None:
        try:
            msg = self.app.undo(change_id)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            InfoBar.warning("Can't undo", str(exc), parent=self.window(), position=InfoBarPosition.TOP_RIGHT,
                            duration=3000)
            return
        InfoBar.success("Undone", msg.removeprefix("Undone: "), parent=self.window(),
                        position=InfoBarPosition.TOP_RIGHT, duration=2500)

    def _on_applied(self, ch) -> None:
        if self.session and ch.sid == self.session.id:
            self._add(self._applied_card(ch.summary, ch.id))

    def _on_undone(self, ch) -> None:
        for card in self._applied_cards.get(ch.id, []):
            try:
                card.set_undone(True)
            except RuntimeError:  # card was deleted with an earlier render
                pass

    def show_confirm(self, text: str, fut) -> None:
        from orchestrator.ui.chat_widgets import ConfirmCard

        self._stick = True
        self._add(ConfirmCard(text, fut))

    def _try_app_command(self, text: str) -> bool:
        """Very simple commands ("increase the font size") apply right here, without a model call."""
        if not self.app or not self.session:
            return False
        from orchestrator.core.app_control import AppCommandError, detect

        hit = detect(text)
        if not hit:
            return False
        op, args = hit
        sid = self.session.id
        self.empty.hide()
        self.scroll.show()
        self._stick = True
        self.db.add_message(sid, "user", "text", {"text": text})
        self._add(UserMessage(text))
        try:
            res = getattr(self.app, "op_" + op)(sid, **args)
        except AppCommandError as exc:
            self._add(self._notice(str(exc), "warning"))
            return True
        if isinstance(res, dict) and res.get("note") and not res.get("applied"):
            self._add(self._notice(res["note"], "warning"))
        return True

    def restyle(self) -> None:
        """Settings › Appearance changed: rebuild the shown chat with the new fonts/density."""
        self.prompt.edit.setFont(chat_font())
        self.stream.setSpacing(space(10))
        old, self._sizes = self._sizes, (T.LOOK["chat_size"], T.LOOK["code_size"], T.LOOK["font"], T.LOOK["code_font"])
        if self.session and self.mgr.is_running(self.session.id):
            # Don't rebuild a streaming turn: resize what is shown now, rebuild fully when it finishes.
            refont_in_place(self.column, old, self._sizes)
            for cb in self.column.findChildren(CodeBlock):
                cb.set_code(cb.code, cb.lang.text())
            self._refit()
            self._restyle_pending = True
            return
        self._restyle_pending = False
        if self.session:
            self.show_session(self.session.id)

    def _chat_width(self) -> int:
        return min(CONTENT_MAX, max(200, self.scroll.viewport().width() - 48))

    def resizeEvent(self, e) -> None:  # noqa: N802
        super().resizeEvent(e)
        QTimer.singleShot(0, self._refit)

    def _refit(self) -> None:
        w = self._chat_width()
        for m in self.column.findChildren(UserMessage):
            m.fit(w)

    def set_side(self, on: bool) -> None:
        """This view is the side pane: show a close button."""
        self.closeBtn.setVisible(on)

    def related_chats(self) -> list[tuple[str, Session]]:
        """(label, chat) pairs worth opening next to this one: its orchestrator, its sibling or child agents."""
        s = self.session
        if not s:
            return []
        out: list[tuple[str, Session]] = []
        if s.parent_id:
            parent = self.db.session(s.parent_id)
            if parent:
                out.append(("Orchestrator", parent))
                out += [("Agent", c) for c in self.db.child_sessions(parent.id) if c.id != s.id]
        out += [("Agent", c) for c in self.db.child_sessions(s.id)]
        return out

    def _pair_menu(self) -> None:
        menu = RoundMenu(parent=self)
        for label, c in self.related_chats():
            icon = FIF.ROBOT if label == "Orchestrator" else FIF.CHAT
            menu.addAction(Action(icon, f"{label}: {c.title}", triggered=lambda _=False, sid=c.id:
                                  self.sideBySideRequested.emit(sid)))
        menu.exec(self.pairBtn.mapToGlobal(self.pairBtn.rect().bottomLeft()))

    def show_session(self, sid: int | None) -> None:
        self._applied_cards = {}
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
        self.title.setText(self.fixed_title or s.title)
        self.pairBtn.setVisible(bool(self.related_chats()))
        where = s.cwd or (proj.path if proj else "")
        self.path.setText(where + ("   ·   imported from engine history" if s.source == "imported" else ""))
        self.pill.set_value(s.engine, s.model or "", s.effort or "", locked=bool(s.engine_session_id))
        self._update_model_warning()
        self.update_usage()
        msgs = self._history(s)
        if not msgs and not self.mgr.is_running(s.id):
            self.scroll.hide()
            self.empty.show()
            self.empty.show_state(FIF.ROBOT, f"New chat in {proj.name if proj else 'project'}",
                                  "Messages run in this project folder. Pick the engine, model and effort next to Send.")
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
                card = ToolCard(call.name, self._rel(call.summary))
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
        if isinstance(w, UserMessage):
            w.fit(self._chat_width())
        self.stream.insertWidget(self.stream.count() - 1, w)
        if animate:
            fade_in(w)

    def _rel(self, summary: str) -> str:
        """Tool summaries with the chat's own folder left out (paths read relative to the project)."""
        folder = self.chat_folder() if self.session else ""
        if not folder or not summary:
            return summary
        out = summary
        for f in {folder, folder.replace("\\", "/"), folder.replace("/", "\\")}:
            for sep in ("/", "\\"):
                out = out.replace(f.rstrip("/\\") + sep, "")
        return out

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
                shown, hidden = split_prompt(m["text"])
                self._add(UserMessage(shown, attachments=m.get("attachments"), hidden=hidden), False)
            elif k == "text":
                self._add(AssistantMessage(self.session.engine, self._engine_label(), m["text"]), False)
            elif k == "tool":
                card = ToolCard(m.get("name", ""), self._rel(m.get("summary", "")))
                card.set_result(m.get("output", ""), m.get("is_error", False))
                self._add(card, False)
            elif k == "error":
                kind = m.get("level") or ("warning" if not m.get("fatal", True) else "error")
                self._add(self._notice(m["text"], kind), False)
            elif k == "context":
                self._add(ContextChip(m.get("tags") or []), False)
            elif k == "reset":
                self._add(ResetDivider(m.get("how", "compact"), m.get("before", 0), m.get("after", 0),
                                       m.get("summary", "")), False)
            elif k == "app_change":
                if m.get("undone"):
                    continue  # the original card shows "Undone"
                undone = any(x.get("kind") == "app_change" and x.get("undone")
                             and x.get("change_id") == m.get("change_id") for x in msgs)
                self._add(self._applied_card(m.get("summary", ""), m.get("change_id"), undone), False)
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
    def update_usage(self) -> None:
        s = self.session
        u = self.db.usage(s.id) if s else {}
        self.usage.setText(usage_text(u, short=True))
        self.usage.setToolTip("Tokens used by this chat in Orchestrator (input includes cached).\n" + usage_text(u)
                              if u.get("turns") else "")

    def chat_folder(self) -> str:
        s = self.session
        if not s:
            return ""
        proj = self.db.project(s.project_id)
        return s.cwd or (proj.path if proj else "")

    def show_context(self) -> None:
        if not self.session:
            return
        from orchestrator.ui.context_view import ContextDialog

        ContextDialog(self.db, self.mgr, self.session.id, self.window()).exec()

    def show_diff(self) -> None:
        if not self.session:
            return
        from orchestrator.ui.diff_view import DiffDialog

        DiffDialog(self.chat_folder(), self.window()).exec()

    def _update_model_warning(self) -> None:
        s = self.session
        bad = bool(s) and not model_registry.is_allowed(self.db, s.engine, s.model or "")
        if bad:
            self.model_warn.setText("⚠  " + model_registry.disabled_message(self.db, s.engine, s.model or ""))
        self.model_warn.setVisible(bad)

    def refresh_models(self) -> None:
        """Settings › Models changed: relabel the pill and re-check the current chat."""
        s = self.session = self.db.session(self.session.id) if self.session else None
        if s:
            self.pill.set_value(s.engine, s.model or "", s.effort or "", locked=bool(s.engine_session_id))
        self._update_model_warning()

    def _send(self, text: str) -> None:
        if not self.session:
            return
        s = self.db.session(self.session.id)
        if s and not model_registry.is_allowed(self.db, s.engine, s.model or ""):
            self._update_model_warning()
            self.prompt.edit.setPlainText(text)
            InfoBar.warning("Model disabled", model_registry.disabled_message(self.db, s.engine, s.model or ""),
                            parent=self.window(), position=InfoBarPosition.TOP_RIGHT, duration=4000)
            return
        files = self.prompt.take_attachments()
        if not files and self._try_app_command(text):
            return
        text = text or "See the attached files."
        self.empty.hide()
        self.scroll.show()
        self._stick = True
        self._add(UserMessage(text, attachments=files))
        self._current = None
        if files:
            self.mgr.send(self.session.id, text, attachments=files)
        else:
            self.mgr.send(self.session.id, text)
        self._show_dots()

    def _run_later(self) -> None:
        s = self.session
        if not s:
            return
        self.runOnceRequested.emit({
            "prompt": self.prompt.edit.toPlainText().strip(), "engine": s.engine, "model": s.model, "effort": s.effort,
            "project_id": s.project_id, "session_id": s.id if s.engine_session_id else None,
        }, self.prompt.later)

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
                self.title.setText(self.fixed_title or self.session.title)

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
        if self._restyle_pending and self.session and sid == self.session.id and status != "running":
            QTimer.singleShot(0, self.restyle)
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
            card = ToolCard(ev.name, self._rel(ev.summary))
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
        elif isinstance(ev, ContextReset):
            self._flush_render()
            self._current = None
            self._add(ResetDivider(ev.how, ev.before, ev.after, ev.summary))
            self.update_usage()
        elif isinstance(ev, Usage):
            self._usage = ev
        elif isinstance(ev, Done):
            self._flush_render()
            self._hide_dots()
            u = self._usage
            self.update_usage()
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
    # The Auto-scroll box in the header is the single source of truth: checked keeps the newest
    # message in view. Scrolling up unchecks it, scrolling back to the bottom checks it again,
    # and sending a message (or opening a chat) checks it and jumps to the bottom.
    STICK_PX = 40  # this close to the bottom counts as "at the newest message"

    @property
    def _stick(self) -> bool:
        return self.autoScroll.isChecked()

    @_stick.setter
    def _stick(self, on: bool) -> None:
        if self.autoScroll.isChecked() != on:
            self.autoScroll.setChecked(on)  # toggled -> _auto_scroll_toggled jumps down when on
        elif on:
            self._scroll_to_bottom()

    def _auto_scroll_toggled(self, on: bool) -> None:
        if on:
            self._scroll_to_bottom()

    def _scroll_to_bottom(self) -> None:
        bar = self.scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _track_stick(self, v: int) -> None:
        bar = self.scroll.verticalScrollBar()
        at_bottom = bar.maximum() - v < self.STICK_PX
        if at_bottom != self.autoScroll.isChecked():
            self.autoScroll.blockSignals(True)  # following the user's scroll, so no jump
            self.autoScroll.setChecked(at_bottom)
            self.autoScroll.blockSignals(False)

    def _autoscroll(self, _min: int, mx: int) -> None:
        if self._stick:
            self.scroll.verticalScrollBar().setValue(mx)
