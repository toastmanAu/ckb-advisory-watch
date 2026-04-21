"""Static-mirror rendering.

render_all(conn, out_dir, min_severity) walks the dashboard's URL tree and
writes one .html file per route into out_dir. Uses the private dashboard's
Jinja templates via `agent/dashboard/templates/`, passing a `mirror=True`
context flag so the templates swap POST-form share buttons for mailto:
anchors.

mailto_href(advisory, match=None, base_url="") builds the RFC 6068 mailto:
URL that those anchors link to. URL-encoded per RFC 6068 (%20 for space,
not '+' which is form-encoding).
"""
from __future__ import annotations

from urllib.parse import quote

from agent.dashboard.queries import AdvisoryContext, MatchRow

# Conservative body cap. Most mail clients cap mailto: URLs around 2000
# chars; we leave room for the scheme + subject + percent-encoding overhead.
_MAX_BODY_CHARS = 1900


def mailto_href(
    advisory: AdvisoryContext,
    match: MatchRow | None = None,
    base_url: str = "",
) -> str:
    """Build a `mailto:?subject=…&body=…` URL for an advisory or a single
    match. No recipient — users compose in their own mail client.

    RFC 6068 mailto: bodies require %-encoding. We use urllib.parse.quote
    (NOT quote_plus) so spaces become %20, not '+'; Gmail and Apple Mail
    otherwise show a literal '+' in the composed message."""
    if match is not None:
        subject = (
            f"[CKB advisory] {match.source_id} — "
            f"{match.dep_name}@{match.dep_version} in {match.project_slug}"
        )
        lines = [
            f"Advisory: {advisory.source_id}",
            _severity_line(advisory),
            f"Summary: {advisory.summary}",
            f"Affected: {match.dep_name}@{match.dep_version} in {match.project_slug}",
        ]
        if match.fixed_in:
            lines.append(f"Fixed in: {match.fixed_in}")
    else:
        fix_part = f" — fix in {advisory.fixed_in}" if advisory.fixed_in else ""
        match_count = len(advisory.matches)
        subject = (
            f"[CKB advisory] {advisory.source_id}{fix_part} "
            f"({match_count} matches)"
        )
        lines = [
            f"Advisory: {advisory.source_id}",
            _severity_line(advisory),
            f"Summary: {advisory.summary}",
            f"Affected projects: {match_count}",
        ]

    if base_url:
        lines.append("")  # blank line before URL
        lines.append(f"{base_url.rstrip('/')}/a/{advisory.source_id}/")

    body = "\n".join(lines)
    if len(body) > _MAX_BODY_CHARS:
        body = body[: _MAX_BODY_CHARS].rstrip() + "…"

    return f"mailto:?subject={quote(subject, safe='')}&body={quote(body, safe='')}"


def _severity_line(advisory: AdvisoryContext) -> str:
    sev = (advisory.severity or "unknown").upper()
    if advisory.cvss is not None:
        return f"Severity: {sev} (CVSS {advisory.cvss:.1f})"
    return f"Severity: {sev}"
