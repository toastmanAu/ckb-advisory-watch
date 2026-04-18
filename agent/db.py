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
    conn = sqlite3.connect(path)
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
