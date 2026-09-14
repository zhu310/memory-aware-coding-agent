"""Append-only storage for canonical runtime events."""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import re
from threading import RLock
from typing import Dict, List, Protocol

from final_version_app.protocol import RuntimeEvent


_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


class EventStoreCorruptionError(RuntimeError):
    """Raised when a persisted event cannot be decoded safely."""


class EventStore(Protocol):
    """Storage contract used by the runtime kernel."""

    def append(self, event: RuntimeEvent) -> RuntimeEvent:
        ...

    def read(self, thread_id: str, after_sequence: int = 0) -> List[RuntimeEvent]:
        ...


class JsonlEventStore:
    """Thread-safe JSONL event store with one append-only file per thread.

    JSONL keeps the first implementation inspectable and recoverable. The
    kernel depends only on ``EventStore``, so SQLite or a remote log can replace
    it without changing agent behavior.
    """

    def __init__(self, root: Path, fsync: bool = False):
        self.root = Path(root)
        self.fsync = fsync
        self._lock = RLock()
        self._last_sequences: Dict[str, int] = {}

    def append(self, event: RuntimeEvent) -> RuntimeEvent:
        self._validate_id(event.thread_id)
        with self._lock:
            sequence = self._next_sequence(event.thread_id)
            persisted = replace(event, sequence=sequence)
            path = self._path(event.thread_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(persisted.to_dict(), ensure_ascii=False) + "\n")
                handle.flush()
                if self.fsync:
                    os.fsync(handle.fileno())
            self._last_sequences[event.thread_id] = sequence
            return persisted

    def read(self, thread_id: str, after_sequence: int = 0) -> List[RuntimeEvent]:
        self._validate_id(thread_id)
        path = self._path(thread_id)
        if not path.exists():
            return []

        events: List[RuntimeEvent] = []
        with self._lock, path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    event = RuntimeEvent.from_dict(json.loads(line))
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise EventStoreCorruptionError(
                        f"Invalid event in {path.name} at line {line_number}: {exc}"
                    ) from exc
                if event.thread_id != thread_id:
                    raise EventStoreCorruptionError(
                        f"Event thread mismatch in {path.name} at line {line_number}."
                    )
                if event.sequence > after_sequence:
                    events.append(event)
        return events

    def _next_sequence(self, thread_id: str) -> int:
        cached = self._last_sequences.get(thread_id)
        if cached is not None:
            return cached + 1
        existing = self.read(thread_id)
        last = existing[-1].sequence if existing else 0
        self._last_sequences[thread_id] = last
        return last + 1

    def _path(self, thread_id: str) -> Path:
        return self.root / "events" / f"{thread_id}.jsonl"

    @staticmethod
    def _validate_id(value: str) -> None:
        if not _SAFE_ID.fullmatch(value):
            raise ValueError(f"Unsafe thread id: {value!r}")
