"""Settings › Appearance: persisted look, applied live (fonts, sizes, density, theme, accent), preview."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def db(tmp_path, monkeypatch, qapp):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    from orchestrator.storage.db import Database
    from orchestrator.ui import appearance as A

    d = Database(tmp_path / "t.db")
    yield d
    A.apply(A.Look(), emit=False)  # leave defaults for other tests


def test_load_save_defaults(db):
    from orchestrator.ui import appearance as A

    look = A.load(db)
    assert look == A.Look()
    db.set_setting("ui.theme", "Weird")
    assert A.load(db).theme == "Dark"
    look.chat_size, look.density, look.accent = 13.0, "Compact", "#ff00aa"
    A.save(db, look)
    got = A.load(db)
    assert got.chat_size == 13.0 and got.density == "Compact" and got.accent == "#ff00aa"
    assert A.accent_hex("Violet") == "#a78bfa" and A.accent_hex("#ff00aa") == "#ff00aa"
    assert A.accent_hex("nonsense") == "#4cc2ff"


def test_apply_changes_fonts_theme_and_helpers(db, qapp):
    from qfluentwidgets import BodyLabel, isDarkTheme, themeColor

    from orchestrator.ui import appearance as A
    from orchestrator.ui import theme as T

    label = BodyLabel("hello")
    before = label.font().pixelSize()
    look = A.Look(ui_size=12.0, chat_size=13.0, code_font="Consolas", code_size=11.0, density="Compact",
                  theme="Light", accent="Emerald")
    hits = []
    A.hub.changed.connect(lambda: hits.append(1))
    A.apply(look)
    assert hits
    assert abs(qapp.font().pointSizeF() - 12.0) < 0.01
    assert label.font().pixelSize() == round(before * 1.2)  # existing Fluent widgets rescale
    assert BodyLabel("new").font().pixelSize() == round(14 * 1.2)  # new ones too
    assert T.chat_font().pointSizeF() == 13.0
    assert T.mono_css().startswith("Consolas") and T.code_pt() == "11pt"
    assert T.compact() and T.space(10) == 6
    assert not isDarkTheme() and themeColor().name() == "#34d399"
    from orchestrator.ui.markdown_render import code_html

    assert "font-family:Consolas" in code_html("x = 1", "python") and "font-size:11pt" in code_html("x", "")
    A.apply(A.Look())
    assert label.font().pixelSize() == before and isDarkTheme() and not T.compact()


def test_card_live_preview_and_persist(db, qapp):
    from orchestrator.ui import appearance as A
    from orchestrator.ui import theme as T
    from orchestrator.ui.appearance_card import AppearanceCard

    card = AppearanceCard(db)
    card.chatSize.setCurrentText("14 pt")
    qapp.processEvents()  # apply runs on the next event-loop turn
    assert A.load(db).chat_size == 14.0 and T.LOOK["chat_size"] == 14.0
    from PySide6.QtWidgets import QLabel

    bubble_fonts = [w.font().pointSizeF() for w in card.preview.findChildren(QLabel) if w.font().pointSizeF() > 0]
    assert 14.0 in bubble_fonts  # the preview was rebuilt with the new chat size
    card.density.setCurrentItem("Compact")
    card._set(density="Compact")
    qapp.processEvents()
    assert A.load(db).density == "Compact"
    card._set(accent="Rose")
    qapp.processEvents()
    assert "border:2px solid" in card.swatches["Rose"].styleSheet()
    card.reset()
    qapp.processEvents()
    assert A.load(db) == A.Look()


def test_settings_page_has_appearance(db, qapp):
    from orchestrator.ui.settings_page import SettingsPage

    page = SettingsPage(db)
    assert page.appearance.preview.objectName() == "appearancePreview"


def test_main_font_size_moves_chat_text():
    from orchestrator.ui import appearance as A

    look = A.Look()
    old = look.ui_size
    look.ui_size = 12.0
    A.follow_ui_size(look, old)
    assert look.chat_size == 12.0 and look.code_size == 11.0  # moved up ~2 pt, snapped to offered sizes
    look2 = A.Look(chat_size=14.0)
    look2.ui_size = 14.0
    A.follow_ui_size(look2, 10.0)
    assert look2.chat_size == 16.0  # clamps to the nearest offered size
