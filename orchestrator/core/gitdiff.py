"""Read-only git helpers for the diff viewer: what changed in a folder since the last commit."""
from __future__ import annotations

import html
import subprocess
import sys
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
MAX_UNTRACKED = 200_000


def _git(folder: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", folder, "-c", "core.quotepath=off", *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW)


def repo_root(folder: str) -> str | None:
    if not folder or not Path(folder).is_dir():
        return None
    r = _git(folder, "rev-parse", "--show-toplevel")
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def _base(root: str) -> str:
    return "HEAD" if _git(root, "rev-parse", "--verify", "-q", "HEAD").returncode == 0 else EMPTY_TREE


def changed_files(folder: str) -> list[tuple[str, str]]:
    """[(status, path relative to repo root)]: M modified, A added, D deleted, R renamed, ? untracked."""
    root = repo_root(folder)
    if not root:
        return []
    r = _git(root, "status", "--porcelain=v1", "-uall")
    out = []
    for line in r.stdout.splitlines():
        if len(line) < 4:
            continue
        xy, path = line[:2], line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        path = path.strip('"')
        st = "?" if xy == "??" else next((c for c in xy if c != " "), "M")
        out.append((st, path))
    return out


def file_diff(folder: str, path: str, status: str) -> str:
    root = repo_root(folder)
    if not root:
        return ""
    if status == "?":
        p = Path(root) / path
        try:
            data = p.read_bytes()[:MAX_UNTRACKED]
        except OSError as exc:
            return f"(cannot read {path}: {exc})"
        if b"\0" in data[:8000]:
            return f"Binary file {path} (new)"
        lines = data.decode("utf-8", errors="replace").splitlines()
        return "\n".join([f"--- /dev/null", f"+++ b/{path}", f"@@ -0,0 +1,{len(lines)} @@"] + ["+" + ln for ln in lines])
    r = _git(root, "diff", _base(root), "--", path)
    return r.stdout or r.stderr


def diff_stats(folder: str) -> tuple[int, int, int]:
    """(files, added lines, removed lines) for tracked changes plus untracked files."""
    root = repo_root(folder)
    if not root:
        return 0, 0, 0
    files = changed_files(root)
    add = rem = 0
    r = _git(root, "diff", "--numstat", _base(root))
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            add += int(parts[0])
            rem += int(parts[1])
    for st, path in files:
        if st == "?":
            try:
                add += (Path(root) / path).read_text(encoding="utf-8", errors="replace").count("\n") + 1
            except OSError:
                pass
    return len(files), add, rem


def render_diff_html(diff: str, dark: bool = True) -> str:
    """Unified diff as colored HTML lines (monospace)."""
    add_bg, rem_bg = ("rgba(63,185,80,0.16)", "rgba(248,81,73,0.16)") if dark else ("#e6ffec", "#ffebe9")
    hunk, meta = ("#79c0ff", "#8b949e") if dark else ("#0969da", "#57606a")
    rows = []
    for line in diff.splitlines():
        esc = html.escape(line) or "&nbsp;"
        if line.startswith(("+++", "---", "diff --git", "index ", "new file", "deleted file", "similarity")):
            rows.append(f'<div style="color:{meta};">{esc}</div>')
        elif line.startswith("@@"):
            rows.append(f'<div style="color:{hunk};">{esc}</div>')
        elif line.startswith("+"):
            rows.append(f'<div style="background:{add_bg};">{esc}</div>')
        elif line.startswith("-"):
            rows.append(f'<div style="background:{rem_bg};">{esc}</div>')
        else:
            rows.append(f"<div>{esc}</div>")
    return ('<div style="font-family:Cascadia Mono, Consolas, monospace; font-size:9pt; white-space:pre;">'
            + "".join(rows) + "</div>")
