"""Tests for ReviewManager — confidence review queue.

Covers:
- get_review_items returns only confidence [0.5, 0.7) items
- get_review_items excludes confidence ≥0.7 and <0.5 items
- get_review_items with walk_name filter uses LIKE heuristic
- accept_review_item sets confidence to 1.0
- reject_review_item sets confidence to 0.0
- override_review_item sets human_override=1 and updates values
- get_review_items across multiple junction types (book, scene, span)
- Empty book returns empty list
"""

from __future__ import annotations

import pytest

from app.pipeline.adapter import InMemorySQLiteAdapter
from app.pipeline.review import ReviewManager


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _populate_storage(storage: InMemorySQLiteAdapter) -> None:
    """Insert a minimal but complete document spine with characters and
    junction records at various confidence levels."""
    # -- Series + Book ------------------------------------------------------
    storage.execute_insert("INSERT INTO series (id) VALUES ('s1')")
    storage.execute_insert(
        "INSERT INTO book (id, series_id, position) VALUES ('b1', 's1', 1)"
    )
    storage.execute_insert(
        "INSERT INTO book (id, series_id, position) VALUES ('b2', 's1', 2)"
    )

    # -- Chapters -----------------------------------------------------------
    storage.execute_insert(
        "INSERT INTO chapter (id, book_id) VALUES ('ch1', 'b1')"
    )
    storage.execute_insert(
        "INSERT INTO book_chapter (child_id, parent_id, position) VALUES ('ch1', 'b1', 1)"
    )

    # -- Scenes -------------------------------------------------------------
    storage.execute_insert("INSERT INTO scene (id) VALUES ('sc1')")
    storage.execute_insert("INSERT INTO scene (id) VALUES ('sc2')")
    storage.execute_insert(
        "INSERT INTO chapter_scene (child_id, parent_id, position) VALUES ('sc1', 'ch1', 1)"
    )
    storage.execute_insert(
        "INSERT INTO chapter_scene (child_id, parent_id, position) VALUES ('sc2', 'ch1', 2)"
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
        "INSERT INTO span (id, span_type, text) VALUES ('sp1', 'quotation', 'Hello!')"
    )
    storage.execute_insert(
        "INSERT INTO span (id, span_type, text) VALUES ('sp2', 'sentence', 'She said.')"
    )
    storage.execute_insert(
        "INSERT INTO span (id, span_type, text) VALUES ('sp3', 'quotation', 'Goodbye.')"
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
        "INSERT INTO character (id, name, aliases) VALUES ('c1', 'Alice', '[]')"
    )
    storage.execute_insert(
        "INSERT INTO character (id, name, aliases) VALUES ('c2', 'Bob', '[]')"
    )
    storage.execute_insert(
        "INSERT INTO character (id, name, aliases) VALUES ('c3', 'Charlie', '[]')"
    )
    storage.execute_insert(
        "INSERT INTO character (id, name, aliases) VALUES ('c4', 'Diana', '[]')"
    )
    storage.execute_insert(
        "INSERT INTO character (id, name, aliases) VALUES ('c5', 'Eve', '[]')"
    )


@pytest.fixture
def manager():
    """Return a ReviewManager connected to a populated in-memory store."""
    storage = InMemorySQLiteAdapter()
    storage.init_db()
    _populate_storage(storage)

    # -- character_book junctions -------------------------------------------
    # c1/Alice: confidence 0.9 (high — not review)
    storage.execute_insert(
        """INSERT INTO character_book (character_id, book_id, source, confidence)
           VALUES ('c1', 'b1', 'walk', 0.9)"""
    )
    # c2/Bob: confidence 0.6 (review)
    storage.execute_insert(
        """INSERT INTO character_book (character_id, book_id, source, confidence)
           VALUES ('c2', 'b1', 'walk', 0.6)"""
    )
    # c3/Charlie: confidence 0.4 (low — not review)
    storage.execute_insert(
        """INSERT INTO character_book (character_id, book_id, source, confidence)
           VALUES ('c3', 'b1', 'walk', 0.4)"""
    )

    # -- character_scene junctions ------------------------------------------
    # c1/Alice in sc1: confidence 0.55 (review)
    storage.execute_insert(
        """INSERT INTO character_scene
           (character_id, scene_id, relation_type, source, confidence)
           VALUES ('c1', 'sc1', 'present', 'walk', 0.55)"""
    )
    # c2/Bob in sc1: confidence 0.8 (high — not review)
    storage.execute_insert(
        """INSERT INTO character_scene
           (character_id, scene_id, relation_type, source, confidence)
           VALUES ('c2', 'sc1', 'speaker', 'walk', 0.8)"""
    )
    # c3/Charlie in sc2: confidence 0.5 (boundary — review, inclusive)
    storage.execute_insert(
        """INSERT INTO character_scene
           (character_id, scene_id, relation_type, source, confidence)
           VALUES ('c3', 'sc2', 'present', 'walk', 0.5)"""
    )

    # -- character_span junctions -------------------------------------------
    # c1/Alice speaker of sp1: confidence 0.65 (review)
    storage.execute_insert(
        """INSERT INTO character_span
           (character_id, span_id, relation_type, source, confidence)
           VALUES ('c1', 'sp1', 'speaker', 'walk', 0.65)"""
    )
    # c2/Bob mentioned in sp2: confidence 0.7 (boundary — NOT review, exclusive)
    storage.execute_insert(
        """INSERT INTO character_span
           (character_id, span_id, relation_type, source, confidence)
           VALUES ('c2', 'sp2', 'mentioned', 'walk', 0.7)"""
    )
    # c4/Diana speaker of sp3: confidence 0.52 (review)
    storage.execute_insert(
        """INSERT INTO character_span
           (character_id, span_id, relation_type, source, confidence)
           VALUES ('c4', 'sp3', 'speaker', 'walk', 0.52)"""
    )

    return ReviewManager(storage)


@pytest.fixture
def empty_manager():
    """Return a ReviewManager with a book that has no junction records."""
    storage = InMemorySQLiteAdapter()
    storage.init_db()
    storage.execute_insert("INSERT INTO series (id) VALUES ('s-empty')")
    storage.execute_insert(
        "INSERT INTO book (id, series_id) VALUES ('b-empty', 's-empty')"
    )
    return ReviewManager(storage)


# ---------------------------------------------------------------------------
# get_review_items — confidence filtering
# ---------------------------------------------------------------------------


class TestGetReviewItemsConfidenceFilter:
    def test_returns_only_review_band_items(self, manager):
        """Only items with confidence in [0.5, 0.7) are returned."""
        items = manager.get_review_items("b1")
        confidences = [item["confidence"] for item in items]
        for conf in confidences:
            assert conf >= 0.5
            assert conf < 0.7

    def test_excludes_high_confidence(self, manager):
        """Items with confidence ≥0.7 are excluded."""
        items = manager.get_review_items("b1")
        # c1/Alice has character_book confidence 0.9 — should not appear
        # as a character_book review item. (Alice appears in other junctions
        # at review-band confidence, so we check junction_table specifically.)
        book_items = [
            i for i in items if i["junction_table"] == "character_book"
        ]
        book_names = {i["character_name"] for i in book_items}
        assert "Alice" not in book_names  # 0.9 — too high

    def test_excludes_low_confidence(self, manager):
        """Items with confidence <0.5 are excluded."""
        items = manager.get_review_items("b1")
        book_items = [
            i for i in items if i["junction_table"] == "character_book"
        ]
        book_names = {i["character_name"] for i in book_items}
        assert "Charlie" not in book_names  # 0.4 — too low

    def test_boundary_0_5_included(self, manager):
        """Confidence exactly 0.5 is included in review items."""
        items = manager.get_review_items("b1")
        scene_items = [
            i for i in items if i["junction_table"] == "character_scene"
        ]
        charlie_scene = [
            i for i in scene_items if i["character_name"] == "Charlie"
        ]
        assert len(charlie_scene) == 1
        assert charlie_scene[0]["confidence"] == 0.5

    def test_boundary_0_7_excluded(self, manager):
        """Confidence exactly 0.7 is excluded from review items."""
        items = manager.get_review_items("b1")
        span_items = [
            i for i in items if i["junction_table"] == "character_span"
        ]
        bob_span = [
            i for i in span_items if i["character_name"] == "Bob"
        ]
        # Bob's character_span has confidence 0.7 — should NOT be in review
        assert len(bob_span) == 0


# ---------------------------------------------------------------------------
# get_review_items — walk_name filter
# ---------------------------------------------------------------------------


class TestGetReviewItemsWalkFilter:
    def test_walk_name_filter_uses_like(self):
        """walk_name filter uses source LIKE %walk_name% heuristic.

        NOTE: The source column has a CHECK constraint limiting values to
        'walk', 'human', 'derived'. The LIKE heuristic matches substrings
        of these values.
        """
        storage = InMemorySQLiteAdapter()
        storage.init_db()
        storage.execute_insert("INSERT INTO series (id) VALUES ('s1')")
        storage.execute_insert(
            "INSERT INTO book (id, series_id) VALUES ('b1', 's1')"
        )
        storage.execute_insert(
            "INSERT INTO character (id, name, aliases) VALUES ('c1', 'Alice', '[]')"
        )
        storage.execute_insert(
            "INSERT INTO character (id, name, aliases) VALUES ('c2', 'Bob', '[]')"
        )
        # Two characters with different source values
        storage.execute_insert(
            """INSERT INTO character_book (character_id, book_id, source, confidence)
               VALUES ('c1', 'b1', 'walk', 0.6)"""
        )
        storage.execute_insert(
            """INSERT INTO character_book (character_id, book_id, source, confidence)
               VALUES ('c2', 'b1', 'human', 0.55)"""
        )
        mgr = ReviewManager(storage)

        # Filter for 'walk' — should only get Alice
        items = mgr.get_review_items("b1", walk_name="walk")
        assert len(items) == 1
        assert items[0]["character_name"] == "Alice"

        # Filter for 'human' — should only get Bob
        items = mgr.get_review_items("b1", walk_name="human")
        assert len(items) == 1
        assert items[0]["character_name"] == "Bob"

        # No filter — should get both
        items = mgr.get_review_items("b1")
        assert len(items) == 2

    def test_walk_name_filter_across_junction_types(self):
        """walk_name filter applies to scene and span junctions too."""
        storage = InMemorySQLiteAdapter()
        storage.init_db()
        storage.execute_insert("INSERT INTO series (id) VALUES ('s1')")
        storage.execute_insert(
            "INSERT INTO book (id, series_id) VALUES ('b1', 's1')"
        )
        storage.execute_insert(
            "INSERT INTO chapter (id, book_id) VALUES ('ch1', 'b1')"
        )
        storage.execute_insert(
            "INSERT INTO book_chapter (child_id, parent_id, position) VALUES ('ch1', 'b1', 1)"
        )
        storage.execute_insert("INSERT INTO scene (id) VALUES ('sc1')")
        storage.execute_insert(
            "INSERT INTO chapter_scene (child_id, parent_id, position) VALUES ('sc1', 'ch1', 1)"
        )
        storage.execute_insert("INSERT INTO paragraph (id) VALUES ('p1')")
        storage.execute_insert(
            "INSERT INTO scene_paragraph (child_id, parent_id, position) VALUES ('p1', 'sc1', 1)"
        )
        storage.execute_insert(
            "INSERT INTO span (id, span_type) VALUES ('sp1', 'quotation')"
        )
        storage.execute_insert(
            "INSERT INTO paragraph_span (child_id, parent_id, position) VALUES ('sp1', 'p1', 1)"
        )
        storage.execute_insert(
            "INSERT INTO character (id, name, aliases) VALUES ('c1', 'Alice', '[]')"
        )
        storage.execute_insert(
            "INSERT INTO character (id, name, aliases) VALUES ('c2', 'Bob', '[]')"
        )
        # Scene junction with 'walk' source
        storage.execute_insert(
            """INSERT INTO character_scene
               (character_id, scene_id, relation_type, source, confidence)
               VALUES ('c1', 'sc1', 'present', 'walk', 0.6)"""
        )
        # Span junction with 'human' source
        storage.execute_insert(
            """INSERT INTO character_span
               (character_id, span_id, relation_type, source, confidence)
               VALUES ('c2', 'sp1', 'speaker', 'human', 0.55)"""
        )
        mgr = ReviewManager(storage)

        # Filter for 'walk' — only scene item
        items = mgr.get_review_items("b1", walk_name="walk")
        assert len(items) == 1
        assert items[0]["junction_table"] == "character_scene"
        assert items[0]["character_name"] == "Alice"

        # Filter for 'human' — only span item
        items = mgr.get_review_items("b1", walk_name="human")
        assert len(items) == 1
        assert items[0]["junction_table"] == "character_span"
        assert items[0]["character_name"] == "Bob"


# ---------------------------------------------------------------------------
# accept_review_item
# ---------------------------------------------------------------------------


class TestAcceptReviewItem:
    def test_sets_confidence_to_1_0(self, manager):
        """Accepting a review item sets its confidence to 1.0."""
        items = manager.get_review_items("b1")
        # Find a character_book review item (Bob, confidence 0.6)
        book_item = next(
            i for i in items if i["junction_table"] == "character_book"
        )
        assert book_item["confidence"] == 0.6

        manager.accept_review_item(book_item["item_id"])

        # Verify confidence is now 1.0
        rows = manager._storage.execute_query(
            """SELECT confidence FROM character_book
               WHERE character_id = ? AND book_id = ?""",
            (book_item["character_id"], "b1"),
        )
        assert rows[0]["confidence"] == 1.0

    def test_accept_scene_item(self, manager):
        """Accepting a character_scene item sets confidence to 1.0."""
        items = manager.get_review_items("b1")
        scene_item = next(
            i for i in items if i["junction_table"] == "character_scene"
            and i["character_name"] == "Alice"
        )
        manager.accept_review_item(scene_item["item_id"])

        rows = manager._storage.execute_query(
            """SELECT confidence FROM character_scene
               WHERE character_id = ? AND scene_id = ?""",
            ("c1", "sc1"),
        )
        assert rows[0]["confidence"] == 1.0

    def test_accept_span_item(self, manager):
        """Accepting a character_span item sets confidence to 1.0."""
        items = manager.get_review_items("b1")
        span_item = next(
            i for i in items if i["junction_table"] == "character_span"
            and i["character_name"] == "Alice"
        )
        manager.accept_review_item(span_item["item_id"])

        rows = manager._storage.execute_query(
            """SELECT confidence FROM character_span
               WHERE character_id = ? AND span_id = ?""",
            ("c1", "sp1"),
        )
        assert rows[0]["confidence"] == 1.0

    def test_accepted_item_no_longer_in_review(self, manager):
        """After accepting, the item no longer appears in review queue."""
        items = manager.get_review_items("b1")
        book_item = next(
            i for i in items if i["junction_table"] == "character_book"
        )
        item_id = book_item["item_id"]

        manager.accept_review_item(item_id)

        # Re-fetch — item should be gone (confidence now 1.0)
        new_items = manager.get_review_items("b1")
        book_items = [
            i for i in new_items if i["junction_table"] == "character_book"
        ]
        assert all(i["item_id"] != item_id for i in book_items)


# ---------------------------------------------------------------------------
# reject_review_item
# ---------------------------------------------------------------------------


class TestRejectReviewItem:
    def test_sets_confidence_to_0_0(self, manager):
        """Rejecting a review item sets its confidence to 0.0."""
        items = manager.get_review_items("b1")
        book_item = next(
            i for i in items if i["junction_table"] == "character_book"
        )
        manager.reject_review_item(book_item["item_id"])

        rows = manager._storage.execute_query(
            """SELECT confidence FROM character_book
               WHERE character_id = ? AND book_id = ?""",
            (book_item["character_id"], "b1"),
        )
        assert rows[0]["confidence"] == 0.0

    def test_sets_human_override(self, manager):
        """Rejecting sets human_override = 1."""
        items = manager.get_review_items("b1")
        book_item = next(
            i for i in items if i["junction_table"] == "character_book"
        )
        manager.reject_review_item(book_item["item_id"])

        rows = manager._storage.execute_query(
            """SELECT human_override FROM character_book
               WHERE character_id = ? AND book_id = ?""",
            (book_item["character_id"], "b1"),
        )
        assert rows[0]["human_override"] == 1

    def test_rejected_item_no_longer_in_review(self, manager):
        """After rejecting, the item no longer appears in review queue."""
        items = manager.get_review_items("b1")
        book_item = next(
            i for i in items if i["junction_table"] == "character_book"
        )
        item_id = book_item["item_id"]

        manager.reject_review_item(item_id)

        new_items = manager.get_review_items("b1")
        book_items = [
            i for i in new_items if i["junction_table"] == "character_book"
        ]
        assert all(i["item_id"] != item_id for i in book_items)


# ---------------------------------------------------------------------------
# override_review_item
# ---------------------------------------------------------------------------


class TestOverrideReviewItem:
    def test_sets_human_override_and_confidence(self, manager):
        """Overriding sets human_override=1 and confidence=1.0."""
        items = manager.get_review_items("b1")
        scene_item = next(
            i for i in items if i["junction_table"] == "character_scene"
            and i["character_name"] == "Alice"
        )
        manager.override_review_item(scene_item["item_id"], {})

        rows = manager._storage.execute_query(
            """SELECT confidence, human_override FROM character_scene
               WHERE character_id = ? AND scene_id = ?""",
            ("c1", "sc1"),
        )
        assert rows[0]["confidence"] == 1.0
        assert rows[0]["human_override"] == 1

    def test_updates_relation_type(self, manager):
        """Override with dict can update relation_type on character_scene."""
        items = manager.get_review_items("b1")
        scene_item = next(
            i for i in items if i["junction_table"] == "character_scene"
            and i["character_name"] == "Alice"
        )
        # Alice was 'present', override to 'speaker'
        manager.override_review_item(
            scene_item["item_id"], {"relation_type": "speaker"}
        )

        rows = manager._storage.execute_query(
            """SELECT relation_type, confidence, human_override
               FROM character_scene
               WHERE character_id = ? AND scene_id = ?""",
            ("c1", "sc1"),
        )
        assert rows[0]["relation_type"] == "speaker"
        assert rows[0]["confidence"] == 1.0
        assert rows[0]["human_override"] == 1

    def test_updates_relation_type_on_span(self, manager):
        """Override can update relation_type on character_span."""
        items = manager.get_review_items("b1")
        span_item = next(
            i for i in items if i["junction_table"] == "character_span"
            and i["character_name"] == "Alice"
        )
        # Alice was 'speaker', override to 'mentioned'
        manager.override_review_item(
            span_item["item_id"], {"relation_type": "mentioned"}
        )

        rows = manager._storage.execute_query(
            """SELECT relation_type, confidence, human_override
               FROM character_span
               WHERE character_id = ? AND span_id = ?""",
            ("c1", "sp1"),
        )
        assert rows[0]["relation_type"] == "mentioned"
        assert rows[0]["confidence"] == 1.0
        assert rows[0]["human_override"] == 1

    def test_ignores_unknown_columns(self, manager):
        """Override with unknown column names silently ignores them."""
        items = manager.get_review_items("b1")
        book_item = next(
            i for i in items if i["junction_table"] == "character_book"
        )
        # Should not raise — unknown column 'nonexistent' is ignored
        manager.override_review_item(
            book_item["item_id"], {"nonexistent": "value"}
        )

        # Confidence and human_override still updated
        rows = manager._storage.execute_query(
            """SELECT confidence, human_override FROM character_book
               WHERE character_id = ? AND book_id = ?""",
            (book_item["character_id"], "b1"),
        )
        assert rows[0]["confidence"] == 1.0
        assert rows[0]["human_override"] == 1

    def test_non_dict_new_value(self, manager):
        """Override with non-dict new_value just sets override flags."""
        items = manager.get_review_items("b1")
        book_item = next(
            i for i in items if i["junction_table"] == "character_book"
        )
        # Non-dict value — should set flags but not update columns
        manager.override_review_item(book_item["item_id"], "some_string")

        rows = manager._storage.execute_query(
            """SELECT confidence, human_override FROM character_book
               WHERE character_id = ? AND book_id = ?""",
            (book_item["character_id"], "b1"),
        )
        assert rows[0]["confidence"] == 1.0
        assert rows[0]["human_override"] == 1


# ---------------------------------------------------------------------------
# Multiple junction types
# ---------------------------------------------------------------------------


class TestGetReviewItemsMultipleJunctionTypes:
    def test_returns_items_from_all_junction_types(self, manager):
        """Review items come from character_book, character_scene, and character_span."""
        items = manager.get_review_items("b1")
        junction_tables = {item["junction_table"] for item in items}
        assert "character_book" in junction_tables
        assert "character_scene" in junction_tables
        assert "character_span" in junction_tables

    def test_each_item_has_item_id(self, manager):
        """Every review item has an item_id field."""
        items = manager.get_review_items("b1")
        for item in items:
            assert "item_id" in item
            # item_id should be parseable
            parts = item["item_id"].split(":")
            assert len(parts) == 3
            assert parts[0] == item["junction_table"]

    def test_each_item_has_required_fields(self, manager):
        """Every review item has all required fields."""
        items = manager.get_review_items("b1")
        required_keys = {
            "item_id",
            "character_id",
            "character_name",
            "junction_table",
            "confidence",
            "walk_name",
            "related_entity_id",
            "reason",
        }
        for item in items:
            assert required_keys.issubset(set(item.keys())), (
                f"Missing keys: {required_keys - set(item.keys())}"
            )

    def test_correct_review_items_per_type(self, manager):
        """Verify the expected review items per junction type."""
        items = manager.get_review_items("b1")

        book_items = [i for i in items if i["junction_table"] == "character_book"]
        scene_items = [i for i in items if i["junction_table"] == "character_scene"]
        span_items = [i for i in items if i["junction_table"] == "character_span"]

        # character_book: Bob (0.6)
        assert len(book_items) == 1
        assert book_items[0]["character_name"] == "Bob"

        # character_scene: Alice (0.55), Charlie (0.5)
        assert len(scene_items) == 2
        scene_names = {i["character_name"] for i in scene_items}
        assert scene_names == {"Alice", "Charlie"}

        # character_span: Alice (0.65), Diana (0.52)
        assert len(span_items) == 2
        span_names = {i["character_name"] for i in span_items}
        assert span_names == {"Alice", "Diana"}


# ---------------------------------------------------------------------------
# Empty book
# ---------------------------------------------------------------------------


class TestEmptyBook:
    def test_empty_book_returns_empty_list(self, empty_manager):
        """A book with no junction records returns an empty review list."""
        items = empty_manager.get_review_items("b-empty")
        assert items == []

    def test_nonexistent_book_returns_empty_list(self, manager):
        """A nonexistent book_id returns an empty review list."""
        items = manager.get_review_items("nonexistent-book")
        assert items == []


# ---------------------------------------------------------------------------
# Item ID parsing
# ---------------------------------------------------------------------------


class TestItemIdParsing:
    def test_invalid_format_raises(self, manager):
        """Invalid item_id format raises ValueError."""
        with pytest.raises(ValueError, match="Invalid item_id format"):
            manager.accept_review_item("invalid-format")

    def test_unknown_table_raises(self, manager):
        """Unknown junction table raises ValueError."""
        with pytest.raises(ValueError, match="Unknown junction table"):
            manager.accept_review_item("unknown_table:c1:b1")

    def test_too_many_parts_raises(self, manager):
        """Item ID with too many parts raises ValueError."""
        with pytest.raises(ValueError, match="Invalid item_id format"):
            manager.reject_review_item("character_book:c1:b1:extra")
