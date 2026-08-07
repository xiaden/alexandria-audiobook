"""Spec-first tests for the walk_override CRUD access methods.

Plan G Phase 4. ``walk_override`` rows (PK ``book_id + walk_name + key``,
``value_json`` TEXT column) are written/read/deleted through the adapter so
the Setup tab (Phase 5) and the resolver never touch raw SQL.

Contract (CONTRACTS.md Plan G / OverrideRow DTO):
- ``get_walk_overrides(book_id) -> list[dict]`` — ALL rows for the book as
  ``{book_id, walk_name, key, value_json}`` with ``value_json`` the RAW TEXT
  string (the caller decides whether to ``json.loads``); missing table
  degrades gracefully to ``[]``.
- ``upsert_walk_override(book_id, walk_name, key, value_json) -> None`` —
  INSERT-or-UPDATE on the composite PK; ``value_json`` is JSON-encoded.
- ``delete_walk_override(book_id, walk_name, key) -> None`` — no-op if absent.

Covered here, for BOTH SQLiteAdapter (file-backed tmp) and
InMemorySQLiteAdapter:
- upsert inserts a row; upsert on an existing PK updates value_json
  (row count stays 1 — true upsert semantics)
- JSON round-trip: json.dumps -> upsert -> get -> json.loads is identical
- read-all-for-book returns only that book's rows (scoping)
- value_json is returned as the raw TEXT string, not parsed
- delete removes the row; delete of an absent row is a no-op;
  delete is scoped to the exact composite PK
- missing walk_override table (uninitialized adapter) -> get returns []
"""

from __future__ import annotations

import json

import pytest

from app.pipeline.adapter import InMemorySQLiteAdapter, SQLiteAdapter

_ADAPTER_FIXTURES = ["sqlite_adapter", "memory_adapter"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sqlite_adapter(tmp_path):
    """File-backed SQLiteAdapter with schema initialised."""
    adapter = SQLiteAdapter(db_path=str(tmp_path / "override.db"))
    adapter.init_db()
    yield adapter
    adapter.close()


@pytest.fixture()
def memory_adapter():
    """InMemorySQLiteAdapter with schema initialised."""
    adapter = InMemorySQLiteAdapter()
    adapter.init_db()
    yield adapter
    adapter.close()


# ---------------------------------------------------------------------------
# Upsert semantics
# ---------------------------------------------------------------------------


class TestUpsertWalkOverride:
    @pytest.mark.parametrize("adapter_name", _ADAPTER_FIXTURES)
    def test_upsert_inserts_a_row(self, adapter_name, request):
        adapter = request.getfixturevalue(adapter_name)
        adapter.upsert_walk_override("book-1", "scene_segmentation", "temperature", "0.9")
        rows = adapter.execute_query("SELECT * FROM walk_override")
        assert len(rows) == 1
        assert rows[0]["book_id"] == "book-1"
        assert rows[0]["walk_name"] == "scene_segmentation"
        assert rows[0]["key"] == "temperature"
        assert rows[0]["value_json"] == "0.9"

    @pytest.mark.parametrize("adapter_name", _ADAPTER_FIXTURES)
    def test_upsert_on_existing_pk_updates_value(self, adapter_name, request):
        adapter = request.getfixturevalue(adapter_name)
        adapter.upsert_walk_override("book-1", "scene_segmentation", "temperature", "0.9")
        adapter.upsert_walk_override("book-1", "scene_segmentation", "temperature", "0.3")
        rows = adapter.execute_query("SELECT * FROM walk_override")
        # True upsert semantics: the PK row count stays 1.
        assert len(rows) == 1
        assert rows[0]["value_json"] == "0.3"
        assert rows[0]["key"] == "temperature"

    @pytest.mark.parametrize("adapter_name", _ADAPTER_FIXTURES)
    def test_upsert_distinct_keys_coexist(self, adapter_name, request):
        adapter = request.getfixturevalue(adapter_name)
        adapter.upsert_walk_override("book-1", "scene_segmentation", "temperature", "0.9")
        adapter.upsert_walk_override("book-1", "scene_segmentation", "model_name", '"gpt-4o"')
        rows = adapter.execute_query("SELECT * FROM walk_override")
        assert len(rows) == 2
        assert {r["key"] for r in rows} == {"temperature", "model_name"}


# ---------------------------------------------------------------------------
# JSON round-trip through value_json
# ---------------------------------------------------------------------------


class TestJsonRoundTrip:
    @pytest.mark.parametrize("adapter_name", _ADAPTER_FIXTURES)
    def test_round_trip_returns_identical_value(self, adapter_name, request):
        adapter = request.getfixturevalue(adapter_name)
        value = {"temperature": 0.9, "prompt": "be concise", "nested": {"a": [1, 2, 3]}}
        adapter.upsert_walk_override("book-1", "scene_segmentation", "config", json.dumps(value))
        rows = adapter.get_walk_overrides("book-1")
        assert len(rows) == 1
        # value_json comes back as the raw TEXT string — the caller json.loads.
        assert isinstance(rows[0]["value_json"], str)
        assert json.loads(rows[0]["value_json"]) == value

    @pytest.mark.parametrize("adapter_name", _ADAPTER_FIXTURES)
    def test_scalar_json_round_trip(self, adapter_name, request):
        adapter = request.getfixturevalue(adapter_name)
        adapter.upsert_walk_override("book-1", "delivery", "temperature", json.dumps(0.0))
        rows = adapter.get_walk_overrides("book-1")
        # Explicit 0.0 survives the round trip (is-not-None resolver semantics).
        assert json.loads(rows[0]["value_json"]) == 0.0


# ---------------------------------------------------------------------------
# Read-all-for-book: scoping + row shape
# ---------------------------------------------------------------------------


class TestGetWalkOverrides:
    @pytest.mark.parametrize("adapter_name", _ADAPTER_FIXTURES)
    def test_returns_only_that_books_rows(self, adapter_name, request):
        adapter = request.getfixturevalue(adapter_name)
        adapter.upsert_walk_override("book-1", "scene_segmentation", "temperature", "0.9")
        adapter.upsert_walk_override("book-1", "delivery", "temperature", "0.8")
        adapter.upsert_walk_override("book-2", "scene_segmentation", "temperature", "0.1")
        rows = adapter.get_walk_overrides("book-1")
        assert len(rows) == 2
        assert {r["walk_name"] for r in rows} == {"scene_segmentation", "delivery"}
        for row in rows:
            assert row["book_id"] == "book-1"
            assert set(row.keys()) == {"book_id", "walk_name", "key", "value_json"}

    @pytest.mark.parametrize("adapter_name", _ADAPTER_FIXTURES)
    def test_empty_book_returns_empty_list(self, adapter_name, request):
        adapter = request.getfixturevalue(adapter_name)
        adapter.upsert_walk_override("book-1", "scene_segmentation", "temperature", "0.9")
        assert adapter.get_walk_overrides("no-such-book") == []


# ---------------------------------------------------------------------------
# Delete semantics
# ---------------------------------------------------------------------------


class TestDeleteWalkOverride:
    @pytest.mark.parametrize("adapter_name", _ADAPTER_FIXTURES)
    def test_delete_removes_the_row(self, adapter_name, request):
        adapter = request.getfixturevalue(adapter_name)
        adapter.upsert_walk_override("book-1", "scene_segmentation", "temperature", "0.9")
        adapter.delete_walk_override("book-1", "scene_segmentation", "temperature")
        assert adapter.get_walk_overrides("book-1") == []

    @pytest.mark.parametrize("adapter_name", _ADAPTER_FIXTURES)
    def test_delete_absent_row_is_noop(self, adapter_name, request):
        adapter = request.getfixturevalue(adapter_name)
        adapter.upsert_walk_override("book-1", "scene_segmentation", "temperature", "0.9")
        adapter.delete_walk_override("book-1", "scene_segmentation", "model_name")
        rows = adapter.get_walk_overrides("book-1")
        assert len(rows) == 1
        assert rows[0]["key"] == "temperature"

    @pytest.mark.parametrize("adapter_name", _ADAPTER_FIXTURES)
    def test_delete_scoped_to_exact_composite_pk(self, adapter_name, request):
        adapter = request.getfixturevalue(adapter_name)
        adapter.upsert_walk_override("book-1", "scene_segmentation", "temperature", "0.9")
        adapter.upsert_walk_override("book-1", "delivery", "temperature", "0.8")
        adapter.upsert_walk_override("book-2", "scene_segmentation", "temperature", "0.1")
        adapter.delete_walk_override("book-1", "scene_segmentation", "temperature")
        assert len(adapter.get_walk_overrides("book-1")) == 1  # delivery row survives
        assert len(adapter.get_walk_overrides("book-2")) == 1  # other book survives


# ---------------------------------------------------------------------------
# Missing-table grace: uninitialized database (no walk_override table)
# ---------------------------------------------------------------------------


class TestMissingTableGrace:
    @pytest.mark.parametrize("backend", ["file", "memory"])
    def test_get_on_uninitialized_adapter_returns_empty(self, backend, tmp_path):
        if backend == "file":
            adapter = SQLiteAdapter(db_path=str(tmp_path / "uninit.db"))
        else:
            adapter = InMemorySQLiteAdapter()
        try:
            # init_db() never ran — walk_override does not exist yet.
            assert adapter.get_walk_overrides("book-1") == []
        finally:
            adapter.close()
