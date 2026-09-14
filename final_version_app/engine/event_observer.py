"""Adapter that converts model-loop callbacks into runtime protocol events."""

from __future__ import annotations

from typing import Any, Callable, Dict, List

from langchain_core.messages import BaseMessage

from final_version_app.engine.message_codec import encode_message, encode_messages
from final_version_app.protocol import AgentItem, EventKind, ItemKind, RuntimeEvent, new_id


AppendEvent = Callable[[RuntimeEvent], RuntimeEvent]


class RuntimeLoopObserver:
    """Keep event translation outside both the kernel and reasoning loop."""

    def __init__(self, append_event: AppendEvent, thread_id: str, turn_id: str):
        self._append_event = append_event
        self.thread_id = thread_id
        self.turn_id = turn_id
        self._tool_items: Dict[str, str] = {}

    def model_started(self, model: str) -> None:
        item = AgentItem(kind=ItemKind.RUNTIME_NOTICE, payload={"stage":"model", "model":model})
        self._model_item = item
        self._append_event(RuntimeEvent(thread_id=self.thread_id,turn_id=self.turn_id,item_id=item.item_id,kind=EventKind.ITEM_STARTED,payload={"item":item.to_dict()}))

    def model_completed(self) -> None:
        from final_version_app.infra.usage import get_run_usage
        item = getattr(self, "_model_item", None)
        if item:
            self._append_event(RuntimeEvent(thread_id=self.thread_id,turn_id=self.turn_id,item_id=item.item_id,kind=EventKind.ITEM_COMPLETED,payload={"item":item.to_dict()}))
        self._append_event(RuntimeEvent(thread_id=self.thread_id,turn_id=self.turn_id,kind=EventKind.TOKEN_USAGE_UPDATED,payload=get_run_usage(self.turn_id).as_dict()))

    def message_appended(self, message: BaseMessage) -> None:
        encoded = encode_message(message)
        role_to_kind = {
            "user": ItemKind.USER_MESSAGE,
            "assistant": ItemKind.ASSISTANT_MESSAGE,
            "tool": ItemKind.TOOL_MESSAGE,
        }
        item = AgentItem(
            kind=role_to_kind[encoded["role"]],
            payload={"message": encoded},
        )
        self._append_event(
            RuntimeEvent(
                thread_id=self.thread_id,
                turn_id=self.turn_id,
                item_id=item.item_id,
                kind=EventKind.ITEM_COMPLETED,
                payload={"item": item.to_dict()},
            )
        )

    def tool_started(self, tool_name: str, tool_call_id: str, arguments: Dict[str, Any]) -> None:
        item = AgentItem(
            kind=ItemKind.TOOL_CALL,
            payload={
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "arguments": arguments,
            },
        )
        self._tool_items[tool_call_id] = item.item_id
        self._append_event(
            RuntimeEvent(
                thread_id=self.thread_id,
                turn_id=self.turn_id,
                item_id=item.item_id,
                kind=EventKind.ITEM_STARTED,
                payload={"item": item.to_dict()},
            )
        )

    def tool_completed(
        self,
        tool_name: str,
        tool_call_id: str,
        output: str,
        *,
        cached: bool,
        failed: bool,
    ) -> None:
        item_id = self._tool_items.pop(tool_call_id, None) or new_id("item")
        item = AgentItem(
            item_id=item_id,
            kind=ItemKind.TOOL_CALL,
            payload={
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "output": output,
                "cached": cached,
                "failed": failed,
            },
        )
        self._append_event(
            RuntimeEvent(
                thread_id=self.thread_id,
                turn_id=self.turn_id,
                item_id=item.item_id,
                kind=EventKind.ITEM_COMPLETED,
                payload={"item": item.to_dict()},
            )
        )

    def context_compacted(
        self,
        trigger: str,
        messages: List[BaseMessage],
        before_tokens: int,
        after_tokens: int,
    ) -> None:
        self._append_event(
            RuntimeEvent(
                thread_id=self.thread_id,
                turn_id=self.turn_id,
                kind=EventKind.CONTEXT_COMPACTED,
                payload={
                    "trigger": trigger,
                    "before_tokens": before_tokens,
                    "after_tokens": after_tokens,
                    "messages": encode_messages(messages),
                },
            )
        )
