"""Pipeline API — FastAPI router for /api/pipeline/* endpoints.

Provides HTTP endpoints for the audiobook pipeline: onboard EPUBs, run walks,
query characters, review low-confidence items, perform structural operations,
export annotated scripts, render audiobooks, and re-onboard books.

Uses dependency injection for storage so tests can inject InMemorySQLiteAdapter.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from app.pipeline.adapter import PipelineStorage, SQLiteAdapter
from app.pipeline.assembly import export_annotated_script, reonboard_book
from app.pipeline.extract import extract_epub_text
from app.pipeline.ledger import CharacterLedger
from app.pipeline.operations import OperationExecutor
from app.pipeline.populate import populate_spine
from app.pipeline.review import ReviewManager
from app.pipeline.tts_integration import render_audiobook
from app.pipeline.walks.runner import WalkRunner


# ---------------------------------------------------------------------------
# Pydantic request/response models
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


class ReviewActionRequest(BaseModel):
    """Request body for POST /api/pipeline/review/accept|reject|override."""

    item_id: str
    new_value: Optional[Any] = None  # Only used for override


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


class RenderRequest(BaseModel):
    """Request body for POST /api/pipeline/render."""

    book_id: str
    use_batch: bool = True
    output_dir: Optional[str] = None
    batch_seed: Optional[int] = None


class ReonboardRequest(BaseModel):
    """Request body for POST /api/pipeline/reonboard."""

    book_id: str


# ---------------------------------------------------------------------------
# Dependency injection — overridable in tests
# ---------------------------------------------------------------------------

# Module-level singletons for production use.
_storage: PipelineStorage | None = None
_walk_runner: WalkRunner | None = None


def _get_production_storage() -> PipelineStorage:
    """Lazily create and return the production SQLiteAdapter singleton."""
    global _storage
    if _storage is None:
        db_path = os.environ.get("PIPELINE_DB_PATH", "./data/pipeline.db")
        _storage = SQLiteAdapter(db_path)
        _storage.init_db()
    return _storage


def get_storage() -> PipelineStorage:
    """FastAPI dependency: return the pipeline storage adapter."""
    return _get_production_storage()


def get_walk_runner(storage: PipelineStorage = Depends(get_storage)) -> WalkRunner:
    """FastAPI dependency: return the WalkRunner singleton."""
    global _walk_runner
    if _walk_runner is None:
        _walk_runner = WalkRunner(storage)
    return _walk_runner


def get_review_manager(storage: PipelineStorage = Depends(get_storage)) -> ReviewManager:
    """FastAPI dependency: return a ReviewManager."""
    return ReviewManager(storage)


def get_operation_executor(
    storage: PipelineStorage = Depends(get_storage),
) -> OperationExecutor:
    """FastAPI dependency: return an OperationExecutor."""
    return OperationExecutor(storage)


def get_character_ledger(
    storage: PipelineStorage = Depends(get_storage),
) -> CharacterLedger:
    """FastAPI dependency: return a CharacterLedger."""
    return CharacterLedger(storage)


def get_tts_engine() -> object | None:
    """FastAPI dependency: return the TTS engine (or None).

    Lazily imports ``app.app.project_manager`` at call time to avoid
    circular imports that would occur at module level (``app.app`` imports
    ``app.pipeline.api`` during its own module initialisation).

    Tests override this dependency via FastAPI ``dependency_overrides``,
    so the lazy import is never reached in test scenarios.
    """
    from app.app import project_manager  # lazy import — breaks circular dependency

    return project_manager.get_engine()


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])


# ---------------------------------------------------------------------------
# P1-S2: POST /api/pipeline/onboard
# ---------------------------------------------------------------------------


@router.post("/onboard")
async def onboard_epub(
    file: UploadFile = File(...),
    storage: PipelineStorage = Depends(get_storage),
) -> dict:
    """Accept an EPUB file, extract text, populate the spine, return book_id.

    The uploaded file is saved to a temporary location, then processed through
    extract_epub_text and populate_spine.
    """
    if not file.filename or not file.filename.lower().endswith(".epub"):
        raise HTTPException(status_code=400, detail="File must be an EPUB (.epub)")

    # Save uploaded file to temp location
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_onboard_")
    tmp_path = os.path.join(tmp_dir, file.filename)
    try:
        content = await file.read()
        with open(tmp_path, "wb") as f:
            f.write(content)

        # Generate a book_id
        book_id = str(uuid.uuid4())

        # Extract EPUB text
        try:
            result = extract_epub_text(tmp_path, book_id, storage)
        except Exception as exc:
            raise HTTPException(
                status_code=400, detail=f"Failed to extract EPUB: {exc}"
            )

        # Populate spine
        try:
            populate_spine(
                result["series_id"],
                result["book_id"],
                result["chapters"],
                storage,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"Failed to populate spine: {exc}"
            )

        return {
            "book_id": book_id,
            "series_id": result["series_id"],
            "chapters": len(result["chapters"]),
        }
    finally:
        # Clean up temp file
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        if os.path.exists(tmp_dir):
            try:
                os.rmdir(tmp_dir)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# P1-S3: POST /api/pipeline/run_walk
# ---------------------------------------------------------------------------


@router.post("/run_walk")
async def run_walk(
    request: RunWalkRequest,
    runner: WalkRunner = Depends(get_walk_runner),
) -> dict:
    """Run a single walk for a book."""
    if request.walk_name not in WalkRunner.WALK_ORDER:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown walk: {request.walk_name}. "
            f"Must be one of {WalkRunner.WALK_ORDER}",
        )
    result = runner.run_walk(request.walk_name, request.book_id, request.config)
    return result


# ---------------------------------------------------------------------------
# P1-S4: POST /api/pipeline/run_all_walks
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
# P1-S5: GET /api/pipeline/walk_status/{book_id}
# ---------------------------------------------------------------------------


@router.get("/walk_status/{book_id}")
async def get_walk_status(
    book_id: str,
    runner: WalkRunner = Depends(get_walk_runner),
) -> dict:
    """Return per-walk status for a book."""
    statuses: dict[str, str] = {}
    for walk_name in WalkRunner.WALK_ORDER:
        statuses[walk_name] = runner.get_walk_status(book_id, walk_name)
    return statuses


# ---------------------------------------------------------------------------
# P1-S6: GET /api/pipeline/characters/{book_id}
# ---------------------------------------------------------------------------


@router.get("/characters/{book_id}")
async def get_characters(
    book_id: str,
    ledger: CharacterLedger = Depends(get_character_ledger),
) -> list[dict]:
    """Return the character ledger for a book."""
    characters = ledger.get_characters_for_book(book_id)
    return characters


# ---------------------------------------------------------------------------
# P1-S7: GET /api/pipeline/review/{book_id}
# ---------------------------------------------------------------------------


@router.get("/review/{book_id}")
async def get_review_items(
    book_id: str,
    manager: ReviewManager = Depends(get_review_manager),
) -> list[dict]:
    """Return review items (confidence 0.5-0.7) for a book."""
    items = manager.get_review_items(book_id)
    return items


# ---------------------------------------------------------------------------
# P1-S8: POST /api/pipeline/review/accept
# ---------------------------------------------------------------------------


@router.post("/review/accept")
async def accept_review_item(
    request: ReviewActionRequest,
    manager: ReviewManager = Depends(get_review_manager),
) -> dict:
    """Accept a review item — set confidence to 1.0."""
    try:
        manager.accept_review_item(request.item_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "accepted", "item_id": request.item_id}


# ---------------------------------------------------------------------------
# P1-S9: POST /api/pipeline/review/reject
# ---------------------------------------------------------------------------


@router.post("/review/reject")
async def reject_review_item(
    request: ReviewActionRequest,
    manager: ReviewManager = Depends(get_review_manager),
) -> dict:
    """Reject a review item — set confidence to 0.0."""
    try:
        manager.reject_review_item(request.item_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "rejected", "item_id": request.item_id}


# ---------------------------------------------------------------------------
# P1-S10: POST /api/pipeline/review/override
# ---------------------------------------------------------------------------


@router.post("/review/override")
async def override_review_item(
    request: ReviewActionRequest,
    manager: ReviewManager = Depends(get_review_manager),
) -> dict:
    """Override a review item — set confidence to 1.0, human_override=1."""
    try:
        manager.override_review_item(request.item_id, request.new_value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "overridden", "item_id": request.item_id}


# ---------------------------------------------------------------------------
# P1-S11: POST /api/pipeline/operation
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
            executor.execute_split(request.presentation_index, request.split_point)

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
                request.presentation_index_left, request.presentation_index_right
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
                request.presentation_index_from, request.presentation_index_to
            )

        elif request.operation == "delete":
            if request.presentation_index is None:
                raise HTTPException(
                    status_code=400,
                    detail="delete requires presentation_index",
                )
            executor.execute_delete(request.presentation_index)

    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {"status": "ok", "operation": request.operation}


# ---------------------------------------------------------------------------
# P1-S12: GET /api/pipeline/export/{book_id}
# ---------------------------------------------------------------------------


@router.get("/export/{book_id}")
async def export_script(
    book_id: str,
    storage: PipelineStorage = Depends(get_storage),
) -> list[dict]:
    """Export the annotated script for a book."""
    script = export_annotated_script(book_id, storage)
    return script


# ---------------------------------------------------------------------------
# P1-S13: POST /api/pipeline/render
# ---------------------------------------------------------------------------


@router.post("/render")
async def render(
    request: RenderRequest,
    storage: PipelineStorage = Depends(get_storage),
    tts_engine: object | None = Depends(get_tts_engine),
) -> dict:
    """Render an audiobook from the pipeline's annotated script."""
    if tts_engine is None:
        raise HTTPException(
            status_code=503,
            detail="TTS engine not available",
        )

    batch_seed = request.batch_seed if request.batch_seed is not None else -1

    try:
        job_id = render_audiobook(
            request.book_id,
            storage,
            tts_engine,
            use_batch=request.use_batch,
            output_dir=request.output_dir,
            batch_seed=batch_seed,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Render failed: {exc}")

    return {"job_id": job_id}


# ---------------------------------------------------------------------------
# P1-S14: POST /api/pipeline/reonboard
# ---------------------------------------------------------------------------


@router.post("/reonboard")
async def reonboard(
    request: ReonboardRequest,
    storage: PipelineStorage = Depends(get_storage),
) -> dict:
    """Re-onboard a book: clear walk outputs, bump version."""
    try:
        new_version = reonboard_book(request.book_id, storage)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    return {
        "book_id": request.book_id,
        "version": new_version,
        "status": "reonboarded",
    }
