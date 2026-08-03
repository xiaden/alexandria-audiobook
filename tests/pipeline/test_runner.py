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

import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from app.pipeline.adapter import InMemorySQLiteAdapter
from app.pipeline.walks.runner import WalkRunner


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
        assert isinstance(WalkRunner.WALK_ORDER, list)
        assert "walk_2a_scene_segmentation" in WalkRunner.WALK_ORDER


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
        mock_execute.assert_called_once_with("book-1", runner._storage, {})
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
        mock_execute.assert_called_once_with("book-1", runner._storage, config)


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
        assert len(results) == len(WalkRunner.WALK_ORDER)
        for walk_name in WalkRunner.WALK_ORDER:
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
        first_walk = WalkRunner.WALK_ORDER[0]
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
        for walk_name in WalkRunner.WALK_ORDER:
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
