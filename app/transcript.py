"""Transcript segments: merging chunk results and rendering output formats."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

_WORD = re.compile(r"[\w']+", re.UNICODE)


@dataclass(slots=True)
class Segment:
    """One timed span of speech, in seconds relative to the start of the clip."""

    start: float
    end: float
    text: str
    speaker: str | None = None
    confidence: float | None = None

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "text": self.text,
        }
        if self.speaker:
            data["speaker"] = self.speaker
        if self.confidence is not None:
            data["confidence"] = round(self.confidence, 4)
        return data


@dataclass(slots=True)
class TranscriptResult:
    """What a single Whisper call returned."""

    text: str
    segments: list[Segment] = field(default_factory=list)
    language: str | None = None
    duration: float | None = None
    backend: str | None = None
    instance: str | None = None


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _strip_repeated_prefix(previous_text: str, text: str, max_words: int = 12) -> str:
    """Drop a leading phrase already present at the tail of ``previous_text``.

    Chunk overlap means the same few words can be transcribed twice. Compare word
    sequences (case- and punctuation-insensitive) and cut the duplicated prefix
    off the newer segment, longest match first.
    """
    prev_words = _words(previous_text)
    new_words = _words(text)
    if not prev_words or not new_words:
        return text
    for size in range(min(max_words, len(new_words), len(prev_words)), 1, -1):
        if prev_words[-size:] == new_words[:size]:
            # Walk the raw string forward past `size` words to keep punctuation.
            matches = list(_WORD.finditer(text))
            if len(matches) > size:
                return text[matches[size].start() :].lstrip(" ,.;:-")
            return ""
    return text


def merge_chunk_segments(
    results: list[tuple[float, float, list[Segment]]],
    *,
    dedupe_text: bool = True,
) -> list[Segment]:
    """Stitch per-chunk segments into one clip-relative timeline.

    ``results`` is ``(chunk_start, nominal_start, segments)`` per chunk, in order,
    with segment times relative to that chunk's own audio. Segments living
    entirely inside the overlap region (before ``nominal_start``) are dropped,
    because the previous chunk already covered that audio with more context.
    """
    merged: list[Segment] = []
    for chunk_start, nominal_start, segments in results:
        overlap = max(0.0, nominal_start - chunk_start)
        for segment in segments:
            text = segment.text.strip()
            if not text:
                continue
            # A segment whose midpoint falls in the overlap belongs to the
            # previous chunk; one that straddles the boundary is kept here.
            midpoint = (segment.start + segment.end) / 2.0
            if overlap > 0 and midpoint < overlap:
                continue
            absolute = Segment(
                start=max(0.0, chunk_start + segment.start),
                end=max(0.0, chunk_start + segment.end),
                text=text,
                speaker=segment.speaker,
                confidence=segment.confidence,
            )
            if absolute.end < absolute.start:
                absolute.end = absolute.start
            if merged and dedupe_text:
                trimmed = _strip_repeated_prefix(merged[-1].text, absolute.text)
                if not trimmed:
                    continue
                absolute.text = trimmed
            merged.append(absolute)

    merged.sort(key=lambda s: (s.start, s.end))
    return merged


def segments_to_text(segments: list[Segment]) -> str:
    return " ".join(s.text.strip() for s in segments if s.text.strip()).strip()


def _clock(seconds: float, *, comma: bool) -> str:
    seconds = max(0.0, seconds)
    whole = int(seconds)
    millis = int(round((seconds - whole) * 1000))
    if millis == 1000:  # rounding pushed us to the next second
        whole, millis = whole + 1, 0
    hours, remainder = divmod(whole, 3600)
    minutes, secs = divmod(remainder, 60)
    sep = "," if comma else "."
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{sep}{millis:03d}"


def to_srt(segments: list[Segment]) -> str:
    lines: list[str] = []
    for index, segment in enumerate(segments, start=1):
        end = max(segment.end, segment.start + 0.1)
        lines += [
            str(index),
            f"{_clock(segment.start, comma=True)} --> {_clock(end, comma=True)}",
            segment.text.strip(),
            "",
        ]
    return "\n".join(lines)


def to_vtt(segments: list[Segment]) -> str:
    lines = ["WEBVTT", ""]
    for segment in segments:
        end = max(segment.end, segment.start + 0.1)
        lines += [
            f"{_clock(segment.start, comma=False)} --> {_clock(end, comma=False)}",
            segment.text.strip(),
            "",
        ]
    return "\n".join(lines)


def to_wallclock_text(segments: list[Segment], clip_start: datetime) -> str:
    """Plain text with real timestamps, for pasting into an incident log."""
    if clip_start.tzinfo is None:
        clip_start = clip_start.replace(tzinfo=UTC)
    lines: list[str] = []
    for segment in segments:
        stamp = clip_start + timedelta(seconds=segment.start)
        lines.append(f"[{stamp.strftime('%Y-%m-%d %H:%M:%S')}] {segment.text.strip()}")
    return "\n".join(lines)
