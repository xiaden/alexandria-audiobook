"""Tests for Walk 2d scene presence refinement."""

import json
from unittest.mock import Mock

import pytest

from app.pipeline.adapter import InMemorySQLiteAdapter
from app.pipeline.populate import populate_initial_spine
from app.pipeline.walks.walk_2d_scene_presence import (
    _build_prompt,
    _parse_llm_response,
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
def seeded_storage(populated_storage):
    """Storage with characters already seeded (walk 2b output)."""
    # Create characters
    populated_storage.execute_insert(
        "INSERT INTO character (id, name, aliases) VALUES (?, ?, ?)",
        ("char-john", "John", "[]"),
    )
    populated_storage.execute_insert(
        "INSERT INTO character (id, name, aliases) VALUES (?, ?, ?)",
        ("char-mary", "Mary", "[]"),
    )
    populated_storage.execute_insert(
        "INSERT INTO character (id, name, aliases) VALUES (?, ?, ?)",
        ("char-bob", "Bob", "[]"),
    )
    # character_book junctions
    populated_storage.execute_insert(
        "INSERT INTO character_book (character_id, book_id, source, confidence, human_override) "
        "VALUES (?, ?, 'walk', 0.9, 0)",
        ("char-john", "book-1"),
    )
    populated_storage.execute_insert(
        "INSERT INTO character_book (character_id, book_id, source, confidence, human_override) "
        "VALUES (?, ?, 'walk', 0.9, 0)",
        ("char-mary", "book-1"),
    )
    populated_storage.execute_insert(
        "INSERT INTO character_book (character_id, book_id, source, confidence, human_override) "
        "VALUES (?, ?, 'walk', 0.9, 0)",
        ("char-bob", "book-1"),
    )
    return populated_storage


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


# ---------------------------------------------------------------------------
# Tests: execute()
# ---------------------------------------------------------------------------


class TestExecute:
    """Test the main execute() function."""

    def test_execute_returns_summary_dict(self, seeded_storage, mock_llm_client, monkeypatch):
        """execute() returns a summary dict with expected keys."""
        response = json.dumps([
            {"character_id": "char-john", "confidence": 0.9}
        ])
        _patch_llm(monkeypatch, mock_llm_client, response)

        result = execute("book-1", seeded_storage, {})

        assert "book_id" in result
        assert "scenes_processed" in result
        assert "junctions_created" in result
        assert "junctions_for_review" in result
        assert "errors" in result
        assert result["book_id"] == "book-1"

    def test_execute_processes_all_scenes(self, seeded_storage, mock_llm_client, monkeypatch):
        """execute() processes all scenes in the book."""
        response = json.dumps([
            {"character_id": "char-john", "confidence": 0.9}
        ])
        _patch_llm(monkeypatch, mock_llm_client, response)

        result = execute("book-1", seeded_storage, {})

        # populate_initial_spine creates one placeholder scene per chapter = 2
        assert result["scenes_processed"] == 2

    def test_presence_junctions_created(self, seeded_storage, mock_llm_client, monkeypatch):
        """character_scene junctions are inserted for characters present in scenes."""
        # Scene 1 (chapter-1): John and Mary present
        # Scene 2 (chapter-2): Bob present
        call_count = [0]
        responses = [
            json.dumps([
                {"character_id": "char-john", "confidence": 0.9},
                {"character_id": "char-mary", "confidence": 0.85},
            ]),
            json.dumps([
                {"character_id": "char-bob", "confidence": 0.9},
            ]),
        ]

        def mock_create(*args, **kwargs):
            idx = call_count[0]
            call_count[0] += 1
            return _make_mock_response(responses[idx] if idx < len(responses) else responses[-1])

        mock_llm_client.chat.completions.create.side_effect = mock_create
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

        result = execute("book-1", seeded_storage, {})

        assert result["junctions_created"] == 3

        # Verify in DB
        rows = seeded_storage.execute_query(
            "SELECT character_id, relation_type, source FROM character_scene ORDER BY character_id"
        )
        char_ids = [r["character_id"] for r in rows]
        assert "char-john" in char_ids
        assert "char-mary" in char_ids
        assert "char-bob" in char_ids
        for row in rows:
            assert row["relation_type"] == "present"
            assert row["source"] == "walk"

    def test_confidence_filter_high_accepted(self, seeded_storage, mock_llm_client, monkeypatch):
        """Characters with confidence >= 0.7 are auto-accepted."""
        response = json.dumps([
            {"character_id": "char-john", "confidence": 0.9},
        ])
        _patch_llm(monkeypatch, mock_llm_client, response)

        result = execute("book-1", seeded_storage, {})

        assert result["junctions_created"] >= 1
        assert result["junctions_for_review"] == 0

    def test_confidence_filter_low_rejected(self, seeded_storage, mock_llm_client, monkeypatch):
        """Characters with confidence < 0.5 are auto-rejected (no junction created)."""
        response = json.dumps([
            {"character_id": "char-john", "confidence": 0.3},
        ])
        _patch_llm(monkeypatch, mock_llm_client, response)

        result = execute("book-1", seeded_storage, {})

        assert result["junctions_created"] == 0
        # Verify no junction was created
        rows = seeded_storage.execute_query(
            "SELECT COUNT(*) AS cnt FROM character_scene"
        )
        assert rows[0]["cnt"] == 0

    def test_confidence_filter_medium_review(self, seeded_storage, mock_llm_client, monkeypatch):
        """Characters with 0.5 <= confidence < 0.7 are flagged for review."""
        response = json.dumps([
            {"character_id": "char-john", "confidence": 0.6},
        ])
        _patch_llm(monkeypatch, mock_llm_client, response)

        result = execute("book-1", seeded_storage, {})

        # Junction IS created but flagged for review
        assert result["junctions_created"] >= 1
        assert result["junctions_for_review"] >= 1

    def test_duplicate_junction_avoided(self, seeded_storage, mock_llm_client, monkeypatch):
        """If character_scene already exists for character+scene, don't create duplicate."""
        # Pre-seed a character_scene junction for char-john in scene 1
        scenes = seeded_storage.execute_query(
            """
            SELECT cs.child_id AS scene_id
            FROM chapter_scene cs
            JOIN chapter c ON cs.parent_id = c.id
            WHERE c.book_id = ?
            ORDER BY c.id
            """,
            ("book-1",),
        )
        scene_1_id = scenes[0]["scene_id"]

        seeded_storage.execute_insert(
            "INSERT INTO character_scene "
            "(character_id, scene_id, relation_type, source, confidence, human_override) "
            "VALUES (?, ?, 'present', 'walk', 0.9, 0)",
            ("char-john", scene_1_id),
        )

        # LLM returns char-john for both scenes
        response = json.dumps([
            {"character_id": "char-john", "confidence": 0.9},
        ])
        _patch_llm(monkeypatch, mock_llm_client, response)

        result = execute("book-1", seeded_storage, {})

        # char-john should have junction in scene 2 only (scene 1 already existed)
        rows = seeded_storage.execute_query(
            "SELECT scene_id FROM character_scene WHERE character_id = ?",
            ("char-john",),
        )
        # Should have 2 junctions: the pre-seeded one + one new one for scene 2
        assert len(rows) == 2

        # junctions_created should be 1 (only the new one)
        assert result["junctions_created"] == 1

    def test_nonexistent_book_returns_error(self, storage, mock_llm_client, monkeypatch):
        """execute() returns error for nonexistent book."""
        _patch_llm(monkeypatch, mock_llm_client, "[]")

        result = execute("nonexistent-book", storage, {})

        assert len(result["errors"]) > 0
        assert result["junctions_created"] == 0


# ---------------------------------------------------------------------------
# Tests: _build_prompt()
# ---------------------------------------------------------------------------


    def test_walk_override_drives_llm_config(self, seeded_storage, monkeypatch, tmp_path):
        """A walk_override row for (book, task) overrides the walk's LLM config.

        Phase 3 (Plan G): the walk resolves its LLM config via
        ``resolve_task_config(task, storage, book_id)`` at unit start, so a
        walk_override row for this book + task must beat the on-disk
        fallbacks and flow into the LLM call (temperature + model).
        """
        # Point config resolution at a tmp path with no config file -> fallbacks.
        monkeypatch.setenv("ALEXANDRIA_CONFIG_PATH", str(tmp_path / "config.json"))

        # walk_override rows override temperature AND model for this book+task.
        seeded_storage.execute_insert(
            "INSERT INTO walk_override (book_id, walk_name, key, value_json)"
            " VALUES (?, ?, ?, ?)",
            ("book-1", "scene_presence", "temperature", json.dumps(0.9)),
        )
        seeded_storage.execute_insert(
            "INSERT INTO walk_override (book_id, walk_name, key, value_json)"
            " VALUES (?, ?, ?, ?)",
            ("book-1", "scene_presence", "model_name", json.dumps("gpt-4o-mini")),
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
            "app.pipeline.walks.walk_2d_scene_presence.chat_completion",
            mock_call_llm,
        )

        execute("book-1", seeded_storage, {})

        # Override wins over the 0.6 fallback temperature and default model.
        assert captured["temperature"] == 0.9
        assert captured["model_name"] == "gpt-4o-mini"

    def test_prompt_override_drives_system_prompt(
        self, seeded_storage, monkeypatch, tmp_path
    ):
        """A walk_override row key='prompt' flows into the LLM system_prompt.

        Phase 3 Amendment (Plan G): ``resolve_task_config`` returns an
        effective ``prompt``; the walk must pass it as ``system_prompt``.
        With no override at any tier the built-in system prompt is used;
        with a walk_override prompt row the row wins.
        """
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
            "app.pipeline.walks.walk_2d_scene_presence.chat_completion",
            mock_call_llm,
        )

        execute("book-1", seeded_storage, {})

        # Unset at every tier -> the walk's built-in system prompt.
        assert captured["system_prompt"] == (
            "You are a literary analyst specializing in character presence in narrative scenes."
        )

        # walk_override row key="prompt" wins over the built-in.
        seeded_storage.execute_insert(
            "INSERT INTO walk_override (book_id, walk_name, key, value_json)"
            " VALUES (?, ?, ?, ?)",
            (
                "book-1",
                "scene_presence",
                "prompt",
                json.dumps("You are a TEST override prompt."),
            ),
        )

        execute("book-1", seeded_storage, {})

        assert captured["system_prompt"] == "You are a TEST override prompt."


class TestBuildPrompt:
    """Test prompt building."""

    def test_prompt_includes_existing_characters(self):
        """Prompt includes existing character names and UUIDs."""
        paragraphs = [
            {"paragraph_id": "para-1", "text": "John spoke loudly."},
            {"paragraph_id": "para-2", "text": "Mary listened."},
        ]
        existing_characters = [
            {"id": "uuid-john", "name": "John"},
            {"id": "uuid-mary", "name": "Mary"},
        ]

        prompt = _build_prompt(paragraphs, existing_characters)

        assert "John" in prompt
        assert "uuid-john" in prompt
        assert "Mary" in prompt
        assert "uuid-mary" in prompt

    def test_prompt_includes_paragraph_text(self):
        """Prompt includes paragraph text."""
        paragraphs = [
            {"paragraph_id": "para-1", "text": "John spoke loudly."},
        ]
        existing_characters = [{"id": "uuid-john", "name": "John"}]

        prompt = _build_prompt(paragraphs, existing_characters)

        assert "John spoke loudly." in prompt

    def test_prompt_asks_for_json_array(self):
        """Prompt asks for JSON array response with character_ids."""
        paragraphs = [{"paragraph_id": "para-1", "text": "Text."}]
        existing_characters = [{"id": "uuid-john", "name": "John"}]

        prompt = _build_prompt(paragraphs, existing_characters)

        assert "JSON array" in prompt
        assert "character_id" in prompt
        assert "confidence" in prompt


# ---------------------------------------------------------------------------
# Tests: _parse_llm_response()
# ---------------------------------------------------------------------------


class TestParseResponse:
    """Test LLM response parsing."""

    def test_parse_valid_json(self):
        """Parse valid JSON response."""
        response = json.dumps([
            {"character_id": "uuid-john", "confidence": 0.9}
        ])

        presence_list = _parse_llm_response(response)

        assert len(presence_list) == 1
        assert presence_list[0]["character_id"] == "uuid-john"
        assert presence_list[0]["confidence"] == 0.9

    def test_parse_json_with_extra_text(self):
        """Parse JSON response with extra text around it."""
        response = 'Here is the JSON:\n[{"character_id": "uuid-john", "confidence": 0.8}]\nDone.'

        presence_list = _parse_llm_response(response)

        assert len(presence_list) == 1
        assert presence_list[0]["character_id"] == "uuid-john"

    def test_parse_invalid_json_returns_empty(self):
        """Parse invalid JSON returns empty list."""
        response = "This is not JSON"

        presence_list = _parse_llm_response(response)

        assert presence_list == []

    def test_parse_skips_empty_character_ids(self):
        """Parse skips entries with empty character_ids."""
        response = json.dumps([
            {"character_id": "", "confidence": 0.9},
            {"character_id": "uuid-john", "confidence": 0.9},
        ])

        presence_list = _parse_llm_response(response)

        assert len(presence_list) == 1
        assert presence_list[0]["character_id"] == "uuid-john"

    def test_parse_defaults_missing_confidence(self):
        """Parse defaults missing confidence to 0.8."""
        response = json.dumps([
            {"character_id": "uuid-john"},
        ])

        presence_list = _parse_llm_response(response)

        assert len(presence_list) == 1
        assert presence_list[0]["character_id"] == "uuid-john"
        assert presence_list[0]["confidence"] == 0.8

    def test_parse_skips_non_dict_items_in_array(self):
        """Parse skips non-dict items in the JSON array (e.g., strings, numbers)."""
        response = json.dumps([
            "just a string",
            42,
            {"character_id": "uuid-john", "confidence": 0.9},
            None,
        ])

        presence_list = _parse_llm_response(response)

        assert len(presence_list) == 1
        assert presence_list[0]["character_id"] == "uuid-john"
