#!/usr/bin/env python3
"""Unit tests for resolve_task_config 3-tier resolution.

Run:  python test_resolve_task_llm.py

Covers the Phase-2 ``resolve_task_config`` helper: resolution order is on-disk
config -> ``llm.task_overrides`` -> ``walk_override`` rows (file-backed
SQLiteAdapter). The legacy ``resolve_task_llm(task, config_path=None)`` wrapper
was deleted in Phase 3 once all 9 walks migrated to ``resolve_task_config``.
"""

import json
import os
import sys
import tempfile

# Make app/ importable regardless of CWD.
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_APP_DIR)
sys.path.insert(0, _APP_DIR)
sys.path.insert(0, _REPO_ROOT)

from utils import resolve_task_config  # noqa: E402
from app.pipeline.adapter import SQLiteAdapter  # noqa: E402

FALLBACK_MODEL = "richardyoung/qwen3-14b-abliterated:Q8_0"

RESULTS = {"passed": 0, "failed": 0}
FAILURES = []


def check(name, got, expected):
    if got == expected:
        RESULTS["passed"] += 1
        return
    RESULTS["failed"] += 1
    FAILURES.append(f"{name}: got {got!r}, expected {expected!r}")


def _point_at_empty_config():
    """Point ALEXANDRIA_CONFIG_PATH at a nonexistent file (env isolation)."""
    os.environ["ALEXANDRIA_CONFIG_PATH"] = os.path.join(tempfile.mkdtemp(), "config.json")


def _write_config(data):
    d = tempfile.mkdtemp()
    path = os.path.join(d, "config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.environ["ALEXANDRIA_CONFIG_PATH"] = path
    return path


def _make_storage(rows):
    """File-backed SQLiteAdapter with the given walk_override rows.

    ``rows``: iterable of (book_id, walk_name, key, value) — value is
    JSON-encoded into the value_json TEXT column.
    """
    d = tempfile.mkdtemp()
    storage = SQLiteAdapter(db_path=os.path.join(d, "pipeline.db"))
    storage.init_db()
    for book_id, walk_name, key, value in rows:
        storage.execute_insert(
            "INSERT INTO walk_override (book_id, walk_name, key, value_json)"
            " VALUES (?, ?, ?, ?)",
            (book_id, walk_name, key, json.dumps(value)),
        )
    return storage


def main():
    _point_at_empty_config()
    empty = _make_storage([])

    # 1. No config + empty storage -> hardcoded fallbacks.
    r = resolve_task_config("scene_segmentation", empty, "book-1")
    check("fallback temp", r["temperature"], 0.6)
    check("fallback model", r["model_name"], FALLBACK_MODEL)
    check("fallback reasoning", r["reasoning_effort"], None)

    # 2. Task override temperature wins.
    _write_config({
        "llm": {
            "temperature": 0.6,
            "task_overrides": {
                "scene_segmentation": {"temperature": 0.1},
                "delivery": {"temperature": 0.3},
            },
        }
    })
    check("task override 0.1", resolve_task_config("scene_segmentation", empty, "book-1")["temperature"], 0.1)
    check("task override 0.3", resolve_task_config("delivery", empty, "book-1")["temperature"], 0.3)

    # 3. Unlisted task inherits global default.
    check("global default", resolve_task_config("unknown_task", empty, "book-1")["temperature"], 0.6)

    # 4. Global default changed.
    _write_config({"llm": {"temperature": 0.5}})
    check("global 0.5", resolve_task_config("unknown_task", empty, "book-1")["temperature"], 0.5)

    # 5. Explicit 0.0 task override honored (not treated as unset).
    _write_config({"llm": {"task_overrides": {"scene_segmentation": {"temperature": 0.0}}}})
    check("explicit 0.0", resolve_task_config("scene_segmentation", empty, "book-1")["temperature"], 0.0)

    # 6. walk_override rows win over task overrides (temperature + model).
    _write_config({
        "llm": {
            "model_name": "gpt-4o",
            "task_overrides": {"scene_segmentation": {"model_name": "claude-3", "temperature": 0.1}},
        }
    })
    storage = _make_storage([
        ("book-1", "scene_segmentation", "temperature", 0.9),
        ("book-1", "scene_segmentation", "model_name", "gpt-4o-mini"),
    ])
    r = resolve_task_config("scene_segmentation", storage, "book-1")
    check("walk_override temp wins", r["temperature"], 0.9)
    check("walk_override model wins", r["model_name"], "gpt-4o-mini")

    # 7. Snapshot: returned dict independent of later storage mutation.
    storage.execute_delete(
        "DELETE FROM walk_override WHERE book_id = ? AND walk_name = ?",
        ("book-1", "scene_segmentation"),
    )
    check("snapshot unchanged", r["temperature"], 0.9)

    print(f"passed={RESULTS['passed']} failed={RESULTS['failed']}")
    for f in FAILURES:
        print("FAIL:", f)
    return 1 if RESULTS["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
