"""After a sub-agent finishes: run the project's test command ourselves, then merge its worktree branch.

Checks. The agent's own "status: done" is a claim, not proof. When a project has a test command (Settings ›
Orchestrator, per project), the app runs it on the delivered work and records the result on the task: exit code,
time and the end of the output. A red run turns the task into "failed" with the output in its report, so the
orchestrator can send a fix.

Merge. A task that ran in its own git worktree (Orchestrate › "Git worktree per task") leaves its work on branch
``orchestrator/task-N``. The app commits it there and then either merges it into the main checkout at once
(default) or, with "Review changes before merging" on, waits in Review until you press Merge or Discard. Only one
merge runs at a time. A merge that conflicts is aborted (your checkout is left as it was) and lands in Waiting on you.
After a merge the worktree and the branch are deleted.

Everything here is plain subprocess work, so it runs on a worker thread (``asyncio.to_thread``).
"""
from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from orchestrator.storage.db import Database, Task

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
REVIEW_SETTING = "orchestrate.review_before_merge"  # off by default: merge on its own when checks pass
CHECK_TIMEOUT = 900
TAIL = 2500


def check_key(project_id: int) -> str:
    return f"checks.cmd.{project_id}"


def check_command(db: Database, project_id: int) -> str:
    return str(db.get_setting(check_key(project_id), "") or "").strip()


def set_check_command(db: Database, project_id: int, cmd: str) -> None:
    db.set_setting(check_key(project_id), (cmd or "").strip())


def review_first(db: Database) -> bool:
    return bool(db.get_setting(REVIEW_SETTING, False))


def _git(path: str, *args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, timeout=timeout,
                          creationflags=CREATE_NO_WINDOW)


# ------------------------------------------------------------------ checks
def run_checks(cmd: str, cwd: str, timeout: float = CHECK_TIMEOUT) -> dict:
    """Run the test command in `cwd`. {ok, cmd, exit, sec, tail, at}; never raises."""
    t0 = time.time()
    try:
        r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW, errors="replace")
        out = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
        code = r.returncode
    except subprocess.TimeoutExpired:
        out, code = f"Timed out after {int(timeout)} s.", -1
    except OSError as exc:
        out, code = f"Could not run the command: {exc}", -1
    out = out.strip()
    return {"ok": code == 0, "cmd": cmd, "exit": code, "sec": round(time.time() - t0, 1),
            "tail": out[-TAIL:], "at": time.time()}


def checks_text(checks: dict | None) -> str:
    if not checks:
        return ""
    return (f"Tests passed ({checks['sec']} s)" if checks.get("ok")
            else f"Tests failed (exit {checks.get('exit')})")


# ------------------------------------------------------------------ worktrees
def branch_of(t: Task) -> str:
    return f"orchestrator/task-{t.id}"


def main_repo(worktree: str) -> str | None:
    """The main checkout a worktree belongs to."""
    r = _git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if r.returncode != 0 or not r.stdout.strip():
        return None
    common = Path(r.stdout.strip())
    return str(common.parent if common.name == ".git" else common)


def _identity_args(repo: str) -> list[str]:
    if _git(repo, "config", "user.email").stdout.strip():
        return []
    return ["-c", "user.name=Orchestrator", "-c", "user.email=orchestrator@localhost"]


def commit_worktree(t: Task) -> bool:
    """Commit everything the agent left in its worktree. True when there was something to commit."""
    wt = t.worktree or ""
    _git(wt, "add", "-A")
    if _git(wt, "diff", "--cached", "--quiet").returncode == 0:
        return False
    msg = f"Task {t.id}: {t.title}"
    r = subprocess.run(["git", *_identity_args(wt), "-C", wt, "commit", "-q", "-m", msg], capture_output=True,
                       text=True, creationflags=CREATE_NO_WINDOW)
    return r.returncode == 0


def has_changes(repo: str, branch: str) -> bool:
    """Does the branch have commits the current HEAD of the main checkout lacks?"""
    r = _git(repo, "rev-list", "--count", f"HEAD..{branch}")
    return r.returncode == 0 and r.stdout.strip() not in ("", "0")


def _in_progress(repo: str) -> str | None:
    git_dir = _git(repo, "rev-parse", "--path-format=absolute", "--git-dir").stdout.strip()
    for name, what in (("MERGE_HEAD", "a merge"), ("rebase-merge", "a rebase"), ("rebase-apply", "a rebase"),
                       ("CHERRY_PICK_HEAD", "a cherry-pick"), ("REVERT_HEAD", "a revert")):
        if git_dir and (Path(git_dir) / name).exists():
            return what
    return None


def cleanup(repo: str, worktree: str, branch: str, force_branch: bool = False) -> None:
    _git(repo, "worktree", "remove", "--force", worktree)
    _git(repo, "worktree", "prune")
    _git(repo, "branch", "-D" if force_branch else "-d", branch)


@dataclass
class MergeResult:
    state: str  # merged | conflict | failed
    note: str


def merge(t: Task) -> MergeResult:
    """Commit the task's worktree and merge its branch into the main checkout's current branch."""
    wt = t.worktree or ""
    if not wt or not Path(wt).is_dir():
        return MergeResult("failed", "The task's worktree folder is gone.")
    repo = main_repo(wt)
    if not repo:
        return MergeResult("failed", "Could not find the main checkout of the worktree.")
    branch = branch_of(t)
    commit_worktree(t)
    busy = _in_progress(repo)
    if busy:
        return MergeResult("conflict", f"Your checkout is in the middle of {busy}. Finish it, then press Merge again.")
    if not has_changes(repo, branch):
        cleanup(repo, wt, branch, force_branch=True)
        return MergeResult("merged", "No changes to merge. Worktree and branch removed.")
    target = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "HEAD"
    r = subprocess.run(["git", *_identity_args(repo), "-C", repo, "merge", "--no-ff", "-m",
                        f"Merge task {t.id}: {t.title}", branch], capture_output=True, text=True,
                       creationflags=CREATE_NO_WINDOW)
    if r.returncode != 0:
        if _in_progress(repo) == "a merge":
            _git(repo, "merge", "--abort")
        out = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
        detail = " ".join(out[-3:])[:400]
        return MergeResult("conflict", f"Could not merge into {target} (left as it was): {detail}")
    cleanup(repo, wt, branch)
    return MergeResult("merged", f"Merged into {target}. Worktree and branch removed.")


def discard(t: Task) -> MergeResult:
    wt = t.worktree or ""
    repo = main_repo(wt) if wt and Path(wt).is_dir() else None
    if repo:
        cleanup(repo, wt, branch_of(t), force_branch=True)
    return MergeResult("discarded", "Changes thrown away. Worktree and branch removed.")
