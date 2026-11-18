"""Auto model/effort choice for sub-agents and the planner, from enabled models only.

Settings › Models order is the strength order per engine: strongest first, cheapest last.
Every pick comes with a one-line reason shown on the task card.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from orchestrator.core import model_registry
from orchestrator.core.catalog import ENGINES
from orchestrator.storage.db import Database, Model

DEFAULT_GUIDE = """- Quick lookups, summaries, tiny edits (typos, renames, docs): cheapest model, low effort.
- Small, clear code changes and simple tests: mid model, medium effort.
- Hard debugging, architecture, unclear multi-file problems: strongest Claude model, high effort.
- Large refactors and long autonomous coding: strongest Codex model, high effort.
- If a cheap pick fails, the app retries once with more effort or a stronger model."""

HARD = re.compile(r"\b(debug|race|deadlock|architect\w*|design|investigat\w*|root cause|performance|profil\w*|"
                  r"security|vulnerab\w*|concurren\w*|flaky|intermittent|why does|unclear)\b", re.I)
BIG_CODE = re.compile(r"\b(refactor\w*|rewrite|migrat\w*|port (?:to|from)|implement\w*|build|scaffold|"
                      r"end[- ]to[- ]end|whole|entire|across the (?:code|repo))\b", re.I)
EASY = re.compile(r"\b(typo|rename|docstring|comment|readme|format\w*|lint|look ?up|list|summar\w*|"
                  r"find|count|small|tiny|one[- ]line|bump|version|create (?:a |an )?(?:small |empty )?file)\b", re.I)


CHEAP = re.compile(r"haiku|mini|nano|lite|luna|flash|small", re.I)


def strength_order(models: list[Model]) -> list[Model]:
    """Settings order (strongest first), with known small/cheap models moved to the end."""
    return [m for m in models if not CHEAP.search(m.id)] + [m for m in models if CHEAP.search(m.id)]


@dataclass
class Pick:
    engine: str
    model: str
    effort: str
    reason: str


def guide(db: Database) -> str:
    return db.get_setting("routing.guide") or DEFAULT_GUIDE


def planner_guide(db: Database) -> str:
    """What the planner prompt shows: the user's preference rules first, then the routing guide."""
    from orchestrator.core import preferences

    block = preferences.prompt_block(db)
    return (block + "\n\nOtherwise:\n" if block else "") + guide(db)


def preferred(db: Database, text: str, engine: str = "") -> Pick | None:
    """A Pick from the first matching preference rule (enabled models only), or None."""
    from orchestrator.core import preferences

    hit = preferences.match(db, text, engine)
    if not hit:
        return None
    r, kw = hit
    m = next(x for x in model_registry.allowed(db, r.engine) if x.id == r.model)
    eff = _effort(m, r.effort) if r.effort else _effort(m, "medium")
    return Pick(r.engine, m.id, eff, f"Preference: {r.name} → {m.label} · {eff} (matched “{kw}”)")


def difficulty(text: str) -> tuple[str, str]:
    """('easy'|'medium'|'hard', short why) from the task text."""
    t = text or ""
    hard, big, easy = HARD.findall(t), BIG_CODE.findall(t), EASY.findall(t)
    long_ = len(t) > 1500
    if hard:
        return "hard", f"{hard[0].lower()}-type task"
    if big and (long_ or len(big) >= 2):
        return "hard", "large coding job"
    if easy and not big and len(t) < 600:
        return "easy", f"small task ({easy[0].lower()})"
    if big:
        return "medium", f"coding task ({big[0].lower()})"
    return ("medium", "long task") if long_ else ("medium", "ordinary task")


def _effort(m: Model, want: str) -> str:
    if want in m.efforts:
        return want
    order = ["low", "medium", "high", "xhigh", "max", "ultra"]
    i = order.index(want) if want in order else 1
    for e in sorted(m.efforts, key=lambda e: abs((order.index(e) if e in order else 1) - i)):
        return e
    return ""


def _tier(models: list[Model], level: str) -> Model:
    if level == "hard":
        return models[0]
    if level == "easy":
        return models[-1]
    return models[len(models) // 2] if len(models) > 2 else models[0] if len(models) == 1 else models[-2]


def route(db: Database, text: str, engine: str = "", prefer: str = "") -> Pick | None:
    """Pick engine/model/effort for a task. `engine` forces the engine; `prefer` breaks ties.
    The user's preference rules (Settings › Orchestrator) come first."""
    pref = preferred(db, text, engine)
    if pref:
        return pref
    level, why = difficulty(text)
    avail = model_registry.available_engines(db)
    if not avail:
        return None
    if engine:
        if engine not in avail:
            return None
        eng = engine
    else:
        big = bool(BIG_CODE.search(text or "")) and level == "hard" and not HARD.search(text or "")
        wanted = "codex" if big else ("claude" if level == "hard" else (prefer or avail[0]))
        eng = wanted if wanted in avail else (prefer if prefer in avail else avail[0])
    models = strength_order(model_registry.allowed(db, eng))
    m = _tier(models, level)
    eff = _effort(m, {"easy": "low", "medium": "medium", "hard": "high"}[level])
    return Pick(eng, m.id, eff, f"Auto: {m.label} · {eff} — {why}")


def route_planner(db: Database, goal: str, prefer: str = "") -> Pick | None:
    """Planner pick for Auto: a matching preference rule, else the strongest model of the fitting engine
    (high effort for hard goals)."""
    pref = preferred(db, goal)
    if pref:
        pref.reason = pref.reason.replace("Preference:", "Preference (planner):", 1)
        return pref
    level, why = difficulty(goal)
    avail = model_registry.available_engines(db)
    if not avail:
        return None
    big = bool(BIG_CODE.search(goal or "")) and not HARD.search(goal or "")
    wanted = "codex" if big and level == "hard" else ("claude" if "claude" in avail else avail[0])
    eng = wanted if wanted in avail else (prefer if prefer in avail else avail[0])
    m = strength_order(model_registry.allowed(db, eng))[0]
    eff = _effort(m, "high" if level == "hard" else "medium")
    return Pick(eng, m.id, eff, f"Auto planner: {ENGINES[eng]} {m.label} · {eff} — {why}")


def escalate(db: Database, engine: str, model: str, effort: str) -> Pick | None:
    """One step up: more effort on the same model, else the next stronger enabled model. None at the top."""
    models = strength_order(model_registry.allowed(db, engine))
    cur = next((m for m in models if m.id == model), None)
    order = ["low", "medium", "high", "xhigh", "max", "ultra"]
    rank = {e: i for i, e in enumerate(order)}
    if cur and effort in cur.efforts and rank.get(effort, 9) < rank["high"]:
        # Below high: step effort up on the same model first (cheaper than a bigger model).
        nxt = next((e for e in cur.efforts if rank.get(e, 9) > rank[effort]), None)
        if nxt:
            return Pick(engine, model, nxt, f"Escalated after failure: {cur.label} {effort} → {nxt}")
    idx = models.index(cur) if cur in models else len(models)
    if idx > 0:
        stronger = models[idx - 1]
        eff = _effort(stronger, "high")
        return Pick(engine, stronger.id, eff,
                    f"Escalated after failure: {cur.label if cur else model} → {stronger.label} · {eff}")
    return None
