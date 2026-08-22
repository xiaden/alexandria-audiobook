"""Operation executor for the audiobook pipeline.

Provides ``OperationExecutor`` which implements split/merge/move/delete
operations on spans using presentation indices.  Two resolution paths exist:

* **Book-scoped path** — when ``book_id`` is provided, a per-book
  ``ROW_NUMBER()`` query (``_BOOK_SPAN_POSITION_SQL``) computes presentation
  order at call time, scoped to a single book.
* **Legacy path** — when ``book_id`` is ``None``, the ``span_presentation``
  VIEW is used (global, unfiltered).

All operations use a single connection + SAVEPOINT for atomicity and employ
the negative-space two-phase reindex to avoid UNIQUE(parent_id, position)
constraint violations during position renumbering.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable
from functools import wraps
from typing import TYPE_CHECKING, Concatenate, ParamSpec, TypeVar

if TYPE_CHECKING:
    from app.pipeline.adapter import PipelineStorage

P = ParamSpec("P")
R = TypeVar("R")


def _guarded_operation(
    operation: Callable[Concatenate[OperationExecutor, P], R],
) -> Callable[Concatenate[OperationExecutor, P], R]:
    """Run a structural operation under the storage transaction guard."""

    @wraps(operation)
    def guarded(self: OperationExecutor, *args: P.args, **kwargs: P.kwargs) -> R:
        with self._storage.transaction():
            return operation(self, *args, **kwargs)

    return guarded


_BOOK_SPAN_POSITION_SQL = """
SELECT span.id AS span_id,
       span_edge.parent_id,
       span_edge.position,
       ROW_NUMBER() OVER (
           ORDER BY book.position,
                    chapter_edge.position,
                    scene_edge.position,
                    paragraph_edge.position,
                    span_edge.position
       ) AS global_index
FROM span
JOIN paragraph_span AS span_edge ON span.id = span_edge.child_id
JOIN scene_paragraph AS paragraph_edge ON span_edge.parent_id = paragraph_edge.child_id
JOIN chapter_scene AS scene_edge ON paragraph_edge.parent_id = scene_edge.child_id
JOIN book_chapter AS chapter_edge ON scene_edge.parent_id = chapter_edge.child_id
JOIN book ON chapter_edge.parent_id = book.id
WHERE book.id = ?
"""


def get_book_span_position(
    conn: sqlite3.Connection, book_id: str, presentation_index: int
) -> tuple[str, str, int]:
    """Resolve a presentation index to (span_id, parent_id, position) within a book.

    Uses ROW_NUMBER() over the full presentation-order join filtered by book.id.
    SQLite does not support parameterised VIEWs, so this is evaluated per-call.

    Returns (span_id, parent_id, position).
    Raises ValueError if the presentation_index is not found for this book.
    """
    row = conn.execute(
        f"SELECT span_id, parent_id, position FROM ({_BOOK_SPAN_POSITION_SQL}) "
        "WHERE global_index = ?",
        (book_id, presentation_index),
    ).fetchone()
    if row is None:
        raise ValueError(
            f"Presentation index {presentation_index} not found in book {book_id}"
        )
    return (row[0], row[1], row[2])


class OperationExecutor:
    """Executes structural operations on the document spine.

    All operations use presentation indices.  When ``book_id`` is provided
    (book-scoped path), a per-book ``ROW_NUMBER()`` query computes presentation
    order at call time.  When ``book_id`` is ``None`` (legacy path), the
    ``span_presentation`` VIEW is used.  The LLM emits intent on indices;
    this executor performs the assembly.

    Parameters
    ----------
    storage:
        A ``PipelineStorage`` instance providing database access.
    """

    def __init__(self, storage: PipelineStorage) -> None:
        self._storage = storage

    # -----------------------------------------------------------------------
    # Helper methods
    # -----------------------------------------------------------------------

    def _get_span_position(
        self,
        conn: sqlite3.Connection,
        presentation_index: int,
        *,
        book_id: str | None = None,
    ) -> tuple[str, str, int]:
        """Resolve a presentation index to (span_id, parent_id, position).

        Parameters
        ----------
        conn:
            Active SQLite connection.
        presentation_index:
            1-based presentation order index.
        book_id:
            When provided, resolves the index within the given book using the
            book-scoped ``ROW_NUMBER()`` query.  When ``None``, falls back to
            the legacy ``span_presentation`` VIEW (global).

        Returns
        -------
        tuple[str, str, int]
            (span_id, parent_id, position)

        Raises
        ------
        ValueError
            If the presentation_index does not exist.
        """
        if book_id is not None:
            return get_book_span_position(conn, book_id, presentation_index)

        row = conn.execute(
            "SELECT id FROM span_presentation WHERE global_index = ?",
            (presentation_index,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Presentation index {presentation_index} not found")
        span_id = row[0]

        edge_row = conn.execute(
            "SELECT parent_id, position FROM paragraph_span WHERE child_id = ?",
            (span_id,),
        ).fetchone()
        if edge_row is None:
            raise ValueError(f"Span {span_id} has no paragraph_span edge")
        parent_id = edge_row[0]
        position = edge_row[1]

        return span_id, parent_id, position

    def _two_phase_reindex(
        self,
        conn: sqlite3.Connection,
        table_name: str,
        parent_id: str,
        start_position: int,
        delta: int,
    ) -> None:
        """Shift positions by delta for all rows with position > start_position.

        Uses negative-space two-phase reindex to avoid UNIQUE constraint violations:
        1. First pass: positions become negative (-(position + delta))
        2. Second pass: negative positions become positive (ABS)

        Parameters
        ----------
        table_name:
            Name of the edge table (e.g., 'paragraph_span')
        parent_id:
            Parent ID to filter on
        start_position:
            Only shift positions strictly greater than this
        delta:
            Amount to shift (positive = up, negative = down)
        """
        if delta == 0:
            return

        # Phase 1: convert to negative space
        conn.execute(
            f"UPDATE {table_name} SET position = -(position + ?) "
            f"WHERE parent_id = ? AND position > ?",
            (delta, parent_id, start_position),
        )
        # Phase 2: convert back to positive
        conn.execute(
            f"UPDATE {table_name} SET position = ABS(position) "
            f"WHERE parent_id = ? AND position < 0",
            (parent_id,),
        )

    def _shift_positions_range(
        self,
        conn: sqlite3.Connection,
        table_name: str,
        parent_id: str,
        start_pos: int,
        end_pos: int,
        delta: int,
    ) -> None:
        """Shift positions by delta for rows with position in [start_pos, end_pos].

        Uses negative-space two-phase reindex to avoid UNIQUE constraint violations.

        Parameters
        ----------
        table_name:
            Name of the edge table
        parent_id:
            Parent ID to filter on
        start_pos:
            Start of range (inclusive)
        end_pos:
            End of range (inclusive)
        delta:
            Amount to shift (positive = up, negative = down)
        """
        if delta == 0 or start_pos > end_pos:
            return

        # Phase 1: convert to negative space
        conn.execute(
            f"UPDATE {table_name} SET position = -(position + ?) "
            f"WHERE parent_id = ? AND position >= ? AND position <= ?",
            (delta, parent_id, start_pos, end_pos),
        )
        # Phase 2: convert back to positive
        conn.execute(
            f"UPDATE {table_name} SET position = ABS(position) "
            f"WHERE parent_id = ? AND position < 0",
            (parent_id,),
        )

    # -----------------------------------------------------------------------
    # Operations
    # -----------------------------------------------------------------------

    @_guarded_operation
    def execute_split(
        self, book_id: str, presentation_index: int, split_point: int
    ) -> None:
        """Split a span into two at the given text offset.

        The left span (original) keeps existing character_span memberships
        and its instruct value.  The right span (new) gets a copy of all
        character_span memberships and NULL instruct.

        Parameters
        ----------
        book_id:
            Book that scopes the presentation index.
        presentation_index:
            Index from span_presentation (book-scoped when *book_id* is given).
        split_point:
            Character offset at which to split.  Must be a strict interior
            offset: ``0 < split_point < len(span.text)``.

        Raises
        ------
        ValueError
            If *split_point* is out of range or the span has NULL text.
        """
        conn = self._storage.get_connection()
        conn.execute("SAVEPOINT split_op")
        try:
            # Resolve span and position
            span_id, parent_id, old_position = self._get_span_position(
                conn, presentation_index, book_id=book_id
            )

            # Get span details including text and instruct
            span_row = conn.execute(
                "SELECT span_type, instruct, text FROM span WHERE id = ?",
                (span_id,),
            ).fetchone()
            if span_row is None:
                raise ValueError(f"Span {span_id} not found")
            span_type, _instruct, span_text = span_row

            # Validate split_point as a strict interior offset
            if span_text is None:
                raise ValueError(f"Cannot split span {span_id}: text is NULL")
            if split_point <= 0 or split_point >= len(span_text):
                raise ValueError(
                    f"split_point {split_point} must satisfy "
                    f"0 < split_point < {len(span_text)} "
                    f"for span {span_id}"
                )

            # Split the text
            left_text = span_text[:split_point]
            right_text = span_text[split_point:]

            # Update original span with left text
            conn.execute(
                "UPDATE span SET text = ? WHERE id = ?",
                (left_text, span_id),
            )

            # Phase 1: shift positions > old_position up by 1
            self._two_phase_reindex(
                conn, "paragraph_span", parent_id, old_position, delta=1
            )

            # Create new span with right text
            new_span_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO span (id, span_type, instruct, text) "
                "VALUES (?, ?, NULL, ?)",
                (new_span_id, span_type, right_text),
            )

            # Insert new paragraph_span edge at old_position + 1
            conn.execute(
                "INSERT INTO paragraph_span (child_id, parent_id, position) "
                "VALUES (?, ?, ?)",
                (new_span_id, parent_id, old_position + 1),
            )

            # Copy character_span memberships from original to new span
            conn.execute(
                "INSERT INTO character_span (character_id, span_id, relation_type, "
                "source, confidence, human_override) "
                "SELECT character_id, ?, relation_type, source, confidence, human_override "
                "FROM character_span WHERE span_id = ?",
                (new_span_id, span_id),
            )

            conn.execute("RELEASE SAVEPOINT split_op")
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT split_op")
            conn.execute("RELEASE SAVEPOINT split_op")
            raise

    @_guarded_operation
    def execute_merge(
        self,
        book_id: str,
        presentation_index_left: int,
        presentation_index_right: int,
    ) -> None:
        """Merge two adjacent spans into one.

        Combines character_span memberships (union). For duplicate
        (character_id, relation_type) combos, keeps the one with higher confidence.

        Parameters
        ----------
        book_id:
            Book that scopes the presentation indices.
        presentation_index_left:
            Index of the left span
        presentation_index_right:
            Index of the right span (must be adjacent to left)

        Raises
        ------
        ValueError
            If the spans are not adjacent or have different parents
        """
        conn = self._storage.get_connection()
        conn.execute("SAVEPOINT merge_op")
        # Defer FK constraint checking until RELEASE
        conn.execute("PRAGMA defer_foreign_keys = ON")
        try:
            # Resolve both spans
            left_span_id, left_parent_id, left_position = self._get_span_position(
                conn, presentation_index_left, book_id=book_id
            )
            right_span_id, right_parent_id, right_position = self._get_span_position(
                conn, presentation_index_right, book_id=book_id
            )

            # Verify same parent
            if left_parent_id != right_parent_id:
                raise ValueError(
                    f"Cannot merge spans with different parents: "
                    f"{left_parent_id} vs {right_parent_id}"
                )

            # Verify adjacency
            if left_position + 1 != right_position:
                raise ValueError(
                    f"Cannot merge non-adjacent spans: positions {left_position} "
                    f"and {right_position} are not consecutive"
                )

            # Get all memberships for both spans
            left_memberships = conn.execute(
                "SELECT character_id, relation_type, source, confidence, human_override "
                "FROM character_span WHERE span_id = ?",
                (left_span_id,),
            ).fetchall()

            right_memberships = conn.execute(
                "SELECT character_id, relation_type, source, confidence, human_override "
                "FROM character_span WHERE span_id = ?",
                (right_span_id,),
            ).fetchall()

            # Preserve the content of both spans on the surviving left span.
            left_text = conn.execute(
                "SELECT text FROM span WHERE id = ?", (left_span_id,)
            ).fetchone()[0]
            right_text = conn.execute(
                "SELECT text FROM span WHERE id = ?", (right_span_id,)
            ).fetchone()[0]
            if left_text is None:
                merged_text = right_text
            elif right_text is None:
                merged_text = left_text
            else:
                merged_text = left_text + right_text
            conn.execute(
                "UPDATE span SET text = ? WHERE id = ?",
                (merged_text, left_span_id),
            )

            # Build union with confidence tiebreak
            # Key: (character_id, relation_type), Value: (source, confidence, human_override)
            membership_map: dict[tuple[str, str], tuple[str, float, int]] = {}

            for row in left_memberships:
                char_id, rel_type, source, conf, override = row
                membership_map[(char_id, rel_type)] = (source, conf, override)

            for row in right_memberships:
                char_id, rel_type, source, conf, override = row
                key = (char_id, rel_type)
                if key not in membership_map or conf > membership_map[key][1]:
                    membership_map[key] = (source, conf, override)

            # Delete left span's old memberships
            conn.execute(
                "DELETE FROM character_span WHERE span_id = ?", (left_span_id,)
            )

            # Insert the union into left span (before deleting right span)
            for (char_id, rel_type), (source, conf, override) in membership_map.items():
                conn.execute(
                    "INSERT INTO character_span (character_id, span_id, relation_type, "
                    "source, confidence, human_override) VALUES (?, ?, ?, ?, ?, ?)",
                    (char_id, left_span_id, rel_type, source, conf, override),
                )

            # Delete right span's character_span memberships
            conn.execute(
                "DELETE FROM character_span WHERE span_id = ?", (right_span_id,)
            )

            # Delete right span's paragraph_span edge
            conn.execute(
                "DELETE FROM paragraph_span WHERE child_id = ?", (right_span_id,)
            )

            # Delete right span
            conn.execute("DELETE FROM span WHERE id = ?", (right_span_id,))

            # Shift positions > right_position down by 1
            self._two_phase_reindex(
                conn, "paragraph_span", left_parent_id, right_position, delta=-1
            )

            conn.execute("RELEASE SAVEPOINT merge_op")
            # Reset FK defer setting
            conn.execute("PRAGMA defer_foreign_keys = OFF")
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT merge_op")
            conn.execute("PRAGMA defer_foreign_keys = OFF")
            raise

    @_guarded_operation
    def execute_move(
        self,
        book_id: str,
        presentation_index_from: int,
        presentation_index_to: int,
    ) -> None:
        """Move a span to a new position within the same parent paragraph.

        Parameters
        ----------
        book_id:
            Book that scopes the presentation indices.
        presentation_index_from:
            Current index of the span
        presentation_index_to:
            Target index (must be within same parent paragraph)

        Raises
        ------
        ValueError
            If the target position is in a different parent paragraph
        """
        conn = self._storage.get_connection()
        conn.execute("SAVEPOINT move_op")
        try:
            # Resolve source span
            span_id, parent_id, old_position = self._get_span_position(
                conn, presentation_index_from, book_id=book_id
            )

            # Resolve target position
            _target_span_id, target_parent_id, target_position = (
                self._get_span_position(conn, presentation_index_to, book_id=book_id)
            )

            # Verify same parent
            if parent_id != target_parent_id:
                raise ValueError(
                    f"Cannot move span to different parent: {parent_id} vs {target_parent_id}"
                )

            # No-op if same position
            if old_position == target_position:
                conn.execute("RELEASE SAVEPOINT move_op")
                return

            # Temporarily set source span to a position outside the range to avoid conflicts
            temp_position = -999
            conn.execute(
                "UPDATE paragraph_span SET position = ? WHERE child_id = ?",
                (temp_position, span_id),
            )

            if old_position < target_position:
                # Moving forward: shift positions in (old, target] down by 1
                self._shift_positions_range(
                    conn,
                    "paragraph_span",
                    parent_id,
                    old_position + 1,
                    target_position,
                    delta=-1,
                )
            else:
                # Moving backward: shift positions in [target, old) up by 1
                self._shift_positions_range(
                    conn,
                    "paragraph_span",
                    parent_id,
                    target_position,
                    old_position - 1,
                    delta=1,
                )

            # Update span's position to final target
            conn.execute(
                "UPDATE paragraph_span SET position = ? WHERE child_id = ?",
                (target_position, span_id),
            )

            conn.execute("RELEASE SAVEPOINT move_op")
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT move_op")
            conn.execute("RELEASE SAVEPOINT move_op")
            raise

    @_guarded_operation
    def execute_delete(self, book_id: str, presentation_index: int) -> None:
        """Remove a span and its memberships, renumbering positions.

        Parameters
        ----------
        book_id:
            Book that scopes the presentation index.
        presentation_index:
            Index of the span to delete
        """
        conn = self._storage.get_connection()
        conn.execute("SAVEPOINT delete_op")
        try:
            # Resolve span and position
            span_id, parent_id, position = self._get_span_position(
                conn, presentation_index, book_id=book_id
            )

            # Delete character_span memberships
            conn.execute("DELETE FROM character_span WHERE span_id = ?", (span_id,))

            # Delete paragraph_span edge
            conn.execute("DELETE FROM paragraph_span WHERE child_id = ?", (span_id,))

            # Delete span
            conn.execute("DELETE FROM span WHERE id = ?", (span_id,))

            # Shift positions > position down by 1
            self._two_phase_reindex(
                conn, "paragraph_span", parent_id, position, delta=-1
            )

            conn.execute("RELEASE SAVEPOINT delete_op")
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT delete_op")
            conn.execute("RELEASE SAVEPOINT delete_op")
            raise
