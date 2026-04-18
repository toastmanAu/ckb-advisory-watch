from pathlib import Path

from agent.db import open_db, upsert_project

SCHEMA = Path(__file__).parent.parent / "db" / "schema.sql"


def test_upsert_project_creates_row(tmp_path):
    conn = open_db(tmp_path / "state.db", SCHEMA)

    project_id = upsert_project(
        conn,
        slug="nervosnetwork/ckb",
        display_name="CKB Node",
        repo_url="https://github.com/nervosnetwork/ckb",
    )

    row = conn.execute(
        "SELECT slug, display_name, repo_url FROM project WHERE id = ?",
        (project_id,),
    ).fetchone()
    assert row == (
        "nervosnetwork/ckb",
        "CKB Node",
        "https://github.com/nervosnetwork/ckb",
    )


def test_upsert_project_is_idempotent(tmp_path):
    conn = open_db(tmp_path / "state.db", SCHEMA)

    id1 = upsert_project(conn, slug="x/y", display_name="X Y", repo_url="u1")
    id2 = upsert_project(conn, slug="x/y", display_name="X Y v2", repo_url="u2")

    assert id1 == id2
    count = conn.execute(
        "SELECT COUNT(*) FROM project WHERE slug = ?", ("x/y",)
    ).fetchone()[0]
    assert count == 1


def test_upsert_project_updates_fields_without_clobbering_added_at(tmp_path):
    conn = open_db(tmp_path / "state.db", SCHEMA)

    upsert_project(conn, slug="x/y", display_name="Old Name", repo_url="old_url")
    original_added = conn.execute(
        "SELECT added_at FROM project WHERE slug = ?", ("x/y",)
    ).fetchone()[0]

    upsert_project(conn, slug="x/y", display_name="New Name", repo_url="new_url")
    row = conn.execute(
        "SELECT display_name, repo_url, added_at FROM project WHERE slug = ?",
        ("x/y",),
    ).fetchone()

    assert row[0] == "New Name"
    assert row[1] == "new_url"
    assert row[2] == original_added
