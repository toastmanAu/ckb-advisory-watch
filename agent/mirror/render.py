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

import logging
import shutil
import sqlite3
from pathlib import Path
from urllib.parse import quote

from jinja2 import Environment, FileSystemLoader, select_autoescape

from agent.dashboard import queries
from agent.dashboard.queries import AdvisoryContext, MatchRow
from agent.dashboard.server import TEMPLATES_DIR, STATIC_DIR, _ago

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
    """Return 'Severity: UPPER (CVSS X.X)' or 'Severity: UPPER' if cvss is None."""
    sev = (advisory.severity or "unknown").upper()
    if advisory.cvss is not None:
        return f"Severity: {sev} (CVSS {advisory.cvss:.1f})"
    return f"Severity: {sev}"


log = logging.getLogger(__name__)


def _make_mirror_env(base_url: str) -> Environment:
    """Jinja env with mirror=True baked in + mailto_href bound to base_url.

    Autoescape is ON for .html (same as the private dashboard) so any
    user-controlled field (summary, ref URL, dep_name) is escaped by
    default. Our mailto: anchors use the implicit default escape —
    no raw markers anywhere in mirror templates."""
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.globals["ago_label"] = _ago

    # Templates call mailto_href(advisory_id_or_advisory, match) — but from
    # the index/project templates we only have the match row's advisory_id
    # (an int), not the full AdvisoryContext. Look up the context by id on
    # demand, cached per render_all pass.
    adv_cache: dict[int, queries.AdvisoryContext | None] = {}

    def _mailto(advisory_ref, match=None) -> str:
        # advisory_ref can be an AdvisoryContext (from the advisory page)
        # or an advisory_id int (from index/project rows).
        if isinstance(advisory_ref, queries.AdvisoryContext):
            adv = advisory_ref
        else:
            key = advisory_ref
            if key not in adv_cache:
                # Need source_id to call advisory_context; look it up.
                row = env.globals["_conn"].execute(
                    "SELECT source_id FROM advisory WHERE id = ?", (key,)
                ).fetchone()
                if row is None:
                    return "mailto:?subject=advisory%20not%20found"
                adv_cache[key] = queries.advisory_context(env.globals["_conn"], row[0])
            adv = adv_cache[key]
            if adv is None:
                return "mailto:?subject=advisory%20not%20found"
        return mailto_href(adv, match, base_url=base_url)

    env.globals["mailto_href"] = _mailto
    return env


def render_all(
    conn: sqlite3.Connection,
    out_dir: Path,
    *,
    severity_floor: tuple[str, ...] = ("critical", "high", "medium"),
    base_url: str = "",
) -> int:
    """Render the full mirror into out_dir. Returns the number of HTML
    pages written.

    Creates out_dir if missing. Reuses existing files (overwrites) so
    repeated runs are idempotent. Does not remove stale files from
    previous passes — Wrangler's deploy replaces the site wholesale, so
    stale pages are a non-issue in production."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    env = _make_mirror_env(base_url)
    env.globals["_conn"] = conn  # consumed by the mailto lookup closure

    # Shared context: all pages show KPIs + top-bar timestamps.
    landing = queries.landing_data(
        conn,
        triage_severities=severity_floor or queries.DEFAULT_TRIAGE_SEVERITIES,
    )
    base_ctx = dict(
        kpis=landing.kpis,
        hostname="advisories.wyltekindustries.com",
        last_osv_ingest_label=_ago(landing.last_osv_ingest),
        last_walk_label=_ago(landing.last_github_walk),
        flash=None,
        mirror=True,
    )

    pages_written = 0

    # --- Index ---
    index_tmpl = env.get_template("index.html")
    (out_dir / "index.html").write_text(
        index_tmpl.render(
            **base_ctx,
            triage=landing.triage,
            top_projects=landing.top_projects,
            top_advisories=landing.top_advisories,
            active_sev=None,
        )
    )
    pages_written += 1

    # --- Project pages ---
    proj_tmpl = env.get_template("project.html")
    proj_slugs = [r[0] for r in conn.execute("SELECT slug FROM project").fetchall()]
    for slug in proj_slugs:
        ctx = queries.project_context(conn, slug, severity_floor=severity_floor)
        if ctx is None:
            continue
        owner, repo = slug.split("/", 1)
        page_dir = out_dir / "p" / owner / repo
        page_dir.mkdir(parents=True, exist_ok=True)
        (page_dir / "index.html").write_text(
            proj_tmpl.render(
                **base_ctx,
                project=ctx,
                active_severity_filter="",
            )
        )
        pages_written += 1

    # --- Advisory pages (only floor-qualifying) ---
    adv_tmpl = env.get_template("advisory.html")
    placeholders = ",".join("?" for _ in severity_floor)
    rows = conn.execute(
        f"""
        SELECT DISTINCT a.source_id
        FROM advisory a
        JOIN match m ON m.advisory_id = a.id
        WHERE m.state = 'open'
          AND COALESCE(a.severity, 'unknown') IN ({placeholders})
        """,
        tuple(severity_floor),
    ).fetchall()
    for (source_id,) in rows:
        adv_ctx = queries.advisory_context(conn, source_id)
        if adv_ctx is None:
            continue
        page_dir = out_dir / "a" / source_id
        page_dir.mkdir(parents=True, exist_ok=True)
        (page_dir / "index.html").write_text(
            adv_tmpl.render(**base_ctx, advisory=adv_ctx)
        )
        pages_written += 1

    # --- Static assets ---
    static_out = out_dir / "static"
    static_out.mkdir(parents=True, exist_ok=True)
    for f in STATIC_DIR.iterdir():
        if f.is_file() and not f.name.startswith("."):
            shutil.copy2(f, static_out / f.name)

    log.info("rendered %d pages into %s", pages_written, out_dir)
    return pages_written
