"""Engine-neutral event types emitted by every AgentAdapter."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Union


@dataclass
class Started:
    session_id: str


@dataclass
class Text:
    """Assistant text. ``delta=True`` means append to the current message;
    ``delta=False`` means a complete message (replaces any streamed deltas)."""

    text: str
    delta: bool = False


@dataclass
class ToolCall:
    id: str
    name: str
    summary: str = ""
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResult:
    id: str
    output: str = ""
    is_error: bool = False
    name: str = ""


@dataclass
class NeedsApproval:
    id: str
    tool: str
    detail: str = ""


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float | None = None


@dataclass
class Done:
    is_error: bool = False
    result: str = ""


@dataclass
class Error:
    message: str
    fatal: bool = True
    kind: str = "error"  # error | auth | warning


Event = Union[Started, Text, ToolCall, ToolResult, NeedsApproval, Usage, Done, Error]
