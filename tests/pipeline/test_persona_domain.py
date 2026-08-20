"""Domain + storage tests for the persona revision foundation.

Covers the S1 acceptance criteria:

* Both adapters implement the same persona and prompt-revision contracts
  (append-only, owner/book bounded, supersede) and the domain maps contention
  through the existing transaction errors.
* Persona writes are append-only, revision-checked, preserve protected-head
  evidence while allowing explicit unlocks, and derive explainable voice
   consequences without assigning a voice.
* Invalid anchors, aliases, scene scopes, and review states are rejected
  deterministically.

Uses ``InMemorySQLiteAdapter`` (a full ``PipelineStorage``) for the domain
behaviour and both adapters for the storage parity checks.
"""

from __future__ import annotations

import pytest

from app.pipeline.adapter import InMemorySQLiteAdapter, SQLiteAdapter
from app.pipeline.persona import (
    BookNotFoundError,
    CharacterNotFoundError,
    PersonaDomain,
    ProtectedRevisionError,
    StaleRevisionError,
    ValidationError,
)


def _seed(storage) -> None:
    """Minimal seed: one book, three characters, two scenes, one spine span."""
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
    # spine rows used by the stable-anchor / scene-reachability tests
    c.execute("INSERT INTO chapter (id, book_id) VALUES ('ch1', 'b1')")
    c.execute("INSERT INTO paragraph (id) VALUES ('p1')")
    c.execute("INSERT INTO span (id, span_type, text) VALUES ('sp1', 'sentence', 'Hello')")
    c.execute("INSERT INTO span (id, span_type, text) VALUES ('sp2', 'sentence', 'World')")
    c.execute("INSERT INTO book_chapter (child_id, parent_id, position) VALUES ('ch1', 'b1', 0)")
    c.execute("INSERT INTO chapter_scene (child_id, parent_id, position) VALUES ('sc1', 'ch1', 0)")
    c.execute("INSERT INTO scene_paragraph (child_id, parent_id, position) VALUES ('p1', 'sc1', 0)")
    c.execute("INSERT INTO paragraph_span (child_id, parent_id, position) VALUES ('sp1', 'p1', 0)")
    c.execute("INSERT INTO paragraph_span (child_id, parent_id, position) VALUES ('sp2', 'p1', 1)")


@pytest.fixture
def storage():
    storage = InMemorySQLiteAdapter()
    storage.init_db()
    _seed(storage)
    return storage


@pytest.fixture
def domain(storage):
    return PersonaDomain(storage)


def _write(**overrides) -> dict:
    write = {
        "character_id": "c1",
        "book_id": "b1",
        "fields": {"identity": "a weathered mariner", "speech": "slow, deliberate"},
        "evidence": [
            {
                "anchor": "sp1",
                "quote": "Hello",
                "source": "walk_2b_character_discovery",
                "confidence": 0.9,
            }
        ],
        "aliases": ["Old Salt"],
        "scene_scope": "book",
        "scene_ids": [],
        "review_state": "draft",
        "protected": False,
    }
    write.update(overrides)
    return write


# ---------------------------------------------------------------------------
# Validation — deterministic rejection of bad scope / review / alias / anchor
# ---------------------------------------------------------------------------


def test_validate_rejects_unknown_scene_scope(domain):
    result = domain.validate(_write(scene_scope="planet"))
    assert result["valid"] is False
    assert any("scene_scope" in e for e in result["errors"])


def test_validate_rejects_bad_review_state(domain):
    result = domain.validate(_write(review_state="published"))
    assert result["valid"] is False
    assert any("review_state" in e for e in result["errors"])


def test_validate_rejects_blank_alias(domain):
    result = domain.validate(_write(aliases=["", "  "]))
    assert result["valid"] is False
    assert any("alias" in e for e in result["errors"])


def test_validate_rejects_unreachable_anchor(domain):
    result = domain.validate(
        _write(evidence=[{"anchor": "sp999", "source": "walk_2b_character_discovery"}])
    )
    assert result["valid"] is False
    assert any("anchor" in e for e in result["errors"])


def test_validate_rejects_unknown_field_key(domain):
    result = domain.validate(_write(fields={"hobbies": "sailing"}))
    assert result["valid"] is False
    assert any("field" in e for e in result["errors"])


def test_validate_rejects_non_dict_fields(domain):
    result = domain.validate(_write(fields="not a dict"))
    assert result["valid"] is False


def test_validate_rejects_unreachable_scene_id(domain):
    # sc2 exists but is not reachable from b1 (no book_chapter/chapter_scene edge)
    result = domain.validate(
        _write(scene_scope="scenes", scene_ids=["sc2"])
    )
    assert result["valid"] is False
    assert any("scene" in e for e in result["errors"])


def test_validate_rejects_empty_scenes_scope(domain):
    result = domain.validate(_write(scene_scope="scenes", scene_ids=[]))
    assert result["valid"] is False


def test_validate_accepts_valid_book_scope(domain):
    result = domain.validate(_write())
    assert result["valid"] is True
    assert result["errors"] == []


def test_validate_accepts_valid_scenes_scope(domain):
    result = domain.validate(
        _write(scene_scope="scenes", scene_ids=["sc1"])
    )
    assert result["valid"] is True


def test_validate_is_side_effect_free(storage, domain):
    domain.validate(_write())
    assert storage.execute_query(
        "SELECT COUNT(*) AS n FROM persona_revision"
    )[0]["n"] == 0


# ---------------------------------------------------------------------------
# Append-only revisions + base citation
# ---------------------------------------------------------------------------


def test_save_creates_first_revision(domain):
    persona = domain.save(_write(), base_revision=0)
    assert persona["revision"] == 1
    assert persona["character_id"] == "c1"
    assert persona["superseded_by"] is None


def test_save_append_only_supersedes_previous(domain):
    first = domain.save(_write(), base_revision=0)
    second = domain.save(_write(fields={"identity": "an even older salt"}), base_revision=1)
    assert second["revision"] == 2
    assert domain.get_revision(first["persona_id"])["superseded_by"] == second["persona_id"]
    assert second["superseded_by"] is None
    revisions = domain.list_revisions("c1")
    assert len(revisions) == 2
    # newest first
    assert revisions[0]["revision"] == 2
    assert revisions[1]["revision"] == 1


def test_save_stale_base_revision_rejected(domain):
    domain.save(_write(), base_revision=0)
    with pytest.raises(StaleRevisionError):
        domain.save(_write(), base_revision=0)


def test_save_requires_base_revision_0_for_fresh(domain):
    with pytest.raises(StaleRevisionError):
        domain.save(_write(), base_revision=1)


def test_save_character_not_found(domain):
    with pytest.raises(CharacterNotFoundError):
        domain.save(_write(character_id="ghost"), base_revision=0)


def test_save_book_not_found(domain):
    with pytest.raises(BookNotFoundError):
        domain.save(_write(book_id="missing"), base_revision=0)


def test_save_validation_rejection_does_not_write(domain):
    with pytest.raises(ValidationError):
        domain.save(_write(review_state="bogus"), base_revision=0)
    assert domain.list_revisions("c1") == []


# ---------------------------------------------------------------------------
# Protected records — unlockable on human edit, rejected for rerun
# ---------------------------------------------------------------------------


def test_protected_revision_can_be_unlocked_on_human_edit(domain):
    first = domain.save(_write(protected=True), base_revision=0)
    second = domain.save(_write(), base_revision=1, source="human")
    # Evidence carries forward, but the explicit unchecked flag unlocks the
    # newly-created revision.
    assert second["protected"] is False
    assert second["evidence"] == first["evidence"]


def test_rerun_cannot_replace_protected_revision(domain):
    domain.save(_write(protected=True), base_revision=0)
    with pytest.raises(ProtectedRevisionError):
        domain.save(_write(), base_revision=1, source="rerun")


def test_rerun_allowed_when_not_protected(domain):
    first = domain.save(_write(protected=False), base_revision=0)
    second = domain.save(_write(), base_revision=1, source="rerun")
    assert second["revision"] == 2
    assert domain.get_revision(first["persona_id"])["superseded_by"] == second["persona_id"]


# ---------------------------------------------------------------------------
# Voice consequences — derived, explainable, never assigns a voice
# ---------------------------------------------------------------------------


def test_voice_consequences_never_assigns_voice(domain):
    persona = domain.save(_write(), base_revision=0)
    consequences = persona["voice_consequences"]
    assert consequences is not None
    # no resolved voice_config id is ever produced
    assert consequences.get("assignment") is None
    assert isinstance(consequences.get("explanation"), str)
    assert consequences["explanation"]


def test_voice_consequences_reflects_fields(domain):
    result = domain.validate(_write(fields={"speech": "rapid, clipped"}))
    assert result["valid"] is True
    explanation = result["voice_consequences"]["explanation"]
    assert "speech" in explanation


# ---------------------------------------------------------------------------
# Prompt-config revision storage parity (both adapters)
# ---------------------------------------------------------------------------


def _prompt_record(**overrides) -> dict:
    record = {
        "revision_id": "pc-1",
        "book_id": "b1",
        "task": "scene_segmentation",
        "base_revision": None,
        "source_layers_json": '{"on_disk": true}',
        "effective_prompt": "Split the chapter into scenes.",
        "settings_json": '{"temperature": 0.0}',
        "raw_json": None,
        "validation_json": '{"valid": true}',
        "author_id": "local",
        "created_ms": 1,
        "superseded_by": None,
    }
    record.update(overrides)
    return record


@pytest.mark.parametrize("adapter_cls", [InMemorySQLiteAdapter, SQLiteAdapter])
def test_prompt_config_revision_storage_parity(adapter_cls, tmp_path):
    if adapter_cls is SQLiteAdapter:
        storage = adapter_cls(str(tmp_path / "p.db"))
    else:
        storage = adapter_cls()
    storage.init_db()
    _seed(storage)
    storage.insert_prompt_config_revision(_prompt_record())
    row = storage.get_prompt_config_revision("pc-1")
    assert row is not None
    assert row["task"] == "scene_segmentation"
    assert row["book_id"] == "b1"
    assert row["effective_prompt"] == "Split the chapter into scenes."
    assert row["superseded_by"] is None

    storage.insert_prompt_config_revision(
        _prompt_record(revision_id="pc-2", base_revision="pc-1", created_ms=2)
    )
    storage.supersede_prompt_config_revision("pc-1", "pc-2")
    rows = storage.list_prompt_config_revisions("b1", "scene_segmentation")
    assert [r["revision_id"] for r in rows] == ["pc-2", "pc-1"]
    assert rows[1]["superseded_by"] == "pc-2"


@pytest.mark.parametrize("adapter_cls", [InMemorySQLiteAdapter, SQLiteAdapter])
def test_persona_revision_storage_parity(adapter_cls, tmp_path):
    if adapter_cls is SQLiteAdapter:
        storage = adapter_cls(str(tmp_path / "p2.db"))
    else:
        storage = adapter_cls()
    storage.init_db()
    _seed(storage)
    domain = PersonaDomain(storage)
    first = domain.save(_write(), base_revision=0)
    second = domain.save(_write(fields={"identity": "v2"}), base_revision=1)

    assert domain.get_revision(first["persona_id"])["superseded_by"] == second["persona_id"]
    assert domain.get_revision("nope") is None
    listed = domain.list_revisions("c1")
    assert [p["revision"] for p in listed] == [2, 1]


def test_list_revisions_empty_for_unknown_character(domain):
    assert domain.list_revisions("canon") == []


def test_revision_roundtrip_scene_ids(domain):
    persona = domain.save(
        _write(scene_scope="scenes", scene_ids=["sc1"]), base_revision=0
    )
    loaded = domain.get_revision(persona["persona_id"])
    assert loaded["scene_scope"] == "scenes"
    assert loaded["scene_ids"] == ["sc1"]
