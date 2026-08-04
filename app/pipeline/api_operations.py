"""Pipeline API — Operation endpoints.

Provides the HTTP endpoint for structural operations on the document spine:
- POST /api/pipeline/operation — execute split/merge/move/delete operations

The single endpoint dispatches by the ``operation`` field in the request body.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.pipeline.adapter import PipelineStorage
from app.pipeline.api_onboard import get_storage
from app.pipeline.operations import OperationExecutor


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------


class OperationRequest(BaseModel):
    """Request body for POST /api/pipeline/operation."""

    operation: str  # split, merge, move, delete
    book_id: str
    # Operation-specific params:
    presentation_index: Optional[int] = None
    presentation_index_left: Optional[int] = None
    presentation_index_right: Optional[int] = None
    presentation_index_from: Optional[int] = None
    presentation_index_to: Optional[int] = None
    split_point: Optional[int] = None


# ---------------------------------------------------------------------------
# Dependency injection — overridable in tests
# ---------------------------------------------------------------------------


def get_operation_executor(
    storage: PipelineStorage = Depends(get_storage),
) -> OperationExecutor:
    """FastAPI dependency: return an OperationExecutor."""
    return OperationExecutor(storage)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])


# ---------------------------------------------------------------------------
# POST /api/pipeline/operation
# ---------------------------------------------------------------------------


@router.post("/operation")
async def execute_operation(
    request: OperationRequest,
    executor: OperationExecutor = Depends(get_operation_executor),
) -> dict:
    """Execute a structural operation (split/merge/move/delete)."""
    valid_ops = {"split", "merge", "move", "delete"}
    if request.operation not in valid_ops:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown operation: {request.operation}. Must be one of {valid_ops}",
        )

    try:
        if request.operation == "split":
            if request.presentation_index is None or request.split_point is None:
                raise HTTPException(
                    status_code=400,
                    detail="split requires presentation_index and split_point",
                )
            executor.execute_split(
                request.book_id, request.presentation_index, request.split_point
            )

        elif request.operation == "merge":
            if (
                request.presentation_index_left is None
                or request.presentation_index_right is None
            ):
                raise HTTPException(
                    status_code=400,
                    detail="merge requires presentation_index_left and presentation_index_right",
                )
            executor.execute_merge(
                request.book_id,
                request.presentation_index_left,
                request.presentation_index_right,
            )

        elif request.operation == "move":
            if (
                request.presentation_index_from is None
                or request.presentation_index_to is None
            ):
                raise HTTPException(
                    status_code=400,
                    detail="move requires presentation_index_from and presentation_index_to",
                )
            executor.execute_move(
                request.book_id,
                request.presentation_index_from,
                request.presentation_index_to,
            )

        elif request.operation == "delete":
            if request.presentation_index is None:
                raise HTTPException(
                    status_code=400,
                    detail="delete requires presentation_index",
                )
            executor.execute_delete(request.book_id, request.presentation_index)

    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {"status": "ok", "operation": request.operation}
