"""Quit really ends the app: confirm when agents run, child processes ended, hard exit fallback."""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
win_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows process tree")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@win_only
def test_kill_children_ends_the_whole_tree():
    code = ("import sys, subprocess, time; sys.path.insert(0, sys.argv[1]);"
            "from orchestrator.core import procs;"
            "c = subprocess.Popen([sys.executable, '-c', 'import subprocess,sys,time;"
            "subprocess.Popen([sys.executable, \"-c\", \"import time; time.sleep(60)\"]); time.sleep(60)']);"
            "time.sleep(1.5); kids = procs.descendants(); print(len(kids), flush=True);"
            "procs.kill_children(); time.sleep(0.5); print(len(procs.descendants()), flush=True)")
    out = subprocess.run([PY, "-c", code, str(ROOT)], capture_output=True, text=True, timeout=60).stdout.split()
    assert int(out[0]) >= 2  # child + grandchild (venv launchers can add one more level)
    assert out[1] == "0"


def test_quit_asks_when_tasks_run(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    from qfluentwidgets import MessageBox

    from orchestrator.core.session_manager import SessionManager
    from orchestrator.storage.db import Database
    from orchestrator.ui.main_window import MainWindow

    db = Database(tmp_path / "t.db")
    win = MainWindow(db, SessionManager(db))
    monkeypatch.setattr(win, "running_turns", lambda: [1, 2])
    asked = []
    monkeypatch.setattr(MessageBox, "exec", lambda self: (asked.append(self.contentLabel.text()), 0)[1])
    closed = []
    monkeypatch.setattr(win, "close", lambda: closed.append(1))
    win.quit_app()
    assert asked and "2 tasks are still running" in asked[0] and not closed and not win._quitting
    monkeypatch.setattr(MessageBox, "exec", lambda self: 1)
    win.quit_app()
    assert closed and win._quitting
    closed.clear()
    win._quitting = False
    monkeypatch.setattr(win, "running_turns", lambda: [])
    asked.clear()
    win.quit_app()  # nothing running: no question
    assert closed and not asked


@win_only
def test_quit_ends_process_within_5s(tmp_path):
    from orchestrator.core import procs

    home = tmp_path / "home"
    env = {**os.environ, "ORCHESTRATOR_HOME": str(home), "ORCHESTRATOR_INDEX_CLAUDE": str(home / "none"),
           "ORCHESTRATOR_INDEX_CODEX": str(home / "none"), "ORCHESTRATOR_NO_USER_ENV": "1",
           "QT_QPA_PLATFORM": "offscreen"}
    env.pop("ORCHESTRATOR_MULTI_INSTANCE", None)
    first = subprocess.Popen([PY, "run.py", "--tray"], cwd=ROOT, env=env)
    try:
        time.sleep(6)
        before = {p for p, _ in procs.descendants(first.pid)}
        t = time.monotonic()
        subprocess.run([PY, "run.py", "--quit"], cwd=ROOT, env=env, timeout=30)
        first.wait(timeout=10)
        assert time.monotonic() - t < 6
        time.sleep(0.5)
        alive = {p for p, _, _ in procs.process_table()}
        assert not (before & alive), "child processes left behind"
    finally:
        if first.poll() is None:
            subprocess.run(["taskkill", "/PID", str(first.pid), "/T", "/F"], capture_output=True)


def test_exit_watchdog_forces_exit(tmp_path):
    """A stuck non-daemon thread would keep Python alive forever; the watchdog ends it and logs the threads."""
    code = ("import os, sys, threading, time; sys.path.insert(0, sys.argv[1]);"
            "os.environ['ORCHESTRATOR_HOME'] = sys.argv[2]; os.environ['ORCHESTRATOR_EXIT_TIMEOUT'] = '1';"
            "threading.Thread(target=lambda: time.sleep(60), name='stuck').start();"
            "from orchestrator.app import _exit_watchdog; _exit_watchdog()")
    t = time.monotonic()
    r = subprocess.run([PY, "-c", code, str(ROOT), str(tmp_path / "h")], timeout=30)
    assert r.returncode == 0 and time.monotonic() - t < 15
    log = (tmp_path / "h" / "logs" / "quit-hang.txt").read_text(encoding="utf-8")
    assert "stuck" in log
