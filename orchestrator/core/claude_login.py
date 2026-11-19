"""Help the person sign in to the Claude Code CLI, which the account usage numbers need.

A `claude setup-token` token runs chats but can't read plan usage; the CLI's own browser sign-in can. We open a
terminal that runs `claude /login` and watch ~/.claude/.credentials.json until the sign-in lands there.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# A token in the environment makes the CLI skip its own sign-in, so the login terminal doesn't get them.
HIDE_ENV = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def credentials_path() -> Path:
    return Path.home() / ".claude" / ".credentials.json"


def has_cli_login(path: Path | None = None) -> bool:
    """The CLI has a browser sign-in saved (the kind that can read plan usage)."""
    try:
        data = json.loads((path or credentials_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool((data.get("claudeAiOauth") or {}).get("accessToken"))


def claude_cli() -> str | None:
    """The `claude` on PATH, else the one bundled with the Claude Agent SDK."""
    exe = shutil.which("claude")
    if exe:
        return exe
    try:
        import claude_agent_sdk

        base = Path(claude_agent_sdk.__file__).parent / "_bundled"
        for name in ("claude.exe", "claude"):
            if (base / name).is_file():
                return str(base / name)
    except Exception:  # noqa: BLE001 - SDK missing or unusual layout
        pass
    return None


def login_command(exe: str) -> list[str]:
    if sys.platform == "win32":
        # A new console that stays open, so the person sees the CLI and can finish in the browser.
        return ["cmd.exe", "/k", exe, "/login"]
    for term in ("x-terminal-emulator", "gnome-terminal", "konsole", "xterm"):
        if shutil.which(term):
            return [term, "-e", exe, "/login"]
    return [exe, "/login"]


def launch_login(popen=subprocess.Popen) -> str:
    """Open a terminal running `claude /login`. Returns "" on success, else what went wrong (plain words)."""
    exe = claude_cli()
    if not exe:
        return "Couldn't find the Claude Code CLI. Install it, then try again."
    env = {k: v for k, v in os.environ.items() if k not in HIDE_ENV}
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) if sys.platform == "win32" else 0
    try:
        popen(login_command(exe), env=env, creationflags=flags, cwd=str(Path.home()))
    except OSError as exc:
        return f"Couldn't open a terminal for the sign-in: {exc}"
    return ""
