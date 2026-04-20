"""Matcher tests: is_affected (pure) and run_matcher (end-to-end vs DB).

Version-range intersection against OSV's typed `events` list. We cover
the common shapes from real crates.io / npm advisories and the edge cases
where version parsing fails (Go pseudo-versions especially).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agent.db import open_db, upsert_project, upsert_project_dep
from agent.matcher import (
    is_affected,
    run_matcher,
    UnparseableVersionPolicy,
)
from agent.sources.osv import upsert_advisory

SCHEMA = Path(__file__).parent.parent / "db" / "schema.sql"
FIXTURES = Path(__file__).parent / "fixtures" / "osv"


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    return open_db(tmp_path / "state.db", SCHEMA)


# --- is_affected: common shapes ---


def test_simple_introduced_fixed_range():
    """introduced=0, fixed=1.2.3 → everything < 1.2.3 is vulnerable."""
    ranges = [{"type": "SEMVER", "events": [{"introduced": "0"}, {"fixed": "1.2.3"}]}]
    assert is_affected("1.0.0", ranges) is True
    assert is_affected("1.2.2", ranges) is True
    assert is_affected("1.2.3", ranges) is False  # fix IS the safe version
    assert is_affected("2.0.0", ranges) is False


def test_last_affected_is_inclusive():
    """last_affected=0.3.24 → 0.3.24 vulnerable, 0.3.25 not."""
    ranges = [{"type": "SEMVER", "events": [{"introduced": "0"}, {"last_affected": "0.3.24"}]}]
    assert is_affected("0.3.24", ranges) is True
    assert is_affected("0.3.25", ranges) is False


def test_introduced_without_fix_matches_forever_forward():
    """Open-ended range: everything at or above `introduced` is vulnerable."""
    ranges = [{"type": "SEMVER", "events": [{"introduced": "1.0.0"}]}]
    assert is_affected("0.9.0", ranges) is False
    assert is_affected("1.0.0", ranges) is True
    assert is_affected("5.0.0", ranges) is True


def test_multiple_ranges_in_one_package():
    """Advisory can list disjoint vulnerable windows (reintroduced bug)."""
    ranges = [
        {"type": "SEMVER", "events": [{"introduced": "0"}, {"fixed": "1.0.0"}]},
        {"type": "SEMVER", "events": [{"introduced": "2.0.0"}, {"fixed": "2.5.0"}]},
    ]
    assert is_affected("0.5.0", ranges) is True
    assert is_affected("1.5.0", ranges) is False  # patched window
    assert is_affected("2.3.0", ranges) is True
    assert is_affected("3.0.0", ranges) is False


def test_ignores_git_range_type():
    """GIT ranges are commit-hash based; out of scope for v0."""
    ranges = [{"type": "GIT", "events": [{"introduced": "abc"}, {"fixed": "def"}]}]
    assert is_affected("1.0.0", ranges) is False


def test_empty_ranges_no_match():
    assert is_affected("1.0.0", []) is False


def test_pre_release_versions():
    """Real-world pre-release from RUSTSEC-2024-0001: introduced=0.1.3-0."""
    ranges = [{"type": "SEMVER", "events": [{"introduced": "0.1.3-0"}, {"fixed": "0.3.1"}]}]
    assert is_affected("0.2.0", ranges) is True
    assert is_affected("0.3.1", ranges) is False


# --- is_affected: unparseable version policy ---


def test_unparseable_skip_policy_returns_false():
    """Go pseudo-versions can't be parsed by packaging.Version. Policy 'skip'
    conservatively returns False — no alert, no false positive, but may miss
    real vulns in deps using pseudo-versions."""
    ranges = [{"type": "SEMVER", "events": [{"introduced": "0"}, {"fixed": "9.9.9"}]}]
    pseudo = "v0.0.0-20220811171246-fbc7d0a398ab"
    assert is_affected(pseudo, ranges, policy=UnparseableVersionPolicy.SKIP) is False


def test_unparseable_match_policy_returns_true():
    """Policy 'match' assumes any unparseable version is vulnerable — lots
    of false positives but no missed alerts."""
    ranges = [{"type": "SEMVER", "events": [{"introduced": "0"}, {"fixed": "9.9.9"}]}]
    assert is_affected("garbage-v0", ranges, policy=UnparseableVersionPolicy.MATCH) is True


# --- run_matcher: end-to-end against DB ---


def test_run_matcher_creates_match_rows(db: sqlite3.Connection):
    """Full pipeline: seed an advisory + a project with a vulnerable dep,
    run matcher, verify match row lands in the table."""
    pid = upsert_project(db, slug="x/y", display_name="X", repo_url="u")
    # Vulnerable dep at the exact version the fixture advisory covers.
    # The libgit2-sys advisory affects crates.io:libgit2-sys <0.16.2.
    with db:
        upsert_project_dep(
            db, project_id=pid, ecosystem="crates.io",
            name="libgit2-sys", version="0.16.1", source_sha="sha1",
        )
        db.execute("UPDATE project SET last_sha=? WHERE id=?", ("sha1", pid))

    adv_raw = json.loads((FIXTURES / "GHSA-22q8-ghmq-63vf.json").read_text())
    with db:
        upsert_advisory(db, adv_raw)

    n = run_matcher(db)
    assert n == 1

    rows = db.execute(
        "SELECT project_id, state FROM match"
    ).fetchall()
    assert rows == [(pid, "open")]


def test_run_matcher_is_idempotent(db: sqlite3.Connection):
    """Second run against identical data must not create duplicate match rows."""
    pid = upsert_project(db, slug="x/y", display_name="X", repo_url="u")
    with db:
        upsert_project_dep(
            db, project_id=pid, ecosystem="crates.io",
            name="libgit2-sys", version="0.16.1", source_sha="sha1",
        )
        db.execute("UPDATE project SET last_sha=? WHERE id=?", ("sha1", pid))
    adv_raw = json.loads((FIXTURES / "GHSA-22q8-ghmq-63vf.json").read_text())
    with db:
        upsert_advisory(db, adv_raw)

    run_matcher(db)
    run_matcher(db)
    (count,) = db.execute("SELECT COUNT(*) FROM match").fetchone()
    assert count == 1


def test_run_matcher_skips_when_version_not_in_range(db: sqlite3.Connection):
    """Project has the right package but a fixed version — no match."""
    pid = upsert_project(db, slug="x/y", display_name="X", repo_url="u")
    with db:
        upsert_project_dep(
            db, project_id=pid, ecosystem="crates.io",
            name="libgit2-sys", version="0.17.0", source_sha="sha1",  # post-fix
        )
        db.execute("UPDATE project SET last_sha=? WHERE id=?", ("sha1", pid))
    adv_raw = json.loads((FIXTURES / "GHSA-22q8-ghmq-63vf.json").read_text())
    with db:
        upsert_advisory(db, adv_raw)

    n = run_matcher(db)
    assert n == 0


def test_run_matcher_only_scans_current_sha(db: sqlite3.Connection):
    """Stale deps from a previous walk (different source_sha) should not
    trigger matches — project.last_sha is the filter."""
    pid = upsert_project(db, slug="x/y", display_name="X", repo_url="u")
    with db:
        # Dep at old SHA — stale audit trail row
        upsert_project_dep(
            db, project_id=pid, ecosystem="crates.io",
            name="libgit2-sys", version="0.16.1", source_sha="old_sha",
        )
        db.execute("UPDATE project SET last_sha=? WHERE id=?", ("new_sha", pid))
    adv_raw = json.loads((FIXTURES / "GHSA-22q8-ghmq-63vf.json").read_text())
    with db:
        upsert_advisory(db, adv_raw)

    n = run_matcher(db)
    assert n == 0
