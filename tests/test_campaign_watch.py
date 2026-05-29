"""Unit and integration tests for campaign watch scanning and alerting."""
from __future__ import annotations

import sqlite3
from pathlib import Path
import pytest
import httpx
import respx
import yaml

from agent.campaign_watch import (
    load_campaigns, scan, to_markdown, run_campaign_check, Campaign, Indicator, Match
)
from tests.dashboard_fixtures import fresh_db


def seed_project_and_dep(conn: sqlite3.Connection, slug: str, display_name: str, ecosystem: str, name: str, version: str, is_direct: int = 1) -> int:
    now = 1700000000
    cur = conn.execute(
        "INSERT INTO project (slug, display_name, repo_url, added_at) VALUES (?, ?, ?, ?)",
        (slug, display_name, f"https://github.com/{slug}", now)
    )
    project_id = cur.lastrowid
    conn.execute(
        "INSERT INTO project_dep (project_id, ecosystem, name, version, is_direct, source_sha, first_seen, last_seen) "
        "VALUES (?, ?, ?, ?, ?, 'sha123', ?, ?)",
        (project_id, ecosystem, name, version, is_direct, now, now)
    )
    conn.commit()
    return project_id


def test_campaign_watch_scan(tmp_path):
    conn = fresh_db(tmp_path)
    seed_project_and_dep(conn, "test/repo", "Test Repo", "npm", "@tanstack/router", "1.169.8")
    seed_project_and_dep(conn, "test/repo-safe", "Safe Repo", "npm", "safe-pkg", "1.0.0")

    campaigns = [
        Campaign(
            id="shai-hulud",
            title="TanStack compromise",
            first_seen="2026-05-11",
            references=("https://ref",),
            indicators=(Indicator(ecosystem="npm", pattern="@tanstack/*"),),
            affected_versions="Compromised router.",
        )
    ]

    matches = scan(conn, campaigns)
    assert len(matches) == 1
    assert matches[0].project_slug == "test/repo"
    assert matches[0].dep_name == "@tanstack/router"
    assert matches[0].dep_version == "1.169.8"
    assert matches[0].is_direct is True


def test_campaign_watch_to_markdown():
    campaigns = [
        Campaign(
            id="shai-hulud",
            title="TanStack compromise",
            first_seen="2026-05-11",
            references=("https://ref",),
            indicators=(Indicator(ecosystem="npm", pattern="@tanstack/*"),),
            affected_versions="Compromised router.",
        )
    ]
    matches = [
        Match(
            campaign=campaigns[0],
            indicator=campaigns[0].indicators[0],
            project_slug="test/repo",
            project_name="Test Repo",
            dep_name="@tanstack/router",
            dep_version="1.169.8",
            is_direct=True,
        )
    ]
    md = to_markdown(campaigns, matches)
    assert "# Supply-chain campaign cross-ref" in md
    assert "TanStack compromise" in md
    assert "test/repo" in md
    assert "@tanstack/router" in md


@pytest.mark.asyncio
async def test_run_campaign_check_flow(tmp_path):
    conn = fresh_db(tmp_path)
    seed_project_and_dep(conn, "test/repo", "Test Repo", "npm", "defi-threat-scanner", "1.0.0")

    # Create watchlist yaml file inside tmp_path
    watchlist_content = {
        "campaigns": [
            {
                "id": "trapdoor",
                "title": "TrapDoor Campaign",
                "first_seen": "2026-05-22",
                "references": ["https://ref"],
                "indicators": [
                    {"ecosystem": "npm", "name": "defi-threat-scanner"}
                ],
                "affected_versions": "Malicious scanner"
            }
        ]
    }
    watchlist_path = tmp_path / "watchlist.yaml"
    watchlist_path.write_text(yaml.dump(watchlist_content), encoding="utf-8")

    # Mock config
    config = {
        "agent": {
            "watchlist_path": str(watchlist_path),
            "data_dir": str(tmp_path)
        },
        "outputs": {
            "telegram": {
                "enabled": True,
                "bot_token": "TEST_TOKEN",
                "chat_id": "999",
                "channel_id": ""
            }
        },
        "dashboard": {
            "base_url": "http://localhost:8080"
        }
    }

    # Mock telegram send message using respx
    API = "https://api.telegram.org"
    with respx.mock() as mock:
        route = mock.post(f"{API}/botTEST_TOKEN/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 12345}})
        )

        # Run campaign check
        await run_campaign_check(conn, config)

        assert route.call_count == 1

        # Verify markdown report file was written to data_dir (tmp_path)
        # In run_campaign_check, it writes to REPO_ROOT / data_dir / "campaign-report.md".
        # But wait! If data_dir is absolute (which it is here), REPO_ROOT / data_dir resolves to the absolute path!
        # Under Python Path rules: Path("/root") / Path("/tmp/foo") -> Path("/tmp/foo")
        report_path = Path(tmp_path) / "campaign-report.md"
        assert report_path.exists()
        assert "TrapDoor Campaign" in report_path.read_text(encoding="utf-8")

        # Verify poller_state table has emission key
        row = conn.execute(
            "SELECT value FROM poller_state WHERE key LIKE 'campaign.emitted.trapdoor%'"
        ).fetchone()
        assert row is not None
        assert row[0] == "1"

        # Run again - should NOT call route again (already emitted)
        await run_campaign_check(conn, config)
        assert route.call_count == 1
