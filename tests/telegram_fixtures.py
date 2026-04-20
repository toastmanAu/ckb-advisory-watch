"""Shared fixtures for Telegram tests — AdvisoryContext, MatchRow, config builders."""
from __future__ import annotations

from agent.dashboard.queries import AdvisoryContext, MatchRow


def make_match(
    *, match_id: int = 1, advisory_id: int = 1, source_id: str = "GHSA-test",
    severity: str | None = "critical", cvss: float | None = 9.8,
    summary: str = "Test", project_slug: str = "x/y", project_display_name: str = "x/y",
    ecosystem: str = "crates.io", dep_name: str = "pkg", dep_version: str = "1.0.0",
    fixed_in: str | None = "2.0.0", first_matched: int = 1712000000,
) -> MatchRow:
    return MatchRow(
        match_id, advisory_id, source_id, severity, cvss, summary,
        project_slug, project_display_name, ecosystem, dep_name, dep_version,
        fixed_in, first_matched,
    )


def make_advisory(
    *, source_id: str = "GHSA-6p3c-v8vc-c244", severity: str | None = "critical",
    cvss: float | None = 9.8,
    summary: str = "Partial read in molecule's total_size function for FixVec "
                   "allows incorrect length deserialization.",
    details: str = "Full details here.",
    modified: int = 1712000000,
    cve_ids: list[str] | None = None,
    references: list[dict] | None = None,
    fixed_in: str | None = "0.7.3",
    matches: list[MatchRow] | None = None,
) -> AdvisoryContext:
    return AdvisoryContext(
        advisory_id=1,
        source_id=source_id,
        severity=severity,
        cvss=cvss,
        summary=summary,
        details=details,
        modified=modified,
        cve_ids=cve_ids or ["CVE-2022-39303"],
        references=references if references is not None else [{"type": "ADVISORY", "url": "https://example.com/ghsa"}],
        fixed_in=fixed_in,
        matches=matches or [
            make_match(match_id=1, source_id=source_id, project_slug="a/one"),
            make_match(match_id=2, source_id=source_id, project_slug="b/two", dep_version="1.0.1"),
        ],
    )


def make_config(
    *, base_url: str = "http://test.local:8080",
    chat_id: str = "", channel_id: str = "",
    min_severity: str = "medium", bot_token: str = "fake-token",
    enabled: bool = True, poll_seconds: int = 30,
) -> dict:
    return {
        "outputs": {
            "telegram": {
                "enabled": enabled,
                "bot_token": bot_token,
                "chat_id": chat_id,
                "channel_id": channel_id,
                "min_severity": min_severity,
            }
        },
        "dashboard": {"base_url": base_url},
        "poll": {"telegram": poll_seconds},
    }
