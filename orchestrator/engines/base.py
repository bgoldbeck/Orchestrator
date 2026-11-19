"""AgentAdapter interface shared by all engines."""
from __future__ import annotations

import abc
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator.core.events import Event, NeedsApproval

# Called when an engine asks for permission. Returns True to allow.
ApprovalHandler = Callable[[NeedsApproval], Awaitable[bool]]


@dataclass
class TurnRequest:
    prompt: str
    cwd: Path
    model: str
    effort: str
    resume_id: str | None = None
    sandbox: str = "workspace-write"  # Codex only
    # Local stdio MCP servers for this turn: name -> {"command", "args", "env", "tool_timeout_sec"}.
    # Their tools are trusted (auto-approved) on both engines.
    mcp: dict[str, dict] | None = None
    # Claude only: skip user-level settings (plugins, skills, hooks, ~/.claude/CLAUDE.md, user MCP servers);
    # the project's own CLAUDE.md and settings still load.
    lean: bool = False
    # MCP servers from the user's own config to hide this turn (switched off in Orchestrator).
    mcp_off: list[str] = field(default_factory=list)
    # Files attached to this message (already copied into the app's data folder).
    attachments: list[str] = field(default_factory=list)


class AgentAdapter(abc.ABC):
    engine: str = ""
    label: str = ""

    def __init__(self, approval_handler: ApprovalHandler | None = None) -> None:
        self.approval_handler = approval_handler

    @abc.abstractmethod
    def run(self, req: TurnRequest) -> AsyncIterator[Event]:
        """Run one turn (new or resumed) and stream events until Done."""

    @abc.abstractmethod
    async def interrupt(self) -> None:
        """Stop the running turn as soon as possible."""
