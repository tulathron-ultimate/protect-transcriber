"""Adapters for self-hosted Whisper HTTP servers, plus a load-balancing pool.

Three server flavours are supported, because the popular Unraid containers do not
agree on an API:

* ``openai`` -- ``POST /v1/audio/transcriptions``. Speaches, faster-whisper-server,
  LocalAI, and anything else OpenAI-compatible.
* ``asr_webservice`` -- ``POST /asr``. ``onerahmet/openai-whisper-asr-webservice``.
* ``whisper_cpp`` -- ``POST /inference``. whisper.cpp's bundled server.

Set ``kind`` to ``auto`` (the default) and the pool probes each instance once and
remembers what answered.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .config import BackendKind, Settings, WhisperInstance
from .transcript import Segment, TranscriptResult

log = logging.getLogger(__name__)


class WhisperError(RuntimeError):
    """A Whisper instance failed to transcribe."""


class NoHealthyInstances(WhisperError):
    """Every configured instance is unreachable."""


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_segments(payload: Any) -> list[Segment]:
    """Read segments out of any of the response shapes these servers return."""
    segments: list[Segment] = []
    for item in payload or []:
        if not isinstance(item, dict):
            continue
        start = _as_float(item.get("start"))
        end = _as_float(item.get("end"))
        text = (item.get("text") or "").strip()
        if start is None or not text:
            continue
        confidence = None
        # faster-whisper reports avg_logprob; map it to a rough 0..1 for the UI.
        logprob = _as_float(item.get("avg_logprob"))
        if logprob is not None:
            confidence = max(0.0, min(1.0, 1.0 + logprob / 5.0))
        segments.append(
            Segment(
                start=start, end=end if end is not None else start, text=text, confidence=confidence
            )
        )
    return segments


def _result_from_json(payload: Any, *, backend: str) -> TranscriptResult:
    if isinstance(payload, str):
        return TranscriptResult(text=payload.strip(), backend=backend)
    if not isinstance(payload, dict):
        raise WhisperError(
            f"{backend} returned an unexpected response type: {type(payload).__name__}"
        )
    segments = _parse_segments(payload.get("segments"))
    text = (payload.get("text") or "").strip()
    if not text and segments:
        text = " ".join(s.text for s in segments).strip()
    return TranscriptResult(
        text=text,
        segments=segments,
        language=payload.get("language") or payload.get("detected_language"),
        duration=_as_float(payload.get("duration")),
        backend=backend,
    )


class WhisperBackend(ABC):
    """One way of talking to a Whisper server."""

    kind: BackendKind

    def __init__(self, instance: WhisperInstance) -> None:
        self.instance = instance

    def _auth_headers(self) -> dict[str, str]:
        if self.instance.api_key:
            return {"Authorization": f"Bearer {self.instance.api_key}"}
        return {}

    @abstractmethod
    async def probe(self, client: httpx.AsyncClient) -> bool:
        """Cheap check that this instance speaks this dialect and is up."""

    @abstractmethod
    async def transcribe(
        self,
        client: httpx.AsyncClient,
        audio: Path,
        *,
        language: str | None,
        task: str,
        vad_filter: bool,
        prompt: str | None = None,
    ) -> TranscriptResult:
        """Transcribe one audio file."""

    async def _files(self, audio: Path, field_name: str) -> dict[str, tuple[str, bytes, str]]:
        """Build the multipart payload, reading off-loop (chunks run ~10 MB)."""
        data = await asyncio.to_thread(audio.read_bytes)
        return {field_name: (audio.name, data, "audio/wav")}


class OpenAIBackend(WhisperBackend):
    """Speaches / faster-whisper-server / any OpenAI-compatible endpoint."""

    kind: BackendKind = "openai"

    async def probe(self, client: httpx.AsyncClient) -> bool:
        response = await client.get(
            f"{self.instance.url}/v1/models", headers=self._auth_headers(), timeout=10.0
        )
        if response.status_code >= 400:
            return False
        try:
            payload = response.json()
        except ValueError:
            return False
        return isinstance(payload, dict) and "data" in payload

    async def models(self, client: httpx.AsyncClient) -> list[str]:
        response = await client.get(
            f"{self.instance.url}/v1/models", headers=self._auth_headers(), timeout=10.0
        )
        response.raise_for_status()
        data = response.json().get("data") or []
        return [str(item.get("id")) for item in data if isinstance(item, dict) and item.get("id")]

    async def transcribe(
        self,
        client: httpx.AsyncClient,
        audio: Path,
        *,
        language: str | None,
        task: str,
        vad_filter: bool,
        prompt: str | None = None,
    ) -> TranscriptResult:
        data: dict[str, Any] = {
            "model": self.instance.model,
            "response_format": "verbose_json",
            # Segment granularity is what the UI needs; word-level would bloat the payload.
            "timestamp_granularities[]": "segment",
        }
        if language:
            data["language"] = language
        if prompt:
            data["prompt"] = prompt
        if task == "translate":
            # The OpenAI-shaped API has a separate translation route.
            endpoint = f"{self.instance.url}/v1/audio/translations"
        else:
            endpoint = f"{self.instance.url}/v1/audio/transcriptions"
        if vad_filter:
            data["vad_filter"] = "true"  # faster-whisper-server extension; ignored elsewhere

        response = await client.post(
            endpoint,
            data=data,
            files=await self._files(audio, "file"),
            headers=self._auth_headers(),
        )
        if response.status_code >= 400:
            raise WhisperError(
                f"{self.instance.name}: HTTP {response.status_code} from {endpoint} "
                f"-- {response.text[:300]}"
            )
        return _result_from_json(response.json(), backend=self.kind)


class AsrWebserviceBackend(WhisperBackend):
    """``onerahmet/openai-whisper-asr-webservice``."""

    kind: BackendKind = "asr_webservice"

    async def probe(self, client: httpx.AsyncClient) -> bool:
        response = await client.get(
            f"{self.instance.url}/openapi.json", headers=self._auth_headers(), timeout=10.0
        )
        if response.status_code >= 400:
            return False
        try:
            paths = response.json().get("paths") or {}
        except ValueError:
            return False
        return "/asr" in paths

    async def transcribe(
        self,
        client: httpx.AsyncClient,
        audio: Path,
        *,
        language: str | None,
        task: str,
        vad_filter: bool,
        prompt: str | None = None,
    ) -> TranscriptResult:
        params: dict[str, Any] = {
            "task": task,
            "output": "json",
            "encode": "true",
            "word_timestamps": "false",
            "vad_filter": "true" if vad_filter else "false",
        }
        if language:
            params["language"] = language
        if prompt:
            params["initial_prompt"] = prompt

        endpoint = f"{self.instance.url}/asr"
        response = await client.post(
            endpoint,
            params=params,
            files=await self._files(audio, "audio_file"),
            headers=self._auth_headers(),
        )
        if response.status_code >= 400:
            raise WhisperError(
                f"{self.instance.name}: HTTP {response.status_code} from {endpoint} "
                f"-- {response.text[:300]}"
            )
        try:
            payload = response.json()
        except ValueError:
            # output=json is documented, but older builds hand back plain text.
            return TranscriptResult(text=response.text.strip(), backend=self.kind)
        return _result_from_json(payload, backend=self.kind)


class WhisperCppBackend(WhisperBackend):
    """whisper.cpp's ``server`` example."""

    kind: BackendKind = "whisper_cpp"

    async def probe(self, client: httpx.AsyncClient) -> bool:
        # No discovery endpoint; a bare GET of /inference answers 405/400 when alive.
        try:
            response = await client.get(f"{self.instance.url}/inference", timeout=10.0)
        except httpx.HTTPError:
            return False
        return response.status_code in (400, 404, 405, 422, 500)

    async def transcribe(
        self,
        client: httpx.AsyncClient,
        audio: Path,
        *,
        language: str | None,
        task: str,
        vad_filter: bool,
        prompt: str | None = None,
    ) -> TranscriptResult:
        data: dict[str, Any] = {"response_format": "verbose_json", "temperature": "0"}
        if language:
            data["language"] = language
        if prompt:
            data["prompt"] = prompt
        if task == "translate":
            data["translate"] = "true"
        endpoint = f"{self.instance.url}/inference"
        response = await client.post(
            endpoint,
            data=data,
            files=await self._files(audio, "file"),
            headers=self._auth_headers(),
        )
        if response.status_code >= 400:
            raise WhisperError(
                f"{self.instance.name}: HTTP {response.status_code} from {endpoint} "
                f"-- {response.text[:300]}"
            )
        return _result_from_json(response.json(), backend=self.kind)


BACKENDS: dict[str, type[WhisperBackend]] = {
    "openai": OpenAIBackend,
    "asr_webservice": AsrWebserviceBackend,
    "whisper_cpp": WhisperCppBackend,
}
# Probe order for kind="auto" -- cheapest and most specific checks first.
_PROBE_ORDER: tuple[str, ...] = ("openai", "asr_webservice", "whisper_cpp")


def build_backend(instance: WhisperInstance, kind: BackendKind | None = None) -> WhisperBackend:
    resolved = kind or instance.resolved_kind or instance.kind
    if resolved in (None, "auto"):
        raise WhisperError(f"Backend kind for {instance.name} is not resolved yet")
    try:
        return BACKENDS[resolved](instance)
    except KeyError as exc:  # pragma: no cover - guarded by config validation
        raise WhisperError(f"Unknown Whisper backend kind {resolved!r}") from exc


@dataclass
class InstanceState:
    """Live health and load for one instance."""

    instance: WhisperInstance
    backend: WhisperBackend | None = None
    healthy: bool = False
    detail: str = "not probed"
    in_flight: int = 0
    completed: int = 0
    failed: int = 0
    total_audio_seconds: float = 0.0
    total_wall_seconds: float = 0.0
    last_checked: float = 0.0
    semaphore: asyncio.Semaphore = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.semaphore = asyncio.Semaphore(self.instance.concurrency)

    @property
    def speed(self) -> float | None:
        """Realtime factor -- audio seconds transcribed per wall second."""
        if self.total_wall_seconds <= 0:
            return None
        return self.total_audio_seconds / self.total_wall_seconds

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.instance.name,
            "url": self.instance.url,
            "kind": self.instance.resolved_kind or self.instance.kind,
            "model": self.instance.model,
            "concurrency": self.instance.concurrency,
            "healthy": self.healthy,
            "detail": self.detail,
            "inFlight": self.in_flight,
            "completed": self.completed,
            "failed": self.failed,
            "realtimeFactor": round(self.speed, 2) if self.speed else None,
        }


class WhisperPool:
    """Dispatches chunks across instances, respecting each one's concurrency."""

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        instances: list[WhisperInstance] | None = None,
    ) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                30.0, read=settings.whisper_timeout, write=settings.whisper_timeout
            )
        )
        # `instances` comes from the database when the app runs; falling back to
        # the parsed env keeps the pool usable on its own (and in tests).
        self.states: list[InstanceState] = [
            InstanceState(instance=inst)
            for inst in (settings.instances if instances is None else instances)
            if inst.enabled
        ]
        self._probe_lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @property
    def client(self) -> httpx.AsyncClient:
        """The shared HTTP client, so callers can probe without a second pool."""
        return self._client

    def reload(self, instances: list[WhisperInstance]) -> list[InstanceState]:
        """Swap in a new set of instances without disturbing in-flight work.

        An instance whose connection settings are unchanged keeps its existing
        state, so health, throughput history and its semaphore survive an edit to
        an unrelated row. A task already running holds its own reference to the
        state object it picked, so a removed or rebuilt instance still finishes
        cleanly -- it just stops receiving new chunks.
        """
        by_name = {state.instance.name: state for state in self.states}
        rebuilt: list[InstanceState] = []
        for instance in instances:
            if not instance.enabled:
                continue
            existing = by_name.get(instance.name)
            if existing is not None and existing.instance.same_runtime(instance):
                # Carry the resolved backend kind across so no re-probe is needed.
                instance.resolved_kind = existing.instance.resolved_kind
                existing.instance = instance
                rebuilt.append(existing)
            else:
                rebuilt.append(InstanceState(instance=instance))
        self.states = rebuilt
        log.info(
            "Whisper pool reloaded: %d instance(s) -- %s",
            len(rebuilt),
            ", ".join(state.instance.name for state in rebuilt) or "none",
        )
        return self.states

    @property
    def total_capacity(self) -> int:
        return sum(s.instance.concurrency for s in self.states if s.healthy)

    async def probe_all(self, force: bool = False) -> list[InstanceState]:
        """Health-check every instance, resolving ``auto`` kinds on the way."""
        async with self._probe_lock:
            await asyncio.gather(*(self._probe_one(state, force) for state in self.states))
        return self.states

    async def _probe_one(self, state: InstanceState, force: bool) -> None:
        # Results are cached briefly so a UI poll does not hammer the containers.
        if not force and state.healthy and (time.monotonic() - state.last_checked) < 30:
            return
        state.last_checked = time.monotonic()
        declared = state.instance.kind
        order = _PROBE_ORDER if declared == "auto" else (declared,)
        for kind in order:
            backend = BACKENDS[kind](state.instance)
            try:
                ok = await backend.probe(self._client)
            except httpx.HTTPError as exc:
                state.healthy = False
                state.detail = f"unreachable: {type(exc).__name__}"
                continue
            if ok:
                state.instance.resolved_kind = backend.kind  # type: ignore[assignment]
                state.backend = backend
                state.healthy = True
                state.detail = f"ok ({backend.kind})"
                return
        state.healthy = False
        if state.detail == "not probed" or "unreachable" not in state.detail:
            state.detail = (
                f"reachable but no supported API found at {state.instance.url} "
                f"(tried: {', '.join(order)})"
            )
        log.warning("Whisper instance %s is unhealthy: %s", state.instance.name, state.detail)

    def _pick(self, exclude: set[str] | None = None) -> InstanceState | None:
        """Least-loaded healthy instance, preferring the faster ones on a tie.

        ``exclude`` names instances that already failed this piece of audio. They
        are skipped while any other healthy instance exists, so a retry moves to a
        different container instead of hammering the one that just failed. When
        every healthy instance has failed -- including a single-instance pool --
        the exclusion is dropped so the remaining attempts still happen.
        """
        healthy = [s for s in self.states if s.healthy]
        untried = [s for s in healthy if s.instance.name not in (exclude or ())]
        candidates = untried or healthy
        # Prefer one with a free slot, but fall back to queueing on the semaphore.
        free = [s for s in candidates if s.in_flight < s.instance.concurrency]
        available = free or candidates
        if not available:
            return None
        return min(
            available,
            key=lambda s: (
                s.in_flight / max(1, s.instance.concurrency),
                -(s.speed or 0.0),
                random.random(),
            ),
        )

    async def transcribe_file(
        self,
        audio: Path,
        *,
        language: str | None = None,
        task: str | None = None,
        prompt: str | None = None,
        audio_seconds: float | None = None,
    ) -> TranscriptResult:
        """Transcribe one file, retrying on a different instance when one fails."""
        await self.probe_all()
        if not any(s.healthy for s in self.states):
            raise NoHealthyInstances(
                "No Whisper instance is reachable. Check WHISPER_INSTANCES and that the "
                "containers are running: "
                + "; ".join(f"{s.instance.name}={s.detail}" for s in self.states)
            )

        language = language if language is not None else (self._settings.whisper_language or None)
        task = task or self._settings.whisper_task
        attempts = max(1, self._settings.whisper_max_attempts)
        tried: list[str] = []
        last_error: Exception | None = None

        for attempt in range(attempts):
            state = self._pick(exclude=set(tried))
            if state is None or state.backend is None:
                break
            async with state.semaphore:
                state.in_flight += 1
                started = time.monotonic()
                try:
                    result = await state.backend.transcribe(
                        self._client,
                        audio,
                        language=language,
                        task=task,
                        vad_filter=self._settings.whisper_vad_filter,
                        prompt=prompt,
                    )
                except (WhisperError, httpx.HTTPError) as exc:
                    state.failed += 1
                    last_error = exc
                    tried.append(state.instance.name)
                    log.warning(
                        "Whisper attempt %d/%d on %s failed: %s",
                        attempt + 1,
                        attempts,
                        state.instance.name,
                        exc,
                    )
                    # A failure may mean the container died; re-probe before retrying.
                    await self._probe_one(state, force=True)
                    continue
                else:
                    elapsed = time.monotonic() - started
                    state.completed += 1
                    state.total_wall_seconds += elapsed
                    state.total_audio_seconds += audio_seconds or result.duration or 0.0
                    result.instance = state.instance.name
                    return result
                finally:
                    state.in_flight -= 1

        raise WhisperError(
            f"Transcription of {audio.name} failed after {attempts} attempt(s) "
            f"(instances tried: {', '.join(tried) or 'none available'}): {last_error}"
        )
