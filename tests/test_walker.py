"""GitHub manifest walker — fetches lockfiles, parses, populates project_dep.

Tests mock the GitHub REST + raw.githubusercontent.com endpoints via respx.
Real GitHub coverage happens in the smoke script, not the test suite.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx
import pytest
import respx

from agent.db import open_db, upsert_project
from agent.walker import (
    GITHUB_API,
    GITHUB_RAW,
    walk_project,
    walk_all,
)

SCHEMA = Path(__file__).parent.parent / "db" / "schema.sql"
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    return open_db(tmp_path / "state.db", SCHEMA)


def _mock_tip_sha(mock: respx.MockRouter, slug: str, branch: str, sha: str):
    mock.get(f"{GITHUB_API}/repos/{slug}/commits/{branch}").mock(
        return_value=httpx.Response(200, json={"sha": sha})
    )


def _mock_tree(mock: respx.MockRouter, slug: str, sha: str, files: list[str]):
    tree = [
        {"path": f, "type": "blob", "sha": "x"} for f in files
    ]
    mock.get(f"{GITHUB_API}/repos/{slug}/git/trees/{sha}").mock(
        return_value=httpx.Response(200, json={"tree": tree, "truncated": False})
    )


def _mock_file(mock: respx.MockRouter, slug: str, sha: str, path: str, body: str):
    mock.get(f"{GITHUB_RAW}/{slug}/{sha}/{path}").mock(
        return_value=httpx.Response(200, text=body)
    )


@pytest.mark.asyncio
async def test_walk_project_populates_deps_from_cargo_lock(db: sqlite3.Connection):
    pid = upsert_project(
        db, slug="nervosnetwork/ckb", display_name="CKB",
        repo_url="https://github.com/nervosnetwork/ckb",
    )
    cargo_lock = (FIXTURES / "sample.Cargo.lock").read_text()

    with respx.mock() as mock:
        _mock_tip_sha(mock, "nervosnetwork/ckb", "main", "abc123")
        _mock_tree(mock, "nervosnetwork/ckb", "abc123", ["Cargo.lock", "README.md"])
        _mock_file(mock, "nervosnetwork/ckb", "abc123", "Cargo.lock", cargo_lock)

        async with httpx.AsyncClient() as client:
            n = await walk_project(client, db, pid, "nervosnetwork/ckb", "main", None)

    assert n > 0
    rows = db.execute(
        "SELECT ecosystem, name, version FROM project_dep WHERE project_id = ?",
        (pid,),
    ).fetchall()
    assert ("cargo", "serde", "1.0.217") in rows
    # And project.last_sha was updated
    (last_sha,) = db.execute(
        "SELECT last_sha FROM project WHERE id = ?", (pid,)
    ).fetchone()
    assert last_sha == "abc123"


@pytest.mark.asyncio
async def test_walk_project_skips_when_sha_unchanged(db: sqlite3.Connection):
    pid = upsert_project(
        db, slug="x/y", display_name="X", repo_url="u",
    )
    db.execute("UPDATE project SET last_sha = ? WHERE id = ?", ("cached", pid))
    db.commit()

    with respx.mock() as mock:
        _mock_tip_sha(mock, "x/y", "main", "cached")
        # No tree / file calls should be needed.
        async with httpx.AsyncClient() as client:
            n = await walk_project(client, db, pid, "x/y", "main", "cached")

    assert n == 0
    # No project_dep rows written.
    (count,) = db.execute(
        "SELECT COUNT(*) FROM project_dep WHERE project_id = ?", (pid,)
    ).fetchone()
    assert count == 0


@pytest.mark.asyncio
async def test_walk_project_handles_multi_ecosystem_repo(db: sqlite3.Connection):
    """A monorepo with Rust + JS + Go lockfiles in one repo populates deps
    for all three ecosystems under the same project_id."""
    pid = upsert_project(db, slug="x/mono", display_name="Mono", repo_url="u")
    cargo = (FIXTURES / "sample.Cargo.lock").read_text()
    npm = (FIXTURES / "sample.package-lock.json").read_text()
    gosum = (FIXTURES / "sample.go.sum").read_text()

    with respx.mock() as mock:
        _mock_tip_sha(mock, "x/mono", "main", "s1")
        _mock_tree(mock, "x/mono", "s1", [
            "Cargo.lock",
            "frontend/package-lock.json",
            "backend/go.sum",
            "docs/README.md",
        ])
        _mock_file(mock, "x/mono", "s1", "Cargo.lock", cargo)
        _mock_file(mock, "x/mono", "s1", "frontend/package-lock.json", npm)
        _mock_file(mock, "x/mono", "s1", "backend/go.sum", gosum)

        async with httpx.AsyncClient() as client:
            await walk_project(client, db, pid, "x/mono", "main", None)

    ecosystems = {
        eco for (eco,) in db.execute(
            "SELECT DISTINCT ecosystem FROM project_dep WHERE project_id = ?",
            (pid,),
        )
    }
    assert ecosystems == {"cargo", "npm", "Go"}


@pytest.mark.asyncio
async def test_walk_project_tolerates_missing_lockfiles(db: sqlite3.Connection):
    """A project with no recognized lockfiles (docs-only, MDX repo) walks
    to completion without error, writing no deps but updating last_sha."""
    pid = upsert_project(db, slug="x/docs", display_name="Docs", repo_url="u")

    with respx.mock() as mock:
        _mock_tip_sha(mock, "x/docs", "main", "s1")
        _mock_tree(mock, "x/docs", "s1", ["README.md", "LICENSE"])

        async with httpx.AsyncClient() as client:
            n = await walk_project(client, db, pid, "x/docs", "main", None)

    assert n == 0
    (last_sha,) = db.execute(
        "SELECT last_sha FROM project WHERE id = ?", (pid,)
    ).fetchone()
    assert last_sha == "s1"  # advanced even with no deps


@pytest.mark.asyncio
async def test_walk_all_isolates_per_project_failures(db: sqlite3.Connection):
    """One project returning 404 (renamed, private) must not abort the run."""
    good = upsert_project(db, slug="x/good", display_name="G", repo_url="u")
    bad = upsert_project(db, slug="x/gone", display_name="B", repo_url="u")
    cargo = (FIXTURES / "sample.Cargo.lock").read_text()

    with respx.mock() as mock:
        _mock_tip_sha(mock, "x/good", "main", "s1")
        _mock_tree(mock, "x/good", "s1", ["Cargo.lock"])
        _mock_file(mock, "x/good", "s1", "Cargo.lock", cargo)
        mock.get(f"{GITHUB_API}/repos/x/gone/commits/main").mock(
            return_value=httpx.Response(404, json={"message": "Not Found"})
        )

        async with httpx.AsyncClient() as client:
            results = await walk_all(client, db)

    assert isinstance(results["x/good"], int) and results["x/good"] > 0
    assert isinstance(results["x/gone"], Exception)
    (count,) = db.execute(
        "SELECT COUNT(*) FROM project_dep WHERE project_id = ?", (good,)
    ).fetchone()
    assert count > 0


@pytest.mark.asyncio
async def test_walk_sends_auth_header_when_token_configured(db: sqlite3.Connection):
    pid = upsert_project(db, slug="x/y", display_name="X", repo_url="u")
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"sha": "s1"})

    with respx.mock() as mock:
        mock.get(f"{GITHUB_API}/repos/x/y/commits/main").mock(side_effect=handler)
        _mock_tree(mock, "x/y", "s1", [])
        async with httpx.AsyncClient(
            headers={"authorization": "Bearer ghp_testtoken"},
        ) as client:
            await walk_project(client, db, pid, "x/y", "main", None)

    assert captured[0].headers["authorization"] == "Bearer ghp_testtoken"
