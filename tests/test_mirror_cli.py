"""Integration tests for agent.mirror __main__ CLI."""
from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.mirror.__main__ import main as mirror_main
from tests.dashboard_fixtures import fresh_db, seed_match


def _write_config(path: Path, enabled: bool = True, **overrides) -> Path:
    cfg = f"""
[agent]
data_dir = "{path}"

[outputs.public_mirror]
enabled = {str(enabled).lower()}
project_name = "ckb-advisories-test"
api_token = "{overrides.get('api_token', 'test-token')}"
account_id = "{overrides.get('account_id', 'test-acct')}"
min_severity = "medium"
out_dir = "{overrides.get('out_dir', path.parent / 'out')}"
base_url = "https://advisories.example.com"
"""
    cfg_path = path.parent / "mirror.toml"
    cfg_path.write_text(textwrap.dedent(cfg))
    return cfg_path


def test_cli_exits_0_when_disabled(tmp_path, caplog):
    import logging
    conn = fresh_db(tmp_path)
    conn.close()
    cfg_path = _write_config(tmp_path, enabled=False)

    with caplog.at_level(logging.INFO):
        rc = mirror_main(["--config", str(cfg_path)])
    assert rc == 0
    assert "disabled" in caplog.text.lower()


def test_cli_exits_2_when_api_token_empty(tmp_path):
    conn = fresh_db(tmp_path)
    conn.close()
    cfg_path = _write_config(tmp_path, enabled=True, api_token="")

    rc = mirror_main(["--config", str(cfg_path)])
    assert rc == 2


def test_cli_happy_path_renders_and_deploys(tmp_path):
    conn = fresh_db(tmp_path)
    seed_match(conn, project_slug="o/r", source_id="GHSA-crit",
               severity="critical", cvss=9.8)
    conn.close()

    out_dir = tmp_path / "out"
    cfg_path = _write_config(tmp_path, enabled=True, out_dir=out_dir)

    with patch("agent.mirror.__main__.deploy_via_wrangler") as deploy:
        rc = mirror_main(["--config", str(cfg_path)])

    assert rc == 0
    assert deploy.called
    kwargs = deploy.call_args.kwargs
    assert kwargs["project_name"] == "ckb-advisories-test"
    assert kwargs["api_token"] == "test-token"
    assert kwargs["account_id"] == "test-acct"
    # Files rendered before deploy
    assert (out_dir / "index.html").exists()
    assert (out_dir / "a" / "GHSA-crit" / "index.html").exists()


def test_cli_exits_1_if_secrets_leak_into_output(tmp_path):
    """Inject a secret-shaped string into the advisory summary via seed,
    watch the scan abort deploy."""
    conn = fresh_db(tmp_path)
    # Seed an advisory whose summary contains a faux ghp_ token. The
    # render writes it into <td>... making the scan trip.
    seed_match(
        conn, project_slug="o/r", source_id="GHSA-crit",
        severity="critical", cvss=9.8,
        summary="DO NOT SHIP: ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    )
    conn.close()

    cfg_path = _write_config(tmp_path, enabled=True)

    with patch("agent.mirror.__main__.deploy_via_wrangler") as deploy:
        rc = mirror_main(["--config", str(cfg_path)])

    assert rc == 1
    assert not deploy.called  # deploy must not run when secrets detected


def test_cli_exits_2_when_account_id_empty(tmp_path):
    conn = fresh_db(tmp_path)
    conn.close()
    cfg_path = _write_config(tmp_path, enabled=True, account_id="")
    rc = mirror_main(["--config", str(cfg_path)])
    assert rc == 2


def test_cli_exits_2_when_project_name_empty(tmp_path):
    """project_name is required to target a Cloudflare Pages project."""
    conn = fresh_db(tmp_path)
    conn.close()
    # _write_config hardcodes project_name; need a variant config.
    cfg_path = tmp_path / "mirror.toml"
    cfg_path.write_text(f'''
[agent]
data_dir = "{tmp_path}"

[outputs.public_mirror]
enabled = true
project_name = ""
api_token = "t"
account_id = "a"
min_severity = "medium"
out_dir = "{tmp_path / 'out'}"
base_url = "https://advisories.example.com"
''')
    rc = mirror_main(["--config", str(cfg_path)])
    assert rc == 2


def test_cli_exits_2_when_min_severity_invalid(tmp_path):
    conn = fresh_db(tmp_path)
    conn.close()
    cfg_path = tmp_path / "mirror.toml"
    cfg_path.write_text(f'''
[agent]
data_dir = "{tmp_path}"

[outputs.public_mirror]
enabled = true
project_name = "ckb-advisories-test"
api_token = "t"
account_id = "a"
min_severity = "banana"
out_dir = "{tmp_path / 'out'}"
base_url = "https://advisories.example.com"
''')
    rc = mirror_main(["--config", str(cfg_path)])
    assert rc == 2


def test_cli_exits_1_when_state_db_missing(tmp_path):
    """state.db must exist before mirror runs — agent is responsible for it.
    A missing DB is a runtime error (exit 1), not a config error (exit 2)."""
    # Deliberately do NOT call fresh_db — no state.db on disk.
    cfg_path = _write_config(tmp_path, enabled=True)
    rc = mirror_main(["--config", str(cfg_path)])
    assert rc == 1


def test_cli_exits_2_when_config_file_missing(tmp_path):
    missing = tmp_path / "nonexistent.toml"
    rc = mirror_main(["--config", str(missing)])
    assert rc == 2
