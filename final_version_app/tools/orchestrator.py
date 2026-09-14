"""Single execution path for model-requested tools.

This first version centralizes routing, caching, invalidation, cancellation and
error normalization. Approval policy, hooks and sandbox selection can be added
around this boundary without growing the model loop.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Callable, Dict, Mapping, MutableMapping, Optional, Set

from final_version_app.engine.cancellation import CancellationToken
from final_version_app.engine.loop_observer import LoopObserver, NullLoopObserver


ToolHandler = Callable[..., Any]


@dataclass(frozen=True)
class ToolCall:
    """Provider-neutral tool request produced by a model response."""

    name: str
    arguments: Dict[str, Any]
    call_id: str

    @classmethod
    def from_model_call(cls, value: Mapping[str, Any]) -> "ToolCall":
        arguments = value.get("args") or {}
        if not isinstance(arguments, dict):
            arguments = {}
        return cls(
            name=str(value.get("name") or ""),
            arguments=dict(arguments),
            call_id=str(value.get("id") or ""),
        )


@dataclass(frozen=True)
class ToolExecutionResult:
    """Normalized result returned to both the model loop and event stream."""

    call: ToolCall
    output: str
    cached: bool = False
    failed: bool = False
    error_type: Optional[str] = None


class ToolOrchestrator:
    """Execute every tool through one observable, cancellable path."""

    def __init__(
        self,
        handlers: Mapping[str, ToolHandler],
        *,
        cacheable_names: Optional[Set[str]] = None,
        cache_invalidating_names: Optional[Set[str]] = None,
        cache_limit: int = 24,
    ) -> None:
        self._handlers = dict(handlers)
        self._cacheable_names = set(cacheable_names or set())
        self._cache_invalidating_names = set(cache_invalidating_names or set())
        self._cache_limit = max(0, cache_limit)
        self._cache: MutableMapping[str, str] = {}
        self._cache_order: list[str] = []

    def execute(
        self,
        model_call: Mapping[str, Any],
        *,
        observer: Optional[LoopObserver] = None,
        cancellation: Optional[CancellationToken] = None,
    ) -> ToolExecutionResult:
        observer = observer or NullLoopObserver()
        cancellation = cancellation or CancellationToken()
        cancellation.raise_if_cancelled()

        call = ToolCall.from_model_call(model_call)
        observer.tool_started(call.name, call.call_id, call.arguments)

        if call.name in self._cache_invalidating_names:
            self.clear_cache()

        cache_key = self._cache_key(call)
        if call.name in self._cacheable_names and cache_key in self._cache:
            result = ToolExecutionResult(call=call, output=self._cache[cache_key], cached=True)
            self._report(observer, result)
            return result

        handler = self._handlers.get(call.name)
        if handler is None:
            result = ToolExecutionResult(
                call=call,
                output=f"Error: Unknown tool: {call.name}",
                failed=True,
                error_type="UnknownToolError",
            )
            self._report(observer, result)
            return result

        try:
            output = str(handler(**call.arguments))
            failed = output.startswith("Error:")
            result = ToolExecutionResult(
                call=call,
                output=output,
                failed=failed,
                error_type="ToolReportedError" if failed else None,
            )
        except Exception as exc:
            result = ToolExecutionResult(
                call=call,
                output=f"Error: {exc}",
                failed=True,
                error_type=type(exc).__name__,
            )

        if call.name in self._cacheable_names and not result.failed:
            self._store_cache(cache_key, result.output)
        self._report(observer, result)
        return result

    def clear_cache(self) -> None:
        self._cache.clear()
        self._cache_order.clear()

    @staticmethod
    def _cache_key(call: ToolCall) -> str:
        payload = json.dumps(call.arguments, ensure_ascii=False, sort_keys=True, default=str)
        return f"{call.name}:{payload}"

    def _store_cache(self, key: str, output: str) -> None:
        if self._cache_limit == 0:
            return
        if key not in self._cache:
            self._cache_order.append(key)
        self._cache[key] = output
        while len(self._cache_order) > self._cache_limit:
            oldest = self._cache_order.pop(0)
            self._cache.pop(oldest, None)

    @staticmethod
    def _report(observer: LoopObserver, result: ToolExecutionResult) -> None:
        observer.tool_completed(
            result.call.name,
            result.call.call_id,
            result.output,
            cached=result.cached,
            failed=result.failed,
        )
