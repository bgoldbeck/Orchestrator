"""MCP servers for Claude Code and Codex: read their configs, add/remove through each CLI, and switch servers
off for Orchestrator's runs (everywhere, or in one chat).

Where they live:
  Claude Code  ~/.claude.json  "mcpServers" (scope user) and "projects"[<folder>]["mcpServers"] (scope local).
               Plugin servers (from the claude.ai account) are only known from the last chat's init frame.
  Codex        ~/.codex/config.toml  [mcp_servers.<name>]

Adding and removing go through `claude mcp ...` / `codex mcp ...` so each tool keeps its own file format.
Switching a server off never edits those files: Orchestrator hides it from its own runs only
(Claude: its tools are disallowed; Codex: `-c mcp_servers.<name>.enabled=false`).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator.storage.db import Database

APP_SERVERS = {"orchestrator", "app"}  # Orchestrator's own servers: always on, not listed as removable
ENGINES = ("claude", "codex")
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


@dataclass
class McpServer:
    engine: str
    name: str
    scope: str  # user | local | project | plugin
    kind: str = "stdio"  # stdio | http | sse
    command: str = ""
    args: list[str] = field(default_factory=list)
    url: str = ""
    env_keys: list[str] = field(default_factory=list)
    project: str = ""  # folder, for scope local
    status: str = ""  # last seen by a Claude chat: connected | failed | needs-auth | ...

    @property
    def key(self) -> str:
        return f"{self.engine}:{self.name}"

    @property
    def removable(self) -> bool:
        return self.scope in ("user", "local") and self.name not in APP_SERVERS

    def target(self) -> str:
        if self.url:
            return self.url
        return " ".join([self.command, *self.args]).strip()


def claude_json_path() -> Path:
    return Path(os.environ.get("ORCHESTRATOR_CLAUDE_JSON") or Path.home() / ".claude.json")


def codex_config_path() -> Path:
    return Path(os.environ.get("ORCHESTRATOR_CODEX_CONFIG") or Path.home() / ".codex" / "config.toml")


def _server(engine: str, name: str, scope: str, d: dict, project: str = "") -> McpServer:
    url = str(d.get("url") or "")
    kind = str(d.get("type") or ("http" if url else "stdio"))
    return McpServer(engine, name, scope, kind, str(d.get("command") or ""), [str(a) for a in d.get("args") or []],
                     url, sorted((d.get("env") or {}).keys()), project)


def claude_servers(path: Path | None = None) -> list[McpServer]:
    p = path or claude_json_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = [_server("claude", n, "user", d) for n, d in (data.get("mcpServers") or {}).items() if isinstance(d, dict)]
    for folder, proj in (data.get("projects") or {}).items():
        for n, d in ((proj or {}).get("mcpServers") or {}).items():
            if isinstance(d, dict):
                out.append(_server("claude", n, "local", d, folder))
    return out


def codex_servers(path: Path | None = None) -> list[McpServer]:
    p = path or codex_config_path()
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [_server("codex", n, "user", d) for n, d in (data.get("mcp_servers") or {}).items() if isinstance(d, dict)]


def list_servers(db: Database | None = None) -> list[McpServer]:
    """Every MCP server either engine knows, plus plugin servers seen by the last Claude chat."""
    out = claude_servers() + codex_servers()
    last = (db.get_setting("context.last.claude") or {}) if db is not None else {}
    seen = {m["server"]: m for m in last.get("mcp") or []}
    names = {s.name for s in out if s.engine == "claude"}
    for s in out:
        if s.engine == "claude" and s.name in seen:
            s.status = seen[s.name].get("status", "")
    for name, m in seen.items():
        if name not in names and name not in APP_SERVERS:
            out.append(McpServer("claude", name, "plugin" if m.get("source") == "plugin" else (m.get("source") or "plugin"),
                                 status=m.get("status", "")))
    return sorted(out, key=lambda s: (ENGINES.index(s.engine), s.scope != "user", s.name.lower()))


# ------------------------------------------------------------------ on / off for Orchestrator's runs
def off_everywhere(db: Database) -> set[str]:
    return set(db.get_setting("mcp.off", []) or [])


def off_in_chat(db: Database, sid: int) -> set[str]:
    return set(db.get_setting(f"chat.mcp_off.{sid}", []) or [])


def set_off(db: Database, key: str, off: bool, sid: int | None = None) -> None:
    name = f"chat.mcp_off.{sid}" if sid else "mcp.off"
    cur = set(db.get_setting(name, []) or [])
    cur = (cur | {key}) if off else (cur - {key})
    db.set_setting(name, sorted(cur))


def off_for_run(db: Database, engine: str, sid: int) -> list[str]:
    """Server names to hide from this chat's next turn."""
    keys = off_everywhere(db) | off_in_chat(db, sid)
    return sorted(k.split(":", 1)[1] for k in keys if k.startswith(engine + ":") and k.split(":", 1)[1] not in APP_SERVERS)


# ------------------------------------------------------------------ add / remove through the CLIs
def claude_cli() -> str | None:
    exe = shutil.which("claude")
    if exe:
        return exe
    try:
        import claude_agent_sdk

        name = "claude.exe" if sys.platform == "win32" else "claude"
        p = Path(claude_agent_sdk.__file__).parent / "_bundled" / name
        return str(p) if p.exists() else None
    except ImportError:
        return None


def parse_env(text: str) -> dict[str, str]:
    env = {}
    for line in (text or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            if k.strip():
                env[k.strip()] = v.strip()
    return env


def add_cmd(engine: str, exe: str, name: str, command: str = "", args: list[str] | None = None, url: str = "",
            env: dict[str, str] | None = None) -> list[str]:
    """CLI command that adds a user-scope server (pure, testable)."""
    env = env or {}
    if engine == "claude":
        cmd = [exe, "mcp", "add", "--scope", "user"]
        for k, v in env.items():
            cmd += ["-e", f"{k}={v}"]
        if url:
            return cmd + ["--transport", "http", name, url]
        return cmd + [name, "--", command, *(args or [])]
    cmd = [exe, "mcp", "add", name]
    for k, v in env.items():
        cmd += ["--env", f"{k}={v}"]
    if url:
        return cmd + ["--url", url]
    return cmd + ["--", command, *(args or [])]


def remove_cmd(server: McpServer, exe: str) -> list[str]:
    if server.engine == "claude":
        return [exe, "mcp", "remove", server.name, "--scope", "local" if server.scope == "local" else "user"]
    return [exe, "mcp", "remove", server.name]


def _run(cmd: list[str], cwd: str | None = None) -> tuple[bool, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, cwd=cwd or None,
                           creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    out = (r.stdout + "\n" + r.stderr).strip()
    return r.returncode == 0, out


def _exe(engine: str) -> str | None:
    return claude_cli() if engine == "claude" else shutil.which("codex")


def add_server(engine: str, name: str, command: str = "", args: list[str] | None = None, url: str = "",
               env: dict[str, str] | None = None) -> tuple[bool, str]:
    exe = _exe(engine)
    if not exe:
        return False, f"{'Claude Code' if engine == 'claude' else 'Codex'} CLI not found."
    if not name or not (command or url):
        return False, "A name and a command or URL are needed."
    return _run(add_cmd(engine, exe, name, command, args, url, env))


def remove_server(server: McpServer) -> tuple[bool, str]:
    if not server.removable:
        return False, "This server comes from a plugin or from Orchestrator itself; turn it off instead."
    exe = _exe(server.engine)
    if not exe:
        return False, "CLI not found."
    return _run(remove_cmd(server, exe), server.project if server.scope == "local" else None)
