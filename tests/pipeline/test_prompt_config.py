"""API tests for the prompt/settings config routes (S3, Plan B).

Covers the S3 acceptance criteria:

* ``GET /api/pipeline/walks/{book_id}/config`` returns all nine
  ``WALK_TASK_NAMES`` tasks with on-disk -> llm.task_overrides -> DB
  ``walk_override`` precedence, DB prompt winning only when a non-empty
  string, explicit temperature ``0.0`` honored, and exact allowed keys.
* ``POST /api/pipeline/walks/{book_id}/config/validate`` is side-effect free.
* ``POST /api/pipeline/walks/{book_id}/config/revisions`` atomically persists a
  complete revision and applies only allowed overrides through the existing
  ``walk_override`` single-writer methods.
* Unknown task/key, malformed data, cross-book, stale, and contention errors
  map to the contract (422 / 409 / 503 + Retry-After: 5 / 404).
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
    """Minimal books (b1, b2) + spine for prompt-config routes."""
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
    c.commit()


def _insert_override(
    storage, book_id: str, walk_name: str, key: str, value
) -> None:
    """Insert a ``walk_override`` row (single-writer target the API reuses)."""
    import json

    c = storage.get_connection()
    c.execute(
        "INSERT INTO walk_override (book_id, walk_name, key, value_json)"
        " VALUES (?, ?, ?, ?)",
        (book_id, walk_name, key, json.dumps(value)),
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


def _assert_conflict(resp, code: str) -> dict:
    """Assert *resp* is a 409 carrying a structured ``RevisionConflictDTO``.

    Verifies the ``detail`` payload (FastAPI error body) has the exact shape
    ``{error, code, message, detail}`` and the conflict ``code`` (P6 amendment:
    ``PipelineWalkPromptConfigRevisionAPI.v1`` advertises ``RevisionConflictDTO``).
    """
    assert resp.status_code == 409
    dto = resp.json()["detail"]
    assert set(dto) == {"error", "code", "message", "detail"}
    assert dto["error"] == "revision_conflict"
    assert dto["code"] == code
    assert isinstance(dto["message"], str) and dto["message"]
    assert dto["detail"] is None or isinstance(dto["detail"], dict)
    return dto


NINE = [
    "scene_segmentation",
    "character_discovery",
    "script_alias_resolution",
    "scene_presence",
    "span_attribution",
    "character_description",
    "voice_audition",
    "voice_assignment",
    "delivery",
]


# ---------------------------------------------------------------------------
# GET /api/pipeline/walks/{book_id}/config
# ---------------------------------------------------------------------------


def test_get_config_returns_all_nine_tasks(client):
    client, storage = _client()
    resp = client.get("/api/pipeline/walks/b1/config")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["tasks"]) == set(NINE)
    for task in NINE:
        entry = body["tasks"][task]
        assert set(entry["values"]) == {"model_name", "reasoning_effort", "temperature", "prompt"}
        assert set(entry["sources"]) == {"model_name", "reasoning_effort", "temperature", "prompt"}


def test_get_config_unknown_book_404(client):
    client, storage = _client()
    resp = client.get("/api/pipeline/walks/nope/config")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Precedence: on-disk -> llm.task_overrides -> DB walk_override (DB wins)
# ---------------------------------------------------------------------------


def test_db_walk_override_wins_over_on_disk_and_task_overrides(client):
    client, storage = _client()
    # DB override for scene_segmentation prompt + temperature 0.0.
    _insert_override(storage, "b1", "scene_segmentation", "prompt", "db prompt wins")
    _insert_override(storage, "b1", "scene_segmentation", "temperature", 0.0)
    resp = client.get("/api/pipeline/walks/b1/config")
    body = resp.json()
    entry = body["tasks"]["scene_segmentation"]
    assert entry["values"]["prompt"] == "db prompt wins"
    assert entry["sources"]["prompt"] == "row"
    assert entry["values"]["temperature"] == 0.0
    assert entry["sources"]["temperature"] == "row"


def test_empty_db_prompt_does_not_win(client):
    client, storage = _client()
    _insert_override(storage, "b1", "scene_segmentation", "prompt", "")
    resp = client.get("/api/pipeline/walks/b1/config")
    entry = resp.json()["tasks"]["scene_segmentation"]
    # Empty DB prompt falls through (never wins).
    assert entry["values"]["prompt"] is None
    assert entry["sources"]["prompt"] != "row"


def test_non_string_db_prompt_does_not_win(client):
    client, storage = _client()
    _insert_override(storage, "b1", "scene_segmentation", "prompt", 123)
    resp = client.get("/api/pipeline/walks/b1/config")
    entry = resp.json()["tasks"]["scene_segmentation"]
    assert entry["values"]["prompt"] is None


# ---------------------------------------------------------------------------
# POST .../config/validate (side-effect free)
# ---------------------------------------------------------------------------


def test_validate_side_effect_free_and_rejects_unknown_task(client):
    client, storage = _client()
    before = storage.execute_query("SELECT COUNT(*) AS n FROM prompt_config_revision")[0]["n"]
    resp = client.post(
        "/api/pipeline/walks/b1/config/validate",
        json={"task": "not_a_task", "settings": {"temperature": 0.0}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is False
    assert body["errors"]
    after = storage.execute_query("SELECT COUNT(*) AS n FROM prompt_config_revision")[0]["n"]
    # Validate never writes a revision.
    assert before == after


def test_validate_unknown_key_rejected(client):
    client, storage = _client()
    resp = client.post(
        "/api/pipeline/walks/b1/config/validate",
        json={"task": "scene_segmentation", "settings": {"bogus_key": "x"}},
    )
    assert resp.status_code == 200
    assert resp.json()["valid"] is False


def test_validate_malformed_settings(client):
    client, storage = _client()
    resp = client.post(
        "/api/pipeline/walks/b1/config/validate",
        json={"task": "scene_segmentation", "settings": {"temperature": "hot"}},
    )
    assert resp.status_code == 200
    assert resp.json()["valid"] is False


# ---------------------------------------------------------------------------
# POST .../config/revisions (revision persistence + single-writer overrides)
# ---------------------------------------------------------------------------


def test_revisions_saves_and_supersedes(client):
    client, storage = _client()
    resp = client.post(
        "/api/pipeline/walks/b1/config/revisions",
        json={
            "task": "scene_segmentation",
            "settings": {"temperature": 0.0, "model_name": "custom/model"},
            "prompt": "first prompt",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["book_id"] == "b1"
    assert body["task"] == "scene_segmentation"
    assert body["revision_id"]
    assert body["effective_prompt"] == "first prompt"
    assert body["superseded_by"] is None

    # Overrides were applied through the walk_override single writer.
    rows = storage.execute_query(
        "SELECT key, value_json FROM walk_override WHERE book_id='b1'"
        " AND walk_name='scene_segmentation' ORDER BY key"
    )
    vals = {r["key"]: r["value_json"] for r in rows}
    assert "temperature" in vals and "prompt" in vals

    # A second save citing the head supersedes it.
    resp2 = client.post(
        "/api/pipeline/walks/b1/config/revisions",
        json={
            "task": "scene_segmentation",
            "settings": {"temperature": 0.1},
            "prompt": "second prompt",
            "base_revision": body["revision_id"],
        },
    )
    assert resp2.status_code == 201
    body2 = resp2.json()
    assert body2["base_revision"] == body["revision_id"]
    rows2 = storage.execute_query(
        "SELECT superseded_by FROM prompt_config_revision WHERE revision_id=?",
        (body["revision_id"],),
    )
    assert rows2[0]["superseded_by"] == body2["revision_id"]


def test_revisions_stale_base_revision_409(client):
    client, storage = _client()
    first = client.post(
        "/api/pipeline/walks/b1/config/revisions",
        json={"task": "scene_segmentation", "settings": {"temperature": 0.1}},
    )
    assert first.status_code == 201
    stale_id = first.json()["revision_id"]
    # Advance the head once more.
    second = client.post(
        "/api/pipeline/walks/b1/config/revisions",
        json={
            "task": "scene_segmentation",
            "settings": {"temperature": 0.2},
            "base_revision": stale_id,
        },
    )
    assert second.status_code == 201
    # Now citing the stale (superseded) id -> 409.
    third = client.post(
        "/api/pipeline/walks/b1/config/revisions",
        json={
            "task": "scene_segmentation",
            "settings": {"temperature": 0.3},
            "base_revision": stale_id,
        },
    )
    assert third.status_code == 409
    _assert_conflict(third, "STALE_BASE_REVISION")


def test_revisions_unknown_task_422(client):
    client, storage = _client()
    resp = client.post(
        "/api/pipeline/walks/b1/config/revisions",
        json={"task": "nope", "settings": {"temperature": 0.1}},
    )
    assert resp.status_code == 422


def test_revisions_unknown_book_404(client):
    client, storage = _client()
    resp = client.post(
        "/api/pipeline/walks/nope/config/revisions",
        json={"task": "scene_segmentation", "settings": {"temperature": 0.1}},
    )
    assert resp.status_code == 404


def test_revisions_cross_book_base_revision_409(client):
    client, storage = _client()
    b1 = client.post(
        "/api/pipeline/walks/b1/config/revisions",
        json={"task": "scene_segmentation", "settings": {"temperature": 0.1}},
    )
    b1_id = b1.json()["revision_id"]
    # b1's revision cited as the base for b2 -> stale/cross-book -> 409.
    b2 = client.post(
        "/api/pipeline/walks/b2/config/revisions",
        json={
            "task": "scene_segmentation",
            "settings": {"temperature": 0.2},
            "base_revision": b1_id,
        },
    )
    assert b2.status_code == 409
    # The save path treats a foreign base_revision as stale (head mismatch).
    _assert_conflict(b2, "STALE_BASE_REVISION")


def test_revisions_invalid_override_value_422(client):
    client, storage = _client()
    resp = client.post(
        "/api/pipeline/walks/b1/config/revisions",
        json={"task": "scene_segmentation", "settings": {"temperature": 99}},
    )
    assert resp.status_code == 422


def test_revisions_malformed_raw_json_422(client):
    client, storage = _client()
    resp = client.post(
        "/api/pipeline/walks/b1/config/revisions",
        json={
            "task": "scene_segmentation",
            "settings": {"temperature": 0.1},
            "raw_json": "{not valid json",
        },
    )
    assert resp.status_code == 422


def test_revisions_unknown_key_in_raw_json_422(client):
    client, storage = _client()
    resp = client.post(
        "/api/pipeline/walks/b1/config/revisions",
        json={
            "task": "scene_segmentation",
            "settings": {"temperature": 0.1},
            "raw_json": '{"evil_key": 1}',
        },
    )
    assert resp.status_code == 422


def test_revisions_contention_503(client):
    client, storage = _client()

    class _ContendingStorage:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            attr = getattr(self._inner, name)
            return attr

        def transaction(self):
            raise ConcurrentTransactionError("BEGIN IMMEDIATE timed out under contention")

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
    resp = tc.post(
        "/api/pipeline/walks/b1/config/revisions",
        json={"task": "scene_segmentation", "settings": {"temperature": 0.1}},
    )
    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == "5"
