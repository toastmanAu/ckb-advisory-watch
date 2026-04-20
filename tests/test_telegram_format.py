"""format_message tests — HTML rendering, escaping, truncation, keyboard."""
from __future__ import annotations

from agent.output.telegram import format_message
from tests.telegram_fixtures import make_advisory, make_config, make_match


def test_format_includes_emoji_and_severity_label():
    adv = make_advisory(severity="critical", cvss=9.8)
    html, _ = format_message(adv, adv.matches, make_config())
    assert "🔴" in html
    assert "<b>CRITICAL</b>" in html


def test_format_omits_cvss_when_absent():
    adv = make_advisory(severity="high", cvss=None)
    html, _ = format_message(adv, adv.matches, make_config())
    assert "CVSS" not in html
    assert "<b>HIGH</b>" in html


def test_format_includes_source_id_and_summary():
    adv = make_advisory(source_id="GHSA-xyz-1234",
                        summary="Short summary here")
    html, _ = format_message(adv, adv.matches, make_config())
    assert "GHSA-xyz-1234" in html
    assert "Short summary here" in html


def test_format_lists_each_affected_project():
    m1 = make_match(match_id=1, project_slug="org/alpha", dep_name="pkg", dep_version="1.0")
    m2 = make_match(match_id=2, project_slug="org/beta", dep_name="pkg", dep_version="1.1")
    adv = make_advisory(matches=[m1, m2])
    html, _ = format_message(adv, adv.matches, make_config())
    assert "org/alpha" in html and "pkg@1.0" in html
    assert "org/beta" in html and "pkg@1.1" in html
    assert "Affects <b>2</b>" in html


def test_format_truncates_match_list_over_limit():
    matches = [make_match(match_id=i, project_slug=f"org/repo{i}") for i in range(12)]
    adv = make_advisory(matches=matches)
    html, _ = format_message(adv, matches, make_config())
    # Shows first 8 projects
    for i in range(8):
        assert f"org/repo{i}" in html
    # Ninth and beyond do NOT appear
    assert "org/repo8" not in html
    # Overflow indicator
    assert "… and <b>4 more</b>" in html


def test_format_includes_fix_line_when_fixed_in_present():
    adv = make_advisory(fixed_in="2.5.0")
    html, _ = format_message(adv, adv.matches, make_config())
    assert "upgrade to <code>2.5.0</code>" in html


def test_format_omits_fix_line_when_fixed_in_absent():
    adv = make_advisory(fixed_in=None)
    html, _ = format_message(adv, adv.matches, make_config())
    assert "upgrade to" not in html


def test_format_escapes_html_dangerous_chars_in_summary():
    adv = make_advisory(summary="<script>alert('x')</script> & friends")
    html, _ = format_message(adv, adv.matches, make_config())
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&amp; friends" in html


def test_format_escapes_html_in_package_names():
    m = make_match(dep_name="pkg<bad>", dep_version="1.0", project_slug="org/<repo>")
    adv = make_advisory(matches=[m])
    html, _ = format_message(adv, [m], make_config())
    assert "<bad>" not in html
    assert "&lt;bad&gt;" in html
    assert "org/&lt;repo&gt;" in html


def test_format_truncates_long_summary_to_500():
    long_summary = "x" * 900
    adv = make_advisory(summary=long_summary)
    html, _ = format_message(adv, adv.matches, make_config())
    # Truncated with an ellipsis indicator
    assert "x" * 500 in html
    assert "x" * 600 not in html


def test_format_unknown_severity_uses_white_circle():
    adv = make_advisory(severity=None, cvss=None)
    html, _ = format_message(adv, adv.matches, make_config())
    assert "⚪" in html
    assert "UNKNOWN" in html.upper()


def test_keyboard_has_dashboard_and_ghsa_buttons_when_both_available():
    adv = make_advisory(
        source_id="GHSA-abc",
        references=[
            {"type": "PACKAGE", "url": "https://example.com/pkg"},
            {"type": "ADVISORY", "url": "https://example.com/ghsa-page"},
        ],
    )
    config = make_config(base_url="http://dash.test:8080")
    _, kb = format_message(adv, adv.matches, config)
    urls = [b["url"] for row in kb["inline_keyboard"] for b in row]
    assert "http://dash.test:8080/a/GHSA-abc" in urls
    assert "https://example.com/ghsa-page" in urls


def test_keyboard_falls_back_to_first_reference_when_no_advisory_type():
    adv = make_advisory(
        references=[{"type": "WEB", "url": "https://example.com/web"}],
    )
    _, kb = format_message(adv, adv.matches, make_config())
    urls = [b["url"] for row in kb["inline_keyboard"] for b in row]
    assert "https://example.com/web" in urls


def test_keyboard_skips_reference_button_when_no_references():
    adv = make_advisory(references=[])
    _, kb = format_message(adv, adv.matches, make_config())
    # Only dashboard button remains
    urls = [b["url"] for row in kb["inline_keyboard"] for b in row]
    assert all("example.com" not in u for u in urls)
    assert len(urls) == 1


def test_keyboard_skips_dashboard_button_when_base_url_missing():
    adv = make_advisory()
    config = make_config(base_url="")
    _, kb = format_message(adv, adv.matches, config)
    urls = [b["url"] for row in kb["inline_keyboard"] for b in row]
    assert all("/a/" not in u for u in urls)


def test_total_message_stays_under_cap_for_pathological_input():
    # Pathological: 50 projects, very long summary
    matches = [make_match(match_id=i, project_slug=f"org/{'x'*80}{i}") for i in range(50)]
    adv = make_advisory(summary="x" * 10000, matches=matches)
    html, _ = format_message(adv, matches, make_config())
    assert len(html) <= 4000
