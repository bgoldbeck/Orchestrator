"""Orchestrator tools as a local stdio MCP server.

Started by the engine (Claude Code or Codex) for an orchestrator session:

    pythonw -m orchestrator.mcp_server

It holds no state. Each tool call is forwarded over a local TCP connection
(127.0.0.1 only, per-run secret token) to the running app, which owns the
tasks. Env: ORCH_BRIDGE=host:port, ORCH_TOKEN, ORCH_ORCHESTRATION_ID,
ORCH_WAIT_LIMIT (max seconds one wait_for call may block, kept under the
engine's tool timeout).
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

WAIT_LIMIT_DEFAULT = 240.0


async def bridge_call(op: str, args: dict[str, Any], timeout: float | None = None) -> Any:
    host, _, port = os.environ.get("ORCH_BRIDGE", "127.0.0.1:0").rpartition(":")
    req = {"token": os.environ.get("ORCH_TOKEN", ""), "orch": int(os.environ.get("ORCH_ORCHESTRATION_ID", "0")),
           "op": op, "args": args}
    reader, writer = await asyncio.open_connection(host or "127.0.0.1", int(port))
    try:
        writer.write((json.dumps(req) + "\n").encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=timeout)
    finally:
        writer.close()
    if not line:
        raise RuntimeError("Orchestrator app closed the connection.")
    resp = json.loads(line)
    if not resp.get("ok"):
        raise RuntimeError(resp.get("error") or "Orchestrator error")
    return resp.get("result")


def _dump(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)


def wait_limit() -> float:
    try:
        return max(5.0, float(os.environ.get("ORCH_WAIT_LIMIT", WAIT_LIMIT_DEFAULT)))
    except ValueError:
        return WAIT_LIMIT_DEFAULT


def build_server():
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(
        "orchestrator",
        instructions="Tools to run sub-agents (Claude or Codex) in the user's project and collect their reports.",
    )

    @server.tool()
    async def delegate(instructions: str, engine: str = "", model: str = "", effort: str = "",
                       title: str = "", cwd: str = "", resume_session_id: str = "") -> str:
        """Start a sub-agent on a task. Returns a task_id right away; the task runs in the background.

        instructions: everything the sub-agent needs; it does not see this conversation.
        engine: "claude" or "codex" (empty = the project's default).
        model / effort: optional, e.g. model "sonnet", effort "low"; empty = defaults for that engine.
        title: short label for the task card (a few words).
        cwd: optional sub-folder of the project to work in.
        resume_session_id: an engine session id to continue instead of starting fresh.
        """
        return _dump(await bridge_call("delegate", {
            "instructions": instructions, "engine": engine, "model": model, "effort": effort,
            "title": title, "cwd": cwd, "resume_session_id": resume_session_id}, timeout=60))

    @server.tool()
    async def task_status(task_id: int = 0) -> str:
        """Status of one task (task_id) or of all tasks in this orchestration (task_id 0)."""
        return _dump(await bridge_call("status", {"task_id": task_id}, timeout=30))

    @server.tool()
    async def wait_for(task_ids: list[int], timeout_seconds: float = 600) -> str:
        """Wait until the given tasks finish, then return their reports.

        One call blocks for at most a few minutes. If tasks are still running when it
        returns, the result has "done": false and lists them under "pending"; call
        wait_for again with those ids to keep waiting.
        """
        limit = min(max(1.0, float(timeout_seconds)), wait_limit())
        res = await bridge_call("wait", {"task_ids": list(task_ids), "timeout": limit}, timeout=limit + 30)
        if not res.get("done"):
            res["note"] = ("Some tasks are still running. Call wait_for again with the pending ids "
                           "(or task_status to look without waiting).")
        return _dump(res)

    @server.tool()
    async def send_to_task(task_id: int, text: str) -> str:
        """Send a follow-up message to a finished sub-agent (continues its session). Returns right away;
        use wait_for to get its new report."""
        return _dump(await bridge_call("send", {"task_id": task_id, "text": text}, timeout=30))

    return server


def main() -> None:
    build_server().run("stdio")


if __name__ == "__main__":
    main()
