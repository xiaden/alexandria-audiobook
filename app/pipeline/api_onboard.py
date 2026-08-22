"""Pipeline API — Onboarding endpoints.

Provides HTTP endpoints for onboarding EPUBs and re-onboarding books:
- POST /api/pipeline/onboard — accept an EPUB, extract text, populate spine
- POST /api/pipeline/reonboard — clear walk outputs, bump version

Also owns the production storage singleton (``_storage`` / ``_get_production_storage``)
and the ``get_storage`` FastAPI dependency, since onboarding is the primary
producer of storage instances.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from app.pipeline.adapter import PipelineStorage, SQLiteAdapter
from app.pipeline.assembly import reonboard_book
from app.pipeline.extract import extract_epub_text
from app.pipeline.populate import populate_spine
from app.pipeline.tts_integration import get_render_root


def _write_bytes(path: str, content: bytes) -> None:
    """Persist uploaded bytes to *path* with a blocking write (off the event loop)."""
    with open(path, "wb") as f:
        f.write(content)


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------


class ReonboardRequest(BaseModel):
    """Request body for POST /api/pipeline/reonboard."""

    book_id: str


# ---------------------------------------------------------------------------
# Dependency injection — overridable in tests
# ---------------------------------------------------------------------------

# Module-level singleton for production use.
_storage: PipelineStorage | None = None


def _get_production_storage() -> PipelineStorage:
    """Lazily create and return the production SQLiteAdapter singleton.

    Startup-only side effects on first acquisition: stale running
    render_job/walk_run rows are flipped to ``interrupted``
    (``reconcile_stale_runs``), then manifests are rebuilt for completed
    render_job rows whose run dir exists and artifact-missing jobs are
    flagged (``rebuild_manifests``).
    """
    global _storage
    if _storage is None:
        db_path = os.environ.get("PIPELINE_DB_PATH", "./data/pipeline.db")
        adapter = SQLiteAdapter(db_path)
        adapter.init_db()
        # Startup-only reconciliation (contract rule #5): one pass flips stale
        # running render_job/walk_run rows to interrupted BEFORE the API serves
        # any request.  No on-read sweeper, no periodic reaper — single-process
        # deployment is race-free by construction.  Runs once, on first
        # acquisition, before any request can be handled.
        adapter.reconcile_stale_runs()
        # Startup-only manifest rebuild (contract rule #3 — rows = truth,
        # manifest = derived): regenerate manifest.json for completed jobs
        # whose run dir exists and flag artifact-missing jobs.  Runs AFTER
        # reconciliation so freshly-interrupted rows are never rebuilt.
        # RENDER_ROOT is read from the environment at call time.
        adapter.rebuild_manifests(get_render_root())
        _storage = adapter
    return _storage


def get_storage() -> PipelineStorage:
    """FastAPI dependency: return the pipeline storage adapter."""
    return _get_production_storage()


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])


# ---------------------------------------------------------------------------
# POST /api/pipeline/onboard
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
    # Never use the client-controlled filename as a filesystem path.  A unique
    # server-generated name also keeps cleanup confined to ``tmp_dir``.
    tmp_path = os.path.join(tmp_dir, f"{uuid.uuid4()}.epub")
    try:
        content = await file.read()
        await asyncio.to_thread(_write_bytes, tmp_path, content)

        # Generate a book_id
        book_id = str(uuid.uuid4())

        # Extract EPUB text
        try:
            result = extract_epub_text(tmp_path, book_id, storage)
        except Exception as exc:  # noqa: BLE001 — EPUB extraction may raise many types; mapped to HTTP 400
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
        except Exception as exc:  # noqa: BLE001 — spine population may raise many types; mapped to HTTP 500
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
# POST /api/pipeline/reonboard
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
