"""Parse go.sum into (module, version) pairs.

Each module appears on two lines:
  <module> <version> h1:<hash>=
  <module> <version>/go.mod h1:<hash>=

We take the first two whitespace-separated fields per line, strip a trailing
`/go.mod` from the version column, and dedup. Pseudo-versions
(`v0.0.0-YYYYMMDDHHMMSS-abcdef`) and major-version-suffixed modules
(`example.com/foo/v2`) are preserved verbatim — both are the real identity
OSV advisories reference.
"""
from __future__ import annotations


def parse_go_sum(text: str) -> list[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        module = parts[0]
        version = parts[1]
        version = version.removesuffix("/go.mod")
        if not module or not version:
            continue
        out.add((module, version))
    return sorted(out)
