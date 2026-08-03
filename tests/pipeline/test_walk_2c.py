"""Tests for walk_2c_alias_resolution — GLOBAL alias resolution across all
characters in a book.

Covers:
- execute() summary dict
- GLOBAL scope (all characters, no filtering)
- No characters → early return
- LLM call with correct prompt
- Merge-group parsing (JSON array)
- Canonical character selection (by name match, fallback)
- Junction redirection (all tables)
- Non-canonical character deletion
- Alias consolidation
- Confidence filtering (≥0.7 auto-accept, <0.5 auto-reject, 0.5-0.7 review)
- LLM error handling
- End-to-end alias merge
"""

from __future__ import annotations

import json

import pytest

from app.pipeline.adapter import InMemorySQLiteAdapter
from app.pipeline.walks.walk_2c_alias_resolution import (
    execute,
    _parse_llm_response,
    _merge_group,
    _redirect_junctions,
    _consolidate_aliases,
    _build_alias_resolution_prompt,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patch_llm(monkeypatch, response_text: str):
    """Patch resolve_task_llm (in app.utils), create_llm_client (in app.utils),
    and _call_llm so walk_2c uses our mocked response.

    Critical: resolve_task_llm / create_llm_client are imported *inside*
    execute() from ``app.utils``, so we patch the source module.
    """

    def mock_resolve(task_name, config_path=None):
        return {
            "model_name": "test-model",
            "temperature": 0.1,
            "max_tokens": 4096,
        }

    def mock_create_client(config_path=None):
        return object(), None

    monkeypatch.setattr(
        "app.utils.resolve_task_llm",
        mock_resolve,
    )
    monkeypatch.setattr(
        "app.utils.create_llm_client",
        mock_create_client,
    )

    def mock_call_llm(client, model_name, temperature, reasoning_effort, prompt):
        return response_text

    monkeypatch.setattr(
        "app.pipeline.walks.walk_2c_alias_resolution._call_llm",
        mock_call_llm,
    )


# ---------------------------------------------------------------------------
# Storage setup helpers
# ---------------------------------------------------------------------------


def _insert_character(storage, char_id: str, name: str, aliases: str = "[]"):
    storage.execute_insert(
        "INSERT INTO character (id, name, aliases) VALUES (?, ?, ?)",
        (char_id, name, aliases),
    )


def _insert_char_book(storage, char_id: str, book_id: str, confidence: float):
    storage.execute_insert(
        """INSERT INTO character_book (character_id, book_id, source, confidence)
           VALUES (?, ?, 'walk', ?)""",
        (char_id, book_id, confidence),
    )


def _insert_char_scene(storage, char_id: str, scene_id: str, confidence: float):
    storage.execute_insert(
        """INSERT INTO character_scene (character_id, scene_id, relation_type, source, confidence)
           VALUES (?, ?, 'present', 'walk', ?)""",
        (char_id, scene_id, confidence),
    )


def _insert_char_span(storage, char_id: str, span_id: str, confidence: float):
    storage.execute_insert(
        """INSERT INTO character_span (character_id, span_id, relation_type, source, confidence)
           VALUES (?, ?, 'speaker', 'walk', ?)""",
        (char_id, span_id, confidence),
    )


def _insert_char_metadata(storage, char_id: str, key: str, value: str):
    storage.execute_insert(
        "INSERT INTO character_metadata (character_id, key, value) VALUES (?, ?, ?)",
        (char_id, key, value),
    )


# ---------------------------------------------------------------------------
# Tests: execute summary and early returns
# ---------------------------------------------------------------------------


class TestExecuteSummary:
    def test_returns_expected_keys(self, monkeypatch):
        """execute() returns a dict with expected top-level keys."""
        storage = InMemorySQLiteAdapter()
        storage.init_db()
        storage.execute_insert("INSERT INTO series (id) VALUES ('s1')")
        storage.execute_insert(
            "INSERT INTO book (id, series_id) VALUES ('b1', 's1')"
        )

        _patch_llm(monkeypatch, "[]")

        result = execute("b1", storage, {})
        assert isinstance(result, dict)
        assert "book_id" in result
        assert "characters_collected" in result
        assert "merge_groups" in result
        assert "characters_merged" in result
        assert "characters_remaining" in result
        assert "merges_for_review" in result
        assert "merges_rejected" in result
        assert "errors" in result

    def test_no_characters_early_return(self, monkeypatch):
        """Returns early when no characters exist for the book."""
        storage = InMemorySQLiteAdapter()
        storage.init_db()
        storage.execute_insert("INSERT INTO series (id) VALUES ('s1')")
        storage.execute_insert(
            "INSERT INTO book (id, series_id) VALUES ('b1', 's1')"
        )

        llm_called = []

        def mock_resolve(task_name, config_path=None):
            return {"model_name": "test", "temperature": 0.1}

        def mock_create_client(config_path=None):
            return object(), None

        def mock_call_llm(client, model_name, temperature, reasoning_effort, prompt):
            llm_called.append(True)
            return "[]"

        monkeypatch.setattr("app.utils.resolve_task_llm", mock_resolve)
        monkeypatch.setattr("app.utils.create_llm_client", mock_create_client)
        monkeypatch.setattr(
            "app.pipeline.walks.walk_2c_alias_resolution._call_llm",
            mock_call_llm,
        )

        result = execute("b1", storage, {})
        assert result["characters_collected"] == 0
        assert result["merge_groups"] == 0
        # LLM should NOT be called when there are no characters
        assert len(llm_called) == 0


# ---------------------------------------------------------------------------
# Tests: GLOBAL scope — collects ALL characters
# ---------------------------------------------------------------------------


class TestGlobalScope:
    def test_collects_all_characters_for_book(self, monkeypatch):
        """All characters in character_book for b1 are collected."""
        storage = InMemorySQLiteAdapter()
        storage.init_db()
        storage.execute_insert("INSERT INTO series (id) VALUES ('s1')")
        storage.execute_insert(
            "INSERT INTO book (id, series_id) VALUES ('b1', 's1')"
        )

        _insert_character(storage, "c1", "Alice")
        _insert_character(storage, "c2", "Bob")
        _insert_character(storage, "c3", "Charlie")
        _insert_char_book(storage, "c1", "b1", 0.9)
        _insert_char_book(storage, "c2", "b1", 0.4)
        _insert_char_book(storage, "c3", "b1", 0.6)

        _patch_llm(monkeypatch, "[]")

        result = execute("b1", storage, {})
        assert result["characters_collected"] == 3


# ---------------------------------------------------------------------------
# Tests: LLM interaction
# ---------------------------------------------------------------------------


class TestLLMInteraction:
    def test_calls_llm_with_character_list_in_prompt(self, monkeypatch):
        """The prompt sent to the LLM includes character names and IDs."""
        storage = InMemorySQLiteAdapter()
        storage.init_db()
        storage.execute_insert("INSERT INTO series (id) VALUES ('s1')")
        storage.execute_insert(
            "INSERT INTO book (id, series_id) VALUES ('b1', 's1')"
        )

        _insert_character(storage, "c1", "Alice")
        _insert_character(storage, "c2", "Bob")
        _insert_char_book(storage, "c1", "b1", 0.9)
        _insert_char_book(storage, "c2", "b1", 0.9)

        captured_prompt = []

        def mock_call_llm(client, model_name, temperature, reasoning_effort, prompt):
            captured_prompt.append(prompt)
            return "[]"

        monkeypatch.setattr(
            "app.utils.resolve_task_llm",
            lambda tn, config_path=None: {
                "model_name": "test-model",
                "temperature": 0.1,
            },
        )
        monkeypatch.setattr(
            "app.utils.create_llm_client",
            lambda config_path=None: (object(), None),
        )
        monkeypatch.setattr(
            "app.pipeline.walks.walk_2c_alias_resolution._call_llm",
            mock_call_llm,
        )

        execute("b1", storage, {})
        assert len(captured_prompt) == 1
        prompt = captured_prompt[0]
        assert "Alice" in prompt
        assert "Bob" in prompt
        assert "c1" in prompt
        assert "c2" in prompt

    def test_uses_script_alias_resolution_task(self, monkeypatch):
        """Verify resolve_task_llm is called with 'script_alias_resolution'."""
        storage = InMemorySQLiteAdapter()
        storage.init_db()
        storage.execute_insert("INSERT INTO series (id) VALUES ('s1')")
        storage.execute_insert(
            "INSERT INTO book (id, series_id) VALUES ('b1', 's1')"
        )
        _insert_character(storage, "c1", "Alice")
        _insert_char_book(storage, "c1", "b1", 0.9)

        captured_task = []

        def mock_resolve(task_name, config_path=None):
            captured_task.append(task_name)
            return {"model_name": "t", "temperature": 0.1}

        monkeypatch.setattr("app.utils.resolve_task_llm", mock_resolve)
        monkeypatch.setattr(
            "app.utils.create_llm_client", lambda config_path=None: (object(), None)
        )
        monkeypatch.setattr(
            "app.pipeline.walks.walk_2c_alias_resolution._call_llm",
            lambda *a, **kw: "[]",
        )

        execute("b1", storage, {})
        assert captured_task == ["script_alias_resolution"]


# ---------------------------------------------------------------------------
# Tests: parse_llm_response
# ---------------------------------------------------------------------------


class TestParseLLMResponse:
    def test_parses_valid_json_array(self):
        """Valid JSON array of merge groups is parsed correctly."""
        response_text = json.dumps([
            {
                "canonical_name": "Alice",
                "character_ids": ["c1", "c2"],
            }
        ])
        all_chars = [
            {"id": "c1", "name": "Alice", "aliases": "[]"},
            {"id": "c2", "name": "Alicia", "aliases": "[]"},
        ]
        result = _parse_llm_response(response_text, all_chars)
        assert len(result) == 1
        assert result[0]["canonical_name"] == "Alice"
        assert set(result[0]["character_ids"]) == {"c1", "c2"}

    def test_parses_multiple_merge_groups(self):
        """Multiple merge groups in one response."""
        response_text = json.dumps([
            {"canonical_name": "Alice", "character_ids": ["c1", "c2"]},
            {"canonical_name": "Bob", "character_ids": ["c3", "c4"]},
        ])
        all_chars = [
            {"id": "c1", "name": "Alice", "aliases": "[]"},
            {"id": "c2", "name": "Alicia", "aliases": "[]"},
            {"id": "c3", "name": "Bob", "aliases": "[]"},
            {"id": "c4", "name": "Robert", "aliases": "[]"},
        ]
        result = _parse_llm_response(response_text, all_chars)
        assert len(result) == 2

    def test_returns_empty_list_for_empty_array(self):
        """Empty JSON array returns no merge groups."""
        response_text = "[]"
        all_chars = [{"id": "c1", "name": "Alice", "aliases": "[]"}]
        result = _parse_llm_response(response_text, all_chars)
        assert result == []

    def test_returns_empty_for_invalid_json(self):
        """Invalid JSON returns empty list (graceful degrade)."""
        response_text = "not json at all"
        all_chars = [{"id": "c1", "name": "Alice", "aliases": "[]"}]
        result = _parse_llm_response(response_text, all_chars)
        assert result == []

    def test_validates_character_ids_exist(self):
        """character_ids that don't match any known character are filtered."""
        response_text = json.dumps([
            {"canonical_name": "Alice", "character_ids": ["c1", "nonexistent"]},
        ])
        all_chars = [
            {"id": "c1", "name": "Alice", "aliases": "[]"},
        ]
        result = _parse_llm_response(response_text, all_chars)
        # Only c1 should survive (need 2+ valid IDs for a merge group)
        assert result == []

    def test_candidate_list_missing_fields(self):
        """Groups missing required fields are skipped."""
        response_text = json.dumps([
            {"canonical_name": "Alice"},
            {"character_ids": ["c1"]},
        ])
        all_chars = [{"id": "c1", "name": "Alice", "aliases": "[]"}]
        result = _parse_llm_response(response_text, all_chars)
        assert result == []


# ---------------------------------------------------------------------------
# Tests: _merge_group
# ---------------------------------------------------------------------------


class TestMergeGroup:
    def test_picks_canonical_by_name_match(self):
        """If canonical_name matches a character's name exactly, that
        character is picked as canonical."""
        storage = InMemorySQLiteAdapter()
        storage.init_db()
        storage.execute_insert("INSERT INTO series (id) VALUES ('s1')")
        storage.execute_insert(
            "INSERT INTO book (id, series_id) VALUES ('b1', 's1')"
        )
        _insert_character(storage, "c1", "Alice")
        _insert_character(storage, "c2", "Alicia")
        _insert_char_book(storage, "c1", "b1", 0.9)
        _insert_char_book(storage, "c2", "b1", 0.8)

        all_chars = [
            {"id": "c1", "name": "Alice", "aliases": "[]"},
            {"id": "c2", "name": "Alicia", "aliases": "[]"},
        ]
        result = {"characters_merged": 0}
        merged_ids: set[str] = set()

        _merge_group(
            character_ids=["c1", "c2"],
            canonical_name="Alice",
            all_characters=all_chars,
            storage=storage,
            merged_ids=merged_ids,
            result=result,
            is_review=False,
        )

        assert result["characters_merged"] == 1
        assert "c2" in merged_ids

        # Canonical survives
        rows = storage.execute_query(
            "SELECT id FROM character WHERE id = ?", ("c1",)
        )
        assert len(rows) == 1

        # Non-canonical deleted
        rows = storage.execute_query(
            "SELECT id FROM character WHERE id = ?", ("c2",)
        )
        assert rows == []

    def test_fallback_first_character(self):
        """If canonical_name doesn't match any name, use the first character
        in the list."""
        storage = InMemorySQLiteAdapter()
        storage.init_db()
        storage.execute_insert("INSERT INTO series (id) VALUES ('s1')")
        storage.execute_insert(
            "INSERT INTO book (id, series_id) VALUES ('b1', 's1')"
        )
        _insert_character(storage, "c-zzz", "Bob")
        _insert_character(storage, "c-aaa", "Alice")
        _insert_char_book(storage, "c-zzz", "b1", 0.9)
        _insert_char_book(storage, "c-aaa", "b1", 0.8)

        all_chars = [
            {"id": "c-zzz", "name": "Bob", "aliases": "[]"},
            {"id": "c-aaa", "name": "Alice", "aliases": "[]"},
        ]
        result = {"characters_merged": 0}

        _merge_group(
            character_ids=["c-zzz", "c-aaa"],
            canonical_name="UnknownName",
            all_characters=all_chars,
            storage=storage,
            merged_ids=set(),
            result=result,
            is_review=False,
        )

        # First in list (c-zzz) should be canonical
        rows = storage.execute_query(
            "SELECT id FROM character WHERE id = ?", ("c-zzz",)
        )
        assert len(rows) == 1

    def test_single_character_group_no_merge(self):
        """A group with only one valid character does not merge."""
        storage = InMemorySQLiteAdapter()
        storage.init_db()
        _insert_character(storage, "c1", "Alice")

        all_chars = [{"id": "c1", "name": "Alice", "aliases": "[]"}]
        result = {"characters_merged": 0}

        _merge_group(
            character_ids=["c1"],
            canonical_name="Alice",
            all_characters=all_chars,
            storage=storage,
            merged_ids=set(),
            result=result,
            is_review=False,
        )

        # No merge happened
        assert result["characters_merged"] == 0


# ---------------------------------------------------------------------------
# Tests: junction redirection
# ---------------------------------------------------------------------------


class TestJunctionRedirection:
    def test_redirects_character_book(self):
        """character_book rows for non-canonical characters are redirected to
        the canonical, and duplicates are removed."""
        storage = InMemorySQLiteAdapter()
        storage.init_db()
        storage.execute_insert("INSERT INTO series (id) VALUES ('s1')")
        storage.execute_insert(
            "INSERT INTO book (id, series_id) VALUES ('b1', 's1')"
        )
        _insert_character(storage, "canon", "Canon")
        _insert_character(storage, "noncanon", "NonCanon")
        _insert_char_book(storage, "canon", "b1", 0.9)
        _insert_char_book(storage, "noncanon", "b1", 0.8)

        _redirect_junctions(storage, "canon", "noncanon")

        rows = storage.execute_query(
            "SELECT character_id FROM character_book WHERE book_id = ?",
            ("b1",),
        )
        char_ids = {r["character_id"] for r in rows}
        assert "canon" in char_ids
        assert "noncanon" not in char_ids

    def test_redirects_character_scene_and_removes_duplicates(self):
        """character_scene rows are redirected; duplicates are removed."""
        storage = InMemorySQLiteAdapter()
        storage.init_db()
        storage.execute_insert("INSERT INTO series (id) VALUES ('s1')")
        storage.execute_insert(
            "INSERT INTO book (id, series_id) VALUES ('b1', 's1')"
        )
        storage.execute_insert("INSERT INTO scene (id) VALUES ('sc1')")
        storage.execute_insert("INSERT INTO scene (id) VALUES ('sc2')")
        _insert_character(storage, "canon", "Canon")
        _insert_character(storage, "noncanon", "NonCanon")
        _insert_char_scene(storage, "canon", "sc1", 0.9)
        _insert_char_scene(storage, "noncanon", "sc1", 0.8)
        _insert_char_scene(storage, "noncanon", "sc2", 0.7)

        _redirect_junctions(storage, "canon", "noncanon")

        rows = storage.execute_query(
            "SELECT character_id, scene_id FROM character_scene ORDER BY scene_id"
        )
        sc1_chars = [r["character_id"] for r in rows if r["scene_id"] == "sc1"]
        sc2_chars = [r["character_id"] for r in rows if r["scene_id"] == "sc2"]
        assert sc1_chars == ["canon"]
        assert sc2_chars == ["canon"]

    def test_redirects_character_span(self):
        """character_span rows are redirected."""
        storage = InMemorySQLiteAdapter()
        storage.init_db()
        storage.execute_insert("INSERT INTO series (id) VALUES ('s1')")
        storage.execute_insert(
            "INSERT INTO book (id, series_id) VALUES ('b1', 's1')"
        )
        storage.execute_insert("INSERT INTO span (id, span_type) VALUES ('sp1', 'quotation')")
        _insert_character(storage, "canon", "Canon")
        _insert_character(storage, "noncanon", "NonCanon")
        _insert_char_span(storage, "noncanon", "sp1", 0.8)

        _redirect_junctions(storage, "canon", "noncanon")

        rows = storage.execute_query(
            "SELECT character_id, span_id FROM character_span WHERE span_id = ?",
            ("sp1",),
        )
        assert rows[0]["character_id"] == "canon"

    def test_redirects_character_metadata(self):
        """character_metadata rows are redirected, duplicates removed."""
        storage = InMemorySQLiteAdapter()
        storage.init_db()
        storage.execute_insert("INSERT INTO series (id) VALUES ('s1')")
        storage.execute_insert(
            "INSERT INTO book (id, series_id) VALUES ('b1', 's1')"
        )
        _insert_character(storage, "canon", "Canon")
        _insert_character(storage, "noncanon", "NonCanon")
        _insert_char_metadata(storage, "noncanon", "gender", "female")
        _insert_char_metadata(storage, "noncanon", "age", "30")

        _redirect_junctions(storage, "canon", "noncanon")

        rows = storage.execute_query(
            "SELECT character_id, key, value FROM character_metadata WHERE character_id = ?",
            ("canon",),
        )
        keys = {r["key"]: r["value"] for r in rows}
        assert keys.get("gender") == "female"
        assert keys.get("age") == "30"


# ---------------------------------------------------------------------------
# Tests: non-canonical character deletion (via _redirect + delete in _merge_group)
# ---------------------------------------------------------------------------


class TestNonCanonicalDeletion:
    def test_non_canonical_character_is_deleted_after_merge(self):
        """After junction redirection, the non-canonical character row is
        deleted from the character table."""
        storage = InMemorySQLiteAdapter()
        storage.init_db()
        storage.execute_insert("INSERT INTO series (id) VALUES ('s1')")
        storage.execute_insert(
            "INSERT INTO book (id, series_id) VALUES ('b1', 's1')"
        )
        _insert_character(storage, "canon", "Canon")
        _insert_character(storage, "noncanon", "NonCanon")
        _insert_char_book(storage, "canon", "b1", 0.9)
        _insert_char_book(storage, "noncanon", "b1", 0.8)

        all_chars = [
            {"id": "canon", "name": "Canon", "aliases": "[]"},
            {"id": "noncanon", "name": "NonCanon", "aliases": "[]"},
        ]
        result = {"characters_merged": 0}

        _merge_group(
            character_ids=["canon", "noncanon"],
            canonical_name="Canon",
            all_characters=all_chars,
            storage=storage,
            merged_ids=set(),
            result=result,
            is_review=False,
        )

        rows = storage.execute_query(
            "SELECT id FROM character WHERE id = ?", ("noncanon",)
        )
        assert rows == []

    def test_canonical_character_survives(self):
        """The canonical character remains after the merge."""
        storage = InMemorySQLiteAdapter()
        storage.init_db()
        storage.execute_insert("INSERT INTO series (id) VALUES ('s1')")
        storage.execute_insert(
            "INSERT INTO book (id, series_id) VALUES ('b1', 's1')"
        )
        _insert_character(storage, "canon", "Canon")
        _insert_character(storage, "noncanon", "NonCanon")

        _redirect_junctions(storage, "canon", "noncanon")

        rows = storage.execute_query(
            "SELECT id FROM character WHERE id = ?", ("canon",)
        )
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# Tests: alias consolidation
# ---------------------------------------------------------------------------


class TestAliasConsolidation:
    def test_adds_non_canonical_name_to_canonical_aliases(self):
        """When a non-canonical character is merged, its name is added to the
        canonical character's aliases list."""
        storage = InMemorySQLiteAdapter()
        storage.init_db()
        storage.execute_insert("INSERT INTO series (id) VALUES ('s1')")
        storage.execute_insert(
            "INSERT INTO book (id, series_id) VALUES ('b1', 's1')"
        )
        _insert_character(storage, "canon", "Alice", '["Ally"]')
        _insert_character(storage, "noncanon", "Alicia")
        _insert_char_book(storage, "canon", "b1", 0.9)
        _insert_char_book(storage, "noncanon", "b1", 0.8)

        all_chars = [
            {"id": "canon", "name": "Alice", "aliases": '["Ally"]'},
            {"id": "noncanon", "name": "Alicia", "aliases": "[]"},
        ]
        result = {"characters_merged": 0}

        _merge_group(
            character_ids=["canon", "noncanon"],
            canonical_name="Alice",
            all_characters=all_chars,
            storage=storage,
            merged_ids=set(),
            result=result,
            is_review=False,
        )

        rows = storage.execute_query(
            "SELECT name, aliases FROM character WHERE id = ?", ("canon",)
        )
        char = rows[0]
        aliases = json.loads(char["aliases"])
        assert "Alicia" in aliases
        assert "Ally" in aliases  # Original alias preserved


class TestConsolidateAliasesStandalone:
    def test_does_not_duplicate_existing_alias(self):
        """If non-canonical name is already in aliases, it is not added again."""
        storage = InMemorySQLiteAdapter()
        storage.init_db()
        _insert_character(storage, "canon", "Alice", '["Alicia"]')

        all_chars = [
            {"id": "canon", "name": "Alice", "aliases": '["Alicia"]'},
        ]

        _consolidate_aliases(
            canonical_id="canon",
            non_canonical_ids=["nc1"],  # not in all_chars, but alias set logic handles it
            canonical_name="Alice",
            all_characters=all_chars,
            storage=storage,
        )

        rows = storage.execute_query(
            "SELECT aliases FROM character WHERE id = ?", ("canon",)
        )
        aliases = json.loads(rows[0]["aliases"])
        assert aliases.count("Alicia") == 1

    def test_adds_new_name_to_empty_aliases(self):
        """If aliases is empty ([]), the new name is added."""
        storage = InMemorySQLiteAdapter()
        storage.init_db()
        _insert_character(storage, "canon", "Alice", "[]")
        _insert_character(storage, "nc1", "Alicia", "[]")

        all_chars = [
            {"id": "canon", "name": "Alice", "aliases": "[]"},
            {"id": "nc1", "name": "Alicia", "aliases": "[]"},
        ]

        _consolidate_aliases(
            canonical_id="canon",
            non_canonical_ids=["nc1"],
            canonical_name="Alice",
            all_characters=all_chars,
            storage=storage,
        )

        rows = storage.execute_query(
            "SELECT aliases FROM character WHERE id = ?", ("canon",)
        )
        aliases = json.loads(rows[0]["aliases"])
        assert "Alicia" in aliases


# ---------------------------------------------------------------------------
# Tests: confidence filtering
# ---------------------------------------------------------------------------


class TestConfidenceFiltering:
    def test_high_confidence_auto_accept(self, monkeypatch):
        """Groups with confidence ≥ 0.7 are auto-accepted (merged)."""
        storage = InMemorySQLiteAdapter()
        storage.init_db()
        storage.execute_insert("INSERT INTO series (id) VALUES ('s1')")
        storage.execute_insert(
            "INSERT INTO book (id, series_id) VALUES ('b1', 's1')"
        )
        _insert_character(storage, "c1", "Alice")
        _insert_character(storage, "c2", "Alicia")
        _insert_char_book(storage, "c1", "b1", 0.9)
        _insert_char_book(storage, "c2", "b1", 0.9)

        response = json.dumps([
            {
                "canonical_name": "Alice",
                "character_ids": ["c1", "c2"],
                "confidence": 0.95,
            }
        ])
        _patch_llm(monkeypatch, response)

        result = execute("b1", storage, {})
        assert result["characters_merged"] == 1
        assert result["merges_for_review"] == 0
        assert result["merges_rejected"] == 0

    def test_low_confidence_auto_reject(self, monkeypatch):
        """Groups with confidence < 0.5 are discarded (auto-rejected)."""
        storage = InMemorySQLiteAdapter()
        storage.init_db()
        storage.execute_insert("INSERT INTO series (id) VALUES ('s1')")
        storage.execute_insert(
            "INSERT INTO book (id, series_id) VALUES ('b1', 's1')"
        )
        _insert_character(storage, "c1", "Alice")
        _insert_character(storage, "c2", "Alicia")
        _insert_char_book(storage, "c1", "b1", 0.9)
        _insert_char_book(storage, "c2", "b1", 0.9)

        response = json.dumps([
            {
                "canonical_name": "Alice",
                "character_ids": ["c1", "c2"],
                "confidence": 0.3,
            }
        ])
        _patch_llm(monkeypatch, response)

        result = execute("b1", storage, {})
        assert result["characters_merged"] == 0
        assert result["merges_rejected"] == 1

    def test_mid_confidence_flagged_for_review(self, monkeypatch):
        """Groups with 0.5 ≤ confidence < 0.7 are merged AND flagged for review."""
        storage = InMemorySQLiteAdapter()
        storage.init_db()
        storage.execute_insert("INSERT INTO series (id) VALUES ('s1')")
        storage.execute_insert(
            "INSERT INTO book (id, series_id) VALUES ('b1', 's1')"
        )
        _insert_character(storage, "c1", "Alice")
        _insert_character(storage, "c2", "Alicia")
        _insert_char_book(storage, "c1", "b1", 0.9)
        _insert_char_book(storage, "c2", "b1", 0.9)

        response = json.dumps([
            {
                "canonical_name": "Alice",
                "character_ids": ["c1", "c2"],
                "confidence": 0.6,
            }
        ])
        _patch_llm(monkeypatch, response)

        result = execute("b1", storage, {})
        assert result["characters_merged"] == 1
        assert result["merges_for_review"] == 1

    def test_multiple_groups_mixed_confidence(self, monkeypatch):
        """Groups with mixed confidence levels are handled correctly."""
        storage = InMemorySQLiteAdapter()
        storage.init_db()
        storage.execute_insert("INSERT INTO series (id) VALUES ('s1')")
        storage.execute_insert(
            "INSERT INTO book (id, series_id) VALUES ('b1', 's1')"
        )
        _insert_character(storage, "c1", "Alice")
        _insert_character(storage, "c2", "Alicia")
        _insert_character(storage, "c3", "Bob")
        _insert_character(storage, "c4", "Robert")
        _insert_character(storage, "c5", "Charlie")
        _insert_character(storage, "c6", "Chuck")
        _insert_char_book(storage, "c1", "b1", 0.9)
        _insert_char_book(storage, "c2", "b1", 0.9)
        _insert_char_book(storage, "c3", "b1", 0.9)
        _insert_char_book(storage, "c4", "b1", 0.9)
        _insert_char_book(storage, "c5", "b1", 0.9)
        _insert_char_book(storage, "c6", "b1", 0.9)

        response = json.dumps([
            {"canonical_name": "Alice", "character_ids": ["c1", "c2"], "confidence": 0.95},
            {"canonical_name": "Bob", "character_ids": ["c3", "c4"], "confidence": 0.6},
            {"canonical_name": "Charlie", "character_ids": ["c5", "c6"], "confidence": 0.2},
        ])
        _patch_llm(monkeypatch, response)

        result = execute("b1", storage, {})
        assert result["characters_merged"] == 2  # Alice group + Bob group
        assert result["merges_for_review"] == 1  # Only Bob group
        assert result["merges_rejected"] == 1  # Charlie group rejected

        # Verify Charlie group was NOT merged
        rows = storage.execute_query(
            "SELECT id FROM character WHERE id IN ('c5', 'c6')"
        )
        assert len(rows) == 2


# ---------------------------------------------------------------------------
# Tests: LLM error handling
# ---------------------------------------------------------------------------


class TestLLMErrorHandling:
    def test_llm_exception_returns_error(self, monkeypatch):
        """If _call_llm raises an exception, execute() returns gracefully."""
        storage = InMemorySQLiteAdapter()
        storage.init_db()
        storage.execute_insert("INSERT INTO series (id) VALUES ('s1')")
        storage.execute_insert(
            "INSERT INTO book (id, series_id) VALUES ('b1', 's1')"
        )
        _insert_character(storage, "c1", "Alice")
        _insert_char_book(storage, "c1", "b1", 0.9)

        monkeypatch.setattr(
            "app.utils.resolve_task_llm",
            lambda tn, config_path=None: {"model_name": "t", "temperature": 0.1},
        )
        monkeypatch.setattr(
            "app.utils.create_llm_client", lambda config_path=None: (object(), None)
        )
        monkeypatch.setattr(
            "app.pipeline.walks.walk_2c_alias_resolution._call_llm",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("LLM failed")),
        )

        result = execute("b1", storage, {})
        assert len(result["errors"]) > 0


# ---------------------------------------------------------------------------
# Tests: end-to-end alias merge
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_full_alias_merge_preserves_all_data(self, monkeypatch):
        """Two character rows that resolve to one: all junctions survive."""
        storage = InMemorySQLiteAdapter()
        storage.init_db()
        storage.execute_insert("INSERT INTO series (id) VALUES ('s1')")
        storage.execute_insert(
            "INSERT INTO book (id, series_id) VALUES ('b1', 's1')"
        )
        storage.execute_insert("INSERT INTO chapter (id, book_id) VALUES ('ch1', 'b1')")
        storage.execute_insert("INSERT INTO scene (id) VALUES ('sc1')")
        storage.execute_insert("INSERT INTO scene (id) VALUES ('sc2')")
        storage.execute_insert("INSERT INTO span (id, span_type) VALUES ('sp1', 'quotation')")

        _insert_character(storage, "c-alice", "Alice", '["Ally"]')
        _insert_character(storage, "c-alicia", "Alicia")

        _insert_char_book(storage, "c-alice", "b1", 0.9)
        _insert_char_book(storage, "c-alicia", "b1", 0.8)

        _insert_char_scene(storage, "c-alice", "sc1", 0.9)
        _insert_char_scene(storage, "c-alicia", "sc2", 0.7)

        _insert_char_span(storage, "c-alice", "sp1", 0.9)

        _insert_char_metadata(storage, "c-alice", "role", "protagonist")
        _insert_char_metadata(storage, "c-alicia", "age", "28")

        response = json.dumps([
            {
                "canonical_name": "Alice",
                "character_ids": ["c-alice", "c-alicia"],
                "confidence": 0.85,
            }
        ])
        _patch_llm(monkeypatch, response)

        result = execute("b1", storage, {})

        # Canonical survived
        canonical = storage.execute_query(
            "SELECT id, name, aliases FROM character WHERE id = ?", ("c-alice",)
        )
        assert len(canonical) == 1
        assert canonical[0]["name"] == "Alice"
        aliases = json.loads(canonical[0]["aliases"])
        assert "Alicia" in aliases
        assert "Ally" in aliases

        # Non-canonical deleted
        noncanon = storage.execute_query(
            "SELECT id FROM character WHERE id = ?", ("c-alicia",)
        )
        assert noncanon == []

        # All junctions now point to c-alice
        book_rows = storage.execute_query(
            "SELECT character_id FROM character_book WHERE book_id = ?", ("b1",)
        )
        assert set(r["character_id"] for r in book_rows) == {"c-alice"}

        scene_rows = storage.execute_query(
            "SELECT character_id FROM character_scene"
        )
        assert all(r["character_id"] == "c-alice" for r in scene_rows)

        span_rows = storage.execute_query("SELECT character_id FROM character_span")
        assert all(r["character_id"] == "c-alice" for r in span_rows)

        # Merge count correct
        assert result["characters_merged"] == 1
        assert result["merges_for_review"] == 0

    def test_prompt_includes_all_character_names_and_ids(self):
        """_build_alias_resolution_prompt includes every character's name,
        id, and aliases."""
        characters = [
            {"id": "c1", "name": "Alice", "aliases": "[]"},
            {"id": "c2", "name": "Bob", "aliases": '["Bobby"]'},
        ]
        prompt = _build_alias_resolution_prompt(characters)
        assert "c1" in prompt
        assert "Alice" in prompt
        assert "c2" in prompt
        assert "Bob" in prompt
        assert "Bobby" in prompt
