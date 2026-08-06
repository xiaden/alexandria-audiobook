"""Pipeline API — Export and render endpoints.

Provides HTTP endpoints for exporting annotated scripts and rendering audiobooks:
- GET /api/pipeline/export/{book_id} — export annotated script for a book
- POST /api/pipeline/render — render an audiobook (background job)
- GET /api/pipeline/render_status/{job_id} — poll render job status
- GET /api/pipeline/export/jobs/{job_id} — render job detail (ExportJobDetail DTO)
- GET /api/pipeline/export/jobs/{job_id}/chunks — chunk rows for a job (ChunkRow DTO)
- POST /api/pipeline/cancel_render — cancel a running render job
- GET /api/pipeline/download/{job_id} — download rendered audiobook file
- POST /api/pipeline/merge — merge rendered chunks into M4B file
"""

from __future__ import annotations

import os
import threading
import time
import uuid
import zipfile
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import FileResponse, JSONResponse
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


def _now_ms() -> int:
    """Current wall-clock time as INTEGER unix milliseconds (schema convention)."""
    return int(time.time() * 1000)


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
    """Background task: execute render_audiobook and update job state.

    ``render_audiobook`` persists the ``render_job`` row (rows = truth) on
    its own success/failure paths; this function keeps the legacy
    in-process ``_render_jobs`` dict in sync as the cancellation channel
    (its ``cancel_event``) and a fallback for row-less entries only —
    download is row-backed and never consults the dict — and mirrors
    failures that escaped the row handling (e.g. patched callers) back
    into the row.
    """
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
        _mark_job_row_terminal(storage, job_id, "cancelled")
    except Exception as exc:  # noqa: BLE001 — surface any render failure
        job["status"] = "failed"
        job["error"] = str(exc)
        _mark_job_row_terminal(storage, job_id, "failed", error=str(exc))


def _mark_job_row_terminal(
    storage: PipelineStorage, job_id: str, status: str, error: str | None = None
) -> None:
    """Mirror a terminal dict state into the ``render_job`` row (safety net).

    ``render_audiobook`` finalizes rows on its own error paths; this covers
    exceptions raised outside its row handling so rows remain the source of
    truth even then.  Idempotent — re-writing the same terminal state is a
    no-op at the row level.
    """
    storage.execute_update(
        "UPDATE render_job SET status = ?, error = ?, finished_ms = ? "
        "WHERE job_id = ?",
        (status, error, _now_ms(), job_id),
    )


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
    # Rows = truth: register the job row up front so the returned job_id
    # matches a persisted row (status 'running') even before the background
    # task starts.  render_audiobook reuses this row.
    mode = "batch" if request.use_batch else "individual"
    now = _now_ms()
    storage.execute_insert(
        "INSERT INTO render_job "
        "(job_id, book_id, mode, status, output_dir, created_ms, started_ms) "
        "VALUES (?, ?, ?, 'running', ?, ?, ?)",
        (job_id, request.book_id, mode, request.output_dir, now, now),
    )
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
async def render_status(
    job_id: str,
    storage: PipelineStorage = Depends(get_storage),
) -> dict:
    """Return the current status of a render job.

    Reads the ``render_job`` row (rows = truth); individual-mode jobs also
    report per-chunk counts derived from ``render_chunk`` rows.  The
    legacy in-process ``_render_jobs`` dict remains only for the
    ``cancel_event`` channel and as a fallback for jobs with no row.
    """
    rows = storage.execute_query(
        "SELECT mode, status, error, output_dir FROM render_job WHERE job_id = ?",
        (job_id,),
    )
    if rows:
        row = rows[0]
        response: dict = {
            "job_id": job_id,
            "status": row["status"],
            "output_dir": row["output_dir"],
            "error": row["error"],
            "mode": row["mode"],
        }
        if row["mode"] == "individual":
            counts = storage.execute_query(
                "SELECT status, COUNT(*) AS cnt FROM render_chunk "
                "WHERE job_id = ? GROUP BY status",
                (job_id,),
            )
            by_status = {c["status"]: c["cnt"] for c in counts}
            response["total_chunks"] = sum(by_status.values())
            response["completed_chunks"] = by_status.get("done", 0)
            response["failed_chunks"] = by_status.get("failed", 0)
        return response

    # Fallback: in-process tracker (legacy entries / cancel_event holder)
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
# GET /api/pipeline/export/jobs/{job_id}
# ---------------------------------------------------------------------------


@router.get("/export/jobs/{job_id}")
async def export_job_detail(
    job_id: str,
    storage: PipelineStorage = Depends(get_storage),
) -> dict:
    """Return the ``render_job`` row for a job (ExportJobDetail DTO).

    Rows = truth: the row is the single authority for job state, so the
    response is the row's full field set (job_id, book_id, mode, status,
    error, output_dir, output_artifact_path, created_ms, started_ms,
    finished_ms).  404 when no row exists for the job.
    """
    rows = storage.execute_query(
        "SELECT job_id, book_id, mode, status, error, output_dir, "
        "output_artifact_path, created_ms, started_ms, finished_ms "
        "FROM render_job WHERE job_id = ?",
        (job_id,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail=f"Unknown job_id: {job_id}")
    return rows[0]


# ---------------------------------------------------------------------------
# GET /api/pipeline/export/jobs/{job_id}/chunks
# ---------------------------------------------------------------------------


@router.get("/export/jobs/{job_id}/chunks")
async def export_job_chunks(
    job_id: str,
    storage: PipelineStorage = Depends(get_storage),
) -> list[dict]:
    """Return the ``render_chunk`` rows for a job (ChunkRow DTO), ordered by idx.

    404 when the job itself is unknown; an existing job with no chunk rows
    (batch mode has none by contract) returns an empty list.
    """
    job_rows = storage.execute_query(
        "SELECT job_id FROM render_job WHERE job_id = ?",
        (job_id,),
    )
    if not job_rows:
        raise HTTPException(status_code=404, detail=f"Unknown job_id: {job_id}")
    rows = storage.execute_query(
        "SELECT job_id, idx, status, wav_path, error FROM render_chunk "
        "WHERE job_id = ? ORDER BY idx",
        (job_id,),
    )
    return rows


# ---------------------------------------------------------------------------
# POST /api/pipeline/cancel_render
# ---------------------------------------------------------------------------


@router.post("/cancel_render")
async def cancel_render(request: CancelRenderRequest) -> dict:
    """Request cancellation of a running render job.

    Schema-compatible cancel semantics (manager decision L20; the
    CONTRACTS/DD wording "status cancelling + persisted cancel flag" is
    NOT storable): the render_job.status CHECK constraint allows only
    ('pending','running','completed','failed','cancelled','interrupted',
    'expired') and render_job has no cancel flag column.  The cancel
    intent is therefore carried by the in-process cancel_event; the row
    reaches the terminal schema-valid status 'cancelled' when the
    background job task observes the event (the CancelledError path in
    ``_run_render_job`` / ``render_audiobook``).  Crash-survival of stuck
    'running' rows is handled by startup reconciliation (running ->
    interrupted).
    """
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


class FileResponse404(FileResponse):
    """FileResponse that serves the file, or sends a JSON 404 when it is missing.

    Starlette 1.3.1 (verified against the installed source) stats the path
    lazily inside ``__call__`` — not in ``__init__`` — and raises
    ``RuntimeError("File at path ... does not exist.")`` when the file
    vanished between the endpoint's existence check and the send, which
    FastAPI surfaces as HTTP 500.  This subclass re-checks existence before
    delegating and, when the file is missing, sends a ``JSONResponse`` 404
    with ``{"detail": ...}`` instead of a broken 200 or 500.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        detail: str = "File not found",
        **kwargs: object,
    ) -> None:
        super().__init__(path, **kwargs)
        self._detail = detail

    async def __call__(self, scope: object, receive: object, send: object) -> None:
        if not os.path.isfile(self.path):
            response = JSONResponse(status_code=404, content={"detail": self._detail})
            await response(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


@router.get("/download/{job_id}")
async def download_render(
    job_id: str,
    storage: PipelineStorage = Depends(get_storage),
) -> FileResponse:
    """Download the rendered audiobook file (row-backed).

    The ``render_job`` row is the source of truth — the in-process
    ``_render_jobs`` dict is never consulted for download lookups (it
    remains only for the ``cancel_event`` channel).  Serves the row's
    ``output_artifact_path`` when it names a file, via ``FileResponse404``
    which returns a JSON 404 when the artifact file is missing.  Rows
    completed without an m4b (artifact = output dir) fall back to
    ``audiobook.zip`` — wav/mp3/m4a/flac packaged ZIP_STORED — built from
    the row's ``output_dir``.  404 for unknown jobs, non-completed jobs,
    missing output directories, and vanished artifacts.
    """
    rows = storage.execute_query(
        "SELECT status, output_dir, output_artifact_path "
        "FROM render_job WHERE job_id = ?",
        (job_id,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail=f"Unknown job_id: {job_id}")
    row = rows[0]
    if row["status"] != "completed":
        raise HTTPException(
            status_code=404,
            detail=f"Job not completed (status: {row['status']})",
        )
    output_dir = row["output_dir"]
    if not output_dir or not os.path.isdir(output_dir):
        raise HTTPException(status_code=404, detail="Output directory not found")

    artifact_path = row["output_artifact_path"]

    # Serve the recorded artifact (the audiobook.m4b written by POST /merge)
    # through FileResponse404: it streams the file when present and sends a
    # JSON 404 when the file is missing — a completed row whose artifact
    # vanished must not degrade to a broken 200 (or an empty zip).  Rows
    # completed without an m4b record the output dir instead and fall
    # through to the zip fallback below.
    if artifact_path and not os.path.isdir(artifact_path):
        return FileResponse404(
            artifact_path,
            media_type="audio/mp4",
            filename="audiobook.m4b",
            detail="Audio file not found",
        )

    # No file artifact recorded at finalize (output dir) — POST /merge may
    # have produced an m4b since; serve it when present.
    m4b_path = os.path.join(output_dir, "audiobook.m4b")
    if os.path.isfile(m4b_path):
        return FileResponse404(
            m4b_path,
            media_type="audio/mp4",
            filename="audiobook.m4b",
            detail="Audio file not found",
        )

    # Fallback: package the audio chunks into a zip archive on demand.
    zip_path = os.path.join(output_dir, "audiobook.zip")
    if not os.path.isfile(zip_path):
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
            for name in sorted(os.listdir(output_dir)):
                if name.endswith((".wav", ".mp3", ".m4a", ".flac")):
                    zf.write(os.path.join(output_dir, name), arcname=name)
    if os.path.isfile(zip_path):
        return FileResponse404(
            zip_path,
            media_type="application/zip",
            filename="audiobook.zip",
            detail="Zip archive not found",
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
