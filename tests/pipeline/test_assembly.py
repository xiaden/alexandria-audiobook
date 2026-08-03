"""Tests for export_annotated_script — deterministic assembly.

Covers:
- Output format: [{speaker, text, instruct}]
- UNKNOWN→NARRATOR: spans with no speaker junction → speaker='NARRATOR'
- Presentation ordering: spans returned in global_index order
- Voice config lookup: character with voice_assignment_id resolves correctly
- Empty book returns empty list
"""

from __future__ import annotations

import pytest

from app.pipeline.adapter import InMemorySQLiteAdapter
from app.pipeline.assembly import export_annotated_script


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _populate_storage(storage: InMemorySQLiteAdapter) -> None:
    """Insert a minimal but complete document spine with characters."""
    # -- Voice config -------------------------------------------------------
    storage.execute_insert(
        "INSERT INTO voice_config (id, name, description) VALUES ('vc1', 'Warm Female', 'A warm female voice')"
    )
    storage.execute_insert(
        "INSERT INTO voice_config (id, name, description) VALUES ('vc2', 'Deep Male', 'A deep male voice')"
    )

    # -- Series + Book ------------------------------------------------------
    storage.execute_insert("INSERT INTO series (id) VALUES ('s1')")
    storage.execute_insert(
        "INSERT INTO book (id, series_id, position) VALUES ('b1', 's1', 1)"
    )

    # -- Chapters -----------------------------------------------------------
    storage.execute_insert(
        "INSERT INTO chapter (id, book_id) VALUES ('ch1', 'b1')"
    )
    storage.execute_insert(
        "INSERT INTO chapter (id, book_id) VALUES ('ch2', 'b1')"
    )
    storage.execute_insert(
        "INSERT INTO book_chapter (child_id, parent_id, position) VALUES ('ch1', 'b1', 1)"
    )
    storage.execute_insert(
        "INSERT INTO book_chapter (child_id, parent_id, position) VALUES ('ch2', 'b1', 2)"
    )

    # -- Scenes -------------------------------------------------------------
    storage.execute_insert("INSERT INTO scene (id) VALUES ('sc1')")
    storage.execute_insert("INSERT INTO scene (id) VALUES ('sc2')")
    storage.execute_insert(
        "INSERT INTO chapter_scene (child_id, parent_id, position) VALUES ('sc1', 'ch1', 1)"
    )
    storage.execute_insert(
        "INSERT INTO chapter_scene (child_id, parent_id, position) VALUES ('sc2', 'ch2', 1)"
    )

    # -- Paragraphs ---------------------------------------------------------
    storage.execute_insert("INSERT INTO paragraph (id) VALUES ('p1')")
    storage.execute_insert("INSERT INTO paragraph (id) VALUES ('p2')")
    storage.execute_insert("INSERT INTO paragraph (id) VALUES ('p3')")
    storage.execute_insert(
        "INSERT INTO scene_paragraph (child_id, parent_id, position) VALUES ('p1', 'sc1', 1)"
    )
    storage.execute_insert(
        "INSERT INTO scene_paragraph (child_id, parent_id, position) VALUES ('p2', 'sc1', 2)"
    )
    storage.execute_insert(
        "INSERT INTO scene_paragraph (child_id, parent_id, position) VALUES ('p3', 'sc2', 1)"
    )

    # -- Spans --------------------------------------------------------------
    storage.execute_insert(
        "INSERT INTO span (id, span_type, text, instruct) VALUES ('sp1', 'quotation', 'Hello there!', 'cheerfully')"
    )
    storage.execute_insert(
        "INSERT INTO span (id, span_type, text, instruct) VALUES ('sp2', 'sentence', 'She walked away.', NULL)"
    )
    storage.execute_insert(
        "INSERT INTO span (id, span_type, text, instruct) VALUES ('sp3', 'quotation', 'Goodbye.', 'sadly')"
    )
    storage.execute_insert(
        "INSERT INTO span (id, span_type, text, instruct) VALUES ('sp4', 'sentence', 'No one spoke.', NULL)"
    )
    storage.execute_insert(
        "INSERT INTO paragraph_span (child_id, parent_id, position) VALUES ('sp1', 'p1', 1)"
    )
    storage.execute_insert(
        "INSERT INTO paragraph_span (child_id, parent_id, position) VALUES ('sp2', 'p2', 1)"
    )
    storage.execute_insert(
        "INSERT INTO paragraph_span (child_id, parent_id, position) VALUES ('sp3', 'p3', 1)"
    )
    storage.execute_insert(
        "INSERT INTO paragraph_span (child_id, parent_id, position) VALUES ('sp4', 'p3', 2)"
    )

    # -- Characters ---------------------------------------------------------
    storage.execute_insert(
        "INSERT INTO character (id, name, aliases, voice_assignment_id) VALUES ('c1', 'Alice', '[]', 'vc1')"
    )
    storage.execute_insert(
        "INSERT INTO character (id, name, aliases, voice_assignment_id) VALUES ('c2', 'Bob', '[]', 'vc2')"
    )

    # -- character_span (speaker junctions) ---------------------------------
    # sp1 → Alice is speaker
    storage.execute_insert(
        """INSERT INTO character_span (character_id, span_id, relation_type, source, confidence)
           VALUES ('c1', 'sp1', 'speaker', 'walk', 0.95)"""
    )
    # sp2 → no speaker junction (UNKNOWN → NARRATOR)
    # sp3 → Bob is speaker
    storage.execute_insert(
        """INSERT INTO character_span (character_id, span_id, relation_type, source, confidence)
           VALUES ('c2', 'sp3', 'speaker', 'walk', 0.9)"""
    )
    # sp4 → no speaker junction (UNKNOWN → NARRATOR)


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def storage():
    """Return a populated InMemorySQLiteAdapter."""
    s = InMemorySQLiteAdapter()
    s.init_db()
    _populate_storage(s)
    return s


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestExportAnnotatedScriptFormat:
    def test_returns_list_of_dicts(self, storage):
        """Output is a list of dicts with speaker, text, instruct keys."""
        script = export_annotated_script("b1", storage)
        assert isinstance(script, list)
        assert len(script) > 0
        for entry in script:
            assert "speaker" in entry
            assert "text" in entry
            assert "instruct" in entry

    def test_speaker_is_string(self, storage):
        """Speaker values are strings."""
        script = export_annotated_script("b1", storage)
        for entry in script:
            assert isinstance(entry["speaker"], str)

    def test_text_is_string(self, storage):
        """Text values are strings."""
        script = export_annotated_script("b1", storage)
        for entry in script:
            assert isinstance(entry["text"], str)

    def test_instruct_is_string(self, storage):
        """Instruct values are strings (empty string when NULL)."""
        script = export_annotated_script("b1", storage)
        for entry in script:
            assert isinstance(entry["instruct"], str)


class TestUnknownToNarrator:
    def test_no_speaker_junction_becomes_narrator(self, storage):
        """Spans with no character_span.speaker junction → speaker='NARRATOR'."""
        script = export_annotated_script("b1", storage)
        # sp2 and sp4 have no speaker junction
        narrated = [e for e in script if e["speaker"] == "NARRATOR"]
        assert len(narrated) == 2
        narrated_texts = {e["text"] for e in narrated}
        assert "She walked away." in narrated_texts
        assert "No one spoke." in narrated_texts

    def test_speaker_junction_resolves_to_character_name(self, storage):
        """Spans with a speaker junction resolve to the character name."""
        script = export_annotated_script("b1", storage)
        alice_lines = [e for e in script if e["speaker"] == "Alice"]
        assert len(alice_lines) == 1
        assert alice_lines[0]["text"] == "Hello there!"

        bob_lines = [e for e in script if e["speaker"] == "Bob"]
        assert len(bob_lines) == 1
        assert bob_lines[0]["text"] == "Goodbye."


class TestPresentationOrdering:
    def test_spans_in_global_index_order(self, storage):
        """Spans are returned in presentation order (chapter → scene → paragraph → span)."""
        script = export_annotated_script("b1", storage)
        texts = [e["text"] for e in script]
        # Expected order: sp1 (ch1/sc1/p1), sp2 (ch1/sc1/p2), sp3 (ch2/sc2/p3), sp4 (ch2/sc2/p3)
        assert texts == [
            "Hello there!",
            "She walked away.",
            "Goodbye.",
            "No one spoke.",
        ]

    def test_ordering_across_chapters(self, storage):
        """Spans from chapter 1 come before spans from chapter 2."""
        script = export_annotated_script("b1", storage)
        # First two spans are from ch1, last two from ch2
        assert script[0]["text"] == "Hello there!"  # ch1
        assert script[1]["text"] == "She walked away."  # ch1
        assert script[2]["text"] == "Goodbye."  # ch2
        assert script[3]["text"] == "No one spoke."  # ch2


class TestVoiceConfigLookup:
    def test_character_with_voice_assignment_resolves(self, storage):
        """A character with voice_assignment_id is correctly resolved as speaker."""
        # Alice has voice_assignment_id = 'vc1'
        script = export_annotated_script("b1", storage)
        alice_entries = [e for e in script if e["speaker"] == "Alice"]
        assert len(alice_entries) == 1
        # Verify the character exists with voice config in the DB
        chars = storage.execute_query(
            "SELECT voice_assignment_id FROM character WHERE name = 'Alice'"
        )
        assert len(chars) == 1
        assert chars[0]["voice_assignment_id"] == "vc1"

    def test_voice_config_exists_for_assigned_character(self, storage):
        """The voice_assignment_id references a valid voice_config row."""
        chars = storage.execute_query(
            """SELECT c.name, vc.name AS voice_name
                 FROM character c
                 JOIN voice_config vc ON c.voice_assignment_id = vc.id
                WHERE c.name = 'Alice'"""
        )
        assert len(chars) == 1
        assert chars[0]["voice_name"] == "Warm Female"


class TestInstructField:
    def test_instruct_present_when_set(self, storage):
        """Spans with instruct column set return the instruction text."""
        script = export_annotated_script("b1", storage)
        cheerful = [e for e in script if e["instruct"] == "cheerfully"]
        assert len(cheerful) == 1
        assert cheerful[0]["text"] == "Hello there!"

    def test_instruct_empty_string_when_null(self, storage):
        """Spans with NULL instruct return empty string."""
        script = export_annotated_script("b1", storage)
        no_instruct = [e for e in script if e["instruct"] == ""]
        assert len(no_instruct) == 2


class TestEmptyBook:
    def test_empty_book_returns_empty_list(self, storage):
        """A book with no spans returns an empty list."""
        script = export_annotated_script("nonexistent", storage)
        assert script == []

    def test_book_with_no_spans_returns_empty(self):
        """A book that exists but has no spans returns empty list."""
        s = InMemorySQLiteAdapter()
        s.init_db()
        s.execute_insert("INSERT INTO series (id) VALUES ('s-empty')")
        s.execute_insert(
            "INSERT INTO book (id, series_id, position) VALUES ('b-empty', 's-empty', 1)"
        )
        script = export_annotated_script("b-empty", s)
        assert script == []


class TestMixedSpeakerAndNarrator:
    def test_full_script_output(self, storage):
        """Full script has correct speaker/text/instruct for all spans."""
        script = export_annotated_script("b1", storage)
        assert len(script) == 4
        assert script[0] == {
            "speaker": "Alice",
            "text": "Hello there!",
            "instruct": "cheerfully",
        }
        assert script[1] == {
            "speaker": "NARRATOR",
            "text": "She walked away.",
            "instruct": "",
        }
        assert script[2] == {
            "speaker": "Bob",
            "text": "Goodbye.",
            "instruct": "sadly",
        }
        assert script[3] == {
            "speaker": "NARRATOR",
            "text": "No one spoke.",
            "instruct": "",
        }
