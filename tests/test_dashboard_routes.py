"""aiohttp TestClient-based route tests."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from agent.dashboard.server import build_app
from agent.dashboard.share import ShareConfig
from tests.dashboard_fixtures import fresh_db, seed_match


@pytest.fixture
def share_config():
    return ShareConfig(
        recipient="p@x",
        sender="p@x",
        smtp_host="smtp.gmail.com",
        smtp_port=465,
        smtp_user="p@x",
        smtp_password="pw",
        dashboard_base_url="http://test",
    )


async def _client(tmp_path, share_config):
    # the connection factory hands out read-only connections
    db_path = tmp_path / "state.db"
    conn = fresh_db(tmp_path)  # creates file + schema, returns writer connection
    # seed before wrapping so the handlers see data
    seed_match(conn, project_slug="a/b", source_id="GHSA-crit",
               severity="critical", cvss=9.8)
    conn.close()

    import sqlite3
    def conn_factory() -> sqlite3.Connection:
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    app = build_app(conn_factory=conn_factory, share_config=share_config,
                    hostname="test-host")
    return TestClient(TestServer(app))


async def test_index_returns_200_with_kpi_and_triage(tmp_path, share_config):
    async with await _client(tmp_path, share_config) as client:
        resp = await client.get("/")
        assert resp.status == 200
        body = await resp.text()
    assert "GHSA-crit" in body
    # KPI strip shows "1" for critical bucket
    assert ">1<" in body
    assert "critical" in body.lower()


async def test_index_serves_html_content_type(tmp_path, share_config):
    async with await _client(tmp_path, share_config) as client:
        resp = await client.get("/")
    assert resp.headers["content-type"].startswith("text/html")


async def test_favicon_served(tmp_path, share_config):
    async with await _client(tmp_path, share_config) as client:
        resp = await client.get("/static/favicon.png")
        assert resp.status == 200
        assert resp.headers["content-type"] == "image/png"
