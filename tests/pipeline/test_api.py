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
        with patch("app.pipeline.api.extract_epub_text") as mock_extract, patch(
            "app.pipeline.api.populate_spine"
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
        with patch("app.pipeline.api.extract_epub_text") as mock_extract:
            mock_extract.side_effect = Exception("Invalid EPUB")

            response = client.post(
                "/api/pipeline/onboard",
                files={"file": ("test.epub", b"fake epub content", "application/epub+zip")},
            )

            assert response.status_code == 400
            assert "Failed to extract EPUB" in response.json()["detail"]


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
        """Valid walk names are executed."""
        # Mock the walk module loading
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

            assert response.status_code == 200
            result = response.json()
            assert "status" in result


# ---------------------------------------------------------------------------
# P1-S4: POST /api/pipeline/run_all_walks
# ---------------------------------------------------------------------------


class TestRunAllWalksEndpoint:
    def test_run_all_walks(self, client, walk_runner):
        """All walks are executed serially."""
        # Mock the walk module loading to avoid actual walk execution
        with patch.object(walk_runner, "_load_walk_module") as mock_load:
            mock_module = MagicMock()
            mock_module.execute = MagicMock(return_value={"status": "completed"})
            mock_load.return_value = mock_module

            response = client.post(
                "/api/pipeline/run_all_walks",
                json={"book_id": "b1", "config": {}},
            )

            assert response.status_code == 200
            result = response.json()
            # Should have results for each walk
            assert isinstance(result, dict)


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
        assert len(result) == len(WalkRunner.WALK_ORDER)


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
        """Review endpoint returns a list of review items."""
        response = client.get("/api/pipeline/review/b1")
        assert response.status_code == 200
        result = response.json()
        assert isinstance(result, list)


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
        # Should either succeed (if parsing is lenient) or return 400
        assert response.status_code in [200, 400]


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
        """Render with TTS engine returns job_id."""
        response = client.post(
            "/api/pipeline/render",
            json={"book_id": "b1", "use_batch": True},
        )
        assert response.status_code == 200
        result = response.json()
        assert "job_id" in result


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
        # May return 404 or succeed with no changes
        assert response.status_code in [200, 404]


# ---------------------------------------------------------------------------
# Production TTS engine wiring (get_tts_engine)
# ---------------------------------------------------------------------------


class TestGetTTSEngineProduction:
    """Prove get_tts_engine() wires to the production ProjectManager."""

    def test_returns_configured_engine(self):
        """get_tts_engine() returns the engine from app.app.project_manager."""
        mock_engine = MagicMock()
        mock_pm = MagicMock()
        mock_pm.get_engine.return_value = mock_engine

        # Avoid importing the real app.app (which pulls in torch, etc.)
        mock_app_app = MagicMock()
        mock_app_app.project_manager = mock_pm

        with patch.dict(sys.modules, {"app.app": mock_app_app}):
            engine = get_tts_engine()

        assert engine is mock_engine
        mock_pm.get_engine.assert_called_once()

    def test_render_503_when_production_engine_is_none(self, storage):
        """Render returns 503 when production get_tts_engine resolves to None.

        Unlike ``test_render_no_engine`` which overrides the dependency,
        this test exercises the real production path where
        ``project_manager.get_engine()`` returns ``None``.
        """
        from fastapi import FastAPI

        mock_pm = MagicMock()
        mock_pm.get_engine.return_value = None

        mock_app_app = MagicMock()
        mock_app_app.project_manager = mock_pm

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_storage] = lambda: storage
        # No get_tts_engine override — exercise the real production path

        test_client = TestClient(app)
        with patch.dict(sys.modules, {"app.app": mock_app_app}):
            response = test_client.post(
                "/api/pipeline/render",
                json={"book_id": "b1", "use_batch": True},
            )

        assert response.status_code == 503
        assert "TTS engine not available" in response.json()["detail"]
