"""Fast startup: shell window first, the rest after the first paint; heavy imports stay out of the way."""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from orchestrator import startup  # noqa: E402
from orchestrator.storage.db import Database  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ORCHESTRATOR_INDEX_CLAUDE", str(tmp_path / "none"))
    monkeypatch.setenv("ORCHESTRATOR_INDEX_CODEX", str(tmp_path / "none"))
    return tmp_path


def fake_runner(db, mgr):
    from orchestrator.core.scheduler import ScheduleRunner

    class FakeRunner(ScheduleRunner):  # real runner, but never starts APScheduler
        started = 0

        def start(self):
            self.started += 1
            self.scheduler = object()

        def shutdown(self):
            pass

    return FakeRunner(db, mgr)


def test_lazy_window_builds_shell_only_then_finishes(qapp, env, monkeypatch):
    from orchestrator.core.session_manager import SessionManager
    from orchestrator.ui import main_window
    from orchestrator.ui.main_window import MainWindow

    monkeypatch.setattr(main_window.QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: False))
    db = Database(env / "t.db")
    db.add_project(str(env), "cached-project")
    mgr = SessionManager(db)
    runner = fake_runner(db, mgr)
    t0 = time.perf_counter()
    win = MainWindow(db, mgr, runner=runner, lazy=True)
    shell_s = time.perf_counter() - t0
    # shell: Projects home already shows the cached project; nothing else built or started
    assert not win.chats.built and not win.orchestrate.built and not win.settings.built
    assert not win.schedules.built and runner.started == 0
    assert win.om is None and win.searchPalette is None
    assert win.projects.db.projects()[0].name == "cached-project"
    assert shell_s < 5  # generous: a sanity bound, the real numbers come from scripts/startup_bench.py

    win.finish_startup(sync=True)
    assert win.chats.built and win.orchestrate.built and win.settings.built and win.schedules.built
    assert runner.started == 1 and win.om is not None and win.summarizer is not None and win.startup_done
    keys = {sc.key().toString() for sc in win.shortcuts}
    assert {"Ctrl+K", "Ctrl+1"} <= keys
    win.finish_startup(sync=True)  # idempotent
    assert runner.started == 1
    win.om.bridge.stop()


def test_lazy_page_builds_on_first_use(qapp, env):
    from orchestrator.core.session_manager import SessionManager
    from orchestrator.ui.main_window import MainWindow

    db = Database(env / "t.db")
    win = MainWindow(db, SessionManager(db), lazy=True)
    assert not win.chats.built
    win.chats.sidebar.reload()  # attribute access builds the real page
    assert win.chats.built and win.chats.content.sidebar is win.chats.sidebar
    win.switchTo(win.settings)
    win.settings.show()
    assert win.settings.built


def test_projects_loading_indicator_and_skeleton(qapp, env):
    from orchestrator.ui.projects_page import ProjectsPage

    db = Database(env / "t.db")
    page = ProjectsPage(db)
    page.set_loading(True)
    assert page.loading and not page.loadingRow.isHidden()
    from PySide6.QtWidgets import QFrame

    cards = lambda: [f for f in page.findChildren(QFrame, "skeletonCard") if not f.parent().isHidden()]  # noqa: E731
    assert len(cards()) == 4  # placeholder cards while nothing is known yet
    page.set_loading(False)
    assert page.loadingRow.isHidden()
    qapp.processEvents()
    from PySide6.QtCore import QCoreApplication, QEvent

    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert not cards()  # back to the 'No projects yet' empty state


def test_ready_needs_paint_sessions_and_deferred(monkeypatch):
    seen = []
    monkeypatch.setattr(startup, "_seen", set())
    monkeypatch.setattr(startup, "_ready_parts", set())
    monkeypatch.setattr(startup, "on_ready", lambda: seen.append("ready"))
    startup.part_done("first_paint")
    startup.part_done("sessions")
    assert not seen
    startup.part_done("deferred")
    assert seen == ["ready"]


def test_qfluentwidgets_import_skips_scipy():
    code = ("import sys\n"
            "from orchestrator.lazy_imports import defer_qfw_image_utils\n"
            "defer_qfw_image_utils()\n"
            "import qfluentwidgets\n"
            "assert 'scipy' not in sys.modules and 'numpy' not in sys.modules, 'heavy import at startup'\n"
            "from qfluentwidgets.components.widgets import acrylic_label\n"
            "assert acrylic_label.isAcrylicAvailable\n"
            "from orchestrator.lazy_imports import _load_real_image_utils\n"
            "assert hasattr(_load_real_image_utils(), 'DominantColor') and 'scipy' in sys.modules\n"
            "print('ok')\n")
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120,
                       env={**os.environ, "QT_QPA_PLATFORM": "offscreen"}, creationflags=0x08000000)
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr[-2000:]
