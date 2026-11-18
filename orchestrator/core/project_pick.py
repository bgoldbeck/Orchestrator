"""Pick the project (folder) a goal is about: Auto mode for orchestrations and the find_projects tool.

Scores each project by how well the goal's words match its name (strongest), its folder path, and the titles,
summaries and keywords of its recent chats. Confident only when the best match is clear; otherwise the caller
asks the user with the top few candidates.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

from orchestrator.core.summaries import tokens
from orchestrator.storage.db import Database, Project

MIN_SCORE = 3.0  # below this nothing really matched
MIN_MARGIN = 1.5  # best must beat the runner-up by this much (and by 30%)


@dataclass
class ProjectMatch:
    project: Project
    score: float
    reason: str


def _words(text: str) -> set[str]:
    parts = re.split(r"[\s_\-./\\]+", text or "")
    out = set(tokens(" ".join(parts)))
    for p in parts:  # camelCase / joined names: WeatherApp -> weather, app
        out |= {w.lower() for w in re.findall(r"[A-Z]?[a-z]{3,}", p)}
    return out


def _variants(w: str) -> set[str]:
    out = {w}
    if w.endswith("s") and len(w) > 4:
        out.add(w[:-1])
    return out


def rank_projects(db: Database, goal: str, limit: int = 5) -> list[ProjectMatch]:
    goal_words = set()
    for w in tokens(goal):
        goal_words |= _variants(w)
    goal_low = (goal or "").lower()
    out: list[ProjectMatch] = []
    for p in db.projects():
        score = 0.0
        why: list[str] = []
        name_words = _words(p.name)
        if p.name.lower() in goal_low and len(p.name) > 2:
            score += 6
            why.append(f"the goal names “{p.name}”")
        else:
            hit = sorted(goal_words & name_words)
            if hit:
                score += 3 * len(hit)
                why.append("name matches " + ", ".join(hit))
        path_words = _words(os.path.dirname(p.path)) - name_words
        hit = sorted(goal_words & path_words)
        if hit:
            score += 1 * len(hit)
            why.append("folder path has " + ", ".join(hit))
        recent = db.sessions(p.id)[:20]
        seen: dict[str, int] = {}
        for s in recent:
            text = " ".join(x for x in (s.title, s.summary or "", getattr(s, "keywords", "") or "") if x)
            for w in goal_words & _words(text):
                seen[w] = seen.get(w, 0) + 1
        if seen:
            score += min(6.0, sum(1.5 + 0.25 * (min(c, 3) - 1) for c in seen.values()))
            top = sorted(seen, key=lambda w: -seen[w])[:3]
            why.append("recent chats mention " + ", ".join(top))
        if p.auto:
            score *= 0.8  # folders only discovered in engine history rank a bit lower
        if score > 0:
            out.append(ProjectMatch(p, round(score, 2), "; ".join(why)))
    out.sort(key=lambda m: (-m.score, m.project.name.lower()))
    return out[:limit]


def confident(matches: list[ProjectMatch]) -> ProjectMatch | None:
    """The best match if it is clearly the one; None means ask the user."""
    if not matches or matches[0].score < MIN_SCORE:
        return None
    if len(matches) > 1:
        second = matches[1].score
        if matches[0].score - second < MIN_MARGIN or matches[0].score < second * 1.3:
            return None
    return matches[0]


def find_projects(db: Database, query: str) -> dict:
    """Result for the orchestrator's find_projects tool."""
    ms = rank_projects(db, query)
    best = confident(ms)
    return {"projects": [{"name": m.project.name, "path": m.project.path, "score": m.score, "why": m.reason}
                         for m in ms],
            "confident": bool(best),
            "note": ("Pass a project's name as `project` to delegate to run a task in that folder."
                     if ms else "No known project matches. Ask the user with ask_user if the folder is unclear.")}
