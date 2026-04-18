"""Seed the project table from projects.yaml.

This is the Phase 1 kickoff — later phases add a GitHub walker that fetches
each project's tip manifests and populates project_dep. For now this just
gets the list of projects into SQLite so the rest of the pipeline has
something to point at.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import yaml

from agent.db import open_db, upsert_project


def seed_projects_from_yaml(conn: sqlite3.Connection, yaml_path: Path) -> int:
    data = yaml.safe_load(yaml_path.read_text()) or {}
    projects = data.get("projects", [])
    for p in projects:
        slug = p["slug"]
        upsert_project(
            conn,
            slug=slug,
            display_name=p["display_name"],
            repo_url=p.get("repo_url", f"https://github.com/{slug}"),
            default_branch=p.get("branch", "main"),
        )
    return len(projects)


def main() -> int:
    parser = argparse.ArgumentParser(prog="ckb-advisory-watch crawl")
    parser.add_argument("--projects", type=Path, default=Path("projects.yaml"))
    parser.add_argument("--db", type=Path, default=Path("data/state.db"))
    parser.add_argument("--schema", type=Path, default=Path("db/schema.sql"))
    args = parser.parse_args()

    conn = open_db(args.db, args.schema)
    n = seed_projects_from_yaml(conn, args.projects)
    print(f"seeded {n} projects from {args.projects}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
