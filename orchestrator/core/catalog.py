"""Seed model catalog. Editable in Settings › Models (stored in SQLite)."""
from __future__ import annotations

ENGINES = {"claude": "Claude", "codex": "Codex"}

SEED_MODELS: list[tuple[str, str, str, list[str]]] = [
    # engine, model id, label, efforts
    ("claude", "claude-opus-5-5", "Opus 5.5", ["low", "medium", "high", "xhigh", "max"]),
    ("claude", "claude-sonnet-5-5", "Sonnet 5.5", ["low", "medium", "high", "xhigh", "max"]),
    ("claude", "claude-haiku-5-5", "Haiku 5.5", ["low", "medium", "high"]),
    ("claude", "claude-fable-5-2", "Fable 5.2", ["low", "medium", "high", "xhigh", "max"]),
    ("codex", "gpt-6.2-sol", "GPT-6.2 Sol", ["low", "medium", "high", "xhigh", "max", "ultra"]),
    ("codex", "gpt-6.2-astra", "GPT-6.2 Astra", ["low", "medium", "high", "xhigh", "max", "ultra"]),
    ("codex", "gpt-6.1-sol", "GPT-6.1 Sol", ["low", "medium", "high", "xhigh", "max", "ultra"]),
    ("codex", "gpt-6.1-terra", "GPT-6.1 Terra", ["low", "medium", "high", "xhigh", "max", "ultra"]),
    ("codex", "gpt-6-luna", "GPT-6 Luna", ["low", "medium", "high", "xhigh", "max"]),
]

DEFAULTS = {"claude": ("claude-sonnet-5-5", "medium"), "codex": ("gpt-6.2-sol", "medium")}
