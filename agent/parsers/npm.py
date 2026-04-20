"""Parse package-lock.json (lockfileVersion 2/3) into (name, version) pairs.

v2 and v3 both use the `packages` dict format keyed by installation path.
We include:
  * registry-resolved deps (top-level under node_modules/)
  * dev deps (they ship through CI and install-time vuln surfaces)
  * nested installs at different versions (duplicate trees are real)

We skip:
  * the "" root entry (the project itself)
  * workspace members (paths not starting with node_modules/)
  * link entries (`link: true` — alias to a local workspace)
  * entries without a concrete version

Output is deduplicated and sorted for stable diffs in project_dep.
"""
from __future__ import annotations

import json


def parse_package_lock(text: str) -> list[tuple[str, str]]:
    data = json.loads(text)
    packages = data.get("packages") or {}

    out: set[tuple[str, str]] = set()
    for path, entry in packages.items():
        if path == "":
            continue  # root project
        if not isinstance(entry, dict):
            continue
        if entry.get("link") is True:
            continue  # symlinked workspace member
        version = entry.get("version")
        if not isinstance(version, str) or not version:
            continue
        if "node_modules/" not in path:
            continue  # workspace path like "packages/foo"

        name = _extract_name_from_path(path)
        if name:
            out.add((name, version))

    return sorted(out)


def _extract_name_from_path(path: str) -> str | None:
    """Return the package name from a node_modules path.

    Handles nested installs (node_modules/foo/node_modules/bar -> bar),
    scoped packages (node_modules/@scope/pkg -> @scope/pkg), and the
    combination (node_modules/foo/node_modules/@scope/pkg -> @scope/pkg)."""
    # Take everything after the LAST "node_modules/".
    idx = path.rfind("node_modules/")
    if idx < 0:
        return None
    tail = path[idx + len("node_modules/"):]
    if not tail:
        return None
    # Scoped package spans two path segments (@scope/name).
    if tail.startswith("@"):
        parts = tail.split("/", 2)
        if len(parts) >= 2:
            return f"{parts[0]}/{parts[1]}"
        return None
    # Unscoped: first segment is the name.
    return tail.split("/", 1)[0]
