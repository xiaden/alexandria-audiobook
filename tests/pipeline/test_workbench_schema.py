"""Schema tests for the combined 2b/2c/2d workbench tables.

Covers creation of every registered workbench table/constraint/index,
idempotency of ``create_schema``, preservation of existing human data, the
``walk_review_item.kind`` migration, and the per-book ``workbench_generation``
revision backfill.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.pipeline.schema import create_schema

WORKBENCH_TABLES = {
    "workbench_generation",
    "workbench_decision",
    "workbench_provenance",
    "character_scene_absence",
    "character_alias_merge",
    "boundary_override",
    "character_scene_generated",
    "character_scene_manual",
}

WORKBENCH_INDEXES = {
    "idx_workbench_decision_book_status",
    "idx_character_scene_generated_book",
    "idx_character_scene_manual_book",
    "ux_alias_active_member",
}


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.execute("PRAGMA foreign_keys = ON")
    create_schema(c)
    return c


def _table_names(c: sqlite3.Connection) -> set[str]:
    return {
        r[0]
        for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }


def _index_names(c: sqlite3.Connection) -> set[str]:
    return {
        r[0]
        for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }


def _columns(c: sqlite3.Connection, table: str) -> dict[str, tuple]:
    return {
        r[1]: (r[2], r[3], r[4])  # name -> (type, notnull, default)
        for r in c.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _seed_spine(c: sqlite3.Connection) -> None:
    c.execute("INSERT INTO series (id) VALUES ('s1')")
    c.execute("INSERT INTO book (id, series_id) VALUES ('b1', 's1')")


def _seed_fk_parents(c: sqlite3.Connection) -> None:
    """Seed the parent rows referenced by workbench FK columns."""
    _seed_spine(c)
    c.execute("INSERT INTO character (id, name, aliases) VALUES ('c1', 'C1', '[]')")
    c.execute("INSERT INTO character (id, name, aliases) VALUES ('c2', 'C2', '[]')")
    c.execute("INSERT INTO character (id, name, aliases) VALUES ('c3', 'C3', '[]')")
    c.execute("INSERT INTO scene (id) VALUES ('sc1')")
    for rid in ("r1", "r2"):
        c.execute(
            "INSERT INTO walk_run (run_id, book_id, walk_name, status)"
            " VALUES (?, 'b1', 'walk_2d_scene_presence', 'completed')",
            (rid,),
        )
    for did in ("d1", "d2", "d3"):
        c.execute(
            "INSERT INTO workbench_decision"
            " (decision_id, book_id, target_kind, target_key, decision_type,"
            "  base_revision, payload_json, status, source, created_ms)"
            " VALUES (?, 'b1', 'presence', 'k', 'presence:present', 0, '{}',"
            "        'active', 'human', 0)",
            (did,),
        )


def test_all_workbench_tables_created(conn):
    tables = _table_names(conn)
    assert WORKBENCH_TABLES <= tables


def test_workbench_tables_idempotent(conn):
    _seed_spine(conn)
    c2 = conn
    create_schema(c2)  # second pass
    tables = _table_names(c2)
    assert WORKBENCH_TABLES <= tables


def test_create_schema_preserves_existing_human_data(conn):
    _seed_spine(conn)
    conn.execute("INSERT INTO character (id, name) VALUES ('c1', 'Alice')")
    conn.execute(
        "INSERT INTO character_book (character_id, book_id, source, confidence)"
        " VALUES ('c1', 'b1', 'human', 1.0)"
    )
    create_schema(conn)
    row = conn.execute(
        "SELECT source, confidence FROM character_book"
        " WHERE character_id = 'c1' AND book_id = 'b1'"
    ).fetchone()
    assert row == ("human", 1.0)


def test_workbench_generation_columns_and_check(conn):
    cols = _columns(conn, "workbench_generation")
    # (book_id, revision, updated_ms) NOT NULL; revision >= 0
    assert cols["book_id"][1] == 1
    assert cols["revision"][1] == 1
    assert cols["updated_ms"][1] == 1
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO workbench_generation"
            " (generation_id, book_id, revision, updated_ms)"
            " VALUES ('g1', 'b1', -1, 0)"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO workbench_generation"
            " (generation_id, book_id, revision, updated_ms)"
            " VALUES ('g1', 'b1', 0, NULL)"
        )


def test_workbench_generation_book_id_unique(conn):
    conn.execute(
        "INSERT INTO workbench_generation"
        " (generation_id, book_id, revision, updated_ms)"
        " VALUES ('g1', 'b1', 0, 0)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO workbench_generation"
            " (generation_id, book_id, revision, updated_ms)"
            " VALUES ('g2', 'b1', 0, 0)"
        )


def test_workbench_decision_constraints(conn):
    cols = _columns(conn, "workbench_decision")
    assert cols["book_id"][1] == 1
    assert cols["payload_json"][1] == 1
    for bad_kind in ("foo",):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO workbench_decision"
                " (decision_id, book_id, target_kind, target_key, decision_type,"
                "  base_revision, payload_json, status, source, created_ms)"
                " VALUES ('d1', 'b1', ?, 'k', 't', 0, '{}', 'active', 'human', 0)",
                (bad_kind,),
            )
    for bad_status in ("broken",):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO workbench_decision"
                " (decision_id, book_id, target_kind, target_key, decision_type,"
                "  base_revision, payload_json, status, source, created_ms)"
                " VALUES ('d1', 'b1', 'presence', 'k', 't', 0, '{}', ?, 'human', 0)",
                (bad_status,),
            )
    for bad_source in ("robot",):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO workbench_decision"
                " (decision_id, book_id, target_kind, target_key, decision_type,"
                "  base_revision, payload_json, status, source, created_ms)"
                " VALUES ('d1', 'b1', 'presence', 'k', 't', 0, '{}', 'active', ?, 0)",
                (bad_source,),
            )


def test_character_scene_generated_constraints(conn):
    cols = _columns(conn, "character_scene_generated")
    assert cols["confidence"][1] == 1
    for bad_relation in ("absent", "walkon"):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO character_scene_generated"
                " (id, book_id, character_id, scene_id, relation_type,"
                "  confidence, generation_revision)"
                " VALUES ('g1', 'b1', 'c1', 'sc1', ?, 0.5, 0)",
                (bad_relation,),
            )
    # confidence bounds
    for bad_conf in (-0.1, 1.5):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO character_scene_generated"
                " (id, book_id, character_id, scene_id, relation_type,"
                "  confidence, generation_revision)"
                " VALUES ('g1', 'b1', 'c1', 'sc1', 'present', ?, 0)",
                (bad_conf,),
            )


def test_character_scene_generated_unique_target_key(conn):
    # Generated rows are unique by (book, character, scene, relation_type) —
    # source_run_id / generation_revision are provenance, never uniqueness.
    _seed_fk_parents(conn)
    conn.execute(
        "INSERT INTO character_scene_generated"
        " (id, book_id, character_id, scene_id, relation_type,"
        "  confidence, generation_revision, source_run_id)"
        " VALUES ('g1', 'b1', 'c1', 'sc1', 'present', 0.5, 0, 'r1')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO character_scene_generated"
            " (id, book_id, character_id, scene_id, relation_type,"
            "  confidence, generation_revision, source_run_id)"
            " VALUES ('g2', 'b1', 'c1', 'sc1', 'present', 0.9, 1, 'r2')"
        )


def test_character_scene_manual_constraints(conn):
    cols = _columns(conn, "character_scene_manual")
    assert cols["decision_id"][1] == 1
    _seed_fk_parents(conn)
    # absent is allowed on manual rows; present/speaker/absent coexist per type.
    conn.execute(
        "INSERT INTO character_scene_manual"
        " (id, book_id, character_id, scene_id, relation_type, decision_id)"
        " VALUES ('m1', 'b1', 'c1', 'sc1', 'absent', 'd1')"
    )
    conn.execute(
        "INSERT INTO character_scene_manual"
        " (id, book_id, character_id, scene_id, relation_type, decision_id)"
        " VALUES ('m2', 'b1', 'c1', 'sc1', 'present', 'd2')"
    )
    # UNIQUE(book_id, character_id, scene_id, relation_type): same key twice fails.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO character_scene_manual"
            " (id, book_id, character_id, scene_id, relation_type, decision_id)"
            " VALUES ('m3', 'b1', 'c1', 'sc1', 'present', 'd3')"
        )


def test_character_scene_absence_active_check(conn):
    _seed_fk_parents(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO character_scene_absence"
            " (book_id, scene_id, character_id, decision_id, active, created_ms)"
            " VALUES ('b1', 'sc1', 'c1', 'd1', 2, 0)"
        )


def test_character_alias_merge_constraints(conn):
    _seed_fk_parents(conn)
    cols = _columns(conn, "character_alias_merge")
    assert cols["prior_member_name"][1] == 1
    assert cols["consequence_json"][1] == 1
    for bad_status in ("merged",):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO character_alias_merge"
                " (merge_id, book_id, canonical_id, member_id, merge_revision,"
                "  decision_id, status, prior_member_name, prior_member_aliases_json,"
                "  consequence_json, created_ms)"
                " VALUES ('mg1', 'b1', 'c1', 'c2', 0, 'd1', ?, 'name', '[]', '{}', 0)",
                (bad_status,),
            )


def test_alias_active_member_partial_unique_index(conn):
    """At most one ACTIVE merge per (book, member); inactive history is free."""
    _seed_fk_parents(conn)
    conn.execute(
        "INSERT INTO character_alias_merge"
        " (merge_id, book_id, canonical_id, member_id, merge_revision,"
        "  decision_id, status, prior_member_name, prior_member_aliases_json,"
        "  consequence_json, created_ms)"
        " VALUES ('mg1', 'b1', 'c1', 'c2', 0, 'd1', 'active', 'name', '[]', '{}', 0)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO character_alias_merge"
            " (merge_id, book_id, canonical_id, member_id, merge_revision,"
            "  decision_id, status, prior_member_name, prior_member_aliases_json,"
            "  consequence_json, created_ms)"
            " VALUES ('mg2', 'b1', 'c3', 'c2', 1, 'd2', 'active', 'name', '[]', '{}', 0)"
        )
    # Same member merged again after the first is undone is allowed.
    conn.execute(
        "UPDATE character_alias_merge SET status = 'undone' WHERE merge_id = 'mg1'"
    )
    conn.execute(
        "INSERT INTO character_alias_merge"
        " (merge_id, book_id, canonical_id, member_id, merge_revision,"
        "  decision_id, status, prior_member_name, prior_member_aliases_json,"
        "  consequence_json, created_ms)"
        " VALUES ('mg3', 'b1', 'c3', 'c2', 2, 'd3', 'active', 'name', '[]', '{}', 0)"
    )


def test_boundary_override_requires_an_anchor(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO boundary_override"
            " (override_id, book_id, decision_id, payload_json, active, created_ms)"
            " VALUES ('bo1', 'b1', 'd1', '{}', 1, 0)"
        )


def test_workbench_indexes_created(conn):
    assert WORKBENCH_INDEXES <= _index_names(conn)


def test_walk_review_item_kind_migration_preserves_rows(conn):
    """Legacy walk_review_item kind CHECK gains alias_merge; rows survive."""
    # Build a legacy DB with the old CHECK, then run create_schema over it.
    legacy = sqlite3.connect(":memory:")
    legacy.execute(
        "CREATE TABLE walk_review_item ("
        " id TEXT PRIMARY KEY, book_id TEXT, run_id TEXT,"
        " kind TEXT NOT NULL CHECK (kind IN ('voice_profile', 'voice_assignment', 'instruction')),"
        " target_table TEXT, target_id TEXT, prior_value TEXT,"
        " status TEXT NOT NULL CHECK (status IN ('pending', 'resolved', 'superseded', 'stale')),"
        " created_ms INTEGER)"
    )
    legacy.execute(
        "INSERT INTO walk_review_item (id, book_id, kind, status)"
        " VALUES ('w1', 'b1', 'voice_profile', 'pending')"
    )
    legacy.execute("PRAGMA foreign_keys = ON")
    create_schema(legacy)
    # Old row survives.
    assert legacy.execute(
        "SELECT kind, status FROM walk_review_item WHERE id = 'w1'"
    ).fetchone() == ("voice_profile", "pending")
    # New kind accepted, and the index is present.
    legacy.execute(
        "INSERT INTO walk_review_item (id, book_id, kind, status)"
        " VALUES ('w2', 'b1', 'alias_merge', 'pending')"
    )
    idx = legacy.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'"
        " AND name = 'idx_walk_review_item_book_status'"
    ).fetchone()
    assert idx is not None
    # Still rejects unknown kinds.
    with pytest.raises(sqlite3.IntegrityError):
        legacy.execute(
            "INSERT INTO walk_review_item (id, book_id, kind, status)"
            " VALUES ('w3', 'b1', 'pronunciation', 'pending')"
        )
    legacy.close()


def test_backfill_creates_one_generation_row_per_book(conn):
    _seed_spine(conn)
    conn.execute("INSERT INTO book (id, series_id) VALUES ('b2', 's1')")
    create_schema(conn)
    rows = conn.execute(
        "SELECT book_id, revision FROM workbench_generation ORDER BY book_id"
    ).fetchall()
    assert {r[0] for r in rows} == {"b1", "b2"}
    assert all(r[1] == 0 for r in rows)
