"""Read-only SQL helpers for the dashboard.

Each function opens no connection of its own — callers pass a
`sqlite3.Connection` (ideally read-only, via
`sqlite3.connect("file:…?mode=ro", uri=True)`) so tests and handlers share
the same interface. Return types are dataclasses so templates get stable
field names.

All queries restrict to current deps only via
`project_dep.source_sha = project.last_sha`, matching `run_matcher`'s
visibility rules.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


SEVERITY_ORDER_CASE = (
    "CASE a.severity "
    "WHEN 'critical' THEN 4 "
    "WHEN 'high' THEN 3 "
    "WHEN 'medium' THEN 2 "
    "WHEN 'low' THEN 1 "
    "ELSE 0 END"
)


@dataclass(frozen=True)
class MatchRow:
    match_id: int
    advisory_id: int
    source_id: str
    severity: str | None
    cvss: float | None
    summary: str
    project_slug: str
    project_display_name: str
    ecosystem: str
    dep_name: str
    dep_version: str
    fixed_in: str | None
    first_matched: int


@dataclass(frozen=True)
class LandingData:
    kpis: dict[str, int]
    triage: list[MatchRow]
    top_projects: list[tuple[str, int]]
    top_advisories: list[tuple[str, int]]
    last_osv_ingest: int | None
    last_github_walk: int | None


def _kpis(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT COALESCE(a.severity, 'unknown') AS sev, COUNT(*)
        FROM match m
        JOIN advisory a ON a.id = m.advisory_id
        WHERE m.state = 'open'
        GROUP BY sev
        """
    ).fetchall()
    out = {"critical": 0, "high": 0, "medium": 0, "low": 0, "unknown": 0}
    for sev, n in rows:
        out[sev] = n
    return out


def _triage(conn: sqlite3.Connection, limit: int) -> list[MatchRow]:
    rows = conn.execute(
        f"""
        SELECT m.id, a.id, a.source_id, a.severity, a.cvss, a.summary,
               p.slug, p.display_name, pd.ecosystem, pd.name, pd.version,
               (SELECT GROUP_CONCAT(aa.fixed_in, ', ')
                  FROM advisory_affects aa
                  WHERE aa.advisory_id = a.id
                    AND aa.ecosystem = pd.ecosystem
                    AND aa.name = pd.name
                    AND aa.fixed_in IS NOT NULL) AS fixed_in,
               m.first_matched
        FROM match m
        JOIN advisory a ON a.id = m.advisory_id
        JOIN project p ON p.id = m.project_id
        JOIN project_dep pd ON pd.id = m.project_dep_id
        WHERE m.state = 'open'
          AND a.severity IN ('critical', 'high')
        ORDER BY {SEVERITY_ORDER_CASE} DESC, a.cvss DESC, m.first_matched DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [MatchRow(*r) for r in rows]


def _top_projects(conn: sqlite3.Connection, limit: int) -> list[tuple[str, int]]:
    return list(conn.execute(
        """
        SELECT p.slug, COUNT(*) AS n
        FROM match m
        JOIN project p ON p.id = m.project_id
        WHERE m.state = 'open'
        GROUP BY p.slug
        ORDER BY n DESC, p.slug ASC
        LIMIT ?
        """,
        (limit,),
    ))


def _top_advisories(conn: sqlite3.Connection, limit: int) -> list[tuple[str, int]]:
    return list(conn.execute(
        """
        SELECT a.source_id, COUNT(DISTINCT m.project_id) AS n
        FROM match m
        JOIN advisory a ON a.id = m.advisory_id
        WHERE m.state = 'open'
        GROUP BY a.source_id
        ORDER BY n DESC, a.source_id ASC
        LIMIT ?
        """,
        (limit,),
    ))


def _last_timestamps(conn: sqlite3.Connection) -> tuple[int | None, int | None]:
    row = conn.execute(
        "SELECT MAX(updated_at) FROM poller_state WHERE key LIKE 'osv.etag.%'"
    ).fetchone()
    last_osv = row[0]
    row = conn.execute("SELECT MAX(last_checked) FROM project").fetchone()
    last_walk = row[0]
    return last_osv, last_walk


def landing_data(
    conn: sqlite3.Connection,
    *,
    triage_limit: int = 12,
    projects_limit: int = 8,
    advisories_limit: int = 6,
) -> LandingData:
    last_osv, last_walk = _last_timestamps(conn)
    return LandingData(
        kpis=_kpis(conn),
        triage=_triage(conn, triage_limit),
        top_projects=_top_projects(conn, projects_limit),
        top_advisories=_top_advisories(conn, advisories_limit),
        last_osv_ingest=last_osv,
        last_github_walk=last_walk,
    )
