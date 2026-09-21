"""End-to-end tests over the HTTP API.

UniFi Protect and the Whisper pool are stubbed, but everything in between is the
real thing: a real mp4 is streamed through the export step, ffmpeg extracts and
chunks the audio, and the segments are merged and written to disk.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.protect import Camera, ProtectError
from app.transcript import Segment, TranscriptResult
from app.whisper import NoHealthyInstances
from tests.conftest import needs_ffmpeg

CAMERAS = [
    Camera(id="cam1", name="Front Door", has_mic=True, mic_enabled=True),
    Camera(id="cam2", name="Barn", has_mic=False),
]


class FakeProtect:
    """Stands in for ProtectClient, serving a real mp4 from disk."""

    def __init__(self, clip: bytes | None = None, fail: Exception | None = None) -> None:
        self.clip = clip or b""
        self.fail = fail
        self.exports: list[tuple[str, datetime, datetime]] = []

    async def nvr_info(self):
        return {"name": "Dream Machine", "version": "4.0.0"}

    async def cameras(self):
        return CAMERAS

    async def camera(self, camera_id):
        for cam in CAMERAS:
            if cam.id == camera_id:
                return cam
        raise ProtectError(f"No camera with id {camera_id!r}")

    async def events(self, start, end, camera_id=None, types=None, limit=500):
        return []

    async def snapshot(self, camera_id, at=None):
        return b"\xff\xd8jpeg"

    async def export_clip(self, camera_id, start, end, chunk_size=1 << 18):
        self.exports.append((camera_id, start, end))
        if self.fail:
            raise self.fail
        for offset in range(0, len(self.clip), 65536):
            yield self.clip[offset : offset + 65536]

    async def aclose(self):
        return None


class FakePool:
    """Returns canned segments, one call per chunk.

    Segments are placed at 1.5s-3.0s within each chunk, i.e. past the 1s overlap
    the splitter reads ahead of a boundary. Anything earlier than that genuinely
    belongs to the previous chunk and is dropped on merge by design.
    """

    def __init__(self, fail: Exception | None = None) -> None:
        self.calls: list[str] = []
        self.fail = fail
        self.states: list = []
        self.total_capacity = 1

    async def probe_all(self, force=False):
        return self.states

    async def transcribe_file(
        self, audio, *, language=None, task=None, prompt=None, audio_seconds=None
    ):
        self.calls.append(audio.name)
        if self.fail:
            raise self.fail
        index = len(self.calls)
        return TranscriptResult(
            text=f"chunk {index} words",
            segments=[Segment(start=1.5, end=3.0, text=f"chunk {index} words")],
            language="en",
            duration=audio_seconds,
            instance="fake-a",
        )

    async def aclose(self):
        return None


def build_client(tmp_path, *, protect=None, pool=None, **overrides):
    settings = Settings(
        protect_host="nvr.example.com",
        protect_username="svc",
        protect_password="pw",
        whisper_instances="a=http://a:8000|openai",
        data_dir=tmp_path / "data",
        chunk_seconds=5,
        chunk_overlap_seconds=1.0,
        silence_snap_window=2.0,
        retention_days=0,
        _env_file=None,
        **overrides,
    )
    app = create_app(settings)
    client = TestClient(app)
    # Swap the real collaborators in once lifespan has built the manager.
    original = app.router.lifespan_context

    class Patched:
        def __init__(self, app):
            self._cm = original(app)

        async def __aenter__(self):
            result = await self._cm.__aenter__()
            if protect is not None:
                app.state.protect = protect
                app.state.manager.protect = protect
            if pool is not None:
                app.state.pool = pool
                app.state.manager.pool = pool
            return result

        async def __aexit__(self, *exc):
            return await self._cm.__aexit__(*exc)

    app.router.lifespan_context = Patched
    return client, app, settings


def wait_for_status(client, job_id, statuses, timeout=60.0):
    import time

    deadline = time.time() + timeout
    job = None
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in statuses:
            return job
        time.sleep(0.1)
    raise AssertionError(f"Job stayed at {job and job['status']}, wanted one of {statuses}")


def make_payload(**overrides):
    end = datetime.now(tz=UTC) - timedelta(minutes=1)
    payload = {
        "cameraId": "cam1",
        "start": (end - timedelta(seconds=12)).isoformat(),
        "end": end.isoformat(),
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------- #
# meta
# --------------------------------------------------------------------------- #


def test_health_reports_configuration(tmp_path):
    client, _, _ = build_client(tmp_path)
    with client:
        body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["configured"] is True
    assert body["whisperInstances"] == 1
    assert body["authRequired"] is False


def test_index_serves_the_ui(tmp_path):
    client, _, _ = build_client(tmp_path)
    with client:
        response = client.get("/")
    assert response.status_code == 200
    assert "Protect Transcriber" in response.text


def test_app_token_gates_the_api(tmp_path):
    client, _, _ = build_client(tmp_path, app_token="s3cret")
    with client:
        assert client.get("/api/health").status_code == 200, "health stays open for probes"
        assert client.get("/api/config").status_code == 401
        assert client.get("/api/config", headers={"X-App-Token": "wrong"}).status_code == 401
        assert client.get("/api/config", headers={"X-App-Token": "s3cret"}).status_code == 200
        # EventSource cannot set headers, so a query token is accepted too.
        assert client.get("/api/config?token=s3cret").status_code == 200
        assert (
            client.get("/api/config", headers={"Authorization": "Bearer s3cret"}).status_code == 200
        )


def test_cameras_are_listed_with_audio_flags(tmp_path):
    client, _, _ = build_client(tmp_path, protect=FakeProtect())
    with client:
        cameras = client.get("/api/cameras").json()["cameras"]
    assert [c["name"] for c in cameras] == ["Front Door", "Barn"]
    assert cameras[0]["hasAudio"] is True
    assert cameras[1]["hasAudio"] is False


def test_protect_status_reports_the_nvr(tmp_path):
    client, _, _ = build_client(tmp_path, protect=FakeProtect())
    with client:
        body = client.get("/api/protect").json()
    assert body["connected"] is True
    assert body["nvr"]["version"] == "4.0.0"


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #


def test_inverted_range_is_rejected(tmp_path):
    client, _, _ = build_client(tmp_path, protect=FakeProtect())
    now = datetime.now(tz=UTC)
    with client:
        response = client.post(
            "/api/jobs",
            json=make_payload(start=now.isoformat(), end=(now - timedelta(minutes=5)).isoformat()),
        )
    assert response.status_code == 400
    assert "after start" in response.json()["detail"]


def test_range_longer_than_the_limit_is_rejected_with_the_setting_name(tmp_path):
    client, _, _ = build_client(tmp_path, protect=FakeProtect(), max_range_seconds=600)
    end = datetime.now(tz=UTC)
    with client:
        response = client.post(
            "/api/jobs",
            json=make_payload(start=(end - timedelta(hours=2)).isoformat(), end=end.isoformat()),
        )
    assert response.status_code == 400
    assert "MAX_RANGE_SECONDS" in response.json()["detail"]


def test_future_range_is_rejected(tmp_path):
    client, _, _ = build_client(tmp_path, protect=FakeProtect())
    start = datetime.now(tz=UTC) + timedelta(hours=1)
    with client:
        response = client.post(
            "/api/jobs",
            json=make_payload(
                start=start.isoformat(), end=(start + timedelta(minutes=5)).isoformat()
            ),
        )
    assert response.status_code == 400
    assert "future" in response.json()["detail"]


def test_unknown_job_is_a_404(tmp_path):
    client, _, _ = build_client(tmp_path)
    with client:
        assert client.get("/api/jobs/does-not-exist").status_code == 404
        assert client.delete("/api/jobs/does-not-exist").status_code == 404
        assert client.post("/api/jobs/does-not-exist/retry").status_code == 404


# --------------------------------------------------------------------------- #
# the happy path
# --------------------------------------------------------------------------- #


@needs_ffmpeg
def test_full_pipeline_produces_a_merged_transcript(tmp_path, clip_bytes):
    protect = FakeProtect(clip=clip_bytes)
    pool = FakePool()
    client, app, settings = build_client(tmp_path, protect=protect, pool=pool)

    with client:
        created = client.post("/api/jobs", json=make_payload(title="Porch check"))
        assert created.status_code == 202
        job_id = created.json()["id"]
        assert created.json()["status"] == "queued"

        job = wait_for_status(client, job_id, {"completed", "failed"})
        assert job["status"] == "completed", job.get("error")

        # The clip was exported for exactly the requested window.
        assert len(protect.exports) == 1
        assert protect.exports[0][0] == "cam1"

        # 12s of audio at chunk_seconds=5 must have produced several chunks,
        # each sent to the pool once.
        assert job["chunkCount"] >= 2
        assert job["chunksDone"] == job["chunkCount"]
        assert len(pool.calls) == job["chunkCount"]

        assert job["progress"] == 1.0
        assert job["detectedLanguage"] == "en"
        assert job["instances"] == ["fake-a"]
        assert job["cameraName"] == "Front Door"
        assert job["title"] == "Porch check"
        assert job["audioSeconds"] > 10

        # Segments are on one clip-relative timeline, in order.
        segments = job["segments"]
        assert len(segments) == job["chunkCount"]
        assert segments == sorted(segments, key=lambda s: s["start"])
        assert segments[0]["start"] == 1.5, "the first chunk is not offset"
        # Later chunks are shifted onto the clip timeline by their read offset.
        assert segments[-1]["start"] > segments[0]["start"] + 5
        assert "chunk 1 words" in job["text"]

        # Every download format renders.
        for fmt, expected in [
            ("txt", "chunk 1 words"),
            ("srt", "00:00:01,500 -->"),
            ("vtt", "WEBVTT"),
            ("log", "chunk 1 words"),
        ]:
            response = client.get(f"/api/jobs/{job_id}/transcript.{fmt}")
            assert response.status_code == 200, fmt
            assert expected in response.text, fmt
        assert client.get(f"/api/jobs/{job_id}/transcript.docx").status_code == 400

        as_json = client.get(f"/api/jobs/{job_id}/transcript.json").json()
        assert as_json["id"] == job_id

        # The clip is downloadable for in-browser playback.
        clip = client.get(f"/api/jobs/{job_id}/clip")
        assert clip.status_code == 200
        assert clip.headers["content-type"] == "video/mp4"

        # Transcript files were written to the data volume.
        written = settings.transcripts_dir / job_id
        assert {p.name for p in written.iterdir()} >= {
            "transcript.txt",
            "transcript.srt",
            "transcript.vtt",
            "transcript.json",
        }
        on_disk = json.loads((written / "transcript.json").read_text())
        assert on_disk["segments"] == segments

        # Chunk scratch files are cleaned up; the wav and clip are kept.
        assert not (settings.audio_dir / job_id).exists()
        assert (settings.audio_dir / f"{job_id}.wav").exists()

        # Full-text search finds it.
        results = client.get("/api/jobs/search?q=chunk").json()["results"]
        assert job_id in [r["id"] for r in results]
        assert "<mark>" in results[0]["snippet"]

        # The listing carries aggregate stats.
        listing = client.get("/api/jobs").json()
        assert listing["stats"]["totalJobs"] == 1
        assert listing["jobs"][0]["id"] == job_id


@needs_ffmpeg
def test_deleting_a_job_removes_its_files(tmp_path, clip_bytes):
    client, _, settings = build_client(
        tmp_path, protect=FakeProtect(clip=clip_bytes), pool=FakePool()
    )
    with client:
        job_id = client.post("/api/jobs", json=make_payload()).json()["id"]
        wait_for_status(client, job_id, {"completed", "failed"})
        clip = settings.clips_dir / f"{job_id}.mp4"
        assert clip.exists()

        assert client.delete(f"/api/jobs/{job_id}").status_code == 200
        assert client.get(f"/api/jobs/{job_id}").status_code == 404
        assert not clip.exists()
        assert not (settings.transcripts_dir / job_id).exists()


@needs_ffmpeg
def test_uploaded_clip_is_transcribed_without_touching_protect(tmp_path, clip_bytes):
    protect = FakeProtect(fail=AssertionError("upload jobs must not export"))
    client, _, _ = build_client(tmp_path, protect=protect, pool=FakePool())
    with client:
        response = client.post(
            "/api/jobs/upload",
            files={"file": ("doorbell.mp4", clip_bytes, "video/mp4")},
            data={"title": "manual upload"},
        )
        assert response.status_code == 202
        job = wait_for_status(client, response.json()["id"], {"completed", "failed"})
    assert job["status"] == "completed", job.get("error")
    assert job["source"] == "upload"
    assert job["title"] == "manual upload"
    assert protect.exports == []


def test_empty_upload_is_rejected(tmp_path):
    client, _, _ = build_client(tmp_path, pool=FakePool())
    with client:
        response = client.post("/api/jobs/upload", files={"file": ("empty.mp4", b"", "video/mp4")})
    assert response.status_code == 400


# --------------------------------------------------------------------------- #
# failure paths
# --------------------------------------------------------------------------- #


def test_export_failure_lands_on_the_job_not_the_request(tmp_path):
    protect = FakeProtect(fail=ProtectError("Protect refused the export"))
    client, _, _ = build_client(tmp_path, protect=protect, pool=FakePool())
    with client:
        created = client.post("/api/jobs", json=make_payload())
        assert created.status_code == 202, "queueing succeeds; the failure shows up on the job"
        job = wait_for_status(client, created.json()["id"], {"failed"})
    assert "refused the export" in job["error"]


def test_empty_export_gives_an_actionable_error(tmp_path):
    client, _, _ = build_client(tmp_path, protect=FakeProtect(clip=b""), pool=FakePool())
    with client:
        job_id = client.post("/api/jobs", json=make_payload()).json()["id"]
        job = wait_for_status(client, job_id, {"failed"})
    assert "empty clip" in job["error"]
    assert "no footage" in job["error"]


@needs_ffmpeg
def test_clip_without_an_audio_track_fails_with_a_mic_hint(tmp_path, make_media):
    silent = make_media(tmp_path / "silent.mp4", seconds=4, with_audio=False, video=True)
    client, _, _ = build_client(
        tmp_path, protect=FakeProtect(clip=silent.read_bytes()), pool=FakePool()
    )
    with client:
        job_id = client.post("/api/jobs", json=make_payload()).json()["id"]
        job = wait_for_status(client, job_id, {"failed"})
    assert "no audio track" in job["error"]
    assert "mic" in job["error"]


@needs_ffmpeg
def test_whisper_outage_fails_the_job_with_the_pool_message(tmp_path, clip_bytes):
    pool = FakePool(fail=NoHealthyInstances("No Whisper instance is reachable"))
    client, _, _ = build_client(tmp_path, protect=FakeProtect(clip=clip_bytes), pool=pool)
    with client:
        job_id = client.post("/api/jobs", json=make_payload()).json()["id"]
        job = wait_for_status(client, job_id, {"failed"})
    assert "No Whisper instance is reachable" in job["error"]


@needs_ffmpeg
def test_failed_job_can_be_retried(tmp_path, clip_bytes):
    protect = FakeProtect(fail=ProtectError("transient"))
    client, app, _ = build_client(tmp_path, protect=protect, pool=FakePool())
    with client:
        job_id = client.post("/api/jobs", json=make_payload()).json()["id"]
        wait_for_status(client, job_id, {"failed"})

        # Protect recovers, then the retry succeeds as a new job.
        protect.fail = None
        protect.clip = clip_bytes
        retried = client.post(f"/api/jobs/{job_id}/retry")
        assert retried.status_code == 202
        new_id = retried.json()["id"]
        assert new_id != job_id, "a retry is a fresh job, so the original stays on record"
        job = wait_for_status(client, new_id, {"completed", "failed"})
    assert job["status"] == "completed", job.get("error")


def test_cancel_rejects_a_finished_job(tmp_path):
    client, _, _ = build_client(
        tmp_path, protect=FakeProtect(fail=ProtectError("no")), pool=FakePool()
    )
    with client:
        job_id = client.post("/api/jobs", json=make_payload()).json()["id"]
        wait_for_status(client, job_id, {"failed"})
        assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 409


def test_clip_download_404s_when_the_file_is_gone(tmp_path):
    client, _, _ = build_client(
        tmp_path, protect=FakeProtect(fail=ProtectError("no")), pool=FakePool()
    )
    with client:
        job_id = client.post("/api/jobs", json=make_payload()).json()["id"]
        wait_for_status(client, job_id, {"failed"})
        response = client.get(f"/api/jobs/{job_id}/clip")
    assert response.status_code == 404
    assert "KEEP_CLIPS" in response.json()["detail"]
