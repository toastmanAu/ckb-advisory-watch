"""CLI entry point — `python -m agent.mirror`.

Called by the systemd timer (see systemd/ckb-mirror.timer) every hour.
Pipeline: load config → gate on enabled → open DB read-only → render →
secret scan → wrangler deploy. Exits 0 on success (including disabled),
1 on render/scan/deploy failure, 2 on config error."""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]

from agent.mirror.deploy import (
    DeployError, SecretScanFailed, deploy_via_wrangler, scan_for_secrets,
)
from agent.mirror.render import render_all

log = logging.getLogger("ckb-mirror")

_SEVERITY_FLOORS: dict[str, tuple[str, ...]] = {
    "critical": ("critical",),
    "high": ("critical", "high"),
    "medium": ("critical", "high", "medium"),
    "low": ("critical", "high", "medium", "low"),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent.mirror")
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if not args.config.exists():
        log.error("config not found: %s", args.config)
        return 2
    with args.config.open("rb") as f:
        config = tomllib.load(f)

    mirror_cfg = (config.get("outputs") or {}).get("public_mirror") or {}
    if not mirror_cfg.get("enabled", False):
        msg = "mirror disabled ([outputs.public_mirror].enabled = false)"
        log.info(msg)
        print(msg, file=sys.stderr)
        return 0

    project_name = mirror_cfg.get("project_name", "")
    api_token = mirror_cfg.get("api_token", "")
    account_id = mirror_cfg.get("account_id", "")
    min_severity = mirror_cfg.get("min_severity", "medium")
    out_dir = Path(mirror_cfg.get("out_dir", "/tmp/mirror-out"))
    base_url = mirror_cfg.get(
        "base_url", "https://advisories.wyltekindustries.com"
    )

    if not api_token:
        log.error("api_token empty — issue a Cloudflare token and set "
                  "[outputs.public_mirror].api_token")
        return 2
    if not account_id:
        log.error("account_id empty — set [outputs.public_mirror].account_id")
        return 2
    if not project_name:
        log.error("project_name empty — set [outputs.public_mirror].project_name")
        return 2

    severity_floor = _SEVERITY_FLOORS.get(min_severity.lower())
    if severity_floor is None:
        log.error("unknown min_severity=%r (valid: critical|high|medium|low)",
                  min_severity)
        return 2

    data_dir = Path((config.get("agent") or {}).get("data_dir", "data"))
    db_path = data_dir / "state.db"
    if not db_path.exists():
        log.error("state.db not found at %s — agent must run first", db_path)
        return 1

    # Read-only connection — matches the dashboard's conn_factory. The
    # agent's writer path is entirely unaffected by a mirror tick.
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.execute("PRAGMA busy_timeout = 10000")
    try:
        log.info("rendering mirror into %s (floor=%s)", out_dir, severity_floor)
        try:
            pages = render_all(
                conn, out_dir,
                severity_floor=severity_floor,
                base_url=base_url,
            )
        except Exception as exc:
            log.exception("render failed: %r", exc)
            return 1
        log.info("rendered %d pages", pages)

        log.info("scanning %s for secrets", out_dir)
        findings = scan_for_secrets(out_dir)
        if findings:
            log.error("secret scan found %d leaks — aborting deploy", len(findings))
            for f in findings[:20]:
                log.error("  %s:%d [%s] %s", f.file, f.line, f.pattern, f.matched_text)
            return 1

        log.info("deploying via wrangler to project=%s", project_name)
        try:
            deploy_via_wrangler(
                out_dir=out_dir,
                project_name=project_name,
                api_token=api_token,
                account_id=account_id,
            )
        except DeployError as exc:
            log.error("deploy failed: %s", exc)
            return 1

        log.info("mirror tick complete")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
