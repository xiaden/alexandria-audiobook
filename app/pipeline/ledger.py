"""CharacterLedger — query layer for character graph data.

Provides read-only access to characters, their junction records, and review
items (low-confidence / uncertain annotations) that need human review.

Usage::

    from app.pipeline.adapter import PipelineStorage
    from app.pipeline.ledger import CharacterLedger

    storage: PipelineStorage = ...
    ledger = CharacterLedger(storage)
    chars = ledger.get_characters_for_book("book-001")
"""

from __future__ import annotations

from typing import Any

from app.pipeline.adapter import PipelineStorage


class CharacterLedger:
    """Read-only query interface over the character graph.

    Parameters
    ----------
    storage:
        An active ``PipelineStorage`` implementation (e.g. ``SQLiteAdapter``
        or ``InMemorySQLiteAdapter``).
    """

    def __init__(self, storage: PipelineStorage) -> None:
        self._storage = storage

    # ------------------------------------------------------------------
    # Character retrieval
    # ------------------------------------------------------------------

    def get_characters_for_book(self, book_id: str) -> list[dict[str, Any]]:
        """Return all characters associated with *book_id* via the
        ``character_book`` junction table.

        Each row includes ``id``, ``name``, ``aliases`` (JSON string), and
        ``confidence`` from the junction.
        """
        return self._storage.execute_query(
            """SELECT c.id, c.name, c.aliases, cb.confidence
                 FROM character c
                 JOIN character_book cb ON c.id = cb.character_id
                WHERE cb.book_id = ?
                ORDER BY c.name""",
            (book_id,),
        )

    def get_characters_for_scene(self, scene_id: str) -> list[dict[str, Any]]:
        """Return all characters present in *scene_id* via the
        ``character_scene`` junction table.

        Each row includes ``id``, ``name``, ``aliases``, ``relation_type``,
        and ``confidence``.
        """
        return self._storage.execute_query(
            """SELECT c.id, c.name, c.aliases, cs.relation_type, cs.confidence
                 FROM character c
                 JOIN character_scene cs ON c.id = cs.character_id
                WHERE cs.scene_id = ?
                ORDER BY c.name""",
            (scene_id,),
        )

    def get_characters_for_span(self, span_id: str) -> list[dict[str, Any]]:
        """Return all characters associated with *span_id* via the
        ``character_span`` junction table.

        Each row includes ``id``, ``name``, ``aliases``, ``relation_type``,
        and ``confidence``.
        """
        return self._storage.execute_query(
            """SELECT c.id, c.name, c.aliases, cs.relation_type, cs.confidence
                 FROM character c
                 JOIN character_span cs ON c.id = cs.character_id
                WHERE cs.span_id = ?
                ORDER BY c.name""",
            (span_id,),
        )

    # ------------------------------------------------------------------
    # Review items
    # ------------------------------------------------------------------

    def get_review_items(
        self,
        book_id: str,
        walk_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return low-confidence or uncertain annotations that need human
        review.

        Parameters
        ----------
        book_id:
            The book to query annotations for.
        walk_name:
            Optional filter — restrict results to a specific walk (e.g.
            ``"walk_2b_character_discovery"`` or
            ``"walk_2c_alias_resolution"``).

        Returns
        -------
        list[dict]
            Each item describes one review candidate with keys such as
            ``character_id``, ``character_name``, ``junction_table``,
            ``confidence``, ``walk_name``, and ``reason``.
        """
        # Collect all active walk names from review-marked entries.
        # review_items records are annotations with confidence in the
        # 0.5-0.7 "uncertain" band; these are the ones flagged for review.
        walk_filter = ""
        params: list[Any] = [book_id]

        if walk_name is not None:
            walk_filter = " AND source LIKE ?"
            params.append(f"%{walk_name}%")

        # Collect review candidates from character_book (low-confidence)
        items: list[dict[str, Any]] = self._storage.execute_query(
            f"""SELECT c.id AS character_id, c.name AS character_name,
                       'character_book' AS junction_table,
                       cb.confidence, cb.source AS walk_name,
                       'Low-confidence character-book association' AS reason
                  FROM character c
                  JOIN character_book cb ON c.id = cb.character_id
                 WHERE cb.book_id = ?
                   AND cb.confidence >= 0.5
                   AND cb.confidence < 0.7
                   {walk_filter}
                 ORDER BY cb.confidence""",
            tuple(params),
        )

        return items
