"""One Orchestrator per user and data folder.

The first instance holds a named mutex and listens on a local socket. A second launch (Orchestrator.cmd, a
shortcut, the exe, start at login) finds the mutex, sends its arguments to the first instance over the socket and
exits at once; the first instance restores its window (from the tray or minimized) and brings it to the front.

The lock is keyed on the user and the data folder (ORCHESTRATOR_HOME), so tests, demos and benchmarks with their
own data folder run side by side, and ``--mcp-server`` never takes it. ORCHESTRATOR_MULTI_INSTANCE=1 turns it off.
"""
from __future__ import annotations

import getpass
import hashlib
import json
import os
import sys
import time
from pathlib import Path

ERROR_ALREADY_EXISTS = 183
ASFW_ANY = -1


def instance_key(data_dir: Path | str | None = None) -> str:
    if data_dir is None:
        from orchestrator.storage.db import app_dir

        data_dir = app_dir()
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - no user name: still unique per data folder
        user = "user"
    norm = os.path.normcase(str(Path(data_dir).resolve()))
    digest = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:10]
    safe = "".join(c for c in user if c.isalnum() or c in "-_") or "user"
    return f"Orchestrator.App-{safe}-{digest}"


def disabled() -> bool:
    return os.environ.get("ORCHESTRATOR_MULTI_INSTANCE") == "1"


# ---------------------------------------------------------------- mutex (race guard)

class Mutex:
    """Named mutex: exists while its first owner runs; Windows releases it when that process ends (even on a
    crash), so a stale lock cannot outlive its process."""

    def __init__(self, key: str) -> None:
        self.key = key
        self.handle = None
        self.already_exists = False

    def acquire(self) -> bool:
        """True if we are the first instance."""
        if sys.platform != "win32":
            return self._acquire_file()
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        h = k32.CreateMutexW(None, False, "Local\\" + self.key)
        err = ctypes.get_last_error()
        self.handle = h
        self.already_exists = err == ERROR_ALREADY_EXISTS
        return bool(h) and not self.already_exists

    def _acquire_file(self) -> bool:  # pragma: no cover - non-Windows fallback
        import fcntl
        import tempfile

        f = open(Path(tempfile.gettempdir()) / f"{self.key}.lock", "w")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            self.already_exists = True
            return False
        self.handle = f
        return True

    def release(self) -> None:
        if self.handle is None:
            return
        if sys.platform == "win32":
            import ctypes

            ctypes.windll.kernel32.CloseHandle(self.handle)
        else:  # pragma: no cover
            self.handle.close()
        self.handle = None


# ---------------------------------------------------------------- second instance: forward and exit

def allow_foreground() -> None:
    """Let the running instance take the foreground (Windows only lets the active process hand it over)."""
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.AllowSetForegroundWindow(ASFW_ANY)
        except (AttributeError, OSError):
            pass


def forward(key: str, args: list[str], timeout: float = 8.0) -> bool:
    """Send `args` to the running instance. Retries while it is still starting up. True when it answered."""
    from PySide6.QtNetwork import QLocalSocket

    allow_foreground()
    msg = (json.dumps({"args": args, "cwd": os.getcwd()}) + "\n").encode("utf-8")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sock = QLocalSocket()
        sock.connectToServer(key)
        if sock.waitForConnected(500):
            sock.write(msg)
            sock.flush()
            sock.waitForBytesWritten(1000)
            ok = sock.waitForReadyRead(3000) and bytes(sock.readAll()).startswith(b"ok")
            sock.disconnectFromServer()
            if ok:
                return True
        time.sleep(0.15)  # first instance not listening yet (still starting)
    return False


# ---------------------------------------------------------------- first instance: listen

def listen(key: str, on_message, parent=None):
    """QLocalServer for `key`; `on_message(args: list[str])` runs on the GUI thread. Recovers a stale socket
    (a crashed instance's leftover) by removing it and listening again."""
    from PySide6.QtNetwork import QLocalServer

    server = QLocalServer(parent)
    server.setSocketOptions(QLocalServer.UserAccessOption)
    if not server.listen(key):
        QLocalServer.removeServer(key)  # stale socket from a crash
        if not server.listen(key):
            return None

    def accept() -> None:
        while server.hasPendingConnections():
            sock = server.nextPendingConnection()

            def read(s=sock) -> None:
                if not s.canReadLine():
                    return
                try:
                    data = json.loads(bytes(s.readLine()).decode("utf-8") or "{}")
                except ValueError:
                    data = {}
                s.write(b"ok\n")
                s.flush()
                s.disconnectFromServer()
                on_message(list(data.get("args") or []))

            sock.readyRead.connect(read)
            sock.disconnected.connect(sock.deleteLater)
            read()

    server.newConnection.connect(accept)
    return server


def bring_window_to_front(win) -> None:
    """Restore from the tray or minimized and really take the foreground on Windows."""
    if win.isMinimized():
        win.showNormal()
    else:
        win.show()
    win.raise_()
    win.activateWindow()
    if sys.platform == "win32":
        try:
            import ctypes

            hwnd = int(win.winId())
            user32 = ctypes.windll.user32
            SW_RESTORE = 9
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, SW_RESTORE)
            user32.SetForegroundWindow(hwnd)
        except (AttributeError, OSError, ValueError):
            pass
