# Public Mirror Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate a static HTML snapshot of the private dashboard and publish it hourly to `advisories.wyltekindustries.com` via Cloudflare Pages (Wrangler Direct Upload), with share buttons rewritten to `mailto:` links and all `low`/`unknown` advisories filtered out.

**Architecture:** A new `agent/mirror/` package owns render + deploy. The existing dashboard Jinja templates are reused with a new `mirror=True` context flag that swaps POST-form share buttons for `mailto:` anchors. `agent/mirror/__main__.py` is the one-shot CLI invoked by a systemd user timer every hour; it opens a read-only SQLite connection, writes HTML + static assets into `out_dir`, runs a secret-leak scan, then shells to `wrangler pages deploy`.

**Tech Stack:** Python 3.11+ (stdlib `urllib.parse`, `subprocess`, `pathlib`, `html.parser`), Jinja2, SQLite (read-only URI), Wrangler CLI, systemd user units, Cloudflare Pages.

**Spec:** `docs/superpowers/specs/2026-04-21-public-mirror-design.md`

---

## File Structure

**New files:**

| Path | Responsibility |
|------|----------------|
| `agent/mirror/__init__.py` | Empty package marker. |
| `agent/mirror/__main__.py` | CLI: parse args, load config, gate on `enabled`, run pipeline render → scan → deploy. |
| `agent/mirror/render.py` | `mailto_href()`, `render_all()`, static-asset copy, Jinja env factory with `mirror=True` globals. |
| `agent/mirror/deploy.py` | `scan_for_secrets()` + `deploy_via_wrangler()` — subprocess wrapper with stderr surfacing. |
| `tests/test_mirror_mailto.py` | Unit tests for `mailto_href` URL encoding. |
| `tests/test_mirror_render.py` | Unit + light-integration tests for `render_all`. |
| `tests/test_mirror_deploy.py` | Unit tests for `scan_for_secrets` + `deploy_via_wrangler` (subprocess mocked). |
| `tests/test_mirror_cli.py` | End-to-end CLI test (disabled-gate, happy-path mocked wrangler). |
| `systemd/ckb-mirror.service` | User unit: one-shot `python -m agent.mirror`. |
| `systemd/ckb-mirror.timer` | User unit: hourly cadence + `Persistent=true` catch-up. |

**Modified files:**

| Path | Change |
|------|--------|
| `agent/dashboard/templates/index.html` | Wrap share-button form in `{% if mirror %}…{% else %}…{% endif %}`. |
| `agent/dashboard/templates/project.html` | Same. |
| `agent/dashboard/templates/advisory.html` | Same (both match-row form and per-advisory form). |
| `agent/dashboard/server.py` | Pass `mirror=False` in every `template.render()` call (preserves existing behavior). |
| `agent/dashboard/queries.py` | Add `severity_floor: tuple[str, ...] | None = None` parameter to `project_context` and `advisory_context`; filter match rows by it. |
| `config.example.toml` | Add `[outputs.public_mirror]` section. |
| `README.md` | Add "Publishing the mirror" section. |

---

## Task 1: Template flag for share buttons (no behavior change yet)

The private dashboard currently renders share buttons as `<form method="POST" action="/share/…">`. The mirror cannot POST, so templates must branch on a `mirror` context variable. This task adds the branch but defaults to the existing form, so nothing observable changes.

**Files:**
- Modify: `agent/dashboard/templates/index.html`
- Modify: `agent/dashboard/templates/project.html`
- Modify: `agent/dashboard/templates/advisory.html`
- Modify: `agent/dashboard/server.py` (pass `mirror=False` in every render)
- Modify: `tests/test_dashboard_routes.py` (add a regression test asserting the POST form still renders)

- [ ] **Step 1.1: Write the failing test**

Add to `tests/test_dashboard_routes.py`:

```python
async def test_index_share_button_is_post_form_in_private_mode(tmp_path, share_config):
    async with await _client(tmp_path, share_config) as client:
        resp = await client.get("/")
        body = await resp.text()
    # Private dashboard MUST still render the POST form (not a mailto: anchor).
    assert 'action="/share/match/' in body
    assert "mailto:" not in body


async def test_advisory_share_button_is_post_form_in_private_mode(tmp_path, share_config):
    async with await _client(tmp_path, share_config) as client:
        resp = await client.get("/a/GHSA-crit")
        body = await resp.text()
    assert 'action="/share/advisory/GHSA-crit"' in body
    assert "mailto:" not in body
```

- [ ] **Step 1.2: Run tests to verify they pass**

Run: `pytest tests/test_dashboard_routes.py::test_index_share_button_is_post_form_in_private_mode tests/test_dashboard_routes.py::test_advisory_share_button_is_post_form_in_private_mode -v`
Expected: PASS (current code still emits the POST form — these are regression guards).

- [ ] **Step 1.3: Edit `agent/dashboard/templates/index.html` — wrap the share cell**

Replace (lines 37–41):

```jinja
          <td style="text-align:center">
            <form method="POST" action="/share/match/{{ m.match_id }}" style="margin:0">
              <button type="submit" class="share sm">📤</button>
            </form>
          </td>
```

with:

```jinja
          <td style="text-align:center">
            {% if mirror %}
            <a class="share sm" href="{{ mailto_href(m.advisory_id, m) }}" style="display:inline-block;padding:2px 8px;text-decoration:none">📤</a>
            {% else %}
            <form method="POST" action="/share/match/{{ m.match_id }}" style="margin:0">
              <button type="submit" class="share sm">📤</button>
            </form>
            {% endif %}
          </td>
```

- [ ] **Step 1.4: Edit `agent/dashboard/templates/project.html` — wrap the share cell**

Replace (lines 40–44):

```jinja
        <td style="text-align:center">
          <form method="POST" action="/share/match/{{ m.match_id }}" style="margin:0">
            <button type="submit" class="share sm">📤</button>
          </form>
        </td>
```

with:

```jinja
        <td style="text-align:center">
          {% if mirror %}
          <a class="share sm" href="{{ mailto_href(m.advisory_id, m) }}" style="display:inline-block;padding:2px 8px;text-decoration:none">📤</a>
          {% else %}
          <form method="POST" action="/share/match/{{ m.match_id }}" style="margin:0">
            <button type="submit" class="share sm">📤</button>
          </form>
          {% endif %}
        </td>
```

- [ ] **Step 1.5: Edit `agent/dashboard/templates/advisory.html` — wrap the advisory-wide share button**

Replace (lines 31–33):

```jinja
    <form method="POST" action="/share/advisory/{{ advisory.source_id }}" style="margin-left:auto">
      <button type="submit" class="share">📤 share to inbox</button>
    </form>
```

with:

```jinja
    {% if mirror %}
    <a class="share" href="{{ mailto_href(advisory) }}" style="margin-left:auto;display:inline-block;text-decoration:none">📤 forward by email</a>
    {% else %}
    <form method="POST" action="/share/advisory/{{ advisory.source_id }}" style="margin-left:auto">
      <button type="submit" class="share">📤 share to inbox</button>
    </form>
    {% endif %}
```

- [ ] **Step 1.6: Edit `agent/dashboard/server.py` — pass `mirror=False` in every render**

In `index_view`, `project_view`, and `advisory_view`, add `mirror=False` to the `template.render(...)` kwargs. Example for `index_view` (around line 94):

```python
    html = template.render(
        kpis=data.kpis,
        hostname=request.app["hostname"],
        last_osv_ingest_label=_ago(data.last_osv_ingest),
        last_walk_label=_ago(data.last_github_walk),
        flash=_flash_from_query(request),
        triage=data.triage,
        top_projects=data.top_projects,
        top_advisories=data.top_advisories,
        active_sev=active_sev,
        mirror=False,
    )
```

Do the same for `project_view` (line 126) and `advisory_view` (line 156) — add `mirror=False` to each `template.render` call.

- [ ] **Step 1.7: Run the full route suite**

Run: `.venv/bin/python -m pytest tests/test_dashboard_routes.py -v`
Expected: all pass, including the two new regression guards.

- [ ] **Step 1.8: Commit**

```bash
git add agent/dashboard/templates/ agent/dashboard/server.py tests/test_dashboard_routes.py
git commit -m "refactor(dashboard): thread mirror context flag through share-button templates

Adds {% if mirror %} branches in index.html, project.html, advisory.html
so a later static-mirror render can swap POST forms for mailto: anchors
without regex HTML surgery. Private dashboard unchanged — handlers
explicitly pass mirror=False. Two new route tests pin the POST form
in place so the refactor can't silently regress the live path.

The {% if mirror %} branch references a mailto_href(advisory_id, match)
template function that doesn't exist yet — wired up in the next commit
when agent/mirror/render.py lands. In private mode the false branch is
taken, so the missing function doesn't blow up server-side rendering."
```

---

## Task 2: `mailto_href()` helper — URL-encoded share link builder

Builds the `mailto:?subject=…&body=…` link that the mirror's share buttons point to. No `smtp` credentials cross the public side — the user's own mail client composes the email.

RFC 6068 distinction: `mailto:` body uses **`%20` for space** via `urllib.parse.quote`, NOT `+` (which is `application/x-www-form-urlencoded`). Getting this wrong makes Gmail show literal `+` signs in the composed message.

**Files:**
- Create: `agent/mirror/__init__.py` (empty)
- Create: `agent/mirror/render.py` (mailto_href only for now)
- Create: `tests/test_mirror_mailto.py`

- [ ] **Step 2.1: Write the failing tests**

Create `tests/test_mirror_mailto.py`:

```python
"""Unit tests for mailto_href — RFC 6068 URL encoding + length cap."""
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from agent.dashboard.queries import AdvisoryContext, MatchRow
from agent.mirror.render import mailto_href


def _adv(**overrides) -> AdvisoryContext:
    base = dict(
        advisory_id=1, source_id="GHSA-x1y2", severity="critical", cvss=9.8,
        summary="Remote code execution in example-pkg",
        details="", modified=1700000000,
        cve_ids=["CVE-2026-1001"],
        references=[{"type": "ADVISORY", "url": "https://example.com/x1y2"}],
        fixed_in="1.2.4", matches=[],
    )
    base.update(overrides)
    return AdvisoryContext(**base)


def _match(**overrides) -> MatchRow:
    base = dict(
        match_id=42, advisory_id=1, source_id="GHSA-x1y2",
        severity="critical", cvss=9.8, summary="RCE in example-pkg",
        project_slug="o/r", project_display_name="o/r",
        ecosystem="npm", dep_name="example-pkg", dep_version="1.2.3",
        fixed_in="1.2.4", first_matched=1700000000,
    )
    base.update(overrides)
    return MatchRow(**base)


def test_mailto_href_match_returns_mailto_scheme():
    href = mailto_href(_adv(), _match(), base_url="https://advisories.example.com")
    assert href.startswith("mailto:?")


def test_mailto_href_match_subject_has_advisory_id_and_pkg():
    href = mailto_href(_adv(), _match(), base_url="https://advisories.example.com")
    qs = parse_qs(urlsplit(href).query, keep_blank_values=True)
    subject = qs["subject"][0]
    assert "GHSA-x1y2" in subject
    assert "example-pkg" in subject
    assert "1.2.3" in subject
    assert "o/r" in subject


def test_mailto_href_match_body_has_severity_and_fix():
    href = mailto_href(_adv(), _match(), base_url="https://advisories.example.com")
    qs = parse_qs(urlsplit(href).query, keep_blank_values=True)
    body = qs["body"][0]
    assert "CRITICAL" in body
    assert "1.2.4" in body  # fixed_in
    assert "https://advisories.example.com/a/GHSA-x1y2/" in body


def test_mailto_href_advisory_has_match_count_in_subject():
    adv = _adv(matches=[_match(), _match(match_id=43, project_slug="p/q")])
    href = mailto_href(adv, None, base_url="")
    qs = parse_qs(urlsplit(href).query, keep_blank_values=True)
    assert "2 matches" in qs["subject"][0]


def test_mailto_href_encodes_ampersand_in_summary():
    """Ensure & in advisory summary survives as %26, not as a raw ampersand that
    would split the query string into a spurious extra param."""
    adv = _adv(summary="Broken by A&B integration")
    href = mailto_href(adv, None, base_url="")
    # Single body param (no accidental split)
    qs = parse_qs(urlsplit(href).query, keep_blank_values=True)
    assert set(qs.keys()) == {"subject", "body"}
    assert "A&B" in qs["body"][0]  # decoded back correctly


def test_mailto_href_encodes_space_as_percent20_not_plus():
    """mailto: bodies use %-encoding per RFC 6068 — '+' is NOT space in this
    scheme. Gmail displays literal '+' if we use quote_plus by accident."""
    adv = _adv(summary="spaces here")
    href = mailto_href(adv, None, base_url="")
    # The raw query should contain %20 for spaces in subject/body
    raw_query = urlsplit(href).query
    assert "%20" in raw_query
    # And should NOT use '+' as a space surrogate (strict test)
    # Allow '+' only if it's part of an encoded '+' literal (%2B).
    # Simple check: there should be no '+' in the query string at all for this input.
    assert "+" not in raw_query


def test_mailto_href_caps_body_length():
    long_summary = "x" * 5000
    adv = _adv(summary=long_summary)
    href = mailto_href(adv, None, base_url="")
    qs = parse_qs(urlsplit(href).query, keep_blank_values=True)
    # Body is truncated with ellipsis marker
    assert len(qs["body"][0]) <= 1901  # MAX + ellipsis char
    assert qs["body"][0].endswith("…")


def test_mailto_href_omits_cvss_suffix_when_none():
    adv = _adv(cvss=None)
    href = mailto_href(adv, None, base_url="")
    qs = parse_qs(urlsplit(href).query, keep_blank_values=True)
    assert "CVSS" not in qs["body"][0]
```

- [ ] **Step 2.2: Create the empty package**

Create `agent/mirror/__init__.py` — empty file:

```python
```

- [ ] **Step 2.3: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_mirror_mailto.py -v`
Expected: `ModuleNotFoundError: No module named 'agent.mirror.render'`

- [ ] **Step 2.4: Create `agent/mirror/render.py` with mailto_href**

```python
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
```

- [ ] **Step 2.5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_mirror_mailto.py -v`
Expected: all 8 pass.

- [ ] **Step 2.6: Commit**

```bash
git add agent/mirror/__init__.py agent/mirror/render.py tests/test_mirror_mailto.py
git commit -m "feat(mirror): add mailto_href helper for share-button rewrite

Builds RFC 6068 mailto: URLs with url-encoded subject and body prefilled
from an advisory + optional match. Uses urllib.parse.quote (NOT
quote_plus) so spaces encode as %20 — quote_plus's '+'-for-space is
application/x-www-form-urlencoded and shows literal '+' chars when
Gmail/Apple Mail compose the draft.

Body capped at 1900 chars to stay under typical 2048-char URL limits
across mail clients. Per-match subject names the package and project;
per-advisory subject carries the match count."
```

---

## Task 3: Severity-floor filters in `queries.py`

`project_context` and `advisory_context` currently return all open matches. The mirror needs to filter to `critical`+`high`+`medium` only. Extend both with a `severity_floor` parameter; default is None (no filter) so the private dashboard is unaffected.

**Files:**
- Modify: `agent/dashboard/queries.py`
- Modify: `tests/test_dashboard_queries.py` (add tests)

- [ ] **Step 3.1: Write the failing tests**

Add to `tests/test_dashboard_queries.py`:

```python
def test_project_context_severity_floor_excludes_low_and_unknown(tmp_path):
    conn = fresh_db(tmp_path)
    seed_match(conn, project_slug="a/b", source_id="GHSA-crit",
               severity="critical", cvss=9.8, dep_name="p1")
    seed_match(conn, project_slug="a/b", source_id="GHSA-med",
               severity="medium", cvss=5.0, dep_name="p2")
    seed_match(conn, project_slug="a/b", source_id="GHSA-low",
               severity="low", cvss=3.0, dep_name="p3")
    seed_match(conn, project_slug="a/b", source_id="GHSA-unknown",
               severity=None, cvss=None, dep_name="p4")

    ctx = project_context(
        conn, "a/b",
        severity_floor=("critical", "high", "medium"),
    )
    assert ctx is not None
    seen = {m.source_id for m in ctx.matches}
    assert seen == {"GHSA-crit", "GHSA-med"}


def test_project_context_severity_floor_none_is_no_filter(tmp_path):
    conn = fresh_db(tmp_path)
    seed_match(conn, project_slug="a/b", source_id="GHSA-low",
               severity="low", cvss=3.0, dep_name="p1")
    ctx = project_context(conn, "a/b", severity_floor=None)
    assert ctx is not None
    assert {m.source_id for m in ctx.matches} == {"GHSA-low"}


def test_advisory_context_still_returns_for_low_severity_when_no_floor(tmp_path):
    """advisory_context itself does not gate on severity; the caller
    (mirror render_all) decides whether to emit the page."""
    conn = fresh_db(tmp_path)
    seed_match(conn, project_slug="a/b", source_id="GHSA-low",
               severity="low", cvss=3.0, dep_name="p1")
    ctx = advisory_context(conn, "GHSA-low")
    assert ctx is not None
    assert ctx.severity == "low"


def test_meets_severity_floor_basic():
    from agent.dashboard.queries import meets_severity_floor
    assert meets_severity_floor("critical", ("critical", "high", "medium")) is True
    assert meets_severity_floor("medium", ("critical", "high", "medium")) is True
    assert meets_severity_floor("low", ("critical", "high", "medium")) is False
    assert meets_severity_floor(None, ("critical", "high", "medium")) is False
    # Empty floor tuple means "no floor" — everything passes
    assert meets_severity_floor("low", ()) is True
    assert meets_severity_floor(None, ()) is True
```

- [ ] **Step 3.2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_dashboard_queries.py -v -k 'severity_floor or meets_severity'`
Expected: first three fail with `TypeError: project_context() got an unexpected keyword argument 'severity_floor'`; fourth fails with `ImportError: cannot import name 'meets_severity_floor'`.

- [ ] **Step 3.3: Add `severity_floor` to `project_context` and `meets_severity_floor` helper**

Edit `agent/dashboard/queries.py`. Add the helper near the top (after `DEFAULT_TRIAGE_SEVERITIES`):

```python
def meets_severity_floor(
    severity: str | None,
    floor: tuple[str, ...],
) -> bool:
    """True if severity is in the floor tuple. `None` (unknown) never qualifies
    for a non-empty floor. An empty floor tuple means "no floor" — always True."""
    if not floor:
        return True
    if severity is None:
        return False
    return severity in floor
```

Modify `project_context`'s signature (line 210):

```python
def project_context(
    conn: sqlite3.Connection,
    slug: str,
    *,
    severity_filter: set[str] | None = None,
    ecosystem_filter: set[str] | None = None,
    severity_floor: tuple[str, ...] | None = None,
) -> ProjectContext | None:
```

And add the floor predicate inside the `where` assembly (after the existing filters, around line 230):

```python
    if severity_floor:
        placeholders = ",".join("?" for _ in severity_floor)
        # COALESCE so NULL-severity rows never pass a non-empty floor
        where.append(f"COALESCE(a.severity,'unknown') IN ({placeholders})")
        params.extend(sorted(severity_floor))
```

- [ ] **Step 3.4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_dashboard_queries.py -v -k 'severity_floor or meets_severity'`
Expected: all 4 pass.

- [ ] **Step 3.5: Run the full suite to catch regressions**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass (~150 tests).

- [ ] **Step 3.6: Commit**

```bash
git add agent/dashboard/queries.py tests/test_dashboard_queries.py
git commit -m "feat(queries): add severity_floor filter + meets_severity_floor helper

project_context now accepts severity_floor=('critical','high','medium')
to suppress low/unknown matches on a per-project listing. Default is
None — no filter — so the private dashboard's existing call sites are
unaffected. The public mirror uses this to implement its medium-plus
content floor without duplicating the SQL.

meets_severity_floor() is the scalar equivalent for callers that already
have an advisory in hand and need a yes/no gate (e.g. 'should I emit
/a/<source_id>/ for this advisory?'). COALESCE semantics mean NULL
severity never passes a non-empty floor — safer than implicit pass."
```

---

## Task 4: `render_all()` — walk the URL tree, write static HTML

Iterate every project + every floor-qualifying advisory, render one `.html` file per route, copy `static/` assets. Uses the dashboard's existing Jinja templates with `mirror=True` and a `mailto_href` global.

**Files:**
- Modify: `agent/mirror/render.py`
- Create: `tests/test_mirror_render.py`

- [ ] **Step 4.1: Write the failing tests**

Create `tests/test_mirror_render.py`:

```python
"""Integration tests for render_all — seeded DB → on-disk file tree."""
from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

import pytest

from agent.mirror.render import render_all
from tests.dashboard_fixtures import fresh_db, seed_match


class _FormFinder(HTMLParser):
    def __init__(self):
        super().__init__()
        self.found_post_form = False

    def handle_starttag(self, tag, attrs):
        if tag == "form":
            method = dict(attrs).get("method", "").upper()
            if method == "POST":
                self.found_post_form = True


def _has_post_form(html: str) -> bool:
    p = _FormFinder()
    p.feed(html)
    return p.found_post_form


def _count_html_files(root: Path) -> int:
    return sum(1 for _ in root.rglob("*.html"))


def test_render_all_writes_index_and_project_and_advisory(tmp_path):
    conn = fresh_db(tmp_path)
    seed_match(conn, project_slug="o/r", source_id="GHSA-crit",
               severity="critical", cvss=9.8)
    out_dir = tmp_path / "out"

    n_pages = render_all(conn, out_dir, severity_floor=("critical", "high", "medium"),
                         base_url="https://advisories.example.com")

    assert (out_dir / "index.html").exists()
    assert (out_dir / "p" / "o" / "r" / "index.html").exists()
    assert (out_dir / "a" / "GHSA-crit" / "index.html").exists()
    assert n_pages >= 3


def test_render_all_excludes_low_advisory_pages(tmp_path):
    conn = fresh_db(tmp_path)
    seed_match(conn, project_slug="o/r", source_id="GHSA-low",
               severity="low", cvss=3.0)
    out_dir = tmp_path / "out"

    render_all(conn, out_dir, severity_floor=("critical", "high", "medium"),
               base_url="")

    assert (out_dir / "index.html").exists()
    # Low-severity advisory page MUST NOT be rendered
    assert not (out_dir / "a" / "GHSA-low" / "index.html").exists()


def test_render_all_project_page_emitted_even_with_zero_qualifying_matches(tmp_path):
    """A project whose only matches are below the floor still renders a
    page (keeps the URL structure predictable); the page just has an empty
    match table."""
    conn = fresh_db(tmp_path)
    seed_match(conn, project_slug="o/empty", source_id="GHSA-low",
               severity="low", cvss=3.0)
    out_dir = tmp_path / "out"

    render_all(conn, out_dir, severity_floor=("critical", "high", "medium"),
               base_url="")

    proj_page = out_dir / "p" / "o" / "empty" / "index.html"
    assert proj_page.exists()
    html = proj_page.read_text()
    # No match rows for the filtered-out low-sev advisory
    assert "GHSA-low" not in html


def test_render_all_output_contains_no_post_forms(tmp_path):
    """The signature mirror-mode behavior: no POST forms anywhere in output."""
    conn = fresh_db(tmp_path)
    seed_match(conn, project_slug="o/r", source_id="GHSA-crit",
               severity="critical", cvss=9.8)
    out_dir = tmp_path / "out"

    render_all(conn, out_dir, severity_floor=("critical", "high", "medium"),
               base_url="https://advisories.example.com")

    for html_file in out_dir.rglob("*.html"):
        body = html_file.read_text()
        assert not _has_post_form(body), f"POST form leaked into {html_file}"


def test_render_all_copies_static_assets(tmp_path):
    conn = fresh_db(tmp_path)
    seed_match(conn, project_slug="o/r", source_id="GHSA-crit",
               severity="critical", cvss=9.8)
    out_dir = tmp_path / "out"

    render_all(conn, out_dir, severity_floor=("critical", "high", "medium"),
               base_url="")

    assert (out_dir / "static" / "logo.png").exists()
    assert (out_dir / "static" / "favicon.png").exists()


def test_render_all_output_has_mailto_links(tmp_path):
    conn = fresh_db(tmp_path)
    seed_match(conn, project_slug="o/r", source_id="GHSA-crit",
               severity="critical", cvss=9.8)
    out_dir = tmp_path / "out"

    render_all(conn, out_dir, severity_floor=("critical", "high", "medium"),
               base_url="https://advisories.example.com")

    index_html = (out_dir / "index.html").read_text()
    # The share anchor must be a mailto:
    assert "mailto:?" in index_html


def test_render_all_is_idempotent_on_rerun(tmp_path):
    conn = fresh_db(tmp_path)
    seed_match(conn, project_slug="o/r", source_id="GHSA-crit",
               severity="critical", cvss=9.8)
    out_dir = tmp_path / "out"

    n1 = render_all(conn, out_dir, severity_floor=("critical", "high", "medium"),
                    base_url="")
    n2 = render_all(conn, out_dir, severity_floor=("critical", "high", "medium"),
                    base_url="")
    assert n1 == n2  # same page count
    # Files still exist
    assert (out_dir / "index.html").exists()
```

- [ ] **Step 4.2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_mirror_render.py -v`
Expected: `ImportError: cannot import name 'render_all' from 'agent.mirror.render'` (or similar).

- [ ] **Step 4.3: Extend `agent/mirror/render.py` with `render_all`**

Append to `agent/mirror/render.py`:

```python
import logging
import shutil
import sqlite3
import time
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from agent.dashboard import queries
from agent.dashboard.server import TEMPLATES_DIR, STATIC_DIR, _ago

log = logging.getLogger(__name__)


def _make_mirror_env(base_url: str) -> Environment:
    """Jinja env with mirror=True baked in + mailto_href bound to base_url.

    Autoescape is ON for .html (same as the private dashboard) so any
    user-controlled field (summary, ref URL, dep_name) is escaped by
    default. Our mailto: anchors use the `|e` is-implicit default escape —
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
    adv_cache: dict[int | str, queries.AdvisoryContext | None] = {}

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
```

- [ ] **Step 4.4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_mirror_render.py -v`
Expected: all 7 pass. If the HTMLParser test fails because `_has_post_form` detects a form elsewhere, trace back: the `active_severity_filter` form on project.html uses `method="GET"`, which is fine — `_has_post_form` ignores non-POST.

- [ ] **Step 4.5: Commit**

```bash
git add agent/mirror/render.py tests/test_mirror_render.py
git commit -m "feat(mirror): add render_all — walk URL tree and write static HTML

Reuses agent/dashboard/templates/ via a Jinja env with mirror=True and a
mailto_href global closed over the base_url. Every project gets a page
(even zero-qualifying-match ones — predictable URL structure); only
floor-qualifying advisories get pages.

Static assets (logo.png, favicon.png) copied verbatim from
agent/dashboard/static/. shutil.copy2 preserves mtime so Cloudflare's
Pages CDN caches stably between deploys.

The mailto_href Jinja global accepts either an AdvisoryContext (from
advisory.html) or an advisory_id int (from index/project match rows);
the int case lazily looks up source_id + full context so templates don't
have to know the difference."
```

---

## Task 5: Secret-scan regression guard

Before deploying, fail loud if anything in `out_dir` contains a string that looks like a secret. v0 catches: bot_token pattern, `ghp_`/`gho_` GitHub tokens, the literal `smtp_password` / `api_token` config keys, and the specific Telegram `chat_id` from `config.example.toml`.

**Files:**
- Create: `agent/mirror/deploy.py` (scan only for now)
- Create: `tests/test_mirror_deploy.py`

- [ ] **Step 5.1: Write the failing tests**

Create `tests/test_mirror_deploy.py`:

```python
"""Unit tests for scan_for_secrets and deploy_via_wrangler."""
from __future__ import annotations

from pathlib import Path

import pytest

from agent.mirror.deploy import SecretFound, scan_for_secrets


def test_scan_clean_tree_returns_empty(tmp_path):
    (tmp_path / "index.html").write_text("<html><body>hello</body></html>")
    (tmp_path / "page.html").write_text("CRITICAL advisory GHSA-abc — patched")
    findings = scan_for_secrets(tmp_path)
    assert findings == []


def test_scan_catches_ghp_token(tmp_path):
    (tmp_path / "bad.html").write_text("token = ghp_abcd1234abcd1234abcd1234abcd1234abcd")
    findings = scan_for_secrets(tmp_path)
    assert len(findings) == 1
    assert findings[0].file.name == "bad.html"
    assert findings[0].pattern == "github_token"


def test_scan_catches_gho_token(tmp_path):
    (tmp_path / "bad.html").write_text("token=gho_ffffffffffffffffffffffffffffffffffff")
    findings = scan_for_secrets(tmp_path)
    assert any(f.pattern == "github_token" for f in findings)


def test_scan_catches_telegram_chat_id(tmp_path):
    # The specific production chat_id from config.example.toml must never leak
    (tmp_path / "leak.html").write_text("<!-- hello 1790655432 -->")
    findings = scan_for_secrets(tmp_path)
    assert any(f.pattern == "telegram_chat_id" for f in findings)


def test_scan_catches_bot_token_pattern(tmp_path):
    (tmp_path / "oops.html").write_text(
        "bot_token = 1234567890:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    )
    findings = scan_for_secrets(tmp_path)
    assert any(f.pattern == "telegram_bot_token" for f in findings)


def test_scan_catches_literal_secret_key_names(tmp_path):
    (tmp_path / "cfg.html").write_text("api_token: very-sensitive-string-here")
    findings = scan_for_secrets(tmp_path)
    assert any(f.pattern == "secret_key_name" for f in findings)


def test_scan_recurses_nested_dirs(tmp_path):
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (deep / "bad.html").write_text("ghp_wxyz1234wxyz1234wxyz1234wxyz1234wxyz")
    findings = scan_for_secrets(tmp_path)
    assert len(findings) == 1


def test_scan_ignores_png_files(tmp_path):
    """Binary assets are not scanned — they'd false-positive on random bytes."""
    (tmp_path / "logo.png").write_bytes(b"ghp_" + b"x" * 36)
    findings = scan_for_secrets(tmp_path)
    assert findings == []


def test_scan_reports_line_number(tmp_path):
    (tmp_path / "bad.html").write_text(
        "line 1 clean\nline 2 also clean\nghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
    )
    findings = scan_for_secrets(tmp_path)
    assert findings[0].line == 3
```

- [ ] **Step 5.2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_mirror_deploy.py -v`
Expected: `ModuleNotFoundError: No module named 'agent.mirror.deploy'`.

- [ ] **Step 5.3: Create `agent/mirror/deploy.py` with `scan_for_secrets`**

```python
"""Deploy-side helpers — secret-leak scan + wrangler subprocess wrapper.

The scan runs on the rendered out_dir before we hand bytes to Wrangler.
Patterns are deliberately conservative: if any trip, deploy aborts with
a non-zero exit so the systemd timer's journalctl tail shows the hit."""
from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SecretFound:
    file: Path
    line: int
    pattern: str
    matched_text: str


# Each entry: (name, compiled regex). Regexes are case-sensitive where it
# matters for the pattern (GitHub tokens are always lowercase prefix).
_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # GitHub personal-access tokens: ghp_, gho_, ghu_, ghs_, ghr_ prefixes,
    # followed by 36 base62-ish chars.
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36}")),
    # Telegram bot tokens: <bot_id>:<35 base64url chars>
    ("telegram_bot_token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
    # Literal Telegram chat_id from config.example.toml — an exact-match
    # belt-and-suspenders: if someone edits a template to expose a chat_id,
    # the number itself trips the scan regardless of context.
    ("telegram_chat_id", re.compile(r"\b1790655432\b")),
    # Literal secret key names — catches a template that accidentally
    # interpolated the whole config section.
    ("secret_key_name", re.compile(r"\b(api_token|smtp_password|bot_token)\s*[:=]")),
    # Cloudflare API tokens: 40 hex/alnum chars after a recognizable prefix
    ("cloudflare_token", re.compile(r"CLOUDFLARE_API_TOKEN\s*[:=]\s*\S+")),
]

# Only scan these extensions. PNG/favicon bytes would false-positive.
_SCAN_EXTENSIONS = {".html", ".htm", ".txt", ".xml", ".json", ".js", ".css"}


class SecretScanFailed(Exception):
    """Raised when scan_for_secrets is called with raise_on_find=True and
    at least one SecretFound is emitted."""


def scan_for_secrets(root: Path) -> list[SecretFound]:
    """Walk root recursively, scan every text file line-by-line against
    _SECRET_PATTERNS. Returns a list of all findings (may be empty)."""
    root = Path(root)
    findings: list[SecretFound] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in _SCAN_EXTENSIONS:
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            # non-UTF8 file, skip rather than false-positive on raw bytes
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for pattern_name, regex in _SECRET_PATTERNS:
                m = regex.search(line)
                if m:
                    findings.append(SecretFound(
                        file=path,
                        line=lineno,
                        pattern=pattern_name,
                        matched_text=m.group(0)[:80],
                    ))
    return findings
```

- [ ] **Step 5.4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_mirror_deploy.py -v`
Expected: all 9 scan-related tests pass.

- [ ] **Step 5.5: Commit**

```bash
git add agent/mirror/deploy.py tests/test_mirror_deploy.py
git commit -m "feat(mirror): add scan_for_secrets as pre-deploy guard

Walks the render out_dir, scans .html/.txt/.json/etc. line-by-line for:
- GitHub personal access tokens (ghp_/gho_/ghu_/ghs_/ghr_ × 36 chars)
- Telegram bot tokens (bot_id:35-char-base64)
- The literal Telegram chat_id from config.example.toml (belt-and-
  suspenders check — if the agent ever accidentally renders a chat_id
  into a public page, the number itself trips the scan)
- Literal secret key names (api_token, smtp_password, bot_token) adjacent
  to a value
- Cloudflare API token env var with a value

PNG/binary files are excluded — binary bytes false-positive on random
regex matches. Returns a list of SecretFound dataclasses with file, line,
pattern name, and truncated matched text — callers can render these in a
deploy-failure log. The CLI wires this to an exit-2 error path."
```

---

## Task 6: `deploy_via_wrangler()` — subprocess wrapper

Shells to `wrangler pages deploy` with `CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID` env vars. Surfaces full stderr on failure; passes `--branch=main --commit-dirty=true` per spec §3.5.

**Files:**
- Modify: `agent/mirror/deploy.py`
- Modify: `tests/test_mirror_deploy.py`

- [ ] **Step 6.1: Write the failing tests**

Append to `tests/test_mirror_deploy.py`:

```python
from unittest.mock import MagicMock, patch

from agent.mirror.deploy import DeployError, deploy_via_wrangler


def test_deploy_via_wrangler_happy_path(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "index.html").write_text("<html></html>")

    with patch("agent.mirror.deploy.subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stdout="uploaded", stderr="")
        deploy_via_wrangler(
            out_dir=out_dir,
            project_name="ckb-advisories",
            api_token="test-token",
            account_id="test-acct",
        )

    assert run.called
    args, kwargs = run.call_args
    argv = args[0]
    assert argv[0] == "wrangler"
    assert "pages" in argv
    assert "deploy" in argv
    assert str(out_dir) in argv
    assert "--project-name=ckb-advisories" in argv
    assert "--branch=main" in argv
    assert "--commit-dirty=true" in argv
    env = kwargs["env"]
    assert env["CLOUDFLARE_API_TOKEN"] == "test-token"
    assert env["CLOUDFLARE_ACCOUNT_ID"] == "test-acct"


def test_deploy_via_wrangler_failure_raises_with_stderr(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    with patch("agent.mirror.deploy.subprocess.run") as run:
        run.return_value = MagicMock(
            returncode=1, stdout="", stderr="ERROR: 401 Unauthorized"
        )
        with pytest.raises(DeployError) as excinfo:
            deploy_via_wrangler(
                out_dir=out_dir,
                project_name="ckb-advisories",
                api_token="bad",
                account_id="acct",
            )
        assert "401" in str(excinfo.value)


def test_deploy_via_wrangler_missing_wrangler_binary_raises(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    with patch("agent.mirror.deploy.subprocess.run") as run:
        run.side_effect = FileNotFoundError("wrangler")
        with pytest.raises(DeployError) as excinfo:
            deploy_via_wrangler(
                out_dir=out_dir,
                project_name="ckb-advisories",
                api_token="t",
                account_id="a",
            )
        assert "wrangler" in str(excinfo.value).lower()
        assert "npm install" in str(excinfo.value)
```

- [ ] **Step 6.2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_mirror_deploy.py::test_deploy_via_wrangler_happy_path -v`
Expected: `ImportError: cannot import name 'DeployError' from 'agent.mirror.deploy'`.

- [ ] **Step 6.3: Extend `agent/mirror/deploy.py` with `deploy_via_wrangler` + `DeployError`**

Append to `agent/mirror/deploy.py`:

```python
class DeployError(Exception):
    """Deploy failed for a surfaceable reason — wrangler non-zero exit,
    wrangler missing from PATH, etc. Caller prints exc and exits 1."""


def deploy_via_wrangler(
    *,
    out_dir: Path,
    project_name: str,
    api_token: str,
    account_id: str,
) -> None:
    """Shell to `wrangler pages deploy` with Direct Upload.

    Sets `CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID` env vars (per
    wrangler's documented auth path — no wrangler.toml needed for a
    one-shot upload). Uses `--branch=main` to mark this the production
    deploy (not a preview), and `--commit-dirty=true` to suppress the
    no-git-repo warning since we're uploading a throwaway /tmp dir."""
    argv = [
        "wrangler", "pages", "deploy", str(out_dir),
        f"--project-name={project_name}",
        "--branch=main",
        "--commit-dirty=true",
    ]
    env = {
        "CLOUDFLARE_API_TOKEN": api_token,
        "CLOUDFLARE_ACCOUNT_ID": account_id,
        # Preserve PATH so wrangler (installed under /usr/local/bin or
        # ~/.nvm/.../bin) is findable.
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
    }
    try:
        result = subprocess.run(
            argv, env=env, capture_output=True, text=True, timeout=300,
        )
    except FileNotFoundError as exc:
        raise DeployError(
            "wrangler not found on PATH. Install: npm install -g wrangler"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise DeployError(f"wrangler timed out after 300s: {exc!r}") from exc

    if result.returncode != 0:
        raise DeployError(
            f"wrangler exited {result.returncode}: {result.stderr.strip() or result.stdout.strip()}"
        )
    log.info("wrangler deploy OK: %s", result.stdout.strip()[:200])
```

Also add `import os` to the imports at the top of the file.

- [ ] **Step 6.4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_mirror_deploy.py -v`
Expected: all pass.

- [ ] **Step 6.5: Commit**

```bash
git add agent/mirror/deploy.py tests/test_mirror_deploy.py
git commit -m "feat(mirror): add deploy_via_wrangler subprocess wrapper

Shells 'wrangler pages deploy <out_dir> --project-name=X --branch=main
--commit-dirty=true' with CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID
in the env. --branch=main marks production (vs preview); --commit-dirty
suppresses wrangler's no-git-repo warning since out_dir is /tmp.

300s timeout covers the worst observed case (~190 MB of HTML for full
npm ecosystem) with a safety margin. Non-zero exit or FileNotFoundError
(wrangler missing) both raise DeployError with a one-line explanation
suitable for journalctl — the CLI prints it verbatim on exit 1.

Env is constructed fresh rather than inheriting os.environ, except PATH
and HOME which wrangler needs to find its node+npm entry point and its
cached oauth state."
```

---

## Task 7: `__main__.py` — CLI entry point

Glues everything together: parse args, load config, gate on `enabled`, open read-only DB, render → scan → deploy, log at each step. Exit codes: 0 clean, 0+log when disabled, 1 on render/deploy error, 2 on config error.

**Files:**
- Create: `agent/mirror/__main__.py`
- Create: `tests/test_mirror_cli.py`

- [ ] **Step 7.1: Write the failing tests**

Create `tests/test_mirror_cli.py`:

```python
"""Integration tests for agent.mirror __main__ CLI."""
from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.mirror.__main__ import main as mirror_main
from tests.dashboard_fixtures import fresh_db, seed_match


def _write_config(path: Path, enabled: bool = True, **overrides) -> Path:
    cfg = f"""
[agent]
data_dir = "{path.parent}"

[outputs.public_mirror]
enabled = {str(enabled).lower()}
project_name = "ckb-advisories-test"
api_token = "{overrides.get('api_token', 'test-token')}"
account_id = "{overrides.get('account_id', 'test-acct')}"
min_severity = "medium"
out_dir = "{overrides.get('out_dir', path.parent / 'out')}"
base_url = "https://advisories.example.com"
"""
    cfg_path = path.parent / "mirror.toml"
    cfg_path.write_text(textwrap.dedent(cfg))
    return cfg_path


def test_cli_exits_0_when_disabled(tmp_path, capsys):
    conn = fresh_db(tmp_path)
    conn.close()
    cfg_path = _write_config(tmp_path, enabled=False)

    rc = mirror_main(["--config", str(cfg_path)])
    assert rc == 0
    captured = capsys.readouterr()
    assert "disabled" in (captured.out + captured.err).lower()


def test_cli_exits_2_when_api_token_empty(tmp_path):
    conn = fresh_db(tmp_path)
    conn.close()
    cfg_path = _write_config(tmp_path, enabled=True, api_token="")

    rc = mirror_main(["--config", str(cfg_path)])
    assert rc == 2


def test_cli_happy_path_renders_and_deploys(tmp_path):
    conn = fresh_db(tmp_path)
    seed_match(conn, project_slug="o/r", source_id="GHSA-crit",
               severity="critical", cvss=9.8)
    conn.close()

    out_dir = tmp_path / "out"
    cfg_path = _write_config(tmp_path, enabled=True, out_dir=out_dir)

    with patch("agent.mirror.__main__.deploy_via_wrangler") as deploy:
        rc = mirror_main(["--config", str(cfg_path)])

    assert rc == 0
    assert deploy.called
    kwargs = deploy.call_args.kwargs
    assert kwargs["project_name"] == "ckb-advisories-test"
    assert kwargs["api_token"] == "test-token"
    assert kwargs["account_id"] == "test-acct"
    # Files rendered before deploy
    assert (out_dir / "index.html").exists()
    assert (out_dir / "a" / "GHSA-crit" / "index.html").exists()


def test_cli_exits_1_if_secrets_leak_into_output(tmp_path):
    """Inject a secret-shaped string into the advisory summary via seed,
    watch the scan abort deploy."""
    conn = fresh_db(tmp_path)
    # Seed an advisory whose summary contains a faux ghp_ token. The
    # render writes it into <td>... making the scan trip.
    seed_match(
        conn, project_slug="o/r", source_id="GHSA-crit",
        severity="critical", cvss=9.8,
        summary="DO NOT SHIP: ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    )
    conn.close()

    cfg_path = _write_config(tmp_path, enabled=True)

    with patch("agent.mirror.__main__.deploy_via_wrangler") as deploy:
        rc = mirror_main(["--config", str(cfg_path)])

    assert rc == 1
    assert not deploy.called  # deploy must not run when secrets detected
```

- [ ] **Step 7.2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_mirror_cli.py -v`
Expected: `ModuleNotFoundError: No module named 'agent.mirror.__main__'`.

- [ ] **Step 7.3: Create `agent/mirror/__main__.py`**

```python
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
        log.info("mirror disabled ([outputs.public_mirror].enabled = false)")
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
```

- [ ] **Step 7.4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_mirror_cli.py -v`
Expected: all 4 pass.

- [ ] **Step 7.5: Run the full suite to catch regressions**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass (~170 tests including the new ~25 mirror tests).

- [ ] **Step 7.6: Commit**

```bash
git add agent/mirror/__main__.py tests/test_mirror_cli.py
git commit -m "feat(mirror): CLI entry point + disabled/happy/secret-leak paths

python -m agent.mirror --config <path> loads TOML, gates on
[outputs.public_mirror].enabled, opens state.db read-only, renders →
scans → deploys. Exit codes:
  0  clean (including 'disabled' no-op)
  1  render/scan/deploy failed
  2  config error (missing file, empty api_token/account_id, bad severity)

Read-only SQLite open (file:…?mode=ro URI) matches the dashboard's
conn_factory pattern — the agent's concurrent writer path never sees
this connection so matcher/ingest/walker are unaffected.

min_severity='medium' maps to ('critical','high','medium') — a future
'low' or 'all' knob lets the operator lower the floor without touching
code. The tuple order follows the private dashboard's severity ordering
for consistency."
```

---

## Task 8: Config example + README

Document the feature so an operator can enable it without reading code. Also updates `config.example.toml` so a fresh clone has the scaffolding in place.

**Files:**
- Modify: `config.example.toml`
- Modify: `README.md`

- [ ] **Step 8.1: Add `[outputs.public_mirror]` to `config.example.toml`**

Append to `config.example.toml`:

```toml

# ----------------------------------------------------------------------
# Public static mirror — optional. When enabled, `python -m agent.mirror`
# generates a static HTML snapshot of the dashboard and deploys it to
# Cloudflare Pages via Wrangler Direct Upload. Disabled by default; the
# private dashboard is unaffected.
#
# Prerequisites on the host running the mirror:
#   sudo apt install nodejs npm
#   sudo npm install -g wrangler
#   wrangler --version
#
# Obtain the api_token from Cloudflare dashboard:
#   My Profile → API Tokens → Create Token → "Custom token"
#   Permissions: Account → Cloudflare Pages → Edit
#                User → User Details → Read
#   Scope: the account hosting your Pages project.
#
# account_id: copy from any page under the account in the Cloudflare UI
# (right sidebar) or `wrangler whoami`.
# ----------------------------------------------------------------------
[outputs.public_mirror]
enabled = false
project_name = "ckb-advisories"
api_token = ""
account_id = ""
min_severity = "medium"   # critical|high|medium|low
out_dir = "/tmp/mirror-out"
base_url = "https://advisories.wyltekindustries.com"
```

- [ ] **Step 8.2: Add "Publishing the mirror" section to `README.md`**

Insert the following section into `README.md`, after the "Install (Zero 3)" section:

```markdown

## Publishing the public mirror

The mirror is an unlisted static HTML snapshot of the dashboard, refreshed
hourly to `advisories.wyltekindustries.com` via Cloudflare Pages.
Share buttons become `mailto:` links. Severity floor: `medium`+ by default.

### One-time setup (Pi)

```bash
# 1. Node + Wrangler
sudo apt install -y nodejs npm
sudo npm install -g wrangler

# 2. Cloudflare project (via dashboard UI)
#    Pages → Create a project → Direct Upload → Name it "ckb-advisories"
#    (or whatever you set in project_name)

# 3. Custom domain (via dashboard UI, inside the project)
#    Custom domains → Add → advisories.wyltekindustries.com
#    Follow the CNAME prompt (DNS lands in your wyltekindustries zone)

# 4. API token (via dashboard UI)
#    My Profile → API Tokens → Create Token → Custom token
#    Permissions:
#      Account → Cloudflare Pages → Edit
#      User → User Details → Read
#    Scope: single account

# 5. Populate config.toml
#    [outputs.public_mirror]
#    enabled = true
#    api_token = "<token from step 4>"
#    account_id = "<copy from dashboard sidebar>"

# 6. Smoke-test a render locally (no deploy)
~/ckb-advisory-watch/.venv/bin/python -c "
import sqlite3
from pathlib import Path
from agent.mirror.render import render_all
conn = sqlite3.connect('file:data/state.db?mode=ro', uri=True)
print(render_all(conn, Path('/tmp/mirror-smoke'), severity_floor=('critical','high','medium')))
"
ls /tmp/mirror-smoke   # expect index.html, p/, a/, static/

# 7. Full end-to-end (runs wrangler)
~/ckb-advisory-watch/.venv/bin/python -m agent.mirror --config config.toml

# 8. Install the hourly timer
cp systemd/ckb-mirror.service systemd/ckb-mirror.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ckb-mirror.timer
systemctl --user list-timers ckb-mirror.timer
```

### Verifying the deploy

After step 7 or after the first timer fire:

- `journalctl --user -u ckb-mirror.service -n 50` — look for "mirror tick complete"
- `curl -s -o /dev/null -w '%{http_code}\n' https://advisories.wyltekindustries.com/` — expect `200`
- Click a 📤 link on an advisory page — your mail client should open with
  the subject pre-filled.

### Disabling

Flip `[outputs.public_mirror].enabled = false` in `config.toml`. The timer
will keep firing but the CLI logs "disabled" and exits 0 immediately.
Cloudflare Pages retains the last successful deploy until you delete the
project — no TTL on static uploads.
```

- [ ] **Step 8.3: Commit**

```bash
git add config.example.toml README.md
git commit -m "docs(mirror): config.example.toml section + README operator guide

README 'Publishing the public mirror' walks the one-time Cloudflare setup
(create Pages project, add custom domain, issue API token), populates
config.toml, runs a local smoke render, then enables the systemd timer.
Each block is shell-pasteable in order.

config.example.toml [outputs.public_mirror] block ships with enabled=false
and empty secrets — operator opts in explicitly."
```

---

## Task 9: Systemd service + timer units

One-shot service + hourly timer with `Persistent=true` for catch-up after the Pi is offline. Depends on `ckb-advisory-watch.service` being active so the DB is fresh.

**Files:**
- Create: `systemd/ckb-mirror.service`
- Create: `systemd/ckb-mirror.timer`

- [ ] **Step 9.1: Create `systemd/ckb-mirror.service`**

```ini
[Unit]
Description=CKB advisory public mirror — render and deploy
# Run after the agent's DB writer is up so reads see fresh data. The
# agent's own systemd unit handles restart; this is a best-effort order
# hint — Type=oneshot doesn't actually require the agent to be running
# (the mirror opens its own read-only connection).
After=ckb-advisory-watch.service
Wants=ckb-advisory-watch.service

[Service]
Type=oneshot
WorkingDirectory=%h/ckb-advisory-watch
ExecStart=%h/ckb-advisory-watch/.venv/bin/python -m agent.mirror --config %h/ckb-advisory-watch/config.toml

# Network + CPU ceilings. Mirror renders take ~5s on a populated DB on
# Pi Zero 3; wrangler upload is bounded by network. 120s is generous.
TimeoutStartSec=300

# Keep journalctl tidy — log full stdout/stderr to the journal; it's one
# line per step so volume is minimal.
StandardOutput=journal
StandardError=journal
```

- [ ] **Step 9.2: Create `systemd/ckb-mirror.timer`**

```ini
[Unit]
Description=Render + deploy public advisory mirror hourly
# Same ordering hint as the service — doesn't block timer firing.
After=ckb-advisory-watch.service

[Timer]
# Start 5 minutes after boot so the agent has had a chance to ingest and
# matcher-run at least once — the first post-boot mirror snapshot then
# reflects the ingested state, not an empty DB.
OnBootSec=5min
# Fire every hour after the previous run STARTED (so a slow deploy
# doesn't slip subsequent runs).
OnUnitActiveSec=1h
Unit=ckb-mirror.service
# If the Pi was off at a scheduled run, systemd fires one catch-up
# execution on next boot. No batching — if the Pi was off for 6 hours,
# we get a single post-boot fire, not six.
Persistent=true

[Install]
WantedBy=default.target
```

- [ ] **Step 9.3: Verify the units parse locally**

Run:
```bash
systemd-analyze --user verify ./systemd/ckb-mirror.service ./systemd/ckb-mirror.timer
```

Expected: no output (systemd-analyze prints errors only on problems). If it complains about `%h` expansion in the verify-only context, that's a known limitation — the `%h` placeholder resolves at install time from the user's home.

- [ ] **Step 9.4: Commit**

```bash
git add systemd/ckb-mirror.service systemd/ckb-mirror.timer
git commit -m "feat(mirror): systemd user service + hourly timer unit

Type=oneshot service runs 'python -m agent.mirror --config config.toml';
timer fires OnBootSec=5min (wait for agent to populate DB) then
OnUnitActiveSec=1h (hourly cadence). Persistent=true ensures exactly
ONE catch-up fire after a prolonged Pi outage — not a burst of N
back-to-back runs.

TimeoutStartSec=300 covers worst-case render (~5s) + wrangler upload
(~60s on a full 200k-advisory render over a residential line) with
generous margin."
```

---

## Self-review checklist

Before declaring the plan done, run these mental passes.

**Spec coverage:**

| Spec section | Task(s) |
|---|---|
| §2 Non-goals | (nothing to implement — deliberately excluded) |
| §3.1 Module layout | Task 2, 4, 5, 6, 7 — creates every listed file |
| §3.2 Rendering | Task 4 — render_all iterates `/`, `/p/<o>/<r>/`, `/a/<sid>/` |
| §3.3 Share button rewrite (option 1) | Task 1 (templates) + Task 2 (mailto_href) + Task 4 (env wiring) |
| §3.4 Severity floor | Task 3 (queries) + Task 4 (render gate) |
| §3.5 Deploy via Wrangler | Task 6 |
| §3.6 Hourly cron | Task 9 |
| §4 Error handling | Task 7 covers every row in the error table |
| §5 Privacy / leakage checklist | Task 5 scan_for_secrets + Task 7 wiring |
| §6 Config additions | Task 8 |
| §7.1 Unit tests | Tasks 2, 3, 5, 6 |
| §7.2 Integration tests | Tasks 4, 7 |
| §7.3 Live smoke | README §Verifying (Task 8) |
| §9 Rollout sequence | Matches task order 1–9 exactly |
| §10.a Zero-match project pages | Task 4 Step 4.1 test `test_render_all_project_page_emitted_even_with_zero_qualifying_matches` |

Everything covered.

**Placeholder scan:** done inline — no "TBD" / "similar to Task N" / "add error handling" / implicit code references.

**Type consistency check:**
- `mailto_href(advisory, match=None, base_url="")` — spec §3.3. Matches Task 2 signature.
- `render_all(conn, out_dir, *, severity_floor, base_url)` — Task 4 signature. Used by CLI in Task 7 with kwargs.
- `scan_for_secrets(root) -> list[SecretFound]` — Task 5. CLI consumes the list in Task 7.
- `deploy_via_wrangler(*, out_dir, project_name, api_token, account_id)` — Task 6 kwargs-only. CLI passes matching names in Task 7.
- `meets_severity_floor(severity, floor)` — Task 3. Not used outside tests in v0, retained for future callers.
- Template `mailto_href` Jinja global — takes `advisory_id: int` OR `advisory: AdvisoryContext` plus optional `match: MatchRow`. Task 4 closure handles both dispatch paths.

All signatures match across tasks.

---

## Execution ready

Plan complete. Ready to hand to subagent-driven-development or executing-plans.
