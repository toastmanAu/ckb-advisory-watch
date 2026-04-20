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

import json
import sqlite3
from dataclasses import dataclass, field


SEVERITY_ORDER_CASE = (
    "CASE a.severity "
    "WHEN 'critical' THEN 4 "
    "WHEN 'high' THEN 3 "
    "WHEN 'medium' THEN 2 "
    "WHEN 'low' THEN 1 "
    "ELSE 0 END"
)

_MATCH_SELECT = """
    SELECT m.id, a.id, a.source_id, a.severity, a.cvss, a.summary,
           p.slug, p.display_name, pd.ecosystem, pd.name, pd.version,
           (SELECT GROUP_CONCAT(aa.fixed_in, ', ')
              FROM advisory_affects aa
              WHERE aa.advisory_id = a.id
                AND aa.ecosystem = pd.ecosystem
                AND aa.name = pd.name
                AND aa.fixed_in IS NOT NULL),
           m.first_matched
    FROM match m
    JOIN advisory a ON a.id = m.advisory_id
    JOIN project p  ON p.id = m.project_id
    JOIN project_dep pd ON pd.id = m.project_dep_id
"""


def _fetch_match_rows(
    conn: sqlite3.Connection,
    where: str,
    params: tuple,
    order_by: str,
    limit: int | None = None,
) -> list[MatchRow]:
    sql = f"{_MATCH_SELECT} WHERE {where} ORDER BY {order_by}"
    bind: tuple = params
    if limit is not None:
        sql += " LIMIT ?"
        bind = params + (limit,)
    return [MatchRow(*r) for r in conn.execute(sql, bind).fetchall()]


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


DEFAULT_TRIAGE_SEVERITIES: tuple[str, ...] = ("critical", "high")


def _triage(
    conn: sqlite3.Connection,
    limit: int,
    severities: tuple[str, ...] = DEFAULT_TRIAGE_SEVERITIES,
) -> list[MatchRow]:
    # COALESCE handles the 'unknown' case: advisories with NULL severity show
    # up when the operator picks Unknown from the KPI tile filter.
    placeholders = ",".join("?" for _ in severities)
    return _fetch_match_rows(
        conn,
        where=f"m.state = 'open' AND COALESCE(a.severity, 'unknown') IN ({placeholders})",
        params=tuple(severities),
        order_by=f"{SEVERITY_ORDER_CASE} DESC, a.cvss DESC, m.first_matched DESC",
        limit=limit,
    )


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
    triage_severities: tuple[str, ...] = DEFAULT_TRIAGE_SEVERITIES,
    projects_limit: int = 8,
    advisories_limit: int = 6,
) -> LandingData:
    last_osv, last_walk = _last_timestamps(conn)
    return LandingData(
        kpis=_kpis(conn),
        triage=_triage(conn, triage_limit, triage_severities),
        top_projects=_top_projects(conn, projects_limit),
        top_advisories=_top_advisories(conn, advisories_limit),
        last_osv_ingest=last_osv,
        last_github_walk=last_walk,
    )


@dataclass(frozen=True)
class ProjectContext:
    project_id: int
    slug: str
    display_name: str
    repo_url: str
    default_branch: str
    last_sha: str | None
    last_checked: int | None
    matches: list[MatchRow]


@dataclass(frozen=True)
class AdvisoryContext:
    advisory_id: int
    source_id: str
    severity: str | None
    cvss: float | None
    summary: str
    details: str
    modified: int | None
    cve_ids: list[str] = field(default_factory=list)
    references: list[dict] = field(default_factory=list)
    fixed_in: str | None = None
    matches: list[MatchRow] = field(default_factory=list)


def project_context(
    conn: sqlite3.Connection,
    slug: str,
    *,
    severity_filter: set[str] | None = None,
    ecosystem_filter: set[str] | None = None,
) -> ProjectContext | None:
    row = conn.execute(
        """
        SELECT id, slug, display_name, repo_url, default_branch, last_sha, last_checked
        FROM project WHERE slug = ?
        """,
        (slug,),
    ).fetchone()
    if not row:
        return None
    pid, slug, display, repo_url, branch, last_sha, last_checked = row

    where = ["m.state = 'open'", "m.project_id = ?"]
    params: list = [pid]
    if severity_filter:
        placeholders = ",".join("?" for _ in severity_filter)
        where.append(f"COALESCE(a.severity,'unknown') IN ({placeholders})")
        params.extend(sorted(severity_filter))
    if ecosystem_filter:
        placeholders = ",".join("?" for _ in ecosystem_filter)
        where.append(f"pd.ecosystem IN ({placeholders})")
        params.extend(sorted(ecosystem_filter))

    return ProjectContext(
        project_id=pid,
        slug=slug,
        display_name=display,
        repo_url=repo_url,
        default_branch=branch,
        last_sha=last_sha,
        last_checked=last_checked,
        matches=_fetch_match_rows(
            conn,
            where=" AND ".join(where),
            params=tuple(params),
            order_by=f"{SEVERITY_ORDER_CASE} DESC, a.cvss DESC, m.first_matched DESC",
        ),
    )


def advisory_context(
    conn: sqlite3.Connection,
    source_id: str,
) -> AdvisoryContext | None:
    row = conn.execute(
        """
        SELECT id, source_id, severity, cvss, summary, details, modified,
               cve_ids, references_json
        FROM advisory WHERE source_id = ?
        """,
        (source_id,),
    ).fetchone()
    if not row:
        return None
    aid, sid, sev, cvss, summary, details, modified, cve_json, refs_json = row

    # first fixed_in we see for this advisory (intentionally broad — any package)
    fixed_row = conn.execute(
        "SELECT fixed_in FROM advisory_affects WHERE advisory_id = ? AND fixed_in IS NOT NULL ORDER BY id LIMIT 1",
        (aid,),
    ).fetchone()
    fixed_in = fixed_row[0] if fixed_row else None

    return AdvisoryContext(
        advisory_id=aid,
        source_id=sid,
        severity=sev,
        cvss=cvss,
        summary=summary or "",
        details=details or "",
        modified=modified,
        cve_ids=json.loads(cve_json) if cve_json else [],
        references=json.loads(refs_json) if refs_json else [],
        fixed_in=fixed_in,
        matches=_fetch_match_rows(
            conn,
            where="m.state = 'open' AND a.id = ?",
            params=(aid,),
            order_by="p.slug ASC, m.first_matched DESC",
        ),
    )
