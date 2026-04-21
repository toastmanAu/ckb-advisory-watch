"""Unit tests for scan_for_secrets and deploy_via_wrangler."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent.mirror.deploy import (
    DeployError, SecretFound, deploy_via_wrangler, scan_for_secrets,
)


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


def test_deploy_via_wrangler_timeout_raises(tmp_path):
    """Hung deploy on Pi Zero 3 residential network must surface as a
    DeployError with 'timed out' in the message — not a raw TimeoutExpired
    that confuses the CLI's exit-code mapping."""
    import subprocess
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    sensitive_token = "SENSITIVE_TOKEN_xyz123"
    with patch("agent.mirror.deploy.subprocess.run") as run:
        run.side_effect = subprocess.TimeoutExpired(cmd=["wrangler"], timeout=240)
        with pytest.raises(DeployError) as excinfo:
            deploy_via_wrangler(
                out_dir=out_dir,
                project_name="ckb-advisories",
                api_token=sensitive_token,
                account_id="a",
            )
        msg = str(excinfo.value)
        assert "timed out" in msg
        assert "240" in msg
        # Token must not leak even via repr(exc) — hardened by using
        # project_name!r in the message instead of exc!r.
        assert sensitive_token not in msg
