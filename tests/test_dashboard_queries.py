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
