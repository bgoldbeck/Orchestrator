"""Change app settings from any chat: model preferences, appearance, schedules and a few safe settings.

Every change is recorded with an undo snapshot; the chat shows "Applied: …" with Undo. Destructive changes
(disabling a model, deleting a schedule) ask the user Allow/Deny in the chat first.

Three ways in: the "app" MCP server attached to every chat (Claude and Codex), the orchestrator planner (same
server), and a local fast path for very simple commands typed in a chat ("increase the font size"), which needs
no model call.
"""
from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from PySide6.QtCore import QObject, Signal

from orchestrator.core import model_registry
from orchestrator.core import preferences as P
from orchestrator.core.catalog import ENGINES
from orchestrator.storage.db import Database


class AppCommandError(Exception):
    """Shown to the agent (tool error) or the user; says what to do instead."""


# ---------------------------------------------------------------- model names

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9.]+", " ", (s or "").lower()).strip()


def resolve_model(db: Database, text: str, engine: str = "", need_enabled: bool = True) -> tuple[str, str]:
    """('claude', 'claude-opus-5-5') from 'opus 5.5', 'Opus', 'gpt 6.2 sol', an id, …

    Raises AppCommandError when nothing matches, when several enabled models match ("gpt 6.2" -> Sol or Astra),
    or when the only match is disabled (with need_enabled)."""
    q = _norm(text).replace("gpt ", "gpt-")
    if not q:
        raise AppCommandError("Name a model.")
    models = [m for m in db.models() if not engine or m.engine == engine]

    def hay(m) -> str:
        return f"{_norm(m.id)} {_norm(m.label).replace('gpt ', 'gpt-')} {m.engine}"

    found = [m for m in models if q in (_norm(m.id), _norm(m.label).replace("gpt ", "gpt-"))]
    if not found:
        words = q.split()
        found = [m for m in models if all(re.search(rf"(?<![\w.]){re.escape(w)}(?![\w.])", hay(m))
                                          or (len(w) > 3 and w in hay(m)) for w in words)]
    if not found:
        names = ", ".join(m.label for m in models)
        raise AppCommandError(f"No model matches {text!r}. Known models: {names}.")
    if len(found) > 1:
        enabled = [m for m in found if m.enabled]
        if len(enabled) == 1:
            found = enabled
        else:
            opts = ", ".join(f"{m.label} ({m.id})" for m in (enabled or found))
            raise AppCommandError(f"{text!r} could mean: {opts}. Ask the user which one.")
    m = found[0]
    if need_enabled and not m.enabled:
        raise AppCommandError(f"{m.label} is disabled in Settings › Models. Ask the user to enable it "
                              "(set_setting model_enabled) or pick another model.")
    return m.engine, m.id


# ---------------------------------------------------------------- natural times

_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "ten": 10, "twelve": 12, "a": 1, "an": 1,
        "half an": 0.5}


def _clock(text: str) -> tuple[int, int] | None:
    t = text.lower().strip()
    if t in ("noon", "midday"):
        return 12, 0
    if t == "midnight":
        return 0, 0
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?", t)
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").replace(".", "")
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    if h > 23 or mi > 59:
        return None
    return h, mi


_CLOCK_RE = r"(noon|midday|midnight|\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)?)"


def parse_when(text: str, now: datetime | None = None) -> datetime:
    """'in 5 hours', 'in 30 minutes', 'tomorrow 8am', 'tonight', 'tonight at 10', 'today at 3pm', 'at 6:30pm',
    'friday 9am', 'next monday', '2026-12-01 14:00'. Raises AppCommandError if it can't tell."""
    now = (now or datetime.now()).replace(second=0, microsecond=0)
    t = " ".join((text or "").lower().replace(",", " ").split())
    if not t or t in ("now", "right now", "asap"):
        return now + timedelta(minutes=1)
    m = re.fullmatch(r"in\s+(\d+(?:\.\d+)?|one|two|three|four|five|six|ten|twelve|an?|half an)\s*"
                     r"(minutes?|mins?|m|hours?|hrs?|h|days?|d)", t)
    if m:
        n = float(_NUM.get(m.group(1), m.group(1)) if m.group(1) in _NUM else m.group(1))
        unit = m.group(2)[0]
        return now + {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n)}[unit]
    if t == "in half an hour":
        return now + timedelta(minutes=30)
    try:
        return datetime.fromisoformat(t.replace(" ", "T") if re.match(r"\d{4}-\d{2}-\d{2} \d", t) else t)
    except ValueError:
        pass
    day = None
    rest = t
    tonight = t.startswith("tonight")
    for word, delta in (("today", 0), ("tonight", 0), ("tomorrow", 1)):
        if rest.startswith(word):
            day = now.date() + timedelta(days=delta)
            rest = rest[len(word):].strip()
            if word == "tonight" and not rest:
                rest = "9pm"
            if word == "tomorrow" and not rest:
                rest = "8am"
            break
    m = re.match(r"(?:next\s+|on\s+)?(mon|tue|wed|thu|fri|sat|sun)[a-z]*\b", rest)
    if day is None and m:
        target = _DAYS.index(m.group(1))
        ahead = (target - now.weekday()) % 7 or 7
        day = now.date() + timedelta(days=ahead)
        rest = rest[m.end():].strip() or "9am"
    rest = re.sub(r"^(?:at|@)\s*", "", rest)
    if day is not None and rest.startswith(("morning", "in the morning")):
        rest = "8am"
    if day is not None and rest in ("evening", "in the evening", "night"):
        rest = "7pm"
    clock = _clock(rest) if rest else None
    if clock and tonight and clock[0] < 12 and not re.search(r"a\.?m", rest):
        clock = (clock[0] + 12, clock[1])  # "tonight at 10" = 10 pm
    if clock is None:
        raise AppCommandError(f"Can't read the time {text!r}. Try 'in 5 hours', 'tomorrow 8am' or 'friday 9:30am'.")
    when = datetime.combine(day or now.date(), datetime.min.time()).replace(hour=clock[0], minute=clock[1])
    if day is None and when <= now:
        when += timedelta(days=1)  # "at 6pm" when it is already past 6pm: tomorrow
    if when <= now:
        raise AppCommandError(f"{text!r} is in the past.")
    return when


def parse_repeat(text: str) -> str:
    """Cron (min hour dom mon dow) from 'every day at 8am', 'weekdays 9:30', 'every monday 7pm', 'hourly',
    'every 2 hours', or a cron string as is."""
    t = " ".join((text or "").lower().replace(",", " ").split())
    if re.fullmatch(r"[\d*/,\-]+ [\d*/,\-]+ [\d*/,\-?LW#]+ [\d*/,\-a-z]+ [\d*/,\-a-z?L#]+", t):
        return t
    if t in ("hourly", "every hour"):
        return "0 * * * *"
    m = re.fullmatch(r"every (\d+) hours?", t)
    if m:
        return f"0 */{int(m.group(1))} * * *"
    m = re.fullmatch(r"every (\d+) minutes?", t)
    if m:
        return f"*/{int(m.group(1))} * * * *"
    clock = (8, 0)
    m = re.search(r"(?:at\s+)?" + _CLOCK_RE + r"$", t)
    if m and _clock(m.group(1)):
        clock = _clock(m.group(1))
        t = t[:m.start()].strip()
    t = re.sub(r"\b(at|on|every|each)\b", " ", t)
    t = " ".join(t.split())
    if t in ("", "day", "daily", "days", "night", "nightly", "morning"):
        dow = "*"
    elif t in ("weekday", "weekdays", "workday", "workdays", "business days"):
        dow = "mon-fri"
    elif t in ("weekend", "weekends"):
        dow = "sat,sun"
    else:
        days = [d[:3] for d in re.split(r"\s+|and|&", t) if d[:3] in _DAYS]
        if not days:
            raise AppCommandError(f"Can't read the repeat {text!r}. Try 'every day at 8am' or 'weekdays 9:30'.")
        dow = ",".join(days)
    return f"{clock[1]} {clock[0]} * * {dow}"


# ---------------------------------------------------------------- changes + undo

@dataclass
class Change:
    id: str
    sid: int | None
    summary: str  # "Font size 10 pt → 11 pt"
    undo: dict[str, Any]  # what restores it (see AppControl.undo)
    at: float = field(default_factory=time.time)
    undone: bool = False


SAFE_SETTINGS = {
    "global_max_parallel": ("global.max_parallel", "Agents running at once (whole app)", int, (1, 32)),
    "summaries_mode": ("summaries.mode", "Session summaries", str, ("model", "heuristic", "off")),
    "reuse_confirm": ("orchestrate.confirm", "Reuse and model picks", str, ("countdown", "always_ask", "never")),
    "escalate": ("orchestrate.escalate", "Retry failed automatic picks once", bool, None),
    "notifications": ("ui.notifications", "Notifications", bool, None),
    "close_to_tray": ("ui.close_to_tray", "Keep running in the tray", bool, None),
}


def _bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on", "enable", "enabled")


class AppControl(QObject):
    """Runs app commands, keeps undo snapshots and asks the user before destructive ones."""

    applied = Signal(object)  # Change
    undone = Signal(object)  # Change
    confirm_requested = Signal(object, str, object)  # sid | None, text, asyncio.Future[bool]
    changed = Signal(str)  # area: preferences | appearance | schedules | settings | models

    def __init__(self, db: Database, runner=None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.db = db
        self.runner = runner
        self.changes: dict[str, Change] = {}
        self.confirm_timeout = 300.0
        from orchestrator.core.orchestration import Bridge

        self.bridge = Bridge(self.handle)

    def mcp_for(self, sid: int) -> dict | None:
        """The "app" MCP server for chat `sid` (both engines); off with the setting chat.app_tools = false."""
        if not self.db.get_setting("chat.app_tools", True):
            return None
        import sys

        from orchestrator.core.orchestration import APP_ROOT, mcp_command

        self.bridge.start()
        command, args = mcp_command()
        env = {"ORCH_SERVER": "app", "ORCH_BRIDGE": self.bridge.address, "ORCH_TOKEN": self.bridge.token,
               "ORCH_ORCHESTRATION_ID": str(sid)}
        if not getattr(sys, "frozen", False):
            env["PYTHONPATH"] = str(APP_ROOT)
        return {"app": {"command": command, "args": args, "env": env, "tool_timeout_sec": 360}}

    # ---- plumbing
    def _record(self, sid: int | None, summary: str, undo: dict[str, Any], area: str) -> dict[str, Any]:
        ch = Change(uuid.uuid4().hex[:10], sid, summary, undo)
        self.changes[ch.id] = ch
        if sid and self.db.session(sid):  # part of the chat's history
            self.db.add_message(sid, "system", "app_change", {"summary": summary, "change_id": ch.id})
        self.changed.emit(area)
        self.applied.emit(ch)
        return {"applied": summary, "change_id": ch.id, "note": "The user sees this change with an Undo button."}

    async def _confirm(self, sid: int | None, text: str) -> bool:
        from PySide6.QtCore import QMetaMethod

        if not self.isSignalConnected(QMetaMethod.fromSignal(self.confirm_requested)):
            return False  # nobody can ask the user: refuse rather than act silently
        fut = asyncio.get_running_loop().create_future()
        self.confirm_requested.emit(sid, text, fut)
        try:
            return bool(await asyncio.wait_for(fut, self.confirm_timeout))
        except asyncio.TimeoutError:
            return False

    async def handle(self, sid: int, op: str, args: dict[str, Any]) -> Any:
        fn = getattr(self, "op_" + op, None)
        if fn is None:
            raise AppCommandError(f"Unknown app command {op!r}.")
        res = fn(sid or None, **{k: v for k, v in args.items() if k != "timeout"})
        return await res if asyncio.iscoroutine(res) else res

    # ---- undo
    def undo(self, change_id: str) -> str:
        ch = self.changes.get(change_id)
        if not ch or ch.undone:
            raise AppCommandError("Nothing to undo.")
        u = ch.undo
        kind = u["kind"]
        # mark first: applying the old values can re-render the chat (appearance), which should show "Undone"
        ch.undone = True
        if ch.sid and self.db.session(ch.sid):
            self.db.add_message(ch.sid, "system", "app_change", {"summary": ch.summary, "change_id": ch.id,
                                                                 "undone": True})
        if kind == "settings":
            for k, v in u["values"].items():
                self.db.set_setting(k, v)
            if u.get("area") == "appearance":
                from orchestrator.ui import appearance as A

                A.apply(A.load(self.db))
        elif kind == "schedule_created":
            if self.runner is not None:
                self.runner.remove(u["id"])
            self.db.delete_schedule(u["id"])
        elif kind == "schedule_deleted":
            data = u["data"]
            new_id = self.db.save_schedule(data)
            if self.runner is not None:
                self.runner.sync(self.db.schedule(new_id))
        elif kind == "model_enabled":
            self.db.set_model_enabled(u["engine"], u["model"], u["was"])
        self.changed.emit(u.get("area", "settings"))
        self.undone.emit(ch)
        return f"Undone: {ch.summary}"

    # ---- preferences
    def op_list_preferences(self, sid) -> dict[str, Any]:
        return {"rules": [{"priority": r.priority, "task_type": r.name, "keywords": r.keywords, "engine": r.engine,
                           "model": r.model, "effort": r.effort, "enabled": r.enabled, "notes": r.notes,
                           "active": P.usable(self.db, r)} for r in P.rules(self.db)]}

    def op_set_model_preference(self, sid, task_type: str, model: str, effort: str = "", keywords: Any = None,
                                priority: int | None = None, notes: str = "", engine: str = "") -> dict[str, Any]:
        task = (task_type or "").strip()
        if not task:
            raise AppCommandError("task_type is empty (e.g. 'Design and planning').")
        eng, mid = resolve_model(self.db, model, engine)
        m = next(x for x in self.db.models(eng) if x.id == mid)
        effort = (effort or "").strip().lower()
        if effort and m.efforts and effort not in m.efforts:
            raise AppCommandError(f"{m.label} has no effort {effort!r}. Use one of: {', '.join(m.efforts)}.")
        kws = P.split_keywords(keywords) if isinstance(keywords, str) else [str(k) for k in (keywords or [])]
        before = self.db.get_setting(P.KEY)
        rules = P.rules(self.db)
        cur = next((r for r in rules if r.name.lower() == task.lower()), None)
        if cur:
            cur.engine, cur.model, cur.effort = eng, mid, effort or cur.effort
            if kws:
                cur.keywords = kws
            if priority is not None:
                cur.priority = int(priority)
            if notes:
                cur.notes = notes
            cur.enabled = True
            verb = "Updated"
        else:
            cur = P.Rule(name=task[:1].upper() + task[1:], keywords=kws or [w for w in re.split(
                r"\s+and\s+|,|/", task.lower()) if w.strip()], engine=eng, model=mid, effort=effort,
                priority=int(priority) if priority is not None else 1, notes=notes or "Set from chat")
            rules.append(cur)
            verb = "Added"
        P.save_rules(self.db, rules)
        summary = f"{verb} preference: {cur.name} → {P.describe(self.db, cur)}"
        warn = P.image_warning(self.db, cur)
        res = self._record(sid, summary, {"kind": "settings", "values": {P.KEY: before}, "area": "preferences"},
                           "preferences")
        if warn:
            res["warning"] = warn
        return res

    # ---- appearance
    def op_set_appearance(self, sid, font_size: Any = None, chat_font_size: Any = None, font: str = "",
                          code_font: str = "", code_font_size: Any = None, theme: str = "", density: str = "",
                          accent: str = "") -> dict[str, Any]:
        from orchestrator.ui import appearance as A

        look = A.load(self.db)
        before = {key: self.db.get_setting(key) for key in A.KEYS.values()}
        parts = []

        def size(cur: float, v: Any, choices: list[float], label: str) -> float:
            s = str(v).strip().lower()
            idx = min(range(len(choices)), key=lambda i: abs(choices[i] - cur))
            if s in ("bigger", "larger", "increase", "up", "+", "+1"):
                new = choices[min(idx + 1, len(choices) - 1)]
            elif s in ("smaller", "decrease", "down", "-", "-1"):
                new = choices[max(idx - 1, 0)]
            elif s in ("default", "reset"):
                new = getattr(A.Look(), label)
            else:
                m = re.fullmatch(r"([+-])?\s*(\d+(?:\.\d+)?)\s*(?:pt)?", s)
                if not m:
                    raise AppCommandError(f"Can't read the size {v!r}. Use a number (pt), 'bigger' or 'smaller'.")
                n = float(m.group(2))
                new = cur + n if m.group(1) == "+" else cur - n if m.group(1) == "-" else n
                new = max(7.0, min(24.0, new))
            return new

        names = {"ui_size": "UI font size", "chat_size": "Chat font size", "code_size": "Code font size"}
        for attr, val, choices in (("ui_size", font_size, A.UI_SIZES), ("chat_size", chat_font_size, A.CHAT_SIZES),
                                   ("code_size", code_font_size, A.CODE_SIZES)):
            if val not in (None, ""):
                old = getattr(look, attr)
                setattr(look, attr, size(old, val, choices, attr))
                parts.append(f"{names[attr]} {old:g} → {getattr(look, attr):g} pt")
        if font:
            look.font = font
            parts.append(f"Font → {font}")
        if code_font:
            look.code_font = code_font
            parts.append(f"Code font → {code_font}")
        if theme:
            t = {"dark": "Dark", "light": "Light", "system": "Auto", "auto": "Auto"}.get(theme.strip().lower())
            if not t:
                raise AppCommandError("theme must be dark, light or system.")
            look.theme = t
            parts.append(f"Theme → {A.THEMES[t]}")
        if density:
            d = {"compact": "Compact", "comfortable": "Comfortable", "dense": "Compact", "spacious": "Comfortable"}.get(
                density.strip().lower())
            if not d:
                raise AppCommandError("density must be compact or comfortable.")
            look.density = d
            parts.append(f"Density → {d}")
        if accent:
            from orchestrator.ui.theme import ACCENTS

            a = next((k for k in ACCENTS if k.lower() == accent.strip().lower()), None)
            if not a and re.fullmatch(r"#[0-9a-fA-F]{6}", accent.strip()):
                a = accent.strip().lower()
            if not a:
                raise AppCommandError(f"accent must be one of {', '.join(ACCENTS)} or #rrggbb.")
            look.accent = a
            parts.append(f"Accent → {a}")
        if not parts:
            raise AppCommandError("Nothing to change. Pass font_size, chat_font_size, theme, density, accent, ….")
        A.save(self.db, look)
        A.apply(look)
        return self._record(sid, "; ".join(parts), {"kind": "settings", "values": before, "area": "appearance"},
                            "appearance")

    # ---- schedules
    def _project_id(self, sid, project: str) -> int:
        projects = [p for p in self.db.projects()]
        if project:
            p = next((x for x in projects if x.name.lower() == project.lower()), None) or next(
                (x for x in projects if project.lower() in (x.name.lower(), x.path.lower())
                 or project.lower() in x.name.lower()), None)
            if not p:
                raise AppCommandError(f"No project {project!r}. Projects: {', '.join(x.name for x in projects)}.")
            return p.id
        s = self.db.session(sid) if sid else None
        if s and s.project_id:
            return s.project_id
        mine = [p for p in projects if not p.auto]
        if len(mine) == 1:
            return mine[0].id
        raise AppCommandError("Which project? Pass project (a project name).")

    def op_create_schedule(self, sid, prompt: str, when: str = "", repeat: str = "", project: str = "",
                           model: str = "", effort: str = "", target: str = "chat", name: str = "",
                           permission: str = "read-only") -> dict[str, Any]:
        from orchestrator.core.scheduler import describe, plain_when

        prompt = (prompt or "").strip()
        if not prompt:
            raise AppCommandError("prompt is empty: what should run?")
        target = "orchestration" if (target or "").lower().startswith("orch") else "chat"
        if permission not in ("read-only", "allow-edits"):
            permission = "allow-edits" if "edit" in (permission or "") or "write" in (permission or "") else "read-only"
        pid = self._project_id(sid, project)
        if model:
            eng, mid = resolve_model(self.db, model)
        else:
            s = self.db.session(sid) if sid else None
            if s and s.model and model_registry.is_allowed(self.db, s.engine, s.model, strict=True):
                eng, mid = s.engine, s.model
                effort = effort or s.effort or ""  # same settings as this chat
            else:
                first = model_registry.first_available(self.db, "claude")
                if not first:
                    raise AppCommandError("No models are enabled.")
                eng, mid = first[0], first[1]
        efforts = next((m.efforts for m in self.db.models(eng) if m.id == mid), [])
        effort = (effort or "").lower() or (efforts[min(1, len(efforts) - 1)] if efforts else "")
        if repeat:
            kind, cron, run_at = "cron", parse_repeat(repeat), None
        else:
            kind, cron, run_at = "once", None, parse_when(when or "in 5 hours").timestamp()
        data = {"name": (name or " ".join(prompt.split())[:40]), "project_id": pid, "prompt": prompt, "engine": eng,
                "model": mid, "effort": effort, "kind": kind, "cron": cron, "run_at": run_at,
                "new_or_resume": "new", "session_id": None, "permission": permission, "missed": "run_once",
                "enabled": True, "target": target, "options": {"engine": "auto"} if target == "orchestration" else None}
        new_id = self.db.save_schedule(data)
        if self.runner is not None:
            self.runner.sync(self.db.schedule(new_id))
        when_txt = plain_when(datetime.fromtimestamp(run_at)) if run_at else describe(self.db.schedule(new_id))
        label = "Run once" if kind == "once" else "Repeat"
        summary = (f"{label} {'orchestration' if target == 'orchestration' else 'chat'} “{data['name']}” — "
                   f"{when_txt} · {model_registry.label(self.db, eng, mid)} · {effort}")
        res = self._record(sid, summary, {"kind": "schedule_created", "id": new_id, "area": "schedules"}, "schedules")
        res["schedule_id"] = new_id
        return res

    def op_list_schedules(self, sid) -> dict[str, Any]:
        from orchestrator.core.scheduler import describe

        out = []
        for s in self.db.schedules():
            nxt = self.runner.next_run(s.id) if self.runner is not None else None
            out.append({"id": s.id, "name": s.name, "when": describe(s), "enabled": s.enabled, "target": s.target,
                        "next_run": nxt.isoformat(timespec="minutes") if nxt else None, "model": s.model})
        return {"schedules": out}

    async def op_cancel_schedule(self, sid, schedule_id: Any = None, name: str = "") -> dict[str, Any]:
        s = None
        if schedule_id:
            s = self.db.schedule(int(schedule_id))
        elif name:
            hits = [x for x in self.db.schedules() if name.lower() in x.name.lower()]
            if len(hits) > 1:
                raise AppCommandError("Several schedules match: " + ", ".join(f"{x.id}: {x.name}" for x in hits))
            s = hits[0] if hits else None
        if not s:
            raise AppCommandError("No such schedule. Use list_schedules.")
        if not await self._confirm(sid, f"Delete the schedule “{s.name}”?"):
            return {"applied": None, "denied": True, "note": "The user declined (or did not answer)."}
        from orchestrator.storage.db import SCHEDULE_FIELDS

        data = {k: getattr(s, k, None) for k in SCHEDULE_FIELDS if k != "options_json"}
        data["options"] = getattr(s, "options", None)
        if self.runner is not None:
            self.runner.remove(s.id)
        self.db.delete_schedule(s.id)
        return self._record(sid, f"Deleted schedule “{s.name}”", {"kind": "schedule_deleted", "data": data,
                                                                  "area": "schedules"}, "schedules")

    # ---- about the app, usage and models
    def op_about_app(self, sid) -> dict[str, Any]:
        from orchestrator.storage.db import app_dir

        src = app_source()
        s = self.db.session(sid) if sid else None
        proj = self.db.project(s.project_id) if s else None
        return {
            "app": "Orchestrator", "about": APP_ABOUT,
            "source_folder": str(src) if src else "unknown (running from a build without its source next to it)",
            "version": _git_commit(src) if src else "",
            "data_folder": str(app_dir()), "database": str(self.db.path),
            "this_chat": ({"id": s.id, "title": s.title, "engine": s.engine, "model": s.model, "effort": s.effort,
                           "project": proj.name if proj else "", "folder": s.cwd or (proj.path if proj else "")}
                          if s else None),
            "usage_sources": USAGE_SOURCES,
            "tools": "app tools: about_app, get_usage, list_models, set_default_model, list_preferences, "
                     "set_model_preference, list_settings, set_setting, set_appearance, create_schedule, "
                     "list_schedules, cancel_schedule. The planner also has the orchestrator tools (delegate, "
                     "wait_for, ...).",
        }

    def op_get_usage(self, sid, days: Any = 7) -> dict[str, Any]:
        from orchestrator.core import usage as U

        now = time.time()
        plans = []
        for pu in U.read_all(self.db):
            plans.append({"provider": pu.provider, "name": pu.name, "source": pu.source, "note": pu.note,
                          "windows": [{"window": w.label, "percent_left": None if w.remaining_pct is None
                                       else round(w.remaining_pct), "resets": _when(w.resets_at, now),
                                       "resets_at": datetime.fromtimestamp(w.resets_at).isoformat(timespec="minutes")
                                       if w.resets_at else "", "exact": w.exact, "estimate": w.estimate,
                                       "status": w.status} for w in pu.windows]})
        try:
            days = max(1, min(90, int(days)))
        except (TypeError, ValueError):
            days = 7
        since = now - days * 86400
        per: dict[str, dict[str, Any]] = {}
        rows = self.db.conn.execute(
            "SELECT s.engine, s.model, m.content_json FROM messages m JOIN sessions s ON s.id=m.session_id "
            "WHERE m.kind='usage' AND m.ts>=?", (since,))
        import json as _json

        for eng, model, cj in rows:
            u = _json.loads(cj)
            key = f"{eng}:{model or 'default'}"
            d = per.setdefault(key, {"engine": eng, "model": model or "default", "turns": 0, "input": 0,
                                     "cached": 0, "output": 0, "cost_usd": 0.0})
            d["turns"] += 1
            for k in ("input", "cached", "output"):
                d[k] += int(u.get(k) or 0)
            d["cost_usd"] += float(u.get("cost") or 0)
        for d in per.values():
            d["cost_usd"] = round(d["cost_usd"], 4)
        return {"plan_limits": plans, f"spend_last_{days}_days": sorted(per.values(), key=lambda d: -d["cost_usd"]),
                "how_it_is_read": USAGE_SOURCES,
                "note": "Plan limits are what the title-bar pills show. Spend covers chats run from this app only."}

    def op_list_models(self, sid) -> dict[str, Any]:
        out = {}
        for eng in ENGINES:
            dm, de = self.db.default_model(eng)
            out[eng] = {"default_for_new_chats": {"model": dm, "effort": de},
                        "models": [{"id": m.id, "label": m.label, "efforts": m.efforts, "enabled": m.enabled}
                                   for m in self.db.models(eng)]}
        return {"engines": out, "preference_rules": self.op_list_preferences(sid)["rules"],
                "note": "Preference rules decide which model the orchestrator picks for a kind of task "
                        "(set_model_preference). The default is what a new chat starts on (set_default_model)."}

    def op_set_default_model(self, sid, model: str, effort: str = "", engine: str = "") -> dict[str, Any]:
        eng, mid = resolve_model(self.db, model, engine)
        m = next(x for x in self.db.models(eng) if x.id == mid)
        effort = (effort or "").strip().lower() or self.db.default_model(eng)[1]
        if m.efforts and effort not in m.efforts:
            effort = "medium" if "medium" in m.efforts else m.efforts[0]
        key = f"default.{eng}"
        before = {key: self.db.get_setting(key), "last": self.db.get_setting("last")}
        self.db.set_setting(key, [mid, effort])
        self.db.set_setting("last", [eng, mid, effort])
        return self._record(sid, f"New chats start on {m.label} · {effort}",
                            {"kind": "settings", "values": before, "area": "models"}, "models")

    # ---- safe settings + models
    def op_list_settings(self, sid) -> dict[str, Any]:
        out = {name: {"label": label, "value": self.db.get_setting(key), "allowed": allowed if allowed else "bool"
                      if typ is bool else "int" if typ is int else "text"}
               for name, (key, label, typ, allowed) in SAFE_SETTINGS.items()}
        out["model_enabled"] = {"label": "Enable or disable a model (value: true/false, model: name)",
                                "models": {m.id: m.enabled for m in self.db.models()}}
        return out

    async def op_set_setting(self, sid, name: str, value: Any = None, model: str = "") -> dict[str, Any]:
        name = (name or "").strip().lower()
        if name == "model_enabled":
            eng, mid = resolve_model(self.db, model, need_enabled=False)
            on = _bool(value)
            m = next(x for x in self.db.models(eng) if x.id == mid)
            if m.enabled == on:
                return {"applied": None, "note": f"{m.label} is already {'enabled' if on else 'disabled'}."}
            if not on and not await self._confirm(sid, f"Disable {m.label}? It won't be used anywhere until "
                                                        "you turn it back on."):
                return {"applied": None, "denied": True, "note": "The user declined (or did not answer)."}
            self.db.set_model_enabled(eng, mid, on)
            return self._record(sid, f"{'Enabled' if on else 'Disabled'} {m.label}",
                                {"kind": "model_enabled", "engine": eng, "model": mid, "was": not on,
                                 "area": "models"}, "models")
        if name not in SAFE_SETTINGS:
            raise AppCommandError(f"{name!r} can't be changed from chat. Allowed: {', '.join(SAFE_SETTINGS)}, "
                                  "model_enabled.")
        key, label, typ, allowed = SAFE_SETTINGS[name]
        if typ is bool:
            v: Any = _bool(value)
        elif typ is int:
            try:
                v = int(value)
            except (TypeError, ValueError):
                raise AppCommandError(f"{name} needs a whole number.") from None
            lo, hi = allowed
            if not lo <= v <= hi:
                raise AppCommandError(f"{name} must be between {lo} and {hi}.")
        else:
            v = str(value).strip()
            if allowed and v not in allowed:
                raise AppCommandError(f"{name} must be one of: {', '.join(allowed)}.")
        before = self.db.get_setting(key)
        self.db.set_setting(key, v)
        if key == "global.max_parallel":
            from orchestrator.core import concurrency

            concurrency.slot_freed()
        shown = "on" if v is True else "off" if v is False else v
        return self._record(sid, f"{label} → {shown}", {"kind": "settings", "values": {key: before},
                                                       "area": "settings"}, "settings")


# ---------------------------------------------------------------- what the app is
APP_ABOUT = (
    "Orchestrator is a Windows desktop app (Python, PySide6 and PyQt-Fluent-Widgets) that runs Claude Code and Codex "
    "chats side by side. Pages: Projects (folders), Chats (one agent per chat, any engine/model/effort), Orchestrate "
    "(a planner chat splits a goal into tasks and runs Claude/Codex sub-agents, then reports back), Board (task "
    "kanban), Waiting on you (questions and reviews), Schedules (run once later / repeat), MCP servers, Settings "
    "(models, model preference rules, limits, appearance). The title bar shows plan usage pills for Claude and Codex "
    "(percent left in the 5-hour and weekly windows, with reset times); the Usage page has their history. Chats are "
    "also read from the engines' own logs (~/.claude/projects, ~/.codex/sessions)."
)
USAGE_SOURCES = {
    "claude": "Claude: rate-limit events the Claude Code SDK reports on each turn (utilization and reset time); "
              "between turns an estimate from the token counts in ~/.claude/projects logs.",
    "codex": "Codex: the rate_limits block in the newest ~/.codex/sessions rollout files (used_percent, resets_in).",
}


def _git_commit(root) -> str:
    try:
        head = (root / ".git" / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            ref = head.split(" ", 1)[1].strip()
            f = root / ".git" / ref
            if f.exists():
                return f.read_text(encoding="utf-8").strip()[:7]
            packed = root / ".git" / "packed-refs"
            for line in packed.read_text(encoding="utf-8").splitlines() if packed.exists() else []:
                if line.endswith(" " + ref):
                    return line[:7]
            return ""
        return head[:7]
    except OSError:
        return ""


def app_source():
    """The app's source folder (the git checkout it was built from), or None."""
    import sys
    from pathlib import Path

    from orchestrator.core.orchestration import APP_ROOT

    cands = [APP_ROOT]
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        cands += [exe.parent, *exe.parents]
    for c in cands:
        if (c / "orchestrator" / "core" / "app_control.py").exists():
            return c
    return None


def _when(ts: float | None, now: float) -> str:
    if not ts:
        return ""
    d = ts - now
    if d <= 0:
        return "now"
    h, m = int(d // 3600), int(d % 3600 // 60)
    return (f"in {h // 24} d {h % 24} h" if h >= 24 else f"in {h} h {m} min" if h else f"in {m} min")


# ---------------------------------------------------------------- local fast path

_FONT = r"(?:the\s+)?(?:(ui|app|chat|code|message|messages)\s+)?(?:font|text)(?:\s+size)?"
_FAST = [
    (re.compile(rf"^(?:please\s+)?(?:increase|bigger|enlarge|raise)\s+{_FONT}(?:\s+in\s+(?:this|the)\s+app)?"
                rf"(?:\s+(?:size|a bit|please))*$", re.I), "bigger"),
    (re.compile(rf"^(?:please\s+)?make\s+{_FONT}\s+(?:bigger|larger)(?:\s+please)?$", re.I), "bigger"),
    (re.compile(rf"^(?:please\s+)?(?:decrease|reduce|shrink|lower)\s+{_FONT}(?:\s+in\s+(?:this|the)\s+app)?"
                rf"(?:\s+(?:size|a bit|please))*$", re.I), "smaller"),
    (re.compile(rf"^(?:please\s+)?make\s+{_FONT}\s+smaller(?:\s+please)?$", re.I), "smaller"),
]
_SET_SIZE = re.compile(rf"^(?:please\s+)?set\s+{_FONT}\s+(?:to\s+)?(\d+(?:\.\d+)?)\s*(?:pt|points?)?$", re.I)
_THEME = re.compile(r"^(?:please\s+)?(?:switch\s+to|use|turn\s+on|enable)\s+(dark|light)\s+(?:mode|theme)$|"
                    r"^(dark|light)\s+(?:mode|theme)(?:\s+please)?$", re.I)
_DENSITY = re.compile(r"^(?:please\s+)?(?:make\s+it|use|switch\s+to)\s+(compact|comfortable)(?:\s+(?:mode|density))?$",
                      re.I)


def detect(text: str) -> tuple[str, dict[str, Any]] | None:
    """A whole-message simple command handled locally (no model call): (op, args), else None."""
    t = " ".join((text or "").strip().rstrip(".!").split())
    if not t or len(t) > 80:
        return None

    def which(group: str | None) -> str:
        g = (group or "").lower()
        return "code_font_size" if g == "code" else "chat_font_size" if g in ("chat", "message", "messages") \
            else "both"

    for rx, val in _FAST:
        m = rx.match(t)
        if m:
            w = which(m.group(1))
            args = {"font_size": val, "chat_font_size": val} if w == "both" else {w: val}
            return "set_appearance", args
    m = _SET_SIZE.match(t)
    if m:
        w = which(m.group(1))
        return "set_appearance", ({"font_size": m.group(2)} if w == "both" else {w: m.group(2)})
    m = _THEME.match(t)
    if m:
        return "set_appearance", {"theme": (m.group(1) or m.group(2)).lower()}
    m = _DENSITY.match(t)
    if m:
        return "set_appearance", {"density": m.group(1).lower()}
    return None
