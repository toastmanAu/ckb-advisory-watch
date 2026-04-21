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
