"""Share email composition + SMTP tests (SMTP mocked)."""
from __future__ import annotations

import smtplib
from unittest.mock import patch, MagicMock

import pytest

from agent.dashboard.queries import AdvisoryContext, MatchRow
from agent.dashboard.share import (
    EmailPayload, ShareConfig, build_advisory_email, build_match_email, send_email,
)


def _advisory_ctx():
    return AdvisoryContext(
        advisory_id=1,
        source_id="GHSA-6p3c-v8vc-c244",
        severity="critical",
        cvss=9.8,
        summary="Summary of the advisory",
        details="Full details here",
        modified=1712000000,
        cve_ids=["CVE-2022-39303"],
        references=[{"type": "ADVISORY", "url": "https://example.com/ghsa"}],
        fixed_in="0.7.3",
        matches=[
            MatchRow(1, 1, "GHSA-6p3c-v8vc-c244", "critical", 9.8, "Summary",
                     "Magickbase/force-bridge", "force-bridge",
                     "crates.io", "molecule", "0.6.0", "0.7.3", 1712099999),
            MatchRow(2, 1, "GHSA-6p3c-v8vc-c244", "critical", 9.8, "Summary",
                     "Magickbase/force-bridge", "force-bridge",
                     "crates.io", "molecule", "0.6.1", "0.7.3", 1712099999),
        ],
    )


def _config():
    return ShareConfig(
        recipient="phill@example.com",
        sender="phill@example.com",
        smtp_host="smtp.gmail.com",
        smtp_port=465,
        smtp_user="phill@example.com",
        smtp_password="app-password",
        dashboard_base_url="http://pi.local:8080",
    )


def test_advisory_email_subject_format():
    payload = build_advisory_email(_advisory_ctx(), _config())
    assert payload.subject == "[CKB advisory] GHSA-6p3c-v8vc-c244 — molecule < 0.7.3 (2 matches)"


def test_advisory_email_recipients():
    payload = build_advisory_email(_advisory_ctx(), _config())
    assert payload.to == "phill@example.com"
    assert payload.sender == "phill@example.com"


def test_advisory_email_html_includes_all_projects():
    payload = build_advisory_email(_advisory_ctx(), _config())
    for version in ("0.6.0", "0.6.1"):
        assert f"molecule@{version}" in payload.html_body
    assert "Magickbase/force-bridge" in payload.html_body


def test_advisory_email_html_includes_dashboard_link():
    payload = build_advisory_email(_advisory_ctx(), _config())
    assert "http://pi.local:8080/a/GHSA-6p3c-v8vc-c244" in payload.html_body


def test_advisory_email_text_body_parallels_html():
    payload = build_advisory_email(_advisory_ctx(), _config())
    assert "GHSA-6p3c-v8vc-c244" in payload.text_body
    assert "molecule@0.6.0" in payload.text_body
    assert "CRITICAL" in payload.text_body.upper()


def test_match_email_subject_format():
    ctx = _advisory_ctx()
    payload = build_match_email(ctx.matches[0], ctx, _config())
    assert payload.subject == "[CKB advisory] GHSA-6p3c-v8vc-c244 — molecule@0.6.0 in Magickbase/force-bridge"


def test_match_email_scopes_to_single_row():
    ctx = _advisory_ctx()
    payload = build_match_email(ctx.matches[0], ctx, _config())
    # only the specific affected version appears
    assert "molecule@0.6.0" in payload.html_body
    assert "molecule@0.6.1" not in payload.html_body


def test_send_email_happy_path():
    payload = EmailPayload(
        subject="subj", sender="a@x", to="b@y",
        text_body="t", html_body="<p>h</p>",
    )
    mock_smtp = MagicMock()
    with patch("smtplib.SMTP_SSL", return_value=mock_smtp.__enter__.return_value) as smtp_ctor:
        send_email(payload, _config())
        smtp_ctor.assert_called_once_with("smtp.gmail.com", 465, timeout=30)


def test_send_email_login_failure_raises():
    payload = EmailPayload(subject="s", sender="a", to="b", text_body="t", html_body="<p>h</p>")
    mock_smtp_instance = MagicMock()
    mock_smtp_instance.__enter__.return_value = mock_smtp_instance
    mock_smtp_instance.login.side_effect = smtplib.SMTPAuthenticationError(535, b"auth")
    with patch("smtplib.SMTP_SSL", return_value=mock_smtp_instance):
        with pytest.raises(smtplib.SMTPAuthenticationError):
            send_email(payload, _config())
