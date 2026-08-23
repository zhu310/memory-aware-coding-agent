"""Async subagent run manager."""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from final_version_app.application.container import AppServices
from final_version_app.config import AGENT_RUNS_DIR
from final_version_app.infra.workspace import read_text_auto


TERMINAL_STATUSES = {"completed", "failed"}


class AgentRunManager:
    """Track background subagent runs with check/join semantics."""

    def __init__(self, services: AppServices, run_subagent: Callable[[str, str], str]):
        AGENT_RUNS_DIR.mkdir(parents=True, exist_ok=True)
        self.services = services
        self.run_subagent = run_subagent

    def start(
        self,
        prompt: str,
        agent_type: str = "Explore",
        expected_output_schema: str = "",
        task_id: int | None = None,
    ) -> str:
        run_id = f"run_{uuid.uuid4().hex[:8]}"
        record = {
            "run_id": run_id,
            "status": "running",
            "agent_type": agent_type,
            "task_id": task_id,
            "prompt": prompt,
            "expected_output_schema": expected_output_schema,
            "created_at": time.time(),
            "updated_at": time.time(),
            "result": None,
            "error": None,
        }
        self._save(record)
        thread = threading.Thread(
            target=self._execute,
            args=(run_id, prompt, agent_type, expected_output_schema, task_id),
            daemon=True,
        )
        thread.start()
        return json.dumps(
            {
                "run_id": run_id,
                "status": "running",
                "agent_type": agent_type,
                "task_id": task_id,
            },
            indent=2,
            ensure_ascii=False,
        )

    def check(self, run_id: str | None = None) -> str:
        if run_id:
            return json.dumps(self._load(run_id), indent=2, ensure_ascii=False)
        runs = []
        for path in sorted(AGENT_RUNS_DIR.glob("run_*.json")):
            record = json.loads(read_text_auto(path))
            runs.append(
                {
                    "run_id": record.get("run_id"),
                    "status": record.get("status"),
                    "agent_type": record.get("agent_type"),
                    "task_id": record.get("task_id"),
                    "updated_at": record.get("updated_at"),
                }
            )
        return json.dumps(runs, indent=2, ensure_ascii=False)

    def join(self, run_id: str, timeout: int = 10) -> str:
        deadline = time.time() + max(timeout, 0)
        while True:
            record = self._load(run_id)
            if record.get("status") in TERMINAL_STATUSES:
                return json.dumps(record, indent=2, ensure_ascii=False)
            if time.time() >= deadline:
                return json.dumps(
                    {
                        "run_id": run_id,
                        "status": record.get("status", "unknown"),
                        "timeout": True,
                        "message": "Run is still active.",
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            time.sleep(0.5)

    def _execute(
        self,
        run_id: str,
        prompt: str,
        agent_type: str,
        expected_output_schema: str,
        task_id: int | None,
    ):
        try:
            delegated_prompt = prompt
            if expected_output_schema:
                delegated_prompt = (
                    f"{prompt}\n\n"
                    "Return your final answer in this structure when possible:\n"
                    f"{expected_output_schema}"
                )
            summary = self.run_subagent(delegated_prompt, agent_type)
            record = self._load(run_id)
            record.update(
                {
                    "status": "completed",
                    "updated_at": time.time(),
                    "result": {
                        "summary": summary,
                        "task_id": task_id,
                        "changed_files": [],
                        "blockers": [],
                        "next_actions": [],
                    },
                    "error": None,
                }
            )
            self._save(record)
            self.services.bus.send(
                "agent_run",
                "lead",
                json.dumps(
                    {
                        "run_id": run_id,
                        "status": "completed",
                        "summary": summary,
                        "task_id": task_id,
                    },
                    ensure_ascii=False,
                ),
                "agent_run_completed",
            )
        except Exception as exc:
            record = self._load(run_id)
            record.update(
                {
                    "status": "failed",
                    "updated_at": time.time(),
                    "result": None,
                    "error": str(exc),
                }
            )
            self._save(record)
            self.services.bus.send(
                "agent_run",
                "lead",
                json.dumps({"run_id": run_id, "status": "failed", "error": str(exc)}, ensure_ascii=False),
                "agent_run_failed",
            )

    def _path(self, run_id: str) -> Path:
        if not run_id.startswith("run_"):
            raise ValueError(f"Invalid run_id: {run_id}")
        return AGENT_RUNS_DIR / f"{run_id}.json"

    def _load(self, run_id: str) -> dict:
        path = self._path(run_id)
        if not path.exists():
            raise ValueError(f"Unknown run_id: {run_id}")
        return json.loads(read_text_auto(path))

    def _save(self, record: dict):
        record["updated_at"] = time.time()
        self._path(record["run_id"]).write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
