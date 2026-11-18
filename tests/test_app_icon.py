"""App icon: rendered sizes, multi-size .ico, taskbar identity, shortcuts with icon + AppUserModelID."""
import os
import struct
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_icon_files_and_qicon(qapp):
    from orchestrator.ui import app_icon as A

    for s in A.SIZES:
        assert A.icon_file(s).is_file(), s
    icon = A.app_icon()
    assert not icon.isNull()
    sizes = {(z.width(), z.height()) for z in icon.availableSizes()}
    assert {(16, 16), (32, 32), (256, 256)} <= sizes
    pm = icon.pixmap(16, 16)
    img = pm.toImage()
    assert img.pixelColor(8, 8).alpha() > 0  # not blank at 16 px


def test_ico_has_every_size():
    from orchestrator.ui import app_icon as A

    data = A.ico_file().read_bytes()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind) == (0, 1) and count >= 7
    dims = []
    for i in range(count):
        w, h = struct.unpack("<BB", data[6 + 16 * i: 8 + 16 * i])
        dims.append(256 if w == 0 else w)
    assert {16, 24, 32, 48, 64, 128, 256} <= set(dims)


def test_main_window_uses_app_icon(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    from orchestrator.app import _app_icon
    from orchestrator.core.session_manager import SessionManager
    from orchestrator.storage.db import Database
    from orchestrator.ui.main_window import MainWindow

    db = Database(tmp_path / "t.db")
    win = MainWindow(db, SessionManager(db), _app_icon())
    assert not win.windowIcon().isNull()
    assert {(z.width(), z.height()) for z in win.windowIcon().availableSizes()} >= {(16, 16), (32, 32)}
    win.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shortcuts")
def test_shortcut_has_icon_and_app_id(tmp_path):
    from orchestrator.ui import app_icon as A

    path = A.create_shortcut(tmp_path / "Orchestrator.lnk")
    info = A.read_shortcut(path)
    assert info["app_id"] == A.APP_ID
    assert info["target"].lower().endswith(("pythonw.exe", "python.exe", "orchestrator.exe"))
    assert "run.py" in info["args"] or getattr(sys, "frozen", False)
    assert info["icon"].lower().endswith(".ico")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows taskbar identity")
def test_app_user_model_id():
    import ctypes

    from orchestrator.ui import app_icon as A

    assert A.set_app_user_model_id()
    buf = ctypes.c_wchar_p()
    ctypes.windll.shell32.GetCurrentProcessExplicitAppUserModelID(ctypes.byref(buf))
    assert buf.value == A.APP_ID
