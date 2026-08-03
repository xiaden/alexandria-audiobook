"""ReviewManager — confidence review queue for the audiobook pipeline.

Manages the human review workflow for low-confidence junction records
(confidence in the [0.5, 0.7) band).  Supports accept, reject, and
override actions across all four character junction tables.

Usage::

    from app.pipeline.adapter import PipelineStorage
    from app.pipeline.review import ReviewManager

    storage: PipelineStorage = ...
    manager = ReviewManager(storage)
    items = manager.get_review_items("book-001")
    manager.accept_review_item("character_book:c1:b1")
"""

from __future__ import annotations

from typing import Any

from app.pipeline.adapter import PipelineStorage
from app.pipeline.ledger import CharacterLedger


# ---------------------------------------------------------------------------
# Confidence thresholds
# ---------------------------------------------------------------------------

#: Minimum confidence (inclusive) for items to appear in the review queue.
REVIEW_CONFIDENCE_MIN = 0.5
#: Maximum confidence (exclusive) for items to appear in the review queue.
REVIEW_CONFIDENCE_MAX = 0.7
#: Confidence value set when a review item is accepted.
CONFIDENCE_ACCEPTED = 1.0
#: Confidence value set when a review item is rejected.
CONFIDENCE_REJECTED = 0.0


# ---------------------------------------------------------------------------
# Item-ID helpers
# ---------------------------------------------------------------------------

# Format: "{junction_table}:{character_id}:{related_entity_id}"
# Examples:
#   "character_book:c1:b1"
#   "character_scene:c1:sc1"
#   "character_span:c1:sp1"
#   "character_series:c1:s1"

_VALID_JUNCTION_TABLES = frozenset(
    {"character_book", "character_scene", "character_span", "character_series"}
)

# Maps junction table → (related_entity_column, table PK columns for UPDATE)
_JUNCTION_META: dict[str, dict[str, Any]] = {
    "character_book": {"related_col": "book_id", "extra_cols": []},
    "character_scene": {
        "related_col": "scene_id",
        "extra_cols": ["relation_type"],
    },
    "character_span": {
        "related_col": "span_id",
        "extra_cols": ["relation_type"],
    },
    "character_series": {"related_col": "series_id", "extra_cols": []},
}


def _parse_item_id(item_id: str) -> tuple[str, str, str]:
    """Parse an item_id into (junction_table, character_id, related_entity_id).

    Raises ``ValueError`` if the format is invalid.
    """
    parts = item_id.split(":")
    if len(parts) != 3:
        raise ValueError(
            f"Invalid item_id format: {item_id!r}. "
            f"Expected '{{table}}:{{character_id}}:{{entity_id}}'"
        )
    junction_table, character_id, related_entity_id = parts
    if junction_table not in _VALID_JUNCTION_TABLES:
        raise ValueError(
            f"Unknown junction table: {junction_table!r}. "
            f"Must be one of {sorted(_VALID_JUNCTION_TABLES)}"
        )
    return junction_table, character_id, related_entity_id


def _make_item_id(junction_table: str, character_id: str, related_entity_id: str) -> str:
    """Build an item_id from its components."""
    return f"{junction_table}:{character_id}:{related_entity_id}"


class ReviewManager:
    """Manage the confidence review queue for character junction records.

    Parameters
    ----------
    storage:
        An active ``PipelineStorage`` implementation.
    """

    def __init__(self, storage: PipelineStorage) -> None:
        self._storage = storage
        self._ledger = CharacterLedger(storage)

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    def get_review_items(
        self,
        book_id: str,
        walk_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return all junction records with confidence in [0.5, 0.7).

        Queries three junction tables: ``character_book``,
        ``character_scene``, and ``character_span``.  Each returned dict
        includes an ``item_id`` key that encodes the junction table and
        primary key for use with accept/reject/override.

        Parameters
        ----------
        book_id:
            The book whose review queue to retrieve.
        walk_name:
            Optional filter — restrict results to entries whose ``source``
            column contains *walk_name* (LIKE ``%walk_name%`` heuristic).
        """
        items: list[dict[str, Any]] = []

        # 1) character_book — delegate to CharacterLedger (existing logic)
        ledger_items = self._ledger.get_review_items(book_id, walk_name=walk_name)
        for row in ledger_items:
            row["related_entity_id"] = book_id
            row["item_id"] = _make_item_id(
                "character_book", row["character_id"], book_id
            )
            items.append(row)

        # 2) character_scene — scenes belonging to this book
        items.extend(self._get_scene_review_items(book_id, walk_name))

        # 3) character_span — spans belonging to this book
        items.extend(self._get_span_review_items(book_id, walk_name))

        return items

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def accept_review_item(self, item_id: str) -> None:
        """Accept a review item — set confidence to 1.0.

        Parameters
        ----------
        item_id:
            Encoded as ``"{junction_table}:{character_id}:{entity_id}"``.
        """
        junction_table, character_id, related_entity_id = _parse_item_id(item_id)
        meta = _JUNCTION_META[junction_table]
        related_col = meta["related_col"]

        self._storage.execute_update(
            f"UPDATE {junction_table} SET confidence = {CONFIDENCE_ACCEPTED} "
            f"WHERE character_id = ? AND {related_col} = ?",
            (character_id, related_entity_id),
        )

    def reject_review_item(self, item_id: str) -> None:
        """Reject a review item — set confidence to 0.0 and mark
        ``human_override = 1``.

        Parameters
        ----------
        item_id:
            Encoded as ``"{junction_table}:{character_id}:{entity_id}"``.
        """
        junction_table, character_id, related_entity_id = _parse_item_id(item_id)
        meta = _JUNCTION_META[junction_table]
        related_col = meta["related_col"]

        self._storage.execute_update(
            f"UPDATE {junction_table} SET confidence = {CONFIDENCE_REJECTED}, human_override = 1 "
            f"WHERE character_id = ? AND {related_col} = ?",
            (character_id, related_entity_id),
        )

    def override_review_item(
        self,
        item_id: str,
        new_value: Any,
    ) -> None:
        """Override a review item — set ``human_override = 1``,
        ``confidence = 1.0``, and optionally update junction columns.

        Parameters
        ----------
        item_id:
            Encoded as ``"{junction_table}:{character_id}:{entity_id}"``.
        new_value:
            If a ``dict``, its keys are treated as column names to update
            on the junction row (e.g. ``{"relation_type": "speaker"}``).
            Non-dict values are ignored (only dict overrides are
            supported for column updates).
        """
        junction_table, character_id, related_entity_id = _parse_item_id(item_id)
        meta = _JUNCTION_META[junction_table]
        related_col = meta["related_col"]
        allowed_extra = set(meta["extra_cols"])

        # Build SET clause
        set_parts: list[str] = [f"confidence = {CONFIDENCE_ACCEPTED}", "human_override = 1"]
        params: list[Any] = []

        if isinstance(new_value, dict):
            for col, val in new_value.items():
                if col in allowed_extra:
                    set_parts.append(f"{col} = ?")
                    params.append(val)
                # Silently ignore unknown columns to avoid SQL injection

        params.extend([character_id, related_entity_id])

        self._storage.execute_update(
            f"UPDATE {junction_table} SET {', '.join(set_parts)} "
            f"WHERE character_id = ? AND {related_col} = ?",
            tuple(params),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_scene_review_items(
        self,
        book_id: str,
        walk_name: str | None,
    ) -> list[dict[str, Any]]:
        """Query character_scene junctions for scenes belonging to *book_id*."""
        walk_filter = ""
        params: list[Any] = [book_id]

        if walk_name is not None:
            walk_filter = " AND cs.source LIKE ?"
            params.append(f"%{walk_name}%")

        rows = self._storage.execute_query(
            f"""SELECT c.id AS character_id, c.name AS character_name,
                       'character_scene' AS junction_table,
                       cs.confidence, cs.source AS walk_name,
                       cs.scene_id AS related_entity_id,
                       'Low-confidence character-scene association' AS reason
                  FROM character c
                  JOIN character_scene cs ON c.id = cs.character_id
                  JOIN chapter_scene csc ON cs.scene_id = csc.child_id
                  JOIN book_chapter bc ON csc.parent_id = bc.child_id
                 WHERE bc.parent_id = ?
                    AND cs.confidence >= {REVIEW_CONFIDENCE_MIN}
                    AND cs.confidence < {REVIEW_CONFIDENCE_MAX}
                   {walk_filter}
                 ORDER BY cs.confidence""",
            tuple(params),
        )

        for row in rows:
            row["item_id"] = _make_item_id(
                "character_scene", row["character_id"], row["related_entity_id"]
            )
        return rows

    def _get_span_review_items(
        self,
        book_id: str,
        walk_name: str | None,
    ) -> list[dict[str, Any]]:
        """Query character_span junctions for spans belonging to *book_id*."""
        walk_filter = ""
        params: list[Any] = [book_id]

        if walk_name is not None:
            walk_filter = " AND csp.source LIKE ?"
            params.append(f"%{walk_name}%")

        rows = self._storage.execute_query(
            f"""SELECT c.id AS character_id, c.name AS character_name,
                       'character_span' AS junction_table,
                       csp.confidence, csp.source AS walk_name,
                       csp.span_id AS related_entity_id,
                       'Low-confidence character-span association' AS reason
                  FROM character c
                  JOIN character_span csp ON c.id = csp.character_id
                  JOIN paragraph_span pse ON csp.span_id = pse.child_id
                  JOIN scene_paragraph spe ON pse.parent_id = spe.child_id
                  JOIN chapter_scene csc ON spe.parent_id = csc.child_id
                  JOIN book_chapter bc ON csc.parent_id = bc.child_id
                 WHERE bc.parent_id = ?
                    AND csp.confidence >= {REVIEW_CONFIDENCE_MIN}
                    AND csp.confidence < {REVIEW_CONFIDENCE_MAX}
                   {walk_filter}
                 ORDER BY csp.confidence""",
            tuple(params),
        )

        for row in rows:
            row["item_id"] = _make_item_id(
                "character_span", row["character_id"], row["related_entity_id"]
            )
        return rows
