"""Read-only indexer for the engines' own session logs.

- Claude: ~/.claude/projects/<project-key>/<session-id>.jsonl
- Codex:  ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<id>.jsonl (+ ~/.codex/session_index.jsonl titles)

Formats are internal to each CLI (see M0_NOTES.md), so every parser here is
tolerant: unknown lines are skipped, nothing is ever written back.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from orchestrator.core.harness_text import clean, strip_harness
from orchestrator.ui_text import short_title

UUID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$", re.I)
NOISE_PREFIXES = ("<command-", "<local-command", "<system-reminder", "<environment_context", "<user_instructions",
                  "<permissions", "Caveat:", "<ide_", "[Request interrupted")


@dataclass
class SessionInfo:
    engine: str
    session_id: str
    cwd: str
    title: str
    started: float
    last_active: float
    path: str
    mtime: float
    turns: int = 0
    model: str = ""
    effort: str = ""


def claude_root() -> Path:
    if os.environ.get("ORCHESTRATOR_INDEX_CLAUDE"):  # indexer-only override (tests / demos)
        return Path(os.environ["ORCHESTRATOR_INDEX_CLAUDE"])
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"


def codex_home() -> Path:
    if os.environ.get("ORCHESTRATOR_INDEX_CODEX"):
        return Path(os.environ["ORCHESTRATOR_INDEX_CODEX"])
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def _lines(path: Path) -> Iterator[dict[str, Any]]:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(d, dict):
                    yield d
    except OSError:
        return


def _ts(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value) / (1000 if value > 1e12 else 1)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _text_of(content: Any) -> str:
    """Text from a str, or a list of {type:text|input_text|output_text|Text, text} blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and str(b.get("type", "")).lower() in ("text", "input_text", "output_text"):
                parts.append(str(b.get("text", "")))
        return "\n".join(p for p in parts if p)
    return ""


def _context_chip(out: list[dict[str, Any]], tags: list[str]) -> None:
    """Harness text was hidden here: one small 'system context' marker (merged with a previous one)."""
    if out and out[-1].get("kind") == "context":
        out[-1]["tags"] = sorted(set(out[-1]["tags"]) | set(tags))
    else:
        out.append({"role": "system", "kind": "context", "tags": sorted(set(tags))})


def is_noise(text: str) -> bool:
    t = text.lstrip()
    return not t or t.startswith(NOISE_PREFIXES)


# --------------------------------------------------------------------- Claude

def parse_claude_file(path: Path) -> SessionInfo | None:
    sid = cwd = None
    title = summary = first_user = ""
    first_ts = last_ts = None
    turns = 0
    model = ""
    for d in _lines(path):
        t = d.get("type")
        sid = sid or d.get("sessionId")
        if t == "assistant" and isinstance(d.get("message"), dict) and d["message"].get("model"):
            m = str(d["message"]["model"])
            model = model if m.startswith("<") else m
        if t == "custom-title" and d.get("customTitle"):
            title = clean(str(d["customTitle"]))
        elif t == "summary" and d.get("summary"):
            summary = clean(str(d["summary"]))
        if t not in ("user", "assistant") or d.get("isSidechain"):
            continue
        cwd = cwd or d.get("cwd")
        ts = _ts(d.get("timestamp"))
        if ts:
            first_ts = first_ts or ts
            last_ts = ts
        if t == "user" and not d.get("isMeta"):
            text = clean(_text_of((d.get("message") or {}).get("content")))
            if text and not is_noise(text):
                turns += 1
                first_user = first_user or text
    if not sid:
        m = UUID_RE.search(path.name)
        sid = m.group(1) if m else None
    if not sid or not turns:
        return None
    st = path.stat()
    return SessionInfo("claude", sid, cwd or "", short_title(title or summary or first_user),
                       first_ts or st.st_mtime, last_ts or st.st_mtime, str(path), st.st_mtime, turns, model)


def load_claude_history(path: Path) -> list[dict[str, Any]]:
    """History in the app's message shape (role/kind/...)."""
    out: list[dict[str, Any]] = []
    tools: dict[str, dict[str, Any]] = {}
    for d in _lines(path):
        t = d.get("type")
        if t not in ("user", "assistant") or d.get("isSidechain"):
            continue
        content = (d.get("message") or {}).get("content")
        if t == "user":
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        call = tools.get(b.get("tool_use_id", ""))
                        res = b.get("content")
                        text = res if isinstance(res, str) else _text_of(res)
                        if call is not None:
                            call["output"] = text[-20000:]
                            call["is_error"] = bool(b.get("is_error"))
            if d.get("isMeta"):
                continue
            raw = _text_of(content)
            text, removed = strip_harness(raw)
            if removed or (raw and is_noise(raw)):
                _context_chip(out, removed or ["system"])
            if text and not is_noise(text):
                out.append({"role": "user", "kind": "text", "text": text})
        else:
            if not isinstance(content, list):
                continue
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text"):
                    text = clean(b["text"])
                    if not text:
                        continue
                    if out and out[-1]["role"] == "assistant" and out[-1]["kind"] == "text":
                        out[-1]["text"] += "\n\n" + text
                    else:
                        out.append({"role": "assistant", "kind": "text", "text": text})
                elif b.get("type") == "tool_use":
                    from orchestrator.engines.claude import tool_summary

                    inp = b.get("input") or {}
                    call = {"role": "tool", "kind": "tool", "id": b.get("id", ""), "name": b.get("name", "tool"),
                            "summary": tool_summary(b.get("name", ""), inp if isinstance(inp, dict) else {}),
                            "output": "", "is_error": False}
                    tools[call["id"]] = call
                    out.append(call)
    return out


def scan_claude(root: Path | None = None) -> Iterator[Path]:
    root = root or claude_root()
    if not root.is_dir():
        return
    for proj in root.iterdir():
        if proj.is_dir():
            yield from (p for p in proj.glob("*.jsonl") if p.is_file())


# ---------------------------------------------------------------------- Codex

def codex_titles(home: Path | None = None) -> dict[str, str]:
    titles: dict[str, str] = {}
    for d in _lines((home or codex_home()) / "session_index.jsonl"):
        if d.get("id") and d.get("thread_name"):
            titles[str(d["id"])] = str(d["thread_name"])
    return titles


def _is_subagent(meta: dict[str, Any]) -> bool:
    src = meta.get("source")
    return meta.get("thread_source") == "subagent" or (isinstance(src, dict) and "subagent" in src)


def _codex_items(path: Path) -> Iterator[tuple[str, dict[str, Any], dict[str, Any]]]:
    for d in _lines(path):
        p = d.get("payload")
        if isinstance(p, dict):
            yield str(d.get("type", "")), p, d


def parse_codex_file(path: Path, titles: dict[str, str] | None = None) -> SessionInfo | None:
    sid = cwd = None
    first_user = ""
    first_ts = last_ts = None
    turns = 0
    fallback_user = ""
    model = effort = ""
    for t, p, d in _codex_items(path):
        ts = _ts(d.get("timestamp"))
        if ts:
            first_ts = first_ts or ts
            last_ts = ts
        if t == "session_meta":
            if _is_subagent(p):
                return None
            sid = sid or p.get("id") or p.get("session_id")
            cwd = cwd or p.get("cwd")
        elif t == "turn_context":
            cwd = cwd or p.get("cwd")
            model = str(p.get("model") or model)
            effort = str(p.get("effort") or effort)
        elif t == "event_msg":
            et = p.get("type")
            text = ""
            if et == "item_completed" and (p.get("item") or {}).get("type") == "UserMessage":
                text = clean(_text_of(p["item"].get("content")))
            elif et == "user_message":
                text = clean(str(p.get("message", "")))
            if text and not is_noise(text):
                turns += 1
                first_user = first_user or text
        elif t == "response_item" and p.get("type") == "message" and p.get("role") == "user":
            text = clean(_text_of(p.get("content")))
            if text and not is_noise(text):
                fallback_user = fallback_user or text
    if not sid:
        m = UUID_RE.search(path.name)
        sid = m.group(1) if m else None
    first_user = first_user or fallback_user
    if not sid or not first_user:
        return None
    st = path.stat()
    title = clean((titles or {}).get(sid) or "") or first_user
    return SessionInfo("codex", sid, cwd or "", short_title(title), first_ts or st.st_mtime,
                       last_ts or st.st_mtime, str(path), st.st_mtime, max(turns, 1), model, effort)


def load_codex_history(path: Path) -> list[dict[str, Any]]:
    items: list[tuple[str, dict[str, Any], dict[str, Any]]] = list(_codex_items(path))
    has_items = any(t == "event_msg" and p.get("type") == "item_completed" and
                    (p.get("item") or {}).get("type") in ("UserMessage", "AgentMessage") for t, p, _ in items)
    has_events = any(t == "event_msg" and p.get("type") in ("user_message", "agent_message") for t, p, _ in items)
    out: list[dict[str, Any]] = []

    def add_text(role: str, raw: str) -> None:
        text, removed = strip_harness(raw)
        if removed or (role == "user" and raw and is_noise(raw)):
            _context_chip(out, removed or ["system"])
        if text and not (role == "user" and is_noise(text)):
            out.append({"role": role, "kind": "text", "text": text})

    from orchestrator.engines.codex import unwrap_shell, _short

    for t, p, _ in items:
        if has_items:
            if t != "event_msg" or p.get("type") != "item_completed":
                continue
            it = p.get("item") or {}
            kind = it.get("type")
            if kind == "UserMessage":
                add_text("user", _text_of(it.get("content")))
            elif kind == "AgentMessage":
                add_text("assistant", _text_of(it.get("content")))
            elif kind == "CommandExecution":
                cmd = it.get("command", "")
                cmd = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
                code = it.get("exit_code")
                out.append({"role": "tool", "kind": "tool", "id": it.get("id", ""), "name": "shell",
                            "summary": _short(unwrap_shell(cmd)), "output": str(it.get("aggregated_output") or "")[-20000:],
                            "is_error": code not in (0, None) or str(it.get("status", "")).lower() == "failed"})
            elif kind == "FileChange":
                ch = it.get("changes") or []
                paths = list(ch.keys()) if isinstance(ch, dict) else [c.get("path", "") for c in ch if isinstance(c, dict)]
                out.append({"role": "tool", "kind": "tool", "id": it.get("id", ""), "name": "edit",
                            "summary": _short(", ".join(paths) or "files"), "output": "\n".join(paths),
                            "is_error": str(it.get("status", "")).lower() == "failed"})
            elif kind == "McpToolCall":
                out.append({"role": "tool", "kind": "tool", "id": it.get("id", ""),
                            "name": f"{it.get('server', 'mcp')}.{it.get('tool', '?')}",
                            "summary": _short(json.dumps(it.get("arguments") or {})),
                            "output": _short(json.dumps(it.get("result")), 4000), "is_error": False})
        elif has_events:
            if t == "event_msg" and p.get("type") == "user_message":
                add_text("user", str(p.get("message", "")))
            elif t == "event_msg" and p.get("type") == "agent_message":
                add_text("assistant", str(p.get("message", "")))
        else:
            if t == "response_item" and p.get("type") == "message" and p.get("role") in ("user", "assistant"):
                add_text(p["role"], _text_of(p.get("content")))
    return out


def scan_codex(home: Path | None = None) -> Iterator[Path]:
    root = (home or codex_home()) / "sessions"
    if root.is_dir():
        yield from (p for p in root.rglob("*.jsonl") if p.is_file())


def load_history(engine: str, path: str) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    return load_claude_history(p) if engine == "claude" else load_codex_history(p)


def norm_path(p: str) -> str:
    return os.path.normcase(os.path.normpath(p)) if p else ""
