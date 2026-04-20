from pathlib import Path

from agent.parsers.npm import parse_package_lock

FIXTURE = Path(__file__).parent / "fixtures" / "sample.package-lock.json"


def test_extracts_top_level_deps():
    deps = parse_package_lock(FIXTURE.read_text())
    assert ("lodash", "4.17.21") in deps
    assert ("@babel/core", "7.24.0") in deps


def test_extracts_dev_deps():
    """Dev deps land in production via transitive closures and build-time
    vuln surfaces (e.g. test runners loaded into CI runners). Include them."""
    deps = parse_package_lock(FIXTURE.read_text())
    assert ("jest", "29.7.0") in deps


def test_extracts_nested_deps_distinctly():
    """Nested installs (a package's own copy of lodash at a different version)
    are real upstream deps with real advisory exposure. Include them as
    separate (name, version) tuples."""
    deps = parse_package_lock(FIXTURE.read_text())
    assert ("lodash", "3.10.1") in deps  # nested under some-pkg
    assert ("@types/node", "20.11.0") in deps  # nested scoped


def test_skips_workspace_members():
    """Workspace packages are the project itself — not upstream deps, same
    reasoning as Cargo workspace skip."""
    deps = parse_package_lock(FIXTURE.read_text())
    names = [n for n, _ in deps]
    assert "@sample/workspace-pkg" not in names
    assert "sample-project" not in names


def test_handles_scoped_package_names():
    deps = parse_package_lock(FIXTURE.read_text())
    # Extracted name includes the @scope prefix exactly as published on npm.
    assert ("@babel/core", "7.24.0") in deps


def test_deduplicates_identical_name_version_pairs():
    """Same (name, version) can appear in multiple packages entries when a
    tree has the same package hoisted + nested-at-same-version. We dedup."""
    synthetic = """
    {
      "lockfileVersion": 3,
      "packages": {
        "": {"name": "p", "version": "1.0.0"},
        "node_modules/lodash": {"version": "4.17.21"},
        "node_modules/a/node_modules/lodash": {"version": "4.17.21"}
      }
    }
    """
    deps = parse_package_lock(synthetic)
    assert deps.count(("lodash", "4.17.21")) == 1


def test_returns_sorted_for_stable_diffs():
    deps = parse_package_lock(FIXTURE.read_text())
    assert deps == sorted(deps)
