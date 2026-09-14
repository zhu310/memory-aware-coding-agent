"""Smoke tests for reproducible benchmark entrypoints."""

from __future__ import annotations

from benchmarks.context_compaction_benchmark import run_benchmark


def test_context_compaction_benchmark_reports_savings():
    result = run_benchmark(rounds=12, tool_chars=8_000)

    assert result["before_tokens_estimate"] > result["after_microcompact_tokens_estimate"]
    assert result["saved_tokens_estimate"] > 0
    assert result["token_reduction_percent"] > 0
