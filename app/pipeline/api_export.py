"""Pipeline API — Export and render endpoints.

Provides HTTP endpoints for exporting annotated scripts and rendering audiobooks:
- GET /api/pipeline/export/{book_id} — export annotated script for a book
- POST /api/pipeline/render — render an audiobook (background job)
- GET /api/pipeline/render_status/{job_id} — poll render job status
- POST /api/pipeline/cancel_render — cancel a running render job
- GET /api/pipeline/download/{job_id} — download rendered audiobook file
- POST /api/pipeline/merge — merge rendered chunks into M4B file
"""

from __future__ import annotations

import os
import threading
import uuid
import zipfile
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.pipeline.adapter import PipelineStorage
from app.pipeline.api_onboard import get_storage
from app.pipeline.assembly import export_annotated_script
from app.pipeline.tts_integration import CancelledError, render_audiobook


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------


class RenderRequest(BaseModel):
    """Request body for POST /api/pipeline/render."""

    book_id: str
    use_batch: bool = True
    output_dir: Optional[str] = None
    batch_seed: Optional[int] = None


class CancelRenderRequest(BaseModel):
    """Request body for POST /api/pipeline/cancel_render."""

    job_id: str


class MergeRequest(BaseModel):
    """Request body for POST /api/pipeline/merge."""

    book_id: str
    job_id: str


# ---------------------------------------------------------------------------
# Render job tracking (module-level)
# ---------------------------------------------------------------------------


# Each entry:
#   {
#     "status": "running" | "completed" | "failed" | "cancelled",
#     "output_dir": str | None,
#     "error": str | None,
#     "cancel_event": threading.Event,
#   }
_render_jobs: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Dependency injection — overridable in tests
# ---------------------------------------------------------------------------


def get_tts_engine() -> object | None:
    """FastAPI dependency: return the TTS engine (or None).

    Lazily imports ``app.engine`` at call time to avoid circular imports
    that would occur at module level (``app.app`` imports ``app.pipeline.api``
    during its own module initialisation).

    Returns ``None`` when the engine factory fails to initialize; callers
    map that to HTTP 503 (see the render endpoint).  Tests override this
    dependency via FastAPI ``dependency_overrides``; only
    ``TestGetTTSEngineProduction`` exercises the real production path.
    """
    from app.engine import get_tts_engine as _get_engine  # lazy import — avoids circular dependency

    return _get_engine()


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


def _run_render_job(
    job_id: str,
    book_id: str,
    storage: PipelineStorage,
    tts_engine: object,
    use_batch: bool,
    output_dir: Optional[str],
    batch_seed: int,
) -> None:
    """Background task: execute render_audiobook and update job state."""
    job = _render_jobs[job_id]
    cancel_event: threading.Event = job["cancel_event"]
    try:
        resolved_dir = render_audiobook(
            book_id,
            storage,
            tts_engine,
            use_batch=use_batch,
            output_dir=output_dir,
            batch_seed=batch_seed,
            job_id=job_id,
            cancel_check=cancel_event.is_set,
        )
        job["output_dir"] = resolved_dir
        job["status"] = "completed"
    except CancelledError:
        job["status"] = "cancelled"
    except Exception as exc:  # noqa: BLE001 — surface any render failure
        job["status"] = "failed"
        job["error"] = str(exc)


@router.post("/render")
async def render(
    request: RenderRequest,
    background_tasks: BackgroundTasks,
    storage: PipelineStorage = Depends(get_storage),
    tts_engine: object | None = Depends(get_tts_engine),
) -> dict:
    """Start an audiobook render as a background job.

    Returns immediately with the job_id; clients poll
    ``GET /api/pipeline/render_status/{job_id}`` for progress.
    """
    if tts_engine is None:
        raise HTTPException(
            status_code=503,
            detail="TTS engine not available",
        )

    job_id = str(uuid.uuid4())
    _render_jobs[job_id] = {
        "status": "running",
        "output_dir": None,
        "error": None,
        "cancel_event": threading.Event(),
    }

    batch_seed = request.batch_seed if request.batch_seed is not None else -1

    background_tasks.add_task(
        _run_render_job,
        job_id,
        request.book_id,
        storage,
        tts_engine,
        request.use_batch,
        request.output_dir,
        batch_seed,
    )

    return {"job_id": job_id, "status": "started"}


# ---------------------------------------------------------------------------
# GET /api/pipeline/render_status/{job_id}
# ---------------------------------------------------------------------------


@router.get("/render_status/{job_id}")
async def render_status(job_id: str) -> dict:
    """Return the current status of a render job."""
    job = _render_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown job_id: {job_id}")
    return {
        "job_id": job_id,
        "status": job["status"],
        "output_dir": job["output_dir"],
        "error": job["error"],
    }


# ---------------------------------------------------------------------------
# POST /api/pipeline/cancel_render
# ---------------------------------------------------------------------------


@router.post("/cancel_render")
async def cancel_render(request: CancelRenderRequest) -> dict:
    """Request cancellation of a running render job."""
    job = _render_jobs.get(request.job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown job_id: {request.job_id}")
    if job["status"] != "running":
        return {"status": "already_finished", "job_id": request.job_id}
    job["cancel_event"].set()
    return {"status": "cancelled", "job_id": request.job_id}


# ---------------------------------------------------------------------------
# GET /api/pipeline/download/{job_id}
# ---------------------------------------------------------------------------


@router.get("/download/{job_id}")
async def download_render(job_id: str) -> FileResponse:
    """Download the rendered audiobook file.

    Serves ``audiobook.m4b`` if present in the job's output directory,
    otherwise falls back to ``audiobook.zip`` (chunks packaged for the
    caller to merge).  Returns 404 if the job has not completed or the
    output file is missing.
    """
    job = _render_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown job_id: {job_id}")
    if job["status"] != "completed":
        raise HTTPException(
            status_code=404,
            detail=f"Job not completed (status: {job['status']})",
        )
    output_dir = job.get("output_dir")
    if not output_dir or not os.path.isdir(output_dir):
        raise HTTPException(status_code=404, detail="Output directory not found")

    m4b_path = os.path.join(output_dir, "audiobook.m4b")
    if os.path.isfile(m4b_path):
        return FileResponse(
            m4b_path,
            media_type="audio/mp4",
            filename="audiobook.m4b",
        )

    # Fallback: package chunks into a zip archive on demand
    zip_path = os.path.join(output_dir, "audiobook.zip")
    if not os.path.isfile(zip_path):
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
            for name in sorted(os.listdir(output_dir)):
                if name.endswith((".wav", ".mp3", ".m4a", ".flac")):
                    zf.write(os.path.join(output_dir, name), arcname=name)
    if os.path.isfile(zip_path):
        return FileResponse(
            zip_path,
            media_type="application/zip",
            filename="audiobook.zip",
        )

    raise HTTPException(status_code=404, detail="No audiobook file found in output directory")


# ---------------------------------------------------------------------------
# POST /api/pipeline/merge
# ---------------------------------------------------------------------------


@router.post("/merge")
async def merge_audiobook(request: MergeRequest) -> dict:
    """Merge rendered audio chunks into a single M4B file.

    Locates WAV chunks in the render job's output directory and uses ffmpeg
    to concatenate them into a single audiobook.m4b file.  Returns the path
    to the merged file.
    """
    import subprocess

    job = _render_jobs.get(request.job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown job_id: {request.job_id}")
    if job["status"] != "completed":
        raise HTTPException(
            status_code=400,
            detail=f"Job not completed (status: {job['status']})",
        )

    output_dir = job.get("output_dir")
    if not output_dir or not os.path.isdir(output_dir):
        raise HTTPException(status_code=404, detail="Output directory not found")

    # Find WAV chunks sorted by name (chunk_0000.wav, chunk_0001.wav, ...)
    chunk_files = sorted(
        f
        for f in os.listdir(output_dir)
        if f.endswith(".wav") and f.startswith("chunk_")
    )

    if not chunk_files:
        raise HTTPException(
            status_code=400,
            detail="No audio chunks found in output directory",
        )

    # Build ffmpeg concat file list
    concat_list_path = os.path.join(output_dir, "concat_list.txt")
    with open(concat_list_path, "w") as f:
        for chunk in chunk_files:
            # ffmpeg concat demuxer expects file paths
            chunk_path = os.path.join(output_dir, chunk)
            f.write(f"file '{chunk_path}'\n")

    m4b_path = os.path.join(output_dir, "audiobook.m4b")

    try:
        # Use ffmpeg concat demuxer to join WAV chunks into M4B
        result = subprocess.run(
            [
                "ffmpeg",
                "-y",  # overwrite output
                "-f", "concat",
                "-safe", "0",
                "-i", concat_list_path,
                "-c:a", "aac",
                "-b:a", "128k",
                "-f", "ipod",  # M4B container
                m4b_path,
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )

        if result.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail=f"ffmpeg failed: {result.stderr[-500:] if result.stderr else 'unknown error'}",
            )

    except FileNotFoundError:
        raise HTTPException(
            status_code=500,
            detail="ffmpeg not found on system",
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=500,
            detail="ffmpeg timed out",
        )
    finally:
        # Clean up concat list file
        if os.path.exists(concat_list_path):
            os.remove(concat_list_path)

    return {"status": "ok", "output_path": m4b_path}
