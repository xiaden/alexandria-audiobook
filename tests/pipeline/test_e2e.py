"""End-to-end integration tests for the audiobook pipeline.

Covers:
- Test 1: Fresh book → export_annotated_script → verify structure
- Test 2: Reonboard → verify version bump and junction clearing
- Test 3: Walk rerun after ledger edit → accept/reject review items
- Test 4: Split/merge operations → export → verify presentation order
- Test 5: Confidence filter → review items → override

All tests use InMemorySQLiteAdapter for isolation. No LLM calls, no EPUB files.
Real pipeline functions are used (export_annotated_script, reonboard_book,
get_book_version, OperationExecutor, ReviewManager).
"""

from __future__ import annotations

import pytest

from app.pipeline.adapter import InMemorySQLiteAdapter
from app.pipeline.assembly import (
    export_annotated_script,
    get_book_version,
    reonboard_book,
)
from app.pipeline.operations import OperationExecutor
from app.pipeline.review import ReviewManager

# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def storage():
    """Return an InMemorySQLiteAdapter with schema initialised."""
    adapter = InMemorySQLiteAdapter()
    adapter.init_db()
    yield adapter
    adapter.close()


# ---------------------------------------------------------------------------
# Test 1: Full pipeline — fresh book export
# ---------------------------------------------------------------------------


class TestFullPipelineFreshBook:
    """Populate a minimal spine and verify export produces correct speaker/text/instruct."""

    def test_full_pipeline_fresh_book(self, storage):
        """Populate a minimal spine, export, and verify speaker/text/instruct."""
        conn = storage.get_connection()

        # -- Spine: 1 series, 1 book, 1 chapter, 1 scene, 1 paragraph, 2 spans
        conn.execute("INSERT INTO series VALUES ('s1')")
        conn.execute(
            "INSERT INTO book (id, series_id, book_number, version, position) "
            "VALUES ('b1', 's1', 1, 1, 1)"
        )
        conn.execute("INSERT INTO chapter (id, book_id) VALUES ('c1', 'b1')")
        conn.execute("INSERT INTO scene VALUES ('sc1')")
        conn.execute("INSERT INTO paragraph VALUES ('p1')")

        conn.execute(
            "INSERT INTO span (id, span_type, instruct, text) "
            "VALUES ('sp1', 'quotation', NULL, 'Hello!')"
        )
        conn.execute(
            "INSERT INTO span (id, span_type, instruct, text) "
            "VALUES ('sp2', 'sentence', NULL, 'She left.')"
        )

        # -- Edge tables
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

        # -- Character "Alice" with speaker junction on sp1
        conn.execute(
            "INSERT INTO character (id, name, aliases, voice_assignment_id, description) "
            "VALUES ('c1', 'Alice', '[]', NULL, NULL)"
        )
        conn.execute(
            "INSERT INTO character_span "
            "(character_id, span_id, relation_type, source, confidence, human_override) "
            "VALUES ('c1', 'sp1', 'speaker', 'walk', 0.9, 0)"
        )

        # -- Export and verify
        script = export_annotated_script("b1", storage)

        assert len(script) == 2

        # First entry: Alice speaks "Hello!"
        assert script[0]["speaker"] == "Alice"
        assert script[0]["text"] == "Hello!"

        # Second entry: NARRATOR (no speaker junction) "She left."
        assert script[1]["speaker"] == "NARRATOR"
        assert script[1]["text"] == "She left."

        # Verify output schema: each entry has keys "speaker", "text", "instruct"
        for entry in script:
            assert "speaker" in entry
            assert "text" in entry
            assert "instruct" in entry


# ---------------------------------------------------------------------------
# Test 2: Full pipeline — reonboard
# ---------------------------------------------------------------------------


class TestFullPipelineReonboard:
    """Populate a spine, reonboard, and verify version bump and junction clearing."""

    def test_full_pipeline_reonboard(self, storage):
        """Populate a fuller spine, reonboard, verify version bump and clearing."""
        conn = storage.get_connection()

        # -- Voice config
        conn.execute(
            "INSERT INTO voice_config (id, name, description) "
            "VALUES ('vc1', 'Warm Female', 'A warm voice')"
        )

        # -- Spine: 1 series, 1 book, 2 chapters, 2 scenes, 2 paragraphs, 3 spans
        conn.execute("INSERT INTO series VALUES ('s1')")
        conn.execute(
            "INSERT INTO book (id, series_id, book_number, version, position) "
            "VALUES ('b1', 's1', 1, 1, 1)"
        )
        conn.execute("INSERT INTO chapter (id, book_id) VALUES ('c1', 'b1')")
        conn.execute("INSERT INTO chapter (id, book_id) VALUES ('c2', 'b1')")
        conn.execute("INSERT INTO scene VALUES ('sc1')")
        conn.execute("INSERT INTO scene VALUES ('sc2')")
        conn.execute("INSERT INTO paragraph VALUES ('p1')")
        conn.execute("INSERT INTO paragraph VALUES ('p2')")

        conn.execute(
            "INSERT INTO span (id, span_type, instruct, text) "
            "VALUES ('sp1', 'quotation', 'cheerfully', 'Hello!')"
        )
        conn.execute(
            "INSERT INTO span (id, span_type, instruct, text) "
            "VALUES ('sp2', 'sentence', NULL, 'She said.')"
        )
        conn.execute(
            "INSERT INTO span (id, span_type, instruct, text) "
            "VALUES ('sp3', 'quotation', 'sadly', 'Bye.')"
        )

        # -- Edge tables
        conn.execute(
            "INSERT INTO book_chapter (child_id, parent_id, position) VALUES ('c1', 'b1', 1)"
        )
        conn.execute(
            "INSERT INTO book_chapter (child_id, parent_id, position) VALUES ('c2', 'b1', 2)"
        )
        conn.execute(
            "INSERT INTO chapter_scene (child_id, parent_id, position) VALUES ('sc1', 'c1', 1)"
        )
        conn.execute(
            "INSERT INTO chapter_scene (child_id, parent_id, position) VALUES ('sc2', 'c2', 1)"
        )
        conn.execute(
            "INSERT INTO scene_paragraph (child_id, parent_id, position) VALUES ('p1', 'sc1', 1)"
        )
        conn.execute(
            "INSERT INTO scene_paragraph (child_id, parent_id, position) VALUES ('p2', 'sc2', 1)"
        )
        conn.execute(
            "INSERT INTO paragraph_span (child_id, parent_id, position) VALUES ('sp1', 'p1', 1)"
        )
        conn.execute(
            "INSERT INTO paragraph_span (child_id, parent_id, position) VALUES ('sp2', 'p1', 2)"
        )
        conn.execute(
            "INSERT INTO paragraph_span (child_id, parent_id, position) VALUES ('sp3', 'p2', 1)"
        )

        # -- Characters
        conn.execute(
            "INSERT INTO character (id, name, aliases, voice_assignment_id, description) "
            "VALUES ('c1', 'Alice', '[]', 'vc1', NULL)"
        )
        conn.execute(
            "INSERT INTO character (id, name, aliases, voice_assignment_id, description) "
            "VALUES ('c2', 'Bob', '[]', NULL, NULL)"
        )

        # -- character_book memberships
        conn.execute(
            "INSERT INTO character_book "
            "(character_id, book_id, source, confidence, human_override) "
            "VALUES ('c1', 'b1', 'walk', 0.9, 0)"
        )
        conn.execute(
            "INSERT INTO character_book "
            "(character_id, book_id, source, confidence, human_override) "
            "VALUES ('c2', 'b1', 'walk', 0.8, 0)"
        )

        # -- character_span junctions
        conn.execute(
            "INSERT INTO character_span "
            "(character_id, span_id, relation_type, source, confidence, human_override) "
            "VALUES ('c1', 'sp1', 'speaker', 'walk', 0.95, 0)"
        )
        conn.execute(
            "INSERT INTO character_span "
            "(character_id, span_id, relation_type, source, confidence, human_override) "
            "VALUES ('c2', 'sp3', 'speaker', 'walk', 0.9, 0)"
        )

        # -- character_scene junctions
        conn.execute(
            "INSERT INTO character_scene "
            "(character_id, scene_id, relation_type, source, confidence, human_override) "
            "VALUES ('c1', 'sc1', 'present', 'walk', 0.85, 0)"
        )

        # -- Verify initial version
        assert get_book_version("b1", storage) == 1

        # -- Reonboard
        new_version = reonboard_book("b1", storage)
        assert new_version == 2

        # -- Verify character_span rows for the book's spans are cleared
        cs_rows = storage.execute_query(
            "SELECT COUNT(*) AS cnt FROM character_span "
            "WHERE span_id IN ('sp1', 'sp2', 'sp3')"
        )
        assert cs_rows[0]["cnt"] == 0

        # -- Verify character_book rows for the book are cleared
        cb_rows = storage.execute_query(
            "SELECT COUNT(*) AS cnt FROM character_book WHERE book_id = 'b1'"
        )
        assert cb_rows[0]["cnt"] == 0

        # -- Verify span.instruct is NULL for all spans
        instruct_rows = storage.execute_query(
            "SELECT instruct FROM span WHERE id IN ('sp1', 'sp2', 'sp3')"
        )
        for row in instruct_rows:
            assert row["instruct"] is None

        # -- Verify character.voice_assignment_id is NULL for book-linked characters
        voice_rows = storage.execute_query(
            "SELECT voice_assignment_id FROM character WHERE id IN ('c1', 'c2')"
        )
        for row in voice_rows:
            assert row["voice_assignment_id"] is None

        # -- Verify version is now 2
        assert get_book_version("b1", storage) == 2


# ---------------------------------------------------------------------------
# Test 3: Walk rerun after ledger edit (review accept/reject)
# ---------------------------------------------------------------------------


class TestWalkRerunAfterLedgerEdit:
    """Create review items and verify accept/reject via ReviewManager."""

    def test_walk_rerun_after_ledger_edit(self, storage):
        """Populate spine with low-confidence memberships, accept/reject via ReviewManager."""
        conn = storage.get_connection()

        # -- Spine: 1 book, 1 chapter, 1 scene, 1 paragraph, 2 spans
        conn.execute("INSERT INTO series VALUES ('s1')")
        conn.execute(
            "INSERT INTO book (id, series_id, book_number, version, position) "
            "VALUES ('b1', 's1', 1, 1, 1)"
        )
        conn.execute("INSERT INTO chapter (id, book_id) VALUES ('c1', 'b1')")
        conn.execute("INSERT INTO scene VALUES ('sc1')")
        conn.execute("INSERT INTO paragraph VALUES ('p1')")
        conn.execute(
            "INSERT INTO span (id, span_type, instruct, text) "
            "VALUES ('sp1', 'quotation', NULL, 'Hello!')"
        )
        conn.execute(
            "INSERT INTO span (id, span_type, instruct, text) "
            "VALUES ('sp2', 'sentence', NULL, 'She left.')"
        )

        # -- Edge tables
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

        # -- 2 characters with character_book memberships at confidence 0.6 (in review band)
        conn.execute(
            "INSERT INTO character (id, name, aliases, voice_assignment_id, description) "
            "VALUES ('c1', 'Alice', '[]', NULL, NULL)"
        )
        conn.execute(
            "INSERT INTO character (id, name, aliases, voice_assignment_id, description) "
            "VALUES ('c2', 'Bob', '[]', NULL, NULL)"
        )
        conn.execute(
            "INSERT INTO character_book "
            "(character_id, book_id, source, confidence, human_override) "
            "VALUES ('c1', 'b1', 'walk', 0.6, 0)"
        )
        conn.execute(
            "INSERT INTO character_book "
            "(character_id, book_id, source, confidence, human_override) "
            "VALUES ('c2', 'b1', 'walk', 0.6, 0)"
        )

        # -- ReviewManager: verify get_review_items returns these items
        manager = ReviewManager(storage)
        items = manager.get_review_items("b1")

        # Should have 2 character_book review items (confidence 0.6 is in [0.5, 0.7))
        book_items = [i for i in items if i["junction_table"] == "character_book"]
        assert len(book_items) == 2

        # -- Accept the first item
        accept_item = book_items[0]
        manager.accept_review_item(accept_item["item_id"])

        # Verify confidence becomes 1.0
        accepted_rows = storage.execute_query(
            "SELECT confidence FROM character_book "
            "WHERE character_id = ? AND book_id = ?",
            (accept_item["character_id"], "b1"),
        )
        assert accepted_rows[0]["confidence"] == 1.0

        # -- Reject the second item
        reject_item = book_items[1]
        manager.reject_review_item(reject_item["item_id"])

        # Verify confidence becomes 0.0 and human_override=1
        rejected_rows = storage.execute_query(
            "SELECT confidence, human_override FROM character_book "
            "WHERE character_id = ? AND book_id = ?",
            (reject_item["character_id"], "b1"),
        )
        assert rejected_rows[0]["confidence"] == 0.0
        assert rejected_rows[0]["human_override"] == 1

        # -- get_review_items no longer returns the accepted/rejected items
        remaining = manager.get_review_items("b1")
        remaining_book = [i for i in remaining if i["junction_table"] == "character_book"]
        assert len(remaining_book) == 0


# ---------------------------------------------------------------------------
# Test 4: Operation then export
# ---------------------------------------------------------------------------


class TestOperationThenExport:
    """Perform split/merge/move/delete operations and verify export reflects changes."""

    def test_operation_then_export(self, storage):
        """Split/merge/move/delete spans, then verify export reflects the changes."""
        conn = storage.get_connection()

        # -- Spine: 1 book, 1 chapter, 1 scene, 1 paragraph, 3 spans
        conn.execute("INSERT INTO series VALUES ('s1')")
        conn.execute(
            "INSERT INTO book (id, series_id, book_number, version, position) "
            "VALUES ('b1', 's1', 1, 1, 1)"
        )
        conn.execute("INSERT INTO chapter (id, book_id) VALUES ('c1', 'b1')")
        conn.execute("INSERT INTO scene VALUES ('sc1')")
        conn.execute("INSERT INTO paragraph VALUES ('p1')")

        conn.execute(
            "INSERT INTO span (id, span_type, instruct, text) "
            "VALUES ('sp1', 'quotation', NULL, 'Hello!')"
        )
        conn.execute(
            "INSERT INTO span (id, span_type, instruct, text) "
            "VALUES ('sp2', 'quotation', NULL, 'How are you?')"
        )
        conn.execute(
            "INSERT INTO span (id, span_type, instruct, text) "
            "VALUES ('sp3', 'quotation', NULL, 'Goodbye.')"
        )

        # -- Edge tables
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

        # -- Character "Alice" with speaker on sp1
        conn.execute(
            "INSERT INTO character (id, name, aliases, voice_assignment_id, description) "
            "VALUES ('c1', 'Alice', '[]', NULL, NULL)"
        )
        conn.execute(
            "INSERT INTO character_span "
            "(character_id, span_id, relation_type, source, confidence, human_override) "
            "VALUES ('c1', 'sp1', 'speaker', 'walk', 0.8, 0)"
        )

        # -- Initial export: 3 entries, first has speaker="Alice"
        script = export_annotated_script("b1", storage)
        assert len(script) == 3
        assert script[0]["speaker"] == "Alice"
        assert script[0]["text"] == "Hello!"

        # -- Split sp2 (presentation_index=2) at offset 4
        # "How are you?" (12 chars) → "How " + "are you?"
        executor = OperationExecutor(storage)
        executor.execute_split(book_id="b1", presentation_index=2, split_point=4)

        # -- Verify: 4 spans now exist
        span_count = conn.execute("SELECT COUNT(*) FROM span").fetchone()[0]
        assert span_count == 4

        # -- Verify: export has 4 entries
        script_after_split = export_annotated_script("b1", storage)
        assert len(script_after_split) == 4
        assert script_after_split[1]["text"] == "How "
        assert script_after_split[2]["text"] == "are you?"

        # -- Merge indices 2 and 3 (the two halves of the split)
        executor.execute_merge(book_id="b1", presentation_index_left=2, presentation_index_right=3)

        # -- Verify: back to 3 spans
        span_count_after_merge = conn.execute("SELECT COUNT(*) FROM span").fetchone()[0]
        assert span_count_after_merge == 3

        # -- Verify: export has 3 entries
        script_after_merge = export_annotated_script("b1", storage)
        assert len(script_after_merge) == 3
        assert script_after_merge[0]["speaker"] == "Alice"
        assert script_after_merge[0]["text"] == "Hello!"

        # -- Move span at index 2 ("How ") to index 1
        executor.execute_move(book_id="b1", presentation_index_from=2, presentation_index_to=1)

        # -- Verify: export order changed
        script_after_move = export_annotated_script("b1", storage)
        assert len(script_after_move) == 3
        assert script_after_move[0]["text"] == "How "
        assert script_after_move[0]["speaker"] == "NARRATOR"
        assert script_after_move[1]["text"] == "Hello!"
        assert script_after_move[1]["speaker"] == "Alice"
        assert script_after_move[2]["text"] == "Goodbye."
        assert script_after_move[2]["speaker"] == "NARRATOR"

        # -- Delete span at index 1 ("How ")
        executor.execute_delete(book_id="b1", presentation_index=1)

        # -- Verify: 2 spans remain, correct order
        span_count_after_delete = conn.execute("SELECT COUNT(*) FROM span").fetchone()[0]
        assert span_count_after_delete == 2

        script_after_delete = export_annotated_script("b1", storage)
        assert len(script_after_delete) == 2
        assert script_after_delete[0]["text"] == "Hello!"
        assert script_after_delete[0]["speaker"] == "Alice"
        assert script_after_delete[1]["text"] == "Goodbye."
        assert script_after_delete[1]["speaker"] == "NARRATOR"


# ---------------------------------------------------------------------------
# Test 5: Confidence filter integration
# ---------------------------------------------------------------------------


class TestConfidenceFilterIntegration:
    """Verify review items are filtered by confidence band and can be overridden."""

    def test_confidence_filter_integration(self, storage):
        """Verify review items filtered by confidence band, then override."""
        conn = storage.get_connection()

        # -- Spine: 1 book, 1 chapter, 1 scene, 1 paragraph, 3 spans
        conn.execute("INSERT INTO series VALUES ('s1')")
        conn.execute(
            "INSERT INTO book (id, series_id, book_number, version, position) "
            "VALUES ('b1', 's1', 1, 1, 1)"
        )
        conn.execute("INSERT INTO chapter (id, book_id) VALUES ('c1', 'b1')")
        conn.execute("INSERT INTO scene VALUES ('sc1')")
        conn.execute("INSERT INTO paragraph VALUES ('p1')")

        conn.execute(
            "INSERT INTO span (id, span_type, instruct, text) "
            "VALUES ('sp1', 'quotation', NULL, 'Hello!')"
        )
        conn.execute(
            "INSERT INTO span (id, span_type, instruct, text) "
            "VALUES ('sp2', 'quotation', NULL, 'How are you?')"
        )
        conn.execute(
            "INSERT INTO span (id, span_type, instruct, text) "
            "VALUES ('sp3', 'quotation', NULL, 'Goodbye.')"
        )

        # -- Edge tables
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

        # -- 3 characters
        conn.execute(
            "INSERT INTO character (id, name, aliases, voice_assignment_id, description) "
            "VALUES ('c1', 'Alice', '[]', NULL, NULL)"
        )
        conn.execute(
            "INSERT INTO character (id, name, aliases, voice_assignment_id, description) "
            "VALUES ('c2', 'Bob', '[]', NULL, NULL)"
        )
        conn.execute(
            "INSERT INTO character (id, name, aliases, voice_assignment_id, description) "
            "VALUES ('c3', 'Charlie', '[]', NULL, NULL)"
        )

        # -- character_span junctions with different confidences
        # c1/sp1: confidence 0.3 (below band — not in review)
        conn.execute(
            "INSERT INTO character_span "
            "(character_id, span_id, relation_type, source, confidence, human_override) "
            "VALUES ('c1', 'sp1', 'speaker', 'walk', 0.3, 0)"
        )
        # c2/sp2: confidence 0.6 (in band — in review)
        conn.execute(
            "INSERT INTO character_span "
            "(character_id, span_id, relation_type, source, confidence, human_override) "
            "VALUES ('c2', 'sp2', 'speaker', 'walk', 0.6, 0)"
        )
        # c3/sp3: confidence 0.8 (above band — not in review)
        conn.execute(
            "INSERT INTO character_span "
            "(character_id, span_id, relation_type, source, confidence, human_override) "
            "VALUES ('c3', 'sp3', 'speaker', 'walk', 0.8, 0)"
        )

        # -- ReviewManager: verify only the 0.6 item is returned
        manager = ReviewManager(storage)
        items = manager.get_review_items("b1")

        span_items = [i for i in items if i["junction_table"] == "character_span"]
        assert len(span_items) == 1
        assert span_items[0]["character_name"] == "Bob"
        assert span_items[0]["confidence"] == 0.6

        # -- Override the 0.6 item with {"relation_type": "mentioned"}
        item_id = span_items[0]["item_id"]
        manager.override_review_item(item_id, {"relation_type": "mentioned"})

        # -- Verify: confidence becomes 1.0, relation_type changes to "mentioned"
        rows = storage.execute_query(
            "SELECT confidence, relation_type, human_override FROM character_span "
            "WHERE character_id = 'c2' AND span_id = 'sp2'"
        )
        assert len(rows) == 1
        assert rows[0]["confidence"] == 1.0
        assert rows[0]["relation_type"] == "mentioned"
        assert rows[0]["human_override"] == 1
