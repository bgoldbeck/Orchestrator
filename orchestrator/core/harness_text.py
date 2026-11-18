"""Strip agent-harness markup from transcript text.

Engine logs (especially imported Claude Code / Codex sessions) carry text the harness injected around the
user's words: ``<system-reminder>`` blocks, ``<command-name>`` wrappers, ``<wake ...><message>…</message></wake>``
envelopes and so on. None of it is something the person typed, so it is removed before rendering and before
it can reach titles, summaries or search. The person's own words inside a ``<message>`` envelope are kept.
"""
from __future__ import annotations

import html
import re

# Blocks dropped entirely (content included).
DROP_TAGS = (
    "system-reminder", "system-note", "command-name", "command-message", "command-args",
    "command-contents", "local-command-stdout", "local-command-stderr", "local-command-caveat",
    "user-prompt-submit-hook", "task-notification", "environment_context", "user_instructions",
    "permissions", "turn_aborted", "relay", "ide_selection", "ide_opened_file", "ide_diagnostics",
)
# Envelopes whose <message> children are the person's words; everything else in them is dropped.
ENVELOPES = ("wake",)

_ALL = DROP_TAGS + ENVELOPES + ("message", "project", "thread")
_ESCAPED_OPEN = re.compile(r"&lt;/?(?:%s)\b" % "|".join(re.escape(t) for t in _ALL))
_ENVELOPE = re.compile(r"<(%s)\b[^>]*>(.*?)(?:</\1\s*>|\Z)" % "|".join(ENVELOPES), re.S)
_MESSAGE = re.compile(r"<message\b[^>]*>(.*?)</message\s*>", re.S)
_DROP = re.compile(r"<(%s)\b[^>]*?(?:/>|>.*?(?:</\1\s*>|\Z))" % "|".join(re.escape(t) for t in DROP_TAGS), re.S)
_STRAY = re.compile(r"</?(?:%s)\b[^>]*>" % "|".join(re.escape(t) for t in _ALL))


def strip_harness(text: str) -> tuple[str, list[str]]:
    """Return (the person's text, names of the harness blocks removed)."""
    if not text or "<" not in text and "&lt;" not in text:
        return text or "", []
    if _ESCAPED_OPEN.search(text):
        text = html.unescape(text)
    removed: list[str] = []

    def envelope(m: re.Match) -> str:
        removed.append(m.group(1))
        return "\n\n".join(x.strip() for x in _MESSAGE.findall(m.group(2)))

    text = _ENVELOPE.sub(envelope, text)

    def drop(m: re.Match) -> str:
        removed.append(m.group(1))
        return ""

    text = _DROP.sub(drop, text)
    if removed and _STRAY.search(text):  # leftovers only once we know this text came wrapped by a harness
        text = _STRAY.sub("", text)
        removed.append("tag")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text, removed


def clean(text: str) -> str:
    return strip_harness(text)[0]


def has_harness(text: str) -> bool:
    return bool(text) and strip_harness(text)[1] != []
