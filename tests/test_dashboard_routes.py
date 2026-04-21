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


async def test_project_page_shows_matches(tmp_path, share_config):
    async with await _client(tmp_path, share_config) as client:
        resp = await client.get("/p/a/b")
        assert resp.status == 200
        body = await resp.text()
    assert "GHSA-crit" in body
    assert "libgit2-sys" in body


async def test_project_page_404_for_unknown(tmp_path, share_config):
    async with await _client(tmp_path, share_config) as client:
        resp = await client.get("/p/does/notexist")
    assert resp.status == 404


async def test_project_page_filters_by_severity_query_param(tmp_path, share_config):
    # _client creates the DB + applies schema + seeds the GHSA-crit match.
    # We add a second (low-severity) match after that, via a fresh connection
    # to the same file, then exercise the filter.
    async with await _client(tmp_path, share_config) as client:
        import sqlite3
        from tests.dashboard_fixtures import seed_match
        conn = sqlite3.connect(str(tmp_path / "state.db"))
        seed_match(conn, project_slug="a/b", source_id="GHSA-low-extra",
                   severity="low", cvss=3.0, dep_name="other-pkg")
        conn.close()

        r1 = await client.get("/p/a/b")
        b1 = await r1.text()
        r2 = await client.get("/p/a/b?severity=critical")
        b2 = await r2.text()
    assert "GHSA-crit" in b1 and "GHSA-low-extra" in b1
    assert "GHSA-crit" in b2 and "GHSA-low-extra" not in b2


async def test_advisory_page_shows_affected_projects(tmp_path, share_config):
    async with await _client(tmp_path, share_config) as client:
        resp = await client.get("/a/GHSA-crit")
        assert resp.status == 200
        body = await resp.text()
    assert "GHSA-crit" in body
    assert "a/b" in body  # project slug appears
    assert "libgit2-sys" in body


async def test_advisory_page_404_for_unknown(tmp_path, share_config):
    async with await _client(tmp_path, share_config) as client:
        resp = await client.get("/a/GHSA-not-real")
    assert resp.status == 404


async def test_share_match_post_sends_and_redirects(tmp_path, share_config, monkeypatch):
    sent_payloads = []
    def fake_send(payload, cfg):
        sent_payloads.append(payload)
    monkeypatch.setattr("agent.dashboard.share.send_email", fake_send)

    async with await _client(tmp_path, share_config) as client:
        # Find the match id via the index page
        resp = await client.post("/share/match/1", allow_redirects=False)
        assert resp.status == 303
        assert resp.headers["Location"].startswith("/") and "sent=1" in resp.headers["Location"]
    assert len(sent_payloads) == 1
    p = sent_payloads[0]
    assert "GHSA-crit" in p.subject
    assert "libgit2-sys" in p.subject


async def test_share_advisory_post_sends_and_redirects(tmp_path, share_config, monkeypatch):
    sent_payloads = []
    monkeypatch.setattr("agent.dashboard.share.send_email",
                        lambda payload, cfg: sent_payloads.append(payload))

    async with await _client(tmp_path, share_config) as client:
        resp = await client.post("/share/advisory/GHSA-crit", allow_redirects=False)
        assert resp.status == 303
        assert resp.headers["Location"] == "/a/GHSA-crit?sent=1"
    assert len(sent_payloads) == 1
    assert "GHSA-crit" in sent_payloads[0].subject


async def test_share_match_post_propagates_smtp_error_as_query_param(tmp_path, share_config, monkeypatch):
    import smtplib
    def boom(payload, cfg):
        raise smtplib.SMTPAuthenticationError(535, b"auth")
    monkeypatch.setattr("agent.dashboard.share.send_email", boom)

    async with await _client(tmp_path, share_config) as client:
        resp = await client.post("/share/match/1", allow_redirects=False)
    assert resp.status == 303
    assert "sent_error=" in resp.headers["Location"]


async def test_index_share_button_is_post_form_in_private_mode(tmp_path, share_config):
    async with await _client(tmp_path, share_config) as client:
        resp = await client.get("/")
        body = await resp.text()
    # Private dashboard MUST still render the POST form (not a mailto: anchor).
    assert 'action="/share/match/' in body
    assert "mailto:" not in body


async def test_advisory_share_button_is_post_form_in_private_mode(tmp_path, share_config):
    async with await _client(tmp_path, share_config) as client:
        resp = await client.get("/a/GHSA-crit")
        body = await resp.text()
    assert 'action="/share/advisory/GHSA-crit"' in body
    assert "mailto:" not in body


async def test_project_share_button_is_post_form_in_private_mode(tmp_path, share_config):
    async with await _client(tmp_path, share_config) as client:
        resp = await client.get("/p/a/b")
        body = await resp.text()
    assert 'action="/share/match/' in body
    assert "mailto:" not in body
