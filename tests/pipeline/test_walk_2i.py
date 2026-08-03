"""Tests for Walk 2i delivery instruction generation."""

import json
import pytest
from unittest.mock import Mock

from app.pipeline.adapter import InMemorySQLiteAdapter
from app.pipeline.populate import populate_initial_spine
from app.pipeline.walks.walk_2i_delivery import (
    execute,
    _build_delivery_prompt,
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
            "temperature": 0.3,
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


def _insert_voice_config(storage, voice_id, name, description=""):
    """Insert a voice config entry."""
    storage.execute_insert(
        "INSERT INTO voice_config (id, name, description) VALUES (?, ?, ?)",
        (voice_id, name, description),
    )


def _make_delivery_response(instruct="neutral and narrative", confidence=0.9):
    """Create a mock LLM response JSON for delivery."""
    return json.dumps({"instruct": instruct, "confidence": confidence})


# ---------------------------------------------------------------------------
# Tests: execute()
# ---------------------------------------------------------------------------


class TestExecute:
    """Test the main execute() function."""

    def test_execute_returns_summary_dict(self, populated_storage, mock_llm_client, monkeypatch):
        """execute() returns a summary dict with expected keys."""
        response = _make_delivery_response()
        _patch_llm(monkeypatch, mock_llm_client, response)

        result = execute("book-1", populated_storage, {})

        assert "book_id" in result
        assert "spans_processed" in result
        assert "instructs_generated" in result
        assert "instructs_for_review" in result
        assert "errors" in result
        assert result["book_id"] == "book-1"

    def test_instruct_stored_on_span(self, populated_storage, mock_llm_client, monkeypatch):
        """Instruct field is set on span after execution."""
        response = _make_delivery_response("slow and somber", confidence=0.9)
        _patch_llm(monkeypatch, mock_llm_client, response)

        execute("book-1", populated_storage, {})

        # Verify instruct was stored on a span
        rows = populated_storage.execute_query(
            "SELECT id, instruct FROM span WHERE instruct IS NOT NULL"
        )
        assert len(rows) > 0
        assert rows[0]["instruct"] == "slow and somber"

    def test_confidence_filter_high_accepted(self, populated_storage, mock_llm_client, monkeypatch):
        """Instructs with confidence >= 0.7 are auto-accepted and stored."""
        response = _make_delivery_response("warm and conversational", confidence=0.9)
        _patch_llm(monkeypatch, mock_llm_client, response)

        result = execute("book-1", populated_storage, {})

        assert result["instructs_generated"] > 0
        assert result["instructs_for_review"] == 0

        # Verify instruct was stored
        rows = populated_storage.execute_query(
            "SELECT COUNT(*) AS cnt FROM span WHERE instruct IS NOT NULL"
        )
        assert rows[0]["cnt"] > 0

    def test_confidence_filter_low_rejected(self, populated_storage, mock_llm_client, monkeypatch):
        """Instructs with confidence < 0.5 are auto-rejected (instruct stays NULL)."""
        response = _make_delivery_response("some instruct", confidence=0.3)
        _patch_llm(monkeypatch, mock_llm_client, response)

        result = execute("book-1", populated_storage, {})

        assert result["instructs_generated"] == 0
        # Verify no instruct was stored
        rows = populated_storage.execute_query(
            "SELECT COUNT(*) AS cnt FROM span WHERE instruct IS NOT NULL"
        )
        assert rows[0]["cnt"] == 0

    def test_confidence_filter_medium_review(self, populated_storage, mock_llm_client, monkeypatch):
        """Instructs with 0.5 <= confidence < 0.7 are stored but flagged for review."""
        response = _make_delivery_response("measured pace", confidence=0.6)
        _patch_llm(monkeypatch, mock_llm_client, response)

        result = execute("book-1", populated_storage, {})

        # Instruct IS stored but flagged for review
        assert result["instructs_generated"] > 0
        assert result["instructs_for_review"] > 0
        assert result["instructs_for_review"] == result["instructs_generated"]

        # Verify instruct was stored
        rows = populated_storage.execute_query(
            "SELECT COUNT(*) AS cnt FROM span WHERE instruct IS NOT NULL"
        )
        assert rows[0]["cnt"] > 0

    def test_spans_in_presentation_order(self, populated_storage, mock_llm_client, monkeypatch):
        """Spans are processed in presentation order (tracked via mock LLM call order)."""
        # Track the order of LLM calls
        call_order = []

        def track_call(*args, **kwargs):
            # Extract span text from the prompt (user message content)
            messages = kwargs.get("messages", args[1] if len(args) > 1 else [])
            for msg in messages:
                if msg.get("role") == "user":
                    content = msg.get("content", "")
                    # Find the span text in the prompt
                    if "Span text:" in content:
                        # Extract text between "Span text: " and next newline
                        start = content.index("Span text: ") + len("Span text: ")
                        end = content.index("\n", start)
                        span_text = content[start:end]
                        call_order.append(span_text)
                        break
            return _make_mock_response(_make_delivery_response())

        mock_llm_client.chat.completions.create.side_effect = track_call
        monkeypatch.setattr(
            "app.utils.create_llm_client",
            lambda config_path=None: (mock_llm_client, "test-model"),
        )
        monkeypatch.setattr(
            "app.utils.resolve_task_llm",
            lambda task_name, config_path=None: {
                "model_name": "test-model",
                "reasoning_effort": None,
                "temperature": 0.3,
            },
        )

        execute("book-1", populated_storage, {})

        # Verify spans were processed in order
        # Expected order: span-1a, span-1b, span-2a, span-3a, span-4a, span-5a
        expected_texts = [
            "The sun rose over the mountains.",
            '"Good morning," said John.',
            "Mary waved from across the room.",
            '"Hello everyone," she said.',
            "Later that day, the scene shifted to the city.",
            '"Welcome," said Bob.',
        ]
        assert call_order == expected_texts

    def test_speaker_span_with_character_context(self, populated_storage, mock_llm_client, monkeypatch):
        """Span with speaker gets character description + voice profile in prompt."""
        # Set up a character with description and voice profile
        _insert_character(populated_storage, "char-1", "John")
        _insert_character_span(populated_storage, "char-1", "span-1b", "speaker")

        # Add character description
        populated_storage.execute_insert(
            "INSERT INTO character_metadata (character_id, key, value) VALUES (?, ?, ?)",
            ("char-1", "description", "A stern but kind mentor."),
        )
        # Add voice profile
        voice_profile = {"age": "middle-aged", "gender": "male", "tone": "warm"}
        populated_storage.execute_insert(
            "INSERT INTO character_metadata (character_id, key, value) VALUES (?, ?, ?)",
            ("char-1", "voice_profile", json.dumps(voice_profile)),
        )
        # Add voice assignment
        _insert_voice_config(populated_storage, "voice-1", "Deep Narrator", "A warm male voice")
        populated_storage.execute_update(
            "UPDATE character SET voice_assignment_id = ? WHERE id = ?",
            ("voice-1", "char-1"),
        )

        # Track prompts
        prompts_seen = []

        def track_call(*args, **kwargs):
            messages = kwargs.get("messages", args[1] if len(args) > 1 else [])
            for msg in messages:
                if msg.get("role") == "user":
                    prompts_seen.append(msg.get("content", ""))
                    break
            return _make_mock_response(_make_delivery_response())

        mock_llm_client.chat.completions.create.side_effect = track_call
        monkeypatch.setattr(
            "app.utils.create_llm_client",
            lambda config_path=None: (mock_llm_client, "test-model"),
        )
        monkeypatch.setattr(
            "app.utils.resolve_task_llm",
            lambda task_name, config_path=None: {
                "model_name": "test-model",
                "reasoning_effort": None,
                "temperature": 0.3,
            },
        )

        execute("book-1", populated_storage, {})

        # Find the prompt for span-1b (John's dialogue)
        john_prompt = None
        for p in prompts_seen:
            if '"Good morning," said John.' in p:
                john_prompt = p
                break

        assert john_prompt is not None
        assert "John" in john_prompt
        assert "stern but kind mentor" in john_prompt
        assert "middle-aged" in john_prompt
        assert "Deep Narrator" in john_prompt

    def test_narrative_span_without_speaker(self, populated_storage, mock_llm_client, monkeypatch):
        """Span without speaker still gets instruct (NARRATOR)."""
        # Track prompts
        prompts_seen = []

        def track_call(*args, **kwargs):
            messages = kwargs.get("messages", args[1] if len(args) > 1 else [])
            for msg in messages:
                if msg.get("role") == "user":
                    prompts_seen.append(msg.get("content", ""))
                    break
            return _make_mock_response(_make_delivery_response())

        mock_llm_client.chat.completions.create.side_effect = track_call
        monkeypatch.setattr(
            "app.utils.create_llm_client",
            lambda config_path=None: (mock_llm_client, "test-model"),
        )
        monkeypatch.setattr(
            "app.utils.resolve_task_llm",
            lambda task_name, config_path=None: {
                "model_name": "test-model",
                "reasoning_effort": None,
                "temperature": 0.3,
            },
        )

        result = execute("book-1", populated_storage, {})

        # All spans should be processed (no speakers set up)
        assert result["spans_processed"] == 6
        assert result["instructs_generated"] == 6

        # Check that narrative prompts use NARRATOR
        narrative_prompt = None
        for p in prompts_seen:
            if "The sun rose over the mountains." in p:
                narrative_prompt = p
                break

        assert narrative_prompt is not None
        assert "NARRATOR" in narrative_prompt
        assert "narrative text" in narrative_prompt.lower()

    def test_uses_llm_not_rule_based(self, populated_storage, mock_llm_client, monkeypatch):
        """Verify that the LLM is actually called (call count matches spans processed)."""
        response = _make_delivery_response()
        _patch_llm(monkeypatch, mock_llm_client, response)

        result = execute("book-1", populated_storage, {})

        # LLM should be called once per span
        assert mock_llm_client.chat.completions.create.call_count == result["spans_processed"]
        assert mock_llm_client.chat.completions.create.call_count == 6

    def test_nonexistent_book_returns_error(self, storage, mock_llm_client, monkeypatch):
        """execute() returns error for nonexistent book."""
        _patch_llm(monkeypatch, mock_llm_client, "{}")

        result = execute("nonexistent-book", storage, {})

        assert len(result["errors"]) > 0
        assert result["spans_processed"] == 0
        assert result["instructs_generated"] == 0


# ---------------------------------------------------------------------------
# Tests: _build_delivery_prompt()
# ---------------------------------------------------------------------------


class TestBuildPrompt:
    """Test prompt building."""

    def test_prompt_includes_span_text_and_type(self):
        """Prompt includes span text and span type."""
        prompt = _build_delivery_prompt(
            span_text='"I command you to stop!"',
            span_type="quotation",
            speaker_name="Commander Blake",
            character_description="A stern military leader.",
            voice_profile='{"tone": "authoritative"}',
            voice_assignment={"id": "v1", "name": "Deep Voice", "description": "A deep male voice"},
            is_narrative=False,
        )

        assert "quotation" in prompt
        assert "I command you to stop!" in prompt

    def test_prompt_includes_speaker_context(self):
        """Prompt includes speaker name, description, voice profile."""
        prompt = _build_delivery_prompt(
            span_text='"Hello."',
            span_type="quotation",
            speaker_name="John",
            character_description="A wise old wizard.",
            voice_profile='{"age": "elderly", "tone": "warm"}',
            voice_assignment=None,
            is_narrative=False,
        )

        assert "John" in prompt
        assert "wise old wizard" in prompt
        assert "elderly" in prompt

    def test_prompt_includes_voice_assignment(self):
        """Prompt includes voice assignment name and description."""
        prompt = _build_delivery_prompt(
            span_text='"Hello."',
            span_type="quotation",
            speaker_name="John",
            character_description="A wizard.",
            voice_profile='{"tone": "warm"}',
            voice_assignment={"id": "v1", "name": "Gandalf Voice", "description": "Deep and wise"},
            is_narrative=False,
        )

        assert "Gandalf Voice" in prompt
        assert "Deep and wise" in prompt

    def test_prompt_handles_narrative(self):
        """Prompt for narrative text uses NARRATOR and notes it's narrative."""
        prompt = _build_delivery_prompt(
            span_text="The sun rose over the mountains.",
            span_type="sentence",
            speaker_name="NARRATOR",
            character_description="",
            voice_profile="",
            voice_assignment=None,
            is_narrative=True,
        )

        assert "NARRATOR" in prompt
        assert "narrative text" in prompt.lower()
        assert "no specific speaker character" in prompt.lower()

    def test_prompt_asks_for_instruct_json(self):
        """Prompt asks for JSON object response with instruct and confidence."""
        prompt = _build_delivery_prompt(
            span_text="Text.",
            span_type="sentence",
            speaker_name="NARRATOR",
            character_description="",
            voice_profile="",
            voice_assignment=None,
            is_narrative=True,
        )

        assert "JSON object" in prompt
        assert "instruct" in prompt
        assert "confidence" in prompt

    def test_prompt_handles_missing_description(self):
        """Prompt handles missing description gracefully."""
        prompt = _build_delivery_prompt(
            span_text='"Hello."',
            span_type="quotation",
            speaker_name="John",
            character_description="",
            voice_profile="",
            voice_assignment=None,
            is_narrative=False,
        )

        assert "no description available" in prompt
        assert "no voice profile available" in prompt
        assert "no voice assigned" in prompt


# ---------------------------------------------------------------------------
# Tests: _parse_llm_response()
# ---------------------------------------------------------------------------


class TestParseResponse:
    """Test LLM response parsing."""

    def test_parse_valid_json(self):
        """Parse valid JSON response."""
        response = json.dumps({
            "instruct": "slow and somber",
            "confidence": 0.9,
        })

        result = _parse_llm_response(response)

        assert result["instruct"] == "slow and somber"
        assert result["confidence"] == 0.9

    def test_parse_json_with_extra_text(self):
        """Parse JSON response with extra text around it."""
        response = (
            'Here is the JSON:\n'
            '{"instruct": "fast and excited", "confidence": 0.8}\n'
            'Done.'
        )

        result = _parse_llm_response(response)

        assert result["instruct"] == "fast and excited"
        assert result["confidence"] == 0.8

    def test_parse_invalid_json_returns_empty(self):
        """Parse invalid JSON returns empty dict."""
        response = "This is not JSON"

        result = _parse_llm_response(response)

        assert result == {}

    def test_parse_missing_instruct_returns_empty(self):
        """Parse response without instruct field returns empty dict."""
        response = json.dumps({"confidence": 0.9})

        result = _parse_llm_response(response)

        assert result == {}

    def test_parse_empty_instruct_returns_empty(self):
        """Parse response with empty instruct returns empty dict."""
        response = json.dumps({"instruct": "", "confidence": 0.9})

        result = _parse_llm_response(response)

        assert result == {}

    def test_parse_defaults_confidence(self):
        """Parse defaults missing confidence to 0.8."""
        response = json.dumps({"instruct": "neutral"})

        result = _parse_llm_response(response)

        assert result["instruct"] == "neutral"
        assert result["confidence"] == 0.8

    def test_parse_non_dict_response_returns_empty(self):
        """Parse non-dict JSON response (e.g., a bare string) returns empty dict."""
        response = json.dumps("just a plain string")

        result = _parse_llm_response(response)

        assert result == {}

    def test_parse_array_response_returns_empty(self):
        """Parse JSON array response (wrong shape) returns empty dict."""
        response = json.dumps([{"instruct": "wrong shape"}])

        result = _parse_llm_response(response)

        assert result == {}

    def test_parse_non_string_instruct_returns_empty(self):
        """Parse response where instruct is not a string returns empty dict."""
        response = json.dumps({"instruct": 123, "confidence": 0.9})

        result = _parse_llm_response(response)

        assert result == {}
