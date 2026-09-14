"""Input commands understood by the agent runtime.

The first runtime version exposes convenient Python methods as well. These
command objects form the stable boundary for future HTTP, WebSocket and UI
adapters, so those clients do not need to call the model loop directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class StartThreadCommand:
    """Create a new durable conversation thread."""

    title: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ResumeThreadCommand:
    """Load a previously persisted thread."""

    thread_id: str


@dataclass(frozen=True)
class ForkThreadCommand:
    """Create a new thread from the current context projection."""

    thread_id: str
    title: str = ""


@dataclass(frozen=True)
class CloseThreadCommand:
    """Prevent new turns while retaining all persisted history."""

    thread_id: str


@dataclass(frozen=True)
class StartTurnCommand:
    """Run one user request inside an existing thread."""

    thread_id: str
    prompt: str


@dataclass(frozen=True)
class InterruptTurnCommand:
    """Request cooperative cancellation of a running turn."""

    thread_id: str
    reason: Optional[str] = None
