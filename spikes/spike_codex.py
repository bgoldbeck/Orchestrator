"""M0 spike: drive Codex via `codex exec --json` subprocess + resume.

Run: .venv\\Scripts\\python spikes\\spike_codex.py
"""
import json
import shutil
import subprocess
from pathlib import Path

SANDBOX = Path(__file__).resolve().parent / "sandbox"
CODEX = shutil.which("codex") or "codex"  # resolves codex.cmd on Windows
MODEL_ARGS = ["-c", 'model_reasoning_effort="low"']


def run(args: list[str], prompt: str) -> tuple[str | None, str]:
    # Prompt via stdin ("-") avoids Windows .cmd argument quoting issues.
    cmd = [CODEX, "exec", *args, "-"]
    print("  cmd:", " ".join(cmd))
    proc = subprocess.Popen(
        cmd,
        cwd=SANDBOX,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    proc.stdin.write(prompt)
    proc.stdin.close()
    thread_id = None
    text = ""
    for line in proc.stdout:  # streamed JSONL
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            print("  [non-json]", line[:120])
            continue
        et = ev.get("type")
        item = ev.get("item") or {}
        extra = ""
        if et == "thread.started":
            thread_id = ev.get("thread_id")
            extra = f"thread_id={thread_id}"
        elif item:
            extra = f"item.type={item.get('type')} item.id={item.get('id')}"
            if item.get("type") == "agent_message" and et == "item.completed":
                text = item.get("text", "")
        elif et == "turn.completed":
            extra = f"usage_keys={sorted((ev.get('usage') or {}).keys())}"
        print(f"  event type={et} keys={sorted(ev.keys())} {extra}")
    err = proc.stderr.read()
    rc = proc.wait()
    print(f"  exit={rc} stderr_lines={len(err.splitlines())}")
    if rc:
        print("  stderr tail:", err.strip().splitlines()[-3:])
    return thread_id, text.strip()


def main() -> None:
    SANDBOX.mkdir(exist_ok=True)
    print("== turn 1")
    tid, reply = run(
        ["--json", "--skip-git-repo-check", "-s", "read-only", "-C", str(SANDBOX), *MODEL_ARGS],
        "Reply with the word PING and nothing else.",
    )
    print(f"  reply={reply!r} thread_id={tid}")
    print("== turn 2 (resume)")
    # `exec resume` has no -C/-s flags; cwd comes from process cwd.
    tid2, reply2 = run(
        ["resume", "--json", "--skip-git-repo-check", *MODEL_ARGS, tid],
        "What word did you just say?",
    )
    print(f"  reply={reply2!r} thread_id={tid2} same_thread={tid == tid2}")
    print("RESULT:", "OK" if "PING" in reply2.upper() else "FAIL")


if __name__ == "__main__":
    main()
