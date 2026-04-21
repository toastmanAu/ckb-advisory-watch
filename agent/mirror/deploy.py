"""Deploy-side helpers — secret-leak scan + wrangler subprocess wrapper.

The scan runs on the rendered out_dir before we hand bytes to Wrangler.
Patterns are deliberately conservative: if any trip, deploy aborts with
a non-zero exit so the systemd timer's journalctl tail shows the hit."""
from __future__ import annotations

import logging
import os
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
    # Use (?<!\d) instead of leading \b because common real-world leak format
    # is a Telegram API URL (bot<id>:<token>), where the digit is preceded by
    # 't' (a word char), so \b would not fire. (?<!\d) allows 'bot1234567890:...'
    # (digit preceded by letter, not another digit) while still rejecting
    # id-embedded false positives like 'userid12345678:...'.
    ("telegram_bot_token", re.compile(r"(?<!\d)\d{8,10}:[A-Za-z0-9_-]{35}\b")),
    # Literal Telegram chat_id from config.example.toml — an exact-match
    # belt-and-suspenders: if someone edits a template to expose a chat_id,
    # the number itself trips the scan regardless of context.
    ("telegram_chat_id", re.compile(r"\b1790655432\b")),
    # Literal secret key names — catches a template that accidentally
    # interpolated the whole config section.
    ("secret_key_name", re.compile(r"\b(api_token|smtp_password|bot_token)\s*[:=]")),
    # Cloudflare API tokens: 40 hex/alnum chars after a recognizable prefix
    # Exclude common documentation placeholders: 'your-token-here',
    # '${CLOUDFLARE_API_TOKEN}', '<fill-me-in>', 'example', 'xxx', or
    # empty-quoted pairs. Real tokens (40+ random hex chars) still match.
    ("cloudflare_token", re.compile(
        r"CLOUDFLARE_API_TOKEN\s*[:=]\s*"
        r"(?!(?:your-|placeholder|xxx|<|\$\{|example)|[\"']\s*[\"'])"
        r"\S+"
    )),
]

# Only scan these extensions. PNG/favicon bytes would false-positive.
_SCAN_EXTENSIONS = {".html", ".htm", ".txt", ".xml", ".json", ".js", ".css"}


class SecretScanFailed(Exception):
    """Reserved for callers that want to convert scan_for_secrets' returned
    findings list into an exception. `scan_for_secrets` itself returns the
    list and never raises — the CLI (Task 7) is responsible for deciding
    whether to raise, log+abort, or otherwise act on findings."""


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
