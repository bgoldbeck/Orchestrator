"""Appearance settings: fonts, sizes, density, theme and accent. Persisted in settings; applied live."""
from __future__ import annotations

from dataclasses import asdict, dataclass

from PySide6.QtCore import QObject, Signal

from orchestrator.storage.db import Database
from orchestrator.ui import theme as T

# stored value -> label (Auto = follow Windows; Black = Dark on a near-black window, for OLED screens)
THEMES = {"Dark": "Dark", "Black": "Black", "Light": "Light", "Auto": "System"}
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
    theme: str = "Dark"  # Dark | Black | Light | Auto
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


OWN_FONTS = "ownFonts"  # widget property: this subtree sizes its fonts from the chat/code sizes itself
_PT = None


def _own_fonts(w) -> bool:
    while w is not None:
        if w.property(OWN_FONTS):
            return True
        w = w.parentWidget()
    return False


def scale_qss_pt(qss: str, ratio: float) -> str:
    """Scale every `font-size: Npt` in a style sheet by `ratio` (one decimal)."""
    import re

    global _PT
    _PT = _PT or re.compile(r"(font-size\s*:\s*)(\d+(?:\.\d+)?)pt")
    return _PT.sub(lambda m: f"{m.group(1)}{max(6.0, round(float(m.group(2)) * ratio, 1)):g}pt", qss)


def refont_in_place(root, old: tuple, new: tuple) -> None:
    """Resize chat text already on screen without rebuilding it. `old`/`new` are
    (chat_size, code_size, ui_font, code_font): chat-sized fonts move by the chat delta, code style sheets by the
    code delta."""
    import re

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QWidget

    (oc, ok, of, ocf), (nc, nk, nf, ncf) = old, new
    dc, dk = nc - oc, nk - ok
    code_pt = re.compile(r"(font-size\s*:\s*)(\d+(?:\.\d+)?)pt")
    for w in [root, *root.findChildren(QWidget)]:
        qss = w.styleSheet()
        if (dk or ocf != ncf) and "font-family" in qss and ocf in qss:
            qss = qss.replace(ocf, ncf)
            if dk:
                qss = code_pt.sub(lambda m: f"{m.group(1)}{max(6.0, float(m.group(2)) + dk):g}pt", qss)
            w.setStyleSheet(qss)
        if not w.testAttribute(Qt.WA_SetFont):
            continue
        f = w.font()
        if f.pointSizeF() <= 0 or not f.families() or f.families()[0] != of:
            continue
        if abs(f.pointSizeF() - oc) <= 2.5:  # chat_font(delta) text; small captions keep their size
            f.setPointSizeF(max(6.0, f.pointSizeF() + dc))
        f.setFamily(nf)
        w.setFont(f)
    if dk or ocf != ncf:  # code inside rendered Markdown carries its font in inline HTML styles
        from PySide6.QtWidgets import QLabel

        for lbl in root.findChildren(QLabel):
            html = lbl.text()
            if lbl.textFormat() == Qt.RichText and ocf in html:
                html = html.replace(ocf, ncf)
                if dk:
                    html = code_pt.sub(lambda m: f"{m.group(1)}{max(6.0, float(m.group(2)) + dk):g}pt", html)
                lbl.setText(html)


def _refont_widgets(ratio: float, old_family: str, new_family: str) -> None:
    """Widgets already built keep their fonts: rescale them (Fluent pixel fonts, our fixed point sizes and
    `font-size:…pt` style sheets) and swap the UI family, then re-render Fluent style sheets. Chat views size
    their text from the chat/code sizes and restyle themselves on hub.changed, so they are left alone here."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from qfluentwidgets.common.style_sheet import updateStyleSheet

    resize = abs(ratio - 1.0) > 1e-6
    refamily = old_family != new_family
    if not (resize or refamily):
        return
    for w in QApplication.allWidgets():
        own = _own_fonts(w)  # chat views: their point sizes follow the chat/code sizes, not the UI size
        if resize and not own and "pt" in (qss := w.styleSheet()) and "font-size" in qss:
            w.setStyleSheet(scale_qss_pt(qss, ratio))
        if not w.testAttribute(Qt.WA_SetFont):
            continue
        f = w.font()
        if own and f.pixelSize() <= 0:
            continue
        if resize:
            if f.pixelSize() > 0:
                f.setPixelSize(max(6, round(f.pixelSize() * ratio)))
            elif f.pointSizeF() > 0:
                f.setPointSizeF(max(6.0, round(f.pointSizeF() * ratio, 1)))
        if refamily and f.families() and f.families()[0] == old_family:
            f.setFamilies(T.ui_families())
        w.setFont(f)
    updateStyleSheet()


BLACK_BG = "#000000"
DARK_BG = "#202020"  # qfluentwidgets' own dark window colour


def style_window(win) -> None:
    """Black theme: a near-black window instead of Windows' Mica/dark grey. Other themes: Fluent's default."""
    from PySide6.QtGui import QColor

    if not hasattr(win, "setCustomBackgroundColor"):
        return
    black = T.LOOK.get("theme") == "Black"
    if black:
        if win.isMicaEffectEnabled():
            win.setMicaEffectEnabled(False)
            win.setProperty("blackMica", True)  # turn Mica back on when leaving Black
        win.setCustomBackgroundColor(QColor(243, 243, 243), QColor(BLACK_BG))
    else:
        win.setCustomBackgroundColor(QColor(243, 243, 243), QColor(DARK_BG))
        if win.property("blackMica"):
            win.setProperty("blackMica", False)
            win.setMicaEffectEnabled(True)
    win.update()


def apply(look: Look, emit: bool = True) -> None:
    """Make `look` current for the whole app (fonts, theme, accent) and tell open views to rebuild."""
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication
    from qfluentwidgets import Theme, setFontFamilies, setTheme, setThemeColor

    old_family = T.LOOK["font"]
    T.LOOK.update(font=look.font, ui_size=look.ui_size, chat_size=look.chat_size, code_font=look.code_font,
                  code_size=look.code_size, density=look.density, theme=look.theme)
    _install_scaling()
    old = SCALE["value"]
    SCALE["value"] = max(0.7, min(1.6, look.ui_size / 10.0))
    app = QApplication.instance()
    if app is not None:
        f = QFont(look.font)
        f.setPointSizeF(look.ui_size)
        app.setFont(f)
    setFontFamilies(T.ui_families())
    want = {"Dark": Theme.DARK, "Black": Theme.DARK, "Light": Theme.LIGHT, "Auto": Theme.AUTO}[look.theme]
    from qfluentwidgets import qconfig

    if qconfig.theme != want or look.theme == "Auto":
        setTheme(want, lazy=True)
    setThemeColor(accent_hex(look.accent))
    if app is not None:
        _refont_widgets(SCALE["value"] / old, old_family, look.font)
        for w in app.topLevelWidgets():
            style_window(w)
    if emit:
        hub.changed.emit()
