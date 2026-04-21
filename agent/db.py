"""SQLite helpers for the advisory-watch agent.

`open_db` applies the schema idempotently (IF NOT EXISTS everywhere) so tests
and the real agent both use the same path. `upsert_project` is the main write
path during seeding — idempotent on slug.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path


def open_db(path: Path, schema: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: lets asyncio.to_thread workers reuse the
    # connection. Safe because our single event loop naturally serializes
    # writes — no two tasks ever hold the conn concurrently.
    conn = sqlite3.connect(path, check_same_thread=False)
    # busy_timeout=60s: matches the worker-thread timeouts in walker/
    # matcher/osv. An OSV batch of 1000 upserts can hold the file-level
    # write lock for ~5-10s on a Pi Zero 3 SD card; 10s was too tight and
    # caused "database is locked" on the main-loop writers (telegram
    # emission inserts, etc.) during heavy ingest. 60s spans a full batch
    # plus the sub-second inter-batch gap, virtually guaranteeing success.
    conn.execute("PRAGMA busy_timeout = 60000")
    conn.executescript(schema.read_text())
    return conn


def upsert_project(
    conn: sqlite3.Connection,
    *,
    slug: str,
    display_name: str,
    repo_url: str,
    default_branch: str = "main",
) -> int:
    now = int(time.time())
    cur = conn.execute(
        """
        INSERT INTO project (slug, display_name, repo_url, default_branch, added_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(slug) DO UPDATE SET
            display_name = excluded.display_name,
            repo_url = excluded.repo_url,
            default_branch = excluded.default_branch
        RETURNING id
        """,
        (slug, display_name, repo_url, default_branch, now),
    )
    project_id = cur.fetchone()[0]
    conn.commit()
    return project_id


def upsert_project_dep(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    ecosystem: str,
    name: str,
    version: str,
    source_sha: str,
    is_direct: bool = False,
) -> None:
    """Insert or refresh a project_dep row.

    The UNIQUE constraint is (project_id, ecosystem, name, version, source_sha)
    so a new SHA for the same dep creates a new row — audit trail of when a
    dep was observed. last_seen is updated on conflict so re-running a walk
    at the same SHA is a no-op. Caller owns the transaction."""
    now = int(time.time())
    conn.execute(
        """
        INSERT INTO project_dep (
            project_id, ecosystem, name, version, is_direct,
            source_sha, first_seen, last_seen
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(project_id, ecosystem, name, version, source_sha) DO UPDATE SET
            last_seen = excluded.last_seen,
            is_direct = excluded.is_direct
        """,
        (project_id, ecosystem, name, version, 1 if is_direct else 0,
         source_sha, now, now),
    )
