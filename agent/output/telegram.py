"""Telegram push output channel for ckb-advisory-watch.

Per-advisory messages (grouped across affected projects) to DM and/or a
channel, deduped via the existing `emission` table. Each destination is a
separate sub-channel (`telegram.dm` / `telegram.channel`) so failures on
one don't affect the other.

See docs/superpowers/specs/2026-04-21-telegram-design.md for the design.
"""
from __future__ import annotations

# Map severity label to numeric rank for threshold comparisons.
# 0 = unknown (treated as lowest for gating purposes).
SEVERITY_LEVEL: dict[str, int] = {
    "critical": 4,
    "high":     3,
    "medium":   2,
    "low":      1,
    "unknown":  0,
}

# Visual hint for Telegram notifications + message body.
SEVERITY_EMOJI: dict[str, str] = {
    "critical": "🔴",
    "high":     "🟠",
    "medium":   "🟡",
    "low":      "🟢",
    "unknown":  "⚪",
}

# emission.channel values. Per-destination tracking via
# UNIQUE(match_id, channel). Each match may have one row per sub-channel.
SUBCH_DM      = "telegram.dm"
SUBCH_CHANNEL = "telegram.channel"

# Body rendering limits — see spec §6.2.
MAX_MATCHES_IN_MESSAGE = 8
SUMMARY_MAX_CHARS      = 500
MESSAGE_TOTAL_CAP      = 4000


def severity_level(label: str | None) -> int:
    """Map a severity string (case-insensitive, None, unknown) to its numeric rank."""
    if not label:
        return SEVERITY_LEVEL["unknown"]
    return SEVERITY_LEVEL.get(label.lower(), SEVERITY_LEVEL["unknown"])


import html as _html  # noqa: E402 — stdlib, after constants block
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

from agent.dashboard.queries import AdvisoryContext, MatchRow

_TEMPLATES_DIR = Path(__file__).parent / "templates"

# Autoescape is intentionally disabled — we WANT <b>/<code>/<i>/<a> to reach
# Telegram. Every interpolated user-controlled field must pass through |e
# explicitly in the template (enforced by tests).
_env = Environment(
    loader=FileSystemLoader(_TEMPLATES_DIR),
    autoescape=False,
    trim_blocks=True,
    lstrip_blocks=True,
)


def _truncate(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    return s[:limit].rstrip() + "…"


def _first_advisory_ref(advisory: AdvisoryContext) -> str | None:
    """Return URL of first reference with type ADVISORY, else first reference
    of any type, else None."""
    refs = advisory.references or []
    for r in refs:
        if (r.get("type") or "").upper() == "ADVISORY":
            url = r.get("url")
            if isinstance(url, str) and url:
                return url
    for r in refs:
        url = r.get("url")
        if isinstance(url, str) and url:
            return url
    return None


def _render_body(
    advisory: AdvisoryContext,
    matches: list[MatchRow],
    summary_chars: int,
) -> str:
    sev_label = (advisory.severity or "unknown").lower()
    sev_emoji = SEVERITY_EMOJI.get(sev_label, SEVERITY_EMOJI["unknown"])
    tpl = _env.get_template("telegram.html")
    return tpl.render(
        advisory=advisory,
        matches=matches,
        sev_label=sev_label,
        sev_emoji=sev_emoji,
        summary_truncated=_truncate(advisory.summary, summary_chars),
        max_matches=MAX_MATCHES_IN_MESSAGE,
    )


def format_message(
    advisory: AdvisoryContext,
    matches: list[MatchRow],
    config: dict,
) -> tuple[str, dict[str, Any]]:
    """Render (html_body, inline_keyboard) for a per-advisory Telegram message.

    Body truncates the advisory summary to SUMMARY_MAX_CHARS and the match
    list to MAX_MATCHES_IN_MESSAGE. If the rendered body still exceeds
    MESSAGE_TOTAL_CAP (rare — requires a pathologically long summary or
    huge slug list), shrink the summary further in 50-char steps until the
    body fits, down to 50 chars. Single-message-per-advisory is invariant.
    """
    # Render with progressively smaller summary caps until body fits.
    summary_chars = SUMMARY_MAX_CHARS
    body = _render_body(advisory, matches, summary_chars)
    while len(body) > MESSAGE_TOTAL_CAP and summary_chars > 50:
        summary_chars = max(50, summary_chars - 50)
        body = _render_body(advisory, matches, summary_chars)
    if len(body) > MESSAGE_TOTAL_CAP:
        # Last resort: hard cut. Invariant: always one message per advisory.
        body = body[: MESSAGE_TOTAL_CAP - 1].rstrip() + "…"

    # Inline keyboard — URL-only buttons, one row.
    buttons: list[dict[str, str]] = []
    base_url = (config.get("dashboard") or {}).get("base_url", "")
    if base_url:
        buttons.append({
            "text": "View on dashboard",
            "url": f"{base_url.rstrip('/')}/a/{advisory.source_id}",
        })
    ref_url = _first_advisory_ref(advisory)
    if ref_url:
        buttons.append({"text": "View on GHSA", "url": ref_url})

    keyboard: dict[str, Any] = {"inline_keyboard": [buttons] if buttons else []}
    return body, keyboard


import httpx  # noqa: E402 — stdlib-ish, after domain code block

TELEGRAM_API = "https://api.telegram.org"


class TransientSendError(Exception):
    """Transient Telegram failure — caller should retry on next tick.

    `retry_after` is the integer seconds to wait before retrying (present
    on 429 responses via Telegram's retry_after parameter). None for network
    errors and 5xx responses.
    """
    def __init__(self, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class PermanentSendError(Exception):
    """Non-retryable Telegram failure — chat not found, bad HTML, bad token.

    Caller should mark the emission as 'error' to prevent infinite retries
    and surface the error for operator intervention.
    """


async def send_message(
    client: httpx.AsyncClient,
    *,
    bot_token: str,
    chat_id: str,
    html_body: str,
    inline_keyboard: dict,
) -> int:
    """POST sendMessage, return the Telegram message_id on success.

    Raises TransientSendError on 429, 5xx, network errors (caller retries).
    Raises PermanentSendError on 400 (bad chat, bad HTML) or non-ok body.
    """
    payload: dict = {
        "chat_id": chat_id,
        "text": html_body,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if inline_keyboard.get("inline_keyboard"):
        payload["reply_markup"] = inline_keyboard

    url = f"{TELEGRAM_API}/bot{bot_token}/sendMessage"
    try:
        resp = await client.post(url, json=payload, timeout=30.0)
    except httpx.HTTPError as exc:
        raise TransientSendError(f"network error: {exc!r}") from exc

    if resp.status_code == 429:
        try:
            retry_after = int(resp.json().get("parameters", {}).get("retry_after", 1))
        except Exception:
            retry_after = 1
        raise TransientSendError(f"rate limited; retry_after={retry_after}", retry_after=retry_after)

    if 500 <= resp.status_code < 600:
        raise TransientSendError(f"server error {resp.status_code}: {resp.text[:200]}")

    if resp.status_code == 400:
        try:
            desc = resp.json().get("description", "bad request")
        except Exception:
            desc = resp.text[:200]
        raise PermanentSendError(f"400: {desc}")

    if resp.status_code != 200:
        raise PermanentSendError(f"unexpected status {resp.status_code}: {resp.text[:200]}")

    data = resp.json()
    if not data.get("ok"):
        raise PermanentSendError(f"api returned ok=false: {data.get('description', '<no desc>')}")

    return int(data["result"]["message_id"])


import sqlite3  # noqa: E402 — stdlib, after async send block
import time     # noqa: E402

# CASE expression mapping severity strings to the numeric rank used by
# SEVERITY_LEVEL. Shared by unemitted-query and baseline-query to guarantee
# identical threshold semantics.
_SEVERITY_CASE_SQL = (
    "CASE COALESCE(a.severity, 'unknown') "
    "WHEN 'critical' THEN 4 "
    "WHEN 'high' THEN 3 "
    "WHEN 'medium' THEN 2 "
    "WHEN 'low' THEN 1 "
    "ELSE 0 END"
)


def _unemitted_advisories_above(
    conn: sqlite3.Connection,
    sub_channel: str,
    min_level: int,
) -> list[tuple[int, str]]:
    """Return [(advisory_id, source_id), ...] for advisories with >=1 open
    match at or above `min_level` that has not yet been emitted on
    `sub_channel`. Ordered newest-advisory-first (by advisory.modified)."""
    rows = conn.execute(
        f"""
        SELECT DISTINCT m.advisory_id, a.source_id
        FROM match m
        JOIN advisory a ON a.id = m.advisory_id
        LEFT JOIN emission e
          ON e.match_id = m.id AND e.channel = ?
        WHERE e.id IS NULL
          AND m.state = 'open'
          AND {_SEVERITY_CASE_SQL} >= ?
        ORDER BY COALESCE(a.modified, 0) DESC, a.source_id ASC
        """,
        (sub_channel, min_level),
    ).fetchall()
    return [(int(r[0]), str(r[1])) for r in rows]


def _baseline_state_key(sub_channel: str) -> str:
    return f"telegram.baseline_done.{sub_channel}"


def baseline_if_first_run(
    conn: sqlite3.Connection,
    sub_channel: str,
    min_level: int,
) -> int:
    """On first call per `sub_channel`, insert `emission` rows for all open
    matches at or above `min_level` without actually sending. Returns the
    number of emission rows inserted (0 on subsequent calls).

    Idempotent via poller_state key `telegram.baseline_done.<sub_channel>`.
    """
    key = _baseline_state_key(sub_channel)
    existing = conn.execute(
        "SELECT 1 FROM poller_state WHERE key = ?", (key,),
    ).fetchone()
    if existing:
        return 0

    now = int(time.time())
    with conn:
        cur = conn.execute(
            f"""
            INSERT INTO emission (match_id, channel, emitted_at, artifact_path)
            SELECT m.id, ?, ?, 'baseline'
            FROM match m
            JOIN advisory a ON a.id = m.advisory_id
            LEFT JOIN emission e
              ON e.match_id = m.id AND e.channel = ?
            WHERE e.id IS NULL
              AND m.state = 'open'
              AND {_SEVERITY_CASE_SQL} >= ?
            """,
            (sub_channel, now, sub_channel, min_level),
        )
        inserted = cur.rowcount
        conn.execute(
            """
            INSERT INTO poller_state (key, value, updated_at)
            VALUES (?, '1', ?)
            ON CONFLICT(key) DO UPDATE SET value='1', updated_at=excluded.updated_at
            """,
            (key, now),
        )
    return inserted
