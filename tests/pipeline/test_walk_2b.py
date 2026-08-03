"""Tests for Walk 2b character discovery."""

import json
import pytest
from unittest.mock import Mock

from app.pipeline.adapter import InMemorySQLiteAdapter
from app.pipeline.populate import populate_initial_spine
from app.pipeline.walks.walk_2b_character_discovery import (
    execute,
    _build_character_discovery_prompt,
    _parse_llm_response,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def storage():
    """Create an in-memory SQLite adapter with schema initialized."""
    adapter = InMemorySQLiteAdapter()
    adapter.init_db()
    return adapter


@pytest.fixture
def sample_chapters():
    """Sample chapter data with paragraphs and spans."""
    return [
        {
            "id": "chapter-1",
            "paragraphs": [
                {
                    "id": "para-1",
                    "spans": [
                        {"id": "span-1a", "span_type": "sentence", "text": "The sun rose over the mountains."},
                        {"id": "span-1b", "span_type": "quotation", "text": '"Good morning," said John.'},
                    ],
                },
                {
                    "id": "para-2",
                    "spans": [
                        {"id": "span-2a", "span_type": "sentence", "text": "Mary waved from across the room."},
                    ],
                },
                {
                    "id": "para-3",
                    "spans": [
                        {"id": "span-3a", "span_type": "quotation", "text": '"Hello everyone," she said.'},
                    ],
                },
            ],
        },
        {
            "id": "chapter-2",
            "paragraphs": [
                {
                    "id": "para-4",
                    "spans": [
                        {"id": "span-4a", "span_type": "sentence", "text": "Later that day, the scene shifted to the city."},
                    ],
                },
                {
                    "id": "para-5",
                    "spans": [
                        {"id": "span-5a", "span_type": "quotation", "text": '"Welcome," said Bob.'},
                    ],
                },
            ],
        },
    ]


@pytest.fixture
def populated_storage(storage, sample_chapters):
    """Storage with populated spine."""
    populate_initial_spine("series-1", "book-1", sample_chapters, storage)
    return storage


@pytest.fixture
def mock_llm_client():
    """Mock OpenAI client."""
    return Mock()


def _make_mock_response(content):
    """Create a mock LLM response with the given content."""
    mock_response = Mock()
    mock_response.choices = [Mock(message=Mock(content=content))]
    return mock_response


def _patch_llm(monkeypatch, mock_llm_client, response_content):
    """Patch resolve_task_llm and create_llm_client for testing."""
    mock_llm_client.chat.completions.create.return_value = _make_mock_response(
        response_content
    )

    monkeypatch.setattr(
        "app.utils.create_llm_client",
        lambda config_path=None: (mock_llm_client, "test-model"),
    )
    monkeypatch.setattr(
        "app.utils.resolve_task_llm",
        lambda task_name, config_path=None: {
            "model_name": "test-model",
            "reasoning_effort": None,
            "temperature": 0.1,
        },
    )


# ---------------------------------------------------------------------------
# Tests: execute()
# ---------------------------------------------------------------------------


class TestExecute:
    """Test the main execute() function."""

    def test_execute_returns_summary_dict(self, populated_storage, mock_llm_client, monkeypatch):
        """execute() returns a summary dict with expected keys."""
        response = json.dumps([
            {"name": "John", "aliases": [], "role": "speaker", "confidence": 0.9}
        ])
        _patch_llm(monkeypatch, mock_llm_client, response)

        result = execute("book-1", populated_storage, {})

        assert "book_id" in result
        assert "scenes_processed" in result
        assert "characters_created" in result
        assert "characters_for_review" in result
        assert "errors" in result
        assert result["book_id"] == "book-1"

    def test_execute_processes_all_scenes(self, populated_storage, mock_llm_client, monkeypatch):
        """execute() processes all scenes in the book."""
        response = json.dumps([
            {"name": "John", "aliases": [], "role": "speaker", "confidence": 0.9}
        ])
        _patch_llm(monkeypatch, mock_llm_client, response)

        result = execute("book-1", populated_storage, {})

        # populate_initial_spine creates one placeholder scene per chapter = 2
        assert result["scenes_processed"] == 2

    def test_characters_created_in_character_table(self, populated_storage, mock_llm_client, monkeypatch):
        """Characters discovered by LLM are created in character table."""
        response = json.dumps([
            {"name": "John", "aliases": ["Mr. J"], "role": "speaker", "confidence": 0.9},
            {"name": "Mary", "aliases": [], "role": "mentioned", "confidence": 0.8},
        ])
        _patch_llm(monkeypatch, mock_llm_client, response)

        result = execute("book-1", populated_storage, {})

        assert result["characters_created"] == 2

        # Verify in DB
        rows = populated_storage.execute_query("SELECT name FROM character ORDER BY name")
        names = [r["name"] for r in rows]
        assert "John" in names
        assert "Mary" in names

    def test_character_scene_junctions_present(self, populated_storage, mock_llm_client, monkeypatch):
        """character_scene junctions are inserted with relation_type='present'."""
        response = json.dumps([
            {"name": "John", "aliases": [], "role": "speaker", "confidence": 0.9}
        ])
        _patch_llm(monkeypatch, mock_llm_client, response)

        execute("book-1", populated_storage, {})

        rows = populated_storage.execute_query(
            "SELECT relation_type FROM character_scene"
        )
        # ALL character_scene relation_types must be 'present' for walk 2b
        for row in rows:
            assert row["relation_type"] == "present"

    def test_character_span_junctions_correct_relation_types(self, populated_storage, mock_llm_client, monkeypatch):
        """character_span junctions are inserted with correct relation_types."""
        response = json.dumps([
            {"name": "John", "aliases": [], "role": "speaker", "confidence": 0.9},
            {"name": "Mary", "aliases": [], "role": "mentioned", "confidence": 0.8},
            {"name": "Bob", "aliases": [], "role": "present", "confidence": 0.85},
        ])
        _patch_llm(monkeypatch, mock_llm_client, response)

        execute("book-1", populated_storage, {})

        # Check John's spans are 'speaker'
        john_rows = populated_storage.execute_query(
            """
            SELECT cs.relation_type FROM character_span cs
            JOIN character c ON cs.character_id = c.id
            WHERE c.name = 'John'
            """
        )
        assert len(john_rows) > 0
        for row in john_rows:
            assert row["relation_type"] == "speaker"

        # Check Mary's spans are 'mentioned'
        mary_rows = populated_storage.execute_query(
            """
            SELECT cs.relation_type FROM character_span cs
            JOIN character c ON cs.character_id = c.id
            WHERE c.name = 'Mary'
            """
        )
        assert len(mary_rows) > 0
        for row in mary_rows:
            assert row["relation_type"] == "mentioned"

        # Check Bob's spans are 'present'
        bob_rows = populated_storage.execute_query(
            """
            SELECT cs.relation_type FROM character_span cs
            JOIN character c ON cs.character_id = c.id
            WHERE c.name = 'Bob'
            """
        )
        assert len(bob_rows) > 0
        for row in bob_rows:
            assert row["relation_type"] == "present"

    def test_confidence_filter_high_accepted(self, populated_storage, mock_llm_client, monkeypatch):
        """Characters with confidence >= 0.7 are auto-accepted."""
        response = json.dumps([
            {"name": "John", "aliases": [], "role": "speaker", "confidence": 0.9},
        ])
        _patch_llm(monkeypatch, mock_llm_client, response)

        result = execute("book-1", populated_storage, {})

        assert result["characters_created"] == 1
        assert result["characters_for_review"] == 0

    def test_confidence_filter_low_rejected(self, populated_storage, mock_llm_client, monkeypatch):
        """Characters with confidence < 0.5 are auto-rejected."""
        response = json.dumps([
            {"name": "Ghost", "aliases": [], "role": "mentioned", "confidence": 0.3},
        ])
        _patch_llm(monkeypatch, mock_llm_client, response)

        result = execute("book-1", populated_storage, {})

        assert result["characters_created"] == 0
        # Verify no character was created
        rows = populated_storage.execute_query("SELECT COUNT(*) AS cnt FROM character")
        assert rows[0]["cnt"] == 0

    def test_confidence_filter_medium_review(self, populated_storage, mock_llm_client, monkeypatch):
        """Characters with 0.5 <= confidence < 0.7 are flagged for review."""
        response = json.dumps([
            {"name": "Mystery", "aliases": [], "role": "mentioned", "confidence": 0.6},
        ])
        _patch_llm(monkeypatch, mock_llm_client, response)

        result = execute("book-1", populated_storage, {})

        # Character IS created but flagged for review
        assert result["characters_created"] == 1
        assert result["characters_for_review"] == 1

    def test_character_book_junction_created(self, populated_storage, mock_llm_client, monkeypatch):
        """character_book junction is created for each character."""
        response = json.dumps([
            {"name": "John", "aliases": [], "role": "speaker", "confidence": 0.9},
        ])
        _patch_llm(monkeypatch, mock_llm_client, response)

        execute("book-1", populated_storage, {})

        rows = populated_storage.execute_query(
            "SELECT book_id, source FROM character_book"
        )
        assert len(rows) == 1
        assert rows[0]["book_id"] == "book-1"
        assert rows[0]["source"] == "walk"

    def test_character_series_junction_created(self, populated_storage, mock_llm_client, monkeypatch):
        """character_series junction is created for each character."""
        response = json.dumps([
            {"name": "John", "aliases": [], "role": "speaker", "confidence": 0.9},
        ])
        _patch_llm(monkeypatch, mock_llm_client, response)

        execute("book-1", populated_storage, {})

        rows = populated_storage.execute_query(
            "SELECT series_id, source FROM character_series"
        )
        assert len(rows) == 1
        assert rows[0]["series_id"] == "series-1"
        assert rows[0]["source"] == "walk"

    def test_no_duplicate_characters_same_book(self, populated_storage, mock_llm_client, monkeypatch):
        """Same character name discovered in multiple scenes creates only one character entity."""
        # Both scenes return "John"
        response = json.dumps([
            {"name": "John", "aliases": [], "role": "speaker", "confidence": 0.9},
        ])
        _patch_llm(monkeypatch, mock_llm_client, response)

        result = execute("book-1", populated_storage, {})

        # Character should only be created once
        assert result["characters_created"] == 1
        rows = populated_storage.execute_query("SELECT COUNT(*) AS cnt FROM character")
        assert rows[0]["cnt"] == 1

        # But should have 2 character_scene junctions (one per scene)
        scene_rows = populated_storage.execute_query(
            "SELECT COUNT(*) AS cnt FROM character_scene"
        )
        assert scene_rows[0]["cnt"] == 2

    def test_default_confidence_when_not_provided(self, populated_storage, mock_llm_client, monkeypatch):
        """Characters without explicit confidence default to 0.8 (auto-accepted)."""
        response = json.dumps([
            {"name": "John", "aliases": [], "role": "speaker"},
        ])
        _patch_llm(monkeypatch, mock_llm_client, response)

        result = execute("book-1", populated_storage, {})

        assert result["characters_created"] == 1

    def test_nonexistent_book_returns_error(self, storage, mock_llm_client, monkeypatch):
        """execute() returns error for nonexistent book."""
        _patch_llm(monkeypatch, mock_llm_client, "[]")

        result = execute("nonexistent-book", storage, {})

        assert len(result["errors"]) > 0
        assert result["characters_created"] == 0


# ---------------------------------------------------------------------------
# Tests: _build_character_discovery_prompt()
# ---------------------------------------------------------------------------


class TestBuildPrompt:
    """Test prompt building."""

    def test_prompt_includes_paragraph_text(self):
        """Prompt includes paragraph text."""
        paragraphs = [
            {"paragraph_id": "para-1", "text": "John spoke loudly."},
            {"paragraph_id": "para-2", "text": "Mary listened."},
        ]

        prompt = _build_character_discovery_prompt(paragraphs)

        assert "John spoke loudly." in prompt
        assert "Mary listened." in prompt

    def test_prompt_asks_for_json_array(self):
        """Prompt asks for JSON array response."""
        paragraphs = [{"paragraph_id": "para-1", "text": "Text."}]

        prompt = _build_character_discovery_prompt(paragraphs)

        assert "JSON array" in prompt
        assert "name" in prompt
        assert "role" in prompt


# ---------------------------------------------------------------------------
# Tests: _parse_llm_response()
# ---------------------------------------------------------------------------


class TestParseResponse:
    """Test LLM response parsing."""

    def test_parse_valid_json(self):
        """Parse valid JSON response."""
        response = json.dumps([
            {"name": "John", "aliases": ["Mr. J"], "role": "speaker", "confidence": 0.9}
        ])

        characters = _parse_llm_response(response)

        assert len(characters) == 1
        assert characters[0]["name"] == "John"
        assert characters[0]["aliases"] == ["Mr. J"]
        assert characters[0]["role"] == "speaker"
        assert characters[0]["confidence"] == 0.9

    def test_parse_json_with_extra_text(self):
        """Parse JSON response with extra text around it."""
        response = 'Here is the JSON:\n[{"name": "John", "aliases": [], "role": "speaker", "confidence": 0.8}]\nDone.'

        characters = _parse_llm_response(response)

        assert len(characters) == 1
        assert characters[0]["name"] == "John"

    def test_parse_invalid_json_returns_empty(self):
        """Parse invalid JSON returns empty list."""
        response = "This is not JSON"

        characters = _parse_llm_response(response)

        assert characters == []

    def test_parse_skips_empty_names(self):
        """Parse skips entries with empty names."""
        response = json.dumps([
            {"name": "", "aliases": [], "role": "speaker", "confidence": 0.9},
            {"name": "John", "aliases": [], "role": "speaker", "confidence": 0.9},
        ])

        characters = _parse_llm_response(response)

        assert len(characters) == 1
        assert characters[0]["name"] == "John"

    def test_parse_defaults_missing_fields(self):
        """Parse defaults missing fields to sensible values."""
        response = json.dumps([
            {"name": "John"},
        ])

        characters = _parse_llm_response(response)

        assert len(characters) == 1
        assert characters[0]["name"] == "John"
        assert characters[0]["aliases"] == []
        assert characters[0]["role"] == "present"
        assert characters[0]["confidence"] == 0.8
