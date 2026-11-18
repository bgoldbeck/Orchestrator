"""The one place that decides which models may be used.

Models switched off in Settings › Models are excluded everywhere: pickers, chats,
schedules and the orchestrator all ask this module, never the raw table.
"""
from __future__ import annotations

from orchestrator.core.catalog import ENGINES
from orchestrator.storage.db import Database, Model


class ModelDisabled(Exception):
    """Raised when a disabled model (or an engine with no enabled models) is requested."""


def allowed(db: Database, engine: str) -> list[Model]:
    """Enabled models for `engine`, in Settings order."""
    return [m for m in db.models(engine) if m.enabled]


def is_allowed(db: Database, engine: str, model: str, strict: bool = False) -> bool:
    """May `model` run on `engine`? A model listed in Settings must be enabled. A model id that is not
    listed at all (e.g. recorded in an imported engine log) is allowed only when not `strict`."""
    if not engine_available(db, engine):
        return False
    if not model:
        return True
    listed = {m.id: m.enabled for m in db.models(engine)}
    if model in listed:
        return listed[model]
    return not strict


def engine_available(db: Database, engine: str) -> bool:
    return bool(allowed(db, engine))


def available_engines(db: Database) -> list[str]:
    return [e for e in ENGINES if engine_available(db, e)]


def unavailable_message(engine: str) -> str:
    return f"{ENGINES.get(engine, engine)} has no enabled models. Turn one on in Settings › Models."


def disabled_message(db: Database, engine: str, model: str) -> str:
    if not engine_available(db, engine):
        return unavailable_message(engine)
    label = next((m.label for m in db.models(engine) if m.id == model), model)
    return f"{label} is disabled in Settings › Models. Pick another model."


def label(db: Database, engine: str, model: str) -> str:
    return next((m.label for m in db.models(engine) if m.id == model), model or "default")


def default(db: Database, engine: str) -> tuple[str, str] | None:
    """The engine's default (model, effort) if allowed, else its first allowed model; None if none."""
    ms = allowed(db, engine)
    if not ms:
        return None
    model, effort = db.default_model(engine)
    m = next((x for x in ms if x.id == model), None)
    if m is None:
        m = ms[0]
        effort = effort if effort in m.efforts else (m.efforts[min(1, len(m.efforts) - 1)] if m.efforts else "")
    return m.id, effort


def first_available(db: Database, prefer: str = "codex") -> tuple[str, str, str] | None:
    """(engine, model, effort) to start with, preferring `prefer`; None if every engine is off."""
    for e in [prefer, *[x for x in ENGINES if x != prefer]]:
        d = default(db, e)
        if d:
            return e, d[0], d[1]
    return None


def pick(db: Database, engine: str, model: str, effort: str) -> tuple[str, str, str] | None:
    """Starting choice for a new picker: (engine, model, effort) if allowed, else that engine's allowed
    default, else any available engine. None when no engine has an enabled model."""
    if engine in ENGINES and is_allowed(db, engine, model, strict=True) and model:
        return engine, model, effort
    d = default(db, engine) if engine in ENGINES else None
    if d:
        return engine, d[0], d[1]
    return first_available(db, engine if engine in ENGINES else "codex")


def check(db: Database, engine: str, model: str, strict: bool = False) -> None:
    """Raise ModelDisabled with a clear message unless engine/model may be used."""
    if engine not in ENGINES:
        raise ModelDisabled(f"Unknown engine {engine!r}. Use one of: {', '.join(ENGINES)}.")
    if not is_allowed(db, engine, model, strict):
        if model and engine_available(db, engine) and model not in {m.id for m in db.models(engine)}:
            ids = ", ".join(m.id for m in allowed(db, engine))
            raise ModelDisabled(f"Unknown model {model!r} for {ENGINES[engine]}. Allowed: {ids}.")
        raise ModelDisabled(disabled_message(db, engine, model))
