"""Workbench-native rerun-safety tests for Walks 2b/2c/2d.

Covers the S3 acceptance criteria:
- 2b and 2d reruns do not duplicate generated rows and never re-add an
  active human absence.
- 2c remains GLOBAL and records reversible merge consequences.
- The invalidation DAG is 2b→2c+2d, 2c→2d, 2d→none.
- Protected manual decisions survive runs and provenance stays queryable.
"""

import json
import time
import uuid
from unittest.mock import Mock

import pytest

from app.pipeline.adapter import InMemorySQLiteAdapter
from app.pipeline.populate import populate_initial_spine
from app.pipeline.walks.walk_2b_character_discovery import execute as execute_2b
from app.pipeline.walks.walk_2c_alias_resolution import execute as execute_2c
from app.pipeline.walks.walk_2d_scene_presence import execute as execute_2d

# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def storage():
    adapter = InMemorySQLiteAdapter()
    adapter.init_db()
    return adapter


@pytest.fixture
def sample_chapters():
    """Two chapters, each with paragraphs+spans (one placeholder scene each)."""
    return [
        {
            "id": "chapter-1",
            "paragraphs": [
                {
                    "id": "para-1",
                    "spans": [
                        {"id": "span-1a", "span_type": "sentence", "text": "The sun rose."},
                        {"id": "span-1b", "span_type": "quotation", "text": '"Hi," said John.'},
                    ],
                },
                {
                    "id": "para-2",
                    "spans": [
                        {"id": "span-2a", "span_type": "sentence", "text": "Mary waved."},
                    ],
                },
            ],
        },
        {
            "id": "chapter-2",
            "paragraphs": [
                {
                    "id": "para-3",
                    "spans": [
                        {"id": "span-3a", "span_type": "sentence", "text": "Later, night fell."},
                    ],
                },
            ],
        },
    ]


@pytest.fixture
def populated_storage(storage, sample_chapters):
    populate_initial_spine("series-1", "book-1", sample_chapters, storage)
    return storage


def _make_response(content):
    r = Mock()
    r.choices = [Mock(message=Mock(content=content))]
    return r


def _patch_2b(monkeypatch, content):
    client = Mock()
    client.chat.completions.create.return_value = _make_response(content)
    monkeypatch.setattr(
        "app.utils.create_llm_client", lambda config_path=None: (client, "test-model")
    )
    monkeypatch.setattr(
        "app.utils.resolve_task_config",
        lambda task, storage, book_id: {
            "model_name": "test-model",
            "reasoning_effort": None,
            "temperature": 0.1,
        },
    )


def _patch_2c(monkeypatch, content):
    client = Mock()
    client.chat.completions.create.return_value = _make_response(content)
    monkeypatch.setattr(
        "app.utils.create_llm_client", lambda config_path=None: (client, "test-model")
    )
    monkeypatch.setattr(
        "app.utils.resolve_task_config",
        lambda task, storage, book_id: {
            "model_name": "test-model",
            "reasoning_effort": None,
            "temperature": 0.1,
        },
    )
    from app.pipeline.walks import walk_2c_alias_resolution as w

    monkeypatch.setattr(w, "chat_completion", lambda **kw: content)


def _patch_2d(monkeypatch, content):
    client = Mock()
    client.chat.completions.create.return_value = _make_response(content)
    monkeypatch.setattr(
        "app.utils.create_llm_client", lambda config_path=None: (client, "test-model")
    )
    monkeypatch.setattr(
        "app.utils.resolve_task_config",
        lambda task, storage, book_id: {
            "model_name": "test-model",
            "reasoning_effort": None,
            "temperature": 0.1,
        },
    )
    from app.pipeline.walks import walk_2d_scene_presence as w

    monkeypatch.setattr(w, "chat_completion", lambda **kw: content)


def _seed_decision(storage, book_id, scene_id, character_id):
    """Create a human presence decision + active absence tombstone."""
    decision_id = f"decision-{uuid.uuid4().hex}"
    now = int(time.time() * 1000)
    storage.execute_insert(
        "INSERT INTO workbench_decision "
        "(decision_id, book_id, target_kind, target_key, decision_type, "
        " base_revision, payload_json, status, source, created_ms) "
        "VALUES (?, ?, 'presence', ?, 'presence:absent', 0, '{}', 'active', "
        " 'human', ?)",
        (decision_id, book_id, f"{scene_id}:{character_id}", now),
    )
    storage.execute_insert(
        "INSERT INTO character_scene_absence "
        "(book_id, scene_id, character_id, decision_id, active, created_ms) "
        "VALUES (?, ?, ?, ?, 1, ?)",
        (book_id, scene_id, character_id, decision_id, now),
    )
    return decision_id


def _seed_manual_presence(storage, book_id, scene_id, character_id):
    """Seed a human character_scene_manual row (protected decision)."""
    decision_id = f"decision-{uuid.uuid4().hex}"
    now = int(time.time() * 1000)
    storage.execute_insert(
        "INSERT INTO workbench_decision "
        "(decision_id, book_id, target_kind, target_key, decision_type, "
        " base_revision, payload_json, status, source, created_ms) "
        "VALUES (?, ?, 'presence', ?, 'presence:present', 0, '{}', 'active', "
        " 'human', ?)",
        (decision_id, book_id, f"{scene_id}:{character_id}", now),
    )
    storage.execute_insert(
        "INSERT INTO character_scene_manual "
        "(id, book_id, character_id, scene_id, relation_type, decision_id) "
        "VALUES (?, ?, ?, ?, 'present', ?)",
        (f"csm-{uuid.uuid4().hex}", book_id, character_id, scene_id, decision_id),
    )
    return decision_id


# ---------------------------------------------------------------------------
# 2b rerun-safety
# ---------------------------------------------------------------------------


class TestWalk2bRerunSafe:
    def test_rerun_does_not_duplicate_generated_rows(
        self, populated_storage, monkeypatch
    ):
        _patch_2b(
            monkeypatch,
            json.dumps(
                [
                    {"name": "John", "aliases": [], "role": "speaker", "confidence": 0.9},
                    {"name": "Mary", "aliases": [], "role": "mentioned", "confidence": 0.8},
                ]
            ),
        )
        # Resolve the actual scene ids produced by the spine.
        scene_ids = [
            r["scene_id"]
            for r in populated_storage.execute_query(
                "SELECT child_id AS scene_id FROM chapter_scene"
            )
        ]
        assert scene_ids

        execute_2b("book-1", populated_storage, {})
        r1 = populated_storage.execute_query(
            "SELECT COUNT(*) AS c FROM character_scene_generated WHERE book_id = 'book-1'"
        )
        assert r1[0]["c"] == 2 * len(scene_ids)

        # Rerun — generated projection must reconcile, not duplicate.
        execute_2b("book-1", populated_storage, {})
        r2 = populated_storage.execute_query(
            "SELECT COUNT(*) AS c FROM character_scene_generated WHERE book_id = 'book-1'"
        )
        assert r2[0]["c"] == 2 * len(scene_ids)

    def test_never_readds_active_human_absence(self, populated_storage, monkeypatch):
        scene_ids = [
            r["scene_id"]
            for r in populated_storage.execute_query(
                "SELECT child_id AS scene_id FROM chapter_scene"
            )
        ]
        char_id = f"char-{uuid.uuid4().hex}"
        populated_storage.execute_insert(
            "INSERT INTO character (id, name, aliases) VALUES (?, ?, '[]')",
            (char_id, "Absent",),
        )
        _seed_decision(populated_storage, "book-1", scene_ids[0], char_id)

        _patch_2b(
            monkeypatch,
            json.dumps(
                [
                    {"name": "Absent", "aliases": [], "role": "present", "confidence": 0.9},
                ]
            ),
        )
        execute_2b("book-1", populated_storage, {})
        # The absent character must NOT appear in the generated projection.
        rows = populated_storage.execute_query(
            "SELECT 1 FROM character_scene_generated "
            "WHERE book_id = 'book-1' AND character_id = ?",
            (char_id,),
        )
        assert rows == []

    def test_rerun_preserves_manual_rows_and_provenance_queryable(
        self, populated_storage, monkeypatch
    ):
        scene_ids = [
            r["scene_id"]
            for r in populated_storage.execute_query(
                "SELECT child_id AS scene_id FROM chapter_scene"
            )
        ]
        char_id = f"char-{uuid.uuid4().hex}"
        populated_storage.execute_insert(
            "INSERT INTO character (id, name, aliases) VALUES (?, ?, '[]')",
            (char_id, "John"),
        )
        _seed_manual_presence(populated_storage, "book-1", scene_ids[0], char_id)

        _patch_2b(
            monkeypatch,
            json.dumps(
                [
                    {"name": "John", "aliases": [], "role": "present", "confidence": 0.9},
                ]
            ),
        )
        execute_2b("book-1", populated_storage, {})
        execute_2b("book-1", populated_storage, {})

        # Manual decision survives; provenance recorded and queryable.
        manual = populated_storage.execute_query(
            "SELECT 1 FROM character_scene_manual WHERE book_id = 'book-1'"
        )
        assert manual
        prov = populated_storage.execute_query(
            "SELECT target_kind, source FROM workbench_provenance WHERE book_id = 'book-1'"
        )
        assert any(p["source"] == "walk" for p in prov)


# ---------------------------------------------------------------------------
# 2d rerun-safety
# ---------------------------------------------------------------------------


class TestWalk2dRerunSafe:
    def test_rerun_does_not_duplicate_generated_rows(
        self, populated_storage, monkeypatch
    ):
        char_id = f"char-{uuid.uuid4().hex}"
        populated_storage.execute_insert(
            "INSERT INTO character (id, name, aliases) VALUES (?, ?, '[]')",
            (char_id, "Mary"),
        )
        populated_storage.execute_insert(
            "INSERT INTO character_book (character_id, book_id, source, confidence, human_override) "
            "VALUES (?, ?, 'walk', 0.9, 0)",
            (char_id, "book-1"),
        )
        scene_ids = [
            r["scene_id"]
            for r in populated_storage.execute_query(
                "SELECT child_id AS scene_id FROM chapter_scene"
            )
        ]
        _patch_2d(monkeypatch, json.dumps([{"character_id": char_id, "confidence": 0.9}]))

        execute_2d("book-1", populated_storage, {})
        r1 = populated_storage.execute_query(
            "SELECT COUNT(*) AS c FROM character_scene_generated WHERE book_id = 'book-1'"
        )
        assert r1[0]["c"] == len(scene_ids)

        execute_2d("book-1", populated_storage, {})
        r2 = populated_storage.execute_query(
            "SELECT COUNT(*) AS c FROM character_scene_generated WHERE book_id = 'book-1'"
        )
        assert r2[0]["c"] == len(scene_ids)

    def test_never_readds_active_human_absence(self, populated_storage, monkeypatch):
        char_id = f"char-{uuid.uuid4().hex}"
        populated_storage.execute_insert(
            "INSERT INTO character (id, name, aliases) VALUES (?, ?, '[]')",
            (char_id, "Mary"),
        )
        populated_storage.execute_insert(
            "INSERT INTO character_book (character_id, book_id, source, confidence, human_override) "
            "VALUES (?, ?, 'walk', 0.9, 0)",
            (char_id, "book-1"),
        )
        scene_ids = [
            r["scene_id"]
            for r in populated_storage.execute_query(
                "SELECT child_id AS scene_id FROM chapter_scene"
            )
        ]
        _seed_decision(populated_storage, "book-1", scene_ids[0], char_id)
        _patch_2d(monkeypatch, json.dumps([{"character_id": char_id, "confidence": 0.9}]))

        execute_2d("book-1", populated_storage, {})
        # The absent scene must NOT have a generated presence row.
        rows = populated_storage.execute_query(
            "SELECT 1 FROM character_scene_generated "
            "WHERE book_id = 'book-1' AND character_id = ? AND scene_id = ?",
            (char_id, scene_ids[0]),
        )
        assert rows == []


# ---------------------------------------------------------------------------
# 2c — GLOBAL + reversible merge consequences
# ---------------------------------------------------------------------------


class TestWalk2cReversible:
    def _seed_two_chars(self, storage):
        storage.execute_insert("INSERT INTO series (id) VALUES ('s1')")
        storage.execute_insert("INSERT INTO book (id, series_id) VALUES ('b1', 's1')")
        storage.execute_insert(
            "INSERT INTO character (id, name, aliases) VALUES ('c1', 'Alice', '[]')"
        )
        storage.execute_insert(
            "INSERT INTO character (id, name, aliases) VALUES ('c2', 'Alicia', '[]')"
        )
        storage.execute_insert(
            "INSERT INTO character_book (character_id, book_id, source, confidence, human_override) "
            "VALUES ('c1', 'b1', 'walk', 0.9, 0)"
        )
        storage.execute_insert(
            "INSERT INTO character_book (character_id, book_id, source, confidence, human_override) "
            "VALUES ('c2', 'b1', 'walk', 0.8, 0)"
        )
        storage.execute_insert("INSERT INTO scene (id) VALUES ('sc1')")
        storage.execute_insert(
            "INSERT INTO character_scene (character_id, scene_id, relation_type, source, confidence, human_override) "
            "VALUES ('c2', 'sc1', 'present', 'walk', 0.8, 0)"
        )

    def test_records_reversible_merge_consequences(self, storage, monkeypatch):
        self._seed_two_chars(storage)
        _patch_2c(
            monkeypatch,
            json.dumps(
                [
                    {
                        "canonical_name": "Alice",
                        "character_ids": ["c1", "c2"],
                        "confidence": 0.95,
                    }
                ]
            ),
        )

        result = execute_2c("b1", storage, {})
        assert result["characters_merged"] == 1

        merges = storage.execute_query(
            "SELECT canonical_id, member_id, status, consequence_json "
            "FROM character_alias_merge WHERE book_id = 'b1'"
        )
        assert len(merges) == 1
        assert merges[0]["canonical_id"] == "c1"
        assert merges[0]["member_id"] == "c2"
        assert merges[0]["status"] == "active"
        cons = json.loads(merges[0]["consequence_json"])
        assert "downstream_invalidations" in cons
        assert cons["downstream_invalidations"]["walk_2d_scene_presence"] == ["sc1"]

        # Member remains addressable; decision recorded with generated source.
        members = storage.execute_query(
            "SELECT id FROM character WHERE id = 'c2'"
        )
        assert members
        decisions = storage.execute_query(
            "SELECT source, status FROM workbench_decision "
            "WHERE book_id = 'b1' AND target_kind = 'alias_merge'"
        )
        assert decisions
        assert decisions[0]["source"] == "generated"
        assert decisions[0]["status"] == "active"

    def test_stays_global(self, storage, monkeypatch):
        self._seed_two_chars(storage)
        _patch_2c(
            monkeypatch,
            json.dumps(
                [
                    {
                        "canonical_name": "Alice",
                        "character_ids": ["c1", "c2"],
                        "confidence": 0.95,
                    }
                ]
            ),
        )
        result = execute_2c("b1", storage, {})
        assert result["characters_collected"] == 2


# ---------------------------------------------------------------------------
# Invalidation DAG
# ---------------------------------------------------------------------------


def test_invalidation_dag_is_contractual():
    from app.pipeline.api_walks import _RERUN_INVALIDATION

    assert _RERUN_INVALIDATION["walk_2b_character_discovery"] == [
        "walk_2c_alias_resolution",
        "walk_2d_scene_presence",
    ]
    assert _RERUN_INVALIDATION["walk_2c_alias_resolution"] == [
        "walk_2d_scene_presence"
    ]
    assert _RERUN_INVALIDATION["walk_2d_scene_presence"] == []
