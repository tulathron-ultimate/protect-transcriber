"""Configuration for protect-transcriber.

Everything is driven by environment variables so the whole thing can live in an
Unraid Docker template. The only genuinely required values are the UniFi Protect
host plus one credential (local account or API key), and at least one Whisper
instance URL.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)

BackendKind = Literal["auto", "openai", "asr_webservice", "whisper_cpp"]


@dataclass(slots=True)
class WhisperInstance:
    """One self-hosted Whisper container."""

    name: str
    url: str
    kind: BackendKind = "auto"
    model: str = "Systran/faster-whisper-base"
    concurrency: int = 1
    api_key: str = ""
    # Populated at runtime by the pool once probing settles.
    resolved_kind: BackendKind | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        self.url = self.url.rstrip("/")
        self.concurrency = max(1, int(self.concurrency))


def _parse_instances(raw: str) -> list[WhisperInstance]:
    """Parse WHISPER_INSTANCES.

    Two accepted forms:

    JSON (full control)::

        [{"name": "fw", "url": "http://10.0.0.5:8000", "kind": "openai",
          "model": "Systran/faster-whisper-large-v3", "concurrency": 2}]

    Shorthand, comma separated, each entry ``[name=]url[|kind][|model][|concurrency]``::

        faster=http://10.0.0.5:8000|openai|large-v3|2,asr=http://10.0.0.5:9000|asr_webservice
    """
    raw = (raw or "").strip()
    if not raw:
        return []

    if raw.startswith("["):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:  # pragma: no cover - config error path
            raise ValueError(f"WHISPER_INSTANCES is not valid JSON: {exc}") from exc
        instances = []
        for idx, item in enumerate(payload):
            if "url" not in item:
                raise ValueError(f"WHISPER_INSTANCES[{idx}] is missing 'url'")
            instances.append(
                WhisperInstance(
                    name=item.get("name") or f"whisper{idx + 1}",
                    url=item["url"],
                    kind=item.get("kind", "auto"),
                    model=item.get("model", "Systran/faster-whisper-base"),
                    concurrency=int(item.get("concurrency", 1)),
                    api_key=item.get("api_key", ""),
                )
            )
        return instances

    instances = []
    for idx, chunk in enumerate(p.strip() for p in raw.split(",")):
        if not chunk:
            continue
        name = ""
        # Only split on '=' before the scheme, so "http://..." survives.
        if "=" in chunk.split("://", 1)[0]:
            name, chunk = chunk.split("=", 1)
        parts = [p.strip() for p in chunk.split("|")]
        url = parts[0]
        kind = parts[1] if len(parts) > 1 and parts[1] else "auto"
        model = parts[2] if len(parts) > 2 and parts[2] else "Systran/faster-whisper-base"
        concurrency = int(parts[3]) if len(parts) > 3 and parts[3] else 1
        instances.append(
            WhisperInstance(
                name=name.strip() or f"whisper{idx + 1}",
                url=url,
                kind=kind,  # type: ignore[arg-type]
                model=model,
                concurrency=concurrency,
            )
        )
    return instances


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # --- UniFi Protect -----------------------------------------------------
    protect_host: str = Field(default="", description="UDM/NVR host or IP, no scheme")
    protect_port: int = 443
    protect_username: str = ""
    protect_password: str = ""
    protect_api_key: str = ""
    protect_verify_ssl: bool = False
    protect_timeout: float = 30.0
    # Exports of long ranges take a while for the NVR to assemble.
    protect_export_timeout: float = 900.0

    # --- Whisper pool ------------------------------------------------------
    whisper_instances: str = ""
    whisper_timeout: float = 1800.0
    whisper_language: str = ""  # "" => let Whisper auto-detect
    whisper_task: Literal["transcribe", "translate"] = "transcribe"
    whisper_vad_filter: bool = True
    whisper_max_attempts: int = 3
    # Chunk-level parallelism already saturates the pool, so one job at a time is
    # usually right; raise it if you have several idle instances.
    max_concurrent_jobs: int = 1

    # --- Chunking ----------------------------------------------------------
    # Whisper degrades and containers time out on very long audio, so clips are
    # split. Boundaries are snapped to silence where possible (see app/media.py).
    chunk_seconds: int = 300
    chunk_overlap_seconds: float = 2.0
    silence_snap_window: float = 15.0

    # --- App ---------------------------------------------------------------
    data_dir: Path = Path("/data")
    app_token: str = ""  # if set, UI + API require it
    max_range_seconds: int = 4 * 60 * 60
    keep_clips: bool = True
    retention_days: int = 30
    log_level: str = "INFO"
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"

    @field_validator("protect_host")
    @classmethod
    def _strip_scheme(cls, value: str) -> str:
        value = value.strip()
        for scheme in ("https://", "http://"):
            if value.startswith(scheme):
                value = value[len(scheme) :]
        return value.rstrip("/")

    @field_validator("log_level")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def _check_credentials(self) -> Settings:
        if self.protect_host and not (
            (self.protect_username and self.protect_password) or self.protect_api_key
        ):
            log.warning(
                "PROTECT_HOST is set but no credentials were provided; "
                "set PROTECT_USERNAME/PROTECT_PASSWORD or PROTECT_API_KEY"
            )
        return self

    @property
    def protect_base_url(self) -> str:
        return f"https://{self.protect_host}:{self.protect_port}"

    @property
    def instances(self) -> list[WhisperInstance]:
        return _parse_instances(self.whisper_instances)

    @property
    def clips_dir(self) -> Path:
        return self.data_dir / "clips"

    @property
    def audio_dir(self) -> Path:
        return self.data_dir / "audio"

    @property
    def transcripts_dir(self) -> Path:
        return self.data_dir / "transcripts"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "protect-transcriber.db"

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.clips_dir, self.audio_dir, self.transcripts_dir):
            path.mkdir(parents=True, exist_ok=True)

    def configured(self) -> bool:
        """True when Protect and at least one Whisper instance are usable."""
        has_creds = (self.protect_username and self.protect_password) or self.protect_api_key
        return bool(self.protect_host and has_creds and self.instances)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
