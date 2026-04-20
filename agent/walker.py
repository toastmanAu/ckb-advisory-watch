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

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
GITHUB_RAW = "https://raw.githubusercontent.com"

# Map basename -> (ecosystem tag stored in project_dep, parser function).
# Basename dispatch handles monorepo paths like packages/foo/Cargo.lock.
LOCKFILE_PARSERS: dict[str, tuple[str, Callable[[str], list[tuple[str, str]]]]] = {
    "Cargo.lock": ("cargo", parse_cargo_lock),
    "package-lock.json": ("npm", parse_package_lock),
    "go.sum": ("Go", parse_go_sum),
}


async def _tip_sha(
    client: httpx.AsyncClient, slug: str, branch: str
) -> str:
    r = await client.get(
        f"{GITHUB_API}/repos/{slug}/commits/{branch}",
        timeout=30.0,
    )
    r.raise_for_status()
    return r.json()["sha"]


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
    rows written. Zero means either unchanged or no recognized lockfiles."""
    new_sha = await _tip_sha(client, slug, default_branch)
    if new_sha == last_sha:
        return 0

    paths = await _list_tree(client, slug, new_sha)
    lockfiles = _find_lockfiles(paths)

    total = 0
    with conn:
        for path, basename in lockfiles:
            ecosystem, parser = LOCKFILE_PARSERS[basename]
            try:
                body = await _fetch_file(client, slug, new_sha, path)
                deps = parser(body)
            except Exception as exc:
                log.warning("%s: parse failed for %s: %r", slug, path, exc)
                continue
            for name, version in deps:
                upsert_project_dep(
                    conn,
                    project_id=project_id,
                    ecosystem=ecosystem,
                    name=name,
                    version=version,
                    source_sha=new_sha,
                )
                total += 1
        conn.execute(
            "UPDATE project SET last_sha = ?, last_checked = ? WHERE id = ?",
            (new_sha, int(time.time()), project_id),
        )

    return total


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
