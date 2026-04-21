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
