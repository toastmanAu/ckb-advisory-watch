"""Parse Cargo.lock into (name, version) tuples for advisory matching.

Workspace members (no `source` field) are skipped — they're the project itself,
not upstream deps. Both registry-sourced and git-sourced crates are included;
both can be subjects of security advisories.
"""
from __future__ import annotations

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]


def parse_cargo_lock(text: str) -> list[tuple[str, str]]:
    data = tomllib.loads(text)
    return [
        (pkg["name"], pkg["version"])
        for pkg in data.get("package", [])
        if pkg.get("source")
    ]
