"""Tests for Walk 2i delivery instruction generation."""

import json
from unittest.mock import Mock

import pytest

from app.pipeline.adapter import InMemorySQLiteAdapter
from app.pipeline.populate import populate_initial_spine
from app.pipeline.walks.runner import HeartbeatStorage
from app.pipeline.walks.walk_2i_delivery import (
    _build_delivery_prompt,
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


def _get_review_items(storage):
    """Return all walk_review_item rows written for the test book."""
    return storage.execute_query(
        "SELECT * FROM walk_review_item WHERE book_id = 'book-1' ORDER BY target_id"
    )


class _FailingItemInsert(HeartbeatStorage):
    """HeartbeatStorage wrapper whose walk_review_item INSERT always fails.

    Used to simulate a mid-savepoint unit failure so tests can assert the
    ROLLBACK TO SAVEPOINT removes both the target write and the item row.
    """

    def execute_insert(self, sql, params=()):
        if "walk_review_item" in sql:
            raise RuntimeError("simulated failure writing walk_review_item")
        return super().execute_insert(sql, params)


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

        result = execute("book-1", HeartbeatStorage(populated_storage, "run-1"), {})

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
            "app.utils.resolve_task_config",
            lambda task, storage, book_id: {
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
            "app.utils.resolve_task_config",
            lambda task, storage, book_id: {
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
            "app.utils.resolve_task_config",
            lambda task, storage, book_id: {
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


    def test_walk_override_drives_llm_config(self, populated_storage, monkeypatch, tmp_path):
        """A walk_override row for (book, task) overrides the walk's LLM config.

        Phase 3 (Plan G): the walk resolves its LLM config via
        ``resolve_task_config(task, storage, book_id)`` at unit start, so a
        walk_override row for this book + task must beat the on-disk
        fallbacks and flow into the LLM call (temperature + model).
        """
        # Point config resolution at a tmp path with no config file -> fallbacks.
        monkeypatch.setenv("ALEXANDRIA_CONFIG_PATH", str(tmp_path / "config.json"))

        # walk_override rows override temperature AND model for this book+task.
        populated_storage.execute_insert(
            "INSERT INTO walk_override (book_id, walk_name, key, value_json)"
            " VALUES (?, ?, ?, ?)",
            ("book-1", "delivery", "temperature", json.dumps(0.9)),
        )
        populated_storage.execute_insert(
            "INSERT INTO walk_override (book_id, walk_name, key, value_json)"
            " VALUES (?, ?, ?, ?)",
            ("book-1", "delivery", "model_name", json.dumps("gpt-4o-mini")),
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
            "app.pipeline.walks.walk_2i_delivery.chat_completion",
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
            "app.pipeline.walks.walk_2i_delivery.chat_completion",
            mock_call_llm,
        )

        execute("book-1", populated_storage, {})

        # Unset at every tier -> the walk's built-in system prompt.
        assert captured["system_prompt"] == (
            "You are a TTS delivery specialist for audiobook production, "
            "generating performance instructions for text-to-speech rendering."
        )

        # walk_override row key="prompt" wins over the built-in.
        populated_storage.execute_insert(
            "INSERT INTO walk_override (book_id, walk_name, key, value_json)"
            " VALUES (?, ?, ?, ?)",
            (
                "book-1",
                "delivery",
                "prompt",
                json.dumps("You are a TEST override prompt."),
            ),
        )

        execute("book-1", populated_storage, {})

        assert captured["system_prompt"] == "You are a TEST override prompt."

    def test_prompt_override_config_section_drives_system_prompt(
        self, populated_storage, monkeypatch, tmp_path
    ):
        """Top-level config walk_override[task].prompt flows into system_prompt."""
        (tmp_path / "config.json").write_text(
            json.dumps(
                {
                    "walk_override": {
                        "delivery": {
                            "prompt": "You are a TEST config prompt."
                        }
                    }
                }
            )
        )
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
            "app.pipeline.walks.walk_2i_delivery.chat_completion",
            mock_call_llm,
        )

        execute("book-1", populated_storage, {})

        assert captured["system_prompt"] == "You are a TEST config prompt."


class TestWalkReviewItem:
    """walk_review_item rows written in-walk for review-band delivery instructs."""

    def test_review_band_writes_item_row_with_prior_value(
        self, populated_storage, mock_llm_client, monkeypatch
    ):
        """Review-band instruct writes an item row capturing the prior instruct."""
        # Seed a prior instruct on span-1b so prior_value can be verified
        populated_storage.execute_update(
            "UPDATE span SET instruct = ? WHERE id = ?",
            ("slow and deliberate", "span-1b"),
        )

        response = _make_delivery_response("measured pace", confidence=0.6)
        _patch_llm(monkeypatch, mock_llm_client, response)

        execute("book-1", HeartbeatStorage(populated_storage, "run-1"), {})

        rows = _get_review_items(populated_storage)
        # Every span in the book is review-band → one item row per span
        assert len(rows) == 6
        by_id = {r["target_id"]: r for r in rows}
        row = by_id["span-1b"]
        assert row["kind"] == "instruction"
        assert row["target_table"] == "span"
        assert row["target_id"] == "span-1b"
        assert row["prior_value"] == "slow and deliberate"
        assert row["status"] == "pending"
        assert isinstance(row["created_ms"], int)
        assert row["created_ms"] > 0
        assert row["book_id"] == "book-1"
        assert row["run_id"] == "run-1"
        assert row["id"] == "run-1:instruction:span-1b"
        # Unseeded spans capture prior_value=None
        assert by_id["span-1a"]["prior_value"] is None

        # The instruct itself was still written
        rows = populated_storage.execute_query(
            "SELECT instruct FROM span WHERE id = ?",
            ("span-1b",),
        )
        assert rows[0]["instruct"] == "measured pace"

    def test_auto_accept_writes_no_item_row(
        self, populated_storage, mock_llm_client, monkeypatch
    ):
        """Auto-accepted instruct (>= 0.7) does not write a walk_review_item row."""
        response = _make_delivery_response(confidence=0.9)
        _patch_llm(monkeypatch, mock_llm_client, response)

        execute("book-1", HeartbeatStorage(populated_storage, "run-1"), {})

        assert _get_review_items(populated_storage) == []

    def test_auto_reject_writes_no_item_row(
        self, populated_storage, mock_llm_client, monkeypatch
    ):
        """Auto-rejected instruct (< 0.5) does not write a walk_review_item row."""
        response = _make_delivery_response(confidence=0.3)
        _patch_llm(monkeypatch, mock_llm_client, response)

        execute("book-1", HeartbeatStorage(populated_storage, "run-1"), {})

        assert _get_review_items(populated_storage) == []

    def test_rollback_removes_item_row_and_target_write(
        self, populated_storage, mock_llm_client, monkeypatch
    ):
        """A mid-savepoint failure rolls back BOTH the item row and the instruct write."""
        # Seed a prior instruct; it must survive the rollback unchanged
        populated_storage.execute_update(
            "UPDATE span SET instruct = ? WHERE id = ?",
            ("prior instruct", "span-1b"),
        )

        response = _make_delivery_response("measured pace", confidence=0.6)
        _patch_llm(monkeypatch, mock_llm_client, response)

        failing = _FailingItemInsert(populated_storage, "run-1")
        result = execute("book-1", failing, {})

        # Every span unit fails at the item insert → errors for all, nothing committed
        assert result["spans_processed"] == 6
        assert len(result["errors"]) == 6
        assert result["instructs_generated"] == 0
        assert _get_review_items(populated_storage) == []
        # The seeded prior instruct survives; no new instructs were committed
        rows = populated_storage.execute_query(
            "SELECT instruct FROM span WHERE id = ?",
            ("span-1b",),
        )
        assert rows[0]["instruct"] == "prior instruct"
        rows = populated_storage.execute_query(
            "SELECT COUNT(*) AS cnt FROM span WHERE instruct = 'measured pace'"
        )
        assert rows[0]["cnt"] == 0


class TestSupersede:
    """Completion-time supersede: a successful run supersedes prior pending
    items of the same kind for the targets it regenerated (contract rule #9)."""

    def _patch_llm_side_effect(self, monkeypatch, mock_llm_client, responses):
        """Patch LLM plumbing with per-call responses (in presentation order)."""
        call_count = [0]

        def side_effect(**kwargs):
            idx = call_count[0]
            call_count[0] += 1
            return _make_mock_response(responses[idx])

        mock_llm_client.chat.completions.create.side_effect = side_effect
        monkeypatch.setattr(
            "app.utils.create_llm_client",
            lambda config_path=None: (mock_llm_client, "test-model"),
        )
        monkeypatch.setattr(
            "app.utils.resolve_task_config",
            lambda task, storage, book_id: {
                "model_name": "test-model",
                "reasoning_effort": None,
                "temperature": 0.3,
            },
        )

    def test_rerun_supersedes_regenerated_and_keeps_unregenerated(
        self, populated_storage, mock_llm_client, monkeypatch
    ):
        """Re-running supersedes prior pending items for regenerated spans;
        a span NOT regenerated (auto-rejected this run) keeps its item."""
        response = _make_delivery_response("measured pace", confidence=0.6)
        _patch_llm(monkeypatch, mock_llm_client, response)
        execute("book-1", HeartbeatStorage(populated_storage, "run-1"), {})
        assert len(_get_review_items(populated_storage)) == 6

        # run-2: every span regenerated EXCEPT span-1b (auto-rejected, conf 0.3).
        # Presentation order: 1a, 1b, 2a, 3a, 4a, 5a.
        responses = [
            _make_delivery_response("a", confidence=0.6),
            _make_delivery_response("b", confidence=0.3),
            _make_delivery_response("c", confidence=0.6),
            _make_delivery_response("d", confidence=0.6),
            _make_delivery_response("e", confidence=0.6),
            _make_delivery_response("f", confidence=0.6),
        ]
        self._patch_llm_side_effect(monkeypatch, mock_llm_client, responses)

        execute("book-1", HeartbeatStorage(populated_storage, "run-2"), {})

        rows = _get_review_items(populated_storage)
        assert len(rows) == 11  # 6 from run-1 + 5 regenerated in run-2
        by_run_target = {(r["run_id"], r["target_id"]): r for r in rows}
        # Regenerated spans: old item superseded, new pending item
        assert by_run_target[("run-1", "span-1a")]["status"] == "superseded"
        assert by_run_target[("run-2", "span-1a")]["status"] == "pending"
        # span-1b not regenerated → its run-1 item stays pending, no new item
        assert by_run_target[("run-1", "span-1b")]["status"] == "pending"
        assert ("run-2", "span-1b") not in by_run_target

    def test_top_level_failure_supersedes_nothing(
        self, populated_storage, mock_llm_client, monkeypatch
    ):
        """If execute() raises, no supersede runs — prior items stay pending."""
        response = _make_delivery_response("measured pace", confidence=0.6)
        _patch_llm(monkeypatch, mock_llm_client, response)
        execute("book-1", HeartbeatStorage(populated_storage, "run-1"), {})
        assert len(_get_review_items(populated_storage)) == 6

        # run-2 fails before the loop: client creation raises
        def boom(config_path=None):
            raise RuntimeError("simulated top-level failure")

        monkeypatch.setattr("app.utils.create_llm_client", boom)

        with pytest.raises(RuntimeError):
            execute("book-1", HeartbeatStorage(populated_storage, "run-2"), {})

        rows = _get_review_items(populated_storage)
        assert len(rows) == 6
        assert all(r["status"] == "pending" for r in rows)

    def test_empty_committed_set_supersedes_nothing(
        self, populated_storage, mock_llm_client, monkeypatch
    ):
        """No committed targets → nothing superseded (prior items stay pending)."""
        response = _make_delivery_response("measured pace", confidence=0.6)
        _patch_llm(monkeypatch, mock_llm_client, response)
        execute("book-1", HeartbeatStorage(populated_storage, "run-1"), {})
        assert len(_get_review_items(populated_storage)) == 6

        # run-2 auto-rejects every span → nothing committed → no supersede
        _patch_llm(
            monkeypatch, mock_llm_client, _make_delivery_response(confidence=0.3)
        )
        execute("book-1", HeartbeatStorage(populated_storage, "run-2"), {})

        rows = _get_review_items(populated_storage)
        assert len(rows) == 6
        assert all(r["run_id"] == "run-1" for r in rows)
        assert all(r["status"] == "pending" for r in rows)


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
