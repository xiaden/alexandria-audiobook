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

import sqlite3

import pytest

import app.pipeline.review as review_module
from app.pipeline.adapter import ConcurrentTransactionError, InMemorySQLiteAdapter
from app.pipeline.review import ReviewManager, supersede_targets

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
    storage.execute_insert("INSERT INTO chapter (id, book_id) VALUES ('ch1', 'b1')")
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


def _populate_junction_review_items(storage: InMemorySQLiteAdapter) -> None:
    """Insert the review-band junction records used by the queue tests.

    Extracted from the ``manager`` fixture so the union fixture can reuse the
    exact same junction data.  Produces 5 review-band items (book/scene/span)
    plus out-of-band rows that must NOT surface.
    """
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


def _insert_walk_review_rows(storage: InMemorySQLiteAdapter) -> None:
    """Insert walk_run + walk_review_item rows for the union queue tests.

    Pending rows (w1/w2/w3, one per kind) MUST surface; resolved/superseded/
    stale rows (w4/w5/w6) MUST NOT; w7 belongs to another book and MUST NOT
    surface.  walk_run rows model the honest run_id → walk_name link that
    run_walk() sets (runner.py), used by the walk_name filter.
    """
    for run_id, book_id, walk_name in (
        ("run-1", "b1", "walk_2g_voice_audition"),
        ("run-2", "b1", "walk_2h_voice_assignment"),
        ("run-3", "b1", "walk_2i_delivery"),
        ("run-9", "b2", "walk_2g_voice_audition"),
    ):
        storage.execute_insert(
            "INSERT INTO walk_run (run_id, book_id, walk_name, status, created_ms) "
            "VALUES (?, ?, ?, 'completed', 1)",
            (run_id, book_id, walk_name),
        )

    def _item(
        item_id,
        book_id,
        run_id,
        kind,
        target_table,
        target_id,
        prior_value,
        status,
        created_ms,
    ):
        storage.execute_insert(
            "INSERT INTO walk_review_item "
            "(id, book_id, run_id, kind, target_table, target_id, prior_value, status, created_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item_id,
                book_id,
                run_id,
                kind,
                target_table,
                target_id,
                prior_value,
                status,
                created_ms,
            ),
        )

    # pending — must appear in the union
    _item(
        "w1",
        "b1",
        "run-1",
        "voice_profile",
        "character_metadata",
        "c1",
        '{"voice":"old"}',
        "pending",
        1000,
    )
    _item(
        "w2",
        "b1",
        "run-2",
        "voice_assignment",
        "character",
        "c2",
        None,
        "pending",
        2000,
    )
    _item("w3", "b1", "run-3", "instruction", "span", "sp1", "slow", "pending", 3000)
    # non-pending — must NOT appear
    _item(
        "w4",
        "b1",
        "run-1",
        "voice_profile",
        "character_metadata",
        "c1",
        '{"voice":"old"}',
        "resolved",
        4000,
    )
    _item(
        "w5",
        "b1",
        "run-2",
        "voice_assignment",
        "character",
        "c2",
        None,
        "superseded",
        5000,
    )
    _item("w6", "b1", "run-3", "instruction", "span", "sp1", "slow", "stale", 6000)
    # other book — must NOT appear
    _item(
        "w7",
        "b2",
        "run-9",
        "voice_assignment",
        "character",
        "c2",
        None,
        "pending",
        7000,
    )


@pytest.fixture
def manager():
    """Return a ReviewManager connected to a populated in-memory store."""
    storage = InMemorySQLiteAdapter()
    storage.init_db()
    _populate_storage(storage)
    _populate_junction_review_items(storage)
    return ReviewManager(storage)


@pytest.fixture
def union_manager():
    """Return a ReviewManager over junction items PLUS walk_review_item rows."""
    storage = InMemorySQLiteAdapter()
    storage.init_db()
    _populate_storage(storage)
    _populate_junction_review_items(storage)
    _insert_walk_review_rows(storage)
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
# supersede_targets() — completion-time per-target supersede
# ---------------------------------------------------------------------------


class TestSupersedeTargets:
    """supersede_targets() marks prior pending items of the same kind as
    superseded when a walk run regenerates their targets (contract rule #9)."""

    def _make_storage(self):
        storage = InMemorySQLiteAdapter()
        storage.init_db()
        return storage

    def _insert_item(
        self,
        storage,
        item_id,
        book_id,
        run_id,
        kind,
        target_id,
        status="pending",
    ):
        """Insert a walk_review_item row directly."""
        storage.execute_insert(
            "INSERT INTO walk_review_item "
            "(id, book_id, run_id, kind, target_table, target_id, prior_value, status, created_ms) "
            "VALUES (?, ?, ?, ?, 'character', ?, NULL, ?, 1)",
            (item_id, book_id, run_id, kind, target_id, status),
        )

    def test_empty_target_ids_is_noop(self):
        """Empty committed set → no-op: nothing is changed."""
        storage = self._make_storage()
        self._insert_item(storage, "i1", "b1", "run-1", "voice_profile", "c1")

        result = supersede_targets(
            storage, book_id="b1", run_id="run-2", kind="voice_profile", target_ids=[]
        )

        assert result == 0
        rows = storage.execute_query(
            "SELECT status FROM walk_review_item WHERE id = 'i1'"
        )
        assert rows[0]["status"] == "pending"

    def test_supersedes_matching_pending_items(self):
        """Committing targets supersedes prior pending same-kind items for them."""
        storage = self._make_storage()
        self._insert_item(storage, "i1", "b1", "run-1", "voice_profile", "c1")
        self._insert_item(storage, "i2", "b1", "run-1", "voice_profile", "c2")
        self._insert_item(storage, "i3", "b1", "run-1", "voice_profile", "c3")

        result = supersede_targets(
            storage,
            book_id="b1",
            run_id="run-2",
            kind="voice_profile",
            target_ids=["c1", "c2"],
        )

        assert result == 2
        statuses = {
            row["id"]: row["status"]
            for row in storage.execute_query("SELECT id, status FROM walk_review_item")
        }
        assert statuses["i1"] == "superseded"
        assert statuses["i2"] == "superseded"
        assert statuses["i3"] == "pending"  # target not regenerated → untouched

    def test_excludes_same_run_items(self):
        """Rows belonging to the current run are never superseded."""
        storage = self._make_storage()
        self._insert_item(storage, "i1", "b1", "run-1", "voice_profile", "c1")
        self._insert_item(storage, "i2", "b1", "run-2", "voice_profile", "c1")

        result = supersede_targets(
            storage,
            book_id="b1",
            run_id="run-2",
            kind="voice_profile",
            target_ids=["c1"],
        )

        assert result == 1
        statuses = {
            row["id"]: row["status"]
            for row in storage.execute_query("SELECT id, status FROM walk_review_item")
        }
        assert statuses["i1"] == "superseded"
        assert statuses["i2"] == "pending"

    def test_excludes_other_kinds(self):
        """Supersede is kind-scoped — items of other kinds are untouched."""
        storage = self._make_storage()
        self._insert_item(storage, "i1", "b1", "run-1", "voice_profile", "c1")
        self._insert_item(storage, "i2", "b1", "run-1", "voice_assignment", "c1")

        result = supersede_targets(
            storage,
            book_id="b1",
            run_id="run-2",
            kind="voice_profile",
            target_ids=["c1"],
        )

        assert result == 1
        statuses = {
            row["id"]: row["status"]
            for row in storage.execute_query("SELECT id, status FROM walk_review_item")
        }
        assert statuses["i1"] == "superseded"
        assert statuses["i2"] == "pending"

    def test_excludes_resolved_and_superseded_rows(self):
        """Only pending rows are touched; resolved/superseded/stale stay as-is."""
        storage = self._make_storage()
        self._insert_item(
            storage, "i1", "b1", "run-1", "voice_profile", "c1", status="resolved"
        )
        self._insert_item(
            storage, "i2", "b1", "run-1", "voice_profile", "c1", status="superseded"
        )
        self._insert_item(
            storage, "i3", "b1", "run-1", "voice_profile", "c1", status="stale"
        )

        result = supersede_targets(
            storage,
            book_id="b1",
            run_id="run-2",
            kind="voice_profile",
            target_ids=["c1"],
        )

        assert result == 0
        statuses = {
            row["id"]: row["status"]
            for row in storage.execute_query("SELECT id, status FROM walk_review_item")
        }
        assert statuses == {"i1": "resolved", "i2": "superseded", "i3": "stale"}

    def test_excludes_other_books(self):
        """Supersede is scoped to the walk's book."""
        storage = self._make_storage()
        self._insert_item(storage, "i1", "b1", "run-1", "voice_profile", "c1")
        self._insert_item(storage, "i2", "b2", "run-1", "voice_profile", "c1")

        result = supersede_targets(
            storage,
            book_id="b1",
            run_id="run-2",
            kind="voice_profile",
            target_ids=["c1"],
        )

        assert result == 1
        statuses = {
            row["id"]: row["status"]
            for row in storage.execute_query("SELECT id, status FROM walk_review_item")
        }
        assert statuses["i1"] == "superseded"
        assert statuses["i2"] == "pending"


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
        book_items = [i for i in items if i["junction_table"] == "character_book"]
        book_names = {i["character_name"] for i in book_items}
        assert "Alice" not in book_names  # 0.9 — too high

    def test_excludes_low_confidence(self, manager):
        """Items with confidence <0.5 are excluded."""
        items = manager.get_review_items("b1")
        book_items = [i for i in items if i["junction_table"] == "character_book"]
        book_names = {i["character_name"] for i in book_items}
        assert "Charlie" not in book_names  # 0.4 — too low

    def test_boundary_0_5_included(self, manager):
        """Confidence exactly 0.5 is included in review items."""
        items = manager.get_review_items("b1")
        scene_items = [i for i in items if i["junction_table"] == "character_scene"]
        charlie_scene = [i for i in scene_items if i["character_name"] == "Charlie"]
        assert len(charlie_scene) == 1
        assert charlie_scene[0]["confidence"] == 0.5

    def test_boundary_0_7_excluded(self, manager):
        """Confidence exactly 0.7 is excluded from review items."""
        items = manager.get_review_items("b1")
        span_items = [i for i in items if i["junction_table"] == "character_span"]
        bob_span = [i for i in span_items if i["character_name"] == "Bob"]
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
        storage.execute_insert("INSERT INTO book (id, series_id) VALUES ('b1', 's1')")
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
        storage.execute_insert("INSERT INTO book (id, series_id) VALUES ('b1', 's1')")
        storage.execute_insert("INSERT INTO chapter (id, book_id) VALUES ('ch1', 'b1')")
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
        book_item = next(i for i in items if i["junction_table"] == "character_book")
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
            i
            for i in items
            if i["junction_table"] == "character_scene"
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
            i
            for i in items
            if i["junction_table"] == "character_span"
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
        book_item = next(i for i in items if i["junction_table"] == "character_book")
        item_id = book_item["item_id"]

        manager.accept_review_item(item_id)

        # Re-fetch — item should be gone (confidence now 1.0)
        new_items = manager.get_review_items("b1")
        book_items = [i for i in new_items if i["junction_table"] == "character_book"]
        assert all(i["item_id"] != item_id for i in book_items)


# ---------------------------------------------------------------------------
# reject_review_item
# ---------------------------------------------------------------------------


class TestRejectReviewItem:
    def test_sets_confidence_to_0_0(self, manager):
        """Rejecting a review item sets its confidence to 0.0."""
        items = manager.get_review_items("b1")
        book_item = next(i for i in items if i["junction_table"] == "character_book")
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
        book_item = next(i for i in items if i["junction_table"] == "character_book")
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
        book_item = next(i for i in items if i["junction_table"] == "character_book")
        item_id = book_item["item_id"]

        manager.reject_review_item(item_id)

        new_items = manager.get_review_items("b1")
        book_items = [i for i in new_items if i["junction_table"] == "character_book"]
        assert all(i["item_id"] != item_id for i in book_items)


# ---------------------------------------------------------------------------
# override_review_item
# ---------------------------------------------------------------------------


class TestOverrideReviewItem:
    def test_sets_human_override_and_confidence(self, manager):
        """Overriding sets human_override=1 and confidence=1.0."""
        items = manager.get_review_items("b1")
        scene_item = next(
            i
            for i in items
            if i["junction_table"] == "character_scene"
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
            i
            for i in items
            if i["junction_table"] == "character_scene"
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
            i
            for i in items
            if i["junction_table"] == "character_span"
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
        book_item = next(i for i in items if i["junction_table"] == "character_book")
        # Should not raise — unknown column 'nonexistent' is ignored
        manager.override_review_item(book_item["item_id"], {"nonexistent": "value"})

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
        book_item = next(i for i in items if i["junction_table"] == "character_book")
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
# Union queue — walk items (honest union of junction + walk_review_item)
# ---------------------------------------------------------------------------


class TestWalkItemsInQueue:
    """get_review_items returns the HONEST UNION: junction live query
    (unchanged, byte-identical ids) PLUS pending walk_review_item rows with
    ``walkitem:``-prefixed ids and their own shape (kind, target_table,
    target_id, prior_value, created_ms — there is no confidence /
    human_override on walk items).  Junction items come first, walk items
    after (plan P3-S3).
    """

    _JUNCTION_IDS = frozenset(
        {
            "character_book:c2:b1",
            "character_scene:c1:sc1",
            "character_scene:c3:sc2",
            "character_span:c1:sp1",
            "character_span:c4:sp3",
        }
    )

    @staticmethod
    def _walk_items(items):
        return [i for i in items if i["item_id"].startswith("walkitem:")]

    def test_union_contains_junction_and_walk_items(self, union_manager):
        """Both junction items and pending walk items are returned."""
        items = union_manager.get_review_items("b1")
        junction = [i for i in items if "junction_table" in i]
        walk = self._walk_items(items)
        assert len(junction) == 5
        assert len(walk) == 3
        assert len(items) == 8

    def test_walk_items_carry_prefixed_ids_and_fields(self, union_manager):
        """Walk items expose walkitem:{id} ids and the table columns."""
        walk = self._walk_items(union_manager.get_review_items("b1"))
        by_id = {i["item_id"]: i for i in walk}
        assert set(by_id) == {"walkitem:w1", "walkitem:w2", "walkitem:w3"}

        w1 = by_id["walkitem:w1"]
        assert w1["kind"] == "voice_profile"
        assert w1["target_table"] == "character_metadata"
        assert w1["target_id"] == "c1"
        assert w1["prior_value"] == '{"voice":"old"}'
        assert w1["created_ms"] == 1000

        w2 = by_id["walkitem:w2"]
        assert w2["kind"] == "voice_assignment"
        assert w2["target_table"] == "character"
        assert w2["target_id"] == "c2"
        assert w2["prior_value"] is None
        assert w2["created_ms"] == 2000

        w3 = by_id["walkitem:w3"]
        assert w3["kind"] == "instruction"
        assert w3["target_table"] == "span"
        assert w3["target_id"] == "sp1"
        assert w3["prior_value"] == "slow"
        assert w3["created_ms"] == 3000

    def test_walk_item_shape_is_table_columns_only(self, union_manager):
        """Walk items carry exactly the walk_review_item columns — no
        confidence/human_override (those do not exist on walk items) — plus
        the Phase 6 contextual-review ``neighbors`` enrichment that every
        queue item now carries."""
        walk = self._walk_items(union_manager.get_review_items("b1"))
        assert len(walk) == 3
        expected = {
            "item_id",
            "kind",
            "target_table",
            "target_id",
            "status",
            "prior_value",
            "created_ms",
            "neighbors",
        }
        for item in walk:
            assert set(item) == expected
            assert set(item["neighbors"]) == {"before", "after"}

    def test_non_pending_walk_items_excluded(self, union_manager):
        """resolved/superseded/stale rows never surface in the queue."""
        walk_ids = {
            i["item_id"] for i in self._walk_items(union_manager.get_review_items("b1"))
        }
        assert "walkitem:w4" not in walk_ids  # resolved
        assert "walkitem:w5" not in walk_ids  # superseded
        assert "walkitem:w6" not in walk_ids  # stale

    def test_walk_items_scoped_to_book(self, union_manager):
        """Pending walk items of OTHER books never surface."""
        walk_ids = {
            i["item_id"] for i in self._walk_items(union_manager.get_review_items("b1"))
        }
        assert "walkitem:w7" not in walk_ids

    def test_union_order_junction_first(self, union_manager):
        """Junction items come first, then walk items (no other sort)."""
        items = union_manager.get_review_items("b1")
        assert len(items) == 8
        assert all("junction_table" in i for i in items[:5])
        assert all(i["item_id"].startswith("walkitem:") for i in items[5:])

    def test_junction_item_ids_byte_identical(self, union_manager):
        """Junction ids stay byte-identical {table}:{char}:{entity} in the
        union — backward compatible with the existing frontend."""
        items = union_manager.get_review_items("b1")
        junction_ids = {i["item_id"] for i in items if "junction_table" in i}
        assert junction_ids == self._JUNCTION_IDS

    def test_junction_items_keep_full_shape(self, union_manager):
        """Junction item shape is unchanged when walk items are present."""
        items = union_manager.get_review_items("b1")
        required = {
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
            if "junction_table" in item:
                assert required.issubset(set(item)), (
                    f"Missing keys: {required - set(item)}"
                )

    def test_walk_name_filter_narrows_junction_and_walk_items(self, union_manager):
        """walk_name narrows junction items (source LIKE) AND walk items
        (join walk_run on run_id → walk_name LIKE).

        'walk_2g' matches no junction source (all sources are 'walk') and
        exactly one walk run (run-1 → walk_2g_voice_audition), so the union
        collapses to the single walk item w1.
        """
        items = union_manager.get_review_items("b1", walk_name="walk_2g")
        assert len(items) == 1
        assert items[0]["item_id"] == "walkitem:w1"

    def test_walk_name_filter_matches_all_walk_sources(self, union_manager):
        """'walk' matches every junction source and every walk_run walk_name
        — the full union (5 junction + 3 walk items) comes back."""
        items = union_manager.get_review_items("b1", walk_name="walk")
        junction = [i for i in items if "junction_table" in i]
        walk = self._walk_items(items)
        assert len(junction) == 5
        assert len(walk) == 3
        # ordering preserved under the filter too
        assert all("junction_table" in i for i in items[:5])

    def test_walk_name_filter_no_match_returns_empty(self, union_manager):
        """A walk_name matching nothing yields an empty union."""
        items = union_manager.get_review_items("b1", walk_name="nonexistent-walk")
        assert items == []

    def test_union_without_walk_items_is_pure_junction(self, manager):
        """Backward compat: a book with no walk rows returns the exact same
        junction-only list as before the union."""
        items = manager.get_review_items("b1")
        assert all("junction_table" in i for i in items)
        assert all(not i["item_id"].startswith("walkitem:") for i in items)
        assert len(items) == 5


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


# ---------------------------------------------------------------------------
# resolve_review_action — prefix dispatch guards (Phase 4)
# ---------------------------------------------------------------------------


class TestResolveReviewActionDispatch:
    """ReviewManager.resolve_review_action — defensive-branch unit tests.

    The happy paths are covered via the API (TestReviewActionDispatch in
    test_api.py); these cover the guard branches that are unreachable through
    the endpoint layer (unknown action names) or through constrained data
    (an item kind absent from the target-write allowlist).
    """

    def test_unknown_action_on_junction_raises_value_error(self, manager):
        """An action outside accept/reject/override is rejected on junction ids."""
        with pytest.raises(ValueError, match="Unknown review action"):
            manager.resolve_review_action("explode", "character_book:c2:b1")

    def test_unknown_action_on_walk_item_raises_value_error(self, union_manager):
        """An action outside accept/reject/override is rejected on walkitem ids."""
        with pytest.raises(ValueError, match="Unknown review action"):
            union_manager.resolve_review_action("explode", "walkitem:w1")

    def test_unsupported_kind_raises_value_error(self, union_manager, monkeypatch):
        """A kind missing from the target-write allowlist is rejected (400 path).

        Patches the module-level allowlist to a subset so the walk branch of
        reject hits the unsupported-kind guard (the real map covers all three
        CHECK-constrained kinds, making this branch defensive).
        """
        monkeypatch.setattr(
            review_module,
            "_WALK_TARGET_WRITES",
            {
                "voice_profile": "UPDATE character_metadata SET value = ? WHERE character_id = ? AND key = 'voice_profile'"
            },
        )
        # w2 is kind voice_assignment — not in the patched allowlist
        with pytest.raises(ValueError, match="Unsupported walk item kind"):
            union_manager.resolve_review_action("reject", "walkitem:w2")


# ---------------------------------------------------------------------------
# Phase 5 — transactional value-restore (undo backend): atomicity + rollback
# ---------------------------------------------------------------------------


def _seed_value_restore_rows(storage: InMemorySQLiteAdapter) -> None:
    """Seed walk_run + one pending walk_review_item per kind, with the walk's
    generated (current) values already written into the target rows.

    Mirrors the walk write order (2g voice_profile, 2h voice_assignment,
    2i instruction): ``prior_value`` is what the walk overwrote.
    """
    storage.execute_insert(
        "INSERT INTO voice_config (id, name, description) "
        "VALUES ('vc1', 'Warm Female', 'A warm female voice')"
    )
    storage.execute_insert(
        "INSERT INTO walk_run (run_id, book_id, walk_name, status, created_ms) "
        "VALUES ('run-p5', 'b1', 'walk_2g_voice_audition', 'completed', 1)"
    )
    # voice_profile -> character_metadata c1 (current '{"voice":"new"}', prior '{"voice":"old"}')
    storage.execute_insert(
        "INSERT INTO character_metadata (character_id, key, value) "
        "VALUES ('c1', 'voice_profile', '{\"voice\":\"new\"}')"
    )
    storage.execute_insert(
        "INSERT INTO walk_review_item "
        "(id, book_id, run_id, kind, target_table, target_id, prior_value, status, created_ms) "
        "VALUES ('wp1', 'b1', 'run-p5', 'voice_profile', 'character_metadata', 'c1', "
        "'{\"voice\":\"old\"}', 'pending', 100)"
    )
    # voice_assignment -> character c2 (current 'vc1', prior NULL)
    storage.execute_update(
        "UPDATE character SET voice_assignment_id = 'vc1' WHERE id = 'c2'"
    )
    storage.execute_insert(
        "INSERT INTO walk_review_item "
        "(id, book_id, run_id, kind, target_table, target_id, prior_value, status, created_ms) "
        "VALUES ('wa1', 'b1', 'run-p5', 'voice_assignment', 'character', 'c2', NULL, 'pending', 200)"
    )
    # instruction -> span sp1 (current 'slowly', prior 'cheerfully')
    storage.execute_update("UPDATE span SET instruct = 'slowly' WHERE id = 'sp1'")
    storage.execute_insert(
        "INSERT INTO walk_review_item "
        "(id, book_id, run_id, kind, target_table, target_id, prior_value, status, created_ms) "
        "VALUES ('wi1', 'b1', 'run-p5', 'instruction', 'span', 'sp1', 'cheerfully', 'pending', 300)"
    )


def _item_status(storage: InMemorySQLiteAdapter, item_id: str) -> str:
    """Return the current status of a walk_review_item row by id."""
    rows = storage.execute_query(
        "SELECT status FROM walk_review_item WHERE id = ?", (item_id,)
    )
    assert rows, f"no walk_review_item row {item_id!r}"
    return rows[0]["status"]


class TestWalkItemValueRestore:
    """Reject/override on a walkitem: value-restore is ATOMIC (contract L64).

    The restore (or override write) and the ``status = 'resolved'`` UPDATE
    commit in ONE transaction — either both are visible or neither.  A failure
    inside the transaction (bogus target SQL, or the adapter raising) rolls
    BOTH back: the item row stays ``pending`` and the target row keeps the
    walk's value.
    """

    @pytest.fixture
    def storage(self):
        storage = InMemorySQLiteAdapter()
        storage.init_db()
        _populate_storage(storage)
        _seed_value_restore_rows(storage)
        return storage

    @pytest.fixture
    def manager(self, storage):
        return ReviewManager(storage)

    # -- happy path: restore/write + resolved, one commit -------------------

    def test_reject_restores_prior_value_and_resolves(self, manager, storage):
        """Reject restores prior_value into the target and marks the item resolved."""
        manager.resolve_review_action("reject", "walkitem:wp1")

        rows = storage.execute_query(
            "SELECT value FROM character_metadata "
            "WHERE character_id = 'c1' AND key = 'voice_profile'"
        )
        assert rows[0]["value"] == '{"voice":"old"}'
        assert _item_status(storage, "wp1") == "resolved"

    def test_reject_restores_null_prior_and_resolves(self, manager, storage):
        """A NULL prior_value is restored as NULL (voice_assignment unset)."""
        manager.resolve_review_action("reject", "walkitem:wa1")

        rows = storage.execute_query(
            "SELECT voice_assignment_id FROM character WHERE id = 'c2'"
        )
        assert rows[0]["voice_assignment_id"] is None
        assert _item_status(storage, "wa1") == "resolved"

    def test_reject_restores_instruct_and_resolves(self, manager, storage):
        """Reject on an instruction item restores span.instruct."""
        manager.resolve_review_action("reject", "walkitem:wi1")

        rows = storage.execute_query("SELECT instruct FROM span WHERE id = 'sp1'")
        assert rows[0]["instruct"] == "cheerfully"
        assert _item_status(storage, "wi1") == "resolved"

    def test_override_writes_new_value_and_resolves(self, manager, storage):
        """Override writes new_value into the target and marks the item resolved."""
        manager.resolve_review_action("override", "walkitem:wp1", '{"voice":"human"}')

        rows = storage.execute_query(
            "SELECT value FROM character_metadata "
            "WHERE character_id = 'c1' AND key = 'voice_profile'"
        )
        assert rows[0]["value"] == '{"voice":"human"}'
        assert _item_status(storage, "wp1") == "resolved"

    def test_accept_resolves_without_target_write(self, manager, storage):
        """Accept writes NO target row — only the status flips to resolved."""
        manager.resolve_review_action("accept", "walkitem:wp1")

        assert _item_status(storage, "wp1") == "resolved"
        rows = storage.execute_query(
            "SELECT value FROM character_metadata "
            "WHERE character_id = 'c1' AND key = 'voice_profile'"
        )
        assert rows[0]["value"] == '{"voice":"new"}'  # the walk's value is kept

    def test_item_select_happens_outside_transaction(
        self, manager, storage, monkeypatch
    ):
        """Rule #6: the walkitem SELECT runs BEFORE the transaction opens."""
        calls: list[tuple[str, str | None]] = []
        real_transaction = storage.transaction
        real_query = storage.execute_query

        def tracking_query(sql, params=()):
            calls.append(("query", sql))
            return real_query(sql, params)

        def tracking_transaction():
            calls.append(("txn", None))
            return real_transaction()

        monkeypatch.setattr(storage, "execute_query", tracking_query)
        monkeypatch.setattr(storage, "transaction", tracking_transaction)

        manager.resolve_review_action("reject", "walkitem:wp1")

        txn_index = next(i for i, (kind, _) in enumerate(calls) if kind == "txn")
        select_calls = [sql for kind, sql in calls[:txn_index] if kind == "query"]
        assert any("walk_review_item" in sql for sql in select_calls)

    # -- rollback: a failure inside the txn undoes BOTH ---------------------

    def test_restore_write_failure_rolls_back_both(self, manager, storage, monkeypatch):
        """A failing restore (bogus target SQL) never commits: the item stays
        pending and the target keeps the walk value."""
        monkeypatch.setattr(
            review_module,
            "_WALK_TARGET_WRITES",
            {
                **review_module._WALK_TARGET_WRITES,
                "voice_profile": "UPDATE nonexistent SET value = ? WHERE character_id = ?",
            },
        )
        with pytest.raises(sqlite3.OperationalError):
            manager.resolve_review_action("reject", "walkitem:wp1")

        assert _item_status(storage, "wp1") == "pending"
        rows = storage.execute_query(
            "SELECT value FROM character_metadata "
            "WHERE character_id = 'c1' AND key = 'voice_profile'"
        )
        assert rows[0]["value"] == '{"voice":"new"}'  # unchanged

    def test_status_update_failure_rolls_back_restore(
        self, manager, storage, monkeypatch
    ):
        """The DISCRIMINATING rollback case: the restore write SUCCEEDS inside
        the txn, then the status UPDATE fails — both roll back (target
        unchanged, item still pending).  Without the transaction wrap the
        restore would have autocommitted and stayed visible."""
        real_update = storage.execute_update

        def failing_status_update(sql, params=()):
            if "status = 'resolved'" in sql:
                raise ConcurrentTransactionError("simulated status-write failure")
            return real_update(sql, params)

        monkeypatch.setattr(storage, "execute_update", failing_status_update)
        with pytest.raises(ConcurrentTransactionError):
            manager.resolve_review_action("reject", "walkitem:wp1")

        assert _item_status(storage, "wp1") == "pending"
        rows = storage.execute_query(
            "SELECT value FROM character_metadata "
            "WHERE character_id = 'c1' AND key = 'voice_profile'"
        )
        assert rows[0]["value"] == '{"voice":"new"}'  # the restore was rolled back

    def test_override_write_failure_rolls_back_both(
        self, manager, storage, monkeypatch
    ):
        """A failing override write (bogus target SQL) never commits: the item
        stays pending and the target keeps the walk value."""
        monkeypatch.setattr(
            review_module,
            "_WALK_TARGET_WRITES",
            {
                **review_module._WALK_TARGET_WRITES,
                "instruction": "UPDATE nonexistent SET instruct = ? WHERE id = ?",
            },
        )
        with pytest.raises(sqlite3.OperationalError):
            manager.resolve_review_action("override", "walkitem:wi1", "quickly")

        assert _item_status(storage, "wi1") == "pending"
        rows = storage.execute_query("SELECT instruct FROM span WHERE id = 'sp1'")
        assert rows[0]["instruct"] == "slowly"  # unchanged

    def test_override_status_update_failure_rolls_back_write(
        self, manager, storage, monkeypatch
    ):
        """Same discriminating rollback for override: the new_value write
        succeeds inside the txn, the status UPDATE fails, both roll back."""
        real_update = storage.execute_update

        def failing_status_update(sql, params=()):
            if "status = 'resolved'" in sql:
                raise ConcurrentTransactionError("simulated status-write failure")
            return real_update(sql, params)

        monkeypatch.setattr(storage, "execute_update", failing_status_update)
        with pytest.raises(ConcurrentTransactionError):
            manager.resolve_review_action(
                "override", "walkitem:wp1", '{"voice":"human"}'
            )

        assert _item_status(storage, "wp1") == "pending"
        rows = storage.execute_query(
            "SELECT value FROM character_metadata "
            "WHERE character_id = 'c1' AND key = 'voice_profile'"
        )
        assert rows[0]["value"] == '{"voice":"new"}'  # the override was rolled back

    def test_failed_restore_leaves_item_actionable(self, manager, storage, monkeypatch):
        """After a rolled-back failure the item is still pending and retryable."""
        real_sql = review_module._WALK_TARGET_WRITES["voice_profile"]
        monkeypatch.setattr(
            review_module,
            "_WALK_TARGET_WRITES",
            {
                **review_module._WALK_TARGET_WRITES,
                "voice_profile": "UPDATE nonexistent SET value = ? WHERE character_id = ?",
            },
        )
        with pytest.raises(sqlite3.OperationalError):
            manager.resolve_review_action("reject", "walkitem:wp1")
        assert _item_status(storage, "wp1") == "pending"

        # restore the allowlist and retry — the item is still actionable
        monkeypatch.setattr(
            review_module,
            "_WALK_TARGET_WRITES",
            {**review_module._WALK_TARGET_WRITES, "voice_profile": real_sql},
        )
        manager.resolve_review_action("reject", "walkitem:wp1")

        assert _item_status(storage, "wp1") == "resolved"
        rows = storage.execute_query(
            "SELECT value FROM character_metadata "
            "WHERE character_id = 'c1' AND key = 'voice_profile'"
        )
        assert rows[0]["value"] == '{"voice":"old"}'


# ---------------------------------------------------------------------------
# Phase 6 — contextual review: ±2 neighboring spans (DD UX workflow #5)
# ---------------------------------------------------------------------------


def _populate_neighbor_storage(storage: InMemorySQLiteAdapter) -> None:
    """Insert a book with 5 spans in a DEFINED presentation order plus review
    items whose span references exercise the full ±2 window and its edges.

    Presentation order (via span_presentation VIEW, book-scoped by
    ``bc.parent_id``): ``sp-nb1`` (global 1) .. ``sp-nb5`` (global 5) — one
    span per paragraph, paragraphs in order in a single scene.

    Review items seeded:
      - character_span junctions: c-nb speaker of sp-nb1 (FIRST span),
        sp-nb3 (middle — full ±2 window) and sp-nb5 (LAST span);
      - character_book junction c-nb → b-nb (no span reference);
      - walk items: kind ``instruction`` targeting span sp-nb2 (resolves via
        target_id), plus kinds ``voice_profile`` and ``voice_assignment``
        (no span reference → empty lists).
    """
    storage.execute_insert("INSERT INTO series (id) VALUES ('s-nb')")
    storage.execute_insert(
        "INSERT INTO book (id, series_id, position) VALUES ('b-nb', 's-nb', 1)"
    )
    storage.execute_insert("INSERT INTO chapter (id, book_id) VALUES ('ch-nb', 'b-nb')")
    storage.execute_insert(
        "INSERT INTO book_chapter (child_id, parent_id, position) "
        "VALUES ('ch-nb', 'b-nb', 1)"
    )
    storage.execute_insert("INSERT INTO scene (id) VALUES ('sc-nb')")
    storage.execute_insert(
        "INSERT INTO chapter_scene (child_id, parent_id, position) "
        "VALUES ('sc-nb', 'ch-nb', 1)"
    )
    for i in range(1, 6):
        storage.execute_insert(f"INSERT INTO paragraph (id) VALUES ('p-nb{i}')")
        storage.execute_insert(
            f"INSERT INTO scene_paragraph (child_id, parent_id, position) "
            f"VALUES ('p-nb{i}', 'sc-nb', {i})"
        )
        storage.execute_insert(
            f"INSERT INTO span (id, span_type, text) "
            f"VALUES ('sp-nb{i}', 'sentence', 'Span {i} text')"
        )
        storage.execute_insert(
            f"INSERT INTO paragraph_span (child_id, parent_id, position) "
            f"VALUES ('sp-nb{i}', 'p-nb{i}', 1)"
        )
    storage.execute_insert(
        "INSERT INTO character (id, name, aliases) VALUES ('c-nb', 'Narrator', '[]')"
    )
    # character_span junctions in the review band [0.5, 0.7)
    for span_id, conf in (("sp-nb1", 0.6), ("sp-nb3", 0.55), ("sp-nb5", 0.52)):
        storage.execute_insert(
            "INSERT INTO character_span "
            "(character_id, span_id, relation_type, source, confidence) "
            f"VALUES ('c-nb', '{span_id}', 'speaker', 'walk', {conf})"
        )
    # character_book junction (no span reference)
    storage.execute_insert(
        "INSERT INTO character_book (character_id, book_id, source, confidence) "
        "VALUES ('c-nb', 'b-nb', 'walk', 0.58)"
    )
    # walk items: instruction targets a span; voice_profile/voice_assignment don't
    storage.execute_insert(
        "INSERT INTO walk_run (run_id, book_id, walk_name, status, created_ms) "
        "VALUES ('run-nb', 'b-nb', 'walk_2i_delivery', 'completed', 1)"
    )
    storage.execute_insert(
        "INSERT INTO walk_review_item "
        "(id, book_id, run_id, kind, target_table, target_id, prior_value, status, created_ms) "
        "VALUES ('w-nb1', 'b-nb', 'run-nb', 'instruction', 'span', 'sp-nb2', "
        "'slow', 'pending', 100)"
    )
    storage.execute_insert(
        "INSERT INTO walk_review_item "
        "(id, book_id, run_id, kind, target_table, target_id, prior_value, status, created_ms) "
        "VALUES ('w-nb2', 'b-nb', 'run-nb', 'voice_profile', 'character_metadata', "
        "'c-nb', '{}', 'pending', 200)"
    )
    storage.execute_insert(
        "INSERT INTO walk_review_item "
        "(id, book_id, run_id, kind, target_table, target_id, prior_value, status, created_ms) "
        "VALUES ('w-nb3', 'b-nb', 'run-nb', 'voice_assignment', 'character', "
        "'c-nb', NULL, 'pending', 300)"
    )


@pytest.fixture
def neighbor_manager():
    """ReviewManager over the 5-span contextual-review fixture."""
    storage = InMemorySQLiteAdapter()
    storage.init_db()
    _populate_neighbor_storage(storage)
    return ReviewManager(storage)


class TestReviewItemNeighborContext:
    """Phase 6 — get_review_items enriches EVERY item with
    ``neighbors: {before: [...], after: [...]}`` — up to 2 span TEXTS in
    presentation order around the item's span reference (DD UX workflow #5).

    Resolution rules:
      - character_span junction items → ``related_entity_id`` (the span_id);
      - walk items of kind ``instruction`` → ``target_id``;
      - every other kind (character_book, character_scene, voice_profile,
        voice_assignment) → empty before/after lists.
    """

    @staticmethod
    def _by_id(manager) -> dict:
        return {i["item_id"]: i for i in manager.get_review_items("b-nb")}

    def test_every_item_carries_neighbors_key(self, neighbor_manager):
        """Every item — junction AND walk — carries the neighbors dict."""
        items = neighbor_manager.get_review_items("b-nb")
        assert len(items) == 7
        for item in items:
            assert "neighbors" in item
            assert set(item["neighbors"]) == {"before", "after"}
            assert isinstance(item["neighbors"]["before"], list)
            assert isinstance(item["neighbors"]["after"], list)

    def test_character_span_middle_span_full_window(self, neighbor_manager):
        """A span in the middle gets the full ±2 window, in presentation order."""
        item = self._by_id(neighbor_manager)["character_span:c-nb:sp-nb3"]
        assert item["neighbors"] == {
            "before": ["Span 1 text", "Span 2 text"],
            "after": ["Span 4 text", "Span 5 text"],
        }

    def test_first_span_empty_before(self, neighbor_manager):
        """Item targeting the book's FIRST span → empty before."""
        item = self._by_id(neighbor_manager)["character_span:c-nb:sp-nb1"]
        assert item["neighbors"]["before"] == []
        assert item["neighbors"]["after"] == ["Span 2 text", "Span 3 text"]

    def test_last_span_empty_after(self, neighbor_manager):
        """Item targeting the book's LAST span → empty after."""
        item = self._by_id(neighbor_manager)["character_span:c-nb:sp-nb5"]
        assert item["neighbors"]["before"] == ["Span 3 text", "Span 4 text"]
        assert item["neighbors"]["after"] == []

    def test_instruction_walk_item_resolves_via_target_id(self, neighbor_manager):
        """Walk items of kind 'instruction' resolve their span via target_id."""
        item = self._by_id(neighbor_manager)["walkitem:w-nb1"]
        assert item["neighbors"] == {
            "before": ["Span 1 text"],
            "after": ["Span 3 text", "Span 4 text"],
        }

    def test_no_span_reference_kinds_get_empty_lists(self, neighbor_manager):
        """character_book junction + voice_profile/voice_assignment walk items
        carry no span reference → empty before/after."""
        by_id = self._by_id(neighbor_manager)
        assert by_id["character_book:c-nb:b-nb"]["neighbors"] == {
            "before": [],
            "after": [],
        }
        assert by_id["walkitem:w-nb2"]["neighbors"] == {"before": [], "after": []}
        assert by_id["walkitem:w-nb3"]["neighbors"] == {"before": [], "after": []}

    def test_character_scene_junction_gets_empty_lists(self, neighbor_manager):
        """character_scene junction items have no span reference (scene id)."""
        storage = neighbor_manager._storage
        storage.execute_insert(
            "INSERT INTO character_scene "
            "(character_id, scene_id, relation_type, source, confidence) "
            "VALUES ('c-nb', 'sc-nb', 'present', 'walk', 0.57)"
        )
        item = self._by_id(neighbor_manager)["character_scene:c-nb:sc-nb"]
        assert item["neighbors"] == {"before": [], "after": []}

    def test_walk_item_targeting_span_outside_book_gets_empty_lists(
        self, neighbor_manager
    ):
        """An instruction walk item whose target_id is not in the book's
        presentation order (e.g. a span of another book) → empty lists."""
        storage = neighbor_manager._storage
        storage.execute_insert(
            "INSERT INTO walk_review_item "
            "(id, book_id, run_id, kind, target_table, target_id, prior_value, status, created_ms) "
            "VALUES ('w-nb4', 'b-nb', 'run-nb', 'instruction', 'span', 'sp-other', "
            "'slow', 'pending', 400)"
        )
        item = self._by_id(neighbor_manager)["walkitem:w-nb4"]
        assert item["neighbors"] == {"before": [], "after": []}
