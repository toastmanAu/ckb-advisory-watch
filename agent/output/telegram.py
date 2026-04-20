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
