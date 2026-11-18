"""Colors, fonts and small styling helpers shared by the UI."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from qfluentwidgets import isDarkTheme, themeColor

UI_FONT = "Segoe UI Variable Text"
UI_FONTS = ["Segoe UI Variable Text", "Segoe UI Variable", "Segoe UI", "Microsoft YaHei"]
MONO_FONT = "Cascadia Code, Cascadia Mono, Consolas, monospace"

ACCENTS = {
    "Sky": "#4cc2ff",
    "Violet": "#a78bfa",
    "Emerald": "#34d399",
    "Amber": "#f5b544",
    "Rose": "#fb7185",
}

STATUS_COLORS = {
    "idle": "#7d8590",
    "running": "#4cc2ff",
    "waiting": "#f5b544",
    "done": "#3fb950",
    "error": "#f85149",
}

ENGINE_COLORS = {"claude": "#d97757", "codex": "#10a37f"}


def c(dark: str, light: str) -> str:
    return dark if isDarkTheme() else light


def palette() -> dict[str, str]:
    return {
        "text": c("#e6e6e6", "#1b1b1b"),
        "muted": c("#9aa0a6", "#5f6368"),
        "faint": c("#6b7178", "#8a8f98"),
        "card": c("rgba(255,255,255,0.045)", "rgba(255,255,255,0.75)"),
        "card_hover": c("rgba(255,255,255,0.07)", "rgba(0,0,0,0.035)"),
        "border": c("rgba(255,255,255,0.08)", "rgba(0,0,0,0.09)"),
        "code_bg": c("#16181d", "#f6f8fa"),
        "code_head": c("#1f2229", "#eceff3"),
        "inline_code": c("rgba(255,255,255,0.09)", "rgba(0,0,0,0.06)"),
        "user_bubble": c("#2a2f3a", "#e8eef9"),
        "error_bg": c("rgba(248,81,73,0.10)", "rgba(248,81,73,0.08)"),
        "error_border": c("rgba(248,81,73,0.35)", "rgba(248,81,73,0.35)"),
        "warn_bg": c("rgba(245,181,68,0.10)", "rgba(245,181,68,0.12)"),
        "accent": themeColor().name(),
        "sidebar": c("rgba(0,0,0,0.12)", "rgba(255,255,255,0.35)"),
    }


def dot_icon(color: str, size: int = 10, ring: bool = False) -> QIcon:
    pm = QPixmap(size * 2, size * 2)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    col = QColor(color)
    if ring:
        halo = QColor(col)
        halo.setAlpha(70)
        p.setBrush(halo)
        p.drawEllipse(0, 0, size * 2, size * 2)
    p.setBrush(col)
    m = size // 2
    p.drawEllipse(m, m, size, size)
    p.end()
    return QIcon(pm)


def engine_badge(engine: str, size: int = 22) -> QPixmap:
    """Small rounded badge with the engine initial (no brand artwork)."""
    pm = QPixmap(size * 2, size * 2)
    pm.setDevicePixelRatio(2)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(ENGINE_COLORS.get(engine, "#888")))
    p.drawRoundedRect(0, 0, size, size, 6, 6)
    p.setPen(QColor("white"))
    f = p.font()
    f.setFamily(UI_FONT)
    f.setBold(True)
    f.setPixelSize(int(size * 0.55))
    p.setFont(f)
    p.drawText(0, 0, size, size, Qt.AlignCenter, (engine[:1] or "?").upper())
    p.end()
    return pm
