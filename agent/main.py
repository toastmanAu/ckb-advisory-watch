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
import sqlite3
import sys
from pathlib import Path

import httpx

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
