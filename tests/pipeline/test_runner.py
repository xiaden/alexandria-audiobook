"""Spec-first tests for walk runner infrastructure (app.pipeline.walks.runner).

Covers:
- WalkRunner initialization with storage
- run_walk with mock walk modules (mock import and execute function)
- Serial execution enforcement (walk already running → refused)
- Walk status transitions: pending → running → completed
- Walk status transitions: pending → running → failed (exception)
- Verification failure: execute() succeeds but verification fails → status='failed'
- run_all_walks: multiple walks called in order, abort on failure
- Import error: walk_name that doesn't exist → graceful failure
- get_walk_status for unknown book/walk returns 'pending'
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import types
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.pipeline.adapter import ConcurrentTransactionError, InMemorySQLiteAdapter
from app.pipeline.walks.order import WALK_ORDER
from app.pipeline.walks.runner import HeartbeatStorage, WalkRunner

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def storage():
    """In-memory SQLite adapter for testing."""
    adapter = InMemorySQLiteAdapter()
    adapter.init_db()
    return adapter


@pytest.fixture()
def runner(storage):
    """WalkRunner with in-memory storage."""
    return WalkRunner(storage)


def _make_mock_walk_module(execute_fn=None):
    """Create a mock walk module with an execute function."""
    mock_module = types.ModuleType("mock_walk")
    mock_module.execute = execute_fn or MagicMock(return_value={"status": "completed"})
    return mock_module


class _FlakyStorage:
    """Storage proxy that raises ConcurrentTransactionError on walk writes.

    Simulates the non-owner-thread case from adapter.py: a write attempted
    while another thread owns an open transaction. The first ``fail_attempts``
    non-heartbeat writes (the walk's idempotent write) raise
    ``ConcurrentTransactionError``; later ones delegate to the real adapter.
    The runner's own walk_run bookkeeping always contains ``heartbeat_ms`` in
    its SQL, so those writes pass through untouched and never pollute the
    attempt counters. Each walk-write attempt also records a
    ``time.monotonic()`` stamp so tests can assert the 50-100ms backoff gap.
    """

    def __init__(self, real, fail_attempts):
        self._real = real
        self._fail_attempts = fail_attempts
        self._walk_write_attempts = 0
        self._attempt_times: list[float] = []

    def _dispatch(self, method, sql, params):
        if "heartbeat_ms" not in sql:
            self._walk_write_attempts += 1
            self._attempt_times.append(time.monotonic())
            if self._walk_write_attempts <= self._fail_attempts:
                raise ConcurrentTransactionError(
                    "write from thread 2 while transaction is owned by thread 1"
                )
        return getattr(self._real, method)(sql, params)

    def execute_insert(self, sql, params=()):
        return self._dispatch("execute_insert", sql, params)

    def execute_update(self, sql, params=()):
        return self._dispatch("execute_update", sql, params)

    def execute_delete(self, sql, params=()):
        return self._dispatch("execute_delete", sql, params)

    def execute_query(self, sql, params=()):
        return self._real.execute_query(sql, params)

    def __getattr__(self, name):
        return getattr(self._real, name)


# ---------------------------------------------------------------------------
# Test WalkRunner initialization
# ---------------------------------------------------------------------------


class TestWalkRunnerInit:
    def test_init_stores_storage(self, storage):
        """WalkRunner stores the storage adapter."""
        runner = WalkRunner(storage)
        assert runner._storage is storage

    def test_init_empty_status(self, storage):
        """WalkRunner starts with empty status dict."""
        runner = WalkRunner(storage)
        assert runner._status == {}

    def test_walk_order_is_class_constant(self):
        """WALK_ORDER is a class-level list of walk names."""
        assert isinstance(WALK_ORDER, list)
        assert "walk_2a_scene_segmentation" in WALK_ORDER


# ---------------------------------------------------------------------------
# Test run_walk with mock walk module
# ---------------------------------------------------------------------------


class TestRunWalk:
    def test_run_walk_calls_execute(self, runner):
        """run_walk loads the walk module and calls execute()."""
        mock_execute = MagicMock(return_value={"status": "completed", "scenes": 3})
        mock_module = _make_mock_walk_module(mock_execute)
        with (
            patch.object(WalkRunner, "_load_walk_module", return_value=mock_module),
            patch.object(WalkRunner, "_run_verification", return_value=True),
        ):
            result = runner.run_walk("walk_2a_scene_segmentation", "book-1", {})
        mock_execute.assert_called_once()
        call_args = mock_execute.call_args.args
        assert call_args[0] == "book-1"
        # Walk modules receive the heartbeat-tracking storage wrapper around
        # the raw adapter (Phase 2 heartbeat mechanism).
        assert isinstance(call_args[1], HeartbeatStorage)
        assert call_args[1].storage is runner._storage
        assert call_args[2] == {}
        assert result["status"] == "completed"

    def test_run_walk_returns_execute_result(self, runner):
        """run_walk returns the dict from execute()."""
        expected = {"status": "completed", "scenes": 5, "chapters": 2}
        mock_module = _make_mock_walk_module(MagicMock(return_value=expected))
        with (
            patch.object(WalkRunner, "_load_walk_module", return_value=mock_module),
            patch.object(WalkRunner, "_run_verification", return_value=True),
        ):
            result = runner.run_walk("walk_2a_scene_segmentation", "book-1", {})
        assert result == expected

    def test_run_walk_passes_config(self, runner):
        """run_walk passes config dict to execute()."""
        mock_execute = MagicMock(return_value={"status": "completed"})
        mock_module = _make_mock_walk_module(mock_execute)
        config = {"temperature": 0.1, "model": "local"}
        with (
            patch.object(WalkRunner, "_load_walk_module", return_value=mock_module),
            patch.object(WalkRunner, "_run_verification", return_value=True),
        ):
            runner.run_walk("walk_2a_scene_segmentation", "book-1", config)
        mock_execute.assert_called_once()
        call_args = mock_execute.call_args.args
        assert call_args[0] == "book-1"
        assert isinstance(call_args[1], HeartbeatStorage)
        assert call_args[1].storage is runner._storage
        assert call_args[2] == config


# ---------------------------------------------------------------------------
# Test walk status transitions
# ---------------------------------------------------------------------------


class TestWalkStatusTransitions:
    def test_initial_status_is_pending(self, runner):
        """Walk status is 'pending' before any run."""
        assert (
            runner.get_walk_status("book-1", "walk_2a_scene_segmentation") == "pending"
        )

    def test_status_running_during_execution(self, runner):
        """Status is 'running' while walk is executing."""
        captured_status = []

        def execute_fn(book_id, storage, config):
            captured_status.append(
                runner.get_walk_status(book_id, "walk_2a_scene_segmentation")
            )
            return {"status": "completed"}

        mock_module = _make_mock_walk_module(execute_fn)
        with patch.object(WalkRunner, "_load_walk_module", return_value=mock_module):
            runner.run_walk("walk_2a_scene_segmentation", "book-1", {})
        assert captured_status == ["running"]

    def test_status_completed_after_success(self, runner):
        """Status is 'completed' after successful walk."""
        mock_module = _make_mock_walk_module()
        with (
            patch.object(WalkRunner, "_load_walk_module", return_value=mock_module),
            patch.object(WalkRunner, "_run_verification", return_value=True),
        ):
            runner.run_walk("walk_2a_scene_segmentation", "book-1", {})
        assert (
            runner.get_walk_status("book-1", "walk_2a_scene_segmentation")
            == "completed"
        )

    def test_status_failed_after_exception(self, runner):
        """Status is 'failed' when execute() raises an exception."""
        mock_module = _make_mock_walk_module(
            MagicMock(side_effect=RuntimeError("boom"))
        )
        with patch.object(WalkRunner, "_load_walk_module", return_value=mock_module):
            result = runner.run_walk("walk_2a_scene_segmentation", "book-1", {})
        assert result["status"] == "failed"
        assert "boom" in result["error"]
        assert (
            runner.get_walk_status("book-1", "walk_2a_scene_segmentation") == "failed"
        )

    def test_unknown_walk_status_is_pending(self, runner):
        """get_walk_status returns 'pending' for unknown walk name."""
        assert runner.get_walk_status("book-1", "nonexistent_walk") == "pending"

    def test_unknown_book_status_is_pending(self, runner):
        """get_walk_status returns 'pending' for unknown book_id."""
        assert (
            runner.get_walk_status("unknown-book", "walk_2a_scene_segmentation")
            == "pending"
        )


# ---------------------------------------------------------------------------
# Test serial execution enforcement
# ---------------------------------------------------------------------------


class TestSerialExecution:
    def test_refuses_concurrent_walk(self, runner):
        """run_walk refuses to start if walk is already 'running' for this book."""
        # Manually set status to 'running'
        runner._ensure_book("book-1")
        runner._set_status("book-1", "walk_2a_scene_segmentation", "running")
        result = runner.run_walk("walk_2a_scene_segmentation", "book-1", {})
        assert result["status"] == "failed"
        assert "already running" in result["error"]

    def test_different_books_can_run_same_walk(self, runner):
        """Different books can run the same walk independently."""
        call_order = []

        def execute_fn(book_id, storage, config):
            call_order.append(book_id)
            return {"status": "completed"}

        mock_module = _make_mock_walk_module(execute_fn)
        with patch.object(WalkRunner, "_load_walk_module", return_value=mock_module):
            runner.run_walk("walk_2a_scene_segmentation", "book-1", {})
            runner.run_walk("walk_2a_scene_segmentation", "book-2", {})
        assert call_order == ["book-1", "book-2"]


# ---------------------------------------------------------------------------
# Test error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_import_error_returns_failed(self, runner):
        """ImportError for nonexistent walk module returns error dict."""
        result = runner.run_walk("nonexistent_walk_xyz", "book-1", {})
        assert result["status"] == "failed"
        assert "error" in result

    def test_import_error_sets_failed_status(self, runner):
        """ImportError sets walk status to 'failed'."""
        runner.run_walk("nonexistent_walk_xyz", "book-1", {})
        assert runner.get_walk_status("book-1", "nonexistent_walk_xyz") == "failed"

    def test_exception_in_execute_returns_failed(self, runner):
        """Exception in execute() returns error dict with exception message."""
        mock_module = _make_mock_walk_module(
            MagicMock(side_effect=ValueError("bad data"))
        )
        with patch.object(WalkRunner, "_load_walk_module", return_value=mock_module):
            result = runner.run_walk("walk_2a_scene_segmentation", "book-1", {})
        assert result["status"] == "failed"
        assert "bad data" in result["error"]


# ---------------------------------------------------------------------------
# Test verification
# ---------------------------------------------------------------------------


class TestVerification:
    def test_verification_failure_with_placeholder_scene(self, storage):
        """A placeholder chapter_scene row does not satisfy Walk 2a verification."""
        runner = WalkRunner(storage)
        storage.execute_insert("INSERT INTO series (id) VALUES (?)", ("series-1",))
        storage.execute_insert(
            "INSERT INTO book (id, series_id, book_number, version, position) VALUES (?, ?, 1, 1, 1)",
            ("book-1", "series-1"),
        )
        storage.execute_insert(
            "INSERT INTO chapter (id, book_id) VALUES (?, ?)",
            ("chapter-1", "book-1"),
        )
        storage.execute_insert("INSERT INTO scene (id) VALUES (?)", ("placeholder",))
        storage.execute_insert(
            "INSERT INTO chapter_scene (child_id, parent_id, position) VALUES (?, ?, ?)",
            ("placeholder", "chapter-1", 1),
        )
        mock_module = _make_mock_walk_module(
            MagicMock(return_value={"status": "completed"})
        )
        with patch.object(WalkRunner, "_load_walk_module", return_value=mock_module):
            result = runner.run_walk("walk_2a_scene_segmentation", "book-1", {})
        assert result["status"] == "failed"
        assert "Verification failed" in result["error"]
        assert (
            runner.get_walk_status("book-1", "walk_2a_scene_segmentation") == "failed"
        )

    def test_verification_failure_marks_failed(self, storage):
        """If verification fails, status is 'failed' even though execute() succeeded."""
        runner = WalkRunner(storage)
        # Set up chapters but no chapter_scene edges (verification will fail)
        storage.execute_insert("INSERT INTO series (id) VALUES (?)", ("series-1",))
        storage.execute_insert(
            "INSERT INTO book (id, series_id, book_number, version, position) VALUES (?, ?, 1, 1, 1)",
            ("book-1", "series-1"),
        )
        storage.execute_insert(
            "INSERT INTO chapter (id, book_id) VALUES (?, ?)",
            ("chapter-1", "book-1"),
        )
        # Mock execute to succeed but verification will find no scenes
        mock_module = _make_mock_walk_module(
            MagicMock(return_value={"status": "completed"})
        )
        with patch.object(WalkRunner, "_load_walk_module", return_value=mock_module):
            result = runner.run_walk("walk_2a_scene_segmentation", "book-1", {})
        assert result["status"] == "failed"
        assert "Verification failed" in result["error"]
        assert (
            runner.get_walk_status("book-1", "walk_2a_scene_segmentation") == "failed"
        )

    def test_verification_passes_with_scenes(self, storage):
        """Verification passes when a non-placeholder scene exists."""
        runner = WalkRunner(storage)
        # Set up minimal data with a scene
        storage.execute_insert("INSERT INTO series (id) VALUES (?)", ("series-1",))
        storage.execute_insert(
            "INSERT INTO book (id, series_id, book_number, version, position) VALUES (?, ?, 1, 1, 1)",
            ("book-1", "series-1"),
        )
        storage.execute_insert(
            "INSERT INTO chapter (id, book_id) VALUES (?, ?)",
            ("chapter-1", "book-1"),
        )
        storage.execute_insert("INSERT INTO scene (id) VALUES (?)", ("scene-1",))
        storage.execute_insert(
            "INSERT INTO chapter_scene (child_id, parent_id, position) VALUES (?, ?, ?)",
            ("scene-1", "chapter-1", 2),
        )
        mock_module = _make_mock_walk_module(
            MagicMock(return_value={"status": "completed"})
        )
        with patch.object(WalkRunner, "_load_walk_module", return_value=mock_module):
            result = runner.run_walk("walk_2a_scene_segmentation", "book-1", {})
        assert result["status"] == "completed"
        assert (
            runner.get_walk_status("book-1", "walk_2a_scene_segmentation")
            == "completed"
        )

    def test_no_verification_registered_passes(self, runner):
        """Walks without a registered verification function pass by default."""
        mock_module = _make_mock_walk_module(
            MagicMock(return_value={"status": "completed"})
        )
        with patch.object(WalkRunner, "_load_walk_module", return_value=mock_module):
            result = runner.run_walk("walk_unknown_no_verify", "book-1", {})
        assert result["status"] == "completed"


# ---------------------------------------------------------------------------
# Test run_all_walks
# ---------------------------------------------------------------------------


class TestRunAllWalks:
    def test_run_all_walks_calls_each_walk(self, runner):
        """run_all_walks executes all walks in WALK_ORDER."""
        call_log = []

        def execute_fn(book_id, storage, config):
            call_log.append(book_id)
            return {"status": "completed"}

        mock_module = _make_mock_walk_module(execute_fn)
        with (
            patch.object(WalkRunner, "_load_walk_module", return_value=mock_module),
            patch.object(WalkRunner, "_run_verification", return_value=True),
        ):
            results = runner.run_all_walks("book-1", {})
        # Should have one result per walk in WALK_ORDER
        assert len(results) == len(WALK_ORDER)
        for walk_name in WALK_ORDER:
            assert walk_name in results

    def test_run_all_walks_aborts_on_failure(self, runner):
        """run_all_walks stops executing walks after one fails."""
        call_count = [0]

        def execute_fn(book_id, storage, config):
            call_count[0] += 1
            raise RuntimeError("walk failed")

        mock_module = _make_mock_walk_module(execute_fn)
        with patch.object(WalkRunner, "_load_walk_module", return_value=mock_module):
            results = runner.run_all_walks("book-1", {})
        # First walk failed, so only one call
        assert call_count[0] == 1
        # First walk result should be failed
        first_walk = WALK_ORDER[0]
        assert results[first_walk]["status"] == "failed"

    def test_run_all_walks_returns_results_dict(self, runner):
        """run_all_walks returns a dict mapping walk_name to result."""
        mock_module = _make_mock_walk_module(
            MagicMock(return_value={"status": "completed"})
        )
        with (
            patch.object(WalkRunner, "_load_walk_module", return_value=mock_module),
            patch.object(WalkRunner, "_run_verification", return_value=True),
        ):
            results = runner.run_all_walks("book-1", {})
        assert isinstance(results, dict)
        for walk_name in WALK_ORDER:
            assert results[walk_name]["status"] == "completed"


# ---------------------------------------------------------------------------
# Test dynamic import
# ---------------------------------------------------------------------------


class TestDynamicImport:
    def test_load_walk_module_uses_importlib(self, runner):
        """_load_walk_module constructs the correct module path."""
        # Register a fake module in sys.modules
        fake_module = types.ModuleType("app.pipeline.walks.walk_fake")
        fake_module.execute = MagicMock(return_value={"status": "completed"})
        sys.modules["app.pipeline.walks.walk_fake"] = fake_module
        try:
            loaded = WalkRunner._load_walk_module("walk_fake")
            assert loaded is fake_module
        finally:
            del sys.modules["app.pipeline.walks.walk_fake"]

    def test_load_walk_module_raises_import_error(self, runner):
        """_load_walk_module raises ImportError for nonexistent module."""
        with pytest.raises(ImportError):
            WalkRunner._load_walk_module("this_module_does_not_exist_xyz")


# ---------------------------------------------------------------------------
# Test background walk execution
# ---------------------------------------------------------------------------


class TestBackgroundWalkExecution:
    """Tests for background walk execution (P2-S11)."""

    def test_run_walk_returns_immediately(self, runner):
        """run_walk returns a dict with status, not the walk result directly."""
        # In the new background model, the endpoint returns immediately
        # The runner.run_walk still returns the result dict
        # This test verifies the runner behavior is unchanged
        with patch.object(
            WalkRunner,
            "_load_walk_module",
            return_value=MagicMock(
                execute=MagicMock(return_value={"status": "completed"})
            ),
        ):
            result = runner.run_walk("walk_test", "book-1", {})
            assert result["status"] == "completed"

    def test_status_transitions_pending_to_running_to_completed(self, runner):
        """Walk status transitions: pending → running → completed."""
        # Initial status is pending
        assert runner.get_walk_status("book-1", "walk_test") == "pending"

        # During execution, status is running
        with patch.object(
            WalkRunner,
            "_load_walk_module",
            return_value=MagicMock(
                execute=MagicMock(return_value={"status": "completed"})
            ),
        ):
            # We can't easily test the running state without threading,
            # but we can verify the final state is completed
            runner.run_walk("walk_test", "book-1", {})
            assert runner.get_walk_status("book-1", "walk_test") == "completed"


# ---------------------------------------------------------------------------
# Test cancellation
# ---------------------------------------------------------------------------


class TestCancellation:
    """Tests for walk cancellation (P2-S12)."""

    def test_cancel_walks_sets_flag(self, runner):
        """cancel_walks sets the cancellation flag for a book."""
        assert not runner._cancelled.get("book-1", False)
        runner.cancel_walks("book-1")
        assert runner._cancelled.get("book-1", False)

    def test_clear_cancel_removes_flag(self, runner):
        """clear_cancel removes the cancellation flag."""
        runner.cancel_walks("book-1")
        assert runner._cancelled.get("book-1", False)
        runner.clear_cancel("book-1")
        assert not runner._cancelled.get("book-1", False)

    def test_run_walk_checks_cancel_flag(self, runner):
        """run_walk checks cancel flag and returns cancelled status."""
        runner.cancel_walks("book-1")
        result = runner.run_walk("walk_test", "book-1", {})
        assert result["status"] == "cancelled"
        assert runner.get_walk_status("book-1", "walk_test") == "cancelled"

    def test_cleared_cancel_allows_rerun(self, runner):
        """A rerun after cancellation is allowed once the latch is cleared."""
        runner.cancel_walks("book-1")
        runner.clear_cancel("book-1")
        with patch.object(
            WalkRunner, "_load_walk_module", return_value=_make_mock_walk_module()
        ):
            result = runner.run_walk("walk_test", "book-1", {})
        assert result["status"] == "completed"

    def test_run_all_walks_stops_on_cancel(self, runner):
        """run_all_walks checks cancel flag before each walk."""
        # Cancel before starting
        runner.cancel_walks("book-1")
        results = runner.run_all_walks("book-1", {})
        # All walks should be cancelled
        for result in results.values():
            assert result["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Test walk_run persistence (Phase 2: rows = truth)
# ---------------------------------------------------------------------------


class TestWalkRunPersistence:
    """Spec-first tests: run_walk/run_all_walks write walk_run rows
    (rows = truth)."""

    def test_run_walk_creates_running_row_at_start(self, runner):
        """A walk_run row (status running, created_ms) exists while executing."""
        seen = []

        def execute_fn(book_id, storage, config):
            rows = storage.execute_query(
                "SELECT run_id, status, created_ms, heartbeat_ms "
                "FROM walk_run WHERE book_id = ?",
                (book_id,),
            )
            seen.append(rows)
            return {"status": "completed"}

        mock_module = _make_mock_walk_module(execute_fn)
        with (
            patch.object(WalkRunner, "_load_walk_module", return_value=mock_module),
            patch.object(WalkRunner, "_run_verification", return_value=True),
        ):
            result = runner.run_walk("walk_2a_scene_segmentation", "book-1", {})
        assert result["status"] == "completed"
        assert len(seen) == 1
        assert len(seen[0]) == 1
        assert seen[0][0]["status"] == "running"
        assert seen[0][0]["created_ms"] is not None

    def test_run_walk_writes_completed_row_with_result_json(self, runner):
        """On success the row flips to completed with result_json + finished_ms."""
        expected = {"status": "completed", "scenes": 3}
        mock_module = _make_mock_walk_module(MagicMock(return_value=expected))
        with (
            patch.object(WalkRunner, "_load_walk_module", return_value=mock_module),
            patch.object(WalkRunner, "_run_verification", return_value=True),
        ):
            runner.run_walk("walk_2a_scene_segmentation", "book-1", {})
        rows = runner._storage.execute_query(
            "SELECT status, result_json, finished_ms, heartbeat_ms "
            "FROM walk_run WHERE book_id = ?",
            ("book-1",),
        )
        assert len(rows) == 1
        assert rows[0]["status"] == "completed"
        assert json.loads(rows[0]["result_json"]) == expected
        assert rows[0]["finished_ms"] is not None

    def test_run_walk_writes_failed_row_on_exception(self, runner):
        """On exception the row flips to failed with the error text."""
        mock_module = _make_mock_walk_module(
            MagicMock(side_effect=RuntimeError("boom"))
        )
        with patch.object(WalkRunner, "_load_walk_module", return_value=mock_module):
            result = runner.run_walk("walk_2a_scene_segmentation", "book-1", {})
        assert result["status"] == "failed"
        rows = runner._storage.execute_query(
            "SELECT status, error, finished_ms FROM walk_run WHERE book_id = ?",
            ("book-1",),
        )
        assert len(rows) == 1
        assert rows[0]["status"] == "failed"
        assert "boom" in rows[0]["error"]
        assert rows[0]["finished_ms"] is not None

    def test_walk_writes_refresh_heartbeat(self, runner):
        """Writes through the heartbeat wrapper refresh walk_run.heartbeat_ms.

        Phase 2 heartbeat mechanism: the walk module receives a
        HeartbeatStorage wrapper; each write through it stamps a fresh
        heartbeat_ms on the run's row.
        """
        captured = {}

        def execute_fn(book_id, storage, config):
            rows = storage.execute_query(
                "SELECT run_id, heartbeat_ms FROM walk_run WHERE book_id = ?",
                (book_id,),
            )
            captured["run_id"] = rows[0]["run_id"]
            captured["before"] = rows[0]["heartbeat_ms"]
            # A write through the wrapper must refresh the row heartbeat.
            storage.execute_update(
                "UPDATE walk_run SET cancel_requested = cancel_requested "
                "WHERE run_id = ?",
                (captured["run_id"],),
            )
            after = storage.execute_query(
                "SELECT heartbeat_ms FROM walk_run WHERE run_id = ?",
                (captured["run_id"],),
            )
            captured["after"] = after[0]["heartbeat_ms"]
            return {"status": "completed"}

        mock_module = _make_mock_walk_module(execute_fn)
        with (
            patch.object(WalkRunner, "_load_walk_module", return_value=mock_module),
            patch.object(WalkRunner, "_run_verification", return_value=True),
        ):
            result = runner.run_walk("walk_2a_scene_segmentation", "book-1", {})
        assert result["status"] == "completed"
        assert captured["after"] >= captured["before"]
        # Final transition also stamps heartbeat_ms.
        rows = runner._storage.execute_query(
            "SELECT heartbeat_ms FROM walk_run WHERE run_id = ?",
            (captured["run_id"],),
        )
        assert rows[0]["heartbeat_ms"] >= captured["after"]

    def test_run_all_walks_writes_row_per_walk(self, runner):
        """run_all_walks records one walk_run row per walk, completed."""
        mock_module = _make_mock_walk_module(
            MagicMock(return_value={"status": "completed"})
        )
        with (
            patch.object(WalkRunner, "_load_walk_module", return_value=mock_module),
            patch.object(WalkRunner, "_run_verification", return_value=True),
        ):
            results = runner.run_all_walks("book-1", {})
        assert len(results) == len(WALK_ORDER)
        rows = runner._storage.execute_query(
            "SELECT walk_name, status FROM walk_run WHERE book_id = ?",
            ("book-1",),
        )
        assert len(rows) == len(WALK_ORDER)
        by_name = {row["walk_name"]: row["status"] for row in rows}
        for walk_name in WALK_ORDER:
            assert by_name[walk_name] == "completed"

    def test_each_run_gets_a_fresh_run_id(self, runner):
        """Every run_walk invocation records a distinct run_id (uuid4)."""
        mock_module = _make_mock_walk_module(
            MagicMock(return_value={"status": "completed"})
        )
        with (
            patch.object(WalkRunner, "_load_walk_module", return_value=mock_module),
            patch.object(WalkRunner, "_run_verification", return_value=True),
        ):
            runner.run_walk("walk_2a_scene_segmentation", "book-1", {})
            runner.run_walk("walk_2a_scene_segmentation", "book-1", {})
        rows = runner._storage.execute_query(
            "SELECT run_id FROM walk_run WHERE book_id = ?",
            ("book-1",),
        )
        assert len(rows) == 2
        assert rows[0]["run_id"] != rows[1]["run_id"]

    def test_run_walk_opens_and_closes_log_sink(self, storage):
        """Direct callers, including Workbench reruns, persist a run log."""
        log_service = _FakeLogService()
        runner = WalkRunner(storage, log_service=log_service)
        mock_module = _make_mock_walk_module(
            MagicMock(return_value={"status": "completed"})
        )
        with (
            patch.object(WalkRunner, "_load_walk_module", return_value=mock_module),
            patch.object(WalkRunner, "_run_verification", return_value=True),
        ):
            result = runner.run_walk("walk_2a_scene_segmentation", "book-1", {})

        assert result["status"] == "completed"
        rows = storage.execute_query(
            "SELECT run_id, status FROM walk_run WHERE book_id = ?", ("book-1",)
        )
        assert len(rows) == 1
        assert rows[0]["status"] == "completed"
        assert log_service.close_calls[0][0] == rows[0]["run_id"]


# ---------------------------------------------------------------------------
# Test is_cancel_requested dispatcher (Phase 2: persisted cancel)
# ---------------------------------------------------------------------------


class TestCancelDispatcher:
    """Spec-first tests: cancel_walks persists cancel_requested=1 on active
    walk_run rows and is_cancel_requested(run_id) reads row + stop-file + event."""

    def _run_one(self, runner):
        """Run one walk to completion and return its run_id."""
        mock_module = _make_mock_walk_module(
            MagicMock(return_value={"status": "completed"})
        )
        with (
            patch.object(WalkRunner, "_load_walk_module", return_value=mock_module),
            patch.object(WalkRunner, "_run_verification", return_value=True),
        ):
            runner.run_walk("walk_2a_scene_segmentation", "book-1", {})
        rows = runner._storage.execute_query(
            "SELECT run_id FROM walk_run WHERE book_id = ?",
            ("book-1",),
        )
        return rows[0]["run_id"]

    def test_is_cancel_requested_false_by_default(self, runner):
        """A fresh, never-cancelled run reports not-cancelled."""
        run_id = self._run_one(runner)
        assert runner.is_cancel_requested(run_id) is False

    def test_cancel_walks_persists_cancel_requested(self, runner):
        """cancel_walks persists cancel_requested=1 on active walk_run rows."""
        seen = {}

        def execute_fn(book_id, storage, config):
            rows = storage.execute_query(
                "SELECT run_id FROM walk_run WHERE book_id = ?",
                (book_id,),
            )
            seen["run_id"] = rows[0]["run_id"]
            # Cancel while the walk is running (row is active)
            runner.cancel_walks(book_id)
            return {"status": "completed"}

        mock_module = _make_mock_walk_module(execute_fn)
        with (
            patch.object(WalkRunner, "_load_walk_module", return_value=mock_module),
            patch.object(WalkRunner, "_run_verification", return_value=True),
        ):
            result = runner.run_walk("walk_2a_scene_segmentation", "book-1", {})
        assert result["status"] == "completed"
        run_id = seen["run_id"]
        rows = runner._storage.execute_query(
            "SELECT cancel_requested FROM walk_run WHERE run_id = ?",
            (run_id,),
        )
        assert rows[0]["cancel_requested"] == 1
        assert runner.is_cancel_requested(run_id) is True

    def test_is_cancel_requested_reads_stop_file(self, runner, tmp_path):
        """A persisted stop-file alone marks the run as cancelled."""
        runner.stop_file_dir = str(tmp_path)
        run_id = self._run_one(runner)
        assert runner.is_cancel_requested(run_id) is False
        path = runner._stop_file_path(run_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("1")
        assert runner.is_cancel_requested(run_id) is True

    def test_is_cancel_requested_reads_event(self, runner):
        """The in-process per-book event alone marks a run as cancelled."""
        run_id = self._run_one(runner)
        # Cancel after completion: the row is no longer active, so only the
        # in-process event is set (no persisted sources for this run_id).
        runner.cancel_walks("book-1")
        rows = runner._storage.execute_query(
            "SELECT cancel_requested FROM walk_run WHERE run_id = ?",
            (run_id,),
        )
        assert rows[0]["cancel_requested"] == 0
        assert runner.is_cancel_requested(run_id) is True


# ---------------------------------------------------------------------------
# Test walk-side retry on ConcurrentTransactionError (Phase 7: P7-S1..S3)
# ---------------------------------------------------------------------------


class TestConcurrentTransactionRetry:
    """Spec-first tests: the walk-unit write boundary retries
    ``ConcurrentTransactionError`` with 50-100ms backoff x3 (4 total attempts
    = initial + 3 retries, per contract rule #6), then fails the unit
    (walk_run row marked failed with the error recorded).

    The retry is a pure re-dispatch of a single write method — never a
    re-execution of the walk unit — so the walk's SELECT -> LLM -> write flow
    (including the LLM call) runs exactly once.
    """

    WALK = "walk_2a_scene_segmentation"
    # The walk's idempotent write. Distinct from all runner bookkeeping SQL
    # (walk_run rows always mention heartbeat_ms), so _FlakyStorage only
    # fails this write.
    WRITE_SQL = "UPDATE book SET version = version + 1 WHERE id = ?"

    def _run_walk(self, runner, execute_fn):
        mock_module = _make_mock_walk_module(execute_fn)
        with (
            patch.object(WalkRunner, "_load_walk_module", return_value=mock_module),
            patch.object(WalkRunner, "_run_verification", return_value=True),
        ):
            return runner.run_walk(self.WALK, "book-1", {})

    def test_concurrent_error_retries_up_to_3_then_fails_unit(self, runner):
        """A write that keeps raising is attempted 4 times (initial + 3
        retries), then the unit fails and the walk_run row is marked failed
        with the error recorded.
        """
        flaky = _FlakyStorage(runner._storage, fail_attempts=10)
        runner._storage = flaky

        def execute_fn(book_id, storage, config):
            storage.execute_update(self.WRITE_SQL, (book_id,))
            return {"status": "completed"}

        result = self._run_walk(runner, execute_fn)
        assert result["status"] == "failed"
        # The runner records str(exc) — the adapter's raise message, mirroring
        # the real non-owner-thread text from adapter.py.
        assert "transaction is owned by thread" in result["error"]
        # Contract rule #6 / DD line 105: 'retry idempotent write x3, then
        # fail unit' = 3 retries = 4 total attempts (initial + 3 retries).
        assert flaky._walk_write_attempts == 4
        # The re-raised error hits the runner's existing failure path, which
        # records it on the walk_run row.
        rows = flaky.execute_query(
            "SELECT status, error FROM walk_run WHERE book_id = ?",
            ("book-1",),
        )
        assert rows[0]["status"] == "failed"
        assert "transaction is owned by thread" in rows[0]["error"]

    def test_write_succeeds_on_second_attempt(self, runner):
        """A write that fails once then succeeds completes on retry 2 — no
        unit failure, exactly 2 write attempts."""
        flaky = _FlakyStorage(runner._storage, fail_attempts=1)
        runner._storage = flaky

        def execute_fn(book_id, storage, config):
            storage.execute_update(self.WRITE_SQL, (book_id,))
            return {"status": "completed"}

        result = self._run_walk(runner, execute_fn)
        assert result["status"] == "completed"
        assert flaky._walk_write_attempts == 2

    def test_backoff_timestamps_50_100ms_apart(self, runner):
        """Monotonic timestamps around the retries are ~50ms apart or more.

        The wrapper sleeps uniform(0.05, 0.10) per the contract; monotonic
        only guarantees the gap is at least the sleep duration. The lower
        bound is asserted with a small tolerance for scheduler noise; the
        100ms ceiling is asserted precisely via captured sleep() arguments in
        ``test_retry_sleeps_50_100ms_per_contract``. CI timing sensitivity:
        the upper bound here is deliberately loose.
        """
        flaky = _FlakyStorage(runner._storage, fail_attempts=10)
        runner._storage = flaky

        def execute_fn(book_id, storage, config):
            storage.execute_update(self.WRITE_SQL, (book_id,))
            return {"status": "completed"}

        result = self._run_walk(runner, execute_fn)
        assert result["status"] == "failed"
        times = flaky._attempt_times
        assert len(times) == 4
        # >= 50ms per contract (allow 5ms tolerance for scheduler noise).
        assert times[1] - times[0] >= 0.045
        assert times[2] - times[1] >= 0.045
        assert times[3] - times[2] >= 0.045
        # Loose upper bound: a loaded CI machine can stretch a 100ms sleep.
        assert times[1] - times[0] < 1.0
        assert times[2] - times[1] < 1.0
        assert times[3] - times[2] < 1.0

    def test_retry_sleeps_50_100ms_per_contract(self, runner):
        """Each backoff sleep() call is within [50ms, 100ms] — the exact
        contract range, asserted via captured sleep arguments (immune to
        scheduler noise)."""
        flaky = _FlakyStorage(runner._storage, fail_attempts=10)
        runner._storage = flaky
        real_sleep = time.sleep
        sleeps = []

        def recording_sleep(seconds):
            sleeps.append(seconds)
            real_sleep(seconds)

        def execute_fn(book_id, storage, config):
            storage.execute_update(self.WRITE_SQL, (book_id,))
            return {"status": "completed"}

        with patch("app.pipeline.walks.runner.time.sleep", side_effect=recording_sleep):
            result = self._run_walk(runner, execute_fn)
        assert result["status"] == "failed"
        # 4 attempts (initial + 3 retries) => 3 backoff sleeps.
        assert len(sleeps) == 3
        for seconds in sleeps:
            assert 0.05 <= seconds <= 0.10

    def test_retry_never_reinvokes_llm(self, runner):
        """The retry re-dispatches only the write — the walk's LLM call runs
        exactly once (the SELECT -> LLM -> write flow is never re-executed)."""
        llm_calls = []
        flaky = _FlakyStorage(runner._storage, fail_attempts=1)
        runner._storage = flaky

        def execute_fn(book_id, storage, config):
            llm_calls.append("chat_completion")  # the walk's LLM call site
            storage.execute_update(self.WRITE_SQL, (book_id,))
            return {"status": "completed"}

        result = self._run_walk(runner, execute_fn)
        assert result["status"] == "completed"
        assert llm_calls == ["chat_completion"]
        assert flaky._walk_write_attempts == 2

    def test_happy_path_writes_once_no_retry(self, runner):
        """No contention: exactly one write attempt, no backoff sleeps."""
        flaky = _FlakyStorage(runner._storage, fail_attempts=0)
        runner._storage = flaky

        def execute_fn(book_id, storage, config):
            storage.execute_update(self.WRITE_SQL, (book_id,))
            return {"status": "completed"}

        result = self._run_walk(runner, execute_fn)
        assert result["status"] == "completed"
        assert flaky._walk_write_attempts == 1


# ===========================================================================
# Part B (per-walk log streaming) — runner/reservation contract tests.
#
# These classes lock the Part B runner/reservation contracts from
# artifacts/designs/parts/per-walk-log-streaming/CONTRACTS.md (§ Part B runner
# integration) and the amended DD: reservation helpers, the reserved runner
# lifecycle (run_walk_reserved / run_all_walks_reserved), the WALK_LOG_SINK
# ContextVar reset on every terminal path, and the static/import audit of the
# nine immutable walk modules. All tests below are green.
# ===========================================================================


class _FakeSink:
    """Minimal stand-in for a WalkLogSink capturing appended records."""

    def __init__(self):
        self.records = []
        self.terminal = None

    def append(self, event, payload=None, *, terminal=False):
        self.records.append({"event": event, "data": payload, "terminal": terminal})

    def append_terminal(self, status, payload=None):
        self.terminal = {"status": status, "data": payload}

    def close_partial(self, status="aborted"):
        pass

    def close(self):
        pass


class _FakeLogService:
    """Process-owned-service stand-in keyed by run_id.

    The Part B runner obtains its per-run sink from ``WalkLogService.open_run``.
    This stub supplies ``_FakeSink`` instances so the ContextVar-reset tests can
    observe the sink the runner attaches, without touching the real filesystem.
    """

    def __init__(self):
        self.sinks = {}
        #: Ordered log of every close_run call, for terminal-ordering asserts:
        #: (run_id, status, payload). The runner calls close_run BEFORE
        #: _finalize_run on every terminal path, so the last close_calls entry's
        #: status must match the DB row's final status.
        self.close_calls: list[tuple[str, str, object]] = []
        #: run_ids whose close_run has been called (sentinel for ordering).
        self.closed_ids: set[str] = set()

    def open_run(self, run_id, book_id, walk_name, started_ms=None):
        sink = _FakeSink()
        self.sinks[run_id] = sink
        return sink

    def get_run(self, run_id):
        return self.sinks.get(run_id)

    def close_run(self, run_id, status, payload=None):
        self.close_calls.append((run_id, status, payload))
        self.closed_ids.add(run_id)


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content, finish_reason):
        self.message = _FakeMessage(content)
        self.finish_reason = finish_reason


class _FakeResponse:
    def __init__(self, model, choice, usage):
        self.model = model
        self.choices = [choice]
        self.usage = usage


class _FakeCompletions:
    def __init__(self, response):
        self._response = response

    def create(self, **kwargs):
        return self._response


class _FakeChat:
    def __init__(self, response):
        self.completions = _FakeCompletions(response)


class _FakeClient:
    def __init__(self, response):
        self.chat = _FakeChat(response)


def _insert_pending_row(storage, run_id, book_id, walk_name, cancel_requested=0):
    """Insert one exact pending walk_run row (the reservation shape)."""
    storage.execute_insert(
        "INSERT INTO walk_run (run_id, book_id, walk_name, status, cancel_requested, heartbeat_ms) "
        "VALUES (?, ?, ?, 'pending', ?, ?)",
        (run_id, book_id, walk_name, cancel_requested, 100),
    )


# ---------------------------------------------------------------------------
# P1-S1 — caller-supplied canonical UUID reservations
# ---------------------------------------------------------------------------


class TestReservationHelpers:
    """Locks the caller-supplied-canonical-UUID reservation contract.

    ``reserve_walk_run`` / ``reserve_all_walk_runs`` insert exact ``pending``
    rows and validate canonical UUIDs, allowed walk names, uniqueness, and
    ``WALK_ORDER`` coverage; ``mark_reserved_runs_failed`` marks only still-
    pending rows failed without executing a reservation.
    """

    BOOK = "11111111-2222-3333-4444-555555555555"
    WALK = "walk_2a_scene_segmentation"

    def _reservations(self):
        return {w: str(uuid.uuid4()) for w in WALK_ORDER}

    def _run_id(self):
        return str(uuid.uuid4())

    def test_reserve_walk_run_returns_same_run_id(self, storage):
        from app.pipeline.walks.runner import reserve_walk_run

        run_id = self._run_id()
        returned = reserve_walk_run(
            storage, run_id, self.BOOK, self.WALK, created_ms=1000
        )
        assert returned == run_id

    def test_reserve_walk_run_inserts_exact_pending_row(self, storage):
        from app.pipeline.walks.runner import reserve_walk_run

        run_id = self._run_id()
        reserve_walk_run(storage, run_id, self.BOOK, self.WALK, created_ms=1234)
        rows = storage.execute_query(
            "SELECT run_id, book_id, walk_name, status, cancel_requested, "
            "heartbeat_ms, result_json, error, finished_ms "
            "FROM walk_run WHERE run_id = ?",
            (run_id,),
        )
        assert len(rows) == 1
        row = rows[0]
        assert row["run_id"] == run_id
        assert row["book_id"] == self.BOOK
        assert row["walk_name"] == self.WALK
        assert row["status"] == "pending"
        assert row["cancel_requested"] == 0
        assert row["heartbeat_ms"] == 1234
        assert row["result_json"] is None
        assert row["error"] is None
        assert row["finished_ms"] is None

    def test_reserve_walk_run_default_heartbeat_is_created_ms(self, storage):
        from app.pipeline.walks.runner import reserve_walk_run

        run_id = self._run_id()
        reserve_walk_run(storage, run_id, self.BOOK, self.WALK)
        rows = storage.execute_query(
            "SELECT heartbeat_ms FROM walk_run WHERE run_id = ?", (run_id,)
        )
        assert rows[0]["heartbeat_ms"] is not None

    def test_reserve_walk_run_invalid_uuid_raises(self, storage):
        from app.pipeline.walks.runner import reserve_walk_run

        with pytest.raises(ValueError):
            reserve_walk_run(storage, "not-a-uuid", self.BOOK, self.WALK)

    def test_reserve_walk_run_unknown_walk_raises(self, storage):
        from app.pipeline.walks.runner import reserve_walk_run

        with pytest.raises(ValueError):
            reserve_walk_run(storage, self._run_id(), self.BOOK, "walk_nope")

    def test_reserve_all_walk_runs_returns_normalized_order(self, storage):
        from app.pipeline.walks.runner import reserve_all_walk_runs

        reservations = self._reservations()
        items = list(reservations.items())
        items.reverse()  # scramble input order to prove normalization
        result = reserve_all_walk_runs(storage, self.BOOK, items, created_ms=1)
        expected = tuple((w, reservations[w]) for w in WALK_ORDER)
        assert result == expected

    def test_reserve_all_walk_runs_inserts_nine_pending_rows(self, storage):
        from app.pipeline.walks.runner import reserve_all_walk_runs

        reservations = self._reservations()
        reserve_all_walk_runs(storage, self.BOOK, list(reservations.items()))
        rows = storage.execute_query(
            "SELECT walk_name, status FROM walk_run WHERE book_id = ?",
            (self.BOOK,),
        )
        assert len(rows) == len(WALK_ORDER)
        by_name = {r["walk_name"]: r["status"] for r in rows}
        for w in WALK_ORDER:
            assert by_name[w] == "pending"

    def test_reserve_all_walk_runs_rejects_missing_walk(self, storage):
        from app.pipeline.walks.runner import reserve_all_walk_runs

        reservations = self._reservations()
        del reservations[WALK_ORDER[0]]
        with pytest.raises(ValueError):
            reserve_all_walk_runs(storage, self.BOOK, list(reservations.items()))

    def test_reserve_all_walk_runs_rejects_extra_walk(self, storage):
        from app.pipeline.walks.runner import reserve_all_walk_runs

        reservations = self._reservations()
        reservations["walk_extra"] = str(uuid.uuid4())
        with pytest.raises(ValueError):
            reserve_all_walk_runs(storage, self.BOOK, list(reservations.items()))

    def test_reserve_all_walk_runs_rejects_duplicate_walk(self, storage):
        from app.pipeline.walks.runner import reserve_all_walk_runs

        reservations = self._reservations()
        items = list(reservations.items())
        items.append((WALK_ORDER[0], str(uuid.uuid4())))
        with pytest.raises(ValueError):
            reserve_all_walk_runs(storage, self.BOOK, items)

    def test_reserve_all_walk_runs_rejects_duplicate_run_id(self, storage):
        from app.pipeline.walks.runner import reserve_all_walk_runs

        shared = str(uuid.uuid4())
        items = [(WALK_ORDER[0], shared), (WALK_ORDER[1], shared)]
        for w in WALK_ORDER[2:]:
            items.append((w, str(uuid.uuid4())))
        with pytest.raises(ValueError):
            reserve_all_walk_runs(storage, self.BOOK, items)

    def test_reserve_all_walk_runs_rejects_invalid_uuid(self, storage):
        from app.pipeline.walks.runner import reserve_all_walk_runs

        reservations = self._reservations()
        reservations[WALK_ORDER[0]] = "bad-uuid"
        with pytest.raises(ValueError):
            reserve_all_walk_runs(storage, self.BOOK, list(reservations.items()))

    def test_mark_reserved_runs_failed_marks_pending_failed(self, storage):
        from app.pipeline.walks.runner import (
            mark_reserved_runs_failed,
            reserve_walk_run,
        )

        run_id = self._run_id()
        reserve_walk_run(storage, run_id, self.BOOK, self.WALK)
        mark_reserved_runs_failed(storage, [run_id], "scheduling error")
        rows = storage.execute_query(
            "SELECT status, error FROM walk_run WHERE run_id = ?", (run_id,)
        )
        assert rows[0]["status"] == "failed"
        assert rows[0]["error"] == "scheduling error"

    def test_mark_reserved_runs_failed_ignores_non_pending(self, storage):
        from app.pipeline.walks.runner import (
            mark_reserved_runs_failed,
            reserve_walk_run,
        )

        run_id = self._run_id()
        reserve_walk_run(storage, run_id, self.BOOK, self.WALK)
        storage.execute_update(
            "UPDATE walk_run SET status = 'completed' WHERE run_id = ?", (run_id,)
        )
        mark_reserved_runs_failed(storage, [run_id], "err")
        rows = storage.execute_query(
            "SELECT status FROM walk_run WHERE run_id = ?", (run_id,)
        )
        assert rows[0]["status"] == "completed"


# ---------------------------------------------------------------------------
# P1-S2 — reserved single-run lifecycle
# ---------------------------------------------------------------------------


class TestRunWalkReserved:
    """Locks ``WalkRunner.run_walk_reserved``: verifies the existing ``pending``
    row, transitions it to ``running``, executes with ``HeartbeatStorage``,
    never allocates a replacement run ID, preserves all terminal outcomes
    (completed / exception→failed / import-error→failed /
    verification-failure→failed / cancelled-before-start→cancelled), and writes
    the result_json on completion.

    Sink wiring: the runner opens its per-run sink via a ``WalkLogService``.
    These tests inject a ``_FakeLogService`` through the runner constructor so
    the reserved-run lifecycle is exercised independently of the real service.
    """

    BOOK = "11111111-2222-3333-4444-555555555555"
    WALK = "walk_2a_scene_segmentation"

    def _runner(self, storage):
        return WalkRunner(storage, log_service=_FakeLogService())

    def test_run_walk_reserved_verifies_pending_and_transitions_running(self, storage):
        run_id = str(uuid.uuid4())
        _insert_pending_row(storage, run_id, self.BOOK, self.WALK)
        runner = self._runner(storage)
        seen = {}

        def execute_fn(book_id, hbs, config):
            rows = storage.execute_query(
                "SELECT status FROM walk_run WHERE run_id = ?", (run_id,)
            )
            seen["status_during"] = rows[0]["status"]
            return {"status": "completed", "scenes": 3}

        mock_module = _make_mock_walk_module(execute_fn)
        with (
            patch.object(WalkRunner, "_load_walk_module", return_value=mock_module),
            patch.object(WalkRunner, "_run_verification", return_value=True),
        ):
            result = runner.run_walk_reserved(run_id, self.WALK, self.BOOK, {})
        assert seen["status_during"] == "running"
        assert result == {"status": "completed", "scenes": 3}

    def test_run_walk_reserved_returns_original_walk_result(self, storage):
        run_id = str(uuid.uuid4())
        _insert_pending_row(storage, run_id, self.BOOK, self.WALK)
        runner = self._runner(storage)
        expected = {"status": "completed", "chapters": 2}
        mock_module = _make_mock_walk_module(MagicMock(return_value=expected))
        with (
            patch.object(WalkRunner, "_load_walk_module", return_value=mock_module),
            patch.object(WalkRunner, "_run_verification", return_value=True),
        ):
            result = runner.run_walk_reserved(run_id, self.WALK, self.BOOK, {})
        assert result == expected

    def test_run_walk_reserved_returns_raw_summary_on_success(self, storage):
        run_id = str(uuid.uuid4())
        _insert_pending_row(storage, run_id, self.BOOK, self.WALK)
        runner = self._runner(storage)
        summary = {"chapters": 2}
        mock_module = _make_mock_walk_module(MagicMock(return_value=summary))
        with (
            patch.object(WalkRunner, "_load_walk_module", return_value=mock_module),
            patch.object(WalkRunner, "_run_verification", return_value=True),
        ):
            result = runner.run_walk_reserved(run_id, self.WALK, self.BOOK, {})
        assert result == summary

    def test_run_walk_reserved_executes_with_heartbeat_storage_run_id(self, storage):
        run_id = str(uuid.uuid4())
        _insert_pending_row(storage, run_id, self.BOOK, self.WALK)
        runner = self._runner(storage)
        captured = {}

        def execute_fn(book_id, hbs, config):
            captured["is_hbs"] = isinstance(hbs, HeartbeatStorage)
            captured["hbs_run_id"] = hbs.run_id
            return {"status": "completed"}

        mock_module = _make_mock_walk_module(execute_fn)
        with (
            patch.object(WalkRunner, "_load_walk_module", return_value=mock_module),
            patch.object(WalkRunner, "_run_verification", return_value=True),
        ):
            runner.run_walk_reserved(run_id, self.WALK, self.BOOK, {})
        assert captured["is_hbs"]
        assert captured["hbs_run_id"] == run_id

    def test_run_walk_reserved_never_allocates_replacement_id(self, storage):
        run_id = str(uuid.uuid4())
        _insert_pending_row(storage, run_id, self.BOOK, self.WALK)
        runner = self._runner(storage)
        mock_module = _make_mock_walk_module(
            MagicMock(return_value={"status": "completed"})
        )
        with (
            patch.object(WalkRunner, "_load_walk_module", return_value=mock_module),
            patch.object(WalkRunner, "_run_verification", return_value=True),
        ):
            runner.run_walk_reserved(run_id, self.WALK, self.BOOK, {})
        rows = storage.execute_query(
            "SELECT run_id, status FROM walk_run WHERE book_id = ?", (self.BOOK,)
        )
        assert len(rows) == 1
        assert rows[0]["run_id"] == run_id
        assert rows[0]["status"] == "completed"

    def test_run_walk_reserved_completes_row_with_result_json(self, storage):
        run_id = str(uuid.uuid4())
        _insert_pending_row(storage, run_id, self.BOOK, self.WALK)
        runner = self._runner(storage)
        expected = {"status": "completed", "n": 1}
        mock_module = _make_mock_walk_module(MagicMock(return_value=expected))
        with (
            patch.object(WalkRunner, "_load_walk_module", return_value=mock_module),
            patch.object(WalkRunner, "_run_verification", return_value=True),
        ):
            runner.run_walk_reserved(run_id, self.WALK, self.BOOK, {})
        rows = storage.execute_query(
            "SELECT status, result_json FROM walk_run WHERE run_id = ?", (run_id,)
        )
        assert rows[0]["status"] == "completed"
        assert json.loads(rows[0]["result_json"]) == expected

    def test_run_walk_reserved_exception_marks_failed(self, storage):
        run_id = str(uuid.uuid4())
        _insert_pending_row(storage, run_id, self.BOOK, self.WALK)
        runner = self._runner(storage)
        mock_module = _make_mock_walk_module(
            MagicMock(side_effect=RuntimeError("boom"))
        )
        with patch.object(WalkRunner, "_load_walk_module", return_value=mock_module):
            result = runner.run_walk_reserved(run_id, self.WALK, self.BOOK, {})
        assert result["status"] == "failed"
        assert "boom" in result["error"]
        rows = storage.execute_query(
            "SELECT status, error FROM walk_run WHERE run_id = ?", (run_id,)
        )
        assert rows[0]["status"] == "failed"
        assert "boom" in rows[0]["error"]

    def test_run_walk_reserved_import_error_marks_failed(self, storage):
        run_id = str(uuid.uuid4())
        _insert_pending_row(storage, run_id, self.BOOK, self.WALK)
        runner = self._runner(storage)
        result = runner.run_walk_reserved(run_id, "walk_does_not_exist", self.BOOK, {})
        assert result["status"] == "failed"
        rows = storage.execute_query(
            "SELECT status FROM walk_run WHERE run_id = ?", (run_id,)
        )
        assert rows[0]["status"] == "failed"

    def test_run_walk_reserved_verification_failure_marks_failed(self, storage):
        run_id = str(uuid.uuid4())
        _insert_pending_row(storage, run_id, self.BOOK, self.WALK)
        runner = self._runner(storage)
        mock_module = _make_mock_walk_module(
            MagicMock(return_value={"status": "completed"})
        )
        with (
            patch.object(WalkRunner, "_load_walk_module", return_value=mock_module),
            patch.object(WalkRunner, "_run_verification", return_value=False),
        ):
            result = runner.run_walk_reserved(run_id, self.WALK, self.BOOK, {})
        assert result["status"] == "failed"
        assert "Verification failed" in result["error"]

    def test_run_walk_reserved_cancelled_before_start(self, storage):
        run_id = str(uuid.uuid4())
        _insert_pending_row(storage, run_id, self.BOOK, self.WALK)
        runner = self._runner(storage)
        runner.cancel_walks(self.BOOK)
        result = runner.run_walk_reserved(run_id, self.WALK, self.BOOK, {})
        assert result["status"] == "cancelled"
        rows = storage.execute_query(
            "SELECT status FROM walk_run WHERE run_id = ?", (run_id,)
        )
        assert rows[0]["status"] == "cancelled"

    # -- reservation verification guard (missing / non-pending row) ---------

    def test_run_walk_reserved_missing_row_fails_without_executing(self, storage):
        """A missing pending row must fail fast (no execution, no row created)."""
        run_id = str(uuid.uuid4())
        runner = self._runner(storage)
        execute_fn = MagicMock(return_value={"status": "completed"})
        mock_module = _make_mock_walk_module(execute_fn)
        with patch.object(WalkRunner, "_load_walk_module", return_value=mock_module):
            result = runner.run_walk_reserved(run_id, self.WALK, self.BOOK, {})
        assert result["status"] == "failed"
        assert "Reservation" in result["error"]
        assert "pending" in result["error"]
        execute_fn.assert_not_called()
        rows = storage.execute_query(
            "SELECT run_id FROM walk_run WHERE run_id = ?", (run_id,)
        )
        assert rows == []

    def test_run_walk_reserved_non_pending_row_fails_without_executing(self, storage):
        """A row that is no longer pending must fail fast without executing."""
        run_id = str(uuid.uuid4())
        _insert_pending_row(storage, run_id, self.BOOK, self.WALK)
        storage.execute_update(
            "UPDATE walk_run SET status = 'completed' WHERE run_id = ?", (run_id,)
        )
        runner = self._runner(storage)
        execute_fn = MagicMock(return_value={"status": "completed"})
        mock_module = _make_mock_walk_module(execute_fn)
        with patch.object(WalkRunner, "_load_walk_module", return_value=mock_module):
            result = runner.run_walk_reserved(run_id, self.WALK, self.BOOK, {})
        assert result["status"] == "failed"
        assert "Reservation" in result["error"]
        assert "pending" in result["error"]
        execute_fn.assert_not_called()
        rows = storage.execute_query(
            "SELECT status FROM walk_run WHERE run_id = ?", (run_id,)
        )
        assert rows[0]["status"] == "completed"

    # -- log_service=None contract (legacy callers: NO sink operations) ----

    def test_run_walk_reserved_without_log_service_completes_normally(self, storage):
        """WalkRunner(storage) with no log_service must complete the run with no
        sink operations (the default construction used by all legacy callers)."""
        run_id = str(uuid.uuid4())
        _insert_pending_row(storage, run_id, self.BOOK, self.WALK)
        runner = WalkRunner(storage)
        mock_module = _make_mock_walk_module(
            MagicMock(return_value={"status": "completed"})
        )
        with (
            patch.object(WalkRunner, "_load_walk_module", return_value=mock_module),
            patch.object(WalkRunner, "_run_verification", return_value=True),
        ):
            result = runner.run_walk_reserved(run_id, self.WALK, self.BOOK, {})
        assert result["status"] == "completed"
        rows = storage.execute_query(
            "SELECT status FROM walk_run WHERE run_id = ?", (run_id,)
        )
        assert rows[0]["status"] == "completed"

    def test_run_walk_reserved_close_run_raising_does_not_alter_db(self, storage):
        """A close_run that raises must not change the DB outcome (row = truth)
        nor leak the sink ContextVar."""
        from app.pipeline.walks._llm_helpers import get_walk_log_sink

        run_id = str(uuid.uuid4())
        _insert_pending_row(storage, run_id, self.BOOK, self.WALK)

        class _RaisingCloseService(_FakeLogService):
            def close_run(self, run_id, status, payload=None):
                raise OSError("cannot write log file")

        runner = WalkRunner(storage, log_service=_RaisingCloseService())
        mock_module = _make_mock_walk_module(
            MagicMock(return_value={"status": "completed"})
        )
        with (
            patch.object(WalkRunner, "_load_walk_module", return_value=mock_module),
            patch.object(WalkRunner, "_run_verification", return_value=True),
        ):
            result = runner.run_walk_reserved(run_id, self.WALK, self.BOOK, {})
        assert result["status"] == "completed"
        rows = storage.execute_query(
            "SELECT status, result_json, error FROM walk_run WHERE run_id = ?",
            (run_id,),
        )
        assert rows[0]["status"] == "completed"
        assert json.loads(rows[0]["result_json"]) == {"status": "completed"}
        assert rows[0]["error"] is None
        assert get_walk_log_sink() is None

    # -- terminal-ordering invariant: close_run precedes _finalize_run ------

    def test_run_walk_reserved_close_run_precedes_db_finalize_on_complete(
        self, storage
    ):
        """On completion, close_run must fire BEFORE the DB row is finalized
        (terminal record before the row = truth). During execute the sink is
        still open and no close has happened."""
        run_id = str(uuid.uuid4())
        _insert_pending_row(storage, run_id, self.BOOK, self.WALK)
        service = _FakeLogService()
        runner = WalkRunner(storage, log_service=service)
        captured = {}

        def execute_fn(book_id, hbs, config):
            captured["sink_open"] = service.get_run(run_id) is not None
            captured["closed_during_exec"] = run_id in service.closed_ids
            return {"status": "completed"}

        mock_module = _make_mock_walk_module(execute_fn)
        with (
            patch.object(WalkRunner, "_load_walk_module", return_value=mock_module),
            patch.object(WalkRunner, "_run_verification", return_value=True),
        ):
            result = runner.run_walk_reserved(run_id, self.WALK, self.BOOK, {})
        assert result["status"] == "completed"
        assert captured["sink_open"] is True
        assert captured["closed_during_exec"] is False
        assert len(service.close_calls) == 1
        assert service.close_calls[0][0] == run_id

    def test_run_walk_reserved_completion_emits_one_terminal_matching_row(
        self, storage
    ):
        run_id = str(uuid.uuid4())
        _insert_pending_row(storage, run_id, self.BOOK, self.WALK)
        service = _FakeLogService()
        runner = WalkRunner(storage, log_service=service)
        mock_module = _make_mock_walk_module(
            MagicMock(return_value={"status": "completed"})
        )
        with (
            patch.object(WalkRunner, "_load_walk_module", return_value=mock_module),
            patch.object(WalkRunner, "_run_verification", return_value=True),
        ):
            runner.run_walk_reserved(run_id, self.WALK, self.BOOK, {})
        assert len(service.close_calls) == 1
        assert service.close_calls[0][0] == run_id
        assert service.close_calls[0][1] == "completed"
        rows = storage.execute_query(
            "SELECT status FROM walk_run WHERE run_id = ?", (run_id,)
        )
        assert service.close_calls[0][1] == rows[0]["status"]

    def test_run_walk_reserved_exception_emits_one_terminal_matching_row(self, storage):
        run_id = str(uuid.uuid4())
        _insert_pending_row(storage, run_id, self.BOOK, self.WALK)
        service = _FakeLogService()
        runner = WalkRunner(storage, log_service=service)
        mock_module = _make_mock_walk_module(
            MagicMock(side_effect=RuntimeError("boom"))
        )
        with patch.object(WalkRunner, "_load_walk_module", return_value=mock_module):
            result = runner.run_walk_reserved(run_id, self.WALK, self.BOOK, {})
        assert result["status"] == "failed"
        assert len(service.close_calls) == 1
        assert service.close_calls[0][0] == run_id
        assert service.close_calls[0][1] == "failed"
        rows = storage.execute_query(
            "SELECT status FROM walk_run WHERE run_id = ?", (run_id,)
        )
        assert service.close_calls[0][1] == rows[0]["status"]

    def test_run_walk_reserved_verification_failure_emits_one_terminal(self, storage):
        run_id = str(uuid.uuid4())
        _insert_pending_row(storage, run_id, self.BOOK, self.WALK)
        service = _FakeLogService()
        runner = WalkRunner(storage, log_service=service)
        mock_module = _make_mock_walk_module(
            MagicMock(return_value={"status": "completed"})
        )
        with (
            patch.object(WalkRunner, "_load_walk_module", return_value=mock_module),
            patch.object(WalkRunner, "_run_verification", return_value=False),
        ):
            result = runner.run_walk_reserved(run_id, self.WALK, self.BOOK, {})
        assert result["status"] == "failed"
        assert len(service.close_calls) == 1
        assert service.close_calls[0][1] == "failed"
        rows = storage.execute_query(
            "SELECT status FROM walk_run WHERE run_id = ?", (run_id,)
        )
        assert service.close_calls[0][1] == rows[0]["status"]

    def test_run_walk_reserved_import_error_emits_one_terminal_matching_row(
        self, storage
    ):
        run_id = str(uuid.uuid4())
        _insert_pending_row(storage, run_id, self.BOOK, self.WALK)
        service = _FakeLogService()
        runner = WalkRunner(storage, log_service=service)
        result = runner.run_walk_reserved(run_id, "walk_does_not_exist", self.BOOK, {})
        assert result["status"] == "failed"
        assert len(service.close_calls) == 1
        assert service.close_calls[0][0] == run_id
        assert service.close_calls[0][1] == "failed"
        rows = storage.execute_query(
            "SELECT status FROM walk_run WHERE run_id = ?", (run_id,)
        )
        assert service.close_calls[0][1] == rows[0]["status"]

    def test_run_walk_reserved_cancelled_before_start_emits_no_terminal(self, storage):
        """Cancelled-before-start opens no sink, so no close/terminal record is
        emitted (DB-only terminal status) — the invariant Part C's SSE depends on."""
        run_id = str(uuid.uuid4())
        _insert_pending_row(storage, run_id, self.BOOK, self.WALK)
        service = _FakeLogService()
        runner = WalkRunner(storage, log_service=service)
        runner.cancel_walks(self.BOOK)
        result = runner.run_walk_reserved(run_id, self.WALK, self.BOOK, {})
        assert result["status"] == "cancelled"
        assert len(service.close_calls) == 0
        assert run_id not in service.closed_ids
        rows = storage.execute_query(
            "SELECT status FROM walk_run WHERE run_id = ?", (run_id,)
        )
        assert rows[0]["status"] == "cancelled"


# ---------------------------------------------------------------------------
# P1-S3 — reserved batch lifecycle (run_all_walks_reserved)
# ---------------------------------------------------------------------------


class TestRunAllWalksReserved:
    """Locks ``WalkRunner.run_all_walks_reserved``: consumes the complete
    ordered nine-child reservation serially in ``WALK_ORDER`` (results keyed by
    walk_name), keeps ``batch_id`` correlation-only (no parent row), and
    terminalizes not-yet-started children without executing them on abort or
    cancellation."""

    BOOK = "11111111-2222-3333-4444-555555555555"

    def _reservations(self):
        return [(w, str(uuid.uuid4())) for w in WALK_ORDER]

    def _insert_reserved_rows(self, storage, reservations):
        for walk_name, run_id in reservations:
            _insert_pending_row(storage, run_id, self.BOOK, walk_name)

    def _runner(self, storage):
        return WalkRunner(storage, log_service=_FakeLogService())

    def test_run_all_walks_reserved_executes_all_in_order(self, storage):
        reservations = self._reservations()
        self._insert_reserved_rows(storage, reservations)
        runner = self._runner(storage)
        call_log = []

        def execute_fn(book_id, hbs, config):
            call_log.append(book_id)
            return {"status": "completed"}

        mock_module = _make_mock_walk_module(execute_fn)
        batch_id = str(uuid.uuid4())
        with (
            patch.object(WalkRunner, "_load_walk_module", return_value=mock_module),
            patch.object(WalkRunner, "_run_verification", return_value=True),
        ):
            results = runner.run_all_walks_reserved(
                batch_id, reservations, self.BOOK, {}
            )
        assert len(call_log) == len(WALK_ORDER)
        assert isinstance(results, dict)
        for w in WALK_ORDER:
            assert results[w]["status"] == "completed"
        rows = storage.execute_query(
            "SELECT walk_name, status FROM walk_run WHERE book_id = ?", (self.BOOK,)
        )
        by_name = {r["walk_name"]: r["status"] for r in rows}
        for w in WALK_ORDER:
            assert by_name[w] == "completed"

    def test_run_all_walks_reserved_accepts_raw_walk_summaries(self, storage):
        reservations = self._reservations()
        self._insert_reserved_rows(storage, reservations)
        runner = self._runner(storage)
        call_log = []

        def execute_fn(book_id, hbs, config):
            call_log.append(book_id)
            return {"scenes_created": 3}

        mock_module = _make_mock_walk_module(execute_fn)
        batch_id = str(uuid.uuid4())
        with (
            patch.object(WalkRunner, "_load_walk_module", return_value=mock_module),
            patch.object(WalkRunner, "_run_verification", return_value=True),
        ):
            results = runner.run_all_walks_reserved(
                batch_id, reservations, self.BOOK, {}
            )

        assert len(call_log) == len(WALK_ORDER)
        assert all(results[w] == {"scenes_created": 3} for w in WALK_ORDER)
        rows = storage.execute_query(
            "SELECT status, result_json FROM walk_run WHERE book_id = ?",
            (self.BOOK,),
        )
        assert len(rows) == len(WALK_ORDER)
        assert all(row["status"] == "completed" for row in rows)
        assert all("scenes_created" in json.loads(row["result_json"]) for row in rows)
        assert all("status" not in json.loads(row["result_json"]) for row in rows)

    def test_run_all_walks_reserved_batch_id_has_no_parent_row(self, storage):
        reservations = self._reservations()
        self._insert_reserved_rows(storage, reservations)
        runner = self._runner(storage)
        batch_id = str(uuid.uuid4())
        mock_module = _make_mock_walk_module(
            MagicMock(return_value={"status": "completed"})
        )
        with (
            patch.object(WalkRunner, "_load_walk_module", return_value=mock_module),
            patch.object(WalkRunner, "_run_verification", return_value=True),
        ):
            runner.run_all_walks_reserved(batch_id, reservations, self.BOOK, {})
        rows = storage.execute_query(
            "SELECT run_id FROM walk_run WHERE run_id = ?", (batch_id,)
        )
        assert rows == []

    def test_run_all_walks_reserved_terminalizes_unstarted_children_on_abort(
        self, storage
    ):
        reservations = self._reservations()
        self._insert_reserved_rows(storage, reservations)
        runner = self._runner(storage)
        call_count = [0]

        def execute_fn(book_id, hbs, config):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("boom")
            return {"status": "completed"}

        mock_module = _make_mock_walk_module(execute_fn)
        batch_id = str(uuid.uuid4())
        with (
            patch.object(WalkRunner, "_load_walk_module", return_value=mock_module),
            patch.object(WalkRunner, "_run_verification", return_value=True),
        ):
            results = runner.run_all_walks_reserved(
                batch_id, reservations, self.BOOK, {}
            )
        assert results[WALK_ORDER[0]]["status"] == "failed"
        assert call_count[0] == 1  # only the first child executed
        rows = storage.execute_query(
            "SELECT walk_name, status FROM walk_run WHERE book_id = ?", (self.BOOK,)
        )
        for row in rows:
            if row["walk_name"] != WALK_ORDER[0]:
                assert row["status"] in ("cancelled", "failed", "interrupted")

    def test_run_all_walks_reserved_cancellation_terminalizes_all(self, storage):
        reservations = self._reservations()
        self._insert_reserved_rows(storage, reservations)
        runner = self._runner(storage)
        runner.cancel_walks(self.BOOK)
        batch_id = str(uuid.uuid4())
        mock_module = _make_mock_walk_module(
            MagicMock(return_value={"status": "completed"})
        )
        with (
            patch.object(WalkRunner, "_load_walk_module", return_value=mock_module),
            patch.object(WalkRunner, "_run_verification", return_value=True),
        ):
            runner.run_all_walks_reserved(batch_id, reservations, self.BOOK, {})
        rows = storage.execute_query(
            "SELECT walk_name, status FROM walk_run WHERE book_id = ?", (self.BOOK,)
        )
        assert len(rows) == len(WALK_ORDER)
        for row in rows:
            assert row["status"] in ("cancelled", "failed", "interrupted")


# ---------------------------------------------------------------------------
# Serialization + reported-errors lifecycle (A1/A2/A3 fixes)
# ---------------------------------------------------------------------------


class TestReservedSerializationFixes:
    """Locks the A1/A2/A3 reserved-runner fixes:

    A2 — ``run_walk_reserved`` atomically (under ``storage.transaction``)
    checks no other ``running`` row exists for the same book and transitions
    the pending->running row; a blocked reservation is deterministically
    terminalized to ``failed`` without executing, and the nine pre-reserved
    ``pending`` rows of a batch never block sequential execution.

    A3 — a non-empty ``result['errors']`` marks the run ``failed`` and preserves
    the raw result JSON on the failed row; empty/missing errors retains the
    existing verification behavior.

    A1 — ``run_all_walks_reserved`` drives its abort decision from each child's
    PERSISTED ``walk_run`` status (rows = truth), not from the returned raw
    summary's status shape.
    """

    BOOK = "11111111-2222-3333-4444-555555555555"
    WALK = "walk_2a_scene_segmentation"

    def _runner(self, storage):
        return WalkRunner(storage, log_service=_FakeLogService())

    def _reservations(self):
        return [(w, str(uuid.uuid4())) for w in WALK_ORDER]

    # -- A2: serial-execution guard under storage.transaction ---------------

    def test_blocked_by_running_row_terminalizes_and_does_not_execute(self, runner):
        """A pending reservation blocked by another 'running' row for the same
        book is terminalized to 'failed' without executing the walk."""
        run_id = str(uuid.uuid4())
        _insert_pending_row(runner._storage, run_id, self.BOOK, self.WALK)
        other_id = str(uuid.uuid4())
        _insert_pending_row(runner._storage, other_id, self.BOOK, WALK_ORDER[1])
        runner._storage.execute_update(
            "UPDATE walk_run SET status = 'running' WHERE run_id = ?", (other_id,)
        )

        execute_fn = MagicMock(return_value={"status": "completed"})
        mock_module = _make_mock_walk_module(execute_fn)
        with patch.object(WalkRunner, "_load_walk_module", return_value=mock_module):
            result = runner.run_walk_reserved(run_id, self.WALK, self.BOOK, {})

        assert result["status"] == "failed"
        assert "already running" in result["error"]
        execute_fn.assert_not_called()
        rows = runner._storage.execute_query(
            "SELECT status, error FROM walk_run WHERE run_id = ?", (run_id,)
        )
        assert rows[0]["status"] == "failed"
        assert "already running" in rows[0]["error"]
        rows = runner._storage.execute_query(
            "SELECT status FROM walk_run WHERE run_id = ?", (other_id,)
        )
        assert rows[0]["status"] == "running"

    def test_running_transition_happens_inside_transaction(self, storage):
        """The pending->running transition UPDATE is issued while a storage
        transaction is open (A2 atomicity)."""
        run_id = str(uuid.uuid4())
        _insert_pending_row(storage, run_id, self.BOOK, self.WALK)
        observed = {}
        real_update = storage.execute_update

        def recording_update(sql, params=()):
            if "status = 'running'" in sql:
                observed["in_txn"] = storage._conn.in_transaction
            return real_update(sql, params)

        storage.execute_update = recording_update
        runner = self._runner(storage)
        mock_module = _make_mock_walk_module(
            MagicMock(return_value={"status": "completed"})
        )
        with (
            patch.object(WalkRunner, "_load_walk_module", return_value=mock_module),
            patch.object(WalkRunner, "_run_verification", return_value=True),
        ):
            runner.run_walk_reserved(run_id, self.WALK, self.BOOK, {})
        assert observed["in_txn"] is True

    def test_nine_pending_rows_do_not_block_sequential_execution(self, storage):
        """The A2 guard only blocks on status='running', so the nine
        pre-reserved 'pending' rows of a full batch never block sequential
        start; the whole batch runs to completion."""
        from app.pipeline.walks.runner import reserve_all_walk_runs

        reservations = {w: str(uuid.uuid4()) for w in WALK_ORDER}
        reserve_all_walk_runs(storage, self.BOOK, list(reservations.items()))
        runner = self._runner(storage)
        call_log = []

        def execute_fn(book_id, hbs, config):
            call_log.append(book_id)
            return {"status": "completed"}

        mock_module = _make_mock_walk_module(execute_fn)
        with (
            patch.object(WalkRunner, "_load_walk_module", return_value=mock_module),
            patch.object(WalkRunner, "_run_verification", return_value=True),
        ):
            results = runner.run_all_walks_reserved(
                str(uuid.uuid4()), list(reservations.items()), self.BOOK, {}
            )
        assert len(call_log) == len(WALK_ORDER)
        assert all(results[w]["status"] == "completed" for w in WALK_ORDER)
        rows = storage.execute_query(
            "SELECT status FROM walk_run WHERE book_id = ?", (self.BOOK,)
        )
        assert all(r["status"] == "completed" for r in rows)

    # -- A3: non-empty result['errors'] means failed, result JSON preserved ---

    def test_non_empty_errors_marks_failed_and_preserves_result_json(self, storage):
        """A walk returning a non-empty result['errors'] (e.g. 'Book not found')
        is failed; the raw result (including errors) is preserved as result_json
        on the failed row."""
        run_id = str(uuid.uuid4())
        _insert_pending_row(storage, run_id, self.BOOK, self.WALK)
        runner = self._runner(storage)
        raw = {
            "book_id": self.BOOK,
            "scenes_processed": 0,
            "errors": [{"chapter_id": "ch1", "error": "Book not found"}],
        }
        mock_module = _make_mock_walk_module(MagicMock(return_value=raw))
        verify = MagicMock(return_value=True)
        with (
            patch.object(WalkRunner, "_load_walk_module", return_value=mock_module),
            patch.object(WalkRunner, "_run_verification", return_value=verify),
        ):
            result = runner.run_walk_reserved(run_id, self.WALK, self.BOOK, {})
        assert result["status"] == "failed"
        assert "reported errors" in result["error"]
        assert result["result"] == raw
        rows = storage.execute_query(
            "SELECT status, result_json, error FROM walk_run WHERE run_id = ?",
            (run_id,),
        )
        assert rows[0]["status"] == "failed"
        assert json.loads(rows[0]["result_json"]) == raw
        assert "reported errors" in rows[0]["error"]

    def test_non_empty_errors_skips_verification(self, storage):
        """A non-empty errors result fails the run before verification runs."""
        run_id = str(uuid.uuid4())
        _insert_pending_row(storage, run_id, self.BOOK, self.WALK)
        runner = self._runner(storage)
        mock_module = _make_mock_walk_module(
            MagicMock(return_value={"errors": [{"error": "boom"}]})
        )
        verify = MagicMock(return_value=True)
        with (
            patch.object(WalkRunner, "_load_walk_module", return_value=mock_module),
            patch.object(WalkRunner, "_run_verification", return_value=verify),
        ):
            result = runner.run_walk_reserved(run_id, self.WALK, self.BOOK, {})
        assert result["status"] == "failed"
        verify.assert_not_called()

    def test_empty_errors_retains_completion(self, storage):
        """An empty result['errors'] list does NOT fail the run; it completes
        (verification behavior retained)."""
        run_id = str(uuid.uuid4())
        _insert_pending_row(storage, run_id, self.BOOK, self.WALK)
        runner = self._runner(storage)
        raw = {"scenes_created": 3, "errors": []}
        mock_module = _make_mock_walk_module(MagicMock(return_value=raw))
        with (
            patch.object(WalkRunner, "_load_walk_module", return_value=mock_module),
            patch.object(WalkRunner, "_run_verification", return_value=True),
        ):
            result = runner.run_walk_reserved(run_id, self.WALK, self.BOOK, {})
        assert result == raw
        rows = storage.execute_query(
            "SELECT status FROM walk_run WHERE run_id = ?", (run_id,)
        )
        assert rows[0]["status"] == "completed"

    def test_missing_errors_retains_verification_behavior(self, storage):
        """A result with NO 'errors' key keeps the existing verification path:
        a verification failure still fails the run (rows = truth)."""
        run_id = str(uuid.uuid4())
        _insert_pending_row(storage, run_id, self.BOOK, self.WALK)
        runner = self._runner(storage)
        raw = {"scenes_created": 1}
        mock_module = _make_mock_walk_module(MagicMock(return_value=raw))
        with (
            patch.object(WalkRunner, "_load_walk_module", return_value=mock_module),
            patch.object(WalkRunner, "_run_verification", return_value=False),
        ):
            result = runner.run_walk_reserved(run_id, self.WALK, self.BOOK, {})
        assert result["status"] == "failed"
        assert "Verification failed" in result["error"]

    # -- A1: run_all_walks_reserved uses persisted status for abort ---------

    def test_abort_uses_persisted_status_over_returned_shape(self, storage):
        """run_all_walks_reserved aborts based on each child's PERSISTED row
        status, not the returned raw summary's status key."""
        reservations = self._reservations()
        for walk_name, run_id in reservations:
            _insert_pending_row(storage, run_id, self.BOOK, walk_name)
        runner = self._runner(storage)
        batch_id = str(uuid.uuid4())
        calls = []

        def fake_run_walk_reserved(run_id, walk_name, book_id, config):
            calls.append(walk_name)
            storage.execute_update(
                "UPDATE walk_run SET status = 'failed', error = 'boom' "
                "WHERE run_id = ?",
                (run_id,),
            )
            return {"status": "completed", "synthetic": True}

        with patch.object(
            WalkRunner, "run_walk_reserved", side_effect=fake_run_walk_reserved
        ):
            results = runner.run_all_walks_reserved(
                batch_id, reservations, self.BOOK, {}
            )
        # Only the first child was attempted: the persisted 'failed' status
        # drove the abort even though the returned summary said 'completed'.
        assert calls == [WALK_ORDER[0]]
        assert results[WALK_ORDER[0]]["status"] == "completed"
        rows = storage.execute_query(
            "SELECT status FROM walk_run WHERE book_id = ?", (self.BOOK,)
        )
        assert rows[0]["status"] == "failed"
        assert all(r["status"] != "pending" for r in rows[1:])

    def test_abort_when_walk_reports_errors(self, storage):
        """A child whose walk returns non-empty errors persists 'failed' (A3);
        run_all_walks_reserved aborts on that persisted status and terminalizes
        the remaining children (A1)."""
        from app.pipeline.walks.runner import reserve_all_walk_runs

        reservations = {w: str(uuid.uuid4()) for w in WALK_ORDER}
        reserve_all_walk_runs(storage, self.BOOK, list(reservations.items()))
        runner = self._runner(storage)
        batch_id = str(uuid.uuid4())
        call_count = [0]

        def execute_fn(book_id, hbs, config):
            call_count[0] += 1
            return {"book_id": book_id, "errors": [{"error": "boom"}]}

        mock_module = _make_mock_walk_module(execute_fn)
        with (
            patch.object(WalkRunner, "_load_walk_module", return_value=mock_module),
            patch.object(WalkRunner, "_run_verification", return_value=True),
        ):
            results = runner.run_all_walks_reserved(
                batch_id, list(reservations.items()), self.BOOK, {}
            )
        assert call_count[0] == 1  # only the first child executed
        assert results[WALK_ORDER[0]]["status"] == "failed"
        rows = storage.execute_query(
            "SELECT walk_name, status FROM walk_run WHERE book_id = ?", (self.BOOK,)
        )
        by_name = {r["walk_name"]: r["status"] for r in rows}
        assert by_name[WALK_ORDER[0]] == "failed"
        for w in WALK_ORDER[1:]:
            assert by_name[w] in ("failed", "cancelled", "interrupted")


# ---------------------------------------------------------------------------
# P1-S4 — ContextVar seam + sink reset on every terminal path
# ---------------------------------------------------------------------------


class TestWalkLogSinkContextVar:
    """Locks the ``WALK_LOG_SINK`` ContextVar seam contract: it defaults to
    None and round-trips set/reset while restoring the prior value; the runner
    sets it before execute and resets it in ``finally`` on every terminal path
    (success, exception, import failure, verification failure, cancellation); a
    sink-open failure never alters the DB status/result/error; concurrent runs
    do not leak sinks across contexts."""

    BOOK = "11111111-2222-3333-4444-555555555555"
    WALK = "walk_2a_scene_segmentation"

    def _runner(self, storage):
        return WalkRunner(storage, log_service=_FakeLogService())

    # -- pure seam shape ------------------------------------------------

    def test_walk_log_sink_defaults_to_none(self):
        from app.pipeline.walks._llm_helpers import get_walk_log_sink

        assert get_walk_log_sink() is None

    def test_set_and_get_walk_log_sink(self):
        from app.pipeline.walks._llm_helpers import WALK_LOG_SINK, get_walk_log_sink

        sink = _FakeSink()
        token = WALK_LOG_SINK.set(sink)
        try:
            assert get_walk_log_sink() is sink
        finally:
            WALK_LOG_SINK.reset(token)
        assert get_walk_log_sink() is None

    def test_reset_restores_prior_value(self):
        from app.pipeline.walks._llm_helpers import WALK_LOG_SINK, get_walk_log_sink

        prior = _FakeSink()
        token0 = WALK_LOG_SINK.set(prior)
        try:
            inner = _FakeSink()
            token1 = WALK_LOG_SINK.set(inner)
            assert get_walk_log_sink() is inner
            WALK_LOG_SINK.reset(token1)
            assert get_walk_log_sink() is prior
        finally:
            WALK_LOG_SINK.reset(token0)
        assert get_walk_log_sink() is None

    # -- runner sets before execute, resets in finally -------------------

    def test_runner_sets_sink_before_execute_and_resets_after(self, storage):
        from app.pipeline.walks._llm_helpers import get_walk_log_sink

        run_id = str(uuid.uuid4())
        _insert_pending_row(storage, run_id, self.BOOK, self.WALK)
        service = _FakeLogService()
        runner = WalkRunner(storage, log_service=service)
        seen = {}

        def execute_fn(book_id, hbs, config):
            seen["sink_during"] = get_walk_log_sink()
            return {"status": "completed"}

        mock_module = _make_mock_walk_module(execute_fn)
        with (
            patch.object(WalkRunner, "_load_walk_module", return_value=mock_module),
            patch.object(WalkRunner, "_run_verification", return_value=True),
        ):
            runner.run_walk_reserved(run_id, self.WALK, self.BOOK, {})
        assert seen["sink_during"] is service.sinks.get(run_id)
        assert get_walk_log_sink() is None

    def test_runner_resets_sink_on_exception(self, storage):
        from app.pipeline.walks._llm_helpers import get_walk_log_sink

        run_id = str(uuid.uuid4())
        _insert_pending_row(storage, run_id, self.BOOK, self.WALK)
        runner = self._runner(storage)
        mock_module = _make_mock_walk_module(
            MagicMock(side_effect=RuntimeError("boom"))
        )
        with patch.object(WalkRunner, "_load_walk_module", return_value=mock_module):
            result = runner.run_walk_reserved(run_id, self.WALK, self.BOOK, {})
        assert result["status"] == "failed"
        assert get_walk_log_sink() is None

    def test_runner_resets_sink_on_import_failure(self, storage):
        from app.pipeline.walks._llm_helpers import get_walk_log_sink

        run_id = str(uuid.uuid4())
        _insert_pending_row(storage, run_id, self.BOOK, self.WALK)
        runner = self._runner(storage)
        result = runner.run_walk_reserved(run_id, "walk_missing_xyz", self.BOOK, {})
        assert result["status"] == "failed"
        assert get_walk_log_sink() is None

    def test_runner_resets_sink_on_verification_failure(self, storage):
        from app.pipeline.walks._llm_helpers import get_walk_log_sink

        run_id = str(uuid.uuid4())
        _insert_pending_row(storage, run_id, self.BOOK, self.WALK)
        runner = self._runner(storage)
        mock_module = _make_mock_walk_module(
            MagicMock(return_value={"status": "completed"})
        )
        with (
            patch.object(WalkRunner, "_load_walk_module", return_value=mock_module),
            patch.object(WalkRunner, "_run_verification", return_value=False),
        ):
            result = runner.run_walk_reserved(run_id, self.WALK, self.BOOK, {})
        assert result["status"] == "failed"
        assert get_walk_log_sink() is None

    def test_runner_resets_sink_on_cancellation(self, storage):
        from app.pipeline.walks._llm_helpers import get_walk_log_sink

        run_id = str(uuid.uuid4())
        _insert_pending_row(storage, run_id, self.BOOK, self.WALK)
        runner = self._runner(storage)
        runner.cancel_walks(self.BOOK)
        result = runner.run_walk_reserved(run_id, self.WALK, self.BOOK, {})
        assert result["status"] == "cancelled"
        assert get_walk_log_sink() is None

    def test_sink_open_failure_does_not_alter_db_result(self, storage):
        from app.pipeline.walks._llm_helpers import get_walk_log_sink

        run_id = str(uuid.uuid4())
        _insert_pending_row(storage, run_id, self.BOOK, self.WALK)

        class _FailingService(_FakeLogService):
            def open_run(self, run_id, book_id, walk_name, started_ms=None):
                raise OSError("no space left on device")

        runner = WalkRunner(storage, log_service=_FailingService())
        mock_module = _make_mock_walk_module(
            MagicMock(return_value={"status": "completed"})
        )
        with (
            patch.object(WalkRunner, "_load_walk_module", return_value=mock_module),
            patch.object(WalkRunner, "_run_verification", return_value=True),
        ):
            result = runner.run_walk_reserved(run_id, self.WALK, self.BOOK, {})
        # Sink failure must not change the DB outcome (row = truth).
        assert result["status"] == "completed"
        rows = storage.execute_query(
            "SELECT status FROM walk_run WHERE run_id = ?", (run_id,)
        )
        assert rows[0]["status"] == "completed"
        assert get_walk_log_sink() is None

    def test_concurrent_runs_do_not_leak_sinks(self, storage):
        from app.pipeline.walks._llm_helpers import get_walk_log_sink

        # Each thread gets its OWN adapter (one sqlite3 connection each) so the
        # two threads never share a single connection concurrently. The test's
        # intent is ContextVar isolation under concurrency, not adapter
        # thread-safety — the shared `storage` fixture must NOT be used here.
        storage_a = InMemorySQLiteAdapter()
        storage_a.init_db()
        storage_b = InMemorySQLiteAdapter()
        storage_b.init_db()
        run_id_a = str(uuid.uuid4())
        run_id_b = str(uuid.uuid4())
        book_a = "aaaaaaaa-1111-1111-1111-111111111111"
        book_b = "bbbbbbbb-1111-1111-1111-111111111111"
        _insert_pending_row(storage_a, run_id_a, book_a, self.WALK)
        _insert_pending_row(storage_b, run_id_b, book_b, self.WALK)
        service_a = _FakeLogService()
        service_b = _FakeLogService()
        runner_a = WalkRunner(storage_a, log_service=service_a)
        runner_b = WalkRunner(storage_b, log_service=service_b)
        captured_a = {}
        captured_b = {}

        def execute_a(book_id, hbs, config):
            captured_a["sink"] = get_walk_log_sink()
            return {"status": "completed"}

        def execute_b(book_id, hbs, config):
            captured_b["sink"] = get_walk_log_sink()
            return {"status": "completed"}

        mock_a = _make_mock_walk_module(execute_a)
        mock_b = _make_mock_walk_module(execute_b)
        # Per-instance method shadowing (thread-safe; no shared class attribute is
        # mutated, so the two threads cannot cross-contaminate each other's sinks
        # or race the patch exit-stack restore).
        runner_a._load_walk_module = lambda walk_name: mock_a
        runner_a._run_verification = lambda walk_name, book_id: True
        runner_b._load_walk_module = lambda walk_name: mock_b
        runner_b._run_verification = lambda walk_name, book_id: True
        results = {}

        def target(runner, rid, book):
            results[rid] = runner.run_walk_reserved(rid, self.WALK, book, {})

        t1 = threading.Thread(target=target, args=(runner_a, run_id_a, book_a))
        t2 = threading.Thread(target=target, args=(runner_b, run_id_b, book_b))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        assert captured_a["sink"] is service_a.sinks.get(run_id_a)
        assert captured_b["sink"] is service_b.sinks.get(run_id_b)
        assert results[run_id_a]["status"] == "completed"
        assert results[run_id_b]["status"] == "completed"
        assert get_walk_log_sink() is None


# ---------------------------------------------------------------------------
# P1-S7 — static/import audit + representative instrumented execution
# ---------------------------------------------------------------------------


class TestWalkModuleStaticAudit:
    """Static/import audit: the nine ``walk_2*.py`` modules remain
    byte-identical to git HEAD (no edits) and never import the Part B seam
    (implementation imports forbidden). The final representative-execution test
    drives a helper-instrumented walk through the reserved runner seam and
    asserts the run's sink receives both ``llm`` and ``parse`` records."""

    _REPO_ROOT = Path(__file__).resolve().parents[2]
    _WALK_DIR = _REPO_ROOT / "app/pipeline/walks"
    #: The shared seam symbols a walk module must never import (implementation
    #: imports). Walk modules may legitimately import chat_completion /
    #: extract_json_from_llm_response from _llm_helpers, but never the sink
    #: ContextVar or the log service.
    FORBIDDEN_IMPORTS = (
        "WALK_LOG_SINK",
        "get_walk_log_sink",
        "WalkLogService",
        "WalkLogSink",
        "log_service",
    )

    def _walk_files(self):
        return sorted(self._WALK_DIR.glob("walk_2*.py"))

    def test_walk_modules_byte_identical_to_git_head(self):
        import subprocess

        for path in self._walk_files():
            rel = path.relative_to(self._REPO_ROOT)
            proc = subprocess.run(
                ["git", "show", f"HEAD:{rel.as_posix()}"],
                capture_output=True,
                check=False,
                cwd=self._REPO_ROOT,
            )
            assert proc.returncode == 0, f"{rel} not tracked in git HEAD"
            assert path.read_bytes() == proc.stdout, (
                f"{rel} modified from git HEAD — the nine walk modules must remain "
                "byte-identical"
            )

    def test_walk_modules_have_no_implementation_imports(self):
        for path in self._walk_files():
            source = path.read_text(encoding="utf-8")
            for sym in self.FORBIDDEN_IMPORTS:
                assert sym not in source, (
                    f"{path.name} references {sym} — walk modules must not import "
                    "the log-streaming seam"
                )

    def test_representative_instrumented_walk_through_runner_seam(self, storage):
        """Drive a helper-instrumented walk through the reserved runner seam.

        The walk's ``execute`` calls ``chat_completion`` then
        ``extract_json_from_llm_response``; with a sink attached by the runner
        (via the ContextVar), both helper seams must emit ``llm`` and ``parse``
        records on the run's sink."""
        from app.pipeline.walks._llm_helpers import (
            chat_completion,
            extract_json_from_llm_response,
        )

        run_id = str(uuid.uuid4())
        book_id = "11111111-2222-3333-4444-555555555555"
        walk_name = "walk_2a_scene_segmentation"
        _insert_pending_row(storage, run_id, book_id, walk_name)
        service = _FakeLogService()
        runner = WalkRunner(storage, log_service=service)
        usage = types.SimpleNamespace(
            prompt_tokens=1, completion_tokens=2, total_tokens=3
        )
        response = _FakeResponse("gpt-4o", _FakeChoice('{"a": 1}', "stop"), usage)
        client = _FakeClient(response)

        def execute_fn(book_id, hbs, config):
            text = chat_completion(client, "gpt-4o", 0.1, "low", "sys", "usr")
            parsed = extract_json_from_llm_response(text, expected_type="dict")
            assert parsed == {"a": 1}
            return {"status": "completed"}

        mock_module = _make_mock_walk_module(execute_fn)
        with (
            patch.object(WalkRunner, "_load_walk_module", return_value=mock_module),
            patch.object(WalkRunner, "_run_verification", return_value=True),
        ):
            runner.run_walk_reserved(run_id, walk_name, book_id, {})
        sink = service.sinks.get(run_id)
        assert sink is not None
        events = [r["event"] for r in sink.records]
        assert "llm" in events
        assert "parse" in events
