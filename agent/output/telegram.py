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
