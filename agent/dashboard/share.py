"""Email composition + SMTP dispatch for the share action.

Two builders — per-advisory (lists every affected project) and per-match
(scopes to one row). Both return an EmailPayload the caller hands to
send_email. send_email uses stdlib smtplib.SMTP_SSL for Gmail.

The HTML and text bodies are Jinja2 templates in templates/; they share a
single context dict so they can't drift."""
from __future__ import annotations

import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from agent.dashboard.queries import AdvisoryContext, MatchRow

TEMPLATES_DIR = Path(__file__).parent / "templates"
_env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


@dataclass(frozen=True)
class ShareConfig:
    recipient: str
    sender: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    dashboard_base_url: str


@dataclass(frozen=True)
class EmailPayload:
    subject: str
    sender: str
    to: str
    text_body: str
    html_body: str


def _render(template_name: str, **ctx: Any) -> str:
    return _env.get_template(template_name).render(**ctx)


def build_advisory_email(
    advisory: AdvisoryContext,
    config: ShareConfig,
) -> EmailPayload:
    # Identify primary affected package for the subject line — use the most
    # common dep name across matches.
    if advisory.matches:
        names = [m.dep_name for m in advisory.matches]
        primary = max(set(names), key=names.count)
    else:
        primary = "unknown"
    fix_part = f" < {advisory.fixed_in}" if advisory.fixed_in else ""
    subject = (
        f"[CKB advisory] {advisory.source_id} — {primary}{fix_part} "
        f"({len(advisory.matches)} matches)"
    )

    ctx = {
        "kind": "advisory",
        "advisory": advisory,
        "config": config,
        "dashboard_url": f"{config.dashboard_base_url}/a/{advisory.source_id}",
    }
    return EmailPayload(
        subject=subject,
        sender=config.sender,
        to=config.recipient,
        text_body=_render("email.txt", **ctx),
        html_body=_render("email.html", **ctx),
    )


def build_match_email(
    match: MatchRow,
    advisory: AdvisoryContext,
    config: ShareConfig,
) -> EmailPayload:
    subject = (
        f"[CKB advisory] {match.source_id} — {match.dep_name}@{match.dep_version} "
        f"in {match.project_slug}"
    )
    ctx = {
        "kind": "match",
        "match": match,
        "advisory": advisory,
        "config": config,
        "dashboard_url": f"{config.dashboard_base_url}/a/{match.source_id}",
    }
    return EmailPayload(
        subject=subject,
        sender=config.sender,
        to=config.recipient,
        text_body=_render("email.txt", **ctx),
        html_body=_render("email.html", **ctx),
    )


def send_email(payload: EmailPayload, config: ShareConfig) -> None:
    msg = EmailMessage()
    msg["Subject"] = payload.subject
    msg["From"] = payload.sender
    msg["To"] = payload.to
    msg.set_content(payload.text_body)
    msg.add_alternative(payload.html_body, subtype="html")

    with smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, timeout=30) as s:
        s.login(config.smtp_user, config.smtp_password)
        s.send_message(msg)
