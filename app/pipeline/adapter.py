"""Storage adapter interface for the audiobook pipeline.

Provides:
- ``PipelineStorage`` — abstract base class defining the storage contract
- ``SQLiteAdapter`` — on-disk SQLite implementation (WAL mode, FK enforcement)
- ``InMemorySQLiteAdapter`` — in-memory SQLite for testing (same schema, no disk)
"""

from __future__ import annotations

import os
import sqlite3
from abc import ABC, abstractmethod

from app.pipeline.schema import create_schema


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
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        # WAL mode for concurrent read access
        self._conn.execute("PRAGMA journal_mode = WAL")
        # Enforce foreign keys
        self._conn.execute("PRAGMA foreign_keys = ON")

    # -- PipelineStorage interface ------------------------------------------

    def init_db(self) -> None:
        create_schema(self._conn)

    def get_connection(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        self._conn.close()

    def execute_query(self, sql: str, params: tuple = ()) -> list[dict]:
        self._conn.row_factory = sqlite3.Row
        cursor = self._conn.execute(sql, params)
        rows = cursor.fetchall()
        return [dict(row) for row in rows]

    def execute_insert(self, sql: str, params: tuple = ()) -> int:
        was_in_transaction = self._conn.in_transaction
        cursor = self._conn.execute(sql, params)
        if not was_in_transaction:
            self._conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    def execute_update(self, sql: str, params: tuple = ()) -> int:
        was_in_transaction = self._conn.in_transaction
        cursor = self._conn.execute(sql, params)
        if not was_in_transaction:
            self._conn.commit()
        return cursor.rowcount

    def execute_delete(self, sql: str, params: tuple = ()) -> int:
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
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.execute("PRAGMA foreign_keys = ON")

    # -- PipelineStorage interface ------------------------------------------

    def init_db(self) -> None:
        create_schema(self._conn)

    def get_connection(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        self._conn.close()

    def execute_query(self, sql: str, params: tuple = ()) -> list[dict]:
        self._conn.row_factory = sqlite3.Row
        cursor = self._conn.execute(sql, params)
        rows = cursor.fetchall()
        return [dict(row) for row in rows]

    def execute_insert(self, sql: str, params: tuple = ()) -> int:
        was_in_transaction = self._conn.in_transaction
        cursor = self._conn.execute(sql, params)
        if not was_in_transaction:
            self._conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    def execute_update(self, sql: str, params: tuple = ()) -> int:
        was_in_transaction = self._conn.in_transaction
        cursor = self._conn.execute(sql, params)
        if not was_in_transaction:
            self._conn.commit()
        return cursor.rowcount

    def execute_delete(self, sql: str, params: tuple = ()) -> int:
        was_in_transaction = self._conn.in_transaction
        cursor = self._conn.execute(sql, params)
        if not was_in_transaction:
            self._conn.commit()
        return cursor.rowcount
