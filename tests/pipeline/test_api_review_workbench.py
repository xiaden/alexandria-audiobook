"""API tests for the review action dispatch (decision:/junction:/walkitem:)
and workbench decision undo (S2).

Covers the S2 acceptance criteria:

* Every review action is one transaction, returns the registered response
  shape, and maps 404/409/422 correctly.
* Existing review accept/reject/override remains resolution authority for
  legacy bare-junction and ``walkitem:`` ids (unchanged ``{status,item_id}``).
* ``decision:`` / ``junction:`` dispatch returns ActionResultDTO.
* Undo creates an inverse decision and returns 409 on newer state.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.pipeline.api as pipeline_api
from app.pipeline.adapter import InMemorySQLiteAdapter
from app.pipeline.api_characters import get_workbench as _cp_workbench
from app.pipeline.api_review import get_workbench as _rv_workbench
from app.pipeline.api_walks import get_workbench as _wk_workbench
from app.pipeline.review import ReviewManager
from app.pipeline.workbench import Workbench

_WORKBENCH_DEPS = (_cp_workbench, _rv_workbench, _wk_workbench)


def _seed(storage) -> None:
    c = storage.get_connection()
    c.execute("INSERT INTO series (id) VALUES ('s1')")
    c.execute("INSERT INTO book (id, series_id, book_number, version, position)"
              " VALUES ('b1', 's1', 1, 0, 0)")
    c.execute("INSERT INTO character (id, name, aliases) VALUES ('c1', 'Alice', '[]')")
    c.execute("INSERT INTO character (id, name, aliases) VALUES ('canon', 'Canon', '[]')")
    c.execute("INSERT INTO scene (id) VALUES ('sc1')")
    c.execute("INSERT INTO chapter (id, book_id) VALUES ('ch1', 'b1')")
    c.execute("INSERT INTO paragraph (id) VALUES ('p1')")
    c.execute("INSERT INTO span (id, span_type, text) VALUES ('sp1', 'sentence', 'Hi')")
    c.execute("INSERT INTO book_chapter (child_id, parent_id, position) VALUES ('ch1', 'b1', 0)")
    c.execute("INSERT INTO chapter_scene (child_id, parent_id, position) VALUES ('sc1', 'ch1', 0)")
    c.execute("INSERT INTO scene_paragraph (child_id, parent_id, position) VALUES ('p1', 'sc1', 0)")
    c.execute("INSERT INTO paragraph_span (child_id, parent_id, position) VALUES ('sp1', 'p1', 0)")
    # a low-confidence live junction -> surfaces in review and is dispatchable
    c.execute(
        "INSERT INTO character_scene (character_id, scene_id, relation_type,"
        " source, confidence, human_override)"
        " VALUES ('c1', 'sc1', 'present', 'walk', 0.6, 0)"
    )
    c.execute("INSERT INTO workbench_generation (generation_id, book_id, revision, updated_ms)"
              " VALUES ('wg-b1', 'b1', 1, 0)")
    c.commit()


@pytest.fixture
def client():
    storage = InMemorySQLiteAdapter()
    storage.init_db()
    _seed(storage)
    workbench = Workbench(storage)

    app = FastAPI()
    app.include_router(pipeline_api.router)
    app.dependency_overrides[pipeline_api.get_storage] = lambda: storage
    for dep in _WORKBENCH_DEPS:
        app.dependency_overrides[dep] = lambda: workbench
    app.dependency_overrides[pipeline_api.get_review_manager] = lambda: ReviewManager(storage)
    app.dependency_overrides[pipeline_api.get_walk_runner] = lambda: None
    return TestClient(app)


def _insert_decision(storage, decision_id, *, status="active", kind="presence",
                     decision_type="presence:absent", key="sc1:c1", book="b1",
                     base_revision=0):
    storage.execute_insert(
        "INSERT INTO workbench_decision (decision_id, book_id, target_kind,"
        " target_key, decision_type, base_revision, payload_json, status, source,"
        " created_ms) VALUES (?, ?, ?, ?, ?, ?, '{}', ?, 'human', 0)",
        (decision_id, book, kind, key, decision_type, base_revision, status),
    )


# ---------------------------------------------------------------------------
# decision: dispatch
# ---------------------------------------------------------------------------


def test_decision_accept_returns_action_dto(client):
    storage = client.app.dependency_overrides[pipeline_api.get_storage]()
    _insert_decision(storage, "dec-1")
    resp = client.post(
        "/api/pipeline/review/accept", json={"item_id": "decision:dec-1"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["item_id"] == "decision:dec-1"
    assert body["status"] == "accepted"
    assert body["decision_id"]
    assert body["superseded_item_ids"] == ["dec-1"]
    assert body["generation_revision"] == 2
    # referenced decision marked superseded
    row = storage.execute_query(
        "SELECT status FROM workbench_decision WHERE decision_id = 'dec-1'"
    )[0]
    assert row["status"] == "superseded"


def test_decision_unknown_404_and_terminal_409(client):
    storage = client.app.dependency_overrides[pipeline_api.get_storage]()
    assert client.post(
        "/api/pipeline/review/accept", json={"item_id": "decision:missing"}
    ).status_code == 404

    _insert_decision(storage, "dec-1", status="undone")
    assert client.post(
        "/api/pipeline/review/reject", json={"item_id": "decision:dec-1"}
    ).status_code == 409


def test_decision_override_returns_action_dto(client):
    storage = client.app.dependency_overrides[pipeline_api.get_storage]()
    _insert_decision(storage, "dec-1")
    resp = client.post(
        "/api/pipeline/review/override",
        json={"item_id": "decision:dec-1", "new_value": {"relation_type": "speaker"}},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "overridden"


# ---------------------------------------------------------------------------
# junction: dispatch
# ---------------------------------------------------------------------------


def test_junction_accept_resolves_and_returns_action_dto(client):
    storage = client.app.dependency_overrides[pipeline_api.get_storage]()
    resp = client.post(
        "/api/pipeline/review/accept",
        json={"item_id": "junction:character_scene:c1:sc1", "base_revision": 1},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["decision_id"]
    assert body["generation_revision"] == 2
    # junction actually resolved (confidence -> 1.0)
    row = storage.execute_query(
        "SELECT confidence FROM character_scene WHERE character_id = 'c1'"
        " AND scene_id = 'sc1'"
    )[0]
    assert row["confidence"] == 1.0


def test_junction_stale_revision_409_and_unknown_404(client):
    # stale base revision -> 409
    stale = client.post(
        "/api/pipeline/review/accept",
        json={"item_id": "junction:character_scene:c1:sc1", "base_revision": 99},
    )
    assert stale.status_code == 409
    # unknown junction -> 404
    unknown = client.post(
        "/api/pipeline/review/accept",
        json={"item_id": "junction:character_scene:c1:sc9"},
    )
    assert unknown.status_code == 404


# ---------------------------------------------------------------------------
# legacy authority preserved
# ---------------------------------------------------------------------------


def test_legacy_bare_junction_and_walkitem_unchanged(client):
    storage = client.app.dependency_overrides[pipeline_api.get_storage]()
    bare = client.post(
        "/api/pipeline/review/accept", json={"item_id": "character_scene:c1:sc1"}
    )
    assert bare.status_code == 200
    assert bare.json() == {"status": "accepted", "item_id": "character_scene:c1:sc1"}

    # a walkitem
    storage.execute_insert(
        "INSERT INTO walk_review_item (id, book_id, run_id, kind, target_table,"
        " target_id, prior_value, status, created_ms)"
        " VALUES ('w1', 'b1', 'run1', 'voice_profile', 'character_metadata',"
        " 'c1', '{}', 'pending', 0)"
    )
    witem = client.post(
        "/api/pipeline/review/reject", json={"item_id": "walkitem:w1"}
    )
    assert witem.status_code == 200
    assert witem.json() == {"status": "rejected", "item_id": "walkitem:w1"}


# ---------------------------------------------------------------------------
# undo
# ---------------------------------------------------------------------------


def test_undo_creates_inverse_decision(client):
    storage = client.app.dependency_overrides[pipeline_api.get_storage]()
    _insert_decision(storage, "dec-1", base_revision=0)
    resp = client.post(
        "/api/pipeline/workbench/b1/decisions/dec-1/undo", json={"base_revision": 1}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "undone"
    assert body["decision_id"]  # inverse decision id
    assert body["generation_revision"] == 2
    row = storage.execute_query(
        "SELECT status, undone_by FROM workbench_decision WHERE decision_id = 'dec-1'"
    )[0]
    assert row["status"] == "undone"
    assert row["undone_by"] == body["decision_id"]


def test_undo_newer_state_409_and_unknown_404(client):
    storage = client.app.dependency_overrides[pipeline_api.get_storage]()
    # stale base revision -> newer state exists -> 409
    _insert_decision(storage, "dec-1", base_revision=0)
    stale = client.post(
        "/api/pipeline/workbench/b1/decisions/dec-1/undo", json={"base_revision": 99}
    )
    assert stale.status_code == 409
    # unknown decision -> 404
    unknown = client.post(
        "/api/pipeline/workbench/b1/decisions/nope/undo", json={"base_revision": 1}
    )
    assert unknown.status_code == 404


def test_undo_terminal_decision_409(client):
    storage = client.app.dependency_overrides[pipeline_api.get_storage]()
    _insert_decision(storage, "dec-1", base_revision=0)
    first = client.post(
        "/api/pipeline/workbench/b1/decisions/dec-1/undo", json={"base_revision": 1}
    )
    assert first.status_code == 200
    second = client.post(
        "/api/pipeline/workbench/b1/decisions/dec-1/undo", json={"base_revision": 2}
    )
    assert second.status_code == 409


def test_alias_unmerge_uses_merge_decision_id(client):
    workbench = client.app.dependency_overrides[_rv_workbench]()
    preview = workbench.preview_alias_conversion(
        book_id="b1", canonical_id="canon", member_ids=["c1"], base_revision=1,
    )
    commit = workbench.commit_alias_conversion(
        book_id="b1", preview_token=preview["preview_token"],
        base_revision=1, confirm_consequences=True,
    )
    merge_id = commit["merge_ids"][0]
    decision_id = commit["decision_id"]

    wrong_id = client.post(
        f"/api/pipeline/workbench/b1/decisions/{merge_id}/undo",
        json={"base_revision": 2},
    )
    assert wrong_id.status_code == 404

    response = client.post(
        f"/api/pipeline/workbench/b1/decisions/{decision_id}/undo",
        json={"base_revision": 2},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "undone"
