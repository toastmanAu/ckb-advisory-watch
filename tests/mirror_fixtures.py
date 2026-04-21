"""Shared builder helpers for mirror tests.

Used by test_mirror_mailto.py, test_mirror_render.py, and (future) test_mirror_cli.py.
Module-level functions (not pytest fixtures) so they can be called with arbitrary
keyword overrides without fixture parameterisation overhead.
"""
from __future__ import annotations

from agent.dashboard.queries import AdvisoryContext, MatchRow


def make_advisory_ctx(**overrides) -> AdvisoryContext:
    base = dict(
        advisory_id=1, source_id="GHSA-x1y2", severity="critical", cvss=9.8,
        summary="Remote code execution in example-pkg",
        details="", modified=1700000000,
        cve_ids=["CVE-2026-1001"],
        references=[{"type": "ADVISORY", "url": "https://example.com/x1y2"}],
        fixed_in="1.2.4", matches=[],
    )
    base.update(overrides)
    return AdvisoryContext(**base)


def make_match_row(**overrides) -> MatchRow:
    base = dict(
        match_id=42, advisory_id=1, source_id="GHSA-x1y2",
        severity="critical", cvss=9.8, summary="RCE in example-pkg",
        project_slug="o/r", project_display_name="o/r",
        ecosystem="npm", dep_name="example-pkg", dep_version="1.2.3",
        fixed_in="1.2.4", first_matched=1700000000,
    )
    base.update(overrides)
    return MatchRow(**base)
