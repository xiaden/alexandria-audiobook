"""Presentation VIEW tests for the audiobook pipeline.

Covers:
- Test 1: span_presentation VIEW nested sort order across books/chapters/scenes/paragraphs/spans
- Test 2: presentation index stability across re-export after modifying a span
- Test 3: presentation index correctness after a split operation
- Test 4: presentation index correctness after a merge operation

All tests use InMemorySQLiteAdapter for isolation. No production code is modified.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.pipeline.adapter import InMemorySQLiteAdapter
from app.pipeline.assembly import export_annotated_script
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
    """Return an OperationExecutor backed by *storage*."""
    return OperationExecutor(storage)


def _get_presentation_rows(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    """Return [(global_index, span_id)] ordered by global_index."""
    rows = conn.execute(
        "SELECT global_index, id FROM span_presentation ORDER BY global_index"
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


# ---------------------------------------------------------------------------
# Test 1: Nested sort order
# ---------------------------------------------------------------------------


class TestPresentationViewNestedSort:
    """Verify span_presentation VIEW orders spans by nested hierarchy positions."""

    def test_presentation_view_nested_sort(self, storage):
        """span_presentation VIEW orders by book.position → chapter → scene → paragraph → span.

        Note: the actual VIEW orders by book.position (not series.position, book.book_number).
        """
        conn = storage.get_connection()

        # -- Series
        conn.execute("INSERT INTO series VALUES ('s1')")

        # -- Book 1 (position=2) — should come AFTER book 2 in presentation order
        conn.execute(
            "INSERT INTO book (id, series_id, book_number, version, position) "
            "VALUES ('b1', 's1', 1, 1, 2)"
        )
        # -- Book 2 (position=1) — should come FIRST
        conn.execute(
            "INSERT INTO book (id, series_id, book_number, version, position) "
            "VALUES ('b2', 's1', 2, 1, 1)"
        )

        # -- Book 1: 2 chapters, 1 scene each, 1 paragraph each, 1 span each
        conn.execute("INSERT INTO chapter VALUES ('b1_c1', 'b1')")
        conn.execute("INSERT INTO chapter VALUES ('b1_c2', 'b1')")
        conn.execute("INSERT INTO scene VALUES ('b1_c1_sc1')")
        conn.execute("INSERT INTO scene VALUES ('b1_c2_sc1')")
        conn.execute("INSERT INTO paragraph VALUES ('b1_c1_sc1_p1')")
        conn.execute("INSERT INTO paragraph VALUES ('b1_c2_sc1_p1')")
        conn.execute(
            "INSERT INTO span (id, span_type, instruct, text) "
            "VALUES ('b1_c1_sc1_p1_sp1', 'sentence', NULL, 'B1C1')"
        )
        conn.execute(
            "INSERT INTO span (id, span_type, instruct, text) "
            "VALUES ('b1_c2_sc1_p1_sp1', 'sentence', NULL, 'B1C2')"
        )

        # -- Book 2: 2 chapters, 1 scene each, 1 paragraph each, 2 spans in first paragraph
        conn.execute("INSERT INTO chapter VALUES ('b2_c1', 'b2')")
        conn.execute("INSERT INTO chapter VALUES ('b2_c2', 'b2')")
        conn.execute("INSERT INTO scene VALUES ('b2_c1_sc1')")
        conn.execute("INSERT INTO scene VALUES ('b2_c2_sc1')")
        conn.execute("INSERT INTO paragraph VALUES ('b2_c1_sc1_p1')")
        conn.execute("INSERT INTO paragraph VALUES ('b2_c2_sc1_p1')")
        conn.execute(
            "INSERT INTO span (id, span_type, instruct, text) "
            "VALUES ('b2_c1_sc1_p1_sp1', 'sentence', NULL, 'B2C1a')"
        )
        conn.execute(
            "INSERT INTO span (id, span_type, instruct, text) "
            "VALUES ('b2_c1_sc1_p1_sp2', 'sentence', NULL, 'B2C1b')"
        )
        conn.execute(
            "INSERT INTO span (id, span_type, instruct, text) "
            "VALUES ('b2_c2_sc1_p1_sp1', 'sentence', NULL, 'B2C2')"
        )

        # -- Edge tables
        # Book 1 chapters
        conn.execute(
            "INSERT INTO book_chapter (child_id, parent_id, position) VALUES ('b1_c1', 'b1', 1)"
        )
        conn.execute(
            "INSERT INTO book_chapter (child_id, parent_id, position) VALUES ('b1_c2', 'b1', 2)"
        )
        # Book 2 chapters
        conn.execute(
            "INSERT INTO book_chapter (child_id, parent_id, position) VALUES ('b2_c1', 'b2', 1)"
        )
        conn.execute(
            "INSERT INTO book_chapter (child_id, parent_id, position) VALUES ('b2_c2', 'b2', 2)"
        )

        # Chapter → Scene
        conn.execute(
            "INSERT INTO chapter_scene (child_id, parent_id, position) VALUES ('b1_c1_sc1', 'b1_c1', 1)"
        )
        conn.execute(
            "INSERT INTO chapter_scene (child_id, parent_id, position) VALUES ('b1_c2_sc1', 'b1_c2', 1)"
        )
        conn.execute(
            "INSERT INTO chapter_scene (child_id, parent_id, position) VALUES ('b2_c1_sc1', 'b2_c1', 1)"
        )
        conn.execute(
            "INSERT INTO chapter_scene (child_id, parent_id, position) VALUES ('b2_c2_sc1', 'b2_c2', 1)"
        )

        # Scene → Paragraph
        conn.execute(
            "INSERT INTO scene_paragraph (child_id, parent_id, position) VALUES ('b1_c1_sc1_p1', 'b1_c1_sc1', 1)"
        )
        conn.execute(
            "INSERT INTO scene_paragraph (child_id, parent_id, position) VALUES ('b1_c2_sc1_p1', 'b1_c2_sc1', 1)"
        )
        conn.execute(
            "INSERT INTO scene_paragraph (child_id, parent_id, position) VALUES ('b2_c1_sc1_p1', 'b2_c1_sc1', 1)"
        )
        conn.execute(
            "INSERT INTO scene_paragraph (child_id, parent_id, position) VALUES ('b2_c2_sc1_p1', 'b2_c2_sc1', 1)"
        )

        # Paragraph → Span
        conn.execute(
            "INSERT INTO paragraph_span (child_id, parent_id, position) VALUES ('b1_c1_sc1_p1_sp1', 'b1_c1_sc1_p1', 1)"
        )
        conn.execute(
            "INSERT INTO paragraph_span (child_id, parent_id, position) VALUES ('b1_c2_sc1_p1_sp1', 'b1_c2_sc1_p1', 1)"
        )
        conn.execute(
            "INSERT INTO paragraph_span (child_id, parent_id, position) VALUES ('b2_c1_sc1_p1_sp1', 'b2_c1_sc1_p1', 1)"
        )
        conn.execute(
            "INSERT INTO paragraph_span (child_id, parent_id, position) VALUES ('b2_c1_sc1_p1_sp2', 'b2_c1_sc1_p1', 2)"
        )
        conn.execute(
            "INSERT INTO paragraph_span (child_id, parent_id, position) VALUES ('b2_c2_sc1_p1_sp1', 'b2_c2_sc1_p1', 1)"
        )

        # -- Query the VIEW and verify ordering
        rows = _get_presentation_rows(conn)

        # Expected order:
        # Book 2 (position=1) comes first:
        #   b2_c1_sc1_p1_sp1 (global_index=1)
        #   b2_c1_sc1_p1_sp2 (global_index=2)
        #   b2_c2_sc1_p1_sp1 (global_index=3)
        # Book 1 (position=2) comes second:
        #   b1_c1_sc1_p1_sp1 (global_index=4)
        #   b1_c2_sc1_p1_sp1 (global_index=5)
        assert len(rows) == 5
        assert rows[0] == (1, "b2_c1_sc1_p1_sp1")
        assert rows[1] == (2, "b2_c1_sc1_p1_sp2")
        assert rows[2] == (3, "b2_c2_sc1_p1_sp1")
        assert rows[3] == (4, "b1_c1_sc1_p1_sp1")
        assert rows[4] == (5, "b1_c2_sc1_p1_sp1")


# ---------------------------------------------------------------------------
# Test 2: Presentation index stability across re-export
# ---------------------------------------------------------------------------


class TestPresentationIndexStabilityAcrossReexport:
    """Verify presentation indices are stable after modifying and re-exporting."""

    def test_presentation_index_stability_across_reexport(self, storage):
        """Export, modify a span's text, re-export — indices remain stable for unchanged spans."""
        conn = storage.get_connection()

        # -- Build a spine: 1 book, 1 chapter, 1 scene, 1 paragraph, 3 spans
        conn.execute("INSERT INTO series VALUES ('s1')")
        conn.execute(
            "INSERT INTO book (id, series_id, book_number, version, position) "
            "VALUES ('b1', 's1', 1, 1, 1)"
        )
        conn.execute("INSERT INTO chapter VALUES ('c1', 'b1')")
        conn.execute("INSERT INTO scene VALUES ('sc1')")
        conn.execute("INSERT INTO paragraph VALUES ('p1')")

        conn.execute(
            "INSERT INTO span (id, span_type, instruct, text) "
            "VALUES ('sp1', 'sentence', NULL, 'First.')"
        )
        conn.execute(
            "INSERT INTO span (id, span_type, instruct, text) "
            "VALUES ('sp2', 'sentence', NULL, 'Second.')"
        )
        conn.execute(
            "INSERT INTO span (id, span_type, instruct, text) "
            "VALUES ('sp3', 'sentence', NULL, 'Third.')"
        )

        conn.execute(
            "INSERT INTO book_chapter (child_id, parent_id, position) VALUES ('c1', 'b1', 1)"
        )
        conn.execute(
            "INSERT INTO chapter_scene (child_id, parent_id, position) VALUES ('sc1', 'c1', 1)"
        )
        conn.execute(
            "INSERT INTO scene_paragraph (child_id, parent_id, position) VALUES ('p1', 'sc1', 1)"
        )
        conn.execute(
            "INSERT INTO paragraph_span (child_id, parent_id, position) VALUES ('sp1', 'p1', 1)"
        )
        conn.execute(
            "INSERT INTO paragraph_span (child_id, parent_id, position) VALUES ('sp2', 'p1', 2)"
        )
        conn.execute(
            "INSERT INTO paragraph_span (child_id, parent_id, position) VALUES ('sp3', 'p1', 3)"
        )

        # -- Capture initial presentation indices
        initial_rows = _get_presentation_rows(conn)
        assert initial_rows == [
            (1, "sp1"),
            (2, "sp2"),
            (3, "sp3"),
        ]

        # -- Export (first time)
        script_before = export_annotated_script("b1", storage)
        assert len(script_before) == 3
        assert script_before[0]["text"] == "First."
        assert script_before[1]["text"] == "Second."
        assert script_before[2]["text"] == "Third."

        # -- Modify sp2's text (the middle span)
        conn.execute(
            "UPDATE span SET text = 'Modified second.' WHERE id = 'sp2'"
        )

        # -- Re-export
        script_after = export_annotated_script("b1", storage)
        assert len(script_after) == 3
        assert script_after[0]["text"] == "First."  # unchanged
        assert script_after[1]["text"] == "Modified second."  # changed
        assert script_after[2]["text"] == "Third."  # unchanged

        # -- Verify presentation indices are unchanged
        rows_after = _get_presentation_rows(conn)
        assert rows_after == initial_rows


# ---------------------------------------------------------------------------
# Test 3: Presentation index after split
# ---------------------------------------------------------------------------


class TestPresentationIndexAfterSplit:
    """Verify split produces correct presentation indices for both halves."""

    def test_presentation_index_after_split(self, storage, executor):
        """Split a span: original keeps its index, new span gets original+1."""
        conn = storage.get_connection()

        # -- Build a spine: 1 book, 1 chapter, 1 scene, 1 paragraph, 3 spans
        conn.execute("INSERT INTO series VALUES ('s1')")
        conn.execute(
            "INSERT INTO book (id, series_id, book_number, version, position) "
            "VALUES ('b1', 's1', 1, 1, 1)"
        )
        conn.execute("INSERT INTO chapter VALUES ('c1', 'b1')")
        conn.execute("INSERT INTO scene VALUES ('sc1')")
        conn.execute("INSERT INTO paragraph VALUES ('p1')")

        conn.execute(
            "INSERT INTO span (id, span_type, instruct, text) "
            "VALUES ('sp1', 'sentence', NULL, 'First.')"
        )
        conn.execute(
            "INSERT INTO span (id, span_type, instruct, text) "
            "VALUES ('sp2', 'sentence', NULL, 'Hello world.')"
        )
        conn.execute(
            "INSERT INTO span (id, span_type, instruct, text) "
            "VALUES ('sp3', 'sentence', NULL, 'Third.')"
        )

        conn.execute(
            "INSERT INTO book_chapter (child_id, parent_id, position) VALUES ('c1', 'b1', 1)"
        )
        conn.execute(
            "INSERT INTO chapter_scene (child_id, parent_id, position) VALUES ('sc1', 'c1', 1)"
        )
        conn.execute(
            "INSERT INTO scene_paragraph (child_id, parent_id, position) VALUES ('p1', 'sc1', 1)"
        )
        conn.execute(
            "INSERT INTO paragraph_span (child_id, parent_id, position) VALUES ('sp1', 'p1', 1)"
        )
        conn.execute(
            "INSERT INTO paragraph_span (child_id, parent_id, position) VALUES ('sp2', 'p1', 2)"
        )
        conn.execute(
            "INSERT INTO paragraph_span (child_id, parent_id, position) VALUES ('sp3', 'p1', 3)"
        )

        # -- Initial presentation: sp1=1, sp2=2, sp3=3
        initial_rows = _get_presentation_rows(conn)
        assert initial_rows == [(1, "sp1"), (2, "sp2"), (3, "sp3")]

        # -- Split sp2 (presentation_index=2) at offset 5 ("Hello" | " world.")
        executor.execute_split(book_id="b1", presentation_index=2, split_point=5)

        # -- Verify: 4 spans now
        rows_after = _get_presentation_rows(conn)
        assert len(rows_after) == 4

        # sp1 still at index 1
        assert rows_after[0] == (1, "sp1")
        # sp2 (left half) still at index 2
        assert rows_after[1] == (2, "sp2")
        # New span at index 3 (original sp2's index + 1)
        new_span_id = rows_after[2][1]
        assert rows_after[2] == (3, new_span_id)
        assert new_span_id not in ("sp1", "sp2", "sp3")
        # sp3 shifted to index 4 (was 3)
        assert rows_after[3] == (4, "sp3")

        # -- Verify text split
        left_text = conn.execute(
            "SELECT text FROM span WHERE id = 'sp2'"
        ).fetchone()[0]
        right_text = conn.execute(
            "SELECT text FROM span WHERE id = ?", (new_span_id,)
        ).fetchone()[0]
        assert left_text == "Hello"
        assert right_text == " world."


# ---------------------------------------------------------------------------
# Test 4: Presentation index after merge
# ---------------------------------------------------------------------------


class TestPresentationIndexAfterMerge:
    """Verify merge produces correct presentation index and shifts remaining spans."""

    def test_presentation_index_after_merge(self, storage, executor):
        """Merge adjacent spans: merged span gets min of original indices, rest shift down."""
        conn = storage.get_connection()

        # -- Build a spine: 1 book, 1 chapter, 1 scene, 1 paragraph, 4 spans
        conn.execute("INSERT INTO series VALUES ('s1')")
        conn.execute(
            "INSERT INTO book (id, series_id, book_number, version, position) "
            "VALUES ('b1', 's1', 1, 1, 1)"
        )
        conn.execute("INSERT INTO chapter VALUES ('c1', 'b1')")
        conn.execute("INSERT INTO scene VALUES ('sc1')")
        conn.execute("INSERT INTO paragraph VALUES ('p1')")

        conn.execute(
            "INSERT INTO span (id, span_type, instruct, text) "
            "VALUES ('sp1', 'sentence', NULL, 'First.')"
        )
        conn.execute(
            "INSERT INTO span (id, span_type, instruct, text) "
            "VALUES ('sp2', 'sentence', NULL, 'Second.')"
        )
        conn.execute(
            "INSERT INTO span (id, span_type, instruct, text) "
            "VALUES ('sp3', 'sentence', NULL, 'Third.')"
        )
        conn.execute(
            "INSERT INTO span (id, span_type, instruct, text) "
            "VALUES ('sp4', 'sentence', NULL, 'Fourth.')"
        )

        conn.execute(
            "INSERT INTO book_chapter (child_id, parent_id, position) VALUES ('c1', 'b1', 1)"
        )
        conn.execute(
            "INSERT INTO chapter_scene (child_id, parent_id, position) VALUES ('sc1', 'c1', 1)"
        )
        conn.execute(
            "INSERT INTO scene_paragraph (child_id, parent_id, position) VALUES ('p1', 'sc1', 1)"
        )
        conn.execute(
            "INSERT INTO paragraph_span (child_id, parent_id, position) VALUES ('sp1', 'p1', 1)"
        )
        conn.execute(
            "INSERT INTO paragraph_span (child_id, parent_id, position) VALUES ('sp2', 'p1', 2)"
        )
        conn.execute(
            "INSERT INTO paragraph_span (child_id, parent_id, position) VALUES ('sp3', 'p1', 3)"
        )
        conn.execute(
            "INSERT INTO paragraph_span (child_id, parent_id, position) VALUES ('sp4', 'p1', 4)"
        )

        # -- Initial presentation: sp1=1, sp2=2, sp3=3, sp4=4
        initial_rows = _get_presentation_rows(conn)
        assert initial_rows == [
            (1, "sp1"),
            (2, "sp2"),
            (3, "sp3"),
            (4, "sp4"),
        ]

        # -- Merge sp2 (index=2) and sp3 (index=3) into one span
        executor.execute_merge(book_id="b1", presentation_index_left=2, presentation_index_right=3)

        # -- Verify: 3 spans now
        rows_after = _get_presentation_rows(conn)
        assert len(rows_after) == 3

        # sp1 still at index 1
        assert rows_after[0] == (1, "sp1")
        # Merged span (sp2, the left one) at index 2 (min of 2 and 3)
        assert rows_after[1] == (2, "sp2")
        # sp4 shifted down to index 3 (was 4)
        assert rows_after[2] == (3, "sp4")

        # -- Verify sp3 is deleted
        sp3_row = conn.execute(
            "SELECT COUNT(*) FROM span WHERE id = 'sp3'"
        ).fetchone()
        assert sp3_row[0] == 0

        # -- Verify sp2 still exists (it's the merged span)
        sp2_row = conn.execute(
            "SELECT COUNT(*) FROM span WHERE id = 'sp2'"
        ).fetchone()
        assert sp2_row[0] == 1
