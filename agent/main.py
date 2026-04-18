"""ckb-advisory-watch agent entrypoint.

Single-process asyncio skeleton. Each poller is an async task on its own
schedule; they write into SQLite. The matcher runs whenever new advisories
land and writes match rows. The output fan-out reads unemitted matches and
publishes to the enabled channels.

Phase 0: this module is a stub that just starts, loads config, pings SQLite,
and exits cleanly. Real pollers and matcher land in later phases.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
import sys
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]

log = logging.getLogger("ckb-advisory-watch")


def load_config(path: Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


def ensure_schema(db_path: Path, schema_sql: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(schema_sql.read_text())


async def run(config: dict) -> None:
    log.info("ckb-advisory-watch starting — Phase 0 skeleton")
    # Phase 2+ will spawn poller tasks here.
    # Phase 3 spawns the matcher.
    # Phase 4 spawns the output fan-out.
    log.info("(no tasks registered yet — exiting)")


def main() -> int:
    parser = argparse.ArgumentParser(prog="ckb-advisory-watch")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if not args.config.exists():
        log.error("config not found at %s — copy config.example.toml", args.config)
        return 2

    config = load_config(args.config)
    data_dir = Path(config.get("agent", {}).get("data_dir", "data"))
    ensure_schema(data_dir / "state.db", Path("db/schema.sql"))

    asyncio.run(run(config))
    return 0


if __name__ == "__main__":
    sys.exit(main())
