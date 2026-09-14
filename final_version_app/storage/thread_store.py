"""Thread metadata index separated from the canonical event log."""

from __future__ import annotations

import json
from pathlib import Path
import re
from threading import RLock
from typing import List, Protocol
from uuid import uuid4

from final_version_app.protocol import ThreadRecord


_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


class ThreadNotFoundError(KeyError):
    """Raised when a requested thread does not exist."""


class ThreadStore(Protocol):
    def save(self, record: ThreadRecord) -> None:
        ...

    def get(self, thread_id: str) -> ThreadRecord:
        ...

    def list(self) -> List[ThreadRecord]:
        ...


class JsonThreadStore:
    """Atomic file-backed metadata store for the v0.1 runtime."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._lock = RLock()

    def save(self, record: ThreadRecord) -> None:
        path = self._path(record.thread_id)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
            try:
                temporary.write_text(
                    json.dumps(record.to_dict(), indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                temporary.replace(path)
            finally:
                if temporary.exists():
                    temporary.unlink()

    def get(self, thread_id: str) -> ThreadRecord:
        path = self._path(thread_id)
        if not path.exists():
            raise ThreadNotFoundError(thread_id)
        with self._lock:
            return ThreadRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def list(self) -> List[ThreadRecord]:
        directory = self.root / "threads"
        if not directory.exists():
            return []
        with self._lock:
            records = [
                ThreadRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
                for path in directory.glob("thr_*.json")
            ]
        return sorted(records, key=lambda item: item.updated_at, reverse=True)

    def _path(self, thread_id: str) -> Path:
        if not _SAFE_ID.fullmatch(thread_id):
            raise ValueError(f"Unsafe thread id: {thread_id!r}")
        return self.root / "threads" / f"{thread_id}.json"
