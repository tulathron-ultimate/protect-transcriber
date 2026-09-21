"""FastAPI application: REST API plus the single-page web UI."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import media
from .analysis import AnalysisClient, AnalysisConfig, AnalysisError, AnalysisNotConfigured
from .config import BackendKind, Settings, WhisperInstance, get_settings
from .jobs import JobManager
from .protect import ProtectAuthError, ProtectClient, ProtectError
from .store import AnalysisConfigStore, InstanceStore, JobStore, PreviewStore
from .transcript import Segment, to_srt, to_vtt, to_wallclock_text
from .whisper import BACKENDS, NoHealthyInstances, OpenAIBackend, WhisperPool

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


# --------------------------------------------------------------------------- #
# request/response models
# --------------------------------------------------------------------------- #


class JobRequest(BaseModel):
    camera_id: str = Field(alias="cameraId", min_length=1)
    start: datetime
    end: datetime
    # When set, the clip already on disk for that preview is used instead of a
    # fresh export; start/end must fall inside the previewed range.
    preview_id: str | None = Field(default=None, alias="previewId")
    title: str = ""
    language: str | None = None
    task: Literal["transcribe", "translate"] = "transcribe"
    prompt: str | None = None

    model_config = {"populate_by_name": True}

    @field_validator("start", "end")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        # Treat a naive timestamp as UTC; the UI always sends an offset.
        return value if value.tzinfo else value.replace(tzinfo=UTC)


class AnalysisSettings(BaseModel):
    """The LLM endpoint used for summaries, review and Q&A."""

    base_url: str = Field(default="", alias="baseUrl", max_length=500)
    api_key: str | None = Field(default=None, alias="apiKey")
    model: str = Field(default="", max_length=200)
    enabled: bool = False

    model_config = {"populate_by_name": True}

    @field_validator("base_url")
    @classmethod
    def _needs_scheme(cls, value: str) -> str:
        value = (value or "").strip().rstrip("/")
        if value and not value.startswith(("http://", "https://")):
            raise ValueError("Base URL must start with http:// or https://")
        return value


class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=1000)


class PreviewRequest(BaseModel):
    camera_id: str = Field(alias="cameraId", min_length=1)
    start: datetime
    end: datetime

    model_config = {"populate_by_name": True}

    @field_validator("start", "end")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=UTC)


class InstanceCreate(BaseModel):
    """A Whisper instance as the settings panel submits it."""

    name: str = Field(min_length=1, max_length=64)
    url: str = Field(min_length=1, max_length=500)
    kind: BackendKind = "auto"
    model: str = ""
    concurrency: int = Field(default=1, ge=1, le=64)
    api_key: str = Field(default="", alias="apiKey")
    enabled: bool = True

    model_config = {"populate_by_name": True}

    @field_validator("url")
    @classmethod
    def _needs_a_scheme(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")
        return value

    @field_validator("name")
    @classmethod
    def _tidy_name(cls, value: str) -> str:
        return value.strip()


class InstanceUpdate(BaseModel):
    """Every field optional; omitted ones keep their stored value."""

    name: str | None = Field(default=None, min_length=1, max_length=64)
    url: str | None = Field(default=None, min_length=1, max_length=500)
    kind: BackendKind | None = None
    model: str | None = None
    concurrency: int | None = Field(default=None, ge=1, le=64)
    api_key: str | None = Field(default=None, alias="apiKey")
    enabled: bool | None = None

    model_config = {"populate_by_name": True}

    _check_url = field_validator("url")(InstanceCreate._needs_a_scheme.__func__)


class InstanceProbe(BaseModel):
    """A candidate instance to test before saving it."""

    url: str = Field(min_length=1)
    kind: BackendKind = "auto"
    api_key: str = Field(default="", alias="apiKey")

    model_config = {"populate_by_name": True}

    _check_url = field_validator("url")(InstanceCreate._needs_a_scheme.__func__)


def _instance_from_row(row: dict[str, Any]) -> WhisperInstance:
    return WhisperInstance(
        name=row["name"],
        url=row["url"],
        kind=row["kind"],
        model=row["model"],
        concurrency=row["concurrency"],
        api_key=row["api_key"],
        enabled=bool(row["enabled"]),
    )


def _public_instance(row: dict[str, Any]) -> dict[str, Any]:
    """Never return the API key itself -- only whether one is set."""
    return {
        "id": row["id"],
        "name": row["name"],
        "url": row["url"],
        "kind": row["kind"],
        "model": row["model"],
        "concurrency": row["concurrency"],
        "enabled": bool(row["enabled"]),
        "hasApiKey": bool(row["api_key"]),
        "position": row["position"],
    }


# --------------------------------------------------------------------------- #
# app wiring
# --------------------------------------------------------------------------- #


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


async def _retention_loop(manager: JobManager) -> None:
    while True:
        try:
            await manager.cleanup_expired()
            await manager.cleanup_previews()
        except Exception:  # pragma: no cover - never kill the loop
            log.exception("Retention sweep failed")
        await asyncio.sleep(6 * 3600)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    _configure_logging(settings.log_level)
    settings.ensure_dirs()

    store = JobStore(settings.db_path)
    await store.init()

    previews = PreviewStore(settings.db_path)
    analysis = AnalysisConfigStore(settings.db_path)
    # Its own client: the Whisper pool's carries a 30-minute read timeout meant
    # for transcription, which is far too long to wait on a chat completion.
    http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=180.0))
    instances = InstanceStore(settings.db_path)
    # First start: lift whatever is in WHISPER_INSTANCES into the database so it
    # shows up in the UI ready to edit. After that the database wins, or a
    # restart would quietly undo edits made through the UI.
    seeded = await instances.seed(
        [
            {
                "name": inst.name,
                "url": inst.url,
                "kind": inst.kind,
                "model": inst.model,
                "concurrency": inst.concurrency,
                "api_key": inst.api_key,
            }
            for inst in settings.instances
        ]
    )
    if seeded:
        log.info("Seeded %d Whisper instance(s) from WHISPER_INSTANCES", seeded)
    rows = await instances.list()
    pool = WhisperPool(settings, instances=[_instance_from_row(row) for row in rows])
    protect = ProtectClient(settings)
    manager = JobManager(settings, store, pool, protect, previews=previews)
    await manager.start()

    app.state.store = store
    app.state.instances = instances
    app.state.previews = previews
    app.state.analysis = analysis
    app.state.http = http
    app.state.pool = pool
    app.state.protect = protect
    app.state.manager = manager
    app.state.retention_task = asyncio.create_task(_retention_loop(manager))

    if not media.ffmpeg_available(settings.ffmpeg_path, settings.ffprobe_path):
        log.error("ffmpeg/ffprobe not found on PATH -- transcription will fail")
    if not pool.states:
        log.warning(
            "No Whisper instances configured. Add one in the UI under Whisper "
            "instances, or set WHISPER_INSTANCES before first start."
        )
    else:
        # Probe in the background so startup is not blocked by a sleeping container.
        asyncio.create_task(pool.probe_all(force=True))

    try:
        yield
    finally:
        app.state.retention_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await app.state.retention_task
        await manager.stop()
        await http.aclose()
        await pool.aclose()
        await protect.aclose()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="protect-transcriber",
        version="0.1.0",
        description=(
            "Pick a camera and a time range in UniFi Protect, get a transcript back "
            "from your own Whisper containers."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings

    def require_token(request: Request) -> None:
        """Optional shared-secret gate (APP_TOKEN)."""
        expected = settings.app_token
        if not expected:
            return
        supplied = (
            request.headers.get("X-App-Token")
            or request.query_params.get("token")
            or request.cookies.get("pt_token")
            or ""
        )
        header = request.headers.get("Authorization", "")
        if not supplied and header.lower().startswith("bearer "):
            supplied = header[7:]
        if not secrets.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="Invalid or missing app token")

    # Applied per route via `dependencies=[Guard]`; /api/health and / stay open so
    # Docker/Unraid health checks work without the token.
    Guard = Depends(require_token)

    def get_manager() -> JobManager:
        return app.state.manager

    def get_store() -> JobStore:
        return app.state.store

    def get_pool() -> WhisperPool:
        return app.state.pool

    def get_protect() -> ProtectClient:
        return app.state.protect

    # -- meta ---------------------------------------------------------------

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        pool: WhisperPool = app.state.pool
        return {
            "status": "ok",
            "configured": settings.configured(),
            "ffmpeg": media.ffmpeg_available(settings.ffmpeg_path, settings.ffprobe_path),
            "protectHost": settings.protect_host or None,
            "whisperInstances": len(pool.states),
            "authRequired": bool(settings.app_token),
        }

    @app.get("/api/config", dependencies=[Guard])
    async def config() -> dict[str, Any]:
        """Non-secret settings the UI needs to render itself."""
        return {
            "protectHost": settings.protect_host,
            "maxRangeSeconds": settings.max_range_seconds,
            "chunkSeconds": settings.chunk_seconds,
            "defaultLanguage": settings.whisper_language,
            "defaultTask": settings.whisper_task,
            "keepClips": settings.keep_clips,
            "retentionDays": settings.retention_days,
        }

    @app.get("/api/whisper", dependencies=[Guard])
    async def whisper_status(
        refresh: bool = False, pool: WhisperPool = Depends(get_pool)
    ) -> dict[str, Any]:
        states = await pool.probe_all(force=refresh)
        return {
            "instances": [state.as_dict() for state in states],
            "healthy": sum(1 for s in states if s.healthy),
            "capacity": pool.total_capacity,
        }

    # -- Whisper instances (editable at runtime) ----------------------------

    def get_instances() -> InstanceStore:
        return app.state.instances

    async def _reload_pool() -> None:
        """Push the stored instances into the live pool and re-probe them."""
        rows = await app.state.instances.list()
        pool: WhisperPool = app.state.pool
        pool.reload([_instance_from_row(row) for row in rows])
        await pool.probe_all(force=True)

    @app.get("/api/whisper/instances", dependencies=[Guard])
    async def list_instances(store: InstanceStore = Depends(get_instances)) -> dict[str, Any]:
        rows = await store.list()
        # Merge in live health so the settings panel shows one coherent picture.
        live = {state.instance.name: state.as_dict() for state in app.state.pool.states}
        return {
            "instances": [
                _public_instance(row) | {"status": live.get(row["name"])} for row in rows
            ],
            "seededFrom": "WHISPER_INSTANCES" if settings.whisper_instances else None,
        }

    @app.post("/api/whisper/instances", status_code=201, dependencies=[Guard])
    async def create_instance(
        payload: InstanceCreate, store: InstanceStore = Depends(get_instances)
    ) -> dict[str, Any]:
        existing = await store.list()
        if any(row["name"].lower() == payload.name.lower() for row in existing):
            raise HTTPException(
                status_code=409, detail=f"An instance named {payload.name!r} exists"
            )
        row = await store.add(
            name=payload.name,
            url=payload.url,
            kind=payload.kind,
            model=payload.model,
            concurrency=payload.concurrency,
            api_key=payload.api_key,
            enabled=int(payload.enabled),
        )
        await _reload_pool()
        return _public_instance(row)

    @app.patch("/api/whisper/instances/{instance_id}", dependencies=[Guard])
    async def update_instance(
        instance_id: int,
        payload: InstanceUpdate,
        store: InstanceStore = Depends(get_instances),
    ) -> dict[str, Any]:
        if await store.get(instance_id) is None:
            raise HTTPException(status_code=404, detail="No such instance")
        # exclude_unset keeps an omitted api_key from wiping the stored one;
        # sending "" explicitly still clears it.
        fields = payload.model_dump(exclude_unset=True, exclude_none=True)
        if "enabled" in fields:
            fields["enabled"] = int(fields["enabled"])
        if "name" in fields:
            clash = [
                row
                for row in await store.list()
                if row["name"].lower() == fields["name"].lower() and row["id"] != instance_id
            ]
            if clash:
                raise HTTPException(status_code=409, detail="Another instance has that name")
        row = await store.update(instance_id, **fields) if fields else await store.get(instance_id)
        await _reload_pool()
        return _public_instance(row)

    @app.delete("/api/whisper/instances/{instance_id}", dependencies=[Guard])
    async def delete_instance(
        instance_id: int, store: InstanceStore = Depends(get_instances)
    ) -> dict[str, Any]:
        if not await store.delete(instance_id):
            raise HTTPException(status_code=404, detail="No such instance")
        await _reload_pool()
        return {"deleted": instance_id}

    @app.post("/api/whisper/instances/test", dependencies=[Guard])
    async def test_instance(payload: InstanceProbe) -> dict[str, Any]:
        """Probe a URL before saving it, and report which API answered."""
        candidate = WhisperInstance(
            name="probe", url=payload.url, kind=payload.kind, api_key=payload.api_key
        )
        order = (
            ("openai", "asr_webservice", "whisper_cpp")
            if payload.kind == "auto"
            else (payload.kind,)
        )
        pool: WhisperPool = app.state.pool
        errors: list[str] = []
        for kind in order:
            backend = BACKENDS[kind](candidate)
            try:
                ok = await backend.probe(pool.client)
            except Exception as exc:  # noqa: BLE001 - any transport error is just "no"
                errors.append(f"{kind}: {type(exc).__name__}")
                continue
            if not ok:
                errors.append(f"{kind}: no match")
                continue
            models: list[str] = []
            if kind == "openai":
                try:
                    models = await OpenAIBackend(candidate).models(pool.client)
                except Exception:  # noqa: BLE001 - the model list is a convenience
                    models = []
            return {"reachable": True, "kind": kind, "models": models[:200]}
        return {"reachable": False, "kind": None, "models": [], "detail": "; ".join(errors)}

    # -- Protect ------------------------------------------------------------

    @app.get("/api/protect", dependencies=[Guard])
    async def protect_info(client: ProtectClient = Depends(get_protect)) -> dict[str, Any]:
        try:
            return {"connected": True, "nvr": await client.nvr_info()}
        except (ProtectError, ProtectAuthError) as exc:
            return {"connected": False, "error": str(exc)}

    @app.get("/api/cameras", dependencies=[Guard])
    async def cameras(client: ProtectClient = Depends(get_protect)) -> dict[str, Any]:
        try:
            found = await client.cameras()
        except ProtectAuthError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except ProtectError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"cameras": [camera.as_dict() for camera in found]}

    @app.get("/api/cameras/{camera_id}/events", dependencies=[Guard])
    async def camera_events(
        camera_id: str,
        start: datetime | None = None,
        end: datetime | None = None,
        client: ProtectClient = Depends(get_protect),
    ) -> dict[str, Any]:
        end = end or datetime.now(tz=UTC)
        start = start or (end - timedelta(hours=24))
        events = await client.events(start, end, camera_id=camera_id)
        return {"events": [event.as_dict() for event in events]}

    @app.get("/api/cameras/{camera_id}/snapshot", dependencies=[Guard])
    async def camera_snapshot(
        camera_id: str, client: ProtectClient = Depends(get_protect)
    ) -> Response:
        try:
            payload = await client.snapshot(camera_id)
        except ProtectError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return Response(
            content=payload,
            media_type="image/jpeg",
            headers={"Cache-Control": "max-age=30"},
        )

    # -- transcript analysis --------------------------------------------------

    def get_analysis_store() -> AnalysisConfigStore:
        return app.state.analysis

    async def _analysis_client() -> AnalysisClient:
        row = await app.state.analysis.get()
        config = AnalysisConfig(
            base_url=row["base_url"],
            api_key=row["api_key"],
            model=row["model"],
            enabled=bool(row["enabled"]),
        )
        if not config.ready:
            raise HTTPException(
                status_code=409,
                detail=(
                    "No analysis endpoint configured. Set one under Analysis in the UI. "
                    "Note a ChatGPT subscription does not include API access -- OpenAI's "
                    "API is billed separately, or point this at a local model."
                ),
            )
        return AnalysisClient(config, client=app.state.http)

    async def _segments_for(job_id: str) -> tuple[dict[str, Any], list[Segment]]:
        job = await app.state.store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="No such job")
        if job["status"] != "completed":
            raise HTTPException(status_code=409, detail="That job has no transcript yet")
        segments = [
            Segment(start=s.get("start", 0.0), end=s.get("end", 0.0), text=s.get("text", ""))
            for s in job["segments"]
        ]
        if not segments and job["text"]:
            segments = [Segment(start=0.0, end=0.0, text=job["text"])]
        return job, segments

    @app.get("/api/analysis", dependencies=[Guard])
    async def get_analysis(store: AnalysisConfigStore = Depends(get_analysis_store)) -> dict:
        row = await store.get()
        config = AnalysisConfig(
            base_url=row["base_url"], model=row["model"], enabled=bool(row["enabled"])
        )
        return {
            "baseUrl": row["base_url"],
            "model": row["model"],
            "enabled": bool(row["enabled"]),
            "hasApiKey": bool(row["api_key"]),
            "ready": config.ready,
        }

    @app.put("/api/analysis", dependencies=[Guard])
    async def save_analysis(
        payload: AnalysisSettings, store: AnalysisConfigStore = Depends(get_analysis_store)
    ) -> dict:
        fields: dict[str, Any] = {
            "base_url": payload.base_url,
            "model": payload.model.strip(),
            "enabled": int(payload.enabled),
        }
        # Omitting the key keeps the stored one; sending "" clears it.
        if payload.api_key is not None:
            fields["api_key"] = payload.api_key
        await store.save(**fields)
        return await get_analysis(store)

    @app.post("/api/analysis/test", dependencies=[Guard])
    async def test_analysis(payload: AnalysisSettings) -> dict:
        stored = await app.state.analysis.get()
        config = AnalysisConfig(
            base_url=payload.base_url,
            # Fall back to the saved key so Test works without retyping it.
            api_key=payload.api_key if payload.api_key is not None else stored["api_key"],
            model=payload.model,
            enabled=True,
        )
        if not config.base_url:
            raise HTTPException(status_code=400, detail="Enter a base URL first")
        client = AnalysisClient(config, client=app.state.http)
        try:
            models = await client.models()
        except Exception as exc:  # noqa: BLE001 - any transport/HTTP error is just "no"
            return {"reachable": False, "models": [], "detail": str(exc)[:300]}
        return {"reachable": True, "models": models[:500]}

    @app.post("/api/jobs/{job_id}/summarize", dependencies=[Guard])
    async def summarize_job(job_id: str) -> dict:
        _, segments = await _segments_for(job_id)
        client = await _analysis_client()
        try:
            result = await client.summarize(segments)
        except (AnalysisError, AnalysisNotConfigured) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        await app.state.store.update(job_id, summary=json.dumps(result))
        return result

    @app.post("/api/jobs/{job_id}/review", dependencies=[Guard])
    async def review_job(job_id: str) -> dict:
        _, segments = await _segments_for(job_id)
        client = await _analysis_client()
        try:
            result = await client.review(segments)
        except (AnalysisError, AnalysisNotConfigured) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        await app.state.store.update(job_id, review=json.dumps(result))
        return result

    @app.post("/api/jobs/{job_id}/ask", dependencies=[Guard])
    async def ask_job(job_id: str, payload: AskRequest) -> dict:
        _, segments = await _segments_for(job_id)
        client = await _analysis_client()
        try:
            return await client.ask(segments, payload.question.strip())
        except (AnalysisError, AnalysisNotConfigured) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    # -- previews -----------------------------------------------------------

    def get_previews() -> PreviewStore:
        return app.state.previews

    @app.post("/api/preview", status_code=202, dependencies=[Guard])
    async def create_preview(
        payload: PreviewRequest,
        manager: JobManager = Depends(get_manager),
        client: ProtectClient = Depends(get_protect),
    ) -> dict[str, Any]:
        span = (payload.end - payload.start).total_seconds()
        if span <= 0:
            raise HTTPException(status_code=400, detail="End must be after start")
        if span > settings.preview_max_seconds:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Preview is limited to {settings.preview_max_seconds // 60} min "
                    f"(this range is {span / 60:.0f} min). Narrow the selection, or raise "
                    "PREVIEW_MAX_SECONDS."
                ),
            )
        if payload.start > datetime.now(tz=UTC):
            raise HTTPException(status_code=400, detail="Start is in the future")

        camera_name = payload.camera_id
        try:
            camera_name = (await client.camera(payload.camera_id)).name
        except ProtectError:
            log.warning("Could not look up camera %s for preview", payload.camera_id)

        record = await manager.create_preview(
            camera_id=payload.camera_id,
            camera_name=camera_name,
            start=payload.start,
            end=payload.end,
        )
        return _public_preview(record)

    @app.get("/api/preview/{preview_id}", dependencies=[Guard])
    async def get_preview(
        preview_id: str, store: PreviewStore = Depends(get_previews)
    ) -> dict[str, Any]:
        record = await store.get(preview_id)
        if record is None:
            raise HTTPException(status_code=404, detail="No such preview")
        return _public_preview(record, include_peaks=True)

    @app.get("/api/preview/{preview_id}/clip", dependencies=[Guard])
    async def preview_clip(
        preview_id: str, store: PreviewStore = Depends(get_previews)
    ) -> FileResponse:
        record = await store.get(preview_id)
        if record is None or not record["clip_path"]:
            raise HTTPException(status_code=404, detail="No such preview")
        path = Path(record["clip_path"])
        if not path.exists():
            raise HTTPException(status_code=404, detail="The preview clip has been cleaned up")
        # FileResponse serves Range requests, which the <video> needs to seek.
        return FileResponse(path, media_type="video/mp4")

    @app.delete("/api/preview/{preview_id}", dependencies=[Guard])
    async def delete_preview(
        preview_id: str, manager: JobManager = Depends(get_manager)
    ) -> dict[str, Any]:
        if not await manager.purge_preview(preview_id):
            raise HTTPException(status_code=404, detail="No such preview")
        return {"deleted": preview_id}

    # -- jobs ---------------------------------------------------------------

    @app.post("/api/jobs", status_code=202, dependencies=[Guard])
    async def create_job(
        payload: JobRequest,
        manager: JobManager = Depends(get_manager),
        client: ProtectClient = Depends(get_protect),
    ) -> dict[str, Any]:
        span = (payload.end - payload.start).total_seconds()
        if span <= 0:
            raise HTTPException(status_code=400, detail="End must be after start")
        if span > settings.max_range_seconds:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Range is {span / 60:.0f} min; the limit is "
                    f"{settings.max_range_seconds / 60:.0f} min (raise MAX_RANGE_SECONDS)"
                ),
            )
        if payload.start > datetime.now(tz=UTC):
            raise HTTPException(status_code=400, detail="Start is in the future")

        camera_name = payload.camera_id
        try:
            camera = await client.camera(payload.camera_id)
            camera_name = camera.name
            if not camera.has_audio:
                log.warning(
                    "Camera %s reports no usable mic; the clip may have no audio track", camera.name
                )
        except ProtectError as exc:
            # Don't block the job on a metadata hiccup -- the export is the real test.
            log.warning("Could not look up camera %s: %s", payload.camera_id, exc)

        # A preview already exported this footage, so reuse its clip and trim
        # to the requested window instead of pulling it from the NVR again.
        preview_id: str | None = None
        clip_path: Path | None = None
        offset = 0.0
        duration: float | None = None
        if payload.preview_id:
            preview = await app.state.previews.get(payload.preview_id)
            if preview is None or preview["status"] != "ready":
                raise HTTPException(status_code=409, detail="That preview is not ready")
            if not preview["clip_path"] or not Path(preview["clip_path"]).exists():
                raise HTTPException(status_code=409, detail="The preview clip has been cleaned up")
            preview_start = datetime.fromisoformat(preview["range_start"])
            preview_end = datetime.fromisoformat(preview["range_end"])
            if payload.start < preview_start or payload.end > preview_end:
                raise HTTPException(
                    status_code=400,
                    detail="The requested range is not inside that preview",
                )
            preview_id = payload.preview_id
            clip_path = Path(preview["clip_path"])
            offset = max(0.0, (payload.start - preview_start).total_seconds())
            duration = span

        return await manager.submit(
            camera_id=payload.camera_id,
            camera_name=camera_name,
            range_start=payload.start,
            range_end=payload.end,
            title=payload.title or f"{camera_name} {payload.start:%Y-%m-%d %H:%M}",
            language=payload.language or settings.whisper_language or None,
            task=payload.task,
            prompt=payload.prompt,
            source="preview" if preview_id else "protect",
            preview_id=preview_id,
            clip_path=clip_path,
            clip_offset=offset,
            clip_duration=duration,
        )

    @app.post("/api/jobs/upload", status_code=202, dependencies=[Guard])
    async def upload_job(
        file: UploadFile = File(...),
        title: str = Form(""),
        language: str = Form(""),
        task: str = Form("transcribe"),
        prompt: str = Form(""),
        manager: JobManager = Depends(get_manager),
    ) -> dict[str, Any]:
        """Transcribe an already-downloaded clip -- handy for testing the pool."""
        safe_name = Path(file.filename or "upload.mp4").name
        destination = settings.clips_dir / f"upload-{secrets.token_hex(4)}-{safe_name}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        with destination.open("wb") as handle:
            while block := await file.read(1 << 20):
                size += len(block)
                handle.write(block)
        if size == 0:
            destination.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail="Uploaded file is empty")
        return await manager.submit(
            title=title or safe_name,
            language=language or settings.whisper_language or None,
            task=task if task in ("transcribe", "translate") else "transcribe",
            prompt=prompt or None,
            source="upload",
            upload_path=destination,
        )

    @app.get("/api/jobs", dependencies=[Guard])
    async def list_jobs(
        status: str | None = None,
        camera_id: str | None = Query(default=None, alias="cameraId"),
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        store: JobStore = Depends(get_store),
    ) -> dict[str, Any]:
        jobs = await store.list(status=status, limit=limit, offset=offset, camera_id=camera_id)
        return {"jobs": [_public_job(job) for job in jobs], "stats": await store.stats()}

    @app.get("/api/jobs/search", dependencies=[Guard])
    async def search_jobs(
        q: str = Query(min_length=1),
        limit: int = Query(default=50, ge=1, le=200),
        store: JobStore = Depends(get_store),
    ) -> dict[str, Any]:
        results = await store.search(q, limit=limit)
        return {
            "query": q,
            "results": [_public_job(job) | {"snippet": job.get("snippet", "")} for job in results],
        }

    @app.get("/api/jobs/stream", dependencies=[Guard])
    async def stream_jobs(manager: JobManager = Depends(get_manager)) -> StreamingResponse:
        """Server-sent events carrying every job state change."""

        async def generator() -> AsyncIterator[bytes]:
            async with manager.events.subscribe() as queue:
                yield b": connected\n\n"
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=20.0)
                    except TimeoutError:
                        yield b": keepalive\n\n"  # keeps proxies from closing the stream
                        continue
                    if "job" in event:
                        event = {**event, "job": _public_job(event["job"])}
                    yield f"data: {json.dumps(event)}\n\n".encode()

        return StreamingResponse(
            generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/jobs/{job_id}", dependencies=[Guard])
    async def get_job(job_id: str, store: JobStore = Depends(get_store)) -> dict[str, Any]:
        job = await store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="No such job")
        return _public_job(job, include_segments=True)

    @app.post("/api/jobs/{job_id}/cancel", dependencies=[Guard])
    async def cancel_job(job_id: str, manager: JobManager = Depends(get_manager)) -> dict[str, Any]:
        if not await manager.cancel(job_id):
            raise HTTPException(status_code=409, detail="Job is not running")
        return {"canceled": job_id}

    @app.post("/api/jobs/{job_id}/retry", status_code=202, dependencies=[Guard])
    async def retry_job(job_id: str, manager: JobManager = Depends(get_manager)) -> dict[str, Any]:
        try:
            job = await manager.retry(job_id)
        except ProtectError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if job is None:
            raise HTTPException(status_code=404, detail="No such job")
        return _public_job(job)

    @app.delete("/api/jobs/{job_id}", dependencies=[Guard])
    async def delete_job(job_id: str, manager: JobManager = Depends(get_manager)) -> dict[str, Any]:
        if not await manager.purge(job_id):
            raise HTTPException(status_code=404, detail="No such job")
        return {"deleted": job_id}

    # -- downloads ----------------------------------------------------------

    @app.get("/api/jobs/{job_id}/transcript.{fmt}", dependencies=[Guard])
    async def download_transcript(
        job_id: str, fmt: str, store: JobStore = Depends(get_store)
    ) -> Response:
        job = await store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="No such job")
        segments = [
            Segment(start=s.get("start", 0.0), end=s.get("end", 0.0), text=s.get("text", ""))
            for s in job["segments"]
        ]
        slug = _slug(job["title"] or job_id)
        if fmt == "txt":
            body, mime = (job["text"] or "") + "\n", "text/plain"
        elif fmt == "srt":
            body, mime = to_srt(segments), "application/x-subrip"
        elif fmt == "vtt":
            body, mime = to_vtt(segments), "text/vtt"
        elif fmt == "log":
            start = job["range_start"]
            if not start:
                raise HTTPException(status_code=400, detail="Job has no wall-clock start")
            body = to_wallclock_text(segments, datetime.fromisoformat(start))
            mime = "text/plain"
        elif fmt == "json":
            body = json.dumps(_public_job(job, include_segments=True), indent=2)
            mime = "application/json"
        else:
            raise HTTPException(status_code=400, detail="Format must be txt, srt, vtt, log or json")
        extension = "txt" if fmt == "log" else fmt
        return Response(
            content=body,
            media_type=mime,
            headers={"Content-Disposition": f'attachment; filename="{slug}.{extension}"'},
        )

    @app.get("/api/jobs/{job_id}/clip", dependencies=[Guard])
    async def download_clip(job_id: str, store: JobStore = Depends(get_store)) -> FileResponse:
        job = await store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="No such job")
        path = Path(job["clip_path"]) if job["clip_path"] else None
        if path is None or not path.exists():
            raise HTTPException(
                status_code=404, detail="Clip is not on disk (KEEP_CLIPS may be off)"
            )
        # FileResponse handles Range requests, so the UI's <video> can seek.
        return FileResponse(path, media_type="video/mp4", filename=f"{_slug(job['title'])}.mp4")

    @app.get("/api/jobs/{job_id}/audio", dependencies=[Guard])
    async def download_audio(job_id: str, store: JobStore = Depends(get_store)) -> FileResponse:
        job = await store.get(job_id)
        if job is None or not job["audio_path"] or not Path(job["audio_path"]).exists():
            raise HTTPException(status_code=404, detail="Audio is not on disk")
        return FileResponse(job["audio_path"], media_type="audio/wav")

    # -- UI -----------------------------------------------------------------

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> Response:
        page = STATIC_DIR / "index.html"
        if not page.exists():  # pragma: no cover
            return PlainTextResponse("UI assets are missing from the image", status_code=500)
        return HTMLResponse(page.read_text(encoding="utf-8"))

    @app.exception_handler(NoHealthyInstances)
    async def _no_instances(_: Request, exc: NoHealthyInstances) -> Response:
        return Response(
            content=json.dumps({"detail": str(exc)}), status_code=503, media_type="application/json"
        )

    return app


def _slug(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_ ." else "-" for ch in value).strip()
    return ("-".join(cleaned.split()) or "transcript")[:80]


def _public_preview(record: dict[str, Any], include_peaks: bool = False) -> dict[str, Any]:
    payload = {
        "id": record["id"],
        "status": record["status"],
        "cameraId": record["camera_id"],
        "cameraName": record["camera_name"],
        "rangeStart": record["range_start"],
        "rangeEnd": record["range_end"],
        "duration": record["duration"],
        "hasAudio": bool(record["has_audio"]),
        "clipBytes": record["clip_bytes"],
        "error": record["error"],
        "createdAt": record["created_at"],
    }
    if include_peaks:
        payload["peaks"] = record["peaks"]
    return payload


def _public_job(job: dict[str, Any], include_segments: bool = False) -> dict[str, Any]:
    """Map a DB row to the camelCase shape the UI consumes."""
    payload = {
        "id": job["id"],
        "status": job["status"],
        "stage": job["stage"],
        "progress": round(float(job["progress"] or 0.0), 4),
        "title": job["title"],
        "cameraId": job["camera_id"],
        "cameraName": job["camera_name"],
        "rangeStart": job["range_start"],
        "rangeEnd": job["range_end"],
        "source": job["source"],
        "language": job["language"],
        "detectedLanguage": job["detected_language"],
        "task": job["task"],
        "audioSeconds": job["audio_seconds"],
        "clipBytes": job["clip_bytes"],
        "chunkCount": job["chunk_count"],
        "chunksDone": job["chunks_done"],
        "instances": job["instances"],
        "error": job["error"],
        "createdAt": job["created_at"],
        "startedAt": job["started_at"],
        "finishedAt": job["finished_at"],
        "hasClip": bool(job["clip_path"]),
        "previewId": job["preview_id"],
        # Playback offset: a preview-backed clip holds the whole previewed range,
        # so segment timestamps need shifting to line up with the video.
        "clipOffset": job["clip_offset"] or 0.0,
        "textLength": len(job["text"] or ""),
    }
    if include_segments:
        payload["text"] = job["text"]
        payload["segments"] = job["segments"]
        for key in ("summary", "review"):
            raw = job.get(key)
            if raw:
                try:
                    payload[key] = json.loads(raw)
                except (TypeError, ValueError):
                    payload[key] = None
    else:
        payload["preview"] = (job["text"] or "")[:280]
    return payload


app = create_app()
