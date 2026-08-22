"""Spine population for the audiobook pipeline.

Provides functions to populate the Graph1 TREE spine (series→book→chapter→scene→paragraph→span)
from the structured dict produced by extract_epub_text().

Phase 2 creates placeholder scenes for each chapter. Walk 2a will later split
chapters into multiple scenes by redistributing paragraphs.
"""

from __future__ import annotations

import sqlite3
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.pipeline.adapter import PipelineStorage


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def populate_spine(
    series_id: str,
    book_id: str,
    chapters: list[dict],
    storage: PipelineStorage,
) -> None:
    """Higher-level wrapper that populates the spine from extracted data.

    Parameters
    ----------
    series_id:
        UUID for the series (from extract_epub_text).
    book_id:
        UUID for the book (from extract_epub_text).
    chapters:
        List of chapter dicts from extract_epub_text.
    storage:
        Pipeline storage adapter.
    """
    populate_initial_spine(series_id, book_id, chapters, storage)


def populate_initial_spine(
    series_id: str,
    book_id: str,
    chapters_data: list[dict],
    storage: PipelineStorage,
) -> None:
    """Insert series, book, chapters, placeholder scenes, paragraphs, and spans.

    Creates a flat spine with one placeholder scene per chapter. All paragraphs
    for a chapter are linked to that chapter's placeholder scene via scene_paragraph
    edges. Walk 2a will later split chapters into multiple scenes.

    Also ensures the paragraph table has a ``text`` column (added via ALTER TABLE
    if missing) so Walk 2a can retrieve paragraph text for LLM prompts.

    Parameters
    ----------
    series_id:
        UUID for the series.
    book_id:
        UUID for the book.
    chapters_data:
        List of chapter dicts: {id, paragraphs: [{id, spans: [{id, span_type, text}]}]}.
    storage:
        Pipeline storage adapter.
    """
    if not chapters_data:
        raise ValueError("Cannot populate a book without chapters")

    _ensure_paragraph_text_column(storage)
    _ensure_span_text_column(storage)
    conn = storage.get_connection()
    conn.execute("SAVEPOINT populate_spine")
    try:
        _insert_series_and_book(series_id, book_id, storage)
        _insert_chapters_with_placeholders(book_id, chapters_data, storage)
        conn.execute("RELEASE SAVEPOINT populate_spine")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT populate_spine")
        conn.execute("RELEASE SAVEPOINT populate_spine")
        raise


def insert_scene(
    scene_id: str,
    chapter_id: str,
    paragraph_ids: list[str],
    storage: PipelineStorage,
) -> None:
    """Insert a scene and redistribute paragraphs from the placeholder scene.

    Walk 2a calls this when it identifies scene boundaries. Creates a new scene
    row, chapter_scene edge, and scene_paragraph edges for the specified paragraphs.
    Removes old scene_paragraph edges from the placeholder scene.

    Parameters
    ----------
    scene_id:
        UUID for the new scene.
    chapter_id:
        UUID of the parent chapter.
    paragraph_ids:
        List of paragraph UUIDs to move to this scene.
    storage:
        Pipeline storage adapter.
    """
    conn = storage.get_connection()
    conn.execute("SAVEPOINT insert_scene")
    try:
        _insert_scene_row(scene_id, storage)
        scene_position = _get_next_scene_position(chapter_id, storage)
        _insert_chapter_scene_edge(scene_id, chapter_id, scene_position, storage)
        _redistribute_paragraphs(scene_id, paragraph_ids, storage)
        conn.execute("RELEASE SAVEPOINT insert_scene")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT insert_scene")
        conn.execute("RELEASE SAVEPOINT insert_scene")
        raise


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _insert_series_and_book(
    series_id: str, book_id: str, storage: PipelineStorage
) -> None:
    """Insert series (if not exists) and book row with version=1."""
    storage.execute_insert(
        "INSERT OR IGNORE INTO series (id) VALUES (?)", (series_id,)
    )
    next_position = _next_book_position(series_id, storage)
    storage.execute_insert(
        "INSERT INTO book (id, series_id, book_number, version, position) "
        "VALUES (?, ?, ?, 1, ?)",
        (book_id, series_id, next_position, next_position),
    )


def _next_book_position(series_id: str, storage: PipelineStorage) -> int:
    """Return the next dense position for a book in a series."""
    rows = storage.execute_query(
        "SELECT MAX(position) AS max_pos FROM book WHERE series_id = ?",
        (series_id,),
    )
    max_position = rows[0]["max_pos"] if rows and rows[0]["max_pos"] is not None else 0
    return max_position + 1


def _insert_chapters_with_placeholders(
    book_id: str, chapters_data: list[dict], storage: PipelineStorage
) -> None:
    """Insert chapters, placeholder scenes, paragraphs, and spans."""
    for chapter_idx, chapter in enumerate(chapters_data, start=1):
        chapter_id = chapter["id"]
        _insert_chapter(chapter_id, book_id, chapter_idx, storage)
        placeholder_scene_id = str(uuid.uuid4())
        _insert_placeholder_scene(placeholder_scene_id, chapter_id, storage)
        _insert_paragraphs_and_spans(
            placeholder_scene_id, chapter["paragraphs"], storage
        )


def _insert_chapter(
    chapter_id: str, book_id: str, position: int, storage: PipelineStorage
) -> None:
    """Insert chapter row and book_chapter edge."""
    storage.execute_insert(
        "INSERT INTO chapter (id, book_id) VALUES (?, ?)", (chapter_id, book_id)
    )
    storage.execute_insert(
        "INSERT INTO book_chapter (child_id, parent_id, position) VALUES (?, ?, ?)",
        (chapter_id, book_id, position),
    )


def _insert_placeholder_scene(
    scene_id: str, chapter_id: str, storage: PipelineStorage
) -> None:
    """Insert placeholder scene row and chapter_scene edge."""
    storage.execute_insert("INSERT INTO scene (id) VALUES (?)", (scene_id,))
    storage.execute_insert(
        "INSERT INTO chapter_scene (child_id, parent_id, position) VALUES (?, ?, ?)",
        (scene_id, chapter_id, 1),
    )


def _insert_paragraphs_and_spans(
    scene_id: str, paragraphs: list[dict], storage: PipelineStorage
) -> None:
    """Insert paragraphs and spans for a scene."""
    for para_idx, paragraph in enumerate(paragraphs, start=1):
        paragraph_id = paragraph["id"]
        para_text = _reconstruct_paragraph_text(paragraph.get("spans", []))
        _insert_paragraph(paragraph_id, scene_id, para_idx, para_text, storage)
        for span_idx, span in enumerate(paragraph["spans"], start=1):
            _insert_span(span["id"], span["span_type"], paragraph_id, span_idx, storage, span.get("text", ""))


def _insert_paragraph(
    paragraph_id: str,
    scene_id: str,
    position: int,
    text: str,
    storage: PipelineStorage,
) -> None:
    """Insert paragraph row with text and scene_paragraph edge."""
    storage.execute_insert(
        "INSERT INTO paragraph (id, text) VALUES (?, ?)",
        (paragraph_id, text),
    )
    storage.execute_insert(
        "INSERT INTO scene_paragraph (child_id, parent_id, position) VALUES (?, ?, ?)",
        (paragraph_id, scene_id, position),
    )


def _insert_span(
    span_id: str, span_type: str, paragraph_id: str, position: int, storage: PipelineStorage, span_text: str = ""
) -> None:
    """Insert span row and paragraph_span edge."""
    storage.execute_insert(
        "INSERT INTO span (id, span_type, instruct, text) VALUES (?, ?, NULL, ?)",
        (span_id, span_type, span_text),
    )
    storage.execute_insert(
        "INSERT INTO paragraph_span (child_id, parent_id, position) VALUES (?, ?, ?)",
        (span_id, paragraph_id, position),
    )


def _insert_scene_row(scene_id: str, storage: PipelineStorage) -> None:
    """Insert a scene row."""
    storage.execute_insert("INSERT INTO scene (id) VALUES (?)", (scene_id,))


def _get_next_scene_position(chapter_id: str, storage: PipelineStorage) -> int:
    """Get the next available position for a scene under this chapter."""
    rows = storage.execute_query(
        "SELECT MAX(position) AS max_pos FROM chapter_scene WHERE parent_id = ?",
        (chapter_id,),
    )
    max_pos = rows[0]["max_pos"] if rows and rows[0]["max_pos"] is not None else 0
    return max_pos + 1


def _insert_chapter_scene_edge(
    scene_id: str, chapter_id: str, position: int, storage: PipelineStorage
) -> None:
    """Insert chapter_scene edge."""
    storage.execute_insert(
        "INSERT INTO chapter_scene (child_id, parent_id, position) VALUES (?, ?, ?)",
        (scene_id, chapter_id, position),
    )


def _redistribute_paragraphs(
    scene_id: str, paragraph_ids: list[str], storage: PipelineStorage
) -> None:
    """Move paragraphs from placeholder scene to new scene."""
    for para_idx, paragraph_id in enumerate(paragraph_ids, start=1):
        rows = storage.execute_query(
            "SELECT parent_id FROM scene_paragraph WHERE child_id = ?",
            (paragraph_id,),
        )
        if rows:
            storage.execute_delete(
                "DELETE FROM scene_paragraph WHERE child_id = ?", (paragraph_id,)
            )
        storage.execute_insert(
            "INSERT INTO scene_paragraph (child_id, parent_id, position) VALUES (?, ?, ?)",
            (paragraph_id, scene_id, para_idx),
        )


def _ensure_paragraph_text_column(storage: PipelineStorage) -> None:
    """Add ``text TEXT`` column to paragraph table if it does not exist.

    Walk 2a needs paragraph text for LLM prompts. The schema module does not
    include this column (it is an additive fix for a Phase-2 gap). SQLite
    3.35.0+ supports ``IF NOT EXISTS`` on ALTER TABLE; older versions fall
    back to a try/except on the duplicate-column error.
    """
    conn = storage.get_connection()
    try:
        conn.execute(
            "ALTER TABLE paragraph ADD COLUMN text TEXT"
        )
    except sqlite3.OperationalError:
        # Column already exists or SQLite version doesn't support IF NOT EXISTS.
        # Verify the column is present; if not, re-raise.
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(paragraph)").fetchall()
        }
        if "text" not in cols:
            raise


def _ensure_span_text_column(storage: PipelineStorage) -> None:
    """Add ``text TEXT`` column to span table if it does not exist.

    Walk 2e needs span text for LLM prompts (quotation attribution). The schema
    module now includes this column, but existing databases need the migration.
    """
    conn = storage.get_connection()
    try:
        conn.execute(
            "ALTER TABLE span ADD COLUMN text TEXT"
        )
    except sqlite3.OperationalError:
        # Column already exists or SQLite version doesn't support IF NOT EXISTS.
        # Verify the column is present; if not, re-raise.
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(span)").fetchall()
        }
        if "text" not in cols:
            raise


def _reconstruct_paragraph_text(spans: list[dict]) -> str:
    """Reconstruct paragraph text by joining span texts in order."""
    return " ".join(span.get("text", "") for span in spans if span.get("text"))
