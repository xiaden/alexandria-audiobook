"""API tests for the explicit scoped prompt rerun route (S4, Plan B).

Covers the S4 acceptance criteria:

* ``POST /api/pipeline/walks/{book_id}/reruns`` requires ``confirm=true``, an
  existing ``revision_id`` owned by the book, a valid nine-task scope, and
  reachable scenes for ``scenes`` scope.
* An identical revision+scope rerun never creates a duplicate run
  (``409 already_ran``); 2b/2c/2d reuse the combined-workbench invalidation
  DAG and ``walk_override`` single-writer; no save/review action auto-runs a
  walk (no auto-cascade).
* Returned run metadata identifies run, revision, and scope; contention maps
  to ``503`` with ``Retry-After: 5``.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import app.pipeline.api as pipeline_api
from app.pipeline.adapter import (
    ConcurrentTransactionError,
    InMemorySQLiteAdapter,
)


def _seed(storage) -> None:
    """Minimal books (b1, b2) + a b1 spine with reachable scene 'sc1'."""
    c = storage.get_connection()
    c.execute("INSERT INTO series (id) VALUES ('s1')")
    c.execute(
        "INSERT INTO book (id, series_id, book_number, version, position)"
        " VALUES ('b1', 's1', 1, 0, 0)"
    )
    c.execute(
        "INSERT INTO book (id, series_id, book_number, version, position)"
        " VALUES ('b2', 's1', 2, 0, 1)"
    )
    c.execute("INSERT INTO scene (id) VALUES ('sc1')")
    c.execute("INSERT INTO scene (id) VALUES ('sc2')")
    c.execute("INSERT INTO chapter (id, book_id) VALUES ('ch1', 'b1')")
    c.execute(
        "INSERT INTO book_chapter (child_id, parent_id, position) VALUES ('ch1', 'b1', 0)"
    )
    c.execute(
        "INSERT INTO chapter_scene (child_id, parent_id, position) VALUES ('sc1', 'ch1', 0)"
    )
    c.execute(
        "INSERT INTO chapter_scene (child_id, parent_id, position) VALUES ('sc2', 'ch1', 1)"
    )
    c.commit()


def _make_app(storage):
    app = FastAPI()
    app.include_router(pipeline_api.router)
    app.dependency_overrides[pipeline_api.get_storage] = lambda: storage

    @app.exception_handler(ConcurrentTransactionError)
    async def _concurrent_handler(request: Request, exc: ConcurrentTransactionError):
        return JSONResponse(
            status_code=503,
            content={"detail": "Concurrent write in progress"},
            headers={"Retry-After": "5"},
        )

    return TestClient(app)


def _client():
    storage = InMemorySQLiteAdapter()
    storage.init_db()
    _seed(storage)
    return _make_app(storage), storage


@pytest.fixture
def client():
    """Router-only TestClient over a seeded in-memory adapter."""
    return _client()[0]


def _save(client, book_id="b1", task="scene_segmentation", **settings):
    """Save a prompt-config revision and return its revision_id."""
    body = {"task": task, "settings": settings or {"temperature": 0.1}}
    resp = client.post(f"/api/pipeline/walks/{book_id}/config/revisions", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["revision_id"]


def _rerun(revision_id, *, scope="book", confirm=True, scene_ids=None):
    body = {"revision_id": revision_id, "scope": scope, "confirm": confirm}
    if scene_ids is not None:
        body["scene_ids"] = scene_ids
    return body


def _assert_conflict(resp, code: str) -> dict:
    """Assert *resp* is a 409 carrying a structured ``RevisionConflictDTO``.

    Verifies the ``detail`` payload (FastAPI error body) has the exact shape
    ``{error, code, message, detail}`` and the conflict ``code`` (P6 amendment).
    """
    assert resp.status_code == 409
    dto = resp.json()["detail"]
    assert set(dto) == {"error", "code", "message", "detail"}
    assert dto["error"] == "revision_conflict"
    assert dto["code"] == code
    assert isinstance(dto["message"], str) and dto["message"]
    assert dto["detail"] is None or isinstance(dto["detail"], dict)
    return dto


# ---------------------------------------------------------------------------
# Confirmation / scope / ownership requirements
# ---------------------------------------------------------------------------


def test_rerun_requires_confirm(client):
    rid = _save(client)
    resp = client.post(
        "/api/pipeline/walks/b1/reruns", json=_rerun(rid, confirm=False)
    )
    assert resp.status_code == 422
    assert "confirm=true" in resp.json()["detail"]


def test_rerun_unknown_revision_returns_404(client):
    resp = client.post(
        "/api/pipeline/walks/b1/reruns", json=_rerun("missing-revision")
    )
    assert resp.status_code == 404


def test_rerun_unknown_book_returns_404(client):
    rid = _save(client)
    resp = client.post(
        "/api/pipeline/walks/nope/reruns", json=_rerun(rid)
    )
    assert resp.status_code == 404


def test_rerun_bad_scope_returns_422(client):
    rid = _save(client)
    resp = client.post(
        "/api/pipeline/walks/b1/reruns",
        json={"revision_id": rid, "scope": "everywhere", "confirm": True},
    )
    assert resp.status_code == 422


def test_rerun_cross_book_revision_returns_409(client):
    # A revision saved under b2 cited for a b1 rerun -> cross-book 409.
    rid = _save(client, book_id="b2")
    resp = client.post("/api/pipeline/walks/b1/reruns", json=_rerun(rid))
    assert resp.status_code == 409
    body = _assert_conflict(resp, "CROSS_BOOK")
    assert body["detail"]["revision_id"] == rid
    assert body["detail"]["revision_book_id"] == "b2"
    assert body["detail"]["requested_book_id"] == "b1"


# ---------------------------------------------------------------------------
# Scenes scope: reachability + book-global 2c
# ---------------------------------------------------------------------------


def test_rerun_scenes_scope_requires_non_empty(client):
    rid = _save(client)
    resp = client.post(
        "/api/pipeline/walks/b1/reruns",
        json=_rerun(rid, scope="scenes", scene_ids=[]),
    )
    assert resp.status_code == 422


def test_rerun_scenes_scope_rejects_unreachable(client):
    rid = _save(client)
    resp = client.post(
        "/api/pipeline/walks/b1/reruns",
        json=_rerun(rid, scope="scenes", scene_ids=["sc999"]),
    )
    assert resp.status_code == 422
    assert "not reachable" in resp.json()["detail"]


def test_rerun_script_alias_resolution_rejects_scenes(client):
    rid = _save(client, task="script_alias_resolution")
    resp = client.post(
        "/api/pipeline/walks/b1/reruns",
        json=_rerun(rid, scope="scenes", scene_ids=["sc1"]),
    )
    assert resp.status_code == 422
    assert "book-global" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Successful rerun: creates a new head revision (the run), no auto-cascade
# ---------------------------------------------------------------------------


def test_rerun_creates_new_run_revision(client):
    rid = _save(client)
    resp = client.post("/api/pipeline/walks/b1/reruns", json=_rerun(rid))
    assert resp.status_code == 200
    body = resp.json()
    assert body["scope"] == "book"
    assert body["revision_id"] == rid
    assert body["run_id"] and body["run_id"] != rid
    # Non-workbench task -> no invalidation DAG downstream walks.
    assert body["invalidated_walks"] == []

    # The rerun produced a new head revision, superseding the referenced one.
    rows = client.app.dependency_overrides[pipeline_api.get_storage]().execute_query(
        "SELECT revision_id, superseded_by FROM prompt_config_revision"
        " WHERE book_id='b1' AND task='scene_segmentation' ORDER BY created_ms"
    )
    assert rows[0]["superseded_by"] == body["run_id"]
    assert rows[-1]["superseded_by"] is None


def test_rerun_scenes_scope(client):
    rid = _save(client)
    resp = client.post(
        "/api/pipeline/walks/b1/reruns",
        json=_rerun(rid, scope="scenes", scene_ids=["sc1"]),
    )
    assert resp.status_code == 200
    assert resp.json()["scope"] == "scenes"


def test_rerun_2b_invalidation_dag_reused(client):
    # character_discovery (walk_2b) invalidates 2c + 2d via the reused DAG.
    rid = _save(client, task="character_discovery")
    resp = client.post("/api/pipeline/walks/b1/reruns", json=_rerun(rid))
    assert resp.status_code == 200
    assert set(resp.json()["invalidated_walks"]) == {
        "walk_2c_alias_resolution",
        "walk_2d_scene_presence",
    }


def test_rerun_2d_invalidation_dag_empty(client):
    rid = _save(client, task="scene_presence")
    resp = client.post("/api/pipeline/walks/b1/reruns", json=_rerun(rid))
    assert resp.status_code == 200
    assert resp.json()["invalidated_walks"] == []


def test_rerun_never_auto_runs_a_walk(client):
    rid = _save(client)
    client.post("/api/pipeline/walks/b1/reruns", json=_rerun(rid))
    # No save/review action auto-runs a walk -> no walk_run row was created.
    rows = client.app.dependency_overrides[pipeline_api.get_storage]().execute_query(
        "SELECT COUNT(*) AS n FROM walk_run WHERE book_id='b1'"
    )
    assert rows[0]["n"] == 0


# ---------------------------------------------------------------------------
# Dedupe: identical revision+scope never creates a duplicate run
# ---------------------------------------------------------------------------


def test_dedupe_same_revision_and_scope(client):
    rid = _save(client)
    first = client.post("/api/pipeline/walks/b1/reruns", json=_rerun(rid))
    assert first.status_code == 200
    second = client.post("/api/pipeline/walks/b1/reruns", json=_rerun(rid))
    assert second.status_code == 409
    body = _assert_conflict(second, "ALREADY_RAN")
    assert body["detail"]["revision_id"] == rid
    assert body["detail"]["scope"] == "book"
    assert body["detail"]["task"] == "scene_segmentation"
    assert body["detail"]["book_id"] == "b1"
    # The head produced by the earlier rerun differs from the referenced id.
    assert body["detail"]["head_revision_id"]
    assert body["detail"]["head_revision_id"] != rid


def test_dedupe_differs_across_tasks(client):
    # A rerun of one task does not block a rerun of a different task.
    rid_a = _save(client, task="scene_segmentation")
    rid_b = _save(client, task="span_attribution")
    assert client.post("/api/pipeline/walks/b1/reruns", json=_rerun(rid_a)).status_code == 200
    assert client.post("/api/pipeline/walks/b1/reruns", json=_rerun(rid_b)).status_code == 200


# ---------------------------------------------------------------------------
# Contention -> 503 + Retry-After: 5
# ---------------------------------------------------------------------------


def test_rerun_contention_503(client):
    client, storage = _client()

    class _ContendingStorage:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def transaction(self):
            raise ConcurrentTransactionError("BEGIN IMMEDIATE timed out under contention")

    # Save the revision against the real storage first so the rerun validates.
    real_client = _make_app(storage)
    rid = _save(real_client)

    app = FastAPI()
    app.include_router(pipeline_api.router)
    app.dependency_overrides[pipeline_api.get_storage] = lambda: _ContendingStorage(storage)

    @app.exception_handler(ConcurrentTransactionError)
    async def _h(request, exc):
        return JSONResponse(
            status_code=503,
            content={"detail": "Concurrent write in progress"},
            headers={"Retry-After": "5"},
        )

    tc = TestClient(app)
    resp = tc.post("/api/pipeline/walks/b1/reruns", json=_rerun(rid))
    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == "5"
