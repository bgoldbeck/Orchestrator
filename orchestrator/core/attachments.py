"""Files and images attached to a chat message.

Each attachment is copied to <data>/attachments/<chat id>/ when the message is sent, so the chat keeps it even if
the original moves. Images go to the model directly (Claude: image blocks; Codex: --image on a new turn); every
attachment is also listed by path in the prompt, so either engine can open it with its own tools.
"""
from __future__ import annotations

import base64
import mimetypes
import shutil
import time
from pathlib import Path

from orchestrator.storage.db import app_dir

IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
               ".webp": "image/webp"}
MAX_INLINE_IMAGE = 5 * 1024 * 1024  # bigger images are only referenced by path
MAX_FILE = 50 * 1024 * 1024


def is_image(path: str | Path) -> bool:
    return Path(path).suffix.lower() in IMAGE_TYPES


def staging_dir() -> Path:
    d = app_dir() / "attachments" / "pasted"
    d.mkdir(parents=True, exist_ok=True)
    return d


def pasted_image_path() -> Path:
    return staging_dir() / f"pasted-{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}.png"


def store(sid: int, src: str | Path) -> Path:
    """Copy one attachment into the chat's folder (unique name) and return the copy's path."""
    src = Path(src)
    d = app_dir() / "attachments" / str(sid)
    d.mkdir(parents=True, exist_ok=True)
    dest = d / src.name
    n = 1
    while dest.exists():
        dest = d / f"{src.stem}-{n}{src.suffix}"
        n += 1
    shutil.copy2(src, dest)
    return dest


def check(path: str | Path) -> str | None:
    """Why this file can't be attached, or None."""
    p = Path(path)
    if not p.is_file():
        return f"{p.name}: not a file"
    if p.stat().st_size > MAX_FILE:
        return f"{p.name}: larger than 50 MB"
    return None


def file_list(paths: list[str]) -> str:
    if not paths:
        return ""
    lines = "\n".join(f"- {p}" for p in paths)
    return f"\n\nAttached files (open them with your tools as needed):\n{lines}"


def prompt_text(text: str, paths: list[str]) -> str:
    return (text or "See the attached files.") + file_list(paths)


def claude_content(text: str, paths: list[str]) -> list[dict]:
    """Content blocks for one Claude user message: images inline, then the text with every path listed."""
    blocks: list[dict] = []
    for p in paths:
        path = Path(p)
        if is_image(path) and path.is_file() and path.stat().st_size <= MAX_INLINE_IMAGE:
            data = base64.b64encode(path.read_bytes()).decode("ascii")
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": IMAGE_TYPES[path.suffix.lower()],
                                                       "data": data}})
    blocks.append({"type": "text", "text": prompt_text(text, paths)})
    return blocks


def label(path: str) -> str:
    p = Path(path)
    kind = "image" if is_image(p) else (mimetypes.guess_type(p.name)[0] or "file").split("/")[-1]
    return f"{p.name} ({kind})"
