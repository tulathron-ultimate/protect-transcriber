"""The transcription pipeline.

One job walks a clip through five stages:

1. **export** -- stream the mp4 for the selected range out of UniFi Protect
2. **extract** -- pull the audio track down to mono 16 kHz WAV with ffmpeg
3. **chunk** -- split on silence near the target interval
4. **transcribe** -- fan the chunks across every healthy Whisper instance at once
5. **finalize** -- stitch segments back onto one timeline and write txt/srt/vtt/json

Jobs run on asyncio tasks tracked by id, so they can be cancelled, and every state
change is published to subscribers for the UI's live progress stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import media
from . import transcript as tx
from .config import Settings
from .protect import ProtectClient, ProtectError
from .store import JobStore, PreviewStore
from .whisper import WhisperError, WhisperPool

log = logging.getLogger(__name__)

# Fractions of overall progress each stage is worth, so the bar moves sensibly.
_STAGE_FLOOR = {
    "queued": 0.0,
    "exporting": 0.02,
    "extracting": 0.30,
    "chunking": 0.36,
    "transcribing": 0.40,
    "finalizing": 0.95,
}


class JobCanceled(Exception):
    """Raised inside a job when the user cancels it."""


class EventBus:
    """Fan-out of job updates to any number of SSE subscribers."""

    def __init__(self, max_queue: int = 100) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._max_queue = max_queue

    def publish(self, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A subscriber that cannot keep up just misses intermediate frames;
                # the UI re-syncs from /api/jobs on the next full update.
                log.debug("Dropping event for a slow SSE subscriber")

    @contextlib.asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._max_queue)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)


class JobManager:
    def __init__(
        self,
        settings: Settings,
        store: JobStore,
        pool: WhisperPool,
        protect: ProtectClient,
        previews: PreviewStore | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.previews = previews or PreviewStore(settings.db_path)
        self.pool = pool
        self.protect = protect
        self.events = EventBus()
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        self._running: dict[str, asyncio.Task[None]] = {}
        self._previews: dict[str, asyncio.Task[None]] = {}
        self._canceled: set[str] = set()

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        count = max(1, self.settings.max_concurrent_jobs)
        for index in range(count):
            self._workers.append(
                asyncio.create_task(self._worker(index), name=f"job-worker-{index}")
            )
        log.info("Job manager started with %d worker(s)", count)

    async def stop(self) -> None:
        tasks = [*self._workers, *self._running.values(), *self._previews.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._workers.clear()
        self._running.clear()
        self._previews.clear()

    async def _worker(self, index: int) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                if job_id in self._canceled:
                    self._canceled.discard(job_id)
                    continue
                task = asyncio.create_task(self._run_job(job_id), name=f"job-{job_id}")
                self._running[job_id] = task
                try:
                    await task
                except asyncio.CancelledError:
                    log.info("Job %s was cancelled", job_id)
                except Exception:  # pragma: no cover - defensive
                    log.exception("Job %s crashed outside its error handler", job_id)
                finally:
                    self._running.pop(job_id, None)
            finally:
                self._queue.task_done()

    # -- public API ---------------------------------------------------------

    async def submit(
        self,
        *,
        camera_id: str = "",
        camera_name: str = "",
        range_start: datetime | None = None,
        range_end: datetime | None = None,
        title: str = "",
        language: str | None = None,
        task: str = "transcribe",
        prompt: str | None = None,
        source: str = "protect",
        upload_path: Path | None = None,
        preview_id: str | None = None,
        clip_path: Path | None = None,
        clip_offset: float = 0.0,
        clip_duration: float | None = None,
    ) -> dict[str, Any]:
        job_id = uuid.uuid4().hex[:12]
        record = await self.store.create(
            {
                "id": job_id,
                "status": "queued",
                "stage": "queued",
                "title": title or camera_name or "clip",
                "camera_id": camera_id,
                "camera_name": camera_name,
                "range_start": range_start.isoformat() if range_start else None,
                "range_end": range_end.isoformat() if range_end else None,
                "source": source,
                "language": language,
                "task": task,
                "prompt": prompt,
                "clip_path": str(upload_path or clip_path) if (upload_path or clip_path) else None,
                "preview_id": preview_id,
                "clip_offset": clip_offset,
                "clip_duration": clip_duration,
            }
        )
        self.events.publish({"type": "job.created", "job": record})
        await self._queue.put(job_id)
        return record

    async def cancel(self, job_id: str) -> bool:
        task = self._running.get(job_id)
        self._canceled.add(job_id)
        if task is not None:
            task.cancel()
        record = await self.store.get(job_id)
        if record is None:
            return False
        if record["status"] in ("completed", "failed", "canceled"):
            return False
        await self._set(job_id, status="canceled", stage="", finished_at=_now_iso())
        return True

    async def retry(self, job_id: str) -> dict[str, Any] | None:
        record = await self.store.get(job_id)
        if record is None:
            return None
        start = _parse_dt(record["range_start"])
        end = _parse_dt(record["range_end"])
        if record["source"] in ("upload", "preview"):
            clip = record.get("clip_path")
            if not clip or not Path(clip).exists():
                raise ProtectError(
                    f"The {record['source']} clip for this job is gone; make the selection again."
                )
        elif not (start and end and record["camera_id"]):
            raise ProtectError("This job has no camera/time range to re-export.")
        return await self.submit(
            camera_id=record["camera_id"],
            camera_name=record["camera_name"],
            range_start=start,
            range_end=end,
            title=record["title"],
            language=record["language"],
            task=record["task"],
            prompt=record["prompt"],
            source=record["source"],
            upload_path=Path(record["clip_path"]) if record["source"] == "upload" else None,
            preview_id=record["preview_id"],
            clip_path=Path(record["clip_path"]) if record["source"] == "preview" else None,
            clip_offset=record["clip_offset"],
            clip_duration=record["clip_duration"],
        )

    async def purge(self, job_id: str) -> bool:
        """Cancel if running, delete the row, and remove the files on disk."""
        await self.cancel(job_id)
        record = await self.store.delete(job_id)
        if record is None:
            return False
        _remove_artifacts(record, self.settings)
        self.events.publish({"type": "job.deleted", "jobId": job_id})
        return True

    # -- internals ----------------------------------------------------------

    async def _set(self, job_id: str, **fields: Any) -> dict[str, Any] | None:
        if "stage" in fields and "progress" not in fields:
            floor = _STAGE_FLOOR.get(fields["stage"])
            if floor is not None:
                fields["progress"] = floor
        record = await self.store.update(job_id, **fields)
        if record is not None:
            self.events.publish({"type": "job.updated", "job": record})
        return record

    def _check_canceled(self, job_id: str) -> None:
        if job_id in self._canceled:
            raise JobCanceled(job_id)

    async def _run_job(self, job_id: str) -> None:
        record = await self.store.get(job_id)
        if record is None:
            return
        settings = self.settings
        clip_path: Path | None = None
        audio_path: Path | None = None
        chunk_dir = settings.audio_dir / job_id
        try:
            await self._set(
                job_id, status="exporting", stage="exporting", started_at=_now_iso(), error=None
            )

            # 1. get a clip -- uploaded, already exported for a preview, or fetched now
            if record["source"] in ("upload", "preview"):
                clip_path = Path(record["clip_path"] or "")
                if not clip_path.exists():
                    raise ProtectError(
                        f"The {record['source']} clip is no longer on disk; re-run the selection."
                    )
            else:
                clip_path = await self._export(job_id, record)

            clip_bytes = clip_path.stat().st_size
            await self._set(job_id, clip_path=str(clip_path), clip_bytes=clip_bytes)
            self._check_canceled(job_id)

            # 2. audio
            await self._set(job_id, status="extracting", stage="extracting")
            audio_path = settings.audio_dir / f"{job_id}.wav"
            # A preview-backed job trims here rather than re-exporting: the clip
            # covers the whole previewed range, the job may want part of it.
            await media.extract_audio(
                clip_path,
                audio_path,
                ffmpeg=settings.ffmpeg_path,
                start=record["clip_offset"] or None,
                duration=record["clip_duration"] or None,
            )
            info = await media.inspect(audio_path, settings.ffprobe_path)
            if info.duration <= 0:
                raise media.MediaError("The extracted audio is empty")
            await self._set(
                job_id, audio_path=str(audio_path), audio_seconds=round(info.duration, 2)
            )
            self._check_canceled(job_id)

            # 3. chunks
            await self._set(job_id, status="transcribing", stage="chunking")
            chunks = await media.split_audio(
                audio_path,
                chunk_dir,
                chunk_seconds=settings.chunk_seconds,
                overlap=settings.chunk_overlap_seconds,
                snap_window=settings.silence_snap_window,
                ffmpeg=settings.ffmpeg_path,
                ffprobe=settings.ffprobe_path,
            )
            await self._set(job_id, stage="transcribing", chunk_count=len(chunks), chunks_done=0)
            self._check_canceled(job_id)

            # 4. transcribe, all chunks in flight at once
            results = await self._transcribe_chunks(job_id, record, chunks)

            # 5. stitch and write
            await self._set(job_id, stage="finalizing")
            segments = tx.merge_chunk_segments(
                [(chunk.start, chunk.nominal_start, result.segments) for chunk, result in results]
            )
            if not segments:
                # No timestamps came back (some servers omit them); fall back to
                # concatenating the plain text so the job is still useful.
                joined = " ".join(r.text for _, r in results if r.text).strip()
                if joined:
                    segments = [tx.Segment(start=0.0, end=info.duration, text=joined)]

            text = tx.segments_to_text(segments)
            languages = [r.language for _, r in results if r.language]
            instances = sorted({r.instance for _, r in results if r.instance})
            await self._write_outputs(job_id, record, segments, text)
            await self._set(
                job_id,
                status="completed",
                stage="",
                progress=1.0,
                text=text,
                segments=[s.as_dict() for s in segments],
                detected_language=languages[0] if languages else None,
                instances=instances,
                finished_at=_now_iso(),
                error=None,
            )
            log.info(
                "Job %s completed: %.0fs of audio, %d segments, %d chars, via %s",
                job_id,
                info.duration,
                len(segments),
                len(text),
                ", ".join(instances) or "?",
            )

        except (JobCanceled, asyncio.CancelledError):
            await self._set(job_id, status="canceled", stage="", finished_at=_now_iso())
            raise
        except Exception as exc:
            log.warning(
                "Job %s failed: %s",
                job_id,
                exc,
                exc_info=not isinstance(exc, (ProtectError, WhisperError, media.MediaError)),
            )
            await self._set(
                job_id, status="failed", stage="", error=str(exc)[:2000], finished_at=_now_iso()
            )
        finally:
            self._canceled.discard(job_id)
            # Chunks are scratch; the full wav and clip are kept for playback.
            await asyncio.to_thread(shutil.rmtree, chunk_dir, True)
            if audio_path and audio_path.exists() and not self.settings.keep_clips:
                await asyncio.to_thread(audio_path.unlink, True)
            if (
                clip_path
                and clip_path.exists()
                and not self.settings.keep_clips
                and record["source"] not in ("upload", "preview")
            ):
                await asyncio.to_thread(clip_path.unlink, True)

    async def _export(self, job_id: str, record: dict[str, Any]) -> Path:
        start = _parse_dt(record["range_start"])
        end = _parse_dt(record["range_end"])
        if not (start and end):
            raise ProtectError("Job has no time range")
        destination = self.settings.clips_dir / f"{job_id}.mp4"
        destination.parent.mkdir(parents=True, exist_ok=True)
        expected_seconds = (end - start).total_seconds()
        # Protect does not send Content-Length for exports, so progress within the
        # export stage is estimated from bytes against a ~1 Mbit/s rough guess.
        estimated_bytes = max(1, int(expected_seconds * 128_000))
        written = 0
        try:
            with destination.open("wb") as handle:
                async for chunk in self.protect.export_clip(record["camera_id"], start, end):
                    self._check_canceled(job_id)
                    handle.write(chunk)
                    written += len(chunk)
                    if written % (4 << 20) < len(chunk):  # roughly every 4 MB
                        fraction = min(0.95, written / estimated_bytes)
                        await self._set(
                            job_id,
                            progress=_STAGE_FLOOR["exporting"]
                            + fraction * (_STAGE_FLOOR["extracting"] - _STAGE_FLOOR["exporting"]),
                            clip_bytes=written,
                        )
        except (JobCanceled, asyncio.CancelledError):
            destination.unlink(missing_ok=True)
            raise
        if written == 0:
            destination.unlink(missing_ok=True)
            raise ProtectError(
                "UniFi Protect returned an empty clip. The camera may have no footage for "
                "that range, or the range may predate the oldest recording."
            )
        return destination

    async def _transcribe_chunks(
        self, job_id: str, record: dict[str, Any], chunks: list[media.Chunk]
    ) -> list[tuple[media.Chunk, tx.TranscriptResult]]:
        done = 0
        lock = asyncio.Lock()
        floor = _STAGE_FLOOR["transcribing"]
        span = _STAGE_FLOOR["finalizing"] - floor

        async def one(chunk: media.Chunk) -> tuple[media.Chunk, tx.TranscriptResult]:
            nonlocal done
            self._check_canceled(job_id)
            result = await self.pool.transcribe_file(
                chunk.path,
                language=record["language"] or None,
                task=record["task"],
                prompt=record["prompt"] or None,
                audio_seconds=chunk.duration,
            )
            async with lock:
                done += 1
                await self._set(
                    job_id,
                    chunks_done=done,
                    progress=floor + span * (done / len(chunks)),
                )
            return chunk, result

        # The pool's per-instance semaphores do the throttling, so every chunk can
        # be launched at once.
        results = await asyncio.gather(*(one(chunk) for chunk in chunks))
        return sorted(results, key=lambda item: item[0].index)

    async def _write_outputs(
        self, job_id: str, record: dict[str, Any], segments: list[tx.Segment], text: str
    ) -> None:
        base = self.settings.transcripts_dir / job_id
        base.mkdir(parents=True, exist_ok=True)
        start = _parse_dt(record["range_start"])
        files = {
            "transcript.txt": text + "\n",
            "transcript.srt": tx.to_srt(segments),
            "transcript.vtt": tx.to_vtt(segments),
            "transcript.json": json.dumps(
                {
                    "jobId": job_id,
                    "title": record["title"],
                    "camera": record["camera_name"],
                    "rangeStart": record["range_start"],
                    "rangeEnd": record["range_end"],
                    "text": text,
                    "segments": [s.as_dict() for s in segments],
                },
                indent=2,
            ),
        }
        if start:
            files["transcript.log.txt"] = tx.to_wallclock_text(segments, start)

        def write_all() -> None:
            for name, content in files.items():
                (base / name).write_text(content, encoding="utf-8")

        await asyncio.to_thread(write_all)

    # -- previews -----------------------------------------------------------

    async def create_preview(
        self, *, camera_id: str, camera_name: str, start: datetime, end: datetime
    ) -> dict[str, Any]:
        """Export a range so it can be watched and its waveform drawn.

        Returns an existing ready preview for the same range when one survives,
        so nudging the selection back and forth does not re-export.
        """
        start_iso = start.isoformat()
        end_iso = end.isoformat()
        existing = await self.previews.find_ready(camera_id, start_iso, end_iso)
        if existing is not None:
            log.info(
                "Reusing preview %s for %s %s..%s", existing["id"], camera_id, start_iso, end_iso
            )
            return existing

        preview_id = uuid.uuid4().hex[:12]
        record = await self.previews.create(
            id=preview_id,
            status="exporting",
            camera_id=camera_id,
            camera_name=camera_name,
            range_start=start_iso,
            range_end=end_iso,
        )
        self.events.publish({"type": "preview.created", "preview": record})
        task = asyncio.create_task(self._run_preview(preview_id), name=f"preview-{preview_id}")
        self._previews[preview_id] = task
        task.add_done_callback(lambda _: self._previews.pop(preview_id, None))
        return record

    async def _set_preview(self, preview_id: str, **fields: Any) -> dict[str, Any] | None:
        record = await self.previews.update(preview_id, **fields)
        if record is not None:
            self.events.publish({"type": "preview.updated", "preview": record})
        return record

    async def _run_preview(self, preview_id: str) -> None:
        record = await self.previews.get(preview_id)
        if record is None:
            return
        settings = self.settings
        clip_path = settings.previews_dir / f"{preview_id}.mp4"
        audio_path = settings.previews_dir / f"{preview_id}.wav"
        try:
            start = _parse_dt(record["range_start"])
            end = _parse_dt(record["range_end"])
            if not (start and end):
                raise ProtectError("Preview has no time range")

            clip_path.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            with clip_path.open("wb") as handle:
                async for chunk in self.protect.export_clip(record["camera_id"], start, end):
                    handle.write(chunk)
                    written += len(chunk)
            if written == 0:
                clip_path.unlink(missing_ok=True)
                raise ProtectError(
                    "UniFi Protect returned an empty clip for that range -- there may be no "
                    "footage there."
                )

            await self._set_preview(
                preview_id, status="processing", clip_path=str(clip_path), clip_bytes=written
            )

            info = await media.inspect(clip_path, settings.ffprobe_path)
            peaks: list[float] = []
            has_audio = info.has_audio
            if has_audio:
                try:
                    await media.extract_audio(clip_path, audio_path, ffmpeg=settings.ffmpeg_path)
                    peaks = await media.waveform_peaks(audio_path, settings.waveform_buckets)
                except media.MediaError as exc:
                    # Video still plays; the waveform is the part that is missing.
                    log.warning("Preview %s has no usable audio: %s", preview_id, exc)
                    has_audio = False

            await self._set_preview(
                preview_id,
                status="ready",
                audio_path=str(audio_path) if has_audio else None,
                duration=round(info.duration, 3),
                has_audio=int(has_audio),
                peaks=peaks,
                error=None,
            )
            log.info(
                "Preview %s ready: %.1fs, %d waveform buckets, audio=%s",
                preview_id,
                info.duration,
                len(peaks),
                has_audio,
            )
        except asyncio.CancelledError:
            clip_path.unlink(missing_ok=True)
            await self._set_preview(preview_id, status="failed", error="Canceled")
            raise
        except Exception as exc:
            log.warning("Preview %s failed: %s", preview_id, exc)
            await self._set_preview(preview_id, status="failed", error=str(exc)[:1000])

    async def purge_preview(self, preview_id: str) -> bool:
        task = self._previews.get(preview_id)
        if task is not None:
            task.cancel()
        record = await self.previews.delete(preview_id)
        if record is None:
            return False
        _remove_preview_files(record)
        self.events.publish({"type": "preview.deleted", "previewId": preview_id})
        return True

    # -- housekeeping -------------------------------------------------------

    async def cleanup_expired(self) -> int:
        """Delete artifacts for jobs past the retention window."""
        removed = 0
        for record in await self.store.expired(self.settings.retention_days):
            _remove_artifacts(record, self.settings)
            await self.store.delete(record["id"])
            removed += 1
        if removed:
            log.info(
                "Retention: removed %d job(s) older than %d days",
                removed,
                self.settings.retention_days,
            )
        return removed

    async def cleanup_previews(self) -> int:
        """Drop previews past their (much shorter) window, keeping any a job uses."""
        keep = await self.previews.referenced_by_jobs()
        removed = 0
        for record in await self.previews.expired(self.settings.preview_retention_hours, keep):
            _remove_preview_files(record)
            await self.previews.delete(record["id"])
            removed += 1
        if removed:
            log.info("Retention: removed %d preview(s)", removed)
        return removed


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _remove_preview_files(record: dict[str, Any]) -> None:
    for key in ("clip_path", "audio_path"):
        raw = record.get(key)
        if raw:
            with contextlib.suppress(OSError):
                Path(raw).unlink(missing_ok=True)


def _remove_artifacts(record: dict[str, Any], settings: Settings) -> None:
    for key in ("clip_path", "audio_path"):
        # The clip under a preview-backed job belongs to the preview, which has
        # its own retention; deleting the job must not break the preview.
        if key == "clip_path" and record.get("preview_id"):
            continue
        raw = record.get(key)
        if raw:
            with contextlib.suppress(OSError):
                Path(raw).unlink(missing_ok=True)
    shutil.rmtree(settings.transcripts_dir / record["id"], ignore_errors=True)
    shutil.rmtree(settings.audio_dir / record["id"], ignore_errors=True)
