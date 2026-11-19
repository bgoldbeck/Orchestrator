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

# Current look (Settings › Appearance). orchestrator.ui.appearance fills this in at startup and on every change;
# widgets read it through the helpers below when they are built.
LOOK: dict = {"font": UI_FONT, "ui_size": 10.0, "chat_size": 10.5, "code_font": "Cascadia Code", "code_size": 9.5,
              "density": "Comfortable", "theme": "Dark"}


def ui_families() -> list[str]:
    fam = LOOK["font"]
    return [fam] + [f for f in UI_FONTS if f != fam]


def chat_font(delta: float = 0.0):
    from PySide6.QtGui import QFont

    f = QFont(LOOK["font"])
    f.setPointSizeF(max(6.0, float(LOOK["chat_size"]) + delta))
    return f


def mono_css() -> str:
    """CSS font-family list for code, starting with the chosen code font."""
    fam = LOOK["code_font"]
    rest = [x.strip() for x in MONO_FONT.split(",") if x.strip() != fam]
    return ", ".join([fam, *rest])


def ui_ptf(base: float) -> float:
    """A fixed point size designed at the default 10 pt UI font, scaled with Settings › Font size."""
    return max(6.0, round(base * float(LOOK["ui_size"]) / 10.0, 1))


def ui_pt(base: float) -> str:
    return f"{ui_ptf(base):g}pt"


def code_pt(delta: float = 0.0) -> str:
    return f"{max(6.0, float(LOOK['code_size']) + delta):g}pt"


def compact() -> bool:
    return LOOK["density"] == "Compact"


def space(n: int) -> int:
    """Spacing/padding scaled for the density setting."""
    return max(1, round(n * 0.6)) if compact() else n


def c(dark: str, light: str) -> str:
    return dark if isDarkTheme() else light


def black() -> bool:
    """The Black (OLED) theme is on: Dark, with near-black surfaces."""
    return LOOK.get("theme") == "Black" and isDarkTheme()


BLACK = {"card": "rgba(255,255,255,0.035)", "card_hover": "rgba(255,255,255,0.06)", "code_bg": "#0a0b0d",
         "code_head": "#121418", "user_bubble": "rgba(255,255,255,0.06)", "sidebar": "rgba(0,0,0,0.0)",
         "border": "rgba(255,255,255,0.07)"}


def palette() -> dict[str, str]:
    p = _palette()
    if black():
        p.update(BLACK)
    return p


def _palette() -> dict[str, str]:
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
        "user_bubble": c("rgba(255,255,255,0.065)", "rgba(0,0,0,0.045)"),
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
