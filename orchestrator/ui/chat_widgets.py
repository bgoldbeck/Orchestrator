"""Message bubbles, code blocks, tool cards and other chat building blocks."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QGuiApplication, QPainter, QColor
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    FluentIcon as FIF,
    IconWidget,
    IndeterminateProgressRing,
    InfoBarIcon,
    PrimaryPushButton,
    PushButton,
    TransparentToolButton,
    ToolTipFilter,
)

from orchestrator.ui.markdown_render import code_html, split_segments, text_html
from orchestrator.ui.theme import chat_font, code_pt, engine_badge, mono_css, palette, space


def fade_in(w: QWidget, ms: int = 220) -> None:
    eff = QGraphicsOpacityEffect(w)
    eff.setOpacity(0.0)
    w.setGraphicsEffect(eff)
    anim = QPropertyAnimation(eff, b"opacity", w)
    anim.setDuration(ms)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setEasingCurve(QEasingCurve.OutCubic)
    anim.finished.connect(lambda: w.setGraphicsEffect(None))
    anim.start()


class RichLabel(QLabel):
    def __init__(self, html: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setTextFormat(Qt.RichText)
        self.setWordWrap(True)
        self.setOpenExternalLinks(True)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        f = chat_font()
        self.setFont(f)
        self.setStyleSheet(f"color:{palette()['text']}; background:transparent;")
        self.setText(html)


class CodeBlock(QFrame):
    def __init__(self, code: str, lang: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.code = code
        p = palette()
        self.setObjectName("codeBlock")
        self.setStyleSheet(
            f"#codeBlock {{ background:{p['code_bg']}; border:1px solid {p['border']}; border-radius:10px; }}"
            f"#codeHead {{ background:{p['code_head']}; border-top-left-radius:10px; border-top-right-radius:10px; }}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        head = QFrame(self)
        head.setObjectName("codeHead")
        hl = QHBoxLayout(head)
        hl.setContentsMargins(12, 2, 4, 2)
        self.lang = CaptionLabel(lang or "text", head)
        self.lang.setStyleSheet(f"color:{p['muted']};")
        self.copyBtn = TransparentToolButton(FIF.COPY, head)
        self.copyBtn.setFixedSize(28, 28)
        self.copyBtn.setIconSize(QSize(13, 13))
        self.copyBtn.setToolTip("Copy code")
        self.copyBtn.installEventFilter(ToolTipFilter(self.copyBtn))
        self.copyBtn.clicked.connect(self._copy)
        hl.addWidget(self.lang)
        hl.addStretch(1)
        hl.addWidget(self.copyBtn)
        lay.addWidget(head)

        self.view = QTextBrowser(self)
        self.view.setFrameShape(QFrame.NoFrame)
        self.view.setLineWrapMode(QTextBrowser.NoWrap)
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.view.setStyleSheet(f"QTextBrowser {{ background:transparent; color:{p['text']}; padding:8px 12px; }}")
        self.view.document().setDocumentMargin(2)
        lay.addWidget(self.view)
        self.set_code(code, lang)

    def set_code(self, code: str, lang: str) -> None:
        self.code = code
        self.lang.setText(lang or "text")
        self.view.setHtml(code_html(code, lang))
        self._fit()

    def _fit(self) -> None:
        doc_h = self.view.document().size().height()
        bar = self.view.horizontalScrollBar().sizeHint().height() if self.view.document().idealWidth() > self.view.viewport().width() else 0
        self.view.setFixedHeight(int(doc_h) + 20 + bar)

    def resizeEvent(self, e) -> None:  # noqa: N802
        super().resizeEvent(e)
        self._fit()

    def _copy(self) -> None:
        QGuiApplication.clipboard().setText(self.code)
        self.copyBtn.setIcon(FIF.ACCEPT)
        QTimer.singleShot(1200, lambda: self.copyBtn.setIcon(FIF.COPY))


class MarkdownView(QWidget):
    """Renders markdown as alternating rich-text labels and code blocks."""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(10)
        self._sig: list[tuple[str, str]] = []
        self._widgets: list[QWidget] = []
        self.set_markdown(text)

    def set_markdown(self, text: str) -> None:
        segs = split_segments(text)
        sig = [(s.kind, s.lang) for s in segs]
        if sig[: len(self._sig)] == self._sig and len(self._widgets) == len(self._sig):
            # Incremental update (streaming): reuse widgets, add new ones.
            for i, s in enumerate(segs):
                if i < len(self._widgets):
                    self._update(self._widgets[i], s)
                else:
                    self._add(s)
        else:
            for w in self._widgets:
                w.deleteLater()
            self._widgets.clear()
            for s in segs:
                self._add(s)
        self._sig = sig

    def _add(self, s) -> None:
        w = CodeBlock(s.body, s.lang, self) if s.kind == "code" else RichLabel(text_html(s.body), self)
        self._widgets.append(w)
        self.lay.addWidget(w)

    @staticmethod
    def _update(w: QWidget, s) -> None:
        if isinstance(w, CodeBlock):
            if w.code != s.body:
                w.set_code(s.body, s.lang)
        else:
            w.setText(text_html(s.body))


class TypingDots(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(44, 22)
        self._phase = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(140)

    def _tick(self) -> None:
        self._phase = (self._phase + 1) % 12
        self.update()

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        base = QColor(palette()["muted"])
        for i in range(3):
            d = (self._phase // 2 - i * 2) % 6
            col = QColor(base)
            col.setAlphaF(1.0 if d == 0 else 0.55 if d == 1 else 0.25)
            p.setBrush(col)
            y = 8 if d == 0 else 10
            p.drawEllipse(4 + i * 13, y, 7, 7)


class AssistantMessage(QWidget):
    def __init__(self, engine: str, label: str, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.text = text
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(4)
        head = QHBoxLayout()
        head.setSpacing(6)
        badge = QLabel(self)
        badge.setPixmap(engine_badge(engine).scaled(16, 16, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        badge.setFixedSize(16, 16)
        name = CaptionLabel(label, self)
        name.setStyleSheet(f"color:{palette()['muted']};")
        head.addWidget(badge)
        head.addWidget(name)
        head.addStretch(1)
        lay.addLayout(head)
        self.body = MarkdownView(text, self)
        wrap = QHBoxLayout()
        wrap.setContentsMargins(22, 0, 0, 0)
        wrap.addWidget(self.body)
        lay.addLayout(wrap)
        self.dots: TypingDots | None = None

    def set_text(self, text: str) -> None:
        self.text = text
        self.body.set_markdown(text)


class UserMessage(QWidget):
    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        p = palette()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(48 if parent is not None and parent.objectName() == "appearancePreview" else 96, 2, 0, 2)
        lay.addStretch(1)
        bubble = QFrame(self)
        bubble.setObjectName("userBubble")
        bubble.setStyleSheet(f"#userBubble {{ background:{p['user_bubble']}; border-radius:12px; }}")
        bl = QVBoxLayout(bubble)
        bl.setContentsMargins(12, space(7), 12, space(7))
        lbl = QLabel(bubble)
        lbl.setTextFormat(Qt.PlainText)
        lbl.setText(text)
        lbl.setWordWrap(True)
        lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        f = chat_font()
        lbl.setFont(f)
        lbl.setStyleSheet(f"color:{p['text']}; background:transparent;")
        # short messages stay on one line instead of wrapping into a narrow column
        self.label = lbl
        self.natural = min(max((lbl.fontMetrics().horizontalAdvance(line) for line in text.splitlines() or [""]),
                               default=0) + 4, 520)
        lbl.setMinimumWidth(self.natural)
        bl.addWidget(lbl)
        bubble.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Minimum)
        lay.addWidget(bubble)

    def fit(self, width: int) -> None:
        """Cap the one-line minimum to the space the chat has (narrow panes, side by side)."""
        self.label.setMinimumWidth(max(60, min(self.natural, int(width * 0.8) - 24)))


TOOL_META = {
    "shell": (FIF.COMMAND_PROMPT, "Ran command"),
    "Bash": (FIF.COMMAND_PROMPT, "Ran command"),
    "edit": (FIF.EDIT, "Edited files"),
    "Edit": (FIF.EDIT, "Edited file"),
    "Write": (FIF.DOCUMENT, "Wrote file"),
    "Read": (FIF.DOCUMENT, "Read file"),
    "Glob": (FIF.SEARCH, "Found files"),
    "Grep": (FIF.SEARCH, "Searched"),
    "web_search": (FIF.GLOBE, "Searched the web"),
    "WebSearch": (FIF.GLOBE, "Searched the web"),
    "WebFetch": (FIF.GLOBE, "Fetched page"),
    "plan": (FIF.CHECKBOX, "Updated plan"),
    "TodoWrite": (FIF.CHECKBOX, "Updated plan"),
}


class ToolCard(QFrame):
    def __init__(self, name: str, summary: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        p = palette()
        self.setObjectName("toolCard")
        self.setStyleSheet(
            "#toolCard { background:transparent; border:none; }"
            f"#toolHead {{ border-radius:8px; }} #toolHead:hover {{ background:{p['card_hover']}; }}")
        icon, title = TOOL_META.get(name, (FIF.DEVELOPER_TOOLS, f"Used {name}"))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.head = QFrame(self)
        self.head.setObjectName("toolHead")
        self.head.setCursor(Qt.PointingHandCursor)
        hl = QHBoxLayout(self.head)
        hl.setContentsMargins(22, 4, 8, 4)
        hl.setSpacing(8)
        ic = IconWidget(icon, self.head)
        ic.setFixedSize(13, 13)
        t = CaptionLabel(title, self.head)
        t.setStyleSheet(f"color:{p['muted']};")
        self.summary = QLabel(summary, self.head)
        self.summary.setStyleSheet(f"color:{p['faint']}; font-family:{mono_css()}; font-size:{code_pt(-1)}; background:transparent;")
        self.summary.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.ring = IndeterminateProgressRing(self.head)
        self.ring.setFixedSize(13, 13)
        self.ring.setStrokeWidth(2)
        self.state = IconWidget(FIF.ACCEPT, self.head)
        self.state.setFixedSize(11, 11)
        self.state.hide()
        self.chev = IconWidget(FIF.CHEVRON_RIGHT_MED, self.head)
        self.chev.setFixedSize(10, 10)
        for w in (ic, t):
            hl.addWidget(w)
        hl.addWidget(self.summary, 1)
        hl.addWidget(self.ring)
        hl.addWidget(self.state)
        hl.addWidget(self.chev)
        lay.addWidget(self.head)

        self.out = QTextBrowser(self)
        self.out.setFrameShape(QFrame.NoFrame)
        self.out.setLineWrapMode(QTextBrowser.NoWrap)
        self.out.setStyleSheet(
            f"QTextBrowser {{ background:{p['code_bg']}; color:{p['text']}; border:1px solid {p['border']};"
            f" border-radius:8px; margin-left:22px; padding:6px 10px;"
            f" font-family:{mono_css()}; font-size:{code_pt(-0.5)}; }}")
        self.out.setMaximumHeight(0)
        self.out.hide()
        lay.addWidget(self.out)
        self._expanded = False
        self._anim = QPropertyAnimation(self.out, b"maximumHeight", self)
        self._anim.setDuration(180)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.finished.connect(self._anim_done)
        self.head.mousePressEvent = lambda _e: self.toggle()  # type: ignore[assignment]

    def set_result(self, output: str, is_error: bool) -> None:
        self.ring.hide()
        self.state.setIcon(FIF.CLOSE if is_error else FIF.ACCEPT)
        self.state.show()
        self.out.setPlainText(output.strip() or "(no output)")
        if is_error:
            self.summary.setStyleSheet(self.summary.styleSheet() + "color:#f85149;")

    def toggle(self) -> None:
        self._expanded = not self._expanded
        self.chev.setIcon(FIF.CHEVRON_DOWN_MED if self._expanded else FIF.CHEVRON_RIGHT_MED)
        target = min(260, int(self.out.document().size().height()) + 24) if self._expanded else 0
        self.out.show()
        self._anim.stop()
        self._anim.setStartValue(self.out.maximumHeight())
        self._anim.setEndValue(target)
        self._anim.start()

    def _anim_done(self) -> None:
        if not self._expanded:
            self.out.hide()
        else:
            self.out.setFixedHeight(self.out.maximumHeight())
            self.out.setMinimumHeight(0)


class NoticeCard(QFrame):
    action = Signal()

    def __init__(self, text: str, kind: str = "error", action_text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        p = palette()
        bg = p["warn_bg"] if kind == "warning" else p["error_bg"]
        border = "rgba(245,181,68,0.35)" if kind == "warning" else p["error_border"]
        self.setObjectName("notice")
        self.setStyleSheet(f"#notice {{ background:{bg}; border:1px solid {border}; border-radius:10px; }}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(10)
        ic = IconWidget(InfoBarIcon.WARNING if kind == "warning" else InfoBarIcon.ERROR, self)
        ic.setFixedSize(16, 16)
        lbl = QLabel(text, self)
        lbl.setWordWrap(True)
        lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lbl.setStyleSheet(f"color:{p['text']}; background:transparent;")
        lay.addWidget(ic, 0, Qt.AlignTop)
        lay.addWidget(lbl, 1)
        if action_text:
            b = PushButton(action_text, self)
            b.clicked.connect(self.action.emit)
            lay.addWidget(b, 0, Qt.AlignVCenter)


class WarningLine(QWidget):
    """Compact, low-key notice for non-fatal engine warnings."""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(22, 0, 0, 0)
        lay.setSpacing(6)
        ic = IconWidget(FIF.INFO, self)
        ic.setFixedSize(12, 12)
        first = " ".join(text.replace(str(Path.home()), "~").split())
        lbl = QLabel(first, self)
        lbl.setStyleSheet(f"color:{palette()['faint']}; font-size:8.5pt; background:transparent;")
        lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setToolTip(text)
        self.installEventFilter(ToolTipFilter(self))
        lay.addWidget(ic)
        lay.addWidget(lbl, 1)


class ContextChip(QWidget):
    """Muted marker where harness/system text was hidden from an imported transcript."""

    def __init__(self, tags: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addStretch(1)
        chip = QLabel("system context", self)
        chip.setObjectName("contextChip")
        p = palette()
        chip.setStyleSheet(f"#contextChip {{ color:{p['faint']}; border:1px solid {p['border']}; border-radius:8px;"
                           " padding:1px 8px; font-size:8pt; background:transparent; }}")
        names = ", ".join(t for t in tags if t != "tag") or "system"
        chip.setToolTip(f"Hidden harness text: {names}")
        chip.installEventFilter(ToolTipFilter(chip))
        lay.addWidget(chip)


class ApprovalCard(QFrame):
    answered = Signal(bool)

    def __init__(self, engine_label: str, tool: str, detail: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        p = palette()
        self.setObjectName("approval")
        self.setStyleSheet(f"#approval {{ background:{p['warn_bg']}; border:1px solid rgba(245,181,68,0.4); border-radius:10px; }}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)
        t = BodyLabel(f"{engine_label} wants to use <b>{tool}</b>", self)
        t.setTextFormat(Qt.RichText)
        d = QLabel(detail, self)
        d.setWordWrap(True)
        d.setStyleSheet(f"color:{p['muted']}; font-family:{mono_css()}; font-size:{code_pt(-0.5)}; background:transparent;")
        row = QHBoxLayout()
        self.allow = PrimaryPushButton("Allow", self)
        self.deny = PushButton("Deny", self)
        self.result = CaptionLabel("", self)
        row.addWidget(self.allow)
        row.addWidget(self.deny)
        row.addWidget(self.result)
        row.addStretch(1)
        lay.addWidget(t)
        lay.addWidget(d)
        lay.addLayout(row)
        self.allow.clicked.connect(lambda: self._answer(True))
        self.deny.clicked.connect(lambda: self._answer(False))

    def _answer(self, ok: bool) -> None:
        self.allow.hide()
        self.deny.hide()
        self.result.setText("Allowed" if ok else "Denied")
        self.answered.emit(ok)


class UsageLine(QLabel):
    def __init__(self, inp: int, out: int, cached: int, cost: float | None, parent: QWidget | None = None) -> None:
        def k(n: int) -> str:
            return f"{n / 1000:.1f}k" if n >= 1000 else str(n)

        parts = [f"{k(inp)} in", f"{k(out)} out"]
        if cached:
            parts.append(f"{k(cached)} cached")
        if cost:
            parts.append(f"${cost:.4f}")
        super().__init__("  ·  ".join(parts), parent)
        self.setAlignment(Qt.AlignLeft)
        self.setContentsMargins(22, 0, 0, 0)
        self.setStyleSheet(f"color:{palette()['faint']}; font-size:8.5pt; background:transparent;")


class AppliedCard(QFrame):
    """'Applied: …' for an app setting changed from chat, with Undo."""

    def __init__(self, summary: str, change_id: str | None, undo=None, undone: bool = False,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        p = palette()
        self.change_id = change_id
        self.setObjectName("appliedCard")
        self.setStyleSheet(f"#appliedCard {{ background:{p['card']}; border:1px solid {p['border']};"
                           " border-radius:10px; }")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, space(6), 8, space(6))
        lay.setSpacing(8)
        icon = QLabel("✓", self)
        icon.setStyleSheet(f"color:{p['accent']}; font-weight:700; background:transparent;")
        lay.addWidget(icon)
        self.label = QLabel(self)
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.PlainText)
        self.label.setStyleSheet(f"color:{p['text']}; background:transparent;")
        lay.addWidget(self.label, 1)
        from qfluentwidgets import TransparentPushButton

        self.undoBtn = TransparentPushButton("Undo", self)
        self.undoBtn.setObjectName("undoChange")
        self.undoBtn.setMinimumWidth(self.undoBtn.sizeHint().width())
        lay.addWidget(self.undoBtn)
        self.summary = summary
        self._undo = undo
        self.undoBtn.clicked.connect(self._click)
        self.set_undone(undone)
        if not (undo and change_id):
            self.undoBtn.hide()

    def _click(self) -> None:
        if self._undo:
            self._undo(self.change_id)

    def set_undone(self, undone: bool) -> None:
        self.label.setText(("Undone: " if undone else "Applied: ") + self.summary)
        self.undoBtn.setVisible(not undone and bool(self._undo and self.change_id))


class ConfirmCard(QFrame):
    """Allow / Deny for a destructive app change asked from chat (disable a model, delete a schedule)."""

    def __init__(self, text: str, fut, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        p = palette()
        self.fut = fut
        self.setObjectName("confirmCard")
        self.setStyleSheet(f"#confirmCard {{ background:{p['warn_bg']}; border:1px solid rgba(245,181,68,0.45);"
                           " border-radius:10px; }")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 8, 8)
        self.label = QLabel(text, self)
        self.label.setWordWrap(True)
        self.label.setStyleSheet(f"color:{p['text']}; background:transparent;")
        lay.addWidget(self.label, 1)
        from qfluentwidgets import PrimaryPushButton, PushButton

        self.denyBtn = PushButton("Deny", self)
        self.allowBtn = PrimaryPushButton("Allow", self)
        for b in (self.denyBtn, self.allowBtn):
            b.setMinimumWidth(b.sizeHint().width())
            lay.addWidget(b)
        self.allowBtn.clicked.connect(lambda: self._answer(True))
        self.denyBtn.clicked.connect(lambda: self._answer(False))
        fut.add_done_callback(lambda f: self._done(f))

    def _answer(self, ok: bool) -> None:
        if not self.fut.done():
            self.fut.set_result(ok)

    def _done(self, f) -> None:
        try:
            ok = bool(f.result())
        except Exception:  # noqa: BLE001 - cancelled / timed out
            ok = False
        try:
            self.allowBtn.hide()
            self.denyBtn.hide()
            self.label.setText(self.label.text() + ("  — allowed" if ok else "  — denied"))
        except RuntimeError:  # card already deleted
            pass
