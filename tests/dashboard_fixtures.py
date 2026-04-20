"""Seeders for dashboard tests — realistic project/advisory/match rows."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from agent.db import open_db, upsert_project, upsert_project_dep
from agent.sources.osv import upsert_advisory

SCHEMA = Path(__file__).parent.parent / "db" / "schema.sql"


def fresh_db(tmp_path) -> sqlite3.Connection:
    return open_db(tmp_path / "state.db", SCHEMA)


def seed_match(
    conn: sqlite3.Connection,
    *,
    project_slug: str = "nervosnetwork/ckb",
    display_name: str | None = None,
    source_id: str = "GHSA-22q8-ghmq-63vf",
    severity: str | None = "high",
    cvss: float | None = 8.6,
    summary: str = "Test advisory summary",
    ecosystem: str = "crates.io",
    dep_name: str = "libgit2-sys",
    dep_version: str = "0.16.1",
    fixed_in: str | None = "0.16.2",
    source_sha: str = "abc123",
) -> tuple[int, int, int]:
    """Seed one project + one advisory + one matching dep. Returns
    (project_id, advisory_id, match_id). The matcher is NOT run — we insert
    the match row directly to keep tests focused on queries."""
    pid = upsert_project(
        conn, slug=project_slug,
        display_name=display_name or project_slug,
        repo_url=f"https://github.com/{project_slug}",
    )
    conn.execute(
        "UPDATE project SET last_sha = ?, last_checked = ? WHERE id = ?",
        (source_sha, int(time.time()), pid),
    )
    with conn:
        upsert_project_dep(
            conn, project_id=pid, ecosystem=ecosystem, name=dep_name,
            version=dep_version, source_sha=source_sha,
        )
    (dep_id,) = conn.execute(
        "SELECT id FROM project_dep WHERE project_id=? AND ecosystem=? AND name=? AND version=?",
        (pid, ecosystem, dep_name, dep_version),
    ).fetchone()

    raw = {
        "id": source_id,
        "modified": "2026-04-01T00:00:00Z",
        "summary": summary,
        "details": "Test details",
        "database_specific": {"severity": (severity or "").upper() or None},
        "affected": [{
            "package": {"ecosystem": ecosystem, "name": dep_name},
            "ranges": [{"type": "SEMVER", "events": [{"introduced": "0"}, {"fixed": fixed_in}]} ],
        }],
        "references": [{"type": "ADVISORY", "url": f"https://example.com/{source_id}"}],
    }
    if severity is None:
        raw["database_specific"].pop("severity", None)
    if cvss is not None:
        raw["severity"] = [{"type": "CVSS_V3", "score": f"CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:L/A:L"}]
    with conn:
        aid = upsert_advisory(conn, raw)
    if cvss is not None:
        conn.execute("UPDATE advisory SET cvss = ? WHERE id = ?", (cvss, aid))
        conn.commit()

    now = int(time.time())
    cur = conn.execute(
        """
        INSERT INTO match (advisory_id, project_id, project_dep_id, first_matched, state)
        VALUES (?, ?, ?, ?, 'open')
        RETURNING id
        """,
        (aid, pid, dep_id, now),
    )
    match_id = cur.fetchone()[0]
    conn.commit()
    return pid, aid, match_id
