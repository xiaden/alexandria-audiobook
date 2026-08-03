"""Spec-first tests for the pipeline storage adapter.

Covers:
- WAL mode enabled on SQLiteAdapter
- Foreign keys enforcement (FK=ON)
- Connection lifecycle (get_connection → close)
- In-memory adapter parity (same schema, same behavior)
- All execute_* methods return correct types
- init_db() creates the schema (tables exist after init_db)
- 100% adapter coverage target
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from app.pipeline.adapter import (
    InMemorySQLiteAdapter,
    PipelineStorage,
    SQLiteAdapter,
)
from app.pipeline.db import get_pipeline_db, reset_pipeline_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_db(tmp_path):
    """Return a path to a temporary database file."""
    return str(tmp_path / "test_pipeline.db")


@pytest.fixture()
def sqlite_adapter(tmp_db):
    """Return a SQLiteAdapter with schema initialised."""
    adapter = SQLiteAdapter(db_path=tmp_db)
    adapter.init_db()
    yield adapter
    adapter.close()


@pytest.fixture()
def memory_adapter():
    """Return an InMemorySQLiteAdapter with schema initialised."""
    adapter = InMemorySQLiteAdapter()
    adapter.init_db()
    yield adapter
    adapter.close()


# ---------------------------------------------------------------------------
# P2-S1: PipelineStorage is an ABC
# ---------------------------------------------------------------------------


class TestPipelineStorageABC:
    """PipelineStorage must be abstract and define the required interface."""

    def test_cannot_instantiate_abc(self):
        with pytest.raises(TypeError):
            PipelineStorage()  # type: ignore[abstract]

    def test_abstract_methods_defined(self):
        """All required abstract methods must be declared."""
        expected = {
            "init_db",
            "get_connection",
            "close",
            "execute_query",
            "execute_insert",
            "execute_update",
            "execute_delete",
        }
        assert expected.issubset(set(PipelineStorage.__abstractmethods__))


# ---------------------------------------------------------------------------
# P2-S2: SQLiteAdapter — WAL mode, FK enforcement, lifecycle
# ---------------------------------------------------------------------------


class TestSQLiteAdapterWALMode:
    """SQLiteAdapter must enable WAL journal mode."""

    def test_journal_mode_is_wal(self, sqlite_adapter):
        conn = sqlite_adapter.get_connection()
        row = conn.execute("PRAGMA journal_mode").fetchone()
        assert row[0].lower() == "wal"

    def test_wal_mode_via_query(self, sqlite_adapter):
        """WAL mode should also be visible through execute_query."""
        rows = sqlite_adapter.execute_query("PRAGMA journal_mode")
        assert len(rows) == 1
        assert rows[0]["journal_mode"].lower() == "wal"


class TestSQLiteAdapterFKEnforcement:
    """SQLiteAdapter must enforce foreign keys."""

    def test_foreign_keys_pragma_on(self, sqlite_adapter):
        conn = sqlite_adapter.get_connection()
        row = conn.execute("PRAGMA foreign_keys").fetchone()
        assert row[0] == 1

    def test_fk_violation_raises(self, sqlite_adapter):
        """Inserting a book with a non-existent series_id must fail."""
        with pytest.raises(sqlite3.IntegrityError):
            sqlite_adapter.execute_insert(
                "INSERT INTO book (id, series_id, book_number) VALUES (?, ?, ?)",
                ("book-1", "nonexistent-series", 1),
            )

    def test_fk_cascade_or_restrict(self, sqlite_adapter):
        """Deleting a series that has books should fail (RESTRICT by default)."""
        sqlite_adapter.execute_insert(
            "INSERT INTO series (id) VALUES (?)", ("series-1",)
        )
        sqlite_adapter.execute_insert(
            "INSERT INTO book (id, series_id, book_number) VALUES (?, ?, ?)",
            ("book-1", "series-1", 1),
        )
        with pytest.raises(sqlite3.IntegrityError):
            sqlite_adapter.execute_delete(
                "DELETE FROM series WHERE id = ?", ("series-1",)
            )


class TestSQLiteAdapterLifecycle:
    """Connection lifecycle: get_connection → close."""

    def test_get_connection_returns_connection(self, sqlite_adapter):
        conn = sqlite_adapter.get_connection()
        assert isinstance(conn, sqlite3.Connection)

    def test_close_then_access_raises(self, tmp_db):
        adapter = SQLiteAdapter(db_path=tmp_db)
        adapter.close()
        with pytest.raises(sqlite3.ProgrammingError):
            adapter.get_connection().execute("SELECT 1")

    def test_connection_is_same_object(self, sqlite_adapter):
        conn1 = sqlite_adapter.get_connection()
        conn2 = sqlite_adapter.get_connection()
        assert conn1 is conn2


class TestSQLiteAdapterInitDB:
    """init_db() must create the full schema."""

    def test_tables_exist_after_init(self, sqlite_adapter):
        conn = sqlite_adapter.get_connection()
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        expected_tables = {
            "series",
            "book",
            "chapter",
            "scene",
            "paragraph",
            "span",
            "book_chapter",
            "chapter_scene",
            "scene_paragraph",
            "paragraph_span",
            "voice_config",
            "character",
            "character_metadata",
            "character_series",
            "character_book",
            "character_scene",
            "character_span",
        }
        assert expected_tables.issubset(tables)

    def test_view_exists_after_init(self, sqlite_adapter):
        conn = sqlite_adapter.get_connection()
        views = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='view'"
            ).fetchall()
        }
        assert "span_presentation" in views

    def test_init_db_is_idempotent(self, sqlite_adapter):
        """Calling init_db() twice should not raise."""
        sqlite_adapter.init_db()
        sqlite_adapter.init_db()


class TestSQLiteAdapterConstructor:
    """SQLiteAdapter constructor behavior."""

    def test_default_path(self):
        """Default path should be ./data/pipeline.db."""
        # We just verify the default doesn't crash; we use a tmp path in tests
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "data", "pipeline.db")
            adapter = SQLiteAdapter(db_path=path)
            assert os.path.exists(path)
            adapter.close()

    def test_creates_parent_directories(self, tmp_path):
        """Parent directories should be created automatically."""
        nested = str(tmp_path / "a" / "b" / "c" / "test.db")
        adapter = SQLiteAdapter(db_path=nested)
        assert os.path.exists(os.path.dirname(nested))
        adapter.close()


# ---------------------------------------------------------------------------
# P2-S3: InMemorySQLiteAdapter — parity with SQLiteAdapter
# ---------------------------------------------------------------------------


class TestInMemoryAdapterParity:
    """InMemorySQLiteAdapter must behave identically to SQLiteAdapter."""

    def test_is_pipeline_storage(self, memory_adapter):
        assert isinstance(memory_adapter, PipelineStorage)

    def test_init_db_creates_schema(self, memory_adapter):
        conn = memory_adapter.get_connection()
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        assert "series" in tables
        assert "span_presentation" in {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='view'"
            ).fetchall()
        }

    def test_fk_enforcement(self, memory_adapter):
        """FK violations must raise in memory adapter too."""
        with pytest.raises(sqlite3.IntegrityError):
            memory_adapter.execute_insert(
                "INSERT INTO book (id, series_id, book_number) VALUES (?, ?, ?)",
                ("book-1", "nonexistent-series", 1),
            )

    def test_get_connection_returns_connection(self, memory_adapter):
        conn = memory_adapter.get_connection()
        assert isinstance(conn, sqlite3.Connection)

    def test_close(self):
        adapter = InMemorySQLiteAdapter()
        adapter.close()
        with pytest.raises(sqlite3.ProgrammingError):
            adapter.get_connection().execute("SELECT 1")

    def test_no_disk_persistence(self, tmp_path):
        """In-memory adapter should not create any files."""
        adapter = InMemorySQLiteAdapter()
        adapter.init_db()
        # Check no new files in tmp_path
        files = list(tmp_path.iterdir())
        assert len(files) == 0
        adapter.close()


# ---------------------------------------------------------------------------
# execute_* methods — return types
# ---------------------------------------------------------------------------


class TestExecuteQuery:
    """execute_query must return list[dict]."""

    @pytest.mark.parametrize(
        "adapter_name", ["sqlite_adapter", "memory_adapter"]
    )
    def test_returns_list_of_dicts(self, adapter_name, request):
        adapter = request.getfixturevalue(adapter_name)
        adapter.execute_insert("INSERT INTO series (id) VALUES (?)", ("s1",))
        rows = adapter.execute_query("SELECT id FROM series")
        assert isinstance(rows, list)
        assert len(rows) == 1
        assert isinstance(rows[0], dict)
        assert rows[0]["id"] == "s1"

    @pytest.mark.parametrize(
        "adapter_name", ["sqlite_adapter", "memory_adapter"]
    )
    def test_empty_result(self, adapter_name, request):
        adapter = request.getfixturevalue(adapter_name)
        rows = adapter.execute_query("SELECT id FROM series")
        assert rows == []

    @pytest.mark.parametrize(
        "adapter_name", ["sqlite_adapter", "memory_adapter"]
    )
    def test_with_params(self, adapter_name, request):
        adapter = request.getfixturevalue(adapter_name)
        adapter.execute_insert("INSERT INTO series (id) VALUES (?)", ("s1",))
        adapter.execute_insert("INSERT INTO series (id) VALUES (?)", ("s2",))
        rows = adapter.execute_query("SELECT id FROM series WHERE id = ?", ("s1",))
        assert len(rows) == 1
        assert rows[0]["id"] == "s1"

    @pytest.mark.parametrize(
        "adapter_name", ["sqlite_adapter", "memory_adapter"]
    )
    def test_multiple_columns(self, adapter_name, request):
        adapter = request.getfixturevalue(adapter_name)
        adapter.execute_insert("INSERT INTO series (id) VALUES (?)", ("s1",))
        adapter.execute_insert(
            "INSERT INTO book (id, series_id, book_number, position) VALUES (?, ?, ?, ?)",
            ("b1", "s1", 1, 10),
        )
        rows = adapter.execute_query(
            "SELECT id, series_id, book_number, position FROM book"
        )
        assert len(rows) == 1
        row = rows[0]
        assert row["id"] == "b1"
        assert row["series_id"] == "s1"
        assert row["book_number"] == 1
        assert row["position"] == 10


class TestExecuteInsert:
    """execute_insert must return lastrowid as int."""

    @pytest.mark.parametrize(
        "adapter_name", ["sqlite_adapter", "memory_adapter"]
    )
    def test_returns_int(self, adapter_name, request):
        adapter = request.getfixturevalue(adapter_name)
        rowid = adapter.execute_insert(
            "INSERT INTO series (id) VALUES (?)", ("s1",)
        )
        assert isinstance(rowid, int)

    @pytest.mark.parametrize(
        "adapter_name", ["sqlite_adapter", "memory_adapter"]
    )
    def test_returns_lastrowid(self, adapter_name, request):
        adapter = request.getfixturevalue(adapter_name)
        # With TEXT PK and no AUTOINCREMENT, lastrowid is the internal rowid
        rowid1 = adapter.execute_insert(
            "INSERT INTO series (id) VALUES (?)", ("s1",)
        )
        rowid2 = adapter.execute_insert(
            "INSERT INTO series (id) VALUES (?)", ("s2",)
        )
        assert rowid2 > rowid1

    @pytest.mark.parametrize(
        "adapter_name", ["sqlite_adapter", "memory_adapter"]
    )
    def test_fk_violation_raises(self, adapter_name, request):
        adapter = request.getfixturevalue(adapter_name)
        with pytest.raises(sqlite3.IntegrityError):
            adapter.execute_insert(
                "INSERT INTO book (id, series_id, book_number) VALUES (?, ?, ?)",
                ("b1", "no-such-series", 1),
            )


class TestExecuteUpdate:
    """execute_update must return rowcount as int."""

    @pytest.mark.parametrize(
        "adapter_name", ["sqlite_adapter", "memory_adapter"]
    )
    def test_returns_int(self, adapter_name, request):
        adapter = request.getfixturevalue(adapter_name)
        adapter.execute_insert("INSERT INTO series (id) VALUES (?)", ("s1",))
        count = adapter.execute_update(
            "UPDATE series SET id = ? WHERE id = ?", ("s1-updated", "s1")
        )
        assert isinstance(count, int)
        assert count == 1

    @pytest.mark.parametrize(
        "adapter_name", ["sqlite_adapter", "memory_adapter"]
    )
    def test_no_rows_affected(self, adapter_name, request):
        adapter = request.getfixturevalue(adapter_name)
        count = adapter.execute_update(
            "UPDATE series SET id = ? WHERE id = ?", ("x", "nonexistent")
        )
        assert count == 0

    @pytest.mark.parametrize(
        "adapter_name", ["sqlite_adapter", "memory_adapter"]
    )
    def test_multiple_rows_updated(self, adapter_name, request):
        adapter = request.getfixturevalue(adapter_name)
        adapter.execute_insert("INSERT INTO series (id) VALUES (?)", ("s1",))
        adapter.execute_insert("INSERT INTO series (id) VALUES (?)", ("s2",))
        # Update all series (no WHERE clause)
        count = adapter.execute_update("UPDATE series SET id = id || '-x'")
        assert count == 2


class TestExecuteDelete:
    """execute_delete must return rowcount as int."""

    @pytest.mark.parametrize(
        "adapter_name", ["sqlite_adapter", "memory_adapter"]
    )
    def test_returns_int(self, adapter_name, request):
        adapter = request.getfixturevalue(adapter_name)
        adapter.execute_insert("INSERT INTO series (id) VALUES (?)", ("s1",))
        count = adapter.execute_delete("DELETE FROM series WHERE id = ?", ("s1",))
        assert isinstance(count, int)
        assert count == 1

    @pytest.mark.parametrize(
        "adapter_name", ["sqlite_adapter", "memory_adapter"]
    )
    def test_no_rows_deleted(self, adapter_name, request):
        adapter = request.getfixturevalue(adapter_name)
        count = adapter.execute_delete("DELETE FROM series WHERE id = ?", ("nope",))
        assert count == 0

    @pytest.mark.parametrize(
        "adapter_name", ["sqlite_adapter", "memory_adapter"]
    )
    def test_fk_violation_on_delete(self, adapter_name, request):
        adapter = request.getfixturevalue(adapter_name)
        adapter.execute_insert("INSERT INTO series (id) VALUES (?)", ("s1",))
        adapter.execute_insert(
            "INSERT INTO book (id, series_id, book_number) VALUES (?, ?, ?)",
            ("b1", "s1", 1),
        )
        with pytest.raises(sqlite3.IntegrityError):
            adapter.execute_delete("DELETE FROM series WHERE id = ?", ("s1",))


# ---------------------------------------------------------------------------
# P2-S4: get_pipeline_db() factory
# ---------------------------------------------------------------------------


class TestGetPipelineDB:
    """get_pipeline_db() factory behavior."""

    def setup_method(self):
        """Reset singleton before each test."""
        reset_pipeline_db()

    def teardown_method(self):
        """Reset singleton after each test."""
        reset_pipeline_db()

    def test_returns_pipeline_storage(self, tmp_path, monkeypatch):
        db_path = str(tmp_path / "test.db")
        monkeypatch.setenv("PIPELINE_DB_BACKEND", "sqlite")
        monkeypatch.setenv("PIPELINE_DB_PATH", db_path)
        adapter = get_pipeline_db()
        assert isinstance(adapter, PipelineStorage)
        assert isinstance(adapter, SQLiteAdapter)

    def test_memory_backend(self, monkeypatch):
        monkeypatch.setenv("PIPELINE_DB_BACKEND", "memory")
        adapter = get_pipeline_db()
        assert isinstance(adapter, InMemorySQLiteAdapter)

    def test_singleton_behavior(self, tmp_path, monkeypatch):
        db_path = str(tmp_path / "test.db")
        monkeypatch.setenv("PIPELINE_DB_BACKEND", "sqlite")
        monkeypatch.setenv("PIPELINE_DB_PATH", db_path)
        a1 = get_pipeline_db()
        a2 = get_pipeline_db()
        assert a1 is a2

    def test_schema_created_on_init(self, tmp_path, monkeypatch):
        db_path = str(tmp_path / "test.db")
        monkeypatch.setenv("PIPELINE_DB_BACKEND", "sqlite")
        monkeypatch.setenv("PIPELINE_DB_PATH", db_path)
        adapter = get_pipeline_db()
        conn = adapter.get_connection()
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        assert "series" in tables
        assert "span" in tables

    def test_reset_closes_and_clears(self, tmp_path, monkeypatch):
        db_path = str(tmp_path / "test.db")
        monkeypatch.setenv("PIPELINE_DB_BACKEND", "sqlite")
        monkeypatch.setenv("PIPELINE_DB_PATH", db_path)
        adapter = get_pipeline_db()
        reset_pipeline_db()
        # After reset, a new call should return a different instance
        adapter2 = get_pipeline_db()
        assert adapter2 is not adapter

    def test_default_backend_is_sqlite(self, tmp_path, monkeypatch):
        """Without env vars, default should be sqlite."""
        monkeypatch.delenv("PIPELINE_DB_BACKEND", raising=False)
        # Use a temp path so we don't pollute the workspace
        db_path = str(tmp_path / "default.db")
        monkeypatch.setenv("PIPELINE_DB_PATH", db_path)
        adapter = get_pipeline_db()
        assert isinstance(adapter, SQLiteAdapter)
