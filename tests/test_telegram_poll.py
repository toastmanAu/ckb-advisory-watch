"""Integration tests for the telegram poll loop."""
from __future__ import annotations

import sqlite3

import pytest

from agent.output.telegram import (
    SUBCH_DM, SUBCH_CHANNEL, SEVERITY_LEVEL,
    _unemitted_advisories_above, baseline_if_first_run,
)
from tests.dashboard_fixtures import fresh_db, seed_match


def test_unemitted_finds_matches_above_threshold(tmp_path):
    conn = fresh_db(tmp_path)
    seed_match(conn, project_slug="a/b", source_id="GHSA-c", severity="critical", cvss=9.8)
    seed_match(conn, project_slug="a/b", source_id="GHSA-l", severity="low", cvss=2.0, dep_name="pkg2")
    rows = _unemitted_advisories_above(conn, SUBCH_DM, SEVERITY_LEVEL["medium"])
    source_ids = {r[1] for r in rows}
    assert "GHSA-c" in source_ids
    assert "GHSA-l" not in source_ids


def test_unemitted_excludes_already_emitted_on_same_subchannel(tmp_path):
    conn = fresh_db(tmp_path)
    _, _, match_id = seed_match(conn, source_id="GHSA-x", severity="critical")
    # Simulate already-emitted on telegram.dm
    conn.execute(
        "INSERT INTO emission (match_id, channel, emitted_at, artifact_path) "
        "VALUES (?, ?, strftime('%s','now'), '42')",
        (match_id, SUBCH_DM),
    )
    conn.commit()
    rows_dm = _unemitted_advisories_above(conn, SUBCH_DM, SEVERITY_LEVEL["medium"])
    assert not any(r[1] == "GHSA-x" for r in rows_dm)
    # Other sub-channel still unemitted
    rows_ch = _unemitted_advisories_above(conn, SUBCH_CHANNEL, SEVERITY_LEVEL["medium"])
    assert any(r[1] == "GHSA-x" for r in rows_ch)


def test_unemitted_returns_distinct_advisory_per_row(tmp_path):
    conn = fresh_db(tmp_path)
    seed_match(conn, project_slug="a/one", source_id="GHSA-shared", severity="critical", dep_name="lib")
    seed_match(conn, project_slug="a/two", source_id="GHSA-shared", severity="critical", dep_name="lib")
    rows = _unemitted_advisories_above(conn, SUBCH_DM, SEVERITY_LEVEL["medium"])
    # One advisory row even though two matches
    assert len([r for r in rows if r[1] == "GHSA-shared"]) == 1


def test_baseline_first_run_inserts_emissions_above_threshold(tmp_path):
    conn = fresh_db(tmp_path)
    seed_match(conn, project_slug="a/b", source_id="GHSA-c", severity="critical", cvss=9.8)
    seed_match(conn, project_slug="a/b", source_id="GHSA-l", severity="low", cvss=2.0, dep_name="pkg2")
    inserted = baseline_if_first_run(conn, SUBCH_DM, SEVERITY_LEVEL["medium"])
    assert inserted == 1  # only critical above medium threshold
    # emission row for the critical match
    rows = conn.execute(
        "SELECT COUNT(*) FROM emission WHERE channel = ? AND artifact_path = 'baseline'",
        (SUBCH_DM,),
    ).fetchone()
    assert rows[0] == 1
    # poller_state key set
    key_row = conn.execute(
        "SELECT value FROM poller_state WHERE key = ?",
        (f"telegram.baseline_done.{SUBCH_DM}",),
    ).fetchone()
    assert key_row[0] == "1"


def test_baseline_second_run_is_noop(tmp_path):
    conn = fresh_db(tmp_path)
    seed_match(conn, source_id="GHSA-c", severity="critical")
    first = baseline_if_first_run(conn, SUBCH_DM, SEVERITY_LEVEL["medium"])
    second = baseline_if_first_run(conn, SUBCH_DM, SEVERITY_LEVEL["medium"])
    assert first == 1
    assert second == 0
    rows = conn.execute(
        "SELECT COUNT(*) FROM emission WHERE channel = ?", (SUBCH_DM,),
    ).fetchone()
    assert rows[0] == 1  # not doubled


def test_baseline_per_subchannel_independent(tmp_path):
    conn = fresh_db(tmp_path)
    seed_match(conn, source_id="GHSA-c", severity="critical")
    n_dm = baseline_if_first_run(conn, SUBCH_DM, SEVERITY_LEVEL["medium"])
    n_ch = baseline_if_first_run(conn, SUBCH_CHANNEL, SEVERITY_LEVEL["medium"])
    assert n_dm == 1 and n_ch == 1
    # Two emission rows, one per sub-channel
    total = conn.execute(
        "SELECT COUNT(*) FROM emission WHERE artifact_path = 'baseline'"
    ).fetchone()[0]
    assert total == 2


import asyncio
import json as _json
from unittest.mock import AsyncMock

import httpx
import respx

from agent.output.telegram import telegram_poll_loop
from tests.telegram_fixtures import make_config


API = "https://api.telegram.org"


def _enable_dm_config(tmp_path, severity="medium"):
    return {
        "outputs": {"telegram": {
            "enabled": True, "bot_token": "TOKEN",
            "chat_id": "123", "channel_id": "",
            "min_severity": severity,
        }},
        "dashboard": {"base_url": "http://t"},
        "poll": {"telegram": 0},  # 0 so the loop's sleep is instant for tests
        "agent": {"data_dir": str(tmp_path)},
    }


async def _one_tick(conn, config):
    """Run one iteration of the loop and return once it hits the sleep."""
    stop = asyncio.Event()

    async def stop_after(delay: float):
        await asyncio.sleep(delay)
        stop.set()

    # Kick off the stopper to fire shortly after work completes.
    task = asyncio.create_task(stop_after(0.5))
    await telegram_poll_loop(conn, config, stop)
    await task


@pytest.mark.asyncio
async def test_loop_sends_for_new_unemitted_critical(tmp_path):
    conn = fresh_db(tmp_path)
    # First tick: empty DB — baseline is a no-op, no sends.
    await _one_tick(conn, _enable_dm_config(tmp_path))
    # Now seed the match — it arrives AFTER baseline, so it must be sent.
    seed_match(conn, project_slug="a/b", source_id="GHSA-crit",
               severity="critical", cvss=9.8)
    with respx.mock() as mock:
        route = mock.post(f"{API}/botTOKEN/sendMessage").mock(
            return_value=httpx.Response(200, json={
                "ok": True, "result": {"message_id": 555},
            })
        )
        await _one_tick(conn, _enable_dm_config(tmp_path))
    assert route.call_count >= 1
    # emission row written with the message_id
    rows = conn.execute(
        "SELECT artifact_path FROM emission WHERE channel = ?", (SUBCH_DM,),
    ).fetchall()
    assert ("555",) in rows or any(r[0] == "555" for r in rows)


@pytest.mark.asyncio
async def test_loop_groups_multiple_matches_per_advisory_into_one_send(tmp_path):
    """Two matches of the same advisory (different projects) -> ONE sendMessage
    call, TWO telegram.dm emission rows both carrying the same message_id.

    Burn baseline first (no matches exist yet -> baseline is a no-op),
    then seed the grouped matches and run the second tick."""
    conn = fresh_db(tmp_path)
    # First tick: nothing to baseline, no sends.
    await _one_tick(conn, _enable_dm_config(tmp_path))

    # Two matches of the SAME advisory, different projects.
    seed_match(conn, project_slug="a/one", source_id="GHSA-shared",
               severity="critical", dep_name="lib", dep_version="1.0")
    seed_match(conn, project_slug="a/two", source_id="GHSA-shared",
               severity="critical", dep_name="lib", dep_version="1.0")
    with respx.mock() as mock:
        route = mock.post(f"{API}/botTOKEN/sendMessage").mock(
            return_value=httpx.Response(200, json={
                "ok": True, "result": {"message_id": 777},
            })
        )
        await _one_tick(conn, _enable_dm_config(tmp_path))
    assert route.call_count == 1
    rows = conn.execute(
        """
        SELECT e.artifact_path FROM emission e
        JOIN match m ON m.id = e.match_id
        JOIN advisory a ON a.id = m.advisory_id
        WHERE a.source_id = 'GHSA-shared' AND e.channel = ?
        """,
        (SUBCH_DM,),
    ).fetchall()
    assert len(rows) == 2
    assert all(r[0] == "777" for r in rows)


@pytest.mark.asyncio
async def test_loop_baseline_runs_first_then_later_matches_fire(tmp_path):
    """Before enabling, matches exist -> baseline silences them.
    A NEW match added after baseline fires normally."""
    conn = fresh_db(tmp_path)
    seed_match(conn, project_slug="a/b", source_id="GHSA-old",
               severity="critical", dep_name="old-pkg")
    # First tick: baseline runs, no send
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(f"{API}/botTOKEN/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 1}}),
        )
        await _one_tick(conn, _enable_dm_config(tmp_path))
    assert route.call_count == 0
    baseline_rows = conn.execute(
        "SELECT COUNT(*) FROM emission WHERE artifact_path = 'baseline'",
    ).fetchone()[0]
    assert baseline_rows == 1

    # New match arrives
    seed_match(conn, project_slug="a/b", source_id="GHSA-new",
               severity="critical", dep_name="new-pkg")
    with respx.mock() as mock:
        route = mock.post(f"{API}/botTOKEN/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 42}}),
        )
        await _one_tick(conn, _enable_dm_config(tmp_path))
    assert route.call_count == 1
    sent = conn.execute(
        "SELECT artifact_path FROM emission WHERE channel = ? AND artifact_path != 'baseline'",
        (SUBCH_DM,),
    ).fetchall()
    assert ("42",) in sent


@pytest.mark.asyncio
async def test_loop_respects_min_severity_floor(tmp_path):
    conn = fresh_db(tmp_path)
    seed_match(conn, source_id="GHSA-l", severity="low", cvss=3.0)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(f"{API}/botTOKEN/sendMessage")
        await _one_tick(conn, _enable_dm_config(tmp_path, severity="medium"))
    assert route.call_count == 0
    rows = conn.execute(
        "SELECT COUNT(*) FROM emission",
    ).fetchone()[0]
    # baseline runs but filters at medium; low-severity match excluded
    assert rows == 0


@pytest.mark.asyncio
async def test_loop_429_leaves_match_unemitted_for_retry(tmp_path):
    conn = fresh_db(tmp_path)
    # seed + consume baseline first, so later fresh match will attempt send
    seed_match(conn, source_id="GHSA-pre", severity="critical")
    await _one_tick(conn, _enable_dm_config(tmp_path))
    # new match
    seed_match(conn, source_id="GHSA-rate", severity="critical", dep_name="p2")
    with respx.mock() as mock:
        mock.post(f"{API}/botTOKEN/sendMessage").mock(
            return_value=httpx.Response(429, json={
                "ok": False, "description": "Too Many",
                "parameters": {"retry_after": 1},
            })
        )
        await _one_tick(conn, _enable_dm_config(tmp_path))
    # No send-emission row written for GHSA-rate on telegram.dm
    rows = conn.execute(
        """
        SELECT e.id FROM emission e
        JOIN match m ON m.id = e.match_id
        JOIN advisory a ON a.id = m.advisory_id
        WHERE a.source_id = ? AND e.channel = ? AND e.artifact_path != 'baseline'
        """,
        ("GHSA-rate", SUBCH_DM),
    ).fetchall()
    assert rows == []


@pytest.mark.asyncio
async def test_loop_400_poisons_emission_to_prevent_retry(tmp_path):
    conn = fresh_db(tmp_path)
    seed_match(conn, source_id="GHSA-pre", severity="critical")
    await _one_tick(conn, _enable_dm_config(tmp_path))
    seed_match(conn, source_id="GHSA-badchat", severity="critical", dep_name="p2")
    with respx.mock() as mock:
        mock.post(f"{API}/botTOKEN/sendMessage").mock(
            return_value=httpx.Response(400, json={
                "ok": False, "description": "Bad Request: chat not found",
            })
        )
        await _one_tick(conn, _enable_dm_config(tmp_path))
    rows = conn.execute(
        """
        SELECT e.artifact_path FROM emission e
        JOIN match m ON m.id = e.match_id
        JOIN advisory a ON a.id = m.advisory_id
        WHERE a.source_id = ? AND e.channel = ?
        """,
        ("GHSA-badchat", SUBCH_DM),
    ).fetchall()
    # Error recorded so next tick doesn't re-try this poisoned advisory
    assert any(r[0].startswith("error:") for r in rows)


@pytest.mark.asyncio
async def test_loop_dm_and_channel_independent_dispatch(tmp_path):
    """DM config returns 200, channel returns 400 — assert DM emission wins,
    channel emission is poisoned, no cross-contamination."""
    conn = fresh_db(tmp_path)
    # baseline both first so new matches fire cleanly
    seed_match(conn, source_id="GHSA-pre", severity="critical")
    cfg = {
        "outputs": {"telegram": {
            "enabled": True, "bot_token": "TOKEN",
            "chat_id": "111", "channel_id": "-100999",
            "min_severity": "medium",
        }},
        "dashboard": {"base_url": "http://t"},
        "poll": {"telegram": 0},
        "agent": {"data_dir": str(tmp_path)},
    }
    await _one_tick(conn, cfg)

    seed_match(conn, source_id="GHSA-mix", severity="critical", dep_name="p2")
    with respx.mock() as mock:
        # Dispatch is in-order per sub-channel. We mock based on chat_id body.
        def handler(request: httpx.Request) -> httpx.Response:
            body = _json.loads(request.content)
            if body["chat_id"] == "111":
                return httpx.Response(200, json={"ok": True, "result": {"message_id": 1001}})
            return httpx.Response(400, json={
                "ok": False, "description": "Bad Request: chat not found",
            })
        mock.post(f"{API}/botTOKEN/sendMessage").mock(side_effect=handler)
        await _one_tick(conn, cfg)

    dm_rows = conn.execute(
        """
        SELECT e.artifact_path FROM emission e
        JOIN match m ON m.id = e.match_id
        JOIN advisory a ON a.id = m.advisory_id
        WHERE a.source_id = ? AND e.channel = ?
        """,
        ("GHSA-mix", SUBCH_DM),
    ).fetchall()
    assert ("1001",) in dm_rows

    ch_rows = conn.execute(
        """
        SELECT e.artifact_path FROM emission e
        JOIN match m ON m.id = e.match_id
        JOIN advisory a ON a.id = m.advisory_id
        WHERE a.source_id = ? AND e.channel = ?
        """,
        ("GHSA-mix", SUBCH_CHANNEL),
    ).fetchall()
    assert any(r[0].startswith("error:") for r in ch_rows)


@pytest.mark.asyncio
async def test_loop_exits_cleanly_when_disabled(tmp_path):
    conn = fresh_db(tmp_path)
    seed_match(conn, source_id="GHSA-x", severity="critical")
    cfg = _enable_dm_config(tmp_path)
    cfg["outputs"]["telegram"]["enabled"] = False
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(f"{API}/botTOKEN/sendMessage")
        await _one_tick(conn, cfg)
    assert route.call_count == 0
    # No emission rows (not even baseline — baseline is gated on enabled too)
    rows = conn.execute("SELECT COUNT(*) FROM emission").fetchone()[0]
    assert rows == 0


@pytest.mark.asyncio
async def test_loop_exits_when_no_destinations_configured(tmp_path):
    conn = fresh_db(tmp_path)
    seed_match(conn, source_id="GHSA-x", severity="critical")
    cfg = _enable_dm_config(tmp_path)
    cfg["outputs"]["telegram"]["chat_id"] = ""
    cfg["outputs"]["telegram"]["channel_id"] = ""
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(f"{API}/botTOKEN/sendMessage")
        await _one_tick(conn, cfg)
    assert route.call_count == 0
