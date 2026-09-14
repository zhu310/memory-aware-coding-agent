"""Narrow observation interface between the legacy loop and runtime kernel."""

from __future__ import annotations

from typing import Any, Dict, List, Protocol


class LoopObserver(Protocol):
    """Receive structured lifecycle updates without owning loop behavior."""

    def message_appended(self, message: Any) -> None:
        ...

    def tool_started(self, tool_name: str, tool_call_id: str, arguments: Dict[str, Any]) -> None:
        ...

    def tool_completed(
        self,
        tool_name: str,
        tool_call_id: str,
        output: str,
        *,
        cached: bool,
        failed: bool,
    ) -> None:
        ...

    def context_compacted(
        self,
        trigger: str,
        messages: List[Any],
        before_tokens: int,
        after_tokens: int,
    ) -> None:
        ...


class NullLoopObserver:
    """No-op observer used by legacy callers and tests."""

    def message_appended(self, message: Any) -> None:
        return None

    def tool_started(self, tool_name: str, tool_call_id: str, arguments: Dict[str, Any]) -> None:
        return None

    def tool_completed(
        self,
        tool_name: str,
        tool_call_id: str,
        output: str,
        *,
        cached: bool,
        failed: bool,
    ) -> None:
        return None

    def context_compacted(
        self,
        trigger: str,
        messages: List[Any],
        before_tokens: int,
        after_tokens: int,
    ) -> None:
        return None
