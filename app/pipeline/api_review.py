"""Pipeline API — Review endpoints.

Provides HTTP endpoints for the unified review workflow:
- GET /api/pipeline/review/{book_id} — get review items (junction items
  with confidence 0.5-0.7, plus ``walkitem:``-prefixed walk items, which
  carry no confidence)
- POST /api/pipeline/review/accept — accept a review item
- POST /api/pipeline/review/reject — reject a review item
- POST /api/pipeline/review/override — override a review item
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.pipeline.adapter import PipelineStorage
from app.pipeline.api_onboard import get_storage
from app.pipeline.review import ReviewItemNotFoundError, ReviewManager
from app.pipeline.workbench import (
    BookNotFoundError,
    ConflictError,
    PreviewExpiredError,
    StaleRevisionError,
    ValidationError as WorkbenchValidationError,
    Workbench,
    WorkbenchError,
)


# ---------------------------------------------------------------------------
# Workbench error mapping + dependency (shared by api_walks / api_characters)
# ---------------------------------------------------------------------------


def _wb_http(exc: WorkbenchError) -> HTTPException:
    """Map a Workbench domain error to its contracted HTTP status.

    404 unknown/cross-book, 422 validation (incl. expired preview tokens),
    409 stale-revision/conflict.  ``ConcurrentTransactionError`` is NOT mapped
    here — it propagates to the app-level 503 + Retry-After handler.
    """
    if isinstance(exc, BookNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (StaleRevisionError, ConflictError)):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (WorkbenchValidationError, PreviewExpiredError)):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


def _guard(fn, *args, **kwargs):
    """Invoke a Workbench method, translating domain errors to HTTPException."""
    try:
        return fn(*args, **kwargs)
    except WorkbenchError as exc:  # noqa: BLE001 - domain boundary translate
        raise _wb_http(exc) from exc


_workbench: Workbench | None = None


def get_workbench(storage: PipelineStorage = Depends(get_storage)) -> Workbench:
    """FastAPI dependency: return the Workbench singleton.

    The singleton is required so in-memory alias-preview tokens persist between
    the ``preview`` and ``commit`` requests.  Tests override this dependency
    with a fresh ``Workbench(storage)`` per test.
    """
    global _workbench
    if _workbench is None:
        _workbench = Workbench(storage)
    return _workbench


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------


class ReviewActionRequest(BaseModel):
    """Request body for POST /api/pipeline/review/accept|reject|override.

    ``base_revision`` is optional and only checked for workbench dispatch
    targets (``decision:`` / ``junction:`` prefixes).  Legacy junction and
    ``walkitem:`` ids keep their existing behavior.
    """

    item_id: str
    new_value: Optional[Any] = None  # Only used for override
    base_revision: Optional[int] = None


class ReviewUndoRequest(BaseModel):
    """Request body for POST /workbench/{book_id}/decisions/{id}/undo."""

    base_revision: int


# ---------------------------------------------------------------------------
# Dependency injection — overridable in tests
# ---------------------------------------------------------------------------


def get_review_manager(storage: PipelineStorage = Depends(get_storage)) -> ReviewManager:
    """FastAPI dependency: return a ReviewManager."""
    return ReviewManager(storage)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])


# ---------------------------------------------------------------------------
# GET /api/pipeline/review/{book_id}
# ---------------------------------------------------------------------------


@router.get("/review/{book_id}")
async def get_review_items(
    book_id: str,
    manager: ReviewManager = Depends(get_review_manager),
) -> list[dict]:
    """Return review items for a book — junction items (confidence 0.5-0.7)
    plus ``walkitem:``-prefixed walk items, which carry no confidence."""
    items = manager.get_review_items(book_id)
    return items


# ---------------------------------------------------------------------------
# Workbench review-action dispatch helpers
# ---------------------------------------------------------------------------


_ACTION_STATUS = {
    "accept": "accepted",
    "reject": "rejected",
    "override": "overridden",
}


#: Allow-listed junction tables for the ``junction:`` dispatch form.
_ALLOWED_JUNCTION_TABLES = frozenset(
    {"character_book", "character_scene", "character_span", "character_series"}
)


def _book_id_for_junction(
    storage: PipelineStorage, table: str, entity_id: str
) -> str | None:
    """Resolve the owning book for a junction target, or ``None``."""
    if table == "character_book":
        return entity_id
    if table == "character_scene":
        rows = storage.execute_query(
            "SELECT bc.parent_id AS book_id FROM chapter_scene cs "
            "JOIN book_chapter bc ON bc.child_id = cs.parent_id WHERE cs.child_id = ?",
            (entity_id,),
        )
    elif table == "character_span":
        rows = storage.execute_query(
            "SELECT bc.parent_id AS book_id FROM paragraph_span ps "
            "JOIN scene_paragraph sp ON sp.child_id = ps.parent_id "
            "JOIN chapter_scene cs ON cs.child_id = sp.parent_id "
            "JOIN book_chapter bc ON bc.child_id = cs.parent_id WHERE ps.child_id = ?",
            (entity_id,),
        )
    else:
        return None
    if not rows:
        return None
    return rows[0].get("book_id")


def _result_action_dto(
    *,
    item_id: str,
    decision_id: str | None,
    status: str,
    generation_revision: int,
    superseded_item_ids: list[str] | None = None,
    conflict: Any = None,
    **extra: Any,
) -> dict:
    """Build the contracted ActionResultDTO, merging any extra keys."""
    dto: dict = {
        "item_id": item_id,
        "decision_id": decision_id,
        "status": status,
        "generation_revision": generation_revision,
        "superseded_item_ids": superseded_item_ids or [],
        "conflict": conflict,
    }
    dto.update(extra)
    return dto


def _resolve_decision_action(
    action: str,
    decision_id: str,
    new_value: Any,
    base_revision: int | None,
    workbench: Workbench,
    storage: PipelineStorage,
) -> dict:
    """Resolve a review action on a ``decision:{uuid}`` workbench target.

    The referenced decision must exist (else 404) and still be ``active``
    (else 409).  A superseding human ``review`` decision is recorded in one
    transaction and the referenced decision is marked superseded.
    """
    rows = storage.execute_query(
        "SELECT * FROM workbench_decision WHERE decision_id = ?", (decision_id,)
    )
    if not rows:
        raise HTTPException(status_code=404, detail=f"Unknown decision: {decision_id}")
    decision = rows[0]
    if decision["status"] != "active":
        raise HTTPException(
            status_code=409,
            detail=f"Decision '{decision_id}' is already {decision['status']}",
        )
    book_id = decision["book_id"]
    _guard(workbench.require_book, book_id)
    if base_revision is not None:
        _guard(workbench.check_revision, book_id, base_revision)
    with storage.transaction():
        revision = _guard(workbench.allocate_revision, book_id)
        new_decision_id = _guard(
            workbench.record_decision,
            book_id=book_id,
            target_kind="review",
            target_key=decision["target_key"],
            decision_type=f"review:{action}",
            base_revision=decision["base_revision"],
            payload={"action": action, "resolves": decision_id},
            supersedes_id=decision_id,
        )
        storage.execute_update(
            "UPDATE workbench_decision SET status = 'superseded' "
            "WHERE decision_id = ? AND book_id = ?",
            (decision_id, book_id),
        )
    return _result_action_dto(
        item_id=f"decision:{decision_id}",
        decision_id=new_decision_id,
        status=_ACTION_STATUS[action],
        generation_revision=revision,
        superseded_item_ids=[decision_id],
    )


def _resolve_junction_action(
    action: str,
    item_id: str,
    new_value: Any,
    base_revision: int | None,
    manager: ReviewManager,
    workbench: Workbench,
    storage: PipelineStorage,
) -> dict:
    """Resolve a review action on a ``junction:{table}:{char}:{entity}`` target.

    The ``junction:`` prefix is stripped and the allow-listed live junction is
    resolved by the existing ReviewManager (authority), then a human review
    decision is recorded in one transaction.
    """
    stripped = item_id[len("junction:"):]
    parts = stripped.split(":")
    if len(parts) != 3 or parts[0] not in _ALLOWED_JUNCTION_TABLES:
        raise HTTPException(
            status_code=400,
            detail=f"Malformed junction target: {item_id!r}",
        )
    table, character_id, entity_id = parts
    book_id = _book_id_for_junction(storage, table, entity_id)
    if book_id is None:
        raise HTTPException(
            status_code=404,
            detail=f"Junction target '{item_id}' does not resolve to a book",
        )
    _guard(workbench.require_book, book_id)
    if base_revision is not None:
        _guard(workbench.check_revision, book_id, base_revision)
    try:
        manager.resolve_review_action(action, stripped, new_value)
    except ReviewItemNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with storage.transaction():
        revision = _guard(workbench.allocate_revision, book_id)
        new_decision_id = _guard(
            workbench.record_decision,
            book_id=book_id,
            target_kind="review",
            target_key=f"junction:{stripped}",
            decision_type=f"review:{action}",
            base_revision=base_revision if base_revision is not None else revision - 1,
            payload={"action": action, "new_value": new_value},
        )
    return _result_action_dto(
        item_id=item_id,
        decision_id=new_decision_id,
        status=_ACTION_STATUS[action],
        generation_revision=revision,
    )


# ---------------------------------------------------------------------------
# POST /api/pipeline/review/accept
# ---------------------------------------------------------------------------


@router.post("/review/accept")
async def accept_review_item(
    request: ReviewActionRequest,
    manager: ReviewManager = Depends(get_review_manager),
    workbench: Workbench = Depends(get_workbench),
    storage: PipelineStorage = Depends(get_storage),
) -> dict:
    """Accept a review item — dispatch by id prefix (decision | junction |
    walkitem | bare junction)."""
    if request.item_id.startswith("decision:"):
        return _resolve_decision_action(
            "accept", request.item_id[len("decision:"):], request.new_value,
            request.base_revision,
            workbench, storage,
        )
    if request.item_id.startswith("junction:"):
        return _resolve_junction_action(
            "accept", request.item_id, request.new_value, request.base_revision,
            manager, workbench, storage,
        )
    try:
        manager.resolve_review_action("accept", request.item_id)
    except ReviewItemNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "accepted", "item_id": request.item_id}


# ---------------------------------------------------------------------------
# POST /api/pipeline/review/reject
# ---------------------------------------------------------------------------


@router.post("/review/reject")
async def reject_review_item(
    request: ReviewActionRequest,
    manager: ReviewManager = Depends(get_review_manager),
    workbench: Workbench = Depends(get_workbench),
    storage: PipelineStorage = Depends(get_storage),
) -> dict:
    """Reject a review item — dispatch by id prefix (decision | junction |
    walkitem | bare junction)."""
    if request.item_id.startswith("decision:"):
        return _resolve_decision_action(
            "reject", request.item_id[len("decision:"):], request.new_value,
            request.base_revision,
            workbench, storage,
        )
    if request.item_id.startswith("junction:"):
        return _resolve_junction_action(
            "reject", request.item_id, request.new_value, request.base_revision,
            manager, workbench, storage,
        )
    try:
        manager.resolve_review_action("reject", request.item_id)
    except ReviewItemNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "rejected", "item_id": request.item_id}


# ---------------------------------------------------------------------------
# POST /api/pipeline/review/override
# ---------------------------------------------------------------------------


@router.post("/review/override")
async def override_review_item(
    request: ReviewActionRequest,
    manager: ReviewManager = Depends(get_review_manager),
    workbench: Workbench = Depends(get_workbench),
    storage: PipelineStorage = Depends(get_storage),
) -> dict:
    """Override a review item — dispatch by id prefix (decision | junction |
    walkitem | bare junction)."""
    if request.item_id.startswith("decision:"):
        return _resolve_decision_action(
            "override", request.item_id[len("decision:"):], request.new_value,
            request.base_revision,
            workbench, storage,
        )
    if request.item_id.startswith("junction:"):
        return _resolve_junction_action(
            "override", request.item_id, request.new_value, request.base_revision,
            manager, workbench, storage,
        )
    try:
        manager.resolve_review_action("override", request.item_id, request.new_value)
    except ReviewItemNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "overridden", "item_id": request.item_id}


# ---------------------------------------------------------------------------
# POST /api/pipeline/workbench/{book_id}/decisions/{decision_id}/undo
# ---------------------------------------------------------------------------


@router.post("/workbench/{book_id}/decisions/{decision_id}/undo")
async def undo_decision(
    book_id: str,
    decision_id: str,
    request: ReviewUndoRequest,
    workbench: Workbench = Depends(get_workbench),
    storage: PipelineStorage = Depends(get_storage),
) -> dict:
    """Revision-checked reversible undo of a workbench decision.

    Creates an inverse decision in one transaction and marks the referenced
    decision ``undone``.  Returns 409 when the base revision is stale (newer
    state exists) or the decision is already terminal; 404 for an unknown or
    cross-book decision.  Alias-merge decisions delegate to the domain's
    reversible ``unmerge_alias``.
    """
    _guard(workbench.require_book, book_id)
    _guard(workbench.check_revision, book_id, request.base_revision)
    rows = storage.execute_query(
        "SELECT * FROM workbench_decision WHERE decision_id = ? AND book_id = ?",
        (decision_id, book_id),
    )
    if not rows:
        raise HTTPException(status_code=404, detail=f"Unknown decision: {decision_id}")
    decision = rows[0]
    if decision["status"] != "active":
        raise HTTPException(
            status_code=409,
            detail=f"Decision '{decision_id}' is already {decision['status']}",
        )
    # Alias merges are reversibly undone by the domain (restores projection and
    # prior voice assignments; reactivates review items).
    if decision["decision_type"] == "alias_merge:merge":
        merges = storage.execute_query(
            "SELECT merge_id FROM character_alias_merge "
            "WHERE decision_id = ? AND book_id = ? AND status = 'active'",
            (decision_id, book_id),
        )
        if not merges:
            raise HTTPException(
                status_code=409,
                detail=f"Active merge for decision '{decision_id}' not found",
            )
        result = _guard(
            workbench.unmerge_alias,
            book_id=book_id,
            merge_id=merges[0]["merge_id"],
            base_revision=request.base_revision,
        )
        return _result_action_dto(
            item_id=f"decision:{decision_id}",
            decision_id=result["decision_id"],
            status=result["status"],
            generation_revision=result["generation_revision"],
            conflict=result.get("conflict"),
        )
    with storage.transaction():
        revision = _guard(workbench.allocate_revision, book_id)
        inverse_id = _guard(
            workbench.record_decision,
            book_id=book_id,
            target_kind=decision["target_kind"],
            target_key=decision["target_key"],
            decision_type=f"undo:{decision['decision_type']}",
            base_revision=request.base_revision,
            payload={"undo_of": decision_id},
            supersedes_id=decision_id,
        )
        storage.execute_update(
            "UPDATE workbench_decision SET status = 'undone', undone_by = ? "
            "WHERE decision_id = ? AND book_id = ?",
            (inverse_id, decision_id, book_id),
        )
    return _result_action_dto(
        item_id=f"decision:{decision_id}",
        decision_id=inverse_id,
        status="undone",
        generation_revision=revision,
    )
