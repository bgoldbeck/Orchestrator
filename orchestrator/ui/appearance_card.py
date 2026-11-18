"""Settings › Appearance: fonts, sizes, density, theme, accent, with a live preview. Changes apply at once."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ColorDialog,
    ComboBox,
    FluentIcon as FIF,
    PushButton,
    SegmentedWidget,
    StrongBodyLabel,
)

from orchestrator.storage.db import Database
from orchestrator.ui import appearance as A
from orchestrator.ui import theme as T

PREVIEW_MD = ("Here is the fix. The cache key now includes the `days` parameter, so a 3-day and a 7-day request "
              "no longer share an entry.")
PREVIEW_CODE = "def cache_key(city: str, days: int) -> str:\n    return f\"forecast:{city}:{days}\""


def _fmt(v: float) -> str:
    return f"{v:g} pt"


class AppearanceCard(QWidget):
    def __init__(self, db: Database, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.db = db
        self.look = A.load(db)
        pal = T.palette()
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(16)
        form = QGridLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(10)
        form.setColumnStretch(1, 1)
        self.rows = 0

        def row(label: str, widget: QWidget, sub: str = "") -> None:
            col = QVBoxLayout()
            col.setSpacing(0)
            col.addWidget(BodyLabel(label, self))
            if sub:
                s = CaptionLabel(sub, self)
                s.setWordWrap(True)
                s.setStyleSheet(f"color:{pal['muted']};")
                col.addWidget(s)
            form.addLayout(col, self.rows, 0)
            form.addWidget(widget, self.rows, 1, Qt.AlignLeft | Qt.AlignVCenter)
            self.rows += 1

        self.theme = SegmentedWidget(self)
        for key, label in A.THEMES.items():
            self.theme.addItem(key, label, lambda k=key: self._set(theme=k))
        self.theme.setCurrentItem(self.look.theme)
        row("Theme", self.theme, "System follows Windows.")

        acc = QWidget(self)
        al = QHBoxLayout(acc)
        al.setContentsMargins(0, 0, 0, 0)
        al.setSpacing(6)
        self.swatches: dict[str, QLabel] = {}
        for name, hexv in T.ACCENTS.items():
            sw = QLabel(acc)
            sw.setFixedSize(26, 26)
            sw.setCursor(Qt.PointingHandCursor)
            sw.setToolTip(name)
            sw.setObjectName(f"accent{name}")
            sw.mousePressEvent = lambda _e, n=name: self._set(accent=n)
            self.swatches[name] = sw
            al.addWidget(sw)
        self.customAccent = PushButton(FIF.PALETTE, "Custom…", acc)
        self.customAccent.setMinimumWidth(self.customAccent.sizeHint().width())
        self.customAccent.clicked.connect(self._pick_color)
        al.addWidget(self.customAccent)
        row("Accent color", acc)

        self.font = self._combo(A.installed(A.UI_FONT_CHOICES, self.look.font), self.look.font,
                                lambda v: self._set(font=v))
        row("Font", self.font, "Used for the whole app.")
        self.uiSize = self._combo([_fmt(v) for v in A.UI_SIZES], _fmt(self.look.ui_size),
                                  lambda v: self._set(ui_size=float(v.split()[0])))
        row("UI font size", self.uiSize, "Menus, buttons, labels. Default 10 pt.")
        self.chatSize = self._combo([_fmt(v) for v in A.CHAT_SIZES], _fmt(self.look.chat_size),
                                    lambda v: self._set(chat_size=float(v.split()[0])))
        row("Chat font size", self.chatSize, "Messages and the prompt box. Default 10.5 pt.")
        codeRow = QWidget(self)
        cl = QHBoxLayout(codeRow)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(6)
        self.codeFont = self._combo(A.installed(A.CODE_FONT_CHOICES, self.look.code_font), self.look.code_font,
                                    lambda v: self._set(code_font=v), codeRow)
        self.codeSize = self._combo([_fmt(v) for v in A.CODE_SIZES], _fmt(self.look.code_size),
                                    lambda v: self._set(code_size=float(v.split()[0])), codeRow)
        cl.addWidget(self.codeFont)
        cl.addWidget(self.codeSize)
        row("Code font", codeRow, "Code blocks, inline code and tool output.")
        self.density = SegmentedWidget(self)
        for d in A.DENSITIES:
            self.density.addItem(d, d, lambda v=d: self._set(density=v))
        self.density.setCurrentItem(self.look.density)
        row("Density", self.density, "Compact fits more chats and messages on screen.")
        self.resetBtn = PushButton(FIF.SYNC, "Reset to defaults", self)
        self.resetBtn.setMinimumWidth(self.resetBtn.sizeHint().width())
        self.resetBtn.clicked.connect(self.reset)
        form.addWidget(self.resetBtn, self.rows, 0, Qt.AlignLeft)
        outer.addLayout(form, 3)

        self.preview = QFrame(self)
        self.preview.setObjectName("appearancePreview")
        self.preview.setMinimumWidth(320)
        self.previewLay = QVBoxLayout(self.preview)
        outer.addWidget(self.preview, 2)
        self._paint_swatches()
        self.render_preview()
        A.hub.changed.connect(self._on_changed)

    def _combo(self, items: list[str], current: str, on_change, parent: QWidget | None = None) -> ComboBox:
        cb = ComboBox(parent or self)
        cb.addItems(items)
        if current in items:
            cb.setCurrentText(current)
        cb.setMinimumWidth(max(110, cb.sizeHint().width()))
        cb.currentTextChanged.connect(on_change)
        return cb

    # ---- changes
    def _set(self, **kw) -> None:
        for k, v in kw.items():
            setattr(self.look, k, v)
        A.save(self.db, self.look)
        # apply after the click/selection event finishes (theme switch restyles every widget)
        QTimer.singleShot(0, lambda: A.apply(self.look))

    def _pick_color(self) -> None:
        dlg = ColorDialog(QColor(A.accent_hex(self.look.accent)), "Accent color", self.window(), enableAlpha=False)
        dlg.colorChanged.connect(lambda c: self._set(accent=c.name()))
        dlg.exec()

    def reset(self) -> None:
        self.look = A.Look()
        for cb, val in ((self.font, self.look.font), (self.uiSize, _fmt(self.look.ui_size)),
                        (self.chatSize, _fmt(self.look.chat_size)), (self.codeFont, self.look.code_font),
                        (self.codeSize, _fmt(self.look.code_size))):
            cb.blockSignals(True)
            cb.setCurrentText(val)
            cb.blockSignals(False)
        self.theme.setCurrentItem(self.look.theme)
        self.density.setCurrentItem(self.look.density)
        self._set()

    def _on_changed(self) -> None:
        self._paint_swatches()
        self.render_preview()

    def _paint_swatches(self) -> None:
        cur = self.look.accent
        for name, sw in self.swatches.items():
            ring = T.palette()["text"] if name == cur else "transparent"
            sw.setStyleSheet(f"QLabel {{ background:{T.ACCENTS[name]}; border-radius:13px; border:2px solid {ring}; }}")
        self.customAccent.setText("Custom…" if cur in T.ACCENTS else f"Custom {cur}")

    # ---- live preview: real chat widgets with the current settings
    def render_preview(self) -> None:
        from orchestrator.ui.chat_widgets import AssistantMessage, UserMessage

        pal = T.palette()
        self.preview.setStyleSheet(f"#appearancePreview {{ background:{pal['code_bg']}; border:1px solid {pal['border']};"
                                   " border-radius:12px; }")
        while self.previewLay.count():
            it = self.previewLay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self.previewLay.setContentsMargins(14, T.space(12), 14, T.space(12))
        self.previewLay.setSpacing(T.space(10))
        head = StrongBodyLabel("Preview", self.preview)
        self.previewLay.addWidget(head)
        self.previewLay.addWidget(UserMessage("Why do 3- and 7-day forecasts match?", self.preview))
        msg = AssistantMessage("claude", "Claude · Opus 5.5", PREVIEW_MD + "\n\n```python\n" + PREVIEW_CODE + "\n```",
                               self.preview)
        self.previewLay.addWidget(msg)
        self.previewLay.addStretch(1)
