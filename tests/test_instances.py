"""Runtime-editable Whisper instances: storage, pool reload, and the API."""

from __future__ import annotations

import httpx
import pytest
import respx

from app.config import Settings, WhisperInstance
from app.store import InstanceStore, JobStore
from app.whisper import WhisperPool
from tests.test_api import FakeProtect, build_client


@pytest.fixture
async def store(tmp_path) -> InstanceStore:
    path = tmp_path / "jobs.db"
    await JobStore(path).init()  # creates the schema
    return InstanceStore(path)


def _row(name: str, url: str, **overrides) -> dict:
    return {
        "name": name,
        "url": url,
        "kind": "auto",
        "model": "",
        "concurrency": 1,
        "api_key": "",
        **overrides,
    }


# --------------------------------------------------------------------------- #
# storage
# --------------------------------------------------------------------------- #


async def test_seed_only_populates_an_empty_table(store: InstanceStore):
    assert await store.seed([_row("a", "http://a:8000")]) == 1
    # A restart must not re-apply the env over edits made in the UI.
    assert await store.seed([_row("b", "http://b:9000")]) == 0
    assert [row["name"] for row in await store.list()] == ["a"]


async def test_seed_preserves_order_as_position(store: InstanceStore):
    await store.seed([_row("a", "http://a:8000"), _row("b", "http://b:9000")])
    assert [(r["name"], r["position"]) for r in await store.list()] == [("a", 0), ("b", 1)]


async def test_add_update_delete_round_trip(store: InstanceStore):
    created = await store.add(**_row("gpu", "http://gpu:8000", concurrency=3), enabled=1)
    assert created["concurrency"] == 3
    assert created["enabled"] == 1

    updated = await store.update(created["id"], concurrency=1, enabled=0)
    assert updated["concurrency"] == 1
    assert updated["enabled"] == 0
    assert updated["updated_at"] >= created["updated_at"]

    assert await store.delete(created["id"]) is True
    assert await store.delete(created["id"]) is False
    assert await store.list() == []


async def test_names_are_unique_at_the_database_level(store: InstanceStore):
    await store.add(**_row("dup", "http://a:8000"), enabled=1)
    with pytest.raises(Exception):  # noqa: B017 - sqlite raises IntegrityError
        await store.add(**_row("dup", "http://b:8000"), enabled=1)


# --------------------------------------------------------------------------- #
# pool reload
# --------------------------------------------------------------------------- #


def _pool(*instances: WhisperInstance) -> WhisperPool:
    return WhisperPool(Settings(_env_file=None), instances=list(instances))


def test_reload_keeps_live_state_for_an_unchanged_instance():
    pool = _pool(WhisperInstance(name="a", url="http://a:8000", kind="openai"))
    state = pool.states[0]
    state.healthy = True
    state.completed = 5
    state.total_audio_seconds = 100.0
    state.total_wall_seconds = 10.0
    state.instance.resolved_kind = "openai"

    pool.reload([WhisperInstance(name="a", url="http://a:8000", kind="openai")])

    assert pool.states[0] is state, "an untouched row must not lose its history"
    assert pool.states[0].completed == 5
    assert pool.states[0].healthy
    assert pool.states[0].instance.resolved_kind == "openai"


def test_reload_rebuilds_state_when_connection_settings_change():
    pool = _pool(WhisperInstance(name="a", url="http://a:8000", kind="openai"))
    pool.states[0].healthy = True
    pool.states[0].completed = 5

    pool.reload([WhisperInstance(name="a", url="http://moved:8000", kind="openai")])

    assert pool.states[0].completed == 0, "a moved instance must be re-probed from scratch"
    assert not pool.states[0].healthy


def test_reload_rebuilds_when_concurrency_changes():
    """The semaphore is sized at construction, so it has to be replaced."""
    pool = _pool(WhisperInstance(name="a", url="http://a:8000", concurrency=1))
    pool.reload([WhisperInstance(name="a", url="http://a:8000", concurrency=4)])
    assert pool.states[0].instance.concurrency == 4
    assert pool.states[0].semaphore._value == 4  # noqa: SLF001 - asserting the resize took


def test_reload_adds_and_removes_instances():
    pool = _pool(
        WhisperInstance(name="a", url="http://a:8000"),
        WhisperInstance(name="b", url="http://b:8000"),
    )
    pool.reload(
        [
            WhisperInstance(name="b", url="http://b:8000"),
            WhisperInstance(name="c", url="http://c:8000"),
        ]
    )
    assert [s.instance.name for s in pool.states] == ["b", "c"]


def test_disabled_instances_are_excluded_from_the_pool():
    pool = _pool(
        WhisperInstance(name="on", url="http://a:8000"),
        WhisperInstance(name="off", url="http://b:8000", enabled=False),
    )
    assert [s.instance.name for s in pool.states] == ["on"]

    pool.reload(
        [
            WhisperInstance(name="on", url="http://a:8000", enabled=False),
            WhisperInstance(name="off", url="http://b:8000", enabled=True),
        ]
    )
    assert [s.instance.name for s in pool.states] == ["off"]


def test_capacity_follows_a_reload():
    pool = _pool(WhisperInstance(name="a", url="http://a:8000", concurrency=2))
    pool.states[0].healthy = True
    assert pool.total_capacity == 2
    pool.reload([WhisperInstance(name="a", url="http://a:8000", concurrency=2)])
    assert pool.total_capacity == 2, "health carried over, so capacity is unchanged"


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #


def test_env_instances_are_seeded_and_listed(tmp_path):
    client, _, _ = build_client(tmp_path, protect=FakeProtect())
    with client:
        body = client.get("/api/whisper/instances").json()
    assert [i["name"] for i in body["instances"]] == ["a"]
    assert body["instances"][0]["url"] == "http://a:8000"
    assert body["instances"][0]["kind"] == "openai"
    assert body["seededFrom"] == "WHISPER_INSTANCES"


def test_create_update_and_delete_through_the_api(tmp_path):
    client, app, _ = build_client(tmp_path, protect=FakeProtect())
    with client:
        created = client.post(
            "/api/whisper/instances",
            json={"name": "gpu", "url": "http://gpu:8000/", "kind": "openai", "concurrency": 3},
        )
        assert created.status_code == 201
        body = created.json()
        assert body["url"] == "http://gpu:8000", "trailing slash is trimmed"
        assert body["concurrency"] == 3
        # The new instance reaches the live pool without a restart.
        assert "gpu" in [s.instance.name for s in app.state.pool.states]

        patched = client.patch(
            f"/api/whisper/instances/{body['id']}", json={"concurrency": 1, "enabled": False}
        )
        assert patched.status_code == 200
        assert patched.json()["concurrency"] == 1
        assert patched.json()["enabled"] is False
        # Disabled instances drop out of the pool but stay in the list.
        assert "gpu" not in [s.instance.name for s in app.state.pool.states]
        assert "gpu" in [
            i["name"] for i in client.get("/api/whisper/instances").json()["instances"]
        ]

        assert client.delete(f"/api/whisper/instances/{body['id']}").status_code == 200
        assert "gpu" not in [
            i["name"] for i in client.get("/api/whisper/instances").json()["instances"]
        ]


def test_api_key_is_never_returned(tmp_path):
    client, _, _ = build_client(tmp_path, protect=FakeProtect())
    with client:
        created = client.post(
            "/api/whisper/instances",
            json={"name": "keyed", "url": "http://k:8000", "apiKey": "sk-secret"},
        ).json()
        assert "apiKey" not in created and "api_key" not in created
        assert created["hasApiKey"] is True
        listing = client.get("/api/whisper/instances").json()["instances"]
        assert "sk-secret" not in str(listing)


def test_patch_without_an_api_key_keeps_the_stored_one(tmp_path):
    client, app, settings = build_client(tmp_path, protect=FakeProtect())
    with client:
        created = client.post(
            "/api/whisper/instances",
            json={"name": "keyed", "url": "http://k:8000", "apiKey": "sk-secret"},
        ).json()
        client.patch(f"/api/whisper/instances/{created['id']}", json={"concurrency": 2})
        listing = client.get("/api/whisper/instances").json()["instances"]
        assert [i for i in listing if i["name"] == "keyed"][0]["hasApiKey"] is True
        # Sending an explicit empty string does clear it.
        client.patch(f"/api/whisper/instances/{created['id']}", json={"apiKey": ""})
        keyed = [
            i
            for i in client.get("/api/whisper/instances").json()["instances"]
            if i["name"] == "keyed"
        ][0]
        assert keyed["hasApiKey"] is False


def test_duplicate_names_are_rejected(tmp_path):
    client, _, _ = build_client(tmp_path, protect=FakeProtect())
    with client:
        client.post("/api/whisper/instances", json={"name": "dup", "url": "http://a:8000"})
        clash = client.post("/api/whisper/instances", json={"name": "DUP", "url": "http://b:8000"})
    assert clash.status_code == 409


def test_invalid_urls_are_rejected(tmp_path):
    client, _, _ = build_client(tmp_path, protect=FakeProtect())
    with client:
        for bad in ("whisper:9000", "ftp://x", ""):
            response = client.post("/api/whisper/instances", json={"name": "x", "url": bad})
            assert response.status_code == 422, bad


def test_unknown_instance_is_a_404(tmp_path):
    client, _, _ = build_client(tmp_path, protect=FakeProtect())
    with client:
        assert (
            client.patch("/api/whisper/instances/999", json={"concurrency": 2}).status_code == 404
        )
        assert client.delete("/api/whisper/instances/999").status_code == 404


def test_edits_survive_a_restart_and_env_does_not_override_them(tmp_path):
    """The whole point: a restart must not undo what was set in the UI."""
    client, _, _ = build_client(tmp_path, protect=FakeProtect())
    with client:
        seeded = client.get("/api/whisper/instances").json()["instances"][0]
        client.patch(f"/api/whisper/instances/{seeded['id']}", json={"url": "http://moved:9999"})

    # Same data directory, fresh app — as if the container had been restarted.
    client2, _, _ = build_client(tmp_path, protect=FakeProtect())
    with client2:
        after = client2.get("/api/whisper/instances").json()["instances"]
    assert [i["url"] for i in after] == ["http://moved:9999"]


@respx.mock
def test_probe_reports_the_detected_api_and_models(tmp_path):
    respx.get("http://probe:8000/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "Systran/faster-whisper-large-v3"}]})
    )
    client, _, _ = build_client(tmp_path, protect=FakeProtect())
    with client:
        result = client.post(
            "/api/whisper/instances/test", json={"url": "http://probe:8000", "kind": "auto"}
        ).json()
    assert result["reachable"] is True
    assert result["kind"] == "openai"
    assert result["models"] == ["Systran/faster-whisper-large-v3"]


@respx.mock
def test_probe_reports_an_unreachable_url_without_raising(tmp_path):
    for path in ("/v1/models", "/openapi.json", "/inference"):
        respx.get(f"http://nope:8000{path}").mock(side_effect=httpx.ConnectError("refused"))
    client, _, _ = build_client(tmp_path, protect=FakeProtect())
    with client:
        result = client.post(
            "/api/whisper/instances/test", json={"url": "http://nope:8000", "kind": "auto"}
        ).json()
    assert result["reachable"] is False
    assert result["kind"] is None
    assert "ConnectError" in result["detail"]


def test_instance_endpoints_respect_the_app_token(tmp_path):
    client, _, _ = build_client(tmp_path, protect=FakeProtect(), app_token="s3cret")
    with client:
        assert client.get("/api/whisper/instances").status_code == 401
        assert (
            client.post(
                "/api/whisper/instances", json={"name": "x", "url": "http://a:8000"}
            ).status_code
            == 401
        )
        assert (
            client.get("/api/whisper/instances", headers={"X-App-Token": "s3cret"}).status_code
            == 200
        )
