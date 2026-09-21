from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx

from app.config import Settings
from app.protect import (
    Camera,
    ProtectAuthError,
    ProtectClient,
    ProtectError,
    from_millis,
    to_millis,
)

BASE = "https://nvr.example.com:443"

CAMERAS_PAYLOAD = [
    {
        "id": "cam1",
        "name": "Front Door",
        "type": "UVC G4 Doorbell",
        "state": "CONNECTED",
        "isRecording": True,
        "micVolume": 100,
        "featureFlags": {"hasMic": True},
        "stats": {"video": {"recordingStart": 1_700_000_000_000}},
    },
    {
        "id": "cam2",
        "name": "Attic",
        "micVolume": 0,
        "featureFlags": {"hasMic": True},
    },
    {"id": "cam3", "name": "Barn", "featureFlags": {"hasMic": False}},
]


def _settings(**kwargs) -> Settings:
    return Settings(
        protect_host="nvr.example.com",
        protect_username="svc",
        protect_password="pw",
        _env_file=None,
        **kwargs,
    )


def _login_ok():
    return respx.post(f"{BASE}/api/auth/login").mock(
        return_value=httpx.Response(
            200,
            json={"username": "svc"},
            headers={"X-CSRF-Token": "csrf-abc", "Set-Cookie": "TOKEN=jwt-value; Path=/"},
        )
    )


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def test_millis_round_trip_is_utc_anchored():
    moment = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    assert from_millis(to_millis(moment)) == moment
    # A naive datetime is read as UTC rather than local time.
    assert to_millis(datetime(2026, 5, 1, 12, 0, 0)) == to_millis(moment)
    assert from_millis(0) is None


def test_camera_audio_detection():
    cameras = [Camera.from_api(item) for item in CAMERAS_PAYLOAD]
    by_name = {camera.name: camera for camera in cameras}
    assert by_name["Front Door"].has_audio, "mic present and unmuted"
    assert not by_name["Attic"].has_audio, "micVolume 0 means muted"
    assert not by_name["Barn"].has_audio, "no mic at all"
    assert by_name["Front Door"].recording_start is not None
    assert by_name["Front Door"].as_dict()["hasAudio"] is True


# --------------------------------------------------------------------------- #
# auth
# --------------------------------------------------------------------------- #


@respx.mock
async def test_login_stores_the_token_cookie_and_csrf_header():
    route = _login_ok()
    respx.get(f"{BASE}/proxy/protect/api/cameras").mock(
        return_value=httpx.Response(200, json=CAMERAS_PAYLOAD)
    )
    async with ProtectClient(_settings()) as client:
        await client.cameras()
    assert route.called
    # The CSRF token captured at login rides along on later requests.
    follow_up = respx.calls[-1].request
    assert follow_up.headers["X-CSRF-Token"] == "csrf-abc"
    assert "TOKEN=jwt-value" in follow_up.headers.get("cookie", "")


@respx.mock
async def test_rejected_credentials_mention_two_factor():
    respx.post(f"{BASE}/api/auth/login").mock(return_value=httpx.Response(401))
    async with ProtectClient(_settings()) as client:
        with pytest.raises(ProtectAuthError, match="2FA"):
            await client.cameras()


@respx.mock
async def test_login_without_a_token_cookie_is_an_error():
    respx.post(f"{BASE}/api/auth/login").mock(return_value=httpx.Response(200, json={}))
    async with ProtectClient(_settings()) as client:
        with pytest.raises(ProtectAuthError, match="no TOKEN cookie"):
            await client.login()


@respx.mock
async def test_missing_credentials_is_a_clear_error():
    settings = Settings(protect_host="nvr.example.com", _env_file=None)
    async with ProtectClient(settings) as client:
        with pytest.raises(ProtectAuthError, match="No UniFi Protect credentials"):
            await client.login()


@respx.mock
async def test_api_key_mode_skips_login_and_sends_the_header():
    settings = Settings(protect_host="nvr.example.com", protect_api_key="key-123", _env_file=None)
    login = respx.post(f"{BASE}/api/auth/login")
    route = respx.get(f"{BASE}/proxy/protect/integration/v1/cameras").mock(
        return_value=httpx.Response(200, json=CAMERAS_PAYLOAD)
    )
    async with ProtectClient(settings) as client:
        cameras = await client.cameras()
    assert not login.called, "an API key must not trigger a session login"
    assert route.calls[0].request.headers["X-API-KEY"] == "key-123"
    assert len(cameras) == 3


@respx.mock
async def test_expired_session_triggers_one_reauthentication():
    login = _login_ok()
    route = respx.get(f"{BASE}/proxy/protect/api/cameras").mock(
        side_effect=[
            httpx.Response(401),
            httpx.Response(200, json=CAMERAS_PAYLOAD),
        ]
    )
    async with ProtectClient(_settings()) as client:
        cameras = await client.cameras()
    assert len(cameras) == 3
    assert login.call_count == 2, "expected exactly one re-login"
    assert route.call_count == 2


# --------------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------------- #


@respx.mock
async def test_cameras_are_sorted_by_name():
    _login_ok()
    respx.get(f"{BASE}/proxy/protect/api/cameras").mock(
        return_value=httpx.Response(200, json=CAMERAS_PAYLOAD)
    )
    async with ProtectClient(_settings()) as client:
        cameras = await client.cameras()
    assert [camera.name for camera in cameras] == ["Attic", "Barn", "Front Door"]


@respx.mock
async def test_camera_list_falls_back_to_the_integration_path_on_404():
    _login_ok()
    respx.get(f"{BASE}/proxy/protect/api/cameras").mock(return_value=httpx.Response(404))
    fallback = respx.get(f"{BASE}/proxy/protect/integration/v1/cameras").mock(
        return_value=httpx.Response(200, json=CAMERAS_PAYLOAD)
    )
    async with ProtectClient(_settings()) as client:
        assert len(await client.cameras()) == 3
    assert fallback.called


@respx.mock
async def test_camera_lookup_by_id_raises_for_an_unknown_id():
    _login_ok()
    respx.get(f"{BASE}/proxy/protect/api/cameras").mock(
        return_value=httpx.Response(200, json=CAMERAS_PAYLOAD)
    )
    async with ProtectClient(_settings()) as client:
        assert (await client.camera("cam1")).name == "Front Door"
        with pytest.raises(ProtectError, match="No camera with id"):
            await client.camera("nope")


@respx.mock
async def test_events_are_requested_in_millis_and_parsed():
    _login_ok()
    route = respx.get(f"{BASE}/proxy/protect/api/events").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": "e1",
                    "type": "smartDetectZone",
                    "camera": "cam1",
                    "start": 1_760_000_000_000,
                    "end": 1_760_000_012_000,
                    "score": 88,
                    "smartDetectTypes": ["person"],
                },
                {"id": "e2", "type": "motion", "camera": "cam1"},  # no start -> dropped
            ],
        )
    )
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 1, 2, tzinfo=UTC)
    async with ProtectClient(_settings()) as client:
        events = await client.events(start, end, camera_id="cam1")
    params = route.calls[0].request.url.params
    assert params["start"] == str(to_millis(start))
    assert params["cameras"] == "cam1"
    assert [event.id for event in events] == ["e1"]
    assert events[0].smart_types == ["person"]


@respx.mock
async def test_event_failures_do_not_raise_since_markers_are_optional():
    _login_ok()
    respx.get(f"{BASE}/proxy/protect/api/events").mock(return_value=httpx.Response(500))
    respx.get(f"{BASE}/proxy/protect/integration/v1/events").mock(return_value=httpx.Response(500))
    async with ProtectClient(_settings()) as client:
        assert await client.events(datetime.now(UTC), datetime.now(UTC)) == []


# --------------------------------------------------------------------------- #
# export
# --------------------------------------------------------------------------- #


async def _collect(client, camera="cam1"):
    start = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    end = datetime(2026, 1, 1, 10, 5, tzinfo=UTC)
    return b"".join([chunk async for chunk in client.export_clip(camera, start, end)])


@respx.mock
async def test_export_streams_bytes_with_the_range_as_millis():
    _login_ok()
    route = respx.get(f"{BASE}/proxy/protect/api/video/export").mock(
        return_value=httpx.Response(200, content=b"mp4-bytes-here")
    )
    async with ProtectClient(_settings()) as client:
        assert await _collect(client) == b"mp4-bytes-here"
    params = route.calls[0].request.url.params
    assert params["camera"] == "cam1"
    assert params["start"] == str(to_millis(datetime(2026, 1, 1, 10, 0, tzinfo=UTC)))
    assert params["end"] == str(to_millis(datetime(2026, 1, 1, 10, 5, tzinfo=UTC)))


@respx.mock
async def test_export_falls_back_to_the_path_style_endpoint():
    _login_ok()
    respx.get(f"{BASE}/proxy/protect/api/video/export").mock(return_value=httpx.Response(404))
    fallback = respx.get(f"{BASE}/proxy/protect/api/video/export/cam1").mock(
        return_value=httpx.Response(200, content=b"fallback-bytes")
    )
    async with ProtectClient(_settings()) as client:
        assert await _collect(client) == b"fallback-bytes"
    assert fallback.called


@respx.mock
async def test_export_failure_lists_what_was_tried_and_names_the_fix():
    _login_ok()
    for path in (
        "/proxy/protect/api/video/export",
        "/proxy/protect/api/video/export/cam1",
        "/proxy/protect/integration/v1/cameras/cam1/video/export",
    ):
        respx.get(f"{BASE}{path}").mock(return_value=httpx.Response(400, text="bad range"))
    async with ProtectClient(_settings()) as client:
        with pytest.raises(ProtectError) as excinfo:
            await _collect(client)
    message = str(excinfo.value)
    assert "bad range" in message
    assert "PROTECT_USERNAME" in message, "the error should name the likely fix"


@respx.mock
async def test_export_rejects_an_inverted_range():
    _login_ok()
    async with ProtectClient(_settings()) as client:
        moment = datetime(2026, 1, 1, tzinfo=UTC)
        with pytest.raises(ProtectError, match="end must be after start"):
            async for _ in client.export_clip("cam1", moment, moment):
                pass


@respx.mock
async def test_snapshot_returns_jpeg_bytes():
    _login_ok()
    respx.get(f"{BASE}/proxy/protect/api/cameras/cam1/snapshot").mock(
        return_value=httpx.Response(200, content=b"\xff\xd8jpeg")
    )
    async with ProtectClient(_settings()) as client:
        assert await client.snapshot("cam1") == b"\xff\xd8jpeg"


@respx.mock
async def test_snapshot_tries_the_integration_path_before_giving_up():
    _login_ok()
    respx.get(f"{BASE}/proxy/protect/api/cameras/cam1/snapshot").mock(
        return_value=httpx.Response(404)
    )
    fallback = respx.get(f"{BASE}/proxy/protect/integration/v1/cameras/cam1/snapshot").mock(
        return_value=httpx.Response(200, content=b"\xff\xd8from-integration")
    )
    async with ProtectClient(_settings()) as client:
        assert await client.snapshot("cam1") == b"\xff\xd8from-integration"
    assert fallback.called


@respx.mock
async def test_snapshot_raises_when_no_endpoint_answers():
    _login_ok()
    respx.get(f"{BASE}/proxy/protect/api/cameras/cam1/snapshot").mock(
        return_value=httpx.Response(404)
    )
    respx.get(f"{BASE}/proxy/protect/integration/v1/cameras/cam1/snapshot").mock(
        return_value=httpx.Response(404)
    )
    async with ProtectClient(_settings()) as client:
        with pytest.raises(ProtectError, match="No snapshot available"):
            await client.snapshot("cam1")
