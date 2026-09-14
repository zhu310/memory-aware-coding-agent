"""HTTP adapter tests using a deterministic, network-free runtime."""

from __future__ import annotations

from threading import Event
from time import sleep

from fastapi.testclient import TestClient

from final_version_app.api import create_app
from final_version_app.tests.test_runtime_protocol import (
    append_assistant,
    build_test_runtime,
)


def test_run_endpoint_streams_real_runtime_events_and_result(tmp_path):
    def executor(messages, _services, _tools, _system, *, observer, cancellation):
        cancellation.raise_if_cancelled()
        observer.tool_started("read_file", "call_1", {"path": "README.md"})
        observer.tool_completed(
            "read_file",
            "call_1",
            "# Coding Agent",
            cached=False,
            failed=False,
        )
        append_assistant(messages, observer, "真实回答")

    runtime = build_test_runtime(tmp_path, executor)
    client = TestClient(create_app(lambda: runtime, auth_path=tmp_path/"auth.sqlite3"))
    assert client.post("/api/auth/setup", json={"username":"tester", "password":"test-password-123"}).status_code == 201

    response = client.post("/api/runs", json={"intent": "读取 README"})
    assert response.status_code == 202
    thread_id = response.json()["thread_id"]

    # The deterministic executor finishes immediately in its worker thread.
    terminal = Event()
    events = []
    for _ in range(50):
        payload = client.get(f"/api/threads/{thread_id}/events").json()
        events = payload["events"]
        if any(item["kind"] == "turn_completed" for item in events):
            terminal.set()
            break
    assert terminal.is_set()
    assert any(item["kind"] == "item_started" for item in events)
    assert any(item["kind"] == "token_usage_updated" for item in events)
    completed = next(item for item in events if item["kind"] == "turn_completed")
    assert completed["payload"]["assistant_text"] == "真实回答"


def test_api_rejects_busy_follow_up_and_can_interrupt(tmp_path):
    entered = Event()

    def executor(messages, _services, _tools, _system, *, observer, cancellation):
        entered.set()
        while True:
            cancellation.raise_if_cancelled()
            sleep(0.005)

    runtime = build_test_runtime(tmp_path, executor)
    client = TestClient(create_app(lambda: runtime, auth_path=tmp_path/"auth.sqlite3"))
    assert client.post("/api/auth/setup", json={"username":"tester", "password":"test-password-123"}).status_code == 201
    thread_id = client.post("/api/runs", json={"intent": "等待"}).json()["thread_id"]
    assert entered.wait(timeout=1)

    busy = client.post(
        f"/api/threads/{thread_id}/turns",
        json={"intent": "第二个任务"},
    )
    assert busy.status_code == 409

    stopped = client.post(
        f"/api/threads/{thread_id}/interrupt",
        json={"reason": "测试停止"},
    )
    assert stopped.status_code == 200
    assert stopped.json()["interrupted"] is True


def test_queued_turn_can_be_cancelled_before_worker_start(tmp_path, monkeypatch):
    entered = Event()

    def executor(messages, _services, _tools, _system, *, observer, cancellation):
        entered.set()
        while True:
            cancellation.raise_if_cancelled()
            sleep(0.005)

    monkeypatch.setenv("AGENT_API_MAX_WORKERS", "1")
    monkeypatch.setenv("AGENT_API_MAX_QUEUE", "1")
    runtime = build_test_runtime(tmp_path, executor)
    client = TestClient(create_app(lambda: runtime, auth_path=tmp_path/"auth.sqlite3"))
    assert client.post("/api/auth/setup", json={"username":"tester", "password":"test-password-123"}).status_code == 201

    active_id = client.post("/api/runs", json={"intent": "active"}).json()["thread_id"]
    assert entered.wait(timeout=1)
    queued_id = client.post("/api/runs", json={"intent": "queued"}).json()["thread_id"]

    stopped = client.post(
        f"/api/threads/{queued_id}/interrupt",
        json={"reason": "cancel before execution"},
    )
    assert stopped.status_code == 200
    assert stopped.json()["interrupted"] is True
    queued_events = client.get(f"/api/threads/{queued_id}/events").json()["events"]
    assert queued_events[-1]["kind"] == "turn_interrupted"
    assert queued_events[-1]["payload"]["phase"] == "queued"

    client.post(
        f"/api/threads/{active_id}/interrupt",
        json={"reason": "test cleanup"},
    )


def test_bounded_queue_returns_429_instead_of_unbounded_growth(tmp_path, monkeypatch):
    entered = Event()

    def executor(messages, _services, _tools, _system, *, observer, cancellation):
        entered.set()
        while True:
            cancellation.raise_if_cancelled()
            sleep(0.005)

    monkeypatch.setenv("AGENT_API_MAX_WORKERS", "1")
    monkeypatch.setenv("AGENT_API_MAX_QUEUE", "1")
    runtime = build_test_runtime(tmp_path, executor)
    client = TestClient(create_app(lambda: runtime, auth_path=tmp_path/"auth.sqlite3"))
    assert client.post("/api/auth/setup", json={"username":"tester", "password":"test-password-123"}).status_code == 201

    first_id = client.post("/api/runs", json={"intent": "first"}).json()["thread_id"]
    assert entered.wait(timeout=1)
    second_id = client.post("/api/runs", json={"intent": "second"}).json()["thread_id"]
    overloaded = client.post("/api/runs", json={"intent": "third"})

    assert overloaded.status_code == 429
    assert client.get("/api/health").json()["queue"] == {
        "max_workers": 1,
        "max_queue": 1,
        "running": 1,
        "queued": 1,
        "inflight": 2,
    }

    client.post(f"/api/threads/{second_id}/interrupt", json={"reason": "cleanup"})
    client.post(f"/api/threads/{first_id}/interrupt", json={"reason": "cleanup"})


def test_text_attachment_content_reaches_runtime_prompt(tmp_path):
    seen_prompts = []

    def executor(messages, _services, _tools, _system, *, observer, cancellation):
        cancellation.raise_if_cancelled()
        seen_prompts.append(str(messages[-1].content))
        append_assistant(messages, observer, "attachment received")

    runtime = build_test_runtime(tmp_path, executor)
    client = TestClient(create_app(lambda: runtime, auth_path=tmp_path/"auth.sqlite3"))
    assert client.post("/api/auth/setup", json={"username":"tester", "password":"test-password-123"}).status_code == 201
    response = client.post(
        "/api/runs",
        json={
            "intent": "summarize",
            "attachments": [
                {
                    "name": "note.md",
                    "media_type": "text/markdown",
                    "content": "enterprise attachment body",
                }
            ],
        },
    )
    assert response.status_code == 202
    thread_id = response.json()["thread_id"]
    for _ in range(50):
        events = client.get(f"/api/threads/{thread_id}/events").json()["events"]
        if any(event["kind"] == "turn_completed" for event in events):
            break

    assert len(seen_prompts) == 1
    assert "summarize" in seen_prompts[0]
    assert 'attachment name="note.md"' in seen_prompts[0]
    assert "enterprise attachment body" in seen_prompts[0]
