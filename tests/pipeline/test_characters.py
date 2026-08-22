"""Spec-first tests for character voice assignment API (app/pipeline/api_characters.py).

Covers:
- PUT /api/pipeline/characters/{id}/voice — set voice assignment
- PUT /api/pipeline/characters/{id}/voice — clear voice assignment (null)
- PUT /api/pipeline/characters/{id}/voice — invalid voice id returns 400
- PUT /api/pipeline/characters/{id}/voice — non-existent character returns 404
- PUT /api/pipeline/characters/{id}/voice — seed-script id ('ryan') accepted (id contract)
- PUT /api/pipeline/characters/{id}/voice — voice NAME ('Ryan') rejected with 400
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.pipeline.adapter import InMemorySQLiteAdapter
from app.pipeline.api_characters import router as characters_router
from app.pipeline.api_onboard import get_storage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _populate_voice(storage: InMemorySQLiteAdapter) -> None:
    """Insert a test voice config."""
    storage.execute_insert(
        "INSERT INTO voice_config (id, name, description, type) "
        "VALUES ('voice-1', 'Warm Voice', 'A warm voice', 'custom')"
    )


def _populate_seeded_voice(storage: InMemorySQLiteAdapter) -> None:
    """Insert a voice row mirroring scripts/seed_voice_catalog.py (id 'ryan', name 'Ryan')."""
    storage.execute_insert(
        "INSERT INTO voice_config (id, name, description, type, voice) "
        "VALUES ('ryan', 'Ryan', 'Default custom voice', 'custom', 'Ryan')"
    )


def _populate_character(storage: InMemorySQLiteAdapter) -> None:
    """Insert a test character with no voice assignment."""
    storage.execute_insert(
        "INSERT INTO character (id, name, aliases, voice_assignment_id, description) "
        "VALUES ('char-1', 'Alice', '[]', NULL, 'A brave protagonist')"
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def storage():
    """In-memory SQLite adapter with a voice and a character."""
    adapter = InMemorySQLiteAdapter()
    adapter.init_db()
    _populate_voice(adapter)
    _populate_seeded_voice(adapter)
    _populate_character(adapter)
    return adapter


@pytest.fixture()
def client(storage):
    """FastAPI TestClient with dependency overrides."""
    app = FastAPI()
    app.include_router(characters_router)
    app.dependency_overrides[get_storage] = lambda: storage
    return TestClient(app)


# ---------------------------------------------------------------------------
# P1-S2: PUT /api/pipeline/characters/{id}/voice — verify DB updated
# ---------------------------------------------------------------------------


class TestUpdateCharacterVoice:
    def test_set_voice_assignment(self, client, storage):
        """PUT with a valid voice_assignment_id updates the character in DB."""
        response = client.put(
            "/api/pipeline/characters/char-1/voice",
            json={"voice_assignment_id": "voice-1"},
        )
        assert response.status_code == 200
        result = response.json()
        assert result["id"] == "char-1"
        assert result["name"] == "Alice"
        assert result["voice_assignment_id"] == "voice-1"
        assert result["description"] == "A brave protagonist"

        # Verify DB state
        rows = storage.execute_query(
            "SELECT voice_assignment_id FROM character WHERE id = ?",
            ("char-1",),
        )
        assert len(rows) == 1
        assert rows[0]["voice_assignment_id"] == "voice-1"

    def test_clear_voice_assignment(self, client, storage):
        """PUT with null voice_assignment_id clears the assignment."""
        # First, set a voice
        client.put(
            "/api/pipeline/characters/char-1/voice",
            json={"voice_assignment_id": "voice-1"},
        )

        # Now clear it
        response = client.put(
            "/api/pipeline/characters/char-1/voice",
            json={"voice_assignment_id": None},
        )
        assert response.status_code == 200
        result = response.json()
        assert result["voice_assignment_id"] is None

        # Verify DB state
        rows = storage.execute_query(
            "SELECT voice_assignment_id FROM character WHERE id = ?",
            ("char-1",),
        )
        assert rows[0]["voice_assignment_id"] is None

    def test_invalid_voice_id_returns_400(self, client, storage):
        """PUT with a non-existent voice_assignment_id returns 400."""
        response = client.put(
            "/api/pipeline/characters/char-1/voice",
            json={"voice_assignment_id": "nonexistent-voice"},
        )
        assert response.status_code == 400
        assert "not found" in response.json()["detail"]

        # Verify character unchanged
        rows = storage.execute_query(
            "SELECT voice_assignment_id FROM character WHERE id = ?",
            ("char-1",),
        )
        assert rows[0]["voice_assignment_id"] is None

    def test_seeded_voice_id_is_accepted(self, client, storage):
        """PUT with a seed-script voice_config id ('ryan') succeeds — the id contract.

        The frontend resolves dropdown voice NAMES to voice_config ids before
        PUT (CONTRACTS.md "voice-id"), so the seeded id ('ryan') must be
        accepted and stored.
        """
        response = client.put(
            "/api/pipeline/characters/char-1/voice",
            json={"voice_assignment_id": "ryan"},
        )
        assert response.status_code == 200
        result = response.json()
        assert result["voice_assignment_id"] == "ryan"

        # Verify DB state
        rows = storage.execute_query(
            "SELECT voice_assignment_id FROM character WHERE id = ?",
            ("char-1",),
        )
        assert rows[0]["voice_assignment_id"] == "ryan"

    def test_voice_name_is_rejected(self, client, storage):
        """PUT with a voice NAME ('Ryan') is rejected with 400 — names are not ids.

        Documents the contract that the backend validates voice_assignment_id
        against voice_config.id only; the seed-script NAME of the 'ryan' row
        must not be sent as an id (the pre-fix frontend bug).
        """
        response = client.put(
            "/api/pipeline/characters/char-1/voice",
            json={"voice_assignment_id": "Ryan"},
        )
        assert response.status_code == 400
        assert "not found" in response.json()["detail"]

        # Verify character unchanged
        rows = storage.execute_query(
            "SELECT voice_assignment_id FROM character WHERE id = ?",
            ("char-1",),
        )
        assert rows[0]["voice_assignment_id"] is None

    def test_returns_all_character_fields(self, client):
        """Response includes all character columns."""
        response = client.put(
            "/api/pipeline/characters/char-1/voice",
            json={"voice_assignment_id": "voice-1"},
        )
        assert response.status_code == 200
        result = response.json()
        expected_keys = {"id", "name", "aliases", "voice_assignment_id", "description"}
        assert set(result.keys()) == expected_keys


# ---------------------------------------------------------------------------
# P1-S3: PUT /api/pipeline/characters/{id}/voice — non-existent character
# ---------------------------------------------------------------------------


class TestUpdateCharacterVoiceNotFound:
    def test_nonexistent_character_returns_404(self, client):
        """PUT for a character that doesn't exist returns 404."""
        response = client.put(
            "/api/pipeline/characters/nonexistent-char/voice",
            json={"voice_assignment_id": "voice-1"},
        )
        assert response.status_code == 404
        assert "not found" in response.json()["detail"]

    def test_404_before_voice_validation(self, client):
        """404 is returned even if voice_assignment_id is also invalid."""
        response = client.put(
            "/api/pipeline/characters/nonexistent-char/voice",
            json={"voice_assignment_id": "also-nonexistent"},
        )
        assert response.status_code == 404
