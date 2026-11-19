"""Appearance settings: fonts, sizes, density, theme and accent. Persisted in settings; applied live."""
from __future__ import annotations

from dataclasses import asdict, dataclass

from PySide6.QtCore import QObject, Signal

from orchestrator.storage.db import Database
from orchestrator.ui import theme as T

THEMES = {"Dark": "Dark", "Light": "Light", "Auto": "System"}  # stored value -> label (Auto = follow Windows)
DENSITIES = ["Comfortable", "Compact"]
UI_FONT_CHOICES = ["Segoe UI Variable Text", "Segoe UI", "Inter", "Calibri", "Arial", "Verdana", "Tahoma",
                   "Bahnschrift"]
CODE_FONT_CHOICES = ["Cascadia Code", "Cascadia Mono", "Consolas", "JetBrains Mono", "Fira Code", "Source Code Pro",
                     "Courier New", "Lucida Console"]
UI_SIZES = [9.0, 9.5, 10.0, 10.5, 11.0, 12.0, 13.0, 14.0]
CHAT_SIZES = [9.0, 9.5, 10.0, 10.5, 11.0, 11.5, 12.0, 13.0, 14.0, 16.0]
CODE_SIZES = [8.0, 8.5, 9.0, 9.5, 10.0, 10.5, 11.0, 12.0, 13.0, 14.0]


@dataclass
class Look:
    font: str = T.UI_FONT
    ui_size: float = 10.0
    chat_size: float = 10.5
    code_font: str = "Cascadia Code"
    code_size: float = 9.5
    density: str = "Comfortable"
    theme: str = "Dark"  # Dark | Light | Auto
    accent: str = "Sky"  # a preset name or "#rrggbb"


KEYS = {"font": "ui.font_family", "ui_size": "ui.font_size", "chat_size": "ui.chat_font_size",
        "code_font": "ui.code_font", "code_size": "ui.code_font_size", "density": "ui.density",
        "theme": "ui.theme", "accent": "ui.accent"}


class _Hub(QObject):
    changed = Signal()  # the look changed: rebuild what shows fonts/colors


hub = _Hub()


def load(db: Database) -> Look:
    d = Look()
    vals = {}
    for attr, key in KEYS.items():
        v = db.get_setting(key)
        if v is None:
            continue
        default = getattr(d, attr)
        try:
            vals[attr] = float(v) if isinstance(default, float) else str(v)
        except (TypeError, ValueError):
            continue
    look = Look(**{**asdict(d), **vals})
    if look.theme not in THEMES:
        look.theme = "Dark"
    if look.density not in DENSITIES:
        look.density = "Comfortable"
    return look


def _nearest(choices: list[float], v: float) -> float:
    return min(choices, key=lambda c: abs(c - v))


def follow_ui_size(look: Look, old_ui: float) -> None:
    """The main font size moved: move chat and code text by the same amount (their offsets stay)."""
    d = look.ui_size - old_ui
    if abs(d) < 1e-6:
        return
    look.chat_size = _nearest(CHAT_SIZES, look.chat_size + d)
    look.code_size = _nearest(CODE_SIZES, look.code_size + d)


def save(db: Database, look: Look) -> None:
    for attr, key in KEYS.items():
        db.set_setting(key, getattr(look, attr))


def accent_hex(accent: str) -> str:
    return T.ACCENTS.get(accent, accent if accent.startswith("#") and len(accent) in (4, 7) else T.ACCENTS["Sky"])


def installed(choices: list[str], current: str) -> list[str]:
    """Choices that are installed (plus the current one), in order."""
    from PySide6.QtGui import QFontDatabase

    fams = set(QFontDatabase.families())
    out = [c for c in choices if c in fams]
    if current and current not in out:
        out.insert(0, current)
    return out or choices[:1]


# ---- UI font size for Fluent widgets
# qfluentwidgets uses fixed pixel sizes (getFont(14), "font: 14px" in its style sheets). We scale both by
# ui_size / 10 (10 pt is the default), so the "UI font size" setting reaches every widget.
SCALE = {"value": 1.0}
_installed = {"done": False}


def scale() -> float:
    return SCALE["value"]


def _install_scaling() -> None:
    if _installed["done"]:
        return
    _installed["done"] = True
    import re

    from PySide6.QtGui import QFont
    from qfluentwidgets.common import font as qf_font
    from qfluentwidgets.common import style_sheet as qf_ss

    class ScaledFont(QFont):
        def setPixelSize(self, px: int) -> None:  # noqa: N802
            super().setPixelSize(max(6, round(px * SCALE["value"])))

    qf_font.QFont = ScaledFont  # getFont()/setFont() build their fonts through this name
    px = re.compile(r"(font(?:-size)?\s*:[^;{}]*?)(\d+(?:\.\d+)?)px")
    original = qf_ss.renderQss

    def render(qss: str) -> str:
        out = original(qss)
        if SCALE["value"] == 1.0:
            return out
        return px.sub(lambda m: f"{m.group(1)}{max(6, round(float(m.group(2)) * SCALE['value']))}px", out)

    qf_ss.renderQss = render


def _refont_widgets(ratio: float, old_family: str, new_family: str) -> None:
    """Widgets already built keep their fonts: rescale the Fluent (pixel-sized) ones and swap the UI family,
    then re-render Fluent style sheets."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from qfluentwidgets.common.style_sheet import updateStyleSheet

    resize = abs(ratio - 1.0) > 1e-6
    refamily = old_family != new_family
    if not (resize or refamily):
        return
    for w in QApplication.allWidgets():
        if not w.testAttribute(Qt.WA_SetFont):
            continue
        f = w.font()
        if f.pixelSize() <= 0:
            continue  # point-sized fonts are ours (chat, code); those views rebuild on hub.changed
        if resize:
            f.setPixelSize(max(6, round(f.pixelSize() * ratio)))
        if refamily and f.families() and f.families()[0] == old_family:
            f.setFamilies(T.ui_families())
        w.setFont(f)
    updateStyleSheet()


def apply(look: Look, emit: bool = True) -> None:
    """Make `look` current for the whole app (fonts, theme, accent) and tell open views to rebuild."""
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication
    from qfluentwidgets import Theme, setFontFamilies, setTheme, setThemeColor

    old_family = T.LOOK["font"]
    T.LOOK.update(font=look.font, ui_size=look.ui_size, chat_size=look.chat_size, code_font=look.code_font,
                  code_size=look.code_size, density=look.density)
    _install_scaling()
    old = SCALE["value"]
    SCALE["value"] = max(0.7, min(1.6, look.ui_size / 10.0))
    app = QApplication.instance()
    if app is not None:
        f = QFont(look.font)
        f.setPointSizeF(look.ui_size)
        app.setFont(f)
    setFontFamilies(T.ui_families())
    want = {"Dark": Theme.DARK, "Light": Theme.LIGHT, "Auto": Theme.AUTO}[look.theme]
    from qfluentwidgets import qconfig

    if qconfig.theme != want or look.theme == "Auto":
        setTheme(want, lazy=True)
    setThemeColor(accent_hex(look.accent))
    if app is not None:
        _refont_widgets(SCALE["value"] / old, old_family, look.font)
    if emit:
        hub.changed.emit()
