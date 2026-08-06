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


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------


class ReviewActionRequest(BaseModel):
    """Request body for POST /api/pipeline/review/accept|reject|override."""

    item_id: str
    new_value: Optional[Any] = None  # Only used for override


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
# POST /api/pipeline/review/accept
# ---------------------------------------------------------------------------


@router.post("/review/accept")
async def accept_review_item(
    request: ReviewActionRequest,
    manager: ReviewManager = Depends(get_review_manager),
) -> dict:
    """Accept a review item — dispatch by id prefix (junction | walkitem)."""
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
) -> dict:
    """Reject a review item — dispatch by id prefix (junction | walkitem)."""
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
) -> dict:
    """Override a review item — dispatch by id prefix (junction | walkitem)."""
    try:
        manager.resolve_review_action("override", request.item_id, request.new_value)
    except ReviewItemNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "overridden", "item_id": request.item_id}
