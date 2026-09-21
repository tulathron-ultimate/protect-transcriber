"""Previews: waveform extraction, the export pipeline, and reuse by jobs."""

from __future__ import annotations

import asyncio
import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from app import media
from app.store import JobStore, PreviewStore
from tests.conftest import FFMPEG, needs_ffmpeg
from tests.test_api import FakePool, FakeProtect, build_client, wait_for_status


def _wav(path, *, seconds=8, pattern="tone"):
    """A wav that alternates 2s of tone with 2s of silence."""
    expr = {
        "tone": r"sin(440*2*PI*t)*lt(mod(t\,4)\,2)",
        "silent": "0",
    }[pattern]
    subprocess.run(
        [
            FFMPEG,
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"aevalsrc={expr}:s=16000:d={seconds}",
            str(path),
        ],
        check=True,
    )
    return path


# --------------------------------------------------------------------------- #
# waveform
# --------------------------------------------------------------------------- #


@needs_ffmpeg
async def test_waveform_tracks_loud_and_quiet_stretches(tmp_path):
    peaks = await media.waveform_peaks(_wav(tmp_path / "a.wav", seconds=8), buckets=16)
    assert len(peaks) == 16
    assert all(0.0 <= p <= 1.0 for p in peaks)
    # 2s tone, 2s silence, repeating: the first quarter is loud, the second quiet.
    assert sum(peaks[:4]) / 4 > 0.5
    assert sum(peaks[4:8]) / 4 < 0.1


@needs_ffmpeg
async def test_waveform_of_silence_is_all_zero(tmp_path):
    peaks = await media.waveform_peaks(_wav(tmp_path / "s.wav", seconds=4, pattern="silent"), 8)
    assert peaks and max(peaks) == 0.0


@needs_ffmpeg
async def test_waveform_never_returns_more_buckets_than_frames(tmp_path):
    """A very short clip must not be padded out to the requested bucket count."""
    peaks = await media.waveform_peaks(_wav(tmp_path / "t.wav", seconds=1), buckets=100_000)
    assert 0 < len(peaks) <= 16000


@needs_ffmpeg
async def test_waveform_rejects_non_16bit_audio(tmp_path):
    path = tmp_path / "f32.wav"
    await asyncio.to_thread(
        subprocess.run,
        [
            FFMPEG,
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "aevalsrc=0:s=16000:d=1",
            "-c:a",
            "pcm_f32le",
            str(path),
        ],
        check=True,
    )
    with pytest.raises(media.MediaError, match="16-bit"):
        await media.waveform_peaks(path, 10)


@needs_ffmpeg
async def test_extract_audio_honours_start_and_duration(tmp_path):
    source = _wav(tmp_path / "long.wav", seconds=8)
    out = await media.extract_audio(source, tmp_path / "trim.wav", start=2.0, duration=2.0)
    assert (await media.inspect(out)).duration == pytest.approx(2.0, abs=0.1)
    # 2s-4s of the pattern is the silent half.
    assert max(await media.waveform_peaks(out, 8)) < 0.1


@needs_ffmpeg
async def test_extract_audio_without_offsets_keeps_everything(tmp_path):
    source = _wav(tmp_path / "long.wav", seconds=6)
    out = await media.extract_audio(source, tmp_path / "full.wav")
    assert (await media.inspect(out)).duration == pytest.approx(6.0, abs=0.2)


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #


@pytest.fixture
async def previews(tmp_path) -> PreviewStore:
    path = tmp_path / "jobs.db"
    await JobStore(path).init()
    return PreviewStore(path)


async def test_peaks_round_trip_as_json(previews: PreviewStore):
    await previews.create(id="p1", camera_id="cam1", status="exporting")
    await previews.update("p1", status="ready", peaks=[0.125_678, 1.0, 0.0])
    record = await previews.get("p1")
    assert record["peaks"] == [0.1257, 1.0, 0.0], "peaks are rounded to keep the payload small"


async def test_find_ready_ignores_previews_whose_clip_is_gone(previews: PreviewStore, tmp_path):
    clip = tmp_path / "clip.mp4"
    await previews.create(id="p1", camera_id="cam1", range_start="A", range_end="B", status="ready")
    await previews.update("p1", clip_path=str(clip))
    assert await previews.find_ready("cam1", "A", "B") is None, "file missing -> do not reuse"
    clip.write_bytes(b"data")
    assert (await previews.find_ready("cam1", "A", "B"))["id"] == "p1"


async def test_find_ready_only_matches_an_exact_range(previews: PreviewStore, tmp_path):
    clip = tmp_path / "c.mp4"
    clip.write_bytes(b"x")
    await previews.create(id="p1", camera_id="cam1", range_start="A", range_end="B", status="ready")
    await previews.update("p1", clip_path=str(clip))
    assert await previews.find_ready("cam1", "A", "DIFFERENT") is None
    assert await previews.find_ready("other", "A", "B") is None


async def test_expired_skips_previews_a_job_still_uses(previews: PreviewStore, tmp_path):
    old = (datetime.now(tz=UTC) - timedelta(days=3)).isoformat(timespec="seconds")
    await previews.create(id="keep", camera_id="c", status="ready", created_at=old)
    await previews.create(id="drop", camera_id="c", status="ready", created_at=old)
    expired = await previews.expired(retention_hours=1, keep_ids={"keep"})
    assert [r["id"] for r in expired] == ["drop"]
    assert await previews.expired(retention_hours=0, keep_ids=set()) == [], "0 disables"


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #


def _range(minutes=1):
    end = datetime.now(tz=UTC) - timedelta(minutes=1)
    return {
        "cameraId": "cam1",
        "start": (end - timedelta(minutes=minutes)).isoformat(),
        "end": end.isoformat(),
    }


def wait_for_preview(client, preview_id, statuses, timeout=60.0):
    import time

    deadline = time.time() + timeout
    record = None
    while time.time() < deadline:
        record = client.get(f"/api/preview/{preview_id}").json()
        if record["status"] in statuses:
            return record
        time.sleep(0.1)
    raise AssertionError(f"preview stuck at {record and record['status']}")


@needs_ffmpeg
def test_preview_exports_a_clip_and_returns_a_waveform(tmp_path, clip_bytes):
    protect = FakeProtect(clip=clip_bytes)
    client, _, settings = build_client(tmp_path, protect=protect, pool=FakePool())
    with client:
        created = client.post("/api/preview", json=_range())
        assert created.status_code == 202
        record = wait_for_preview(client, created.json()["id"], {"ready", "failed"})

    assert record["status"] == "ready", record.get("error")
    assert record["hasAudio"] is True
    assert record["duration"] > 10
    assert record["clipBytes"] > 0
    assert len(record["peaks"]) > 10
    assert all(0.0 <= p <= 1.0 for p in record["peaks"])
    assert (settings.previews_dir / f"{record['id']}.mp4").exists()


@needs_ffmpeg
def test_preview_clip_is_downloadable_for_playback(tmp_path, clip_bytes):
    client, _, _ = build_client(tmp_path, protect=FakeProtect(clip=clip_bytes), pool=FakePool())
    with client:
        preview_id = client.post("/api/preview", json=_range()).json()["id"]
        wait_for_preview(client, preview_id, {"ready", "failed"})
        response = client.get(f"/api/preview/{preview_id}/clip")
    assert response.status_code == 200
    assert response.headers["content-type"] == "video/mp4"
    assert len(response.content) > 0


@needs_ffmpeg
def test_an_identical_range_reuses_the_existing_preview(tmp_path, clip_bytes):
    """Nudging the selection back and forth must not re-export."""
    protect = FakeProtect(clip=clip_bytes)
    client, _, _ = build_client(tmp_path, protect=protect, pool=FakePool())
    payload = _range()
    with client:
        first = client.post("/api/preview", json=payload).json()["id"]
        wait_for_preview(client, first, {"ready"})
        second = client.post("/api/preview", json=payload).json()
    assert second["id"] == first
    assert second["status"] == "ready"
    assert len(protect.exports) == 1, "the second request must not hit Protect again"


@needs_ffmpeg
def test_preview_without_an_audio_track_still_plays(tmp_path, make_media):
    silent = make_media(tmp_path / "silent.mp4", seconds=4, with_audio=False, video=True)
    client, _, _ = build_client(
        tmp_path, protect=FakeProtect(clip=silent.read_bytes()), pool=FakePool()
    )
    with client:
        preview_id = client.post("/api/preview", json=_range()).json()["id"]
        record = wait_for_preview(client, preview_id, {"ready", "failed"})
    assert record["status"] == "ready", "no audio is not a failure -- the video is still useful"
    assert record["hasAudio"] is False
    assert record["peaks"] == []


def test_preview_range_cap_is_enforced_and_names_the_setting(tmp_path):
    client, _, _ = build_client(tmp_path, protect=FakeProtect(), preview_max_seconds=60)
    with client:
        response = client.post("/api/preview", json=_range(minutes=30))
    assert response.status_code == 400
    assert "PREVIEW_MAX_SECONDS" in response.json()["detail"]


def test_preview_rejects_an_inverted_or_future_range(tmp_path):
    client, _, _ = build_client(tmp_path, protect=FakeProtect())
    now = datetime.now(tz=UTC)
    with client:
        inverted = client.post(
            "/api/preview",
            json={
                "cameraId": "cam1",
                "start": now.isoformat(),
                "end": (now - timedelta(minutes=5)).isoformat(),
            },
        )
        future = client.post(
            "/api/preview",
            json={
                "cameraId": "cam1",
                "start": (now + timedelta(hours=1)).isoformat(),
                "end": (now + timedelta(hours=2)).isoformat(),
            },
        )
    assert inverted.status_code == 400
    assert future.status_code == 400


def test_empty_export_marks_the_preview_failed(tmp_path):
    client, _, _ = build_client(tmp_path, protect=FakeProtect(clip=b""), pool=FakePool())
    with client:
        preview_id = client.post("/api/preview", json=_range()).json()["id"]
        record = wait_for_preview(client, preview_id, {"failed"})
    assert "no footage" in record["error"]


def test_unknown_preview_is_a_404(tmp_path):
    client, _, _ = build_client(tmp_path, protect=FakeProtect())
    with client:
        assert client.get("/api/preview/nope").status_code == 404
        assert client.get("/api/preview/nope/clip").status_code == 404
        assert client.delete("/api/preview/nope").status_code == 404


@needs_ffmpeg
def test_deleting_a_preview_removes_its_files(tmp_path, clip_bytes):
    client, _, settings = build_client(
        tmp_path, protect=FakeProtect(clip=clip_bytes), pool=FakePool()
    )
    with client:
        preview_id = client.post("/api/preview", json=_range()).json()["id"]
        wait_for_preview(client, preview_id, {"ready"})
        clip = settings.previews_dir / f"{preview_id}.mp4"
        assert clip.exists()
        assert client.delete(f"/api/preview/{preview_id}").status_code == 200
        assert not clip.exists()
        assert client.get(f"/api/preview/{preview_id}").status_code == 404


# --------------------------------------------------------------------------- #
# jobs built from a preview
# --------------------------------------------------------------------------- #


@needs_ffmpeg
def test_a_job_from_a_preview_does_not_re_export(tmp_path, clip_bytes):
    protect = FakeProtect(clip=clip_bytes)
    pool = FakePool()
    client, _, _ = build_client(tmp_path, protect=protect, pool=pool)
    payload = _range()
    with client:
        preview = client.post("/api/preview", json=payload).json()
        wait_for_preview(client, preview["id"], {"ready"})
        assert len(protect.exports) == 1

        created = client.post("/api/jobs", json={**payload, "previewId": preview["id"]})
        assert created.status_code == 202
        job = wait_for_status(client, created.json()["id"], {"completed", "failed"})

    assert job["status"] == "completed", job.get("error")
    assert job["source"] == "preview"
    assert job["previewId"] == preview["id"]
    assert len(protect.exports) == 1, "the job must reuse the preview's clip"
    assert job["clipOffset"] == 0.0


@needs_ffmpeg
def test_a_trimmed_job_transcribes_only_the_selected_part(tmp_path, clip_bytes):
    """Dragging on the waveform narrows the range without a fresh export."""
    protect = FakeProtect(clip=clip_bytes)
    client, _, _ = build_client(tmp_path, protect=protect, pool=FakePool())
    payload = _range()
    with client:
        preview = client.post("/api/preview", json=payload).json()
        record = wait_for_preview(client, preview["id"], {"ready"})

        # Take the middle ~4 seconds of the previewed range.
        start = datetime.fromisoformat(record["rangeStart"]) + timedelta(seconds=4)
        end = start + timedelta(seconds=4)
        created = client.post(
            "/api/jobs",
            json={
                "cameraId": "cam1",
                "previewId": preview["id"],
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
        )
        job = wait_for_status(client, created.json()["id"], {"completed", "failed"})

    assert job["status"] == "completed", job.get("error")
    assert job["clipOffset"] == pytest.approx(4.0, abs=0.1)
    # Only the trimmed span was decoded, not the whole preview.
    assert job["audioSeconds"] == pytest.approx(4.0, abs=0.5)
    assert len(protect.exports) == 1


@needs_ffmpeg
def test_a_range_outside_the_preview_is_rejected(tmp_path, clip_bytes):
    client, _, _ = build_client(tmp_path, protect=FakeProtect(clip=clip_bytes), pool=FakePool())
    payload = _range()
    with client:
        preview = client.post("/api/preview", json=payload).json()
        record = wait_for_preview(client, preview["id"], {"ready"})
        start = datetime.fromisoformat(record["rangeStart"]) - timedelta(minutes=5)
        response = client.post(
            "/api/jobs",
            json={
                "cameraId": "cam1",
                "previewId": preview["id"],
                "start": start.isoformat(),
                "end": record["rangeEnd"],
            },
        )
    assert response.status_code == 400
    assert "inside that preview" in response.json()["detail"]


def test_a_job_referencing_an_unready_preview_is_rejected(tmp_path):
    client, _, _ = build_client(tmp_path, protect=FakeProtect(clip=b""), pool=FakePool())
    with client:
        preview_id = client.post("/api/preview", json=_range()).json()["id"]
        wait_for_preview(client, preview_id, {"failed"})
        response = client.post("/api/jobs", json={**_range(), "previewId": preview_id})
    assert response.status_code == 409
    assert "not ready" in response.json()["detail"]


@needs_ffmpeg
def test_deleting_a_preview_backed_job_keeps_the_preview_clip(tmp_path, clip_bytes):
    """The clip belongs to the preview; the job is only borrowing it."""
    client, _, settings = build_client(
        tmp_path, protect=FakeProtect(clip=clip_bytes), pool=FakePool()
    )
    payload = _range()
    with client:
        preview = client.post("/api/preview", json=payload).json()
        wait_for_preview(client, preview["id"], {"ready"})
        job_id = client.post("/api/jobs", json={**payload, "previewId": preview["id"]}).json()["id"]
        wait_for_status(client, job_id, {"completed", "failed"})

        clip = settings.previews_dir / f"{preview['id']}.mp4"
        assert client.delete(f"/api/jobs/{job_id}").status_code == 200
        assert clip.exists(), "deleting the job must not delete the preview's clip"
        assert client.get(f"/api/preview/{preview['id']}").status_code == 200
