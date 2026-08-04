"""Pipeline API — Export and render endpoints.

Provides HTTP endpoints for exporting annotated scripts and rendering audiobooks:
- GET /api/pipeline/export/{book_id} — export annotated script for a book
- POST /api/pipeline/render — render an audiobook from the pipeline's script
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.pipeline.adapter import PipelineStorage
from app.pipeline.api_onboard import get_storage
from app.pipeline.assembly import export_annotated_script
from app.pipeline.tts_integration import render_audiobook


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------


class RenderRequest(BaseModel):
    """Request body for POST /api/pipeline/render."""

    book_id: str
    use_batch: bool = True
    output_dir: Optional[str] = None
    batch_seed: Optional[int] = None


# ---------------------------------------------------------------------------
# Dependency injection — overridable in tests
# ---------------------------------------------------------------------------


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
# GET /api/pipeline/export/{book_id}
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
# POST /api/pipeline/render
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
