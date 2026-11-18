"""Guards: nothing Orchestrator starts may pop a console window or a bare Python REPL."""
import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "orchestrator"
ALLOWED_VISIBLE = {"settings_page.py"}  # 'Open login terminal' intentionally opens a console


def subprocess_calls():
    for path in ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess" \
                    and node.func.attr in ("Popen", "run", "call", "check_output", "check_call"):
                yield path, node


def test_every_subprocess_call_sets_creationflags():
    missing = [f"{p.name}:{n.lineno}" for p, n in subprocess_calls()
               if not any(k.arg == "creationflags" for k in n.keywords)]
    assert not missing, f"subprocess calls without creationflags: {missing}"


def test_no_subprocess_launches_bare_python():
    """sys.executable with no script would open an interactive REPL."""
    for path in ROOT.rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("Popen", "run"):
                first = node.args[0] if node.args else None
                if isinstance(first, ast.List) and len(first.elts) == 1:
                    seg = ast.get_source_segment(src, first.elts[0]) or ""
                    assert "executable" not in seg, f"{path.name}:{node.lineno} starts a bare interpreter"


def test_autostart_command_runs_script_windowless():
    from orchestrator.core.autostart import launch_command

    cmd = launch_command()
    assert "run.py" in cmd and cmd.endswith("--tray")
    if sys.platform == "win32":
        assert "pythonw" in cmd.lower()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_anyio_open_process_defaults_to_no_window(monkeypatch):
    import asyncio

    import anyio

    from orchestrator.engines import winproc

    seen = {}

    async def fake(*args, **kwargs):
        seen.update(kwargs)
        return "proc"

    monkeypatch.setattr(anyio, "open_process", fake)
    monkeypatch.setattr(winproc, "_installed", False)
    assert winproc.install_no_window_spawn()
    assert asyncio.run(anyio.open_process(["claude.exe"], stdin=-1)) == "proc"
    assert seen["creationflags"] == winproc.CREATE_NO_WINDOW
    seen.clear()
    asyncio.run(anyio.open_process(["x"], creationflags=0x10))  # explicit flags win
    assert seen["creationflags"] == 0x10
