"""Tests for Walk 2f character description generation."""

import json
from unittest.mock import Mock

import pytest

from app.pipeline.adapter import InMemorySQLiteAdapter
from app.pipeline.populate import populate_initial_spine
from app.pipeline.walks.walk_2f_character_description import (
    _build_description_prompt,
    _parse_llm_response,
    _sample_spans,
    execute,
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
    """Patch resolve_task_config and create_llm_client for testing."""
    mock_llm_client.chat.completions.create.return_value = _make_mock_response(
        response_content
    )

    monkeypatch.setattr(
        "app.utils.create_llm_client",
        lambda config_path=None: (mock_llm_client, "test-model"),
    )
    monkeypatch.setattr(
        "app.utils.resolve_task_config",
        lambda task, storage, book_id: {
            "model_name": "test-model",
            "reasoning_effort": None,
            "temperature": 0.1,
        },
    )


def _insert_character(storage, character_id, name, aliases="[]"):
    """Insert a character and character_book junction."""
    storage.execute_insert(
        "INSERT INTO character (id, name, aliases) VALUES (?, ?, ?)",
        (character_id, name, aliases),
    )
    storage.execute_insert(
        "INSERT INTO character_book (character_id, book_id, source, confidence, human_override) "
        "VALUES (?, ?, 'walk', 0.9, 0)",
        (character_id, "book-1"),
    )


def _insert_character_span(storage, character_id, span_id, relation_type):
    """Insert a character_span junction."""
    storage.execute_insert(
        "INSERT INTO character_span "
        "(character_id, span_id, relation_type, source, confidence, human_override) "
        "VALUES (?, ?, ?, 'walk', 0.9, 0)",
        (character_id, span_id, relation_type),
    )


# ---------------------------------------------------------------------------
# Tests: execute()
# ---------------------------------------------------------------------------


class TestExecute:
    """Test the main execute() function."""

    def test_execute_returns_summary_dict(self, populated_storage, mock_llm_client, monkeypatch):
        """execute() returns a summary dict with expected keys."""
        # Set up a character with spans
        _insert_character(populated_storage, "char-1", "John")
        _insert_character_span(populated_storage, "char-1", "span-1b", "speaker")

        response = json.dumps({"description": "John is a speaker.", "confidence": 0.9})
        _patch_llm(monkeypatch, mock_llm_client, response)

        result = execute("book-1", populated_storage, {})

        assert "book_id" in result
        assert "characters_processed" in result
        assert "descriptions_generated" in result
        assert "descriptions_for_review" in result
        assert "errors" in result
        assert result["book_id"] == "book-1"

    def test_description_stored_in_metadata(self, populated_storage, mock_llm_client, monkeypatch):
        """Description is stored in character_metadata with key='description'."""
        _insert_character(populated_storage, "char-1", "John")
        _insert_character_span(populated_storage, "char-1", "span-1b", "speaker")

        response = json.dumps({"description": "John is a stern mentor.", "confidence": 0.9})
        _patch_llm(monkeypatch, mock_llm_client, response)

        execute("book-1", populated_storage, {})

        rows = populated_storage.execute_query(
            "SELECT key, value FROM character_metadata WHERE character_id = ?",
            ("char-1",),
        )
        assert len(rows) == 1
        assert rows[0]["key"] == "description"
        assert rows[0]["value"] == "John is a stern mentor."

    def test_confidence_filter_high_accepted(self, populated_storage, mock_llm_client, monkeypatch):
        """Descriptions with confidence >= 0.7 are auto-accepted."""
        _insert_character(populated_storage, "char-1", "John")
        _insert_character_span(populated_storage, "char-1", "span-1b", "speaker")

        response = json.dumps({"description": "John is a speaker.", "confidence": 0.9})
        _patch_llm(monkeypatch, mock_llm_client, response)

        result = execute("book-1", populated_storage, {})

        assert result["descriptions_generated"] == 1
        assert result["descriptions_for_review"] == 0

    def test_confidence_filter_low_rejected(self, populated_storage, mock_llm_client, monkeypatch):
        """Descriptions with confidence < 0.5 are auto-rejected."""
        _insert_character(populated_storage, "char-1", "John")
        _insert_character_span(populated_storage, "char-1", "span-1b", "speaker")

        response = json.dumps({"description": "John is a speaker.", "confidence": 0.3})
        _patch_llm(monkeypatch, mock_llm_client, response)

        result = execute("book-1", populated_storage, {})

        assert result["descriptions_generated"] == 0
        # Verify no metadata was stored
        rows = populated_storage.execute_query(
            "SELECT COUNT(*) AS cnt FROM character_metadata WHERE character_id = ?",
            ("char-1",),
        )
        assert rows[0]["cnt"] == 0

    def test_confidence_filter_medium_review(self, populated_storage, mock_llm_client, monkeypatch):
        """Descriptions with 0.5 <= confidence < 0.7 are flagged for review."""
        _insert_character(populated_storage, "char-1", "John")
        _insert_character_span(populated_storage, "char-1", "span-1b", "speaker")

        response = json.dumps({"description": "John is a mystery.", "confidence": 0.6})
        _patch_llm(monkeypatch, mock_llm_client, response)

        result = execute("book-1", populated_storage, {})

        # Description IS stored but flagged for review
        assert result["descriptions_generated"] == 1
        assert result["descriptions_for_review"] == 1

        # Verify metadata was stored
        rows = populated_storage.execute_query(
            "SELECT key, value FROM character_metadata WHERE character_id = ?",
            ("char-1",),
        )
        assert len(rows) == 1
        assert rows[0]["key"] == "description"

    def test_skip_character_with_no_spans(self, populated_storage, mock_llm_client, monkeypatch):
        """Character with no spans is skipped (no LLM call, no metadata stored)."""
        _insert_character(populated_storage, "char-1", "John")
        # No character_span junctions inserted

        response = json.dumps({"description": "John is a speaker.", "confidence": 0.9})
        _patch_llm(monkeypatch, mock_llm_client, response)

        result = execute("book-1", populated_storage, {})

        # Character is processed but no description generated (no spans)
        assert result["characters_processed"] == 1
        assert result["descriptions_generated"] == 0

        # Verify no metadata was stored
        rows = populated_storage.execute_query(
            "SELECT COUNT(*) AS cnt FROM character_metadata WHERE character_id = ?",
            ("char-1",),
        )
        assert rows[0]["cnt"] == 0

        # Verify LLM was NOT called
        assert mock_llm_client.chat.completions.create.call_count == 0

    def test_description_update_existing(self, populated_storage, mock_llm_client, monkeypatch):
        """If metadata key='description' already exists, it gets updated (not duplicated)."""
        _insert_character(populated_storage, "char-1", "John")
        _insert_character_span(populated_storage, "char-1", "span-1b", "speaker")

        # Pre-insert a description
        populated_storage.execute_insert(
            "INSERT INTO character_metadata (character_id, key, value) VALUES (?, ?, ?)",
            ("char-1", "description", "Old description"),
        )

        response = json.dumps({"description": "New description", "confidence": 0.9})
        _patch_llm(monkeypatch, mock_llm_client, response)

        execute("book-1", populated_storage, {})

        # Verify only one row exists with the new value
        rows = populated_storage.execute_query(
            "SELECT key, value FROM character_metadata WHERE character_id = ? AND key = 'description'",
            ("char-1",),
        )
        assert len(rows) == 1
        assert rows[0]["value"] == "New description"

    def test_nonexistent_book_returns_error(self, storage, mock_llm_client, monkeypatch):
        """execute() returns error for nonexistent book."""
        _patch_llm(monkeypatch, mock_llm_client, "{}")

        result = execute("nonexistent-book", storage, {})

        assert len(result["errors"]) > 0
        assert result["descriptions_generated"] == 0


# ---------------------------------------------------------------------------
# Tests: _build_description_prompt()
# ---------------------------------------------------------------------------


    def test_walk_override_drives_llm_config(self, populated_storage, monkeypatch, tmp_path):
        """A walk_override row for (book, task) overrides the walk's LLM config.

        Phase 3 (Plan G): the walk resolves its LLM config via
        ``resolve_task_config(task, storage, book_id)`` at unit start, so a
        walk_override row for this book + task must beat the on-disk
        fallbacks and flow into the LLM call (temperature + model).
        """
        _insert_character(populated_storage, "char-1", "John")
        _insert_character_span(populated_storage, "char-1", "span-1b", "speaker")

        # Point config resolution at a tmp path with no config file -> fallbacks.
        monkeypatch.setenv("ALEXANDRIA_CONFIG_PATH", str(tmp_path / "config.json"))

        # walk_override rows override temperature AND model for this book+task.
        populated_storage.execute_insert(
            "INSERT INTO walk_override (book_id, walk_name, key, value_json)"
            " VALUES (?, ?, ?, ?)",
            ("book-1", "character_description", "temperature", json.dumps(0.9)),
        )
        populated_storage.execute_insert(
            "INSERT INTO walk_override (book_id, walk_name, key, value_json)"
            " VALUES (?, ?, ?, ?)",
            ("book-1", "character_description", "model_name", json.dumps("gpt-4o-mini")),
        )

        monkeypatch.setattr(
            "app.utils.create_llm_client", lambda config_path=None: (object(), None)
        )

        captured = {}

        def mock_call_llm(
            client, model_name, temperature, reasoning_effort, system_prompt, user_prompt
        ):
            captured["temperature"] = temperature
            captured["model_name"] = model_name
            return "[]"

        monkeypatch.setattr(
            "app.pipeline.walks.walk_2f_character_description.chat_completion",
            mock_call_llm,
        )

        execute("book-1", populated_storage, {})

        # Override wins over the 0.6 fallback temperature and default model.
        assert captured["temperature"] == 0.9
        assert captured["model_name"] == "gpt-4o-mini"

    def test_prompt_override_drives_system_prompt(
        self, populated_storage, monkeypatch, tmp_path
    ):
        """A walk_override row key='prompt' flows into the LLM system_prompt.

        Phase 3 Amendment (Plan G): ``resolve_task_config`` returns an
        effective ``prompt``; the walk must pass it as ``system_prompt``.
        With no override at any tier the built-in system prompt is used;
        with a walk_override prompt row the row wins.
        """
        # Seed a character with a speaker span so the walk calls the LLM.
        _insert_character(populated_storage, "char-1", "John")
        _insert_character_span(populated_storage, "char-1", "span-1b", "speaker")

        # No config file at the pinned path -> no prompt override at any tier.
        monkeypatch.setenv("ALEXANDRIA_CONFIG_PATH", str(tmp_path / "config.json"))

        monkeypatch.setattr(
            "app.utils.create_llm_client", lambda config_path=None: (object(), None)
        )

        captured = {}

        def mock_call_llm(
            client, model_name, temperature, reasoning_effort, system_prompt, user_prompt
        ):
            captured["system_prompt"] = system_prompt
            return "[]"

        monkeypatch.setattr(
            "app.pipeline.walks.walk_2f_character_description.chat_completion",
            mock_call_llm,
        )

        execute("book-1", populated_storage, {})

        # Unset at every tier -> the walk's built-in system prompt.
        assert captured["system_prompt"] == (
            "You are a literary analyst specializing in character analysis and description."
        )

        # walk_override row key="prompt" wins over the built-in.
        populated_storage.execute_insert(
            "INSERT INTO walk_override (book_id, walk_name, key, value_json)"
            " VALUES (?, ?, ?, ?)",
            (
                "book-1",
                "character_description",
                "prompt",
                json.dumps("You are a TEST override prompt."),
            ),
        )

        execute("book-1", populated_storage, {})

        assert captured["system_prompt"] == "You are a TEST override prompt."


class TestBuildPrompt:
    """Test prompt building."""

    def test_prompt_includes_character_name_and_spans(self):
        """Prompt includes character name and span text."""
        sampled_spans = [
            {"span_id": "span-1", "text": "John spoke loudly.", "relation_type": "speaker"},
            {"span_id": "span-2", "text": "Mary listened.", "relation_type": "mentioned"},
        ]

        prompt = _build_description_prompt("John", '["Mr. J"]', sampled_spans)

        assert "John" in prompt
        assert "John spoke loudly." in prompt
        assert "Mary listened." in prompt
        assert "speaker" in prompt
        assert "mentioned" in prompt

    def test_prompt_includes_aliases(self):
        """Prompt includes character aliases."""
        sampled_spans = [
            {"span_id": "span-1", "text": "Text.", "relation_type": "speaker"},
        ]

        prompt = _build_description_prompt("John", '["Mr. J", "Johnny"]', sampled_spans)

        assert "Mr. J" in prompt
        assert "Johnny" in prompt

    def test_prompt_asks_for_json_object(self):
        """Prompt asks for JSON object response."""
        sampled_spans = [
            {"span_id": "span-1", "text": "Text.", "relation_type": "speaker"},
        ]

        prompt = _build_description_prompt("John", "[]", sampled_spans)

        assert "JSON object" in prompt
        assert "description" in prompt
        assert "confidence" in prompt


# ---------------------------------------------------------------------------
# Tests: _parse_llm_response()
# ---------------------------------------------------------------------------


class TestParseResponse:
    """Test LLM response parsing."""

    def test_parse_valid_json(self):
        """Parse valid JSON response."""
        response = json.dumps({
            "description": "John is a stern mentor.",
            "confidence": 0.9,
        })

        result = _parse_llm_response(response)

        assert result["description"] == "John is a stern mentor."
        assert result["confidence"] == 0.9

    def test_parse_json_with_extra_text(self):
        """Parse JSON response with extra text around it."""
        response = 'Here is the JSON:\n{"description": "John is a speaker.", "confidence": 0.8}\nDone.'

        result = _parse_llm_response(response)

        assert result["description"] == "John is a speaker."
        assert result["confidence"] == 0.8

    def test_parse_invalid_json_returns_empty(self):
        """Parse invalid JSON returns empty dict."""
        response = "This is not JSON"

        result = _parse_llm_response(response)

        assert result == {}

    def test_parse_missing_description_returns_empty(self):
        """Parse response without description field returns empty dict."""
        response = json.dumps({"confidence": 0.9})

        result = _parse_llm_response(response)

        assert result == {}

    def test_parse_empty_description_returns_empty(self):
        """Parse response with empty description returns empty dict."""
        response = json.dumps({"description": "", "confidence": 0.9})

        result = _parse_llm_response(response)

        assert result == {}

    def test_parse_defaults_confidence(self):
        """Parse defaults missing confidence to 0.8."""
        response = json.dumps({"description": "John is a speaker."})

        result = _parse_llm_response(response)

        assert result["description"] == "John is a speaker."
        assert result["confidence"] == 0.8

    def test_parse_non_dict_response_returns_empty(self):
        """Parse non-dict JSON response (e.g., a bare string) returns empty dict."""
        response = json.dumps("just a plain string")

        result = _parse_llm_response(response)

        assert result == {}

    def test_parse_array_response_returns_empty(self):
        """Parse JSON array response (wrong shape) returns empty dict."""
        response = json.dumps([{"description": "wrong shape"}])

        result = _parse_llm_response(response)

        assert result == {}


# ---------------------------------------------------------------------------
# Tests: _sample_spans()
# ---------------------------------------------------------------------------


class TestSampleSpans:
    """Test span sampling logic."""

    def _make_spans(self, n):
        """Create n span dicts with distinct span_ids."""
        return [{"span_id": f"span-{i}", "text": f"Text {i}", "relation_type": "speaker"} for i in range(n)]

    def test_fewer_than_max_returns_all(self):
        """Fewer than 5 spans returns all of them."""
        spans = self._make_spans(3)
        result = _sample_spans(spans, max_samples=5)

        assert len(result) == 3
        assert result == spans

    def test_exactly_max_returns_all(self):
        """Exactly 5 spans returns all 5."""
        spans = self._make_spans(5)
        result = _sample_spans(spans, max_samples=5)

        assert len(result) == 5
        assert result == spans

    def test_more_than_max_returns_evenly_spaced(self):
        """More than 5 spans returns exactly 5 evenly spaced."""
        spans = self._make_spans(10)
        result = _sample_spans(spans, max_samples=5)

        assert len(result) == 5
        # With 10 spans and step=2, indices should be 0, 2, 4, 6, 8
        assert result[0]["span_id"] == "span-0"
        assert result[1]["span_id"] == "span-2"
        assert result[2]["span_id"] == "span-4"
        assert result[3]["span_id"] == "span-6"
        assert result[4]["span_id"] == "span-8"

    def test_empty_list_returns_empty(self):
        """Empty span list returns empty list."""
        result = _sample_spans([], max_samples=5)
        assert result == []
