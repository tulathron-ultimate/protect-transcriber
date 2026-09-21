"""SQLite-backed job store.

All work happens off the event loop via ``asyncio.to_thread``. Transcript text is
mirrored into an FTS5 table so "find the clip where someone said X" is one query.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

JobStatus = str  # queued | exporting | extracting | transcribing | completed | failed | canceled

TERMINAL_STATUSES = ("completed", "failed", "canceled")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    status          TEXT NOT NULL,
    stage           TEXT NOT NULL DEFAULT '',
    progress        REAL NOT NULL DEFAULT 0,
    title           TEXT NOT NULL DEFAULT '',
    camera_id       TEXT NOT NULL DEFAULT '',
    camera_name     TEXT NOT NULL DEFAULT '',
    range_start     TEXT,
    range_end       TEXT,
    source          TEXT NOT NULL DEFAULT 'protect',
    language        TEXT,
    task            TEXT NOT NULL DEFAULT 'transcribe',
    prompt          TEXT,
    clip_path       TEXT,
    audio_path      TEXT,
    clip_bytes      INTEGER NOT NULL DEFAULT 0,
    audio_seconds   REAL NOT NULL DEFAULT 0,
    chunk_count     INTEGER NOT NULL DEFAULT 0,
    chunks_done     INTEGER NOT NULL DEFAULT 0,
    detected_language TEXT,
    text            TEXT NOT NULL DEFAULT '',
    segments        TEXT NOT NULL DEFAULT '[]',
    instances       TEXT NOT NULL DEFAULT '[]',
    error           TEXT,
    started_at      TEXT,
    finished_at     TEXT,
    preview_id      TEXT,
    clip_offset     REAL NOT NULL DEFAULT 0,
    clip_duration   REAL,
    summary         TEXT,
    review          TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

CREATE TABLE IF NOT EXISTS analysis_config (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    base_url   TEXT NOT NULL DEFAULT '',
    api_key    TEXT NOT NULL DEFAULT '',
    model      TEXT NOT NULL DEFAULT '',
    enabled    INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS previews (
    id           TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'queued',
    camera_id    TEXT NOT NULL DEFAULT '',
    camera_name  TEXT NOT NULL DEFAULT '',
    range_start  TEXT,
    range_end    TEXT,
    clip_path    TEXT,
    audio_path   TEXT,
    clip_bytes   INTEGER NOT NULL DEFAULT 0,
    duration     REAL NOT NULL DEFAULT 0,
    has_audio    INTEGER NOT NULL DEFAULT 0,
    peaks        TEXT NOT NULL DEFAULT '[]',
    error        TEXT
);

CREATE INDEX IF NOT EXISTS idx_previews_range
    ON previews(camera_id, range_start, range_end);

CREATE TABLE IF NOT EXISTS whisper_instances (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    url         TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'auto',
    model       TEXT NOT NULL DEFAULT '',
    concurrency INTEGER NOT NULL DEFAULT 1,
    api_key     TEXT NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,
    position    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS job_search USING fts5(
    job_id UNINDEXED,
    title,
    camera_name,
    text,
    tokenize = 'porter unicode61'
);
"""


@dataclass(slots=True)
class JobRecord:
    data: dict[str, Any]

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    @property
    def id(self) -> str:
        return str(self.data["id"])

    @property
    def status(self) -> str:
        return str(self.data["status"])


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    for key in ("segments", "instances"):
        raw = data.get(key)
        try:
            data[key] = json.loads(raw) if raw else []
        except (TypeError, ValueError):
            data[key] = []
    return data


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class JobStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        return _connect(self._path)

    # -- lifecycle ----------------------------------------------------------

    def _init_sync(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            # A job left mid-flight by a container restart can never finish.
            conn.execute(
                "UPDATE jobs SET status='failed', stage='', "
                "error=COALESCE(error, 'Interrupted by a service restart'), "
                "finished_at=?, updated_at=? "
                "WHERE status NOT IN ('completed','failed','canceled')",
                (_now(), _now()),
            )

    async def init(self) -> None:
        await asyncio.to_thread(self._init_sync)

    # -- writes -------------------------------------------------------------

    def _create_sync(self, job: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        job.setdefault("created_at", now)
        job.setdefault("updated_at", now)
        job.setdefault("status", "queued")
        columns = ", ".join(job.keys())
        placeholders = ", ".join(f":{k}" for k in job)
        with self._connect() as conn:
            conn.execute(f"INSERT INTO jobs ({columns}) VALUES ({placeholders})", job)
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job["id"],)).fetchone()
        return _row_to_dict(row)

    async def create(self, job: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(self._create_sync, job)

    def _update_sync(self, job_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        payload = dict(fields)
        for key in ("segments", "instances"):
            if key in payload and not isinstance(payload[key], str):
                payload[key] = json.dumps(payload[key])
        payload["updated_at"] = _now()
        assignments = ", ".join(f"{k} = :{k}" for k in payload)
        payload["id"] = job_id
        with self._connect() as conn:
            conn.execute(f"UPDATE jobs SET {assignments} WHERE id = :id", payload)
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                return None
            # Keep the search index in step whenever transcript text lands.
            if "text" in fields or "title" in fields:
                conn.execute("DELETE FROM job_search WHERE job_id = ?", (job_id,))
                if row["text"]:
                    conn.execute(
                        "INSERT INTO job_search (job_id, title, camera_name, text) "
                        "VALUES (?, ?, ?, ?)",
                        (job_id, row["title"], row["camera_name"], row["text"]),
                    )
        return _row_to_dict(row)

    async def update(self, job_id: str, **fields: Any) -> dict[str, Any] | None:
        async with self._lock:
            return await asyncio.to_thread(self._update_sync, job_id, fields)

    def _delete_sync(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                return None
            conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            conn.execute("DELETE FROM job_search WHERE job_id = ?", (job_id,))
        return _row_to_dict(row)

    async def delete(self, job_id: str) -> dict[str, Any] | None:
        async with self._lock:
            return await asyncio.to_thread(self._delete_sync, job_id)

    # -- reads --------------------------------------------------------------

    def _get_sync(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_dict(row) if row else None

    async def get(self, job_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_sync, job_id)

    def _list_sync(
        self, status: str | None, limit: int, offset: int, camera_id: str | None
    ) -> list[dict[str, Any]]:
        clauses, params = [], []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if camera_id:
            clauses.append("camera_id = ?")
            params.append(camera_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params += [limit, offset]
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM jobs {where} ORDER BY created_at DESC LIMIT ? OFFSET ?", params
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    async def list(
        self,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
        camera_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_sync, status, limit, offset, camera_id)

    def _search_sync(self, query: str, limit: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            try:
                rows = conn.execute(
                    "SELECT j.*, snippet(job_search, 3, '<mark>', '</mark>', '...', 24) AS snippet "
                    "FROM job_search s JOIN jobs j ON j.id = s.job_id "
                    "WHERE job_search MATCH ? ORDER BY rank LIMIT ?",
                    (query, limit),
                ).fetchall()
            except sqlite3.OperationalError as exc:
                # FTS5 rejects unbalanced quotes and bare operators; treat the
                # whole thing as a phrase rather than surfacing a 500.
                log.debug("FTS query %r rejected (%s); retrying as a phrase", query, exc)
                phrase = '"' + query.replace('"', " ") + '"'
                rows = conn.execute(
                    "SELECT j.*, snippet(job_search, 3, '<mark>', '</mark>', '...', 24) AS snippet "
                    "FROM job_search s JOIN jobs j ON j.id = s.job_id "
                    "WHERE job_search MATCH ? ORDER BY rank LIMIT ?",
                    (phrase, limit),
                ).fetchall()
        return [_row_to_dict(row) for row in rows]

    async def search(self, query: str, limit: int = 50) -> list[dict[str, Any]]:
        query = query.strip()
        if not query:
            return []
        return await asyncio.to_thread(self._search_sync, query, limit)

    def _stats_sync(self) -> dict[str, Any]:
        with self._connect() as conn:
            counts = {
                row["status"]: row["n"]
                for row in conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")
            }
            totals = conn.execute(
                "SELECT COUNT(*) AS jobs, COALESCE(SUM(audio_seconds),0) AS seconds, "
                "COALESCE(SUM(clip_bytes),0) AS bytes FROM jobs"
            ).fetchone()
        return {
            "byStatus": counts,
            "totalJobs": totals["jobs"],
            "totalAudioSeconds": round(totals["seconds"], 1),
            "totalClipBytes": totals["bytes"],
        }

    async def stats(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._stats_sync)

    def _expired_sync(self, older_than: datetime) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE created_at < ? AND status IN "
                "('completed','failed','canceled')",
                (older_than.isoformat(timespec="seconds"),),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    async def expired(self, retention_days: int) -> list[dict[str, Any]]:
        if retention_days <= 0:
            return []
        cutoff = datetime.now(tz=UTC) - timedelta(days=retention_days)
        return await asyncio.to_thread(self._expired_sync, cutoff)


class InstanceStore:
    """Whisper instances, editable at runtime instead of only through the env.

    ``WHISPER_INSTANCES`` seeds this table on first start so existing installs
    keep working and their instances show up in the UI ready to edit. After that
    the table is the source of truth and the environment variable is ignored --
    otherwise a container restart would silently undo changes made in the UI.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    # -- reads --------------------------------------------------------------

    def _list_sync(self) -> list[dict[str, Any]]:
        with _connect(self._path) as conn:
            rows = conn.execute("SELECT * FROM whisper_instances ORDER BY position, id").fetchall()
        return [dict(row) for row in rows]

    async def list(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_sync)

    def _get_sync(self, instance_id: int) -> dict[str, Any] | None:
        with _connect(self._path) as conn:
            row = conn.execute(
                "SELECT * FROM whisper_instances WHERE id = ?", (instance_id,)
            ).fetchone()
        return dict(row) if row else None

    async def get(self, instance_id: int) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_sync, instance_id)

    # -- writes -------------------------------------------------------------

    def _next_position_sync(self, conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 AS n FROM whisper_instances")
        return int(row.fetchone()["n"])

    def _add_sync(self, values: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        with _connect(self._path) as conn:
            values.setdefault("position", self._next_position_sync(conn))
            cursor = conn.execute(
                "INSERT INTO whisper_instances "
                "(name, url, kind, model, concurrency, api_key, enabled, position, "
                " created_at, updated_at) "
                "VALUES (:name, :url, :kind, :model, :concurrency, :api_key, :enabled, "
                ":position, :created_at, :updated_at)",
                {**values, "created_at": now, "updated_at": now},
            )
            row = conn.execute(
                "SELECT * FROM whisper_instances WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return dict(row)

    async def add(self, **values: Any) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(self._add_sync, values)

    def _update_sync(self, instance_id: int, fields: dict[str, Any]) -> dict[str, Any] | None:
        payload = {**fields, "updated_at": _now()}
        assignments = ", ".join(f"{key} = :{key}" for key in payload)
        payload["id"] = instance_id
        with _connect(self._path) as conn:
            conn.execute(f"UPDATE whisper_instances SET {assignments} WHERE id = :id", payload)
            row = conn.execute(
                "SELECT * FROM whisper_instances WHERE id = ?", (instance_id,)
            ).fetchone()
        return dict(row) if row else None

    async def update(self, instance_id: int, **fields: Any) -> dict[str, Any] | None:
        async with self._lock:
            return await asyncio.to_thread(self._update_sync, instance_id, fields)

    def _delete_sync(self, instance_id: int) -> bool:
        with _connect(self._path) as conn:
            cursor = conn.execute("DELETE FROM whisper_instances WHERE id = ?", (instance_id,))
        return cursor.rowcount > 0

    async def delete(self, instance_id: int) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._delete_sync, instance_id)

    def _seed_sync(self, rows: list[dict[str, Any]]) -> int:
        now = _now()
        with _connect(self._path) as conn:
            existing = conn.execute("SELECT COUNT(*) AS n FROM whisper_instances").fetchone()["n"]
            if existing:
                return 0
            for position, values in enumerate(rows):
                conn.execute(
                    "INSERT INTO whisper_instances "
                    "(name, url, kind, model, concurrency, api_key, enabled, position, "
                    " created_at, updated_at) "
                    "VALUES (:name, :url, :kind, :model, :concurrency, :api_key, 1, :position, "
                    ":created_at, :updated_at)",
                    {**values, "position": position, "created_at": now, "updated_at": now},
                )
        return len(rows)

    async def seed(self, rows: list[dict[str, Any]]) -> int:
        """Insert ``rows`` only when the table is empty. Returns how many landed."""
        async with self._lock:
            return await asyncio.to_thread(self._seed_sync, rows)


class PreviewStore:
    """Exported clips kept around so a range can be watched before transcribing.

    A preview is deliberately cheap to re-request: the same camera and range
    returns the existing row when its files are still on disk, so nudging the
    selection and previewing again does not re-export what is already there.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    def _create_sync(self, values: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        values = dict(values)
        values.setdefault("created_at", now)
        values.setdefault("updated_at", now)
        columns = ", ".join(values)
        placeholders = ", ".join(f":{key}" for key in values)
        with _connect(self._path) as conn:
            conn.execute(f"INSERT INTO previews ({columns}) VALUES ({placeholders})", values)
            row = conn.execute("SELECT * FROM previews WHERE id = ?", (values["id"],)).fetchone()
        return _preview_row(row)

    async def create(self, **values: Any) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(self._create_sync, values)

    def _update_sync(self, preview_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        payload = dict(fields)
        if "peaks" in payload and not isinstance(payload["peaks"], str):
            payload["peaks"] = json.dumps([round(float(p), 4) for p in payload["peaks"]])
        payload["updated_at"] = _now()
        assignments = ", ".join(f"{key} = :{key}" for key in payload)
        payload["id"] = preview_id
        with _connect(self._path) as conn:
            conn.execute(f"UPDATE previews SET {assignments} WHERE id = :id", payload)
            row = conn.execute("SELECT * FROM previews WHERE id = ?", (preview_id,)).fetchone()
        return _preview_row(row) if row else None

    async def update(self, preview_id: str, **fields: Any) -> dict[str, Any] | None:
        async with self._lock:
            return await asyncio.to_thread(self._update_sync, preview_id, fields)

    def _get_sync(self, preview_id: str) -> dict[str, Any] | None:
        with _connect(self._path) as conn:
            row = conn.execute("SELECT * FROM previews WHERE id = ?", (preview_id,)).fetchone()
        return _preview_row(row) if row else None

    async def get(self, preview_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_sync, preview_id)

    def _find_sync(self, camera_id: str, start: str, end: str) -> dict[str, Any] | None:
        with _connect(self._path) as conn:
            row = conn.execute(
                "SELECT * FROM previews WHERE camera_id = ? AND range_start = ? "
                "AND range_end = ? AND status = 'ready' ORDER BY created_at DESC LIMIT 1",
                (camera_id, start, end),
            ).fetchone()
        return _preview_row(row) if row else None

    async def find_ready(self, camera_id: str, start: str, end: str) -> dict[str, Any] | None:
        """An existing ready preview for exactly this range, if its clip survives."""
        row = await asyncio.to_thread(self._find_sync, camera_id, start, end)
        if row is None:
            return None
        clip = row.get("clip_path")
        if not clip or not Path(clip).exists():
            return None
        return row

    def _delete_sync(self, preview_id: str) -> dict[str, Any] | None:
        with _connect(self._path) as conn:
            row = conn.execute("SELECT * FROM previews WHERE id = ?", (preview_id,)).fetchone()
            if row is None:
                return None
            conn.execute("DELETE FROM previews WHERE id = ?", (preview_id,))
        return _preview_row(row)

    async def delete(self, preview_id: str) -> dict[str, Any] | None:
        async with self._lock:
            return await asyncio.to_thread(self._delete_sync, preview_id)

    def _expired_sync(self, cutoff: str, keep_ids: set[str]) -> list[dict[str, Any]]:
        with _connect(self._path) as conn:
            rows = conn.execute("SELECT * FROM previews WHERE created_at < ?", (cutoff,)).fetchall()
        return [_preview_row(row) for row in rows if row["id"] not in keep_ids]

    async def expired(self, retention_hours: int, keep_ids: set[str]) -> list[dict[str, Any]]:
        """Old previews, excluding any whose clip a job still depends on."""
        if retention_hours <= 0:
            return []
        cutoff = datetime.now(tz=UTC) - timedelta(hours=retention_hours)
        return await asyncio.to_thread(
            self._expired_sync, cutoff.isoformat(timespec="seconds"), keep_ids
        )

    def _referenced_sync(self) -> set[str]:
        with _connect(self._path) as conn:
            rows = conn.execute(
                "SELECT DISTINCT preview_id FROM jobs WHERE preview_id IS NOT NULL "
                "AND status NOT IN ('failed', 'canceled')"
            ).fetchall()
        return {row["preview_id"] for row in rows if row["preview_id"]}

    async def referenced_by_jobs(self) -> set[str]:
        """Preview ids whose clip a surviving job is still using for playback."""
        return await asyncio.to_thread(self._referenced_sync)


def _preview_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    try:
        data["peaks"] = json.loads(data.get("peaks") or "[]")
    except (TypeError, ValueError):
        data["peaks"] = []
    return data


class AnalysisConfigStore:
    """The single LLM endpoint used for summaries, review and Q&A.

    Stored rather than configured through the environment for the same reason as
    the Whisper instances: changing a model or endpoint should not need a
    container restart.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    def _get_sync(self) -> dict[str, Any]:
        with _connect(self._path) as conn:
            row = conn.execute("SELECT * FROM analysis_config WHERE id = 1").fetchone()
        if row is None:
            return {
                "base_url": "",
                "api_key": "",
                "model": "",
                "enabled": 0,
                "updated_at": "",
            }
        return dict(row)

    async def get(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_sync)

    def _save_sync(self, fields: dict[str, Any]) -> dict[str, Any]:
        current = self._get_sync()
        merged = {**current, **fields}
        merged.pop("id", None)
        merged["updated_at"] = _now()
        with _connect(self._path) as conn:
            conn.execute(
                "INSERT INTO analysis_config (id, base_url, api_key, model, enabled, updated_at) "
                "VALUES (1, :base_url, :api_key, :model, :enabled, :updated_at) "
                "ON CONFLICT(id) DO UPDATE SET base_url=:base_url, api_key=:api_key, "
                "model=:model, enabled=:enabled, updated_at=:updated_at",
                merged,
            )
        return self._get_sync()

    async def save(self, **fields: Any) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(self._save_sync, fields)
