"""Minimal async UniFi Protect client.

Covers exactly what this app needs: authenticate, list cameras, list events (so
the UI timeline has markers to aim at), pull a snapshot, and export an mp4 for an
arbitrary time range.

Two auth modes are supported:

* **Local account** (``PROTECT_USERNAME``/``PROTECT_PASSWORD``) -- logs in against
  UniFi OS, keeps the ``TOKEN`` cookie and the CSRF token. This is the mode that
  reliably drives the clip-export endpoint.
* **API key** (``PROTECT_API_KEY``) -- sends ``X-API-KEY`` at the newer Protect
  integration API. Great for read-only listing; export coverage varies by
  firmware, so the client falls back to the session mode when a key-only export
  is rejected.

Endpoint paths differ slightly between Protect/UniFi OS versions, so requests go
through an ordered list of candidates and the first one that answers is
remembered for the rest of the process.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from .config import Settings

log = logging.getLogger(__name__)

# UniFi OS refuses requests without a browser-ish UA on some firmware.
_USER_AGENT = "protect-transcriber/0.1 (+https://github.com/tulathron-ultimate/protect-transcriber)"


class ProtectError(RuntimeError):
    """Any failure talking to UniFi Protect."""


class ProtectAuthError(ProtectError):
    """Credentials were rejected."""


def to_millis(value: datetime) -> int:
    """UTC-anchored epoch milliseconds, which is what Protect speaks."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def from_millis(value: int | float | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromtimestamp(float(value) / 1000.0, tz=UTC)


@dataclass(slots=True)
class Camera:
    id: str
    name: str
    type: str = ""
    state: str = ""
    has_mic: bool = False
    mic_enabled: bool = True
    is_recording: bool = False
    recording_start: datetime | None = None

    @property
    def has_audio(self) -> bool:
        """Whether a transcript is even plausible for this camera."""
        return self.has_mic and self.mic_enabled

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "state": self.state,
            "hasMic": self.has_mic,
            "micEnabled": self.mic_enabled,
            "hasAudio": self.has_audio,
            "isRecording": self.is_recording,
            "recordingStart": self.recording_start.isoformat() if self.recording_start else None,
        }

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> Camera:
        flags = payload.get("featureFlags") or {}
        stats = (payload.get("stats") or {}).get("video") or {}
        # Integration API uses a flatter shape than /proxy/protect/api/cameras.
        has_mic = bool(flags.get("hasMic", payload.get("hasMic", False)))
        mic_volume = payload.get("micVolume")
        return cls(
            id=str(payload.get("id", "")),
            name=payload.get("name") or payload.get("marketName") or "camera",
            type=payload.get("type") or payload.get("modelKey") or "",
            state=payload.get("state") or "",
            has_mic=has_mic,
            # micVolume == 0 means the mic is muted in Protect, so no audio track.
            mic_enabled=True if mic_volume is None else int(mic_volume) > 0,
            is_recording=bool(payload.get("isRecording", False)),
            recording_start=from_millis(stats.get("recordingStart")),
        )


@dataclass(slots=True)
class ProtectEvent:
    """A Protect event, used purely as a timeline marker in the UI."""

    id: str
    type: str
    camera_id: str
    start: datetime | None
    end: datetime | None
    score: int = 0
    smart_types: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "cameraId": self.camera_id,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "score": self.score,
            "smartTypes": self.smart_types,
        }

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> ProtectEvent:
        return cls(
            id=str(payload.get("id", "")),
            type=payload.get("type") or "",
            camera_id=str(payload.get("camera") or payload.get("cameraId") or ""),
            start=from_millis(payload.get("start")),
            end=from_millis(payload.get("end")),
            score=int(payload.get("score") or 0),
            smart_types=list(payload.get("smartDetectTypes") or []),
        )


class ProtectClient:
    """Async client with lazy login and endpoint-shape discovery."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=settings.protect_base_url,
            verify=settings.protect_verify_ssl,
            timeout=httpx.Timeout(settings.protect_timeout, read=settings.protect_export_timeout),
            follow_redirects=False,
            headers={"User-Agent": _USER_AGENT},
        )
        self._csrf: str | None = None
        self._logged_in = False
        self._login_lock = asyncio.Lock()
        # Remembered working endpoint shapes, so we only probe once per process.
        self._camera_path: str | None = None
        self._event_path: str | None = None
        self._export_shape: str | None = None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> ProtectClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # -- auth ---------------------------------------------------------------

    @property
    def _use_api_key(self) -> bool:
        s = self._settings
        return bool(s.protect_api_key) and not (s.protect_username and s.protect_password)

    async def login(self, force: bool = False) -> None:
        """Establish a UniFi OS session. No-op in API-key-only mode."""
        if self._use_api_key:
            return
        s = self._settings
        if not (s.protect_username and s.protect_password):
            raise ProtectAuthError(
                "No UniFi Protect credentials configured. Set PROTECT_USERNAME and "
                "PROTECT_PASSWORD (a local Protect account), or PROTECT_API_KEY."
            )
        async with self._login_lock:
            if self._logged_in and not force:
                return
            try:
                response = await self._client.post(
                    "/api/auth/login",
                    json={
                        "username": s.protect_username,
                        "password": s.protect_password,
                        "rememberMe": True,
                    },
                )
            except httpx.HTTPError as exc:
                raise ProtectError(
                    f"Cannot reach UniFi Protect at {s.protect_base_url}: {exc}"
                ) from exc

            if response.status_code in (401, 403):
                raise ProtectAuthError(
                    "UniFi Protect rejected the credentials. If the account has 2FA "
                    "enabled, create a dedicated local account without it."
                )
            if response.status_code == 429:
                raise ProtectAuthError("UniFi Protect is rate-limiting logins; wait and retry.")
            if response.status_code >= 400:
                raise ProtectAuthError(f"Login failed with HTTP {response.status_code}")

            self._capture_csrf(response)
            if not self._client.cookies.get("TOKEN"):
                raise ProtectAuthError("Login succeeded but no TOKEN cookie was returned.")
            self._logged_in = True
            log.info("Authenticated to UniFi Protect at %s", s.protect_base_url)

    def _capture_csrf(self, response: httpx.Response) -> None:
        for header in ("X-Updated-CSRF-Token", "X-CSRF-Token"):
            token = response.headers.get(header)
            if token:
                self._csrf = token

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._use_api_key:
            headers["X-API-KEY"] = self._settings.protect_api_key
        elif self._settings.protect_api_key:
            # Session auth is primary, but passing the key too is harmless and
            # lets integration-API paths work without a second round trip.
            headers["X-API-KEY"] = self._settings.protect_api_key
        if self._csrf:
            headers["X-CSRF-Token"] = self._csrf
        if extra:
            headers.update(extra)
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        retry_auth: bool = True,
        **kwargs: Any,
    ) -> httpx.Response:
        await self.login()
        response = await self._client.request(
            method, path, params=params, headers=self._headers(), **kwargs
        )
        self._capture_csrf(response)
        if response.status_code in (401, 403) and retry_auth and not self._use_api_key:
            log.info("Protect session expired (HTTP %s); re-authenticating", response.status_code)
            self._logged_in = False
            await self.login(force=True)
            response = await self._client.request(
                method, path, params=params, headers=self._headers(), **kwargs
            )
            self._capture_csrf(response)
        return response

    # -- reads --------------------------------------------------------------

    async def _get_json(self, candidates: list[str], **kwargs: Any) -> tuple[str, Any]:
        """Try each candidate path, return (path, parsed json) for the first hit."""
        last_error = "no candidate paths"
        for path in candidates:
            response = await self._request("GET", path, **kwargs)
            if response.status_code == 404:
                last_error = f"{path} -> 404"
                continue
            if response.status_code in (401, 403):
                raise ProtectAuthError(
                    f"Not authorized for {path} (HTTP {response.status_code}). The Protect "
                    "account needs at least read access to the cameras."
                )
            if response.status_code >= 400:
                last_error = f"{path} -> HTTP {response.status_code}"
                continue
            try:
                return path, response.json()
            except ValueError:
                last_error = f"{path} -> response was not JSON"
        raise ProtectError(f"UniFi Protect request failed ({last_error})")

    async def nvr_info(self) -> dict[str, Any]:
        candidates = ["/proxy/protect/integration/v1/meta/info"] if self._use_api_key else []
        candidates += ["/proxy/protect/api/bootstrap"]
        _, payload = await self._get_json(candidates)
        nvr = payload.get("nvr") if isinstance(payload, dict) else None
        if isinstance(nvr, dict):
            return {
                "name": nvr.get("name"),
                "version": nvr.get("version"),
                "host": nvr.get("host"),
                "timezone": nvr.get("timezone"),
            }
        if isinstance(payload, dict):
            return {"version": payload.get("applicationVersion") or payload.get("version")}
        return {}

    async def cameras(self) -> list[Camera]:
        candidates = [self._camera_path] if self._camera_path else []
        if self._use_api_key:
            candidates += ["/proxy/protect/integration/v1/cameras", "/proxy/protect/api/cameras"]
        else:
            candidates += ["/proxy/protect/api/cameras", "/proxy/protect/integration/v1/cameras"]
        path, payload = await self._get_json([c for c in candidates if c])
        self._camera_path = path
        if isinstance(payload, dict):
            payload = payload.get("cameras", [])
        cameras = [Camera.from_api(item) for item in payload if isinstance(item, dict)]
        cameras.sort(key=lambda c: c.name.lower())
        return cameras

    async def camera(self, camera_id: str) -> Camera:
        for cam in await self.cameras():
            if cam.id == camera_id:
                return cam
        raise ProtectError(f"No camera with id {camera_id!r}")

    async def events(
        self,
        start: datetime,
        end: datetime,
        camera_id: str | None = None,
        types: list[str] | None = None,
        limit: int = 500,
    ) -> list[ProtectEvent]:
        params: dict[str, Any] = {
            "start": to_millis(start),
            "end": to_millis(end),
            "limit": limit,
        }
        if camera_id:
            params["cameras"] = camera_id
        if types:
            params["types"] = types
        candidates = [self._event_path] if self._event_path else []
        candidates += ["/proxy/protect/api/events", "/proxy/protect/integration/v1/events"]
        try:
            path, payload = await self._get_json([c for c in candidates if c], params=params)
        except ProtectError:
            # Timeline markers are a nicety; never fail a page render over them.
            log.warning("Could not load Protect events for the timeline", exc_info=True)
            return []
        self._event_path = path
        if isinstance(payload, dict):
            payload = payload.get("events", [])
        events = [ProtectEvent.from_api(item) for item in payload if isinstance(item, dict)]
        return [e for e in events if e.start is not None]

    async def snapshot(self, camera_id: str, at: datetime | None = None) -> bytes:
        params: dict[str, Any] = {"force": "true"}
        if at is not None:
            params["ts"] = to_millis(at)
        for path in (
            f"/proxy/protect/api/cameras/{camera_id}/snapshot",
            f"/proxy/protect/integration/v1/cameras/{camera_id}/snapshot",
        ):
            response = await self._request("GET", path, params=params)
            if response.status_code == 200 and response.content:
                return response.content
        raise ProtectError(f"No snapshot available for camera {camera_id}")

    # -- export -------------------------------------------------------------

    def _export_candidates(
        self, camera_id: str, start_ms: int, end_ms: int
    ) -> list[tuple[str, dict]]:
        """Ordered (path, params) pairs covering the known export endpoint shapes."""
        query = {"camera": camera_id, "start": start_ms, "end": end_ms}
        shapes: list[tuple[str, dict]] = [
            ("/proxy/protect/api/video/export", query),
            (f"/proxy/protect/api/video/export/{camera_id}", {"start": start_ms, "end": end_ms}),
            (
                f"/proxy/protect/integration/v1/cameras/{camera_id}/video/export",
                {"start": start_ms, "end": end_ms},
            ),
        ]
        if self._export_shape:
            shapes.sort(key=lambda item: item[0] != self._export_shape)
        return shapes

    async def export_clip(
        self, camera_id: str, start: datetime, end: datetime, chunk_size: int = 1 << 18
    ) -> AsyncIterator[bytes]:
        """Stream an mp4 for ``[start, end)`` from the NVR's recordings.

        Yields raw bytes so a multi-hundred-megabyte clip never has to be held in
        memory. Raises :class:`ProtectError` before yielding anything if no
        endpoint shape works.
        """
        start_ms, end_ms = to_millis(start), to_millis(end)
        if end_ms <= start_ms:
            raise ProtectError("Export end must be after start")

        await self.login()
        failures: list[str] = []
        for path, params in self._export_candidates(camera_id, start_ms, end_ms):
            request = self._client.build_request(
                "GET", path, params=params, headers=self._headers({"Accept": "video/mp4"})
            )
            response = await self._client.send(request, stream=True)
            if response.status_code >= 400:
                body = (await response.aread())[:300].decode("utf-8", "replace")
                await response.aclose()
                failures.append(f"{path} -> HTTP {response.status_code} {body.strip()}")
                if response.status_code in (401, 403) and not self._use_api_key:
                    # One re-auth attempt, then move on to the next shape.
                    self._logged_in = False
                    await self.login(force=True)
                    retry = await self._client.send(
                        self._client.build_request(
                            "GET", path, params=params, headers=self._headers()
                        ),
                        stream=True,
                    )
                    if retry.status_code < 400:
                        response = retry
                    else:
                        await retry.aclose()
                        continue
                else:
                    continue

            self._export_shape = path
            log.info(
                "Exporting camera %s %s..%s via %s",
                camera_id,
                start.isoformat(),
                end.isoformat(),
                path,
            )
            try:
                async for chunk in response.aiter_bytes(chunk_size):
                    if chunk:
                        yield chunk
            finally:
                await response.aclose()
            return

        raise ProtectError(
            "UniFi Protect refused the clip export. Tried: "
            + "; ".join(failures)
            + ". Export needs a local Protect account (PROTECT_USERNAME/PROTECT_PASSWORD); "
            "an API key alone is not always enough."
        )
