"""Thread-safe process-level accounting for LLM token usage."""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
import json
import os
from pathlib import Path
from threading import Lock
from time import time
from typing import Any
from uuid import uuid4


_current_run_id: ContextVar[str | None] = ContextVar("llm_usage_run_id", default=None)


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        return dumped if isinstance(dumped, dict) else {}
    return {}


def _integer(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0


def extract_token_usage(message: Any) -> TokenUsage:
    """Normalize LangChain/OpenAI-compatible usage metadata."""
    usage = _as_mapping(getattr(message, "usage_metadata", None))
    response_metadata = _as_mapping(getattr(message, "response_metadata", None))
    provider_usage = _as_mapping(
        response_metadata.get("token_usage") or response_metadata.get("usage")
    )

    input_tokens = _integer(
        usage.get("input_tokens", provider_usage.get("prompt_tokens"))
    )
    output_tokens = _integer(
        usage.get("output_tokens", provider_usage.get("completion_tokens"))
    )
    total_tokens = _integer(
        usage.get("total_tokens", provider_usage.get("total_tokens"))
    )
    if total_tokens == 0:
        total_tokens = input_tokens + output_tokens

    input_details = _as_mapping(
        usage.get("input_token_details")
        or provider_usage.get("prompt_tokens_details")
    )
    output_details = _as_mapping(
        usage.get("output_token_details")
        or provider_usage.get("completion_tokens_details")
    )
    cached_input_tokens = _integer(
        input_details.get("cache_read", input_details.get("cached_tokens"))
    )
    reasoning_tokens = _integer(
        output_details.get("reasoning", output_details.get("reasoning_tokens"))
    )
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cached_input_tokens=cached_input_tokens,
        reasoning_tokens=reasoning_tokens,
    )


@dataclass(frozen=True)
class UsageSnapshot:
    sequence: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0
    llm_time_seconds: float = 0.0

    @property
    def calls(self) -> int:
        return self.successful_calls + self.failed_calls

    def as_dict(self) -> dict[str, int | float]:
        result = asdict(self)
        result["calls"] = self.calls
        return result


@dataclass(frozen=True)
class UsageEvent:
    sequence: int
    timestamp: float
    model: str
    succeeded: bool
    duration_seconds: float
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0
    error_type: str | None = None
    run_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class UsageLedger:
    """In-memory ledger. Its bounded event history avoids unbounded growth."""

    def __init__(self, max_events: int = 2000, log_path: Path | None = None):
        self._lock = Lock()
        self._totals = UsageSnapshot()
        self._events: deque[UsageEvent] = deque(maxlen=max_events)
        self._log_path = log_path

    def _persist(self, event: UsageEvent) -> None:
        """Best-effort append: telemetry must never break the agent itself."""
        if self._log_path is None:
            return
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event.as_dict(), ensure_ascii=False) + "\n")
        except OSError:
            pass

    def record_success(self, message: Any, model: str, duration_seconds: float) -> UsageEvent:
        usage = extract_token_usage(message)
        with self._lock:
            sequence = self._totals.sequence + 1
            event = UsageEvent(
                sequence=sequence,
                timestamp=time(),
                model=model,
                succeeded=True,
                duration_seconds=max(0.0, duration_seconds),
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                total_tokens=usage.total_tokens,
                cached_input_tokens=usage.cached_input_tokens,
                reasoning_tokens=usage.reasoning_tokens,
                run_id=_current_run_id.get(),
            )
            current = self._totals
            self._totals = UsageSnapshot(
                sequence=sequence,
                successful_calls=current.successful_calls + 1,
                failed_calls=current.failed_calls,
                input_tokens=current.input_tokens + usage.input_tokens,
                output_tokens=current.output_tokens + usage.output_tokens,
                total_tokens=current.total_tokens + usage.total_tokens,
                cached_input_tokens=current.cached_input_tokens + usage.cached_input_tokens,
                reasoning_tokens=current.reasoning_tokens + usage.reasoning_tokens,
                llm_time_seconds=current.llm_time_seconds + event.duration_seconds,
            )
            self._events.append(event)
            self._persist(event)
            return event

    def record_failure(self, model: str, duration_seconds: float, error: BaseException) -> UsageEvent:
        with self._lock:
            sequence = self._totals.sequence + 1
            event = UsageEvent(
                sequence=sequence,
                timestamp=time(),
                model=model,
                succeeded=False,
                duration_seconds=max(0.0, duration_seconds),
                error_type=type(error).__name__,
                run_id=_current_run_id.get(),
            )
            current = self._totals
            self._totals = replace(
                current,
                sequence=sequence,
                failed_calls=current.failed_calls + 1,
                llm_time_seconds=current.llm_time_seconds + event.duration_seconds,
            )
            self._events.append(event)
            self._persist(event)
            return event

    def snapshot(self) -> UsageSnapshot:
        with self._lock:
            return replace(self._totals)

    def events_since(self, sequence: int = 0) -> list[UsageEvent]:
        with self._lock:
            return [event for event in self._events if event.sequence > sequence]

    def snapshot_for_run(self, run_id: str, after_sequence: int = 0) -> UsageSnapshot:
        """Aggregate only events attributed to one concurrent Runtime turn."""

        with self._lock:
            events = [
                event
                for event in self._events
                if event.sequence > after_sequence and event.run_id == run_id
            ]
        return UsageSnapshot(
            sequence=max((event.sequence for event in events), default=after_sequence),
            successful_calls=sum(event.succeeded for event in events),
            failed_calls=sum(not event.succeeded for event in events),
            input_tokens=sum(event.input_tokens for event in events),
            output_tokens=sum(event.output_tokens for event in events),
            total_tokens=sum(event.total_tokens for event in events),
            cached_input_tokens=sum(event.cached_input_tokens for event in events),
            reasoning_tokens=sum(event.reasoning_tokens for event in events),
            llm_time_seconds=sum(event.duration_seconds for event in events),
        )

    def reset(self) -> None:
        with self._lock:
            self._totals = UsageSnapshot()
            self._events.clear()


def usage_delta(before: UsageSnapshot, after: UsageSnapshot) -> UsageSnapshot:
    """Return non-negative process usage accrued between two snapshots."""
    return UsageSnapshot(
        sequence=after.sequence,
        successful_calls=max(0, after.successful_calls - before.successful_calls),
        failed_calls=max(0, after.failed_calls - before.failed_calls),
        input_tokens=max(0, after.input_tokens - before.input_tokens),
        output_tokens=max(0, after.output_tokens - before.output_tokens),
        total_tokens=max(0, after.total_tokens - before.total_tokens),
        cached_input_tokens=max(0, after.cached_input_tokens - before.cached_input_tokens),
        reasoning_tokens=max(0, after.reasoning_tokens - before.reasoning_tokens),
        llm_time_seconds=max(0.0, after.llm_time_seconds - before.llm_time_seconds),
    )


def format_usage(label: str, usage: UsageSnapshot) -> str:
    return (
        f"[usage] {label}: calls={usage.successful_calls} ok/{usage.failed_calls} failed, "
        f"input={usage.input_tokens:,}, output={usage.output_tokens:,}, "
        f"total={usage.total_tokens:,}, cached_input={usage.cached_input_tokens:,}, "
        f"reasoning={usage.reasoning_tokens:,}, llm_time={usage.llm_time_seconds:.2f}s"
    )


_log_enabled = os.getenv("LLM_USAGE_LOG_ENABLED", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
_default_log_path = (
    Path(os.getenv("LLM_USAGE_LOG_PATH") or (Path.cwd() / ".usage" / "llm_usage.jsonl"))
    if _log_enabled
    else None
)
usage_ledger = UsageLedger(log_path=_default_log_path)


@contextmanager
def usage_scope(run_id: str | None = None):
    """Attach a stable identifier to calls made in the current execution context."""
    resolved = run_id or uuid4().hex
    token = _current_run_id.set(resolved)
    try:
        yield resolved
    finally:
        _current_run_id.reset(token)


def get_usage_snapshot() -> UsageSnapshot:
    return usage_ledger.snapshot()


def get_usage_events(sequence: int = 0) -> list[UsageEvent]:
    return usage_ledger.events_since(sequence)


def get_run_usage(run_id: str, after_sequence: int = 0) -> UsageSnapshot:
    """Return usage isolated to ``run_id`` even when turns overlap."""

    return usage_ledger.snapshot_for_run(run_id, after_sequence)


def reset_usage() -> None:
    usage_ledger.reset()
