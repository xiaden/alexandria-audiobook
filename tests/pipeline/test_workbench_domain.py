"""Domain tests for the workbench storage/domain service (walks 2b/2c/2d).

Covers the S1 acceptance criteria:

* Sole per-book revision allocation uses BEGIN IMMEDIATE semantics and stale
  writes are rejected (StaleRevisionError).
* Domain operations preserve manual decisions/absence, support reversible alias
  consequences, and expose stable anchors.
* Generated/manual projections remain separate and disagreement is surfaced as
  a conflict (not a duplicate insert).

Uses ``InMemorySQLiteAdapter`` (a full ``PipelineStorage``) so the BEGIN
IMMEDIATE allocator path is exercised end-to-end.
"""

from __future__ import annotations

import pytest

from app.pipeline.adapter import InMemorySQLiteAdapter
from app.pipeline.workbench import (
    BookNotFoundError,
    ConflictError,
    PreviewExpiredError,
    StaleRevisionError,
    ValidationError,
    Workbench,
)


@pytest.fixture
def wb():
    """A fresh workbench over an in-memory storage with a minimal seed."""
    storage = InMemorySQLiteAdapter()
    storage.init_db()
    c = storage.get_connection()
    c.execute("INSERT INTO series (id) VALUES ('s1')")
    c.execute(
        "INSERT INTO book (id, series_id, book_number, version, position)"
        " VALUES ('b1', 's1', 1, 0, 0)"
    )
    c.execute("INSERT INTO character (id, name, aliases) VALUES ('c1', 'Alice', '[]')")
    c.execute("INSERT INTO character (id, name, aliases) VALUES ('c2', 'Bob', '[]')")
    c.execute("INSERT INTO character (id, name, aliases) VALUES ('canon', 'Canon', '[]')")
    c.execute("INSERT INTO scene (id) VALUES ('sc1')")
    c.execute("INSERT INTO scene (id) VALUES ('sc2')")
    c.execute(
        "INSERT INTO walk_run (run_id, book_id, walk_name, status)"
        " VALUES ('run-1', 'b1', 'walk_2d_scene_presence', 'completed')"
    )
    # spine rows used by the stable-anchor tests
    c.execute("INSERT INTO chapter (id, book_id) VALUES ('ch1', 'b1')")
    c.execute("INSERT INTO paragraph (id) VALUES ('p1')")
    c.execute("INSERT INTO span (id, span_type, text) VALUES ('sp1', 'sentence', 'Hello')")
    c.execute("INSERT INTO span (id, span_type, text) VALUES ('sp2', 'sentence', 'World')")
    c.execute("INSERT INTO book_chapter (child_id, parent_id, position) VALUES ('ch1', 'b1', 0)")
    c.execute("INSERT INTO chapter_scene (child_id, parent_id, position) VALUES ('sc1', 'ch1', 0)")
    c.execute("INSERT INTO scene_paragraph (child_id, parent_id, position) VALUES ('p1', 'sc1', 0)")
    c.execute("INSERT INTO paragraph_span (child_id, parent_id, position) VALUES ('sp1', 'p1', 0)")
    c.execute("INSERT INTO paragraph_span (child_id, parent_id, position) VALUES ('sp2', 'p1', 1)")
    return Workbench(storage)


def _backfill(wb: Workbench, book_id: str, revision: int) -> None:
    """Create an initial generation row at *revision* (as the migration does)."""
    wb._storage.execute_insert(
        "INSERT INTO workbench_generation (generation_id, book_id, revision, updated_ms)"
        " VALUES (?, ?, ?, 0)",
        (f"wg-{book_id}", book_id, revision),
    )


# ---------------------------------------------------------------------------
# Sole per-book revision allocation + stale-write rejection
# ---------------------------------------------------------------------------


def test_allocate_revision_monotonic_and_stale_rejected(wb):
    _backfill(wb, "b1", 0)
    assert wb.allocate_revision("b1") == 1
    assert wb.allocate_revision("b1") == 2
    assert wb.get_revision("b1") == 2
    # stale base_revision is rejected
    with pytest.raises(StaleRevisionError):
        wb.check_revision("b1", 1)


def test_allocate_revision_is_per_book(wb):
    _backfill(wb, "b1", 0)
    wb._storage.execute_insert(
        "INSERT INTO workbench_generation (generation_id, book_id, revision, updated_ms)"
        " VALUES ('wg-b2', 'b2', 0, 0)",
        (),
    )
    assert wb.allocate_revision("b1") == 1
    assert wb.allocate_revision("b2") == 1  # independent counters


def test_allocate_revision_rolls_back_with_transaction(wb):
    _backfill(wb, "b1", 0)
    try:
        with wb._storage.transaction():
            rev = wb.allocate_revision("b1")
            assert rev == 1
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    # the increment was rolled back with the enclosing write
    assert wb.get_revision("b1") == 0


def test_allocate_revision_returns_0_for_fresh_book(wb):
    # no backfilled generation row -> first allocation seeds at revision 0
    assert wb.allocate_revision("b1") == 0
    assert wb.allocate_revision("b1") == 1


# ---------------------------------------------------------------------------
# Presence / absence: preservation and tombstone authority
# ---------------------------------------------------------------------------


def test_presence_stale_base_revision_rejected(wb):
    _backfill(wb, "b1", 0)
    wb.set_presence(
        book_id="b1", scene_id="sc1", character_id="c1",
        relation_type="present", base_revision=0,
    )
    with pytest.raises(StaleRevisionError):
        wb.set_presence(
            book_id="b1", scene_id="sc1", character_id="c1",
            relation_type="speaker", base_revision=0,  # stale
        )


def test_unknown_book_rejected(wb):
    with pytest.raises(BookNotFoundError):
        wb.set_presence(
            book_id="nope", scene_id="sc1", character_id="c1",
            relation_type="present", base_revision=0,
        )


def test_absent_creates_active_tombstone_and_restore_deactivates(wb):
    _backfill(wb, "b1", 0)
    absent = wb.set_presence(
        book_id="b1", scene_id="sc1", character_id="c1",
        relation_type="absent", base_revision=0,
    )
    assert absent["relation_type"] == "absent"
    # absence tombstone is active
    rows = wb._storage.execute_query(
        "SELECT active FROM character_scene_absence"
        " WHERE book_id='b1' AND scene_id='sc1' AND character_id='c1'"
    )
    assert rows[0]["active"] == 1
    # projection reports absent (tombstone authoritative)
    pres = {r["character_id"]: r for r in wb.get_presence("b1")}
    assert pres["c1"]["relation_type"] == "absent"

    # restoring presence deactivates the tombstone in the same transaction
    restored = wb.set_presence(
        book_id="b1", scene_id="sc1", character_id="c1",
        relation_type="present", base_revision=absent["generation_revision"],
    )
    rows = wb._storage.execute_query(
        "SELECT active FROM character_scene_absence"
        " WHERE book_id='b1' AND scene_id='sc1' AND character_id='c1'"
    )
    assert rows[0]["active"] == 0
    pres = {r["character_id"]: r for r in wb.get_presence("b1")}
    assert pres["c1"]["relation_type"] == "present"


def test_absence_tombstone_never_removed_by_restore(wb):
    """Restoring presence deactivates but does not delete the tombstone."""
    _backfill(wb, "b1", 0)
    wb.set_presence(book_id="b1", scene_id="sc1", character_id="c1",
                    relation_type="absent", base_revision=0)
    rev = wb.get_revision("b1")
    wb.set_presence(book_id="b1", scene_id="sc1", character_id="c1",
                    relation_type="present", base_revision=rev)
    rows = wb._storage.execute_query(
        "SELECT count(*) AS n FROM character_scene_absence"
        " WHERE book_id='b1' AND scene_id='sc1' AND character_id='c1'"
    )
    assert rows[0]["n"] == 1  # one tombstone row, deactivated


def test_invalid_relation_type_rejected(wb):
    with pytest.raises(ValidationError):
        wb.set_presence(book_id="b1", scene_id="sc1", character_id="c1",
                        relation_type="witness", base_revision=0)


# ---------------------------------------------------------------------------
# Generated vs manual projections: separation and conflict
# ---------------------------------------------------------------------------


def _add_generated(wb, character_id, scene_id, relation_type, confidence=0.9):
    wb._storage.execute_insert(
        "INSERT INTO character_scene_generated"
        " (id, book_id, character_id, scene_id, relation_type, confidence,"
        "  generation_revision, source_run_id)"
        " VALUES (?, 'b1', ?, ?, ?, ?, 1, 'run-1')",
        (f"g-{character_id}-{scene_id}-{relation_type}", character_id,
         scene_id, relation_type, confidence),
    )


def test_projection_manual_wins_over_generated(wb):
    _backfill(wb, "b1", 0)
    _add_generated(wb, "c1", "sc1", "present")
    wb.set_presence(book_id="b1", scene_id="sc1", character_id="c1",
                    relation_type="speaker", base_revision=0)
    pres = {r["character_id"]: r for r in wb.get_presence("b1")}
    assert pres["c1"]["relation_type"] == "speaker"  # manual wins
    assert pres["c1"]["source"] == "human"


def test_generated_row_present_when_no_manual(wb):
    _backfill(wb, "b1", 0)
    _add_generated(wb, "c2", "sc2", "present", confidence=0.7)
    pres = {r["character_id"]: r for r in wb.get_presence("b1")}
    assert pres["c2"]["relation_type"] == "present"
    assert pres["c2"]["source"] == "walk"
    assert pres["c2"]["confidence"] == 0.7


def test_disagreement_is_conflict_not_duplicate(wb):
    _backfill(wb, "b1", 0)
    _add_generated(wb, "c1", "sc1", "present")
    # human disagrees -> speaker
    wb.set_presence(book_id="b1", scene_id="sc1", character_id="c1",
                    relation_type="speaker", base_revision=0)
    conflicts = wb.get_conflicts("b1")
    assert len(conflicts) == 1
    conf = conflicts[0]
    assert conf["code"] == "projection_disagreement"
    assert conf["current_value"] == "speaker"  # manual stays effective
    assert conf["requested_value"] == "present"  # generated disagrees


def test_absence_suppresses_conflict(wb):
    _backfill(wb, "b1", 0)
    _add_generated(wb, "c1", "sc1", "present")
    wb.set_presence(book_id="b1", scene_id="sc1", character_id="c1",
                    relation_type="absent", base_revision=0)
    assert wb.get_conflicts("b1") == []


def test_manual_and_generated_tables_stay_separate(wb):
    _backfill(wb, "b1", 0)
    _add_generated(wb, "c1", "sc1", "present")
    wb.set_presence(book_id="b1", scene_id="sc1", character_id="c1",
                    relation_type="absent", base_revision=0)
    # generated row untouched (never re-added/deleted by a human decision)
    gen = wb.get_generated_rows("b1")
    man = wb.get_manual_rows("b1")
    assert len(gen) == 1 and gen[0]["relation_type"] == "present"
    assert len(man) == 1 and man[0]["relation_type"] == "absent"


# ---------------------------------------------------------------------------
# Append-only decisions / provenance
# ---------------------------------------------------------------------------


def test_decisions_are_append_only(wb):
    _backfill(wb, "b1", 0)
    wb.set_presence(book_id="b1", scene_id="sc1", character_id="c1",
                    relation_type="absent", base_revision=0)
    wb.set_presence(book_id="b1", scene_id="sc1", character_id="c1",
                    relation_type="present", base_revision=1)
    decisions = wb.list_decisions("b1")
    assert len(decisions) == 2  # both preserved; none rewritten
    assert all(d["status"] == "active" for d in decisions)


# ---------------------------------------------------------------------------
# Stable anchors
# ---------------------------------------------------------------------------


def test_stable_anchors_expose_paragraph_offsets(wb):
    anchors = wb.get_stable_anchors("b1")
    # two spans in one paragraph: sp1 "Hello" (0-5), sp2 "World" (5-10)
    assert len(anchors) == 2
    a1 = next(a for a in anchors if a["span_id"] == "sp1")
    a2 = next(a for a in anchors if a["span_id"] == "sp2")
    assert a1 == {
        "book_id": "b1", "scene_id": "sc1", "chapter_id": "ch1",
        "paragraph_id": "p1", "span_id": "sp1",
        "start_offset": 0, "end_offset": 5,
    }
    assert a2["start_offset"] == 5 and a2["end_offset"] == 10


# ---------------------------------------------------------------------------
# Alias preview / commit / reversible unmerge
# ---------------------------------------------------------------------------


def test_alias_preview_and_commit_then_unmerge_restores(wb):
    _backfill(wb, "b1", 0)
    preview = wb.preview_alias_conversion(
        book_id="b1", canonical_id="canon", member_ids=["c1"], base_revision=0,
    )
    assert preview["base_revision"] == 0
    assert preview["preview_token"].startswith("ap-")
    commit = wb.commit_alias_conversion(
        book_id="b1", preview_token=preview["preview_token"],
        base_revision=0, confirm_consequences=True,
    )
    assert commit["status"] == "active"
    assert commit["merge_ids"]
    assert commit["conflict"] is False
    # active merge recorded with reversible prior state
    row = wb._storage.execute_query(
        "SELECT * FROM character_alias_merge WHERE merge_id = ?",
        (commit["merge_ids"][0],),
    )[0]
    assert row["status"] == "active"
    assert row["prior_member_name"] == "Alice"
    assert row["canonical_id"] == "canon" and row["member_id"] == "c1"

    # unmerge creates a new decision, marks merge undone, restores member
    unmerge = wb.unmerge_alias(
        book_id="b1", merge_id=commit["merge_ids"][0],
        base_revision=commit["generation_revision"],
    )
    assert unmerge["status"] == "undone"
    rows = wb._storage.execute_query(
        "SELECT status FROM character_alias_merge WHERE merge_id = ?",
        (commit["merge_ids"][0],),
    )
    assert rows[0]["status"] == "undone"
    # both merge + unmerge decisions are preserved (append-only)
    decisions = wb.list_decisions("b1")
    assert any(d["decision_type"] == "alias_merge:merge" for d in decisions)
    assert any(d["decision_type"] == "alias_merge:unmerge" for d in decisions)


def test_alias_preview_token_is_single_use(wb):
    _backfill(wb, "b1", 0)
    preview = wb.preview_alias_conversion(
        book_id="b1", canonical_id="canon", member_ids=["c1"], base_revision=0,
    )
    wb.commit_alias_conversion(
        book_id="b1", preview_token=preview["preview_token"],
        base_revision=0, confirm_consequences=True,
    )
    # second use of the same token is rejected
    with pytest.raises(PreviewExpiredError):
        wb.commit_alias_conversion(
            book_id="b1", preview_token=preview["preview_token"],
            base_revision=0, confirm_consequences=True,
        )


def test_alias_preview_requires_confirmation(wb):
    _backfill(wb, "b1", 0)
    preview = wb.preview_alias_conversion(
        book_id="b1", canonical_id="canon", member_ids=["c1"], base_revision=0,
    )
    with pytest.raises(ValidationError):
        wb.commit_alias_conversion(
            book_id="b1", preview_token=preview["preview_token"],
            base_revision=0, confirm_consequences=False,
        )


def test_alias_commit_rejects_affected_set_drift(wb):
    _backfill(wb, "b1", 0)
    preview = wb.preview_alias_conversion(
        book_id="b1", canonical_id="canon", member_ids=["c1"], base_revision=0,
    )
    # a competing presence decision changes the affected-row set before commit
    wb.set_presence(book_id="b1", scene_id="sc1", character_id="c1",
                    relation_type="present", base_revision=0)
    with pytest.raises(StaleRevisionError):
        wb.commit_alias_conversion(
            book_id="b1", preview_token=preview["preview_token"],
            base_revision=0, confirm_consequences=True,
        )


def test_alias_expired_preview_rejected(wb):
    _backfill(wb, "b1", 0)
    preview = wb.preview_alias_conversion(
        book_id="b1", canonical_id="canon", member_ids=["c1"], base_revision=0,
    )
    wb._previews[preview["preview_token"]]["expires_ms"] = 0  # force expiry
    with pytest.raises(PreviewExpiredError):
        wb.commit_alias_conversion(
            book_id="b1", preview_token=preview["preview_token"],
            base_revision=0, confirm_consequences=True,
        )


def test_unmerge_of_non_active_merge_conflicts(wb):
    _backfill(wb, "b1", 0)
    preview = wb.preview_alias_conversion(
        book_id="b1", canonical_id="canon", member_ids=["c1"], base_revision=0,
    )
    commit = wb.commit_alias_conversion(
        book_id="b1", preview_token=preview["preview_token"],
        base_revision=0, confirm_consequences=True,
    )
    wb.unmerge_alias(book_id="b1", merge_id=commit["merge_ids"][0],
                     base_revision=commit["generation_revision"])
    with pytest.raises(ConflictError):
        wb.unmerge_alias(book_id="b1", merge_id=commit["merge_ids"][0],
                         base_revision=commit["generation_revision"] + 1)


# ---------------------------------------------------------------------------
# Boundary overrides
# ---------------------------------------------------------------------------


def test_boundary_override_create_and_deactivate(wb):
    _backfill(wb, "b1", 0)
    dto = wb.put_boundary_override(
        book_id="b1",
        anchor={"chapter_id": "ch1"},
        payload={"operation": "split", "boundary_offsets": [2, 4]},
        base_revision=0,
    )
    assert dto["anchor"] == {"chapter_id": "ch1", "scene_id": None, "paragraph_id": None}
    assert dto["active"] is True
    overrides = wb.get_boundary_overrides("b1")
    assert len(overrides) == 1
    assert overrides[0]["payload"]["operation"] == "split"

    deact = wb.deactivate_boundary_override(
        book_id="b1", override_id=dto["override_id"],
        base_revision=dto["generation_revision"],
    )
    assert deact["active"] is False
    assert wb.get_boundary_overrides("b1") == []  # active-only projection


def test_boundary_override_requires_an_anchor(wb):
    _backfill(wb, "b1", 0)
    with pytest.raises(ValidationError):
        wb.put_boundary_override(
            book_id="b1", anchor={}, base_revision=0,
            payload={"operation": "merge", "boundary_offsets": [1]},
        )


def test_boundary_override_rejects_unreachable_anchor(wb):
    _backfill(wb, "b1", 0)
    with pytest.raises(ValidationError):
        wb.put_boundary_override(
            book_id="b1", anchor={"scene_id": "sc2"},
            base_revision=0,
            payload={"operation": "resegment", "boundary_offsets": [1]},
        )


def test_boundary_override_rejects_bad_operation(wb):
    _backfill(wb, "b1", 0)
    with pytest.raises(ValidationError):
        wb.put_boundary_override(
            book_id="b1", anchor={"chapter_id": "ch1"}, base_revision=0,
            payload={"operation": "splice", "boundary_offsets": [1]},
        )


# ---------------------------------------------------------------------------
# Config overrides + effective config model
# ---------------------------------------------------------------------------


def test_put_override_validates_key_and_value(wb):
    _backfill(wb, "b1", 0)
    with pytest.raises(ValidationError):
        wb.put_override(book_id="b1", walk_name="walk_2b_character_discovery",
                        key="api_key", value="secret", base_revision=0)
    with pytest.raises(ValidationError):
        wb.put_override(book_id="b1", walk_name="walk_2b_character_discovery",
                        key="temperature", value=2.5, base_revision=0)


def test_put_override_roundtrip_and_effective_source(wb):
    _backfill(wb, "b1", 0)
    res = wb.put_override(book_id="b1", walk_name="walk_2d_scene_presence",
                          key="temperature", value=0.3, base_revision=0)
    assert res["generation_revision"] == 1
    overrides = wb.get_overrides("b1")
    assert overrides[0]["value"] == 0.3
    eff = wb.resolve_effective_config("b1", "walk_2d_scene_presence")
    assert eff["values"]["temperature"] == 0.3
    assert eff["sources"]["temperature"] == "row"


def test_delete_override_restores_effective_value(wb):
    _backfill(wb, "b1", 0)
    wb.put_override(book_id="b1", walk_name="walk_2d_scene_presence",
                    key="temperature", value=0.3, base_revision=0)
    rev = wb.get_revision("b1")
    wb.delete_override(book_id="b1", walk_name="walk_2d_scene_presence",
                       key="temperature", base_revision=rev)
    eff = wb.resolve_effective_config("b1", "walk_2d_scene_presence")
    assert eff["sources"]["temperature"] == "fallback"
    assert wb.get_overrides("b1") == []


def test_effective_config_has_all_fields_and_sources(wb):
    eff = wb.resolve_effective_config("b1", "walk_2b_character_discovery")
    assert set(eff["values"]) == {"model_name", "reasoning_effort", "temperature", "prompt"}
    assert set(eff["sources"]) == {"model_name", "reasoning_effort", "temperature", "prompt"}
    # defaults are all fallback with no DB row or config set
    assert eff["sources"]["model_name"] == "fallback"
