"""ffmpeg helpers: probing, audio extraction, and silence-aware chunking.

Whisper accuracy falls off and HTTP transcribe calls time out on very long audio,
so a clip gets split into chunks. Cutting at a fixed interval can slice a word in
half, so boundaries are snapped to a nearby silence when one exists, with a small
overlap as a safety net for the cases where it does not.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_SILENCE_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?[\d.]+)")


class MediaError(RuntimeError):
    """An ffmpeg/ffprobe invocation failed."""


@dataclass(slots=True)
class Chunk:
    """One slice of audio handed to a Whisper instance."""

    index: int
    path: Path
    start: float  # where this chunk's audio begins, in clip-relative seconds
    duration: float
    # Where this chunk's *authoritative* content begins. Equals ``start`` for the
    # first chunk; later chunks read a little early for overlap, and everything
    # before ``nominal_start`` was already transcribed by the previous chunk.
    nominal_start: float = 0.0

    @property
    def end(self) -> float:
        return self.start + self.duration


# Own the timeout here rather than at the caller: an outer asyncio.timeout would
# cancel the await and leave the ffmpeg process running.
async def _run(cmd: list[str], *, timeout: float = 3600.0) -> tuple[int, bytes, bytes]:  # noqa: ASYNC109
    log.debug("run: %s", " ".join(cmd))
    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise MediaError(f"{cmd[0]} timed out after {timeout:.0f}s") from None
    except FileNotFoundError as exc:  # pragma: no cover - environment issue
        raise MediaError(f"{cmd[0]} is not installed") from exc
    return process.returncode or 0, stdout, stderr


def ffmpeg_available(ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> bool:
    return shutil.which(ffmpeg) is not None and shutil.which(ffprobe) is not None


async def probe(path: Path, ffprobe: str = "ffprobe") -> dict:
    """Return the ffprobe JSON for a media file."""
    code, stdout, stderr = await _run(
        [
            ffprobe,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        timeout=120,
    )
    if code != 0:
        raise MediaError(
            f"ffprobe failed for {path.name}: {stderr.decode('utf-8', 'replace')[:400]}"
        )
    try:
        return json.loads(stdout or b"{}")
    except ValueError as exc:
        raise MediaError(f"ffprobe returned unparseable JSON for {path.name}") from exc


def _duration_from_probe(info: dict) -> float:
    fmt = info.get("format") or {}
    if fmt.get("duration"):
        try:
            return float(fmt["duration"])
        except (TypeError, ValueError):
            pass
    best = 0.0
    for stream in info.get("streams") or []:
        try:
            best = max(best, float(stream.get("duration") or 0.0))
        except (TypeError, ValueError):
            continue
    return best


def _has_audio_from_probe(info: dict) -> bool:
    return any((s.get("codec_type") == "audio") for s in info.get("streams") or [])


@dataclass(slots=True)
class MediaInfo:
    duration: float
    has_audio: bool


async def inspect(path: Path, ffprobe: str = "ffprobe") -> MediaInfo:
    info = await probe(path, ffprobe)
    return MediaInfo(duration=_duration_from_probe(info), has_audio=_has_audio_from_probe(info))


async def extract_audio(
    source: Path, destination: Path, *, ffmpeg: str = "ffmpeg", sample_rate: int = 16000
) -> Path:
    """Decode the audio track to mono 16 kHz PCM WAV -- what Whisper wants anyway.

    Doing the resample here rather than inside each Whisper container saves the
    pool a decode step and keeps chunk boundaries exact.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    code, _, stderr = await _run(
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-c:a",
            "pcm_s16le",
            str(destination),
        ]
    )
    if code != 0 or not destination.exists() or destination.stat().st_size == 0:
        detail = stderr.decode("utf-8", "replace")[:400]
        raise MediaError(
            f"Could not extract audio from {source.name}. The clip may have no audio track "
            f"(check that the camera has a mic and that micVolume is not 0). ffmpeg said: {detail}"
        )
    return destination


async def detect_silences(
    path: Path,
    *,
    ffmpeg: str = "ffmpeg",
    noise_db: float = -35.0,
    min_duration: float = 0.35,
) -> list[tuple[float, float]]:
    """Return ``(start, end)`` spans of silence, via ffmpeg's silencedetect filter."""
    code, _, stderr = await _run(
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-i",
            str(path),
            "-af",
            f"silencedetect=noise={noise_db}dB:d={min_duration}",
            "-f",
            "null",
            "-",
        ]
    )
    if code != 0:
        log.warning("silencedetect failed for %s; falling back to fixed cuts", path.name)
        return []
    text = stderr.decode("utf-8", "replace")
    starts = [float(m) for m in _SILENCE_START.findall(text)]
    ends = [float(m) for m in _SILENCE_END.findall(text)]
    spans: list[tuple[float, float]] = []
    for idx, start in enumerate(starts):
        end = ends[idx] if idx < len(ends) else None
        if end is not None and end > start:
            spans.append((start, end))
    return spans


def plan_cut_points(
    duration: float,
    chunk_seconds: float,
    silences: list[tuple[float, float]],
    snap_window: float = 15.0,
) -> list[float]:
    """Decide where to cut, preferring the middle of a nearby silence.

    Returns interior cut points only (never 0 or ``duration``), strictly
    increasing. Pure function so the interesting logic is unit-testable without
    ffmpeg.
    """
    if duration <= 0 or chunk_seconds <= 0 or duration <= chunk_seconds:
        return []

    midpoints = sorted((s + e) / 2.0 for s, e in silences if e > s)
    cuts: list[float] = []
    position = 0.0
    # Keep at least a second of audio on either side of any cut.
    min_gap = 1.0

    while duration - position > chunk_seconds:
        target = position + chunk_seconds
        candidate = target
        best_delta = None
        for mid in midpoints:
            if mid <= position + min_gap or mid >= duration - min_gap:
                continue
            delta = abs(mid - target)
            if delta > snap_window:
                continue
            if best_delta is None or delta < best_delta:
                best_delta, candidate = delta, mid
        if candidate <= position + min_gap:
            candidate = target
        cuts.append(round(candidate, 3))
        position = candidate
    return cuts


async def split_audio(
    path: Path,
    output_dir: Path,
    *,
    chunk_seconds: float,
    overlap: float = 2.0,
    snap_window: float = 15.0,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    detect_silence: bool = True,
) -> list[Chunk]:
    """Split ``path`` into chunks, snapping boundaries to silence where possible.

    Each chunk after the first starts ``overlap`` seconds early so a word spanning
    a boundary is heard at least once in full; the overlap is stitched out when
    segments are merged (see :mod:`app.transcript`).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    info = await inspect(path, ffprobe)
    duration = info.duration
    if duration <= 0:
        raise MediaError(f"{path.name} has zero duration")

    silences = await detect_silences(path, ffmpeg=ffmpeg) if detect_silence else []
    cuts = plan_cut_points(duration, chunk_seconds, silences, snap_window)
    boundaries = [0.0, *cuts, duration]

    chunks: list[Chunk] = []
    for index in range(len(boundaries) - 1):
        nominal_start = boundaries[index]
        nominal_end = boundaries[index + 1]
        read_start = max(0.0, nominal_start - (overlap if index else 0.0))
        read_duration = nominal_end - read_start
        if read_duration <= 0.05:
            continue
        chunk_path = output_dir / f"{path.stem}.part{index:03d}.wav"
        code, _, stderr = await _run(
            [
                ffmpeg,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                # -ss before -i seeks fast; the input is already PCM so it is exact.
                "-ss",
                f"{read_start:.3f}",
                "-t",
                f"{read_duration:.3f}",
                "-i",
                str(path),
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(chunk_path),
            ]
        )
        if code != 0 or not chunk_path.exists():
            raise MediaError(
                f"Failed to cut chunk {index} of {path.name}: "
                f"{stderr.decode('utf-8', 'replace')[:300]}"
            )
        chunks.append(
            Chunk(
                index=len(chunks),
                path=chunk_path,
                start=read_start,
                duration=read_duration,
                nominal_start=nominal_start,
            )
        )

    if not chunks:
        raise MediaError(f"Splitting {path.name} produced no chunks")
    return chunks
