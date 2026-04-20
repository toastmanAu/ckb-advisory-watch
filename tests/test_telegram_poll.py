"""Integration tests for the telegram poll loop."""
from __future__ import annotations

import sqlite3

import pytest

from agent.output.telegram import (
    SUBCH_DM, SUBCH_CHANNEL, SEVERITY_LEVEL,
    _unemitted_advisories_above, baseline_if_first_run,
)
from tests.dashboard_fixtures import fresh_db, seed_match


def test_unemitted_finds_matches_above_threshold(tmp_path):
    conn = fresh_db(tmp_path)
    seed_match(conn, project_slug="a/b", source_id="GHSA-c", severity="critical", cvss=9.8)
    seed_match(conn, project_slug="a/b", source_id="GHSA-l", severity="low", cvss=2.0, dep_name="pkg2")
    rows = _unemitted_advisories_above(conn, SUBCH_DM, SEVERITY_LEVEL["medium"])
    source_ids = {r[1] for r in rows}
    assert "GHSA-c" in source_ids
    assert "GHSA-l" not in source_ids


def test_unemitted_excludes_already_emitted_on_same_subchannel(tmp_path):
    conn = fresh_db(tmp_path)
    _, _, match_id = seed_match(conn, source_id="GHSA-x", severity="critical")
    # Simulate already-emitted on telegram.dm
    conn.execute(
        "INSERT INTO emission (match_id, channel, emitted_at, artifact_path) "
        "VALUES (?, ?, strftime('%s','now'), '42')",
        (match_id, SUBCH_DM),
    )
    conn.commit()
    rows_dm = _unemitted_advisories_above(conn, SUBCH_DM, SEVERITY_LEVEL["medium"])
    assert not any(r[1] == "GHSA-x" for r in rows_dm)
    # Other sub-channel still unemitted
    rows_ch = _unemitted_advisories_above(conn, SUBCH_CHANNEL, SEVERITY_LEVEL["medium"])
    assert any(r[1] == "GHSA-x" for r in rows_ch)


def test_unemitted_returns_distinct_advisory_per_row(tmp_path):
    conn = fresh_db(tmp_path)
    seed_match(conn, project_slug="a/one", source_id="GHSA-shared", severity="critical", dep_name="lib")
    seed_match(conn, project_slug="a/two", source_id="GHSA-shared", severity="critical", dep_name="lib")
    rows = _unemitted_advisories_above(conn, SUBCH_DM, SEVERITY_LEVEL["medium"])
    # One advisory row even though two matches
    assert len([r for r in rows if r[1] == "GHSA-shared"]) == 1


def test_baseline_first_run_inserts_emissions_above_threshold(tmp_path):
    conn = fresh_db(tmp_path)
    seed_match(conn, project_slug="a/b", source_id="GHSA-c", severity="critical", cvss=9.8)
    seed_match(conn, project_slug="a/b", source_id="GHSA-l", severity="low", cvss=2.0, dep_name="pkg2")
    inserted = baseline_if_first_run(conn, SUBCH_DM, SEVERITY_LEVEL["medium"])
    assert inserted == 1  # only critical above medium threshold
    # emission row for the critical match
    rows = conn.execute(
        "SELECT COUNT(*) FROM emission WHERE channel = ? AND artifact_path = 'baseline'",
        (SUBCH_DM,),
    ).fetchone()
    assert rows[0] == 1
    # poller_state key set
    key_row = conn.execute(
        "SELECT value FROM poller_state WHERE key = ?",
        (f"telegram.baseline_done.{SUBCH_DM}",),
    ).fetchone()
    assert key_row[0] == "1"


def test_baseline_second_run_is_noop(tmp_path):
    conn = fresh_db(tmp_path)
    seed_match(conn, source_id="GHSA-c", severity="critical")
    first = baseline_if_first_run(conn, SUBCH_DM, SEVERITY_LEVEL["medium"])
    second = baseline_if_first_run(conn, SUBCH_DM, SEVERITY_LEVEL["medium"])
    assert first == 1
    assert second == 0
    rows = conn.execute(
        "SELECT COUNT(*) FROM emission WHERE channel = ?", (SUBCH_DM,),
    ).fetchone()
    assert rows[0] == 1  # not doubled


def test_baseline_per_subchannel_independent(tmp_path):
    conn = fresh_db(tmp_path)
    seed_match(conn, source_id="GHSA-c", severity="critical")
    n_dm = baseline_if_first_run(conn, SUBCH_DM, SEVERITY_LEVEL["medium"])
    n_ch = baseline_if_first_run(conn, SUBCH_CHANNEL, SEVERITY_LEVEL["medium"])
    assert n_dm == 1 and n_ch == 1
    # Two emission rows, one per sub-channel
    total = conn.execute(
        "SELECT COUNT(*) FROM emission WHERE artifact_path = 'baseline'"
    ).fetchone()[0]
    assert total == 2
