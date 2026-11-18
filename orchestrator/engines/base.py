"""AgentAdapter interface shared by all engines."""
from __future__ import annotations

import abc
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
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
