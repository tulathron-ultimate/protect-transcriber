from __future__ import annotations

from datetime import UTC, datetime

from app.transcript import (
    Segment,
    _strip_repeated_prefix,
    merge_chunk_segments,
    segments_to_text,
    to_srt,
    to_vtt,
    to_wallclock_text,
)


def test_segments_are_shifted_onto_the_clip_timeline():
    merged = merge_chunk_segments(
        [
            (0.0, 0.0, [Segment(1.0, 2.0, "first chunk")]),
            (10.0, 10.0, [Segment(0.5, 1.5, "second chunk")]),
        ]
    )
    assert [(s.start, s.text) for s in merged] == [(1.0, "first chunk"), (10.5, "second chunk")]


def test_segments_inside_the_overlap_are_dropped():
    # Chunk two starts reading at 8.0 but only owns audio from 10.0 onward.
    merged = merge_chunk_segments(
        [
            (0.0, 0.0, [Segment(8.2, 9.8, "heard by chunk one")]),
            (8.0, 10.0, [Segment(0.2, 1.8, "heard by chunk one"), Segment(2.0, 3.0, "new words")]),
        ]
    )
    texts = [s.text for s in merged]
    assert texts.count("heard by chunk one") == 1, "the duplicate in the overlap should go"
    assert "new words" in texts


def test_segment_straddling_the_boundary_is_kept():
    # Midpoint 1.5 > overlap 1.0, so this belongs to the new chunk.
    merged = merge_chunk_segments([(9.0, 10.0, [Segment(0.5, 2.5, "spans the cut")])])
    assert [s.text for s in merged] == ["spans the cut"]
    assert merged[0].start == 9.5


def test_repeated_phrase_at_a_boundary_is_trimmed():
    merged = merge_chunk_segments(
        [
            (0.0, 0.0, [Segment(0.0, 2.0, "the package is at the front door")]),
            (10.0, 10.0, [Segment(0.0, 2.0, "at the front door and the dog is barking")]),
        ]
    )
    assert segments_to_text(merged) == "the package is at the front door and the dog is barking"


def test_strip_repeated_prefix_is_punctuation_and_case_insensitive():
    assert (
        _strip_repeated_prefix("hello there, big world", "Big World! how are you") == "how are you"
    )
    assert _strip_repeated_prefix("nothing shared", "totally different") == "totally different"
    # A single shared word is too weak a signal to cut on -- "the" ends plenty of
    # sentences and starts plenty more.
    assert _strip_repeated_prefix("ends with the", "the beginning") == "the beginning"


def test_strip_repeated_prefix_consumes_the_whole_segment_when_fully_duplicated():
    assert _strip_repeated_prefix("say that again please", "that again please") == ""


def test_empty_and_whitespace_segments_are_skipped():
    merged = merge_chunk_segments([(0.0, 0.0, [Segment(0, 1, "   "), Segment(1, 2, "kept")])])
    assert [s.text for s in merged] == ["kept"]


def test_srt_format():
    srt = to_srt([Segment(0.0, 1.5, "one"), Segment(61.25, 62.0, "two")])
    assert srt.splitlines()[:3] == ["1", "00:00:00,000 --> 00:00:01,500", "one"]
    assert "00:01:01,250 --> 00:01:02,000" in srt


def test_vtt_starts_with_the_required_header_and_uses_dots():
    vtt = to_vtt([Segment(3661.0, 3662.0, "late")])
    assert vtt.startswith("WEBVTT")
    assert "01:01:01.000 --> 01:01:02.000" in vtt


def test_zero_length_segment_gets_a_visible_duration():
    assert "00:00:05,000 --> 00:00:05,100" in to_srt([Segment(5.0, 5.0, "blip")])


def test_wallclock_text_uses_real_timestamps():
    start = datetime(2026, 3, 4, 13, 45, 0, tzinfo=UTC)
    text = to_wallclock_text([Segment(75.0, 77.0, "someone at the gate")], start)
    assert text == "[2026-03-04 13:46:15] someone at the gate"
