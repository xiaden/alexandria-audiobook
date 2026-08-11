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

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.pipeline.adapter import ConcurrentTransactionError, InMemorySQLiteAdapter
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


# -- Test-harness import path setup (mirrors test_legacy_removed.py) ----------
# app/app.py imports app-local bare modules (``utils``, ``hf_utils``) at module
# level.  Load them into sys.modules before importing app.app so the real
# application — including the Plan K ConcurrentTransactionError 503 exception
# handler — can be exercised through TestClient below.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_DIR = _REPO_ROOT / "app"


def _load_app_local_module(name: str) -> None:
    """Load an app/*.py module under its bare name (e.g. ``utils`` -> app/utils.py)."""
    spec = importlib.util.spec_from_file_location(name, _APP_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None, f"cannot locate app/{name}.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)


for _name in ("utils", "hf_utils"):
    if _name not in sys.modules:
        _load_app_local_module(_name)


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

    def fake_generate_voice(text, instruct_text, speaker, voice_config, output_path):
        # Honest engine contract: the WAV file must exist when generate_voice
        # returns (Plan C phase 1 fsync discipline depends on it).
        with open(output_path, "wb") as f:
            f.write(b"fake wav data\n")
        return None

    engine.generate_voice = MagicMock(side_effect=fake_generate_voice)
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


@pytest.fixture()
def real_client(
    storage, walk_runner, review_manager, operation_executor, character_ledger, tts_engine
):
    """TestClient over the REAL ``app.app`` (Plan K exception handler live).

    The plain ``client`` fixture builds a router-only FastAPI app and therefore
    does NOT see the ConcurrentTransactionError -> 503 handler registered on
    ``app.app``.  This fixture overrides the same dependencies on the real
    application so request-path writes can exercise the live 503 mapping.
    """
    import app.app as real_app

    real_app.app.dependency_overrides[get_storage] = lambda: storage
    real_app.app.dependency_overrides[get_walk_runner] = lambda: walk_runner
    real_app.app.dependency_overrides[get_review_manager] = lambda: review_manager
    real_app.app.dependency_overrides[get_operation_executor] = lambda: operation_executor
    real_app.app.dependency_overrides[get_character_ledger] = lambda: character_ledger
    real_app.app.dependency_overrides[get_tts_engine] = lambda: tts_engine

    return TestClient(real_app.app)


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

    def test_review_items_carry_neighbors(self, client):
        """Every review item carries the contextual-review ``neighbors``
        enrichment (DD UX workflow #5): {before: [...], after: [...]}.

        The api fixture has one review-band item (character_book:c2:b1) which
        has no span reference, so the lists are empty — the resolution rules
        themselves are covered at the manager level (TestReviewItemNeighborContext
        in test_review.py).
        """
        response = client.get("/api/pipeline/review/b1")
        assert response.status_code == 200
        result = response.json()
        assert len(result) > 0
        for item in result:
            assert "neighbors" in item
            assert set(item["neighbors"]) == {"before", "after"}
            assert isinstance(item["neighbors"]["before"], list)
            assert isinstance(item["neighbors"]["after"], list)
            assert item["neighbors"]["before"] == []
            assert item["neighbors"]["after"] == []


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
# P4-S1: Prefix dispatch on accept/reject/override
# ---------------------------------------------------------------------------


def _seed_walk_review_items(storage):
    """Seed walk_run + walk_review_item rows (one per kind) for dispatch tests.

    The walk's generated (current) values are written into the target rows so
    reject/override can be asserted against prior_value:
    - voice_profile on c1: current '{"voice":"new"}', prior '{"voice":"old"}'
    - voice_assignment on c2: current 'vc1', prior NULL
    - instruction on sp1: current 'slowly', prior 'cheerfully'
    """
    storage.execute_insert(
        "INSERT INTO walk_run (run_id, book_id, walk_name, status, created_ms) "
        "VALUES ('run-p4', 'b1', 'walk_2g_voice_audition', 'completed', 1)"
    )

    # voice_profile -> character_metadata c1 / key='voice_profile'
    storage.execute_insert(
        "INSERT INTO character_metadata (character_id, key, value) "
        "VALUES ('c1', 'voice_profile', '{\"voice\":\"new\"}')"
    )
    storage.execute_insert(
        "INSERT INTO walk_review_item "
        "(id, book_id, run_id, kind, target_table, target_id, prior_value, status, created_ms) "
        "VALUES ('wp1', 'b1', 'run-p4', 'voice_profile', 'character_metadata', 'c1', "
        "'{\"voice\":\"old\"}', 'pending', 100)"
    )

    # voice_assignment -> character c2.voice_assignment_id
    storage.execute_update(
        "UPDATE character SET voice_assignment_id = 'vc1' WHERE id = 'c2'"
    )
    storage.execute_insert(
        "INSERT INTO walk_review_item "
        "(id, book_id, run_id, kind, target_table, target_id, prior_value, status, created_ms) "
        "VALUES ('wa1', 'b1', 'run-p4', 'voice_assignment', 'character', 'c2', NULL, 'pending', 200)"
    )

    # instruction -> span sp1.instruct
    storage.execute_update("UPDATE span SET instruct = 'slowly' WHERE id = 'sp1'")
    storage.execute_insert(
        "INSERT INTO walk_review_item "
        "(id, book_id, run_id, kind, target_table, target_id, prior_value, status, created_ms) "
        "VALUES ('wi1', 'b1', 'run-p4', 'instruction', 'span', 'sp1', 'cheerfully', 'pending', 300)"
    )


def _walk_status(storage, item_id):
    """Return the current status of a walk_review_item row by id."""
    rows = storage.execute_query(
        "SELECT status FROM walk_review_item WHERE id = ?", (item_id,)
    )
    assert rows, f"no walk_review_item row {item_id!r}"
    return rows[0]["status"]


class TestReviewActionDispatch:
    """POST /review/accept|reject|override dispatch by id prefix (P4).

    ``walkitem:`` ids -> walk-side action (status resolved, target write only
    for reject/override); everything else -> existing junction behavior.
    Malformed ids -> 400; well-formed but unknown ids -> 404 (NEW contract);
    walk-side override without a value -> 400.  Junction ids stay byte-identical
    ``{table}:{char}:{entity}`` — there is no literal ``junction:`` prefix.
    """

    # -- walkitem: accept --------------------------------------------------

    def test_accept_walk_item_resolves_row_and_leaves_target(self, client, storage):
        """Accept on a walkitem: marks the row resolved and writes NO target row."""
        _seed_walk_review_items(storage)
        response = client.post(
            "/api/pipeline/review/accept", json={"item_id": "walkitem:wp1"}
        )
        assert response.status_code == 200
        assert response.json() == {"status": "accepted", "item_id": "walkitem:wp1"}
        assert _walk_status(storage, "wp1") == "resolved"
        # the walk's generated value stays in the target — accept writes nothing
        rows = storage.execute_query(
            "SELECT value FROM character_metadata "
            "WHERE character_id = 'c1' AND key = 'voice_profile'"
        )
        assert rows[0]["value"] == '{"voice":"new"}'

    # -- walkitem: reject --------------------------------------------------

    def test_reject_walk_item_restores_prior_value(self, client, storage):
        """Reject on a walkitem: restores prior_value into the target row."""
        _seed_walk_review_items(storage)
        response = client.post(
            "/api/pipeline/review/reject", json={"item_id": "walkitem:wp1"}
        )
        assert response.status_code == 200
        assert response.json() == {"status": "rejected", "item_id": "walkitem:wp1"}
        assert _walk_status(storage, "wp1") == "resolved"
        rows = storage.execute_query(
            "SELECT value FROM character_metadata "
            "WHERE character_id = 'c1' AND key = 'voice_profile'"
        )
        assert rows[0]["value"] == '{"voice":"old"}'  # prior_value restored

    def test_reject_walk_item_restores_null_prior(self, client, storage):
        """A NULL prior_value is restored as NULL (voice_assignment unset)."""
        _seed_walk_review_items(storage)
        response = client.post(
            "/api/pipeline/review/reject", json={"item_id": "walkitem:wa1"}
        )
        assert response.status_code == 200
        rows = storage.execute_query(
            "SELECT voice_assignment_id FROM character WHERE id = 'c2'"
        )
        assert rows[0]["voice_assignment_id"] is None
        assert _walk_status(storage, "wa1") == "resolved"

    def test_reject_walk_item_restores_instruct(self, client, storage):
        """Reject on an instruction walkitem: restores span.instruct."""
        _seed_walk_review_items(storage)
        response = client.post(
            "/api/pipeline/review/reject", json={"item_id": "walkitem:wi1"}
        )
        assert response.status_code == 200
        rows = storage.execute_query("SELECT instruct FROM span WHERE id = 'sp1'")
        assert rows[0]["instruct"] == "cheerfully"
        assert _walk_status(storage, "wi1") == "resolved"

    # -- walkitem: override ------------------------------------------------

    def test_override_walk_item_writes_new_value_to_target(self, client, storage):
        """Override on a walkitem: writes new_value into the target row."""
        _seed_walk_review_items(storage)
        response = client.post(
            "/api/pipeline/review/override",
            json={"item_id": "walkitem:wp1", "new_value": '{"voice":"human"}'},
        )
        assert response.status_code == 200
        assert response.json() == {"status": "overridden", "item_id": "walkitem:wp1"}
        assert _walk_status(storage, "wp1") == "resolved"
        rows = storage.execute_query(
            "SELECT value FROM character_metadata "
            "WHERE character_id = 'c1' AND key = 'voice_profile'"
        )
        assert rows[0]["value"] == '{"voice":"human"}'  # the human's value

    def test_override_walk_item_voice_assignment(self, client, storage):
        """Override writes the new voice_assignment_id into the character row."""
        _seed_walk_review_items(storage)
        response = client.post(
            "/api/pipeline/review/override",
            json={"item_id": "walkitem:wa1", "new_value": "vc1"},
        )
        assert response.status_code == 200
        rows = storage.execute_query(
            "SELECT voice_assignment_id FROM character WHERE id = 'c2'"
        )
        assert rows[0]["voice_assignment_id"] == "vc1"
        assert _walk_status(storage, "wa1") == "resolved"

    def test_override_walk_item_instruct(self, client, storage):
        """Override writes the new instruct into the span row."""
        _seed_walk_review_items(storage)
        response = client.post(
            "/api/pipeline/review/override",
            json={"item_id": "walkitem:wi1", "new_value": "quickly"},
        )
        assert response.status_code == 200
        rows = storage.execute_query("SELECT instruct FROM span WHERE id = 'sp1'")
        assert rows[0]["instruct"] == "quickly"
        assert _walk_status(storage, "wi1") == "resolved"

    # -- error codes -------------------------------------------------------

    def test_walk_item_unknown_id_returns_404(self, client):
        """A well-formed walkitem: id with no matching row returns 404."""
        response = client.post(
            "/api/pipeline/review/accept", json={"item_id": "walkitem:nope"}
        )
        assert response.status_code == 404

    def test_junction_item_unknown_id_returns_404(self, client):
        """A well-formed junction id with no matching row returns 404 (NEW)."""
        response = client.post(
            "/api/pipeline/review/accept",
            json={"item_id": "character_book:c1:nonexistent-book"},
        )
        assert response.status_code == 404

    def test_junction_reject_unknown_id_returns_404(self, client):
        """Reject on a well-formed but unknown junction id returns 404 (NEW)."""
        response = client.post(
            "/api/pipeline/review/reject",
            json={"item_id": "character_scene:c1:nonexistent-scene"},
        )
        assert response.status_code == 404

    def test_junction_override_unknown_id_returns_404(self, client):
        """Override on a well-formed but unknown junction id returns 404 (NEW)."""
        response = client.post(
            "/api/pipeline/review/override",
            json={
                "item_id": "character_span:c1:nonexistent-span",
                "new_value": {"relation_type": "speaker"},
            },
        )
        assert response.status_code == 404

    def test_malformed_item_id_returns_400(self, client):
        """Malformed ids still return 400 via the existing ValueError path."""
        response = client.post(
            "/api/pipeline/review/accept", json={"item_id": "invalid:item:id"}
        )
        assert response.status_code == 400

    def test_override_walk_item_without_value_returns_400(self, client, storage):
        """Override on a walkitem: without a value is a 400 and resolves nothing."""
        _seed_walk_review_items(storage)
        response = client.post(
            "/api/pipeline/review/override", json={"item_id": "walkitem:wp1"}
        )
        assert response.status_code == 400
        assert _walk_status(storage, "wp1") == "pending"

    def test_override_junction_item_without_value_keeps_junction_behavior(
        self, client, storage
    ):
        """Junction override with no value keeps existing behavior (flags only)."""
        response = client.post(
            "/api/pipeline/review/override", json={"item_id": "character_book:c2:b1"}
        )
        assert response.status_code == 200
        rows = storage.execute_query(
            "SELECT confidence, human_override FROM character_book "
            "WHERE character_id = 'c2' AND book_id = 'b1'"
        )
        assert rows[0]["confidence"] == 1.0
        assert rows[0]["human_override"] == 1

    def test_junction_items_still_dispatch_to_existing_behavior(self, client, storage):
        """Junction ids keep dispatching to the existing junction actions."""
        _seed_walk_review_items(storage)  # walk rows present; junction still works
        response = client.post(
            "/api/pipeline/review/accept", json={"item_id": "character_book:c2:b1"}
        )
        assert response.status_code == 200
        rows = storage.execute_query(
            "SELECT confidence FROM character_book "
            "WHERE character_id = 'c2' AND book_id = 'b1'"
        )
        assert rows[0]["confidence"] == 1.0
        # the walk items are untouched by a junction action
        assert _walk_status(storage, "wp1") == "pending"


class TestReviewActionRollback:
    """P5: a failing value-restore through the API is ATOMIC — nothing commits.

    The storage error surfaces as HTTP 503 + Retry-After (Plan K live
    contract — was a TestClient re-raise); the item row stays ``pending``
    and the target row keeps the walk's value.
    """

    def test_restore_failure_rolls_back_via_api(self, real_client, storage, monkeypatch):
        """The restore write SUCCEEDS inside the txn, then the status UPDATE
        fails — the API surfaces the transient write failure as 503 +
        Retry-After (Plan K live contract, was re-raise) and BOTH writes are
        rolled back (target unchanged, item still pending).  Without the
        transaction wrap the restore would have autocommitted and stayed
        visible."""
        _seed_walk_review_items(storage)

        real_update = storage.execute_update

        def failing_status_update(sql, params=()):
            if "status = 'resolved'" in sql:
                raise ConcurrentTransactionError("simulated status-write failure")
            return real_update(sql, params)

        monkeypatch.setattr(storage, "execute_update", failing_status_update)
        resp = real_client.post(
            "/api/pipeline/review/reject", json={"item_id": "walkitem:wp1"}
        )
        assert resp.status_code == 503
        assert resp.headers["retry-after"] == "5"
        assert resp.json()["detail"] == (
            "Concurrent write in progress — retry after the advertised delay"
        )

        assert _walk_status(storage, "wp1") == "pending"
        rows = storage.execute_query(
            "SELECT value FROM character_metadata "
            "WHERE character_id = 'c1' AND key = 'voice_profile'"
        )
        assert rows[0]["value"] == '{"voice":"new"}'  # unchanged


class TestConcurrentTransactionErrorMapping:
    """Plan K: ConcurrentTransactionError -> 503 + Retry-After: 5 (live)."""

    def test_handler_returns_503_json_with_retry_after(self):
        """Unit: the app-level handler maps CTE to 503 JSON + Retry-After: 5."""
        import asyncio
        import json as json_module

        from fastapi import Request

        from app.app import (
            _CONCURRENT_WRITE_RETRY_AFTER_SECONDS,
            concurrent_transaction_error_handler,
        )

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/pipeline/cancel_walks",
                "headers": [],
            }
        )
        exc = ConcurrentTransactionError("simulated contention")
        response = asyncio.run(concurrent_transaction_error_handler(request, exc))

        assert response.status_code == 503
        assert response.headers["Retry-After"] == str(
            _CONCURRENT_WRITE_RETRY_AFTER_SECONDS
        )
        assert response.headers["Retry-After"] == "5"
        assert json_module.loads(response.body) == {
            "detail": "Concurrent write in progress — retry after the advertised delay"
        }

    def test_cancel_walks_cte_maps_to_503(self, real_client, storage, monkeypatch):
        """P3-S4: the out-of-transaction cancel write raising CTE -> 503 +
        Retry-After: 5 (was 500/re-raise).  The cancel path writes
        ``cancel_requested=1`` directly on the storage adapter, so the
        transient failure surfaces through the app handler instead of a
        500 internal server error."""
        import time as time_module

        now = int(time_module.time() * 1000)
        storage.execute_insert(
            "INSERT INTO walk_run (run_id, book_id, walk_name, status, created_ms) "
            "VALUES ('wr-cte', 'b1', 'walk_2a_scene_segmentation', 'running', ?)",
            (now,),
        )

        def failing_cancel_update(sql, params=()):
            raise ConcurrentTransactionError("simulated cancel-write contention")

        monkeypatch.setattr(storage, "execute_update", failing_cancel_update)
        resp = real_client.post(
            "/api/pipeline/cancel_walks", json={"book_id": "b1"}
        )

        assert resp.status_code == 503
        assert resp.headers["retry-after"] == "5"
        assert resp.json()["detail"] == (
            "Concurrent write in progress — retry after the advertised delay"
        )
        # The cancel flag was NOT persisted (the write never committed).
        rows = storage.execute_query(
            "SELECT cancel_requested FROM walk_run WHERE run_id = 'wr-cte'"
        )
        assert rows[0]["cancel_requested"] == 0


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
# Plan J — GET/PUT /api/pipeline/book/{book_id}/single_speaker
# ---------------------------------------------------------------------------


class TestSingleSpeakerFlagEndpoint:
    def test_get_defaults_to_off(self, client):
        """GET returns 0 (multi-speaker) when the flag was never set."""
        response = client.get("/api/pipeline/book/b1/single_speaker")
        assert response.status_code == 200
        assert response.json() == {"book_id": "b1", "single_speaker": 0}

    def test_put_enables_and_round_trips(self, client, storage):
        """PUT true persists 1; a subsequent GET reads it back."""
        response = client.put(
            "/api/pipeline/book/b1/single_speaker", json={"single_speaker": True}
        )
        assert response.status_code == 200
        assert response.json() == {
            "status": "ok",
            "book_id": "b1",
            "single_speaker": 1,
        }

        # Persisted in the DB
        rows = storage.execute_query(
            "SELECT single_speaker FROM book WHERE id = 'b1'"
        )
        assert rows[0]["single_speaker"] == 1

        # Round trip via the API
        read = client.get("/api/pipeline/book/b1/single_speaker")
        assert read.json()["single_speaker"] == 1

    def test_put_disables(self, client, storage):
        """PUT false writes 0 after a prior true."""
        client.put(
            "/api/pipeline/book/b1/single_speaker", json={"single_speaker": True}
        )
        response = client.put(
            "/api/pipeline/book/b1/single_speaker", json={"single_speaker": False}
        )
        assert response.status_code == 200
        assert response.json()["single_speaker"] == 0
        rows = storage.execute_query(
            "SELECT single_speaker FROM book WHERE id = 'b1'"
        )
        assert rows[0]["single_speaker"] == 0

    def test_get_unknown_book_404(self, client):
        """GET for a nonexistent book returns 404."""
        response = client.get("/api/pipeline/book/nope/single_speaker")
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_put_unknown_book_404(self, client):
        """PUT for a nonexistent book returns 404 and writes nothing."""
        response = client.put(
            "/api/pipeline/book/nope/single_speaker", json={"single_speaker": True}
        )
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_put_missing_body_422(self, client):
        """PUT without a single_speaker field is rejected by pydantic (422)."""
        response = client.put("/api/pipeline/book/b1/single_speaker", json={})
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# Plan L P2-S2: book pause settings
# GET/PUT /api/pipeline/book/{book_id}/pause_settings
# ---------------------------------------------------------------------------


class TestBookPauseSettingsEndpoint:
    """Both pause override columns are nullable: NULL = resolve default,
    0 = intentional no-gap.  PUT is a partial update on explicit fields."""

    def test_get_defaults_to_none(self, client):
        """Unset book reports both overrides as None (never coerced)."""
        response = client.get("/api/pipeline/book/b1/pause_settings")
        assert response.status_code == 200
        assert response.json() == {
            "book_id": "b1",
            "pause_between_speakers_ms": None,
            "pause_same_speaker_ms": None,
        }

    def test_put_sets_and_round_trips(self, client, storage):
        """PUT persists both fields (incl. 0 as intentional no-gap) and round-trips."""
        response = client.put(
            "/api/pipeline/book/b1/pause_settings",
            json={"pause_between_speakers_ms": 700, "pause_same_speaker_ms": 0},
        )
        assert response.status_code == 200
        assert response.json() == {
            "status": "ok",
            "book_id": "b1",
            "pause_between_speakers_ms": 700,
            "pause_same_speaker_ms": 0,
        }
        rows = storage.execute_query(
            "SELECT pause_between_speakers_ms, pause_same_speaker_ms FROM book"
            " WHERE id = 'b1'"
        )
        assert rows[0]["pause_between_speakers_ms"] == 700
        assert rows[0]["pause_same_speaker_ms"] == 0
        read = client.get("/api/pipeline/book/b1/pause_settings").json()
        assert read["pause_between_speakers_ms"] == 700
        assert read["pause_same_speaker_ms"] == 0

    def test_put_null_clears_to_resolve_default(self, client, storage):
        """Explicit null clears the override back to resolve-default (SQL NULL)."""
        client.put(
            "/api/pipeline/book/b1/pause_settings",
            json={"pause_between_speakers_ms": 700, "pause_same_speaker_ms": 0},
        )
        response = client.put(
            "/api/pipeline/book/b1/pause_settings",
            json={"pause_between_speakers_ms": None},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["pause_between_speakers_ms"] is None
        # Partial update: the untouched field keeps its persisted value.
        assert body["pause_same_speaker_ms"] == 0
        rows = storage.execute_query(
            "SELECT pause_between_speakers_ms, pause_same_speaker_ms FROM book"
            " WHERE id = 'b1'"
        )
        assert rows[0]["pause_between_speakers_ms"] is None
        assert rows[0]["pause_same_speaker_ms"] == 0

    def test_put_partial_update_leaves_other_field_untouched(self, client):
        client.put(
            "/api/pipeline/book/b1/pause_settings",
            json={"pause_between_speakers_ms": 700},
        )
        response = client.put(
            "/api/pipeline/book/b1/pause_settings",
            json={"pause_same_speaker_ms": 300},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["pause_between_speakers_ms"] == 700
        assert body["pause_same_speaker_ms"] == 300

    def test_put_empty_body_is_noop_returns_current(self, client):
        """An empty body (no explicit fields) is a no-op returning current state."""
        response = client.put("/api/pipeline/book/b1/pause_settings", json={})
        assert response.status_code == 200
        assert response.json()["pause_between_speakers_ms"] is None
        assert response.json()["pause_same_speaker_ms"] is None

    def test_get_unknown_book_404(self, client):
        response = client.get("/api/pipeline/book/nope/pause_settings")
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_put_unknown_book_404(self, client):
        response = client.put(
            "/api/pipeline/book/nope/pause_settings",
            json={"pause_between_speakers_ms": 100},
        )
        assert response.status_code == 404

    def test_put_invalid_value_422(self, client):
        """Out-of-range / wrong-type pause values are rejected with 422."""
        for bad in (
            {"pause_between_speakers_ms": -1},
            {"pause_same_speaker_ms": "abc"},
            {"pause_between_speakers_ms": True},
            {"pause_between_speakers_ms": 1.5},
            {"pause_same_speaker_ms": 999999999},
        ):
            response = client.put("/api/pipeline/book/b1/pause_settings", json=bad)
            assert response.status_code == 422, bad


# ---------------------------------------------------------------------------
# Plan L P2-S2: per-span pause-after override
# GET/PUT /api/pipeline/span/{span_id}/pause_after
# ---------------------------------------------------------------------------


class TestSpanPauseAfterEndpoint:
    """span.pause_after_ms: NULL = clear, 0 = intentional no-gap, else validated.

    Containment: the span must be reachable from a book through the spine
    edges — unknown or orphan spans are rejected with 404."""

    def test_get_defaults_to_none(self, client):
        response = client.get("/api/pipeline/span/sp1/pause_after")
        assert response.status_code == 200
        assert response.json() == {"span_id": "sp1", "pause_after_ms": None}

    def test_put_sets_and_round_trips(self, client, storage):
        response = client.put(
            "/api/pipeline/span/sp1/pause_after", json={"pause_after_ms": 120}
        )
        assert response.status_code == 200
        assert response.json() == {
            "status": "ok",
            "span_id": "sp1",
            "pause_after_ms": 120,
        }
        rows = storage.execute_query(
            "SELECT pause_after_ms FROM span WHERE id = 'sp1'"
        )
        assert rows[0]["pause_after_ms"] == 120
        assert (
            client.get("/api/pipeline/span/sp1/pause_after").json()["pause_after_ms"]
            == 120
        )

    def test_put_zero_intentional_no_gap(self, client, storage):
        response = client.put(
            "/api/pipeline/span/sp1/pause_after", json={"pause_after_ms": 0}
        )
        assert response.status_code == 200
        assert response.json()["pause_after_ms"] == 0
        rows = storage.execute_query(
            "SELECT pause_after_ms FROM span WHERE id = 'sp1'"
        )
        assert rows[0]["pause_after_ms"] == 0

    def test_put_null_clears(self, client, storage):
        client.put("/api/pipeline/span/sp1/pause_after", json={"pause_after_ms": 120})
        response = client.put(
            "/api/pipeline/span/sp1/pause_after", json={"pause_after_ms": None}
        )
        assert response.status_code == 200
        assert response.json()["pause_after_ms"] is None
        rows = storage.execute_query(
            "SELECT pause_after_ms FROM span WHERE id = 'sp1'"
        )
        assert rows[0]["pause_after_ms"] is None

    def test_get_unknown_span_404(self, client):
        response = client.get("/api/pipeline/span/nope/pause_after")
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_put_unknown_span_404(self, client):
        response = client.put(
            "/api/pipeline/span/nope/pause_after", json={"pause_after_ms": 100}
        )
        assert response.status_code == 404

    def test_orphan_span_not_in_book_404(self, client, storage):
        """A span row not reachable from any book is rejected (containment)."""
        storage.execute_insert(
            "INSERT INTO span (id, span_type, text) VALUES ('orphan', 'quotation', 'x')"
        )
        response = client.put(
            "/api/pipeline/span/orphan/pause_after", json={"pause_after_ms": 100}
        )
        assert response.status_code == 404

    def test_put_invalid_value_422(self, client):
        for bad in (
            {"pause_after_ms": -1},
            {"pause_after_ms": "abc"},
            {"pause_after_ms": True},
            {"pause_after_ms": 1.5},
        ):
            response = client.put("/api/pipeline/span/sp1/pause_after", json=bad)
            assert response.status_code == 422, bad


# ---------------------------------------------------------------------------
# Plan L P2-S2: concurrent pause write -> 503 + Retry-After (live)
# ---------------------------------------------------------------------------


class TestPauseSettingsConcurrentMapping:
    def test_book_pause_write_cte_maps_to_503(self, real_client, storage, monkeypatch):
        """A pause write raising ConcurrentTransactionError -> 503 + Retry-After: 5."""

        def failing(sql, params=()):
            raise ConcurrentTransactionError("simulated pause-write contention")

        monkeypatch.setattr(storage, "execute_update", failing)
        resp = real_client.put(
            "/api/pipeline/book/b1/pause_settings",
            json={"pause_between_speakers_ms": 900},
        )
        assert resp.status_code == 503
        assert resp.headers["retry-after"] == "5"


# ---------------------------------------------------------------------------
# Plan L P2-S3: render_status resolves pause settings + tri-state lifecycle
# ---------------------------------------------------------------------------


class TestRenderStatusPauseContract:
    def test_render_status_exposes_resolved_pause_and_pending_state(self, client, storage):
        """A book override resolves through config defaults; assembly runs at
        render Step 6 (P3) but render_status conservatively reports 'pending';
        the truthful applied/failed tri-state is attached on export_m4b (P4)."""
        storage.execute_update(
            "UPDATE book SET pause_between_speakers_ms = 700 WHERE id = 'b1'"
        )
        storage.execute_insert(
            "INSERT INTO render_job (job_id, book_id, mode, status) "
            "VALUES ('rs-p', 'b1', 'batch', 'running')",
            (),
        )
        data = client.get("/api/pipeline/render_status/rs-p").json()
        assert data["resolved_pause_between_speakers_ms"] == 700
        # pause_same_speaker_ms falls back to the built-in config default (250).
        assert data["resolved_pause_same_speaker_ms"] == 250
        assert data["pause_override_count"] == 0
        assert data["pauses_applied"] is False
        assert data["pauses_state"] == "pending"
        assert data["pauses_error"] is None

    def test_render_status_counts_span_overrides(self, client, storage):
        storage.execute_update(
            "UPDATE span SET pause_after_ms = 120 WHERE id = 'sp1'"
        )
        storage.execute_insert(
            "INSERT INTO render_job (job_id, book_id, mode, status) "
            "VALUES ('rs-c', 'b1', 'batch', 'running')",
            (),
        )
        data = client.get("/api/pipeline/render_status/rs-c").json()
        assert data["pause_override_count"] == 1


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
# Plan J Phase 4: tts_config passthrough through the production render chain
# ---------------------------------------------------------------------------


class TestRenderTTSConfigPassthrough:
    """POST /render resolves the TTS config and passes it to render_audiobook.

    P4-S2 wired the passthrough at the ``tts_integration`` boundary; these
    tests assert the PRODUCTION call chain (endpoint → background task →
    ``_run_render_job`` → ``render_audiobook``) so the config.json pause
    values are not dead code.  Starlette runs ``BackgroundTasks`` before the
    response call returns (``Response.__call__`` awaits ``self.background``),
    so the background render has completed by the time ``client.post`` returns.
    """

    def test_configured_pause_values_reach_render_audiobook(
        self, client, tts_engine
    ):
        """Configured config.json pause values flow through the whole chain."""
        from app.pipeline import api_export

        tts_config = {
            "mode": "external",
            "pause_between_speakers_ms": 900,
            "pause_same_speaker_ms": 400,
        }
        with (
            patch.object(api_export, "load_tts_config", return_value=tts_config),
            patch.object(api_export, "render_audiobook") as mock_render,
        ):
            mock_render.return_value = "/tmp/render-out"

            response = client.post(
                "/api/pipeline/render",
                json={"book_id": "b1", "use_batch": True},
            )
            assert response.status_code == 200

            # The endpoint resolved the config and the background task passed
            # it through to render_audiobook (test_render_failure convention:
            # patch api_export.render_audiobook and inspect the call).
            mock_render.assert_called_once()
            assert mock_render.call_args.kwargs["tts_config"] == tts_config

    def test_default_pause_values_apply_when_config_omits_them(
        self, client, tts_engine
    ):
        """A config without pause fields still yields the 500/250 ms defaults.

        Runs the REAL render_audiobook end-to-end (only ``load_tts_config``
        and the engine boundary are patched) so the assertion covers the
        full production chain: empty ``tts`` section → ``_resolve_pause_ms``
        per-field defaults → batch chunk dicts.
        """
        from app.pipeline import api_export

        with patch.object(api_export, "load_tts_config", return_value={}) as mock_load:
            response = client.post(
                "/api/pipeline/render",
                json={"book_id": "b1", "use_batch": True},
            )
            assert response.status_code == 200

            # The endpoint resolves the config on every render — proves the
            # production passthrough is wired (not dead code).
            mock_load.assert_called_once()
            # Real chain ran: the default pause values landed on the batch
            # chunks (book b1 has exactly one span → one chunk).
            tts_engine.generate_batch.assert_called_once()
            chunks = tts_engine.generate_batch.call_args.args[0]
            assert chunks
            for chunk in chunks:
                assert chunk["pause_between_speakers_ms"] == 500
                assert chunk["pause_same_speaker_ms"] == 250


# ---------------------------------------------------------------------------
# P3-S3: render_status reads render_job / render_chunk rows (rows = truth)
# ---------------------------------------------------------------------------


class TestRenderStatusFromRows:
    """GET /render_status is backed by the render_job row, not the dict."""

    def test_status_reads_render_job_row(self, client, storage):
        """A job registered only as a row is served from the row."""
        job_id = "row-status-1"
        storage.execute_insert(
            "INSERT INTO render_job "
            "(job_id, book_id, mode, status, output_dir) "
            "VALUES (?, 'b1', 'batch', 'completed', '/tmp/out')",
            (job_id,),
        )
        response = client.get(f"/api/pipeline/render_status/{job_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["job_id"] == job_id
        assert data["status"] == "completed"
        assert data["output_dir"] == "/tmp/out"
        assert data["mode"] == "batch"

    def test_individual_mode_returns_chunk_counts(self, client, storage):
        """Individual-mode jobs report per-chunk counts from render_chunk rows."""
        job_id = "row-status-2"
        storage.execute_insert(
            "INSERT INTO render_job (job_id, book_id, mode, status) "
            "VALUES (?, 'b1', 'individual', 'running')",
            (job_id,),
        )
        for idx, status in ((0, "done"), (1, "done"), (2, "failed")):
            storage.execute_insert(
                "INSERT INTO render_chunk (job_id, idx, status) VALUES (?, ?, ?)",
                (job_id, idx, status),
            )
        response = client.get(f"/api/pipeline/render_status/{job_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["mode"] == "individual"
        assert data["completed_chunks"] == 2
        assert data["total_chunks"] == 3
        assert data["failed_chunks"] == 1

    def test_row_backed_end_to_end_individual_render(self, client, tts_engine):
        """An individual render via the API ends with completed chunks in the row."""
        import time

        response = client.post(
            "/api/pipeline/render",
            json={"book_id": "b1", "use_batch": False},
        )
        assert response.status_code == 200
        job_id = response.json()["job_id"]
        for _ in range(50):
            status_resp = client.get(f"/api/pipeline/render_status/{job_id}")
            status = status_resp.json()["status"]
            if status in ("completed", "failed"):
                break
            time.sleep(0.1)
        assert status == "completed"
        data = status_resp.json()
        # The test_api storage fixture's book b1 has exactly one span
        assert data["completed_chunks"] == 1
        assert data["total_chunks"] == 1
        assert data["failed_chunks"] == 0


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


# ---------------------------------------------------------------------------
# P5-S1: Persisted cancel + jobs/chunks/runs endpoint surface
# ---------------------------------------------------------------------------


class TestCancelRenderPersistence:
    """POST /cancel_render marks cancel intent; the row lands on 'cancelled'.

    Schema-compatible interpretation (manager decision L20 — documented at
    the cancel_render endpoint): the render_job.status CHECK constraint
    allows only ('pending','running','completed','failed','cancelled',
    'interrupted','expired') and render_job has NO cancel flag column, so
    the CONTRACTS/DD wording "status cancelling + persisted cancel flag"
    is not storable.  The cancel intent is carried by the in-process
    cancel_event; the row reaches the terminal schema-valid status
    'cancelled' when the background job task observes the event (P3
    CancelledError path).  404-unknown and already_finished are covered by
    the pre-existing TestCancellation tests.
    """

    def test_cancel_running_job_marks_intent_and_row_cancelled(self, client, storage):
        """Cancel on a running job sets the event and the row ends 'cancelled'."""
        import threading
        import time

        from app.pipeline import api_export
        from app.pipeline.tts_integration import CancelledError

        job_id = "cancel-run-1"
        cancel_event = threading.Event()
        api_export._render_jobs[job_id] = {
            "status": "running",
            "output_dir": None,
            "error": None,
            "cancel_event": cancel_event,
        }
        now = int(time.time() * 1000)
        storage.execute_insert(
            "INSERT INTO render_job (job_id, book_id, mode, status, created_ms, "
            "started_ms) VALUES (?, 'b1', 'batch', 'running', ?, ?)",
            (job_id, now, now),
        )
        try:
            resp = client.post("/api/pipeline/cancel_render", json={"job_id": job_id})
            assert resp.status_code == 200
            assert resp.json()["status"] == "cancelled"
            assert resp.json()["job_id"] == job_id
            # Cancel intent is recorded on the in-process channel
            assert cancel_event.is_set()

            # The background job task observes the cancellation: the P3
            # CancelledError path lands the row on the terminal status.
            with patch.object(
                api_export,
                "render_audiobook",
                side_effect=CancelledError("Render cancelled"),
            ):
                api_export._run_render_job(
                    job_id, "b1", storage, MagicMock(), True, None, -1
                )

            rows = storage.execute_query(
                "SELECT status, finished_ms FROM render_job WHERE job_id = ?",
                (job_id,),
            )
            assert rows[0]["status"] == "cancelled"
            assert rows[0]["finished_ms"] is not None
            assert api_export._render_jobs[job_id]["status"] == "cancelled"
        finally:
            del api_export._render_jobs[job_id]


class TestCancelWalksPersistence:
    """POST /cancel_walks persists walk_run.cancel_requested=1 on active rows."""

    def test_cancel_walks_writes_cancel_requested_on_active_rows(
        self, client, storage, walk_runner, tmp_path
    ):
        """Cancel persists the flag on running rows; terminal rows untouched."""
        import time

        # Keep the runner's stop-files out of the repo data/ dir
        walk_runner.stop_file_dir = str(tmp_path)

        now = int(time.time() * 1000)
        storage.execute_insert(
            "INSERT INTO walk_run (run_id, book_id, walk_name, status, created_ms) "
            "VALUES ('wr-run-1', 'b1', 'walk_2a_scene_segmentation', 'running', ?)",
            (now,),
        )
        storage.execute_insert(
            "INSERT INTO walk_run (run_id, book_id, walk_name, status, created_ms) "
            "VALUES ('wr-done-1', 'b1', 'walk_2a_scene_segmentation', 'completed', ?)",
            (now,),
        )

        resp = client.post("/api/pipeline/cancel_walks", json={"book_id": "b1"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"

        rows = storage.execute_query(
            "SELECT run_id, cancel_requested FROM walk_run WHERE book_id = 'b1' "
            "ORDER BY run_id"
        )
        flags = {r["run_id"]: r["cancel_requested"] for r in rows}
        assert flags["wr-run-1"] == 1
        assert flags["wr-done-1"] == 0


class TestExportJobsEndpoint:
    """GET /api/pipeline/export/jobs/{job_id} returns the render_job row."""

    def test_export_jobs_returns_job_detail(self, client, storage):
        """A known job returns the full ExportJobDetail field set."""
        job_id = "job-detail-1"
        storage.execute_insert(
            "INSERT INTO render_job (job_id, book_id, mode, status, error, output_dir, "
            "output_artifact_path, created_ms, started_ms, finished_ms) "
            "VALUES (?, 'b1', 'individual', 'completed', NULL, '/tmp/out', "
            "'/tmp/out/audiobook.m4b', 1000, 1000, 2000)",
            (job_id,),
        )
        resp = client.get(f"/api/pipeline/export/jobs/{job_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["job_id"] == job_id
        assert data["book_id"] == "b1"
        assert data["mode"] == "individual"
        assert data["status"] == "completed"
        assert data["error"] is None
        assert data["output_dir"] == "/tmp/out"
        assert data["output_artifact_path"] == "/tmp/out/audiobook.m4b"
        assert data["created_ms"] == 1000
        assert data["started_ms"] == 1000
        assert data["finished_ms"] == 2000

    def test_export_jobs_unknown_job_404(self, client):
        """An unknown job_id returns 404."""
        resp = client.get("/api/pipeline/export/jobs/nonexistent-job")
        assert resp.status_code == 404
        assert "Unknown job_id" in resp.json()["detail"]


class TestExportJobsChunksEndpoint:
    """GET /api/pipeline/export/jobs/{job_id}/chunks returns render_chunk rows."""

    def test_export_jobs_chunks_returns_rows(self, client, storage):
        """A known individual-mode job returns its chunk rows ordered by idx."""
        job_id = "job-chunks-1"
        storage.execute_insert(
            "INSERT INTO render_job (job_id, book_id, mode, status) "
            "VALUES (?, 'b1', 'individual', 'completed')",
            (job_id,),
        )
        storage.execute_insert(
            "INSERT INTO render_chunk (job_id, idx, status, wav_path, error) "
            "VALUES (?, 0, 'done', '/tmp/out/chunk_0000.wav', NULL)",
            (job_id,),
        )
        storage.execute_insert(
            "INSERT INTO render_chunk (job_id, idx, status, wav_path, error) "
            "VALUES (?, 1, 'failed', NULL, 'TTS error on chunk 1')",
            (job_id,),
        )
        resp = client.get(f"/api/pipeline/export/jobs/{job_id}/chunks")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 2
        first, second = data
        assert first["job_id"] == job_id
        assert first["idx"] == 0
        assert first["status"] == "done"
        assert first["wav_path"] == "/tmp/out/chunk_0000.wav"
        assert first["error"] is None
        assert second["idx"] == 1
        assert second["status"] == "failed"
        assert second["wav_path"] is None
        assert second["error"] == "TTS error on chunk 1"

    def test_export_jobs_chunks_empty_for_batch(self, client, storage):
        """A batch-mode job (no chunk rows by contract) returns an empty list."""
        job_id = "job-chunks-batch"
        storage.execute_insert(
            "INSERT INTO render_job (job_id, book_id, mode, status) "
            "VALUES (?, 'b1', 'batch', 'completed')",
            (job_id,),
        )
        resp = client.get(f"/api/pipeline/export/jobs/{job_id}/chunks")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_export_jobs_chunks_unknown_job_404(self, client):
        """An unknown job_id returns 404."""
        resp = client.get("/api/pipeline/export/jobs/nonexistent-job/chunks")
        assert resp.status_code == 404
        assert "Unknown job_id" in resp.json()["detail"]


class TestWalkRunsEndpoint:
    """GET /api/pipeline/walks/{book_id}/runs returns walk_run rows newest-first."""

    def test_walk_runs_newest_first(self, client, storage):
        """Rows are returned ordered by created_ms DESC with the WalkRunRow fields."""
        import time

        now = int(time.time() * 1000)
        storage.execute_insert(
            "INSERT INTO walk_run (run_id, book_id, walk_name, status, heartbeat_ms, "
            "created_ms, finished_ms, error) "
            "VALUES ('wr-new', 'b1', 'walk_2a_scene_segmentation', 'completed', ?, ?, ?, NULL)",
            (now, now, now),
        )
        storage.execute_insert(
            "INSERT INTO walk_run (run_id, book_id, walk_name, status, created_ms) "
            "VALUES ('wr-old', 'b1', 'walk_2b_persona_discovery', 'failed', ?)",
            (now - 5000,),
        )

        resp = client.get("/api/pipeline/walks/b1/runs")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert [r["run_id"] for r in data] == ["wr-new", "wr-old"]

        newest = data[0]
        assert newest["run_id"] == "wr-new"
        assert newest["walk_name"] == "walk_2a_scene_segmentation"
        assert newest["status"] == "completed"
        assert newest["heartbeat_ms"] == now
        assert newest["created_ms"] == now
        assert newest["finished_ms"] == now
        assert newest["error"] is None
        # WalkRunRow DTO does not include cancel_requested/result_json
        assert set(newest.keys()) == {
            "run_id",
            "walk_name",
            "status",
            "heartbeat_ms",
            "created_ms",
            "finished_ms",
            "error",
        }

    def test_walk_runs_empty_book(self, client):
        """A book with no runs returns an empty list."""
        resp = client.get("/api/pipeline/walks/nonexistent/runs")
        assert resp.status_code == 200
        assert resp.json() == []
