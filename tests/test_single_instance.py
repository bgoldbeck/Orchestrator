"""Single instance: one app per user + data folder; a second launch forwards its args and exits; stale locks
recover; --mcp-server and other data folders are never blocked."""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from orchestrator import single_instance as SI  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
win_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows named mutex")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_key_per_data_folder(tmp_path):
    a = SI.instance_key(tmp_path / "a")
    assert a == SI.instance_key(str(tmp_path / "a"))
    assert a != SI.instance_key(tmp_path / "b")
    assert a.startswith("Orchestrator.App-")
    if sys.platform == "win32":
        assert a == SI.instance_key(str(tmp_path / "A"))  # same folder, other case


@win_only
def test_mutex_and_stale_lock_after_crash(tmp_path):
    key = SI.instance_key(tmp_path / "m")
    holder = subprocess.Popen([PY, "-c", "import sys,time; sys.path.insert(0, sys.argv[1]);"
                               "from orchestrator.single_instance import Mutex; m=Mutex(sys.argv[2]);"
                               "print(m.acquire(), flush=True); time.sleep(60)", str(ROOT), key],
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "True"
        second = SI.Mutex(key)
        assert second.acquire() is False and second.already_exists
        second.release()
    finally:
        holder.kill()  # a crash: no clean release
        holder.wait()
    time.sleep(0.2)
    again = SI.Mutex(key)
    assert again.acquire() is True  # Windows released the dead owner's mutex
    again.release()


def test_forward_reaches_running_instance(qapp, tmp_path):
    key = SI.instance_key(tmp_path / "f")
    got = []
    server = SI.listen(key, got.append)
    assert server is not None
    client = subprocess.Popen([PY, "-c", "import sys; sys.path.insert(0, sys.argv[1]);"
                               "from PySide6.QtCore import QCoreApplication; a=QCoreApplication([]);"
                               "from orchestrator.single_instance import forward;"
                               "print(forward(sys.argv[2], ['--open', 'x'], timeout=8), flush=True)", str(ROOT), key],
                              stdout=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 15
    while not got and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.02)
    out, _ = client.communicate(timeout=10)
    assert got == [["--open", "x"]]
    assert out.strip() == "True" and client.returncode == 0
    server.close()


def test_forward_gives_up_when_nobody_listens(qapp, tmp_path):
    t = time.monotonic()
    assert SI.forward(SI.instance_key(tmp_path / "nobody"), [], timeout=1.0) is False
    assert time.monotonic() - t < 3


def test_listen_recovers_stale_socket(qapp, tmp_path):
    from PySide6.QtNetwork import QLocalServer

    key = SI.instance_key(tmp_path / "s")
    leftover = QLocalServer()
    leftover.listen(key)  # stands in for a crashed instance's socket
    server = SI.listen(key, lambda a: None)
    assert server is not None and server.isListening()
    server.close()
    leftover.close()


def test_bring_window_to_front_restores(qapp):
    from PySide6.QtWidgets import QWidget

    w = QWidget()
    w.showMinimized()
    qapp.processEvents()
    SI.bring_window_to_front(w)
    qapp.processEvents()
    assert w.isVisible() and not w.isMinimized()
    w.hide()
    SI.bring_window_to_front(w)  # hidden in the tray
    assert w.isVisible()
    w.close()


@win_only
def test_second_launch_exits_and_mcp_server_is_not_blocked(tmp_path):
    """Real launches with their own data folder: the second one hands over and exits; --mcp-server keeps running."""
    home = tmp_path / "home"
    env = {**os.environ, "ORCHESTRATOR_HOME": str(home), "ORCHESTRATOR_INDEX_CLAUDE": str(home / "none"),
           "ORCHESTRATOR_INDEX_CODEX": str(home / "none"), "ORCHESTRATOR_NO_USER_ENV": "1"}
    env.pop("QT_QPA_PLATFORM", None)
    env.pop("ORCHESTRATOR_MULTI_INSTANCE", None)
    first = subprocess.Popen([PY, "run.py", "--tray"], cwd=ROOT, env={**env, "QT_QPA_PLATFORM": "offscreen"})
    procs = [first]
    try:
        key = SI.instance_key(home)
        from PySide6.QtCore import QCoreApplication
        from PySide6.QtNetwork import QLocalSocket

        QCoreApplication.instance() or QCoreApplication([])
        deadline = time.monotonic() + 30
        listening = False
        while time.monotonic() < deadline and not listening:
            s = QLocalSocket()
            s.connectToServer(key)
            listening = s.waitForConnected(300)
            s.abort()
            time.sleep(0.2)
        assert listening, "first instance never started listening"
        t = time.monotonic()
        second = subprocess.run([PY, "run.py"], cwd=ROOT, env={**env, "QT_QPA_PLATFORM": "offscreen"}, timeout=30)
        assert second.returncode == 0 and time.monotonic() - t < 15
        assert first.poll() is None  # first one still running
        mcp = subprocess.Popen([PY, "run.py", "--mcp-server"], cwd=ROOT, env=env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        procs.append(mcp)
        time.sleep(3)
        assert mcp.poll() is None  # serving stdio, not blocked by the running app
    finally:
        for p in procs:
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True)
