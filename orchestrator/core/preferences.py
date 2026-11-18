"""Model preference rules: task type / keywords -> engine + model + effort, in priority order.

Rules live in the setting ``routing.rules`` and are edited in Settings › Orchestrator. The router (Auto picks for
sub-agents and the planner) checks them before its built-in difficulty heuristics, and the planner prompt lists
them first. Only rules whose model is enabled are used; the others are shown as inactive.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from orchestrator.core import model_registry
from orchestrator.core.catalog import ENGINES
from orchestrator.storage.db import Database

KEY = "routing.rules"
CAPS_KEY = "caps.image_generation"  # {"codex": bool, "checked_at": ts}


@dataclass
class Rule:
    name: str  # task type, e.g. "Complex coding"
    keywords: list[str]
    engine: str
    model: str
    effort: str = ""
    priority: int = 1  # 1 = checked first
    enabled: bool = True
    notes: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Rule":
        kw = d.get("keywords") or []
        if isinstance(kw, str):
            kw = split_keywords(kw)
        return Rule(name=str(d.get("name") or "").strip(), keywords=[str(k) for k in kw],
                    engine=str(d.get("engine") or "").strip().lower(), model=str(d.get("model") or "").strip(),
                    effort=str(d.get("effort") or "").strip(), priority=int(d.get("priority") if d.get("priority") is not None else 1),
                    enabled=bool(d.get("enabled", True)), notes=str(d.get("notes") or ""),
                    id=str(d.get("id") or uuid.uuid4().hex[:8]))


SEED: list[dict[str, Any]] = [
    {"name": "Complex coding", "priority": 1, "engine": "claude", "model": "claude-opus-5-5", "effort": "medium",
     "keywords": ["debug", "architecture", "design", "race condition", "concurrency", "root cause", "multi-file",
                  "investigate"],
     "notes": "Hard problems where judgement matters more than speed."},
    {"name": "Large refactors", "priority": 2, "engine": "codex", "model": "gpt-6.2-sol", "effort": "high",
     "keywords": ["refactor", "migrate", "rewrite", "port to", "scaffold", "end-to-end"],
     "notes": "Long autonomous coding across many files."},
    {"name": "Quick edits and summaries", "priority": 3, "engine": "claude", "model": "claude-haiku-5-5",
     "effort": "low", "keywords": ["typo", "rename", "summarize", "summary", "readme", "docstring", "changelog"],
     "notes": "Cheap and fast."},
]

IMAGE_WORDS = re.compile(r"\b(images?|pictures?|photos?|illustrations?|logos?|icons? art|artwork|drawings?|draw|"
                         r"paint\w*|sprites?|render (?:an? )?image|generate (?:an? )?(?:image|picture)|"
                         r"image generation|mockup images?)\b", re.I)


def split_keywords(text: str) -> list[str]:
    return [k.strip() for k in re.split(r"[,;\n]", text or "") if k.strip()]


def rules(db: Database) -> list[Rule]:
    raw = db.get_setting(KEY)
    if raw is None:
        raw = SEED
    out = [Rule.from_dict(d) for d in raw if isinstance(d, dict)]
    return sorted(out, key=lambda r: (r.priority, r.name.lower()))


def save_rules(db: Database, items: list[Rule]) -> None:
    db.set_setting(KEY, [asdict(r) for r in items])


def reset_rules(db: Database) -> None:
    db.set_setting(KEY, None)


def add_rule(db: Database, rule: Rule) -> Rule:
    items = rules(db)
    items.append(rule)
    save_rules(db, items)
    return rule


def usable(db: Database, r: Rule) -> bool:
    return r.enabled and r.engine in ENGINES and model_registry.is_allowed(db, r.engine, r.model, strict=True) \
        and bool(r.model)


def _kw_pattern(kw: str) -> re.Pattern:
    words = [re.escape(w) for w in kw.lower().split()]
    return re.compile(r"(?<![\w-])" + r"[\s-]+".join(words) + r"(?:s|es|ed|ing)?(?![\w-])", re.I)


def matched_keyword(r: Rule, text: str) -> str | None:
    t = text or ""
    if r.name and _kw_pattern(r.name).search(t):
        return r.name
    for kw in r.keywords:
        if kw and _kw_pattern(kw).search(t):
            return kw
    return None


def match(db: Database, text: str, engine: str = "") -> tuple[Rule, str] | None:
    """First usable rule (by priority) whose task type or a keyword appears in `text`."""
    for r in rules(db):
        if engine and r.engine != engine:
            continue
        if not usable(db, r):
            continue
        kw = matched_keyword(r, text)
        if kw:
            return r, kw
    return None


def describe(db: Database, r: Rule) -> str:
    eff = f" · {r.effort}" if r.effort else ""
    return f"{ENGINES.get(r.engine, r.engine)} {model_registry.label(db, r.engine, r.model)}{eff}"


def prompt_block(db: Database) -> str:
    """Rules for the planner prompt: enabled models only, priority order."""
    lines = []
    for r in rules(db):
        if not usable(db, r):
            continue
        kws = ", ".join(r.keywords[:8])
        note = f" ({r.notes})" if r.notes else ""
        lines.append(f"{r.priority}. {r.name} [{kws}] -> engine={r.engine} model={r.model} effort={r.effort or 'default'}"
                     f"{note}")
    if not lines:
        return ""
    return ("User preferences (apply these first, in this order; when a task matches, use that engine/model/effort "
            "and start `reason` with \"Preference: <task type>\"):\n" + "\n".join(lines))


# ------------------------------------------------------------------ image generation capability

def is_image_rule(r: Rule) -> bool:
    return bool(IMAGE_WORDS.search(" ".join([r.name, *r.keywords, r.notes])))


def codex_image_generation(db: Database) -> bool | None:
    """Cached: is Codex's built-in image_generation feature on? None until checked."""
    return (db.get_setting(CAPS_KEY) or {}).get("codex")


def caps_stale(db: Database, max_age: float = 86400) -> bool:
    got = db.get_setting(CAPS_KEY) or {}
    return time.time() - float(got.get("checked_at") or 0) > max_age


def probe_codex_image_generation() -> bool | None:
    """Runs `codex features list` (a few seconds; call off the GUI thread)."""
    exe = shutil.which("codex")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "features", "list"], capture_output=True, text=True, timeout=20,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32"
                             else 0).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        parts = line.split()
        if parts and parts[0] == "image_generation":
            return parts[-1].lower() == "true"
    return False


def store_codex_caps(db: Database, on: bool | None) -> None:
    if on is not None:
        db.set_setting(CAPS_KEY, {"codex": on, "checked_at": time.time()})


def image_warning(db: Database, r: Rule) -> str:
    """Why this image rule won't produce images, or ''. Claude Code has no image generation; Codex has a built-in
    image_generation tool when that feature is on."""
    if not is_image_rule(r):
        return ""
    codex_ok = codex_image_generation(db)
    codex_models = bool(model_registry.allowed(db, "codex"))
    if r.engine == "claude":
        alt = " Point this rule at a Codex model instead." if codex_ok and codex_models else ""
        return "Claude Code can't generate images; it would write code (for example SVG) instead." + alt
    if r.engine == "codex":
        if not codex_models:
            return "No Codex model is enabled, so no enabled model can generate images."
        if codex_ok is False:
            return "Codex's image_generation feature is off (codex features list), so no enabled model can " \
                   "generate images."
    return ""


# ------------------------------------------------------------------ "prefer X for Y" said in chat

_PREFER = re.compile(
    r"\b(?:i\s+)?(?:prefer|always use|use|pick|go with)\s+(?P<model>[\w .\-]+?)\s+(?:for|when (?:doing|it'?s))\s+"
    r"(?P<task>[^.,;!?\n]{3,60})", re.I)
_EFFORT = re.compile(r"\b(low|medium|high|xhigh|max|ultra)\s+effort\b|\beffort\s+(low|medium|high|xhigh|max|ultra)\b",
                     re.I)


def _find_model(db: Database, phrase: str) -> tuple[str, str] | None:
    p = re.sub(r"\s+", " ", phrase.lower()).strip()
    best = None
    for m in db.models():
        names = {m.id.lower(), m.label.lower(), m.label.lower().replace(" ", "-")}
        short = m.label.lower().split()[0]  # "opus", "sonnet", "gpt-6.2"
        for n in names:
            if n and n in p:
                cand = (len(n), m.engine, m.id)
                best = max(best, cand) if best else cand
        if short and re.search(rf"\b{re.escape(short)}\b", p):
            cand = (len(short), m.engine, m.id)
            best = best if best and best[0] >= cand[0] else cand
    return (best[1], best[2]) if best else None


def parse_preference(db: Database, text: str) -> Rule | None:
    """A draft rule from 'prefer Opus 5.5 for complex coding' (or 'use GPT-6.2 Sol for images'), else None."""
    for m in _PREFER.finditer(text or ""):
        found = _find_model(db, m.group("model"))
        if not found:
            continue
        engine, model = found
        task = m.group("task").strip()
        task = re.sub(r"\s+(?:tasks?|work|jobs?|stuff|please)$", "", task, flags=re.I).strip()
        eff = _EFFORT.search(text)
        effort = (eff.group(1) or eff.group(2)).lower() if eff else ""
        name = task[:1].upper() + task[1:]
        kws = [w for w in re.split(r"\s+(?:and|or)\s+|,", task) if w.strip()]
        return Rule(name=name, keywords=[k.strip().lower() for k in kws], engine=engine, model=model, effort=effort,
                    priority=1, notes="Saved from chat")
    return None
