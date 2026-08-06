"""Tests for Walk 2h voice assignment."""

import json
import pytest
from unittest.mock import Mock

from app.pipeline.adapter import InMemorySQLiteAdapter
from app.pipeline.populate import populate_initial_spine
from app.pipeline.walks.runner import HeartbeatStorage
from app.pipeline.walks.walk_2h_voice_assignment import (
    execute,
    _build_voice_assignment_prompt,
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


def _insert_voice_config(storage, voice_id, name, description=""):
    """Insert a voice into voice_config."""
    storage.execute_insert(
        "INSERT INTO voice_config (id, name, description) VALUES (?, ?, ?)",
        (voice_id, name, description),
    )


def _insert_voice_profile(storage, character_id, voice_profile):
    """Insert a voice profile into character_metadata."""
    storage.execute_insert(
        "INSERT INTO character_metadata (character_id, key, value) VALUES (?, ?, ?)",
        (character_id, "voice_profile", json.dumps(voice_profile)),
    )


def _make_voice_match_response(voice_config_id=None, confidence=0.9, reasoning="Good match."):
    """Create a mock LLM response JSON for voice assignment."""
    return json.dumps({
        "voice_config_id": voice_config_id,
        "reasoning": reasoning,
        "confidence": confidence,
    })


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
        _insert_character(populated_storage, "char-1", "John")
        _insert_voice_config(populated_storage, "voice-1", "Warm Male")
        _insert_voice_profile(
            populated_storage, "char-1",
            {"age": "middle-aged", "gender": "male", "tone": "warm"},
        )

        response = _make_voice_match_response("voice-1", confidence=0.9)
        _patch_llm(monkeypatch, mock_llm_client, response)

        result = execute("book-1", populated_storage, {})

        assert "book_id" in result
        assert "characters_processed" in result
        assert "voices_matched" in result
        assert "voices_unmatched" in result
        assert "assignments_for_review" in result
        assert "errors" in result
        assert result["book_id"] == "book-1"

    def test_voice_matched_with_high_confidence(self, populated_storage, mock_llm_client, monkeypatch):
        """Voice matched with confidence >= 0.7 → voice_assignment_id set on character."""
        _insert_character(populated_storage, "char-1", "John")
        _insert_voice_config(populated_storage, "voice-1", "Warm Male", "A warm, authoritative male voice.")
        _insert_voice_profile(
            populated_storage, "char-1",
            {"age": "middle-aged", "gender": "male", "tone": "warm"},
        )

        response = _make_voice_match_response("voice-1", confidence=0.9)
        _patch_llm(monkeypatch, mock_llm_client, response)

        result = execute("book-1", populated_storage, {})

        assert result["voices_matched"] == 1
        assert result["voices_unmatched"] == 0
        assert result["assignments_for_review"] == 0

        # Verify voice_assignment_id was set on character
        rows = populated_storage.execute_query(
            "SELECT voice_assignment_id FROM character WHERE id = ?",
            ("char-1",),
        )
        assert len(rows) == 1
        assert rows[0]["voice_assignment_id"] == "voice-1"

    def test_voice_matched_medium_confidence_review(self, populated_storage, mock_llm_client, monkeypatch):
        """Voice matched with 0.5 <= confidence < 0.7 → assigned but flagged for review."""
        _insert_character(populated_storage, "char-1", "John")
        _insert_voice_config(populated_storage, "voice-1", "Warm Male", "A warm male voice.")
        _insert_voice_profile(
            populated_storage, "char-1",
            {"age": "middle-aged", "gender": "male", "tone": "warm"},
        )

        response = _make_voice_match_response("voice-1", confidence=0.6)
        _patch_llm(monkeypatch, mock_llm_client, response)

        result = execute("book-1", HeartbeatStorage(populated_storage, "run-1"), {})

        # Voice IS assigned but flagged for review
        assert result["voices_matched"] == 1
        assert result["assignments_for_review"] == 1

        # Verify voice_assignment_id was set
        rows = populated_storage.execute_query(
            "SELECT voice_assignment_id FROM character WHERE id = ?",
            ("char-1",),
        )
        assert rows[0]["voice_assignment_id"] == "voice-1"

    def test_voice_unmatched_low_confidence(self, populated_storage, mock_llm_client, monkeypatch):
        """Voice matched with confidence < 0.5 → voice_assignment_id stays NULL."""
        _insert_character(populated_storage, "char-1", "John")
        _insert_voice_config(populated_storage, "voice-1", "Warm Male", "A warm male voice.")
        _insert_voice_profile(
            populated_storage, "char-1",
            {"age": "middle-aged", "gender": "male", "tone": "warm"},
        )

        response = _make_voice_match_response("voice-1", confidence=0.3)
        _patch_llm(monkeypatch, mock_llm_client, response)

        result = execute("book-1", populated_storage, {})

        assert result["voices_matched"] == 0
        assert result["voices_unmatched"] == 1

        # Verify voice_assignment_id is still NULL
        rows = populated_storage.execute_query(
            "SELECT voice_assignment_id FROM character WHERE id = ?",
            ("char-1",),
        )
        assert rows[0]["voice_assignment_id"] is None

    def test_voice_no_match_in_config(self, populated_storage, mock_llm_client, monkeypatch):
        """LLM returns null voice_config_id → voice_assignment_id stays NULL."""
        _insert_character(populated_storage, "char-1", "John")
        _insert_voice_config(populated_storage, "voice-1", "Warm Male", "A warm male voice.")
        _insert_voice_profile(
            populated_storage, "char-1",
            {"age": "elderly", "gender": "female", "tone": "ethereal"},
        )

        response = _make_voice_match_response(
            voice_config_id=None, confidence=0.8, reasoning="No suitable voice found."
        )
        _patch_llm(monkeypatch, mock_llm_client, response)

        result = execute("book-1", populated_storage, {})

        assert result["voices_matched"] == 0
        assert result["voices_unmatched"] == 1

        # Verify voice_assignment_id is still NULL
        rows = populated_storage.execute_query(
            "SELECT voice_assignment_id FROM character WHERE id = ?",
            ("char-1",),
        )
        assert rows[0]["voice_assignment_id"] is None

    def test_character_without_voice_profile_skipped(self, populated_storage, mock_llm_client, monkeypatch):
        """Character without voice_profile in metadata is skipped."""
        _insert_character(populated_storage, "char-1", "John")
        _insert_voice_config(populated_storage, "voice-1", "Warm Male", "A warm male voice.")
        # No voice_profile inserted into character_metadata

        response = _make_voice_match_response("voice-1", confidence=0.9)
        _patch_llm(monkeypatch, mock_llm_client, response)

        result = execute("book-1", populated_storage, {})

        # Character is processed but no voice matched (no profile)
        assert result["characters_processed"] == 1
        assert result["voices_matched"] == 0
        assert result["voices_unmatched"] == 1

        # Verify LLM was NOT called
        assert mock_llm_client.chat.completions.create.call_count == 0

        # Verify voice_assignment_id is still NULL
        rows = populated_storage.execute_query(
            "SELECT voice_assignment_id FROM character WHERE id = ?",
            ("char-1",),
        )
        assert rows[0]["voice_assignment_id"] is None

    def test_assignment_is_not_locked(self, populated_storage, mock_llm_client, monkeypatch):
        """After assignment, voice_assignment_id can be changed (no locked column)."""
        _insert_character(populated_storage, "char-1", "John")
        _insert_voice_config(populated_storage, "voice-1", "Warm Male", "A warm male voice.")
        _insert_voice_config(populated_storage, "voice-2", "Deep Male", "A deep male voice.")
        _insert_voice_profile(
            populated_storage, "char-1",
            {"age": "middle-aged", "gender": "male", "tone": "warm"},
        )

        # First assignment
        response = _make_voice_match_response("voice-1", confidence=0.9)
        _patch_llm(monkeypatch, mock_llm_client, response)
        execute("book-1", populated_storage, {})

        # Verify initial assignment
        rows = populated_storage.execute_query(
            "SELECT voice_assignment_id FROM character WHERE id = ?",
            ("char-1",),
        )
        assert rows[0]["voice_assignment_id"] == "voice-1"

        # Now manually change the assignment (simulating frontend user change)
        populated_storage.execute_update(
            "UPDATE character SET voice_assignment_id = ? WHERE id = ?",
            ("voice-2", "char-1"),
        )

        # Verify the change took effect
        rows = populated_storage.execute_query(
            "SELECT voice_assignment_id FROM character WHERE id = ?",
            ("char-1",),
        )
        assert rows[0]["voice_assignment_id"] == "voice-2"

        # Verify there is no 'locked' column in the character table
        conn = populated_storage.get_connection()
        cursor = conn.execute("PRAGMA table_info(character)")
        columns = [row[1] for row in cursor.fetchall()]
        assert "locked" not in columns

    def test_voice_config_table_empty(self, populated_storage, mock_llm_client, monkeypatch):
        """Gracefully handle empty voice_config table — all characters unmatched."""
        _insert_character(populated_storage, "char-1", "John")
        _insert_voice_profile(
            populated_storage, "char-1",
            {"age": "middle-aged", "gender": "male", "tone": "warm"},
        )
        # No voices inserted into voice_config

        response = _make_voice_match_response("voice-1", confidence=0.9)
        _patch_llm(monkeypatch, mock_llm_client, response)

        result = execute("book-1", populated_storage, {})

        assert result["characters_processed"] == 1
        assert result["voices_matched"] == 0
        assert result["voices_unmatched"] == 1

        # Verify LLM was NOT called (no voices to match against)
        assert mock_llm_client.chat.completions.create.call_count == 0

        # Verify voice_assignment_id is still NULL
        rows = populated_storage.execute_query(
            "SELECT voice_assignment_id FROM character WHERE id = ?",
            ("char-1",),
        )
        assert rows[0]["voice_assignment_id"] is None

    def test_nonexistent_book_returns_error(self, storage, mock_llm_client, monkeypatch):
        """execute() returns error for nonexistent book."""
        _patch_llm(monkeypatch, mock_llm_client, "{}")

        result = execute("nonexistent-book", storage, {})

        assert len(result["errors"]) > 0
        assert result["voices_matched"] == 0

    def test_invalid_voice_config_id_rejected(self, populated_storage, mock_llm_client, monkeypatch):
        """LLM returns a voice_config_id not in voice_config → rejected."""
        _insert_character(populated_storage, "char-1", "John")
        _insert_voice_config(populated_storage, "voice-1", "Warm Male", "A warm male voice.")
        _insert_voice_profile(
            populated_storage, "char-1",
            {"age": "middle-aged", "gender": "male", "tone": "warm"},
        )

        # LLM returns a voice ID that doesn't exist
        response = _make_voice_match_response("nonexistent-voice", confidence=0.9)
        _patch_llm(monkeypatch, mock_llm_client, response)

        result = execute("book-1", populated_storage, {})

        assert result["voices_matched"] == 0
        assert result["voices_unmatched"] == 1

        # Verify voice_assignment_id is still NULL
        rows = populated_storage.execute_query(
            "SELECT voice_assignment_id FROM character WHERE id = ?",
            ("char-1",),
        )
        assert rows[0]["voice_assignment_id"] is None

    def test_multiple_characters_with_different_voices(self, populated_storage, mock_llm_client, monkeypatch):
        """Multiple characters can be assigned different voices."""
        _insert_character(populated_storage, "char-1", "John")
        _insert_character(populated_storage, "char-2", "Mary")
        _insert_voice_config(populated_storage, "voice-1", "Warm Male", "A warm male voice.")
        _insert_voice_config(populated_storage, "voice-2", "Soft Female", "A soft female voice.")
        _insert_voice_profile(
            populated_storage, "char-1",
            {"age": "middle-aged", "gender": "male", "tone": "warm"},
        )
        _insert_voice_profile(
            populated_storage, "char-2",
            {"age": "young", "gender": "female", "tone": "soft"},
        )

        # Use a side_effect to return different responses for each character
        responses = [
            _make_voice_match_response("voice-1", confidence=0.9),
            _make_voice_match_response("voice-2", confidence=0.85),
        ]
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
            "app.utils.resolve_task_llm",
            lambda task_name, config_path=None: {
                "model_name": "test-model",
                "reasoning_effort": None,
                "temperature": 0.1,
            },
        )

        result = execute("book-1", populated_storage, {})

        assert result["characters_processed"] == 2
        assert result["voices_matched"] == 2

        # Verify assignments
        rows = populated_storage.execute_query(
            "SELECT id, voice_assignment_id FROM character ORDER BY name"
        )
        # John comes before Mary alphabetically (J < M)
        assert rows[0]["voice_assignment_id"] == "voice-1"  # John
        assert rows[1]["voice_assignment_id"] == "voice-2"  # Mary


class TestWalkReviewItem:
    """walk_review_item rows written in-walk for review-band voice assignments."""

    def test_review_band_writes_item_row_with_prior_value(
        self, populated_storage, mock_llm_client, monkeypatch
    ):
        """Review-band assignment writes an item row capturing the prior assignment."""
        _insert_character(populated_storage, "char-1", "John")
        _insert_voice_config(populated_storage, "voice-0", "Old Voice", "A previously assigned voice.")
        _insert_voice_config(populated_storage, "voice-1", "Warm Male", "A warm male voice.")
        _insert_voice_profile(
            populated_storage, "char-1",
            {"age": "middle-aged", "gender": "male", "tone": "warm"},
        )
        # Seed a prior assignment so prior_value can be verified
        populated_storage.execute_update(
            "UPDATE character SET voice_assignment_id = ? WHERE id = ?",
            ("voice-0", "char-1"),
        )

        response = _make_voice_match_response("voice-1", confidence=0.6)
        _patch_llm(monkeypatch, mock_llm_client, response)

        execute("book-1", HeartbeatStorage(populated_storage, "run-1"), {})

        rows = _get_review_items(populated_storage)
        assert len(rows) == 1
        row = rows[0]
        assert row["kind"] == "voice_assignment"
        assert row["target_table"] == "character"
        assert row["target_id"] == "char-1"
        assert row["prior_value"] == "voice-0"
        assert row["status"] == "pending"
        assert isinstance(row["created_ms"], int)
        assert row["created_ms"] > 0
        assert row["book_id"] == "book-1"
        assert row["run_id"] == "run-1"
        assert row["id"] == "run-1:voice_assignment:char-1"

        # The assignment itself was still written
        rows = populated_storage.execute_query(
            "SELECT voice_assignment_id FROM character WHERE id = ?",
            ("char-1",),
        )
        assert rows[0]["voice_assignment_id"] == "voice-1"

    def test_review_band_without_prior_assignment_records_none(
        self, populated_storage, mock_llm_client, monkeypatch
    ):
        """Review-band assignment with no prior assignment records prior_value=None."""
        _insert_character(populated_storage, "char-1", "John")
        _insert_voice_config(populated_storage, "voice-1", "Warm Male", "A warm male voice.")
        _insert_voice_profile(
            populated_storage, "char-1",
            {"age": "middle-aged", "gender": "male", "tone": "warm"},
        )

        response = _make_voice_match_response("voice-1", confidence=0.6)
        _patch_llm(monkeypatch, mock_llm_client, response)

        execute("book-1", HeartbeatStorage(populated_storage, "run-1"), {})

        rows = _get_review_items(populated_storage)
        assert len(rows) == 1
        assert rows[0]["prior_value"] is None

    def test_auto_accept_writes_no_item_row(
        self, populated_storage, mock_llm_client, monkeypatch
    ):
        """Auto-accepted assignment (>= 0.7) does not write a walk_review_item row."""
        _insert_character(populated_storage, "char-1", "John")
        _insert_voice_config(populated_storage, "voice-1", "Warm Male", "A warm male voice.")
        _insert_voice_profile(
            populated_storage, "char-1",
            {"age": "middle-aged", "gender": "male", "tone": "warm"},
        )

        response = _make_voice_match_response("voice-1", confidence=0.9)
        _patch_llm(monkeypatch, mock_llm_client, response)

        execute("book-1", HeartbeatStorage(populated_storage, "run-1"), {})

        assert _get_review_items(populated_storage) == []

    def test_auto_reject_writes_no_item_row(
        self, populated_storage, mock_llm_client, monkeypatch
    ):
        """Auto-rejected match (< 0.5) does not write a walk_review_item row."""
        _insert_character(populated_storage, "char-1", "John")
        _insert_voice_config(populated_storage, "voice-1", "Warm Male", "A warm male voice.")
        _insert_voice_profile(
            populated_storage, "char-1",
            {"age": "middle-aged", "gender": "male", "tone": "warm"},
        )

        response = _make_voice_match_response("voice-1", confidence=0.3)
        _patch_llm(monkeypatch, mock_llm_client, response)

        execute("book-1", HeartbeatStorage(populated_storage, "run-1"), {})

        assert _get_review_items(populated_storage) == []

    def test_rollback_removes_item_row_and_target_write(
        self, populated_storage, mock_llm_client, monkeypatch
    ):
        """A mid-savepoint failure rolls back BOTH the item row and the assignment write."""
        _insert_character(populated_storage, "char-1", "John")
        _insert_voice_config(populated_storage, "voice-0", "Old Voice", "A previously assigned voice.")
        _insert_voice_config(populated_storage, "voice-1", "Warm Male", "A warm male voice.")
        _insert_voice_profile(
            populated_storage, "char-1",
            {"age": "middle-aged", "gender": "male", "tone": "warm"},
        )
        # Seed a prior assignment; it must survive the rollback unchanged
        populated_storage.execute_update(
            "UPDATE character SET voice_assignment_id = ? WHERE id = ?",
            ("voice-0", "char-1"),
        )

        response = _make_voice_match_response("voice-1", confidence=0.6)
        _patch_llm(monkeypatch, mock_llm_client, response)

        failing = _FailingItemInsert(populated_storage, "run-1")
        result = execute("book-1", failing, {})

        # Unit failure recorded; nothing committed
        assert len(result["errors"]) == 1
        assert result["voices_matched"] == 0
        assert _get_review_items(populated_storage) == []
        rows = populated_storage.execute_query(
            "SELECT voice_assignment_id FROM character WHERE id = ?",
            ("char-1",),
        )
        assert rows[0]["voice_assignment_id"] == "voice-0"


class TestSupersede:
    """Completion-time supersede: a successful run supersedes prior pending
    items of the same kind for the targets it regenerated (contract rule #9)."""

    def test_rerun_supersedes_prior_pending_item(
        self, populated_storage, mock_llm_client, monkeypatch
    ):
        """Re-running with a NEW run_id supersedes the old pending item and
        writes a fresh pending item for the regenerated target."""
        _insert_character(populated_storage, "char-1", "John")
        _insert_voice_config(populated_storage, "voice-1", "Warm Male", "A warm male voice.")
        _insert_voice_profile(
            populated_storage, "char-1",
            {"age": "middle-aged", "gender": "male", "tone": "warm"},
        )

        response = _make_voice_match_response("voice-1", confidence=0.6)
        _patch_llm(monkeypatch, mock_llm_client, response)
        execute("book-1", HeartbeatStorage(populated_storage, "run-1"), {})

        rows = _get_review_items(populated_storage)
        assert len(rows) == 1
        assert rows[0]["run_id"] == "run-1"
        assert rows[0]["status"] == "pending"

        # Second run with a new run_id regenerates char-1's assignment
        execute("book-1", HeartbeatStorage(populated_storage, "run-2"), {})

        rows = _get_review_items(populated_storage)
        assert len(rows) == 2
        by_run = {r["run_id"]: r for r in rows}
        assert by_run["run-1"]["status"] == "superseded"
        assert by_run["run-2"]["status"] == "pending"
        assert by_run["run-2"]["id"] == "run-2:voice_assignment:char-1"

    def test_target_not_regenerated_keeps_prior_pending_item(
        self, populated_storage, mock_llm_client, monkeypatch
    ):
        """A target NOT regenerated this run keeps its prior pending item."""
        _insert_character(populated_storage, "char-1", "John")
        _insert_character(populated_storage, "char-2", "Mary")
        _insert_voice_config(populated_storage, "voice-1", "Warm Male", "A warm male voice.")
        _insert_voice_profile(
            populated_storage, "char-1",
            {"age": "middle-aged", "gender": "male", "tone": "warm"},
        )
        _insert_voice_profile(
            populated_storage, "char-2",
            {"age": "young", "gender": "female", "tone": "soft"},
        )

        response = _make_voice_match_response("voice-1", confidence=0.6)
        _patch_llm(monkeypatch, mock_llm_client, response)
        execute("book-1", HeartbeatStorage(populated_storage, "run-1"), {})
        assert len(_get_review_items(populated_storage)) == 2

        # char-2 loses its voice profile → skipped in run-2 → not committed
        populated_storage.execute_delete(
            "DELETE FROM character_metadata "
            "WHERE character_id = 'char-2' AND key = 'voice_profile'",
            (),
        )
        execute("book-1", HeartbeatStorage(populated_storage, "run-2"), {})

        rows = _get_review_items(populated_storage)
        assert len(rows) == 3
        by_run_target = {(r["run_id"], r["target_id"]): r for r in rows}
        # char-1 regenerated → old item superseded, new pending item
        assert by_run_target[("run-1", "char-1")]["status"] == "superseded"
        assert by_run_target[("run-2", "char-1")]["status"] == "pending"
        # char-2 not regenerated → its run-1 item stays pending
        assert by_run_target[("run-1", "char-2")]["status"] == "pending"

    def test_top_level_failure_supersedes_nothing(
        self, populated_storage, mock_llm_client, monkeypatch
    ):
        """If execute() raises, no supersede runs — prior items stay pending."""
        _insert_character(populated_storage, "char-1", "John")
        _insert_voice_config(populated_storage, "voice-1", "Warm Male", "A warm male voice.")
        _insert_voice_profile(
            populated_storage, "char-1",
            {"age": "middle-aged", "gender": "male", "tone": "warm"},
        )

        response = _make_voice_match_response("voice-1", confidence=0.6)
        _patch_llm(monkeypatch, mock_llm_client, response)
        execute("book-1", HeartbeatStorage(populated_storage, "run-1"), {})
        assert _get_review_items(populated_storage)[0]["status"] == "pending"

        # run-2 fails before the loop: client creation raises
        def boom(config_path=None):
            raise RuntimeError("simulated top-level failure")

        monkeypatch.setattr("app.utils.create_llm_client", boom)

        with pytest.raises(RuntimeError):
            execute("book-1", HeartbeatStorage(populated_storage, "run-2"), {})

        rows = _get_review_items(populated_storage)
        assert len(rows) == 1
        assert rows[0]["run_id"] == "run-1"
        assert rows[0]["status"] == "pending"


# ---------------------------------------------------------------------------
# Tests: _build_voice_assignment_prompt()
# ---------------------------------------------------------------------------


class TestBuildPrompt:
    """Test prompt building."""

    def test_prompt_includes_character_name_and_voice_profile(self):
        """Prompt includes character name and voice profile."""
        voice_profile = {"age": "middle-aged", "gender": "male", "tone": "warm"}
        available_voices = [
            {"id": "voice-1", "name": "Warm Male", "description": "A warm male voice."},
        ]

        prompt = _build_voice_assignment_prompt("John", voice_profile, available_voices)

        assert "John" in prompt
        assert "middle-aged" in prompt
        assert "male" in prompt
        assert "warm" in prompt

    def test_prompt_includes_available_voices(self):
        """Prompt includes all available voices."""
        voice_profile = {"age": "middle-aged", "gender": "male"}
        available_voices = [
            {"id": "voice-1", "name": "Warm Male", "description": "A warm male voice."},
            {"id": "voice-2", "name": "Deep Male", "description": "A deep male voice."},
        ]

        prompt = _build_voice_assignment_prompt("John", voice_profile, available_voices)

        assert "voice-1" in prompt
        assert "Warm Male" in prompt
        assert "voice-2" in prompt
        assert "Deep Male" in prompt

    def test_prompt_asks_for_json_with_voice_config_id(self):
        """Prompt asks for JSON object with voice_config_id and confidence."""
        voice_profile = {"age": "young"}
        available_voices = [
            {"id": "voice-1", "name": "Voice One", "description": ""},
        ]

        prompt = _build_voice_assignment_prompt("Jane", voice_profile, available_voices)

        assert "JSON object" in prompt
        assert "voice_config_id" in prompt
        assert "confidence" in prompt
        assert "reasoning" in prompt
        assert "null" in prompt


# ---------------------------------------------------------------------------
# Tests: _parse_llm_response()
# ---------------------------------------------------------------------------


class TestParseResponse:
    """Test LLM response parsing."""

    def test_parse_valid_json_with_match(self):
        """Parse valid JSON response with a voice match."""
        response = json.dumps({
            "voice_config_id": "voice-1",
            "reasoning": "Good match.",
            "confidence": 0.9,
        })

        result = _parse_llm_response(response)

        assert result["voice_config_id"] == "voice-1"
        assert result["reasoning"] == "Good match."
        assert result["confidence"] == 0.9

    def test_parse_valid_json_with_null_match(self):
        """Parse valid JSON response with null voice_config_id."""
        response = json.dumps({
            "voice_config_id": None,
            "reasoning": "No suitable voice.",
            "confidence": 0.3,
        })

        result = _parse_llm_response(response)

        assert result["voice_config_id"] is None
        assert result["reasoning"] == "No suitable voice."
        assert result["confidence"] == 0.3

    def test_parse_json_with_extra_text(self):
        """Parse JSON response with extra text around it."""
        response = (
            'Here is the result:\n'
            '{"voice_config_id": "voice-1", "reasoning": "Match.", "confidence": 0.8}\n'
            'Done.'
        )

        result = _parse_llm_response(response)

        assert result["voice_config_id"] == "voice-1"
        assert result["confidence"] == 0.8

    def test_parse_invalid_json_returns_empty(self):
        """Parse invalid JSON returns empty dict."""
        response = "This is not JSON"

        result = _parse_llm_response(response)

        assert result == {}

    def test_parse_missing_voice_config_id(self):
        """Parse response without voice_config_id still works (defaults to None-like behavior)."""
        response = json.dumps({"reasoning": "Something.", "confidence": 0.9})

        result = _parse_llm_response(response)

        # voice_config_id defaults to None when not present
        assert result["voice_config_id"] is None
        assert result["confidence"] == 0.9

    def test_parse_defaults_confidence(self):
        """Parse defaults missing confidence to 0.8."""
        response = json.dumps({
            "voice_config_id": "voice-1",
            "reasoning": "Match.",
        })

        result = _parse_llm_response(response)

        assert result["voice_config_id"] == "voice-1"
        assert result["confidence"] == 0.8

    def test_parse_non_dict_response_returns_empty(self):
        """Parse non-dict JSON response returns empty dict."""
        response = json.dumps("just a plain string")

        result = _parse_llm_response(response)

        assert result == {}

    def test_parse_array_response_returns_empty(self):
        """Parse JSON array response (wrong shape) returns empty dict."""
        response = json.dumps([{"voice_config_id": "voice-1"}])

        result = _parse_llm_response(response)

        assert result == {}

    def test_parse_invalid_voice_config_id_type_returns_empty(self):
        """Parse response where voice_config_id is not a string or null returns empty dict."""
        response = json.dumps({
            "voice_config_id": 123,
            "reasoning": "Bad type.",
            "confidence": 0.9,
        })

        result = _parse_llm_response(response)

        assert result == {}
