"""OSV.dev advisory ingest.

OSV publishes per-ecosystem bulk ZIPs at
  https://osv-vulnerabilities.storage.googleapis.com/<Ecosystem>/all.zip
Each ZIP contains one JSON file per advisory conforming to the OSV schema
(https://ossf.github.io/osv-schema/).

This module parses those records and upserts into the advisory + advisory_affects
tables. The fetcher + scheduler live in ingest.py (Phase 2b).

Schema notes:
  * advisory.severity — lowercase one of: low | medium | high | critical | None
  * advisory.cvss — numeric base score (float) when a CVSS vector is present
  * advisory.raw_json — canonical OSV payload, so matcher changes don't need re-polling
"""
from __future__ import annotations

import asyncio
import io
import json
import re
import sqlite3
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

import httpx
from cvss import CVSS2, CVSS3, CVSS4, CVSSError

CVE_RE = re.compile(r"^CVE-\d{4}-\d+$")

OSV_BASE = "https://osv-vulnerabilities.storage.googleapis.com"


@dataclass(frozen=True)
class ParsedAdvisory:
    source: str
    source_id: str
    summary: str
    details: str
    published: int | None
    modified: int | None
    cve_ids: list[str]
    severity: str | None
    cvss: float | None
    references: list[dict[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class AffectedRow:
    ecosystem: str
    name: str
    version_range: str
    fixed_in: str | None


# ---------- severity normalization ----------

# GHSA `database_specific.severity` is ALL-CAPS, our schema is lowercase.
_GHSA_SEVERITY_MAP = {
    "LOW": "low",
    "MODERATE": "medium",
    "MEDIUM": "medium",
    "HIGH": "high",
    "CRITICAL": "critical",
}


def _cvss_vector_to_score(vector: str) -> float | None:
    """Parse a CVSS vector string and return the base score, or None if the
    vector doesn't parse cleanly. Supports v2, v3.x, and v4.0."""
    try:
        if vector.startswith("CVSS:4"):
            return float(CVSS4(vector).base_score)
        if vector.startswith("CVSS:3"):
            return float(CVSS3(vector).base_score)
        # CVSS v2 vectors have no prefix.
        return float(CVSS2(vector).base_score)
    except (CVSSError, ValueError):
        return None


def normalize_severity(raw: dict[str, Any]) -> tuple[str | None, float | None]:
    """Pick the canonical (severity_label, cvss_score) for an OSV record.

    DESIGN CALL — write your preferred priority order here.

    OSV advisories carry severity info in several places:

    1. `database_specific.severity` (GHSA source only)
         String: "LOW" | "MODERATE" | "HIGH" | "CRITICAL"
         Pros: always present for GHSA records, matches how GitHub categorises.
         Cons: coarse (4 buckets), not present on RUSTSEC records.

    2. `severity[]` list of {type, score}
         `score` is a CVSS vector string (v2, v3.x, or v4.0).
         _cvss_vector_to_score() above converts vector -> numeric 0.0-10.0.
         Pros: numeric precision, cross-source.
         Cons: not always present, needs parsing, v4 still rare.

    3. `affected[].database_specific.cvss` (RUSTSEC source)
         Usually null, sometimes a numeric score.
         Pros: direct.
         Cons: rarely populated.

    Decide the fallback chain. Suggestions:
      (a) CVSS-first: parse vector for cvss; derive label from score buckets
          (<4 low, <7 medium, <9 high, else critical). Uniform label across
          sources but re-buckets GHSA classifications.
      (b) Label-first: use database_specific.severity if present, else bucket
          from CVSS. Preserves GHSA's judgement but means RUSTSEC gets
          severity only if it shipped a CVSS vector.
      (c) Both-independently: label comes from database_specific.severity,
          cvss from the vector. Never derive one from the other. Can have
          (label=high, cvss=None) or (label=None, cvss=8.6). Most honest but
          downstream filters have to cope with the asymmetry.

    Returns (severity, cvss) where severity is one of "low" | "medium" |
    "high" | "critical" | None, and cvss is a float 0.0-10.0 or None.

    Strategy (b) — label-first, fallback to bucketed CVSS. GHSA's label wins
    when present (preserves human review judgement); RUSTSEC records without
    a label still get classified via CVSS score buckets so nothing silently
    falls through the output filter.
    """
    cvss: float | None = None
    for sev in raw.get("severity", []) or []:
        vector = sev.get("score")
        if isinstance(vector, str):
            score = _cvss_vector_to_score(vector)
            if score is not None:
                cvss = score
                break

    label = raw.get("database_specific", {}).get("severity")
    if isinstance(label, str):
        severity = _GHSA_SEVERITY_MAP.get(label.upper())
    elif cvss is not None:
        severity = _bucket_cvss(cvss)
    else:
        severity = None

    return severity, cvss


def _bucket_cvss(score: float) -> str:
    if score < 4.0:
        return "low"
    if score < 7.0:
        return "medium"
    if score < 9.0:
        return "high"
    return "critical"


# ---------- timestamp parsing ----------


def _parse_rfc3339(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        # fromisoformat handles 'Z' in 3.11+; strip it for older, keep tz-aware.
        iso = value.replace("Z", "+00:00")
        return int(datetime.fromisoformat(iso).astimezone(timezone.utc).timestamp())
    except ValueError:
        return None


# ---------- parsing ----------


def parse_osv_record(raw: dict[str, Any]) -> ParsedAdvisory:
    source_id = raw["id"]
    severity, cvss = normalize_severity(raw)
    cve_ids = [a for a in raw.get("aliases", []) if CVE_RE.match(a)]

    return ParsedAdvisory(
        source="osv",
        source_id=source_id,
        summary=raw.get("summary", "") or "",
        details=raw.get("details", "") or "",
        published=_parse_rfc3339(raw.get("published")),
        modified=_parse_rfc3339(raw.get("modified")),
        cve_ids=cve_ids,
        severity=severity,
        cvss=cvss,
        references=list(raw.get("references", []) or []),
    )


def extract_affects(raw: dict[str, Any]) -> list[AffectedRow]:
    rows: list[AffectedRow] = []
    for aff in raw.get("affected", []) or []:
        pkg = aff.get("package", {}) or {}
        name = pkg.get("name")
        ecosystem = pkg.get("ecosystem")
        if not (name and ecosystem):
            continue
        ranges = aff.get("ranges", []) or []
        version_range = json.dumps(ranges, sort_keys=True, separators=(",", ":"))
        fixed_in = _first_fixed(ranges)
        rows.append(
            AffectedRow(
                ecosystem=ecosystem,
                name=name,
                version_range=version_range,
                fixed_in=fixed_in,
            )
        )
    return rows


def _first_fixed(ranges: list[dict[str, Any]]) -> str | None:
    for r in ranges:
        for event in r.get("events", []) or []:
            if "fixed" in event:
                return event["fixed"]
    return None


# ---------- upsert ----------


def upsert_advisory(conn: sqlite3.Connection, raw: dict[str, Any]) -> int:
    """Write one OSV record into advisory + advisory_affects.

    Idempotent on (source, source_id). On conflict, updates fields and
    replaces advisory_affects rows wholesale — source of truth is upstream.

    Does not commit. Callers are responsible for the enclosing transaction —
    see ingest_ecosystem, which wraps a whole ecosystem's upserts in one
    transaction. Per-record commits fsync each record and drop throughput by
    ~100x on small SSDs, which matters on the Zero 3."""
    p = parse_osv_record(raw)
    affects = extract_affects(raw)
    now = int(time.time())
    cve_json = json.dumps(p.cve_ids) if p.cve_ids else None
    refs_json = json.dumps(p.references) if p.references else None
    raw_json = json.dumps(raw, sort_keys=True, separators=(",", ":"))

    cur = conn.execute(
        """
        INSERT INTO advisory (
            source, source_id, published, modified, cve_ids,
            severity, cvss, summary, details, references_json,
            raw_json, ingested_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source, source_id) DO UPDATE SET
            published       = excluded.published,
            modified        = excluded.modified,
            cve_ids         = excluded.cve_ids,
            severity        = excluded.severity,
            cvss            = excluded.cvss,
            summary         = excluded.summary,
            details         = excluded.details,
            references_json = excluded.references_json,
            raw_json        = excluded.raw_json,
            ingested_at     = excluded.ingested_at
        RETURNING id
        """,
        (
            p.source, p.source_id, p.published, p.modified, cve_json,
            p.severity, p.cvss, p.summary, p.details, refs_json,
            raw_json, now,
        ),
    )
    advisory_id = cur.fetchone()[0]

    conn.execute("DELETE FROM advisory_affects WHERE advisory_id = ?", (advisory_id,))
    conn.executemany(
        """
        INSERT INTO advisory_affects (advisory_id, ecosystem, name, version_range, fixed_in)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (advisory_id, a.ecosystem, a.name, a.version_range, a.fixed_in)
            for a in affects
        ],
    )
    return advisory_id


# ---------- fetch ----------


@dataclass(frozen=True)
class FetchResult:
    modified: bool
    etag: str | None
    # Raw zip bytes — deliberately NOT parsed here. Parsing 217k npm
    # records synchronously would block the main event loop for 30-60s
    # on ARM. Callers pass this to a worker thread via asyncio.to_thread
    # and iterate via _iter_zip_json there. None when modified=False.
    zip_bytes: bytes | None

    @property
    def records(self) -> list[dict[str, Any]]:
        """Back-compat for tests and any legacy caller that wanted the
        materialised record list. Parses the zip synchronously on access —
        don't call on the main event loop for large ecosystems."""
        if self.zip_bytes is None:
            return []
        return list(_iter_zip_json(self.zip_bytes))


async def fetch_ecosystem(
    client: httpx.AsyncClient,
    ecosystem: str,
    prev_etag: str | None,
) -> FetchResult:
    """GET the ecosystem bulk ZIP with conditional If-None-Match.

    Returns modified=False on 304 (nothing changed upstream). Raises on any
    non-2xx/304 response — callers decide whether to log-and-continue or abort
    the whole ingest run.

    Deliberately does NOT parse the zip here — that happens inside the
    worker thread in ingest_ecosystem so the main event loop stays
    responsive during large ingests (Task #49)."""
    headers = {"If-None-Match": prev_etag} if prev_etag else {}
    resp = await client.get(
        f"{OSV_BASE}/{ecosystem}/all.zip",
        headers=headers,
        timeout=60.0,
    )
    if resp.status_code == 304:
        return FetchResult(modified=False, etag=prev_etag, zip_bytes=None)
    resp.raise_for_status()
    return FetchResult(
        modified=True,
        etag=resp.headers.get("etag"),
        zip_bytes=resp.content,
    )


def _iter_zip_json(data: bytes) -> Iterable[dict[str, Any]]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in zf.namelist():
            if not name.endswith(".json"):
                continue
            with zf.open(name) as f:
                yield json.load(f)


# ---------- poller_state helpers ----------


def read_poller_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM poller_state WHERE key = ?", (key,)
    ).fetchone()
    return row[0] if row else None


def _write_poller_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Does not commit — caller owns the transaction, same as upsert_advisory."""
    conn.execute(
        """
        INSERT INTO poller_state (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
        """,
        (key, value, int(time.time())),
    )


# ---------- orchestrator ----------


async def ingest_ecosystem(
    conn: sqlite3.Connection,
    client: httpx.AsyncClient,
    ecosystem: str,
) -> int:
    """Fetch + upsert one ecosystem. Returns the number of advisories written
    (0 when upstream returns 304 Not Modified).

    All upserts for the ecosystem + the ETag write happen in a single
    transaction. Either the whole ecosystem lands atomically or nothing
    does — prevents half-written state on crash, and is ~100x faster than
    per-record commits on the Zero 3's SD card.

    The sync upsert loop runs inside asyncio.to_thread so the main event
    loop stays responsive during large ingests (npm's 217k records used
    to block the dashboard HTTP accept for ~20 min on ARM). See task #49.

    The worker thread opens its OWN SQLite connection — separate from the
    main loop's shared conn — so a concurrent walker/matcher write on the
    main thread can't collide with the big ingest transaction. (SQLite
    permits multiple connections to one DB file in WAL mode; each can hold
    its own transaction independently.) Observed symptom before this fix:
    `OperationalError('cannot commit transaction - SQL statements in
    progress')` in the walker when it tried to upsert project_dep rows
    while npm ingest was still running."""
    state_key = f"osv.etag.{ecosystem}"
    prev_etag = read_poller_state(conn, state_key)
    result = await fetch_ecosystem(client, ecosystem, prev_etag)
    if not result.modified:
        return 0

    # Resolve the on-disk path of the main-thread connection so the worker
    # thread can open its own connection to the same file. PRAGMA returns
    # rows (seq, name, file); seq=0 is the main attached database.
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]

    # Parse the zip AND batch-upsert in the worker thread so neither the
    # 30-60s JSON-parse of a large zip (npm) nor the multi-minute upsert
    # loop blocks the event loop. Batches of 1000 give other writers
    # (walker, matcher) windows to grab the file-level write lock.
    BATCH_SIZE = 1000
    zip_bytes = result.zip_bytes  # captured before the closure

    def _apply() -> int:
        thread_conn = sqlite3.connect(db_path)
        # 60s: symmetric with walker/matcher. OSV doesn't usually race
        # itself, but if a large ingest overlaps a concurrent tick (e.g.
        # on restart with two stale ETags) the 10s timeout was too tight.
        thread_conn.execute("PRAGMA busy_timeout = 60000")
        total = 0
        try:
            batch: list[dict[str, Any]] = []
            for raw in _iter_zip_json(zip_bytes or b""):
                batch.append(raw)
                if len(batch) >= BATCH_SIZE:
                    with thread_conn:
                        for r in batch:
                            upsert_advisory(thread_conn, r)
                    total += len(batch)
                    batch = []
            if batch:
                with thread_conn:
                    for r in batch:
                        upsert_advisory(thread_conn, r)
                total += len(batch)
            # ETag in its own tiny transaction, written only after every
            # batch has committed. On a crash mid-ingest the ETag isn't
            # written, so the next run re-fetches and re-upserts
            # idempotently (advisory.source_id UNIQUE).
            if result.etag:
                with thread_conn:
                    _write_poller_state(thread_conn, state_key, result.etag)
        finally:
            thread_conn.close()
        return total

    return await asyncio.to_thread(_apply)


DEFAULT_ECOSYSTEMS = ("crates.io", "npm", "PyPI", "Go", "Maven")


async def ingest_all(
    conn: sqlite3.Connection,
    client: httpx.AsyncClient,
    ecosystems: Iterable[str] = DEFAULT_ECOSYSTEMS,
) -> dict[str, int | Exception]:
    """Run ingest_ecosystem for each ecosystem, isolating failures.

    One ecosystem's fetch failing (network blip, upstream 5xx, malformed zip)
    must not abort the others. Result is a dict mapping ecosystem -> count
    written OR the exception raised. Callers decide whether a single failed
    ecosystem is worth escalating."""
    results: dict[str, int | Exception] = {}
    for eco in ecosystems:
        try:
            results[eco] = await ingest_ecosystem(conn, client, eco)
        except Exception as exc:
            results[eco] = exc
    return results
