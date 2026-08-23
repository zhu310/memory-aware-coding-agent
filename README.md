# Memory-Aware Coding Agent

A layered coding agent with tool orchestration, task collaboration,
on-demand skills, background execution, and session-memory-based context compaction.

## Highlights

- Tool calling with tool-result budget control
- Todo, task, teammate, and message-bus collaboration
- Lazy-loaded skills
- Background task execution
- Session Memory with incremental extraction
- Tool-chain-safe context compaction

## Architecture

```text
final_version_app/
  application/  # Agent loop, prompting, compaction, memory orchestration
  domain/       # Tasks, todos, messaging, background runtime, skills
  infra/        # LLM, shell, workspace adapters

skills/         # On-demand instructions loaded by the agent
```

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Fill in a valid API key in `.env`, then run:

```powershell
python -m final_version_app.runtime
```

## Test

```powershell
python -m pytest -q
```

## Security

Never commit `.env`, runtime memory, task state, transcripts, or API keys.