"""Tests for dashboard SQL helpers."""
from __future__ import annotations

import time

from agent.dashboard.queries import landing_data, LandingData
from agent.dashboard.queries import (
    project_context, advisory_context, ProjectContext, AdvisoryContext,
    meets_severity_floor,
)
from tests.dashboard_fixtures import fresh_db, seed_match


def test_landing_data_last_timestamps(tmp_path):
    conn = fresh_db(tmp_path)
    osv_ts = 1_700_000_000
    walk_ts = 1_700_001_000
    conn.execute(
        "INSERT INTO poller_state (key, value, updated_at) VALUES (?, ?, ?)",
        ("osv.etag.crates.io", "etag-abc", osv_ts),
    )
    conn.commit()
    # seed a project with a known last_checked
    from agent.db import upsert_project
    pid = upsert_project(conn, slug="ts/test", display_name="ts/test", repo_url="https://github.com/ts/test")
    conn.execute("UPDATE project SET last_checked = ? WHERE id = ?", (walk_ts, pid))
    conn.commit()

    data = landing_data(conn)
    assert data.last_osv_ingest == osv_ts
    assert data.last_github_walk == walk_ts


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


def test_project_context_severity_floor_excludes_low_and_unknown(tmp_path):
    conn = fresh_db(tmp_path)
    seed_match(conn, project_slug="a/b", source_id="GHSA-crit",
               severity="critical", cvss=9.8, dep_name="p1")
    seed_match(conn, project_slug="a/b", source_id="GHSA-med",
               severity="medium", cvss=5.0, dep_name="p2")
    seed_match(conn, project_slug="a/b", source_id="GHSA-low",
               severity="low", cvss=3.0, dep_name="p3")
    seed_match(conn, project_slug="a/b", source_id="GHSA-unknown",
               severity=None, cvss=None, dep_name="p4")

    ctx = project_context(
        conn, "a/b",
        severity_floor=("critical", "high", "medium"),
    )
    assert ctx is not None
    seen = {m.source_id for m in ctx.matches}
    assert seen == {"GHSA-crit", "GHSA-med"}


def test_project_context_severity_floor_none_is_no_filter(tmp_path):
    conn = fresh_db(tmp_path)
    seed_match(conn, project_slug="a/b", source_id="GHSA-low",
               severity="low", cvss=3.0, dep_name="p1")
    ctx = project_context(conn, "a/b", severity_floor=None)
    assert ctx is not None
    assert {m.source_id for m in ctx.matches} == {"GHSA-low"}


def test_advisory_context_still_returns_for_low_severity_when_no_floor(tmp_path):
    """advisory_context itself does not gate on severity; the caller
    (mirror render_all) decides whether to emit the page."""
    conn = fresh_db(tmp_path)
    seed_match(conn, project_slug="a/b", source_id="GHSA-low",
               severity="low", cvss=3.0, dep_name="p1")
    ctx = advisory_context(conn, "GHSA-low")
    assert ctx is not None
    assert ctx.severity == "low"


def test_meets_severity_floor_basic():
    assert meets_severity_floor("critical", ("critical", "high", "medium")) is True
    assert meets_severity_floor("medium", ("critical", "high", "medium")) is True
    assert meets_severity_floor("low", ("critical", "high", "medium")) is False
    assert meets_severity_floor(None, ("critical", "high", "medium")) is False
    # Empty floor tuple means "no floor" — everything passes
    assert meets_severity_floor("low", ()) is True
    assert meets_severity_floor(None, ()) is True
