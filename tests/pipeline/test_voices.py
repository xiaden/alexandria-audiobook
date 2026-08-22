"""Spec-first tests for pipeline voice catalog API (app/pipeline/api_voices.py).

Covers:
- GET /api/pipeline/voices — returns all voice configs
- GET /api/pipeline/voices?type=clone — filters by voice type
- POST /api/pipeline/voices — creates a new voice config
- POST /api/pipeline/voices — rejects invalid voice type
- PUT /api/pipeline/voices/{id} — partial update of an existing voice config
- PUT /api/pipeline/voices/{id} — returns 404 for non-existent id
- DELETE /api/pipeline/voices/{id} — deletes a voice config
- DELETE /api/pipeline/voices/{id} — returns 404 for non-existent id
- POST /api/pipeline/voices/{id}/preview — generates a TTS audio preview
- POST /api/pipeline/voices/{id}/preview — returns 404 for non-existent id
- CRUD integration — full POST → GET → PUT → DELETE lifecycle
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.pipeline.adapter import InMemorySQLiteAdapter
from app.pipeline.api_onboard import get_storage
from app.pipeline.api_voices import router as voices_router

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _populate_voices(storage: InMemorySQLiteAdapter) -> None:
    """Insert test voice configs with different types."""
    storage.execute_insert(
        "INSERT INTO voice_config (id, name, description, type, voice, character_style, seed, ref_audio, ref_text, adapter_id, adapter_path, alias_of) "
        "VALUES ('v1', 'Warm Female', 'A warm female voice', 'custom', 'Ryan', 'cheerful', '-1', NULL, NULL, NULL, NULL, NULL)"
    )
    storage.execute_insert(
        "INSERT INTO voice_config (id, name, description, type, voice, character_style, seed, ref_audio, ref_text, adapter_id, adapter_path, alias_of) "
        "VALUES ('v2', 'Clone Voice', 'A cloned voice', 'clone', NULL, NULL, '42', '/path/to/ref.wav', 'Hello world', NULL, NULL, NULL)"
    )
    storage.execute_insert(
        "INSERT INTO voice_config (id, name, description, type, voice, character_style, seed, ref_audio, ref_text, adapter_id, adapter_path, alias_of) "
        "VALUES ('v3', 'Design Voice', 'A designed voice', 'design', NULL, 'dramatic', '-1', NULL, NULL, NULL, NULL, NULL)"
    )
    storage.execute_insert(
        "INSERT INTO voice_config (id, name, description, type, voice, character_style, seed, ref_audio, ref_text, adapter_id, adapter_path, alias_of) "
        "VALUES ('v4', 'LoRA Voice', 'A LoRA voice', 'lora', NULL, NULL, '-1', NULL, NULL, 'adapter-1', '/path/to/adapter.safetensors', NULL)"
    )
    storage.execute_insert(
        "INSERT INTO voice_config (id, name, description, type, voice, character_style, seed, ref_audio, ref_text, adapter_id, adapter_path, alias_of) "
        "VALUES ('v5', 'Builtin LoRA', 'A builtin LoRA voice', 'builtin_lora', NULL, NULL, '-1', NULL, NULL, 'builtin-1', NULL, NULL)"
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def storage():
    """In-memory SQLite adapter for testing."""
    adapter = InMemorySQLiteAdapter()
    adapter.init_db()
    _populate_voices(adapter)
    return adapter


@pytest.fixture()
def client(storage):
    """FastAPI TestClient with dependency overrides."""
    app = FastAPI()
    app.include_router(voices_router)
    app.dependency_overrides[get_storage] = lambda: storage
    return TestClient(app)


# ---------------------------------------------------------------------------
# P1-S3: GET /api/pipeline/voices — returns all voices
# ---------------------------------------------------------------------------


class TestListVoicesEndpoint:
    def test_returns_all_voices(self, client):
        """GET /api/pipeline/voices returns all voice configs."""
        response = client.get("/api/pipeline/voices")
        assert response.status_code == 200
        result = response.json()
        assert isinstance(result, list)
        assert len(result) == 5

    def test_returns_all_columns(self, client):
        """Each voice dict contains all 12 columns from voice_config."""
        response = client.get("/api/pipeline/voices")
        result = response.json()
        expected_keys = {
            "id", "name", "description", "type", "voice", "character_style",
            "seed", "ref_audio", "ref_text", "adapter_id", "adapter_path", "alias_of",
        }
        for voice in result:
            assert set(voice.keys()) == expected_keys

    def test_voice_data_integrity(self, client):
        """Voice data matches what was inserted."""
        response = client.get("/api/pipeline/voices")
        result = response.json()
        # Find the custom voice
        custom = next(v for v in result if v["id"] == "v1")
        assert custom["name"] == "Warm Female"
        assert custom["type"] == "custom"
        assert custom["voice"] == "Ryan"
        assert custom["character_style"] == "cheerful"
        assert custom["seed"] == "-1"

    def test_empty_table_returns_empty_list(self):
        """Empty voice_config table returns empty list."""
        adapter = InMemorySQLiteAdapter()
        adapter.init_db()

        app = FastAPI()
        app.include_router(voices_router)
        app.dependency_overrides[get_storage] = lambda: adapter
        test_client = TestClient(app)

        response = test_client.get("/api/pipeline/voices")
        assert response.status_code == 200
        assert response.json() == []


# ---------------------------------------------------------------------------
# P1-S4: GET /api/pipeline/voices?type=clone — filters by type
# ---------------------------------------------------------------------------


class TestListVoicesFilterByType:
    def test_filter_by_clone(self, client):
        """GET /api/pipeline/voices?type=clone returns only clone voices."""
        response = client.get("/api/pipeline/voices?type=clone")
        assert response.status_code == 200
        result = response.json()
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["id"] == "v2"
        assert result[0]["type"] == "clone"

    def test_filter_by_custom(self, client):
        """GET /api/pipeline/voices?type=custom returns only custom voices."""
        response = client.get("/api/pipeline/voices?type=custom")
        assert response.status_code == 200
        result = response.json()
        assert len(result) == 1
        assert result[0]["id"] == "v1"
        assert result[0]["type"] == "custom"

    def test_filter_by_design(self, client):
        """GET /api/pipeline/voices?type=design returns only design voices."""
        response = client.get("/api/pipeline/voices?type=design")
        assert response.status_code == 200
        result = response.json()
        assert len(result) == 1
        assert result[0]["id"] == "v3"
        assert result[0]["type"] == "design"

    def test_filter_by_lora(self, client):
        """GET /api/pipeline/voices?type=lora returns only lora voices."""
        response = client.get("/api/pipeline/voices?type=lora")
        assert response.status_code == 200
        result = response.json()
        assert len(result) == 1
        assert result[0]["id"] == "v4"
        assert result[0]["type"] == "lora"

    def test_filter_by_builtin_lora(self, client):
        """GET /api/pipeline/voices?type=builtin_lora returns only builtin_lora voices."""
        response = client.get("/api/pipeline/voices?type=builtin_lora")
        assert response.status_code == 200
        result = response.json()
        assert len(result) == 1
        assert result[0]["id"] == "v5"
        assert result[0]["type"] == "builtin_lora"

    def test_filter_nonexistent_type_returns_empty(self, client):
        """Filtering by a type that doesn't exist returns empty list."""
        response = client.get("/api/pipeline/voices?type=nonexistent")
        assert response.status_code == 200
        assert response.json() == []

    def test_no_filter_returns_all(self, client):
        """Without type param, all voices are returned."""
        response = client.get("/api/pipeline/voices")
        assert response.status_code == 200
        assert len(response.json()) == 5


# ---------------------------------------------------------------------------
# P2-S2: POST /api/pipeline/voices — creates a new voice config
# ---------------------------------------------------------------------------


class TestCreateVoiceEndpoint:
    @pytest.fixture()
    def empty_storage(self):
        """In-memory SQLite adapter with no pre-existing voices."""
        adapter = InMemorySQLiteAdapter()
        adapter.init_db()
        return adapter

    @pytest.fixture()
    def empty_client(self, empty_storage):
        """FastAPI TestClient with empty voice_config table."""
        app = FastAPI()
        app.include_router(voices_router)
        app.dependency_overrides[get_storage] = lambda: empty_storage
        return TestClient(app)

    def test_create_voice_all_fields(self, empty_client, empty_storage):
        """POST /api/pipeline/voices inserts row and returns all 12 columns."""
        payload = {
            "name": "Clone Alice",
            "description": "Alice's cloned voice",
            "type": "clone",
            "voice": "AliceBase",
            "character_style": "warm",
            "seed": "42",
            "ref_audio": "/refs/alice.wav",
            "ref_text": "Hello world",
            "adapter_id": "adapter-1",
            "adapter_path": "/adapters/alice.safetensors",
            "alias_of": "alice-canonical",
        }
        response = empty_client.post("/api/pipeline/voices", json=payload)
        assert response.status_code == 201
        result = response.json()

        # id derived from name since not provided
        assert result["id"] == "Clone Alice"
        assert result["name"] == "Clone Alice"
        assert result["description"] == "Alice's cloned voice"
        assert result["type"] == "clone"
        assert result["voice"] == "AliceBase"
        assert result["character_style"] == "warm"
        assert result["seed"] == "42"
        assert result["ref_audio"] == "/refs/alice.wav"
        assert result["ref_text"] == "Hello world"
        assert result["adapter_id"] == "adapter-1"
        assert result["adapter_path"] == "/adapters/alice.safetensors"
        assert result["alias_of"] == "alice-canonical"

        # Verify row exists in DB
        rows = empty_storage.execute_query(
            "SELECT * FROM voice_config WHERE id = ?", ("Clone Alice",)
        )
        assert len(rows) == 1
        assert rows[0]["type"] == "clone"

    def test_create_voice_with_explicit_id(self, empty_client):
        """POST with explicit id uses that id instead of deriving from name."""
        payload = {"id": "custom-id-123", "name": "My Voice", "type": "custom"}
        response = empty_client.post("/api/pipeline/voices", json=payload)
        assert response.status_code == 201
        result = response.json()
        assert result["id"] == "custom-id-123"
        assert result["name"] == "My Voice"

    def test_create_voice_minimal_fields(self, empty_client):
        """POST with only name uses defaults for all other fields."""
        payload = {"name": "Minimal Voice"}
        response = empty_client.post("/api/pipeline/voices", json=payload)
        assert response.status_code == 201
        result = response.json()
        assert result["id"] == "Minimal Voice"
        assert result["name"] == "Minimal Voice"
        assert result["type"] == "custom"
        assert result["seed"] == "-1"
        assert result["description"] is None
        assert result["voice"] is None
        assert result["ref_audio"] is None

    def test_create_voice_duplicate_returns_409(self, empty_client):
        """Creating a voice with a duplicate id returns 409."""
        payload = {"name": "Duplicate Voice", "type": "custom"}
        response = empty_client.post("/api/pipeline/voices", json=payload)
        assert response.status_code == 201

        # Try to create again with same name (→ same id)
        response = empty_client.post("/api/pipeline/voices", json=payload)
        assert response.status_code == 409
        assert "already exists" in response.json()["detail"]

    def test_create_voice_each_type(self, empty_client):
        """All 5 valid voice types are accepted."""
        for voice_type in ("custom", "clone", "design", "lora", "builtin_lora"):
            payload = {"name": f"Voice {voice_type}", "type": voice_type}
            response = empty_client.post("/api/pipeline/voices", json=payload)
            assert response.status_code == 201, f"Failed for type={voice_type}"
            assert response.json()["type"] == voice_type


# ---------------------------------------------------------------------------
# P2-S3: POST /api/pipeline/voices — rejects invalid voice type
# ---------------------------------------------------------------------------


class TestCreateVoiceInvalidType:
    @pytest.fixture()
    def empty_storage(self):
        """In-memory SQLite adapter with no pre-existing voices."""
        adapter = InMemorySQLiteAdapter()
        adapter.init_db()
        return adapter

    @pytest.fixture()
    def empty_client(self, empty_storage):
        """FastAPI TestClient with empty voice_config table."""
        app = FastAPI()
        app.include_router(voices_router)
        app.dependency_overrides[get_storage] = lambda: empty_storage
        return TestClient(app)

    def test_invalid_type_returns_422(self, empty_client, empty_storage):
        """POST with an invalid voice type returns 422 (Pydantic validation)."""
        payload = {"name": "Bad Voice", "type": "invalid_type"}
        response = empty_client.post("/api/pipeline/voices", json=payload)
        assert response.status_code == 422

        # Verify no row was inserted
        rows = empty_storage.execute_query("SELECT * FROM voice_config")
        assert len(rows) == 0

    def test_empty_type_string_returns_422(self, empty_client, empty_storage):
        """POST with empty string type returns 422."""
        payload = {"name": "Empty Type Voice", "type": ""}
        response = empty_client.post("/api/pipeline/voices", json=payload)
        assert response.status_code == 422

        # Verify no row was inserted
        rows = empty_storage.execute_query("SELECT * FROM voice_config")
        assert len(rows) == 0

    def test_missing_name_returns_422(self, empty_client):
        """POST without required name field returns 422."""
        payload = {"type": "custom"}
        response = empty_client.post("/api/pipeline/voices", json=payload)
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# P3-S2: PUT /api/pipeline/voices/{id} — verify row updated (partial update)
# ---------------------------------------------------------------------------


class TestUpdateVoiceEndpoint:
    def test_partial_update_changes_one_field_preserves_others(self, client, storage):
        """PUT with only `description` changes description, preserves all other fields."""
        # v1 starts as: name='Warm Female', type='custom', voice='Ryan',
        # character_style='cheerful', seed='-1', description='A warm female voice'
        response = client.put(
            "/api/pipeline/voices/v1",
            json={"description": "Updated description"},
        )
        assert response.status_code == 200
        result = response.json()

        # The updated field
        assert result["description"] == "Updated description"

        # All other fields preserved
        assert result["id"] == "v1"
        assert result["name"] == "Warm Female"
        assert result["type"] == "custom"
        assert result["voice"] == "Ryan"
        assert result["character_style"] == "cheerful"
        assert result["seed"] == "-1"
        assert result["ref_audio"] is None
        assert result["ref_text"] is None
        assert result["adapter_id"] is None
        assert result["adapter_path"] is None
        assert result["alias_of"] is None

        # Verify in DB directly
        rows = storage.execute_query(
            "SELECT * FROM voice_config WHERE id = ?", ("v1",)
        )
        assert len(rows) == 1
        assert rows[0]["description"] == "Updated description"
        assert rows[0]["name"] == "Warm Female"
        assert rows[0]["type"] == "custom"

    def test_update_multiple_fields(self, client, storage):
        """PUT with multiple fields updates all of them."""
        response = client.put(
            "/api/pipeline/voices/v2",
            json={"voice": "NewBase", "seed": "99", "ref_audio": "/new/ref.wav"},
        )
        assert response.status_code == 200
        result = response.json()
        assert result["voice"] == "NewBase"
        assert result["seed"] == "99"
        assert result["ref_audio"] == "/new/ref.wav"
        # Unchanged fields preserved
        assert result["name"] == "Clone Voice"
        assert result["type"] == "clone"
        assert result["ref_text"] == "Hello world"

    def test_update_with_null_clears_field(self, client, storage):
        """PUT with explicit null clears a field."""
        response = client.put(
            "/api/pipeline/voices/v1",
            json={"voice": None},
        )
        assert response.status_code == 200
        result = response.json()
        assert result["voice"] is None
        # Other fields preserved
        assert result["name"] == "Warm Female"

    def test_update_invalid_type_returns_422(self, client, storage):
        """PUT with invalid voice type returns 422 (Pydantic validation)."""
        response = client.put(
            "/api/pipeline/voices/v1",
            json={"type": "invalid_type"},
        )
        assert response.status_code == 422
        # Row unchanged
        rows = storage.execute_query(
            "SELECT * FROM voice_config WHERE id = ?", ("v1",)
        )
        assert rows[0]["type"] == "custom"

    def test_update_empty_body_returns_row_unchanged(self, client):
        """PUT with empty JSON body returns the row unchanged."""
        response = client.put("/api/pipeline/voices/v1", json={})
        assert response.status_code == 200
        result = response.json()
        assert result["id"] == "v1"
        assert result["name"] == "Warm Female"
        assert result["type"] == "custom"


# ---------------------------------------------------------------------------
# P3-S3: PUT /api/pipeline/voices/{id} — non-existent id returns 404
# ---------------------------------------------------------------------------


class TestUpdateVoiceNotFound:
    def test_nonexistent_id_returns_404(self, client):
        """PUT for a voice id that doesn't exist returns 404."""
        response = client.put(
            "/api/pipeline/voices/nonexistent-voice",
            json={"description": "should fail"},
        )
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_nonexistent_id_does_not_create_row(self, client, storage):
        """PUT for non-existent id does not create a new row."""
        initial_count = len(storage.execute_query("SELECT * FROM voice_config"))
        response = client.put(
            "/api/pipeline/voices/new-voice",
            json={"name": "New Voice"},
        )
        assert response.status_code == 404
        final_count = len(storage.execute_query("SELECT * FROM voice_config"))
        assert final_count == initial_count


# ---------------------------------------------------------------------------
# P4-S2: DELETE /api/pipeline/voices/{id} — verify row deleted
# ---------------------------------------------------------------------------


class TestDeleteVoiceEndpoint:
    def test_delete_voice_removes_row(self, client, storage):
        """DELETE /api/pipeline/voices/{id} removes the row and returns 204."""
        # Verify the voice exists before deletion
        rows_before = storage.execute_query(
            "SELECT * FROM voice_config WHERE id = ?", ("v1",)
        )
        assert len(rows_before) == 1

        response = client.delete("/api/pipeline/voices/v1")
        assert response.status_code == 204

        # Verify the row is gone from DB
        rows_after = storage.execute_query(
            "SELECT * FROM voice_config WHERE id = ?", ("v1",)
        )
        assert len(rows_after) == 0

    def test_delete_voice_reduces_count(self, client):
        """After deletion, GET returns one fewer voice."""
        response_before = client.get("/api/pipeline/voices")
        count_before = len(response_before.json())

        response = client.delete("/api/pipeline/voices/v2")
        assert response.status_code == 204

        response_after = client.get("/api/pipeline/voices")
        count_after = len(response_after.json())
        assert count_after == count_before - 1

    def test_delete_voice_no_response_body(self, client):
        """DELETE returns 204 with no response body."""
        response = client.delete("/api/pipeline/voices/v3")
        assert response.status_code == 204
        assert response.text == ""


# ---------------------------------------------------------------------------
# P4-S3: DELETE /api/pipeline/voices/{id} — non-existent id returns 404
# ---------------------------------------------------------------------------


class TestDeleteVoiceNotFound:
    def test_delete_nonexistent_returns_404(self, client):
        """DELETE on a non-existent voice id returns 404."""
        response = client.delete("/api/pipeline/voices/nonexistent-voice")
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_delete_nonexistent_does_not_affect_existing(self, client, storage):
        """DELETE on non-existent id does not affect existing rows."""
        rows_before = storage.execute_query("SELECT * FROM voice_config")
        count_before = len(rows_before)

        response = client.delete("/api/pipeline/voices/does-not-exist")
        assert response.status_code == 404

        rows_after = storage.execute_query("SELECT * FROM voice_config")
        assert len(rows_after) == count_before

    def test_delete_already_deleted_returns_404(self, client):
        """Deleting the same voice twice returns 404 on the second attempt."""
        response1 = client.delete("/api/pipeline/voices/v4")
        assert response1.status_code == 204

        response2 = client.delete("/api/pipeline/voices/v4")
        assert response2.status_code == 404


# ---------------------------------------------------------------------------
# P5-S3: Integration test — full CRUD flow end-to-end
# ---------------------------------------------------------------------------


class TestVoiceCRUDIntegration:
    """Integration test that exercises all 4 CRUD endpoints in sequence.

    Uses the combined router from app.pipeline.api (not voices_router directly)
    to verify the full integration path.
    """

    @pytest.fixture()
    def integration_storage(self):
        """In-memory SQLite adapter for integration test."""
        adapter = InMemorySQLiteAdapter()
        adapter.init_db()
        return adapter

    @pytest.fixture()
    def integration_client(self, integration_storage):
        """FastAPI TestClient using the combined pipeline router."""
        from app.pipeline.api import router as pipeline_router

        app = FastAPI()
        app.include_router(pipeline_router)
        app.dependency_overrides[get_storage] = lambda: integration_storage
        return TestClient(app)

    def test_full_crud_flow(self, integration_client, integration_storage):
        """Test complete CRUD lifecycle: POST → GET → PUT → DELETE → GET."""
        # 1. POST: Create a new voice
        create_payload = {
            "name": "Integration Test Voice",
            "description": "Voice for integration testing",
            "type": "clone",
            "voice": "TestBase",
            "character_style": "neutral",
            "seed": "123",
            "ref_audio": "/test/ref.wav",
            "ref_text": "Test reference text",
        }
        post_response = integration_client.post(
            "/api/pipeline/voices", json=create_payload
        )
        assert post_response.status_code == 201
        created = post_response.json()
        assert created["id"] == "Integration Test Voice"
        assert created["name"] == "Integration Test Voice"
        assert created["type"] == "clone"
        assert created["voice"] == "TestBase"
        assert created["description"] == "Voice for integration testing"

        # 2. GET: Verify the voice appears in the list
        get_response = integration_client.get("/api/pipeline/voices")
        assert get_response.status_code == 200
        voices_list = get_response.json()
        assert len(voices_list) == 1
        assert voices_list[0]["id"] == "Integration Test Voice"
        assert voices_list[0]["type"] == "clone"

        # 3. PUT: Update the voice
        update_payload = {
            "description": "Updated description",
            "seed": "456",
            "ref_audio": "/test/updated_ref.wav",
        }
        put_response = integration_client.put(
            "/api/pipeline/voices/Integration Test Voice",
            json=update_payload,
        )
        assert put_response.status_code == 200
        updated = put_response.json()
        assert updated["id"] == "Integration Test Voice"
        assert updated["description"] == "Updated description"
        assert updated["seed"] == "456"
        assert updated["ref_audio"] == "/test/updated_ref.wav"
        # Unchanged fields preserved
        assert updated["name"] == "Integration Test Voice"
        assert updated["type"] == "clone"
        assert updated["voice"] == "TestBase"
        assert updated["character_style"] == "neutral"
        assert updated["ref_text"] == "Test reference text"

        # 4. DELETE: Remove the voice
        delete_response = integration_client.delete(
            "/api/pipeline/voices/Integration Test Voice"
        )
        assert delete_response.status_code == 204
        assert delete_response.text == ""

        # 5. GET: Verify the voice is gone
        get_response_after = integration_client.get("/api/pipeline/voices")
        assert get_response_after.status_code == 200
        voices_list_after = get_response_after.json()
        assert len(voices_list_after) == 0

        # Verify in DB directly
        rows = integration_storage.execute_query("SELECT * FROM voice_config")
        assert len(rows) == 0

    def test_crud_with_filter(self, integration_client):
        """Test CRUD with type filter to verify filtering works end-to-end."""
        # Create two voices with different types
        integration_client.post(
            "/api/pipeline/voices",
            json={"name": "Custom Voice", "type": "custom"},
        )
        integration_client.post(
            "/api/pipeline/voices",
            json={"name": "Clone Voice", "type": "clone"},
        )

        # Verify filter works
        custom_response = integration_client.get(
            "/api/pipeline/voices?type=custom"
        )
        assert custom_response.status_code == 200
        custom_voices = custom_response.json()
        assert len(custom_voices) == 1
        assert custom_voices[0]["name"] == "Custom Voice"

        clone_response = integration_client.get(
            "/api/pipeline/voices?type=clone"
        )
        assert clone_response.status_code == 200
        clone_voices = clone_response.json()
        assert len(clone_voices) == 1
        assert clone_voices[0]["name"] == "Clone Voice"

        # Delete one and verify filter still works
        integration_client.delete("/api/pipeline/voices/Custom Voice")

        custom_after = integration_client.get(
            "/api/pipeline/voices?type=custom"
        )
        assert len(custom_after.json()) == 0

        clone_after = integration_client.get(
            "/api/pipeline/voices?type=clone"
        )
        assert len(clone_after.json()) == 1


# ---------------------------------------------------------------------------
# P1-S2: POST /api/pipeline/voices/{id}/preview — verify audio generated
# ---------------------------------------------------------------------------


class FakeTTSEngine:
    """Fake TTS engine that records calls and creates a dummy output file."""

    def __init__(self):
        self.voice_calls: list[dict] = []

    def generate_voice(self, text, instruct_text, speaker, voice_config, output_path):
        """Record the call and create a dummy file at output_path."""
        self.voice_calls.append({
            "text": text,
            "instruct_text": instruct_text,
            "speaker": speaker,
            "voice_config": voice_config,
            "output_path": output_path,
        })
        # Create the output file so the endpoint can reference it
        import os
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "wb") as f:
            f.write(b"RIFF")  # minimal WAV header stub


class TestPreviewVoiceEndpoint:
    @pytest.fixture()
    def preview_storage(self):
        """In-memory SQLite adapter with a test voice for preview."""
        adapter = InMemorySQLiteAdapter()
        adapter.init_db()
        adapter.execute_insert(
            "INSERT INTO voice_config (id, name, description, type, voice, character_style, seed, ref_audio, ref_text, adapter_id, adapter_path, alias_of) "
            "VALUES ('test-voice', 'TestVoice', 'A test voice', 'custom', 'TestVoice', 'cheerful', '-1', NULL, NULL, NULL, NULL, NULL)"
        )
        return adapter

    @pytest.fixture()
    def fake_engine(self):
        """FakeTTSEngine instance for recording calls."""
        return FakeTTSEngine()

    @pytest.fixture()
    def preview_client(self, preview_storage, fake_engine):
        """FastAPI TestClient with storage and TTS engine overrides."""
        from app.pipeline.api_export import get_tts_engine

        app = FastAPI()
        app.include_router(voices_router)
        app.dependency_overrides[get_storage] = lambda: preview_storage
        app.dependency_overrides[get_tts_engine] = lambda: fake_engine
        return TestClient(app)

    def test_preview_returns_audio_url(self, preview_client, fake_engine, tmp_path, monkeypatch):
        """POST /api/pipeline/voices/{id}/preview returns audio_url and calls TTS."""
        # Override the previews directory to use tmp_path for isolation
        import app.pipeline.api_voices as mod
        monkeypatch.setattr(mod, "_PREVIEWS_DIR", str(tmp_path / "previews"))

        response = preview_client.post(
            "/api/pipeline/voices/test-voice/preview",
            json={"sample_text": "Hello, this is a test."},
        )
        assert response.status_code == 200
        result = response.json()
        assert result["audio_url"] == "/designed_voices/previews/test-voice.wav"
        assert result["voice_id"] == "test-voice"

        # Verify FakeTTSEngine received the call
        assert len(fake_engine.voice_calls) == 1
        call = fake_engine.voice_calls[0]
        assert call["text"] == "Hello, this is a test."
        assert call["speaker"] == "test-voice"
        # The full voice_config dict (all 10 generated fields) matches the
        # seeded row: id='test-voice', name='TestVoice', description='A test
        # voice', type='custom', voice='TestVoice', character_style='cheerful',
        # seed='-1', and NULL ref_audio/ref_text/adapter_id/adapter_path/alias_of.
        assert call["voice_config"] == {
            "test-voice": {
                "type": "custom",
                "voice": "TestVoice",
                "description": "A test voice",
                "ref_audio": None,
                "ref_text": None,
                "adapter_id": None,
                "adapter_path": None,
                "character_style": "cheerful",
                "seed": "-1",
                "alias_of": None,
            }
        }
        # The endpoint always passes an empty instruct_text to generate_voice
        assert call["instruct_text"] == ""

    def test_preview_tts_engine_none_returns_503(self, preview_storage):
        """When TTS engine is None, preview returns 503."""
        from app.pipeline.api_export import get_tts_engine

        app = FastAPI()
        app.include_router(voices_router)
        app.dependency_overrides[get_storage] = lambda: preview_storage
        app.dependency_overrides[get_tts_engine] = lambda: None
        client = TestClient(app)

        response = client.post(
            "/api/pipeline/voices/test-voice/preview",
            json={"sample_text": "Hello"},
        )
        assert response.status_code == 503
        assert "not available" in response.json()["detail"].lower()

    def test_preview_tts_failure_returns_500(
        self, preview_storage, tmp_path, monkeypatch
    ):
        """When TTS generation raises, preview returns 500 and leaves no
        partial audio file behind."""
        import app.pipeline.api_voices as mod
        from app.pipeline.api_export import get_tts_engine

        class RaisingTTSEngine:
            """Fake TTS engine whose generate_voice always raises."""

            def generate_voice(
                self, text, instruct_text, speaker, voice_config, output_path
            ):
                raise RuntimeError("synthesis exploded")

        previews_dir = tmp_path / "previews"
        monkeypatch.setattr(mod, "_PREVIEWS_DIR", str(previews_dir))

        app = FastAPI()
        app.include_router(voices_router)
        app.dependency_overrides[get_storage] = lambda: preview_storage
        app.dependency_overrides[get_tts_engine] = lambda: RaisingTTSEngine()
        client = TestClient(app)

        response = client.post(
            "/api/pipeline/voices/test-voice/preview",
            json={"sample_text": "Hello"},
        )
        assert response.status_code == 500
        assert "TTS generation failed" in response.json()["detail"]

        # The preview directory is left consistent — no partial file is written
        assert not (previews_dir / "test-voice.wav").exists()

    def test_preview_audio_file_is_accessible(self, preview_client, fake_engine, tmp_path, monkeypatch):
        """POST preview creates audio file that can be accessed via static mount."""
        # Override the previews directory to use tmp_path for isolation
        import app.pipeline.api_voices as mod
        monkeypatch.setattr(mod, "_PREVIEWS_DIR", str(tmp_path / "previews"))

        # Generate preview
        response = preview_client.post(
            "/api/pipeline/voices/test-voice/preview",
            json={"sample_text": "Hello, this is a test."},
        )
        assert response.status_code == 200
        result = response.json()
        audio_url = result["audio_url"]

        # Verify the audio file was created
        expected_path = tmp_path / "previews" / "test-voice.wav"
        assert expected_path.exists(), f"Audio file not created at {expected_path}"

        # Verify the file has content (FakeTTSEngine writes "fake audio data")
        assert expected_path.stat().st_size > 0, "Audio file is empty"

        # Verify the audio_url format is correct for static serving
        assert audio_url == "/designed_voices/previews/test-voice.wav"
        assert audio_url.startswith("/designed_voices/previews/")

    def test_preview_sanitizes_path_traversal_voice_id(
        self, preview_client, fake_engine, preview_storage, tmp_path, monkeypatch
    ):
        """A hostile voice_id with parent-dir + backslash is sanitized so the
        output stays under _PREVIEWS_DIR (no traversal).

        The backslash separator (URL-encoded %5C) is used because a literal
        forward slash cannot reach the {voice_id} path param through the
        ASGI/httpx stack: scope["path"] is percent-decoded before route
        matching, so a real '/' in the id breaks the route (404). The
        forward-slash form is covered directly in
        test_preview_sanitizes_forward_slash_traversal below.
        """
        import os

        import app.pipeline.api_voices as mod

        previews_dir = tmp_path / "previews"
        monkeypatch.setattr(mod, "_PREVIEWS_DIR", str(previews_dir))

        # Insert the hostile-id row directly so the 404 check passes
        preview_storage.execute_insert(
            "INSERT INTO voice_config (id, name, description, type, voice, character_style, seed, ref_audio, ref_text, adapter_id, adapter_path, alias_of) "
            "VALUES ('..\\evil', 'Evil Voice', 'hostile id', 'custom', 'EvilBase', 'cheerful', '-1', NULL, NULL, NULL, NULL, NULL)"
        )

        response = preview_client.post(
            "/api/pipeline/voices/%2E%2E%5Cevil/preview",
            json={"sample_text": "Hello"},
        )
        assert response.status_code == 200
        result = response.json()

        # '\\' -> '_' then '..' -> '_': '..\\evil' becomes '__evil'
        assert result["audio_url"] == "/designed_voices/previews/__evil.wav"

        assert len(fake_engine.voice_calls) == 1
        output_path = fake_engine.voice_calls[0]["output_path"]
        # The output file stays inside the previews directory — no traversal
        assert os.path.normpath(output_path).startswith(
            os.path.normpath(str(previews_dir))
        )

    def test_preview_sanitizes_forward_slash_traversal(
        self, fake_engine, tmp_path, monkeypatch
    ):
        """The literal '../' form is sanitized the same way (direct call).

        This cannot be POSTed through the HTTP stack — the ASGI scope path is
        percent-decoded before routing, so a literal '/' in the {voice_id}
        segment breaks route matching before the handler runs. This calls the
        endpoint function directly with voice_id='../evil' to pin the exact
        sanitization QA reported.
        """
        import asyncio
        import os

        import app.pipeline.api_voices as mod
        from app.pipeline.api_voices import VoicePreviewRequest

        previews_dir = tmp_path / "previews"
        monkeypatch.setattr(mod, "_PREVIEWS_DIR", str(previews_dir))

        storage = InMemorySQLiteAdapter()
        storage.init_db()
        storage.execute_insert(
            "INSERT INTO voice_config (id, name, description, type, voice, character_style, seed, ref_audio, ref_text, adapter_id, adapter_path, alias_of) "
            "VALUES ('../evil', 'Evil Voice', 'hostile id', 'custom', 'EvilBase', 'cheerful', '-1', NULL, NULL, NULL, NULL, NULL)"
        )

        result = asyncio.run(
            mod.preview_voice(
                "../evil",
                VoicePreviewRequest(sample_text="Hello"),
                storage,
                fake_engine,
            )
        )

        # '/' -> '_' then '..' -> '_': '../evil' becomes '__evil'
        assert result["audio_url"] == "/designed_voices/previews/__evil.wav"

        assert len(fake_engine.voice_calls) == 1
        output_path = fake_engine.voice_calls[0]["output_path"]
        # The output file stays inside the previews directory — no traversal
        assert os.path.normpath(output_path).startswith(
            os.path.normpath(str(previews_dir))
        )


# ---------------------------------------------------------------------------
# P1-S3: POST /api/pipeline/voices/{id}/preview — non-existent voice returns 404
# ---------------------------------------------------------------------------


class TestPreviewVoiceNotFound:
    def test_nonexistent_voice_returns_404(self):
        """POST preview for a non-existent voice id returns 404."""
        from app.pipeline.api_export import get_tts_engine

        adapter = InMemorySQLiteAdapter()
        adapter.init_db()

        app = FastAPI()
        app.include_router(voices_router)
        app.dependency_overrides[get_storage] = lambda: adapter
        app.dependency_overrides[get_tts_engine] = lambda: FakeTTSEngine()
        client = TestClient(app)

        response = client.post(
            "/api/pipeline/voices/nonexistent-voice/preview",
            json={"sample_text": "Hello"},
        )
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_404_before_tts_check(self):
        """404 is returned even if TTS engine is None (voice check happens first)."""
        from app.pipeline.api_export import get_tts_engine

        adapter = InMemorySQLiteAdapter()
        adapter.init_db()

        app = FastAPI()
        app.include_router(voices_router)
        app.dependency_overrides[get_storage] = lambda: adapter
        app.dependency_overrides[get_tts_engine] = lambda: None
        client = TestClient(app)

        response = client.post(
            "/api/pipeline/voices/does-not-exist/preview",
            json={"sample_text": "Hello"},
        )
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()