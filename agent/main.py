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
from agent.sources.osv import DEFAULT_ECOSYSTEMS, ingest_all

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
        for eco, outcome in results.items():
            if isinstance(outcome, Exception):
                log.error("osv.%s: FAILED %r", eco, outcome)
            else:
                log.info("osv.%s: %d advisories", eco, outcome)

        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue


async def run(config: dict, schema_path: Path) -> None:
    data_dir = Path(config.get("agent", {}).get("data_dir", "data"))
    ecosystems = list(
        config.get("osv", {}).get("ecosystems", DEFAULT_ECOSYSTEMS)
    )
    interval = float(config.get("poll", {}).get("osv", 3600))

    conn = open_db(data_dir / "state.db", schema_path)
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    log.info(
        "ckb-advisory-watch starting — %d ecosystems, %s poll interval",
        len(ecosystems),
        f"{interval:.0f}s",
    )
    async with httpx.AsyncClient(
        headers={"user-agent": "ckb-advisory-watch/0 (+https://github.com/toastmanAu/ckb-advisory-watch)"}
    ) as client:
        try:
            await osv_poll_loop(conn, client, ecosystems, interval, stop)
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
