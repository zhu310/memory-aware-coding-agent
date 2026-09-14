"""Serialization boundary for LangChain messages stored in runtime events."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any, Dict, Iterable, List

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage


def _json_safe(value: Any) -> Any:
    """Convert provider-specific content blocks into JSON-compatible values."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if hasattr(value, "model_dump"):
        return _json_safe(value.model_dump())
    return str(value)


def encode_message(message: BaseMessage) -> Dict[str, Any]:
    """Serialize the message fields needed to resume an agent thread."""

    common: Dict[str, Any] = {
        "content": _json_safe(message.content),
        "id": getattr(message, "id", None),
        "name": getattr(message, "name", None),
        "additional_kwargs": _json_safe(getattr(message, "additional_kwargs", {})),
        "response_metadata": _json_safe(getattr(message, "response_metadata", {})),
    }
    if isinstance(message, HumanMessage):
        common["role"] = "user"
    elif isinstance(message, AIMessage):
        common["role"] = "assistant"
        common["tool_calls"] = _json_safe(message.tool_calls or [])
        common["invalid_tool_calls"] = _json_safe(message.invalid_tool_calls or [])
        common["usage_metadata"] = _json_safe(message.usage_metadata)
    elif isinstance(message, ToolMessage):
        common["role"] = "tool"
        common["tool_call_id"] = message.tool_call_id
        common["status"] = getattr(message, "status", "success")
    else:
        raise TypeError(f"Unsupported message type: {type(message).__name__}")
    return common


def decode_message(value: Dict[str, Any]) -> BaseMessage:
    """Recreate a LangChain message from an event payload."""

    role = value.get("role")
    kwargs = {
        "content": value.get("content", ""),
        "id": value.get("id"),
        "name": value.get("name"),
        "additional_kwargs": value.get("additional_kwargs") or {},
        "response_metadata": value.get("response_metadata") or {},
    }
    if role == "user":
        return HumanMessage(**kwargs)
    if role == "assistant":
        return AIMessage(
            **kwargs,
            tool_calls=value.get("tool_calls") or [],
            invalid_tool_calls=value.get("invalid_tool_calls") or [],
            usage_metadata=value.get("usage_metadata"),
        )
    if role == "tool":
        return ToolMessage(
            **kwargs,
            tool_call_id=str(value.get("tool_call_id") or ""),
            status=value.get("status") or "success",
        )
    raise ValueError(f"Unsupported persisted message role: {role!r}")


def encode_messages(messages: Iterable[BaseMessage]) -> List[Dict[str, Any]]:
    return [encode_message(message) for message in messages]


def decode_messages(messages: Iterable[Dict[str, Any]]) -> List[BaseMessage]:
    return [decode_message(message) for message in messages]
