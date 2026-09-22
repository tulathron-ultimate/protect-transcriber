"""Schema upgrades on a database created by an older version.

`CREATE TABLE IF NOT EXISTS` is a no-op against a table that already exists, so
adding a column to `_SCHEMA` alone silently breaks every existing install: the
table is left as it was and writes fail with "table jobs has no column named …".
These tests pin the upgrade path and make forgetting a future migration a test
failure rather than a support thread.
"""

from __future__ import annotations

import re
import sqlite3

import pytest

from app.store import _ADDED_COLUMNS, _SCHEMA, JobStore

# The `jobs` table exactly as the first release created it. Frozen on purpose:
# it is the oldest database shape still in the wild, so it is what an upgrade
# has to cope with. Never edit it to match new code -- that is the bug.
LEGACY_JOBS_SQL = """
CREATE TABLE jobs (
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
"""


def _columns_of(create_sql: str) -> set[str]:
    """Column names from a CREATE TABLE body."""
    body = create_sql[create_sql.index("(") + 1 : create_sql.rindex(")")]
    names = set()
    for line in body.splitlines():
        line = line.strip().rstrip(",")
        if not line or line.upper().startswith(("PRIMARY", "FOREIGN", "UNIQUE", "CHECK")):
            continue
        names.add(line.split()[0])
    return names


def _schema_jobs_sql() -> str:
    match = re.search(r"CREATE TABLE IF NOT EXISTS jobs \(.*?\n\);", _SCHEMA, re.DOTALL)
    assert match, "could not find the jobs table in _SCHEMA"
    return match.group(0)


def _legacy_db(path):
    """A database as an install from the first release has it, with real data."""
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_JOBS_SQL)
    conn.execute(
        "INSERT INTO jobs (id, created_at, updated_at, status, title, text, segments) VALUES "
        "('legacy', '2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00', 'completed', "
        "'Front Door', 'the package was left by the door', '[]')"
    )
    conn.commit()
    conn.close()
    return path


def _table_columns(path, table="jobs") -> set[str]:
    with sqlite3.connect(path) as conn:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


# --------------------------------------------------------------------------- #


def test_every_new_column_has_a_migration_entry():
    """The guard: adding a column to _SCHEMA without listing it here fails here.

    Without this, the mistake only surfaces on somebody's existing install.
    """
    in_schema = _columns_of(_schema_jobs_sql())
    in_legacy = _columns_of(LEGACY_JOBS_SQL)
    migrated = {column for table, column, _ in _ADDED_COLUMNS if table == "jobs"}
    assert in_schema - in_legacy == migrated, (
        "columns added to the jobs table must also be listed in _ADDED_COLUMNS, "
        "or existing databases will never gain them"
    )


def test_migration_definitions_are_safe_to_add_to_a_populated_table():
    """SQLite refuses a NOT NULL column with no DEFAULT on a table with rows."""
    for table, column, definition in _ADDED_COLUMNS:
        upper = definition.upper()
        if "NOT NULL" in upper:
            assert "DEFAULT" in upper, f"{table}.{column} is NOT NULL but has no DEFAULT"


async def test_an_old_database_gains_the_missing_columns(tmp_path):
    path = _legacy_db(tmp_path / "jobs.db")
    assert "preview_id" not in _table_columns(path), "fixture should start without it"

    await JobStore(path).init()

    columns = _table_columns(path)
    for name in ("preview_id", "clip_offset", "clip_duration", "summary", "review"):
        assert name in columns, f"{name} was not added by the upgrade"


async def test_upgrading_preserves_existing_transcripts(tmp_path):
    """The whole point: an upgrade must not cost anyone their data."""
    path = _legacy_db(tmp_path / "jobs.db")
    store = JobStore(path)
    await store.init()

    legacy = await store.get("legacy")
    assert legacy is not None
    assert legacy["text"] == "the package was left by the door"
    assert legacy["title"] == "Front Door"
    # New columns read back as sensible defaults rather than blowing up.
    assert legacy["preview_id"] is None
    assert legacy["clip_offset"] == 0.0


async def test_creating_a_job_works_after_the_upgrade(tmp_path):
    """This is the failure that was reported: a 500 on POST /api/jobs."""
    path = _legacy_db(tmp_path / "jobs.db")
    store = JobStore(path)
    await store.init()

    created = await store.create(
        {"id": "new", "preview_id": None, "clip_offset": 0.0, "clip_duration": None}
    )
    assert created["id"] == "new"


async def test_an_upgraded_database_matches_a_fresh_one(tmp_path):
    upgraded = _legacy_db(tmp_path / "old.db")
    await JobStore(upgraded).init()
    fresh = tmp_path / "new.db"
    await JobStore(fresh).init()
    assert _table_columns(upgraded) == _table_columns(fresh)


async def test_migrating_twice_is_a_no_op(tmp_path):
    """Every container restart runs init again."""
    path = _legacy_db(tmp_path / "jobs.db")
    await JobStore(path).init()
    before = _table_columns(path)
    await JobStore(path).init()
    await JobStore(path).init()
    assert _table_columns(path) == before
    assert (await JobStore(path).get("legacy")) is not None


async def test_a_fresh_database_needs_no_migration(tmp_path):
    """_SCHEMA alone must be complete, so new installs never rely on the upgrade."""
    path = tmp_path / "fresh.db"
    await JobStore(path).init()
    columns = _table_columns(path)
    for table, column, _ in _ADDED_COLUMNS:
        if table == "jobs":
            assert column in columns


@pytest.mark.parametrize("table", ["previews", "whisper_instances", "analysis_config"])
async def test_tables_added_later_are_created_on_an_old_database(tmp_path, table):
    """Whole new tables are handled by CREATE TABLE IF NOT EXISTS, not migrations."""
    path = _legacy_db(tmp_path / "jobs.db")
    await JobStore(path).init()
    with sqlite3.connect(path) as conn:
        found = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    assert found, f"{table} was not created on an upgraded database"
