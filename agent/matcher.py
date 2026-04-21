"""Advisory × dep matcher.

Joins project_dep × advisory_affects on (ecosystem, name), intersects the
dep's version with the advisory's affected ranges, writes match rows.

v0 scope:
  * SEMVER and ECOSYSTEM range types — parsed via packaging.Version,
    which is PEP 440 but lenient enough for most SemVer.
  * introduced / fixed / last_affected events.
  * GIT ranges ignored (commit-hash matching is a separate problem).
  * Enumerated `versions` list ignored (rare in modern records — extend later).
  * Only current deps match: filtered by project_dep.source_sha == project.last_sha.
"""
from __future__ import annotations

import enum
import json
import logging
import sqlite3
import time

from packaging.version import InvalidVersion, Version

log = logging.getLogger(__name__)


class UnparseableVersionPolicy(str, enum.Enum):
    """How is_affected behaves when packaging.Version can't parse a version.

    Go pseudo-versions (v0.0.0-20220811171246-fbc7d0a398ab) and some npm
    pre-release strings fall here. The choice is a false-positive vs.
    false-negative trade-off that affects alert volume and trust:

    * SKIP    — unparseable deps are treated as NOT affected. Conservative:
                zero false positives from this path, but may silently miss
                real vulnerabilities in Go-heavy stacks.

    * MATCH   — unparseable deps are treated as affected whenever the
                advisory covers their package name at all. Maximally paranoid:
                no missed alerts, but Go projects will generate noise until
                an operator acks or suppresses.
    """
    SKIP = "skip"
    MATCH = "match"


# DESIGN CALL — MATCH default. Better to over-alert and let the operator
# triage than silently drop a real vuln because packaging.Version choked on
# a Go pseudo-version. Paired with a suppression workflow once Phase 4 lands;
# until then expect some noise on Go-heavy projects.
DEFAULT_UNPARSEABLE_POLICY = UnparseableVersionPolicy.MATCH


def _parse_version(version: str) -> Version | None:
    """Parse a version string into a comparable Version, or None if unparseable.

    Uses packaging.Version (PEP 440). This works cleanly for PyPI and covers
    most SemVer, but fails on Go pseudo-versions and some esoteric npm
    pre-release strings. See UnparseableVersionPolicy for the policy."""
    try:
        return Version(version)
    except InvalidVersion:
        return None


def _range_matches(
    dep_version: Version,
    range_events: list[dict],
) -> bool:
    """One OSV range → is the dep version in its vulnerable window?

    OSV events are an ordered list of introduced/fixed/last_affected markers.
    Real advisories have 1-2 events per range; this handles any count by
    pairing introduced with the next terminus."""
    introduced: Version | None = None
    for event in range_events:
        if "introduced" in event:
            raw = event["introduced"]
            introduced = None if raw == "0" else _parse_version(raw)
            if introduced is None and raw != "0":
                return False  # unparseable introduced bound — skip this range
        elif "fixed" in event:
            fix = _parse_version(event["fixed"])
            if fix is None:
                continue
            lo_ok = introduced is None or dep_version >= introduced
            if lo_ok and dep_version < fix:
                return True
            introduced = None
        elif "last_affected" in event:
            last = _parse_version(event["last_affected"])
            if last is None:
                continue
            lo_ok = introduced is None or dep_version >= introduced
            if lo_ok and dep_version <= last:
                return True
            introduced = None
    # Open-ended (introduced with no terminus) → vulnerable forever forward.
    if introduced is not None and dep_version >= introduced:
        return True
    return False


def is_affected(
    dep_version_str: str,
    ranges: list[dict],
    *,
    policy: UnparseableVersionPolicy = DEFAULT_UNPARSEABLE_POLICY,
) -> bool:
    """Main predicate. True if `dep_version_str` falls in any of the SEMVER
    or ECOSYSTEM ranges. GIT ranges are ignored."""
    dep_v = _parse_version(dep_version_str)
    if dep_v is None:
        return policy == UnparseableVersionPolicy.MATCH
    for r in ranges:
        if r.get("type") not in ("SEMVER", "ECOSYSTEM"):
            continue
        events = r.get("events") or []
        if _range_matches(dep_v, events):
            return True
    return False


def run_matcher(
    conn: sqlite3.Connection,
    *,
    policy: UnparseableVersionPolicy = DEFAULT_UNPARSEABLE_POLICY,
) -> int:
    """Scan current deps × advisories, insert new match rows. Returns the
    count of new matches (duplicates are no-ops via the UNIQUE constraint).

    The JOIN restricts to current deps only (project_dep.source_sha ==
    project.last_sha). Stale audit-trail rows from previous walks don't
    fire alerts. UNIQUE (advisory_id, project_dep_id) handles idempotency.

    Opens its own connection (same pattern as ingest_ecosystem, walker —
    avoids transaction races with concurrent writers). Safe to call from
    within asyncio.to_thread."""
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    thread_conn = sqlite3.connect(db_path)
    thread_conn.execute("PRAGMA busy_timeout = 10000")
    try:
        rows = thread_conn.execute(
            """
            SELECT a.id, pd.id, pd.project_id, pd.version, aa.version_range
            FROM advisory a
            JOIN advisory_affects aa ON aa.advisory_id = a.id
            JOIN project_dep pd
                 ON pd.ecosystem = aa.ecosystem
                AND pd.name = aa.name
            JOIN project p ON p.id = pd.project_id
            WHERE pd.source_sha = p.last_sha
            """
        ).fetchall()

        # Pass 1 — evaluate candidates OUTSIDE a write tx. is_affected does
        # JSON parsing + version parsing + range walking for every joined row
        # (200k+ on a populated DB), which is the slow part. Keeping the
        # write-tx open over this window starves walker/ingest of the WAL
        # write lock and breaks their own retry budgets (see walker.py).
        candidates: list[tuple[int, int, int]] = []
        for advisory_id, dep_id, project_id, dep_version, version_range_json in rows:
            try:
                ranges = json.loads(version_range_json)
            except json.JSONDecodeError:
                continue
            if is_affected(dep_version, ranges, policy=policy):
                candidates.append((advisory_id, project_id, dep_id))

        # Pass 2 — batch-commit inserts. Each batch releases the write lock
        # briefly so concurrent walker/ingest writers can slip in.
        inserted = 0
        now = int(time.time())
        batch_size = 500
        for start in range(0, len(candidates), batch_size):
            batch = candidates[start:start + batch_size]
            with thread_conn:
                for advisory_id, project_id, dep_id in batch:
                    cur = thread_conn.execute(
                        """
                        INSERT INTO match (
                            advisory_id, project_id, project_dep_id, first_matched, state
                        ) VALUES (?, ?, ?, ?, 'open')
                        ON CONFLICT (advisory_id, project_dep_id) DO NOTHING
                        """,
                        (advisory_id, project_id, dep_id, now),
                    )
                    if cur.rowcount:
                        inserted += 1
        return inserted
    finally:
        thread_conn.close()
