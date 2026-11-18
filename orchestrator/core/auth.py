"""Engine credentials. Secrets live in the OS keyring only; nothing secret is
written to SQLite or logs. UI only ever sees masked values."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

SERVICE = "Orchestrator"
CLAUDE_TOKEN = "CLAUDE_CODE_OAUTH_TOKEN"
ANTHROPIC_KEY = "ANTHROPIC_API_KEY"
OPENAI_KEY = "OPENAI_API_KEY"
SECRET_NAMES = (CLAUDE_TOKEN, ANTHROPIC_KEY, OPENAI_KEY)


def _keyring():
    try:
        import keyring

        return keyring
    except Exception:
        return None


def get_secret(name: str) -> str | None:
    kr = _keyring()
    if not kr:
        return None
    try:
        return kr.get_password(SERVICE, name) or None
    except Exception:
        return None


def set_secret(name: str, value: str) -> None:
    kr = _keyring()
    if not kr:
        raise RuntimeError("keyring is not available")
    kr.set_password(SERVICE, name, value.strip())


def delete_secret(name: str) -> None:
    kr = _keyring()
    if not kr:
        return
    try:
        kr.delete_password(SERVICE, name)
    except Exception:
        pass


def _user_env_registry(name: str) -> str | None:
    """HKCU\\Environment value (set by `setx`), for processes started before it."""
    if sys.platform != "win32" or os.environ.get("ORCHESTRATOR_NO_USER_ENV"):
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            val, _ = winreg.QueryValueEx(k, name)
            return str(val) or None
    except OSError:
        return None


def resolve(name: str) -> tuple[str | None, str]:
    """Return (value, source). Order: keyring, process env, HKCU\\Environment."""
    v = get_secret(name)
    if v:
        return v, "keyring"
    v = os.environ.get(name)
    if v:
        return v, "environment"
    v = _user_env_registry(name)
    if v:
        return v, "user environment"
    return None, ""


def mask(value: str | None) -> str:
    if not value:
        return ""
    return "••••" + value[-4:] if len(value) > 8 else "••••"


@dataclass
class EngineStatus:
    ok: bool
    summary: str
    detail: str = ""
    env: dict[str, str] = field(default_factory=dict, repr=False)


def _claude_cli_logged_in() -> bool:
    p = Path.home() / ".claude" / ".credentials.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return bool((data.get("claudeAiOauth") or {}).get("accessToken"))
    except Exception:
        return False


def claude_status() -> EngineStatus:
    tok, src = resolve(CLAUDE_TOKEN)
    if tok:
        return EngineStatus(True, f"OAuth token {mask(tok)}", f"from {src}", {CLAUDE_TOKEN: tok})
    key, src = resolve(ANTHROPIC_KEY)
    if key:
        return EngineStatus(True, f"API key {mask(key)}", f"from {src}", {ANTHROPIC_KEY: key})
    if _claude_cli_logged_in():
        return EngineStatus(True, "Signed in via Claude Code CLI", "~/.claude/.credentials.json")
    return EngineStatus(False, "Not logged in", "Run `claude setup-token` and paste the token here.")


def codex_status() -> EngineStatus:
    key, src = resolve(OPENAI_KEY)
    if key:
        return EngineStatus(True, f"API key {mask(key)}", f"from {src}", {OPENAI_KEY: key})
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    if (home / "auth.json").exists():
        return EngineStatus(True, "Signed in via Codex CLI", str(home / "auth.json").replace(str(Path.home()), "~"))
    return EngineStatus(False, "Not signed in", "Run `codex login` in a terminal.")


def scrub_inherited_host_env() -> None:
    """If launched from inside a Claude Code session, drop its host-only vars so
    child engines use the user's own login instead of the parent's proxy."""
    if not os.environ.get("CLAUDECODE"):
        return
    for k in list(os.environ):
        if k == CLAUDE_TOKEN:
            continue
        if k.startswith("CLAUDE_CODE_") or k in ("CLAUDECODE", "ANTHROPIC_BASE_URL", "CLAUDE_AGENT_SDK_VERSION"):
            os.environ.pop(k, None)
