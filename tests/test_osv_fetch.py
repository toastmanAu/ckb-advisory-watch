"""OSV bulk-ZIP fetcher tests — respx-mocked httpx.

Covers the conditional-GET path (ETag -> 304), the normal 200 fetch-and-parse,
and the ingest orchestrator that wires fetching into upsert + poller_state.
"""
from __future__ import annotations

import io
import json
import sqlite3
import zipfile
from pathlib import Path

import httpx
import pytest
import respx

from agent.db import open_db
from agent.sources.osv import (
    OSV_BASE,
    fetch_ecosystem,
    ingest_ecosystem,
    read_poller_state,
)

FIXTURES = Path(__file__).parent / "fixtures" / "osv"
SCHEMA = Path(__file__).parent.parent / "db" / "schema.sql"


def _build_zip(names: list[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n in names:
            payload = (FIXTURES / f"{n}.json").read_text()
            zf.writestr(f"{n}.json", payload)
    return buf.getvalue()


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    return open_db(tmp_path / "t.db", SCHEMA)


@pytest.mark.asyncio
async def test_fetch_returns_records_on_200():
    zip_bytes = _build_zip(["GHSA-2226-4v3c-cff8", "RUSTSEC-2024-0001"])
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{OSV_BASE}/crates.io/all.zip").mock(
            return_value=httpx.Response(
                200, content=zip_bytes, headers={"etag": '"abc123"'}
            )
        )
        async with httpx.AsyncClient() as client:
            result = await fetch_ecosystem(client, "crates.io", prev_etag=None)

    assert result.modified is True
    assert result.etag == '"abc123"'
    ids = sorted(r["id"] for r in result.records)
    assert ids == ["GHSA-2226-4v3c-cff8", "RUSTSEC-2024-0001"]


@pytest.mark.asyncio
async def test_fetch_sends_if_none_match_and_handles_304():
    route_calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        route_calls.append(request)
        return httpx.Response(304)

    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{OSV_BASE}/crates.io/all.zip").mock(side_effect=handler)
        async with httpx.AsyncClient() as client:
            result = await fetch_ecosystem(
                client, "crates.io", prev_etag='"abc123"'
            )

    assert result.modified is False
    assert result.etag == '"abc123"'
    assert result.records == []
    assert route_calls[0].headers["if-none-match"] == '"abc123"'


@pytest.mark.asyncio
async def test_fetch_raises_on_server_error():
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{OSV_BASE}/crates.io/all.zip").mock(
            return_value=httpx.Response(500, text="boom")
        )
        async with httpx.AsyncClient() as client:
            with pytest.raises(httpx.HTTPStatusError):
                await fetch_ecosystem(client, "crates.io", prev_etag=None)


@pytest.mark.asyncio
async def test_ingest_stores_etag_and_upserts_records(db: sqlite3.Connection):
    zip_bytes = _build_zip(["GHSA-275g-g844-73jh", "GHSA-2226-4v3c-cff8"])
    with respx.mock() as mock:
        mock.get(f"{OSV_BASE}/crates.io/all.zip").mock(
            return_value=httpx.Response(
                200, content=zip_bytes, headers={"etag": '"e1"'}
            )
        )
        async with httpx.AsyncClient() as client:
            n = await ingest_ecosystem(db, client, "crates.io")

    assert n == 2
    (adv_count,) = db.execute("SELECT COUNT(*) FROM advisory").fetchone()
    assert adv_count == 2
    assert read_poller_state(db, "osv.etag.crates.io") == '"e1"'


@pytest.mark.asyncio
async def test_ingest_skips_when_not_modified(db: sqlite3.Connection):
    # Prime the poller_state as if we'd fetched before.
    from agent.sources.osv import _write_poller_state
    _write_poller_state(db, "osv.etag.crates.io", '"cached"')

    with respx.mock() as mock:
        mock.get(f"{OSV_BASE}/crates.io/all.zip").mock(
            return_value=httpx.Response(304)
        )
        async with httpx.AsyncClient() as client:
            n = await ingest_ecosystem(db, client, "crates.io")

    assert n == 0
    (adv_count,) = db.execute("SELECT COUNT(*) FROM advisory").fetchone()
    assert adv_count == 0


@pytest.mark.asyncio
async def test_ingest_second_run_is_conditional(db: sqlite3.Connection):
    """First run stores the etag; second run sends If-None-Match and, on 304,
    leaves the advisory table untouched."""
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        if request.headers.get("if-none-match") == '"e1"':
            return httpx.Response(304)
        zip_bytes = _build_zip(["GHSA-2226-4v3c-cff8"])
        return httpx.Response(200, content=zip_bytes, headers={"etag": '"e1"'})

    with respx.mock() as mock:
        mock.get(f"{OSV_BASE}/crates.io/all.zip").mock(side_effect=handler)
        async with httpx.AsyncClient() as client:
            n1 = await ingest_ecosystem(db, client, "crates.io")
            n2 = await ingest_ecosystem(db, client, "crates.io")

    assert n1 == 1 and n2 == 0
    assert len(requests_seen) == 2
    assert requests_seen[1].headers["if-none-match"] == '"e1"'


@pytest.mark.asyncio
async def test_ingest_all_isolates_failures(db: sqlite3.Connection):
    """One ecosystem returning 500 must not abort the others."""
    from agent.sources.osv import ingest_all

    zip_ok = _build_zip(["GHSA-2226-4v3c-cff8"])
    with respx.mock() as mock:
        mock.get(f"{OSV_BASE}/crates.io/all.zip").mock(
            return_value=httpx.Response(200, content=zip_ok, headers={"etag": '"c"'})
        )
        mock.get(f"{OSV_BASE}/npm/all.zip").mock(
            return_value=httpx.Response(500)
        )
        mock.get(f"{OSV_BASE}/PyPI/all.zip").mock(
            return_value=httpx.Response(200, content=zip_ok, headers={"etag": '"p"'})
        )
        async with httpx.AsyncClient() as client:
            results = await ingest_all(db, client, ["crates.io", "npm", "PyPI"])

    assert results["crates.io"] == 1
    assert isinstance(results["npm"], httpx.HTTPStatusError)
    assert results["PyPI"] == 1
    (count,) = db.execute("SELECT COUNT(*) FROM advisory").fetchone()
    assert count == 1  # same advisory upserted from both crates.io and PyPI zips
