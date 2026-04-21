"""Parse pnpm-lock.yaml (lockfileVersion 9.x) into (name, version) pairs.

pnpm's lockfile is pure YAML. The authoritative list of external deps
lives under the top-level `packages:` dict, keyed by `<name>@<version>`
(with optional `(...)` peer-dep annotation suffix).

We include:
  * every entry in `packages:` resolved to (name, version)
  * both scoped (@scope/name) and unscoped names

We skip:
  * `importers:` (workspace members and their specifiers — pnpm's dep
    graph is unified under packages: anyway)
  * `patchedDependencies:` (references to packages:, not separate deps)
  * keys whose version looks like file: / git+ / workspace: (defensive;
    such keys shouldn't appear in packages: but lockfiles in the wild lie)

The peer-dep resolution suffix (e.g. `react@18.2.0(@types/react@18.0.0)`)
is stripped — for advisory matching the (name, version) identity is what
matters, not the peer context. Two entries that differ only in peer
resolutions dedupe to one tuple.

Output is deduplicated and sorted for stable diffs in project_dep.
"""
from __future__ import annotations

import yaml


def parse_pnpm_lock(text: str) -> list[tuple[str, str]]:
    data = yaml.safe_load(text) or {}
    packages = data.get("packages") or {}
    if not isinstance(packages, dict):
        return []

    out: set[tuple[str, str]] = set()
    for key in packages:
        if not isinstance(key, str):
            continue
        nv = _split_name_version(key)
        if nv:
            out.add(nv)
    return sorted(out)


def _split_name_version(key: str) -> tuple[str, str] | None:
    """Split a pnpm packages:-dict key into (name, version).

    Recognised key shapes:
        lodash@4.17.21
        @babel/core@7.24.0
        react@18.2.0(@types/react@18.0.0)
        @testing-library/react@14.0.0(react@18.2.0)

    Returns None for keys that don't match the expected shape or whose
    version is a workspace/file reference."""
    # Strip the peer-dep resolution suffix if present.
    paren = key.find("(")
    if paren >= 0:
        key = key[:paren]

    # Locate the name/version separator. For scoped names (@scope/name)
    # the leading @ is the scope marker, not the separator — so we search
    # for the SECOND @. For unscoped names, the first @ is the separator.
    if key.startswith("@"):
        sep = key.find("@", 1)
    else:
        sep = key.find("@")
    if sep <= 0:
        return None

    name = key[:sep]
    version = key[sep + 1:]
    if not name or not version:
        return None
    # Defensive: never treat a workspace/file/git reference as a real dep.
    if version.startswith(("link:", "file:", "workspace:", "git+", "http:", "https:")):
        return None
    return name, version
