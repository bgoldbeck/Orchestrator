"""Codex engine: drives `codex exec --json` (see M0_NOTES.md)."""
from __future__ import annotations

import json
import re
import os
import shutil
import subprocess
import sys
from collections.abc import AsyncIterator
from typing import Any

from orchestrator.core.events import (
    Done,
    Error,
    Event,
    Started,
    Text,
    ToolCall,
    ToolResult,
    Usage,
)
from orchestrator.engines._threadbridge import ThreadBridge
from orchestrator.engines.base import AgentAdapter, TurnRequest

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _short(text: str, n: int = 90) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


_PS_WRAP = re.compile(r"""^\S*powershell(?:\.exe)?"?\s+(?:-\w+\s+)*-Command\s+(['"])(.*)\1\s*$""", re.I | re.S)


def unwrap_shell(cmd: str) -> str:
    """Show the inner command instead of the `powershell.exe -Command '...'` wrapper."""
    m = _PS_WRAP.match(cmd.strip())
    return m.group(2) if m else cmd


def _tool_from_item(item: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    """(name, summary, input) for a tool-like item."""
    t = item.get("type", "")
    if t == "command_execution":
        cmd = item.get("command", "")
        return "shell", _short(unwrap_shell(cmd)), {"command": cmd}
    if t == "file_change":
        changes = item.get("changes") or []
        paths = [f"{c.get('kind', 'update')} {c.get('path', '')}" for c in changes]
        return "edit", _short(", ".join(paths) or "files"), {"changes": changes}
    if t == "mcp_tool_call":
        name = f"{item.get('server', 'mcp')}.{item.get('tool', '?')}"
        args = item.get("arguments") or {}
        return name, _short(json.dumps(args)), {"arguments": args}
    if t == "web_search":
        q = item.get("query", "")
        return "web_search", _short(q), {"query": q}
    if t == "todo_list":
        items = item.get("items") or []
        done = sum(1 for i in items if i.get("completed"))
        return "plan", f"{done}/{len(items)} steps", {"items": items}
    return t or "tool", "", {k: v for k, v in item.items() if k not in ("id", "type")}


def _tool_output(item: dict[str, Any]) -> tuple[str, bool]:
    t = item.get("type", "")
    status = item.get("status")
    if t == "command_execution":
        code = item.get("exit_code")
        out = item.get("aggregated_output", "") or ""
        return out, (code not in (0, None)) or status == "failed"
    if t == "file_change":
        return "\n".join(f"{c.get('kind')}: {c.get('path')}" for c in item.get("changes") or []), status == "failed"
    if t == "mcp_tool_call":
        if item.get("error"):
            return str(item["error"].get("message", item["error"])), True
        res = item.get("result")
        return (json.dumps(res, indent=2) if res is not None else ""), status == "failed"
    if t == "todo_list":
        return "\n".join(("[x] " if i.get("completed") else "[ ] ") + i.get("text", "") for i in item.get("items") or []), False
    return "", status == "failed"


TOOL_ITEM_TYPES = {"command_execution", "file_change", "mcp_tool_call", "web_search", "todo_list"}


def parse_codex_line(line: str) -> list[Event]:
    """Translate one JSONL line from `codex exec --json` into events."""
    line = line.strip()
    if not line:
        return []
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        return []
    et = ev.get("type")
    item = ev.get("item") or {}
    it = item.get("type")

    if et == "thread.started":
        return [Started(session_id=ev.get("thread_id", ""))]
    if et == "turn.completed":
        u = ev.get("usage") or {}
        return [
            Usage(
                input_tokens=int(u.get("input_tokens", 0)),
                output_tokens=int(u.get("output_tokens", 0)) + int(u.get("reasoning_output_tokens", 0)),
                cached_tokens=int(u.get("cached_input_tokens", 0)),
            ),
            Done(),
        ]
    if et == "turn.failed":
        msg = (ev.get("error") or {}).get("message", "Codex turn failed")
        return [Error(message=msg), Done(is_error=True, result=msg)]
    if et == "error":
        return [Error(message=ev.get("message", "Codex error"))]

    if et in ("item.started", "item.updated", "item.completed") and item:
        iid = item.get("id", "")
        if it == "agent_message":
            return [Text(text=item.get("text", ""), delta=False)] if et == "item.completed" else []
        if it == "error":
            return [Error(message=item.get("message", ""), fatal=False, kind="warning")]
        if it in TOOL_ITEM_TYPES:
            name, summary, inp = _tool_from_item(item)
            if et == "item.started":
                return [ToolCall(id=iid, name=name, summary=summary, input=inp)]
            if et == "item.completed":
                out, is_err = _tool_output(item)
                # Some items (file_change) arrive only as completed: emit the call too.
                return [ToolCall(id=iid, name=name, summary=summary, input=inp),
                        ToolResult(id=iid, output=out, is_error=is_err, name=name)]
            return []
    return []


def codex_executable() -> str | None:
    return shutil.which("codex")


def _toml_str(s: str) -> str:
    """TOML literal string (no escapes, so Windows paths stay as they are)."""
    if "'" in s or "\n" in s:
        return json.dumps(s)
    return f"'{s}'"


def codex_mcp_overrides(mcp: dict[str, dict] | None) -> list[str]:
    """`-c` overrides that register local stdio MCP servers with auto-approved tools."""
    out: list[str] = []
    for name, c in (mcp or {}).items():
        key = f"mcp_servers.{name}"
        args = "[" + ",".join(_toml_str(a) for a in c.get("args", [])) + "]"
        env = "{" + ",".join(f"{k}={_toml_str(v)}" for k, v in c.get("env", {}).items()) + "}"
        out += ["-c", f"{key}.command={_toml_str(c['command'])}", "-c", f"{key}.args={args}",
                "-c", f"{key}.env={env}", "-c", f"{key}.default_tools_approval_mode='approve'"]
        if c.get("tool_timeout_sec"):
            out += ["-c", f"{key}.tool_timeout_sec={int(c['tool_timeout_sec'])}"]
    return out


def codex_prompt(req: TurnRequest) -> str:
    if not req.attachments:
        return req.prompt
    from orchestrator.core.attachments import prompt_text

    return prompt_text(req.prompt, req.attachments)


def build_codex_cmd(exe: str, req: TurnRequest) -> list[str]:
    common = ["--json", "--skip-git-repo-check", *codex_mcp_overrides(req.mcp)]
    for name in req.mcp_off:  # switched off in Orchestrator; config.toml is left as it is
        common += ["-c", f"mcp_servers.{name}.enabled=false"]
    if req.model:
        common += ["-m", req.model]
    if req.effort:
        common += ["-c", f'model_reasoning_effort="{req.effort}"']
    if req.compact_limit:
        common += ["-c", f"model_auto_compact_token_limit={int(req.compact_limit)}"]
    if req.resume_id:
        # `exec resume` has no -s/-C; sandbox via config, cwd via process cwd.
        return [exe, "exec", "resume", *common, "-c", f'sandbox_mode="{req.sandbox}"', req.resume_id, "-"]
    from orchestrator.core.attachments import is_image

    images = [f"--image={p}" for p in req.attachments if is_image(p)]  # new turns only; resume lists paths
    return [exe, "exec", *common, *images, "-s", req.sandbox, "-C", str(req.cwd), "-"]


class CodexAdapter(AgentAdapter):
    engine = "codex"
    label = "Codex"

    def __init__(self, approval_handler=None, env: dict[str, str] | None = None) -> None:
        super().__init__(approval_handler)
        self._proc: subprocess.Popen | None = None
        self._stopped = False
        self._env = env

    async def run(self, req: TurnRequest) -> AsyncIterator[Event]:
        exe = codex_executable()
        if not exe:
            yield Error("Codex CLI not found on PATH. Install it with `npm i -g @openai/codex`.", kind="auth")
            yield Done(is_error=True)
            return
        cmd = build_codex_cmd(exe, req)
        env = {**os.environ, **(self._env or {})}
        bridge = ThreadBridge()
        self._stopped = False

        def produce(b: ThreadBridge) -> None:
            proc = subprocess.Popen(
                cmd, cwd=str(req.cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                env=env, creationflags=CREATE_NO_WINDOW,
            )
            self._proc = proc
            assert proc.stdin and proc.stdout and proc.stderr
            proc.stdin.write(codex_prompt(req))
            proc.stdin.close()
            for line in proc.stdout:
                b.put(line)
            err = proc.stderr.read()
            b.put(("__exit__", proc.wait(), err))

        bridge.start(produce)
        got_done = False
        async for item in bridge.items():
            if isinstance(item, tuple) and item and item[0] == "__exit__":
                _, rc, err = item
                if self._stopped:
                    yield Error("Stopped.", fatal=False, kind="warning")
                    yield Done(is_error=True, result="stopped")
                elif rc != 0 and not got_done:
                    tail = "\n".join(err.strip().splitlines()[-4:]) or f"exit code {rc}"
                    yield Error(f"Codex exited with code {rc}: {tail}")
                    yield Done(is_error=True)
                elif not got_done:
                    yield Done()
                break
            for ev in parse_codex_line(item):
                if isinstance(ev, Done):
                    got_done = True
                yield ev
        self._proc = None

    async def interrupt(self) -> None:
        proc = self._proc
        if not proc or proc.poll() is not None:
            return
        self._stopped = True
        if sys.platform == "win32":
            # codex.cmd -> node -> codex.exe: kill the whole tree.
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, creationflags=CREATE_NO_WINDOW)
        else:
            proc.kill()
