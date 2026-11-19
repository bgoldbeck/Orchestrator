"""Plan usage: compact per-provider pills for the title bar, and the Usage page with details and history."""
from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    FluentIcon as FIF,
    PushButton,
    SmoothScrollArea,
    StrongBodyLabel,
    TitleLabel,
    ToolTipFilter,
)

from orchestrator.core import usage as U
from orchestrator.core.scheduler import plain_when
from orchestrator.storage.db import Database
from orchestrator.ui.theme import palette

LEVEL_COLORS = {"green": "#3fb950", "amber": "#f5b544", "red": "#f85149", "unknown": "#6e7681"}
REFRESH_MS = 120_000


def qcolor(css: str) -> QColor:
    """QColor from '#rrggbb' or CSS 'rgba(r,g,b,a)' (the palette uses both)."""
    m = re.match(r"rgba?\(([^)]*)\)", css.strip())
    if not m:
        return QColor(css)
    parts = [float(x) for x in m.group(1).split(",")]
    col = QColor(int(parts[0]), int(parts[1]), int(parts[2]))
    if len(parts) > 3:
        col.setAlphaF(parts[3])
    return col


def _when(ts: float | None) -> str:
    return plain_when(datetime.fromtimestamp(ts)) if ts else "unknown"


def window_text(w: U.Window) -> str:
    if w.remaining_pct is None:
        head = f"{w.label}: no percentage reported"
    else:
        head = f"{w.label}: {w.used_pct:.0f}% used · {w.remaining_pct:.0f}% left" + ("" if w.exact else " (estimate)")
    if w.resets_at:
        head += f" · resets {_when(w.resets_at)}"
    if w.status == "allowed_warning":
        head += " · close to the limit"
    elif w.status == "rejected":
        head += " · limit reached"
    return head


def tooltip_for(u: U.ProviderUsage) -> str:
    lines = [u.name]
    for w in u.windows:
        lines.append(window_text(w))
        if w.estimate and not w.exact:
            lines.append("   " + w.estimate)
    if u.note:
        lines.append(u.note)
    lines.append("Click for details.")
    return "\n".join(lines)


class UsagePill(QWidget):
    """'Codex' + two thin bars (5-hour on top, weekly below), colored green / amber / red."""

    clicked = Signal(str)

    def __init__(self, provider: str, name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.provider, self.name = provider, name
        self.usage: U.ProviderUsage | None = None
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(30)
        self.installEventFilter(ToolTipFilter(self, 300))
        self._hover = False

    def sizeHint(self) -> QSize:  # noqa: N802
        f = QFont(self.font())
        f.setPointSizeF(8.5)
        from PySide6.QtGui import QFontMetrics

        fm = QFontMetrics(f)
        return QSize(fm.horizontalAdvance(self.name) + 18 + 58 + 8 + fm.horizontalAdvance("100% left") + 10, 30)

    def set_usage(self, u: U.ProviderUsage) -> None:
        self.usage = u
        self.setToolTip(tooltip_for(u))
        self.setAccessibleName(tooltip_for(u).replace("\n", ". "))
        self.update()

    def enterEvent(self, e) -> None:  # noqa: N802
        self._hover = True
        self.update()

    def leaveEvent(self, e) -> None:  # noqa: N802
        self._hover = False
        self.update()

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802
        if e.button() == Qt.LeftButton:
            self.clicked.emit(self.provider)

    def mousePressEvent(self, e) -> None:  # noqa: N802
        e.accept()  # keep the title bar from starting a window drag

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = palette()
        r = QRectF(self.rect()).adjusted(1, 3, -1, -3)
        path = QPainterPath()
        path.addRoundedRect(r, 10, 10)
        p.fillPath(path, qcolor(pal["card_hover"] if self._hover else pal["card"]))
        p.setPen(qcolor(pal["border"]))
        p.drawPath(path)
        f = QFont(self.font())
        f.setPointSizeF(8.5)
        p.setFont(f)
        p.setPen(qcolor(pal["muted"]))
        text_w = p.fontMetrics().horizontalAdvance(self.name)
        p.drawText(QRectF(r.left() + 9, r.top(), text_w + 2, r.height()), Qt.AlignVCenter, self.name)
        x = r.left() + 9 + text_w + 8
        bw = 58
        windows = self.usage.windows if self.usage else []
        for i, key in enumerate(("five_hour", "weekly")):
            w = next((w for w in windows if w.key == key), None)
            y = r.center().y() - 4 + i * 6
            track = QRectF(x, y, bw, 3)
            p.setPen(Qt.NoPen)
            p.setBrush(qcolor(pal["border"]))
            p.drawRoundedRect(track, 1.5, 1.5)
            if w and w.remaining_pct is not None:  # the bar fills with what is LEFT
                col = QColor(LEVEL_COLORS[U.level(w.remaining_pct)])
                if not w.exact:
                    col.setAlphaF(0.55)
                p.setBrush(col)
                p.drawRoundedRect(QRectF(x, y, max(3.0, bw * w.remaining_pct / 100), 3), 1.5, 1.5)
        tight = U.tightest(windows)
        pct = f"{tight.remaining_pct:.0f}% left" if tight else "—"  # what limits you first
        p.setPen(qcolor(pal["text"]))
        p.drawText(QRectF(x + bw + 6, r.top(), r.right() - (x + bw + 6) - 4, r.height()),
                   Qt.AlignVCenter | Qt.AlignLeft, pct)
        p.end()


def read_usage(db: Database) -> list[U.ProviderUsage]:
    items = U.read_all(db)
    U.persist(db)
    U.record_history(db, items)
    return items


class UsageBar(QWidget):
    """One pill per registered provider; refreshes on a timer and after turns finish."""

    openUsage = Signal(str)
    refreshed = Signal()

    def __init__(self, db: Database, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.db = db
        self.items: list[U.ProviderUsage] = []
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 8, 0)
        lay.setSpacing(6)
        self.pills: dict[str, UsagePill] = {}
        for prov in U.providers():
            pill = UsagePill(prov.id, prov.name, self)
            pill.clicked.connect(self.openUsage.emit)
            self.pills[prov.id] = pill
            lay.addWidget(pill)
        self._busy = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._soon = QTimer(self)
        self._soon.setSingleShot(True)
        self._soon.timeout.connect(self.refresh)

    def start(self) -> None:
        self.refresh()
        self._timer.start(REFRESH_MS)

    def refresh_soon(self, ms: int = 1500) -> None:
        self._soon.start(ms)

    def refresh(self) -> None:
        if self._busy:
            return
        try:
            running = asyncio.get_running_loop().is_running()  # qasync marks its loop running for Qt slots
        except RuntimeError:
            running = False
        if not running:
            self.apply(read_usage(self.db))
            return
        self._busy = True

        async def go() -> None:
            try:
                snap = U.snapshot(self.db)
                items = await asyncio.to_thread(U.read_all, snap)  # log reading stays off the GUI thread
                U.persist(self.db)
                U.record_history(self.db, items)
                self.apply(items)
            finally:
                self._busy = False

        asyncio.ensure_future(go())

    def apply(self, items: list[U.ProviderUsage]) -> None:
        self.items = items
        for u in items:
            pill = self.pills.get(u.provider)
            if pill:
                pill.set_usage(u)
        self.refreshed.emit()


class Spark(QWidget):
    """Tiny history line of used % over time."""

    def __init__(self, points: list[dict], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.points = points[-48:]
        self.setFixedHeight(34)
        self.setMinimumWidth(120)

    def paintEvent(self, _e) -> None:  # noqa: N802
        if len(self.points) < 2:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(2, 3, -2, -3)
        t0, t1 = self.points[0]["t"], self.points[-1]["t"]
        span = max(1.0, t1 - t0)
        path = QPainterPath()
        for i, pt in enumerate(self.points):
            x = r.left() + (pt["t"] - t0) / span * r.width()
            y = r.bottom() - min(100.0, pt["v"]) / 100 * r.height()
            path.moveTo(x, y) if i == 0 else path.lineTo(x, y)
        pen = p.pen()
        pen.setColor(QColor(LEVEL_COLORS[U.level(self.points[-1]["v"])]))  # samples are percent left
        pen.setWidthF(1.6)
        p.setPen(pen)
        p.drawPath(path)
        p.end()


class WindowRow(QWidget):
    def __init__(self, provider: str, w: U.Window, db: Database, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        pal = palette()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 4)
        lay.setSpacing(4)
        top = QHBoxLayout()
        name = StrongBodyLabel(w.label, self)
        top.addWidget(name)
        badge = QLabel("exact" if w.exact else "estimate", self)
        col = LEVEL_COLORS["green"] if w.exact else LEVEL_COLORS["amber"]
        badge.setObjectName("exactBadge")
        badge.setStyleSheet(f"QLabel {{ color:{col}; border:1px solid {col}; border-radius:8px; padding:0 7px;"
                            " font-size:8pt; background:transparent; }}")
        top.addWidget(badge)
        top.addStretch(1)
        pct = BodyLabel("—" if w.remaining_pct is None else f"{w.remaining_pct:.0f}% left", self)
        pct.setObjectName("leftLabel")
        if w.remaining_pct is not None:
            pct.setToolTip(f"{w.used_pct:.0f}% used · {w.remaining_pct:.0f}% left")
        top.addWidget(pct)
        lay.addLayout(top)
        bar = QFrame(self)
        bar.setFixedHeight(8)
        level = U.level(w.remaining_pct)
        frac = 0 if w.remaining_pct is None else w.remaining_pct / 100
        bar.setStyleSheet(
            "QFrame { border-radius:4px; background: qlineargradient(x1:0, y1:0, x2:1, y2:0, "
            f"stop:0 {LEVEL_COLORS[level]}, stop:{max(0.001, frac):.3f} {LEVEL_COLORS[level]}, "
            f"stop:{min(0.999, frac + 0.001):.3f} {pal['border']}, stop:1 {pal['border']}); }}")
        lay.addWidget(bar)
        bits = []
        if w.resets_at:
            bits.append(f"Resets {_when(w.resets_at)}")
        if w.status:
            bits.append({"allowed": "Within the limit", "allowed_warning": "Close to the limit",
                         "rejected": "Limit reached"}.get(w.status, w.status))
        if w.updated_at:
            day = _when(w.updated_at).split(",")[0]
            bits.append("updated " + day[:1].lower() + day[1:])
        if bits:
            lay.addWidget(self._muted(" · ".join(bits)))
        if w.estimate:
            lay.addWidget(self._muted(w.estimate))
        hist = U.history(db, provider, w.key)
        if len(hist) >= 2:
            row = QHBoxLayout()
            row.addWidget(self._muted("History"))
            row.addWidget(Spark(hist, self), 1)
            lay.addLayout(row)

    def _muted(self, text: str) -> CaptionLabel:
        lb = CaptionLabel(text, self)
        lb.setWordWrap(True)
        lb.setStyleSheet(f"color:{palette()['muted']};")
        return lb


class UsagePage(SmoothScrollArea):
    def __init__(self, db: Database, bar: UsageBar, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.db, self.bar = db, bar
        self.setObjectName("usagePage")
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setStyleSheet("QScrollArea { background:transparent; border:none; }")
        body = QWidget()
        body.setStyleSheet("background:transparent;")
        self.lay = QVBoxLayout(body)
        self.lay.setContentsMargins(36, 28, 36, 36)
        self.lay.setSpacing(12)
        head = QHBoxLayout()
        tcol = QVBoxLayout()
        tcol.addWidget(TitleLabel("Usage", body))
        sub = CaptionLabel("Plan limits per provider: the 5-hour window and the weekly window, shown as percent "
                           "left for every provider. Bars fill with what is left: green above 30% left, amber "
                           "10–30%, red under 10%.", body)
        sub.setWordWrap(True)
        sub.setStyleSheet(f"color:{palette()['muted']};")
        tcol.addWidget(sub)
        head.addLayout(tcol, 1)
        self.refreshBtn = PushButton(FIF.SYNC, "Refresh", body)
        self.refreshBtn.setMinimumWidth(self.refreshBtn.sizeHint().width())
        self.refreshBtn.clicked.connect(bar.refresh)
        head.addWidget(self.refreshBtn, 0, Qt.AlignTop)
        self.lay.addLayout(head)
        self.cards = QVBoxLayout()
        self.cards.setSpacing(12)
        self.lay.addLayout(self.cards)
        how = CaptionLabel(
            "How it is measured — Codex: exact, read from the rate limits Codex records in its session logs after "
            "every turn. Claude: exact when the Claude Code CLI reports its rate-limit status during a turn in this "
            "app; otherwise an estimate (tokens used in the window, from local Claude Code logs; no percentage, "
            "because the logs do not include the plan size).", body)
        how.setWordWrap(True)
        how.setStyleSheet(f"color:{palette()['faint']};")
        self.lay.addWidget(how)
        self.lay.addStretch(1)
        self.setWidget(body)
        bar.refreshed.connect(self.render)
        self.render()

    def render(self) -> None:
        while self.cards.count():
            w = self.cards.takeAt(0).widget()
            if w:
                w.deleteLater()
        pal = palette()
        items = self.bar.items or read_usage(self.db)
        for u in items:
            card = QFrame(self.widget())
            card.setObjectName("usageCard")
            card.setStyleSheet(f"#usageCard {{ background:{pal['card']}; border:1px solid {pal['border']};"
                               " border-radius:12px; }")
            cl = QVBoxLayout(card)
            cl.setContentsMargins(18, 14, 18, 14)
            cl.setSpacing(6)
            top = QHBoxLayout()
            top.addWidget(StrongBodyLabel(u.name, card))
            top.addStretch(1)
            src = CaptionLabel(u.source, card)
            src.setStyleSheet(f"color:{pal['muted']};")
            top.addWidget(src)
            cl.addLayout(top)
            for w in u.windows:
                cl.addWidget(WindowRow(u.provider, w, self.db, card))
            if u.note:
                n = CaptionLabel(u.note, card)
                n.setWordWrap(True)
                n.setStyleSheet(f"color:{pal['muted']};")
                cl.addWidget(n)
            card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
            self.cards.addWidget(card)

    def showEvent(self, e) -> None:  # noqa: N802
        super().showEvent(e)
        if time.time() - getattr(self, "_shown", 0) > 30:
            self._shown = time.time()
            self.bar.refresh()
