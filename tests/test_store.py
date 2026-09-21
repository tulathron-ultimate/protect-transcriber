from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.store import JobStore


@pytest.fixture
async def store(tmp_path) -> JobStore:
    store = JobStore(tmp_path / "jobs.db")
    await store.init()
    return store


async def test_create_and_get_round_trip(store: JobStore):
    await store.create({"id": "j1", "title": "Front Door", "camera_id": "cam1"})
    job = await store.get("j1")
    assert job["status"] == "queued"
    assert job["title"] == "Front Door"
    assert job["segments"] == [], "JSON columns come back decoded"


async def test_get_unknown_job_is_none(store: JobStore):
    assert await store.get("nope") is None


async def test_update_serializes_json_columns_and_bumps_updated_at(store: JobStore):
    created = await store.create({"id": "j1"})
    updated = await store.update(
        "j1", status="completed", segments=[{"start": 0, "end": 1, "text": "hi"}], instances=["a"]
    )
    assert updated["segments"] == [{"start": 0, "end": 1, "text": "hi"}]
    assert updated["instances"] == ["a"]
    assert updated["updated_at"] >= created["updated_at"]


async def test_listing_filters_by_status_and_camera(store: JobStore):
    await store.create({"id": "a", "camera_id": "cam1", "status": "completed"})
    await store.create({"id": "b", "camera_id": "cam2", "status": "failed"})
    await store.create({"id": "c", "camera_id": "cam1", "status": "completed"})
    assert {j["id"] for j in await store.list(status="completed")} == {"a", "c"}
    assert {j["id"] for j in await store.list(camera_id="cam2")} == {"b"}
    assert len(await store.list(limit=2)) == 2


async def test_search_finds_transcript_text_and_highlights_it(store: JobStore):
    await store.create({"id": "j1", "title": "Porch", "camera_name": "Front Door"})
    await store.update("j1", text="the delivery driver left a package on the porch")
    await store.create({"id": "j2", "title": "Barn"})
    await store.update("j2", text="nothing but wind")

    results = await store.search("package")
    assert [r["id"] for r in results] == ["j1"]
    assert "<mark>package</mark>" in results[0]["snippet"]
    assert await store.search("helicopter") == []
    assert await store.search("  ") == []


async def test_search_survives_a_malformed_fts_query(store: JobStore):
    await store.create({"id": "j1"})
    await store.update("j1", text='he said "hello" loudly')
    # An unbalanced quote is invalid FTS5 syntax; it must not raise.
    results = await store.search('hello"')
    assert [r["id"] for r in results] == ["j1"]


async def test_search_index_follows_the_latest_text(store: JobStore):
    await store.create({"id": "j1"})
    await store.update("j1", text="first attempt mentions cats")
    await store.update("j1", text="second attempt mentions dogs")
    assert await store.search("cats") == []
    assert len(await store.search("dogs")) == 1


async def test_delete_removes_the_row_and_its_search_entry(store: JobStore):
    await store.create({"id": "j1"})
    await store.update("j1", text="find me")
    assert await store.delete("j1") is not None
    assert await store.get("j1") is None
    assert await store.search("find me") == []
    assert await store.delete("j1") is None


async def test_stats_aggregate_counts_and_totals(store: JobStore):
    await store.create({"id": "a", "status": "completed", "audio_seconds": 60, "clip_bytes": 1000})
    await store.create({"id": "b", "status": "failed"})
    stats = await store.stats()
    assert stats["byStatus"] == {"completed": 1, "failed": 1}
    assert stats["totalJobs"] == 2
    assert stats["totalAudioSeconds"] == 60.0
    assert stats["totalClipBytes"] == 1000


async def test_expired_only_returns_finished_jobs_past_the_window(store: JobStore):
    old = (datetime.now(tz=UTC) - timedelta(days=40)).isoformat(timespec="seconds")
    await store.create({"id": "old-done", "status": "completed", "created_at": old})
    await store.create({"id": "old-running", "status": "transcribing", "created_at": old})
    await store.create({"id": "new-done", "status": "completed"})
    expired = await store.expired(retention_days=30)
    assert [job["id"] for job in expired] == ["old-done"]
    assert await store.expired(retention_days=0) == [], "0 disables retention"


async def test_init_fails_jobs_left_running_by_a_restart(tmp_path):
    path = tmp_path / "jobs.db"
    store = JobStore(path)
    await store.init()
    await store.create({"id": "stuck", "status": "transcribing"})

    # Simulate the container coming back up.
    reopened = JobStore(path)
    await reopened.init()
    job = await reopened.get("stuck")
    assert job["status"] == "failed"
    assert "restart" in job["error"]
    assert job["finished_at"]
