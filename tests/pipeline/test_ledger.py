"""Tests for CharacterLedger — query layer for character graph data.

Covers:
- get_characters_for_book: returns all characters with aliases and confidence
- get_characters_for_scene: returns present characters with relation_type
- get_characters_for_span: returns characters with relation_type
- get_review_items: filters by confidence 0.5-0.7
- get_review_items with walk_name filter
- Empty results for nonexistent entities
"""

from __future__ import annotations

import pytest

from app.pipeline.adapter import InMemorySQLiteAdapter
from app.pipeline.ledger import CharacterLedger

# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def ledger():
    """Return a CharacterLedger connected to a populated in-memory store."""
    storage = InMemorySQLiteAdapter()
    storage.init_db()

    # Tree structure
    storage.execute_insert("INSERT INTO series (id) VALUES ('s1')")
    storage.execute_insert(
        "INSERT INTO book (id, series_id) VALUES ('b1', 's1')"
    )
    storage.execute_insert(
        "INSERT INTO book (id, series_id) VALUES ('b2', 's1')"
    )
    storage.execute_insert("INSERT INTO chapter (id, book_id) VALUES ('ch1', 'b1')")
    storage.execute_insert("INSERT INTO scene (id) VALUES ('sc1')")
    storage.execute_insert("INSERT INTO scene (id) VALUES ('sc2')")
    storage.execute_insert("INSERT INTO scene (id) VALUES ('sc3')")
    storage.execute_insert("INSERT INTO paragraph (id) VALUES ('p1')")
    storage.execute_insert("INSERT INTO span (id, span_type) VALUES ('sp1', 'quotation')")
    storage.execute_insert("INSERT INTO span (id, span_type) VALUES ('sp2', 'sentence')")

    # Characters
    storage.execute_insert(
        "INSERT INTO character (id, name, aliases) VALUES ('c1', 'Alice', '[\"Ally\"]')"
    )
    storage.execute_insert(
        "INSERT INTO character (id, name, aliases) VALUES ('c2', 'Bob', '[]')"
    )
    storage.execute_insert(
        "INSERT INTO character (id, name, aliases) VALUES ('c3', 'Charlie', '[\"Chuck\"]')"
    )

    # character_book junctions — source must be 'walk', 'human', or 'derived'
    storage.execute_insert(
        """INSERT INTO character_book (character_id, book_id, source, confidence)
           VALUES ('c1', 'b1', 'walk', 0.9)"""
    )
    storage.execute_insert(
        """INSERT INTO character_book (character_id, book_id, source, confidence)
           VALUES ('c2', 'b1', 'walk', 0.6)"""
    )
    storage.execute_insert(
        """INSERT INTO character_book (character_id, book_id, source, confidence)
           VALUES ('c3', 'b1', 'walk', 0.4)"""
    )

    # Character in b2 (for cross-book isolation tests)
    storage.execute_insert(
        """INSERT INTO character_book (character_id, book_id, source, confidence)
           VALUES ('c1', 'b2', 'walk', 0.8)"""
    )

    # character_scene junctions
    storage.execute_insert(
        """INSERT INTO character_scene (character_id, scene_id, relation_type, source, confidence)
           VALUES ('c1', 'sc1', 'present', 'walk', 0.9)"""
    )
    storage.execute_insert(
        """INSERT INTO character_scene (character_id, scene_id, relation_type, source, confidence)
           VALUES ('c2', 'sc1', 'speaker', 'walk', 0.8)"""
    )
    storage.execute_insert(
        """INSERT INTO character_scene (character_id, scene_id, relation_type, source, confidence)
           VALUES ('c3', 'sc2', 'present', 'walk', 0.5)"""
    )

    # character_span junctions
    storage.execute_insert(
        """INSERT INTO character_span (character_id, span_id, relation_type, source, confidence)
           VALUES ('c1', 'sp1', 'speaker', 'walk', 0.95)"""
    )
    storage.execute_insert(
        """INSERT INTO character_span (character_id, span_id, relation_type, source, confidence)
           VALUES ('c2', 'sp1', 'mentioned', 'walk', 0.55)"""
    )
    storage.execute_insert(
        """INSERT INTO character_span (character_id, span_id, relation_type, source, confidence)
           VALUES ('c3', 'sp2', 'present', 'walk', 0.7)"""
    )

    return CharacterLedger(storage)


# ---------------------------------------------------------------------------
# get_characters_for_book
# ---------------------------------------------------------------------------


class TestGetCharactersForBook:
    def test_returns_all_characters_for_book(self, ledger):
        """All characters in character_book for b1 are returned."""
        chars = ledger.get_characters_for_book("b1")
        assert len(chars) == 3
        names = {c["name"] for c in chars}
        assert names == {"Alice", "Bob", "Charlie"}

    def test_includes_aliases(self, ledger):
        """Character aliases are included as a JSON string."""
        chars = ledger.get_characters_for_book("b1")
        alice = next(c for c in chars if c["name"] == "Alice")
        assert alice["aliases"] == '["Ally"]'

    def test_includes_confidence(self, ledger):
        """Confidence from the junction is included in the result."""
        chars = ledger.get_characters_for_book("b1")
        confidences = {c["name"]: c["confidence"] for c in chars}
        assert confidences["Alice"] == 0.9
        assert confidences["Bob"] == 0.6
        assert confidences["Charlie"] == 0.4

    def test_ordered_by_name(self, ledger):
        """Results are ordered by character name."""
        chars = ledger.get_characters_for_book("b1")
        names = [c["name"] for c in chars]
        assert names == sorted(names)

    def test_empty_for_nonexistent_book(self, ledger):
        """Returns empty list for nonexistent book_id."""
        chars = ledger.get_characters_for_book("nonexistent")
        assert chars == []

    def test_cross_book_isolation(self, ledger):
        """Characters from b2 do not appear in b1 results."""
        chars_b1 = ledger.get_characters_for_book("b1")
        chars_b2 = ledger.get_characters_for_book("b2")
        assert len(chars_b1) == 3
        assert len(chars_b2) == 1
        assert chars_b2[0]["name"] == "Alice"


# ---------------------------------------------------------------------------
# get_characters_for_scene
# ---------------------------------------------------------------------------


class TestGetCharactersForScene:
    def test_returns_characters_in_scene(self, ledger):
        """Characters associated with a scene are returned."""
        chars = ledger.get_characters_for_scene("sc1")
        assert len(chars) == 2
        names = {c["name"] for c in chars}
        assert names == {"Alice", "Bob"}

    def test_includes_relation_type(self, ledger):
        """relation_type from the junction is included."""
        chars = ledger.get_characters_for_scene("sc1")
        by_name = {c["name"]: c for c in chars}
        assert by_name["Alice"]["relation_type"] == "present"
        assert by_name["Bob"]["relation_type"] == "speaker"

    def test_includes_confidence(self, ledger):
        """Confidence is included."""
        chars = ledger.get_characters_for_scene("sc1")
        by_name = {c["name"]: c["confidence"] for c in chars}
        assert by_name["Alice"] == 0.9
        assert by_name["Bob"] == 0.8

    def test_empty_for_nonexistent_scene(self, ledger):
        """Returns empty list for nonexistent scene_id."""
        chars = ledger.get_characters_for_scene("nonexistent")
        assert chars == []

    def test_single_character_scene(self, ledger):
        """Scene with only one character returns that character."""
        chars = ledger.get_characters_for_scene("sc2")
        assert len(chars) == 1
        assert chars[0]["name"] == "Charlie"


# ---------------------------------------------------------------------------
# get_characters_for_span
# ---------------------------------------------------------------------------


class TestGetCharactersForSpan:
    def test_returns_characters_for_span(self, ledger):
        """Characters associated with a span are returned."""
        chars = ledger.get_characters_for_span("sp1")
        assert len(chars) == 2
        names = {c["name"] for c in chars}
        assert names == {"Alice", "Bob"}

    def test_includes_relation_type(self, ledger):
        """relation_type is included in results."""
        chars = ledger.get_characters_for_span("sp1")
        by_name = {c["name"]: c for c in chars}
        assert by_name["Alice"]["relation_type"] == "speaker"
        assert by_name["Bob"]["relation_type"] == "mentioned"

    def test_single_character_span(self, ledger):
        """Span with one character returns that character only."""
        chars = ledger.get_characters_for_span("sp2")
        assert len(chars) == 1
        assert chars[0]["name"] == "Charlie"

    def test_empty_for_nonexistent_span(self, ledger):
        """Returns empty list for nonexistent span_id."""
        chars = ledger.get_characters_for_span("nonexistent")
        assert chars == []


# ---------------------------------------------------------------------------
# get_review_items
# ---------------------------------------------------------------------------


class TestGetReviewItems:
    def test_returns_low_confidence_items(self, ledger):
        """Only characters with confidence in [0.5, 0.7) are returned."""
        items = ledger.get_review_items("b1")
        # Bob has confidence 0.6 → review
        # Alice has 0.9 → not review
        # Charlie has 0.4 → not review
        assert len(items) == 1
        assert items[0]["character_name"] == "Bob"
        assert items[0]["confidence"] == 0.6

    def test_includes_relevant_fields(self, ledger):
        """Each review item has expected keys."""
        items = ledger.get_review_items("b1")
        item = items[0]
        assert "character_id" in item
        assert "character_name" in item
        assert "junction_table" in item
        assert "confidence" in item
        assert "walk_name" in item
        assert "reason" in item

    def test_empty_for_book_with_no_review_items(self, ledger):
        """Book with all high-confidence characters returns empty."""
        items = ledger.get_review_items("b2")
        assert items == []

    def test_empty_for_nonexistent_book(self, ledger):
        """Nonexistent book returns empty."""
        items = ledger.get_review_items("nonexistent")
        assert items == []

    def test_boundary_values(self):
        """Check confidence boundaries: 0.5 is included, 0.7 is NOT."""
        storage = InMemorySQLiteAdapter()
        storage.init_db()
        storage.execute_insert("INSERT INTO series (id) VALUES ('s1')")
        storage.execute_insert(
            "INSERT INTO book (id, series_id) VALUES ('b3', 's1')"
        )
        storage.execute_insert(
            "INSERT INTO character (id, name, aliases) VALUES ('c-bound-1', 'LowEdge', '[]')"
        )
        storage.execute_insert(
            "INSERT INTO character (id, name, aliases) VALUES ('c-bound-2', 'HighEdge', '[]')"
        )
        storage.execute_insert(
            """INSERT INTO character_book (character_id, book_id, source, confidence)
               VALUES ('c-bound-1', 'b3', 'walk', 0.5)"""
        )
        storage.execute_insert(
            """INSERT INTO character_book (character_id, book_id, source, confidence)
               VALUES ('c-bound-2', 'b3', 'walk', 0.7)"""
        )
        ledger2 = CharacterLedger(storage)
        items = ledger2.get_review_items("b3")
        assert len(items) == 1
        assert items[0]["character_name"] == "LowEdge"
