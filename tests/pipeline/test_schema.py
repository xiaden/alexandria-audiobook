"""Spec-first tests for the pipeline schema.

Covers:
- All table creation (Graph1 TREE, Graph1 EDGE, Graph2 CHARACTER, voice_config)
- All constraints (UNIQUE, CHECK, FK, NOT NULL, DEFAULT)
- span_presentation VIEW correctness with sample data
- Nested sort ordering (book.position, chapter.position, paragraph.position, span.position)
"""

from __future__ import annotations

import sqlite3

import pytest

from app.pipeline.schema import create_schema


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn():
    """Return an in-memory SQLite connection with schema + FK enforcement."""
    c = sqlite3.connect(":memory:")
    c.execute("PRAGMA foreign_keys = ON")
    create_schema(c)
    yield c
    c.close()


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {r[0] for r in rows}


def _view_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='view'"
    ).fetchall()
    return {r[0] for r in rows}


def _column_info(conn: sqlite3.Connection, table: str) -> list[tuple]:
    return conn.execute(f"PRAGMA table_info({table})").fetchall()


def _index_list(conn: sqlite3.Connection, table: str) -> list[tuple]:
    return conn.execute(f"PRAGMA index_list({table})").fetchall()


def _index_info(conn: sqlite3.Connection, index: str) -> list[tuple]:
    return conn.execute(f"PRAGMA index_info({index})").fetchall()


# ---------------------------------------------------------------------------
# Table creation
# ---------------------------------------------------------------------------


EXPECTED_TABLES = {
    # Graph1 TREE
    "series",
    "book",
    "chapter",
    "scene",
    "paragraph",
    "span",
    # Graph1 EDGE
    "book_chapter",
    "chapter_scene",
    "scene_paragraph",
    "paragraph_span",
    # Graph2 CHARACTER
    "voice_config",
    "character",
    "character_metadata",
    # Graph2 JUNCTION
    "character_series",
    "character_book",
    "character_scene",
    "character_span",
    # Universal Upgrade (Plan A)
    "render_job",
    "render_chunk",
    "walk_run",
    "walk_review_item",
    "walk_override",
    "project_snapshot",
}


class TestTableCreation:
    def test_all_tables_exist(self, conn):
        assert _table_names(conn) == EXPECTED_TABLES

    def test_span_presentation_view_exists(self, conn):
        assert "span_presentation" in _view_names(conn)

    def test_idempotent(self, conn):
        """Calling create_schema twice does not raise."""
        create_schema(conn)
        assert _table_names(conn) == EXPECTED_TABLES


# ---------------------------------------------------------------------------
# Graph1 TREE column definitions
# ---------------------------------------------------------------------------


class TestGraph1TreeColumns:
    def test_series_columns(self, conn):
        cols = {row[1]: row for row in _column_info(conn, "series")}
        assert "id" in cols
        assert cols["id"][2] == "TEXT"  # type column

    def test_book_columns(self, conn):
        cols = {row[1]: row for row in _column_info(conn, "book")}
        assert set(cols.keys()) == {
            "id", "series_id", "book_number", "version", "position", "single_speaker"
        }
        # version DEFAULT 1
        assert cols["version"][4] == '1'  # dflt_value (returned as string)
        # single_speaker INTEGER NOT NULL DEFAULT 0 (guarded ALTER)
        assert cols["single_speaker"][2] == "INTEGER"
        assert cols["single_speaker"][3] == 1  # notnull
        assert cols["single_speaker"][4] == "0"  # dflt_value

    def test_chapter_columns(self, conn):
        cols = {row[1]: row for row in _column_info(conn, "chapter")}
        assert set(cols.keys()) == {"id", "book_id"}

    def test_scene_columns(self, conn):
        cols = {row[1]: row for row in _column_info(conn, "scene")}
        assert set(cols.keys()) == {"id"}

    def test_paragraph_columns(self, conn):
        cols = {row[1]: row for row in _column_info(conn, "paragraph")}
        assert set(cols.keys()) == {"id"}

    def test_span_columns(self, conn):
        cols = {row[1]: row for row in _column_info(conn, "span")}
        assert set(cols.keys()) == {"id", "span_type", "instruct", "text"}


# ---------------------------------------------------------------------------
# Graph2 CHARACTER column definitions
# ---------------------------------------------------------------------------


class TestGraph2CharacterColumns:
    def test_character_columns(self, conn):
        cols = {row[1]: row for row in _column_info(conn, "character")}
        assert set(cols.keys()) == {"id", "name", "aliases", "voice_assignment_id", "description"}

    def test_character_aliases_default(self, conn):
        cols = {row[1]: row for row in _column_info(conn, "character")}
        # aliases DEFAULT '[]'
        assert "[]" in str(cols["aliases"][4])

    def test_character_metadata_columns(self, conn):
        cols = {row[1]: row for row in _column_info(conn, "character_metadata")}
        assert set(cols.keys()) == {"character_id", "key", "value"}

    def test_voice_config_columns(self, conn):
        cols = {row[1]: row for row in _column_info(conn, "voice_config")}
        assert set(cols.keys()) == {
            "id", "name", "description",
            "type", "voice", "character_style", "seed",
            "ref_audio", "ref_text", "adapter_id", "adapter_path", "alias_of",
        }


# ---------------------------------------------------------------------------
# Edge table columns
# ---------------------------------------------------------------------------


class TestEdgeTableColumns:
    @pytest.mark.parametrize(
        "table",
        ["book_chapter", "chapter_scene", "scene_paragraph", "paragraph_span"],
    )
    def test_edge_table_columns(self, conn, table):
        cols = {row[1]: row for row in _column_info(conn, table)}
        assert set(cols.keys()) == {"child_id", "parent_id", "position"}


# ---------------------------------------------------------------------------
# UNIQUE constraints on edge tables
# ---------------------------------------------------------------------------


class TestEdgeUniqueConstraints:
    def test_child_id_unique_book_chapter(self, conn):
        """Each child can only appear once (UNIQUE on child_id)."""
        conn.execute("INSERT INTO series VALUES ('s1')")
        conn.execute("INSERT INTO book VALUES ('b1', 's1', 1, 1, 1, 0)")
        conn.execute("INSERT INTO book VALUES ('b2', 's1', 2, 1, 2, 0)")
        conn.execute("INSERT INTO chapter VALUES ('c1', 'b1')")

        # Insert first edge
        conn.execute("INSERT INTO book_chapter VALUES ('c1', 'b1', 1)")

        # Duplicate child_id should fail
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO book_chapter VALUES ('c1', 'b2', 1)")

    def test_child_id_unique_chapter_scene(self, conn):
        conn.execute("INSERT INTO series VALUES ('s1')")
        conn.execute("INSERT INTO book VALUES ('b1', 's1', 1, 1, 1, 0)")
        conn.execute("INSERT INTO chapter VALUES ('c1', 'b1')")
        conn.execute("INSERT INTO chapter VALUES ('c2', 'b1')")
        conn.execute("INSERT INTO scene VALUES ('sc1')")

        conn.execute("INSERT INTO chapter_scene VALUES ('sc1', 'c1', 1)")

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO chapter_scene VALUES ('sc1', 'c2', 1)")

    def test_child_id_unique_scene_paragraph(self, conn):
        conn.execute("INSERT INTO series VALUES ('s1')")
        conn.execute("INSERT INTO book VALUES ('b1', 's1', 1, 1, 1, 0)")
        conn.execute("INSERT INTO chapter VALUES ('c1', 'b1')")
        conn.execute("INSERT INTO scene VALUES ('sc1')")
        conn.execute("INSERT INTO scene VALUES ('sc2')")
        conn.execute("INSERT INTO paragraph VALUES ('p1')")

        conn.execute("INSERT INTO scene_paragraph VALUES ('p1', 'sc1', 1)")

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO scene_paragraph VALUES ('p1', 'sc2', 1)")

    def test_child_id_unique_paragraph_span(self, conn):
        conn.execute("INSERT INTO paragraph VALUES ('p1')")
        conn.execute("INSERT INTO paragraph VALUES ('p2')")
        conn.execute("INSERT INTO span VALUES ('sp1', 'sentence', NULL, NULL)")

        conn.execute("INSERT INTO paragraph_span VALUES ('sp1', 'p1', 1)")

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO paragraph_span VALUES ('sp1', 'p2', 1)")

    def test_parent_position_unique_book_chapter(self, conn):
        """Same parent cannot have two children at the same position."""
        conn.execute("INSERT INTO series VALUES ('s1')")
        conn.execute("INSERT INTO book VALUES ('b1', 's1', 1, 1, 1, 0)")
        conn.execute("INSERT INTO chapter VALUES ('c1', 'b1')")
        conn.execute("INSERT INTO chapter VALUES ('c2', 'b1')")

        conn.execute("INSERT INTO book_chapter VALUES ('c1', 'b1', 1)")

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO book_chapter VALUES ('c2', 'b1', 1)")

    def test_parent_position_unique_chapter_scene(self, conn):
        conn.execute("INSERT INTO series VALUES ('s1')")
        conn.execute("INSERT INTO book VALUES ('b1', 's1', 1, 1, 1, 0)")
        conn.execute("INSERT INTO chapter VALUES ('c1', 'b1')")
        conn.execute("INSERT INTO scene VALUES ('sc1')")
        conn.execute("INSERT INTO scene VALUES ('sc2')")

        conn.execute("INSERT INTO chapter_scene VALUES ('sc1', 'c1', 1)")

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO chapter_scene VALUES ('sc2', 'c1', 1)")

    def test_parent_position_unique_scene_paragraph(self, conn):
        conn.execute("INSERT INTO scene VALUES ('sc1')")
        conn.execute("INSERT INTO paragraph VALUES ('p1')")
        conn.execute("INSERT INTO paragraph VALUES ('p2')")

        conn.execute("INSERT INTO scene_paragraph VALUES ('p1', 'sc1', 1)")

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO scene_paragraph VALUES ('p2', 'sc1', 1)")

    def test_parent_position_unique_paragraph_span(self, conn):
        conn.execute("INSERT INTO paragraph VALUES ('p1')")
        conn.execute("INSERT INTO span VALUES ('sp1', 'sentence', NULL, NULL)")
        conn.execute("INSERT INTO span VALUES ('sp2', 'sentence', NULL, NULL)")

        conn.execute("INSERT INTO paragraph_span VALUES ('sp1', 'p1', 1)")

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO paragraph_span VALUES ('sp2', 'p1', 1)")


# ---------------------------------------------------------------------------
# CHECK constraints
# ---------------------------------------------------------------------------


class TestCheckConstraints:
    def test_span_type_check_sentence(self, conn):
        conn.execute("INSERT INTO span VALUES ('sp1', 'sentence', NULL, NULL)")

    def test_span_type_check_quotation(self, conn):
        conn.execute("INSERT INTO span VALUES ('sp1', 'quotation', NULL, NULL)")

    def test_span_type_check_invalid(self, conn):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO span VALUES ('sp1', 'invalid', NULL, NULL)")

    def test_source_check_walk(self, conn):
        conn.execute("INSERT INTO series VALUES ('s1')")
        conn.execute("INSERT INTO character VALUES ('ch1', 'Alice', '[]', NULL, NULL)")
        conn.execute(
            "INSERT INTO character_series VALUES ('ch1', 's1', 'walk', 0.8, 0)"
        )

    def test_source_check_human(self, conn):
        conn.execute("INSERT INTO series VALUES ('s1')")
        conn.execute("INSERT INTO character VALUES ('ch1', 'Alice', '[]', NULL, NULL)")
        conn.execute(
            "INSERT INTO character_series VALUES ('ch1', 's1', 'human', 0.9, 1)"
        )

    def test_source_check_derived(self, conn):
        conn.execute("INSERT INTO series VALUES ('s1')")
        conn.execute("INSERT INTO character VALUES ('ch1', 'Alice', '[]', NULL, NULL)")
        conn.execute(
            "INSERT INTO character_series VALUES ('ch1', 's1', 'derived', 0.5, 0)"
        )

    def test_source_check_invalid(self, conn):
        conn.execute("INSERT INTO series VALUES ('s1')")
        conn.execute("INSERT INTO character VALUES ('ch1', 'Alice', '[]', NULL, NULL)")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO character_series VALUES ('ch1', 's1', 'invalid', 0.5, 0)"
            )

    def test_confidence_check_zero(self, conn):
        conn.execute("INSERT INTO series VALUES ('s1')")
        conn.execute("INSERT INTO character VALUES ('ch1', 'Alice', '[]', NULL, NULL)")
        conn.execute(
            "INSERT INTO character_series VALUES ('ch1', 's1', 'walk', 0.0, 0)"
        )

    def test_confidence_check_one(self, conn):
        conn.execute("INSERT INTO series VALUES ('s1')")
        conn.execute("INSERT INTO character VALUES ('ch1', 'Alice', '[]', NULL, NULL)")
        conn.execute(
            "INSERT INTO character_series VALUES ('ch1', 's1', 'walk', 1.0, 0)"
        )

    def test_confidence_check_negative(self, conn):
        conn.execute("INSERT INTO series VALUES ('s1')")
        conn.execute("INSERT INTO character VALUES ('ch1', 'Alice', '[]', NULL, NULL)")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO character_series VALUES ('ch1', 's1', 'walk', -0.1, 0)"
            )

    def test_confidence_check_over_one(self, conn):
        conn.execute("INSERT INTO series VALUES ('s1')")
        conn.execute("INSERT INTO character VALUES ('ch1', 'Alice', '[]', NULL, NULL)")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO character_series VALUES ('ch1', 's1', 'walk', 1.1, 0)"
            )

    def test_character_scene_relation_type_present(self, conn):
        conn.execute("INSERT INTO scene VALUES ('sc1')")
        conn.execute("INSERT INTO character VALUES ('ch1', 'Alice', '[]', NULL, NULL)")
        conn.execute(
            "INSERT INTO character_scene VALUES ('ch1', 'sc1', 'present', 'walk', 0.8, 0)"
        )

    def test_character_scene_relation_type_speaker(self, conn):
        conn.execute("INSERT INTO scene VALUES ('sc1')")
        conn.execute("INSERT INTO character VALUES ('ch1', 'Alice', '[]', NULL, NULL)")
        conn.execute(
            "INSERT INTO character_scene VALUES ('ch1', 'sc1', 'speaker', 'walk', 0.8, 0)"
        )

    def test_character_scene_relation_type_invalid(self, conn):
        conn.execute("INSERT INTO scene VALUES ('sc1')")
        conn.execute("INSERT INTO character VALUES ('ch1', 'Alice', '[]', NULL, NULL)")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO character_scene VALUES ('ch1', 'sc1', 'mentioned', 'walk', 0.8, 0)"
            )

    def test_character_span_relation_type_speaker(self, conn):
        conn.execute("INSERT INTO span VALUES ('sp1', 'quotation', NULL, NULL)")
        conn.execute("INSERT INTO character VALUES ('ch1', 'Alice', '[]', NULL, NULL)")
        conn.execute(
            "INSERT INTO character_span VALUES ('ch1', 'sp1', 'speaker', 'walk', 0.9, 0)"
        )

    def test_character_span_relation_type_mentioned(self, conn):
        conn.execute("INSERT INTO span VALUES ('sp1', 'sentence', NULL, NULL)")
        conn.execute("INSERT INTO character VALUES ('ch1', 'Alice', '[]', NULL, NULL)")
        conn.execute(
            "INSERT INTO character_span VALUES ('ch1', 'sp1', 'mentioned', 'walk', 0.7, 0)"
        )

    def test_character_span_relation_type_present(self, conn):
        conn.execute("INSERT INTO span VALUES ('sp1', 'sentence', NULL, NULL)")
        conn.execute("INSERT INTO character VALUES ('ch1', 'Alice', '[]', NULL, NULL)")
        conn.execute(
            "INSERT INTO character_span VALUES ('ch1', 'sp1', 'present', 'walk', 0.6, 0)"
        )

    def test_character_span_relation_type_invalid(self, conn):
        conn.execute("INSERT INTO span VALUES ('sp1', 'sentence', NULL, NULL)")
        conn.execute("INSERT INTO character VALUES ('ch1', 'Alice', '[]', NULL, NULL)")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO character_span VALUES ('ch1', 'sp1', 'present', 'invalid', 0.6, 0)"
            )


# ---------------------------------------------------------------------------
# NOT NULL constraints
# ---------------------------------------------------------------------------


class TestNotNullConstraints:
    def test_character_name_not_null(self, conn):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO character VALUES ('ch1', NULL, '[]', NULL, NULL)")

    def test_span_type_not_null(self, conn):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO span VALUES ('sp1', NULL, NULL, NULL)")

    def test_source_not_null(self, conn):
        conn.execute("INSERT INTO series VALUES ('s1')")
        conn.execute("INSERT INTO character VALUES ('ch1', 'Alice', '[]', NULL, NULL)")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO character_series VALUES ('ch1', 's1', NULL, 0.5, 0)"
            )

    def test_confidence_not_null(self, conn):
        conn.execute("INSERT INTO series VALUES ('s1')")
        conn.execute("INSERT INTO character VALUES ('ch1', 'Alice', '[]', NULL, NULL)")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO character_series VALUES ('ch1', 's1', 'walk', NULL, 0)"
            )


# ---------------------------------------------------------------------------
# DEFAULT values
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_book_version_default(self, conn):
        conn.execute("INSERT INTO series VALUES ('s1')")
        conn.execute(
            "INSERT INTO book (id, series_id, book_number, position) VALUES ('b1', 's1', 1, 1)"
        )
        row = conn.execute("SELECT version FROM book WHERE id='b1'").fetchone()
        assert row[0] == 1

    def test_character_aliases_default(self, conn):
        conn.execute("INSERT INTO character (id, name) VALUES ('ch1', 'Alice')")
        row = conn.execute("SELECT aliases FROM character WHERE id='ch1'").fetchone()
        assert row[0] == "[]"

    def test_human_override_default(self, conn):
        conn.execute("INSERT INTO series VALUES ('s1')")
        conn.execute("INSERT INTO character VALUES ('ch1', 'Alice', '[]', NULL, NULL)")
        conn.execute(
            "INSERT INTO character_series (character_id, series_id, source, confidence) "
            "VALUES ('ch1', 's1', 'walk', 0.8)"
        )
        row = conn.execute(
            "SELECT human_override FROM character_series WHERE character_id='ch1'"
        ).fetchone()
        assert row[0] == 0


# ---------------------------------------------------------------------------
# Foreign key constraints
# ---------------------------------------------------------------------------


class TestForeignKeyConstraints:
    def test_book_series_fk(self, conn):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO book VALUES ('b1', 'nonexistent', 1, 1, 1, 0)"
            )

    def test_chapter_book_fk(self, conn):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO chapter VALUES ('c1', 'nonexistent')")

    def test_book_chapter_child_fk(self, conn):
        conn.execute("INSERT INTO series VALUES ('s1')")
        conn.execute("INSERT INTO book VALUES ('b1', 's1', 1, 1, 1, 0)")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO book_chapter VALUES ('nonexistent', 'b1', 1)"
            )

    def test_book_chapter_parent_fk(self, conn):
        conn.execute("INSERT INTO series VALUES ('s1')")
        conn.execute("INSERT INTO book VALUES ('b1', 's1', 1, 1, 1, 0)")
        conn.execute("INSERT INTO chapter VALUES ('c1', 'b1')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO book_chapter VALUES ('c1', 'nonexistent', 1)"
            )

    def test_character_voice_assignment_fk(self, conn):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO character VALUES ('ch1', 'Alice', '[]', 'nonexistent', NULL)"
            )

    def test_character_voice_assignment_nullable(self, conn):
        conn.execute(
            "INSERT INTO character VALUES ('ch1', 'Alice', '[]', NULL, NULL)"
        )

    def test_character_metadata_character_fk(self, conn):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO character_metadata VALUES ('nonexistent', 'key', 'value')"
            )

    def test_character_series_character_fk(self, conn):
        conn.execute("INSERT INTO series VALUES ('s1')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO character_series VALUES ('nonexistent', 's1', 'walk', 0.8, 0)"
            )

    def test_character_series_series_fk(self, conn):
        conn.execute("INSERT INTO character VALUES ('ch1', 'Alice', '[]', NULL, NULL)")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO character_series VALUES ('ch1', 'nonexistent', 'walk', 0.8, 0)"
            )

    def test_character_span_character_fk(self, conn):
        conn.execute("INSERT INTO span VALUES ('sp1', 'sentence', NULL, NULL)")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO character_span VALUES ('nonexistent', 'sp1', 'speaker', 'walk', 0.9, 0)"
            )

    def test_character_span_span_fk(self, conn):
        conn.execute("INSERT INTO character VALUES ('ch1', 'Alice', '[]', NULL, NULL)")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO character_span VALUES ('ch1', 'nonexistent', 'speaker', 'walk', 0.9, 0)"
            )


# ---------------------------------------------------------------------------
# UNIQUE constraint on character_metadata
# ---------------------------------------------------------------------------


class TestCharacterMetadataUnique:
    def test_unique_character_key(self, conn):
        conn.execute("INSERT INTO character VALUES ('ch1', 'Alice', '[]', NULL, NULL)")
        conn.execute(
            "INSERT INTO character_metadata VALUES ('ch1', 'age', '30')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO character_metadata VALUES ('ch1', 'age', '31')"
            )

    def test_same_key_different_characters(self, conn):
        conn.execute("INSERT INTO character VALUES ('ch1', 'Alice', '[]', NULL, NULL)")
        conn.execute("INSERT INTO character VALUES ('ch2', 'Bob', '[]', NULL, NULL)")
        conn.execute(
            "INSERT INTO character_metadata VALUES ('ch1', 'age', '30')"
        )
        conn.execute(
            "INSERT INTO character_metadata VALUES ('ch2', 'age', '25')"
        )


# ---------------------------------------------------------------------------
# span_presentation VIEW — correctness and nested sort ordering
# ---------------------------------------------------------------------------


def _populate_spine(conn: sqlite3.Connection):
    """Insert a minimal but complete spine for VIEW testing.

    Structure:
      series s1
        book b1 (position=1)
          chapter c1 (position=1)
            scene sc1 (position=1)
              paragraph p1 (position=1)
                span sp1 (position=1, sentence)
                span sp2 (position=2, quotation)
              paragraph p2 (position=2)
                span sp3 (position=1, sentence)
          chapter c2 (position=2)
            scene sc2 (position=1)
              paragraph p3 (position=1)
                span sp4 (position=1, sentence)
        book b2 (position=2)
          chapter c3 (position=1)
            scene sc3 (position=1)
              paragraph p4 (position=1)
                span sp5 (position=1, quotation)
    """
    conn.execute("INSERT INTO series VALUES ('s1')")
    conn.execute("INSERT INTO book VALUES ('b1', 's1', 1, 1, 1, 0)")
    conn.execute("INSERT INTO book VALUES ('b2', 's1', 2, 1, 2, 0)")
    conn.execute("INSERT INTO chapter VALUES ('c1', 'b1')")
    conn.execute("INSERT INTO chapter VALUES ('c2', 'b1')")
    conn.execute("INSERT INTO chapter VALUES ('c3', 'b2')")
    conn.execute("INSERT INTO scene VALUES ('sc1')")
    conn.execute("INSERT INTO scene VALUES ('sc2')")
    conn.execute("INSERT INTO scene VALUES ('sc3')")
    conn.execute("INSERT INTO paragraph VALUES ('p1')")
    conn.execute("INSERT INTO paragraph VALUES ('p2')")
    conn.execute("INSERT INTO paragraph VALUES ('p3')")
    conn.execute("INSERT INTO paragraph VALUES ('p4')")
    conn.execute("INSERT INTO span VALUES ('sp1', 'sentence', NULL, NULL)")
    conn.execute("INSERT INTO span VALUES ('sp2', 'quotation', 'angrily', NULL)")
    conn.execute("INSERT INTO span VALUES ('sp3', 'sentence', NULL, NULL)")
    conn.execute("INSERT INTO span VALUES ('sp4', 'sentence', NULL, NULL)")
    conn.execute("INSERT INTO span VALUES ('sp5', 'quotation', 'softly', NULL)")

    # Edge tables
    conn.execute("INSERT INTO book_chapter VALUES ('c1', 'b1', 1)")
    conn.execute("INSERT INTO book_chapter VALUES ('c2', 'b1', 2)")
    conn.execute("INSERT INTO book_chapter VALUES ('c3', 'b2', 1)")
    conn.execute("INSERT INTO chapter_scene VALUES ('sc1', 'c1', 1)")
    conn.execute("INSERT INTO chapter_scene VALUES ('sc2', 'c2', 1)")
    conn.execute("INSERT INTO chapter_scene VALUES ('sc3', 'c3', 1)")
    conn.execute("INSERT INTO scene_paragraph VALUES ('p1', 'sc1', 1)")
    conn.execute("INSERT INTO scene_paragraph VALUES ('p2', 'sc1', 2)")
    conn.execute("INSERT INTO scene_paragraph VALUES ('p3', 'sc2', 1)")
    conn.execute("INSERT INTO scene_paragraph VALUES ('p4', 'sc3', 1)")
    conn.execute("INSERT INTO paragraph_span VALUES ('sp1', 'p1', 1)")
    conn.execute("INSERT INTO paragraph_span VALUES ('sp2', 'p1', 2)")
    conn.execute("INSERT INTO paragraph_span VALUES ('sp3', 'p2', 1)")
    conn.execute("INSERT INTO paragraph_span VALUES ('sp4', 'p3', 1)")
    conn.execute("INSERT INTO paragraph_span VALUES ('sp5', 'p4', 1)")


class TestSpanPresentationView:
    def test_view_returns_all_spans(self, conn):
        _populate_spine(conn)
        rows = conn.execute(
            "SELECT id, span_type, instruct, global_index FROM span_presentation ORDER BY global_index"
        ).fetchall()
        assert len(rows) == 5

    def test_view_global_index_ordering(self, conn):
        """Nested sort: book.position, chapter.position, paragraph.position, span.position."""
        _populate_spine(conn)
        rows = conn.execute(
            "SELECT id, global_index FROM span_presentation ORDER BY global_index"
        ).fetchall()
        ids = [r[0] for r in rows]
        # Expected order:
        # b1(1) -> c1(1) -> sc1(1) -> p1(1) -> sp1(1), sp2(2)
        #                        -> p2(2) -> sp3(1)
        #        -> c2(2) -> sc2(1) -> p3(1) -> sp4(1)
        # b2(2) -> c3(1) -> sc3(1) -> p4(1) -> sp5(1)
        assert ids == ["sp1", "sp2", "sp3", "sp4", "sp5"]

    def test_view_global_index_values(self, conn):
        _populate_spine(conn)
        rows = conn.execute(
            "SELECT id, global_index FROM span_presentation ORDER BY global_index"
        ).fetchall()
        indices = {r[0]: r[1] for r in rows}
        assert indices == {
            "sp1": 1,
            "sp2": 2,
            "sp3": 3,
            "sp4": 4,
            "sp5": 5,
        }

    def test_view_includes_instruct(self, conn):
        _populate_spine(conn)
        rows = conn.execute(
            "SELECT id, instruct FROM span_presentation ORDER BY global_index"
        ).fetchall()
        instructs = {r[0]: r[1] for r in rows}
        assert instructs["sp1"] is None
        assert instructs["sp2"] == "angrily"
        assert instructs["sp5"] == "softly"

    def test_view_includes_span_type(self, conn):
        _populate_spine(conn)
        rows = conn.execute(
            "SELECT id, span_type FROM span_presentation ORDER BY global_index"
        ).fetchall()
        types = {r[0]: r[1] for r in rows}
        assert types["sp1"] == "sentence"
        assert types["sp2"] == "quotation"

    def test_view_cross_book_ordering(self, conn):
        """Spans from book b2 come after all spans from book b1."""
        _populate_spine(conn)
        rows = conn.execute(
            "SELECT id, global_index FROM span_presentation ORDER BY global_index"
        ).fetchall()
        # sp5 is in b2, should have the highest global_index
        sp5_index = [r[1] for r in rows if r[0] == "sp5"][0]
        assert sp5_index == 5

    def test_view_cross_chapter_ordering(self, conn):
        """Spans from c2 come after all spans from c1 (within b1)."""
        _populate_spine(conn)
        rows = conn.execute(
            "SELECT id, global_index FROM span_presentation ORDER BY global_index"
        ).fetchall()
        indices = {r[0]: r[1] for r in rows}
        # sp4 is in c2, should come after sp1, sp2, sp3 (all in c1)
        assert indices["sp4"] > indices["sp3"]

    def test_view_cross_paragraph_ordering(self, conn):
        """Spans from p2 come after all spans from p1 (within sc1)."""
        _populate_spine(conn)
        rows = conn.execute(
            "SELECT id, global_index FROM span_presentation ORDER BY global_index"
        ).fetchall()
        indices = {r[0]: r[1] for r in rows}
        # sp3 is in p2, should come after sp1, sp2 (both in p1)
        assert indices["sp3"] > indices["sp2"]

    def test_view_within_paragraph_ordering(self, conn):
        """Spans within the same paragraph are ordered by span.position."""
        _populate_spine(conn)
        rows = conn.execute(
            "SELECT id, global_index FROM span_presentation ORDER BY global_index"
        ).fetchall()
        indices = {r[0]: r[1] for r in rows}
        # sp1 and sp2 are both in p1; sp1 has position=1, sp2 has position=2
        assert indices["sp1"] < indices["sp2"]


# ---------------------------------------------------------------------------
# No forbidden fields
# ---------------------------------------------------------------------------


class TestNoForbiddenFields:
    def test_no_timestamps(self, conn):
        """No table should have created_at or updated_at columns."""
        for table in EXPECTED_TABLES:
            cols = {row[1] for row in _column_info(conn, table)}
            assert "created_at" not in cols, f"{table} has created_at"
            assert "updated_at" not in cols, f"{table} has updated_at"

    def test_no_content_hash(self, conn):
        """No table should have content_hash column."""
        for table in EXPECTED_TABLES:
            cols = {row[1] for row in _column_info(conn, table)}
            assert "content_hash" not in cols, f"{table} has content_hash"

    def test_no_jaccard_fields(self, conn):
        """No table should have jaccard-related columns."""
        for table in EXPECTED_TABLES:
            cols = {row[1] for row in _column_info(conn, table)}
            for col in cols:
                assert "jaccard" not in col.lower(), f"{table} has {col}"

    def test_no_reattribution_scope(self, conn):
        """No table should have reattribution_scope column."""
        for table in EXPECTED_TABLES:
            cols = {row[1] for row in _column_info(conn, table)}
            assert "reattribution_scope" not in cols, f"{table} has reattribution_scope"

    def test_scene_has_no_chapter_id(self, conn):
        """scene table should not have chapter_id FK (uses chapter_scene edge)."""
        cols = {row[1] for row in _column_info(conn, "scene")}
        assert "chapter_id" not in cols

# ---------------------------------------------------------------------------
# Universal Upgrade schema (Plan A) — 6 tables, 3 indices, book.single_speaker
# ---------------------------------------------------------------------------


class TestUniversalUpgradeColumns:
    """Column sets for the Universal Upgrade tables (per contracts ledger)."""

    def test_render_job_columns(self, conn):
        cols = {row[1]: row for row in _column_info(conn, "render_job")}
        assert set(cols.keys()) == {
            "job_id", "book_id", "mode", "status",
            "error", "output_dir", "output_artifact_path",
            "created_ms", "started_ms", "finished_ms",
        }
        # job_id TEXT PK; unix-ms timestamps are INTEGER (new tables only)
        assert cols["job_id"][2] == "TEXT"
        assert cols["created_ms"][2] == "INTEGER"
        assert cols["started_ms"][2] == "INTEGER"
        assert cols["finished_ms"][2] == "INTEGER"

    def test_render_chunk_columns(self, conn):
        cols = {row[1]: row for row in _column_info(conn, "render_chunk")}
        assert set(cols.keys()) == {"job_id", "idx", "status", "wav_path", "error"}

    def test_walk_run_columns(self, conn):
        cols = {row[1]: row for row in _column_info(conn, "walk_run")}
        assert set(cols.keys()) == {
            "run_id", "book_id", "walk_name", "status", "cancel_requested",
            "heartbeat_ms", "result_json", "error", "created_ms", "finished_ms",
        }
        assert cols["run_id"][2] == "TEXT"
        assert cols["cancel_requested"][2] == "INTEGER"
        assert cols["heartbeat_ms"][2] == "INTEGER"
        assert cols["created_ms"][2] == "INTEGER"
        assert cols["finished_ms"][2] == "INTEGER"

    def test_walk_review_item_columns(self, conn):
        cols = {row[1]: row for row in _column_info(conn, "walk_review_item")}
        assert set(cols.keys()) == {
            "id", "book_id", "run_id", "kind", "target_table",
            "target_id", "prior_value", "status", "created_ms",
        }
        assert cols["id"][2] == "TEXT"
        assert cols["created_ms"][2] == "INTEGER"

    def test_walk_override_columns(self, conn):
        cols = {row[1]: row for row in _column_info(conn, "walk_override")}
        assert set(cols.keys()) == {"book_id", "walk_name", "key", "value_json"}

    def test_project_snapshot_columns(self, conn):
        cols = {row[1]: row for row in _column_info(conn, "project_snapshot")}
        assert set(cols.keys()) == {"name", "book_id", "snapshot_json", "created_ms"}
        assert cols["name"][2] == "TEXT"
        assert cols["created_ms"][2] == "INTEGER"


class TestUniversalUpgradeCheckConstraints:
    """CHECK enum values registered in the contracts ledger."""

    def test_render_job_mode_batch(self, conn):
        conn.execute(
            "INSERT INTO render_job (job_id, mode, status) "
            "VALUES ('j1', 'batch', 'pending')"
        )

    def test_render_job_mode_individual(self, conn):
        conn.execute(
            "INSERT INTO render_job (job_id, mode, status) "
            "VALUES ('j1', 'individual', 'pending')"
        )

    def test_render_job_mode_invalid(self, conn):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO render_job (job_id, mode, status) "
                "VALUES ('j1', 'invalid', 'pending')"
            )

    @pytest.mark.parametrize(
        "status",
        ["pending", "running", "completed", "failed", "cancelled", "interrupted", "expired"],
    )
    def test_render_job_status_valid(self, conn, status):
        conn.execute(
            "INSERT INTO render_job (job_id, mode, status) VALUES ('j1', 'batch', ?)",
            (status,),
        )

    def test_render_job_status_invalid(self, conn):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO render_job (job_id, mode, status) "
                "VALUES ('j1', 'batch', 'queued')"
            )

    @pytest.mark.parametrize("status", ["pending", "done", "failed", "evicted"])
    def test_render_chunk_status_valid(self, conn, status):
        conn.execute(
            "INSERT INTO render_job (job_id, mode, status) VALUES ('j1', 'batch', 'pending')"
        )
        conn.execute(
            "INSERT INTO render_chunk (job_id, idx, status) VALUES ('j1', 0, ?)",
            (status,),
        )

    def test_render_chunk_status_invalid(self, conn):
        conn.execute(
            "INSERT INTO render_job (job_id, mode, status) VALUES ('j1', 'batch', 'pending')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO render_chunk (job_id, idx, status) VALUES ('j1', 0, 'invalid')"
            )

    @pytest.mark.parametrize(
        "status",
        ["pending", "running", "completed", "failed", "interrupted", "cancelled"],
    )
    def test_walk_run_status_valid(self, conn, status):
        conn.execute(
            "INSERT INTO walk_run (run_id, status) VALUES ('r1', ?)",
            (status,),
        )

    def test_walk_run_status_invalid(self, conn):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO walk_run (run_id, status) VALUES ('r1', 'queued')"
            )

    @pytest.mark.parametrize(
        "kind", ["voice_profile", "voice_assignment", "instruction"]
    )
    def test_walk_review_item_kind_valid(self, conn, kind):
        conn.execute(
            "INSERT INTO walk_review_item (id, kind, status) VALUES ('w1', ?, 'pending')",
            (kind,),
        )

    def test_walk_review_item_kind_invalid(self, conn):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO walk_review_item (id, kind, status) "
                "VALUES ('w1', 'pronunciation', 'pending')"
            )

    @pytest.mark.parametrize("status", ["pending", "resolved", "superseded", "stale"])
    def test_walk_review_item_status_valid(self, conn, status):
        conn.execute(
            "INSERT INTO walk_review_item (id, kind, status) VALUES ('w1', 'instruction', ?)",
            (status,),
        )

    def test_walk_review_item_status_invalid(self, conn):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO walk_review_item (id, kind, status) "
                "VALUES ('w1', 'instruction', 'closed')"
            )


class TestUniversalUpgradeIndices:
    """Composite (book_id, status) indices from the contracts ledger."""

    def test_render_job_book_status_index(self, conn):
        names = {r[1] for r in _index_list(conn, "render_job")}
        assert "idx_render_job_book_status" in names
        cols = [r[2] for r in _index_info(conn, "idx_render_job_book_status")]
        assert cols == ["book_id", "status"]

    def test_walk_run_book_status_index(self, conn):
        names = {r[1] for r in _index_list(conn, "walk_run")}
        assert "idx_walk_run_book_status" in names
        cols = [r[2] for r in _index_info(conn, "idx_walk_run_book_status")]
        assert cols == ["book_id", "status"]

    def test_walk_review_item_book_status_index(self, conn):
        names = {r[1] for r in _index_list(conn, "walk_review_item")}
        assert "idx_walk_review_item_book_status" in names
        cols = [r[2] for r in _index_info(conn, "idx_walk_review_item_book_status")]
        assert cols == ["book_id", "status"]


class TestUniversalUpgradeConstraints:
    """PK / FK constraints from the contracts ledger."""

    def test_render_chunk_composite_pk(self, conn):
        conn.execute(
            "INSERT INTO render_job (job_id, mode, status) VALUES ('j1', 'batch', 'pending')"
        )
        conn.execute(
            "INSERT INTO render_chunk (job_id, idx, status) VALUES ('j1', 0, 'pending')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO render_chunk (job_id, idx, status) VALUES ('j1', 0, 'done')"
            )

    def test_render_chunk_job_fk(self, conn):
        """render_chunk.job_id references render_job(job_id)."""
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO render_chunk (job_id, idx, status) VALUES ('missing', 0, 'pending')"
            )

    def test_walk_override_composite_pk(self, conn):
        conn.execute(
            "INSERT INTO walk_override (book_id, walk_name, key, value_json) "
            "VALUES ('b1', 'w1', 'k1', '{}')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO walk_override (book_id, walk_name, key, value_json) "
                "VALUES ('b1', 'w1', 'k1', '{}')"
            )

    def test_render_job_job_id_pk(self, conn):
        conn.execute(
            "INSERT INTO render_job (job_id, mode, status) VALUES ('j1', 'batch', 'pending')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO render_job (job_id, mode, status) VALUES ('j1', 'individual', 'pending')"
            )

    def test_project_snapshot_name_pk(self, conn):
        conn.execute(
            "INSERT INTO project_snapshot (name, book_id, snapshot_json) "
            "VALUES ('snap1', 'b1', '{}')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO project_snapshot (name, book_id, snapshot_json) "
                "VALUES ('snap1', 'b2', '{}')"
            )


class TestBookSingleSpeaker:
    """book.single_speaker INTEGER NOT NULL DEFAULT 0 (render-boundary only)."""

    def test_single_speaker_default_zero(self, conn):
        conn.execute("INSERT INTO series VALUES ('s1')")
        conn.execute(
            "INSERT INTO book (id, series_id, book_number, position) "
            "VALUES ('b1', 's1', 1, 1)"
        )
        row = conn.execute(
            "SELECT single_speaker FROM book WHERE id='b1'"
        ).fetchone()
        assert row[0] == 0

    def test_single_speaker_not_null(self, conn):
        conn.execute("INSERT INTO series VALUES ('s1')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO book (id, series_id, book_number, position, single_speaker) "
                "VALUES ('b1', 's1', 1, 1, NULL)"
            )
