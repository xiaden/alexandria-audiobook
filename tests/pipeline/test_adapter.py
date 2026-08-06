"""Spec-first tests for the pipeline storage adapter.

Covers:
- WAL mode enabled on SQLiteAdapter
- Foreign keys enforcement (FK=ON)
- Connection lifecycle (get_connection → close)
- In-memory adapter parity (same schema, same behavior)
- All execute_* methods return correct types
- init_db() creates the schema (tables exist after init_db)
- 100% adapter coverage target
- Startup reconciliation (reconcile_stale_runs): stale running rows
  (render_job/walk_run) flip to interrupted in ONE pass; terminal rows and
  fresh rows untouched; idempotent; file-backed crash-recovery scenario
- Production storage bootstrap invokes reconcile exactly once at startup
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import time

import pytest
from unittest.mock import MagicMock

from app.pipeline.adapter import (
    ConcurrentTransactionError,
    InMemorySQLiteAdapter,
    PipelineStorage,
    SQLiteAdapter,
)


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
# Persistence — writes survive across connections
# ---------------------------------------------------------------------------


class TestSQLiteAdapterPersistence:
    """SQLiteAdapter writes must be durable across connections."""

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _init_and_populate(adapter: SQLiteAdapter) -> None:
        """Create a series row that tests can read back."""
        adapter.init_db()
        adapter.execute_insert(
            "INSERT INTO series (id) VALUES (?)", ("series-persist",)
        )

    @staticmethod
    def _open_connection(db_path: str) -> sqlite3.Connection:
        """Open a fresh read-only connection to verify persisted state."""
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    # -- insert persistence -------------------------------------------------

    def test_insert_survives_new_connection(self, tmp_path):
        """A row inserted through the adapter must be visible from a
        second, independent connection."""
        db_path = str(tmp_path / "persist.db")

        adapter = SQLiteAdapter(db_path=db_path)
        self._init_and_populate(adapter)
        adapter.close()

        # Open a fresh connection — inserted row must be durable.
        conn = self._open_connection(db_path)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT id FROM series").fetchall()
            assert len(rows) == 1
            assert rows[0]["id"] == "series-persist"
        finally:
            conn.close()
    def test_insert_survives_using_adapter(self, tmp_path):
        """Same as above but reads back through a second SQLiteAdapter
        instance (not just a raw connection)."""
        db_path = str(tmp_path / "persist2.db")

        adapter1 = SQLiteAdapter(db_path=db_path)
        self._init_and_populate(adapter1)
        adapter1.close()

        # Second adapter on the same file — should see the committed row.
        adapter2 = SQLiteAdapter(db_path=db_path)
        try:
            adapter2.init_db()
            rows = adapter2.execute_query("SELECT id FROM series")
            assert len(rows) == 1
            assert rows[0]["id"] == "series-persist"
        finally:
            adapter2.close()

    # -- update persistence -------------------------------------------------

    def test_update_survives_new_connection(self, tmp_path):
        """An update committed through the adapter must be visible from
        a second connection."""
        db_path = str(tmp_path / "update_persist.db")

        adapter = SQLiteAdapter(db_path=db_path)
        self._init_and_populate(adapter)
        adapter.execute_update(
            "UPDATE series SET id = ? WHERE id = ?",
            ("series-updated", "series-persist"),
        )
        adapter.close()

        conn = self._open_connection(db_path)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT id FROM series").fetchall()
            assert len(rows) == 1
            assert rows[0]["id"] == "series-updated"
            # The old value must be gone.
            old = conn.execute(
                "SELECT id FROM series WHERE id = ?", ("series-persist",)
            ).fetchall()
            assert len(old) == 0
        finally:
            conn.close()

    # -- delete persistence -------------------------------------------------

    def test_delete_survives_new_connection(self, tmp_path):
        """A delete committed through the adapter must be visible from
        a second connection."""
        db_path = str(tmp_path / "delete_persist.db")

        adapter = SQLiteAdapter(db_path=db_path)
        self._init_and_populate(adapter)
        count = adapter.execute_delete(
            "DELETE FROM series WHERE id = ?", ("series-persist",)
        )
        assert count == 1
        adapter.close()

        conn = self._open_connection(db_path)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT id FROM series").fetchall()
            assert len(rows) == 0
        finally:
            conn.close()

    # -- atomicity: failure must not commit partial data --------------------

    def test_fk_violation_does_not_commit(self, tmp_path):
        """When an INSERT violates a foreign key, the transaction must
        NOT be committed — no partial data should persist."""
        db_path = str(tmp_path / "fk_atomic.db")

        adapter = SQLiteAdapter(db_path=db_path)
        adapter.init_db()

        # First insert some valid data.
        adapter.execute_insert(
            "INSERT INTO series (id) VALUES (?)", ("series-ok",)
        )

        # Now attempt an FK-violating insert.
        with pytest.raises(sqlite3.IntegrityError):
            adapter.execute_insert(
                "INSERT INTO book (id, series_id, book_number) VALUES (?, ?, ?)",
                ("book-bad", "no-such-series", 1),
            )

        adapter.close()

        # Only the valid data should survive — no book row.
        conn = self._open_connection(db_path)
        try:
            conn.row_factory = sqlite3.Row
            series_rows = conn.execute("SELECT id FROM series").fetchall()
            assert len(series_rows) == 1
            assert series_rows[0]["id"] == "series-ok"

            book_rows = conn.execute("SELECT id FROM book").fetchall()
            assert len(book_rows) == 0
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# P1-S1: transaction() context manager — concurrency-safe BEGIN IMMEDIATE
# ---------------------------------------------------------------------------

_ADAPTER_FIXTURES = ["sqlite_adapter", "memory_adapter"]


class TestConcurrentTransactionError:
    """ConcurrentTransactionError must be a RuntimeError subclass."""

    def test_is_runtime_error_subclass(self):
        assert issubclass(ConcurrentTransactionError, RuntimeError)


class TestTransactionContextManager:
    """SQLiteAdapter.transaction() must provide guarded, atomic transactions."""

    # -- (a) begins a transaction -------------------------------------------

    @pytest.mark.parametrize("adapter_name", _ADAPTER_FIXTURES)
    def test_begins_a_transaction(self, adapter_name, request):
        adapter = request.getfixturevalue(adapter_name)
        with adapter.transaction():
            assert adapter.get_connection().in_transaction is True

    # -- (b) commits on clean exit ------------------------------------------

    @pytest.mark.parametrize("adapter_name", _ADAPTER_FIXTURES)
    def test_commits_on_clean_exit(self, adapter_name, request):
        adapter = request.getfixturevalue(adapter_name)
        with adapter.transaction():
            adapter.execute_insert("INSERT INTO series (id) VALUES (?)", ("s1",))
        # No transaction may be left open after the block exits.
        assert adapter.get_connection().in_transaction is False
        rows = adapter.execute_query("SELECT id FROM series")
        assert [r["id"] for r in rows] == ["s1"]

    # -- (c) rolls back on exception ----------------------------------------

    @pytest.mark.parametrize("adapter_name", _ADAPTER_FIXTURES)
    def test_rolls_back_on_exception(self, adapter_name, request):
        adapter = request.getfixturevalue(adapter_name)
        with pytest.raises(RuntimeError, match="boom"):
            with adapter.transaction():
                adapter.execute_insert("INSERT INTO series (id) VALUES (?)", ("s1",))
                raise RuntimeError("boom")
        # Rollback must close the transaction and discard the write.
        assert adapter.get_connection().in_transaction is False
        rows = adapter.execute_query("SELECT id FROM series")
        assert rows == []

    # -- (d) cross-thread write raises ConcurrentTransactionError ------------

    @pytest.mark.parametrize("adapter_name", _ADAPTER_FIXTURES)
    def test_cross_thread_write_raises_concurrent_transaction_error(
        self, adapter_name, request
    ):
        adapter = request.getfixturevalue(adapter_name)
        captured: dict[str, ConcurrentTransactionError] = {}

        def write_from_other_thread() -> None:
            try:
                adapter.execute_insert(
                    "INSERT INTO series (id) VALUES (?)", ("s2",)
                )
            except ConcurrentTransactionError as exc:
                captured["error"] = exc

        with adapter.transaction():
            thread = threading.Thread(target=write_from_other_thread)
            thread.start()
            thread.join()
            assert isinstance(captured.get("error"), ConcurrentTransactionError)
            # The owner thread can still write while the guard is armed.
            adapter.execute_insert("INSERT INTO series (id) VALUES (?)", ("s1",))
        # The rejected write must not appear; the owner's write commits.
        rows = adapter.execute_query("SELECT id FROM series")
        assert {r["id"] for r in rows} == {"s1"}

    # -- (e) nested re-entry joins the outer transaction ---------------------

    @pytest.mark.parametrize("adapter_name", _ADAPTER_FIXTURES)
    def test_nested_reentry_joins_outer_transaction(self, adapter_name, request):
        adapter = request.getfixturevalue(adapter_name)
        with adapter.transaction():
            adapter.execute_insert("INSERT INTO series (id) VALUES (?)", ("s1",))
            with adapter.transaction():
                adapter.execute_insert("INSERT INTO series (id) VALUES (?)", ("s2",))
                # Inner exit must NOT commit — still inside a transaction.
                assert adapter.get_connection().in_transaction is True
            # Still inside the outer transaction after the inner block.
            assert adapter.get_connection().in_transaction is True
        # The outer exit commits both writes atomically.
        assert adapter.get_connection().in_transaction is False
        rows = adapter.execute_query("SELECT id FROM series")
        assert {r["id"] for r in rows} == {"s1", "s2"}

    # -- exception in a nested block rolls back the whole outer transaction --

    @pytest.mark.parametrize("adapter_name", _ADAPTER_FIXTURES)
    def test_exception_in_nested_block_rolls_back_outer(self, adapter_name, request):
        adapter = request.getfixturevalue(adapter_name)
        with pytest.raises(RuntimeError, match="inner"):
            with adapter.transaction():
                adapter.execute_insert("INSERT INTO series (id) VALUES (?)", ("s1",))
                with adapter.transaction():
                    raise RuntimeError("inner")
        # The uncaught inner exception must roll back the entire transaction.
        assert adapter.get_connection().in_transaction is False
        rows = adapter.execute_query("SELECT id FROM series")
        assert rows == []


class TestIsolationLevelAutocommit:
    """Both adapters must run in explicit autocommit mode (isolation_level=None)."""

    @pytest.mark.parametrize("adapter_name", _ADAPTER_FIXTURES)
    def test_isolation_level_is_none(self, adapter_name, request):
        adapter = request.getfixturevalue(adapter_name)
        assert adapter.get_connection().isolation_level is None


# ---------------------------------------------------------------------------
# P2-S1: busy_timeout — init_db() must arm the busy handler at startup
# ---------------------------------------------------------------------------


class TestBusyTimeout:
    """init_db() must set PRAGMA busy_timeout=5000 so a second writer blocks
    up to the timeout instead of failing with "database is locked" instantly.

    The blocking test is file-backed only: :memory: databases are per
    connection and can never contend on a shared lock.
    """

    @pytest.mark.parametrize("adapter_name", _ADAPTER_FIXTURES)
    def test_init_db_sets_busy_timeout_5000(self, adapter_name, request):
        """After init_db(), the connection must report busy_timeout=5000.

        Not a tautology: sqlite3's driver default already reports 5000, so
        zero the timeout first and require init_db() to re-arm it.  If the
        PRAGMA line in init_db() is removed, the readback stays 0 and this
        fails.
        """
        adapter = request.getfixturevalue(adapter_name)
        conn = adapter.get_connection()
        # Precondition: override the driver default (5000) so the readback
        # can only reach 5000 again if init_db() re-arms the busy handler.
        conn.execute("PRAGMA busy_timeout = 0")
        row = conn.execute("PRAGMA busy_timeout").fetchone()
        assert row[0] == 0
        # init_db() is idempotent (create_schema uses executescript).
        adapter.init_db()
        row = conn.execute("PRAGMA busy_timeout").fetchone()
        assert row[0] == 5000

    def test_second_connection_blocks_until_write_lock_released(self, tmp_path):
        """While the adapter holds the write lock, a second connection's
        BEGIN IMMEDIATE must wait (busy_timeout) rather than raising
        'database is locked' immediately."""
        db_path = str(tmp_path / "busy_timeout.db")
        adapter = SQLiteAdapter(db_path=db_path)
        adapter.init_db()
        result: dict = {}
        contending = threading.Event()

        def acquire_from_second_connection() -> None:
            # Note: this connection relies on the sqlite3 driver default
            # busy timeout; the assertion below only validates SQLite
            # contention behavior, NOT that init_db() armed the timeout
            # (test_init_db_sets_busy_timeout_5000 is the contract test).
            conn = sqlite3.connect(db_path, isolation_level=None)
            try:
                contending.set()
                start = time.monotonic()
                conn.execute("BEGIN IMMEDIATE")
                result["elapsed"] = time.monotonic() - start
                result["ok"] = True
            except sqlite3.OperationalError as exc:
                result["ok"] = False
                result["error"] = exc
            finally:
                conn.close()

        try:
            with adapter.transaction():
                adapter.execute_insert(
                    "INSERT INTO series (id) VALUES (?)", ("s1",)
                )
                thread = threading.Thread(target=acquire_from_second_connection)
                thread.start()
                # The second connection is now contending on the write lock;
                # give it time to actually hit the busy handler.
                assert contending.wait(timeout=5)
                time.sleep(0.3)
            # Exiting the with-block commits and releases the write lock.
            thread.join(timeout=10)
            # The second connection waited for the lock instead of failing.
            assert result.get("ok") is True, result
            assert result["elapsed"] >= 0.25
        finally:
            adapter.close()


# ---------------------------------------------------------------------------
# QA Round 1: BEGIN-failure guard state — stale-owner fix + contract
# translation (contended BEGIN IMMEDIATE raises ConcurrentTransactionError)
# ---------------------------------------------------------------------------


class _FailingBeginConnection:
    """Proxy around a real ``sqlite3.Connection`` whose ``BEGIN IMMEDIATE``
    fails.  Every other call delegates to the wrapped connection.

    ``sqlite3.Connection.execute`` is a read-only attribute, so the failure
    has to be injected through a wrapper instead of a monkeypatch.
    """

    def __init__(
        self, real: sqlite3.Connection, error: Exception
    ) -> None:
        self._real = real
        self._error = error

    def execute(self, sql, *args, **kwargs):
        if isinstance(sql, str) and sql.strip().upper() == "BEGIN IMMEDIATE":
            raise self._error
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


class TestTransactionBeginFailure:
    """A ``BEGIN IMMEDIATE`` that fails must not corrupt the guard.

    Round-1 QA finding: if BEGIN raised, ``_txn_owner`` stayed set while
    ``_txn_depth`` remained 0, so every other thread's write spuriously
    raised ``ConcurrentTransactionError`` until the owner ran another
    ``transaction()``.  Also, a contended BEGIN must surface as
    ``ConcurrentTransactionError`` (the registered contract) rather than a
    bare ``sqlite3.OperationalError``.
    """

    @staticmethod
    def _arm_failing_begin(adapter, error: Exception, monkeypatch) -> None:
        monkeypatch.setattr(
            adapter,
            "_conn",
            _FailingBeginConnection(adapter.get_connection(), error),
        )

    def test_contended_begin_raises_concurrent_transaction_error(self, tmp_path):
        """Real contention: while a second connection holds the write lock,
        ``transaction()`` must raise ``ConcurrentTransactionError`` and leave
        the adapter fully usable afterwards."""
        db_path = str(tmp_path / "begin_timeout.db")
        adapter = SQLiteAdapter(db_path=db_path)
        try:
            adapter.init_db()
            # Shorten the busy timeout so the contended BEGIN fails fast
            # instead of waiting out the full 5s startup value.
            adapter.get_connection().execute("PRAGMA busy_timeout = 200")
            holder = sqlite3.connect(db_path, isolation_level=None)
            holder.execute("BEGIN IMMEDIATE")
            try:
                with pytest.raises(
                    ConcurrentTransactionError, match="BEGIN IMMEDIATE timed out"
                ):
                    with adapter.transaction():
                        pass
                # No transaction may be left half-open.
                assert adapter.get_connection().in_transaction is False
            finally:
                holder.rollback()
                holder.close()
            # The adapter must be usable again (guard state reset).
            with adapter.transaction():
                adapter.execute_insert(
                    "INSERT INTO series (id) VALUES (?)", ("s1",)
                )
            rows = adapter.execute_query("SELECT id FROM series")
            assert [r["id"] for r in rows] == ["s1"]
        finally:
            adapter.close()

    @pytest.mark.parametrize("adapter_name", _ADAPTER_FIXTURES)
    def test_begin_failure_resets_owner_for_other_threads(
        self, adapter_name, request, monkeypatch
    ):
        """After a failed BEGIN, the guard must be disarmed: a write from a
        different thread must NOT raise ``ConcurrentTransactionError``."""
        adapter = request.getfixturevalue(adapter_name)
        self._arm_failing_begin(
            adapter, sqlite3.OperationalError("database is locked"), monkeypatch
        )

        with pytest.raises(
            ConcurrentTransactionError, match="BEGIN IMMEDIATE timed out"
        ):
            with adapter.transaction():
                pass

        captured: dict[str, bool] = {}

        def write_from_other_thread() -> None:
            try:
                adapter.execute_insert(
                    "INSERT INTO series (id) VALUES (?)", ("s2",)
                )
                captured["ok"] = True
            except ConcurrentTransactionError:
                captured["ok"] = False

        thread = threading.Thread(target=write_from_other_thread)
        thread.start()
        thread.join()
        assert captured.get("ok") is True

    @pytest.mark.parametrize("adapter_name", _ADAPTER_FIXTURES)
    def test_non_operational_begin_failure_re_raises_and_resets_owner(
        self, adapter_name, request, monkeypatch
    ):
        """A non-OperationalError BEGIN failure propagates unchanged, but the
        owner guard is still cleared."""
        adapter = request.getfixturevalue(adapter_name)
        self._arm_failing_begin(
            adapter, RuntimeError("begin exploded"), monkeypatch
        )

        with pytest.raises(RuntimeError, match="begin exploded"):
            with adapter.transaction():
                pass

        # Guard disarmed: a write from another thread must not be rejected.
        captured: dict[str, bool] = {}

        def write_from_other_thread() -> None:
            try:
                adapter.execute_insert(
                    "INSERT INTO series (id) VALUES (?)", ("s2",)
                )
                captured["ok"] = True
            except ConcurrentTransactionError:
                captured["ok"] = False

        thread = threading.Thread(target=write_from_other_thread)
        thread.start()
        thread.join()
        assert captured.get("ok") is True

# ---------------------------------------------------------------------------
# P4-S1: reconcile_stale_runs — startup-only stale running → interrupted
# ---------------------------------------------------------------------------

# 1 hour in the past — comfortably older than the reconcile cutoff, so a row
# timestamped this far back is definitely "started before the cutoff".
_STALE_AGE_MS = 3_600_000


def _insert_render_job(
    adapter,
    job_id: str,
    status: str,
    started_ms: int,
    created_ms: int | None = None,
) -> None:
    """Insert a render_job row with explicit timestamps.

    render_job has NO heartbeat column (schema registered in CONTRACTS.md §
    Universal Upgrade), so ``started_ms`` is the only liveness signal.
    """
    adapter.execute_insert(
        "INSERT INTO render_job "
        "(job_id, book_id, mode, status, output_dir, created_ms, started_ms) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            job_id,
            "b1",
            "batch",
            status,
            "/tmp/out",
            created_ms if created_ms is not None else started_ms,
            started_ms,
        ),
    )


def _insert_walk_run(
    adapter,
    run_id: str,
    status: str,
    created_ms: int,
    heartbeat_ms: int | None = None,
) -> None:
    """Insert a walk_run row with explicit timestamps (created_ms + optional
    heartbeat_ms — walk_run DOES carry a heartbeat column)."""
    adapter.execute_insert(
        "INSERT INTO walk_run "
        "(run_id, book_id, walk_name, status, created_ms, heartbeat_ms) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, "b1", "walk_2a", status, created_ms, heartbeat_ms),
    )


class TestReconcileStaleRuns:
    """``reconcile_stale_runs()`` must flip stale ``running`` rows to
    ``interrupted`` in ONE pass, and only stale ones.

    Crash-recovery scenario is FILE-BACKED only: ``:memory:`` conceals crash
    recovery (the crash scenario is: process dies mid-run with
    status='running', rows persist on disk, the NEXT process startup
    reconciles them). Per the DD test strategy, the file-backed fixture
    writes with one adapter instance, closes it (process death), reopens a
    NEW adapter on the same file, then reconciles.
    """

    @staticmethod
    def _reopen(db_path: str) -> SQLiteAdapter:
        """Simulate a fresh process start on an existing database file."""
        adapter = SQLiteAdapter(db_path=db_path)
        adapter.init_db()
        return adapter

    def test_crash_recovery_flips_stale_running_rows_on_reopen(self, tmp_path):
        """A process dying mid-run leaves running rows on disk; the next
        startup's single reconcile pass flips them to interrupted."""
        db_path = str(tmp_path / "crash.db")
        stale = int(time.time() * 1000) - _STALE_AGE_MS

        # Process 1: starts jobs, dies without finalizing any row.
        writer = SQLiteAdapter(db_path=db_path)
        writer.init_db()
        _insert_render_job(writer, "rj-stale", "running", started_ms=stale)
        _insert_walk_run(
            writer, "wr-stale", "running", created_ms=stale, heartbeat_ms=stale
        )
        _insert_walk_run(writer, "wr-nohb", "running", created_ms=stale)
        writer.close()

        # Process 2: same file, one startup reconcile pass.
        adapter = self._reopen(db_path)
        try:
            counts = adapter.reconcile_stale_runs()
            assert counts == {"render_job": 1, "walk_run": 2}

            rj = adapter.execute_query(
                "SELECT status, error, finished_ms FROM render_job "
                "WHERE job_id = ?",
                ("rj-stale",),
            )[0]
            assert rj["status"] == "interrupted"
            assert rj["error"] == "interrupted by process restart"
            assert rj["finished_ms"] is not None

            for run_id in ("wr-stale", "wr-nohb"):
                wr = adapter.execute_query(
                    "SELECT status, error, finished_ms FROM walk_run "
                    "WHERE run_id = ?",
                    (run_id,),
                )[0]
                assert wr["status"] == "interrupted"
                assert wr["error"] == "interrupted by process restart"
                assert wr["finished_ms"] is not None
        finally:
            adapter.close()

    def test_reconcile_leaves_fresh_running_rows_untouched(self, tmp_path):
        """Rows started just before reconcile (within the grace window) are
        NOT stale — they must survive untouched."""
        db_path = str(tmp_path / "fresh.db")
        now = int(time.time() * 1000)

        writer = SQLiteAdapter(db_path=db_path)
        writer.init_db()
        _insert_render_job(writer, "rj-fresh", "running", started_ms=now)
        _insert_walk_run(
            writer, "wr-fresh", "running", created_ms=now, heartbeat_ms=now
        )
        writer.close()

        adapter = self._reopen(db_path)
        try:
            counts = adapter.reconcile_stale_runs()
            assert counts == {"render_job": 0, "walk_run": 0}
            jobs = adapter.execute_query(
                "SELECT job_id, status FROM render_job"
            )
            assert jobs == [{"job_id": "rj-fresh", "status": "running"}]
            runs = adapter.execute_query(
                "SELECT run_id, status FROM walk_run"
            )
            assert runs == [{"run_id": "wr-fresh", "status": "running"}]
        finally:
            adapter.close()

    def test_reconcile_leaves_terminal_rows_untouched(self, tmp_path):
        """completed/failed/cancelled/interrupted (and pending) rows are never
        flipped — the predicate targets status='running' only, even when the
        timestamps are old."""
        db_path = str(tmp_path / "terminal.db")
        stale = int(time.time() * 1000) - _STALE_AGE_MS

        writer = SQLiteAdapter(db_path=db_path)
        writer.init_db()
        for status in ("completed", "failed", "cancelled", "interrupted"):
            _insert_render_job(writer, f"rj-{status}", status, started_ms=stale)
            _insert_walk_run(writer, f"wr-{status}", status, created_ms=stale)
        _insert_walk_run(writer, "wr-pending", "pending", created_ms=stale)
        writer.close()

        adapter = self._reopen(db_path)
        try:
            counts = adapter.reconcile_stale_runs()
            assert counts == {"render_job": 0, "walk_run": 0}
            jobs = {
                row["job_id"]: row["status"]
                for row in adapter.execute_query(
                    "SELECT job_id, status FROM render_job"
                )
            }
            assert jobs == {
                f"rj-{s}": s for s in ("completed", "failed", "cancelled", "interrupted")
            }
            runs = {
                row["run_id"]: row["status"]
                for row in adapter.execute_query(
                    "SELECT run_id, status FROM walk_run"
                )
            }
            assert runs == {
                **{
                    f"wr-{s}": s
                    for s in ("completed", "failed", "cancelled", "interrupted")
                },
                "wr-pending": "pending",
            }
        finally:
            adapter.close()

    def test_reconcile_is_idempotent_second_call_flips_nothing(self, tmp_path):
        """One pass only: after the first reconcile flips the stale rows, a
        second call must flip nothing new (contract rule #5 — no sweeper)."""
        db_path = str(tmp_path / "idempotent.db")
        stale = int(time.time() * 1000) - _STALE_AGE_MS

        writer = SQLiteAdapter(db_path=db_path)
        writer.init_db()
        _insert_render_job(writer, "rj-stale", "running", started_ms=stale)
        _insert_walk_run(writer, "wr-stale", "running", created_ms=stale)
        writer.close()

        adapter = self._reopen(db_path)
        try:
            first = adapter.reconcile_stale_runs()
            assert first == {"render_job": 1, "walk_run": 1}
            second = adapter.reconcile_stale_runs()
            assert second == {"render_job": 0, "walk_run": 0}
        finally:
            adapter.close()

    def test_reconcile_is_safe_on_empty_db(self, tmp_path):
        """Reconcile must be a no-op on a freshly initialised (empty) DB."""
        adapter = SQLiteAdapter(db_path=str(tmp_path / "empty.db"))
        adapter.init_db()
        try:
            assert adapter.reconcile_stale_runs() == {
                "render_job": 0,
                "walk_run": 0,
            }
        finally:
            adapter.close()

    @pytest.mark.parametrize("adapter_name", _ADAPTER_FIXTURES)
    def test_reconcile_flips_stale_rows_on_both_adapters(
        self, adapter_name, request
    ):
        """Both adapters expose the same reconcile behavior (mirror parity):
        stale rows flip, fresh rows survive."""
        adapter = request.getfixturevalue(adapter_name)
        stale = int(time.time() * 1000) - _STALE_AGE_MS
        now = int(time.time() * 1000)
        _insert_render_job(adapter, "rj-stale", "running", started_ms=stale)
        _insert_render_job(adapter, "rj-fresh", "running", started_ms=now)
        _insert_walk_run(
            adapter, "wr-stale", "running", created_ms=stale, heartbeat_ms=stale
        )
        _insert_walk_run(adapter, "wr-nohb", "running", created_ms=stale)
        _insert_walk_run(
            adapter, "wr-fresh", "running", created_ms=now, heartbeat_ms=now
        )

        counts = adapter.reconcile_stale_runs()

        assert counts == {"render_job": 1, "walk_run": 2}
        flipped_jobs = {
            row["job_id"]
            for row in adapter.execute_query(
                "SELECT job_id FROM render_job WHERE status = 'interrupted'"
            )
        }
        assert flipped_jobs == {"rj-stale"}
        flipped_runs = {
            row["run_id"]
            for row in adapter.execute_query(
                "SELECT run_id FROM walk_run WHERE status = 'interrupted'"
            )
        }
        assert flipped_runs == {"wr-stale", "wr-nohb"}


class TestReconcileStartupWiring:
    """The production storage bootstrap must run exactly one reconcile pass,
    after init_db(), before the API serves any request."""

    def test_production_storage_bootstrap_runs_reconcile_once(self, monkeypatch):
        import app.pipeline.api_onboard as api_onboard

        fake = MagicMock()
        monkeypatch.setattr(api_onboard, "_storage", None)
        monkeypatch.setattr(api_onboard, "SQLiteAdapter", lambda db_path: fake)

        first = api_onboard._get_production_storage()
        assert first is fake
        fake.init_db.assert_called_once_with()
        fake.reconcile_stale_runs.assert_called_once_with()

        # The singleton short-circuits subsequent calls: no second reconcile.
        second = api_onboard._get_production_storage()
        assert second is fake
        fake.reconcile_stale_runs.assert_called_once_with()
