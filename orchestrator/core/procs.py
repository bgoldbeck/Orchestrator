"""Child processes of this app (engine CLIs, their MCP servers): list and end them on Quit."""
from __future__ import annotations

import os
import subprocess
import sys


def process_table() -> list[tuple[int, int, str]]:
    """(pid, parent pid, exe name) for every process (Windows Toolhelp snapshot; empty elsewhere)."""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == wintypes.HANDLE(-1).value:
        return []
    out = []
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    try:
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile))
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return out


def descendants(pid: int | None = None) -> list[tuple[int, str]]:
    """Every process started (directly or not) by `pid` (default: this process), deepest last."""
    pid = pid or os.getpid()
    table = process_table()
    kids: dict[int, list[tuple[int, str]]] = {}
    for p, pp, name in table:
        if p != pp:
            kids.setdefault(pp, []).append((p, name))
    out, todo, seen = [], [pid], {pid}
    while todo:
        cur = todo.pop()
        for child, name in kids.get(cur, []):
            if child not in seen:
                seen.add(child)
                out.append((child, name))
                todo.append(child)
    return out


def kill_children(pid: int | None = None) -> list[int]:
    """End every process this app started (engine CLIs and their MCP servers). Returns the pids ended."""
    pid = pid or os.getpid()
    direct = [(p, n) for p, pp, n in process_table() if pp == pid and p != pid]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    ended = []
    for child, _name in direct:
        r = subprocess.run(["taskkill", "/PID", str(child), "/T", "/F"], capture_output=True,
                           creationflags=flags)
        if r.returncode == 0:
            ended.append(child)
    return ended
