"""Tests for Walk 2e span speaker attribution."""

import json
from unittest.mock import Mock

import pytest

from app.pipeline.adapter import InMemorySQLiteAdapter
from app.pipeline.populate import populate_initial_spine
from app.pipeline.walks.walk_2e_span_attribution import (
    _build_speaker_attribution_prompt,
    _get_surrounding_context,
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
                        {
                            "id": "span-1a",
                            "span_type": "sentence",
                            "text": "The sun rose over the mountains.",
                        },
                        {
                            "id": "span-1b",
                            "span_type": "quotation",
                            "text": '"Good morning," said John.',
                        },
                        {
                            "id": "span-1c",
                            "span_type": "sentence",
                            "text": "Mary smiled in response.",
                        },
                    ],
                },
                {
                    "id": "para-2",
                    "spans": [
                        {
                            "id": "span-2a",
                            "span_type": "sentence",
                            "text": "They walked together.",
                        },
                    ],
                },
                {
                    "id": "para-3",
                    "spans": [
                        {
                            "id": "span-3a",
                            "span_type": "quotation",
                            "text": '"Hello everyone," she said.',
                        },
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
                        {
                            "id": "span-4a",
                            "span_type": "sentence",
                            "text": "Later that day, the scene shifted to the city.",
                        },
                    ],
                },
                {
                    "id": "para-5",
                    "spans": [
                        {
                            "id": "span-5a",
                            "span_type": "quotation",
                            "text": '"Welcome," said Bob.',
                        },
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

    def test_execute_returns_summary_dict(
        self, seeded_storage, mock_llm_client, monkeypatch
    ):
        """execute() returns a summary dict with expected keys."""
        response = json.dumps({"character_id": "char-john", "confidence": 0.9})
        _patch_llm(monkeypatch, mock_llm_client, response)

        result = execute("book-1", seeded_storage, {})

        assert "book_id" in result
        assert "spans_processed" in result
        assert "speakers_attributed" in result
        assert "speakers_unknown" in result
        assert "attributions_for_review" in result
        assert "errors" in result
        assert result["book_id"] == "book-1"

    def test_execute_processes_all_quotation_spans(
        self, seeded_storage, mock_llm_client, monkeypatch
    ):
        """execute() processes all quotation spans in the book."""
        response = json.dumps({"character_id": "char-john", "confidence": 0.9})
        _patch_llm(monkeypatch, mock_llm_client, response)

        result = execute("book-1", seeded_storage, {})

        # sample_chapters has 3 quotation spans: span-1b, span-3a, span-5a
        assert result["spans_processed"] == 3

    def test_speaker_junction_created(
        self, seeded_storage, mock_llm_client, monkeypatch
    ):
        """character_span junctions are inserted with relation_type='speaker'."""
        response = json.dumps({"character_id": "char-john", "confidence": 0.9})
        _patch_llm(monkeypatch, mock_llm_client, response)

        result = execute("book-1", seeded_storage, {})

        assert result["speakers_attributed"] >= 1

        # Verify in DB
        rows = seeded_storage.execute_query(
            "SELECT character_id, relation_type, source FROM character_span"
        )
        assert len(rows) >= 1
        for row in rows:
            assert row["relation_type"] == "speaker"
            assert row["source"] == "walk"

    def test_rerun_does_not_duplicate_speaker_junctions(
        self, seeded_storage, mock_llm_client, monkeypatch
    ):
        """Rerunning attribution keeps one speaker row per quotation span."""
        response = json.dumps({"character_id": "char-john", "confidence": 0.9})
        _patch_llm(monkeypatch, mock_llm_client, response)

        execute("book-1", seeded_storage, {})
        execute("book-1", seeded_storage, {})

        rows = seeded_storage.execute_query(
            """SELECT span_id, COUNT(*) AS count
               FROM character_span
               WHERE relation_type = 'speaker'
               GROUP BY span_id"""
        )
        assert rows
        assert all(row["count"] == 1 for row in rows)

    def test_generated_speaker_guess_is_corrected(
        self, seeded_storage, mock_llm_client, monkeypatch
    ):
        """Walk 2e replaces a prior generated speaker attribution."""
        seeded_storage.execute_insert(
            """INSERT INTO character_span
               (character_id, span_id, relation_type, source, confidence, human_override)
               VALUES (?, ?, 'speaker', 'walk', 0.6, 0)""",
            ("char-mary", "span-1b"),
        )
        response = json.dumps({"character_id": "char-john", "confidence": 0.9})
        _patch_llm(monkeypatch, mock_llm_client, response)

        execute("book-1", seeded_storage, {})

        row = seeded_storage.execute_query(
            """SELECT character_id, source, human_override
               FROM character_span
               WHERE span_id = ? AND relation_type = 'speaker'""",
            ("span-1b",),
        )[0]
        assert row["character_id"] == "char-john"
        assert row["source"] == "walk"
        assert row["human_override"] == 0

    def test_human_speaker_override_is_protected(
        self, seeded_storage, mock_llm_client, monkeypatch
    ):
        """Walk 2e cannot replace a human speaker override."""
        seeded_storage.execute_insert(
            """INSERT INTO character_span
               (character_id, span_id, relation_type, source, confidence, human_override)
               VALUES (?, ?, 'speaker', 'human', 1.0, 1)""",
            ("char-mary", "span-1b"),
        )
        response = json.dumps({"character_id": "char-john", "confidence": 0.9})
        _patch_llm(monkeypatch, mock_llm_client, response)

        execute("book-1", seeded_storage, {})

        row = seeded_storage.execute_query(
            """SELECT character_id, source, confidence, human_override
               FROM character_span
               WHERE span_id = ? AND relation_type = 'speaker'""",
            ("span-1b",),
        )[0]
        assert row["character_id"] == "char-mary"
        assert row["source"] == "human"
        assert row["confidence"] == 1.0
        assert row["human_override"] == 1

    def test_unknown_speaker_no_junction(
        self, seeded_storage, mock_llm_client, monkeypatch
    ):
        """When LLM returns null character_id, no junction is created."""
        response = json.dumps({"character_id": None, "confidence": 0.3})
        _patch_llm(monkeypatch, mock_llm_client, response)

        result = execute("book-1", seeded_storage, {})

        # All spans should be unknown
        assert result["speakers_unknown"] == 3
        assert result["speakers_attributed"] == 0

        # Verify no junctions were created
        rows = seeded_storage.execute_query(
            "SELECT COUNT(*) AS cnt FROM character_span"
        )
        assert rows[0]["cnt"] == 0

    def test_confidence_filter_high_accepted(
        self, seeded_storage, mock_llm_client, monkeypatch
    ):
        """Spans with confidence >= 0.7 are auto-accepted."""
        response = json.dumps({"character_id": "char-john", "confidence": 0.9})
        _patch_llm(monkeypatch, mock_llm_client, response)

        result = execute("book-1", seeded_storage, {})

        assert result["speakers_attributed"] >= 1
        assert result["attributions_for_review"] == 0

    def test_confidence_filter_low_rejected(
        self, seeded_storage, mock_llm_client, monkeypatch
    ):
        """Spans with confidence < 0.5 are auto-rejected (no junction created)."""
        response = json.dumps({"character_id": "char-john", "confidence": 0.3})
        _patch_llm(monkeypatch, mock_llm_client, response)

        result = execute("book-1", seeded_storage, {})

        assert result["speakers_attributed"] == 0
        # Verify no junction was created
        rows = seeded_storage.execute_query(
            "SELECT COUNT(*) AS cnt FROM character_span"
        )
        assert rows[0]["cnt"] == 0

    def test_confidence_filter_medium_review(
        self, seeded_storage, mock_llm_client, monkeypatch
    ):
        """Spans with 0.5 <= confidence < 0.7 are flagged for review."""
        response = json.dumps({"character_id": "char-john", "confidence": 0.6})
        _patch_llm(monkeypatch, mock_llm_client, response)

        result = execute("book-1", seeded_storage, {})

        # Junction IS created but flagged for review
        assert result["speakers_attributed"] >= 1
        assert result["attributions_for_review"] >= 1

    def test_nonexistent_book_returns_error(
        self, storage, mock_llm_client, monkeypatch
    ):
        """execute() returns error for nonexistent book."""
        _patch_llm(monkeypatch, mock_llm_client, "{}")

        result = execute("nonexistent-book", storage, {})

        assert len(result["errors"]) > 0
        assert result["speakers_attributed"] == 0

    # ---------------------------------------------------------------------------
    # Tests: _build_speaker_attribution_prompt()
    # ---------------------------------------------------------------------------

    def test_walk_override_drives_llm_config(
        self, seeded_storage, monkeypatch, tmp_path
    ):
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
            ("book-1", "span_attribution", "temperature", json.dumps(0.9)),
        )
        seeded_storage.execute_insert(
            "INSERT INTO walk_override (book_id, walk_name, key, value_json)"
            " VALUES (?, ?, ?, ?)",
            ("book-1", "span_attribution", "model_name", json.dumps("gpt-4o-mini")),
        )

        monkeypatch.setattr(
            "app.utils.create_llm_client", lambda config_path=None: (object(), None)
        )

        captured = {}

        def mock_call_llm(
            client,
            model_name,
            temperature,
            reasoning_effort,
            system_prompt,
            user_prompt,
        ):
            captured["temperature"] = temperature
            captured["model_name"] = model_name
            return "[]"

        monkeypatch.setattr(
            "app.pipeline.walks.walk_2e_span_attribution.chat_completion",
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
            client,
            model_name,
            temperature,
            reasoning_effort,
            system_prompt,
            user_prompt,
        ):
            captured["system_prompt"] = system_prompt
            return "[]"

        monkeypatch.setattr(
            "app.pipeline.walks.walk_2e_span_attribution.chat_completion",
            mock_call_llm,
        )

        execute("book-1", seeded_storage, {})

        # Unset at every tier -> the walk's built-in system prompt.
        assert captured["system_prompt"] == (
            "You are a literary analyst specializing in dialogue attribution in fiction."
        )

        # walk_override row key="prompt" wins over the built-in.
        seeded_storage.execute_insert(
            "INSERT INTO walk_override (book_id, walk_name, key, value_json)"
            " VALUES (?, ?, ?, ?)",
            (
                "book-1",
                "span_attribution",
                "prompt",
                json.dumps("You are a TEST override prompt."),
            ),
        )

        execute("book-1", seeded_storage, {})

        assert captured["system_prompt"] == "You are a TEST override prompt."


class TestBuildPrompt:
    """Test prompt building."""

    def test_prompt_includes_quotation_and_context(self):
        """Prompt includes quotation text and surrounding context."""
        span_text = '"Good morning," said John.'
        context_before = ["The sun rose over the mountains."]
        context_after = ["Mary smiled in response."]
        existing_characters = [{"id": "uuid-john", "name": "John"}]

        prompt = _build_speaker_attribution_prompt(
            span_text=span_text,
            context_before=context_before,
            context_after=context_after,
            existing_characters=existing_characters,
        )

        assert span_text in prompt
        assert "BEFORE: The sun rose over the mountains." in prompt
        assert "AFTER: Mary smiled in response." in prompt

    def test_prompt_includes_character_list(self):
        """Prompt includes character names and UUIDs."""
        span_text = '"Hello," she said.'
        existing_characters = [
            {"id": "uuid-john", "name": "John"},
            {"id": "uuid-mary", "name": "Mary"},
        ]

        prompt = _build_speaker_attribution_prompt(
            span_text=span_text,
            context_before=[],
            context_after=[],
            existing_characters=existing_characters,
        )

        assert "John" in prompt
        assert "uuid-john" in prompt
        assert "Mary" in prompt
        assert "uuid-mary" in prompt

    def test_prompt_asks_for_json_object(self):
        """Prompt asks for JSON object response with character_id."""
        span_text = '"Hello," she said.'
        existing_characters = [{"id": "uuid-john", "name": "John"}]

        prompt = _build_speaker_attribution_prompt(
            span_text=span_text,
            context_before=[],
            context_after=[],
            existing_characters=existing_characters,
        )

        assert "JSON object" in prompt
        assert "character_id" in prompt
        assert "confidence" in prompt


# ---------------------------------------------------------------------------
# Tests: _parse_llm_response()
# ---------------------------------------------------------------------------


class TestParseResponse:
    """Test LLM response parsing."""

    def test_parse_valid_json(self):
        """Parse valid JSON response."""
        response = json.dumps({"character_id": "uuid-john", "confidence": 0.9})

        attribution = _parse_llm_response(response)

        assert attribution["character_id"] == "uuid-john"
        assert attribution["confidence"] == 0.9

    def test_parse_json_with_extra_text(self):
        """Parse JSON response with extra text around it."""
        response = (
            'Here is the JSON:\n{"character_id": "uuid-john", "confidence": 0.8}\nDone.'
        )

        attribution = _parse_llm_response(response)

        assert attribution["character_id"] == "uuid-john"

    def test_parse_invalid_json_returns_empty(self):
        """Parse invalid JSON returns empty dict."""
        response = "This is not JSON"

        attribution = _parse_llm_response(response)

        assert attribution == {}

    def test_parse_null_character_id(self):
        """Parse null character_id (unknown speaker)."""
        response = json.dumps({"character_id": None, "confidence": 0.3})

        attribution = _parse_llm_response(response)

        assert attribution["character_id"] is None
        assert attribution["confidence"] == 0.3

    def test_parse_defaults_missing_confidence(self):
        """Parse defaults missing confidence to 0.8."""
        response = json.dumps({"character_id": "uuid-john"})

        attribution = _parse_llm_response(response)

        assert attribution["character_id"] == "uuid-john"
        assert attribution["confidence"] == 0.8


# ---------------------------------------------------------------------------
# Tests: _get_surrounding_context()
# ---------------------------------------------------------------------------


class TestGetSurroundingContext:
    """Test surrounding context extraction for a quotation span."""

    def test_returns_before_and_after_text(self, populated_storage):
        """Returns text for spans before and after a target quotation span."""
        # para-1 has: span-1a (sentence), span-1b (quotation), span-1c (sentence)
        context = _get_surrounding_context("para-1", "span-1b", populated_storage)

        assert context["before"] == ["The sun rose over the mountains."]
        assert context["after"] == ["Mary smiled in response."]

    def test_first_span_has_no_before(self, populated_storage):
        """First span in paragraph has empty before list."""
        context = _get_surrounding_context("para-1", "span-1a", populated_storage)

        assert context["before"] == []
        assert len(context["after"]) == 2  # span-1b and span-1c

    def test_last_span_has_no_after(self, populated_storage):
        """Last span in paragraph has empty after list."""
        context = _get_surrounding_context("para-1", "span-1c", populated_storage)

        assert len(context["before"]) == 2  # span-1a and span-1b
        assert context["after"] == []

    def test_unknown_span_returns_empty(self, populated_storage):
        """Unknown span_id returns empty before and after."""
        context = _get_surrounding_context(
            "para-1", "nonexistent-span", populated_storage
        )

        assert context == {"before": [], "after": []}
