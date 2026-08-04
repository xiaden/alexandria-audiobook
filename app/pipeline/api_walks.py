"""Pipeline API — Walk endpoints.

Provides HTTP endpoints for running walks and querying walk/character status:
- POST /api/pipeline/run_walk — run a single walk for a book
- POST /api/pipeline/run_all_walks — run all 9 walks serially for a book
- GET /api/pipeline/walk_status/{book_id} — per-walk status for a book
- GET /api/pipeline/characters/{book_id} — character ledger for a book
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.pipeline.adapter import PipelineStorage
from app.pipeline.api_onboard import get_storage
from app.pipeline.ledger import CharacterLedger
from app.pipeline.walks.order import WALK_ORDER
from app.pipeline.walks.runner import WalkRunner


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------


class RunWalkRequest(BaseModel):
    """Request body for POST /api/pipeline/run_walk."""

    walk_name: str
    book_id: str
    config: dict = {}


class RunAllWalksRequest(BaseModel):
    """Request body for POST /api/pipeline/run_all_walks."""

    book_id: str
    config: dict = {}


# ---------------------------------------------------------------------------
# Dependency injection — overridable in tests
# ---------------------------------------------------------------------------

_walk_runner: WalkRunner | None = None


def get_walk_runner(storage: PipelineStorage = Depends(get_storage)) -> WalkRunner:
    """FastAPI dependency: return the WalkRunner singleton."""
    global _walk_runner
    if _walk_runner is None:
        _walk_runner = WalkRunner(storage)
    return _walk_runner


def get_character_ledger(
    storage: PipelineStorage = Depends(get_storage),
) -> CharacterLedger:
    """FastAPI dependency: return a CharacterLedger."""
    return CharacterLedger(storage)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])


# ---------------------------------------------------------------------------
# POST /api/pipeline/run_walk
# ---------------------------------------------------------------------------


@router.post("/run_walk")
async def run_walk(
    request: RunWalkRequest,
    runner: WalkRunner = Depends(get_walk_runner),
) -> dict:
    """Run a single walk for a book."""
    if request.walk_name not in WALK_ORDER:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown walk: {request.walk_name}. "
            f"Must be one of {WALK_ORDER}",
        )
    result = runner.run_walk(request.walk_name, request.book_id, request.config)
    return result


# ---------------------------------------------------------------------------
# POST /api/pipeline/run_all_walks
# ---------------------------------------------------------------------------


@router.post("/run_all_walks")
async def run_all_walks(
    request: RunAllWalksRequest,
    runner: WalkRunner = Depends(get_walk_runner),
) -> dict:
    """Run all 9 walks serially for a book."""
    results = runner.run_all_walks(request.book_id, request.config)
    return results


# ---------------------------------------------------------------------------
# GET /api/pipeline/walk_status/{book_id}
# ---------------------------------------------------------------------------


@router.get("/walk_status/{book_id}")
async def get_walk_status(
    book_id: str,
    runner: WalkRunner = Depends(get_walk_runner),
) -> dict:
    """Return per-walk status for a book."""
    statuses: dict[str, str] = {}
    for walk_name in WALK_ORDER:
        statuses[walk_name] = runner.get_walk_status(book_id, walk_name)
    return statuses


# ---------------------------------------------------------------------------
# GET /api/pipeline/characters/{book_id}
# ---------------------------------------------------------------------------


@router.get("/characters/{book_id}")
async def get_characters(
    book_id: str,
    ledger: CharacterLedger = Depends(get_character_ledger),
) -> list[dict]:
    """Return the character ledger for a book."""
    characters = ledger.get_characters_for_book(book_id)
    return characters
