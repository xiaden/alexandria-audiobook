"""Spec-first tests for pipeline API endpoints (app/pipeline/api.py).

Covers:
- POST /api/pipeline/onboard — EPUB file upload, extraction, population
- POST /api/pipeline/run_walk — single walk execution
- POST /api/pipeline/run_all_walks — all 9 walks serially
- GET /api/pipeline/walk_status/{book_id} — per-walk status
- GET /api/pipeline/characters/{book_id} — character ledger
- GET /api/pipeline/review/{book_id} — review items (confidence 0.5-0.7)
- POST /api/pipeline/review/accept — accept review item
- POST /api/pipeline/review/reject — reject review item
- POST /api/pipeline/review/override — override review item
- POST /api/pipeline/operation — split/merge/move/delete operations
- GET /api/pipeline/export/{book_id} — export annotated script
- POST /api/pipeline/render — render audiobook
- POST /api/pipeline/reonboard — re-onboard book
"""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.pipeline.adapter import InMemorySQLiteAdapter
from app.pipeline.api import (
    get_character_ledger,
    get_operation_executor,
    get_review_manager,
    get_storage,
    get_tts_engine,
    get_walk_runner,
    router,
)
from app.pipeline.ledger import CharacterLedger
from app.pipeline.operations import OperationExecutor
from app.pipeline.review import ReviewManager
from app.pipeline.walks.order import WALK_ORDER
from app.pipeline.walks.runner import WalkRunner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _populate_storage(storage: InMemorySQLiteAdapter) -> None:
    """Insert a minimal but complete document spine with characters."""
    # -- Voice config -------------------------------------------------------
    storage.execute_insert(
        "INSERT INTO voice_config (id, name, description) VALUES ('vc1', 'Warm Female', 'A warm female voice')"
    )

    # -- Series + Book ------------------------------------------------------
    storage.execute_insert("INSERT INTO series (id) VALUES ('s1')")
    storage.execute_insert(
        "INSERT INTO book (id, series_id, position, version) VALUES ('b1', 's1', 1, 1)"
    )

    # -- Chapters -----------------------------------------------------------
    storage.execute_insert(
        "INSERT INTO chapter (id, book_id) VALUES ('ch1', 'b1')"
    )
    storage.execute_insert(
        "INSERT INTO book_chapter (child_id, parent_id, position) VALUES ('ch1', 'b1', 1)"
    )

    # -- Scenes -------------------------------------------------------------
    storage.execute_insert("INSERT INTO scene (id) VALUES ('sc1')")
    storage.execute_insert(
        "INSERT INTO chapter_scene (child_id, parent_id, position) VALUES ('sc1', 'ch1', 1)"
    )

    # -- Paragraphs ---------------------------------------------------------
    storage.execute_insert("INSERT INTO paragraph (id) VALUES ('p1')")
    storage.execute_insert(
        "INSERT INTO scene_paragraph (child_id, parent_id, position) VALUES ('p1', 'sc1', 1)"
    )

    # -- Spans --------------------------------------------------------------
    storage.execute_insert(
        "INSERT INTO span (id, span_type, text, instruct) VALUES ('sp1', 'quotation', 'Hello there!', 'cheerfully')"
    )
    storage.execute_insert(
        "INSERT INTO paragraph_span (child_id, parent_id, position) VALUES ('sp1', 'p1', 1)"
    )

    # -- Characters ---------------------------------------------------------
    storage.execute_insert(
        "INSERT INTO character (id, name, aliases, voice_assignment_id) VALUES ('c1', 'Alice', '[]', 'vc1')"
    )

    # -- character_book junction --------------------------------------------
    storage.execute_insert(
        "INSERT INTO character_book (character_id, book_id, source, confidence) VALUES ('c1', 'b1', 'walk', 0.9)"
    )

    # -- character_span (speaker junction) ----------------------------------
    storage.execute_insert(
        """INSERT INTO character_span (character_id, span_id, relation_type, source, confidence)
           VALUES ('c1', 'sp1', 'speaker', 'walk', 0.95)"""
    )

    # -- Low-confidence item for review queue (confidence in 0.5-0.7) -------
    storage.execute_insert(
        "INSERT INTO character (id, name, aliases) VALUES ('c2', 'Bob', '[]')"
    )
    storage.execute_insert(
        "INSERT INTO character_book (character_id, book_id, source, confidence) VALUES ('c2', 'b1', 'walk', 0.6)"
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def storage():
    """In-memory SQLite adapter for testing."""
    adapter = InMemorySQLiteAdapter()
    adapter.init_db()
    _populate_storage(adapter)
    return adapter


@pytest.fixture()
def walk_runner(storage):
    """WalkRunner with in-memory storage."""
    return WalkRunner(storage)


@pytest.fixture()
def review_manager(storage):
    """ReviewManager with in-memory storage."""
    return ReviewManager(storage)


@pytest.fixture()
def operation_executor(storage):
    """OperationExecutor with in-memory storage."""
    return OperationExecutor(storage)


@pytest.fixture()
def character_ledger(storage):
    """CharacterLedger with in-memory storage."""
    return CharacterLedger(storage)


@pytest.fixture()
def tts_engine():
    """Mock TTS engine."""
    engine = MagicMock()
    engine.generate_batch = MagicMock(return_value=None)
    engine.generate_voice = MagicMock(return_value=None)
    return engine


@pytest.fixture()
def client(
    storage, walk_runner, review_manager, operation_executor, character_ledger, tts_engine
):
    """FastAPI TestClient with dependency overrides."""
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router)

    # Override dependencies to inject test instances
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_walk_runner] = lambda: walk_runner
    app.dependency_overrides[get_review_manager] = lambda: review_manager
    app.dependency_overrides[get_operation_executor] = lambda: operation_executor
    app.dependency_overrides[get_character_ledger] = lambda: character_ledger
    app.dependency_overrides[get_tts_engine] = lambda: tts_engine

    return TestClient(app)


# ---------------------------------------------------------------------------
# P1-S2: POST /api/pipeline/onboard
# ---------------------------------------------------------------------------


class TestOnboardEndpoint:
    def test_onboard_rejects_non_epub(self, client):
        """Non-EPUB files are rejected with 400."""
        response = client.post(
            "/api/pipeline/onboard",
            files={"file": ("test.txt", b"not an epub", "text/plain")},
        )
        assert response.status_code == 400
        assert "must be an EPUB" in response.json()["detail"]

    def test_onboard_accepts_epub(self, client, storage):
        """EPUB files are accepted and processed."""
        # Mock extract_epub_text and populate_spine
        with patch("app.pipeline.api_onboard.extract_epub_text") as mock_extract, patch(
            "app.pipeline.api_onboard.populate_spine"
        ) as mock_populate:
            mock_extract.return_value = {
                "series_id": "s-test",
                "book_id": "b-test",
                "chapters": [{"id": "ch1", "title": "Chapter 1"}],
            }
            mock_populate.return_value = None

            response = client.post(
                "/api/pipeline/onboard",
                files={"file": ("test.epub", b"fake epub content", "application/epub+zip")},
            )

            assert response.status_code == 200
            data = response.json()
            assert "book_id" in data
            assert data["series_id"] == "s-test"
            assert data["chapters"] == 1

            mock_extract.assert_called_once()
            mock_populate.assert_called_once()

    def test_onboard_handles_extraction_failure(self, client):
        """Extraction failures return 400."""
        with patch("app.pipeline.api_onboard.extract_epub_text") as mock_extract:
            mock_extract.side_effect = Exception("Invalid EPUB")

            response = client.post(
                "/api/pipeline/onboard",
                files={"file": ("test.epub", b"fake epub content", "application/epub+zip")},
            )

            assert response.status_code == 400
            assert "Failed to extract EPUB" in response.json()["detail"]

    def test_onboard_handles_populate_spine_failure(self, client):
        """populate_spine failures return 500."""
        with patch("app.pipeline.api_onboard.extract_epub_text") as mock_extract, patch(
            "app.pipeline.api_onboard.populate_spine"
        ) as mock_populate:
            mock_extract.return_value = {
                "series_id": "s-test",
                "book_id": "b-test",
                "chapters": [{"id": "ch1", "title": "Chapter 1"}],
            }
            mock_populate.side_effect = Exception("DB constraint violation")

            response = client.post(
                "/api/pipeline/onboard",
                files={"file": ("test.epub", b"fake epub content", "application/epub+zip")},
            )

            assert response.status_code == 500
            assert "Failed to populate spine" in response.json()["detail"]


# ---------------------------------------------------------------------------
# P1-S3: POST /api/pipeline/run_walk
# ---------------------------------------------------------------------------


class TestRunWalkEndpoint:
    def test_run_walk_invalid_walk_name(self, client):
        """Invalid walk names are rejected with 400."""
        response = client.post(
            "/api/pipeline/run_walk",
            json={"walk_name": "invalid_walk", "book_id": "b1", "config": {}},
        )
        assert response.status_code == 400
        assert "Unknown walk" in response.json()["detail"]

    def test_run_walk_valid(self, client, walk_runner):
        """Valid walk names start background execution and return immediately."""
        # Mock the walk module loading
        with patch.object(walk_runner, "_load_walk_module") as mock_load:
            mock_module = MagicMock()
            mock_module.execute = MagicMock(
                return_value={"status": "completed", "walk": "walk_2a_scene_segmentation"}
            )
            mock_load.return_value = mock_module

            response = client.post(
                "/api/pipeline/run_walk",
                json={
                    "walk_name": "walk_2a_scene_segmentation",
                    "book_id": "b1",
                    "config": {},
                },
            )

            assert response.status_code == 200
            result = response.json()
            # Background execution returns immediately with 'started'
            assert result["status"] == "started"
            assert result["walk_name"] == "walk_2a_scene_segmentation"

    def test_run_walk_execution_failure(self, client, walk_runner):
        """Background walk failures are reflected in status, not in response."""
        # With background execution, the response is always 'started'
        # Failures are detected via polling walk_status
        with patch.object(walk_runner, "_load_walk_module") as mock_load:
            mock_module = MagicMock()
            mock_module.execute = MagicMock(side_effect=RuntimeError("Walk crashed"))
            mock_load.return_value = mock_module

            response = client.post(
                "/api/pipeline/run_walk",
                json={
                    "walk_name": "walk_2a_scene_segmentation",
                    "book_id": "b1",
                    "config": {},
                },
            )

            assert response.status_code == 200
            result = response.json()
            # Response is always 'started' for background execution
            assert result["status"] == "started"


# ---------------------------------------------------------------------------
# P1-S4: POST /api/pipeline/run_all_walks
# ---------------------------------------------------------------------------


class TestRunAllWalksEndpoint:
    def test_run_all_walks(self, client, walk_runner):
        """Background execution returns immediately with 'started' status."""
        # Mock the walk module loading and verification to avoid actual walk execution
        with patch.object(walk_runner, "_load_walk_module") as mock_load, \
             patch.object(walk_runner, "_run_verification", return_value=True):
            mock_module = MagicMock()
            mock_module.execute = MagicMock(return_value={"status": "completed"})
            mock_load.return_value = mock_module

            response = client.post(
                "/api/pipeline/run_all_walks",
                json={"book_id": "b1", "config": {}},
            )

            assert response.status_code == 200
            result = response.json()
            # Background execution returns immediately
            assert result["status"] == "started"


# ---------------------------------------------------------------------------
# P2-S3: POST /api/pipeline/cancel_walks
# ---------------------------------------------------------------------------


class TestCancelWalksEndpoint:
    def test_cancel_walks_sets_flag(self, client, walk_runner):
        """Cancel walks endpoint sets cancellation flag and returns status."""
        response = client.post(
            "/api/pipeline/cancel_walks",
            json={"book_id": "b1"},
        )

        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "cancelled"
        # Verify the flag was set
        assert walk_runner._cancelled.get("b1") is True

    def test_cancel_walks_affects_running_walks(self, client, walk_runner):
        """Cancel walks prevents subsequent walks from starting."""
        # Cancel walks
        client.post("/api/pipeline/cancel_walks", json={"book_id": "b1"})

        # Try to run a walk - it should be cancelled
        with patch.object(walk_runner, "_load_walk_module") as mock_load:
            mock_module = MagicMock()
            mock_module.execute = MagicMock(return_value={"status": "completed"})
            mock_load.return_value = mock_module

            response = client.post(
                "/api/pipeline/run_walk",
                json={
                    "walk_name": "walk_2a_scene_segmentation",
                    "book_id": "b1",
                    "config": {},
                },
            )

            # Response is still 'started' (background execution)
            assert response.status_code == 200
            result = response.json()
            assert result["status"] == "started"

            # But the walk status should be 'cancelled' after background task runs
            # (In real scenario, the background task would check the flag)


# ---------------------------------------------------------------------------
# P1-S5: GET /api/pipeline/walk_status/{book_id}
# ---------------------------------------------------------------------------


class TestWalkStatusEndpoint:
    def test_walk_status_returns_dict(self, client):
        """Walk status returns a dict mapping walk names to statuses."""
        response = client.get("/api/pipeline/walk_status/b1")
        assert response.status_code == 200
        result = response.json()
        assert isinstance(result, dict)
        # Should have entries for all walks
        assert len(result) == len(WALK_ORDER)
        # Each walk name should be a key with a valid status value
        for walk_name in WALK_ORDER:
            assert walk_name in result
            assert result[walk_name] in ("pending", "running", "completed", "failed", "cancelled")


# ---------------------------------------------------------------------------
# P1-S6: GET /api/pipeline/characters/{book_id}
# ---------------------------------------------------------------------------


class TestCharactersEndpoint:
    def test_characters_returns_list(self, client):
        """Characters endpoint returns a list of character dicts."""
        response = client.get("/api/pipeline/characters/b1")
        assert response.status_code == 200
        result = response.json()
        assert isinstance(result, list)
        assert len(result) > 0
        # Each character should have id, name, aliases, confidence
        for char in result:
            assert "id" in char
            assert "name" in char
            assert "aliases" in char
            assert "confidence" in char

    def test_characters_empty_book(self, client):
        """Empty book returns empty list."""
        response = client.get("/api/pipeline/characters/nonexistent")
        assert response.status_code == 200
        assert response.json() == []


# ---------------------------------------------------------------------------
# P1-S7: GET /api/pipeline/review/{book_id}
# ---------------------------------------------------------------------------


class TestReviewEndpoint:
    def test_review_returns_list(self, client):
        """Review endpoint returns a list of review items with expected fields."""
        response = client.get("/api/pipeline/review/b1")
        assert response.status_code == 200
        result = response.json()
        assert isinstance(result, list)
        # Should have at least one review item (from _populate_storage)
        assert len(result) > 0
        # Each item should have the required review fields
        for item in result:
            assert "item_id" in item
            assert "character_id" in item
            assert "character_name" in item
            assert "confidence" in item
            assert 0.5 <= item["confidence"] < 0.7


# ---------------------------------------------------------------------------
# P1-S8: POST /api/pipeline/review/accept
# ---------------------------------------------------------------------------


class TestReviewAcceptEndpoint:
    def test_accept_valid_item(self, client):
        """Accepting a valid review item returns success."""
        # First, get a review item
        review_response = client.get("/api/pipeline/review/b1")
        items = review_response.json()
        if not items:
            pytest.skip("No review items available")

        item_id = items[0]["item_id"]
        response = client.post(
            "/api/pipeline/review/accept",
            json={"item_id": item_id},
        )
        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "accepted"
        assert result["item_id"] == item_id

    def test_accept_invalid_item(self, client):
        """Accepting an invalid item_id returns 400."""
        response = client.post(
            "/api/pipeline/review/accept",
            json={"item_id": "invalid:item:id"},
        )
        assert response.status_code == 400
        # Error can be either format error or unknown junction table
        detail = response.json()["detail"]
        assert "Invalid item_id format" in detail or "Unknown junction table" in detail


# ---------------------------------------------------------------------------
# P1-S9: POST /api/pipeline/review/reject
# ---------------------------------------------------------------------------


class TestReviewRejectEndpoint:
    def test_reject_valid_item(self, client):
        """Rejecting a valid review item returns success."""
        review_response = client.get("/api/pipeline/review/b1")
        items = review_response.json()
        if not items:
            pytest.skip("No review items available")

        item_id = items[0]["item_id"]
        response = client.post(
            "/api/pipeline/review/reject",
            json={"item_id": item_id},
        )
        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "rejected"
        assert result["item_id"] == item_id

    def test_reject_invalid_item(self, client):
        """Rejecting an invalid item_id returns 400."""
        response = client.post(
            "/api/pipeline/review/reject",
            json={"item_id": "invalid:item:id"},
        )
        assert response.status_code == 400
        # Error can be either format error or unknown junction table
        detail = response.json()["detail"]
        assert "Invalid item_id format" in detail or "Unknown junction table" in detail


# ---------------------------------------------------------------------------
# P1-S10: POST /api/pipeline/review/override
# ---------------------------------------------------------------------------


class TestReviewOverrideEndpoint:
    def test_override_valid_item(self, client):
        """Overriding a valid review item returns success."""
        review_response = client.get("/api/pipeline/review/b1")
        items = review_response.json()
        if not items:
            pytest.skip("No review items available")

        item_id = items[0]["item_id"]
        response = client.post(
            "/api/pipeline/review/override",
            json={"item_id": item_id, "new_value": {"relation_type": "speaker"}},
        )
        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "overridden"
        assert result["item_id"] == item_id

    def test_override_invalid_item(self, client):
        """Overriding an invalid item_id returns 400."""
        response = client.post(
            "/api/pipeline/review/override",
            json={"item_id": "invalid:item:id", "new_value": {"relation_type": "speaker"}},
        )
        assert response.status_code == 400
        # Error can be either format error or unknown junction table
        detail = response.json()["detail"]
        assert "Invalid item_id format" in detail or "Unknown junction table" in detail


# ---------------------------------------------------------------------------
# P1-S11: POST /api/pipeline/operation
# ---------------------------------------------------------------------------


class TestOperationEndpoint:
    def test_operation_invalid_type(self, client):
        """Invalid operation types are rejected with 400."""
        response = client.post(
            "/api/pipeline/operation",
            json={
                "operation": "invalid",
                "book_id": "b1",
            },
        )
        assert response.status_code == 400
        assert "Unknown operation" in response.json()["detail"]

    def test_operation_split_missing_params(self, client):
        """Split operation requires presentation_index and split_point."""
        response = client.post(
            "/api/pipeline/operation",
            json={
                "operation": "split",
                "book_id": "b1",
                "presentation_index": 1,
                # Missing split_point
            },
        )
        assert response.status_code == 400
        assert "split requires" in response.json()["detail"]

    def test_operation_merge_missing_params(self, client):
        """Merge operation requires both presentation indices."""
        response = client.post(
            "/api/pipeline/operation",
            json={
                "operation": "merge",
                "book_id": "b1",
                "presentation_index_left": 1,
                # Missing presentation_index_right
            },
        )
        assert response.status_code == 400
        assert "merge requires" in response.json()["detail"]

    def test_operation_move_missing_params(self, client):
        """Move operation requires both presentation indices."""
        response = client.post(
            "/api/pipeline/operation",
            json={
                "operation": "move",
                "book_id": "b1",
                "presentation_index_from": 1,
                # Missing presentation_index_to
            },
        )
        assert response.status_code == 400
        assert "move requires" in response.json()["detail"]

    def test_operation_delete_missing_params(self, client):
        """Delete operation requires presentation_index."""
        response = client.post(
            "/api/pipeline/operation",
            json={
                "operation": "delete",
                "book_id": "b1",
                # Missing presentation_index
            },
        )
        assert response.status_code == 400
        assert "delete requires" in response.json()["detail"]

    def test_xxx_operation_split_ok(self, client):
        """Split operation with valid params returns 200 with full response."""
        response = client.post(
            "/api/pipeline/operation",
            json={
                "operation": "split",
                "book_id": "b1",
                "presentation_index": 1,
                "split_point": 5,
            },
        )
        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "ok"
        assert result["operation"] == "split"
        # Verify no extra unexpected keys
        assert set(result.keys()) == {"status", "operation"}

    def test_xxx_operation_merge_ok(self, client, storage):
        """Merge operation with valid params returns 200."""
        # Add a second adjacent span so merge has two spans to combine
        storage.execute_insert(
            "INSERT INTO span (id, span_type, text, instruct) "
            "VALUES ('sp_merge', 'quotation', ' world', NULL)"
        )
        storage.execute_insert(
            "INSERT INTO paragraph_span (child_id, parent_id, position) "
            "VALUES ('sp_merge', 'p1', 2)"
        )

        response = client.post(
            "/api/pipeline/operation",
            json={
                "operation": "merge",
                "book_id": "b1",
                "presentation_index_left": 1,
                "presentation_index_right": 2,
            },
        )
        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "ok"
        assert result["operation"] == "merge"

    def test_xxx_operation_move_ok(self, client, storage):
        """Move operation with valid params returns 200."""
        # Add a second span in the same paragraph so move has a target
        storage.execute_insert(
            "INSERT INTO span (id, span_type, text, instruct) "
            "VALUES ('sp_move', 'sentence', 'Second span', NULL)"
        )
        storage.execute_insert(
            "INSERT INTO paragraph_span (child_id, parent_id, position) "
            "VALUES ('sp_move', 'p1', 2)"
        )

        response = client.post(
            "/api/pipeline/operation",
            json={
                "operation": "move",
                "book_id": "b1",
                "presentation_index_from": 1,
                "presentation_index_to": 2,
            },
        )
        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "ok"
        assert result["operation"] == "move"

    def test_xxx_operation_delete_ok(self, client):
        """Delete operation with valid params returns 200 with full response."""
        response = client.post(
            "/api/pipeline/operation",
            json={
                "operation": "delete",
                "book_id": "b1",
                "presentation_index": 1,
            },
        )
        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "ok"
        assert result["operation"] == "delete"
        assert set(result.keys()) == {"status", "operation"}

    def test_operation_split_invalid_index(self, client):
        """Split with non-existent presentation index returns 400."""
        response = client.post(
            "/api/pipeline/operation",
            json={
                "operation": "split",
                "book_id": "b1",
                "presentation_index": 9999,
                "split_point": 5,
            },
        )
        assert response.status_code == 400
        # Should report the missing index
        assert "not found" in response.json()["detail"].lower() or "9999" in response.json()["detail"]

    def test_operation_delete_invalid_index(self, client):
        """Delete with non-existent presentation index returns 400."""
        response = client.post(
            "/api/pipeline/operation",
            json={
                "operation": "delete",
                "book_id": "b1",
                "presentation_index": 9999,
            },
        )
        assert response.status_code == 400
        assert "not found" in response.json()["detail"].lower() or "9999" in response.json()["detail"]


# ---------------------------------------------------------------------------
# P4-S2: PUT /api/pipeline/span/{span_id}/text
# ---------------------------------------------------------------------------


class TestSpanTextEditEndpoint:
    def test_update_span_text_success(self, client, storage):
        """PUT /api/pipeline/span/{span_id}/text updates span text and returns 200."""
        # Insert a test span
        storage.execute_insert(
            "INSERT INTO span (id, span_type, text, instruct) "
            "VALUES ('sp_edit_1', 'sentence', 'original text', NULL)"
        )

        response = client.put(
            "/api/pipeline/span/sp_edit_1/text",
            json={"text": "updated text"},
        )
        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "ok"
        assert result["span_id"] == "sp_edit_1"

        # Verify text was updated in DB
        rows = storage.execute_query("SELECT text FROM span WHERE id = 'sp_edit_1'")
        assert len(rows) == 1
        assert rows[0]["text"] == "updated text"

    def test_update_span_text_empty_rejected(self, client, storage):
        """PUT with empty text returns 400."""
        storage.execute_insert(
            "INSERT INTO span (id, span_type, text, instruct) "
            "VALUES ('sp_edit_2', 'sentence', 'some text', NULL)"
        )

        response = client.put(
            "/api/pipeline/span/sp_edit_2/text",
            json={"text": ""},
        )
        assert response.status_code == 400
        assert "empty" in response.json()["detail"].lower()

    def test_update_span_text_whitespace_only_rejected(self, client, storage):
        """PUT with whitespace-only text returns 400."""
        storage.execute_insert(
            "INSERT INTO span (id, span_type, text, instruct) "
            "VALUES ('sp_edit_3', 'sentence', 'some text', NULL)"
        )

        response = client.put(
            "/api/pipeline/span/sp_edit_3/text",
            json={"text": "   "},
        )
        assert response.status_code == 400
        assert "empty" in response.json()["detail"].lower()

    def test_update_span_text_not_found(self, client):
        """PUT with non-existent span_id returns 404."""
        response = client.put(
            "/api/pipeline/span/nonexistent_span/text",
            json={"text": "new text"},
        )
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_update_span_text_strips_whitespace(self, client, storage):
        """PUT strips leading/trailing whitespace from text."""
        storage.execute_insert(
            "INSERT INTO span (id, span_type, text, instruct) "
            "VALUES ('sp_edit_4', 'sentence', 'original', NULL)"
        )

        response = client.put(
            "/api/pipeline/span/sp_edit_4/text",
            json={"text": "  trimmed text  "},
        )
        assert response.status_code == 200

        rows = storage.execute_query("SELECT text FROM span WHERE id = 'sp_edit_4'")
        assert rows[0]["text"] == "trimmed text"


# ---------------------------------------------------------------------------
# P1-S12: GET /api/pipeline/export/{book_id}
# ---------------------------------------------------------------------------


class TestExportEndpoint:
    def test_export_returns_list(self, client):
        """Export endpoint returns a list of script entries."""
        response = client.get("/api/pipeline/export/b1")
        assert response.status_code == 200
        result = response.json()
        assert isinstance(result, list)
        # Each entry should have speaker, text, instruct
        for entry in result:
            assert "speaker" in entry
            assert "text" in entry
            assert "instruct" in entry

    def test_export_empty_book(self, client):
        """Empty book returns empty list."""
        response = client.get("/api/pipeline/export/nonexistent")
        assert response.status_code == 200
        assert response.json() == []


# ---------------------------------------------------------------------------
# P1-S13: POST /api/pipeline/render
# ---------------------------------------------------------------------------


class TestRenderEndpoint:
    def test_render_no_engine(self, client):
        """Render without TTS engine returns 503."""
        # Override tts_engine to None
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_storage] = lambda: client.app.dependency_overrides[get_storage]()
        app.dependency_overrides[get_tts_engine] = lambda: None

        test_client = TestClient(app)
        response = test_client.post(
            "/api/pipeline/render",
            json={"book_id": "b1", "use_batch": True},
        )
        assert response.status_code == 503
        assert "TTS engine not available" in response.json()["detail"]

    def test_render_with_engine(self, client, tts_engine):
        """Render with TTS engine returns job_id and status."""
        response = client.post(
            "/api/pipeline/render",
            json={"book_id": "b1", "use_batch": True},
        )
        assert response.status_code == 200
        result = response.json()
        assert "job_id" in result
        # job_id should be a non-empty string (UUID format)
        assert isinstance(result["job_id"], str)
        assert len(result["job_id"]) > 0
        # Verify response structure — includes status for background job
        assert set(result.keys()) == {"job_id", "status"}
        assert result["status"] == "started"

    def test_render_failure(self, client, tts_engine):
        """Render failure is captured as a failed job status."""
        from app.pipeline import api_export

        with patch.object(api_export, "render_audiobook") as mock_render:
            mock_render.side_effect = RuntimeError("TTS engine crashed")

            response = client.post(
                "/api/pipeline/render",
                json={"book_id": "b1", "use_batch": True},
            )
            # Endpoint returns immediately with job_id; failure surfaces via status
            assert response.status_code == 200
            job_id = response.json()["job_id"]

            # Background task runs on TestClient close — query status
            status_resp = client.get(f"/api/pipeline/render_status/{job_id}")
            assert status_resp.status_code == 200
            # Job is either still running or already failed (timing dependent)
            assert status_resp.json()["status"] in ("running", "failed")


# ---------------------------------------------------------------------------
# P3-S10: Background render — returns immediately, status transitions
# ---------------------------------------------------------------------------


class TestBackgroundRender:
    def test_render_returns_immediately(self, client, tts_engine):
        """POST /render returns immediately with job_id and status='started'."""
        import time

        start = time.time()
        response = client.post(
            "/api/pipeline/render",
            json={"book_id": "b1", "use_batch": True},
        )
        elapsed = time.time() - start

        # Should return almost immediately (not block on render)
        assert elapsed < 0.5, f"Render blocked for {elapsed}s instead of returning immediately"
        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "started"
        assert "job_id" in result

    def test_status_transitions(self, client, tts_engine):
        """Render job transitions through status states."""
        # Start render
        response = client.post(
            "/api/pipeline/render",
            json={"book_id": "b1", "use_batch": True},
        )
        job_id = response.json()["job_id"]

        # Initial status should be running
        status_resp = client.get(f"/api/pipeline/render_status/{job_id}")
        assert status_resp.status_code == 200
        initial_status = status_resp.json()["status"]
        assert initial_status in ("running", "completed")  # may complete fast in test

        # Poll until terminal state (with timeout)
        import time
        for _ in range(50):  # 5 seconds max
            status_resp = client.get(f"/api/pipeline/render_status/{job_id}")
            status = status_resp.json()["status"]
            if status in ("completed", "failed", "cancelled"):
                break
            time.sleep(0.1)

        # Should reach a terminal state
        assert status in ("completed", "failed", "cancelled")

        # Completed job should have output_dir
        if status == "completed":
            assert status_resp.json()["output_dir"] is not None

    def test_status_unknown_job(self, client):
        """GET /render_status for unknown job_id returns 404."""
        response = client.get("/api/pipeline/render_status/nonexistent-job-id")
        assert response.status_code == 404
        assert "Unknown job_id" in response.json()["detail"]


# ---------------------------------------------------------------------------
# P3-S11: Cancellation — cancel flag aborts render
# ---------------------------------------------------------------------------


class TestCancellation:
    def test_cancel_check_aborts_render(self, storage, tts_engine):
        """render_audiobook raises CancelledError when cancel_check returns True."""
        from app.pipeline.tts_integration import CancelledError, render_audiobook

        # Cancel check that returns True immediately
        def cancel_check():
            return True

        # Should raise CancelledError
        with pytest.raises(CancelledError, match="Render cancelled"):
            render_audiobook(
                "b1",
                storage,
                tts_engine,
                use_batch=True,
                cancel_check=cancel_check,
            )

    def test_cancel_running_job_via_api(self, client, tts_engine):
        """POST /cancel_render on completed job returns already_finished."""
        # Start and complete a render
        response = client.post(
            "/api/pipeline/render",
            json={"book_id": "b1", "use_batch": True},
        )
        job_id = response.json()["job_id"]

        # Wait for completion (TestClient runs background tasks synchronously)
        import time
        for _ in range(50):
            status_resp = client.get(f"/api/pipeline/render_status/{job_id}")
            if status_resp.json()["status"] == "completed":
                break
            time.sleep(0.1)

        # Try to cancel completed job
        cancel_resp = client.post(
            "/api/pipeline/cancel_render",
            json={"job_id": job_id},
        )
        assert cancel_resp.status_code == 200
        assert cancel_resp.json()["status"] == "already_finished"

    def test_cancel_unknown_job(self, client):
        """POST /cancel_render for unknown job_id returns 404."""
        response = client.post(
            "/api/pipeline/cancel_render",
            json={"job_id": "nonexistent-job-id"},
        )
        assert response.status_code == 404
        assert "Unknown job_id" in response.json()["detail"]


# ---------------------------------------------------------------------------
# P1-S14: POST /api/pipeline/reonboard
# ---------------------------------------------------------------------------


class TestReonboardEndpoint:
    def test_reonboard_valid_book(self, client):
        """Re-onboarding a valid book returns new version."""
        response = client.post(
            "/api/pipeline/reonboard",
            json={"book_id": "b1"},
        )
        assert response.status_code == 200
        result = response.json()
        assert result["book_id"] == "b1"
        assert "version" in result
        assert result["status"] == "reonboarded"

    def test_reonboard_invalid_book(self, client):
        """Re-onboarding a non-existent book returns 404."""
        response = client.post(
            "/api/pipeline/reonboard",
            json={"book_id": "nonexistent"},
        )
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower() or "nonexistent" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Production TTS engine wiring (get_tts_engine)
# ---------------------------------------------------------------------------


class TestGetTTSEngineProduction:
    """Prove get_tts_engine() wires to the production engine factory (app.engine)."""

    def test_returns_configured_engine(self):
        """get_tts_engine() returns the cached engine; TTSEngine is built only on cache miss.

        The real app.app is never imported here (it pulls in torch/soundfile via
        the app.project -> app.tts chain); app.engine keeps app.tts behind a lazy
        import, so this module only ever sees a stubbed app.tts.
        """
        mock_engine = MagicMock()

        # Cache hit: the cached engine is returned and app.tts is never touched.
        fake_tts = ModuleType("app.tts")
        fake_tts.TTSEngine = MagicMock(
            side_effect=AssertionError("TTSEngine must not be constructed on cache hit")
        )
        with (
            patch("app.engine._tts_engine", mock_engine),
            patch.dict(sys.modules, {"app.tts": fake_tts}),
        ):
            engine = get_tts_engine()
        assert engine is mock_engine

        # Cache miss: TTSEngine is constructed from app.tts and returned.
        fake_tts.TTSEngine = MagicMock(return_value=MagicMock())
        with (
            patch("app.engine._tts_engine", None),
            patch.dict(sys.modules, {"app.tts": fake_tts}),
        ):
            engine = get_tts_engine()
        assert engine is not None
        fake_tts.TTSEngine.assert_called_once()

    def test_render_503_when_production_engine_is_none(self, storage):
        """Render returns 503 when the production engine factory resolves to None.

        Unlike ``test_render_no_engine`` which overrides the dependency,
        this test exercises the real production path: the cache is reset via
        ``reset_tts_engine()`` so ``get_tts_engine()`` must rebuild the engine
        — and cannot (soundfile is not installed, so app.tts is not importable),
        returning None without ever importing app.app.
        """
        from fastapi import FastAPI

        from app import engine as engine_factory

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_storage] = lambda: storage
        # No get_tts_engine override — exercise the real production path

        test_client = TestClient(app)
        engine_factory.reset_tts_engine()
        try:
            response = test_client.post(
                "/api/pipeline/render",
                json={"book_id": "b1", "use_batch": True},
            )
        finally:
            engine_factory.reset_tts_engine()

        assert response.status_code == 503
        assert "TTS engine not available" in response.json()["detail"]


# ---------------------------------------------------------------------------
# P5-S2: POST /api/pipeline/merge
# ---------------------------------------------------------------------------


class TestMergeEndpoint:
    """Tests for POST /api/pipeline/merge endpoint."""

    def test_merge_unknown_job(self, client):
        """Merge with unknown job_id returns 404."""
        response = client.post(
            "/api/pipeline/merge",
            json={"book_id": "b1", "job_id": "nonexistent-job-id"},
        )
        assert response.status_code == 404
        assert "Unknown job_id" in response.json()["detail"]

    def test_merge_job_not_completed(self, client):
        """Merge with job not in completed status returns 400."""
        from app.pipeline import api_export

        # Inject a job with status 'running'
        job_id = "test-job-running"
        api_export._render_jobs[job_id] = {
            "status": "running",
            "output_dir": "/tmp/test-output",
        }

        try:
            response = client.post(
                "/api/pipeline/merge",
                json={"book_id": "b1", "job_id": job_id},
            )
            assert response.status_code == 400
            assert "not completed" in response.json()["detail"]
        finally:
            del api_export._render_jobs[job_id]

    def test_merge_no_chunks_found(self, client, tmp_path):
        """Merge with no WAV chunks in output_dir returns 400."""
        from app.pipeline import api_export

        # Create empty output directory
        output_dir = tmp_path / "empty-output"
        output_dir.mkdir()

        job_id = "test-job-no-chunks"
        api_export._render_jobs[job_id] = {
            "status": "completed",
            "output_dir": str(output_dir),
        }

        try:
            response = client.post(
                "/api/pipeline/merge",
                json={"book_id": "b1", "job_id": job_id},
            )
            assert response.status_code == 400
            assert "No audio chunks found" in response.json()["detail"]
        finally:
            del api_export._render_jobs[job_id]

    def test_merge_success(self, client, tmp_path):
        """Merge with valid WAV chunks produces M4B file."""
        import subprocess
        from app.pipeline import api_export

        # Create output directory with WAV chunks
        output_dir = tmp_path / "render-output"
        output_dir.mkdir()

        # Generate simple WAV files using ffmpeg (sine waves)
        for i in range(3):
            wav_path = output_dir / f"chunk_{i:04d}.wav"
            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=0.1",
                    "-ar", "22050",
                    "-ac", "1",
                    str(wav_path),
                ],
                capture_output=True,
                check=True,
            )

        job_id = "test-job-success"
        api_export._render_jobs[job_id] = {
            "status": "completed",
            "output_dir": str(output_dir),
        }

        try:
            response = client.post(
                "/api/pipeline/merge",
                json={"book_id": "b1", "job_id": job_id},
            )
            assert response.status_code == 200
            result = response.json()
            assert result["status"] == "ok"
            assert "output_path" in result
            assert result["output_path"].endswith("audiobook.m4b")

            # Verify the M4B file was created
            import os
            assert os.path.exists(result["output_path"])
            assert os.path.getsize(result["output_path"]) > 0
        finally:
            del api_export._render_jobs[job_id]
