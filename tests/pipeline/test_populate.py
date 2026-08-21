"""Spec-first tests for spine population (app.pipeline.populate).

Covers:
- Initial spine creation with placeholder scenes
- Series and book insertion (INSERT OR IGNORE for series)
- Chapter insertion with dense integer positions
- Placeholder scene creation per chapter
- Paragraph and span insertion with correct edges
- Scene insertion for Walk 2a (redistributing paragraphs)
- Edge table constraints after insert_scene
- Position renumbering (dense integers)
- populate_spine wrapper behavior
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from app.pipeline.adapter import InMemorySQLiteAdapter
from app.pipeline.populate import (
    insert_scene,
    populate_initial_spine,
    populate_spine,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def storage():
    """In-memory SQLite adapter for testing."""
    adapter = InMemorySQLiteAdapter()
    adapter.init_db()
    return adapter


@pytest.fixture()
def sample_chapters():
    """Sample chapter data matching extract_epub_text output structure."""
    return [
        {
            "id": "chapter-1-uuid",
            "paragraphs": [
                {
                    "id": "para-1-1-uuid",
                    "spans": [
                        {"id": "span-1-1-1-uuid", "span_type": "sentence", "text": "First sentence."},
                        {"id": "span-1-1-2-uuid", "span_type": "quotation", "text": "hello"},
                    ],
                },
                {
                    "id": "para-1-2-uuid",
                    "spans": [
                        {"id": "span-1-2-1-uuid", "span_type": "sentence", "text": "Second paragraph."},
                    ],
                },
            ],
        },
        {
            "id": "chapter-2-uuid",
            "paragraphs": [
                {
                    "id": "para-2-1-uuid",
                    "spans": [
                        {"id": "span-2-1-1-uuid", "span_type": "sentence", "text": "Chapter 2 text."},
                    ],
                },
            ],
        },
    ]


# ---------------------------------------------------------------------------
# Test initial spine creation
# ---------------------------------------------------------------------------


class TestPopulateInitialSpine:
    def test_creates_series_row(self, storage, sample_chapters):
        """populate_initial_spine creates series row."""
        populate_initial_spine("series-uuid", "book-uuid", sample_chapters, storage)
        rows = storage.execute_query("SELECT id FROM series WHERE id = ?", ("series-uuid",))
        assert len(rows) == 1
        assert rows[0]["id"] == "series-uuid"

    def test_creates_book_row_with_version_1(self, storage, sample_chapters):
        """populate_initial_spine creates book row with version=1."""
        populate_initial_spine("series-uuid", "book-uuid", sample_chapters, storage)
        rows = storage.execute_query("SELECT id, series_id, version FROM book WHERE id = ?", ("book-uuid",))
        assert len(rows) == 1
        assert rows[0]["id"] == "book-uuid"
        assert rows[0]["series_id"] == "series-uuid"
        assert rows[0]["version"] == 1

    def test_creates_chapter_rows(self, storage, sample_chapters):
        """populate_initial_spine creates chapter rows."""
        populate_initial_spine("series-uuid", "book-uuid", sample_chapters, storage)
        rows = storage.execute_query("SELECT id, book_id FROM chapter ORDER BY id")
        assert len(rows) == 2
        chapter_ids = {row["id"] for row in rows}
        assert chapter_ids == {"chapter-1-uuid", "chapter-2-uuid"}
        for row in rows:
            assert row["book_id"] == "book-uuid"

    def test_creates_book_chapter_edges_with_dense_positions(self, storage, sample_chapters):
        """populate_initial_spine creates book_chapter edges with dense integer positions."""
        populate_initial_spine("series-uuid", "book-uuid", sample_chapters, storage)
        rows = storage.execute_query(
            "SELECT child_id, parent_id, position FROM book_chapter ORDER BY position"
        )
        assert len(rows) == 2
        assert rows[0]["child_id"] == "chapter-1-uuid"
        assert rows[0]["parent_id"] == "book-uuid"
        assert rows[0]["position"] == 1
        assert rows[1]["child_id"] == "chapter-2-uuid"
        assert rows[1]["parent_id"] == "book-uuid"
        assert rows[1]["position"] == 2

    def test_creates_placeholder_scenes(self, storage, sample_chapters):
        """populate_initial_spine creates one placeholder scene per chapter."""
        populate_initial_spine("series-uuid", "book-uuid", sample_chapters, storage)
        rows = storage.execute_query("SELECT id FROM scene")
        assert len(rows) == 2  # One placeholder per chapter

    def test_creates_chapter_scene_edges(self, storage, sample_chapters):
        """populate_initial_spine creates chapter_scene edges for placeholder scenes."""
        populate_initial_spine("series-uuid", "book-uuid", sample_chapters, storage)
        rows = storage.execute_query(
            "SELECT child_id, parent_id, position FROM chapter_scene ORDER BY parent_id, position"
        )
        assert len(rows) == 2
        # Both placeholder scenes have position=1 under their respective chapters
        for row in rows:
            assert row["position"] == 1
            assert row["parent_id"] in {"chapter-1-uuid", "chapter-2-uuid"}

    def test_creates_paragraph_rows(self, storage, sample_chapters):
        """populate_initial_spine creates paragraph rows."""
        populate_initial_spine("series-uuid", "book-uuid", sample_chapters, storage)
        rows = storage.execute_query("SELECT id FROM paragraph")
        assert len(rows) == 3  # 2 paragraphs in chapter 1, 1 in chapter 2

    def test_creates_scene_paragraph_edges(self, storage, sample_chapters):
        """populate_initial_spine creates scene_paragraph edges linking paragraphs to placeholder scenes."""
        populate_initial_spine("series-uuid", "book-uuid", sample_chapters, storage)
        rows = storage.execute_query(
            "SELECT child_id, parent_id, position FROM scene_paragraph ORDER BY parent_id, position"
        )
        assert len(rows) == 3
        # Check that positions are dense integers
        positions = [row["position"] for row in rows]
        assert sorted(positions) == [1, 1, 2]  # Two scenes: one with 2 paragraphs, one with 1

    def test_creates_span_rows(self, storage, sample_chapters):
        """populate_initial_spine creates span rows with correct span_type."""
        populate_initial_spine("series-uuid", "book-uuid", sample_chapters, storage)
        rows = storage.execute_query("SELECT id, span_type FROM span ORDER BY id")
        assert len(rows) == 4  # 2 spans in para 1-1, 1 in para 1-2, 1 in para 2-1
        span_types = {row["id"]: row["span_type"] for row in rows}
        assert span_types["span-1-1-1-uuid"] == "sentence"
        assert span_types["span-1-1-2-uuid"] == "quotation"
        assert span_types["span-1-2-1-uuid"] == "sentence"
        assert span_types["span-2-1-1-uuid"] == "sentence"

    def test_creates_paragraph_span_edges(self, storage, sample_chapters):
        """populate_initial_spine creates paragraph_span edges with dense positions."""
        populate_initial_spine("series-uuid", "book-uuid", sample_chapters, storage)
        rows = storage.execute_query(
            "SELECT child_id, parent_id, position FROM paragraph_span ORDER BY parent_id, position"
        )
        assert len(rows) == 4
        # Check paragraph 1-1 has 2 spans with positions 1, 2
        para_1_1_spans = [r for r in rows if r["parent_id"] == "para-1-1-uuid"]
        assert len(para_1_1_spans) == 2
        assert para_1_1_spans[0]["position"] == 1
        assert para_1_1_spans[1]["position"] == 2

    def test_series_insert_or_ignore(self, storage, sample_chapters):
        """populate_initial_spine does not duplicate series row if it already exists."""
        # Insert series first
        storage.execute_insert("INSERT INTO series (id) VALUES (?)", ("series-uuid",))
        # Call populate_initial_spine — should not raise
        populate_initial_spine("series-uuid", "book-uuid", sample_chapters, storage)
        # Verify only one series row
        rows = storage.execute_query("SELECT id FROM series WHERE id = ?", ("series-uuid",))
        assert len(rows) == 1

    def test_empty_chapters_list_is_rejected_without_writing(self, storage):
        """An empty extraction cannot create an orphan book."""
        with pytest.raises(ValueError, match="without chapters"):
            populate_initial_spine("series-uuid", "book-uuid", [], storage)

        assert storage.execute_query("SELECT id FROM series") == []
        assert storage.execute_query("SELECT id FROM book") == []

    def test_book_position_advances_for_existing_series(self, storage, sample_chapters):
        """A second book does not collide with the first book's position."""
        populate_initial_spine("series-uuid", "first-book", sample_chapters, storage)
        second_chapters = deepcopy(sample_chapters)
        for chapter in second_chapters:
            chapter["id"] += "-second"
            for paragraph in chapter["paragraphs"]:
                paragraph["id"] += "-second"
                for span in paragraph["spans"]:
                    span["id"] += "-second"
        populate_initial_spine("series-uuid", "second-book", second_chapters, storage)

        rows = storage.execute_query(
            "SELECT id, book_number, position FROM book "
            "WHERE series_id = ? ORDER BY position",
            ("series-uuid",),
        )
        assert rows == [
            {"id": "first-book", "book_number": 1, "position": 1},
            {"id": "second-book", "book_number": 2, "position": 2},
        ]


# ---------------------------------------------------------------------------
# Test scene insertion (Walk 2a)
# ---------------------------------------------------------------------------


class TestInsertScene:
    def test_creates_scene_row(self, storage, sample_chapters):
        """insert_scene creates a new scene row."""
        populate_initial_spine("series-uuid", "book-uuid", sample_chapters, storage)
        insert_scene("new-scene-uuid", "chapter-1-uuid", ["para-1-1-uuid"], storage)
        rows = storage.execute_query("SELECT id FROM scene WHERE id = ?", ("new-scene-uuid",))
        assert len(rows) == 1

    def test_creates_chapter_scene_edge(self, storage, sample_chapters):
        """insert_scene creates chapter_scene edge with next available position."""
        populate_initial_spine("series-uuid", "book-uuid", sample_chapters, storage)
        insert_scene("new-scene-uuid", "chapter-1-uuid", ["para-1-1-uuid"], storage)
        rows = storage.execute_query(
            "SELECT child_id, parent_id, position FROM chapter_scene WHERE child_id = ?",
            ("new-scene-uuid",),
        )
        assert len(rows) == 1
        assert rows[0]["parent_id"] == "chapter-1-uuid"
        # Placeholder scene has position=1, so new scene should have position=2
        assert rows[0]["position"] == 2

    def test_redistributes_paragraphs(self, storage, sample_chapters):
        """insert_scene moves paragraphs from placeholder scene to new scene."""
        populate_initial_spine("series-uuid", "book-uuid", sample_chapters, storage)
        # Get placeholder scene ID for chapter 1
        placeholder_rows = storage.execute_query(
            "SELECT child_id FROM chapter_scene WHERE parent_id = ? AND position = 1",
            ("chapter-1-uuid",),
        )
        placeholder_scene_id = placeholder_rows[0]["child_id"]

        # Insert new scene with para-1-1-uuid
        insert_scene("new-scene-uuid", "chapter-1-uuid", ["para-1-1-uuid"], storage)

        # Check that para-1-1-uuid now belongs to new-scene-uuid
        para_rows = storage.execute_query(
            "SELECT parent_id FROM scene_paragraph WHERE child_id = ?",
            ("para-1-1-uuid",),
        )
        assert len(para_rows) == 1
        assert para_rows[0]["parent_id"] == "new-scene-uuid"

        # Check that para-1-2-uuid still belongs to placeholder scene
        para_rows_2 = storage.execute_query(
            "SELECT parent_id FROM scene_paragraph WHERE child_id = ?",
            ("para-1-2-uuid",),
        )
        assert len(para_rows_2) == 1
        assert para_rows_2[0]["parent_id"] == placeholder_scene_id

    def test_multiple_paragraphs_in_new_scene(self, storage, sample_chapters):
        """insert_scene can move multiple paragraphs to a new scene."""
        populate_initial_spine("series-uuid", "book-uuid", sample_chapters, storage)
        insert_scene(
            "new-scene-uuid",
            "chapter-1-uuid",
            ["para-1-1-uuid", "para-1-2-uuid"],
            storage,
        )
        # Both paragraphs should now belong to new-scene-uuid
        rows = storage.execute_query(
            "SELECT child_id, position FROM scene_paragraph WHERE parent_id = ? ORDER BY position",
            ("new-scene-uuid",),
        )
        assert len(rows) == 2
        assert rows[0]["child_id"] == "para-1-1-uuid"
        assert rows[0]["position"] == 1
        assert rows[1]["child_id"] == "para-1-2-uuid"
        assert rows[1]["position"] == 2

    def test_position_renumbering_dense(self, storage, sample_chapters):
        """insert_scene assigns dense integer positions to paragraphs."""
        populate_initial_spine("series-uuid", "book-uuid", sample_chapters, storage)
        insert_scene(
            "new-scene-uuid",
            "chapter-1-uuid",
            ["para-1-1-uuid", "para-1-2-uuid"],
            storage,
        )
        rows = storage.execute_query(
            "SELECT position FROM scene_paragraph WHERE parent_id = ? ORDER BY position",
            ("new-scene-uuid",),
        )
        positions = [row["position"] for row in rows]
        assert positions == [1, 2]  # Dense integers

    def test_multiple_scenes_in_chapter(self, storage, sample_chapters):
        """Multiple insert_scene calls create multiple scenes under same chapter."""
        populate_initial_spine("series-uuid", "book-uuid", sample_chapters, storage)
        insert_scene("scene-a-uuid", "chapter-1-uuid", ["para-1-1-uuid"], storage)
        insert_scene("scene-b-uuid", "chapter-1-uuid", ["para-1-2-uuid"], storage)

        # Check chapter_scene edges
        rows = storage.execute_query(
            "SELECT child_id, position FROM chapter_scene WHERE parent_id = ? ORDER BY position",
            ("chapter-1-uuid",),
        )
        assert len(rows) == 3  # Placeholder + 2 new scenes
        positions = [row["position"] for row in rows]
        assert positions == [1, 2, 3]  # Dense positions


# ---------------------------------------------------------------------------
# Test populate_spine wrapper
# ---------------------------------------------------------------------------


class TestPopulateSpine:
    def test_wrapper_calls_populate_initial_spine(self, storage, sample_chapters):
        """populate_spine is a wrapper that calls populate_initial_spine."""
        populate_spine("series-uuid", "book-uuid", sample_chapters, storage)
        # Verify spine was created
        series_rows = storage.execute_query("SELECT id FROM series WHERE id = ?", ("series-uuid",))
        assert len(series_rows) == 1
        book_rows = storage.execute_query("SELECT id FROM book WHERE id = ?", ("book-uuid",))
        assert len(book_rows) == 1
        chapter_rows = storage.execute_query("SELECT id FROM chapter")
        assert len(chapter_rows) == 2


# ---------------------------------------------------------------------------
# Test edge constraints
# ---------------------------------------------------------------------------


class TestEdgeConstraints:
    def test_unique_child_id_scene_paragraph(self, storage, sample_chapters):
        """A paragraph can only belong to one scene at a time (UNIQUE on child_id)."""
        populate_initial_spine("series-uuid", "book-uuid", sample_chapters, storage)
        # Insert para-1-1-uuid into scene-a
        insert_scene("scene-a-uuid", "chapter-1-uuid", ["para-1-1-uuid"], storage)
        # Move para-1-1-uuid to scene-b (deletes old edge, creates new one)
        insert_scene("scene-b-uuid", "chapter-1-uuid", ["para-1-1-uuid"], storage)
        # Verify paragraph now belongs to scene-b, not scene-a
        rows = storage.execute_query(
            "SELECT parent_id FROM scene_paragraph WHERE child_id = ?",
            ("para-1-1-uuid",),
        )
        assert len(rows) == 1
        assert rows[0]["parent_id"] == "scene-b-uuid"

    def test_unique_parent_position_chapter_scene(self, storage, sample_chapters):
        """A chapter cannot have two scenes at the same position."""
        populate_initial_spine("series-uuid", "book-uuid", sample_chapters, storage)
        # Manually try to insert two scenes at position 2
        storage.execute_insert("INSERT INTO scene (id) VALUES (?)", ("scene-x-uuid",))
        storage.execute_insert("INSERT INTO scene (id) VALUES (?)", ("scene-y-uuid",))
        storage.execute_insert(
            "INSERT INTO chapter_scene (child_id, parent_id, position) VALUES (?, ?, ?)",
            ("scene-x-uuid", "chapter-1-uuid", 2),
        )
        with pytest.raises(Exception):  # IntegrityError
            storage.execute_insert(
                "INSERT INTO chapter_scene (child_id, parent_id, position) VALUES (?, ?, ?)",
                ("scene-y-uuid", "chapter-1-uuid", 2),
            )


# ---------------------------------------------------------------------------
# Test atomicity (SAVEPOINT)
# ---------------------------------------------------------------------------


class TestAtomicity:
    def test_populate_initial_spine_rollback_on_error(self, storage):
        """If populate_initial_spine fails, all changes are rolled back."""
        # Create invalid data that will cause an error
        invalid_chapters = [
            {
                "id": "chapter-1-uuid",
                "paragraphs": [
                    {
                        "id": "para-1-uuid",
                        "spans": [
                            {"id": "span-1-uuid", "span_type": "invalid_type", "text": "text"},
                        ],
                    },
                ],
            },
        ]
        # This should fail due to invalid span_type
        with pytest.raises(Exception):
            populate_initial_spine("series-uuid", "book-uuid", invalid_chapters, storage)
        # Verify no partial data was inserted
        series_rows = storage.execute_query("SELECT id FROM series WHERE id = ?", ("series-uuid",))
        assert len(series_rows) == 0
        book_rows = storage.execute_query("SELECT id FROM book WHERE id = ?", ("book-uuid",))
        assert len(book_rows) == 0

    def test_insert_scene_rollback_on_error(self, storage, sample_chapters):
        """If insert_scene fails, all changes are rolled back."""
        populate_initial_spine("series-uuid", "book-uuid", sample_chapters, storage)
        # Try to insert scene with non-existent paragraph
        with pytest.raises(Exception):
            insert_scene("new-scene-uuid", "chapter-1-uuid", ["non-existent-para"], storage)
        # Verify scene was not created
        scene_rows = storage.execute_query("SELECT id FROM scene WHERE id = ?", ("new-scene-uuid",))
        assert len(scene_rows) == 0
