# Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a browser-accessible, read-only dashboard for ckb-advisory-watch that turns the 500+ matches in SQLite into an actionable surface. Per-match and per-advisory share-to-email actions included. Chain-palette (dark navy) data-dense UI, 1366×768 primary viewport, zero JavaScript.

**Architecture:** aiohttp `web.Application` added to the existing asyncio loop in `agent/main.py`. Per-request read-only SQLite connections (WAL lets readers not block writers). Jinja2 templates rendered server-side. `<form POST>` for shares — redirect back with `?sent=1` flash param. Gmail SMTP via `stdlib smtplib.SMTP_SSL`. Spec: `docs/superpowers/specs/2026-04-20-dashboard-design.md`.

**Tech Stack:** Python 3.10+, aiohttp, Jinja2, sqlite3 (stdlib), smtplib (stdlib), pytest + pytest-asyncio.

**Dev host:** driveThree only. Pi (192.168.68.121) is running the current agent and finishing its cold npm ingest. Deploy step is deferred — plan stops at local smoke test.

---

## File Structure

**New files:**
```
agent/dashboard/__init__.py           Empty — package marker
agent/dashboard/queries.py            SQL read-only helpers (dataclasses for rows)
agent/dashboard/share.py              EmailPayload, build_email, send_email
agent/dashboard/server.py             aiohttp Application factory + all route handlers
agent/dashboard/templates/base.html   Top strip + KPI strip + flash banner + {% block content %}
agent/dashboard/templates/index.html  Triage table + exploration sidebar
agent/dashboard/templates/project.html  Project header + filterable match table
agent/dashboard/templates/advisory.html  Advisory detail + affected projects
agent/dashboard/templates/email.html  Multipart HTML body (advisory|match)
agent/dashboard/templates/email.txt   Multipart text body (advisory|match)
agent/dashboard/static/favicon.png    32×32 placeholder (real favicon later)

tests/test_dashboard_queries.py       Unit tests against in-memory SQLite
tests/test_dashboard_share.py         Unit tests for email build + mocked SMTP
tests/test_dashboard_routes.py        aiohttp TestClient — GET pages + POST shares
tests/dashboard_fixtures.py           Seed helpers (project, advisory, match rows)
```

**Modified:**
```
agent/main.py                Add start_dashboard coroutine to asyncio.gather
config.example.toml          Add [dashboard] and [share] sections
requirements.txt             Add aiohttp, jinja2
README.md                    Install/config notes for dashboard
```

---

## Design for frontend tasks

Tasks 6–10 build templates. When you reach them, **invoke the `frontend-design:frontend-design` skill for the HTML+CSS polish pass** — it will help translate the mockups referenced in the spec (`docs/superpowers/specs/2026-04-20-dashboard-design.md` §5 and §7) into production-quality Jinja templates. Full code stubs in each task let you run the tests first; use the skill when polishing markup/CSS to match the spec's palette and typography.

---

## Task 1: Add dependencies and scaffold package

**Files:**
- Modify: `requirements.txt`
- Create: `agent/dashboard/__init__.py`
- Create: `agent/dashboard/templates/` (empty directory)
- Create: `agent/dashboard/static/` (empty directory)

- [ ] **Step 1: Add deps to `requirements.txt`**

Append to `requirements.txt`:

```
aiohttp>=3.10
jinja2>=3.1
```

- [ ] **Step 2: Install**

Run: `. .venv/bin/activate && pip install -r requirements.txt`
Expected: `Successfully installed aiohttp-... jinja2-...` (or already-satisfied lines).

- [ ] **Step 3: Create package directories**

Run:
```bash
mkdir -p agent/dashboard/templates agent/dashboard/static
touch agent/dashboard/__init__.py agent/dashboard/templates/.gitkeep agent/dashboard/static/.gitkeep
```

- [ ] **Step 4: Verify imports + tests still green**

Run: `python -c "import agent.dashboard; import aiohttp; import jinja2; print('ok')"`
Expected: `ok`

Run: `python -m pytest -q`
Expected: `62 passed`

- [ ] **Step 5: Commit**

```bash
git add requirements.txt agent/dashboard/
git commit -m "scaffold(dashboard): add aiohttp+jinja2 deps, create package dirs"
```

---

## Task 2: queries.landing_data — KPIs, triage, sidebars, timestamps

**Files:**
- Create: `agent/dashboard/queries.py`
- Create: `tests/test_dashboard_queries.py`
- Create: `tests/dashboard_fixtures.py`

- [ ] **Step 1: Write the fixture helper**

Create `tests/dashboard_fixtures.py`:

```python
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
        "SELECT id FROM project_dep WHERE project_id=? AND name=? AND version=?",
        (pid, dep_name, dep_version),
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
```

- [ ] **Step 2: Write failing tests for landing_data**

Create `tests/test_dashboard_queries.py`:

```python
"""Tests for dashboard SQL helpers."""
from __future__ import annotations

import time

from agent.dashboard.queries import landing_data, LandingData
from tests.dashboard_fixtures import fresh_db, seed_match


def test_landing_data_empty_db(tmp_path):
    conn = fresh_db(tmp_path)
    data = landing_data(conn)
    assert isinstance(data, LandingData)
    assert data.kpis == {"critical": 0, "high": 0, "medium": 0, "low": 0, "unknown": 0}
    assert data.triage == []
    assert data.top_projects == []
    assert data.top_advisories == []


def test_landing_data_counts_severities(tmp_path):
    conn = fresh_db(tmp_path)
    seed_match(conn, project_slug="a/b", source_id="GHSA-crit", severity="critical", cvss=9.8)
    seed_match(conn, project_slug="a/b", source_id="GHSA-high", severity="high", cvss=7.5, dep_name="other")
    seed_match(conn, project_slug="c/d", source_id="GHSA-med", severity="medium", cvss=5.0, dep_name="pkg3")
    seed_match(conn, project_slug="c/d", source_id="GHSA-null", severity=None, cvss=None, dep_name="pkg4")
    data = landing_data(conn)
    assert data.kpis == {"critical": 1, "high": 1, "medium": 1, "low": 0, "unknown": 1}


def test_landing_data_triage_filters_to_critical_high_and_sorts(tmp_path):
    conn = fresh_db(tmp_path)
    seed_match(conn, project_slug="a/b", source_id="GHSA-low", severity="low", cvss=3.0)
    seed_match(conn, project_slug="a/b", source_id="GHSA-crit", severity="critical", cvss=9.8, dep_name="p2")
    seed_match(conn, project_slug="a/b", source_id="GHSA-high", severity="high", cvss=7.5, dep_name="p3")
    data = landing_data(conn, triage_limit=10)
    # Only critical + high appear; critical first.
    sources = [r.source_id for r in data.triage]
    assert sources == ["GHSA-crit", "GHSA-high"]


def test_landing_data_triage_respects_limit(tmp_path):
    conn = fresh_db(tmp_path)
    for i in range(5):
        seed_match(conn, project_slug=f"a/b{i}", source_id=f"GHSA-crit-{i}",
                   severity="critical", cvss=9.8, dep_name=f"pkg{i}")
    data = landing_data(conn, triage_limit=3)
    assert len(data.triage) == 3


def test_landing_data_top_projects_and_advisories(tmp_path):
    conn = fresh_db(tmp_path)
    for i in range(3):
        seed_match(conn, project_slug="loud/project", source_id=f"GHSA-a-{i}", dep_name=f"p{i}")
    seed_match(conn, project_slug="quiet/project", source_id="GHSA-quiet")
    seed_match(conn, project_slug="other/project", source_id="GHSA-a-0", dep_name="pX")  # shared advisory
    data = landing_data(conn)
    slug_to_count = dict(data.top_projects)
    assert slug_to_count["loud/project"] == 3
    assert slug_to_count["quiet/project"] == 1
    # GHSA-a-0 hits two projects -> #1 top advisory
    assert data.top_advisories[0] == ("GHSA-a-0", 2)
```

- [ ] **Step 3: Run tests — expect ImportError**

Run: `python -m pytest tests/test_dashboard_queries.py -v`
Expected: import failure (module doesn't exist yet).

- [ ] **Step 4: Implement `agent/dashboard/queries.py`**

Create `agent/dashboard/queries.py`:

```python
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
from dataclasses import dataclass, field  # `field` is used by later additions (ProjectContext, AdvisoryContext)


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
               (SELECT aa.fixed_in FROM advisory_affects aa
                  WHERE aa.advisory_id = a.id AND aa.name = pd.name
                  LIMIT 1) AS fixed_in,
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
    last_osv = row[0] if row else None
    row = conn.execute("SELECT MAX(last_checked) FROM project").fetchone()
    last_walk = row[0] if row else None
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
```

- [ ] **Step 5: Run tests — expect pass**

Run: `python -m pytest tests/test_dashboard_queries.py -v`
Expected: all tests pass.

Also run the full suite to confirm nothing regressed:
Run: `python -m pytest -q`
Expected: `67 passed` (62 prior + 5 new).

- [ ] **Step 6: Commit**

```bash
git add agent/dashboard/queries.py tests/test_dashboard_queries.py tests/dashboard_fixtures.py
git commit -m "feat(dashboard): queries.landing_data — KPIs, triage, sidebars, timestamps"
```

---

## Task 3: queries.project_context + queries.advisory_context

**Files:**
- Modify: `agent/dashboard/queries.py`
- Modify: `tests/test_dashboard_queries.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_dashboard_queries.py`:

```python
from agent.dashboard.queries import (
    project_context, advisory_context, ProjectContext, AdvisoryContext,
)


def test_project_context_returns_project_and_matches(tmp_path):
    conn = fresh_db(tmp_path)
    seed_match(conn, project_slug="x/y", display_name="X/Y", source_id="GHSA-1")
    seed_match(conn, project_slug="x/y", source_id="GHSA-2", dep_name="pkg2")
    seed_match(conn, project_slug="other/one", source_id="GHSA-3")

    ctx = project_context(conn, "x/y")
    assert ctx is not None
    assert ctx.slug == "x/y"
    assert ctx.display_name == "X/Y"
    assert ctx.last_sha == "abc123"
    assert len(ctx.matches) == 2
    assert {m.source_id for m in ctx.matches} == {"GHSA-1", "GHSA-2"}


def test_project_context_returns_none_for_unknown_slug(tmp_path):
    conn = fresh_db(tmp_path)
    assert project_context(conn, "nonexistent/repo") is None


def test_project_context_filters_by_severity_param(tmp_path):
    conn = fresh_db(tmp_path)
    seed_match(conn, project_slug="x/y", source_id="GHSA-c", severity="critical", cvss=9.8)
    seed_match(conn, project_slug="x/y", source_id="GHSA-l", severity="low", cvss=3.0, dep_name="p2")
    ctx = project_context(conn, "x/y", severity_filter={"critical"})
    assert len(ctx.matches) == 1
    assert ctx.matches[0].source_id == "GHSA-c"


def test_advisory_context_returns_advisory_and_affected(tmp_path):
    conn = fresh_db(tmp_path)
    seed_match(conn, project_slug="a/one", source_id="GHSA-shared", dep_name="lib", dep_version="1.0.0")
    seed_match(conn, project_slug="a/two", source_id="GHSA-shared", dep_name="lib", dep_version="1.0.0")
    ctx = advisory_context(conn, "GHSA-shared")
    assert ctx is not None
    assert ctx.source_id == "GHSA-shared"
    assert ctx.severity == "high"
    assert len(ctx.matches) == 2
    slugs = sorted(m.project_slug for m in ctx.matches)
    assert slugs == ["a/one", "a/two"]


def test_advisory_context_returns_none_for_unknown_id(tmp_path):
    conn = fresh_db(tmp_path)
    assert advisory_context(conn, "GHSA-not-real") is None


def test_advisory_context_includes_references(tmp_path):
    conn = fresh_db(tmp_path)
    seed_match(conn, source_id="GHSA-refs")
    ctx = advisory_context(conn, "GHSA-refs")
    assert any("example.com" in ref["url"] for ref in ctx.references)
```

- [ ] **Step 2: Run tests — expect ImportError**

Run: `python -m pytest tests/test_dashboard_queries.py -v`
Expected: ImportError on `project_context`.

- [ ] **Step 3: Append implementations to `agent/dashboard/queries.py`**

First, add `import json` at the top of `agent/dashboard/queries.py` next to the existing `import sqlite3` (standard library imports grouped together). Then append these class definitions and functions after `landing_data`:

```python
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

    match_rows = conn.execute(
        f"""
        SELECT m.id, a.id, a.source_id, a.severity, a.cvss, a.summary,
               p.slug, p.display_name, pd.ecosystem, pd.name, pd.version,
               (SELECT aa.fixed_in FROM advisory_affects aa
                  WHERE aa.advisory_id = a.id AND aa.name = pd.name LIMIT 1),
               m.first_matched
        FROM match m
        JOIN advisory a ON a.id = m.advisory_id
        JOIN project p ON p.id = m.project_id
        JOIN project_dep pd ON pd.id = m.project_dep_id
        WHERE {' AND '.join(where)}
        ORDER BY {SEVERITY_ORDER_CASE} DESC, a.cvss DESC, m.first_matched DESC
        """,
        tuple(params),
    ).fetchall()

    return ProjectContext(
        project_id=pid,
        slug=slug,
        display_name=display,
        repo_url=repo_url,
        default_branch=branch,
        last_sha=last_sha,
        last_checked=last_checked,
        matches=[MatchRow(*r) for r in match_rows],
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

    # first fixed_in we see for this advisory
    fixed_row = conn.execute(
        "SELECT fixed_in FROM advisory_affects WHERE advisory_id = ? AND fixed_in IS NOT NULL LIMIT 1",
        (aid,),
    ).fetchone()
    fixed_in = fixed_row[0] if fixed_row else None

    match_rows = conn.execute(
        f"""
        SELECT m.id, a.id, a.source_id, a.severity, a.cvss, a.summary,
               p.slug, p.display_name, pd.ecosystem, pd.name, pd.version,
               (SELECT aa.fixed_in FROM advisory_affects aa
                  WHERE aa.advisory_id = a.id AND aa.name = pd.name LIMIT 1),
               m.first_matched
        FROM match m
        JOIN advisory a ON a.id = m.advisory_id
        JOIN project p ON p.id = m.project_id
        JOIN project_dep pd ON pd.id = m.project_dep_id
        WHERE m.state = 'open' AND a.id = ?
        ORDER BY p.slug ASC
        """,
        (aid,),
    ).fetchall()

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
        matches=[MatchRow(*r) for r in match_rows],
    )
```

- [ ] **Step 4: Run tests — expect pass**

Run: `python -m pytest tests/test_dashboard_queries.py -v`
Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add agent/dashboard/queries.py tests/test_dashboard_queries.py
git commit -m "feat(dashboard): project_context + advisory_context queries"
```

---

## Task 4: share.build_email — advisory + match bodies

**Files:**
- Create: `agent/dashboard/share.py`
- Create: `tests/test_dashboard_share.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_dashboard_share.py`:

```python
"""Share email composition + SMTP tests (SMTP mocked)."""
from __future__ import annotations

import smtplib
from unittest.mock import patch, MagicMock

import pytest

from agent.dashboard.queries import AdvisoryContext, MatchRow
from agent.dashboard.share import (
    EmailPayload, ShareConfig, build_advisory_email, build_match_email, send_email,
)


def _advisory_ctx():
    return AdvisoryContext(
        advisory_id=1,
        source_id="GHSA-6p3c-v8vc-c244",
        severity="critical",
        cvss=9.8,
        summary="Summary of the advisory",
        details="Full details here",
        modified=1712000000,
        cve_ids=["CVE-2022-39303"],
        references=[{"type": "ADVISORY", "url": "https://example.com/ghsa"}],
        fixed_in="0.7.3",
        matches=[
            MatchRow(1, 1, "GHSA-6p3c-v8vc-c244", "critical", 9.8, "Summary",
                     "Magickbase/force-bridge", "force-bridge",
                     "crates.io", "molecule", "0.6.0", "0.7.3", 1712099999),
            MatchRow(2, 1, "GHSA-6p3c-v8vc-c244", "critical", 9.8, "Summary",
                     "Magickbase/force-bridge", "force-bridge",
                     "crates.io", "molecule", "0.6.1", "0.7.3", 1712099999),
        ],
    )


def _config():
    return ShareConfig(
        recipient="phill@example.com",
        sender="phill@example.com",
        smtp_host="smtp.gmail.com",
        smtp_port=465,
        smtp_user="phill@example.com",
        smtp_password="app-password",
        dashboard_base_url="http://pi.local:8080",
    )


def test_advisory_email_subject_format():
    payload = build_advisory_email(_advisory_ctx(), _config())
    assert payload.subject == "[CKB advisory] GHSA-6p3c-v8vc-c244 — molecule < 0.7.3 (2 matches)"


def test_advisory_email_recipients():
    payload = build_advisory_email(_advisory_ctx(), _config())
    assert payload.to == "phill@example.com"
    assert payload.sender == "phill@example.com"


def test_advisory_email_html_includes_all_projects():
    payload = build_advisory_email(_advisory_ctx(), _config())
    for version in ("0.6.0", "0.6.1"):
        assert f"molecule@{version}" in payload.html_body
    assert "Magickbase/force-bridge" in payload.html_body


def test_advisory_email_html_includes_dashboard_link():
    payload = build_advisory_email(_advisory_ctx(), _config())
    assert "http://pi.local:8080/a/GHSA-6p3c-v8vc-c244" in payload.html_body


def test_advisory_email_text_body_parallels_html():
    payload = build_advisory_email(_advisory_ctx(), _config())
    assert "GHSA-6p3c-v8vc-c244" in payload.text_body
    assert "molecule@0.6.0" in payload.text_body
    assert "CRITICAL" in payload.text_body.upper()


def test_match_email_subject_format():
    ctx = _advisory_ctx()
    payload = build_match_email(ctx.matches[0], ctx, _config())
    assert payload.subject == "[CKB advisory] GHSA-6p3c-v8vc-c244 — molecule@0.6.0 in Magickbase/force-bridge"


def test_match_email_scopes_to_single_row():
    ctx = _advisory_ctx()
    payload = build_match_email(ctx.matches[0], ctx, _config())
    # only the specific affected version appears
    assert "molecule@0.6.0" in payload.html_body
    assert "molecule@0.6.1" not in payload.html_body


def test_send_email_happy_path():
    payload = EmailPayload(
        subject="subj", sender="a@x", to="b@y",
        text_body="t", html_body="<p>h</p>",
    )
    mock_smtp = MagicMock()
    with patch("smtplib.SMTP_SSL", return_value=mock_smtp.__enter__.return_value) as smtp_ctor:
        send_email(payload, _config())
        smtp_ctor.assert_called_once_with("smtp.gmail.com", 465, timeout=30)


def test_send_email_login_failure_raises():
    payload = EmailPayload(subject="s", sender="a", to="b", text_body="t", html_body="<p>h</p>")
    mock_smtp = MagicMock()
    mock_smtp.__enter__.return_value.login.side_effect = smtplib.SMTPAuthenticationError(535, b"auth")
    with patch("smtplib.SMTP_SSL", return_value=mock_smtp.__enter__.return_value):
        with pytest.raises(smtplib.SMTPAuthenticationError):
            send_email(payload, _config())
```

- [ ] **Step 2: Run tests — expect ImportError**

Run: `python -m pytest tests/test_dashboard_share.py -v`
Expected: ImportError on `agent.dashboard.share`.

- [ ] **Step 3: Implement `agent/dashboard/share.py`**

Create `agent/dashboard/share.py`:

```python
"""Email composition + SMTP dispatch for the share action.

Two builders — per-advisory (lists every affected project) and per-match
(scopes to one row). Both return an EmailPayload the caller hands to
send_email. send_email uses stdlib smtplib.SMTP_SSL for Gmail.

The HTML and text bodies are Jinja2 templates in templates/; they share a
single context dict so they can't drift."""
from __future__ import annotations

import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from agent.dashboard.queries import AdvisoryContext, MatchRow

TEMPLATES_DIR = Path(__file__).parent / "templates"
_env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


@dataclass(frozen=True)
class ShareConfig:
    recipient: str
    sender: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    dashboard_base_url: str


@dataclass(frozen=True)
class EmailPayload:
    subject: str
    sender: str
    to: str
    text_body: str
    html_body: str


def _render(template_name: str, **ctx: Any) -> str:
    return _env.get_template(template_name).render(**ctx)


def build_advisory_email(
    advisory: AdvisoryContext,
    config: ShareConfig,
) -> EmailPayload:
    # Identify primary affected package for the subject line — use the most
    # common dep name across matches.
    if advisory.matches:
        names = [m.dep_name for m in advisory.matches]
        primary = max(set(names), key=names.count)
    else:
        primary = "unknown"
    fix_part = f" < {advisory.fixed_in}" if advisory.fixed_in else ""
    subject = (
        f"[CKB advisory] {advisory.source_id} — {primary}{fix_part} "
        f"({len(advisory.matches)} matches)"
    )

    ctx = {
        "kind": "advisory",
        "advisory": advisory,
        "config": config,
        "dashboard_url": f"{config.dashboard_base_url}/a/{advisory.source_id}",
    }
    return EmailPayload(
        subject=subject,
        sender=config.sender,
        to=config.recipient,
        text_body=_render("email.txt", **ctx),
        html_body=_render("email.html", **ctx),
    )


def build_match_email(
    match: MatchRow,
    advisory: AdvisoryContext,
    config: ShareConfig,
) -> EmailPayload:
    subject = (
        f"[CKB advisory] {match.source_id} — {match.dep_name}@{match.dep_version} "
        f"in {match.project_slug}"
    )
    ctx = {
        "kind": "match",
        "match": match,
        "advisory": advisory,
        "config": config,
        "dashboard_url": f"{config.dashboard_base_url}/a/{match.source_id}",
    }
    return EmailPayload(
        subject=subject,
        sender=config.sender,
        to=config.recipient,
        text_body=_render("email.txt", **ctx),
        html_body=_render("email.html", **ctx),
    )


def send_email(payload: EmailPayload, config: ShareConfig) -> None:
    msg = EmailMessage()
    msg["Subject"] = payload.subject
    msg["From"] = payload.sender
    msg["To"] = payload.to
    msg.set_content(payload.text_body)
    msg.add_alternative(payload.html_body, subtype="html")

    with smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, timeout=30) as s:
        s.login(config.smtp_user, config.smtp_password)
        s.send_message(msg)
```

- [ ] **Step 4: Create the email templates (minimum to satisfy build tests)**

Create `agent/dashboard/templates/email.txt`:

```jinja
{% if kind == 'advisory' -%}
Severity: {{ (advisory.severity or 'unknown')|upper }}{% if advisory.cvss %} · CVSS {{ '%.1f'|format(advisory.cvss) }}{% endif %}

Summary: {{ advisory.summary }}

Affects {{ advisory.matches|length }} project(s) in tracked stack:
{% for m in advisory.matches -%}
  - {{ m.project_slug }} — {{ m.dep_name }}@{{ m.dep_version }}
{% endfor %}
{% if advisory.fixed_in -%}
Fix: upgrade to {{ advisory.matches[0].dep_name if advisory.matches else 'package' }} >= {{ advisory.fixed_in }}
{% endif %}
Dashboard: {{ dashboard_url }}
{% else -%}
Severity: {{ (advisory.severity or 'unknown')|upper }}{% if advisory.cvss %} · CVSS {{ '%.1f'|format(advisory.cvss) }}{% endif %}

Summary: {{ advisory.summary }}

Affects: {{ match.project_slug }} — {{ match.dep_name }}@{{ match.dep_version }}
{% if match.fixed_in %}Fix: upgrade to {{ match.dep_name }} >= {{ match.fixed_in }}{% endif %}

Dashboard: {{ dashboard_url }}
{% endif %}
```

Create `agent/dashboard/templates/email.html`:

```jinja
<!doctype html>
<html><body style="font-family:Arial,sans-serif;color:#202124;font-size:13px;line-height:1.55">
<p><strong>Severity:</strong> {{ (advisory.severity or 'unknown')|upper }}{% if advisory.cvss %} · CVSS {{ '%.1f'|format(advisory.cvss) }}{% endif %}</p>
<p><strong>Summary:</strong> {{ advisory.summary }}</p>
{% if kind == 'advisory' %}
<p><strong>Affects {{ advisory.matches|length }} project(s) in tracked stack:</strong></p>
<ul>
{% for m in advisory.matches %}
  <li>{{ m.project_slug }} — <code>{{ m.dep_name }}@{{ m.dep_version }}</code></li>
{% endfor %}
</ul>
{% if advisory.fixed_in %}<p><strong>Fix:</strong> upgrade to <code>{{ advisory.fixed_in }}</code></p>{% endif %}
{% else %}
<p><strong>Affects:</strong> {{ match.project_slug }} — <code>{{ match.dep_name }}@{{ match.dep_version }}</code></p>
{% if match.fixed_in %}<p><strong>Fix:</strong> upgrade to <code>{{ match.fixed_in }}</code></p>{% endif %}
{% endif %}
<p><strong>Dashboard:</strong> <a href="{{ dashboard_url }}">{{ dashboard_url }}</a></p>
<p style="color:#5f6368;font-size:11px;border-top:1px solid #e8eaed;padding-top:10px">Reported by ckb-advisory-watch</p>
</body></html>
```

- [ ] **Step 5: Run tests — expect pass**

Run: `python -m pytest tests/test_dashboard_share.py -v`
Expected: all tests pass.

Run full suite: `python -m pytest -q` → `78 passed`.

- [ ] **Step 6: Commit**

```bash
git add agent/dashboard/share.py agent/dashboard/templates/email.txt agent/dashboard/templates/email.html tests/test_dashboard_share.py
git commit -m "feat(dashboard): share.build_advisory_email + build_match_email + send_email"
```

---

## Task 5: base.html template + CSS design tokens

> **For the visual work in this task, invoke `frontend-design:frontend-design` skill** — pass it the spec (§5.1, §7) and the brainstorm mockups under `.superpowers/brainstorm/*/content/layout-landing.html` as references for the chain palette, typography, and layout structure.

**Files:**
- Create: `agent/dashboard/templates/base.html`
- Create: `agent/dashboard/static/favicon.png` (32×32 placeholder)

- [ ] **Step 1: Create `agent/dashboard/templates/base.html`**

The canvas every page extends. Inlined CSS (no external stylesheet — one round-trip).

```jinja
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{% block title %}ckb-advisory-watch{% endblock %}</title>
  <link rel="icon" type="image/png" href="/static/favicon.png">
  <style>
    :root {
      --bg: #0f1418;
      --bg-alt: #141c23;
      --bg-row: #141820;
      --border: #1e2a35;
      --text: #e6edf3;
      --text-muted: #a5b3cc;
      --text-dim: #6a7a88;
      --link: #6fb0ff;
      --accent: #4285ff;
      --sev-crit: #ff4d5b; --sev-crit-bg: #2a1319;
      --sev-high: #ff8a4d; --sev-high-bg: #2a1e13;
      --sev-med:  #f0c94d; --sev-med-bg:  #2a2513;
      --sev-low:  #4dc67b; --sev-low-bg:  #132a1b;
      --sev-unk:  #7b8fb3; --sev-unk-bg:  #1b1e2a;
    }
    * { box-sizing: border-box; }
    html, body { margin:0; padding:0; background: var(--bg); color: var(--text); }
    body {
      font-family: Inter, system-ui, -apple-system, "Segoe UI", sans-serif;
      font-variant-numeric: tabular-nums;
      font-size: 13px;
      line-height: 1.5;
      min-height: 100vh;
    }
    .mono { font-family: "JetBrains Mono", "SF Mono", Menlo, monospace; }
    a { color: var(--link); text-decoration: none; }
    a:hover { text-decoration: underline; }

    /* ---- TOP STRIP ---- */
    .topstrip { padding: 14px 20px; border-bottom: 1px solid var(--border);
                display: flex; align-items: center; gap: 12px; background: var(--bg-alt); }
    .topstrip .logo { width: 32px; height: 32px; border-radius: 6px;
                      background: linear-gradient(135deg,#d84565,#4285ff); flex-shrink: 0; }
    .topstrip .name { font-weight: 600; font-size: 14px; letter-spacing: -0.01em; }
    .topstrip .meta { font-family: "JetBrains Mono", monospace; font-size: 11px;
                      color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.05em; }
    .topstrip .timestamps { margin-left: auto; font-family: "JetBrains Mono", monospace;
                            font-size: 11px; color: var(--text-dim); text-align: right; line-height: 1.5; }
    .topstrip .timestamps span { color: var(--text-muted); }

    /* ---- KPI STRIP ---- */
    .kpis { padding: 12px 20px; display: grid; grid-template-columns: repeat(5,1fr);
            gap: 10px; background: var(--bg); border-bottom: 1px solid var(--border); }
    .kpi { padding: 10px 12px; border-left: 3px solid; }
    .kpi .n { font: 700 24px "JetBrains Mono", monospace; line-height: 1; }
    .kpi .l { font: 10px "JetBrains Mono", monospace; text-transform: uppercase;
              letter-spacing: 0.08em; margin-top: 4px; }
    .kpi.critical { border-color: var(--sev-crit); background: var(--sev-crit-bg); }
    .kpi.critical .n { color: #ffd6db; } .kpi.critical .l { color: var(--sev-crit); }
    .kpi.high { border-color: var(--sev-high); background: var(--sev-high-bg); }
    .kpi.high .n { color: #ffe2d0; } .kpi.high .l { color: var(--sev-high); }
    .kpi.medium { border-color: var(--sev-med); background: var(--sev-med-bg); }
    .kpi.medium .n { color: #f5df9f; } .kpi.medium .l { color: var(--sev-med); }
    .kpi.low { border-color: var(--sev-low); background: var(--sev-low-bg); }
    .kpi.low .n { color: #cff1d8; } .kpi.low .l { color: var(--sev-low); }
    .kpi.unknown { border-color: var(--sev-unk); background: var(--sev-unk-bg); }
    .kpi.unknown .n { color: #d8dfee; } .kpi.unknown .l { color: var(--sev-unk); }

    /* ---- FLASH BANNER ---- */
    .flash { padding: 10px 20px; font: 12px "JetBrains Mono", monospace; }
    .flash.ok { background: var(--sev-low-bg); color: #cff1d8; border-bottom: 1px solid var(--sev-low); }
    .flash.err { background: var(--sev-crit-bg); color: #ffd6db; border-bottom: 1px solid var(--sev-crit); }

    /* ---- TABLES ---- */
    table.data { width: 100%; border-collapse: collapse; font: 12px "JetBrains Mono", monospace;
                 color: var(--text-muted); }
    table.data thead th { padding: 7px 10px; text-align: left; color: var(--text-dim);
                          text-transform: uppercase; font-size: 10px; letter-spacing: 0.05em;
                          background: var(--bg-alt); border-bottom: 1px solid var(--border); }
    table.data th.num, table.data td.num { text-align: right; }
    table.data tbody tr:nth-child(even) { background: var(--bg-row); }
    table.data td { padding: 5px 10px; }
    table.data td.proj, table.data td.sum { color: var(--text); }

    /* ---- SEVERITY PILLS ---- */
    .pill { display: inline-block; padding: 1px 6px; border-radius: 2px;
            font: 700 10px "JetBrains Mono", monospace; letter-spacing: 0.05em; }
    .pill.critical { background: var(--sev-crit); color: #1a0608; }
    .pill.high { background: var(--sev-high); color: #1a0c04; }
    .pill.medium { background: var(--sev-med); color: #1a1004; }
    .pill.low { background: var(--sev-low); color: #041a0a; }
    .pill.unknown { background: var(--sev-unk); color: #06081a; }

    /* ---- BUTTON / FORM ---- */
    button.share { background: var(--accent); color: #fff; border: 0; padding: 6px 14px;
                   border-radius: 3px; font: 600 12px Inter, system-ui; cursor: pointer; }
    button.share.sm { padding: 2px 8px; font-size: 11px; }

    /* ---- LAYOUT ---- */
    main { padding: 0; }
    .page { padding: 16px 20px; }
    h1, h2, h3 { margin: 0 0 8px 0; color: var(--text); }
    h1 { font: 600 18px Inter; letter-spacing: -0.01em; }
    code { background: var(--bg-alt); padding: 1px 4px; border-radius: 2px; font-size: 11.5px; }
  </style>
</head>
<body>

<div class="topstrip">
  <div class="logo"></div>
  <div>
    <div class="name">ckb-advisory-watch</div>
    <div class="meta">v0.4 · {{ hostname }} · <span style="color:#4dc67b">●</span> live</div>
  </div>
  <div class="timestamps">
    <div>osv ingest: <span>{{ last_osv_ingest_label }}</span></div>
    <div>walker: <span>{{ last_walk_label }}</span></div>
  </div>
</div>

<div class="kpis">
  <div class="kpi critical"><div class="n">{{ kpis.critical }}</div><div class="l">critical</div></div>
  <div class="kpi high"><div class="n">{{ kpis.high }}</div><div class="l">high</div></div>
  <div class="kpi medium"><div class="n">{{ kpis.medium }}</div><div class="l">medium</div></div>
  <div class="kpi low"><div class="n">{{ kpis.low }}</div><div class="l">low</div></div>
  <div class="kpi unknown"><div class="n">{{ kpis.unknown }}</div><div class="l">unknown</div></div>
</div>

{% if flash %}
<div class="flash {{ flash.level }}">{{ flash.message }}</div>
{% endif %}

<main>{% block content %}{% endblock %}</main>

</body>
</html>
```

- [ ] **Step 2: Add a 32×32 favicon placeholder**

Run:
```bash
python -c "
import struct, zlib, pathlib
# tiny 32x32 PNG, solid #6fb0ff
w=h=32
raw = b''.join(b'\\x00' + b'\\x6f\\xb0\\xff\\xff'*w for _ in range(h))
def chunk(t,d):
    import zlib as z; c=z.crc32(t+d)&0xffffffff
    return struct.pack('>I',len(d))+t+d+struct.pack('>I',c)
ihdr = struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0)
idat = zlib.compress(raw, 9)
png = b'\\x89PNG\\r\\n\\x1a\\n' + chunk(b'IHDR', ihdr) + chunk(b'IDAT', idat) + chunk(b'IEND', b'')
pathlib.Path('agent/dashboard/static/favicon.png').write_bytes(png)
print('wrote favicon', len(png), 'bytes')
"
```

Expected: `wrote favicon <N> bytes`

- [ ] **Step 3: Verify the template renders via Jinja directly**

Run:
```bash
python -c "
from jinja2 import Environment, FileSystemLoader
env = Environment(loader=FileSystemLoader('agent/dashboard/templates'))
# Sanity: derive a child that extends base and confirm rendering doesn't crash
t = env.from_string('{% extends \"base.html\" %}{% block content %}<p>ok</p>{% endblock %}')
html = t.render(
    kpis={'critical':1,'high':0,'medium':0,'low':0,'unknown':0},
    hostname='driveThree', last_osv_ingest_label='just now', last_walk_label='just now',
    flash=None,
)
print('rendered', len(html), 'bytes;', 'KPI strip included:', 'critical' in html)
"
```

Expected: `rendered <N> bytes; KPI strip included: True`

- [ ] **Step 4: Run full test suite (unchanged)**

Run: `python -m pytest -q`
Expected: `78 passed` — no regressions.

- [ ] **Step 5: Commit**

```bash
git add agent/dashboard/templates/base.html agent/dashboard/static/favicon.png
git commit -m "feat(dashboard): base.html with chain palette tokens + placeholder favicon"
```

---

## Task 6: server.py — Application factory, landing route, route tests

> **For the visual work in this task, invoke `frontend-design:frontend-design`** for the triage table + sidebar polish in `index.html` against the landing mockup in `.superpowers/brainstorm/.../layout-landing.html`.

**Files:**
- Create: `agent/dashboard/server.py`
- Create: `agent/dashboard/templates/index.html`
- Create: `tests/test_dashboard_routes.py`

- [ ] **Step 1: Write a failing route test**

Create `tests/test_dashboard_routes.py`:

```python
"""aiohttp TestClient-based route tests."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from agent.dashboard.server import build_app
from agent.dashboard.share import ShareConfig
from tests.dashboard_fixtures import fresh_db, seed_match


@pytest.fixture
def share_config():
    return ShareConfig(
        recipient="p@x",
        sender="p@x",
        smtp_host="smtp.gmail.com",
        smtp_port=465,
        smtp_user="p@x",
        smtp_password="pw",
        dashboard_base_url="http://test",
    )


async def _client(tmp_path, share_config):
    # the connection factory hands out read-only connections
    db_path = tmp_path / "state.db"
    conn = fresh_db(tmp_path)  # creates file + schema, returns writer connection
    # seed before wrapping so the handlers see data
    seed_match(conn, project_slug="a/b", source_id="GHSA-crit",
               severity="critical", cvss=9.8)
    conn.close()

    import sqlite3
    def conn_factory() -> sqlite3.Connection:
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    app = build_app(conn_factory=conn_factory, share_config=share_config,
                    hostname="test-host")
    return TestClient(TestServer(app))


async def test_index_returns_200_with_kpi_and_triage(tmp_path, share_config):
    async with await _client(tmp_path, share_config) as client:
        resp = await client.get("/")
        assert resp.status == 200
        body = await resp.text()
    assert "GHSA-crit" in body
    # KPI strip shows "1" for critical bucket
    assert ">1<" in body
    assert "critical" in body.lower()


async def test_index_serves_html_content_type(tmp_path, share_config):
    async with await _client(tmp_path, share_config) as client:
        resp = await client.get("/")
    assert resp.headers["content-type"].startswith("text/html")


async def test_favicon_served(tmp_path, share_config):
    async with await _client(tmp_path, share_config) as client:
        resp = await client.get("/static/favicon.png")
        assert resp.status == 200
        assert resp.headers["content-type"] == "image/png"
```

- [ ] **Step 2: Run tests — expect ImportError**

Run: `python -m pytest tests/test_dashboard_routes.py -v`
Expected: ImportError on `agent.dashboard.server`.

- [ ] **Step 3: Implement `agent/dashboard/server.py`**

Create `agent/dashboard/server.py`:

```python
"""aiohttp Application factory + route handlers for the dashboard.

build_app returns an aiohttp.web.Application. The caller is responsible
for running it (either via AppRunner in the agent's asyncio loop or via
aiohttp.web.run_app for tests). Handlers open their own per-request
read-only SQLite connections via the conn_factory passed at build time,
so the agent's writer connection is never shared across the event loop."""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Callable

from aiohttp import web
from jinja2 import Environment, FileSystemLoader, select_autoescape

from agent.dashboard import queries, share

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


def _make_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _ago(ts: int | None, now: int | None = None) -> str:
    if ts is None:
        return "never"
    delta = (now or int(time.time())) - ts
    if delta < 60:    return f"{delta}s ago"
    if delta < 3600:  return f"{delta // 60}m ago"
    if delta < 86400: return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def _flash_from_query(request: web.Request) -> dict | None:
    q = request.query
    if q.get("sent") == "1":
        return {"level": "ok", "message": "✓ share email sent."}
    err = q.get("sent_error")
    if err:
        return {"level": "err", "message": f"✗ share failed: {err}"}
    return None


def _base_context(request: web.Request) -> dict:
    conn = request.app["conn_factory"]()
    try:
        data = queries.landing_data(conn)
    finally:
        conn.close()
    return {
        "kpis": data.kpis,
        "hostname": request.app["hostname"],
        "last_osv_ingest_label": _ago(data.last_osv_ingest),
        "last_walk_label": _ago(data.last_github_walk),
        "flash": _flash_from_query(request),
        "_landing_data": data,  # convenience for handlers that want it
    }


async def index_view(request: web.Request) -> web.Response:
    ctx = _base_context(request)
    data = ctx.pop("_landing_data")
    template = request.app["jinja"].get_template("index.html")
    html = template.render(
        triage=data.triage,
        top_projects=data.top_projects,
        top_advisories=data.top_advisories,
        **ctx,
    )
    return web.Response(text=html, content_type="text/html")


def build_app(
    *,
    conn_factory: Callable[[], sqlite3.Connection],
    share_config: share.ShareConfig,
    hostname: str = "",
) -> web.Application:
    app = web.Application()
    app["conn_factory"] = conn_factory
    app["share_config"] = share_config
    app["hostname"] = hostname or "dashboard"
    app["jinja"] = _make_env()

    app.router.add_get("/", index_view)
    app.router.add_static("/static/", STATIC_DIR, follow_symlinks=False)
    return app
```

- [ ] **Step 4: Create minimal `index.html` (extends base, triage table + sidebar)**

Create `agent/dashboard/templates/index.html`:

```jinja
{% extends "base.html" %}
{% block content %}
<div style="display:grid;grid-template-columns:1fr 260px;min-height:480px">

  <section style="border-right:1px solid var(--border)">
    <div style="padding:10px 20px;background:var(--bg-alt);display:flex;align-items:baseline;gap:12px;border-bottom:1px solid var(--border)">
      <span style="font-weight:600;font-size:12px;letter-spacing:0.02em">OPEN MATCHES · CRITICAL + HIGH</span>
      <span class="mono" style="font-size:11px;color:var(--text-dim)">showing {{ triage|length }}</span>
    </div>
    <table class="data">
      <thead><tr>
        <th style="width:68px">sev</th>
        <th class="num" style="width:50px">cvss</th>
        <th>advisory</th>
        <th>project</th>
        <th>affected</th>
        <th style="width:90px">fixed in</th>
        <th class="num" style="width:64px">seen</th>
        <th style="width:56px;text-align:center">share</th>
      </tr></thead>
      <tbody>
        {% for m in triage %}
        <tr>
          <td><span class="pill {{ m.severity or 'unknown' }}">{{ (m.severity or 'unk')[:4]|upper }}</span></td>
          <td class="num" style="color:var(--text)">{% if m.cvss %}{{ '%.1f'|format(m.cvss) }}{% else %}—{% endif %}</td>
          <td><a href="/a/{{ m.source_id }}">{{ m.source_id }}</a></td>
          <td class="proj"><a href="/p/{{ m.project_slug }}">{{ m.project_slug }}</a></td>
          <td>{{ m.dep_name }}@{{ m.dep_version }}</td>
          <td style="color:var(--sev-low)">{{ m.fixed_in or '—' }}</td>
          <td class="num" style="color:var(--text-dim)">{{ ago_label(m.first_matched) if ago_label is defined else m.first_matched }}</td>
          <td style="text-align:center">
            <form method="POST" action="/share/match/{{ m.match_id }}" style="margin:0">
              <button type="submit" class="share sm">📤</button>
            </form>
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </section>

  <aside style="padding:16px">
    <div style="font-weight:600;font-size:10px;text-transform:uppercase;letter-spacing:0.08em;color:var(--text-dim);margin-bottom:8px">PROJECTS BY MATCHES</div>
    <div class="mono">
      {% for slug, n in top_projects %}
      <div style="display:flex;justify-content:space-between;line-height:1.9">
        <a href="/p/{{ slug }}">{{ slug }}</a><span style="color:var(--text-dim)">{{ n }}</span>
      </div>
      {% endfor %}
    </div>

    <div style="font-weight:600;font-size:10px;text-transform:uppercase;letter-spacing:0.08em;color:var(--text-dim);margin:22px 0 8px">TOP ADVISORIES</div>
    <div class="mono">
      {% for sid, n in top_advisories %}
      <div style="display:flex;justify-content:space-between;line-height:1.9">
        <a href="/a/{{ sid }}">{{ sid }}</a><span style="color:var(--text-dim)">{{ n }}</span>
      </div>
      {% endfor %}
    </div>
  </aside>
</div>
{% endblock %}
```

- [ ] **Step 5: Run tests — expect pass**

Run: `python -m pytest tests/test_dashboard_routes.py -v`
Expected: all tests pass.

Run full: `python -m pytest -q` → `81 passed`.

- [ ] **Step 6: Commit**

```bash
git add agent/dashboard/server.py agent/dashboard/templates/index.html tests/test_dashboard_routes.py
git commit -m "feat(dashboard): aiohttp app factory + landing route + favicon serving"
```

---

## Task 7: Per-project page

> **Invoke `frontend-design:frontend-design`** for project.html polish.

**Files:**
- Modify: `agent/dashboard/server.py`
- Create: `agent/dashboard/templates/project.html`
- Modify: `tests/test_dashboard_routes.py`

- [ ] **Step 1: Append failing tests**

Append to `tests/test_dashboard_routes.py`:

```python
async def test_project_page_shows_matches(tmp_path, share_config):
    async with await _client(tmp_path, share_config) as client:
        resp = await client.get("/p/a/b")
        assert resp.status == 200
        body = await resp.text()
    assert "GHSA-crit" in body
    assert "libgit2-sys" in body


async def test_project_page_404_for_unknown(tmp_path, share_config):
    async with await _client(tmp_path, share_config) as client:
        resp = await client.get("/p/does/notexist")
    assert resp.status == 404


async def test_project_page_filters_by_severity_query_param(tmp_path, share_config):
    # _client creates the DB + applies schema + seeds the GHSA-crit match.
    # We add a second (low-severity) match after that, via a fresh connection
    # to the same file, then exercise the filter.
    async with await _client(tmp_path, share_config) as client:
        import sqlite3
        from tests.dashboard_fixtures import seed_match
        conn = sqlite3.connect(str(tmp_path / "state.db"))
        seed_match(conn, project_slug="a/b", source_id="GHSA-low-extra",
                   severity="low", cvss=3.0, dep_name="other-pkg")
        conn.close()

        r1 = await client.get("/p/a/b")
        b1 = await r1.text()
        r2 = await client.get("/p/a/b?severity=critical")
        b2 = await r2.text()
    assert "GHSA-crit" in b1 and "GHSA-low-extra" in b1
    assert "GHSA-crit" in b2 and "GHSA-low-extra" not in b2
```

Note: the third test uses a second `seed_match` on the existing db file — that's fine since `seed_match` is idempotent for its project slug.

- [ ] **Step 2: Add `project_view` handler + route**

In `agent/dashboard/server.py`, replace the `build_app` function's body-end so the router gets the new route. Add this handler above `build_app`:

```python
async def project_view(request: web.Request) -> web.Response:
    slug = f"{request.match_info['owner']}/{request.match_info['repo']}"
    conn = request.app["conn_factory"]()
    try:
        data = queries.landing_data(conn)
        severity_filter = _parse_csv_set(request.query.get("severity"))
        ecosystem_filter = _parse_csv_set(request.query.get("ecosystem"))
        ctx = queries.project_context(
            conn, slug,
            severity_filter=severity_filter,
            ecosystem_filter=ecosystem_filter,
        )
    finally:
        conn.close()
    if ctx is None:
        return web.Response(status=404, text=f"project not found: {slug}")

    template = request.app["jinja"].get_template("project.html")
    html = template.render(
        kpis=data.kpis,
        hostname=request.app["hostname"],
        last_osv_ingest_label=_ago(data.last_osv_ingest),
        last_walk_label=_ago(data.last_github_walk),
        flash=_flash_from_query(request),
        project=ctx,
        active_severity_filter=",".join(sorted(severity_filter)) if severity_filter else "",
    )
    return web.Response(text=html, content_type="text/html")


def _parse_csv_set(s: str | None) -> set[str] | None:
    if not s:
        return None
    return {v.strip() for v in s.split(",") if v.strip()}
```

In `build_app`, add before `add_static`:

```python
    app.router.add_get(r"/p/{owner:[^/]+}/{repo:[^/]+}", project_view)
```

- [ ] **Step 3: Create `agent/dashboard/templates/project.html`**

```jinja
{% extends "base.html" %}
{% block title %}{{ project.slug }} · ckb-advisory-watch{% endblock %}
{% block content %}
<div class="page">
  <div style="display:flex;align-items:baseline;gap:14px;margin-bottom:12px">
    <h1 class="mono">{{ project.slug }}</h1>
    <span class="mono" style="color:var(--text-dim);font-size:12px">{{ project.display_name }}</span>
    <a class="mono" style="margin-left:auto" href="{{ project.repo_url }}">{{ project.repo_url }}</a>
  </div>

  <form method="GET" style="margin-bottom:10px;font:12px Inter">
    <label>severity:
      <select name="severity">
        <option value="">all</option>
        <option value="critical" {% if active_severity_filter == 'critical' %}selected{% endif %}>critical only</option>
        <option value="critical,high" {% if active_severity_filter == 'critical,high' %}selected{% endif %}>critical + high</option>
      </select>
    </label>
    <button type="submit" class="share sm">apply</button>
    <span class="mono" style="margin-left:10px;color:var(--text-dim)">{{ project.matches|length }} matches</span>
  </form>

  <table class="data">
    <thead><tr>
      <th style="width:68px">sev</th>
      <th class="num" style="width:50px">cvss</th>
      <th>advisory</th>
      <th>affected</th>
      <th style="width:90px">fixed in</th>
      <th style="width:56px;text-align:center">share</th>
    </tr></thead>
    <tbody>
      {% for m in project.matches %}
      <tr>
        <td><span class="pill {{ m.severity or 'unknown' }}">{{ (m.severity or 'unk')[:4]|upper }}</span></td>
        <td class="num" style="color:var(--text)">{% if m.cvss %}{{ '%.1f'|format(m.cvss) }}{% else %}—{% endif %}</td>
        <td><a href="/a/{{ m.source_id }}">{{ m.source_id }}</a></td>
        <td>{{ m.dep_name }}@{{ m.dep_version }}</td>
        <td style="color:var(--sev-low)">{{ m.fixed_in or '—' }}</td>
        <td style="text-align:center">
          <form method="POST" action="/share/match/{{ m.match_id }}" style="margin:0">
            <button type="submit" class="share sm">📤</button>
          </form>
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</div>
{% endblock %}
```

- [ ] **Step 4: Run tests — expect pass**

Run: `python -m pytest tests/test_dashboard_routes.py -v`
Expected: all tests pass. Full: `python -m pytest -q` → `84 passed`.

- [ ] **Step 5: Commit**

```bash
git add agent/dashboard/server.py agent/dashboard/templates/project.html tests/test_dashboard_routes.py
git commit -m "feat(dashboard): /p/<owner>/<repo> project page with severity filter"
```

---

## Task 8: Per-advisory page

> **Invoke `frontend-design:frontend-design`** for advisory.html polish.

**Files:**
- Modify: `agent/dashboard/server.py`
- Create: `agent/dashboard/templates/advisory.html`
- Modify: `tests/test_dashboard_routes.py`

- [ ] **Step 1: Append failing tests**

Append to `tests/test_dashboard_routes.py`:

```python
async def test_advisory_page_shows_affected_projects(tmp_path, share_config):
    async with await _client(tmp_path, share_config) as client:
        resp = await client.get("/a/GHSA-crit")
        assert resp.status == 200
        body = await resp.text()
    assert "GHSA-crit" in body
    assert "a/b" in body  # project slug appears
    assert "libgit2-sys" in body


async def test_advisory_page_404_for_unknown(tmp_path, share_config):
    async with await _client(tmp_path, share_config) as client:
        resp = await client.get("/a/GHSA-not-real")
    assert resp.status == 404
```

- [ ] **Step 2: Add handler + route**

Append to `agent/dashboard/server.py` (above `build_app`):

```python
async def advisory_view(request: web.Request) -> web.Response:
    source_id = request.match_info["source_id"]
    conn = request.app["conn_factory"]()
    try:
        landing = queries.landing_data(conn)
        ctx = queries.advisory_context(conn, source_id)
    finally:
        conn.close()
    if ctx is None:
        return web.Response(status=404, text=f"advisory not found: {source_id}")

    template = request.app["jinja"].get_template("advisory.html")
    html = template.render(
        kpis=landing.kpis,
        hostname=request.app["hostname"],
        last_osv_ingest_label=_ago(landing.last_osv_ingest),
        last_walk_label=_ago(landing.last_github_walk),
        flash=_flash_from_query(request),
        advisory=ctx,
    )
    return web.Response(text=html, content_type="text/html")
```

In `build_app`, add:

```python
    app.router.add_get(r"/a/{source_id:[A-Za-z0-9_\-]+}", advisory_view)
```

- [ ] **Step 3: Create `agent/dashboard/templates/advisory.html`**

```jinja
{% extends "base.html" %}
{% block title %}{{ advisory.source_id }} · ckb-advisory-watch{% endblock %}
{% block content %}
<div class="page">
  <div class="mono" style="font-size:11px;color:var(--text-dim);margin-bottom:8px">
    <a href="/">← dashboard</a> / advisory
  </div>

  <div style="display:flex;align-items:center;gap:10px;margin-bottom:6px">
    <span class="pill {{ advisory.severity or 'unknown' }}">
      {{ (advisory.severity or 'unknown')|upper }}
    </span>
    {% if advisory.cvss %}<span class="mono" style="font-weight:700">CVSS {{ '%.1f'|format(advisory.cvss) }}</span>{% endif %}
  </div>
  <h1 class="mono" style="color:var(--link)">{{ advisory.source_id }}</h1>
  <p style="color:var(--text-muted);margin:4px 0 12px">{{ advisory.summary }}</p>

  <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px">
    {% for ref in advisory.references %}
      <a class="mono" style="background:var(--bg-alt);padding:4px 10px;border-radius:2px;font-size:11px"
         href="{{ ref.url }}">{{ ref.url|truncate(60) }} ↗</a>
    {% endfor %}
    {% for cve in advisory.cve_ids %}
      <a class="mono" style="background:var(--bg-alt);padding:4px 10px;border-radius:2px;font-size:11px"
         href="https://nvd.nist.gov/vuln/detail/{{ cve }}">{{ cve }} ↗</a>
    {% endfor %}
  </div>

  <div style="padding:12px 0 14px;display:flex;align-items:center;gap:12px;border-top:1px solid var(--border);border-bottom:1px solid var(--border)">
    <strong style="font-size:12px">AFFECTS {{ advisory.matches|length }} PROJECT(S)</strong>
    <form method="POST" action="/share/advisory/{{ advisory.source_id }}" style="margin-left:auto">
      <button type="submit" class="share">📤 share to inbox</button>
    </form>
  </div>

  <table class="data" style="margin-top:14px">
    <thead><tr>
      <th>project</th>
      <th>affected</th>
      <th style="width:90px">fixed in</th>
    </tr></thead>
    <tbody>
      {% for m in advisory.matches %}
      <tr>
        <td class="proj"><a href="/p/{{ m.project_slug }}">{{ m.project_slug }}</a></td>
        <td>{{ m.dep_name }}@{{ m.dep_version }}</td>
        <td style="color:var(--sev-low)">{{ m.fixed_in or '—' }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>

  {% if advisory.details %}
  <details style="margin-top:20px">
    <summary style="cursor:pointer;color:var(--text-muted);font-size:12px">show full advisory details</summary>
    <pre style="white-space:pre-wrap;background:var(--bg-alt);padding:12px;margin-top:8px;border-radius:3px;font-size:11px;color:var(--text-muted)">{{ advisory.details }}</pre>
  </details>
  {% endif %}
</div>
{% endblock %}
```

- [ ] **Step 4: Run tests — expect pass**

Run: `python -m pytest tests/test_dashboard_routes.py -v`
Expected: all tests pass. Full: `python -m pytest -q` → `86 passed`.

- [ ] **Step 5: Commit**

```bash
git add agent/dashboard/server.py agent/dashboard/templates/advisory.html tests/test_dashboard_routes.py
git commit -m "feat(dashboard): /a/<source-id> advisory page with references + affected list"
```

---

## Task 9: Share POST handlers — match + advisory

**Files:**
- Modify: `agent/dashboard/server.py`
- Modify: `tests/test_dashboard_routes.py`

- [ ] **Step 1: Append failing tests**

Append to `tests/test_dashboard_routes.py`:

```python
async def test_share_match_post_sends_and_redirects(tmp_path, share_config, monkeypatch):
    sent_payloads = []
    def fake_send(payload, cfg):
        sent_payloads.append(payload)
    monkeypatch.setattr("agent.dashboard.share.send_email", fake_send)

    async with await _client(tmp_path, share_config) as client:
        # Find the match id via the index page
        resp = await client.post("/share/match/1", allow_redirects=False)
        assert resp.status == 303
        assert resp.headers["Location"].startswith("/") and "sent=1" in resp.headers["Location"]
    assert len(sent_payloads) == 1
    p = sent_payloads[0]
    assert "GHSA-crit" in p.subject
    assert "libgit2-sys" in p.subject


async def test_share_advisory_post_sends_and_redirects(tmp_path, share_config, monkeypatch):
    sent_payloads = []
    monkeypatch.setattr("agent.dashboard.share.send_email",
                        lambda payload, cfg: sent_payloads.append(payload))

    async with await _client(tmp_path, share_config) as client:
        resp = await client.post("/share/advisory/GHSA-crit", allow_redirects=False)
        assert resp.status == 303
        assert resp.headers["Location"] == "/a/GHSA-crit?sent=1"
    assert len(sent_payloads) == 1
    assert "GHSA-crit" in sent_payloads[0].subject


async def test_share_match_post_propagates_smtp_error_as_query_param(tmp_path, share_config, monkeypatch):
    import smtplib
    def boom(payload, cfg):
        raise smtplib.SMTPAuthenticationError(535, b"auth")
    monkeypatch.setattr("agent.dashboard.share.send_email", boom)

    async with await _client(tmp_path, share_config) as client:
        resp = await client.post("/share/match/1", allow_redirects=False)
    assert resp.status == 303
    assert "sent_error=" in resp.headers["Location"]
```

- [ ] **Step 2: Add POST handlers + routes**

Append to `agent/dashboard/server.py`:

```python
async def share_match_view(request: web.Request) -> web.Response:
    match_id = int(request.match_info["match_id"])
    conn = request.app["conn_factory"]()
    try:
        # Fetch the match + advisory context. match_id -> source_id lookup.
        row = conn.execute(
            "SELECT a.source_id FROM match m JOIN advisory a ON a.id = m.advisory_id "
            "WHERE m.id = ?", (match_id,),
        ).fetchone()
        if not row:
            return web.Response(status=404, text=f"match not found: {match_id}")
        source_id = row[0]
        advisory = queries.advisory_context(conn, source_id)
    finally:
        conn.close()

    if advisory is None:
        return web.Response(status=500, text="advisory context missing")
    match = next((m for m in advisory.matches if m.match_id == match_id), None)
    if match is None:
        return web.Response(status=500, text="match dropped between lookups")

    referer = request.headers.get("Referer") or f"/a/{source_id}"
    try:
        payload = share.build_match_email(match, advisory, request.app["share_config"])
        share.send_email(payload, request.app["share_config"])
    except Exception as exc:
        log.error("share_match: send failed: %r", exc)
        sep = "&" if "?" in referer else "?"
        raise web.HTTPSeeOther(f"{referer}{sep}sent_error={type(exc).__name__}")

    sep = "&" if "?" in referer else "?"
    raise web.HTTPSeeOther(f"{referer}{sep}sent=1")


async def share_advisory_view(request: web.Request) -> web.Response:
    source_id = request.match_info["source_id"]
    conn = request.app["conn_factory"]()
    try:
        advisory = queries.advisory_context(conn, source_id)
    finally:
        conn.close()
    if advisory is None:
        return web.Response(status=404, text=f"advisory not found: {source_id}")

    try:
        payload = share.build_advisory_email(advisory, request.app["share_config"])
        share.send_email(payload, request.app["share_config"])
    except Exception as exc:
        log.error("share_advisory: send failed: %r", exc)
        raise web.HTTPSeeOther(f"/a/{source_id}?sent_error={type(exc).__name__}")
    raise web.HTTPSeeOther(f"/a/{source_id}?sent=1")
```

In `build_app`, add:

```python
    app.router.add_post(r"/share/match/{match_id:\d+}", share_match_view)
    app.router.add_post(r"/share/advisory/{source_id:[A-Za-z0-9_\-]+}", share_advisory_view)
```

- [ ] **Step 3: Run tests — expect pass**

Run: `python -m pytest tests/test_dashboard_routes.py -v`
Expected: all tests pass. Full: `python -m pytest -q` → `89 passed`.

- [ ] **Step 4: Commit**

```bash
git add agent/dashboard/server.py tests/test_dashboard_routes.py
git commit -m "feat(dashboard): POST /share/match/<id> + /share/advisory/<sid>"
```

---

## Task 10: Wire dashboard into main.py + config

**Files:**
- Modify: `agent/main.py`
- Modify: `config.example.toml`
- Modify: `README.md`

- [ ] **Step 1: Update `config.example.toml`**

Append:

```toml

[dashboard]
# Bind host and port for the browser dashboard. 0.0.0.0 makes it reachable
# from other machines on the LAN.
host = "0.0.0.0"
port = 8080
# Absolute URL to this instance; used in share emails so recipients can
# click back in. On the Pi: http://192.168.68.121:8080 (or domain if proxied).
base_url = "http://127.0.0.1:8080"

[share]
enabled = true
# Where share emails land. Typically your own inbox for quick forward.
recipient = ""
sender    = ""
# Gmail SMTP defaults; fill smtp_user + smtp_password with a Gmail address
# and an app password (https://myaccount.google.com/apppasswords).
smtp_host = "smtp.gmail.com"
smtp_port = 465
smtp_user = ""
smtp_password = ""
```

- [ ] **Step 2: Modify `agent/main.py`**

Add imports near the top of `agent/main.py`:

```python
import socket
import sqlite3 as _sqlite3  # re-imported name for conn_factory closure

from aiohttp import web

from agent.dashboard import server as dashboard_server
from agent.dashboard import share as dashboard_share
```

Add this function above `run`:

```python
async def start_dashboard(
    config: dict,
    data_dir: Path,
    stop: asyncio.Event,
) -> None:
    """aiohttp AppRunner lifecycle bound to the shared stop Event.

    Each request opens its own read-only SQLite connection via the factory;
    the agent's writer loop is untouched."""
    dash_cfg = config.get("dashboard", {}) or {}
    share_cfg_d = config.get("share", {}) or {}
    if not share_cfg_d.get("enabled", False):
        log.info("dashboard: share disabled (config [share].enabled = false)")

    db_path = data_dir / "state.db"

    def conn_factory() -> _sqlite3.Connection:
        return _sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    share_cfg = dashboard_share.ShareConfig(
        recipient=share_cfg_d.get("recipient", ""),
        sender=share_cfg_d.get("sender", ""),
        smtp_host=share_cfg_d.get("smtp_host", "smtp.gmail.com"),
        smtp_port=int(share_cfg_d.get("smtp_port", 465)),
        smtp_user=share_cfg_d.get("smtp_user", ""),
        smtp_password=share_cfg_d.get("smtp_password", ""),
        dashboard_base_url=dash_cfg.get("base_url", "http://127.0.0.1:8080"),
    )

    app = dashboard_server.build_app(
        conn_factory=conn_factory,
        share_config=share_cfg,
        hostname=socket.gethostname(),
    )

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(
        runner,
        host=dash_cfg.get("host", "0.0.0.0"),
        port=int(dash_cfg.get("port", 8080)),
    )
    await site.start()
    log.info(
        "dashboard: listening on http://%s:%d",
        dash_cfg.get("host", "0.0.0.0"),
        int(dash_cfg.get("port", 8080)),
    )

    try:
        await stop.wait()
    finally:
        await runner.cleanup()
```

Modify `run` — add `start_dashboard(config, data_dir, stop)` to the `asyncio.gather(...)` call. Replace the gather block:

```python
        try:
            await asyncio.gather(
                osv_poll_loop(conn, osv_client, ecosystems, osv_interval, stop),
                github_poll_loop(conn, gh_client, github_interval, stop),
                start_dashboard(config, data_dir, stop),
            )
        finally:
            log.info("ckb-advisory-watch stopped")
            conn.close()
```

- [ ] **Step 3: Update README Phases section**

In `README.md`, update the Phase 4 line:

```markdown
- **Phase 4** — Outputs: browser dashboard ✓ (read-only, share-to-email); Telegram bot, vault sync, wyltekindustries page pending
```

And add an "Open the dashboard" subsection below Install:

```markdown
## Open the dashboard

Once the service is running:

```
http://<host-or-ip>:8080/
```

URL structure:
- `/` — landing (glance + triage + exploration)
- `/p/<owner>/<repo>` — per-project matches
- `/a/<source-id>` — per-advisory affected projects

Share buttons on match rows and advisory pages send a structured email
via Gmail SMTP to the address in `[share].recipient` — configure
`smtp_user` and `smtp_password` (app password) in `config.toml`.
```

- [ ] **Step 4: Smoke test locally (driveThree)**

Create a throwaway config:

```bash
cat > /tmp/dash-test-config.toml <<'EOF'
[agent]
data_dir = "data"
[github]
token = ""
[poll]
github_repos = 86400
osv = 3600
[osv]
ecosystems = []    # skip polling during smoke
[dashboard]
host = "127.0.0.1"
port = 8099
base_url = "http://127.0.0.1:8099"
[share]
enabled = false    # don't try to send during smoke
recipient = ""
sender = ""
smtp_host = "smtp.gmail.com"
smtp_port = 465
smtp_user = ""
smtp_password = ""
EOF
```

The `data/state.db` already populated from earlier smoke tests will serve as live data.

Run the agent:
```bash
. .venv/bin/activate && python -m agent.main --config /tmp/dash-test-config.toml &
AGENT_PID=$!
sleep 3
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8099/
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8099/a/GHSA-22q8-ghmq-63vf || true
kill $AGENT_PID
```

Expected: first curl = `200`; second curl = `200` or `404` (depending on whether that exact source_id is in your db).

- [ ] **Step 5: Full test suite**

Run: `python -m pytest -q`
Expected: `89 passed` (no regressions).

- [ ] **Step 6: Commit**

```bash
git add agent/main.py config.example.toml README.md
git commit -m "feat(dashboard): wire into main.py + config + docs"
```

---

## Task 11: End-to-end visual QA on driveThree (no Pi deploy)

**Files:**
- None (manual QA).

- [ ] **Step 1: Run the agent with your real config**

On driveThree:
```bash
cp config.example.toml config.toml
# edit config.toml: set [github].token from `gh auth token`, and for a
# real send test, set [share] recipient/sender/smtp_user/smtp_password
. .venv/bin/activate && python -m agent.main --config config.toml &
```

Wait 30s for at least one OSV ingest + walker to run.

- [ ] **Step 2: Browse and check each page in a real browser**

Visit `http://127.0.0.1:8080/`. Verify:
- Severity tiles light up with actual counts.
- Triage table shows recent critical+high matches.
- Sidebar has the projects you expect (Magickbase/force-bridge near the top).

Click into `/p/Magickbase/force-bridge`. Verify:
- Table of all matches for that project renders.
- Severity filter dropdown works (reload with `?severity=critical` in URL).

Click an advisory link (e.g. one from the force-bridge table). Verify:
- Advisory summary + references render.
- Affected projects table shows real project slugs with versions.

- [ ] **Step 3: Share button smoke (if SMTP configured)**

With real Gmail app password in config:
- Click 📤 on a match row. Page redirects with `?sent=1` banner.
- Check Gmail inbox — email arrives with expected subject + body.
- Click 📤 "share to inbox" on an advisory page. Same flow.

If SMTP not yet configured, skip — or leave `[share].enabled=false` and confirm the 📤 buttons still render (they'll hit the handler, which will error on the SMTP call and redirect with `?sent_error=`).

- [ ] **Step 4: Kill the agent**

```bash
kill %1 || pkill -f 'agent.main'
```

- [ ] **Step 5: Commit nothing — this is QA**

(No changes to commit. If you found issues, go back to the relevant task and fix before Pi deploy, which happens outside this plan.)

---

## Out-of-scope follow-ups

These were agreed as out of scope in §12 of the spec and should become new plans later:

- **pnpm-lock.yaml / yarn.lock parsers** — lifts JS coverage from ~40% to ~90%.
- **Python lockfile parsers** — closes self-watch gap.
- **Telegram output** — separate push channel spec.
- **Pi deployment** — straightforward: push commits to GitHub, `git pull + pip install + systemctl --user restart` on `192.168.68.121`. Deferred until Pi's cold-start npm ingest settles.
- **Static mirror generation** for `wyltekindustries.com/advisories` — wget crawl + rsync hook.
- **Match state mutation** (ack / suppress UI) — the `suppression` table already exists but no UI for it.
- **GitHub issue as share target** — implement a second dispatcher in `share.py` alongside `send_email`.
