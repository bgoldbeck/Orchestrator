"""M2 end-to-end check: import + resume + parallel chats, in the real window.

    .venv\\Scripts\\python scripts\\ui_smoke_m2.py real      # index real ~/.claude + ~/.codex (prints counts only),
                                                           # resume an imported Codex chat while a 2nd chat runs
    .venv\\Scripts\\python scripts\\ui_smoke_m2.py demo --shot docs/screenshots/m2.png
                                                           # demo session logs (no personal data) + live chat, screenshot

Uses a throwaway ORCHESTRATOR_HOME; engine logs are only read.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FIX = ROOT / "tests" / "fixtures"
SANDBOX = ROOT / "spikes" / "sandbox"


def build_demo_homes(base: Path) -> tuple[Path, Path]:
    """Synthetic engine logs built from the test fixtures, spread over a few demo projects."""
    now = time.time()
    claude_root = base / "claude" / "projects"
    codex_home = base / "codex"
    demos = [
        ("claude", r"D:\Demo\weather-app", "Forecast retry with backoff", 2 * 3600),
        ("claude", r"D:\Demo\weather-app", "Unit tests for parser", 26 * 3600),
        ("codex", r"D:\Demo\weather-app", "Bump httpx and fix types", 4 * 86400),
        ("codex", r"D:\Demo\api-server", "Health check returns 500", 40 * 60),
        ("claude", r"D:\Demo\api-server", "Add rate limiting middleware", 3 * 86400),
        ("codex", r"D:\Demo\api-server", "Dockerfile multi-stage build", 9 * 86400),
        ("claude", r"D:\Demo\design-system", "Dark mode tokens", 15 * 86400),
    ]
    index_lines = []
    for n, (engine, cwd, title, ago) in enumerate(demos):
        ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now - ago))
        sid = f"0000{n:04d}-1111-4222-8333-444455556666"
        if engine == "claude":
            text = (FIX / "claude_session.jsonl").read_text(encoding="utf-8")
            text = text.replace("11111111-2222-4333-8444-555555555555", sid).replace("Forecast retry", title)
            text = text.replace("D:\\\\Demo\\\\weather-app", cwd.replace("\\", "\\\\"))
            lines = []
            for line in text.splitlines():
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "timestamp" in d:
                    d["timestamp"] = ts
                lines.append(json.dumps(d))
            d = claude_root / ("D--Demo-" + Path(cwd).name)
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{sid}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        else:
            text = (FIX / "codex_rollout_items.jsonl").read_text(encoding="utf-8")
            text = text.replace("01a1ffff-0000-7000-8000-000000000001", sid)
            text = text.replace("D:\\\\Demo\\\\api-server", cwd.replace("\\", "\\\\"))
            lines = []
            for line in text.splitlines():
                d = json.loads(line)
                d["timestamp"] = ts
                lines.append(json.dumps(d))
            day = codex_home / "sessions" / "2026" / "11" / "12"
            day.mkdir(parents=True, exist_ok=True)
            (day / f"rollout-2026-11-12T00-00-00-{sid}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
            index_lines.append(json.dumps({"id": sid, "thread_name": title, "updated_at": ts}))
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "session_index.jsonl").write_text("\n".join(index_lines) + "\n", encoding="utf-8")
    return claude_root, codex_home


async def wait_until(pred, timeout: float, step: float = 0.2) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        await asyncio.sleep(step)
    return False


async def wait_status(mgr, sid: int, timeout: float = 240) -> str:
    await wait_until(lambda: not mgr.is_running(sid), timeout)
    return mgr.db.session(sid).status


def ask(chat, text: str) -> None:
    chat.prompt.edit.setPlainText(text)
    chat.prompt._submit()


async def real(win, db, mgr, idx, args) -> bool:
    side, chat = win.chats.sidebar, win.chats.chat
    proj = db.add_project(str(SANDBOX), "sandbox")
    side.reload()
    await wait_until(lambda: any(s.source == "imported" for p in db.projects() for s in db.sessions(p.id)), 60)
    await asyncio.sleep(1)
    allsess = [s for p in db.projects() for s in db.sessions(p.id)]
    imp = [s for s in allsess if s.source == "imported"]
    print(f"indexed: {sum(s.engine == 'claude' for s in imp)} claude, {sum(s.engine == 'codex' for s in imp)} codex, "
          f"{sum(p.auto for p in db.projects())} discovered folders")
    target = next((s for s in db.sessions(proj.id) if s.source == "imported" and s.engine == "codex"), None)
    if not target:
        print("no imported codex session in sandbox; run scripts/ui_smoke.py first")
        return False
    first_id = target.engine_session_id
    side.select(target.id)
    await asyncio.sleep(0.5)
    hist_widgets = chat.stream.count() - 1
    print("opened imported codex chat, history widgets:", hist_widgets)

    # Two chats at once: a new one + resuming the imported one.
    side.new_chat(proj.id)
    new_sid = side.current_sid
    db.update_session(new_sid, engine="codex", model="gpt-6.2-sol", effort="low")
    chat.show_session(new_sid)
    ask(chat, "Reply with the word PONG and nothing else.")
    side.select(target.id)
    await asyncio.sleep(0.3)
    db.update_session(target.id, effort="low")
    ask(chat, "What exact word did you reply with in your first answer of this conversation? One word.")
    both = await wait_until(lambda: mgr.is_running(new_sid) and mgr.is_running(target.id), 5, 0.05)
    print("both chats running at once:", both)
    side.select(new_sid)  # switch while streaming
    await asyncio.sleep(1.5)
    side.select(target.id)
    st1, st2 = await wait_status(mgr, new_sid), await wait_status(mgr, target.id)
    new_texts = [m["text"] for m in db.messages(new_sid) if m["role"] == "assistant"]
    res_texts = [m["text"] for m in db.messages(target.id) if m["role"] == "assistant"]
    same = db.session(target.id).engine_session_id == first_id
    print("new chat:", st1, new_texts, "| resumed:", st2, res_texts, "| same thread id:", same)
    return (both and st1 == st2 == "done" and same and new_texts and "PONG" in new_texts[-1].upper()
            and res_texts and "PING" in res_texts[-1].upper())


async def demo(win, db, mgr, idx, args) -> bool:
    side, chat = win.chats.sidebar, win.chats.chat
    db.add_project(r"D:\Demo\weather-app", "weather-app")
    db.add_project(r"D:\Demo\api-server", "api-server")
    proj = db.add_project(str(SANDBOX), "sandbox")
    side.reload()
    await wait_until(lambda: sum(len(db.sessions(p.id)) for p in db.projects()) >= 7, 30)
    await asyncio.sleep(0.5)
    side.new_chat(proj.id)
    live_sid = side.current_sid
    db.update_session(live_sid, engine="codex", model="gpt-6.2-sol", effort="low")
    chat.show_session(live_sid)
    ask(chat, "In two short sentences, what is a good name for a weather app? Then stop.")
    await asyncio.sleep(0.5)
    api = next(p for p in db.projects() if p.path.endswith("api-server"))
    target = next(s for s in db.sessions(api.id) if s.title.startswith("Health"))
    side.select(target.id)
    await asyncio.sleep(2.0)
    ok = mgr.is_running(live_sid)
    if args.shot:
        out = ROOT / args.shot
        out.parent.mkdir(parents=True, exist_ok=True)
        win.grab().save(str(out))
        print("screenshot:", out, "| other chat running during shot:", ok)
    await wait_status(mgr, live_sid)
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["real", "demo"])
    ap.add_argument("--shot", default="")
    args = ap.parse_args()
    os.environ["ORCHESTRATOR_HOME"] = tempfile.mkdtemp(prefix="orch-m2-")
    if args.mode == "demo":
        base = Path(tempfile.mkdtemp(prefix="orch-demo-logs-"))
        c, x = build_demo_homes(base)
        os.environ["ORCHESTRATOR_INDEX_CLAUDE"] = str(c)
        os.environ["ORCHESTRATOR_INDEX_CODEX"] = str(x)

    from orchestrator.core import auth

    auth.scrub_inherited_host_env()
    import qasync
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication
    from qfluentwidgets import Theme, setFontFamilies, setTheme, setThemeColor

    from orchestrator.app import _app_icon
    from orchestrator.core.session_index import SessionIndexer
    from orchestrator.core.session_manager import SessionManager
    from orchestrator.storage.db import Database
    from orchestrator.ui.main_window import MainWindow
    from orchestrator.ui.theme import ACCENTS, UI_FONT, UI_FONTS

    app = QApplication(sys.argv)
    app.setFont(QFont(UI_FONT, 10))
    setFontFamilies(UI_FONTS)
    setTheme(Theme.DARK)
    setThemeColor(ACCENTS["Sky"])
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    db = Database()
    mgr = SessionManager(db)
    idx = SessionIndexer(db)
    win = MainWindow(db, mgr, _app_icon(), indexer=idx)
    win.setMicaEffectEnabled(False)
    win.show()
    fn = real if args.mode == "real" else demo
    with loop:
        ok = loop.run_until_complete(fn(win, db, mgr, idx, args))
    print("RESULT:", "OK" if ok else "FAIL")
    if args.mode == "demo":
        shutil.rmtree(Path(os.environ["ORCHESTRATOR_INDEX_CLAUDE"]).parents[1], ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
