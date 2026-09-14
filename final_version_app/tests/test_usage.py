from __future__ import annotations

from types import SimpleNamespace

import pytest

from final_version_app.infra.usage import (
    UsageLedger,
    extract_token_usage,
    usage_scope,
    usage_delta,
)


def test_extracts_langchain_usage_metadata():
    message = SimpleNamespace(
        usage_metadata={
            "input_tokens": 120,
            "output_tokens": 30,
            "total_tokens": 150,
            "input_token_details": {"cache_read": 40},
            "output_token_details": {"reasoning": 12},
        },
        response_metadata={},
    )

    usage = extract_token_usage(message)

    assert usage.input_tokens == 120
    assert usage.output_tokens == 30
    assert usage.total_tokens == 150
    assert usage.cached_input_tokens == 40
    assert usage.reasoning_tokens == 12


def test_extracts_openai_compatible_response_metadata_fallback():
    message = SimpleNamespace(
        usage_metadata=None,
        response_metadata={
            "token_usage": {
                "prompt_tokens": 80,
                "completion_tokens": 20,
                "total_tokens": 100,
                "prompt_tokens_details": {"cached_tokens": 32},
                "completion_tokens_details": {"reasoning_tokens": 9},
            }
        },
    )

    usage = extract_token_usage(message)

    assert usage.input_tokens == 80
    assert usage.output_tokens == 20
    assert usage.total_tokens == 100
    assert usage.cached_input_tokens == 32
    assert usage.reasoning_tokens == 9


def test_ledger_tracks_success_failure_events_and_delta():
    ledger = UsageLedger(max_events=10)
    before = ledger.snapshot()
    message = SimpleNamespace(
        usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        response_metadata={},
    )

    success = ledger.record_success(message, "test-model", 0.25)
    failure = ledger.record_failure("test-model", 0.5, TimeoutError("timeout"))
    after = ledger.snapshot()
    delta = usage_delta(before, after)

    assert success.sequence == 1
    assert failure.sequence == 2
    assert delta.successful_calls == 1
    assert delta.failed_calls == 1
    assert delta.calls == 2
    assert delta.input_tokens == 10
    assert delta.output_tokens == 5
    assert delta.total_tokens == 15
    assert delta.llm_time_seconds == 0.75
    assert [event.error_type for event in ledger.events_since(0)] == [None, "TimeoutError"]


def test_total_falls_back_to_input_plus_output():
    message = SimpleNamespace(
        usage_metadata={"input_tokens": 11, "output_tokens": 7},
        response_metadata={},
    )

    assert extract_token_usage(message).total_tokens == 18


def test_persists_each_event_with_run_id(tmp_path):
    log_path = tmp_path / "usage" / "events.jsonl"
    ledger = UsageLedger(log_path=log_path)
    message = SimpleNamespace(
        usage_metadata={"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
        response_metadata={},
    )

    with usage_scope("run-123"):
        ledger.record_success(message, "test-model", 0.1)

    line = log_path.read_text(encoding="utf-8").strip()
    assert '"run_id": "run-123"' in line
    assert '"total_tokens": 6' in line


def test_ledger_isolates_overlapping_run_usage():
    ledger = UsageLedger(max_events=10)
    message_a = SimpleNamespace(
        usage_metadata={"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
        response_metadata={},
    )
    message_b = SimpleNamespace(
        usage_metadata={"input_tokens": 200, "output_tokens": 20, "total_tokens": 220},
        response_metadata={},
    )

    with usage_scope("turn-a"):
        ledger.record_success(message_a, "test-model", 0.2)
    with usage_scope("turn-b"):
        ledger.record_success(message_b, "test-model", 0.3)
    with usage_scope("turn-a"):
        ledger.record_failure("test-model", 0.1, TimeoutError("retry"))

    run_a = ledger.snapshot_for_run("turn-a")
    run_b = ledger.snapshot_for_run("turn-b")

    assert run_a.total_tokens == 110
    assert run_a.successful_calls == 1
    assert run_a.failed_calls == 1
    assert run_a.llm_time_seconds == pytest.approx(0.3)
    assert run_b.total_tokens == 220
    assert run_b.calls == 1
