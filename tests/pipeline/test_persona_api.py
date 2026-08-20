"""Spec-first tests for the persona HTTP API (S2).

Covers the S2 acceptance criteria for ``app/pipeline/api_characters.py``:

* GET  /api/pipeline/characters/{character_id}/persona          -> PersonaDTO
* PUT  /api/pipeline/characters/{character_id}/persona          -> PersonaDTO
* GET  /api/pipeline/characters/{character_id}/persona/revisions -> list
* POST /api/pipeline/characters/{character_id}/persona/validate -> side-effect free
* POST /api/pipeline/characters/{character_id}/persona/rerun    -> {run_id, revision_id, scope}

Error mapping:
* unknown character / book / cross-owner   -> 404
* stale base_revision / protected          -> 409
* validation failures                      -> 422
* concurrent write contention              -> 503 + Retry-After: 5

Every route is character-local (local-owner bound); persona writes never assign
a resolved voice; validate never writes a revision.
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
from app.pipeline.api_onboard import get_storage
from app.pipeline.api_characters import get_persona_domain


def _seed(storage: InMemorySQLiteAdapter) -> None:
    """Minimal series/book/spine/characters with one book (b1) and scenes."""
    c = storage.get_connection()
    c.execute("INSERT INTO series (id) VALUES ('s1')")
    c.execute(
        "INSERT INTO book (id, series_id, book_number, version, position)"
        " VALUES ('b1', 's1', 1, 0, 0)"
    )
    for cid in ("c1", "c2"):
        c.execute(
            "INSERT INTO character (id, name, aliases) VALUES (?, ?, '[]')",
            (cid, cid.capitalize()),
        )
    c.execute("INSERT INTO scene (id) VALUES ('sc1')")
    c.execute("INSERT INTO scene (id) VALUES ('sc2')")
    c.execute("INSERT INTO chapter (id, book_id) VALUES ('ch1', 'b1')")
    c.execute("INSERT INTO paragraph (id) VALUES ('p1')")
    c.execute("INSERT INTO span (id, span_type, text) VALUES ('sp1', 'sentence', 'Hi')")
    c.execute("INSERT INTO span (id, span_type, text) VALUES ('sp2', 'sentence', 'Bye')")
    c.execute("INSERT INTO book_chapter (child_id, parent_id, position) VALUES ('ch1', 'b1', 0)")
    c.execute("INSERT INTO chapter_scene (child_id, parent_id, position) VALUES ('sc1', 'ch1', 0)")
    c.execute("INSERT INTO chapter_scene (child_id, parent_id, position) VALUES ('sc2', 'ch1', 1)")
    c.execute("INSERT INTO scene_paragraph (child_id, parent_id, position) VALUES ('p1', 'sc1', 0)")
    c.execute("INSERT INTO paragraph_span (child_id, parent_id, position) VALUES ('sp1', 'p1', 0)")
    c.execute("INSERT INTO paragraph_span (child_id, parent_id, position) VALUES ('sp2', 'p1', 1)")


def _write(
    *,
    character_id: str = "c1",
    book_id: str | None = "b1",
    base_revision: int,
    protected: bool = False,
    **overrides: object,
) -> dict:
    body: dict = {
        "book_id": book_id,
        "base_revision": base_revision,
        "fields": {
            "identity": "Brave and curious",
            "speech": "Measured, deliberate",
        },
        "evidence": [
            {"anchor": "sp1", "quote": "Hi", "source": "book", "confidence": 0.9}
        ],
        "aliases": ["Ali"],
        "scene_scope": "book",
        "scene_ids": [],
        "review_state": "draft",
        "protected": protected,
    }
    body.update(overrides)
    body["character_id"] = character_id
    return body


@pytest.fixture()
def storage():
    adapter = InMemorySQLiteAdapter()
    adapter.init_db()
    _seed(adapter)
    return adapter


@pytest.fixture()
def client(storage):
    app = FastAPI()
    app.include_router(pipeline_api.router)

    def _get_storage():
        return storage

    app.dependency_overrides[get_storage] = _get_storage
    app.dependency_overrides[get_persona_domain] = lambda: _pd(storage)

    @app.exception_handler(ConcurrentTransactionError)
    async def _handler(request: Request, exc: ConcurrentTransactionError):
        return JSONResponse(status_code=503, content={"detail": str(exc)},
                            headers={"Retry-After": "5"})

    return TestClient(app)


def _pd(storage):
    # Late import to avoid circular wiring in test module scope.
    from app.pipeline.persona import PersonaDomain
    return PersonaDomain(storage)


def _assert_conflict(resp, code: str) -> dict:
    """Assert *resp* is a 409 carrying a structured ``RevisionConflictDTO``.

    Verifies the ``detail`` payload (FastAPI error body) has the exact shape
    ``{error, code, message, detail}`` and the conflict ``code`` (P6 amendment:
    both parity contracts advertise ``RevisionConflictDTO``).  Returns the DTO
    for further ``detail`` checks.
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
# GET /api/pipeline/characters/{character_id}/persona
# ---------------------------------------------------------------------------


class TestGetPersona:
    def test_returns_current_head(self, client):
        client.put(
            "/api/pipeline/characters/c1/persona",
            json=_write(base_revision=0),
        )
        r = client.get("/api/pipeline/characters/c1/persona")
        assert r.status_code == 200
        dto = r.json()
        assert dto["character_id"] == "c1"
        assert dto["revision"] == 1
        assert dto["scene_scope"] == "book"
        assert dto["fields"]["identity"] == "Brave and curious"
        assert dto["voice_consequences"]["assignment"] is None
        assert set(dto) >= {
            "persona_id", "character_id", "book_id", "revision", "fields",
            "evidence", "aliases", "scene_scope", "scene_ids", "review_state",
            "protected", "voice_consequences", "author_id", "created_ms",
            "superseded_by",
        }

    def test_returns_404_for_unknown_character(self, client):
        r = client.get("/api/pipeline/characters/nope/persona")
        assert r.status_code == 404

    def test_returns_404_when_no_persona_yet(self, client):
        r = client.get("/api/pipeline/characters/c1/persona")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# PUT /api/pipeline/characters/{character_id}/persona
# ---------------------------------------------------------------------------


class TestPutPersona:
    def test_creates_first_revision(self, client):
        r = client.put(
            "/api/pipeline/characters/c1/persona",
            json=_write(base_revision=0),
        )
        assert r.status_code == 200
        dto = r.json()
        assert dto["revision"] == 1
        assert dto["protected"] is False
        assert dto["voice_consequences"]["assignment"] is None

    def test_requires_base_revision(self, client):
        body = _write(base_revision=0)
        del body["base_revision"]
        r = client.put("/api/pipeline/characters/c1/persona", json=body)
        # Missing required field -> 422 (FastAPI validation)
        assert r.status_code == 422

    def test_stale_revision_returns_409(self, client):
        client.put("/api/pipeline/characters/c1/persona", json=_write(base_revision=0))
        r = client.put(
            "/api/pipeline/characters/c1/persona",
            json=_write(base_revision=0),  # head is now revision 1
        )
        assert r.status_code == 409
        body = _assert_conflict(r, "STALE_BASE_REVISION")
        # Stale detail is derived from the domain message only (null is valid).
        assert body["detail"] is None

    def test_unknown_character_returns_404(self, client):
        r = client.put(
            "/api/pipeline/characters/nope/persona",
            json=_write(base_revision=0),
        )
        assert r.status_code == 404

    def test_unknown_book_returns_404(self, client):
        r = client.put(
            "/api/pipeline/characters/c1/persona",
            json=_write(base_revision=0, book_id="missing-book"),
        )
        assert r.status_code == 404

    def test_validation_failure_returns_422(self, client):
        r = client.put(
            "/api/pipeline/characters/c1/persona",
            json=_write(base_revision=0, review_state="bogus"),
        )
        assert r.status_code == 422
        assert "review_state" in r.json()["detail"]

    def test_local_owner_bound(self, client):
        # Character c2 is a different local owner; c1 persona write must not
        # leak onto c2.
        client.put("/api/pipeline/characters/c1/persona", json=_write(base_revision=0))
        r = client.get("/api/pipeline/characters/c2/persona")
        assert r.status_code == 404

    def test_never_assigns_voice(self, client, storage):
        client.put(
            "/api/pipeline/characters/c1/persona",
            json=_write(base_revision=0),
        )
        # The character voice assignment must remain untouched (no implicit voice).
        rows = storage.execute_query(
            "SELECT voice_assignment_id FROM character WHERE id = 'c1'"
        )
        assert rows[0]["voice_assignment_id"] is None

    def test_contention_returns_503(self, storage, monkeypatch):
        # Force the persona write to contend.
        from app.pipeline import persona as persona_mod

        def _boom(self_, *a, **k):
            raise ConcurrentTransactionError("contended")

        monkeypatch.setattr(persona_mod.PersonaDomain, "save", _boom)
        client = _client_with_retry_handler(storage)
        r = client.put(
            "/api/pipeline/characters/c1/persona",
            json=_write(base_revision=0),
        )
        assert r.status_code == 503
        assert r.headers.get("Retry-After") == "5"


# ---------------------------------------------------------------------------
# GET /api/pipeline/characters/{character_id}/persona/revisions
# ---------------------------------------------------------------------------


class TestGetPersonaRevisions:
    def test_lists_revisions_newest_first(self, client):
        client.put("/api/pipeline/characters/c1/persona", json=_write(base_revision=0))
        client.put(
            "/api/pipeline/characters/c1/persona",
            json=_write(base_revision=1, fields={"identity": "Updated"}),
        )
        r = client.get("/api/pipeline/characters/c1/persona/revisions")
        assert r.status_code == 200
        body = r.json()
        assert body["character_id"] == "c1"
        revs = body["revisions"]
        assert [v["revision"] for v in revs] == [2, 1]
        assert revs[1]["superseded_by"] == revs[0]["persona_id"]

    def test_unknown_character_returns_404(self, client):
        r = client.get("/api/pipeline/characters/nope/persona/revisions")
        assert r.status_code == 404

    def test_empty_list_for_character_without_persona(self, client):
        r = client.get("/api/pipeline/characters/c2/persona/revisions")
        assert r.status_code == 200
        assert r.json()["revisions"] == []


# ---------------------------------------------------------------------------
# POST /api/pipeline/characters/{character_id}/persona/validate
# ---------------------------------------------------------------------------


class TestPostValidate:
    def test_valid_write(self, client):
        r = client.post(
            "/api/pipeline/characters/c1/persona/validate",
            json=_write(base_revision=0),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["valid"] is True
        assert body["errors"] == []
        assert body["voice_consequences"]["assignment"] is None

    def test_invalid_write(self, client):
        r = client.post(
            "/api/pipeline/characters/c1/persona/validate",
            json=_write(base_revision=0, review_state="bogus"),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["valid"] is False
        assert any("review_state" in e for e in body["errors"])

    def test_side_effect_free(self, client, storage):
        client.post(
            "/api/pipeline/characters/c1/persona/validate",
            json=_write(base_revision=0),
        )
        rows = storage.execute_query("SELECT COUNT(*) AS n FROM persona_revision")
        assert rows[0]["n"] == 0

    def test_unknown_character_returns_404(self, client):
        r = client.post(
            "/api/pipeline/characters/nope/persona/validate",
            json=_write(base_revision=0),
        )
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/pipeline/characters/{character_id}/persona/rerun
# ---------------------------------------------------------------------------


def _rerun(revision_id: str, *, scope: str = "book", confirm: bool = True,
           scene_ids: list[str] | None = None) -> dict:
    body: dict = {"revision_id": revision_id, "scope": scope, "confirm": confirm}
    if scene_ids is not None:
        body["scene_ids"] = scene_ids
    return body


class TestPostRerun:
    def test_requires_confirm(self, client):
        # Create a persona first.
        client.put("/api/pipeline/characters/c1/persona", json=_write(base_revision=0))
        r = client.post(
            "/api/pipeline/characters/c1/persona/rerun",
            json=_rerun("whatever", confirm=False),
        )
        assert r.status_code == 422

    def test_rerun_creates_new_revision(self, client):
        first = client.put(
            "/api/pipeline/characters/c1/persona", json=_write(base_revision=0)
        ).json()
        r = client.post(
            "/api/pipeline/characters/c1/persona/rerun",
            json=_rerun(first["persona_id"]),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["scope"] == "book"
        assert body["revision_id"] == first["persona_id"]
        assert body["run_id"]

        head = client.get("/api/pipeline/characters/c1/persona").json()
        assert head["revision"] == 2
        assert head["persona_id"] == body["run_id"]

    def test_scoped_rerun(self, client):
        first = client.put(
            "/api/pipeline/characters/c1/persona", json=_write(base_revision=0)
        ).json()
        r = client.post(
            "/api/pipeline/characters/c1/persona/rerun",
            json=_rerun(first["persona_id"], scope="scenes", scene_ids=["sc1"]),
        )
        assert r.status_code == 200
        head = client.get("/api/pipeline/characters/c1/persona").json()
        assert head["scene_scope"] == "scenes"
        assert head["scene_ids"] == ["sc1"]

    def test_dedupe_same_revision_and_scope(self, client):
        first = client.put(
            "/api/pipeline/characters/c1/persona", json=_write(base_revision=0)
        ).json()
        client.post(
            "/api/pipeline/characters/c1/persona/rerun",
            json=_rerun(first["persona_id"]),
        )
        # Same revision + same scope -> 409 already_ran, never a silent dup.
        r = client.post(
            "/api/pipeline/characters/c1/persona/rerun",
            json=_rerun(first["persona_id"]),
        )
        body = _assert_conflict(r, "ALREADY_RAN")
        assert body["detail"]["character_id"] == "c1"
        assert body["detail"]["revision_id"] == first["persona_id"]
        assert body["detail"]["scope"] == "book"
        # The head produced by the earlier rerun is a different persona revision.
        assert body["detail"]["head_persona_id"]
        assert body["detail"]["head_persona_id"] != first["persona_id"]

    def test_rerun_older_revision_after_later_head_is_allowed(self, client):
        first = client.put(
            "/api/pipeline/characters/c1/persona", json=_write(base_revision=0)
        ).json()
        second = client.post(
            "/api/pipeline/characters/c1/persona/rerun",
            json=_rerun(first["persona_id"]),
        ).json()
        third = client.post(
            "/api/pipeline/characters/c1/persona/rerun",
            json=_rerun(second["run_id"]),
        )
        assert third.status_code == 200

        direct_producer = client.post(
            "/api/pipeline/characters/c1/persona/rerun",
            json=_rerun(second["run_id"]),
        )
        assert direct_producer.status_code == 409
        _assert_conflict(direct_producer, "ALREADY_RAN")

        # The first revision did not directly produce the current head, so it
        # is a valid source for another rerun.
        rerun_first = client.post(
            "/api/pipeline/characters/c1/persona/rerun",
            json=_rerun(first["persona_id"]),
        )
        assert rerun_first.status_code == 200
        assert rerun_first.json()["run_id"] not in {
            first["persona_id"],
            second["run_id"],
            third.json()["run_id"],
        }

    def test_rerun_against_protected_head_returns_409(self, client):
        client.put(
            "/api/pipeline/characters/c1/persona",
            json=_write(base_revision=0, protected=True),
        )
        first = client.get("/api/pipeline/characters/c1/persona").json()
        r = client.post(
            "/api/pipeline/characters/c1/persona/rerun",
            json=_rerun(first["persona_id"]),
        )
        body = _assert_conflict(r, "PROTECTED_REVISION")
        assert body["detail"]["character_id"] == "c1"
        assert body["detail"]["head_persona_id"] == first["persona_id"]

    def test_rerun_unknown_revision_returns_404(self, client):
        r = client.post(
            "/api/pipeline/characters/c1/persona/rerun",
            json=_rerun("missing-revision"),
        )
        assert r.status_code == 404

    def test_rerun_unknown_character_returns_404(self, client):
        r = client.post(
            "/api/pipeline/characters/nope/persona/rerun",
            json=_rerun("missing-revision"),
        )
        assert r.status_code == 404

    def test_rerun_bad_scope_returns_422(self, client):
        first = client.put(
            "/api/pipeline/characters/c1/persona", json=_write(base_revision=0)
        ).json()
        r = client.post(
            "/api/pipeline/characters/c1/persona/rerun",
            json=_rerun(first["persona_id"], scope="everywhere"),
        )
        assert r.status_code == 422

    def test_rerun_scenes_scope_requires_reachable_scenes(self, client):
        first = client.put(
            "/api/pipeline/characters/c1/persona", json=_write(base_revision=0)
        ).json()
        r = client.post(
            "/api/pipeline/characters/c1/persona/rerun",
            json=_rerun(first["persona_id"], scope="scenes", scene_ids=["nope"]),
        )
        assert r.status_code == 422

    def test_rerun_never_assigns_voice(self, client, storage):
        client.put(
            "/api/pipeline/characters/c1/persona", json=_write(base_revision=0)
        )
        first = client.get("/api/pipeline/characters/c1/persona").json()
        client.post(
            "/api/pipeline/characters/c1/persona/rerun",
            json=_rerun(first["persona_id"]),
        )
        rows = storage.execute_query(
            "SELECT voice_assignment_id FROM character WHERE id = 'c1'"
        )
        assert rows[0]["voice_assignment_id"] is None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _client_with_retry_handler(storage):
    app = FastAPI()
    app.include_router(pipeline_api.router)
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_persona_domain] = lambda: _pd(storage)

    @app.exception_handler(ConcurrentTransactionError)
    async def _handler(request: Request, exc: ConcurrentTransactionError):
        return JSONResponse(status_code=503, content={"detail": str(exc)},
                            headers={"Retry-After": "5"})

    return TestClient(app)
