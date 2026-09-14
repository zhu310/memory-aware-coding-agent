"""Serializable runtime protocol models.

This module intentionally has no LangChain dependency. Protocol objects are
safe to persist and can later be shared with non-Python clients.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from time import time
from typing import Any, Dict, Optional
from uuid import uuid4


def new_id(prefix: str) -> str:
    """Return a compact, sortable-enough identifier with a readable prefix."""

    return f"{prefix}_{uuid4().hex}"


class ThreadStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    CLOSED = "closed"


class TurnStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class ItemKind(str, Enum):
    USER_MESSAGE = "user_message"
    ASSISTANT_MESSAGE = "assistant_message"
    TOOL_MESSAGE = "tool_message"
    TOOL_CALL = "tool_call"
    CONTEXT_SNAPSHOT = "context_snapshot"
    RUNTIME_NOTICE = "runtime_notice"


class EventKind(str, Enum):
    THREAD_STARTED = "thread_started"
    THREAD_RESUMED = "thread_resumed"
    THREAD_FORKED = "thread_forked"
    THREAD_CLOSED = "thread_closed"
    TURN_STARTED = "turn_started"
    TURN_COMPLETED = "turn_completed"
    TURN_FAILED = "turn_failed"
    TURN_INTERRUPTED = "turn_interrupted"
    CANCELLATION_REQUESTED = "cancellation_requested"
    ITEM_STARTED = "item_started"
    ITEM_COMPLETED = "item_completed"
    CONTEXT_COMPACTED = "context_compacted"
    TOKEN_USAGE_UPDATED = "token_usage_updated"


@dataclass(frozen=True)
class AgentItem:
    """A user-visible unit produced during a turn."""

    kind: ItemKind
    payload: Dict[str, Any]
    item_id: str = field(default_factory=lambda: new_id("item"))
    created_at: float = field(default_factory=time)

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result["kind"] = self.kind.value
        return result

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "AgentItem":
        return cls(
            item_id=str(value["item_id"]),
            kind=ItemKind(value["kind"]),
            payload=dict(value.get("payload") or {}),
            created_at=float(value.get("created_at") or time()),
        )


@dataclass(frozen=True)
class RuntimeEvent:
    """An immutable fact appended to a thread's canonical event log."""

    thread_id: str
    kind: EventKind
    payload: Dict[str, Any] = field(default_factory=dict)
    turn_id: Optional[str] = None
    item_id: Optional[str] = None
    event_id: str = field(default_factory=lambda: new_id("evt"))
    sequence: int = 0
    timestamp: float = field(default_factory=time)
    schema_version: int = 1

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result["kind"] = self.kind.value
        return result

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "RuntimeEvent":
        return cls(
            thread_id=str(value["thread_id"]),
            turn_id=value.get("turn_id"),
            item_id=value.get("item_id"),
            event_id=str(value["event_id"]),
            sequence=int(value["sequence"]),
            timestamp=float(value["timestamp"]),
            kind=EventKind(value["kind"]),
            payload=dict(value.get("payload") or {}),
            schema_version=int(value.get("schema_version") or 1),
        )


@dataclass(frozen=True)
class ThreadRecord:
    """Small mutable-through-replacement index entry for a thread."""

    thread_id: str
    status: ThreadStatus
    created_at: float
    updated_at: float
    title: str = ""
    latest_turn_id: Optional[str] = None
    parent_thread_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result["status"] = self.status.value
        return result

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "ThreadRecord":
        return cls(
            thread_id=str(value["thread_id"]),
            status=ThreadStatus(value["status"]),
            created_at=float(value["created_at"]),
            updated_at=float(value["updated_at"]),
            title=str(value.get("title") or ""),
            latest_turn_id=value.get("latest_turn_id"),
            parent_thread_id=value.get("parent_thread_id"),
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass(frozen=True)
class TurnResult:
    """Stable result returned by a synchronous runtime adapter."""

    thread_id: str
    turn_id: str
    status: TurnStatus
    assistant_text: str = ""
    error: Optional[str] = None
    usage: Dict[str, Any] = field(default_factory=dict)
