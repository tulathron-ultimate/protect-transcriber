from __future__ import annotations

from itertools import pairwise

import pytest

from app import media
from tests.conftest import needs_ffmpeg


def test_no_cuts_when_the_clip_is_shorter_than_one_chunk():
    assert media.plan_cut_points(200, 300, []) == []
    assert media.plan_cut_points(300, 300, []) == []


def test_fixed_cuts_when_no_silence_is_found():
    assert media.plan_cut_points(1000, 300, []) == [300.0, 600.0, 900.0]


def test_cuts_snap_to_nearby_silence():
    silences = [(292.0, 296.0), (590.0, 600.0)]
    cuts = media.plan_cut_points(1000, 300, silences, snap_window=15)
    # Midpoint of the first silence, then the second measured from there.
    assert cuts[0] == 294.0
    assert cuts[1] == 595.0


def test_silence_outside_the_snap_window_is_ignored():
    cuts = media.plan_cut_points(1000, 300, [(100.0, 120.0)], snap_window=15)
    assert cuts == [300.0, 600.0, 900.0]


def test_cut_points_are_strictly_increasing_and_inside_the_clip():
    silences = [(x, x + 1.0) for x in range(0, 1000, 7)]
    cuts = media.plan_cut_points(1000, 120, silences, snap_window=30)
    assert cuts == sorted(cuts)
    assert len(set(cuts)) == len(cuts)
    assert all(0 < cut < 1000 for cut in cuts)


def test_degenerate_inputs_do_not_raise():
    assert media.plan_cut_points(0, 300, []) == []
    assert media.plan_cut_points(1000, 0, []) == []
    assert media.plan_cut_points(-5, 300, []) == []


@needs_ffmpeg
async def test_extract_audio_produces_a_mono_16k_wav(tmp_path, make_media):
    source = make_media(tmp_path / "clip.mp4", seconds=3, with_audio=True, video=True)
    destination = await media.extract_audio(source, tmp_path / "out.wav")
    assert destination.exists() and destination.stat().st_size > 0
    info = await media.probe(destination)
    audio = next(s for s in info["streams"] if s["codec_type"] == "audio")
    assert audio["channels"] == 1
    assert audio["sample_rate"] == "16000"
    assert audio["codec_name"] == "pcm_s16le"


@needs_ffmpeg
async def test_extract_audio_reports_a_clip_with_no_audio_track(tmp_path, make_media):
    source = make_media(tmp_path / "silent.mp4", seconds=2, with_audio=False, video=True)
    with pytest.raises(media.MediaError, match="no audio track"):
        await media.extract_audio(source, tmp_path / "out.wav")


@needs_ffmpeg
async def test_inspect_reports_duration_and_audio_presence(tmp_path, make_media):
    source = make_media(tmp_path / "a.wav", seconds=4)
    info = await media.inspect(source)
    assert info.has_audio
    assert 3.5 < info.duration < 4.5


@needs_ffmpeg
async def test_detect_silences_finds_the_quiet_stretches(tmp_path, make_media):
    # The fixture alternates 2s of tone with 2s of silence.
    source = make_media(tmp_path / "beeps.wav", seconds=12)
    silences = await media.detect_silences(source)
    assert silences, "expected silencedetect to find the gaps"
    assert all(end > start for start, end in silences)


@needs_ffmpeg
async def test_split_audio_covers_the_clip_with_overlapping_chunks(tmp_path, make_media):
    source = make_media(tmp_path / "long.wav", seconds=24)
    chunks = await media.split_audio(
        source, tmp_path / "chunks", chunk_seconds=8, overlap=1.0, snap_window=3
    )
    assert len(chunks) >= 3
    assert all(chunk.path.exists() for chunk in chunks)
    # Indices are dense, the first chunk starts at zero, and coverage has no holes.
    assert [c.index for c in chunks] == list(range(len(chunks)))
    assert chunks[0].start == 0.0
    for previous, nxt in pairwise(chunks):
        assert nxt.start <= previous.end, "chunks must not leave a gap"
        assert nxt.nominal_start >= previous.nominal_start
    assert chunks[-1].end == pytest.approx(24.0, abs=0.5)


@needs_ffmpeg
async def test_split_audio_returns_one_chunk_for_a_short_clip(tmp_path, make_media):
    source = make_media(tmp_path / "short.wav", seconds=3)
    chunks = await media.split_audio(source, tmp_path / "c", chunk_seconds=300, overlap=2.0)
    assert len(chunks) == 1
    assert chunks[0].start == 0.0
    assert chunks[0].nominal_start == 0.0


@needs_ffmpeg
async def test_probe_raises_a_clear_error_for_a_non_media_file(tmp_path):
    junk = tmp_path / "not-video.mp4"
    junk.write_bytes(b"this is not an mp4")
    with pytest.raises(media.MediaError):
        await media.probe(junk)
