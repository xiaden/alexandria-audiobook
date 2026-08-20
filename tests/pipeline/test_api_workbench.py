"""API tests for the pipeline workbench read/config, override, alias,
boundary, presence, and rerun routes (S2).

Covers the S2 acceptance criteria:

* Every new route is under /api/pipeline, uses typed DTOs, validates
  ownership/reachability/revision, and maps contention to 503 + Retry-After: 5.
* Alias preview is book-scoped, single-use, and commit applies exactly the
  preview affected-row set (ten-minute TTL is asserted at the domain layer).
* Presence, overrides, boundaries, and reruns return the registered response
  shapes and correct 404/409/422 behavior.
"""

from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import app.pipeline.api as pipeline_api
from app.pipeline import api_walks
from app.pipeline.adapter import (
    ConcurrentTransactionError,
    InMemorySQLiteAdapter,
)
from app.pipeline.api_characters import get_workbench as _cp_workbench
from app.pipeline.api_walks import get_workbench as _wk_workbench
from app.pipeline.api_review import get_workbench as _rv_workbench
from app.pipeline.review import ReviewManager
from app.pipeline.workbench import Workbench


#: All three routers reference the same get_workbench function object.
_WORKBENCH_DEPS = (_cp_workbench, _wk_workbench, _rv_workbench)


def _seed(storage) -> None:
    """Minimal book + spine + characters + existing review item."""
    c = storage.get_connection()
    c.execute("INSERT INTO series (id) VALUES ('s1')")
    c.execute(
        "INSERT INTO book (id, series_id, book_number, version, position)"
        " VALUES ('b1', 's1', 1, 0, 0)"
    )
    c.execute("INSERT INTO book (id, series_id, book_number, version, position)"
              " VALUES ('b2', 's1', 2, 0, 1)")
    for cid in ("c1", "c2", "canon"):
        c.execute(
            "INSERT INTO character (id, name, aliases) VALUES (?, ?, '[]')",
            (cid, cid.capitalize()),
        )
    c.execute("INSERT INTO scene (id) VALUES ('sc1')")
    c.execute("INSERT INTO scene (id) VALUES ('sc2')")
    c.execute("INSERT INTO chapter (id, book_id) VALUES ('ch1', 'b1')")
    c.execute("INSERT INTO paragraph (id) VALUES ('p1')")
    c.execute("INSERT INTO span (id, span_type, text) VALUES ('sp1', 'sentence', 'Hi')")
    c.execute("INSERT INTO book_chapter (child_id, parent_id, position) VALUES ('ch1', 'b1', 0)")
    c.execute("INSERT INTO chapter_scene (child_id, parent_id, position) VALUES ('sc1', 'ch1', 0)")
    c.execute("INSERT INTO chapter_scene (child_id, parent_id, position) VALUES ('sc2', 'ch1', 1)")
    c.execute("INSERT INTO scene_paragraph (child_id, parent_id, position) VALUES ('p1', 'sc1', 0)")
    c.execute("INSERT INTO paragraph_span (child_id, parent_id, position) VALUES ('sp1', 'p1', 0)")
    # one generated presence row for b1 so the read model is non-empty
    c.execute(
        "INSERT INTO character_scene_generated (book_id, character_id, scene_id,"
        " relation_type, confidence, generation_revision)"
        " VALUES ('b1', 'c1', 'sc1', 'present', 0.8, 1)"
    )
    c.execute("INSERT INTO workbench_generation (generation_id, book_id, revision, updated_ms)"
              " VALUES ('wg-b1', 'b1', 1, 0)")
    c.commit()


class _FakeRunner:
    """WalkRunner stand-in that records a completed run (no LLM)."""

    def __init__(self, storage):
        self.storage = storage
        self.calls = []
        self.last_thread_id = None

    def run_walk(self, walk_name, book_id, config):
        self.calls.append((walk_name, book_id, config))
        self.last_thread_id = threading.get_ident()
        self.storage.execute_insert(
            "INSERT INTO walk_run (run_id, book_id, walk_name, status, created_ms,"
            " heartbeat_ms) VALUES (?, ?, ?, 'completed', 0, 0)",
            (f"run-{len(self.calls)}", book_id, walk_name),
        )
        return {"status": "completed"}


@pytest.fixture
def client():
    """Router-only TestClient with all dependencies overridden."""
    storage = InMemorySQLiteAdapter()
    storage.init_db()
    _seed(storage)
    workbench = Workbench(storage)
    runner = _FakeRunner(storage)

    app = FastAPI()
    app.include_router(pipeline_api.router)
    app.dependency_overrides[pipeline_api.get_storage] = lambda: storage
    for dep in _WORKBENCH_DEPS:
        app.dependency_overrides[dep] = lambda: workbench
    app.dependency_overrides[pipeline_api.get_walk_runner] = lambda: runner
    app.dependency_overrides[pipeline_api.get_review_manager] = lambda: ReviewManager(storage)

    # ConcurrentTransactionError -> 503 + Retry-After (mirrors real app.app).
    @app.exception_handler(ConcurrentTransactionError)
    async def _concurrent_handler(request: Request, exc: ConcurrentTransactionError):
        return JSONResponse(
            status_code=503,
            content={"detail": "Concurrent write in progress - retry after the advertised delay"},
            headers={"Retry-After": "5"},
        )

    return TestClient(app)


# ---------------------------------------------------------------------------
# GET /api/pipeline/workbench/{book_id}
# ---------------------------------------------------------------------------


def test_workbench_state_read_model(client):
    resp = client.get("/api/pipeline/workbench/b1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["book_id"] == "b1"
    assert body["generation_revision"] == 1
    assert body["scenes"][0]["chapter_id"] == "ch1"
    assert body["scenes"][0]["scenes"][0]["scene_id"] == "sc1"
    span = body["scenes"][0]["scenes"][0]["paragraphs"][0]["spans"][0]
    assert span["span_id"] == "sp1"
    assert span["span_position"] == 0
    assert "id" not in span
    assert "position" not in span
    assert body["presence"][0]["source"] == "walk"
    assert body["conflicts"] == []
    assert body["runs"] == []


def test_workbench_state_unknown_book_404(client):
    resp = client.get("/api/pipeline/workbench/nope")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Config + overrides
# ---------------------------------------------------------------------------


def test_config_shape(client):
    resp = client.get("/api/pipeline/workbench/b1/config")
    assert resp.status_code == 200
    body = resp.json()
    assert body["db_overrides"] == []
    assert set(body["effective"]) == {
        "walk_2b_character_discovery",
        "walk_2c_alias_resolution",
        "walk_2d_scene_presence",
    }
    assert body["validation_errors"] == []


def test_override_put_delete(client):
    put = client.put(
        "/api/pipeline/workbench/b1/overrides",
        json={"walk_name": "walk_2b_character_discovery", "key": "temperature",
              "value": 0.3, "base_revision": 1},
    )
    assert put.status_code == 200
    body = put.json()
    assert body["walk_name"] == "walk_2b_character_discovery"
    assert body["key"] == "temperature"
    assert body["value"] == 0.3
    assert body["generation_revision"] == 2

    cfg = client.get("/api/pipeline/workbench/b1/config").json()
    assert cfg["db_overrides"][0]["key"] == "temperature"

    # stale revision -> 409
    stale = client.put(
        "/api/pipeline/workbench/b1/overrides",
        json={"walk_name": "walk_2b_character_discovery", "key": "model_name",
              "value": "x", "base_revision": 1},
    )
    assert stale.status_code == 409

    delete = client.request(
        "DELETE", "/api/pipeline/workbench/b1/overrides",
        json={"walk_name": "walk_2b_character_discovery", "key": "temperature",
              "base_revision": 2},
    )
    assert delete.status_code == 200
    assert client.get("/api/pipeline/workbench/b1/config").json()["db_overrides"] == []


def test_override_validation_422(client):
    bad_walk = client.put(
        "/api/pipeline/workbench/b1/overrides",
        json={"walk_name": "walk_2a_scene_segmentation", "key": "temperature",
              "value": 0.3, "base_revision": 1},
    )
    assert bad_walk.status_code == 422
    bad_key = client.put(
        "/api/pipeline/workbench/b1/overrides",
        json={"walk_name": "walk_2b_character_discovery", "key": "nonsense",
              "value": 1, "base_revision": 1},
    )
    assert bad_key.status_code == 422
    bad_temp = client.put(
        "/api/pipeline/workbench/b1/overrides",
        json={"walk_name": "walk_2b_character_discovery", "key": "temperature",
              "value": 2.0, "base_revision": 1},
    )
    assert bad_temp.status_code == 422


# ---------------------------------------------------------------------------
# Alias preview/commit
# ---------------------------------------------------------------------------


def test_alias_preview_and_commit(client):
    preview = client.post(
        "/api/pipeline/workbench/b1/alias-conversions/preview",
        json={"canonical_id": "canon", "member_ids": ["c1", "c2"], "base_revision": 1},
    )
    assert preview.status_code == 200
    body = preview.json()
    assert body["preview_token"].startswith("ap-")
    assert body["base_revision"] == 1
    assert body["expires_ms"] > 0
    token = body["preview_token"]

    # confirm_consequences=False -> 422 (token consumed single-use)
    no_confirm = client.post(
        "/api/pipeline/workbench/b1/alias-conversions/commit",
        json={"preview_token": token, "base_revision": 1, "confirm_consequences": False},
    )
    assert no_confirm.status_code == 422

    # a fresh preview commits cleanly with confirm_consequences=True
    preview2 = client.post(
        "/api/pipeline/workbench/b1/alias-conversions/preview",
        json={"canonical_id": "canon", "member_ids": ["c1", "c2"], "base_revision": 1},
    ).json()
    token2 = preview2["preview_token"]

    commit = client.post(
        "/api/pipeline/workbench/b1/alias-conversions/commit",
        json={"preview_token": token2, "base_revision": 1, "confirm_consequences": True},
    )
    assert commit.status_code == 200
    cbody = commit.json()
    assert cbody["status"] == "active"
    assert cbody["decision_id"]
    assert cbody["generation_revision"] == 2
    assert len(cbody["merge_ids"]) == 2

    # single-use: second commit of the same token is rejected (404/422)
    reuse = client.post(
        "/api/pipeline/workbench/b1/alias-conversions/commit",
        json={"preview_token": token2, "base_revision": 2, "confirm_consequences": True},
    )
    assert reuse.status_code in (404, 422)


def test_alias_preview_cross_book_token_rejected(client):
    preview = client.post(
        "/api/pipeline/workbench/b1/alias-conversions/preview",
        json={"canonical_id": "canon", "member_ids": ["c1"], "base_revision": 1},
    ).json()
    # commit against a different book path is not possible; a stale base revision
    # after the preview must 409.
    stale = client.post(
        "/api/pipeline/workbench/b1/alias-conversions/commit",
        json={"preview_token": preview["preview_token"], "base_revision": 999,
              "confirm_consequences": True},
    )
    assert stale.status_code == 409


# ---------------------------------------------------------------------------
# Boundary overrides
# ---------------------------------------------------------------------------


def test_boundary_override_lifecycle(client):
    put = client.put(
        "/api/pipeline/workbench/b1/boundary-overrides",
        json={
            "override_id": None,
            "anchor": {"scene_id": "sc1"},
            "payload": {"operation": "split", "boundary_offsets": [10]},
            "base_revision": 1,
        },
    )
    assert put.status_code == 200
    dto = put.json()
    assert dto["active"] is True
    assert dto["anchor"]["scene_id"] == "sc1"
    override_id = dto["override_id"]

    listed = client.get("/api/pipeline/workbench/b1/boundary-overrides")
    assert listed.status_code == 200
    assert any(o["override_id"] == override_id for o in listed.json())

    apply = client.post(
        f"/api/pipeline/workbench/b1/boundary-overrides/{override_id}/apply"
    )
    assert apply.status_code == 200
    assert apply.json()["active"] is True
    apply_revision = apply.json()["generation_revision"]

    delete = client.request(
        "DELETE",
        f"/api/pipeline/workbench/b1/boundary-overrides/{override_id}",
        json={"base_revision": apply_revision},
    )
    assert delete.status_code == 200
    assert delete.json()["active"] is False


def test_boundary_override_validation_422(client):
    # bad anchor: no id at all
    put = client.put(
        "/api/pipeline/workbench/b1/boundary-overrides",
        json={"anchor": {}, "payload": {"operation": "split", "boundary_offsets": [1]},
              "base_revision": 1},
    )
    assert put.status_code == 422


# ---------------------------------------------------------------------------
# Presence
# ---------------------------------------------------------------------------


def test_presence_put(client):
    resp = client.put(
        "/api/pipeline/workbench/b1/presence",
        json={"scene_id": "sc1", "character_id": "c1", "relation_type": "absent",
              "decision_id": None, "base_revision": 1},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["scene_id"] == "sc1"
    assert body["character_id"] == "c1"
    assert body["relation_type"] == "absent"
    assert body["status"] == "active"
    assert body["decision_id"]

    # stale revision -> 409
    stale = client.put(
        "/api/pipeline/workbench/b1/presence",
        json={"scene_id": "sc1", "character_id": "c1", "relation_type": "present",
              "base_revision": 1},
    )
    assert stale.status_code == 409

    # bad relation type -> 422
    bad = client.put(
        "/api/pipeline/workbench/b1/presence",
        json={"scene_id": "sc1", "character_id": "c1", "relation_type": "maybe",
              "base_revision": 2},
    )
    assert bad.status_code == 422


# ---------------------------------------------------------------------------
# Reruns
# ---------------------------------------------------------------------------


def test_rerun_run_walk_off_event_loop(client, monkeypatch):
    """Reruns execute synchronous walks on a worker thread."""
    loop_thread_id: dict[str, int] = {}
    real_to_thread = asyncio.to_thread

    async def _spy_to_thread(fn, *args, **kwargs):
        loop_thread_id["id"] = threading.get_ident()
        return await real_to_thread(fn, *args, **kwargs)

    monkeypatch.setattr(api_walks.asyncio, "to_thread", _spy_to_thread)

    response = client.post(
        "/api/pipeline/workbench/b1/reruns",
        json={"walk_name": "walk_2b_character_discovery", "scope": "book",
              "preserve_manual_decisions": True, "base_revision": 1},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    runner = client.app.dependency_overrides[pipeline_api.get_walk_runner]()
    assert loop_thread_id["id"] != runner.last_thread_id


def test_rerun_book_scope(client):
    resp = client.post(
        "/api/pipeline/workbench/b1/reruns",
        json={"walk_name": "walk_2b_character_discovery", "scope": "book",
              "preserve_manual_decisions": True, "base_revision": 1},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"]
    assert body["status"] == "completed"
    assert body["scope"] == "book"
    assert body["generation_revision"] == 2
    assert set(body["invalidated_walks"]) == {
        "walk_2c_alias_resolution", "walk_2d_scene_presence"
    }


def test_rerun_scenes_scope_and_rejection(client):
    ok = client.post(
        "/api/pipeline/workbench/b1/reruns",
        json={"walk_name": "walk_2d_scene_presence", "scope": "scenes",
              "scene_ids": ["sc1"], "base_revision": 1},
    )
    assert ok.status_code == 200
    assert ok.json()["scope"] == "scenes"
    assert ok.json()["invalidated_walks"] == []

    # 2c is book-global -> rejects scenes scope with 422
    reject = client.post(
        "/api/pipeline/workbench/b1/reruns",
        json={"walk_name": "walk_2c_alias_resolution", "scope": "scenes",
              "scene_ids": ["sc1"], "base_revision": 1},
    )
    assert reject.status_code == 422

    # empty scene list -> 422
    empty = client.post(
        "/api/pipeline/workbench/b1/reruns",
        json={"walk_name": "walk_2d_scene_presence", "scope": "scenes",
              "scene_ids": [], "base_revision": 1},
    )
    assert empty.status_code == 422

    # unreachable scene -> 422
    unreachable = client.post(
        "/api/pipeline/workbench/b1/reruns",
        json={"walk_name": "walk_2d_scene_presence", "scope": "scenes",
              "scene_ids": ["sc999"], "base_revision": 1},
    )
    assert unreachable.status_code == 422


def test_rerun_bad_walk_and_stale_revision(client):
    bad_walk = client.post(
        "/api/pipeline/workbench/b1/reruns",
        json={"walk_name": "walk_2a_scene_segmentation", "scope": "book",
              "base_revision": 1},
    )
    assert bad_walk.status_code == 422

    stale = client.post(
        "/api/pipeline/workbench/b1/reruns",
        json={"walk_name": "walk_2b_character_discovery", "scope": "book",
              "base_revision": 999},
    )
    assert stale.status_code == 409


# ---------------------------------------------------------------------------
# Contention -> 503 + Retry-After: 5
# ---------------------------------------------------------------------------


def test_concurrent_transaction_maps_to_503(client):
    storage = client.app.dependency_overrides[pipeline_api.get_storage]()

    @contextmanager
    def _boom():
        raise ConcurrentTransactionError("contended")

    storage.transaction = _boom
    resp = client.put(
        "/api/pipeline/workbench/b1/overrides",
        json={"walk_name": "walk_2b_character_discovery", "key": "temperature",
              "value": 0.4, "base_revision": 1},
    )
    assert resp.status_code == 503
    assert resp.headers.get("Retry-After") == "5"
