"""OSV parser + upsert tests.

Fixtures in tests/fixtures/osv/ are real records pulled from
https://osv-vulnerabilities.storage.googleapis.com/crates.io/all.zip so the
shape can't drift from the spec without the tests noticing.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agent.db import open_db
from agent.sources.osv import (
    extract_affects,
    parse_osv_record,
    upsert_advisory,
)

FIXTURES = Path(__file__).parent / "fixtures" / "osv"
SCHEMA = Path(__file__).parent.parent / "db" / "schema.sql"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    return open_db(tmp_path / "t.db", SCHEMA)


# ---------- parse_osv_record ----------


def test_parse_basic_ghsa_with_severity_string():
    raw = _load("GHSA-2226-4v3c-cff8")
    p = parse_osv_record(raw)
    assert p.source == "osv"
    assert p.source_id == "GHSA-2226-4v3c-cff8"
    assert p.summary.startswith("Stack overflow in rustc_serialize")
    assert p.severity == "medium"  # MODERATE -> medium
    assert p.cvss is None  # no CVSS vector on this record
    assert "RUSTSEC-2022-0004" in raw["aliases"]
    assert p.cve_ids == []  # no CVE alias here
    assert p.published is not None and p.modified is not None


def test_parse_extracts_cves_from_aliases():
    raw = _load("GHSA-275g-g844-73jh")
    p = parse_osv_record(raw)
    assert p.cve_ids == ["CVE-2025-53549"]


def test_parse_with_cvss_vector():
    raw = _load("GHSA-22q8-ghmq-63vf")
    p = parse_osv_record(raw)
    assert p.severity == "high"  # HIGH -> high
    # CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:L/A:L -> base score 8.6
    assert p.cvss is not None
    assert 8.0 <= p.cvss <= 9.0


def test_parse_rustsec_no_severity():
    raw = _load("RUSTSEC-2024-0001")
    p = parse_osv_record(raw)
    assert p.source_id == "RUSTSEC-2024-0001"
    # Informational advisory — no severity field, no CVSS vector.
    assert p.severity is None
    assert p.cvss is None


def test_severity_falls_back_to_cvss_bucket():
    """Strategy (b): when no GHSA label is present, bucket the CVSS score so
    RUSTSEC / CVSS-only advisories don't silently fall through output filters."""
    # Synthesize: no database_specific.severity, but a CVSS:3.1 vector at 8.6.
    raw = {
        "id": "RUSTSEC-FAKE-0001",
        "modified": "2026-01-01T00:00:00Z",
        "severity": [
            {"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:L/A:L"},
        ],
    }
    p = parse_osv_record(raw)
    assert p.cvss is not None and 8.0 <= p.cvss <= 9.0
    assert p.severity == "high"  # 8.6 falls in [7.0, 9.0) -> high


def test_severity_label_wins_over_cvss_bucket():
    """When GHSA ships a label AND a CVSS vector, the label wins (preserves
    human review judgement rather than re-bucketing)."""
    raw = {
        "id": "GHSA-FAKE",
        "modified": "2026-01-01T00:00:00Z",
        "severity": [
            # 9.8 would bucket as "critical" but label says MODERATE.
            {"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"},
        ],
        "database_specific": {"severity": "MODERATE"},
    }
    p = parse_osv_record(raw)
    assert p.severity == "medium"
    assert p.cvss is not None and p.cvss >= 9.0


# ---------- extract_affects ----------


def test_extract_affects_single_package():
    raw = _load("GHSA-2226-4v3c-cff8")
    rows = extract_affects(raw)
    assert len(rows) == 1
    r = rows[0]
    assert r.ecosystem == "crates.io"
    assert r.name == "rustc-serialize"
    # last_affected=0.3.24 -> stored as raw range expression
    assert "0.3.24" in r.version_range
    assert r.fixed_in is None


def test_extract_affects_multi_package():
    raw = _load("GHSA-275g-g844-73jh")
    rows = extract_affects(raw)
    assert len(rows) == 2
    names = {r.name for r in rows}
    assert names == {"matrix-sdk", "matrix-sdk-sqlite"}
    for r in rows:
        assert r.fixed_in == "0.13.0"


def test_extract_affects_preserves_ecosystem_case():
    # OSV uses "crates.io" (lowercase). project_dep stores "cargo" in our
    # schema — that mapping is the matcher's job, not the parser's.
    raw = _load("RUSTSEC-2024-0001")
    rows = extract_affects(raw)
    assert rows[0].ecosystem == "crates.io"


# ---------- upsert_advisory ----------


def test_upsert_writes_advisory_and_affects(db: sqlite3.Connection):
    raw = _load("GHSA-275g-g844-73jh")
    adv_id = upsert_advisory(db, raw)
    assert adv_id > 0

    rows = db.execute(
        "SELECT source, source_id, severity FROM advisory WHERE id = ?", (adv_id,)
    ).fetchall()
    assert rows == [("osv", "GHSA-275g-g844-73jh", "medium")]

    aff = db.execute(
        "SELECT ecosystem, name, fixed_in FROM advisory_affects WHERE advisory_id = ? ORDER BY name",
        (adv_id,),
    ).fetchall()
    assert aff == [
        ("crates.io", "matrix-sdk", "0.13.0"),
        ("crates.io", "matrix-sdk-sqlite", "0.13.0"),
    ]


def test_upsert_is_idempotent(db: sqlite3.Connection):
    raw = _load("GHSA-2226-4v3c-cff8")
    first = upsert_advisory(db, raw)
    second = upsert_advisory(db, raw)
    assert first == second

    (count,) = db.execute("SELECT COUNT(*) FROM advisory").fetchone()
    assert count == 1
    (aff_count,) = db.execute("SELECT COUNT(*) FROM advisory_affects").fetchone()
    assert aff_count == 1


def test_upsert_updates_on_newer_modified(db: sqlite3.Connection):
    raw = _load("GHSA-2226-4v3c-cff8")
    upsert_advisory(db, raw)

    # simulate a newer version arriving with a different summary
    raw2 = dict(raw)
    raw2["modified"] = "2099-01-01T00:00:00Z"
    raw2["summary"] = "UPDATED SUMMARY"
    upsert_advisory(db, raw2)

    (summary,) = db.execute(
        "SELECT summary FROM advisory WHERE source_id = ?",
        ("GHSA-2226-4v3c-cff8",),
    ).fetchone()
    assert summary == "UPDATED SUMMARY"


def test_upsert_replaces_affects_on_update(db: sqlite3.Connection):
    """If affected packages change between versions, old rows must be
    removed. Otherwise stale package entries silently linger and the
    matcher will over-report."""
    raw = _load("GHSA-275g-g844-73jh")
    upsert_advisory(db, raw)

    # Newer version now affects only one of the two packages.
    raw2 = dict(raw)
    raw2["modified"] = "2099-01-01T00:00:00Z"
    raw2["affected"] = [raw["affected"][0]]  # drop matrix-sdk-sqlite
    upsert_advisory(db, raw2)

    names = [
        n for (n,) in db.execute(
            "SELECT name FROM advisory_affects WHERE advisory_id = "
            "(SELECT id FROM advisory WHERE source_id = ?) ORDER BY name",
            ("GHSA-275g-g844-73jh",),
        )
    ]
    assert names == ["matrix-sdk"]
