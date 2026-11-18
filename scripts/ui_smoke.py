"""End-to-end UI smoke test: drives the real window, runs a real engine chat
(new turn + resume), then saves a screenshot.

    .venv\\Scripts\\python scripts\\ui_smoke.py [--engine codex|claude] [--shot docs/screenshots/m1.png]

Uses a throwaway data folder (ORCHESTRATOR_HOME) so your real chats are untouched.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def wait_status(mgr, sid: int, timeout: float = 240) -> str:
    fut = asyncio.get_running_loop().create_future()

    def on(s: int, status: str) -> None:
        if s == sid and status in ("done", "error", "idle") and not fut.done():
            fut.set_result(status)

    mgr.status_changed.connect(on)
    try:
        return await asyncio.wait_for(fut, timeout)
    finally:
        mgr.status_changed.disconnect(on)


async def scenario(app, win, db, mgr, args) -> int:
    from PySide6.QtWidgets import QApplication

    sandbox = ROOT / "spikes" / "sandbox"
    sandbox.mkdir(parents=True, exist_ok=True)
    proj = db.add_project(str(sandbox), "sandbox")
    model, effort = db.default_model(args.engine)
    effort = "low"
    if args.engine == "claude":
        model = "claude-haiku-5-5"
    s = db.create_session(proj.id, args.engine, model, effort)
    side, chat = win.chats.sidebar, win.chats.chat
    side.reload()
    side.select(s.id)
    await asyncio.sleep(0.5)

    def ask(text: str) -> None:
        chat.prompt.edit.setPlainText(text)
        chat.prompt._submit()

    ok = True
    ask("Reply with the word PING and nothing else.")
    st = await wait_status(mgr, s.id)
    first = db.session(s.id).engine_session_id
    print("turn1:", st, "engine_session_id:", first)
    if args.engine == "claude":
        msgs = db.messages(s.id)
        print("messages:", [(m["kind"], m.get("text", "")[:80]) for m in msgs])
    else:
        ask("What word did you just say? Answer with one word.")
        st2 = await wait_status(mgr, s.id)
        second = db.session(s.id).engine_session_id
        texts = [m["text"] for m in db.messages(s.id) if m["kind"] == "text" and m["role"] == "assistant"]
        print("turn2:", st2, "same session:", first == second, "assistant texts:", texts)
        ok = st == st2 == "done" and first == second and len(texts) >= 2 and "PING" in texts[-1].upper()
        if args.rich:
            ask("Run a shell command to list the files in this folder, then reply with a one-line summary "
                "and a tiny Python hello-world function in a fenced code block.")
            st3 = await wait_status(mgr, s.id)
            print("turn3:", st3)
    await asyncio.sleep(1.0)
    if args.shot:
        out = ROOT / args.shot
        out.parent.mkdir(parents=True, exist_ok=True)
        win.grab().save(str(out))
        print("screenshot:", out)
        if args.settings_shot:
            win.switchTo(win.settings)
            await asyncio.sleep(2.5)  # let version probe finish
            so = ROOT / args.settings_shot
            win.grab().save(str(so))
            print("screenshot:", so)
    print("RESULT:", "OK" if ok else "FAIL")
    QApplication.instance().quit()
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default="codex")
    ap.add_argument("--shot", default="")
    ap.add_argument("--rich", action="store_true")
    ap.add_argument("--settings-shot", default="")
    args = ap.parse_args()
    os.environ.setdefault("ORCHESTRATOR_HOME", tempfile.mkdtemp(prefix="orch-smoke-"))

    from orchestrator.core import auth

    auth.scrub_inherited_host_env()
    import qasync
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication
    from qfluentwidgets import Theme, setFontFamilies, setTheme, setThemeColor

    from orchestrator.app import _app_icon
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
    win = MainWindow(db, mgr, _app_icon())
    win.setMicaEffectEnabled(False)  # grab() can't capture Mica; use solid dark background
    win.show()
    with loop:
        rc = loop.run_until_complete(scenario(app, win, db, mgr, args))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
