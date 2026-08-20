"""Pipeline API — Character endpoints.

Provides HTTP endpoints for managing character voice assignments and personas:
- PUT  /api/pipeline/characters/{id}/voice — set or clear voice assignment
- GET|PUT /api/pipeline/characters/{id}/persona — read / append persona revision
- GET  /api/pipeline/characters/{id}/persona/revisions — revision history
- POST /api/pipeline/characters/{id}/persona/validate — side-effect free check
- POST /api/pipeline/characters/{id}/persona/rerun — explicit scoped rerun

Uses dependency injection for storage so tests can inject InMemorySQLiteAdapter.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.pipeline.adapter import PipelineStorage
from app.pipeline.api_onboard import get_storage
from app.pipeline.api_review import _guard, get_workbench
from app.pipeline.persona import (
    BookNotFoundError,
    CharacterNotFoundError,
    PersonaDomain,
    PersonaError,
    ProtectedRevisionError,
    StaleRevisionError,
    ValidationError,
)
from app.pipeline.revision_conflict import (
    CODE_ALREADY_RAN,
    CODE_PROTECTED,
    CODE_STALE,
    revision_conflict_http,
)
from app.pipeline.workbench import Workbench


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------


class CharacterVoiceUpdateRequest(BaseModel):
    """Request body for PUT /api/pipeline/characters/{id}/voice.

    ``voice_assignment_id`` may be ``null`` to clear the assignment.
    """

    voice_assignment_id: Optional[str] = None


class CharacterPresenceRequest(BaseModel):
    """Request body for PUT /api/pipeline/workbench/{book_id}/presence.

    ``relation_type`` is one of ``present`` | ``speaker`` | ``absent``; an
    ``absent`` write creates a tombstone so walk 2d cannot re-add the
    character to that scene.  ``decision_id`` is an optional client reference
    (the domain records the authoritative decision).
    """

    scene_id: str
    character_id: str
    relation_type: str
    decision_id: Optional[str] = None
    base_revision: int


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])


class PersonaWriteRequest(BaseModel):
    """Request body for PUT .../persona and POST .../persona/validate.

    ``base_revision`` is required for the PUT write (optimistic concurrency);
    the validate endpoint ignores it and never persists.
    """

    base_revision: int
    book_id: Optional[str] = None
    fields: dict[str, str] = {}
    evidence: list[dict] = []
    aliases: list[str] = []
    scene_scope: str = "book"
    scene_ids: list[str] = []
    review_state: str = "draft"
    protected: bool = False


class PersonaRerunRequest(BaseModel):
    """Request body for POST .../persona/rerun.

    A rerun is explicit and never implicit: it requires ``confirm=True`` plus a
    ``revision_id`` (an existing persona revision to re-apply) and a ``scope``.
    """

    revision_id: str
    scope: str = "book"
    scene_ids: list[str] = []
    confirm: bool = False


def _persona_http(exc: PersonaError) -> HTTPException:
    """Map a persona domain error to the contracted HTTP status.

    The 409 conflict branches (stale ``base_revision`` and protected-current-
    revision) return the structured ``RevisionConflictDTO`` body (P6 amendment).
    """
    if isinstance(exc, (CharacterNotFoundError, BookNotFoundError)):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, StaleRevisionError):
        return revision_conflict_http(
            code=CODE_STALE,
            message=str(exc),
        )
    if isinstance(exc, ProtectedRevisionError):
        return revision_conflict_http(
            code=CODE_PROTECTED,
            message=str(exc),
        )
    if isinstance(exc, ValidationError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


def _persona_guard(fn, *args, **kwargs):
    """Invoke a persona domain method, translating PersonaError to HTTP."""
    try:
        return fn(*args, **kwargs)
    except PersonaError as exc:
        raise _persona_http(exc) from exc


def get_persona_domain(
    storage: PipelineStorage = Depends(get_storage),
) -> PersonaDomain:
    """Dependency that builds a persona domain over the shared storage."""
    return PersonaDomain(storage)


# ---------------------------------------------------------------------------
# PUT /api/pipeline/characters/{id}/voice
# ---------------------------------------------------------------------------


@router.put("/characters/{character_id}/voice")
async def update_character_voice(
    character_id: str,
    request: CharacterVoiceUpdateRequest,
    storage: PipelineStorage = Depends(get_storage),
) -> dict:
    """Set or clear a character's voice assignment.

    Parameters
    ----------
    character_id : str
        The character's primary key.
    request : CharacterVoiceUpdateRequest
        JSON body with ``voice_assignment_id`` (may be ``null`` to clear).

    Returns
    -------
    dict
        The updated character row (id, name, aliases, voice_assignment_id,
        description).

    Raises
    ------
    HTTPException 404
        If the character does not exist.
    HTTPException 400
        If ``voice_assignment_id`` references a non-existent voice config.
    """
    # Verify character exists
    characters = storage.execute_query(
        "SELECT id, name, aliases, voice_assignment_id, description "
        "FROM character WHERE id = ?",
        (character_id,),
    )
    if not characters:
        raise HTTPException(
            status_code=404,
            detail=f"Character '{character_id}' not found",
        )

    # If a voice_assignment_id is provided, verify it exists in voice_config
    if request.voice_assignment_id is not None:
        voices = storage.execute_query(
            "SELECT id FROM voice_config WHERE id = ?",
            (request.voice_assignment_id,),
        )
        if not voices:
            raise HTTPException(
                status_code=400,
                detail=f"Voice config '{request.voice_assignment_id}' not found",
            )

    # Update the character's voice_assignment_id
    storage.execute_update(
        "UPDATE character SET voice_assignment_id = ? WHERE id = ?",
        (request.voice_assignment_id, character_id),
    )

    # Return the updated character
    updated = storage.execute_query(
        "SELECT id, name, aliases, voice_assignment_id, description "
        "FROM character WHERE id = ?",
        (character_id,),
    )
    return updated[0]


# ---------------------------------------------------------------------------
# GET|PUT /api/pipeline/characters/{id}/persona
# ---------------------------------------------------------------------------


def _persona_dto(domain: PersonaDomain, character_id: str) -> dict:
    """Return the current persona head for *character_id* as a DTO."""
    revisions = domain.list_revisions(character_id)
    if not revisions:
        raise HTTPException(
            status_code=404,
            detail=f"No persona revision for character '{character_id}'",
        )
    return revisions[0]


def _write_dict(request: PersonaWriteRequest, character_id: str) -> dict:
    """Normalize a PersonaWriteRequest into the persona domain write shape."""
    return {
        "character_id": character_id,
        "book_id": request.book_id,
        "fields": request.fields,
        "evidence": request.evidence,
        "aliases": request.aliases,
        "scene_scope": request.scene_scope,
        "scene_ids": request.scene_ids,
        "review_state": request.review_state,
        "protected": request.protected,
    }


@router.get("/characters/{character_id}/persona")
async def get_persona(
    character_id: str,
    domain: PersonaDomain = Depends(get_persona_domain),
) -> dict:
    """Return the current persona revision for *character_id* as a PersonaDTO.

    Returns ``404`` for an unknown character or when no persona exists yet.
    """
    _persona_guard(domain.require_character, character_id)
    return _persona_dto(domain, character_id)


@router.put("/characters/{character_id}/persona")
async def put_persona(
    character_id: str,
    request: PersonaWriteRequest,
    domain: PersonaDomain = Depends(get_persona_domain),
) -> dict:
    """Append a new persona revision for *character_id* (append-only).

    Requires ``base_revision`` to equal the current head revision (``409`` on
    staleness).  Persona writes never assign a resolved voice; ``validate``
    is not invoked implicitly — the write itself is validated deterministically
    (``422`` on failure).  Contention maps to ``503`` + ``Retry-After`` at the
    app layer.
    """
    _persona_guard(domain.require_character, character_id)
    return _persona_guard(
        domain.save,
        _write_dict(request, character_id),
        base_revision=request.base_revision,
        source="human",
    )


@router.get("/characters/{character_id}/persona/revisions")
async def get_persona_revisions(
    character_id: str,
    domain: PersonaDomain = Depends(get_persona_domain),
) -> dict:
    """Return the full revision history for *character_id*, newest first."""
    _persona_guard(domain.require_character, character_id)
    return {
        "character_id": character_id,
        "revisions": domain.list_revisions(character_id),
    }


@router.post("/characters/{character_id}/persona/validate")
async def validate_persona(
    character_id: str,
    request: PersonaWriteRequest,
    domain: PersonaDomain = Depends(get_persona_domain),
) -> dict:
    """Side-effect-free validation of a persona write.

    Returns ``{valid, errors, voice_consequences}`` and never persists a
    revision.  ``base_revision`` is ignored for validation.
    """
    _persona_guard(domain.require_character, character_id)
    return domain.validate(_write_dict(request, character_id))


@router.post("/characters/{character_id}/persona/rerun")
async def rerun_persona(
    character_id: str,
    request: PersonaRerunRequest,
    domain: PersonaDomain = Depends(get_persona_domain),
) -> dict:
    """Explicit, confirmed, scoped persona rerun.

    Re-applies the content of the referenced ``revision_id`` at the requested
    ``scope`` as a new append-only revision.  Requires ``confirm=True`` (422
    otherwise), a valid ``scope``, and (for ``scenes`` scope) reachable
    ``scene_ids``.  A rerun of the same revision at the same scope that would
    reproduce the current head is rejected ``409 already_ran``.  Reruns never
    replace a protected head (``409``) and never assign a resolved voice.
    """
    _persona_guard(domain.require_character, character_id)
    if not request.confirm:
        raise HTTPException(
            status_code=422,
            detail="persona rerun requires confirm=true",
        )
    if request.scope not in ("book", "scenes"):
        raise HTTPException(
            status_code=422,
            detail="scope must be exactly 'book' or 'scenes'",
        )
    # The rerun derives from an existing revision owned by this character.
    base = domain.get_revision(request.revision_id)
    if base is None or base["character_id"] != character_id:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown persona revision '{request.revision_id}'",
        )
    if base["book_id"]:
        _persona_guard(domain.require_book, base["book_id"])

    if request.scope == "scenes":
        if not request.scene_ids:
            raise HTTPException(
                status_code=422,
                detail="scenes scope requires a non-empty scene_ids",
            )
        if base["book_id"]:
            reachable = domain.get_reachable_scene_ids(base["book_id"])
            missing = [s for s in request.scene_ids if s not in reachable]
            if missing:
                raise HTTPException(
                    status_code=422,
                    detail=f"scene_ids not reachable from book: {missing}",
                )

    write = {
        "character_id": character_id,
        "book_id": base["book_id"],
        "fields": base["fields"],
        "evidence": base["evidence"],
        "aliases": base["aliases"],
        "scene_scope": request.scope,
        "scene_ids": request.scene_ids,
        "review_state": base["review_state"],
        "protected": False,
    }

    head = domain.list_revisions(character_id)
    if head and head[0]["protected"]:
        raise revision_conflict_http(
            code=CODE_PROTECTED,
            message=(
                f"protected persona revision '{head[0]['persona_id']}' cannot"
                " be replaced by a rerun"
            ),
            detail={
                "character_id": character_id,
                "head_persona_id": head[0]["persona_id"],
            },
        )
    # Dedupe only when this revision directly produced the current head.  An
    # older revision may still be rerun after later revisions have advanced the
    # chain; rejecting every non-head revision incorrectly blocks that case.
    if head and base["superseded_by"] == head[0]["persona_id"]:
        raise revision_conflict_http(
            code=CODE_ALREADY_RAN,
            message=(
                f"persona rerun already_ran: revision {request.revision_id} "
                f"scope {request.scope} produced head '{head[0]['persona_id']}'"
            ),
            detail={
                "character_id": character_id,
                "revision_id": request.revision_id,
                "scope": request.scope,
                "head_persona_id": head[0]["persona_id"],
            },
        )

    head_revision = head[0]["revision"] if head else 0
    saved = _persona_guard(
        domain.save,
        write,
        base_revision=head_revision,
        source="rerun",
    )
    return {
        "run_id": saved["persona_id"],
        "revision_id": request.revision_id,
        "scope": request.scope,
    }


# ---------------------------------------------------------------------------
# PUT /api/pipeline/workbench/{book_id}/presence
# ---------------------------------------------------------------------------


@router.put("/workbench/{book_id}/presence")
async def set_character_presence(
    book_id: str,
    request: CharacterPresenceRequest,
    workbench: Workbench = Depends(get_workbench),
) -> dict:
    """Set a character's presence in a scene for *book_id*.

    Records a human presence decision and returns the contracted
    ActionResultDTO plus ``scene_id`` / ``character_id`` / ``relation_type``.
    An ``absent`` write creates a tombstone so walk 2d cannot re-add the
    character.  Revision-checked; contention maps to 503 + Retry-After.
    """
    result = _guard(
        workbench.set_presence,
        book_id=book_id,
        scene_id=request.scene_id,
        character_id=request.character_id,
        relation_type=request.relation_type,
        base_revision=request.base_revision,
    )
    return {
        "item_id": f"presence:{request.scene_id}:{request.character_id}",
        "decision_id": result["decision_id"],
        "status": "active",
        "generation_revision": result["generation_revision"],
        "superseded_item_ids": [],
        "conflict": None,
        "scene_id": result["scene_id"],
        "character_id": result["character_id"],
        "relation_type": result["relation_type"],
    }
