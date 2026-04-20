"""ckb-advisory-watch agent entrypoint.

Single-process asyncio agent. Polls OSV per-ecosystem bulk ZIPs on a
configurable cadence, upserts into SQLite via conditional-GET caching, logs
counts. Matching engine and output fan-out land in later phases.

Cleanly shuts down on SIGTERM/SIGINT — asyncio.run() catches KeyboardInterrupt,
and httpx.AsyncClient's context manager closes sockets on exit.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import socket
import sqlite3
import sqlite3 as _sqlite3  # re-imported name for conn_factory closure
import sys
from pathlib import Path

import httpx
from aiohttp import web

from agent.dashboard import server as dashboard_server
from agent.dashboard import share as dashboard_share
from agent.output.telegram import telegram_poll_loop

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]

from agent.db import open_db
from agent.matcher import run_matcher
from agent.sources.osv import DEFAULT_ECOSYSTEMS, ingest_all
from agent.walker import walk_all

log = logging.getLogger("ckb-advisory-watch")


def load_config(path: Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


async def osv_poll_loop(
    conn: sqlite3.Connection,
    client: httpx.AsyncClient,
    ecosystems: list[str],
    interval: float,
    stop: asyncio.Event,
) -> None:
    """Run ingest_all in a loop. First run fires immediately; subsequent runs
    wait `interval` seconds. Exits when `stop` is set."""
    while not stop.is_set():
        log.info("osv poll: starting run across %d ecosystems", len(ecosystems))
        results = await ingest_all(conn, client, ecosystems)
        changed = False
        for eco, outcome in results.items():
            if isinstance(outcome, Exception):
                log.error("osv.%s: FAILED %r", eco, outcome)
            else:
                log.info("osv.%s: %d advisories", eco, outcome)
                if outcome > 0:
                    changed = True

        if changed:
            new_matches = run_matcher(conn)
            log.info("matcher: %d new matches after osv ingest", new_matches)

        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue


async def github_poll_loop(
    conn: sqlite3.Connection,
    client: httpx.AsyncClient,
    interval: float,
    stop: asyncio.Event,
) -> None:
    """Walk every seeded project's lockfiles into project_dep. Runs less
    often than OSV (default daily) — projects mostly don't change hourly,
    and the SHA-cache in walk_project short-circuits unchanged repos."""
    while not stop.is_set():
        log.info("github poll: walking lockfiles")
        results = await walk_all(client, conn)
        failed = [s for s, r in results.items() if isinstance(r, Exception)]
        changed = sum(1 for r in results.values() if isinstance(r, int) and r > 0)
        log.info(
            "github poll: %d projects, %d updated, %d failed",
            len(results), changed, len(failed),
        )
        for slug in failed:
            log.warning("github.%s: %r", slug, results[slug])

        if changed > 0:
            new_matches = run_matcher(conn)
            log.info("matcher: %d new matches after github walk", new_matches)

        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue


async def start_dashboard(
    config: dict,
    data_dir: Path,
    stop: asyncio.Event,
) -> None:
    """aiohttp AppRunner lifecycle bound to the shared stop Event.

    Each request opens its own read-only SQLite connection via the factory;
    the agent's writer loop is untouched."""
    dash_cfg = config.get("dashboard", {}) or {}
    share_cfg_d = config.get("share", {}) or {}
    if not share_cfg_d.get("enabled", False):
        log.info("dashboard: share disabled (config [share].enabled = false)")

    db_path = data_dir / "state.db"

    def conn_factory() -> _sqlite3.Connection:
        return _sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    share_cfg = dashboard_share.ShareConfig(
        recipient=share_cfg_d.get("recipient", ""),
        sender=share_cfg_d.get("sender", ""),
        smtp_host=share_cfg_d.get("smtp_host", "smtp.gmail.com"),
        smtp_port=int(share_cfg_d.get("smtp_port", 465)),
        smtp_user=share_cfg_d.get("smtp_user", ""),
        smtp_password=share_cfg_d.get("smtp_password", ""),
        dashboard_base_url=dash_cfg.get("base_url", "http://127.0.0.1:8080"),
    )

    app = dashboard_server.build_app(
        conn_factory=conn_factory,
        share_config=share_cfg,
        hostname=socket.gethostname(),
    )

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(
        runner,
        host=dash_cfg.get("host", "0.0.0.0"),
        port=int(dash_cfg.get("port", 8080)),
    )
    await site.start()
    log.info(
        "dashboard: listening on http://%s:%d",
        dash_cfg.get("host", "0.0.0.0"),
        int(dash_cfg.get("port", 8080)),
    )

    try:
        await stop.wait()
    finally:
        await runner.cleanup()


async def run(config: dict, schema_path: Path) -> None:
    data_dir = Path(config.get("agent", {}).get("data_dir", "data"))
    ecosystems = list(
        config.get("osv", {}).get("ecosystems", DEFAULT_ECOSYSTEMS)
    )
    osv_interval = float(config.get("poll", {}).get("osv", 3600))
    github_interval = float(config.get("poll", {}).get("github_repos", 86400))
    github_token = (config.get("github", {}) or {}).get("token") or ""

    conn = open_db(data_dir / "state.db", schema_path)
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    ua = "ckb-advisory-watch/0 (+https://github.com/toastmanAu/ckb-advisory-watch)"
    github_headers: dict[str, str] = {
        "user-agent": ua,
        "accept": "application/vnd.github+json",
    }
    if github_token:
        github_headers["authorization"] = f"Bearer {github_token}"
    else:
        log.warning("no github token configured — 60 req/hour rate limit applies")

    log.info(
        "ckb-advisory-watch starting — osv=%ds, github=%ds, ecosystems=%d",
        int(osv_interval), int(github_interval), len(ecosystems),
    )
    async with (
        httpx.AsyncClient(headers={"user-agent": ua}) as osv_client,
        httpx.AsyncClient(headers=github_headers) as gh_client,
    ):
        try:
            await asyncio.gather(
                osv_poll_loop(conn, osv_client, ecosystems, osv_interval, stop),
                github_poll_loop(conn, gh_client, github_interval, stop),
                start_dashboard(config, data_dir, stop),
                telegram_poll_loop(conn, config, stop),
            )
        finally:
            log.info("ckb-advisory-watch stopped")
            conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(prog="ckb-advisory-watch")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--schema", type=Path, default=Path("db/schema.sql"))
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if not args.config.exists():
        log.error("config not found at %s — copy config.example.toml", args.config)
        return 2

    config = load_config(args.config)
    asyncio.run(run(config, args.schema))
    return 0


if __name__ == "__main__":
    sys.exit(main())
