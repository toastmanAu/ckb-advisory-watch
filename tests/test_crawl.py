from pathlib import Path

from agent.crawl import seed_projects_from_yaml
from agent.db import open_db

SCHEMA = Path(__file__).parent.parent / "db" / "schema.sql"


def test_seed_from_yaml_populates_project_table(tmp_path):
    yaml_path = tmp_path / "projects.yaml"
    yaml_path.write_text(
        """
projects:
  - slug: foo/bar
    display_name: Foo Bar
  - slug: a/b
    display_name: A B
"""
    )
    conn = open_db(tmp_path / "state.db", SCHEMA)

    count = seed_projects_from_yaml(conn, yaml_path)

    assert count == 2
    rows = conn.execute(
        "SELECT slug, display_name, repo_url FROM project ORDER BY slug"
    ).fetchall()
    assert rows == [
        ("a/b", "A B", "https://github.com/a/b"),
        ("foo/bar", "Foo Bar", "https://github.com/foo/bar"),
    ]


def test_seed_from_yaml_is_rerunnable(tmp_path):
    yaml_path = tmp_path / "projects.yaml"
    yaml_path.write_text(
        "projects:\n  - slug: foo/bar\n    display_name: Foo Bar\n"
    )
    conn = open_db(tmp_path / "state.db", SCHEMA)

    seed_projects_from_yaml(conn, yaml_path)
    seed_projects_from_yaml(conn, yaml_path)

    count = conn.execute("SELECT COUNT(*) FROM project").fetchone()[0]
    assert count == 1
