"""Tests for reonboard_book and get_book_version — re-onboarding & version.

Covers:
- get_book_version returns correct version (default 1 for new books)
- reonboard_book increments version (1→2, 2→3, etc.)
- reonboard_book returns the new version number
- reonboard_book clears character_span junctions
- reonboard_book clears character_scene junctions
- reonboard_book clears character_book entries
- reonboard_book clears span.instruct
- reonboard_book clears character.voice_assignment_id
- reonboard_book preserves tree structure (book, chapters, paragraphs, spans)
- reonboard_book preserves characters (shared across series)
- reonboard_book on a book with no walk outputs still works
- get_book_version raises ValueError for nonexistent book
"""

from __future__ import annotations

import pytest

from app.pipeline.adapter import InMemorySQLiteAdapter
from app.pipeline.assembly import get_book_version, reonboard_book


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _populate_storage(storage: InMemorySQLiteAdapter) -> None:
    """Insert a complete document spine with walk outputs for testing."""
    # -- Voice config -------------------------------------------------------
    storage.execute_insert(
        "INSERT INTO voice_config (id, name, description) VALUES ('vc1', 'Warm Female', 'A warm voice')"
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
        "INSERT INTO span (id, span_type, text, instruct) VALUES ('sp1', 'quotation', 'Hello!', 'cheerfully')"
    )
    storage.execute_insert(
        "INSERT INTO span (id, span_type, text, instruct) VALUES ('sp2', 'sentence', 'She said.', NULL)"
    )
    storage.execute_insert(
        "INSERT INTO span (id, span_type, text, instruct) VALUES ('sp3', 'quotation', 'Bye.', 'sadly')"
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

    # -- Characters ---------------------------------------------------------
    storage.execute_insert(
        "INSERT INTO character (id, name, aliases, voice_assignment_id) VALUES ('c1', 'Alice', '[]', 'vc1')"
    )
    storage.execute_insert(
        "INSERT INTO character (id, name, aliases, voice_assignment_id) VALUES ('c2', 'Bob', '[]', NULL)"
    )

    # -- character_book (memberships) ---------------------------------------
    storage.execute_insert(
        "INSERT INTO character_book (character_id, book_id, source, confidence) VALUES ('c1', 'b1', 'walk', 0.9)"
    )
    storage.execute_insert(
        "INSERT INTO character_book (character_id, book_id, source, confidence) VALUES ('c2', 'b1', 'walk', 0.8)"
    )

    # -- character_span (speaker junctions) ---------------------------------
    storage.execute_insert(
        "INSERT INTO character_span (character_id, span_id, relation_type, source, confidence) VALUES ('c1', 'sp1', 'speaker', 'walk', 0.95)"
    )
    storage.execute_insert(
        "INSERT INTO character_span (character_id, span_id, relation_type, source, confidence) VALUES ('c2', 'sp3', 'speaker', 'walk', 0.9)"
    )

    # -- character_scene (presence junctions) -------------------------------
    storage.execute_insert(
        "INSERT INTO character_scene (character_id, scene_id, relation_type, source, confidence) VALUES ('c1', 'sc1', 'present', 'walk', 0.85)"
    )
    storage.execute_insert(
        "INSERT INTO character_scene (character_id, scene_id, relation_type, source, confidence) VALUES ('c2', 'sc1', 'speaker', 'walk', 0.9)"
    )

    # -- character_metadata -------------------------------------------------
    storage.execute_insert(
        "INSERT INTO character_metadata (character_id, key, value) VALUES ('c1', 'description', 'A brave heroine')"
    )
    storage.execute_insert(
        "INSERT INTO character_metadata (character_id, key, value) VALUES ('c1', 'voice_profile', 'warm and confident')"
    )
    storage.execute_insert(
        "INSERT INTO character_metadata (character_id, key, value) VALUES ('c2', 'description', 'A mysterious stranger')"
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def storage():
    """Return a populated InMemorySQLiteAdapter."""
    s = InMemorySQLiteAdapter()
    s.init_db()
    _populate_storage(s)
    return s


# ---------------------------------------------------------------------------
# Tests: get_book_version
# ---------------------------------------------------------------------------


class TestGetBookVersion:
    def test_returns_default_version_1(self, storage):
        """New books have version=1 by default."""
        assert get_book_version("b1", storage) == 1

    def test_raises_for_nonexistent_book(self, storage):
        """get_book_version raises ValueError for a book that doesn't exist."""
        with pytest.raises(ValueError, match="not found"):
            get_book_version("nonexistent-book", storage)


# ---------------------------------------------------------------------------
# Tests: reonboard_book — version increment
# ---------------------------------------------------------------------------


class TestReonboardVersionIncrement:
    def test_increments_version_1_to_2(self, storage):
        """First reonboard bumps version from 1 to 2."""
        new_version = reonboard_book("b1", storage)
        assert new_version == 2

    def test_increments_version_2_to_3(self, storage):
        """Second reonboard bumps version from 2 to 3."""
        reonboard_book("b1", storage)  # 1 → 2
        new_version = reonboard_book("b1", storage)  # 2 → 3
        assert new_version == 3

    def test_returns_new_version_number(self, storage):
        """reonboard_book returns the new version (not the old one)."""
        result = reonboard_book("b1", storage)
        assert result == get_book_version("b1", storage)

    def test_version_persists_across_calls(self, storage):
        """Version remains at the new value after reonboard."""
        reonboard_book("b1", storage)
        assert get_book_version("b1", storage) == 2
        reonboard_book("b1", storage)
        assert get_book_version("b1", storage) == 3


# ---------------------------------------------------------------------------
# Tests: reonboard_book — clearing junctions
# ---------------------------------------------------------------------------


class TestReonboardClearsCharacterSpan:
    def test_clears_character_span_junctions(self, storage):
        """character_span rows for the book's spans are deleted."""
        # Before: 2 character_span rows for b1's spans
        before = storage.execute_query(
            """SELECT COUNT(*) AS cnt FROM character_span cs
               JOIN paragraph_span ps ON cs.span_id = ps.child_id
               JOIN scene_paragraph sp ON ps.parent_id = sp.child_id
               JOIN chapter_scene cscene ON sp.parent_id = cscene.child_id
               JOIN book_chapter bce ON cscene.parent_id = bce.child_id
               WHERE bce.parent_id = 'b1'"""
        )
        assert before[0]["cnt"] == 2

        reonboard_book("b1", storage)

        # After: 0 character_span rows (chapter_scene deleted, so query via
        # the span IDs we know belong to b1)
        after = storage.execute_query(
            "SELECT COUNT(*) AS cnt FROM character_span WHERE span_id IN ('sp1', 'sp2', 'sp3')"
        )
        assert after[0]["cnt"] == 0


class TestReonboardClearsCharacterScene:
    def test_clears_character_scene_junctions(self, storage):
        """character_scene rows for the book's scenes are deleted."""
        # Before: 2 character_scene rows for b1's scenes
        before = storage.execute_query(
            """SELECT COUNT(*) AS cnt FROM character_scene
               WHERE scene_id IN ('sc1', 'sc2')"""
        )
        assert before[0]["cnt"] == 2

        reonboard_book("b1", storage)

        after = storage.execute_query(
            "SELECT COUNT(*) AS cnt FROM character_scene WHERE scene_id IN ('sc1', 'sc2')"
        )
        assert after[0]["cnt"] == 0


class TestReonboardClearsCharacterBook:
    def test_clears_character_book_entries(self, storage):
        """character_book rows (memberships) are deleted — NOT carried over."""
        before = storage.execute_query(
            "SELECT COUNT(*) AS cnt FROM character_book WHERE book_id = 'b1'"
        )
        assert before[0]["cnt"] == 2

        reonboard_book("b1", storage)

        after = storage.execute_query(
            "SELECT COUNT(*) AS cnt FROM character_book WHERE book_id = 'b1'"
        )
        assert after[0]["cnt"] == 0

    def test_clears_character_metadata(self, storage):
        """character_metadata for book-linked characters is deleted."""
        before = storage.execute_query(
            """SELECT COUNT(*) AS cnt FROM character_metadata
               WHERE character_id IN ('c1', 'c2')"""
        )
        assert before[0]["cnt"] == 3

        reonboard_book("b1", storage)

        after = storage.execute_query(
            """SELECT COUNT(*) AS cnt FROM character_metadata
               WHERE character_id IN ('c1', 'c2')"""
        )
        assert after[0]["cnt"] == 0


# ---------------------------------------------------------------------------
# Tests: reonboard_book — clearing span/character fields
# ---------------------------------------------------------------------------


class TestReonboardClearsSpanInstruct:
    def test_clears_span_instruct(self, storage):
        """span.instruct is reset to NULL for the book's spans."""
        # Before: sp1 has instruct='cheerfully', sp3 has instruct='sadly'
        before = storage.execute_query(
            "SELECT instruct FROM span WHERE id = 'sp1'"
        )
        assert before[0]["instruct"] == "cheerfully"

        reonboard_book("b1", storage)

        after_sp1 = storage.execute_query(
            "SELECT instruct FROM span WHERE id = 'sp1'"
        )
        assert after_sp1[0]["instruct"] is None

        after_sp3 = storage.execute_query(
            "SELECT instruct FROM span WHERE id = 'sp3'"
        )
        assert after_sp3[0]["instruct"] is None


class TestReonboardClearsVoiceAssignment:
    def test_clears_voice_assignment_id(self, storage):
        """character.voice_assignment_id is reset to NULL for book-linked characters."""
        # Before: c1/Alice has voice_assignment_id = 'vc1'
        before = storage.execute_query(
            "SELECT voice_assignment_id FROM character WHERE id = 'c1'"
        )
        assert before[0]["voice_assignment_id"] == "vc1"

        reonboard_book("b1", storage)

        after = storage.execute_query(
            "SELECT voice_assignment_id FROM character WHERE id = 'c1'"
        )
        assert after[0]["voice_assignment_id"] is None


# ---------------------------------------------------------------------------
# Tests: reonboard_book — preserving tree structure
# ---------------------------------------------------------------------------


class TestReonboardPreservesTreeStructure:
    def test_preserves_book(self, storage):
        """Book row still exists after reonboard."""
        reonboard_book("b1", storage)
        rows = storage.execute_query("SELECT id FROM book WHERE id = 'b1'")
        assert len(rows) == 1

    def test_preserves_chapters(self, storage):
        """Chapter rows still exist after reonboard."""
        reonboard_book("b1", storage)
        rows = storage.execute_query(
            "SELECT id FROM chapter WHERE book_id = 'b1'"
        )
        assert len(rows) == 2

    def test_preserves_book_chapter_edges(self, storage):
        """book_chapter edges still exist after reonboard."""
        reonboard_book("b1", storage)
        rows = storage.execute_query(
            "SELECT child_id FROM book_chapter WHERE parent_id = 'b1'"
        )
        assert len(rows) == 2

    def test_preserves_paragraphs(self, storage):
        """Paragraph rows still exist after reonboard."""
        reonboard_book("b1", storage)
        rows = storage.execute_query("SELECT id FROM paragraph")
        assert len(rows) == 3

    def test_preserves_spans(self, storage):
        """Span rows still exist after reonboard (only instruct is cleared)."""
        reonboard_book("b1", storage)
        rows = storage.execute_query(
            "SELECT id FROM span WHERE id IN ('sp1', 'sp2', 'sp3')"
        )
        assert len(rows) == 3

    def test_paragraphs_disconnected_from_scenes(self, storage):
        """After reonboard, paragraphs exist but scene_paragraph edges are gone.
        
        Scenes are walk-created and deleted during reonboard. The scene_paragraph
        edges must also be deleted (FK constraint), so paragraphs become
        disconnected from the scene layer. This is expected — walks will
        re-create scenes and re-connect paragraphs on the next run.
        """
        reonboard_book("b1", storage)
        # Paragraphs still exist
        para_rows = storage.execute_query("SELECT id FROM paragraph")
        assert len(para_rows) == 3
        # But scene_paragraph edges are gone (scenes were deleted)
        edge_rows = storage.execute_query("SELECT COUNT(*) AS cnt FROM scene_paragraph")
        assert edge_rows[0]["cnt"] == 0

    def test_preserves_paragraph_span_edges(self, storage):
        """paragraph_span edges still exist after reonboard."""
        reonboard_book("b1", storage)
        rows = storage.execute_query(
            "SELECT child_id FROM paragraph_span WHERE child_id IN ('sp1', 'sp2', 'sp3')"
        )
        assert len(rows) == 3


class TestReonboardPreservesCharacters:
    def test_preserves_character_rows(self, storage):
        """Character rows are NOT deleted — they may be shared across books."""
        reonboard_book("b1", storage)
        rows = storage.execute_query("SELECT id FROM character WHERE id IN ('c1', 'c2')")
        assert len(rows) == 2

    def test_preserves_character_names(self, storage):
        """Character names are preserved (not modified by reonboard)."""
        reonboard_book("b1", storage)
        rows = storage.execute_query(
            "SELECT name FROM character WHERE id = 'c1'"
        )
        assert rows[0]["name"] == "Alice"


# ---------------------------------------------------------------------------
# Tests: reonboard_book — edge cases
# ---------------------------------------------------------------------------


class TestReonboardEmptyBook:
    def test_reonboard_book_with_no_walk_outputs(self):
        """A book with no walk outputs still increments version."""
        s = InMemorySQLiteAdapter()
        s.init_db()
        s.execute_insert("INSERT INTO series (id) VALUES ('s-empty')")
        s.execute_insert(
            "INSERT INTO book (id, series_id, position) VALUES ('b-empty', 's-empty', 1)"
        )
        # No chapters, scenes, characters, or walk outputs.

        new_version = reonboard_book("b-empty", s)
        assert new_version == 2
        assert get_book_version("b-empty", s) == 2

    def test_reonboard_book_with_chapters_but_no_scenes(self):
        """A book with chapters but no scenes still works."""
        s = InMemorySQLiteAdapter()
        s.init_db()
        s.execute_insert("INSERT INTO series (id) VALUES ('s1')")
        s.execute_insert(
            "INSERT INTO book (id, series_id, position) VALUES ('b1', 's1', 1)"
        )
        s.execute_insert(
            "INSERT INTO chapter (id, book_id) VALUES ('ch1', 'b1')"
        )
        s.execute_insert(
            "INSERT INTO book_chapter (child_id, parent_id, position) VALUES ('ch1', 'b1', 1)"
        )
        # No scenes, no characters, no walk outputs.

        new_version = reonboard_book("b1", s)
        assert new_version == 2
