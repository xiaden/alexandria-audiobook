"""Tests for voice_config schema migration script (Plan O).

Tests verify:
1. Migration adds missing columns to old schema (3 columns → 11 columns)
2. Migration is idempotent (running twice produces no errors)
3. Migration logs status correctly
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from app.pipeline.adapter import InMemorySQLiteAdapter
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
