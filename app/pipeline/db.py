"""Pipeline database factory.

Provides ``get_pipeline_db()`` which returns a configured ``PipelineStorage``
adapter.  By default it returns an on-disk ``SQLiteAdapter`` pointing at
``./data/pipeline.db``.

Configuration
-------------
Set the environment variable ``PIPELINE_DB_BACKEND`` to select the backend:

- ``"sqlite"`` (default) — on-disk SQLite at ``./data/pipeline.db``
  (override path with ``PIPELINE_DB_PATH``)
- ``"memory"`` — in-memory SQLite (for testing)
"""

from __future__ import annotations

import os
from typing import Optional

from app.pipeline.adapter import InMemorySQLiteAdapter, PipelineStorage, SQLiteAdapter

# Module-level singleton so repeated calls return the same instance.
_instance: Optional[PipelineStorage] = None


def get_pipeline_db() -> PipelineStorage:
    """Return the configured pipeline storage adapter.

    The backend is selected by the ``PIPELINE_DB_BACKEND`` environment
    variable (``"sqlite"`` or ``"memory"``).  The default is ``"sqlite"``
    with the database file at ``./data/pipeline.db`` (overridable via
    ``PIPELINE_DB_PATH``).

    The returned instance is cached at module level — subsequent calls
    return the same object.  Call ``close()`` on the adapter to release
    resources, or use ``reset_pipeline_db()`` in tests.
    """
    global _instance  # noqa: PLW0603
    if _instance is not None:
        return _instance

    backend = os.environ.get("PIPELINE_DB_BACKEND", "sqlite").lower()

    if backend == "memory":
        _instance = InMemorySQLiteAdapter()
    else:
        db_path = os.environ.get("PIPELINE_DB_PATH", "./data/pipeline.db")
        _instance = SQLiteAdapter(db_path=db_path)

    _instance.init_db()
    return _instance


def reset_pipeline_db() -> None:
    """Close and discard the cached adapter instance.

    Intended for test teardown — ensures a clean slate between tests.
    """
    global _instance  # noqa: PLW0603
    if _instance is not None:
        _instance.close()
        _instance = None
