from pathlib import Path

from agent.parsers.go_sum import parse_go_sum

FIXTURE = Path(__file__).parent / "fixtures" / "sample.go.sum"


def test_extracts_module_version_pairs():
    deps = parse_go_sum(FIXTURE.read_text())
    assert ("github.com/mattn/go-isatty", "v0.0.14") in deps
    assert ("github.com/mattn/go-runewidth", "v0.0.13") in deps


def test_handles_pseudo_versions():
    """Go pseudo-versions (v0.0.0-<timestamp>-<commit>) are legit version
    strings. Preserve verbatim."""
    deps = parse_go_sum(FIXTURE.read_text())
    assert ("golang.org/x/sys", "v0.0.0-20220811171246-fbc7d0a398ab") in deps


def test_handles_major_version_suffix():
    """Modules with a major version path suffix (/v2, /v3) keep the suffix
    as part of the module name — that's the real import path."""
    deps = parse_go_sum(FIXTURE.read_text())
    assert ("github.com/nervosnetwork/ckb-sdk-go/v2", "v2.3.0") in deps


def test_dedupes_module_version():
    """Each module has two lines in go.sum (code + go.mod). Dedup so the
    matcher doesn't double-count."""
    deps = parse_go_sum(FIXTURE.read_text())
    # 4 modules in fixture, 8 lines — should return 4 tuples.
    assert len(deps) == 4


def test_returns_sorted_for_stable_diffs():
    deps = parse_go_sum(FIXTURE.read_text())
    assert deps == sorted(deps)


def test_ignores_blank_and_malformed_lines():
    """Don't explode on trailing newlines, blank lines, or short lines.
    go.sum format is rigid but defensive parsing matters when repos have
    uncommitted edits or CRLF line endings."""
    text = """
github.com/foo/bar v1.0.0 h1:x=

github.com/foo/bar v1.0.0/go.mod h1:y=
    \t
broken-line-without-version
"""
    deps = parse_go_sum(text)
    assert deps == [("github.com/foo/bar", "v1.0.0")]
