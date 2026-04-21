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


def test_scan_catches_telegram_bot_token_in_url_format(tmp_path):
    """Real-world leak: bot token embedded in api.telegram.org URL where the
    'bot' prefix is contiguous with the bot_id digits. The original \\b
    pattern failed here because the digit is preceded by 't' (both word
    characters), so \\b didn't fire. Using (?<!\\d) fixes this."""
    (tmp_path / "leaked.html").write_text(
        "https://api.telegram.org/bot1234567890:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/sendMessage"
    )
    findings = scan_for_secrets(tmp_path)
    assert any(f.pattern == "telegram_bot_token" for f in findings)


def test_scan_ignores_cloudflare_placeholder(tmp_path):
    """Documentation-style placeholders must not trip the scan."""
    (tmp_path / "docs.html").write_text(
        "CLOUDFLARE_API_TOKEN=your-token-here\n"
        "CLOUDFLARE_API_TOKEN=${CLOUDFLARE_API_TOKEN}\n"
        "CLOUDFLARE_API_TOKEN=<fill-me-in>\n"
    )
    findings = scan_for_secrets(tmp_path)
    cf_findings = [f for f in findings if f.pattern == "cloudflare_token"]
    assert cf_findings == [], f"Expected no cloudflare findings, got {cf_findings}"


def test_scan_catches_real_looking_cloudflare_token(tmp_path):
    """Real tokens (40+ random hex chars) must still be caught."""
    (tmp_path / "leaked.html").write_text(
        "CLOUDFLARE_API_TOKEN=abc123def456abc123def456abc123def456abc1"
    )
    findings = scan_for_secrets(tmp_path)
    assert any(f.pattern == "cloudflare_token" for f in findings)


def test_scan_ignores_non_utf8_html_file(tmp_path):
    """Legacy Latin-1 or other non-UTF-8 content in a .html file must not
    crash the scan — silently skip rather than false-trip on decode bytes."""
    (tmp_path / "latin1.html").write_bytes(b"\xff\xfe" + b"ghp_" + b"x" * 36)
    # Should not raise; the file gets skipped via UnicodeDecodeError path
    findings = scan_for_secrets(tmp_path)
    assert findings == []
