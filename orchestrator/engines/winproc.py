"""Keep engine subprocesses windowless on Windows.

When Orchestrator runs under pythonw.exe (Orchestrator.cmd, start at login) it has
no console, so every console child (claude.exe, its MCP servers, hooks...) would
get a *visible* console window of its own. Our own Popen calls pass
CREATE_NO_WINDOW; claude-agent-sdk spawns claude.exe through anyio.open_process
without it, so we default creationflags there. Children inherit the hidden console.
"""
from __future__ import annotations

import functools
import sys

CREATE_NO_WINDOW = 0x08000000
_installed = False


def install_no_window_spawn() -> bool:
    """Idempotently make anyio.open_process default to CREATE_NO_WINDOW on Windows."""
    global _installed
    if sys.platform != "win32" or _installed:
        return _installed
    import anyio

    original = anyio.open_process

    @functools.wraps(original)
    async def open_process(*args, **kwargs):
        if not kwargs.get("creationflags"):
            kwargs["creationflags"] = CREATE_NO_WINDOW
        return await original(*args, **kwargs)

    open_process.__orchestrator_patched__ = True  # type: ignore[attr-defined]
    anyio.open_process = open_process
    _installed = True
    return True
