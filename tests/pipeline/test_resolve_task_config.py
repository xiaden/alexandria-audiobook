"""Spec-first tests for the resolve_task_config 3-tier LLM resolution.

Plan G Phase 2. ``resolve_task_config(task, storage, book_id)`` replaces the
dead-param ``resolve_task_llm(task, config_path=None)``: it applies on-disk
config -> ``llm.task_overrides`` -> ``walk_override`` rows, snapshotted per
walk-unit start.

Covered here:
- Precedence: on-disk global defaults -> task_overrides -> walk_override rows
  (walk_override wins for temperature AND model_name)
- Explicit 0.0 temperature honored at both the task-override and
  walk_override tiers (``is not None`` semantics, not truthiness)
- walk_override rows are scoped to (book_id, walk_name)
- Missing walk_override table / corrupt value_json degrade gracefully
- Snapshot semantics: the returned dict is an independent value snapshot —
  later storage or config mutations never alter an already-resolved dict

Amendment Round 2 (prompt tier): an effective ``prompt`` key is surfaced with
its own 3-tier chain — config top-level ``walk_override[task].prompt`` →
``llm.task_overrides[task].prompt`` → walk_override row ``key="prompt"`` (row
wins). A value is honored only when it is a non-empty string; None when unset
or empty at every tier (walks then fall back to their built-in system prompt).
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.pipeline.adapter import SQLiteAdapter
from app.utils import resolve_task_config

FALLBACK_MODEL = "richardyoung/qwen3-14b-abliterated:Q8_0"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def config_path(tmp_path, monkeypatch):
    """Point ALEXANDRIA_CONFIG_PATH at a tmp config.json and return its path.

    resolve_task_config reads the on-disk config via find_config_path() (no
    config_path param), so tests must pin the env var to stay off the repo's
    default location.
    """
    path = tmp_path / "config.json"
    monkeypatch.setenv("ALEXANDRIA_CONFIG_PATH", str(path))
    return str(path)


@pytest.fixture()
def storage(tmp_path):
    """File-backed SQLiteAdapter with schema initialised."""
    adapter = SQLiteAdapter(db_path=str(tmp_path / "pipeline.db"))
    adapter.init_db()
    yield adapter
    adapter.close()


def _write_config(config_path, data):
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def _insert_override(storage, book_id, walk_name, key, value):
    """Insert a walk_override row; value is JSON-encoded into value_json."""
    storage.execute_insert(
        "INSERT INTO walk_override (book_id, walk_name, key, value_json)"
        " VALUES (?, ?, ?, ?)",
        (book_id, walk_name, key, json.dumps(value)),
    )


# ---------------------------------------------------------------------------
# Precedence: on-disk config -> task_overrides -> walk_override rows
# ---------------------------------------------------------------------------


class TestPrecedence:
    def test_no_config_empty_storage_falls_back_to_hardcoded(self, config_path, storage):
        r = resolve_task_config("scene_segmentation", storage, "book-1")
        assert r == {
            "model_name": FALLBACK_MODEL,
            "reasoning_effort": None,
            "temperature": 0.6,
            "prompt": None,
        }

    def test_global_defaults_from_disk(self, config_path, storage):
        _write_config(
            config_path,
            {"llm": {"model_name": "gpt-4o", "reasoning_effort": "high", "temperature": 0.5}},
        )
        r = resolve_task_config("unknown_task", storage, "book-1")
        assert r["model_name"] == "gpt-4o"
        assert r["reasoning_effort"] == "high"
        assert r["temperature"] == 0.5

    def test_task_override_wins_over_global(self, config_path, storage):
        _write_config(
            config_path,
            {
                "llm": {
                    "model_name": "gpt-4o",
                    "temperature": 0.6,
                    "task_overrides": {
                        "scene_segmentation": {"model_name": "claude-3", "temperature": 0.1}
                    },
                }
            },
        )
        r = resolve_task_config("scene_segmentation", storage, "book-1")
        assert r["model_name"] == "claude-3"
        assert r["temperature"] == 0.1

    def test_unlisted_task_inherits_global(self, config_path, storage):
        _write_config(
            config_path,
            {
                "llm": {
                    "temperature": 0.5,
                    "task_overrides": {"scene_segmentation": {"temperature": 0.1}},
                }
            },
        )
        r = resolve_task_config("delivery", storage, "book-1")
        assert r["temperature"] == 0.5

    def test_explicit_zero_task_override_honored(self, config_path, storage):
        _write_config(
            config_path,
            {
                "llm": {
                    "temperature": 0.6,
                    "task_overrides": {"scene_segmentation": {"temperature": 0.0}},
                }
            },
        )
        r = resolve_task_config("scene_segmentation", storage, "book-1")
        assert r["temperature"] == 0.0

    def test_walk_override_wins_over_task_override(self, config_path, storage):
        _write_config(
            config_path,
            {
                "llm": {
                    "model_name": "gpt-4o",
                    "task_overrides": {
                        "scene_segmentation": {"model_name": "claude-3", "temperature": 0.1}
                    },
                }
            },
        )
        _insert_override(storage, "book-1", "scene_segmentation", "temperature", 0.9)
        _insert_override(storage, "book-1", "scene_segmentation", "model_name", "gpt-4o-mini")
        r = resolve_task_config("scene_segmentation", storage, "book-1")
        assert r["temperature"] == 0.9
        assert r["model_name"] == "gpt-4o-mini"

    def test_walk_override_partial_override(self, config_path, storage):
        _write_config(
            config_path,
            {
                "llm": {
                    "model_name": "gpt-4o",
                    "reasoning_effort": "medium",
                    "task_overrides": {
                        "scene_segmentation": {"reasoning_effort": "high", "temperature": 0.1}
                    },
                }
            },
        )
        _insert_override(storage, "book-1", "scene_segmentation", "temperature", 0.2)
        r = resolve_task_config("scene_segmentation", storage, "book-1")
        assert r["temperature"] == 0.2  # walk_override wins for temperature
        assert r["reasoning_effort"] == "high"  # untouched by override rows
        assert r["model_name"] == "gpt-4o"  # untouched by override rows

    def test_walk_override_zero_temperature_honored(self, config_path, storage):
        _write_config(
            config_path,
            {
                "llm": {
                    "temperature": 0.6,
                    "task_overrides": {"scene_segmentation": {"temperature": 0.1}},
                }
            },
        )
        _insert_override(storage, "book-1", "scene_segmentation", "temperature", 0.0)
        r = resolve_task_config("scene_segmentation", storage, "book-1")
        assert r["temperature"] == 0.0

    def test_walk_override_scoped_to_book_and_task(self, config_path, storage):
        _write_config(
            config_path,
            {
                "llm": {
                    "temperature": 0.6,
                    "task_overrides": {"scene_segmentation": {"temperature": 0.1}},
                }
            },
        )
        _insert_override(storage, "other-book", "scene_segmentation", "temperature", 0.9)
        _insert_override(storage, "book-1", "delivery", "temperature", 0.8)
        r = resolve_task_config("scene_segmentation", storage, "book-1")
        assert r["temperature"] == 0.1  # rows for other book/task ignored

    def test_malformed_value_json_falls_back(self, config_path, storage):
        _write_config(
            config_path,
            {
                "llm": {
                    "temperature": 0.6,
                    "task_overrides": {"scene_segmentation": {"temperature": 0.1}},
                }
            },
        )
        storage.execute_insert(
            "INSERT INTO walk_override (book_id, walk_name, key, value_json)"
            " VALUES (?, ?, ?, ?)",
            ("book-1", "scene_segmentation", "temperature", "not-json{"),
        )
        r = resolve_task_config("scene_segmentation", storage, "book-1")
        assert r["temperature"] == 0.1  # corrupt row skipped, tier 2 survives

    def test_missing_walk_override_table_guarded(self, config_path):
        class _NoWalkOverrideTable:
            """Storage whose walk_override table does not exist (older DBs)."""

            def execute_query(self, sql, params=()):
                raise sqlite3.OperationalError("no such table: walk_override")

        _write_config(config_path, {"llm": {"temperature": 0.5}})
        r = resolve_task_config("scene_segmentation", _NoWalkOverrideTable(), "book-1")
        assert r["temperature"] == 0.5
        assert r["model_name"] == FALLBACK_MODEL


# ---------------------------------------------------------------------------
# Snapshot semantics: independent dict resolved once per walk-unit start
# ---------------------------------------------------------------------------


class TestSnapshot:
    def test_returned_dict_independent_of_later_storage_mutation(self, config_path, storage):
        _write_config(
            config_path,
            {
                "llm": {
                    "temperature": 0.6,
                    "task_overrides": {"scene_segmentation": {"temperature": 0.1}},
                }
            },
        )
        _insert_override(storage, "book-1", "scene_segmentation", "temperature", 0.9)
        r = resolve_task_config("scene_segmentation", storage, "book-1")
        assert r["temperature"] == 0.9

        # A later walk-unit resolution re-reads storage and sees the deletion…
        storage.execute_delete(
            "DELETE FROM walk_override WHERE book_id = ? AND walk_name = ?",
            ("book-1", "scene_segmentation"),
        )
        assert resolve_task_config("scene_segmentation", storage, "book-1")["temperature"] == 0.1
        # …but the earlier snapshot is untouched.
        assert r["temperature"] == 0.9
        assert r["model_name"] == FALLBACK_MODEL

    def test_returned_dict_independent_of_later_config_change(self, config_path, storage):
        _write_config(config_path, {"llm": {"temperature": 0.6}})
        r = resolve_task_config("scene_segmentation", storage, "book-1")
        assert r["temperature"] == 0.6

        _write_config(config_path, {"llm": {"temperature": 0.2}})
        assert resolve_task_config("scene_segmentation", storage, "book-1")["temperature"] == 0.2
        assert r["temperature"] == 0.6  # original snapshot unchanged


# ---------------------------------------------------------------------------
# Prompt tier (Amendment Round 2): config top-level walk_override section ->
# llm.task_overrides -> walk_override row key="prompt" (row wins). A prompt is
# honored only when it is a non-empty string; None when unset/empty everywhere.
# ---------------------------------------------------------------------------


class TestPromptPrecedence:
    def test_config_top_level_walk_override_prompt_honored(self, config_path, storage):
        # Tier 1 (config["walk_override"][task]["prompt"]) beats tier 2.
        _write_config(
            config_path,
            {
                "walk_override": {"scene_segmentation": {"prompt": "tier1 prompt"}},
                "llm": {
                    "task_overrides": {
                        "scene_segmentation": {"prompt": "tier2 prompt"},
                    },
                },
            },
        )
        r = resolve_task_config("scene_segmentation", storage, "book-1")
        assert r["prompt"] == "tier1 prompt"

    def test_task_override_prompt_honored_when_no_tier1(self, config_path, storage):
        _write_config(
            config_path,
            {
                "llm": {
                    "task_overrides": {
                        "scene_segmentation": {"prompt": "tier2 prompt"},
                    },
                },
            },
        )
        r = resolve_task_config("scene_segmentation", storage, "book-1")
        assert r["prompt"] == "tier2 prompt"

    def test_walk_override_row_prompt_wins_over_both_config_tiers(self, config_path, storage):
        _write_config(
            config_path,
            {
                "walk_override": {"scene_segmentation": {"prompt": "tier1 prompt"}},
                "llm": {
                    "task_overrides": {
                        "scene_segmentation": {"prompt": "tier2 prompt"},
                    },
                },
            },
        )
        _insert_override(storage, "book-1", "scene_segmentation", "prompt", "row prompt")
        r = resolve_task_config("scene_segmentation", storage, "book-1")
        assert r["prompt"] == "row prompt"
        # The prompt row must not disturb the other resolved keys.
        assert r["model_name"] == FALLBACK_MODEL
        assert r["reasoning_effort"] is None
        assert r["temperature"] == 0.6

    def test_prompt_none_when_unset_everywhere(self, config_path, storage):
        _write_config(config_path, {})
        r = resolve_task_config("scene_segmentation", storage, "book-1")
        assert r["prompt"] is None

    def test_empty_string_prompt_treated_as_unset(self, config_path, storage):
        _write_config(
            config_path,
            {
                "walk_override": {"scene_segmentation": {"prompt": ""}},
                "llm": {
                    "task_overrides": {"scene_segmentation": {"prompt": ""}},
                },
            },
        )
        r = resolve_task_config("scene_segmentation", storage, "book-1")
        assert r["prompt"] is None

    def test_empty_tier1_falls_through_to_tier2(self, config_path, storage):
        _write_config(
            config_path,
            {
                "walk_override": {"scene_segmentation": {"prompt": ""}},
                "llm": {
                    "task_overrides": {"scene_segmentation": {"prompt": "tier2 prompt"}},
                },
            },
        )
        r = resolve_task_config("scene_segmentation", storage, "book-1")
        assert r["prompt"] == "tier2 prompt"

    def test_empty_row_prompt_does_not_override_config_prompt(self, config_path, storage):
        _write_config(
            config_path,
            {"walk_override": {"scene_segmentation": {"prompt": "tier1 prompt"}}},
        )
        _insert_override(storage, "book-1", "scene_segmentation", "prompt", "")
        r = resolve_task_config("scene_segmentation", storage, "book-1")
        assert r["prompt"] == "tier1 prompt"  # empty row treated as unset

    def test_non_string_row_prompt_not_honored(self, config_path, storage):
        _write_config(
            config_path,
            {
                "llm": {
                    "task_overrides": {"scene_segmentation": {"prompt": "tier2 prompt"}},
                },
            },
        )
        _insert_override(storage, "book-1", "scene_segmentation", "prompt", 123)
        r = resolve_task_config("scene_segmentation", storage, "book-1")
        assert r["prompt"] == "tier2 prompt"  # non-string row skipped

    def test_non_dict_walk_override_section_guarded(self, config_path, storage):
        # Raw-JSON merge can leave arbitrary shapes at top level; the guard
        # degrades to lower tiers instead of raising.
        _write_config(
            config_path,
            {
                "walk_override": "not-a-dict",
                "llm": {
                    "task_overrides": {"scene_segmentation": {"prompt": "tier2 prompt"}},
                },
            },
        )
        r = resolve_task_config("scene_segmentation", storage, "book-1")
        assert r["prompt"] == "tier2 prompt"

    def test_corrupt_prompt_value_json_skipped(self, config_path, storage):
        _write_config(
            config_path,
            {
                "llm": {
                    "task_overrides": {"scene_segmentation": {"prompt": "tier2 prompt"}},
                },
            },
        )
        storage.execute_insert(
            "INSERT INTO walk_override (book_id, walk_name, key, value_json)"
            " VALUES (?, ?, ?, ?)",
            ("book-1", "scene_segmentation", "prompt", "not-json{"),
        )
        r = resolve_task_config("scene_segmentation", storage, "book-1")
        assert r["prompt"] == "tier2 prompt"  # corrupt row skipped, tier 2 survives

    def test_missing_walk_override_table_keeps_config_prompt(self, config_path):
        class _NoWalkOverrideTable:
            """Storage whose walk_override table does not exist (older DBs)."""

            def execute_query(self, sql, params=()):
                raise sqlite3.OperationalError("no such table: walk_override")

        _write_config(
            config_path,
            {
                "walk_override": {"scene_segmentation": {"prompt": "tier1 prompt"}},
                "llm": {"temperature": 0.5},
            },
        )
        r = resolve_task_config("scene_segmentation", _NoWalkOverrideTable(), "book-1")
        assert r["prompt"] == "tier1 prompt"
        assert r["temperature"] == 0.5

    def test_missing_walk_override_table_prompt_none(self, config_path):
        class _NoWalkOverrideTable:
            """Storage whose walk_override table does not exist (older DBs)."""

            def execute_query(self, sql, params=()):
                raise sqlite3.OperationalError("no such table: walk_override")

        _write_config(config_path, {"llm": {"temperature": 0.5}})
        r = resolve_task_config("scene_segmentation", _NoWalkOverrideTable(), "book-1")
        assert r["prompt"] is None

    def test_prompt_snapshot_independent_of_later_storage_mutation(self, config_path, storage):
        _write_config(
            config_path,
            {"walk_override": {"scene_segmentation": {"prompt": "tier1 prompt"}}},
        )
        _insert_override(storage, "book-1", "scene_segmentation", "prompt", "row prompt")
        r = resolve_task_config("scene_segmentation", storage, "book-1")
        assert r["prompt"] == "row prompt"

        storage.execute_delete(
            "DELETE FROM walk_override WHERE book_id = ? AND walk_name = ?",
            ("book-1", "scene_segmentation"),
        )
        # The next resolution re-reads storage and sees the deletion…
        assert resolve_task_config("scene_segmentation", storage, "book-1")["prompt"] == "tier1 prompt"
        # …but the earlier snapshot is untouched.
        assert r["prompt"] == "row prompt"

    def test_prompt_snapshot_independent_of_later_config_change(self, config_path, storage):
        _write_config(config_path, {"walk_override": {"scene_segmentation": {"prompt": "first"}}})
        r = resolve_task_config("scene_segmentation", storage, "book-1")
        assert r["prompt"] == "first"

        _write_config(config_path, {"walk_override": {"scene_segmentation": {"prompt": "second"}}})
        assert resolve_task_config("scene_segmentation", storage, "book-1")["prompt"] == "second"
        assert r["prompt"] == "first"  # original snapshot unchanged
