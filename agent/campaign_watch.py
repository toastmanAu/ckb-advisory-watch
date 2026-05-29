"""Supply-chain campaign cross-ref.

One-shot read-side scan that joins a curated IoC list (data/supply-chain-campaigns.yaml)
against the existing project_dep table.

This is intentionally separate from the OSV advisory pipeline: campaign IoCs
aren't CVE records, they're "this package is itself the payload" — distinct
shape, distinct trigger, distinct trust model (operator-curated).

Run as a module from the repo root:

    python -m agent.campaign_watch                  # markdown report to stdout
    python -m agent.campaign_watch --format json    # machine-readable
    python -m agent.campaign_watch --only mini-*    # filter to specific campaign id glob
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO_ROOT / "data" / "state.db"
DEFAULT_WATCHLIST = REPO_ROOT / "data" / "supply-chain-campaigns.yaml"


@dataclass(frozen=True)
class Indicator:
    ecosystem: str
    pattern: str  # may contain *

    @property
    def sql_like(self) -> str:
        return self.pattern.replace("*", "%")


@dataclass(frozen=True)
class Campaign:
    id: str
    title: str
    first_seen: str | None
    references: tuple[str, ...]
    indicators: tuple[Indicator, ...]
    affected_versions: str | None


@dataclass(frozen=True)
class Match:
    campaign: Campaign
    indicator: Indicator
    project_slug: str
    project_name: str
    dep_name: str
    dep_version: str
    is_direct: bool


def load_campaigns(path: Path) -> list[Campaign]:
    with path.open() as f:
        raw = yaml.safe_load(f) or {}
    out: list[Campaign] = []
    for entry in raw.get("campaigns", []):
        indicators = tuple(
            Indicator(ecosystem=i["ecosystem"], pattern=i["name"])
            for i in entry.get("indicators", [])
        )
        out.append(
            Campaign(
                id=entry["id"],
                title=entry["title"],
                first_seen=entry.get("first_seen"),
                references=tuple(entry.get("references", [])),
                indicators=indicators,
                affected_versions=entry.get("affected_versions"),
            )
        )
    return out


def scan(conn: sqlite3.Connection, campaigns: Iterable[Campaign]) -> list[Match]:
    matches: list[Match] = []
    cur = conn.cursor()
    for campaign in campaigns:
        for ind in campaign.indicators:
            cur.execute(
                """
                SELECT p.slug, p.display_name, d.name, d.version, d.is_direct
                FROM project_dep d
                JOIN project p ON p.id = d.project_id
                WHERE d.ecosystem = ? AND d.name LIKE ?
                ORDER BY p.slug, d.name, d.version
                """,
                (ind.ecosystem, ind.sql_like),
            )
            for slug, display_name, dep_name, dep_version, is_direct in cur.fetchall():
                matches.append(
                    Match(
                        campaign=campaign,
                        indicator=ind,
                        project_slug=slug,
                        project_name=display_name,
                        dep_name=dep_name,
                        dep_version=dep_version,
                        is_direct=bool(is_direct),
                    )
                )
    return matches


def to_markdown(campaigns: list[Campaign], matches: list[Match]) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines: list[str] = []
    lines.append("# Supply-chain campaign cross-ref")
    lines.append("")
    lines.append(f"*Generated {now} — {len(campaigns)} campaign(s), {len(matches)} match(es)*")
    lines.append("")

    by_campaign: dict[str, list[Match]] = {}
    for m in matches:
        by_campaign.setdefault(m.campaign.id, []).append(m)

    for campaign in campaigns:
        cm = by_campaign.get(campaign.id, [])
        header = f"## {campaign.title}"
        if not cm:
            lines.append(f"{header} — ✅ no matches")
            lines.append("")
            continue
        lines.append(f"{header} — 🚨 **{len(cm)} match(es)**")
        if campaign.first_seen:
            lines.append(f"*First seen: {campaign.first_seen}*")
        if campaign.affected_versions:
            lines.append("")
            lines.append("**Affected versions / notes:**")
            for ln in campaign.affected_versions.rstrip().splitlines():
                lines.append(f"> {ln}")
        lines.append("")
        # Group by project for readability
        by_project: dict[str, list[Match]] = {}
        for m in cm:
            by_project.setdefault(m.project_slug, []).append(m)
        for slug, pm in sorted(by_project.items()):
            lines.append(f"### `{slug}` — {pm[0].project_name}")
            lines.append("")
            lines.append("| ecosystem | dep | version | direct? | matched pattern |")
            lines.append("|---|---|---|---|---|")
            for m in pm:
                direct = "✅" if m.is_direct else "transitive"
                lines.append(
                    f"| {m.indicator.ecosystem} | `{m.dep_name}` | `{m.dep_version}` | "
                    f"{direct} | `{m.indicator.pattern}` |"
                )
            lines.append("")
        if campaign.references:
            lines.append("**References:**")
            for ref in campaign.references:
                lines.append(f"- {ref}")
            lines.append("")
    return "\n".join(lines)


def to_json(campaigns: list[Campaign], matches: list[Match]) -> str:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "campaigns": [
            {
                "id": c.id,
                "title": c.title,
                "first_seen": c.first_seen,
                "references": list(c.references),
                "match_count": sum(1 for m in matches if m.campaign.id == c.id),
            }
            for c in campaigns
        ],
        "matches": [
            {
                "campaign_id": m.campaign.id,
                "ecosystem": m.indicator.ecosystem,
                "pattern": m.indicator.pattern,
                "project_slug": m.project_slug,
                "project_name": m.project_name,
                "dep_name": m.dep_name,
                "dep_version": m.dep_version,
                "is_direct": m.is_direct,
            }
            for m in matches
        ],
    }
    return json.dumps(payload, indent=2)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Supply-chain campaign cross-ref")
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--watchlist", type=Path, default=DEFAULT_WATCHLIST)
    p.add_argument("--format", choices=("markdown", "json"), default="markdown")
    p.add_argument(
        "--only",
        default=None,
        help="Filter campaigns by id glob (e.g. 'mini-*').",
    )
    args = p.parse_args(argv)

    if not args.db.exists():
        print(f"DB not found: {args.db}", file=sys.stderr)
        return 2
    if not args.watchlist.exists():
        print(f"Watchlist not found: {args.watchlist}", file=sys.stderr)
        return 2

    campaigns = load_campaigns(args.watchlist)
    if args.only:
        campaigns = [c for c in campaigns if fnmatch.fnmatch(c.id, args.only)]
        if not campaigns:
            print(f"No campaigns matched filter: {args.only}", file=sys.stderr)
            return 2

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        matches = scan(conn, campaigns)
    finally:
        conn.close()

    if args.format == "json":
        print(to_json(campaigns, matches))
    else:
        print(to_markdown(campaigns, matches))

    # Exit code = number of matches capped at 255 — useful for cron/CI gating.
    return min(len(matches), 255)


import logging
import httpx
import time

log = logging.getLogger(__name__)


def _escape_html(s: str) -> str:
    import html
    return html.escape(s)


async def run_campaign_check(
    conn: sqlite3.Connection,
    config: dict,
    client: httpx.AsyncClient | None = None,
) -> None:
    """Run campaign scan, update data/campaign-report.md, and send Telegram alerts if configured."""
    watchlist_path = REPO_ROOT / config.get("agent", {}).get("watchlist_path", "data/supply-chain-campaigns.yaml")
    if not watchlist_path.exists():
        watchlist_path = DEFAULT_WATCHLIST

    try:
        campaigns = load_campaigns(watchlist_path)
    except Exception as exc:
        log.error("Failed to load campaigns from %s: %r", watchlist_path, exc)
        return

    try:
        matches = scan(conn, campaigns)
    except Exception as exc:
        log.error("Failed to scan database for campaign matches: %r", exc)
        return

    # Write markdown summary output
    try:
        report_md = to_markdown(campaigns, matches)
        data_dir = Path(config.get("agent", {}).get("data_dir", "data"))
        report_path = REPO_ROOT / data_dir / "campaign-report.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report_md, encoding="utf-8")
        log.info("Campaign watch summary written to %s (%d matches)", report_path, len(matches))
    except Exception as exc:
        log.error("Failed to write campaign summary report: %r", exc)

    # Telegram alerts if enabled
    tele_config = config.get("outputs", {}).get("telegram", {})
    if not tele_config.get("enabled", False):
        return

    bot_token = tele_config.get("bot_token")
    chat_id = tele_config.get("chat_id")
    channel_id = tele_config.get("channel_id")
    if not bot_token or not (chat_id or channel_id):
        return

    async_client = client or httpx.AsyncClient()
    own_client = client is None

    try:
        from agent.output.telegram import send_message
        for m in matches:
            state_key = f"campaign.emitted.{m.campaign.id}.{m.project_slug}.{m.dep_name}.{m.dep_version}"

            # Check if alert already sent
            row = conn.execute("SELECT 1 FROM poller_state WHERE key = ?", (state_key,)).fetchone()
            if row:
                continue

            # Format the Telegram HTML message
            direct_str = "direct dependency" if m.is_direct else "transitive dependency"
            html_body = (
                f"🚨 <b>Supply-Chain Compromise Alert</b>\n\n"
                f"Campaign: <b>{_escape_html(m.campaign.title)}</b>\n"
                f"Project: <code>{_escape_html(m.project_slug)}</code> ({_escape_html(m.project_name)})\n"
                f"Dependency: <code>{_escape_html(m.dep_name)}</code> (version <code>{_escape_html(m.dep_version)}</code>, {direct_str})\n"
            )
            if m.campaign.affected_versions:
                html_body += f"\n<b>Affected Versions / Notes:</b>\n"
                for line in m.campaign.affected_versions.splitlines():
                    html_body += f"&gt; <i>{_escape_html(line)}</i>\n"
            if m.campaign.references:
                html_body += f"\n<b>References:</b>\n"
                for ref in m.campaign.references:
                    html_body += f"- <a href=\"{ref}\">{_escape_html(ref)}</a>\n"

            keyboard = {}
            base_url = (config.get("dashboard") or {}).get("base_url", "")
            if base_url:
                keyboard = {
                    "inline_keyboard": [[{
                        "text": "View Project Matches",
                        "url": f"{base_url.rstrip('/')}/p/{m.project_slug}",
                    }]]
                }

            dm_sent = False
            chan_sent = False

            if chat_id:
                try:
                    await send_message(
                        async_client, bot_token=bot_token, chat_id=chat_id,
                        html_body=html_body, inline_keyboard=keyboard
                    )
                    dm_sent = True
                except Exception as exc:
                    log.error("Failed to send campaign alert to Telegram DM (%s): %r", chat_id, exc)

            if channel_id:
                try:
                    await send_message(
                        async_client, bot_token=bot_token, chat_id=channel_id,
                        html_body=html_body, inline_keyboard=keyboard
                    )
                    chan_sent = True
                except Exception as exc:
                    log.error("Failed to send campaign alert to Telegram Channel (%s): %r", channel_id, exc)

            if dm_sent or chan_sent:
                now_ts = int(time.time())
                try:
                    with conn:
                        conn.execute(
                            "INSERT INTO poller_state (key, value, updated_at) VALUES (?, '1', ?) "
                            "ON CONFLICT(key) DO UPDATE SET value='1', updated_at=excluded.updated_at",
                            (state_key, now_ts)
                        )
                except sqlite3.OperationalError as exc:
                    # Database might be locked, log it but don't fail the loop.
                    log.warning("Failed to record campaign emission state for %s: %r", state_key, exc)
    finally:
        if own_client:
            await async_client.aclose()


if __name__ == "__main__":
    sys.exit(main())
