"""Tests for voice_config schema migration script (Plan O).

Tests verify:
1. Migration adds missing columns to old schema (3 columns → 11 columns)
2. Migration is idempotent (running twice produces no errors)
3. Migration logs status correctly
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.pipeline.adapter import InMemorySQLiteAdapter
from app.pipeline.schema import create_schema
from scripts.migrate_voice_config_schema import (
    VOICE_CONFIG_COLUMNS,
    get_existing_columns,
    migrate_voice_config_schema,
)


@pytest.fixture
def old_schema_db(tmp_path: Path) -> Path:
    """Create a test DB with old voice_config schema (3 columns only)."""
    db_path = tmp_path / "test_pipeline.db"
    conn = sqlite3.connect(str(db_path))

    # Create old schema (3 columns only)
    conn.execute("""
        CREATE TABLE voice_config (
            id TEXT PRIMARY KEY,
            name TEXT,
            description TEXT
        )
    """)
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def new_schema_db(tmp_path: Path) -> Path:
    """Create a test DB with new voice_config schema (11 columns)."""
    db_path = tmp_path / "test_pipeline.db"
    conn = sqlite3.connect(str(db_path))

    # Create new schema (11 columns)
    conn.execute("""
        CREATE TABLE voice_config (
            id TEXT PRIMARY KEY,
            name TEXT,
            description TEXT,
            type TEXT DEFAULT 'custom',
            voice TEXT,
            character_style TEXT,
            seed TEXT DEFAULT '-1',
            ref_audio TEXT,
            ref_text TEXT,
            adapter_id TEXT,
            adapter_path TEXT,
            alias_of TEXT
        )
    """)
    conn.commit()
    conn.close()
    return db_path


class TestMigrationOldSchema:
    """Test migration on old schema (3 columns)."""

    def test_migration_adds_missing_columns(self, old_schema_db: Path) -> None:
        """Verify migration adds all 9 missing columns."""
        # Before migration: 3 columns
        conn = sqlite3.connect(str(old_schema_db))
        before_cols = get_existing_columns(conn)
        conn.close()
        assert before_cols == {"id", "name", "description"}

        # Run migration
        migrate_voice_config_schema(str(old_schema_db))

        # After migration: 12 columns (3 original + 9 new)
        conn = sqlite3.connect(str(old_schema_db))
        after_cols = get_existing_columns(conn)
        conn.close()

        expected_cols = {"id", "name", "description"}
        for col_name, _ in VOICE_CONFIG_COLUMNS:
            expected_cols.add(col_name)

        assert after_cols == expected_cols

    def test_migration_preserves_existing_data(self, old_schema_db: Path) -> None:
        """Verify migration doesn't lose existing data."""
        # Insert test data
        conn = sqlite3.connect(str(old_schema_db))
        conn.execute(
            "INSERT INTO voice_config (id, name, description) VALUES (?, ?, ?)",
            ("test-1", "Test Voice", "A test voice"),
        )
        conn.commit()
        conn.close()

        # Run migration
        migrate_voice_config_schema(str(old_schema_db))

        # Verify data preserved
        conn = sqlite3.connect(str(old_schema_db))
        cursor = conn.execute("SELECT id, name, description FROM voice_config")
        row = cursor.fetchone()
        conn.close()

        assert row == ("test-1", "Test Voice", "A test voice")

    def test_new_columns_have_correct_defaults(self, old_schema_db: Path) -> None:
        """Verify new columns have correct default values."""
        # Run migration
        migrate_voice_config_schema(str(old_schema_db))

        # Insert a row with only id/name/description
        conn = sqlite3.connect(str(old_schema_db))
        conn.execute(
            "INSERT INTO voice_config (id, name, description) VALUES (?, ?, ?)",
            ("test-1", "Test Voice", "A test voice"),
        )
        conn.commit()

        # Check defaults
        cursor = conn.execute("SELECT type, seed FROM voice_config WHERE id = 'test-1'")
        row = cursor.fetchone()
        conn.close()

        type_val, seed_val = row
        assert type_val == "custom"
        assert seed_val == "-1"


class TestMigrationIdempotency:
    """Test migration is idempotent."""

    def test_migration_runs_twice_without_error(self, old_schema_db: Path) -> None:
        """Verify migration can run multiple times without errors."""
        # First run
        migrate_voice_config_schema(str(old_schema_db))

        # Second run — should not raise
        migrate_voice_config_schema(str(old_schema_db))

        # Verify schema is correct
        conn = sqlite3.connect(str(old_schema_db))
        cols = get_existing_columns(conn)
        conn.close()

        expected_cols = {"id", "name", "description"}
        for col_name, _ in VOICE_CONFIG_COLUMNS:
            expected_cols.add(col_name)

        assert cols == expected_cols

    def test_migration_on_new_schema_no_error(self, new_schema_db: Path) -> None:
        """Verify migration on already-migrated schema produces no errors."""
        # Run migration on schema that already has all columns
        migrate_voice_config_schema(str(new_schema_db))

        # Verify schema unchanged
        conn = sqlite3.connect(str(new_schema_db))
        cols = get_existing_columns(conn)
        conn.close()

        expected_cols = {"id", "name", "description"}
        for col_name, _ in VOICE_CONFIG_COLUMNS:
            expected_cols.add(col_name)

        assert cols == expected_cols


class TestMigrationEdgeCases:
    """Test migration edge cases."""

    def test_migration_on_nonexistent_table(self, tmp_path: Path) -> None:
        """Verify migration handles missing voice_config table gracefully."""
        db_path = tmp_path / "empty.db"
        conn = sqlite3.connect(str(db_path))
        conn.close()

        # Should not raise — just log warning
        migrate_voice_config_schema(str(db_path))


class TestIntegrationWithInMemoryAdapter:
    """Test migration with InMemorySQLiteAdapter pattern."""

    def test_migration_pattern_with_inmemory(self) -> None:
        """Verify migration logic works with in-memory adapter pattern."""
        # Create in-memory adapter with old schema
        adapter = InMemorySQLiteAdapter()
        conn = adapter.get_connection()

        # Drop the new schema and create old schema
        conn.execute("DROP TABLE IF EXISTS voice_config")
        conn.execute("""
            CREATE TABLE voice_config (
                id TEXT PRIMARY KEY,
                name TEXT,
                description TEXT
            )
        """)
        conn.commit()

        # Verify old schema
        before_cols = get_existing_columns(conn)
        assert before_cols == {"id", "name", "description"}

        # Manually apply migration logic (can't use file path with in-memory)
        for col_name, col_def in VOICE_CONFIG_COLUMNS:
            existing = get_existing_columns(conn)
            if col_name not in existing:
                conn.execute(
                    f"ALTER TABLE voice_config ADD COLUMN {col_name} {col_def}"
                )
        conn.commit()

        # Verify new schema
        after_cols = get_existing_columns(conn)
        expected_cols = {"id", "name", "description"}
        for col_name, _ in VOICE_CONFIG_COLUMNS:
            expected_cols.add(col_name)
        assert after_cols == expected_cols

        adapter.close()


class TestCreateSchemaMigrationForUniversalUpgrade:
    """create_schema must evolve legacy DBs and stay idempotent (Plan A).

    The Universal Upgrade adds ``book.single_speaker`` via a guarded
    ALTER TABLE, so an existing (pre-upgrade) database gains the column
    without data loss and a second create_schema run adds nothing twice.
    """

    def test_create_schema_adds_single_speaker_to_legacy_book(
        self, tmp_path: Path
    ) -> None:
        """A legacy book table (5 columns) gains single_speaker, data intact."""
        db_path = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE book (
                id TEXT PRIMARY KEY,
                series_id TEXT NOT NULL REFERENCES series(id),
                book_number INTEGER,
                version INTEGER DEFAULT 1,
                position INTEGER
            )
        """)
        conn.execute(
            "INSERT INTO book (id, series_id, book_number, version, position) "
            "VALUES ('b1', 's1', 1, 1, 1)"
        )
        conn.commit()
        conn.close()

        # Run create_schema — should add single_speaker without touching data
        conn = sqlite3.connect(str(db_path))
        create_schema(conn)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(book)").fetchall()}
        assert "single_speaker" in cols
        row = conn.execute(
            "SELECT series_id, book_number, single_speaker FROM book WHERE id='b1'"
        ).fetchone()
        assert row == ("s1", 1, 0)
        conn.close()

    def test_create_schema_idempotent_on_file_db(self, tmp_path: Path) -> None:
        """Running create_schema twice on a file-backed DB adds nothing twice."""
        db_path = tmp_path / "schema.db"
        conn = sqlite3.connect(str(db_path))
        create_schema(conn)
        conn.close()

        # Second run against the same file — no error, no duplicates
        conn = sqlite3.connect(str(db_path))
        create_schema(conn)
        dup_tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "GROUP BY name HAVING COUNT(*) > 1"
        ).fetchall()
        assert dup_tables == []
        dup_indices = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name NOT LIKE 'sqlite_%' GROUP BY name HAVING COUNT(*) > 1"
        ).fetchall()
        assert dup_indices == []
        # book has exactly one single_speaker column
        cols = [r[1] for r in conn.execute("PRAGMA table_info(book)").fetchall()]
        assert cols.count("single_speaker") == 1
        conn.close()


class TestCreateSchemaPauseColumns:
    """create_schema adds nullable Plan L pause columns idempotently."""

    def test_legacy_book_gains_nullable_pause_columns(self, tmp_path: Path) -> None:
        """A legacy book table gains the two pause override columns, NULL."""
        db_path = tmp_path / "legacy_pause.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE book (
                id TEXT PRIMARY KEY,
                series_id TEXT NOT NULL REFERENCES series(id),
                book_number INTEGER,
                version INTEGER DEFAULT 1,
                position INTEGER,
                single_speaker INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute(
            "INSERT INTO book (id, series_id, book_number, version, position) "
            "VALUES ('b1', 's1', 1, 1, 1)"
        )
        conn.commit()
        conn.close()

        conn = sqlite3.connect(str(db_path))
        create_schema(conn)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(book)").fetchall()}
        assert "pause_between_speakers_ms" in cols
        assert "pause_same_speaker_ms" in cols
        # Existing row migrates with NULL (no override) — never coerced to 0.
        row = conn.execute(
            "SELECT pause_between_speakers_ms, pause_same_speaker_ms"
            " FROM book WHERE id='b1'"
        ).fetchone()
        assert row == (None, None)
        conn.close()

    def test_legacy_span_gains_pause_after_with_check(self, tmp_path: Path) -> None:
        """A legacy span table gains pause_after_ms INTEGER NULL + CHECK."""
        db_path = tmp_path / "legacy_span_pause.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE span (
                id TEXT PRIMARY KEY,
                span_type TEXT,
                instruct TEXT,
                text TEXT
            )
        """)
        conn.execute(
            "INSERT INTO span (id, span_type, instruct, text) "
            "VALUES ('sp1', 'sentence', NULL, 'hello')"
        )
        conn.commit()
        conn.close()

        conn = sqlite3.connect(str(db_path))
        create_schema(conn)
        col_info = {
            row[1]: row for row in conn.execute("PRAGMA table_info(span)").fetchall()
        }
        assert "pause_after_ms" in col_info
        # nullable column (notnull = 0)
        assert col_info["pause_after_ms"][3] == 0
        # CHECK constraint registered in the table's DDL
        span_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='span'"
        ).fetchone()[0]
        assert "pause_after_ms IS NULL OR pause_after_ms >= 0" in span_sql
        # existing row migrates with NULL
        row = conn.execute("SELECT pause_after_ms FROM span WHERE id='sp1'").fetchone()
        assert row == (None,)
        # CHECK rejects negative, accepts 0 and NULL
        conn.execute("UPDATE span SET pause_after_ms = 0 WHERE id='sp1'")
        conn.execute("UPDATE span SET pause_after_ms = 500 WHERE id='sp1'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE span SET pause_after_ms = -1 WHERE id='sp1'")
        conn.close()

    def test_create_schema_pause_columns_idempotent(self, tmp_path: Path) -> None:
        """Running create_schema twice adds the pause columns only once."""
        db_path = tmp_path / "pause_schema.db"
        conn = sqlite3.connect(str(db_path))
        create_schema(conn)
        conn.close()

        conn = sqlite3.connect(str(db_path))
        create_schema(conn)
        book_cols = [r[1] for r in conn.execute("PRAGMA table_info(book)").fetchall()]
        assert book_cols.count("pause_between_speakers_ms") == 1
        assert book_cols.count("pause_same_speaker_ms") == 1
        span_cols = [r[1] for r in conn.execute("PRAGMA table_info(span)").fetchall()]
        assert span_cols.count("pause_after_ms") == 1
        conn.close()


class TestParityMigrationOnOldSchema:
    """create_schema adds the three parity tables to a pre-parity DB idempotently.

    The Voice / Persona / Prompt parity tables (``clone_reference``,
    ``persona_revision``, ``prompt_config_revision``) are additive.  An
    existing database created before the parity DDL existed must gain all
    three tables plus their indexes on a single ``create_schema`` run,
    without data loss, and a second run must add nothing twice.
    """

    PARITY_TABLES = frozenset(
        {"clone_reference", "persona_revision", "prompt_config_revision"}
    )

    def _old_schema_db(self, tmp_path: Path, name: str) -> Path:
        """A pre-parity DB: full old schema but without the three parity tables."""
        db_path = tmp_path / name
        conn = sqlite3.connect(str(db_path))
        # Create the base tables (Graph1/Graph2 + universal upgrade + workbench)
        # by running create_schema, then drop the three parity tables to emulate
        # a database created before the parity DDL existed.
        create_schema(conn)
        for table in self.PARITY_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.commit()
        conn.close()
        return db_path

    def _parity_table_names(self, conn: sqlite3.Connection) -> set[str]:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return {r[0] for r in rows}

    def _parity_index_names(self, conn: sqlite3.Connection) -> set[str]:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
            " AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return {r[0] for r in rows}

    def test_old_schema_gains_parity_tables(self, tmp_path: Path) -> None:
        db_path = self._old_schema_db(tmp_path, "old.db")

        conn = sqlite3.connect(str(db_path))
        assert self._parity_table_names(conn).isdisjoint(self.PARITY_TABLES)
        conn.close()

        # Run create_schema — the parity tables and indexes appear.
        conn = sqlite3.connect(str(db_path))
        create_schema(conn)
        tables = self._parity_table_names(conn)
        conn.close()
        assert self.PARITY_TABLES <= tables

        conn = sqlite3.connect(str(db_path))
        indexes = self._parity_index_names(conn)
        conn.close()
        assert {
            "idx_clone_reference_voice_owner",
            "idx_persona_revision_character",
            "idx_prompt_config_revision_book_task",
        } <= indexes

    def test_old_schema_parity_migration_preserves_data(self, tmp_path: Path) -> None:
        """Existing rows in old tables survive the parity migration."""
        db_path = tmp_path / "preserve.db"
        conn = sqlite3.connect(str(db_path))
        create_schema(conn)
        for table in self.PARITY_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute("INSERT INTO series VALUES ('s1')")
        conn.execute(
            "INSERT INTO book (id, series_id, book_number, position, single_speaker)"
            " VALUES ('b1', 's1', 1, 1, 0)"
        )
        conn.execute("INSERT INTO voice_config (id, name) VALUES ('v1', 'Voice')")
        conn.commit()
        conn.close()

        conn = sqlite3.connect(str(db_path))
        create_schema(conn)
        row = conn.execute("SELECT id FROM book WHERE id='b1'").fetchone()
        voice = conn.execute("SELECT id FROM voice_config WHERE id='v1'").fetchone()
        conn.close()
        assert row == ("b1",)
        assert voice == ("v1",)

    def test_parity_migration_idempotent(self, tmp_path: Path) -> None:
        """Running create_schema twice on the migrated DB adds tables/indexes once."""
        db_path = self._old_schema_db(tmp_path, "idem.db")

        conn = sqlite3.connect(str(db_path))
        create_schema(conn)
        conn.close()

        # Second run — no duplicate tables or indexes.
        conn = sqlite3.connect(str(db_path))
        create_schema(conn)
        dup_tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name NOT LIKE 'sqlite_%' GROUP BY name HAVING COUNT(*) > 1"
        ).fetchall()
        dup_indexes = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
            " AND name NOT LIKE 'sqlite_%' GROUP BY name HAVING COUNT(*) > 1"
        ).fetchall()
        conn.close()
        assert dup_tables == []
        assert dup_indexes == []

    def test_new_schema_create_schema_runs_twice_clean(self, tmp_path: Path) -> None:
        """Fresh full schema (incl. parity) tolerates repeated create_schema."""
        db_path = tmp_path / "fresh.db"
        conn = sqlite3.connect(str(db_path))
        create_schema(conn)
        conn.close()

        conn = sqlite3.connect(str(db_path))
        create_schema(conn)
        tables = self._parity_table_names(conn)
        conn.close()
        assert self.PARITY_TABLES <= tables
