"""Spec-first tests for the pipeline operation executor.

Covers:
- execute_split: position renumbering, character_span redistribution (left keeps, right copies)
- execute_merge: position renumbering, character_span union with confidence tiebreak
- execute_move: position renumbering, same-parent validation
- execute_delete: position renumbering, character_span cleanup
- Edge cases: invalid indices, non-adjacent merge, cross-parent move, empty memberships
- 100% operation executor coverage target
"""

from __future__ import annotations

import sqlite3

import pytest

from app.pipeline.adapter import InMemorySQLiteAdapter
from app.pipeline.operations import OperationExecutor


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def storage():
    """Return an InMemorySQLiteAdapter with schema initialised."""
    adapter = InMemorySQLiteAdapter()
    adapter.init_db()
    yield adapter
    adapter.close()


@pytest.fixture()
def executor(storage):
    """Return an OperationExecutor with initialised storage."""
    return OperationExecutor(storage)


def _populate_test_spine(conn: sqlite3.Connection) -> dict:
    """Create a minimal test spine with multiple spans for operations testing.

    Structure:
      series s1
        book b1 (position=1)
          chapter c1 (position=1)
            scene sc1 (position=1)
              paragraph p1 (position=1)
                span sp1 (position=1, sentence) - global_index=1
                span sp2 (position=2, quotation) - global_index=2
                span sp3 (position=3, sentence) - global_index=3
              paragraph p2 (position=2)
                span sp4 (position=1, sentence) - global_index=4

    Returns dict with span IDs for reference.
    """
    conn.execute("INSERT INTO series VALUES ('s1')")
    conn.execute("INSERT INTO book VALUES ('b1', 's1', 1, 1, 1)")
    conn.execute("INSERT INTO chapter VALUES ('c1', 'b1')")
    conn.execute("INSERT INTO scene VALUES ('sc1')")
    conn.execute("INSERT INTO paragraph VALUES ('p1')")
    conn.execute("INSERT INTO paragraph VALUES ('p2')")
    conn.execute("INSERT INTO span VALUES ('sp1', 'sentence', NULL)")
    conn.execute("INSERT INTO span VALUES ('sp2', 'quotation', 'angrily')")
    conn.execute("INSERT INTO span VALUES ('sp3', 'sentence', NULL)")
    conn.execute("INSERT INTO span VALUES ('sp4', 'sentence', NULL)")

    # Edge tables
    conn.execute("INSERT INTO book_chapter VALUES ('c1', 'b1', 1)")
    conn.execute("INSERT INTO chapter_scene VALUES ('sc1', 'c1', 1)")
    conn.execute("INSERT INTO scene_paragraph VALUES ('p1', 'sc1', 1)")
    conn.execute("INSERT INTO scene_paragraph VALUES ('p2', 'sc1', 2)")
    conn.execute("INSERT INTO paragraph_span VALUES ('sp1', 'p1', 1)")
    conn.execute("INSERT INTO paragraph_span VALUES ('sp2', 'p1', 2)")
    conn.execute("INSERT INTO paragraph_span VALUES ('sp3', 'p1', 3)")
    conn.execute("INSERT INTO paragraph_span VALUES ('sp4', 'p2', 1)")

    return {"sp1": "sp1", "sp2": "sp2", "sp3": "sp3", "sp4": "sp4"}


def _add_character(conn: sqlite3.Connection, char_id: str, name: str) -> None:
    """Insert a character for testing."""
    conn.execute(f"INSERT INTO character VALUES ('{char_id}', '{name}', '[]', NULL, NULL)")


def _add_character_span(
    conn: sqlite3.Connection,
    char_id: str,
    span_id: str,
    relation_type: str,
    source: str = "walk",
    confidence: float = 0.8,
    human_override: int = 0,
) -> None:
    """Insert a character_span membership."""
    conn.execute(
        "INSERT INTO character_span VALUES (?, ?, ?, ?, ?, ?)",
        (char_id, span_id, relation_type, source, confidence, human_override),
    )


def _get_presentation_order(conn: sqlite3.Connection) -> list[str]:
    """Return span IDs in presentation order."""
    rows = conn.execute(
        "SELECT id FROM span_presentation ORDER BY global_index"
    ).fetchall()
    return [r[0] for r in rows]


def _get_span_positions(conn: sqlite3.Connection, parent_id: str) -> dict[str, int]:
    """Return {span_id: position} for a given parent paragraph."""
    rows = conn.execute(
        "SELECT child_id, position FROM paragraph_span WHERE parent_id = ?",
        (parent_id,),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def _get_character_memberships(
    conn: sqlite3.Connection, span_id: str
) -> list[tuple[str, str, float]]:
    """Return [(character_id, relation_type, confidence)] for a span."""
    rows = conn.execute(
        "SELECT character_id, relation_type, confidence FROM character_span WHERE span_id = ?",
        (span_id,),
    ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


# ---------------------------------------------------------------------------
# execute_split tests
# ---------------------------------------------------------------------------


class TestExecuteSplit:
    def test_split_creates_new_span(self, storage, executor):
        """Split creates a new span with same span_type."""
        conn = storage.get_connection()
        _populate_test_spine(conn)

        executor.execute_split(presentation_index=2, split_point=5)

        # Should now have 5 spans (was 4, added 1)
        rows = conn.execute("SELECT COUNT(*) FROM span").fetchone()
        assert rows[0] == 5

    def test_split_preserves_span_type(self, storage, executor):
        """New span has same span_type as original."""
        conn = storage.get_connection()
        _populate_test_spine(conn)

        executor.execute_split(presentation_index=2, split_point=5)

        # sp2 is quotation, new span should also be quotation
        new_span = conn.execute(
            "SELECT span_type FROM span WHERE id NOT IN ('sp1', 'sp2', 'sp3', 'sp4')"
        ).fetchone()
        assert new_span[0] == "quotation"

    def test_split_renumbers_positions(self, storage, executor):
        """Positions after split point are incremented."""
        conn = storage.get_connection()
        _populate_test_spine(conn)

        executor.execute_split(presentation_index=2, split_point=5)

        positions = _get_span_positions(conn, "p1")
        # sp1=1, sp2=2, new_span=3, sp3=4
        assert positions["sp1"] == 1
        assert positions["sp2"] == 2
        assert positions["sp3"] == 4  # was 3, now 4
        assert len(positions) == 4

    def test_split_presentation_order_correct(self, storage, executor):
        """Presentation order reflects new span position."""
        conn = storage.get_connection()
        _populate_test_spine(conn)

        executor.execute_split(presentation_index=2, split_point=5)

        order = _get_presentation_order(conn)
        # sp1, sp2, new_span, sp3, sp4
        assert order[0] == "sp1"
        assert order[1] == "sp2"
        assert order[3] == "sp3"
        assert order[4] == "sp4"
        assert len(order) == 5

    def test_split_left_keeps_memberships(self, storage, executor):
        """Left span (original) keeps existing character_span memberships."""
        conn = storage.get_connection()
        _populate_test_spine(conn)
        _add_character(conn, "ch1", "Alice")
        _add_character_span(conn, "ch1", "sp2", "speaker", confidence=0.9)

        executor.execute_split(presentation_index=2, split_point=5)

        # sp2 (left) should still have ch1 membership
        memberships = _get_character_memberships(conn, "sp2")
        assert len(memberships) == 1
        assert memberships[0] == ("ch1", "speaker", 0.9)

    def test_split_right_copies_memberships(self, storage, executor):
        """Right span (new) gets copy of all character_span memberships."""
        conn = storage.get_connection()
        _populate_test_spine(conn)
        _add_character(conn, "ch1", "Alice")
        _add_character(conn, "ch2", "Bob")
        _add_character_span(conn, "ch1", "sp2", "speaker", confidence=0.9)
        _add_character_span(conn, "ch2", "sp2", "mentioned", confidence=0.7)

        executor.execute_split(presentation_index=2, split_point=5)

        # Find new span
        new_span_id = conn.execute(
            "SELECT id FROM span WHERE id NOT IN ('sp1', 'sp2', 'sp3', 'sp4')"
        ).fetchone()[0]

        # New span should have copies of both memberships
        memberships = _get_character_memberships(conn, new_span_id)
        assert len(memberships) == 2
        assert ("ch1", "speaker", 0.9) in memberships
        assert ("ch2", "mentioned", 0.7) in memberships

    def test_split_no_memberships(self, storage, executor):
        """Split works when span has no character_span memberships."""
        conn = storage.get_connection()
        _populate_test_spine(conn)

        # sp2 has no memberships
        executor.execute_split(presentation_index=2, split_point=5)

        # Should succeed without error
        order = _get_presentation_order(conn)
        assert len(order) == 5

    def test_split_invalid_presentation_index(self, storage, executor):
        """Split raises ValueError for invalid presentation index."""
        conn = storage.get_connection()
        _populate_test_spine(conn)

        with pytest.raises(ValueError, match="Presentation index .* not found"):
            executor.execute_split(presentation_index=999, split_point=5)


# ---------------------------------------------------------------------------
# execute_merge tests
# ---------------------------------------------------------------------------


class TestExecuteMerge:
    def test_merge_removes_right_span(self, storage, executor):
        """Merge deletes the right span."""
        conn = storage.get_connection()
        _populate_test_spine(conn)

        executor.execute_merge(presentation_index_left=1, presentation_index_right=2)

        # sp2 should be deleted
        rows = conn.execute("SELECT COUNT(*) FROM span").fetchone()
        assert rows[0] == 3  # sp1, sp3, sp4

    def test_merge_renumbers_positions(self, storage, executor):
        """Positions after merge are decremented."""
        conn = storage.get_connection()
        _populate_test_spine(conn)

        executor.execute_merge(presentation_index_left=1, presentation_index_right=2)

        positions = _get_span_positions(conn, "p1")
        # sp1=1, sp3=2 (was 3)
        assert positions["sp1"] == 1
        assert positions["sp3"] == 2
        assert len(positions) == 2

    def test_merge_presentation_order_correct(self, storage, executor):
        """Presentation order reflects merged span."""
        conn = storage.get_connection()
        _populate_test_spine(conn)

        executor.execute_merge(presentation_index_left=1, presentation_index_right=2)

        order = _get_presentation_order(conn)
        # sp1, sp3, sp4
        assert order == ["sp1", "sp3", "sp4"]

    def test_merge_union_memberships(self, storage, executor):
        """Merge combines character_span memberships from both spans."""
        conn = storage.get_connection()
        _populate_test_spine(conn)
        _add_character(conn, "ch1", "Alice")
        _add_character(conn, "ch2", "Bob")
        _add_character_span(conn, "ch1", "sp1", "speaker", confidence=0.9)
        _add_character_span(conn, "ch2", "sp2", "mentioned", confidence=0.7)

        executor.execute_merge(presentation_index_left=1, presentation_index_right=2)

        # sp1 should have both memberships
        memberships = _get_character_memberships(conn, "sp1")
        assert len(memberships) == 2
        assert ("ch1", "speaker", 0.9) in memberships
        assert ("ch2", "mentioned", 0.7) in memberships

    def test_merge_confidence_tiebreak(self, storage, executor):
        """Merge keeps higher confidence for duplicate (character_id, relation_type)."""
        conn = storage.get_connection()
        _populate_test_spine(conn)
        _add_character(conn, "ch1", "Alice")
        _add_character_span(conn, "ch1", "sp1", "speaker", confidence=0.6)
        _add_character_span(conn, "ch1", "sp2", "speaker", confidence=0.9)

        executor.execute_merge(presentation_index_left=1, presentation_index_right=2)

        # sp1 should have ch1 with confidence 0.9 (higher)
        memberships = _get_character_memberships(conn, "sp1")
        assert len(memberships) == 1
        assert memberships[0] == ("ch1", "speaker", 0.9)

    def test_merge_non_adjacent_spans_error(self, storage, executor):
        """Merge raises ValueError for non-adjacent spans."""
        conn = storage.get_connection()
        _populate_test_spine(conn)

        with pytest.raises(ValueError, match="Cannot merge non-adjacent spans"):
            executor.execute_merge(presentation_index_left=1, presentation_index_right=3)

    def test_merge_different_parents_error(self, storage, executor):
        """Merge raises ValueError for spans with different parents."""
        conn = storage.get_connection()
        _populate_test_spine(conn)

        # sp1 (p1) and sp4 (p2) have different parents
        with pytest.raises(ValueError, match="Cannot merge spans with different parents"):
            executor.execute_merge(presentation_index_left=1, presentation_index_right=4)

    def test_merge_no_memberships(self, storage, executor):
        """Merge works when spans have no character_span memberships."""
        conn = storage.get_connection()
        _populate_test_spine(conn)

        executor.execute_merge(presentation_index_left=1, presentation_index_right=2)

        # Should succeed without error
        order = _get_presentation_order(conn)
        assert len(order) == 3


# ---------------------------------------------------------------------------
# execute_move tests
# ---------------------------------------------------------------------------


class TestExecuteMove:
    def test_move_forward(self, storage, executor):
        """Move span forward within paragraph."""
        conn = storage.get_connection()
        _populate_test_spine(conn)

        # Move sp1 (position 1) to position 3
        executor.execute_move(presentation_index_from=1, presentation_index_to=3)

        positions = _get_span_positions(conn, "p1")
        # sp2=1, sp3=2, sp1=3
        assert positions["sp1"] == 3
        assert positions["sp2"] == 1
        assert positions["sp3"] == 2

    def test_move_backward(self, storage, executor):
        """Move span backward within paragraph."""
        conn = storage.get_connection()
        _populate_test_spine(conn)

        # Move sp3 (position 3) to position 1
        executor.execute_move(presentation_index_from=3, presentation_index_to=1)

        positions = _get_span_positions(conn, "p1")
        # sp3=1, sp1=2, sp2=3
        assert positions["sp1"] == 2
        assert positions["sp2"] == 3
        assert positions["sp3"] == 1

    def test_move_same_position_noop(self, storage, executor):
        """Move to same position is a no-op."""
        conn = storage.get_connection()
        _populate_test_spine(conn)

        positions_before = _get_span_positions(conn, "p1")
        executor.execute_move(presentation_index_from=2, presentation_index_to=2)
        positions_after = _get_span_positions(conn, "p1")

        assert positions_before == positions_after

    def test_move_presentation_order_correct(self, storage, executor):
        """Presentation order reflects moved span."""
        conn = storage.get_connection()
        _populate_test_spine(conn)

        # Move sp1 to position 3
        executor.execute_move(presentation_index_from=1, presentation_index_to=3)

        order = _get_presentation_order(conn)
        # sp2, sp3, sp1, sp4
        assert order == ["sp2", "sp3", "sp1", "sp4"]

    def test_move_different_parents_error(self, storage, executor):
        """Move raises ValueError for different parent paragraphs."""
        conn = storage.get_connection()
        _populate_test_spine(conn)

        # sp1 (p1) cannot move to sp4's position (p2)
        with pytest.raises(ValueError, match="Cannot move span to different parent"):
            executor.execute_move(presentation_index_from=1, presentation_index_to=4)

    def test_move_preserves_memberships(self, storage, executor):
        """Move does not affect character_span memberships."""
        conn = storage.get_connection()
        _populate_test_spine(conn)
        _add_character(conn, "ch1", "Alice")
        _add_character_span(conn, "ch1", "sp1", "speaker", confidence=0.9)

        executor.execute_move(presentation_index_from=1, presentation_index_to=3)

        memberships = _get_character_memberships(conn, "sp1")
        assert len(memberships) == 1
        assert memberships[0] == ("ch1", "speaker", 0.9)


# ---------------------------------------------------------------------------
# execute_delete tests
# ---------------------------------------------------------------------------


class TestExecuteDelete:
    def test_delete_removes_span(self, storage, executor):
        """Delete removes the span."""
        conn = storage.get_connection()
        _populate_test_spine(conn)

        executor.execute_delete(presentation_index=2)

        rows = conn.execute("SELECT COUNT(*) FROM span").fetchone()
        assert rows[0] == 3  # sp1, sp3, sp4

    def test_delete_renumbers_positions(self, storage, executor):
        """Positions after delete are decremented."""
        conn = storage.get_connection()
        _populate_test_spine(conn)

        executor.execute_delete(presentation_index=2)

        positions = _get_span_positions(conn, "p1")
        # sp1=1, sp3=2 (was 3)
        assert positions["sp1"] == 1
        assert positions["sp3"] == 2
        assert len(positions) == 2

    def test_delete_presentation_order_correct(self, storage, executor):
        """Presentation order reflects deleted span."""
        conn = storage.get_connection()
        _populate_test_spine(conn)

        executor.execute_delete(presentation_index=2)

        order = _get_presentation_order(conn)
        # sp1, sp3, sp4
        assert order == ["sp1", "sp3", "sp4"]

    def test_delete_removes_memberships(self, storage, executor):
        """Delete removes character_span memberships for deleted span."""
        conn = storage.get_connection()
        _populate_test_spine(conn)
        _add_character(conn, "ch1", "Alice")
        _add_character_span(conn, "ch1", "sp2", "speaker", confidence=0.9)

        executor.execute_delete(presentation_index=2)

        # sp2 is deleted, so no memberships should exist
        rows = conn.execute(
            "SELECT COUNT(*) FROM character_span WHERE span_id = 'sp2'"
        ).fetchone()
        assert rows[0] == 0

    def test_delete_preserves_other_memberships(self, storage, executor):
        """Delete does not affect other spans' memberships."""
        conn = storage.get_connection()
        _populate_test_spine(conn)
        _add_character(conn, "ch1", "Alice")
        _add_character(conn, "ch2", "Bob")
        _add_character_span(conn, "ch1", "sp1", "speaker", confidence=0.9)
        _add_character_span(conn, "ch2", "sp2", "mentioned", confidence=0.7)

        executor.execute_delete(presentation_index=2)

        # sp1 should still have ch1 membership
        memberships = _get_character_memberships(conn, "sp1")
        assert len(memberships) == 1
        assert memberships[0] == ("ch1", "speaker", 0.9)

    def test_delete_no_memberships(self, storage, executor):
        """Delete works when span has no character_span memberships."""
        conn = storage.get_connection()
        _populate_test_spine(conn)

        executor.execute_delete(presentation_index=2)

        # Should succeed without error
        order = _get_presentation_order(conn)
        assert len(order) == 3

    def test_delete_last_span_in_paragraph(self, storage, executor):
        """Delete last span in paragraph works correctly."""
        conn = storage.get_connection()
        _populate_test_spine(conn)

        executor.execute_delete(presentation_index=3)

        positions = _get_span_positions(conn, "p1")
        # sp1=1, sp2=2
        assert positions["sp1"] == 1
        assert positions["sp2"] == 2
        assert len(positions) == 2

    def test_delete_invalid_presentation_index(self, storage, executor):
        """Delete raises ValueError for invalid presentation index."""
        conn = storage.get_connection()
        _populate_test_spine(conn)

        with pytest.raises(ValueError, match="Presentation index .* not found"):
            executor.execute_delete(presentation_index=999)


# ---------------------------------------------------------------------------
# Edge cases and integration
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_multiple_operations_sequence(self, storage, executor):
        """Multiple operations in sequence maintain consistency."""
        conn = storage.get_connection()
        _populate_test_spine(conn)

        # Split sp2
        executor.execute_split(presentation_index=2, split_point=5)
        # Now: sp1, sp2, new_span, sp3, sp4

        # Delete new_span (now at index 3)
        executor.execute_delete(presentation_index=3)
        # Now: sp1, sp2, sp3, sp4

        order = _get_presentation_order(conn)
        assert order == ["sp1", "sp2", "sp3", "sp4"]

    def test_split_then_merge(self, storage, executor):
        """Split then merge restores original state (mostly)."""
        conn = storage.get_connection()
        _populate_test_spine(conn)
        _add_character(conn, "ch1", "Alice")
        _add_character_span(conn, "ch1", "sp2", "speaker", confidence=0.9)

        # Split sp2
        executor.execute_split(presentation_index=2, split_point=5)
        # Now: sp1, sp2, new_span, sp3, sp4

        # Merge sp2 and new_span
        executor.execute_merge(presentation_index_left=2, presentation_index_right=3)
        # Now: sp1, sp2, sp3, sp4

        order = _get_presentation_order(conn)
        assert order == ["sp1", "sp2", "sp3", "sp4"]

        # sp2 should still have ch1 membership
        memberships = _get_character_memberships(conn, "sp2")
        assert len(memberships) == 1

    def test_operations_preserve_other_paragraphs(self, storage, executor):
        """Operations in one paragraph don't affect other paragraphs."""
        conn = storage.get_connection()
        _populate_test_spine(conn)

        executor.execute_delete(presentation_index=1)

        # p2 should be unaffected
        positions_p2 = _get_span_positions(conn, "p2")
        assert positions_p2 == {"sp4": 1}

    def test_operations_with_multiple_characters(self, storage, executor):
        """Operations handle multiple character memberships correctly."""
        conn = storage.get_connection()
        _populate_test_spine(conn)
        _add_character(conn, "ch1", "Alice")
        _add_character(conn, "ch2", "Bob")
        _add_character(conn, "ch3", "Charlie")
        _add_character_span(conn, "ch1", "sp2", "speaker", confidence=0.9)
        _add_character_span(conn, "ch2", "sp2", "mentioned", confidence=0.7)
        _add_character_span(conn, "ch3", "sp2", "present", confidence=0.8)

        executor.execute_split(presentation_index=2, split_point=5)

        # Find new span
        new_span_id = conn.execute(
            "SELECT id FROM span WHERE id NOT IN ('sp1', 'sp2', 'sp3', 'sp4')"
        ).fetchone()[0]

        # Both spans should have all three memberships
        sp2_memberships = _get_character_memberships(conn, "sp2")
        new_memberships = _get_character_memberships(conn, new_span_id)

        assert len(sp2_memberships) == 3
        assert len(new_memberships) == 3
