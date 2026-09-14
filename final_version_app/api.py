"""HTTP transport for the protocol-driven agent runtime.

The runtime remains transport agnostic.  This module only translates browser
requests into runtime commands and exposes the append-only event stream for
polling clients.  Turns execute on worker threads so HTTP requests never have
to wait for an LLM or tool call to finish.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import os
from pathlib import Path
from threading import BoundedSemaphore, Lock, RLock
from typing import Any, Callable, Dict, Optional

from fastapi import FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from final_version_app.storage.auth_store import AuthStore, AuthError, COOKIE, SESSION_TTL
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from final_version_app.application.repl import build_agent_runtime
from final_version_app.config import MODEL
from final_version_app.engine.runtime import AgentRuntime, RuntimeThreadBusyError
from final_version_app.storage.thread_store import ThreadNotFoundError


RuntimeFactory = Callable[[], AgentRuntime]


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=10, max_length=256)


class AttachmentPayload(BaseModel):
    """Small text attachment sent with a workspace turn."""

    name: str = Field(min_length=1, max_length=255)
    content: str = Field(default="", max_length=500_000)
    file_id: Optional[str] = Field(default=None, pattern=r"^file_[0-9a-f]{32}$")
    media_type: str = Field(default="text/plain", max_length=120)


class StartRunRequest(BaseModel):
    """Input required to create a thread and start its first turn."""

    intent: str = Field(min_length=1, max_length=100_000)
    title: str = Field(default="", max_length=200)
    attachments: list[AttachmentPayload] = Field(default_factory=list, max_length=5)


class StartTurnRequest(BaseModel):
    """A follow-up instruction for an existing idle thread."""

    intent: str = Field(min_length=1, max_length=100_000)
    attachments: list[AttachmentPayload] = Field(default_factory=list, max_length=5)


class InterruptRequest(BaseModel):
    reason: str = Field(default="用户请求停止", max_length=500)


class RuntimeQueueFullError(RuntimeError):
    """Raised when accepting more work would exceed the bounded queue."""


class RuntimeService:
    """Own one lazily constructed runtime and its background turn workers."""

    def __init__(
        self,
        runtime_factory: RuntimeFactory,
        max_workers: int = 4,
        max_queue: int = 32,
    ):
        self._runtime_factory = runtime_factory
        self._runtime: Optional[AgentRuntime] = None
        self._runtime_lock = Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="agent-turn",
        )
        self.max_workers = max_workers
        self.max_queue = max_queue
        self._capacity = BoundedSemaphore(max_workers + max_queue)
        self._futures: Dict[str, Future[Any]] = {}
        self._futures_lock = RLock()

    @property
    def runtime(self) -> AgentRuntime:
        if self._runtime is None:
            with self._runtime_lock:
                if self._runtime is None:
                    self._runtime = self._runtime_factory()
        return self._runtime

    @property
    def initialized(self) -> bool:
        return self._runtime is not None

    def start_run(
        self,
        intent: str,
        title: str = "",
        attachments: Optional[list[AttachmentPayload]] = None,
    ) -> Dict[str, str]:
        prompt = _prompt_with_attachments(intent, attachments or [])
        if not prompt:
            raise ValueError("任务意图不能为空。")
        thread = self.runtime.start_thread(
            title=title.strip() or _title_from_intent(prompt),
            metadata={"source": "workspace-api"},
        )
        try:
            self._submit_turn(thread.thread_id, prompt)
        except RuntimeQueueFullError:
            # Preserve an auditable record without leaving an apparently
            # runnable idle thread that never had a chance to execute.
            self.runtime.close_thread(thread.thread_id)
            raise
        return {"thread_id": thread.thread_id, "status": "accepted"}

    def start_turn(
        self,
        thread_id: str,
        intent: str,
        attachments: Optional[list[AttachmentPayload]] = None,
    ) -> Dict[str, str]:
        prompt = _prompt_with_attachments(intent, attachments or [])
        if not prompt:
            raise ValueError("任务意图不能为空。")
        # Resolve the thread before returning 202, otherwise missing-thread
        # errors would only surface inside the background future.
        self.runtime.thread_store.get(thread_id)
        self._submit_turn(thread_id, prompt)
        return {"thread_id": thread_id, "status": "accepted"}

    def _submit_turn(self, thread_id: str, prompt: str) -> None:
        with self._futures_lock:
            current = self._futures.get(thread_id)
            if current is not None and not current.done():
                raise RuntimeThreadBusyError(
                    f"Thread {thread_id} already has a running turn."
                )
            if not self._capacity.acquire(blocking=False):
                raise RuntimeQueueFullError("Agent execution queue is full.")
            try:
                future = self._executor.submit(self.runtime.run_turn, thread_id, prompt)
                self._futures[thread_id] = future
                future.add_done_callback(
                    lambda completed, current_thread=thread_id: self._finish_future(
                        current_thread,
                        completed,
                    )
                )
            except BaseException:
                self._capacity.release()
                raise

    def _finish_future(self, thread_id: str, future: Future[Any]) -> None:
        with self._futures_lock:
            if self._futures.get(thread_id) is future:
                self._futures.pop(thread_id, None)
        self._capacity.release()

    def interrupt(self, thread_id: str, reason: str) -> bool:
        """Cancel queued work or cooperatively signal an active Runtime turn."""

        self.runtime.thread_store.get(thread_id)
        with self._futures_lock:
            future = self._futures.get(thread_id)
            if future is not None and future.cancel():
                self.runtime.record_queued_interruption(thread_id, reason)
                return True
        return self.runtime.interrupt_turn(thread_id, reason)

    def queue_status(self) -> Dict[str, int]:
        with self._futures_lock:
            futures = list(self._futures.values())
        return {
            "max_workers": self.max_workers,
            "max_queue": self.max_queue,
            "running": sum(future.running() for future in futures),
            "queued": sum(not future.running() and not future.done() for future in futures),
            "inflight": sum(not future.done() for future in futures),
        }


def _title_from_intent(intent: str) -> str:
    compact = " ".join(intent.split())
    return compact[:60] or "新任务"


def _prompt_with_attachments(
    intent: str,
    attachments: list[AttachmentPayload],
) -> str:
    prompt = intent.strip()
    if not prompt:
        raise ValueError("任务意图不能为空。")
    if sum(len(item.content) for item in attachments) > 2_000_000:
        raise ValueError("附件总内容不能超过 2 MB。")
    if not attachments:
        return prompt
    blocks = []
    for item in attachments:
        if item.file_id:
            import json
            from final_version_app.storage.assets import get_asset_store
            metadata = get_asset_store().metadata(item.file_id)
            blocks.append("<file_reference>" + json.dumps({"file_id":item.file_id,"name":metadata["name"],"status":metadata["status"],"instruction":"Use file_read or image_understand to inspect this file; do not infer its contents from its filename."},ensure_ascii=False) + "</file_reference>")
            continue
        safe_name = item.name.replace("\\", "/").split("/")[-1]
        blocks.append(
            f'<attachment name="{safe_name}" media_type="{item.media_type}">\n'
            f"{item.content}\n</attachment>"
        )
    return (
        f"{prompt}\n\n"
        "以下是用户主动提供的附件内容，请把它们作为任务资料处理：\n"
        "<workspace_attachments>\n"
        + "\n".join(blocks)
        + "\n</workspace_attachments>"
    )


def _allowed_origins() -> list[str]:
    configured = os.getenv("AGENT_API_CORS_ORIGINS", "")
    if configured.strip():
        return [origin.strip() for origin in configured.split(",") if origin.strip()]
    return [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:3001",
        "http://127.0.0.1:3001",
    ]


def create_app(runtime_factory: RuntimeFactory = build_agent_runtime, *, auth_path=None) -> FastAPI:
    """Build the API application; tests can inject a network-free runtime."""

    app = FastAPI(
        title="Agent Workspace API",
        version="0.1.0",
        description="Observable HTTP adapter for the coding-agent runtime.",
    )
    service = RuntimeService(
        runtime_factory,
        max_workers=max(1, int(os.getenv("AGENT_API_MAX_WORKERS", "4"))),
        max_queue=max(0, int(os.getenv("AGENT_API_MAX_QUEUE", "32"))),
    )
    app.state.runtime_service = service
    auth = AuthStore(auth_path or os.getenv("AGENT_AUTH_DB", ".agent_runtime/auth.sqlite3"))
    app.state.auth_store = auth

    @app.middleware("http")
    async def authenticate_request(request: Request, call_next):
        public = {"/api/health", "/api/auth/status", "/api/auth/setup", "/api/auth/login"}
        path = request.url.path.rstrip("/")
        if path.startswith("/api/") and request.method != "OPTIONS":
            origin = request.headers.get("origin")
            if request.method not in {"GET", "HEAD"} and origin and origin not in _allowed_origins() and origin != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "Origin not allowed."}, status_code=403)
            if path not in public and not auth.authenticate(request.cookies.get(COOKIE)):
                return JSONResponse({"detail": "Please sign in."}, status_code=401)
        response = await call_next(request)
        if path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/auth/status")
    def auth_status(request: Request):
        username = auth.authenticate(request.cookies.get(COOKIE))
        return {"configured": auth.configured(), "authenticated": bool(username), "username": username}

    def login_result(payload: LoginRequest, request: Request, response: Response, setup=False):
        try:
            token = auth.sign_in(payload.username, payload.password,
                                 request.client.host if request.client else "local", setup=setup)
        except AuthError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        response.set_cookie(COOKIE, token, max_age=SESSION_TTL, httponly=True,
                            samesite="strict", secure=os.getenv("AGENT_AUTH_SECURE_COOKIE", "0") == "1", path="/api")
        return {"configured": True, "authenticated": True, "username": payload.username}

    @app.post("/api/auth/setup", status_code=201)
    def setup_account(payload: LoginRequest, request: Request, response: Response):
        return login_result(payload, request, response, setup=True)

    @app.post("/api/auth/login")
    def login(payload: LoginRequest, request: Request, response: Response):
        return login_result(payload, request, response)

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response):
        auth.logout(request.cookies.get(COOKIE))
        response.delete_cookie(COOKIE, path="/api", httponly=True, samesite="strict")
        return {"ok": True}

    @app.get("/api/threads")
    def list_threads():
        # The one local owner inherits the existing workspace's conversations.
        return {"threads": [{**record.to_dict(), "active": record.thread_id in service._futures}
                            for record in service.runtime.thread_store.list()
                            if record.metadata.get("source") == "workspace-api"]}

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_allowed_origins(),
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    def health() -> Dict[str, Any]:
        return {
            "status": "ok",
            "runtime_initialized": service.initialized,
            "model": MODEL,
            "queue": service.queue_status(),
        }

    @app.post("/api/runs", status_code=status.HTTP_202_ACCEPTED)
    def start_run(request: StartRunRequest) -> Dict[str, str]:
        try:
            return service.start_run(request.intent, request.title, request.attachments)
        except RuntimeQueueFullError as exc:
            raise HTTPException(status_code=429, detail="系统繁忙，请稍后重试。") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post(
        "/api/threads/{thread_id}/turns",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def start_turn(thread_id: str, request: StartTurnRequest) -> Dict[str, str]:
        try:
            return service.start_turn(thread_id, request.intent, request.attachments)
        except ThreadNotFoundError as exc:
            raise HTTPException(status_code=404, detail="任务线程不存在。") from exc
        except RuntimeThreadBusyError as exc:
            raise HTTPException(status_code=409, detail="当前任务仍在执行。") from exc
        except RuntimeQueueFullError as exc:
            raise HTTPException(status_code=429, detail="系统繁忙，请稍后重试。") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/threads/{thread_id}/events")
    def events(
        thread_id: str,
        after: int = Query(default=0, ge=0),
    ) -> Dict[str, Any]:
        try:
            items = service.runtime.events(thread_id, after_sequence=after)
        except ThreadNotFoundError as exc:
            raise HTTPException(status_code=404, detail="任务线程不存在。") from exc
        serialized = [item.to_dict() for item in items]
        next_sequence = serialized[-1]["sequence"] if serialized else after
        return {
            "thread_id": thread_id,
            "events": serialized,
            "next_sequence": next_sequence,
            "active": thread_id in service._futures,
        }

    @app.post("/api/threads/{thread_id}/interrupt")
    def interrupt(thread_id: str, request: InterruptRequest) -> Dict[str, Any]:
        try:
            service.runtime.thread_store.get(thread_id)
            interrupted = service.interrupt(thread_id, request.reason)
        except ThreadNotFoundError as exc:
            raise HTTPException(status_code=404, detail="任务线程不存在。") from exc
        return {"thread_id": thread_id, "interrupted": interrupted}

    from final_version_app.storage.assets import get_asset_store
    from final_version_app.files_api import install_file_routes
    from final_version_app.domain.vision import get_image_jobs
    install_file_routes(app, get_asset_store())
    image_jobs = get_image_jobs()
    @app.get("/api/image-jobs")
    def list_image_jobs():
        return {"jobs": image_jobs.list()}
    from final_version_app.domain.creative_jobs import get_creative_jobs
    from final_version_app.domain.skills import SkillLoader
    from final_version_app.config import SKILLS_DIR
    creative_jobs=get_creative_jobs()
    @app.get('/api/creative-jobs')
    def creative_list():return {'jobs':creative_jobs.list()}
    @app.post('/api/creative-jobs')
    def creative_start(payload:dict):
        try:
            if payload.get('kind')!='office':raise ValueError('This endpoint starts Office previews only')
            return creative_jobs.start('office',payload['request_id'],file_id=payload['file_id'],format=payload.get('format','pdf'))
        except (ValueError,KeyError) as exc:raise HTTPException(422,str(exc)) from exc
    @app.post('/api/creative-jobs/{job_id}/cancel')
    def creative_cancel(job_id:str):
        try:return creative_jobs.cancel(job_id)
        except ValueError as exc:raise HTTPException(404,str(exc)) from exc
    @app.on_event('startup')
    def recover_creative():creative_jobs.recover()
    @app.get("/api/capabilities")
    def capabilities():
        return {"search": bool(os.getenv("SEARXNG_URL")), "files": True,
                "vision": bool(os.getenv("ALIYUN_API_KEY") or os.getenv("DASHSCOPE_API_KEY")),
                "image_generation": bool(os.getenv("ALIYUN_API_KEY") or os.getenv("DASHSCOPE_API_KEY")),
                "word_edit": True, "excel_edit": True, "media_files": True,
                "office_render": bool(os.getenv("AGENT_CREATIVE_URL")),
                "music_generation": bool(os.getenv("ELEVENLABS_API_KEY")),
                "skills": list(SkillLoader(SKILLS_DIR).skills)}
    @app.on_event("startup")
    def recover_images():
        image_jobs.recover()
    from final_version_app.domain.services import get_service_manager
    from final_version_app.preview import install_preview_routes
    manager = get_service_manager()
    install_preview_routes(app, auth, manager)
    if os.getenv("AGENT_RESTORE_SERVICES", "0") == "1":
        @app.on_event("startup")
        def restore_services():
            manager.restore()
    return app


app = create_app()


def main() -> None:
    """Run a local development server with ``python -m final_version_app.api``."""

    import uvicorn

    uvicorn.run(
        "final_version_app.api:app",
        host=os.getenv("AGENT_API_HOST", "127.0.0.1"),
        port=int(os.getenv("AGENT_API_PORT", "8000")),
        reload=False,
    )


if __name__ == "__main__":
    main()
