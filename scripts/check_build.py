"""Smoke-test the built exe: --mcp-server answers MCP over stdio, and the GUI starts and stays up.

    .venv\\Scripts\\python scripts\\check_build.py
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "dist" / "Orchestrator" / "Orchestrator.exe"


ORCH_TOOLS = ["ask_user", "delegate", "find_projects", "find_sessions", "send_to_task", "suggest_preference",
              "task_status", "wait_for"]
APP_TOOLS = ["about_app", "cancel_schedule", "create_schedule", "get_usage", "list_models", "list_preferences",
             "list_schedules", "list_settings", "set_appearance", "set_default_model", "set_model_preference",
             "set_setting"]


async def mcp_check(server: str = "") -> list[str]:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    env = {**os.environ, "ORCH_BRIDGE": "127.0.0.1:9", "ORCH_TOKEN": "x"}
    if server:
        env["ORCH_SERVER"] = server
    params = StdioServerParameters(command=str(EXE), args=["--mcp-server"], env=env)
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await asyncio.wait_for(s.initialize(), 60)
            tools = await s.list_tools()
            return sorted(t.name for t in tools.tools)


def gui_check() -> bool:
    home = tempfile.mkdtemp(prefix="orch-build-")
    env = {**os.environ, "ORCHESTRATOR_HOME": home, "ORCHESTRATOR_INDEX_CLAUDE": home + "\\none",
           "ORCHESTRATOR_INDEX_CODEX": home + "\\none"}
    p = subprocess.Popen([str(EXE)], env=env)
    time.sleep(12)
    alive = p.poll() is None
    db_made = (Path(home) / "orchestrator.db").exists()
    subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True)
    print(f"gui: alive after 12s={alive}, database created={db_made}")
    return alive and db_made


def main() -> int:
    if not EXE.exists():
        print("missing", EXE)
        return 1
    size = sum(f.stat().st_size for f in EXE.parent.rglob("*") if f.is_file())
    print(f"exe: {EXE} (folder {size / 1e6:.0f} MB)")
    names = asyncio.run(mcp_check())
    print("mcp tools:", names)
    app_names = asyncio.run(mcp_check("app"))
    print("app tools:", app_names)
    ok = names == ORCH_TOOLS and app_names == APP_TOOLS
    ok = gui_check() and ok
    print("BUILD CHECK", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
