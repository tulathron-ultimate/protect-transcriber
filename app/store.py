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
    finished_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

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


class JobStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

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
