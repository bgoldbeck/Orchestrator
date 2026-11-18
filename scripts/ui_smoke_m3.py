"""M3 end-to-end check: a real every-minute Codex schedule fires from APScheduler,
runs unattended (read-only), records history, notifies via the tray; close-to-tray works.

    .venv\\Scripts\\python scripts\\ui_smoke_m3.py --shot docs/screenshots/m3.png

Throwaway ORCHESTRATOR_HOME and empty engine-log folders, so only demo data is shown.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SANDBOX = ROOT / "spikes" / "sandbox"


async def wait_until(pred, timeout: float, step: float = 0.25) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        await asyncio.sleep(step)
    return False


def seed_demo(db) -> None:
    web = db.add_project(r"D:\Demo\weather-app", "weather-app")
    api = db.add_project(r"D:\Demo\api-server", "api-server")
    demos = [
        dict(name="Morning repo digest", project_id=web.id, engine="claude", model="claude-sonnet-5-5", effort="medium",
             kind="cron", cron="0 8 * * mon-fri", prompt="Summarize yesterday's commits and open TODOs in 5 bullets.",
             permission="read-only", missed="run_once"),
        dict(name="Dependency check", project_id=api.id, engine="codex", model="gpt-6.2-sol", effort="medium",
             kind="cron", cron="0 9 * * mon", prompt="List outdated dependencies and any with known CVEs. Do not upgrade.",
             permission="read-only", missed="skip"),
        dict(name="Release notes draft", project_id=api.id, engine="claude", model="claude-opus-5-5", effort="high",
             kind="once", run_at=time.time() + 26 * 3600, prompt="Draft release notes for v2.4 from merged PRs.",
             permission="allow-edits", missed="run_once", enabled=True),
    ]
    for d in demos:
        sid = db.save_schedule({"new_or_resume": "new", "enabled": True, **d})
        for i, st in enumerate(["done", "done", "error"][: 2 if d["kind"] == "cron" else 0]):
            rid = db.add_run(sid, None)
            db.conn.execute("UPDATE runs SET started_at=?, finished_at=?, status=? WHERE id=?",
                            (time.time() - (i + 1) * 86400, time.time() - (i + 1) * 86400 + 42, st, rid))
        db.conn.commit()


async def scenario(win, db, mgr, runner, args) -> bool:
    proj = db.add_project(str(SANDBOX), "sandbox")
    seed_demo(db)
    for s in db.schedules():
        runner.sync(s)
    sid = db.save_schedule(dict(name="Heartbeat (every minute)", project_id=proj.id, engine="codex", model="gpt-6.2-sol",
                                effort="low", kind="cron", cron="* * * * *", new_or_resume="new",
                                prompt="Reply with the word TICK and nothing else.", permission="read-only",
                                missed="skip", enabled=True))
    runner.sync(db.schedule(sid))
    win.switchTo(win.schedules)
    win.schedules.reload()
    nxt = runner.next_run(sid)
    print("next run:", nxt)
    fired = await wait_until(lambda: bool(db.runs(sid)), 75)
    print("schedule fired from APScheduler:", fired)
    done = await wait_until(lambda: db.runs(sid) and db.runs(sid)[0].status != "running", 180)
    run = db.runs(sid)[0] if db.runs(sid) else None
    # Stop the heartbeat after one run so it does not keep spending tokens.
    db.save_schedule({"enabled": False}, sid)
    runner.sync(db.schedule(sid))
    reply = []
    if run and run.session_id:
        reply = [m["text"] for m in db.messages(run.session_id) if m["role"] == "assistant"]
    print("run:", run.status if run else None, "| reply:", reply, "| tray notifications:", win.tray.notified if win.tray else None)
    ok = bool(fired and done and run and run.status == "done" and reply and "TICK" in reply[-1].upper())
    ok = ok and bool(win.tray and any("Heartbeat" in t for t, _ in win.tray.notified))

    # Run once through the popover: custom time = the next minute boundary (+1 if too close).
    from datetime import datetime, timedelta

    from PySide6.QtCore import QDate, QTime

    from orchestrator.ui.run_once import open_run_once

    page = win.schedules
    fly = open_run_once(win, page.onceBtn, db, runner, {"project_id": proj.id})
    view = fly.view
    view.prompt.setPlainText("Reply with the word ONCE and nothing else.")
    t = (datetime.now() + timedelta(minutes=1)).replace(second=0, microsecond=0)
    if (t - datetime.now()).total_seconds() < 20:
        t += timedelta(minutes=1)
    view.customBtn.setChecked(True)
    view.date.setDate(QDate(t.year, t.month, t.day))
    view.time.setTime(QTime(t.hour, t.minute))
    db.set_setting("last", ["codex", "gpt-6.2-sol", "low"])
    view.pill.set_value("codex", "gpt-6.2-sol", "low", False)
    view._update()  # programmatic picker changes don't emit the user signals
    await asyncio.sleep(0.6)
    if args.shot:
        fly.grab().save(str(ROOT / args.shot.replace(".png", "-run-once.png")))
    view._schedule()
    once = max(s.id for s in db.schedules())
    print("run once scheduled for", t.strftime("%H:%M"))
    ran = await wait_until(lambda: db.runs(once) and db.runs(once)[0].status != "running", 200)
    o = db.schedule(once)
    orep = [m["text"] for m in db.messages(db.runs(once)[0].session_id) if m["role"] == "assistant"] if ran else []
    in_history = o.kind == "once" and not o.enabled and bool(db.runs(once))
    print("run once:", db.runs(once)[0].status if ran else None, orep, "| moved to history:", in_history)
    ok = ok and bool(ran and orep and "ONCE" in orep[-1].upper() and in_history)

    win.resize(1320, 1240)
    win.schedules.expanded.add(sid)
    win.schedules.reload()
    await asyncio.sleep(0.8)
    if args.shot:
        out = ROOT / args.shot
        out.parent.mkdir(parents=True, exist_ok=True)
        win.grab().save(str(out))
        print("screenshot:", out)

    # Close-to-tray: closing hides the window, app keeps running.
    win.close()
    await asyncio.sleep(0.3)
    hidden = not win.isVisible()
    print("close hides to tray:", hidden, "| app still running:", runner.scheduler.running)
    return ok and hidden and runner.scheduler.running


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shot", default="")
    args = ap.parse_args()
    home = Path(tempfile.mkdtemp(prefix="orch-m3-"))
    os.environ["ORCHESTRATOR_HOME"] = str(home)
    os.environ["ORCHESTRATOR_INDEX_CLAUDE"] = str(home / "no-claude-logs")
    os.environ["ORCHESTRATOR_INDEX_CODEX"] = str(home / "no-codex-logs")

    from orchestrator.core import auth

    auth.scrub_inherited_host_env()
    import qasync
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication
    from qfluentwidgets import Theme, setFontFamilies, setTheme, setThemeColor

    from orchestrator.app import _app_icon
    from orchestrator.core.scheduler import ScheduleRunner
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
    db.set_setting("ui.tray_hint_shown", True)
    mgr = SessionManager(db)
    runner = ScheduleRunner(db, mgr)
    win = MainWindow(db, mgr, _app_icon(), indexer=SessionIndexer(db), runner=runner)
    win.setMicaEffectEnabled(False)
    win.show()
    with loop:
        ok = loop.run_until_complete(scenario(win, db, mgr, runner, args))
        win.quit_app()
    print("RESULT:", "OK" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
