"""Tests for Walk 2g voice audition."""

import json
import pytest
from unittest.mock import Mock

from app.pipeline.adapter import InMemorySQLiteAdapter
from app.pipeline.populate import populate_initial_spine
from app.pipeline.walks.runner import HeartbeatStorage
from app.pipeline.walks.walk_2g_voice_audition import (
    execute,
    _build_voice_audition_prompt,
    _parse_llm_response,
    _sample_spans,
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


def _make_voice_profile_response(voice_profile=None, confidence=0.9):
    """Create a mock LLM response JSON for voice audition."""
    if voice_profile is None:
        voice_profile = {
            "age": "middle-aged",
            "gender": "male",
            "tone": "authoritative but warm",
            "accent": "British RP",
            "pitch": "medium-low",
            "pace": "measured",
        }
    return json.dumps({"voice_profile": voice_profile, "confidence": confidence})


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
        _insert_character_span(populated_storage, "char-1", "span-1b", "speaker")

        response = _make_voice_profile_response()
        _patch_llm(monkeypatch, mock_llm_client, response)

        result = execute("book-1", populated_storage, {})

        assert "book_id" in result
        assert "characters_processed" in result
        assert "profiles_generated" in result
        assert "profiles_for_review" in result
        assert "errors" in result
        assert result["book_id"] == "book-1"

    def test_voice_profile_stored_in_metadata(self, populated_storage, mock_llm_client, monkeypatch):
        """Voice profile is stored in character_metadata with key='voice_profile'."""
        _insert_character(populated_storage, "char-1", "John")
        _insert_character_span(populated_storage, "char-1", "span-1b", "speaker")

        voice_profile = {
            "age": "middle-aged",
            "gender": "male",
            "tone": "authoritative but warm",
            "accent": "British RP",
            "pitch": "medium-low",
            "pace": "measured",
        }
        response = _make_voice_profile_response(voice_profile, confidence=0.9)
        _patch_llm(monkeypatch, mock_llm_client, response)

        execute("book-1", populated_storage, {})

        rows = populated_storage.execute_query(
            "SELECT key, value FROM character_metadata WHERE character_id = ? AND key = 'voice_profile'",
            ("char-1",),
        )
        assert len(rows) == 1
        assert rows[0]["key"] == "voice_profile"
        stored_profile = json.loads(rows[0]["value"])
        assert stored_profile["age"] == "middle-aged"
        assert stored_profile["gender"] == "male"
        assert stored_profile["tone"] == "authoritative but warm"

    def test_confidence_filter_high_accepted(self, populated_storage, mock_llm_client, monkeypatch):
        """Voice profiles with confidence >= 0.7 are auto-accepted."""
        _insert_character(populated_storage, "char-1", "John")
        _insert_character_span(populated_storage, "char-1", "span-1b", "speaker")

        response = _make_voice_profile_response(confidence=0.9)
        _patch_llm(monkeypatch, mock_llm_client, response)

        result = execute("book-1", populated_storage, {})

        assert result["profiles_generated"] == 1
        assert result["profiles_for_review"] == 0

    def test_confidence_filter_low_rejected(self, populated_storage, mock_llm_client, monkeypatch):
        """Voice profiles with confidence < 0.5 are auto-rejected (no metadata stored)."""
        _insert_character(populated_storage, "char-1", "John")
        _insert_character_span(populated_storage, "char-1", "span-1b", "speaker")

        response = _make_voice_profile_response(confidence=0.3)
        _patch_llm(monkeypatch, mock_llm_client, response)

        result = execute("book-1", populated_storage, {})

        assert result["profiles_generated"] == 0
        # Verify no metadata was stored
        rows = populated_storage.execute_query(
            "SELECT COUNT(*) AS cnt FROM character_metadata WHERE character_id = ? AND key = 'voice_profile'",
            ("char-1",),
        )
        assert rows[0]["cnt"] == 0

    def test_confidence_filter_medium_review(self, populated_storage, mock_llm_client, monkeypatch):
        """Voice profiles with 0.5 <= confidence < 0.7 are flagged for review."""
        _insert_character(populated_storage, "char-1", "John")
        _insert_character_span(populated_storage, "char-1", "span-1b", "speaker")

        response = _make_voice_profile_response(confidence=0.6)
        _patch_llm(monkeypatch, mock_llm_client, response)

        result = execute("book-1", HeartbeatStorage(populated_storage, "run-1"), {})

        # Profile IS stored but flagged for review
        assert result["profiles_generated"] == 1
        assert result["profiles_for_review"] == 1

        # Verify metadata was stored
        rows = populated_storage.execute_query(
            "SELECT key, value FROM character_metadata WHERE character_id = ? AND key = 'voice_profile'",
            ("char-1",),
        )
        assert len(rows) == 1

    def test_character_without_description(self, populated_storage, mock_llm_client, monkeypatch):
        """Character with no description in metadata still gets voice profile (based on dialogue only)."""
        _insert_character(populated_storage, "char-1", "John")
        _insert_character_span(populated_storage, "char-1", "span-1b", "speaker")
        # No description in character_metadata

        response = _make_voice_profile_response(confidence=0.9)
        _patch_llm(monkeypatch, mock_llm_client, response)

        result = execute("book-1", populated_storage, {})

        # Profile should still be generated
        assert result["profiles_generated"] == 1

        # Verify metadata was stored
        rows = populated_storage.execute_query(
            "SELECT key, value FROM character_metadata WHERE character_id = ? AND key = 'voice_profile'",
            ("char-1",),
        )
        assert len(rows) == 1

    def test_skip_character_with_no_spans(self, populated_storage, mock_llm_client, monkeypatch):
        """Character with no dialogue spans is skipped (no LLM call, no metadata stored)."""
        _insert_character(populated_storage, "char-1", "John")
        # No character_span junctions inserted

        response = _make_voice_profile_response(confidence=0.9)
        _patch_llm(monkeypatch, mock_llm_client, response)

        result = execute("book-1", populated_storage, {})

        # Character is processed but no profile generated (no spans)
        assert result["characters_processed"] == 1
        assert result["profiles_generated"] == 0

        # Verify no metadata was stored
        rows = populated_storage.execute_query(
            "SELECT COUNT(*) AS cnt FROM character_metadata WHERE character_id = ?",
            ("char-1",),
        )
        assert rows[0]["cnt"] == 0

        # Verify LLM was NOT called
        assert mock_llm_client.chat.completions.create.call_count == 0

    def test_profile_update_existing(self, populated_storage, mock_llm_client, monkeypatch):
        """If voice_profile already exists in metadata, it gets updated (UPSERT)."""
        _insert_character(populated_storage, "char-1", "John")
        _insert_character_span(populated_storage, "char-1", "span-1b", "speaker")

        # Pre-insert a voice profile
        old_profile = {"age": "young", "gender": "female"}
        populated_storage.execute_insert(
            "INSERT INTO character_metadata (character_id, key, value) VALUES (?, ?, ?)",
            ("char-1", "voice_profile", json.dumps(old_profile)),
        )

        new_profile = {
            "age": "middle-aged",
            "gender": "male",
            "tone": "authoritative",
        }
        response = _make_voice_profile_response(new_profile, confidence=0.9)
        _patch_llm(monkeypatch, mock_llm_client, response)

        execute("book-1", populated_storage, {})

        # Verify only one row exists with the new value
        rows = populated_storage.execute_query(
            "SELECT key, value FROM character_metadata WHERE character_id = ? AND key = 'voice_profile'",
            ("char-1",),
        )
        assert len(rows) == 1
        stored_profile = json.loads(rows[0]["value"])
        assert stored_profile["age"] == "middle-aged"
        assert stored_profile["gender"] == "male"

    def test_nonexistent_book_returns_error(self, storage, mock_llm_client, monkeypatch):
        """execute() returns error for nonexistent book."""
        _patch_llm(monkeypatch, mock_llm_client, "{}")

        result = execute("nonexistent-book", storage, {})

        assert len(result["errors"]) > 0
        assert result["profiles_generated"] == 0


class TestWalkReviewItem:
    """walk_review_item rows written in-walk for review-band voice profiles."""

    def test_review_band_writes_item_row_with_prior_value(
        self, populated_storage, mock_llm_client, monkeypatch
    ):
        """Review-band profile writes an item row capturing the prior profile value."""
        _insert_character(populated_storage, "char-1", "John")
        _insert_character_span(populated_storage, "char-1", "span-1b", "speaker")

        # Seed a prior voice_profile so prior_value can be verified
        old_profile = {"age": "young", "gender": "female"}
        populated_storage.execute_insert(
            "INSERT INTO character_metadata (character_id, key, value) VALUES (?, ?, ?)",
            ("char-1", "voice_profile", json.dumps(old_profile)),
        )

        new_profile = {"age": "middle-aged", "gender": "male", "tone": "authoritative but warm"}
        response = _make_voice_profile_response(new_profile, confidence=0.6)
        _patch_llm(monkeypatch, mock_llm_client, response)

        execute("book-1", HeartbeatStorage(populated_storage, "run-1"), {})

        rows = _get_review_items(populated_storage)
        assert len(rows) == 1
        row = rows[0]
        assert row["kind"] == "voice_profile"
        assert row["target_table"] == "character_metadata"
        assert row["target_id"] == "char-1"
        assert row["prior_value"] == json.dumps(old_profile)
        assert row["status"] == "pending"
        assert isinstance(row["created_ms"], int)
        assert row["created_ms"] > 0
        assert row["book_id"] == "book-1"
        assert row["run_id"] == "run-1"
        assert row["id"] == "run-1:voice_profile:char-1"

    def test_review_band_without_prior_value_records_none(
        self, populated_storage, mock_llm_client, monkeypatch
    ):
        """Review-band profile with no prior value records prior_value=None."""
        _insert_character(populated_storage, "char-1", "John")
        _insert_character_span(populated_storage, "char-1", "span-1b", "speaker")

        response = _make_voice_profile_response(confidence=0.6)
        _patch_llm(monkeypatch, mock_llm_client, response)

        execute("book-1", HeartbeatStorage(populated_storage, "run-1"), {})

        rows = _get_review_items(populated_storage)
        assert len(rows) == 1
        assert rows[0]["prior_value"] is None

    def test_auto_accept_writes_no_item_row(
        self, populated_storage, mock_llm_client, monkeypatch
    ):
        """Auto-accepted profile (>= 0.7) does not write a walk_review_item row."""
        _insert_character(populated_storage, "char-1", "John")
        _insert_character_span(populated_storage, "char-1", "span-1b", "speaker")

        response = _make_voice_profile_response(confidence=0.9)
        _patch_llm(monkeypatch, mock_llm_client, response)

        execute("book-1", HeartbeatStorage(populated_storage, "run-1"), {})

        assert _get_review_items(populated_storage) == []

    def test_auto_reject_writes_no_item_row(
        self, populated_storage, mock_llm_client, monkeypatch
    ):
        """Auto-rejected profile (< 0.5) does not write a walk_review_item row."""
        _insert_character(populated_storage, "char-1", "John")
        _insert_character_span(populated_storage, "char-1", "span-1b", "speaker")

        response = _make_voice_profile_response(confidence=0.3)
        _patch_llm(monkeypatch, mock_llm_client, response)

        execute("book-1", HeartbeatStorage(populated_storage, "run-1"), {})

        assert _get_review_items(populated_storage) == []

    def test_rollback_removes_item_row_and_target_write(
        self, populated_storage, mock_llm_client, monkeypatch
    ):
        """A mid-savepoint failure rolls back BOTH the item row and the profile write."""
        _insert_character(populated_storage, "char-1", "John")
        _insert_character_span(populated_storage, "char-1", "span-1b", "speaker")

        response = _make_voice_profile_response(confidence=0.6)
        _patch_llm(monkeypatch, mock_llm_client, response)

        failing = _FailingItemInsert(populated_storage, "run-1")
        result = execute("book-1", failing, {})

        # Unit failure recorded; walk continues; nothing committed
        assert len(result["errors"]) == 1
        assert result["profiles_generated"] == 0
        assert _get_review_items(populated_storage) == []
        rows = populated_storage.execute_query(
            "SELECT COUNT(*) AS cnt FROM character_metadata WHERE character_id = ? AND key = 'voice_profile'",
            ("char-1",),
        )
        assert rows[0]["cnt"] == 0


class TestSupersede:
    """Completion-time supersede: a successful run supersedes prior pending
    items of the same kind for the targets it regenerated (contract rule #9)."""

    def test_rerun_supersedes_prior_pending_item(
        self, populated_storage, mock_llm_client, monkeypatch
    ):
        """Re-running with a NEW run_id supersedes the old pending item and
        writes a fresh pending item for the regenerated target."""
        _insert_character(populated_storage, "char-1", "John")
        _insert_character_span(populated_storage, "char-1", "span-1b", "speaker")

        response = _make_voice_profile_response(confidence=0.6)
        _patch_llm(monkeypatch, mock_llm_client, response)
        execute("book-1", HeartbeatStorage(populated_storage, "run-1"), {})

        rows = _get_review_items(populated_storage)
        assert len(rows) == 1
        assert rows[0]["run_id"] == "run-1"
        assert rows[0]["status"] == "pending"

        # Second run with a new run_id regenerates char-1's profile
        execute("book-1", HeartbeatStorage(populated_storage, "run-2"), {})

        rows = _get_review_items(populated_storage)
        assert len(rows) == 2
        by_run = {r["run_id"]: r for r in rows}
        assert by_run["run-1"]["status"] == "superseded"
        assert by_run["run-2"]["status"] == "pending"
        assert by_run["run-2"]["id"] == "run-2:voice_profile:char-1"

    def test_target_not_regenerated_keeps_prior_pending_item(
        self, populated_storage, mock_llm_client, monkeypatch
    ):
        """A target NOT regenerated this run keeps its prior pending item."""
        _insert_character(populated_storage, "char-1", "John")
        _insert_character_span(populated_storage, "char-1", "span-1b", "speaker")
        _insert_character(populated_storage, "char-2", "Mary")
        _insert_character_span(populated_storage, "char-2", "span-3a", "speaker")

        response = _make_voice_profile_response(confidence=0.6)
        _patch_llm(monkeypatch, mock_llm_client, response)
        execute("book-1", HeartbeatStorage(populated_storage, "run-1"), {})
        assert len(_get_review_items(populated_storage)) == 2

        # char-1 loses its dialogue junction → skipped in run-2 → not committed
        populated_storage.execute_delete(
            "DELETE FROM character_span WHERE character_id = 'char-1'", ()
        )
        execute("book-1", HeartbeatStorage(populated_storage, "run-2"), {})

        rows = _get_review_items(populated_storage)
        assert len(rows) == 3
        by_run_target = {(r["run_id"], r["target_id"]): r for r in rows}
        # char-2 regenerated → old item superseded, new pending item
        assert by_run_target[("run-1", "char-2")]["status"] == "superseded"
        assert by_run_target[("run-2", "char-2")]["status"] == "pending"
        # char-1 not regenerated → its run-1 item stays pending
        assert by_run_target[("run-1", "char-1")]["status"] == "pending"

    def test_supersede_touches_same_kind_only(
        self, populated_storage, mock_llm_client, monkeypatch
    ):
        """Items of a different kind are untouched by the supersede."""
        _insert_character(populated_storage, "char-1", "John")
        _insert_character_span(populated_storage, "char-1", "span-1b", "speaker")

        response = _make_voice_profile_response(confidence=0.6)
        _patch_llm(monkeypatch, mock_llm_client, response)
        execute("book-1", HeartbeatStorage(populated_storage, "run-1"), {})

        # Seed a voice_assignment item for the same target from another run
        populated_storage.execute_insert(
            "INSERT INTO walk_review_item "
            "(id, book_id, run_id, kind, target_table, target_id, prior_value, status, created_ms) "
            "VALUES ('run-other:voice_assignment:char-1', 'book-1', 'run-other', "
            "'voice_assignment', 'character', 'char-1', NULL, 'pending', 1)"
        )

        execute("book-1", HeartbeatStorage(populated_storage, "run-2"), {})

        rows = _get_review_items(populated_storage)
        by_id = {r["id"]: r for r in rows}
        assert by_id["run-1:voice_profile:char-1"]["status"] == "superseded"
        assert by_id["run-2:voice_profile:char-1"]["status"] == "pending"
        # Different kind → untouched
        assert by_id["run-other:voice_assignment:char-1"]["status"] == "pending"

    def test_top_level_failure_supersedes_nothing(
        self, populated_storage, mock_llm_client, monkeypatch
    ):
        """If execute() raises, no supersede runs — prior items stay pending."""
        _insert_character(populated_storage, "char-1", "John")
        _insert_character_span(populated_storage, "char-1", "span-1b", "speaker")

        response = _make_voice_profile_response(confidence=0.6)
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

    def test_empty_committed_set_supersedes_nothing(
        self, populated_storage, mock_llm_client, monkeypatch
    ):
        """No committed targets → nothing superseded (prior item stays pending)."""
        _insert_character(populated_storage, "char-1", "John")
        _insert_character_span(populated_storage, "char-1", "span-1b", "speaker")

        _patch_llm(
            monkeypatch, mock_llm_client, _make_voice_profile_response(confidence=0.6)
        )
        execute("book-1", HeartbeatStorage(populated_storage, "run-1"), {})
        assert len(_get_review_items(populated_storage)) == 1

        # run-2 auto-rejects char-1 → nothing committed → no supersede
        _patch_llm(
            monkeypatch, mock_llm_client, _make_voice_profile_response(confidence=0.3)
        )
        execute("book-1", HeartbeatStorage(populated_storage, "run-2"), {})

        rows = _get_review_items(populated_storage)
        assert len(rows) == 1
        assert rows[0]["run_id"] == "run-1"
        assert rows[0]["status"] == "pending"


# ---------------------------------------------------------------------------
# Tests: _build_voice_audition_prompt()
# ---------------------------------------------------------------------------


class TestBuildPrompt:
    """Test prompt building."""

    def test_prompt_includes_character_name_and_dialogue(self):
        """Prompt includes character name and dialogue text."""
        sampled_spans = [
            {"span_id": "span-1", "text": '"I command you to stop!"'},
            {"span_id": "span-2", "text": '"Very well, have it your way."'},
        ]

        prompt = _build_voice_audition_prompt(
            "Commander Blake",
            '["The Commander"]',
            "A stern military leader.",
            sampled_spans,
        )

        assert "Commander Blake" in prompt
        assert "I command you to stop!" in prompt
        assert "Very well, have it your way." in prompt

    def test_prompt_includes_description(self):
        """Prompt includes character description."""
        sampled_spans = [
            {"span_id": "span-1", "text": '"Hello."'},
        ]

        prompt = _build_voice_audition_prompt(
            "John",
            "[]",
            "A wise old wizard with a long beard.",
            sampled_spans,
        )

        assert "wise old wizard" in prompt

    def test_prompt_handles_missing_description(self):
        """Prompt handles missing description gracefully."""
        sampled_spans = [
            {"span_id": "span-1", "text": '"Hello."'},
        ]

        prompt = _build_voice_audition_prompt("John", "[]", "", sampled_spans)

        assert "no description available" in prompt

    def test_prompt_includes_aliases(self):
        """Prompt includes character aliases."""
        sampled_spans = [
            {"span_id": "span-1", "text": '"Text."'},
        ]

        prompt = _build_voice_audition_prompt(
            "John", '["Mr. J", "Johnny"]', "A speaker.", sampled_spans
        )

        assert "Mr. J" in prompt
        assert "Johnny" in prompt

    def test_prompt_asks_for_voice_profile_json(self):
        """Prompt asks for JSON object response with voice_profile."""
        sampled_spans = [
            {"span_id": "span-1", "text": '"Text."'},
        ]

        prompt = _build_voice_audition_prompt("John", "[]", "A speaker.", sampled_spans)

        assert "JSON object" in prompt
        assert "voice_profile" in prompt
        assert "confidence" in prompt
        assert "age" in prompt
        assert "gender" in prompt
        assert "tone" in prompt


# ---------------------------------------------------------------------------
# Tests: _parse_llm_response()
# ---------------------------------------------------------------------------


class TestParseResponse:
    """Test LLM response parsing."""

    def test_parse_valid_json(self):
        """Parse valid JSON response."""
        voice_profile = {"age": "middle-aged", "gender": "male", "tone": "warm"}
        response = json.dumps({
            "voice_profile": voice_profile,
            "confidence": 0.9,
        })

        result = _parse_llm_response(response)

        assert result["voice_profile"] == voice_profile
        assert result["confidence"] == 0.9

    def test_parse_json_with_extra_text(self):
        """Parse JSON response with extra text around it."""
        response = (
            'Here is the JSON:\n'
            '{"voice_profile": {"age": "young"}, "confidence": 0.8}\n'
            'Done.'
        )

        result = _parse_llm_response(response)

        assert result["voice_profile"]["age"] == "young"
        assert result["confidence"] == 0.8

    def test_parse_invalid_json_returns_empty(self):
        """Parse invalid JSON returns empty dict."""
        response = "This is not JSON"

        result = _parse_llm_response(response)

        assert result == {}

    def test_parse_missing_voice_profile_returns_empty(self):
        """Parse response without voice_profile field returns empty dict."""
        response = json.dumps({"confidence": 0.9})

        result = _parse_llm_response(response)

        assert result == {}

    def test_parse_non_dict_voice_profile_returns_empty(self):
        """Parse response where voice_profile is not a dict returns empty dict."""
        response = json.dumps({"voice_profile": "not a dict", "confidence": 0.9})

        result = _parse_llm_response(response)

        assert result == {}

    def test_parse_defaults_confidence(self):
        """Parse defaults missing confidence to 0.8."""
        response = json.dumps({
            "voice_profile": {"age": "young"},
        })

        result = _parse_llm_response(response)

        assert result["voice_profile"]["age"] == "young"
        assert result["confidence"] == 0.8

    def test_parse_non_dict_response_returns_empty(self):
        """Parse non-dict JSON response (e.g., a bare string) returns empty dict."""
        response = json.dumps("just a plain string")

        result = _parse_llm_response(response)

        assert result == {}

    def test_parse_array_response_returns_empty(self):
        """Parse JSON array response (wrong shape) returns empty dict."""
        response = json.dumps([{"voice_profile": {"age": "young"}}])

        result = _parse_llm_response(response)

        assert result == {}


# ---------------------------------------------------------------------------
# Tests: _sample_spans()
# ---------------------------------------------------------------------------


class TestSampleSpans:
    """Test span sampling logic."""

    def _make_spans(self, n):
        """Create n span dicts with distinct span_ids."""
        return [{"span_id": f"span-{i}", "text": f"Text {i}"} for i in range(n)]

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
