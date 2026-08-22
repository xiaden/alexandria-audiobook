"""Spec-first tests for snapshot project endpoints (app/pipeline/api_operations.py).

Plan I phase 1 — Snapshot CRUD backend:

- POST /api/pipeline/projects — auto-named snapshot of the current
  spans/script state (no free-form name input)
- GET /api/pipeline/projects — list snapshots newest-first, optional
  ``book_id`` filter; DTO ``{name, book_id, created_ms, size_bytes}``
- DELETE /api/pipeline/projects/{name} — remove a snapshot; 404 on unknown
- PATCH /api/pipeline/projects/{name} — rename; 409 on duplicate name;
  404 on unknown; name validation (no path traversal / control chars)

Restore semantics (POST /projects/load — merge-vs-replace, active-run 409 +
Retry-After, re-render notice) are Plan I phase 2 and are covered in the
``TestLoad*`` classes at the bottom of this file.

Auto-name format decision (documented in the P1-S1 plan annotation): the
plan text's ``Project {YYYY-MM-DD HH:MM}`` format was chosen over the
CONTRACTS example ``book-{id}-{timestamp}``; a `` (N)`` suffix
disambiguates snapshots created within the same minute (name is the PK).
"""

from __future__ import annotations

import json
import re
import shutil
import time

import pytest
from fastapi.testclient import TestClient

from app.pipeline import api_operations
from app.pipeline.adapter import InMemorySQLiteAdapter
from app.pipeline.api import get_storage, router

# Base auto-name: "Project 2026-08-07 12:34", with optional same-minute
# collision suffix " (2)".
_SNAPSHOT_NAME_RE = re.compile(r"^Project \d{4}-\d{2}-\d{2} \d{2}:\d{2}( \(\d+\))?$")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _populate_storage(storage: InMemorySQLiteAdapter) -> None:
    """Insert a minimal but complete document spine with a speaker character."""
    storage.execute_insert(
        "INSERT INTO voice_config (id, name, description)"
        " VALUES ('vc1', 'Warm Female', 'A warm female voice')"
    )
    storage.execute_insert("INSERT INTO series (id) VALUES ('s1')")
    storage.execute_insert(
        "INSERT INTO book (id, series_id, position, version) VALUES ('b1', 's1', 1, 1)"
    )
    storage.execute_insert("INSERT INTO chapter (id, book_id) VALUES ('ch1', 'b1')")
    storage.execute_insert(
        "INSERT INTO book_chapter (child_id, parent_id, position)"
        " VALUES ('ch1', 'b1', 1)"
    )
    storage.execute_insert("INSERT INTO scene (id) VALUES ('sc1')")
    storage.execute_insert(
        "INSERT INTO chapter_scene (child_id, parent_id, position)"
        " VALUES ('sc1', 'ch1', 1)"
    )
    storage.execute_insert("INSERT INTO paragraph (id) VALUES ('p1')")
    storage.execute_insert(
        "INSERT INTO scene_paragraph (child_id, parent_id, position)"
        " VALUES ('p1', 'sc1', 1)"
    )
    storage.execute_insert(
        "INSERT INTO span (id, span_type, text, instruct)"
        " VALUES ('sp1', 'quotation', 'Hello there!', 'cheerfully')"
    )
    storage.execute_insert(
        "INSERT INTO paragraph_span (child_id, parent_id, position)"
        " VALUES ('sp1', 'p1', 1)"
    )
    storage.execute_insert(
        "INSERT INTO character (id, name, aliases, voice_assignment_id)"
        " VALUES ('c1', 'Alice', '[]', 'vc1')"
    )
    storage.execute_insert(
        "INSERT INTO character_book (character_id, book_id, source, confidence)"
        " VALUES ('c1', 'b1', 'walk', 0.9)"
    )
    storage.execute_insert(
        "INSERT INTO character_span"
        " (character_id, span_id, relation_type, source, confidence)"
        " VALUES ('c1', 'sp1', 'speaker', 'walk', 0.95)"
    )


@pytest.fixture()
def storage():
    """In-memory SQLite adapter with schema + test spine initialised."""
    adapter = InMemorySQLiteAdapter()
    adapter.init_db()
    _populate_storage(adapter)
    yield adapter
    adapter.close()


@pytest.fixture()
def client(storage):
    """FastAPI TestClient with the pipeline router and overridden storage."""
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_storage] = lambda: storage
    return TestClient(app)


def _seed_snapshot(storage, name: str, book_id: str, created_ms: int) -> None:
    """Insert a project_snapshot row directly (payload is opaque here)."""
    storage.create_project_snapshot(
        name, book_id, json.dumps({"seed": name, "n": created_ms}), created_ms
    )


# ---------------------------------------------------------------------------
# POST /api/pipeline/projects — create (auto-named)
# ---------------------------------------------------------------------------


class TestCreateSnapshot:
    def test_post_creates_auto_named_snapshot(self, client):
        """POST /projects returns an auto-generated ``Project {date} {time}`` name."""
        resp = client.post("/api/pipeline/projects", json={"book_id": "b1"})
        assert resp.status_code == 200
        data = resp.json()
        assert _SNAPSHOT_NAME_RE.match(data["name"]), data["name"]
        assert data["book_id"] == "b1"
        assert data["created_ms"] > 0
        assert data["size_bytes"] > 0

    def test_post_stores_current_spans_state(self, client, storage):
        """snapshot_json captures spans (text/instruct/speaker + Plan L
        pause_after_ms), characters (voice assignments), the current book
        version, and the Plan L book-level pause override columns."""
        resp = client.post("/api/pipeline/projects", json={"book_id": "b1"})
        assert resp.status_code == 200
        name = resp.json()["name"]

        rows = storage.execute_query(
            "SELECT snapshot_json FROM project_snapshot WHERE name = ?", (name,)
        )
        assert len(rows) == 1
        manifest = json.loads(rows[0]["snapshot_json"])
        assert manifest["book_id"] == "b1"
        assert manifest["book_version"] == 1
        assert manifest["spans"] == [
            {
                "id": "sp1",
                "speaker": "Alice",
                "text": "Hello there!",
                "instruct": "cheerfully",
                "pause_after_ms": None,
            }
        ]
        assert manifest["characters"] == [
            {"id": "c1", "name": "Alice", "voice_assignment_id": "vc1"}
        ]
        # Plan L: no book pause override → keys present with NULL (not 0).
        assert manifest["pause_between_speakers_ms"] is None
        assert manifest["pause_same_speaker_ms"] is None

    def test_post_unknown_book_404(self, client):
        """POST /projects for a nonexistent book returns 404."""
        resp = client.post("/api/pipeline/projects", json={"book_id": "nope"})
        assert resp.status_code == 404

    def test_post_size_bytes_matches_stored_json(self, client, storage):
        """size_bytes in the response equals len(snapshot_json) as stored."""
        resp = client.post("/api/pipeline/projects", json={"book_id": "b1"})
        name = resp.json()["name"]
        rows = storage.execute_query(
            "SELECT snapshot_json FROM project_snapshot WHERE name = ?", (name,)
        )
        assert resp.json()["size_bytes"] == len(rows[0]["snapshot_json"])

    def test_post_size_bytes_counts_utf8_bytes(self, client, storage):
        """size_bytes is the UTF-8 BYTE length: a manifest containing
        multi-byte (CJK) characters reports more than len(str)."""
        storage.execute_update(
            "UPDATE span SET text = '你好世界' WHERE id = 'sp1'"
        )
        resp = client.post("/api/pipeline/projects", json={"book_id": "b1"})
        assert resp.status_code == 200
        name = resp.json()["name"]
        rows = storage.execute_query(
            "SELECT snapshot_json FROM project_snapshot WHERE name = ?", (name,)
        )
        stored = rows[0]["snapshot_json"]
        # Sanity: the manifest really carries multi-byte characters.
        assert "你好世界" in stored
        assert len(stored) < len(stored.encode("utf-8"))
        assert resp.json()["size_bytes"] == len(stored.encode("utf-8"))

    def test_post_twice_within_minute_gives_distinct_names(self, client):
        """Two saves in the same minute must not collide (name is the PK)."""
        r1 = client.post("/api/pipeline/projects", json={"book_id": "b1"})
        r2 = client.post("/api/pipeline/projects", json={"book_id": "b1"})
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.json()["name"] != r2.json()["name"]

    def test_post_rejects_oversized_snapshot(self, client, storage, monkeypatch):
        """Defensive cap (P5-S4 security review): a manifest exceeding the
        snapshot size limit is rejected 400 and no row is created."""
        monkeypatch.setattr(api_operations, "_max_snapshot_json_bytes", lambda: 64)

        resp = client.post("/api/pipeline/projects", json={"book_id": "b1"})
        assert resp.status_code == 400
        assert "too large" in resp.json()["detail"].lower()
        assert storage.list_project_snapshots() == []

    def test_post_accepts_snapshot_at_limit(self, client, storage, monkeypatch):
        """A manifest within the (tuned-down) limit still saves fine."""
        monkeypatch.setattr(api_operations, "_max_snapshot_json_bytes", lambda: 4096)

        resp = client.post("/api/pipeline/projects", json={"book_id": "b1"})
        assert resp.status_code == 200
        assert len(storage.list_project_snapshots()) == 1

    def test_post_cap_counts_utf8_bytes(self, client, storage, monkeypatch):
        """The size cap is enforced on UTF-8 BYTE length: a cap set between
        the manifest's character count and its byte count still rejects a
        multi-byte manifest (a character-based check would accept it)."""
        storage.execute_update(
            "UPDATE span SET text = ? WHERE id = 'sp1'", ('你' * 40,)
        )
        probe = client.post("/api/pipeline/projects", json={"book_id": "b1"})
        assert probe.status_code == 200
        stored = storage.execute_query(
            "SELECT snapshot_json FROM project_snapshot"
        )[0]["snapshot_json"]
        char_len = len(stored)
        byte_len = len(stored.encode("utf-8"))
        assert byte_len > char_len  # otherwise the test proves nothing

        monkeypatch.setattr(
            api_operations,
            "_max_snapshot_json_bytes",
            lambda: (char_len + byte_len) // 2,
        )
        resp = client.post("/api/pipeline/projects", json={"book_id": "b1"})
        assert resp.status_code == 400
        assert "too large" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# GET /api/pipeline/projects — list
# ---------------------------------------------------------------------------


class TestListSnapshots:
    def test_list_newest_first(self, client, storage):
        """Snapshots are listed newest-first (created_ms DESC)."""
        _seed_snapshot(storage, "old", "b1", 1000)
        _seed_snapshot(storage, "new", "b1", 2000)

        resp = client.get("/api/pipeline/projects")
        assert resp.status_code == 200
        assert [s["name"] for s in resp.json()] == ["new", "old"]

    def test_list_dto_fields(self, client, storage):
        """Each item is exactly {name, book_id, created_ms, size_bytes}."""
        _seed_snapshot(storage, "snap", "b1", 1000)

        resp = client.get("/api/pipeline/projects")
        assert resp.status_code == 200
        snap = resp.json()[0]
        assert set(snap.keys()) == {"name", "book_id", "created_ms", "size_bytes"}
        assert snap["name"] == "snap"
        assert snap["book_id"] == "b1"
        assert snap["created_ms"] == 1000
        assert snap["size_bytes"] == len(json.dumps({"seed": "snap", "n": 1000}))

    def test_list_dto_size_bytes_counts_utf8_bytes(self, client, storage):
        """The GET /projects DTO size_bytes is the UTF-8 byte length: a
        multi-byte manifest reports > len(str) in the listing."""
        storage.execute_update(
            "UPDATE span SET text = '你好世界' WHERE id = 'sp1'"
        )
        created = client.post("/api/pipeline/projects", json={"book_id": "b1"}).json()
        resp = client.get("/api/pipeline/projects")
        assert resp.status_code == 200
        item = resp.json()[0]
        stored = storage.execute_query(
            "SELECT snapshot_json FROM project_snapshot WHERE name = ?",
            (created["name"],),
        )[0]["snapshot_json"]
        assert len(stored.encode("utf-8")) > len(stored)
        assert item["size_bytes"] == len(stored.encode("utf-8"))

    def test_list_filter_by_book_id(self, client, storage):
        """Optional book_id query param restricts the listing."""
        _seed_snapshot(storage, "a", "b1", 1000)
        _seed_snapshot(storage, "b", "b2", 2000)

        resp = client.get("/api/pipeline/projects", params={"book_id": "b1"})
        assert resp.status_code == 200
        assert [s["name"] for s in resp.json()] == ["a"]

    def test_list_empty(self, client):
        """No snapshots yet -> empty list."""
        resp = client.get("/api/pipeline/projects")
        assert resp.status_code == 200
        assert resp.json() == []


# ---------------------------------------------------------------------------
# DELETE /api/pipeline/projects/{name}
# ---------------------------------------------------------------------------


class TestDeleteSnapshot:
    def test_delete_removes_row(self, client, storage):
        """DELETE removes the snapshot row."""
        _seed_snapshot(storage, "snap", "b1", 1000)

        resp = client.delete("/api/pipeline/projects/snap")
        assert resp.status_code == 200
        assert resp.json()["name"] == "snap"
        assert storage.get_project_snapshot("snap") is None

    def test_delete_unknown_name_404(self, client):
        """DELETE for an unknown snapshot returns 404."""
        resp = client.delete("/api/pipeline/projects/missing")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /api/pipeline/projects/{name} — rename
# ---------------------------------------------------------------------------


class TestRenameSnapshot:
    def test_patch_renames(self, client, storage):
        """PATCH moves the row to the new name, preserving data."""
        _seed_snapshot(storage, "old", "b1", 1000)

        resp = client.patch("/api/pipeline/projects/old", json={"new_name": "renamed"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "renamed"
        assert storage.get_project_snapshot("old") is None
        row = storage.get_project_snapshot("renamed")
        assert row is not None
        assert row["book_id"] == "b1"

    def test_patch_duplicate_name_409(self, client, storage):
        """Renaming onto an existing name returns 409 (name is the PK)."""
        _seed_snapshot(storage, "a", "b1", 1000)
        _seed_snapshot(storage, "b", "b1", 2000)

        resp = client.patch("/api/pipeline/projects/a", json={"new_name": "b"})
        assert resp.status_code == 409

    def test_patch_unknown_name_404(self, client):
        """PATCH for an unknown snapshot returns 404."""
        resp = client.patch("/api/pipeline/projects/missing", json={"new_name": "x"})
        assert resp.status_code == 404

    def test_patch_rename_to_self_is_noop(self, client, storage):
        """Renaming a snapshot to its own name succeeds as a no-op."""
        _seed_snapshot(storage, "a", "b1", 1000)

        resp = client.patch("/api/pipeline/projects/a", json={"new_name": "a"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "a"

    @pytest.mark.parametrize(
        "bad_name",
        [
            "",
            "   ",
            "a/b",
            "a\\b",
            "a..b",
            "..",
            "a\tb",
            "a\nb",
            " padded",
            "a%b",
            "%2F",
            "100%",
        ],
    )
    def test_patch_rejects_invalid_names(self, client, storage, bad_name):
        """Empty, whitespace, path separators, '..', '%', and control chars -> 400."""
        _seed_snapshot(storage, "a", "b1", 1000)

        resp = client.patch("/api/pipeline/projects/a", json={"new_name": bad_name})
        assert resp.status_code == 400

    @pytest.mark.parametrize(
        ("new_name", "expected_status"),
        [
            ("a" * 200, 200),  # exactly at the 200-char cap
            ("a" * 201, 400),  # one char over the cap
            ("a" + chr(127) + "b", 400),  # DEL (0x7F) is a control char
        ],
    )
    def test_patch_name_length_boundary(
        self, client, storage, new_name, expected_status
    ):
        """The 200-char name cap is inclusive; 201 chars and DEL are rejected."""
        _seed_snapshot(storage, "a", "b1", 1000)

        resp = client.patch("/api/pipeline/projects/a", json={"new_name": new_name})
        assert resp.status_code == expected_status

# ---------------------------------------------------------------------------
# POST /api/pipeline/projects/load — restore (Plan I phase 2)
#
# Contract:
# - 409 + Retry-After while ANY walk_run/render_job row for the book is
#   active (status pending/running) — restore mid-run would corrupt the
#   run's rows/files
# - merge-vs-replace: span text/instruct replaced from the snapshot;
#   characters NEVER deleted; characters missing from the book restored
# - re_render_required flag when the snapshot-referenced audio artifacts
#   are absent under RENDER_ROOT
# ---------------------------------------------------------------------------


def _seed_manifest_snapshot(
    storage,
    name: str,
    book_id: str,
    spans: list[dict] | None = None,
    characters: list[dict] | None = None,
    audio_run_dir: str | None = None,
    created_ms: int = 1000,
) -> None:
    """Insert a project_snapshot row with a phase-2-shaped manifest."""
    manifest = {
        "schema_version": 1,
        "book_id": book_id,
        "book_version": 1,
        "created_ms": created_ms,
        "spans": spans or [],
        "characters": characters or [],
    }
    if audio_run_dir is not None:
        manifest["audio_run_dir"] = audio_run_dir
    storage.create_project_snapshot(
        name, book_id, json.dumps(manifest, sort_keys=True), created_ms
    )


def _span_text(storage, span_id: str) -> str:
    rows = storage.execute_query("SELECT text FROM span WHERE id = ?", (span_id,))
    return rows[0]["text"]


class TestLoadBlockedWhileActive:
    """Rule #10: restore is blocked while a walk/render is in flight."""

    @pytest.mark.parametrize("status", ["pending", "running"])
    def test_load_409_when_walk_run_active(self, client, storage, status):
        _seed_manifest_snapshot(
            storage,
            "snap",
            "b1",
            spans=[
                {
                    "id": "sp1",
                    "speaker": "Alice",
                    "text": "Hello there!",
                    "instruct": "cheerfully",
                }
            ],
        )
        storage.execute_insert(
            "INSERT INTO walk_run (run_id, book_id, walk_name, status, created_ms)"
            " VALUES ('w1', 'b1', '2a', ?, 1000)",
            (status,),
        )

        resp = client.post(
            "/api/pipeline/projects/load", json={"name": "snap", "book_id": "b1"}
        )
        assert resp.status_code == 409
        assert resp.headers["retry-after"] == "5"
        # Blocked BEFORE the merge: the span text is untouched.
        assert _span_text(storage, "sp1") == "Hello there!"

    @pytest.mark.parametrize("status", ["pending", "running"])
    def test_load_409_when_render_job_active(self, client, storage, status):
        _seed_snapshot(storage, "snap", "b1", 1000)
        storage.execute_insert(
            "INSERT INTO render_job"
            " (job_id, book_id, mode, status, created_ms, started_ms)"
            " VALUES ('j1', 'b1', 'individual', ?, 1000, 1000)",
            (status,),
        )

        resp = client.post(
            "/api/pipeline/projects/load", json={"name": "snap", "book_id": "b1"}
        )
        assert resp.status_code == 409
        assert resp.headers["retry-after"] == "5"

    def test_load_200_when_only_terminal_rows_exist(self, client, storage):
        _seed_manifest_snapshot(storage, "snap", "b1")
        storage.execute_insert(
            "INSERT INTO walk_run"
            " (run_id, book_id, walk_name, status, created_ms, finished_ms)"
            " VALUES ('w1', 'b1', '2a', 'completed', 1000, 2000)"
        )
        storage.execute_insert(
            "INSERT INTO render_job"
            " (job_id, book_id, mode, status, created_ms, started_ms, finished_ms)"
            " VALUES ('j1', 'b1', 'batch', 'failed', 1000, 1000, 2000)"
        )

        resp = client.post(
            "/api/pipeline/projects/load", json={"name": "snap", "book_id": "b1"}
        )
        assert resp.status_code == 200

    def test_load_200_when_no_runs_exist(self, client, storage):
        _seed_manifest_snapshot(storage, "snap", "b1")
        resp = client.post(
            "/api/pipeline/projects/load", json={"name": "snap", "book_id": "b1"}
        )
        assert resp.status_code == 200


class TestLoadMergeSemantics:
    """Merge-vs-replace: spans updated; characters never deleted; missing
    characters restored."""

    def test_load_restores_span_text_and_instruct(self, client, storage):
        # Round trip: save → edit → load undoes the edit.
        created = client.post("/api/pipeline/projects", json={"book_id": "b1"}).json()
        storage.execute_update(
            "UPDATE span SET text = 'edited', instruct = 'flatly' WHERE id = 'sp1'"
        )

        resp = client.post(
            "/api/pipeline/projects/load",
            json={"name": created["name"], "book_id": "b1"},
        )
        assert resp.status_code == 200
        row = storage.execute_query(
            "SELECT text, instruct FROM span WHERE id = 'sp1'"
        )[0]
        assert row["text"] == "Hello there!"
        assert row["instruct"] == "cheerfully"

    def test_load_does_not_touch_spans_absent_from_snapshot(self, client, storage):
        # A span added AFTER the snapshot must survive the load untouched.
        created = client.post("/api/pipeline/projects", json={"book_id": "b1"}).json()
        storage.execute_insert(
            "INSERT INTO span (id, span_type, text, instruct)"
            " VALUES ('sp2', 'sentence', 'Added later', '')"
        )
        storage.execute_insert(
            "INSERT INTO paragraph_span (child_id, parent_id, position)"
            " VALUES ('sp2', 'p1', 2)"
        )

        resp = client.post(
            "/api/pipeline/projects/load",
            json={"name": created["name"], "book_id": "b1"},
        )
        assert resp.status_code == 200
        assert _span_text(storage, "sp2") == "Added later"

    def test_load_skips_spans_deleted_since_snapshot(self, client, storage):
        # The manifest cannot re-insert spans that no longer exist in the
        # spine (it carries no position/type) — they are skipped, not
        # recreated, and remaining spans still update.
        _seed_manifest_snapshot(
            storage,
            "snap",
            "b1",
            spans=[
                {
                    "id": "sp1",
                    "speaker": "Alice",
                    "text": "Hello there!",
                    "instruct": "cheerfully",
                },
                {"id": "sp-ghost", "speaker": "NARRATOR", "text": "gone", "instruct": ""},
            ],
        )
        resp = client.post(
            "/api/pipeline/projects/load", json={"name": "snap", "book_id": "b1"}
        )
        assert resp.status_code == 200
        assert _span_text(storage, "sp1") == "Hello there!"
        assert (
            storage.execute_query("SELECT id FROM span WHERE id = 'sp-ghost'") == []
        )

    def test_load_keeps_characters_absent_from_snapshot(self, client, storage):
        # c2 joins the book after the snapshot; load must NOT delete it.
        created = client.post("/api/pipeline/projects", json={"book_id": "b1"}).json()
        storage.execute_insert(
            "INSERT INTO character (id, name, aliases, voice_assignment_id)"
            " VALUES ('c2', 'Bob', '[]', 'vc1')"
        )
        storage.execute_insert(
            "INSERT INTO character_book (character_id, book_id, source, confidence)"
            " VALUES ('c2', 'b1', 'walk', 0.8)"
        )

        resp = client.post(
            "/api/pipeline/projects/load",
            json={"name": created["name"], "book_id": "b1"},
        )
        assert resp.status_code == 200
        rows = storage.execute_query(
            "SELECT character_id FROM character_book"
            " WHERE book_id = 'b1' ORDER BY character_id"
        )
        assert [r["character_id"] for r in rows] == ["c1", "c2"]

    def test_load_creates_character_missing_from_book(self, client, storage):
        # A character in the snapshot but absent from the book is restored
        # with its name and voice assignment, plus the book junction.
        _seed_manifest_snapshot(
            storage,
            "snap",
            "b1",
            characters=[
                {"id": "c1", "name": "Alice", "voice_assignment_id": "vc1"},
                {"id": "c-new", "name": "Carol", "voice_assignment_id": "vc1"},
            ],
        )
        resp = client.post(
            "/api/pipeline/projects/load", json={"name": "snap", "book_id": "b1"}
        )
        assert resp.status_code == 200
        row = storage.execute_query(
            "SELECT name, voice_assignment_id FROM character WHERE id = 'c-new'"
        )
        assert len(row) == 1
        assert row[0]["name"] == "Carol"
        assert row[0]["voice_assignment_id"] == "vc1"
        junction = storage.execute_query(
            "SELECT source, confidence FROM character_book"
            " WHERE character_id = 'c-new' AND book_id = 'b1'"
        )
        assert len(junction) == 1
        # The restored junction is marked 'derived' with full confidence
        # (api_operations._apply_snapshot_merge).
        assert junction[0]["source"] == "derived"
        assert junction[0]["confidence"] == 1.0

    def test_load_restores_junction_for_existing_shared_character(
        self, client, storage
    ):
        # Character row already exists (shared across the series) but has no
        # junction to this book: only the junction is restored — the shared
        # character row itself is left untouched.
        storage.execute_insert(
            "INSERT INTO character (id, name, aliases, voice_assignment_id)"
            " VALUES ('c-other', 'Dana', '[]', 'vc1')"
        )
        _seed_manifest_snapshot(
            storage,
            "snap",
            "b1",
            characters=[{"id": "c-other", "name": "Dana", "voice_assignment_id": "vc1"}],
        )
        resp = client.post(
            "/api/pipeline/projects/load", json={"name": "snap", "book_id": "b1"}
        )
        assert resp.status_code == 200
        assert (
            len(
                storage.execute_query(
                    "SELECT 1 FROM character_book"
                    " WHERE character_id = 'c-other' AND book_id = 'b1'"
                )
            )
            == 1
        )
        row = storage.execute_query(
            "SELECT voice_assignment_id FROM character WHERE id = 'c-other'"
        )[0]
        assert row["voice_assignment_id"] == "vc1"

    def test_load_does_not_clobber_existing_character_voice(self, client, storage):
        # Save (c1 → vc1); the user reassigns c1 to vc2; loading the snapshot
        # keeps vc2 — existing characters are kept verbatim (merge, not
        # replace; the assignment is shared state across the series).
        storage.execute_insert(
            "INSERT INTO voice_config (id, name, description)"
            " VALUES ('vc2', 'Deep Male', 'A deep male voice')"
        )
        created = client.post("/api/pipeline/projects", json={"book_id": "b1"}).json()
        storage.execute_update(
            "UPDATE character SET voice_assignment_id = 'vc2' WHERE id = 'c1'"
        )

        resp = client.post(
            "/api/pipeline/projects/load",
            json={"name": created["name"], "book_id": "b1"},
        )
        assert resp.status_code == 200
        row = storage.execute_query(
            "SELECT voice_assignment_id FROM character WHERE id = 'c1'"
        )[0]
        assert row["voice_assignment_id"] == "vc2"

    def test_load_restores_character_whose_voice_was_deleted(self, client, storage):
        """Save while a character references a voice; delete the character row
        and then its now-unreferenced voice; restore succeeds — the character
        comes back with NULL voice_assignment_id instead of a FK IntegrityError
        500 (PRAGMA foreign_keys=ON)."""
        storage.execute_insert(
            "INSERT INTO voice_config (id, name, description)"
            " VALUES ('vc-extra', 'Gravelly', 'A gravelly voice')"
        )
        storage.execute_insert(
            "INSERT INTO character (id, name, aliases, voice_assignment_id)"
            " VALUES ('c-extra', 'Eve', '[]', 'vc-extra')"
        )
        storage.execute_insert(
            "INSERT INTO character_book (character_id, book_id, source, confidence)"
            " VALUES ('c-extra', 'b1', 'walk', 0.85)"
        )
        created = client.post("/api/pipeline/projects", json={"book_id": "b1"}).json()

        # Simulate the character being dropped from the book (e.g. reassembly)
        # and its now-unreferenced voice being deleted afterwards.
        storage.execute_delete(
            "DELETE FROM character_book WHERE character_id = 'c-extra'"
        )
        storage.execute_delete("DELETE FROM character WHERE id = 'c-extra'")
        storage.execute_delete("DELETE FROM voice_config WHERE id = 'vc-extra'")

        resp = client.post(
            "/api/pipeline/projects/load",
            json={"name": created["name"], "book_id": "b1"},
        )
        assert resp.status_code == 200
        rows = storage.execute_query(
            "SELECT name, voice_assignment_id FROM character WHERE id = 'c-extra'"
        )
        assert len(rows) == 1
        assert rows[0]["name"] == "Eve"
        assert rows[0]["voice_assignment_id"] is None
        # The restored character is joined to the book again.
        assert (
            len(
                storage.execute_query(
                    "SELECT 1 FROM character_book"
                    " WHERE character_id = 'c-extra' AND book_id = 'b1'"
                )
            )
            == 1
        )

    def test_load_skips_malformed_manifest_entries(self, client, storage):
        """Non-dict entries and entries missing 'id' in the snapshot's
        spans/characters are skipped by the merge guards — the load still
        succeeds and leaves the book's state unchanged."""
        _seed_manifest_snapshot(
            storage,
            "snap",
            "b1",
            spans=[42, {"text": "no id", "instruct": ""}],
            characters=[42, {"name": "no id", "voice_assignment_id": "vc1"}],
        )
        resp = client.post(
            "/api/pipeline/projects/load", json={"name": "snap", "book_id": "b1"}
        )
        assert resp.status_code == 200
        # The guards skipped both junk entries: the spine is untouched and
        # no character/junction rows were created.
        assert _span_text(storage, "sp1") == "Hello there!"
        character_ids = [
            r["id"]
            for r in storage.execute_query("SELECT id FROM character ORDER BY id")
        ]
        assert character_ids == ["c1"]
        junction_ids = [
            r["character_id"]
            for r in storage.execute_query(
                "SELECT character_id FROM character_book WHERE book_id = 'b1'"
            )
        ]
        assert junction_ids == ["c1"]


class TestLoadNotFound:
    """404s: unknown snapshot, unknown book, cross-book access, corrupt data."""

    def test_load_unknown_snapshot_404(self, client):
        resp = client.post(
            "/api/pipeline/projects/load", json={"name": "missing", "book_id": "b1"}
        )
        assert resp.status_code == 404

    def test_load_unknown_book_404(self, client, storage):
        _seed_snapshot(storage, "snap", "b1", 1000)
        resp = client.post(
            "/api/pipeline/projects/load", json={"name": "snap", "book_id": "nope"}
        )
        assert resp.status_code == 404

    def test_load_cross_book_snapshot_404(self, client, storage):
        # A snapshot belongs to its book: loading it into a different book is
        # refused with the same 404 shape as an unknown snapshot.
        _seed_snapshot(storage, "snap", "b1", 1000)
        storage.execute_insert(
            "INSERT INTO book (id, series_id, position, version)"
            " VALUES ('b2', 's1', 2, 1)"
        )
        resp = client.post(
            "/api/pipeline/projects/load", json={"name": "snap", "book_id": "b2"}
        )
        assert resp.status_code == 404

    def test_load_corrupt_snapshot_json_500(self, client, storage):
        # snapshot_json is inert data: a corrupt row is refused, never
        # evaluated.
        storage.create_project_snapshot("bad", "b1", "{not json", 1000)
        resp = client.post(
            "/api/pipeline/projects/load", json={"name": "bad", "book_id": "b1"}
        )
        assert resp.status_code == 500

    def test_load_rejects_unknown_schema_version_400(self, client, storage):
        """A manifest whose schema_version this build does not know is
        refused with a clear 400 instead of being merged blindly."""
        manifest = {
            "schema_version": 999,
            "book_id": "b1",
            "book_version": 1,
            "created_ms": 1000,
            "spans": [
                {
                    "id": "sp1",
                    "speaker": "Alice",
                    "text": "must not apply",
                    "instruct": "ignored",
                }
            ],
        }
        storage.create_project_snapshot(
            "future", "b1", json.dumps(manifest, sort_keys=True), 1000
        )

        resp = client.post(
            "/api/pipeline/projects/load", json={"name": "future", "book_id": "b1"}
        )
        assert resp.status_code == 400
        assert "schema" in resp.json()["detail"].lower()
        # Rejected BEFORE the merge: the span text is untouched.
        assert _span_text(storage, "sp1") == "Hello there!"


class TestLoadReRenderNotice:
    """Audio missing after restore → explicit re-render notice."""

    def _seed_completed_render(self, storage, run_dir: str, ms: int = 2000):
        storage.execute_insert(
            "INSERT INTO render_job"
            " (job_id, book_id, mode, status, output_dir, created_ms, started_ms,"
            "  finished_ms)"
            " VALUES ('j1', 'b1', 'individual', 'completed', ?, 1000, 1000, ?)",
            (run_dir, ms),
        )

    def test_load_reports_audio_present(self, client, storage, tmp_path, monkeypatch):
        render_root = tmp_path / "render_root"
        monkeypatch.setenv("RENDER_ROOT", str(render_root))
        run_dir = render_root / "book-b1" / "job1"
        run_dir.mkdir(parents=True)
        (run_dir / "chunk_0000.wav").write_bytes(b"RIFF")
        self._seed_completed_render(storage, str(run_dir))

        created = client.post("/api/pipeline/projects", json={"book_id": "b1"}).json()
        # The save records the audio reference (run dir) in the manifest.
        rows = storage.execute_query(
            "SELECT snapshot_json FROM project_snapshot WHERE name = ?",
            (created["name"],),
        )
        assert json.loads(rows[0]["snapshot_json"])["audio_run_dir"] == str(run_dir)

        resp = client.post(
            "/api/pipeline/projects/load",
            json={"name": created["name"], "book_id": "b1"},
        )
        assert resp.status_code == 200
        assert set(resp.json().keys()) == {
            "status",
            "name",
            "book_id",
            "re_render_required",
        }
        assert resp.json()["re_render_required"] is False

    def test_load_reports_audio_missing_after_gc(self, client, storage, tmp_path, monkeypatch):
        render_root = tmp_path / "render_root"
        monkeypatch.setenv("RENDER_ROOT", str(render_root))
        run_dir = render_root / "book-b1" / "job1"
        run_dir.mkdir(parents=True)
        (run_dir / "chunk_0000.wav").write_bytes(b"RIFF")
        self._seed_completed_render(storage, str(run_dir))

        created = client.post("/api/pipeline/projects", json={"book_id": "b1"}).json()
        # Simulate GC expiry: the audio artifacts disappear from disk.
        shutil.rmtree(run_dir)

        resp = client.post(
            "/api/pipeline/projects/load",
            json={"name": created["name"], "book_id": "b1"},
        )
        assert resp.status_code == 200
        assert resp.json()["re_render_required"] is True

    def test_load_reports_audio_missing_when_never_rendered(self, client, storage):
        # No completed render at save time → no audio reference → re-render.
        created = client.post("/api/pipeline/projects", json={"book_id": "b1"}).json()
        resp = client.post(
            "/api/pipeline/projects/load",
            json={"name": created["name"], "book_id": "b1"},
        )
        assert resp.status_code == 200
        assert resp.json()["re_render_required"] is True

    def test_load_out_of_root_reference_treated_missing(self, client, storage, tmp_path, monkeypatch):
        # A run dir outside RENDER_ROOT (e.g. an explicit test output_dir) is
        # not trusted as an audio reference — the load flags re-render.
        monkeypatch.setenv("RENDER_ROOT", str(tmp_path / "render_root"))
        outside = tmp_path / "elsewhere" / "job1"
        outside.mkdir(parents=True)
        (outside / "chunk_0000.wav").write_bytes(b"RIFF")
        self._seed_completed_render(storage, str(outside))

        created = client.post("/api/pipeline/projects", json={"book_id": "b1"}).json()
        resp = client.post(
            "/api/pipeline/projects/load",
            json={"name": created["name"], "book_id": "b1"},
        )
        assert resp.status_code == 200
        assert resp.json()["re_render_required"] is True

    def test_load_reports_audio_missing_when_dir_has_no_audio(
        self, client, storage, tmp_path, monkeypatch
    ):
        """The referenced run dir exists under RENDER_ROOT but holds no audio
        artifacts (only manifest.json) — the user must re-render."""
        render_root = tmp_path / "render_root"
        monkeypatch.setenv("RENDER_ROOT", str(render_root))
        run_dir = render_root / "book-b1" / "job1"
        run_dir.mkdir(parents=True)
        (run_dir / "manifest.json").write_text("{}")
        self._seed_completed_render(storage, str(run_dir))

        created = client.post("/api/pipeline/projects", json={"book_id": "b1"}).json()
        resp = client.post(
            "/api/pipeline/projects/load",
            json={"name": created["name"], "book_id": "b1"},
        )
        assert resp.status_code == 200
        assert resp.json()["re_render_required"] is True

    def test_load_reports_audio_present_for_mp3(
        self, client, storage, tmp_path, monkeypatch
    ):
        """A run dir holding only an .mp3 artifact counts as audio present
        (the existing positive test only covers .wav)."""
        render_root = tmp_path / "render_root"
        monkeypatch.setenv("RENDER_ROOT", str(render_root))
        run_dir = render_root / "book-b1" / "job1"
        run_dir.mkdir(parents=True)
        (run_dir / "chunk_0000.mp3").write_bytes(b"ID3")
        self._seed_completed_render(storage, str(run_dir))

        created = client.post("/api/pipeline/projects", json={"book_id": "b1"}).json()
        resp = client.post(
            "/api/pipeline/projects/load",
            json={"name": created["name"], "book_id": "b1"},
        )
        assert resp.status_code == 200
        assert resp.json()["re_render_required"] is False


class TestPauseRoundTrip:
    """Plan L P1-S3: snapshots round-trip book pause overrides + span pause_after.

    The manifest carries ``pause_between_speakers_ms`` / ``pause_same_speaker_ms``
    at book level and ``pause_after_ms`` per span.  NULL (resolve default) and
    0 (intentional no-gap) must survive a save → mutate → load round trip
    without being coerced to each other; pre-pause snapshots (keys absent)
    must leave current pause state untouched.
    """

    def test_save_captures_book_pause_and_span_pause_after(self, client, storage):
        storage.execute_update(
            "UPDATE book SET pause_between_speakers_ms = 700,"
            " pause_same_speaker_ms = 0 WHERE id = 'b1'"
        )
        storage.execute_update("UPDATE span SET pause_after_ms = 120 WHERE id = 'sp1'")
        created = client.post("/api/pipeline/projects", json={"book_id": "b1"}).json()
        manifest = json.loads(storage.get_project_snapshot(created["name"])["snapshot_json"])
        assert manifest["pause_between_speakers_ms"] == 700
        assert manifest["pause_same_speaker_ms"] == 0  # 0 preserved, not coerced
        span_entry = next(s for s in manifest["spans"] if s["id"] == "sp1")
        assert span_entry["pause_after_ms"] == 120

    def test_save_defaults_spans_with_null_pause_after(self, client, storage):
        created = client.post("/api/pipeline/projects", json={"book_id": "b1"}).json()
        manifest = json.loads(storage.get_project_snapshot(created["name"])["snapshot_json"])
        span_entry = next(s for s in manifest["spans"] if s["id"] == "sp1")
        assert span_entry["pause_after_ms"] is None
        assert manifest["pause_between_speakers_ms"] is None
        assert manifest["pause_same_speaker_ms"] is None

    def test_load_round_trips_null_vs_zero(self, client, storage):
        # Save with span pause_after=0 (no-gap) and book pauses 700/0.
        storage.execute_update(
            "UPDATE book SET pause_between_speakers_ms = 700,"
            " pause_same_speaker_ms = 0 WHERE id = 'b1'"
        )
        storage.execute_update("UPDATE span SET pause_after_ms = 0 WHERE id = 'sp1'")
        created = client.post("/api/pipeline/projects", json={"book_id": "b1"}).json()
        # Mutate away.
        storage.execute_update(
            "UPDATE book SET pause_between_speakers_ms = NULL,"
            " pause_same_speaker_ms = NULL WHERE id = 'b1'"
        )
        storage.execute_update("UPDATE span SET pause_after_ms = NULL WHERE id = 'sp1'")
        # Load restores.
        resp = client.post(
            "/api/pipeline/projects/load",
            json={"name": created["name"], "book_id": "b1"},
        )
        assert resp.status_code == 200
        book_row = storage.execute_query(
            "SELECT pause_between_speakers_ms, pause_same_speaker_ms"
            " FROM book WHERE id = 'b1'"
        )[0]
        assert book_row["pause_between_speakers_ms"] == 700
        assert book_row["pause_same_speaker_ms"] == 0  # 0 preserved, not NULL
        span_row = storage.execute_query(
            "SELECT pause_after_ms FROM span WHERE id = 'sp1'"
        )[0]
        assert span_row["pause_after_ms"] == 0  # 0 preserved, not coerced to NULL

    def test_load_preserves_null_span_pause(self, client, storage):
        storage.execute_update("UPDATE span SET pause_after_ms = NULL WHERE id = 'sp1'")
        created = client.post("/api/pipeline/projects", json={"book_id": "b1"}).json()
        storage.execute_update("UPDATE span SET pause_after_ms = 0 WHERE id = 'sp1'")
        resp = client.post(
            "/api/pipeline/projects/load",
            json={"name": created["name"], "book_id": "b1"},
        )
        assert resp.status_code == 200
        span_row = storage.execute_query(
            "SELECT pause_after_ms FROM span WHERE id = 'sp1'"
        )[0]
        assert span_row["pause_after_ms"] is None  # NULL restored, not 0

    def test_load_old_snapshot_without_pause_keys_leaves_pause_untouched(
        self, client, storage
    ):
        created = client.post("/api/pipeline/projects", json={"book_id": "b1"}).json()
        manifest = json.loads(storage.get_project_snapshot(created["name"])["snapshot_json"])
        for span in manifest["spans"]:
            span.pop("pause_after_ms", None)
        manifest.pop("pause_between_speakers_ms", None)
        manifest.pop("pause_same_speaker_ms", None)
        storage.create_project_snapshot(
            "old-snap",
            "b1",
            json.dumps(manifest, ensure_ascii=False, sort_keys=True),
            int(time.time() * 1000),
        )
        storage.execute_update(
            "UPDATE book SET pause_between_speakers_ms = 300 WHERE id = 'b1'"
        )
        storage.execute_update("UPDATE span SET pause_after_ms = 200 WHERE id = 'sp1'")
        resp = client.post(
            "/api/pipeline/projects/load",
            json={"name": "old-snap", "book_id": "b1"},
        )
        assert resp.status_code == 200
        book_row = storage.execute_query(
            "SELECT pause_between_speakers_ms FROM book WHERE id = 'b1'"
        )[0]
        assert book_row["pause_between_speakers_ms"] == 300  # untouched
        span_row = storage.execute_query(
            "SELECT pause_after_ms FROM span WHERE id = 'sp1'"
        )[0]
        assert span_row["pause_after_ms"] == 200  # untouched

    def test_load_drops_out_of_range_snapshot_pause(self, client, storage):
        storage.execute_update("UPDATE span SET pause_after_ms = NULL WHERE id = 'sp1'")
        created = client.post("/api/pipeline/projects", json={"book_id": "b1"}).json()
        manifest = json.loads(storage.get_project_snapshot(created["name"])["snapshot_json"])
        manifest["spans"][0]["pause_after_ms"] = 999999999
        manifest["pause_between_speakers_ms"] = 999999999
        storage.create_project_snapshot(
            "corrupt-snap",
            "b1",
            json.dumps(manifest, ensure_ascii=False, sort_keys=True),
            int(time.time() * 1000),
        )
        resp = client.post(
            "/api/pipeline/projects/load",
            json={"name": "corrupt-snap", "book_id": "b1"},
        )
        assert resp.status_code == 200
        book_row = storage.execute_query(
            "SELECT pause_between_speakers_ms FROM book WHERE id = 'b1'"
        )[0]
        assert book_row["pause_between_speakers_ms"] is None
        span_row = storage.execute_query(
            "SELECT pause_after_ms FROM span WHERE id = 'sp1'"
        )[0]
        assert span_row["pause_after_ms"] is None
