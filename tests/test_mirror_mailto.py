"""Unit tests for mailto_href — RFC 6068 URL encoding + length cap."""
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from agent.mirror.render import mailto_href
from tests.mirror_fixtures import make_advisory_ctx as _adv, make_match_row as _match


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
