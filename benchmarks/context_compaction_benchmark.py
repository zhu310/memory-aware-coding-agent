"""Deterministic benchmark for local context compaction.

This script does not call an LLM. It measures the local guardrail layers that
run before expensive summary compaction: single tool-result clipping and
micro-compaction of older tool outputs.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from final_version_app.application.compression import apply_tool_result_budget, microcompact
from final_version_app.infra.llm import estimate_tokens


def synthetic_messages(rounds: int, tool_chars: int) -> list:
    messages = [HumanMessage(content="Analyze a project and keep important findings.")]
    payload = "\n".join(
        f"line {line}: function_{line % 17} important diagnostic detail"
        for line in range(max(1, tool_chars // 48))
    )
    payload = (payload + "\n") * max(1, tool_chars // max(1, len(payload)))
    payload = payload[:tool_chars]

    for idx in range(rounds):
        call_id = f"call_{idx}"
        messages.append(
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_file" if idx % 2 else "grep_content",
                        "id": call_id,
                        "args": {"path": f"src/module_{idx}.py"},
                    }
                ],
            )
        )
        messages.append(ToolMessage(content=payload, tool_call_id=call_id))
        messages.append(AIMessage(content=f"Round {idx} finding recorded."))
    return messages


def run_benchmark(rounds: int = 24, tool_chars: int = 12_000) -> dict:
    messages = synthetic_messages(rounds, tool_chars)
    before_tokens = estimate_tokens(messages)
    before_chars = sum(len(str(message.content)) for message in messages)

    apply_tool_result_budget(messages)
    after_budget_tokens = estimate_tokens(messages)
    microcompact(messages)
    after_micro_tokens = estimate_tokens(messages)
    after_chars = sum(len(str(message.content)) for message in messages)

    saved_tokens = before_tokens - after_micro_tokens
    return {
        "rounds": rounds,
        "tool_chars_per_result": tool_chars,
        "before_tokens_estimate": before_tokens,
        "after_tool_budget_tokens_estimate": after_budget_tokens,
        "after_microcompact_tokens_estimate": after_micro_tokens,
        "saved_tokens_estimate": saved_tokens,
        "token_reduction_percent": round(saved_tokens * 100 / before_tokens, 2),
        "before_chars": before_chars,
        "after_chars": after_chars,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run local context compaction benchmark.")
    parser.add_argument("--rounds", type=int, default=24)
    parser.add_argument("--tool-chars", type=int, default=12_000)
    args = parser.parse_args()
    print(json.dumps(run_benchmark(args.rounds, args.tool_chars), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
