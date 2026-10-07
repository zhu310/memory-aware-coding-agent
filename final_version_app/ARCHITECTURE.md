# Coding Agent Runtime Architecture

## Purpose

`final_version_app` uses a small runtime kernel around the existing model loop.
The kernel owns lifecycle, persistence, cancellation and observability. It does
not contain coding, research or document-generation business logic.

```text
CLI / future HTTP and WebSocket adapters
                    │
                    ▼
             protocol commands
                    │
                    ▼
              AgentRuntime
       Thread / Turn lifecycle and budget events
          │                    │
          ▼                    ▼
 append-only EventStore    existing agent_loop
 Thread metadata store           │
                                 ▼
                         ToolOrchestrator
                                 │
                                 ▼
                         registered handlers
```

## Package boundaries

| Package | Responsibility | Must not own |
|---|---|---|
| `protocol` | Serializable commands, events, items and statuses | LangChain or storage code |
| `storage` | Event and thread persistence contracts | Agent decisions or tool behavior |
| `engine` | Thread/Turn lifecycle, cancellation and history projection | Specialist business capabilities |
| `tools` | Provider-neutral tool routing, cache and error normalization | CLI or model prompting |
| `application` | Existing use cases, model loop and adapters | Durable protocol definitions |
| `domain` | Todo, task, messaging and background domain services | Model/provider integration |
| `infra` | LLM, filesystem, shell and usage gateways | Agent planning policy |

## Canonical event log

Every thread has one JSONL file. Events are immutable and receive a contiguous
per-thread sequence number from the store. Thread metadata is deliberately kept
separate so listing threads never requires replaying every event.

The active LangChain message list is a projection:

- message items append model-visible history;
- a `context_compacted` event replaces the active projection;
- a fork starts from a serialized projection of its parent;
- resuming a thread rebuilds the projection from persisted events.

This means compaction no longer destroys the only durable record of execution.

## Public runtime operations

```python
runtime.start_thread(title="...")
runtime.resume_thread(thread_id)
runtime.fork_thread(thread_id)
runtime.run_turn(thread_id, prompt)
runtime.interrupt_turn(thread_id, reason="...")
runtime.compact_thread(thread_id)
runtime.close_thread(thread_id)
runtime.events(thread_id)
```

Transport adapters should prefer `runtime.submit(command)` so they only depend
on protocol objects. Direct methods remain convenient for in-process callers.

## Deployment boundary

The supported deployment topology is:

```text
1 Workspace = 1 API Process = 1 AgentRuntime
```

Multiple API processes writing to the same workspace are outside the current
support boundary. The JSONL/file stores are intentionally local and simple; they
are not a cross-process coordination layer.

The optional Gateway is only a static route table from account/slot names to
pre-existing Runtime API URLs. It does not create workspaces, start runtimes,
allocate ports, manage containers, provision volumes, or verify the workspace
identity behind a URL. Deployers must ensure each configured slot points to the
intended independent workspace runtime.

## Compatibility policy

The original `build_runtime()` tuple and optional-free `agent_loop(...)` call
remain supported. Benchmarks and teaching examples can continue using them.
Production entrypoints should use `build_agent_runtime()`.

## Extension rules

1. Do not add persistence, approval or UI code to `agent_loop.py`.
2. Every tool must execute through `ToolOrchestrator`.
3. New runtime facts must be typed `EventKind` values, not console-only text.
4. Persisted schema changes require a version and a resume compatibility test.
5. Specialist capabilities must later implement the Agent Unit contract; they
   must not become condition branches inside `AgentRuntime`.
6. Runtime changes require integration tests for completion, failure and
   interruption paths.

## Current v0.1 limits

- Cancellation is cooperative; a running subprocess is not yet force-killed.
- The current shell safety layer is still a command blacklist, not an OS sandbox.
- Tools are still advertised eagerly; deferred tool discovery is the next Token
  optimization stage.
- JSONL is appropriate for the local runtime; multiple API processes sharing one
  workspace are not supported by the current architecture.
- Usage is recorded per Turn, but hard budgets and shared subagent-tree budgets
  are not enforced yet.

These limitations are explicit so follow-up work extends the intended boundary
instead of hiding more behavior in the core loop.
