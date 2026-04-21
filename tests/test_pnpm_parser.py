from pathlib import Path

from agent.parsers.pnpm import parse_pnpm_lock

FIXTURE = Path(__file__).parent / "fixtures" / "sample.pnpm-lock.yaml"


def test_extracts_simple_unscoped_package():
    deps = parse_pnpm_lock(FIXTURE.read_text())
    assert ("lodash", "4.17.21") in deps
    assert ("some-pkg", "2.1.0") in deps


def test_extracts_scoped_package_with_scope_intact():
    """The leading @ in scoped names is NOT the name-version separator."""
    deps = parse_pnpm_lock(FIXTURE.read_text())
    assert ("@babel/core", "7.24.0") in deps


def test_strips_peer_dep_suffix_from_version():
    """pnpm annotates packages with peer-dep resolutions like
    `react@18.2.0(@types/react@18.0.0)`. For advisory matching we only
    care about (name, version) — the peer context doesn't change identity."""
    deps = parse_pnpm_lock(FIXTURE.read_text())
    assert ("react", "18.2.0") in deps
    # No variant with the peer-dep tail leaked through
    versions = [v for n, v in deps if n == "react"]
    assert all("(" not in v for v in versions)


def test_strips_peer_dep_suffix_on_scoped_package():
    deps = parse_pnpm_lock(FIXTURE.read_text())
    assert ("@testing-library/react", "14.0.0") in deps


def test_deduplicates_same_package_with_different_peer_resolutions():
    """Two entries for react@18.2.0 with different peer contexts -> one tuple."""
    deps = parse_pnpm_lock(FIXTURE.read_text())
    assert deps.count(("react", "18.2.0")) == 1


def test_skips_workspace_link_entries():
    """`importers:` may reference `link:../shared` — that's a workspace
    member, not an upstream dep. It should NOT appear in the output."""
    deps = parse_pnpm_lock(FIXTURE.read_text())
    names = [n for n, _ in deps]
    assert "@local/shared" not in names


def test_returns_sorted_for_stable_diffs():
    deps = parse_pnpm_lock(FIXTURE.read_text())
    assert deps == sorted(deps)


def test_handles_empty_packages_section():
    """No `packages:` at all (rare but possible for lockfiles with only
    workspace members and no external deps). Don't crash."""
    text = """
lockfileVersion: '9.0'
importers:
  .: {}
"""
    deps = parse_pnpm_lock(text)
    assert deps == []


def test_handles_missing_packages_keyed_as_null():
    """Defensive: `packages:` present but explicitly null."""
    text = """
lockfileVersion: '9.0'
packages:
"""
    deps = parse_pnpm_lock(text)
    assert deps == []


def test_ignores_non_packages_file_references():
    """Defensive: if a version looks like file:/git+/workspace:, skip it.
    These normally only appear in importers, not in packages, but be safe."""
    text = """
lockfileVersion: '9.0'
packages:
  'broken@file:./local.tgz':
    resolution: {integrity: sha512-x}
  'good@1.2.3':
    resolution: {integrity: sha512-y}
"""
    deps = parse_pnpm_lock(text)
    assert ("good", "1.2.3") in deps
    assert all(not v.startswith("file:") for _, v in deps)
