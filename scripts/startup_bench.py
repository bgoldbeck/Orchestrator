"""Startup benchmark: time to first painted window and to fully ready (sessions refreshed).

    .venv\\Scripts\\python scripts\\startup_bench.py [--target cmd|exe|both] [--runs 5] [--importtime]

Uses a private copy of your Orchestrator database (schedules switched off in the copy, so nothing
runs) and your real engine history folders (read only). Each run opens the window, waits for the
"ready" milestone and quits. "first" is the first run (for cmd: with orchestrator's .pyc files
removed first, i.e. cold bytecode); "warm" is the median of the remaining runs.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CREATE_NO_WINDOW = 0x08000000
TARGETS = {
    "cmd": [str(ROOT / ".venv" / "Scripts" / "pythonw.exe"), "run.py"],
    "exe": [str(ROOT / "dist" / "Orchestrator" / "Orchestrator.exe")],
}
MILESTONES = ["imports", "qapp", "window_created", "first_paint", "sessions", "ready"]


def user_db() -> Path:
    return Path(os.environ.get("APPDATA", Path.home())) / "Orchestrator" / "orchestrator.db"


def prepare_home() -> Path:
    home = Path(tempfile.mkdtemp(prefix="orch-bench-"))
    src = user_db()
    if src.exists():  # consistent copy even while the real app has it open
        with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as s, sqlite3.connect(home / "orchestrator.db") as d:
            s.backup(d)
        with sqlite3.connect(home / "orchestrator.db") as d:
            d.execute("UPDATE schedules SET enabled=0")
    return home


def one_run(target: str, home: Path, timeout: float = 120) -> dict[str, float]:
    log = home / f"startup-{time.time_ns()}.log"
    env = {**os.environ, "ORCHESTRATOR_HOME": str(home), "ORCHESTRATOR_STARTUP_LOG": str(log),
           "ORCHESTRATOR_BENCH_QUIT": "1"}
    t0 = time.time()
    p = subprocess.Popen(TARGETS[target], cwd=ROOT, env=env)
    marks: dict[str, float] = {}
    while time.time() - t0 < timeout:
        if log.exists():
            for line in log.read_text(encoding="utf-8").splitlines():
                name, _, ts = line.partition(" ")
                marks[name] = float(ts) - t0
        if "ready" in marks:
            break
        time.sleep(0.05)
    try:
        p.wait(20)
    except subprocess.TimeoutExpired:
        subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True)
    return marks


def clear_pyc() -> None:
    for d in (ROOT / "orchestrator").rglob("__pycache__"):
        shutil.rmtree(d, ignore_errors=True)


def bench(target: str, runs: int) -> dict:
    if not Path(TARGETS[target][0]).exists():
        print(f"{target}: {TARGETS[target][0]} not found, skipped")
        return {}
    home = prepare_home()
    results = []
    for i in range(runs):
        if target == "cmd" and i == 0:
            clear_pyc()
        m = one_run(target, home)
        results.append(m)
        print(f"  {target} run {i + 1}: " + "  ".join(f"{k}={m[k]:.2f}s" for k in MILESTONES if k in m), flush=True)
    shutil.rmtree(home, ignore_errors=True)
    first = results[0]
    warm = {k: statistics.median([r[k] for r in results[1:] if k in r]) for k in MILESTONES
            if any(k in r for r in results[1:])}
    return {"first": first, "warm": warm}


def importtime(top: int = 15) -> None:
    home = prepare_home()
    env = {**os.environ, "ORCHESTRATOR_HOME": str(home), "ORCHESTRATOR_STARTUP_LOG": str(home / "it.log"),
           "ORCHESTRATOR_BENCH_QUIT": "1"}
    r = subprocess.run([str(ROOT / ".venv" / "Scripts" / "python.exe"), "-X", "importtime", "run.py"], cwd=ROOT,
                       env=env, capture_output=True, text=True, timeout=180, creationflags=CREATE_NO_WINDOW)
    rows = []
    for line in r.stderr.splitlines():
        if line.startswith("import time:") and "|" in line:
            parts = [x.strip() for x in line[len("import time:"):].split("|")]
            if parts[0].isdigit():
                rows.append((int(parts[1]), parts[2]))
    top_level = [(us, name) for us, name in rows if not name.startswith(" ")]
    print(f"\nTop {top} top-level imports by cumulative time (python -X importtime run.py):")
    for us, name in sorted(top_level, reverse=True)[:top]:
        print(f"  {us / 1000:8.1f} ms  {name.strip()}")
    shutil.rmtree(home, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="both", choices=["cmd", "exe", "both"])
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--importtime", action="store_true")
    a = ap.parse_args()
    out = {}
    for t in (["cmd", "exe"] if a.target == "both" else [a.target]):
        out[t] = bench(t, a.runs)
    print("\nSeconds from launch            first window   fully ready")
    for t, r in out.items():
        if not r:
            continue
        for kind in ("first", "warm"):
            m = r[kind]
            label = f"{t} {'first run' if kind == 'first' else 'warm (median)'}"
            print(f"  {label:<28} {m.get('first_paint', float('nan')):>8.2f}     {m.get('ready', float('nan')):>8.2f}")
    if a.importtime:
        importtime()
    return 0


if __name__ == "__main__":
    sys.exit(main())
