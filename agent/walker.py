"""GitHub manifest walker.

For each seeded project:
  1. Ask GitHub for the tip commit SHA on the default branch.
  2. If unchanged since last walk, skip (projects can easily go weeks without
     a new commit — this saves API quota and Pi cycles).
  3. Otherwise, list the repo tree at tip, pick out every file whose basename
     matches a known lockfile (Cargo.lock, package-lock.json, go.sum).
  4. Fetch each lockfile verbatim from raw.githubusercontent.com (unmetered
     for public repos; no base64 round-trip), dispatch to the right parser.
  5. Upsert into project_dep under one transaction, stamp project.last_sha.

Rate-limit aware: uses the GITHUB_TOKEN in the Authorization header when the
client carries one, giving 5000 req/hour instead of 60.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from pathlib import PurePosixPath
from typing import Callable, Iterable

import httpx

from agent.db import upsert_project_dep
from agent.parsers.cargo import parse_cargo_lock
from agent.parsers.go_sum import parse_go_sum
from agent.parsers.npm import parse_package_lock
from agent.parsers.pnpm import parse_pnpm_lock

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
GITHUB_RAW = "https://raw.githubusercontent.com"

# Map basename -> (OSV ecosystem tag, parser function).
# Tags MUST match what OSV uses in advisory_affects.ecosystem so the
# matcher's JOIN is trivial. Basename dispatch handles monorepo paths
# like crates/foo/Cargo.lock naturally.
LOCKFILE_PARSERS: dict[str, tuple[str, Callable[[str], list[tuple[str, str]]]]] = {
    "Cargo.lock": ("crates.io", parse_cargo_lock),
    "package-lock.json": ("npm", parse_package_lock),
    "pnpm-lock.yaml": ("npm", parse_pnpm_lock),
    "go.sum": ("Go", parse_go_sum),
}


async def _tip_sha(
    client: httpx.AsyncClient, slug: str, branch: str
) -> tuple[str, str]:
    """Return (actual_branch, tip_sha).

    Falls back to repo metadata when the stored branch 404s — the seed
    defaulted everything to `main`, but many CKB repos use `master` or
    `develop`. This keeps the seed maintenance-free and self-correcting."""
    r = await client.get(
        f"{GITHUB_API}/repos/{slug}/commits/{branch}",
        timeout=30.0,
    )
    # 404 = repo missing / private; 422 = repo exists but branch doesn't.
    # Both are worth a metadata retry before giving up.
    if r.status_code in (404, 422):
        meta = await client.get(f"{GITHUB_API}/repos/{slug}", timeout=30.0)
        meta.raise_for_status()
        real_branch = meta.json()["default_branch"]
        if real_branch == branch:
            r.raise_for_status()  # same branch as tried — re-raise original
        r = await client.get(
            f"{GITHUB_API}/repos/{slug}/commits/{real_branch}", timeout=30.0,
        )
        r.raise_for_status()
        return real_branch, r.json()["sha"]
    r.raise_for_status()
    return branch, r.json()["sha"]


async def _list_tree(
    client: httpx.AsyncClient, slug: str, sha: str
) -> list[str]:
    r = await client.get(
        f"{GITHUB_API}/repos/{slug}/git/trees/{sha}",
        params={"recursive": "1"},
        timeout=30.0,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("truncated"):
        log.warning(
            "tree truncated for %s@%s — some lockfiles may be missed", slug, sha
        )
    return [
        item["path"]
        for item in data.get("tree", [])
        if item.get("type") == "blob"
    ]


async def _fetch_file(
    client: httpx.AsyncClient, slug: str, sha: str, path: str
) -> str:
    # raw.githubusercontent.com does not require auth for public repos and
    # skips the 1 MB base64 limit of the contents API — important for
    # package-lock.json files, which routinely exceed that.
    r = await client.get(
        f"{GITHUB_RAW}/{slug}/{sha}/{path}",
        timeout=60.0,
    )
    r.raise_for_status()
    return r.text


def _find_lockfiles(paths: Iterable[str]) -> list[tuple[str, str]]:
    """Return (path, basename) for every tree entry whose basename is a known
    lockfile. Basename dispatch naturally handles nested lockfiles in
    monorepos (frontend/package-lock.json, backend/go.sum, crates/*/Cargo.lock)."""
    matches: list[tuple[str, str]] = []
    for p in paths:
        name = PurePosixPath(p).name
        if name in LOCKFILE_PARSERS:
            matches.append((p, name))
    return matches


async def walk_project(
    client: httpx.AsyncClient,
    conn: sqlite3.Connection,
    project_id: int,
    slug: str,
    default_branch: str,
    last_sha: str | None,
) -> int:
    """Walk one project. Returns the number of (ecosystem, name, version)
    rows written. Zero means either unchanged or no recognized lockfiles.

    All network I/O happens before the DB transaction opens — asyncio could
    otherwise yield to another coroutine mid-transaction, accidentally
    bundling writes from different work units into the same COMMIT and
    rolling them back together on one side's error."""
    actual_branch, new_sha = await _tip_sha(client, slug, default_branch)
    if new_sha == last_sha:
        if actual_branch != default_branch:
            # Discovered a branch rename — write back so subsequent walks
            # skip the fallback round-trip.
            with conn:
                conn.execute(
                    "UPDATE project SET default_branch = ? WHERE id = ?",
                    (actual_branch, project_id),
                )
        return 0

    paths = await _list_tree(client, slug, new_sha)
    lockfiles = _find_lockfiles(paths)

    # Collect raw lockfile bodies on the main loop via async httpx. Parsing
    # and writing happen together in a worker thread below so neither
    # large-YAML parsing (pnpm) nor large-JSON parsing (npm) block the
    # event loop. Per Task #49: every sync CPU- or disk-bound step over
    # big collections should live in asyncio.to_thread.
    bodies: list[tuple[str, Callable, str, str]] = []  # (ecosystem, parser, lockfile_path, body)
    for path, basename in lockfiles:
        ecosystem, parser = LOCKFILE_PARSERS[basename]
        try:
            body = await _fetch_file(client, slug, new_sha, path)
        except Exception as exc:
            log.warning("%s: fetch failed for %s: %r", slug, path, exc)
            continue
        bodies.append((ecosystem, parser, path, body))

    db_path = conn.execute("PRAGMA database_list").fetchone()[2]

    def _apply() -> int:
        thread_conn = sqlite3.connect(db_path)
        # 60s: must span a full OSV ingest batch (up to ~10s hold) plus
        # any inter-batch gap. 10s was too tight — walker raced every
        # batch and missed the ~0.5s gap most of the time during npm
        # re-ingest, leaving project rows stuck with NULL last_sha.
        thread_conn.execute("PRAGMA busy_timeout = 60000")
        total_local = 0
        try:
            with thread_conn:
                for ecosystem, parser, path, body in bodies:
                    try:
                        deps = parser(body)
                    except Exception as exc:
                        log.warning("%s: parse failed for %s: %r", slug, path, exc)
                        continue
                    for name, version in deps:
                        upsert_project_dep(
                            thread_conn,
                            project_id=project_id,
                            ecosystem=ecosystem,
                            name=name,
                            version=version,
                            source_sha=new_sha,
                        )
                        total_local += 1
                thread_conn.execute(
                    "UPDATE project SET last_sha = ?, last_checked = ?, "
                    "default_branch = ? WHERE id = ?",
                    (new_sha, int(time.time()), actual_branch, project_id),
                )
        finally:
            thread_conn.close()
        return total_local

    # Retry on transient "database is locked" — when an OSV ingest batch
    # runs longer than the 10s busy_timeout (can happen on ARM for big
    # npm batches), walker's commit fails. A short backoff retry clears
    # the spurious failure without making the operator wait for the next
    # daily tick. Three attempts covers the worst observed gap (~30s).
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            return await asyncio.to_thread(_apply)
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower():
                raise
            last_err = exc
            await asyncio.sleep(5 * (attempt + 1))
    assert last_err is not None
    raise last_err


async def walk_all(
    client: httpx.AsyncClient,
    conn: sqlite3.Connection,
) -> dict[str, int | Exception]:
    """Walk every project in the DB, isolating per-project failures.

    Returns slug -> count-written OR the raised exception. One repo
    disappearing (rename, deletion, going private) must not abort the run."""
    rows = conn.execute(
        "SELECT id, slug, default_branch, last_sha FROM project"
    ).fetchall()
    results: dict[str, int | Exception] = {}
    for project_id, slug, branch, last_sha in rows:
        try:
            results[slug] = await walk_project(
                client, conn, project_id, slug, branch, last_sha
            )
        except Exception as exc:
            results[slug] = exc
    return results
