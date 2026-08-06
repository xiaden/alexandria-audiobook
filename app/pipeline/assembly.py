"""Deterministic assembly — export annotated scripts for TTS rendering.

Provides ``export_annotated_script`` which walks the document spine in
presentation order and resolves each span's speaker via the
``character_span`` junction (``relation_type = 'speaker'``).  Spans with
no speaker attribution are presented as ``'NARRATOR'``.

Also provides re-onboarding utilities:

* ``reonboard_book`` — increments ``book.version``, surgically clears
  all walk-created junction/edge data (character spans, scenes,
  memberships, span instructions, voice assignments), and returns the
  new version number.  **Memberships (``character_book`` rows) are NOT
  carried over** — they are deleted and must be re-created by the next
  walk run.
* ``get_book_version`` — returns the current ``version`` for a book.

Usage::

    from app.pipeline.adapter import PipelineStorage
    from app.pipeline.assembly import export_annotated_script

    storage: PipelineStorage = ...
    script = export_annotated_script("book-001", storage)
    # script == [{"speaker": "Alice", "text": "...", "instruct": "..."}, ...]
"""

from __future__ import annotations

from app.pipeline.adapter import PipelineStorage


def export_annotated_script(
    book_id: str, storage: PipelineStorage
) -> list[dict]:
    """Export the annotated script for *book_id* in presentation order.

    The document spine is traversed using the same join chain as the
    ``span_presentation`` VIEW (span → paragraph_span → scene_paragraph →
    chapter_scene → book_chapter → book), filtered to a single book and
    ordered by the positional edges.

    For each span the speaker is resolved via ``character_span`` with
    ``relation_type = 'speaker'``.  If no speaker junction exists the span
    is attributed to ``'NARRATOR'``.

    Parameters
    ----------
    book_id:
        Primary key of the book to export.
    storage:
        An active ``PipelineStorage`` implementation.

    Returns
    -------
    list[dict]
        Each entry has keys ``speaker`` (str), ``text`` (str), and
        ``instruct`` (str).  The list is ordered by presentation
        (global_index).
    """
    rows = storage.execute_query(
        """
        SELECT
            span.id,
            span.text,
            span.instruct,
            c.name    AS character_name
        FROM span
        JOIN paragraph_span  AS span_edge
            ON span.id = span_edge.child_id
        JOIN scene_paragraph AS paragraph_edge
            ON span_edge.parent_id = paragraph_edge.child_id
        JOIN chapter_scene   AS scene_edge
            ON paragraph_edge.parent_id = scene_edge.child_id
        JOIN book_chapter    AS chapter_edge
            ON scene_edge.parent_id = chapter_edge.child_id
        JOIN book
            ON chapter_edge.parent_id = book.id
        LEFT JOIN character_span AS cs
            ON span.id = cs.span_id
           AND cs.relation_type = 'speaker'
        LEFT JOIN character AS c
            ON cs.character_id = c.id
        WHERE book.id = ?
        ORDER BY
            book.position,
            chapter_edge.position,
            scene_edge.position,
            paragraph_edge.position,
            span_edge.position
        """,
        (book_id,),
    )

    result: list[dict] = []
    for row in rows:
        speaker = row["character_name"] if row["character_name"] else "NARRATOR"
        result.append(
            {
                "id": row["id"],
                "speaker": speaker,
                "text": row["text"] or "",
                "instruct": row["instruct"] or "",
            }
        )
    return result


# ---------------------------------------------------------------------------
# Re-onboarding & version management
# ---------------------------------------------------------------------------


def get_book_version(book_id: str, storage: PipelineStorage) -> int:
    """Return the current ``version`` number for *book_id*.

    Parameters
    ----------
    book_id:
        Primary key of the book.
    storage:
        An active ``PipelineStorage`` implementation.

    Returns
    -------
    int
        The current version (defaults to 1 for newly onboarded books).

    Raises
    ------
    ValueError
        If no book with *book_id* exists.
    """
    rows = storage.execute_query(
        "SELECT version FROM book WHERE id = ?", (book_id,)
    )
    if not rows:
        raise ValueError(f"Book '{book_id}' not found")
    return rows[0]["version"]


def _clear_span_junctions(
    storage: PipelineStorage, book_id: str, scene_ids: list[str]
) -> None:
    """Clear character-span and character-scene junctions for *book_id*.

    Deletes ``character_span`` rows reachable through the book tree,
    deletes ``character_scene`` rows for the book's scenes, and resets
    ``span.instruct`` to NULL for the book's spans.

    Parameters
    ----------
    storage:
        An active ``PipelineStorage`` implementation.
    book_id:
        Primary key of the book.
    scene_ids:
        Pre-snapshot of scene IDs belonging to this book.
    """
    # character_span: delete for all spans reachable through the book tree.
    storage.execute_delete(
        """DELETE FROM character_span
           WHERE span_id IN (
               SELECT span.id FROM span
               JOIN paragraph_span AS span_edge
                   ON span.id = span_edge.child_id
               JOIN scene_paragraph AS paragraph_edge
                   ON span_edge.parent_id = paragraph_edge.child_id
               JOIN chapter_scene AS scene_edge
                   ON paragraph_edge.parent_id = scene_edge.child_id
               JOIN book_chapter AS chapter_edge
                   ON scene_edge.parent_id = chapter_edge.child_id
               WHERE chapter_edge.parent_id = ?
           )""",
        (book_id,),
    )

    # character_scene: delete for the book's scenes (using snapshot).
    if scene_ids:
        placeholders = ",".join("?" for _ in scene_ids)
        storage.execute_delete(
            f"DELETE FROM character_scene WHERE scene_id IN ({placeholders})",
            tuple(scene_ids),
        )

    # Reset span.instruct to NULL for the book's spans (still needs
    # chapter_scene join).
    storage.execute_update(
        """UPDATE span SET instruct = NULL
           WHERE id IN (
               SELECT span.id FROM span
               JOIN paragraph_span AS span_edge
                   ON span.id = span_edge.child_id
               JOIN scene_paragraph AS paragraph_edge
                   ON span_edge.parent_id = paragraph_edge.child_id
               JOIN chapter_scene AS scene_edge
                   ON paragraph_edge.parent_id = scene_edge.child_id
               JOIN book_chapter AS chapter_edge
                   ON scene_edge.parent_id = chapter_edge.child_id
               WHERE chapter_edge.parent_id = ?
           )""",
        (book_id,),
    )


def _clear_memberships(
    storage: PipelineStorage, book_id: str, character_ids: list[str]
) -> None:
    """Clear character-book memberships and metadata for *book_id*.

    Deletes ``character_book`` rows (memberships are NOT carried over),
    deletes ``character_metadata`` rows for the linked characters, and
    resets ``character.voice_assignment_id`` to NULL.

    Parameters
    ----------
    storage:
        An active ``PipelineStorage`` implementation.
    book_id:
        Primary key of the book.
    character_ids:
        Pre-snapshot of character IDs linked to this book.
    """
    # character_book: memberships NOT carried over — deleted entirely.
    storage.execute_delete(
        "DELETE FROM character_book WHERE book_id = ?", (book_id,)
    )

    # character_metadata: clear for characters linked to this book.
    if character_ids:
        placeholders = ",".join("?" for _ in character_ids)
        storage.execute_delete(
            f"DELETE FROM character_metadata WHERE character_id IN ({placeholders})",
            tuple(character_ids),
        )
        # Reset voice_assignment_id for those characters.
        storage.execute_update(
            f"""UPDATE character SET voice_assignment_id = NULL
                WHERE id IN ({placeholders})""",
            tuple(character_ids),
        )


def _clear_scene_entities(
    storage: PipelineStorage, book_id: str, scene_ids: list[str]
) -> None:
    """Clear scene entities and edges for *book_id*.

    Deletes ``scene_paragraph`` edges, ``chapter_scene`` edges, and
    ``scene`` rows that belong to this book.

    Parameters
    ----------
    storage:
        An active ``PipelineStorage`` implementation.
    book_id:
        Primary key of the book.
    scene_ids:
        Pre-snapshot of scene IDs belonging to this book.
    """
    # scene_paragraph edges: must be deleted before scenes (FK constraint).
    if scene_ids:
        placeholders = ",".join("?" for _ in scene_ids)
        storage.execute_delete(
            f"DELETE FROM scene_paragraph WHERE parent_id IN ({placeholders})",
            tuple(scene_ids),
        )

    # chapter_scene edges: delete for the book's chapters.
    storage.execute_delete(
        """DELETE FROM chapter_scene
           WHERE parent_id IN (
               SELECT id FROM chapter WHERE book_id = ?
           )""",
        (book_id,),
    )

    # scene rows: delete scenes that belonged to this book (using snapshot).
    if scene_ids:
        placeholders = ",".join("?" for _ in scene_ids)
        storage.execute_delete(
            f"DELETE FROM scene WHERE id IN ({placeholders})",
            tuple(scene_ids),
        )


def reonboard_book(book_id: str, storage: PipelineStorage) -> int:
    """Re-onboard a book by clearing walk outputs and bumping its version.

    The document tree (series, book, chapters, paragraphs, spans) is
    preserved.  Only walk-created junction/edge data is removed:

    * ``character_span`` rows for the book's spans
    * ``character_scene`` rows for the book's scenes
    * ``character_book`` rows for the book (**memberships are NOT
      carried over** — they must be re-created by the next walk run)
    * ``character_metadata`` rows for characters linked to this book
    * ``chapter_scene`` edges for the book's chapters
    * ``scene`` rows that belong to this book
    * ``span.instruct`` reset to NULL for the book's spans
    * ``character.voice_assignment_id`` reset to NULL for characters
      linked to this book

    Characters themselves are **not** deleted — they may be shared
    across books in a series.

    Parameters
    ----------
    book_id:
        Primary key of the book to re-onboard.
    storage:
        An active ``PipelineStorage`` implementation.

    Returns
    -------
    int
        The new version number after incrementing.
    """
    # -- Snapshot IDs before destructive deletes ----------------------------
    # character_ids: needed for metadata cleanup and voice_assignment reset.
    char_rows = storage.execute_query(
        "SELECT character_id FROM character_book WHERE book_id = ?",
        (book_id,),
    )
    character_ids = [r["character_id"] for r in char_rows]

    # scene_ids: needed for scene deletion after chapter_scene edges are
    # removed (the chapter_scene join is the standard way to find a book's
    # scenes, so we must snapshot before deleting those edges).
    scene_rows = storage.execute_query(
        """SELECT chapter_scene.child_id AS scene_id
           FROM chapter_scene
           JOIN book_chapter
               ON chapter_scene.parent_id = book_chapter.child_id
           WHERE book_chapter.parent_id = ?""",
        (book_id,),
    )
    scene_ids = [r["scene_id"] for r in scene_rows]

    # -- Phase 1: Clear span/scene junctions --------------------------------
    _clear_span_junctions(storage, book_id, scene_ids)

    # -- Phase 2: Clear character memberships and metadata ------------------
    _clear_memberships(storage, book_id, character_ids)

    # -- Phase 3: Clear scene entities and edges ----------------------------
    _clear_scene_entities(storage, book_id, scene_ids)

    # -- Phase 4: Bump version ----------------------------------------------
    storage.execute_update(
        "UPDATE book SET version = version + 1 WHERE id = ?", (book_id,)
    )
    return get_book_version(book_id, storage)
