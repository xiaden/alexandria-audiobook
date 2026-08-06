"""Storage adapter interface for the audiobook pipeline.

Provides:
- ``PipelineStorage`` — abstract base class defining the storage contract
- ``SQLiteAdapter`` — on-disk SQLite implementation (WAL mode, FK enforcement)
- ``InMemorySQLiteAdapter`` — in-memory SQLite for testing (same schema, no disk)

Concurrency: both adapters connect with ``isolation_level=None`` (explicit
autocommit) and serialize writes through the owner-thread ``transaction()``
context manager (``BEGIN IMMEDIATE`` + explicit COMMIT/ROLLBACK); writes from
a non-owner thread — or a ``BEGIN IMMEDIATE`` that times out under contention —
raise ``ConcurrentTransactionError``, mapped to HTTP 503 + ``Retry-After``.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager

from app.pipeline.schema import create_schema


# ---------------------------------------------------------------------------
# Concurrency guard
# ---------------------------------------------------------------------------


class ConcurrentTransactionError(RuntimeError):
    """Raised when a write is attempted from a thread that does not own the
    open transaction, or when ``BEGIN IMMEDIATE`` times out under contention.

    The API layer maps this to HTTP 503 + ``Retry-After`` so a concurrent
    walk/render writer can back off and retry its idempotent write phase.
    """


# ---------------------------------------------------------------------------
# Abstract base class
# ---------------------------------------------------------------------------


class PipelineStorage(ABC):
    """Abstract storage interface for the pipeline.

    All concrete adapters must implement these methods.  The interface is
    deliberately narrow: callers interact through ``execute_*`` helpers
    rather than raw cursors so that the backend can be swapped without
    touching call-sites.
    """

    @abstractmethod
    def init_db(self) -> None:
        """Create the schema (tables + views) if they do not exist."""

    @abstractmethod
    def get_connection(self) -> sqlite3.Connection:
        """Return the underlying ``sqlite3.Connection``."""

    @abstractmethod
    def close(self) -> None:
        """Close the connection and release resources."""

    @abstractmethod
    def execute_query(self, sql: str, params: tuple = ()) -> list[dict]:
        """Execute a SELECT and return rows as a list of dicts."""

    @abstractmethod
    def execute_insert(self, sql: str, params: tuple = ()) -> int:
        """Execute an INSERT and return ``lastrowid``."""

    @abstractmethod
    def execute_update(self, sql: str, params: tuple = ()) -> int:
        """Execute an UPDATE and return ``rowcount``."""

    @abstractmethod
    def execute_delete(self, sql: str, params: tuple = ()) -> int:
        """Execute a DELETE and return ``rowcount``."""


# ---------------------------------------------------------------------------
# SQLite on-disk adapter
# ---------------------------------------------------------------------------


class SQLiteAdapter(PipelineStorage):
    """On-disk SQLite adapter with WAL journaling and FK enforcement.

    Parameters
    ----------
    db_path:
        Filesystem path to the SQLite database file.  Parent directories
        are created automatically.  Defaults to ``./data/pipeline.db``.
    """

    def __init__(self, db_path: str = "./data/pipeline.db") -> None:
        self._db_path = db_path
        # Ensure parent directory exists
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # isolation_level=None => explicit autocommit: transactions are only
        # opened through transaction() (BEGIN IMMEDIATE), so a multi-statement
        # write can never interleave with another thread's auto-commit.
        self._conn = sqlite3.connect(
            db_path, check_same_thread=False, isolation_level=None
        )
        # WAL mode for concurrent read access
        self._conn.execute("PRAGMA journal_mode = WAL")
        # Enforce foreign keys
        self._conn.execute("PRAGMA foreign_keys = ON")
        # Owner-thread bookkeeping for the transaction() guard.
        self._txn_owner: int | None = None
        self._txn_depth = 0

    # -- PipelineStorage interface ------------------------------------------

    def init_db(self) -> None:
        create_schema(self._conn)
        # Busy timeout: wait up to 5s for a locked database instead of
        # failing immediately with "database is locked". Set at startup,
        # after the WAL/foreign_keys PRAGMAs issued in __init__, so the
        # connection is armed before any transaction() use.
        self._conn.execute("PRAGMA busy_timeout = 5000")

    def get_connection(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        self._conn.close()

    # -- transaction support ------------------------------------------------

    def _ensure_owner_thread(self) -> None:
        """Reject writes from threads that do not own the open transaction."""
        owner = self._txn_owner
        if owner is not None and owner != threading.get_ident():
            raise ConcurrentTransactionError(
                "write from thread %s while transaction is owned by thread %s"
                % (threading.get_ident(), owner)
            )

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Run the wrapped block inside a ``BEGIN IMMEDIATE`` transaction.

        The first entry records the owning thread and issues
        ``BEGIN IMMEDIATE``; a clean exit COMMITs and an exception ROLLBACKs.
        Nested re-entry from the same thread joins the outer transaction (the
        inner exit neither commits nor rolls back).  Writes issued from any
        other thread while a transaction is open raise
        ``ConcurrentTransactionError``.
        """
        tid = threading.get_ident()
        if self._txn_depth == 0:
            self._txn_owner = tid
            try:
                self._conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                # The write lock was not acquired within busy_timeout — clear
                # the owner so the guard cannot reject other threads for a
                # transaction that never opened, and surface the contention
                # as the contracted ConcurrentTransactionError.
                self._txn_owner = None
                raise ConcurrentTransactionError(
                    "BEGIN IMMEDIATE timed out under contention"
                ) from exc
            except BaseException:
                self._txn_owner = None
                raise
        self._txn_depth += 1
        try:
            yield
        except BaseException:
            self._txn_depth -= 1
            if self._txn_depth == 0:
                try:
                    self._conn.rollback()
                finally:
                    self._txn_owner = None
            raise
        else:
            self._txn_depth -= 1
            if self._txn_depth == 0:
                try:
                    self._conn.commit()
                finally:
                    self._txn_owner = None

    def execute_query(self, sql: str, params: tuple = ()) -> list[dict]:
        self._conn.row_factory = sqlite3.Row
        cursor = self._conn.execute(sql, params)
        rows = cursor.fetchall()
        return [dict(row) for row in rows]

    def execute_insert(self, sql: str, params: tuple = ()) -> int:
        self._ensure_owner_thread()
        was_in_transaction = self._conn.in_transaction
        cursor = self._conn.execute(sql, params)
        if not was_in_transaction:
            self._conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    def execute_update(self, sql: str, params: tuple = ()) -> int:
        self._ensure_owner_thread()
        was_in_transaction = self._conn.in_transaction
        cursor = self._conn.execute(sql, params)
        if not was_in_transaction:
            self._conn.commit()
        return cursor.rowcount

    def execute_delete(self, sql: str, params: tuple = ()) -> int:
        self._ensure_owner_thread()
        was_in_transaction = self._conn.in_transaction
        cursor = self._conn.execute(sql, params)
        if not was_in_transaction:
            self._conn.commit()
        return cursor.rowcount


# ---------------------------------------------------------------------------
# In-memory adapter (for testing)
# ---------------------------------------------------------------------------


class InMemorySQLiteAdapter(PipelineStorage):
    """In-memory SQLite adapter for testing.

    Same schema and interface as ``SQLiteAdapter`` but uses ``:memory:``
    so no disk I/O occurs.  Ideal for unit tests.
    """

    def __init__(self) -> None:
        # isolation_level=None => explicit autocommit (see SQLiteAdapter).
        self._conn = sqlite3.connect(
            ":memory:", check_same_thread=False, isolation_level=None
        )
        self._conn.execute("PRAGMA foreign_keys = ON")
        # Owner-thread bookkeeping for the transaction() guard.
        self._txn_owner: int | None = None
        self._txn_depth = 0

    # -- PipelineStorage interface ------------------------------------------

    def init_db(self) -> None:
        create_schema(self._conn)
        # Busy timeout: wait up to 5s for a locked database instead of
        # failing immediately with "database is locked". Set at startup,
        # after the foreign_keys PRAGMA issued in __init__, so the
        # connection is armed before any transaction() use.
        self._conn.execute("PRAGMA busy_timeout = 5000")

    def get_connection(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        self._conn.close()

    # -- transaction support ------------------------------------------------

    def _ensure_owner_thread(self) -> None:
        """Reject writes from threads that do not own the open transaction."""
        owner = self._txn_owner
        if owner is not None and owner != threading.get_ident():
            raise ConcurrentTransactionError(
                "write from thread %s while transaction is owned by thread %s"
                % (threading.get_ident(), owner)
            )

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Run the wrapped block inside a ``BEGIN IMMEDIATE`` transaction.

        The first entry records the owning thread and issues
        ``BEGIN IMMEDIATE``; a clean exit COMMITs and an exception ROLLBACKs.
        Nested re-entry from the same thread joins the outer transaction (the
        inner exit neither commits nor rolls back).  Writes issued from any
        other thread while a transaction is open raise
        ``ConcurrentTransactionError``.
        """
        tid = threading.get_ident()
        if self._txn_depth == 0:
            self._txn_owner = tid
            try:
                self._conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                # The write lock was not acquired within busy_timeout — clear
                # the owner so the guard cannot reject other threads for a
                # transaction that never opened, and surface the contention
                # as the contracted ConcurrentTransactionError.
                self._txn_owner = None
                raise ConcurrentTransactionError(
                    "BEGIN IMMEDIATE timed out under contention"
                ) from exc
            except BaseException:
                self._txn_owner = None
                raise
        self._txn_depth += 1
        try:
            yield
        except BaseException:
            self._txn_depth -= 1
            if self._txn_depth == 0:
                try:
                    self._conn.rollback()
                finally:
                    self._txn_owner = None
            raise
        else:
            self._txn_depth -= 1
            if self._txn_depth == 0:
                try:
                    self._conn.commit()
                finally:
                    self._txn_owner = None

    def execute_query(self, sql: str, params: tuple = ()) -> list[dict]:
        self._conn.row_factory = sqlite3.Row
        cursor = self._conn.execute(sql, params)
        rows = cursor.fetchall()
        return [dict(row) for row in rows]

    def execute_insert(self, sql: str, params: tuple = ()) -> int:
        self._ensure_owner_thread()
        was_in_transaction = self._conn.in_transaction
        cursor = self._conn.execute(sql, params)
        if not was_in_transaction:
            self._conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    def execute_update(self, sql: str, params: tuple = ()) -> int:
        self._ensure_owner_thread()
        was_in_transaction = self._conn.in_transaction
        cursor = self._conn.execute(sql, params)
        if not was_in_transaction:
            self._conn.commit()
        return cursor.rowcount

    def execute_delete(self, sql: str, params: tuple = ()) -> int:
        self._ensure_owner_thread()
        was_in_transaction = self._conn.in_transaction
        cursor = self._conn.execute(sql, params)
        if not was_in_transaction:
            self._conn.commit()
        return cursor.rowcount
