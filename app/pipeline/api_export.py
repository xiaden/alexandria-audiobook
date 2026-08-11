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
- GET /api/pipeline/export/chunk/{job_id}/{idx} — bounded-range WAV chunk
  serving (200/206/416, capped per-request Range slices)
- GET /api/pipeline/export/audio/{job_id} — whole-book playback: serves the
  job's file artifact (media type from the extension) or, for jobs without
  one, a synthesized whole-book WAV streamed chunk-by-chunk from disk with
  Range support computed across chunk boundaries
- POST /api/pipeline/export/m4b — 3-phase FFMETADATA1 polished export:
  concat the chunk WAVs (ffmpeg concat demuxer), write audiobook.ffmetadata
  (global tags + auto chapters, TIMEBASE=1/1000, integer-ms END clamped to a
  single ffprobe pass), mux the m4b (+ optional attached_pic cover) with an
  optional libmp3lame mp3 and an always-producible ZIP_STORED Audacity bundle
"""

from __future__ import annotations

import os
import shutil
import stat
import struct
import subprocess
import threading
import time
import uuid
import zipfile
from typing import Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
)
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel
from starlette.concurrency import iterate_in_threadpool
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import MalformedRangeHeader, PlainTextResponse, RangeNotSatisfiable

from app.pipeline.adapter import PipelineStorage
from app.pipeline.api_onboard import get_storage
from app.pipeline.assembly import export_annotated_script
from app.pipeline.tts_integration import (
    PAUSE_BETWEEN_SPEAKERS_MS,
    PAUSE_SAME_SPEAKER_MS,
    PAUSED_ARTIFACT_NAME,
    CancelledError,
    get_render_root,
    render_audiobook,
    resolve_effective_pauses,
)
from app.utils import load_tts_config


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
# Pause-assembly status contract (Plan L, P2-S3)
#
# The render/job-status and export responses surface a tri-state lifecycle
# describing whether the paused audio was actually assembled into the output:
#
#   pauses_state    "pending"   assembly has not run yet (Phase 2: postprocessor
#                               not wired — always the current value)
#                   "applied"   the paused artifact was assembled and used
#                   "failed"    assembly was attempted but raised (see pauses_error)
#   pauses_applied  True        iff pauses_state == "applied" (a derived boolean
#                               for consumers that only need the flag)
#   pauses_error    str | None  failure detail when pauses_state == "failed"
#
# Alongside the tri-state, every response resolves the effective pause pair for
# the book (book override -> config default -> built-in fallback) and reports
# how many per-span ``pause_after_ms`` overrides are present.  Assembly itself
# is implemented in Phase 3; Phase 2 only defines and serves this contract,
# always in the "pending" state.
# ---------------------------------------------------------------------------


_PAUSES_STATE_PENDING = "pending"
_PAUSES_STATE_APPLIED = "applied"
_PAUSES_STATE_FAILED = "failed"


def _resolved_pause_metadata(storage: PipelineStorage, book_id: str) -> dict:
    """Resolve the effective pause pair for *book_id* + its override count.

    Precedence mirrors ``resolve_effective_pauses``: book override ->
    config default (``load_tts_config``) -> built-in 500/250 fallback.  NULL
    book columns resolve to the next tier; ``0`` is honored as an intentional
    no-gap.  ``pause_override_count`` counts spans with a non-NULL
    ``pause_after_ms``.
    """
    book_rows = storage.execute_query(
        "SELECT pause_between_speakers_ms, pause_same_speaker_ms FROM book"
        " WHERE id = ?",
        (book_id,),
    )
    book_overrides = book_rows[0] if book_rows else {}
    between, same = resolve_effective_pauses(
        book_overrides=book_overrides, config_defaults=load_tts_config()
    )
    override_count = storage.execute_query(
        "SELECT COUNT(*) AS cnt FROM span"
        " JOIN paragraph_span AS span_edge ON span.id = span_edge.child_id"
        " JOIN scene_paragraph AS paragraph_edge"
        "     ON span_edge.parent_id = paragraph_edge.child_id"
        " JOIN chapter_scene AS scene_edge"
        "     ON paragraph_edge.parent_id = scene_edge.child_id"
        " JOIN book_chapter AS chapter_edge"
        "     ON scene_edge.parent_id = chapter_edge.child_id"
        " JOIN book ON chapter_edge.parent_id = book.id"
        " WHERE book.id = ? AND span.pause_after_ms IS NOT NULL",
        (book_id,),
    )
    return {
        "resolved_pause_between_speakers_ms": between,
        "resolved_pause_same_speaker_ms": same,
        "pause_override_count": override_count[0]["cnt"],
    }


def _pause_contract_payload(storage: PipelineStorage, book_id: str) -> dict:
    """Resolved pause metadata + tri-state (Phase 2: always 'pending')."""
    payload = _resolved_pause_metadata(storage, book_id)
    payload["pauses_applied"] = False
    payload["pauses_state"] = _PAUSES_STATE_PENDING
    payload["pauses_error"] = None
    return payload


def _paused_artifact_path(run_dir: str) -> str | None:
    """Return the canonical paused whole-book artifact when it is usable.

    The paused artifact (``PAUSED_ARTIFACT_NAME``, ``audiobook-paused.wav``) is
    the whole book with the resolved pauses baked in and is the authoritative
    source for every whole-book export surface (P4-S1).  It is usable when it
    exists as a file inside *run_dir* AND parses as a PCM WAV (a malformed or
    truncated file from a partial write is treated as absent so the caller
    falls back to the per-chunk concat).  Returns ``None`` when absent or
    unusable.
    """
    if not run_dir:
        return None
    path = os.path.join(run_dir, PAUSED_ARTIFACT_NAME)
    if not os.path.isfile(path):
        return None
    try:
        _parse_wav(path)
    except ValueError:
        return None
    return path


def _pause_contract_placeholder() -> dict:
    """Tri-state shape for row-less jobs (no book_id): built-in defaults."""
    return {
        "resolved_pause_between_speakers_ms": PAUSE_BETWEEN_SPEAKERS_MS,
        "resolved_pause_same_speaker_ms": PAUSE_SAME_SPEAKER_MS,
        "pause_override_count": 0,
        "pauses_applied": False,
        "pauses_state": _PAUSES_STATE_PENDING,
        "pauses_error": None,
    }


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
    tts_config: dict | None = None,
) -> None:
    """Background task: execute render_audiobook and update job state.

    ``render_audiobook`` persists the ``render_job`` row (rows = truth) on
    its own success/failure paths; this function keeps the legacy
    in-process ``_render_jobs`` dict in sync as the cancellation channel
    (its ``cancel_event``) and a fallback for row-less entries only —
    download is row-backed and never consults the dict — and mirrors
    failures that escaped the row handling (e.g. patched callers) back
    into the row.

    ``tts_config`` is the ``tts`` section of config.json resolved by the
    endpoint at request time; it is passed through to ``render_audiobook``
    so the global pause values survive the production chain (the background
    task executes with the config the user saw when starting the render).
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
            tts_config=tts_config,
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

    # TTS config snapshot at request time: the ``tts`` section of config.json
    # (the same source the TTS engine reads), carrying the global pause values
    # (pause_between_speakers_ms / pause_same_speaker_ms) into the render.
    tts_config = load_tts_config()

    background_tasks.add_task(
        _run_render_job,
        job_id,
        request.book_id,
        storage,
        tts_engine,
        request.use_batch,
        request.output_dir,
        batch_seed,
        tts_config=tts_config,
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
        "SELECT mode, status, error, output_dir, book_id FROM render_job"
        " WHERE job_id = ?",
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
        # Plan L (P2-S3): resolved pause settings + tri-state lifecycle
        # (Phase 2: assembly not wired, so pauses_state is always 'pending').
        response.update(_pause_contract_payload(storage, row["book_id"]))
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
        **_pause_contract_placeholder(),
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
# GET /api/pipeline/export/chunk/{job_id}/{idx}
# ---------------------------------------------------------------------------


@router.api_route("/export/chunk/{job_id}/{idx}", methods=["GET", "HEAD"])
async def export_chunk(
    job_id: str,
    idx: str,
    storage: PipelineStorage = Depends(get_storage),
) -> FileResponse:
    """Serve one rendered chunk WAV with bounded-range support.

    Rows = truth: the ``render_chunk`` row supplies the status and the
    ``wav_path`` — the request path never names a file, so no input can
    escape into an arbitrary filesystem path.  A non-integer or out-of-range
    ``idx`` names no chunk row and returns 404 (the plan allows 404 or 400
    for non-int / out-of-range idx; 404 keeps every "no such chunk" case
    uniform).  A chunk row in a non-``done`` status is not servable:
    ``pending``/``failed`` return 409 Conflict (the row exists but the
    artifact is not servable in that state), and ``evicted`` — a GC
    tombstone; the artifact was intentionally removed and will not return —
    returns 410 Gone.  A ``done`` chunk's stored ``wav_path`` is resolved
    against the run dir (the row's ``output_dir``, or the derived
    ``RENDER_ROOT/book-{id}/{job_id}/`` when NULL) and containment-checked
    under it via realpath (path traversal -> 404).  The file is streamed
    through ``BoundedRangeFileResponse``: audio/wav 200 for a full GET,
    206 + Content-Range for a valid Range, 416 + ``Content-Range: bytes
    */N`` for an unsatisfiable Range, 400 for a malformed Range (starlette
    >= 0.49.1 native semantics), and at most PIPELINE_MAX_RANGE_BYTES
    (default 4 MiB) bytes served per Range request.  No whole-file
    buffering — starlette seeks and streams 64 KiB slices.
    """
    # Validate idx as an integer chunk index.  A non-integer idx can name no
    # chunk row, so it is an unknown chunk (404), uniform with out-of-range.
    try:
        chunk_idx = int(idx)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Unknown chunk idx: {idx}")

    job_rows = storage.execute_query(
        "SELECT book_id, output_dir FROM render_job WHERE job_id = ?",
        (job_id,),
    )
    if not job_rows:
        raise HTTPException(status_code=404, detail=f"Unknown job_id: {job_id}")
    book_id = job_rows[0]["book_id"]
    output_dir = job_rows[0]["output_dir"]

    chunk_rows = storage.execute_query(
        "SELECT status, wav_path FROM render_chunk WHERE job_id = ? AND idx = ?",
        (job_id, chunk_idx),
    )
    if not chunk_rows:
        raise HTTPException(status_code=404, detail=f"Unknown chunk idx: {idx}")
    chunk_status = chunk_rows[0]["status"]
    wav_path = chunk_rows[0]["wav_path"]

    if chunk_status != "done":
        # Do NOT attempt to read the file for non-done chunks: pending is not
        # ready yet, failed will never produce audio, and evicted is a GC
        # tombstone (row exists, artifact gone).  410 for the tombstone (the
        # artifact was available once and is intentionally gone forever),
        # 409 for the live-but-unservable states.
        if chunk_status == "evicted":
            raise HTTPException(
                status_code=410,
                detail=f"Chunk evicted by garbage collection (idx {idx})",
            )
        raise HTTPException(
            status_code=409,
            detail=f"Chunk not servable (status: {chunk_status})",
        )

    if not wav_path:
        raise HTTPException(status_code=404, detail="Chunk WAV path not recorded")

    # Resolve the stored wav_path against the run dir.  Rows store the
    # absolute path (see tts_integration._mark_chunk_done); a relative path
    # (the manifest form) is resolved against the run dir for robustness.
    # The run dir is the row's output_dir, or the derived
    # RENDER_ROOT/book-{id}/{job_id}/ (phase 1 layout; get_render_root reads
    # the RENDER_ROOT env at call time) when the row has none.
    if output_dir:
        run_dir = output_dir
    else:
        run_dir = os.path.join(get_render_root(), f"book-{book_id}", job_id)
    if os.path.isabs(wav_path):
        candidate = wav_path
    else:
        candidate = os.path.join(run_dir, wav_path)
    real_candidate = os.path.realpath(candidate)
    real_run_dir = os.path.realpath(run_dir)
    if real_candidate != real_run_dir and not real_candidate.startswith(real_run_dir + os.sep):
        # Path escapes the run dir (poisoned/relocated row, relative '..'
        # traversal): treat as not found — never serve outside the run dir.
        raise HTTPException(status_code=404, detail="Chunk not found")

    return BoundedRangeFileResponse(
        real_candidate,
        media_type="audio/wav",
        max_range_bytes=_max_range_bytes(),
        detail="Chunk WAV not found",
    )


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


# ---------------------------------------------------------------------------
# Bounded-range WAV serving (GET /api/pipeline/export/chunk/{job_id}/{idx})
# ---------------------------------------------------------------------------


# Per-request byte cap for Range slices (the phase-4 "bounded-range"
# requirement): a single Range request may never stream more than this many
# bytes, so an attacker (or a misbehaving client) cannot repeatedly issue
# open-ended ``bytes=0-`` requests to stream the whole file.  Single chunk
# WAVs are typically 1-5 MB; the 4 MiB default bounds any one request to
# roughly a chunk's worth of audio while a legitimate full GET (no Range
# header) still returns the whole body uncapped.  Oversized slices are
# clamped to a cap-sized prefix and served as 206 with the clamped
# Content-Range — clients (media elements, curl) transparently re-request
# the remainder.
_DEFAULT_MAX_RANGE_BYTES = 4 * 1024 * 1024
_ENV_MAX_RANGE_BYTES = "PIPELINE_MAX_RANGE_BYTES"


def _max_range_bytes() -> int:
    """Per-request Range slice cap; env-tunable, read at call time."""
    raw = os.environ.get(_ENV_MAX_RANGE_BYTES)
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value > 0:
            return value
    return _DEFAULT_MAX_RANGE_BYTES


def _cap_range_value(range_value: str, file_size: int, cap: int) -> str:
    """Clamp the strict single-range form ``bytes=start-end`` to ``cap`` bytes.

    Deliberately NOT a general Range parser: only the exact single-range
    form is rewritten (end capped to ``start + cap - 1``); multi-range,
    suffix (``bytes=-N``), and malformed headers pass through untouched so
    starlette's own Range parser — DoS-safe because the installed starlette
    satisfies the >= 0.49.1 pin (GHSA-7f5h-v6xp-fcq8 / CVE-2025-62727,
    O(n^2) Range-header DoS fixed in 0.49.1) — makes every status decision.
    """
    if not range_value.lower().startswith("bytes="):
        return range_value
    spec = range_value.split("=", 1)[1]
    if "," in spec:
        return range_value  # multi-range: starlette handles (bounded by pin)
    dash = spec.find("-")
    if dash < 0:
        return range_value
    start_str = spec[:dash].strip()
    end_str = spec[dash + 1 :].strip()
    if not start_str or not start_str.isdigit():
        return range_value  # suffix form bytes=-N: serves at most N bytes
    if end_str and not end_str.isdigit():
        return range_value  # malformed: let starlette 400 it
    start = int(start_str)
    end = int(end_str) if end_str else file_size - 1
    if start >= file_size:
        return range_value  # unsatisfiable: let starlette 416 it
    if end >= file_size:
        end = file_size - 1
    if end - start + 1 > cap:
        return f"bytes={start}-{start + cap - 1}"
    return range_value


def _cap_range_headers(headers, file_size: int, cap: int) -> list:
    """Return the ASGI scope headers with the Range slice clamped to ``cap`` bytes.

    FileResponse reads the Range header from the ASGI scope at call time;
    replacing it before delegation is how the cap layers onto starlette's
    native 200/206/416 handling without re-implementing it.
    """
    capped = []
    for name, value in headers:
        if name.lower() == b"range":
            value = _cap_range_value(value.decode("latin-1"), file_size, cap).encode("latin-1")
        capped.append((name, value))
    return capped


class BoundedRangeFileResponse(FileResponse):
    """FileResponse that JSON-404s a missing/vanished file and caps Range slices.

    Mirrors the ``FileResponse404`` missing-file guard (a vanished artifact
    must never degrade to a broken 200 or a 500) and adds the phase-4
    bounded-range cap: a Range request never streams more than
    ``max_range_bytes`` bytes.  Only the strict single-range form is clamped
    (see ``_cap_range_value``); starlette's pinned (>= 0.49.1) parser keeps
    ownership of all status semantics.  A full GET without a Range header is
    never capped.  Streaming is starlette's native 64 KiB seek/read — the
    file is never loaded into memory whole.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        max_range_bytes: int = _DEFAULT_MAX_RANGE_BYTES,
        detail: str = "File not found",
        **kwargs: object,
    ) -> None:
        super().__init__(path, **kwargs)
        self._max_range_bytes = max_range_bytes
        self._detail = detail

    async def __call__(self, scope: object, receive: object, send: object) -> None:
        try:
            st = os.stat(self.path)
        except OSError:
            response = JSONResponse(status_code=404, content={"detail": self._detail})
            await response(scope, receive, send)
            return
        if not stat.S_ISREG(st.st_mode):
            # Not a regular file (directory, fifo, ...) — treat as missing.
            response = JSONResponse(status_code=404, content={"detail": self._detail})
            await response(scope, receive, send)
            return
        scope["headers"] = _cap_range_headers(
            scope["headers"], st.st_size, self._max_range_bytes
        )
        await super().__call__(scope, receive, send)


# ---------------------------------------------------------------------------
# GET /api/pipeline/export/audio/{job_id} — whole-book playback
# ---------------------------------------------------------------------------


_AUDIO_EXTENSION_MEDIA_TYPES = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".m4b": "audio/mp4",
    ".mp4": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".webm": "audio/webm",
    # Non-audio artifact bundles served by the pipeline export routes.
    ".zip": "application/zip",
}


# 64 KiB bounded read slice for synthesized streaming: the whole book is never
# loaded into memory — one chunk file is open at a time and read in slices.
_STREAM_CHUNK_SIZE = 64 * 1024


def _media_type_for_artifact(path: str) -> str:
    """Media type from the artifact file extension (default octet-stream)."""
    return _AUDIO_EXTENSION_MEDIA_TYPES.get(
        os.path.splitext(path)[1].lower(), "application/octet-stream"
    )


def _resolve_within_run_dir(candidate: str, run_dir: str) -> str | None:
    """Resolve a row-stored path inside *run_dir*; ``None`` when it escapes.

    Path-traversal discipline (phase 4): a stored path is joined against the
    run dir (or used as-is when absolute) and the realpath must stay under the
    run dir's realpath — an escape names no servable file.
    """
    if os.path.isabs(candidate):
        resolved = candidate
    else:
        resolved = os.path.join(run_dir, candidate)
    real = os.path.realpath(resolved)
    real_run = os.path.realpath(run_dir)
    if real != real_run and not real.startswith(real_run + os.sep):
        return None
    return real


def _parse_wav(path: str) -> tuple[bytes, int, int, tuple[int, int, int, int]]:
    """Parse a PCM WAV file into (header_bytes, data_offset, data_size, fmt).

    ``header_bytes`` is everything before the ``data`` chunk (the bytes to
    re-emit, with sizes patched, for a concatenated stream); ``data_offset``
    and ``data_size`` locate the PCM payload; ``fmt`` is
    ``(audio_format, channels, sample_rate, bits_per_sample)`` — the fields
    that must match across chunks for a lossless concatenation.

    Raises ``ValueError`` when the file is not a RIFF/WAVE with fmt + data
    chunks.  A ``data`` size running past EOF is clamped to the file size.
    """
    with open(path, "rb") as f:
        head = f.read(12)
        if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            raise ValueError("chunk is not a RIFF/WAVE file")
        file_size = os.path.getsize(path)
        offset = 12
        fmt = None
        data_offset = None
        data_size = None
        while offset + 8 <= file_size:
            f.seek(offset)
            chunk_header = f.read(8)
            cid, csize = struct.unpack("<4sI", chunk_header)
            if cid == b"fmt ":
                payload = f.read(min(csize, 16))
                if len(payload) >= 16:
                    (
                        audio_format,
                        channels,
                        sample_rate,
                        _byte_rate,
                        _block_align,
                        bits,
                    ) = struct.unpack("<HHIIHH", payload[:16])
                    fmt = (audio_format, channels, sample_rate, bits)
            elif cid == b"data":
                data_offset = offset + 8
                data_size = csize
                break
            offset += 8 + csize + (csize & 1)
        if fmt is None or data_offset is None:
            raise ValueError("chunk WAV missing fmt/data chunk")
        if data_offset + data_size > file_size:
            data_size = max(file_size - data_offset, 0)
        f.seek(0)
        header = f.read(data_offset)
    return (header, data_offset, data_size, fmt)


def _patch_wav_header(header: bytes, data_offset: int, total_data: int) -> bytes:
    """Fix up a concatenated WAV header: RIFF size + data-chunk size for the sum.

    ``data_offset`` is the position of the ``data`` chunk payload inside the
    source file — the four size bytes directly before it are the ``data``
    chunk's size field, patched to the summed payload; the RIFF size (bytes
    4-8) becomes ``header_len + total_data - 8``.
    """
    riff_size = len(header) + total_data - 8
    return (
        header[:4]
        + struct.pack("<I", riff_size)
        + header[8 : data_offset - 4]
        + struct.pack("<I", total_data)
        + header[data_offset:]
    )


class ConcatenatedWavResponse(Response):
    """Stream a virtual whole-book WAV (concatenated chunk PCM) with Range support.

    The body is synthesized on the fly: the first chunk's RIFF header (with the
    RIFF/data sizes patched for the summed payload) followed by each chunk's
    PCM data section, read from disk in bounded 64 KiB slices — the whole book
    is never materialized on disk nor loaded into memory.  Range requests are
    resolved against the virtual byte stream across chunk boundaries;
    starlette's own Range parser (``FileResponse._parse_range_header`` — the
    same semantics phase 4 relies on) decides 400 (malformed) / 416
    (unsatisfiable, with ``Content-Range: bytes */N``) / 206.  Single ranges
    are capped to ``max_range_bytes`` per request (``PIPELINE_MAX_RANGE_BYTES``,
    default 4 MiB) like the phase-4 chunk endpoint; a full GET without Range
    is never capped.  Multi-range requests fall back to a full 200:
    multipart/byteranges across a virtual stream is not supported, and browser
    ``<audio>`` seeking never sends multi-range.  HEAD returns headers only
    (Content-Length is the full virtual size).  Playback is inline — no
    Content-Disposition.
    """

    def __init__(
        self,
        chunks: list[tuple[str, int, int]],
        header: bytes,
        total_size: int,
        *,
        max_range_bytes: int = _DEFAULT_MAX_RANGE_BYTES,
    ) -> None:
        # chunks: [(path, data_offset, data_size), ...] in play order
        self._chunks = chunks
        self._header = header
        self._total_size = total_size
        self._max_range_bytes = max_range_bytes
        super().__init__(media_type="audio/wav")

    def _iter_slice(self, start: int, end: int):
        """Yield the virtual stream bytes in [start, end) — end is exclusive."""
        header_len = len(self._header)
        if start < header_len:
            yield self._header[start:min(end, header_len)]
        position = header_len
        for path, data_offset, data_size in self._chunks:
            segment_end = position + data_size
            if segment_end <= start or position >= end:
                position = segment_end
                continue
            lo = max(start, position) - position
            hi = min(end, segment_end) - position
            with open(path, "rb") as f:
                f.seek(data_offset + lo)
                remaining = hi - lo
                while remaining > 0:
                    buf = f.read(min(_STREAM_CHUNK_SIZE, remaining))
                    if not buf:
                        break
                    yield buf
                    remaining -= len(buf)
            position = segment_end

    def _base_headers(self) -> MutableHeaders:
        headers = MutableHeaders(raw=list(self.raw_headers))
        headers["accept-ranges"] = "bytes"
        return headers

    async def _send_body(
        self, send: object, start: int, end: int, send_header_only: bool
    ) -> None:
        if send_header_only:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            return
        # The sync generator runs in a threadpool so disk reads never block
        # the event loop (same pattern as StreamingResponse).
        async for buf in iterate_in_threadpool(self._iter_slice(start, end)):
            await send({"type": "http.response.body", "body": buf, "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def _send_simple(self, send: object, send_header_only: bool) -> None:
        headers = self._base_headers()
        headers["content-length"] = str(self._total_size)
        await send({"type": "http.response.start", "status": 200, "headers": headers.raw})
        await self._send_body(send, 0, self._total_size, send_header_only)

    async def _send_range(
        self, send: object, start: int, end: int, send_header_only: bool
    ) -> None:
        headers = self._base_headers()
        headers["content-range"] = f"bytes {start}-{end - 1}/{self._total_size}"
        headers["content-length"] = str(end - start)
        await send({"type": "http.response.start", "status": 206, "headers": headers.raw})
        await self._send_body(send, start, end, send_header_only)

    async def __call__(self, scope: object, receive: object, send: object) -> None:
        send_header_only = scope["type"] == "http" and scope["method"].upper() == "HEAD"
        http_range = Headers(scope=scope).get("range")
        if http_range is None:
            await self._send_simple(send, send_header_only)
            return
        try:
            ranges = FileResponse._parse_range_header(http_range, self._total_size)
        except MalformedRangeHeader as exc:
            return await PlainTextResponse(exc.content, status_code=400)(scope, receive, send)
        except RangeNotSatisfiable as exc:
            response = PlainTextResponse(
                status_code=416, headers={"Content-Range": f"bytes */{exc.max_size}"}
            )
            return await response(scope, receive, send)
        if len(ranges) != 1:
            # Multi-range: multipart/byteranges is not supported on the virtual
            # stream; fall back to a full 200 (a server MAY ignore a Range it
            # cannot satisfy in multipart form).  Browser seeking sends single
            # ranges only.
            await self._send_simple(send, send_header_only)
            return
        start, end = ranges[0]  # end is exclusive
        if end - start > self._max_range_bytes:
            end = start + self._max_range_bytes
        await self._send_range(send, start, end, send_header_only)


@router.api_route("/export/audio/{job_id}", methods=["GET", "HEAD"])
async def export_audio(
    job_id: str,
    storage: PipelineStorage = Depends(get_storage),
) -> Response:
    """Serve whole-book playback for a render job.

    Rows = truth: the ``render_job`` row decides what is served.  A completed
    job whose ``output_artifact_path`` names a file (an m4b produced by the
    batch merge path, or any artifact) serves that artifact through
    ``BoundedRangeFileResponse`` — media type from the file extension
    (audio/mp4 for .m4b), full Range semantics, inline playback (no
    Content-Disposition).  A completed job whose artifact is the run dir
    (individual mode without an m4b) serves a synthesized whole-book WAV:
    chunk wav_paths come from the ``render_chunk`` rows (rows = truth), or the
    sorted ``*.wav`` files in the run dir for batch mode (which has no chunk
    rows by contract), concatenated via ``ConcatenatedWavResponse``.  Chunk
    paths are containment-checked under the run dir (path traversal -> 404);
    every chunk must exist and share one format (sample rate / channels / bit
    depth) — differing formats return 409 (no resampler exists; ffmpeg is
    present via ``export_m4b`` but enforces the same single-format rule).
    Status dispatch: unknown job -> 404; non-completed
    jobs -> 404 'Job not completed (status: X)' (download_render pattern);
    expired jobs (GC tombstone) and evicted chunks -> 410 Gone (phase-4
    precedent: an artifact that was available once and is intentionally gone);
    missing files -> 404 JSON (FileResponse404 pattern).
    """
    rows = storage.execute_query(
        "SELECT book_id, mode, status, output_dir, output_artifact_path "
        "FROM render_job WHERE job_id = ?",
        (job_id,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail=f"Unknown job_id: {job_id}")
    row = rows[0]
    status = row["status"]
    if status == "expired":
        # GC tombstone: the artifacts were available once and are intentionally
        # gone forever (phase-4 evicted-chunk precedent).
        raise HTTPException(status_code=410, detail="Job expired by garbage collection")
    if status != "completed":
        raise HTTPException(
            status_code=404,
            detail=f"Job not completed (status: {status})",
        )

    output_dir = row["output_dir"]
    run_dir = output_dir or os.path.join(
        get_render_root(), f"book-{row['book_id']}", job_id
    )

    # File artifact: serve it with Range support and the media type for its
    # extension.  An artifact path escaping the run dir (poisoned/relocated
    # row) is treated as not found — never serve outside the run dir.
    artifact_path = row["output_artifact_path"]
    if artifact_path and not os.path.isdir(artifact_path):
        resolved = _resolve_within_run_dir(artifact_path, run_dir)
        if resolved is None:
            raise HTTPException(status_code=404, detail="Audio file not found")
        return BoundedRangeFileResponse(
            resolved,
            media_type=_media_type_for_artifact(artifact_path),
            max_range_bytes=_max_range_bytes(),
            detail="Audio file not found",
        )

    # Parity with download_render: a row finalized without an m4b may have had
    # audiobook.m4b produced since by POST /merge — serve it when present.
    m4b_path = os.path.join(run_dir, "audiobook.m4b")
    if os.path.isfile(m4b_path):
        return BoundedRangeFileResponse(
            m4b_path,
            media_type="audio/mp4",
            max_range_bytes=_max_range_bytes(),
            detail="Audio file not found",
        )

    # Synthesized whole-book WAV.  Individual mode: chunk wav_paths from the
    # rows (rows = truth), containment-checked.  Batch mode: no chunk rows by
    # contract, so the sorted *.wav files in the run dir are the whole book.
    if row["mode"] == "individual":
        chunk_rows = storage.execute_query(
            "SELECT status, wav_path FROM render_chunk WHERE job_id = ? ORDER BY idx",
            (job_id,),
        )
        if not chunk_rows:
            raise HTTPException(status_code=404, detail="No audio chunks found for job")
        if any(c["status"] == "evicted" for c in chunk_rows):
            raise HTTPException(
                status_code=410, detail="Audio chunks evicted by garbage collection"
            )
        for c in chunk_rows:
            if c["status"] != "done":
                raise HTTPException(
                    status_code=409, detail=f"Chunk not servable (status: {c['status']})"
                )
        candidates: list[str] = []
        for c in chunk_rows:
            wav_path = c["wav_path"]
            if not wav_path:
                raise HTTPException(status_code=404, detail="Audio file not found")
            resolved = _resolve_within_run_dir(wav_path, run_dir)
            if resolved is None:
                raise HTTPException(status_code=404, detail="Audio file not found")
            candidates.append(resolved)
    else:
        try:
            names = os.listdir(run_dir)
        except OSError:
            raise HTTPException(status_code=404, detail="Output directory not found")
        candidates = sorted(
            os.path.join(run_dir, name)
            for name in names
            if name.endswith(".wav")
            and name != PAUSED_ARTIFACT_NAME
            and os.path.isfile(os.path.join(run_dir, name))
        )
        if not candidates:
            raise HTTPException(
                status_code=404, detail="No audio files found in output directory"
            )

    # Parse every chunk, require identical formats (same sample rate / channels
    # / bit depth), and stream the concatenation.  A missing or malformed chunk
    # is never silently skipped: incomplete audio is not served.
    chunks: list[tuple[str, int, int]] = []
    fmt = None
    header = None
    for path in candidates:
        if not os.path.isfile(path):
            raise HTTPException(status_code=404, detail="Audio file not found")
        try:
            parsed = _parse_wav(path)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        if fmt is None:
            fmt = parsed[3]
            header = parsed[0]
        elif parsed[3] != fmt:
            # No resampler exists in the pipeline; ffmpeg (via export_m4b)
            # enforces the same single-format rule.
            raise HTTPException(
                status_code=409,
                detail="Chunk WAV formats differ (sample rate/channels/bit depth)",
            )
        chunks.append((path, parsed[1], parsed[2]))

    total_size = len(header) + sum(data_size for _, _, data_size in chunks)
    header = _patch_wav_header(header, chunks[0][1], total_size - len(header))
    return ConcatenatedWavResponse(
        chunks,
        header,
        total_size,
        max_range_bytes=_max_range_bytes(),
    )


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
# GET /api/pipeline/export/mp3/{job_id} + /export/audacity/{job_id}
# ---------------------------------------------------------------------------

# Hardcoded artifact file names inside a run dir — the exact names the
# POST /api/pipeline/export/m4b phase-3 mux writes (audiobook.mp3 via
# libmp3lame, audiobook-audacity.zip as the ZIP_STORED Audacity bundle).
# Constants only: no user input ever reaches a path.
_MP3_ARTIFACT_NAME = "audiobook.mp3"
_AUDACITY_ARTIFACT_NAME = "audiobook-audacity.zip"


def _completed_run_dir(storage: PipelineStorage, job_id: str) -> str:
    """Row-backed dispatch: the run dir of a completed render job, or 4xx.

    Rows = truth (rule #3): the ``render_job`` row decides what is servable
    — never the in-process ``_render_jobs`` dict.  Dispatch mirrors
    ``download_render`` / ``export_m4b``: unknown job -> 404
    'Unknown job_id: {job_id}'; expired (GC tombstone) -> 410 'Job expired
    by garbage collection'; non-completed -> 404 'Job not completed
    (status: {status})'; missing output dir -> 404 'Output directory not
    found'.  The run dir comes from the row's ``output_dir`` or the derived
    ``RENDER_ROOT/book-{book_id}/{job_id}`` layout.  Raises HTTPException;
    the caller builds the artifact response.
    """
    rows = storage.execute_query(
        "SELECT book_id, mode, status, output_dir FROM render_job WHERE job_id = ?",
        (job_id,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail=f"Unknown job_id: {job_id}")
    row = rows[0]
    status = row["status"]
    if status == "expired":
        raise HTTPException(status_code=410, detail="Job expired by garbage collection")
    if status != "completed":
        raise HTTPException(
            status_code=404,
            detail=f"Job not completed (status: {status})",
        )
    run_dir = row["output_dir"] or os.path.join(
        get_render_root(), f"book-{row['book_id']}", job_id
    )
    if not run_dir or not os.path.isdir(run_dir):
        raise HTTPException(status_code=404, detail="Output directory not found")
    return run_dir


@router.get("/export/mp3/{job_id}")
async def export_mp3_artifact(
    job_id: str,
    storage: PipelineStorage = Depends(get_storage),
) -> FileResponse:
    """Serve the MP3 artifact (``audiobook.mp3``) for a completed render job.

    The artifact name is a hardcoded constant — the same ``audiobook.mp3``
    the POST /api/pipeline/export/m4b phase-3 mux writes — so no user input
    reaches the path.  Served via ``FileResponse404`` (JSON 404 when the
    file is missing) with the media type for the extension (audio/mpeg) and
    an attachment Content-Disposition of ``audiobook.mp3``.
    """
    run_dir = _completed_run_dir(storage, job_id)
    artifact_path = os.path.join(run_dir, _MP3_ARTIFACT_NAME)
    return FileResponse404(
        artifact_path,
        media_type=_media_type_for_artifact(artifact_path),
        filename=os.path.basename(artifact_path),
    )


@router.get("/export/audacity/{job_id}")
async def export_audacity_artifact(
    job_id: str,
    storage: PipelineStorage = Depends(get_storage),
) -> FileResponse:
    """Serve the Audacity bundle (``audiobook-audacity.zip``) for a completed job.

    Same row-backed dispatch as the mp3 route; the artifact name is the
    hardcoded ``audiobook-audacity.zip`` written by the m4b export.  Served
    as application/zip with an attachment Content-Disposition of
    ``audiobook-audacity.zip`` via ``FileResponse404``.
    """
    run_dir = _completed_run_dir(storage, job_id)
    artifact_path = os.path.join(run_dir, _AUDACITY_ARTIFACT_NAME)
    return FileResponse404(
        artifact_path,
        media_type=_media_type_for_artifact(artifact_path),
        filename=os.path.basename(artifact_path),
    )


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

    # Find WAV chunks sorted by name (chunk_0000.wav, chunk_0001.wav, ...).
    # P4-S1: when the canonical paused whole-book artifact (PAUSED_ARTIFACT_NAME)
    # is present it is the authoritative source — merge it (single entry)
    # rather than re-concatenating the unpaused per-chunk WAVs.  Fall back to
    # the per-chunk concat when it is absent (a render that completed without
    # assembly).
    names = os.listdir(output_dir)
    paused_path = os.path.join(output_dir, PAUSED_ARTIFACT_NAME)
    if PAUSED_ARTIFACT_NAME in names and os.path.isfile(paused_path):
        chunk_files = [PAUSED_ARTIFACT_NAME]
    else:
        chunk_files = sorted(
            f
            for f in names
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


# ---------------------------------------------------------------------------
# POST /api/pipeline/export/m4b — 3-phase FFMETADATA1 polished export
# ---------------------------------------------------------------------------

# Subprocess discipline mirrors merge_audiobook: list-args only (never
# shell=True), capture_output, bounded timeouts, JSON 500 on failure.
_FFMPEG_TIMEOUT_S = 300
_FFPROBE_TIMEOUT_S = 60

# Intermediate concat/metadata artifacts live in this hidden subdir of the run
# dir so batch-mode *.wav enumeration (export_audio) and the zip fallback
# (download_render) never see them; the subdir is removed in ``finally``.
_M4B_EXPORT_TMP_DIR = ".m4b-export"

# Per-process feature-detect cache for the libmp3lame encoder.
_libmp3lame_cache: Optional[bool] = None


def _max_cover_bytes() -> int:
    """Cover upload size cap in bytes, env-tunable like PIPELINE_MAX_RANGE_BYTES."""
    try:
        return max(1, int(os.environ.get("PIPELINE_MAX_COVER_BYTES", str(20 * 1024 * 1024))))
    except ValueError:
        return 20 * 1024 * 1024


def _run_media_command(
    args: list[str], *, timeout: int = _FFMPEG_TIMEOUT_S, what: str = "ffmpeg"
) -> subprocess.CompletedProcess:
    """Run ffmpeg/ffprobe with list-args only; raise JSON 500 on failure.

    Mirrors merge_audiobook's error contract: missing binary / timeout /
    non-zero exit all become HTTP 500 with a bounded stderr excerpt.
    """
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail=f"{what} not found on system")
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=500, detail=f"{what} timed out")
    if result.returncode != 0:
        stderr = (result.stderr or "")[-500:]
        raise HTTPException(status_code=500, detail=f"{what} failed: {stderr}")
    return result


def _libmp3lame_available() -> bool:
    """Feature-detect the libmp3lame encoder (per-process cached probe).

    Runs ``ffmpeg -encoders`` once per process and scans for the encoder;
    absence of the binary or a failed probe degrades to M4B-only — the
    endpoint still works (DD open item #8).
    """
    global _libmp3lame_cache
    if _libmp3lame_cache is None:
        try:
            result = subprocess.run(
                ["ffmpeg", "-hide_banner", "-encoders"],
                capture_output=True,
                text=True,
                timeout=_FFMPEG_TIMEOUT_S,
            )
            _libmp3lame_cache = result.returncode == 0 and "libmp3lame" in result.stdout
        except (OSError, subprocess.TimeoutExpired):
            _libmp3lame_cache = False
    return _libmp3lame_cache


# Control characters (0x00-0x1F incl. newline, plus 0x7F) are stripped from
# every metadata value: FFMETADATA1 is line-oriented, so a literal newline in
# a value would silently corrupt the file structure.
_CONTROL_CHARS_TRANSLATE = str.maketrans("", "", "".join(chr(c) for c in range(32)) + chr(127))


def _clean_metadata_value(value: str) -> str:
    """Strip control characters and surrounding whitespace from a value."""
    if not value:
        return ""
    return value.translate(_CONTROL_CHARS_TRANSLATE).strip()


def _escape_ffmetadata_value(value: str) -> str:
    """Escape a value per ffmpeg's FFMETADATA1 writer convention.

    The reader unescapes backslash sequences, so the specials are written as
    ``\\``, ``\\=``, ``\\;``, ``\\#`` and round-trip byte-exact (verified
    empirically against ffmpeg 7.1.5).  Backslash must be escaped first;
    control characters are removed by the caller before escaping.
    """
    return (
        value.replace("\\", "\\\\")
        .replace("=", "\\=")
        .replace(";", "\\;")
        .replace("#", "\\#")
    )


def _write_ffmetadata(
    path: str,
    *,
    title: str,
    author: str,
    narrator: str,
    year: str,
    description: str,
    chapters: list[tuple[int, int, str]],
) -> None:
    """Write an FFMETADATA1 file atomically (tmp file -> os.replace).

    Global tags map title/artist (author)/album_artist (narrator)/date
    (year)/comment (description); every [CHAPTER] carries TIMEBASE=1/1000 with
    integer-millisecond START/END — the CI-validated contract.  The writer
    escapes ``= ; # \\`` per the reader's unescape rules, so values round-trip
    byte-exact into the muxed container.
    """
    lines = [";FFMETADATA1"]
    for key, value in (
        ("title", title),
        ("artist", author),
        ("album_artist", narrator),
        ("date", year),
        ("comment", description),
    ):
        cleaned = _clean_metadata_value(value)
        if cleaned:
            lines.append(f"{key}={_escape_ffmetadata_value(cleaned)}")
    for start_ms, end_ms, chapter_title in chapters:
        lines.append("[CHAPTER]")
        lines.append("TIMEBASE=1/1000")
        lines.append(f"START={start_ms}")
        lines.append(f"END={end_ms}")
        lines.append(f"title={_escape_ffmetadata_value(_clean_metadata_value(chapter_title))}")
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp_path, path)


def _chunk_duration_ms(path: str) -> int:
    """Integer-ms duration of a PCM WAV chunk from its header.

    Reuses ``_parse_wav`` (phase 5); the data_size/byte_rate math is exactly
    what ffprobe reports for WAV input.  Non-PCM payloads -> 409.
    """
    parsed = _parse_wav(path)
    data_size = parsed[2]
    audio_format, channels, sample_rate, bits = parsed[3]
    if audio_format != 1:
        raise HTTPException(status_code=409, detail="Chunk is not PCM audio")
    byte_rate = sample_rate * channels * bits // 8
    if byte_rate <= 0:
        raise HTTPException(status_code=409, detail="Chunk has invalid WAV parameters")
    return int(round(data_size / byte_rate * 1000))


def _probe_duration_ms(path: str) -> int:
    """Single ffprobe pass: stream duration of *path* in integer ms.

    This is the END-clamp source for the last auto chapter marker.
    """
    result = _run_media_command(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
        ],
        timeout=_FFPROBE_TIMEOUT_S,
        what="ffprobe",
    )
    text = (result.stdout or "").strip()
    try:
        return int(round(float(text) * 1000))
    except ValueError:
        raise HTTPException(status_code=500, detail="ffprobe returned an unparseable duration")


@router.post("/export/m4b")
def export_m4b(
    job_id: str = Form(...),
    title: str = Form(""),
    author: str = Form(""),
    narrator: str = Form(""),
    year: str = Form(""),
    description: str = Form(""),
    cover: UploadFile | None = File(None),
    storage: PipelineStorage = Depends(get_storage),
) -> dict:
    """3-phase FFMETADATA1 polished M4B export for a completed render job.

    Phase 1 CONCAT: when the canonical paused whole-book artifact
    (``PAUSED_ARTIFACT_NAME``) is present and parseable in the run dir it is the
    single export source (P4-S1) — the whole book with the resolved pauses baked
    in — and ffmpeg concat encodes it directly.  Otherwise ffmpeg concat demuxer
    joins the chunk WAVs in order (render_chunk rows by idx for individual mode
    — rows = truth; sorted *.wav files for batch mode, excluding the paused
    artifact) into an intermediate WAV in a hidden subdir.
    Phase 2 METADATA: ``audiobook.ffmetadata`` written atomically with the
    global tags (title/artist/album_artist/date/comment) and auto chapter
    markers (one per export source; the whole book is a single source when the
    paused artifact is used) — START/END in integer ms, TIMEBASE=1/1000, and the
    last END
    clamped to the concatenated duration from a single ffprobe pass (never
    exceeding the real stream duration).  Phase 3 MUX: m4b (aac, ipod
    container) carrying the tags/chapters and an optional uploaded cover as an
    mjpeg ``attached_pic`` stream; then an mp3 via libmp3lame when the encoder
    is feature-detected (else M4B-only with a message, DD open item #8); and
    an always-producible ``audiobook-audacity.zip`` (ZIP_STORED, the export
    source(s) + mp3) for Audacity import.  On success the render_job row's
    ``output_artifact_path`` is updated to the m4b (rows = truth) so the
    phase-5 whole-book endpoint serves it.  Dispatch mirrors export_audio:
    unknown job -> 404, expired -> 410, non-completed -> 404, evicted/non-done
    chunks -> 410/409, path traversal -> 404, format mismatch -> 409, no
    chunks -> 400.  User input never reaches a shell: every subprocess call
    uses list-args and metadata values are control-char-stripped and
    ffmetadata-escaped before being written.

    Returns:
        A dict with ``status`` ("ok"), ``output_path`` (the m4b path),
        ``mp3`` (bool: whether the MP3 was produced), ``mp3_path`` (path or
        None), ``audacity`` (always True), and ``audacity_path`` (the
        ``audiobook-audacity.zip`` path).  A ``message`` key is present only
        when libmp3lame is unavailable and the export is degraded to
        M4B-only (DD open item #8), explaining why no MP3 was produced.
        Every successful export also carries the resolved pause metadata
        (``resolved_pause_between_speakers_ms`` / ``resolved_pause_same_speaker_ms``
        / ``pause_override_count``) and the truthful pause tri-state (P4-S2):
        ``pauses_applied``/``pauses_state`` is true/'applied' with a concise
        ``pauses_message`` when the canonical paused artifact was the source;
        false/'failed' with a bounded ``pauses_error`` (no filesystem paths)
        when assembly was unavailable and the unpaused per-chunk concat was
        exported instead.
    """
    # 0. Row lookup — rows are the source of truth (phase-5 dispatch).
    rows = storage.execute_query(
        "SELECT book_id, mode, status, output_dir FROM render_job WHERE job_id = ?",
        (job_id,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail=f"Unknown job_id: {job_id}")
    row = rows[0]
    status = row["status"]
    if status == "expired":
        raise HTTPException(status_code=410, detail="Job expired by garbage collection")
    if status != "completed":
        raise HTTPException(status_code=404, detail=f"Job not completed (status: {status})")

    run_dir = row["output_dir"] or os.path.join(
        get_render_root(), f"book-{row['book_id']}", job_id
    )
    if not os.path.isdir(run_dir):
        raise HTTPException(status_code=404, detail="Output directory not found")

    # 1. Resolve chunk sources (rows = truth for individual mode; run-dir
    #    *.wav enumeration for batch).  Containment-checked like phases 4-5
    #    so a poisoned wav_path can never reach ffmpeg.
    if row["mode"] == "individual":
        chunk_rows = storage.execute_query(
            "SELECT status, wav_path FROM render_chunk WHERE job_id = ? ORDER BY idx",
            (job_id,),
        )
        if not chunk_rows:
            raise HTTPException(status_code=400, detail="No audio chunks found for job")
        if any(c["status"] == "evicted" for c in chunk_rows):
            raise HTTPException(
                status_code=410, detail="Audio chunks evicted by garbage collection"
            )
        for c in chunk_rows:
            if c["status"] != "done":
                raise HTTPException(
                    status_code=409, detail=f"Chunk not servable (status: {c['status']})"
                )
        candidates: list[str] = []
        for c in chunk_rows:
            wav_path = c["wav_path"]
            if not wav_path:
                raise HTTPException(status_code=404, detail="Audio file not found")
            resolved = _resolve_within_run_dir(wav_path, run_dir)
            if resolved is None or not os.path.isfile(resolved):
                raise HTTPException(status_code=404, detail="Audio file not found")
            candidates.append(resolved)
    else:
        try:
            names = os.listdir(run_dir)
        except OSError:
            raise HTTPException(status_code=404, detail="Output directory not found")
        candidates = sorted(
            os.path.join(run_dir, name)
            for name in names
            if name.endswith(".wav")
            and name != PAUSED_ARTIFACT_NAME
            and os.path.isfile(os.path.join(run_dir, name))
        )
        if not candidates:
            raise HTTPException(status_code=400, detail="No audio files found in output directory")

    # P4-S1: consume the canonical paused whole-book artifact when it is
    # usable.  The paused artifact (PAUSED_ARTIFACT_NAME) is the whole book with
    # the resolved pauses baked in — the authoritative source for whole-book
    # export.  When present and parseable it REPLACES the per-chunk source set
    # so the export never mixes paused audio with the unpaused per-chunk concat.
    # When absent (a render that completed without assembly, e.g. a fake
    # engine), fall back to the per-chunk concat so legacy rows still export —
    # the truthful tri-state in the response reflects which path ran.
    paused_path = _paused_artifact_path(run_dir)
    paused_used = paused_path is not None
    if paused_used:
        candidates = [paused_path]

    # 2. Validate the chunk set before any ffmpeg work: every chunk parses as
    #    PCM WAV and all share one format (phase-5 rule).
    durations_ms: list[int] = []
    fmt = None
    for path in candidates:
        try:
            parsed = _parse_wav(path)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        if fmt is None:
            fmt = parsed[3]
        elif parsed[3] != fmt:
            raise HTTPException(
                status_code=409,
                detail="Chunk WAV formats differ (sample rate/channels/bit depth)",
            )
        durations_ms.append(_chunk_duration_ms(path))

    tmp_dir = os.path.join(run_dir, _M4B_EXPORT_TMP_DIR)
    os.makedirs(tmp_dir, exist_ok=True)
    concat_list_path = os.path.join(tmp_dir, "concat_list.txt")
    intermediate_wav = os.path.join(tmp_dir, "intermediate.wav")
    m4b_path = os.path.join(run_dir, "audiobook.m4b")
    ffmetadata_path = os.path.join(run_dir, "audiobook.ffmetadata")
    mp3_path = os.path.join(run_dir, _MP3_ARTIFACT_NAME)
    zip_path = os.path.join(run_dir, _AUDACITY_ARTIFACT_NAME)
    mp3_produced = False
    try:
        # Cover upload: validate and persist before any ffmpeg work.  The
        # bytes are read with the cap + 1 sentinel so oversize is detected;
        # the file name does not matter (ffmpeg sniffs the real format).
        cover_path = None
        if cover is not None:
            max_bytes = _max_cover_bytes()
            cover_bytes = cover.file.read(max_bytes + 1)
            if len(cover_bytes) > max_bytes:
                raise HTTPException(status_code=400, detail="Cover image exceeds the size limit")
            if not (cover.content_type or "").startswith("image/"):
                raise HTTPException(status_code=400, detail="Cover must be an image file")
            cover_path = os.path.join(tmp_dir, "cover.img")
            with open(cover_path, "wb") as f:
                f.write(cover_bytes)

        # 3. Phase 1 CONCAT: concat demuxer joins the chunk WAVs in order.
        #    Paths are containment-checked; single quotes are escaped per the
        #    demuxer's own rules (``'`` -> ``'\''``) for odd-but-legal paths.
        with open(concat_list_path, "w", encoding="utf-8") as f:
            for path in candidates:
                f.write(f"file '{path.replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n")
        _run_media_command(
            [
                "ffmpeg", "-y", "-v", "error",
                "-f", "concat", "-safe", "0",
                "-i", concat_list_path,
                "-c:a", "copy",
                intermediate_wav,
            ],
            what="ffmpeg",
        )

        # 4. Phase 2 METADATA: auto chapter markers — one per chunk (the
        #    natural audiobook mapping) with the last END clamped to the
        #    concatenated duration from a SINGLE ffprobe pass over the
        #    intermediate (never exceeding the real stream duration).
        duration_ms = _probe_duration_ms(intermediate_wav)
        chapters: list[tuple[int, int, str]] = []
        cursor = 0
        for i, chunk_ms in enumerate(durations_ms):
            start_ms = cursor
            end_ms = min(cursor + chunk_ms, duration_ms)
            chapters.append((start_ms, end_ms, f"Chapter {i + 1}"))
            cursor = end_ms
        _write_ffmetadata(
            ffmetadata_path,
            title=title,
            author=author,
            narrator=narrator,
            year=year,
            description=description,
            chapters=chapters,
        )

        # 5. Phase 3 MUX: m4b (aac + ipod container) with tags and chapters
        #    from the ffmetadata file; the cover is embedded as an mjpeg
        #    ``attached_pic`` stream when provided.
        mux_args = [
            "ffmpeg", "-y", "-v", "error",
            "-i", intermediate_wav,
            "-i", ffmetadata_path,
        ]
        if cover_path is not None:
            mux_args += ["-i", cover_path]
        mux_args += ["-map", "0:a"]
        if cover_path is not None:
            mux_args += ["-map", "2:v", "-c:v", "mjpeg", "-disposition:v", "attached_pic"]
        mux_args += [
            "-map_metadata", "1", "-map_chapters", "1",
            "-c:a", "aac", "-b:a", "128k",
            "-f", "ipod",
            m4b_path,
        ]
        _run_media_command(mux_args, what="ffmpeg")

        # 6. MP3 where libmp3lame is available (feature-detect); degrade to
        #    M4B-only with a message when it is not.
        if _libmp3lame_available():
            _run_media_command(
                [
                    "ffmpeg", "-y", "-v", "error",
                    "-i", intermediate_wav,
                    "-i", ffmetadata_path,
                    "-map_metadata", "1",
                    "-c:a", "libmp3lame", "-b:a", "128k",
                    mp3_path,
                ],
                what="ffmpeg",
            )
            mp3_produced = True

        # 7. Audacity bundle: ZIP_STORED with the per-chunk WAVs (universally
        #    importable) plus the mp3 when produced.  Always producible — the
        #    download zip fallback (download_render) uses the same ZIP_STORED
        #    pattern.
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
            for path in candidates:
                zf.write(path, arcname=os.path.basename(path))
            if mp3_produced:
                zf.write(mp3_path, arcname=_MP3_ARTIFACT_NAME)

        # 8. Rows = truth: record the m4b as the job's output artifact so the
        #    phase-5 whole-book path serves it.  Only on full success — a
        #    failed export leaves any prior artifact recorded in the row
        #    untouched.
        storage.execute_update(
            "UPDATE render_job SET output_artifact_path = ? WHERE job_id = ?",
            (m4b_path, job_id),
        )
    finally:
        # Intermediates never linger in the run dir (a crashed request would
        #    otherwise pollute batch-mode *.wav enumeration / zip fallback).
        if os.path.isdir(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)

    response: dict = {
        "status": "ok",
        "output_path": m4b_path,
        "mp3": mp3_produced,
        "mp3_path": mp3_path if mp3_produced else None,
        "audacity": True,
        "audacity_path": zip_path,
    }
    # P4-S2 truthful pause contract (supersedes the Plan K disclosure, decision
    # #13): the resolved pause pair + override count ride on every export, and
    # the tri-state truthfully reflects whether the canonical paused artifact
    # was the source.  When it was, pauses_applied=true and a concise message is
    # carried (the only case a message appears).  When it was not (assembly
    # unavailable — the render completed without a paused artifact), the export
    # falls back to the unpaused per-chunk concat and reports pauses_applied=
    # false with pauses_state='failed' and a bounded pauses_error that leaks no
    # filesystem paths.
    pause_payload = _resolved_pause_metadata(storage, row["book_id"])
    if paused_used:
        response["pauses_applied"] = True
        response["pauses_state"] = _PAUSES_STATE_APPLIED
        response["pauses_error"] = None
        response["pauses_message"] = (
            "Pauses applied: the exported audio includes the resolved speaker "
            "pauses between spans."
        )
    else:
        response["pauses_applied"] = False
        response["pauses_state"] = _PAUSES_STATE_FAILED
        response["pauses_error"] = (
            "Paused audio assembly artifact not found; exported the concatenated "
            "source audio without inserted pauses."
        )
    response.update(pause_payload)
    if not _libmp3lame_available():
        response["message"] = (
            "MP3 export unavailable: the libmp3lame encoder was not found in "
            "this ffmpeg build; exported M4B only."
        )
    return response
