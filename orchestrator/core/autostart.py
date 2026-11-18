"""Start-at-login via HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run (off by default)."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Protocol

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE = "Orchestrator"


class Backend(Protocol):
    def get(self, key: str, name: str) -> str | None: ...
    def set(self, key: str, name: str, value: str) -> None: ...
    def delete(self, key: str, name: str) -> None: ...


class WinRegBackend:
    def get(self, key: str, name: str) -> str | None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                return str(winreg.QueryValueEx(k, name)[0])
        except OSError:
            return None

    def set(self, key: str, name: str, value: str) -> None:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)

    def delete(self, key: str, name: str) -> None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, name)
        except OSError:
            pass


def launch_command() -> str:
    """pythonw (no console) + run.py --tray."""
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    runner = Path(__file__).resolve().parents[2] / "run.py"
    return f'"{pyw if pyw.exists() else exe}" "{runner}" --tray'


def is_enabled(backend: Backend | None = None) -> bool:
    return (backend or WinRegBackend()).get(RUN_KEY, VALUE) is not None


def set_enabled(on: bool, backend: Backend | None = None, command: str | None = None) -> None:
    b = backend or WinRegBackend()
    if on:
        b.set(RUN_KEY, VALUE, command or launch_command())
    else:
        b.delete(RUN_KEY, VALUE)
