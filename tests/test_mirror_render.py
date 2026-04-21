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
    """Return the number of .html files written under *root* (recursive)."""
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
    # Return value must match on-disk truth — catches bugs where render_all
    # silently drops files or double-counts.
    assert n_pages == _count_html_files(out_dir)


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


def test_render_all_kpi_tiles_are_not_clickable_on_mirror(tmp_path):
    """Mirror has no server to handle /?severity=X — KPI filter links
    would 404. Tiles render as plain divs, not anchor-wrapped."""
    conn = fresh_db(tmp_path)
    seed_match(conn, project_slug="o/r", source_id="GHSA-crit",
               severity="critical", cvss=9.8)
    out_dir = tmp_path / "out"

    render_all(conn, out_dir, severity_floor=("critical", "high", "medium"),
               base_url="")

    index_html = (out_dir / "index.html").read_text()
    # No KPI anchor links (class kpi-link) should appear in mirror output
    assert 'class="kpi-link"' not in index_html
    # No query-string severity filter links either
    assert "?severity=" not in index_html
    # But the count content must still be present
    assert "kpi critical" in index_html


def test_render_all_shows_snapshot_not_live_in_topstrip(tmp_path):
    """Mirror is a snapshot, not live. Topstrip indicator must say so."""
    conn = fresh_db(tmp_path)
    seed_match(conn, project_slug="o/r", source_id="GHSA-crit",
               severity="critical", cvss=9.8)
    out_dir = tmp_path / "out"

    render_all(conn, out_dir, severity_floor=("critical", "high", "medium"),
               base_url="")

    index_html = (out_dir / "index.html").read_text()
    assert "snapshot" in index_html
    # The private dashboard's "● live" must NOT appear on mirror pages
    assert "● live" not in index_html


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
