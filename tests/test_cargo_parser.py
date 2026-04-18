from pathlib import Path

from agent.parsers.cargo import parse_cargo_lock

FIXTURE = Path(__file__).parent / "fixtures" / "sample.Cargo.lock"


def test_extracts_registry_sourced_packages():
    deps = parse_cargo_lock(FIXTURE.read_text())

    assert ("serde", "1.0.217") in deps
    assert ("tokio", "1.41.1") in deps


def test_includes_git_sourced_packages():
    """Git-sourced crates are real upstream deps — advisories apply to them."""
    deps = parse_cargo_lock(FIXTURE.read_text())

    assert ("my-fork", "0.2.0") in deps


def test_skips_workspace_members_without_source():
    """Packages with no `source` are local — not upstream, not advisory targets."""
    deps = parse_cargo_lock(FIXTURE.read_text())

    names = [name for name, _ in deps]
    assert "my-project" not in names
    assert "internal-utils" not in names
