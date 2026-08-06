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
import time
import types
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
        assert runner.get_walk_status("book-1", "walk_2a_scene_segmentation") == "pending"

    def test_status_running_during_execution(self, runner):
        """Status is 'running' while walk is executing."""
        captured_status = []

        def execute_fn(book_id, storage, config):
            captured_status.append(runner.get_walk_status(book_id, "walk_2a_scene_segmentation"))
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
        assert runner.get_walk_status("book-1", "walk_2a_scene_segmentation") == "completed"

    def test_status_failed_after_exception(self, runner):
        """Status is 'failed' when execute() raises an exception."""
        mock_module = _make_mock_walk_module(MagicMock(side_effect=RuntimeError("boom")))
        with patch.object(WalkRunner, "_load_walk_module", return_value=mock_module):
            result = runner.run_walk("walk_2a_scene_segmentation", "book-1", {})
        assert result["status"] == "failed"
        assert "boom" in result["error"]
        assert runner.get_walk_status("book-1", "walk_2a_scene_segmentation") == "failed"

    def test_unknown_walk_status_is_pending(self, runner):
        """get_walk_status returns 'pending' for unknown walk name."""
        assert runner.get_walk_status("book-1", "nonexistent_walk") == "pending"

    def test_unknown_book_status_is_pending(self, runner):
        """get_walk_status returns 'pending' for unknown book_id."""
        assert runner.get_walk_status("unknown-book", "walk_2a_scene_segmentation") == "pending"


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
    def test_verification_failure_marks_failed(self, storage):
        """If verification fails, status is 'failed' even though execute() succeeded."""
        runner = WalkRunner(storage)
        # Set up chapters but no chapter_scene edges (verification will fail)
        storage.execute_insert(
            "INSERT INTO series (id) VALUES (?)", ("series-1",)
        )
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
        assert runner.get_walk_status("book-1", "walk_2a_scene_segmentation") == "failed"

    def test_verification_passes_with_scenes(self, storage):
        """Verification passes when chapter_scene rows exist for the book."""
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
            ("scene-1", "chapter-1", 1),
        )
        mock_module = _make_mock_walk_module(
            MagicMock(return_value={"status": "completed"})
        )
        with patch.object(WalkRunner, "_load_walk_module", return_value=mock_module):
            result = runner.run_walk("walk_2a_scene_segmentation", "book-1", {})
        assert result["status"] == "completed"
        assert runner.get_walk_status("book-1", "walk_2a_scene_segmentation") == "completed"

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
            return_value=MagicMock(execute=MagicMock(return_value={"status": "completed"})),
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
            return_value=MagicMock(execute=MagicMock(return_value={"status": "completed"})),
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

    def test_run_all_walks_stops_on_cancel(self, runner):
        """run_all_walks checks cancel flag before each walk."""
        # Cancel before starting
        runner.cancel_walks("book-1")
        results = runner.run_all_walks("book-1", {})
        # All walks should be cancelled
        for walk_name, result in results.items():
            assert result["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Test walk_run persistence (Phase 2: rows = truth)
# ---------------------------------------------------------------------------


class TestWalkRunPersistence:
    """Spec-first tests: run_walk/run_all_walks write walk_run rows.

    P2-S1 (RED): these fail against the in-memory-only implementation.
    """

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

        with patch(
            "app.pipeline.walks.runner.time.sleep", side_effect=recording_sleep
        ):
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
