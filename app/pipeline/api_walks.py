"""Pipeline API — Walk endpoints.

Provides HTTP endpoints for running walks and querying walk/character status:
- POST /api/pipeline/run_walk — run a single walk for a book
- POST /api/pipeline/run_all_walks — run all 9 walks serially for a book
- GET /api/pipeline/walks/log/{run_id} — stream a walk run's JSONL log as SSE
- GET /api/pipeline/walk_status/{book_id} — per-walk status for a book
- GET /api/pipeline/characters/{book_id} — character ledger for a book
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.pipeline.adapter import PipelineStorage
from app.pipeline.api_onboard import get_storage
from app.pipeline.api_review import (
    Workbench,
    WorkbenchValidationError,
    _guard,
    _wb_http,
    get_review_manager,
    get_workbench,
)
from app.pipeline.ledger import CharacterLedger
from app.pipeline.prompt_config import TASK_NAMES, PromptConfigDomain
from app.pipeline.revision_conflict import (
    CODE_ALREADY_RAN,
    CODE_CROSS_BOOK,
    CODE_STALE,
    revision_conflict_http,
)
from app.pipeline.walks import runner as walk_runner_mod
from app.pipeline.walks.log_service import (
    WalkLogService,
    WalkLogSubscription,
    _is_valid_uuid,
)
from app.pipeline.walks.order import WALK_ORDER
from app.pipeline.walks.runner import WalkRunner
from app.pipeline.workbench import (
    BookNotFoundError,
    StaleRevisionError,
    ValidationError,
)

# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------


logger = logging.getLogger(__name__)


class RunWalkRequest(BaseModel):
    """Request body for POST /api/pipeline/run_walk."""

    walk_name: str
    book_id: str
    config: dict = {}


class RunAllWalksRequest(BaseModel):
    """Request body for POST /api/pipeline/run_all_walks."""

    book_id: str
    config: dict = {}


class CancelWalksRequest(BaseModel):
    """Request body for POST /api/pipeline/cancel_walks."""

    book_id: str


# ---------------------------------------------------------------------------
# Dependency injection — overridable in tests
# ---------------------------------------------------------------------------

_walk_runner: WalkRunner | None = None


def get_walk_runner(
    request: Request,
    storage: PipelineStorage = Depends(get_storage),
) -> WalkRunner:
    """FastAPI dependency: return the WalkRunner singleton.

    Production wiring (CONTRACTS.md line 75): the singleton is constructed
    with the process-owned Part A service via
    ``getattr(request.app.state, 'walk_log_service', None)`` -- the SAME
    access path the ``get_walk_log_service`` dependency uses -- so
    API-started runs (``run_walk``/``run_all_walks`` through the reserved
    runner) perform sink operations and the SSE route has records to stream.
    The ``getattr`` None fallback preserves the pre-existing
    ``WalkRunner(storage)`` default (``log_service=None`` -> no sink ops) for
    any context where the lifespan has not run (e.g. router-only TestClients),
    and never constructs a second WalkLogService or broker. The module-level
    singleton caches the first resolution; FastAPI runs the lifespan before
    the first request, so ``app.state.walk_log_service`` is bound in
    production.
    """
    global _walk_runner
    if _walk_runner is None:
        _walk_runner = WalkRunner(
            storage,
            log_service=getattr(request.app.state, "walk_log_service", None),
        )
    return _walk_runner


def get_character_ledger(
    storage: PipelineStorage = Depends(get_storage),
) -> CharacterLedger:
    """FastAPI dependency: return a CharacterLedger."""
    return CharacterLedger(storage)


def get_walk_log_service(request: Request) -> WalkLogService:
    """FastAPI dependency: return the process-owned Part A WalkLogService.

    The service is created and owned by the ``app.app`` lifespan, which sets
    ``app.state.walk_log_service``. Reading ``request.app.state`` avoids a
    circular import with ``app.app`` (which imports this router) while sharing
    the single process-owned instance -- never constructing a second service
    or broker. Tests override this dependency directly via
    ``app.dependency_overrides[get_walk_log_service]``.
    """
    service: WalkLogService = request.app.state.walk_log_service
    return service


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
    background_tasks: BackgroundTasks,
    runner: WalkRunner = Depends(get_walk_runner),
    storage: PipelineStorage = Depends(get_storage),
) -> dict:
    """Run a single walk for a book in the background.

    The API owns identity and reservation (Part B contract): it generates ONE
    canonical UUID, persists the exact ``pending`` ``walk_run`` row via
    ``reserve_walk_run``, then schedules the reserved runner method
    ``run_walk_reserved`` through ``BackgroundTasks`` so the response returns
    before the walk starts. Invalid walk names are rejected with 400 BEFORE any
    reservation (no pending row). Returns immediately with
    ``{status: 'started', started: true, walk_name: ..., run_id: ...}``; poll
    ``GET /walk_status/{book_id}`` for progress.
    """
    if request.walk_name not in WALK_ORDER:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown walk: {request.walk_name}. "
            f"Must be one of {WALK_ORDER}",
        )
    # One canonical run UUID per request, generated before reservation.
    run_id = str(uuid.uuid4())
    # Clear any previous cancellation flag (existing boundary, unchanged).
    runner.clear_cancel(request.book_id)
    # Reserve the pending row (may raise -> mark pending failed, never execute).
    try:
        walk_runner_mod.reserve_walk_run(
            storage, run_id, request.book_id, request.walk_name
        )
    except Exception as exc:
        try:
            walk_runner_mod.mark_reserved_runs_failed(storage, [run_id], str(exc))
        except Exception:
            # Best-effort cleanup: a write-broken storage must not mask the
            # contracted 500 error shape (CONTRACTS: allocation failure returns
            # the existing API error shape).
            logger.warning(
                "mark_reserved_runs_failed failed for run %s", run_id, exc_info=True
            )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to reserve walk run: {exc}",
        ) from exc
    background_tasks.add_task(
        runner.run_walk_reserved, run_id, request.walk_name, request.book_id, request.config
    )
    return {
        "status": "started",
        "started": True,
        "walk_name": request.walk_name,
        "run_id": run_id,
    }


# ---------------------------------------------------------------------------
# POST /api/pipeline/run_all_walks
# ---------------------------------------------------------------------------


@router.post("/run_all_walks")
async def run_all_walks(
    request: RunAllWalksRequest,
    background_tasks: BackgroundTasks,
    runner: WalkRunner = Depends(get_walk_runner),
    storage: PipelineStorage = Depends(get_storage),
) -> dict:
    """Run all 9 walks serially for a book in the background.

    The API owns identity and reservation (Part B contract): it generates ONE
    canonical ``batch_id`` (correlation-only, no parent row) plus nine canonical
    child UUIDs in ``WALK_ORDER``, persists all nine ``pending`` rows via
    ``reserve_all_walk_runs``, then schedules the reserved runner method
    ``run_all_walks_reserved`` through ``BackgroundTasks`` so the response
    returns before the batch starts. Returns immediately with
    ``{status: 'started', started: true, batch_id: ..., run_ids: [...], runs: [...]}``;
    poll ``GET /walk_status/{book_id}`` for progress.
    """
    # One canonical batch_id + nine canonical child UUIDs in WALK_ORDER.
    batch_id = str(uuid.uuid4())
    reservations = tuple(
        (walk_name, str(uuid.uuid4())) for walk_name in WALK_ORDER
    )
    # Clear any previous cancellation flag (existing boundary, unchanged).
    runner.clear_cancel(request.book_id)
    # Reserve all pending rows (may raise -> mark pending failed, never execute).
    try:
        walk_runner_mod.reserve_all_walk_runs(
            storage, request.book_id, reservations
        )
    except Exception as exc:
        try:
            walk_runner_mod.mark_reserved_runs_failed(
                storage, [rid for _, rid in reservations], str(exc)
            )
        except Exception:
            # Best-effort cleanup: a write-broken storage must not mask the
            # contracted 500 error shape (CONTRACTS: allocation failure returns
            # the existing API error shape).
            logger.warning(
                "mark_reserved_runs_failed failed for run_ids %s",
                [rid for _, rid in reservations],
                exc_info=True,
            )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to reserve walk runs: {exc}",
        ) from exc
    background_tasks.add_task(
        runner.run_all_walks_reserved,
        batch_id,
        reservations,
        request.book_id,
        request.config,
    )
    return {
        "status": "started",
        "started": True,
        "batch_id": batch_id,
        "run_ids": [rid for _, rid in reservations],
        "runs": [
            {"walk_name": walk_name, "run_id": rid}
            for walk_name, rid in reservations
        ],
    }


# ---------------------------------------------------------------------------
# POST /api/pipeline/cancel_walks
# ---------------------------------------------------------------------------


@router.post("/cancel_walks")
async def cancel_walks(
    request: CancelWalksRequest,
    runner: WalkRunner = Depends(get_walk_runner),
) -> dict:
    """Cancel any running walks for a book.

    Sets a cancellation flag that the runner checks before each walk.
    The request is persisted — ``cancel_requested = 1`` on the book's
    active (pending/running) ``walk_run`` rows with a heartbeat refresh,
    plus a stop-file per active run so the cancel survives a restart.
    Returns ``{status: 'cancelled'}``.
    """
    runner.cancel_walks(request.book_id)
    return {"status": "cancelled"}


# ---------------------------------------------------------------------------
# GET /api/pipeline/walks/{book_id}/runs
# ---------------------------------------------------------------------------


@router.get("/walks/{book_id}/runs")
async def get_walk_runs(
    book_id: str,
    storage: PipelineStorage = Depends(get_storage),
) -> list[dict]:
    """Return the ``walk_run`` rows for a book, newest-first (WalkRunRow DTO).

    Ordered by ``created_ms`` DESC so the most recent run appears first.
    A book with no runs returns an empty list.  Rows = truth: the
    response is the row's field set (run_id, walk_name, status,
    heartbeat_ms, created_ms, finished_ms, error).
    """
    rows = storage.execute_query(
        "SELECT run_id, walk_name, status, heartbeat_ms, created_ms, "
        "finished_ms, error FROM walk_run WHERE book_id = ? "
        "ORDER BY created_ms DESC",
        (book_id,),
    )
    return rows


# ---------------------------------------------------------------------------
# GET /api/pipeline/walks/log/{run_id} (Per-walk log streaming, Part C SSE)
#
# Consumes the Part A WalkLogService contracts: the authoritative JSONL file is
# the replay source for BOTH active and completed runs; the subscription bridges
# only broker records after the file tail. No Part A replay/broker/terminal logic
# is reimplemented here -- open_subscription owns replay capture + live
# registration atomically, and its subscription semantics deliver terminal
# completion and future-event waiting for a cursor beyond the file tail.
# ---------------------------------------------------------------------------


_MAX_LAST_EVENT_ID = 2**63 - 1


def _parse_last_event_id(request: Request, run_id: str) -> int:
    """Parse ``Last-Event-ID`` (empty or ``{run_id}:{non-negative integer}``).

    Returns the sequence to pass as ``after_seq``, or raises 400 for malformed,
    foreign-run, negative, non-integer, or impossible (``> 2**63 - 1``) values.
    This runs BEFORE ``open_subscription`` is called.
    """
    value = request.headers.get("last-event-id") or ""
    if not value:
        return -1
    parts = value.split(":")
    if len(parts) != 2:
        raise HTTPException(
            status_code=400, detail=f"Malformed Last-Event-ID: {value!r}"
        )
    rid, seq_str = parts
    if rid != run_id:
        raise HTTPException(
            status_code=400,
            detail=f"Last-Event-ID names a different run than the path: {rid!r}",
        )
    if not seq_str or not seq_str.isascii() or not seq_str.isdigit():
        raise HTTPException(
            status_code=400,
            detail=f"Last-Event-ID sequence must be a non-negative integer: {seq_str!r}",
        )
    seq = int(seq_str)
    if seq > _MAX_LAST_EVENT_ID:
        raise HTTPException(
            status_code=400,
            detail=f"Last-Event-ID sequence out of range: {seq_str!r}",
        )
    return seq


async def _event_stream(
    subscription: WalkLogSubscription,
    run_id: str,
    db_status: str,
    after_seq: int = -1,
) -> AsyncIterator[str]:
    """Emit each ``WalkLogRecord`` as SSE framing, then ``event: complete``.

    Each record is ``id: {run_id}:{seq}`` / ``event: log`` / one JSON ``data:``
    line / blank line. Records with ``seq <= after_seq`` (already consumed by the
    client's ``Last-Event-ID`` cursor) are suppressed so the stream strictly
    resumes after the cursor for both file replay and live records. After the
    terminal record the stream emits ``complete`` with ``{run_id, status}`` and
    stops. The ``finally`` closes the subscription on normal completion,
    cancellation, or client disconnect (non-blocking).
    """
    try:
        terminal_status: str | None = None
        async for rec in subscription:
            if rec.seq <= after_seq:
                continue
            yield (
                f"id: {rec.id}\n"
                f"event: log\n"
                f"data: {json.dumps(dict(rec.data), ensure_ascii=False)}\n\n"
            )
            if rec.terminal:
                terminal_status = (rec.data or {}).get("status")
        # Terminal status is authoritative from the streamed terminal record; when
        # no terminal record was streamed (e.g. a cursor beyond the tail of an
        # already-terminal run), fall back to the walk_run row's status.
        status = terminal_status if terminal_status is not None else db_status
        yield (
            f"event: complete\n"
            f"data: {json.dumps({'run_id': run_id, 'status': status}, ensure_ascii=False)}\n\n"
        )
    finally:
        subscription.close()


@router.get("/walks/log/{run_id}")
async def stream_walk_log(
    run_id: str,
    request: Request,
    storage: PipelineStorage = Depends(get_storage),
    service: WalkLogService = Depends(get_walk_log_service),
) -> StreamingResponse:
    """Stream the per-walk JSONL log for ``run_id`` as SSE.

    Enforces a canonical UUID (400) before any DB lookup, returns 404 for an
    unknown run, 410 for a known run whose ephemeral ``{root}/{run_id}.log`` is
    absent, rejects symlink escapes, parses ``Last-Event-ID`` (400 before opening
    a subscription), and opens exactly one subscription whose authoritative-file
    replay + live registration are atomic (Part A). The DB row/status is never
    changed by a missing-file response.
    """
    # (a) canonical UUID syntax validation BEFORE any DB lookup or subscription.
    if not _is_valid_uuid(run_id):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid run id (must be a canonical UUID): {run_id!r}",
        )
    # (b) parameterized walk_run lookup -> 404 for an unknown run.
    rows = storage.execute_query(
        "SELECT run_id, status FROM walk_run WHERE run_id = ?", (run_id,)
    )
    if not rows:
        raise HTTPException(status_code=404, detail=f"Unknown walk run: {run_id}")
    db_status = rows[0]["status"]
    # (d) symlink escape: derive the path only from the validated canonical UUID
    # and reject a symlinked log path so the target is never read into the body.
    if (service._root_dir / f"{run_id}.log").is_symlink():
        raise HTTPException(
            status_code=400,
            detail=f"Refusing symlinked log path for run: {run_id}",
        )
    # (P2-S3) parse Last-Event-ID before opening any subscription (400 on error).
    after_seq = _parse_last_event_id(request, run_id)
    # (P2-S4) open exactly once after validation: file replay + live registration
    # are atomic. KeyError means the known run's ephemeral file is absent -> 410
    # (the DB row is left untouched).
    try:
        subscription = service.open_subscription(
            run_id, after_seq=after_seq, loop=asyncio.get_running_loop()
        )
    except KeyError:
        raise HTTPException(
            status_code=410,
            detail=f"Walk log file not available for run: {run_id}",
        ) from None
    return StreamingResponse(
        _event_stream(subscription, run_id, db_status, after_seq),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/walks/log/{rest:path}")
async def stream_walk_log_reject_path(
    rest: str,
) -> StreamingResponse:
    """Reject any non-canonical path under ``/walks/log/`` with 400.

    The primary ``{run_id}`` route only matches a single path segment, so a
    traversal-encoded ``run_id`` (e.g. ``..%2F..%2Fetc%2Fpasswd``, which Starlette
    decodes into a multi-segment path) falls through to this catch-all. Such a
    path can never be a canonical UUID, so it is rejected 400 before any DB
    lookup or file access -- never a 200 file read.
    """
    raise HTTPException(
        status_code=400,
        detail=f"Invalid run id (must be a canonical UUID): {rest!r}",
    )


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


# ---------------------------------------------------------------------------
# Workbench — read/config, overrides, alias, boundary, and rerun routes
# (DD-combined-walks-2b-2d-workbench).  All new routes are under
# /api/pipeline/workbench/... and validate ownership/revision through the S1
# Workbench domain service.  Contention (ConcurrentTransactionError from a
# BEGIN IMMEDIATE write) propagates to the app-level 503 + Retry-After handler.
# ---------------------------------------------------------------------------

# Workbench-native walk modules (2b/2c/2d) — the only rerun targets.
_WORKBENCH_WALK_NAMES = frozenset(
    {
        "walk_2b_character_discovery",
        "walk_2c_alias_resolution",
        "walk_2d_scene_presence",
    }
)

# Downstream invalidation DAG for rerun reconciliation.
_RERUN_INVALIDATION: dict[str, list[str]] = {
    "walk_2b_character_discovery": ["walk_2c_alias_resolution", "walk_2d_scene_presence"],
    "walk_2c_alias_resolution": ["walk_2d_scene_presence"],
    "walk_2d_scene_presence": [],
}


# ---------------------------------------------------------------------------
# Workbench request DTOs
# ---------------------------------------------------------------------------


class WorkbenchOverrideWriteRequest(BaseModel):
    """Request body for PUT /workbench/{book_id}/overrides."""

    walk_name: str
    key: str
    value: object
    base_revision: int


class WorkbenchOverrideDeleteRequest(BaseModel):
    """Request body for DELETE /workbench/{book_id}/overrides."""

    walk_name: str
    key: str
    base_revision: int


class AliasPreviewRequest(BaseModel):
    """Request body for POST /workbench/{book_id}/alias-conversions/preview."""

    canonical_id: str
    member_ids: list[str]
    base_revision: int


class AliasCommitRequest(BaseModel):
    """Request body for POST /workbench/{book_id}/alias-conversions/commit."""

    preview_token: str
    base_revision: int
    confirm_consequences: bool = False


class WorkbenchRerunRequest(BaseModel):
    """Request body for POST /workbench/{book_id}/reruns."""

    walk_name: str
    scope: str = "book"
    scene_ids: list[str] | None = None
    preserve_manual_decisions: bool = True
    base_revision: int


class WorkbenchBoundaryAnchor(BaseModel):
    """Stable boundary anchor — at least one id must be non-null/reachable."""

    chapter_id: str | None = None
    scene_id: str | None = None
    paragraph_id: str | None = None


class WorkbenchBoundaryPayload(BaseModel):
    """Boundary operation payload."""

    operation: str
    boundary_offsets: list[int]
    label: str | None = None


class WorkbenchBoundaryOverrideWriteRequest(BaseModel):
    """Request body for PUT /workbench/{book_id}/boundary-overrides."""

    override_id: str | None = None
    anchor: WorkbenchBoundaryAnchor
    payload: WorkbenchBoundaryPayload
    base_revision: int


class WorkbenchBoundaryDeleteRequest(BaseModel):
    """Request body for DELETE /workbench/{book_id}/boundary-overrides/{id}."""

    base_revision: int


# ---------------------------------------------------------------------------
# Workbench read model construction
# ---------------------------------------------------------------------------


def _build_scene_hierarchy(storage: PipelineStorage, book_id: str) -> list[dict]:
    """Normalized chapters -> scenes -> paragraphs -> spans for *book_id*.

    Uses only durable IDs and edge positions (never presentation index as
    identity).  Returns a list of chapter dicts, each with ``scene`` children.
    """
    chapters = storage.execute_query(
        """SELECT ch.id AS chapter_id, bc.position AS chapter_position
           FROM book_chapter bc
           JOIN chapter ch ON bc.child_id = ch.id
           WHERE bc.parent_id = ?
           ORDER BY bc.position, ch.id""",
        (book_id,),
    )
    hierarchy: list[dict] = []
    for chapter in chapters:
        scenes = storage.execute_query(
            """SELECT sc.id AS scene_id, csc.position AS scene_position
               FROM chapter_scene csc
               JOIN scene sc ON csc.child_id = sc.id
               WHERE csc.parent_id = ?
               ORDER BY csc.position, sc.id""",
            (chapter["chapter_id"],),
        )
        scene_nodes: list[dict] = []
        for scene in scenes:
            paragraphs = storage.execute_query(
                """SELECT p.id AS paragraph_id, sp.position AS paragraph_position
                   FROM scene_paragraph sp
                   JOIN paragraph p ON sp.child_id = p.id
                   WHERE sp.parent_id = ?
                   ORDER BY sp.position, p.id""",
                (scene["scene_id"],),
            )
            paragraph_nodes: list[dict] = []
            for paragraph in paragraphs:
                spans = storage.execute_query(
                    """SELECT s.id AS span_id, s.span_type, s.text, s.instruct,
                              ps.position AS span_position
                       FROM paragraph_span ps
                       JOIN span s ON ps.child_id = s.id
                       WHERE ps.parent_id = ?
                       ORDER BY ps.position, s.id""",
                    (paragraph["paragraph_id"],),
                )
                paragraph_nodes.append(
                    {
                        "paragraph_id": paragraph["paragraph_id"],
                        "position": paragraph["paragraph_position"],
                        "spans": spans,
                    }
                )
            scene_nodes.append(
                {
                    "scene_id": scene["scene_id"],
                    "position": scene["scene_position"],
                    "paragraphs": paragraph_nodes,
                }
            )
        hierarchy.append(
            {
                "chapter_id": chapter["chapter_id"],
                "position": chapter["chapter_position"],
                "scenes": scene_nodes,
            }
        )
    return hierarchy


def _build_aliases(storage: PipelineStorage, book_id: str) -> list[dict]:
    """Active alias merges for *book_id* (projected with canonical/member names)."""
    rows = storage.execute_query(
        """SELECT m.merge_id, m.decision_id, m.canonical_id, m.member_id, m.status,
                  m.merge_revision, m.created_ms,
                  cc.name AS canonical_name, mc.name AS member_name
           FROM character_alias_merge m
           JOIN character cc ON cc.id = m.canonical_id
           JOIN character mc ON mc.id = m.member_id
           WHERE m.book_id = ?
           ORDER BY m.created_ms, m.merge_id""",
        (book_id,),
    )
    return rows


def _build_state(
    workbench: Workbench,
    storage: PipelineStorage,
    book_id: str,
    review_manager=None,
) -> dict:
    """Build the normalized WorkbenchStateDTO read model for *book_id*."""
    generation = workbench.get_generation(book_id)
    review_items = (
        review_manager.get_review_items(book_id) if review_manager is not None else []
    )
    runs = storage.execute_query(
        "SELECT run_id, walk_name, status, heartbeat_ms, created_ms, "
        "finished_ms, error FROM walk_run WHERE book_id = ? ORDER BY created_ms DESC",
        (book_id,),
    )
    effective_config: dict = {}
    for walk_name in sorted(_WORKBENCH_WALK_NAMES):
        effective_config[walk_name] = workbench.resolve_effective_config(
            book_id, walk_name
        )
    return {
        "book_id": book_id,
        "generation_revision": (generation["revision"] if generation else 0),
        "scenes": _build_scene_hierarchy(storage, book_id),
        "characters": storage.execute_query(
            "SELECT id, name, aliases, voice_assignment_id, description "
            "FROM character WHERE id IN ("
            "  SELECT character_id FROM character_scene_manual WHERE book_id = ?"
            "  UNION SELECT character_id FROM character_scene_generated WHERE book_id = ?"
            ") ORDER BY name, id",
            (book_id, book_id),
        ),
        "aliases": _build_aliases(storage, book_id),
        "presence": workbench.get_presence(book_id),
        "review_items": review_items,
        "overrides": workbench.get_overrides(book_id),
        "effective_config": effective_config,
        "conflicts": workbench.get_conflicts(book_id),
        "runs": runs,
    }


# ---------------------------------------------------------------------------
# GET /api/pipeline/workbench/{book_id}
# ---------------------------------------------------------------------------


@router.get("/workbench/{book_id}")
async def get_workbench_state(
    book_id: str,
    workbench: Workbench = Depends(get_workbench),
    storage: PipelineStorage = Depends(get_storage),
    manager=Depends(get_review_manager),
) -> dict:
    """Return the normalized workbench read model for *book_id*."""
    _guard(workbench.require_book, book_id)
    return _build_state(workbench, storage, book_id, manager)


# ---------------------------------------------------------------------------
# GET /api/pipeline/workbench/{book_id}/config
# ---------------------------------------------------------------------------


@router.get("/workbench/{book_id}/config")
async def get_workbench_config(
    book_id: str,
    workbench: Workbench = Depends(get_workbench),
) -> dict:
    """Return per-walk effective configuration plus raw override source data."""
    _guard(workbench.require_book, book_id)
    db_overrides = workbench.get_overrides(book_id)
    effective: dict = {}
    source: dict = {}
    for walk_name in sorted(_WORKBENCH_WALK_NAMES):
        resolved = workbench.resolve_effective_config(book_id, walk_name)
        effective[walk_name] = resolved["values"]
        source[walk_name] = resolved["sources"]
    # Validation is a no-op pass: values are already validated by the domain.
    return {
        "global": None,
        "task_overrides": {},
        "top_level_walk_override": {},
        "db_overrides": db_overrides,
        "effective": effective,
        "source": source,
        "validation_errors": [],
    }


# ---------------------------------------------------------------------------
# PUT / DELETE /api/pipeline/workbench/{book_id}/overrides
# ---------------------------------------------------------------------------


@router.put("/workbench/{book_id}/overrides")
async def put_workbench_override(
    book_id: str,
    request: WorkbenchOverrideWriteRequest,
    workbench: Workbench = Depends(get_workbench),
) -> dict:
    """Set a per-book workbench walk override (typed, revision-checked)."""
    return _guard(
        workbench.put_override,
        book_id=book_id,
        walk_name=request.walk_name,
        key=request.key,
        value=request.value,
        base_revision=request.base_revision,
    )


@router.delete("/workbench/{book_id}/overrides")
async def delete_workbench_override(
    book_id: str,
    request: WorkbenchOverrideDeleteRequest,
    workbench: Workbench = Depends(get_workbench),
) -> dict:
    """Remove a per-book workbench walk override (revision-checked)."""
    return _guard(
        workbench.delete_override,
        book_id=book_id,
        walk_name=request.walk_name,
        key=request.key,
        base_revision=request.base_revision,
    )


# ---------------------------------------------------------------------------
# POST /api/pipeline/workbench/{book_id}/alias-conversions/preview
# ---------------------------------------------------------------------------


@router.post("/workbench/{book_id}/alias-conversions/preview")
async def preview_alias_conversion(
    book_id: str,
    request: AliasPreviewRequest,
    workbench: Workbench = Depends(get_workbench),
) -> dict:
    """Create a book-scoped, single-use, ten-minute alias-conversion preview."""
    return _guard(
        workbench.preview_alias_conversion,
        book_id=book_id,
        canonical_id=request.canonical_id,
        member_ids=request.member_ids,
        base_revision=request.base_revision,
    )


# ---------------------------------------------------------------------------
# POST /api/pipeline/workbench/{book_id}/alias-conversions/commit
# ---------------------------------------------------------------------------


@router.post("/workbench/{book_id}/alias-conversions/commit")
async def commit_alias_conversion(
    book_id: str,
    request: AliasCommitRequest,
    workbench: Workbench = Depends(get_workbench),
) -> dict:
    """Commit a previewed alias conversion, applying exactly its row set."""
    return _guard(
        workbench.commit_alias_conversion,
        book_id=book_id,
        preview_token=request.preview_token,
        base_revision=request.base_revision,
        confirm_consequences=request.confirm_consequences,
    )


# ---------------------------------------------------------------------------
# POST /api/pipeline/workbench/{book_id}/reruns
# ---------------------------------------------------------------------------


@router.post("/workbench/{book_id}/reruns")
async def rerun_workbench_walk(
    book_id: str,
    request: WorkbenchRerunRequest,
    workbench: Workbench = Depends(get_workbench),
    storage: PipelineStorage = Depends(get_storage),
    runner: WalkRunner = Depends(get_walk_runner),
) -> dict:
    """Explicit book- or scenes-scoped rerun of a 2b/2c/2d walk.

    Validates ownership, revision, walk name, and scope; allocates a fresh
    generation revision (BEGIN IMMEDIATE — the contention point), then executes
    one run and returns the contracted rerun DTO.
    """
    # --- validation (workbench ValidationError -> 422) ----------------------
    _guard(workbench.require_book, book_id)
    if request.walk_name not in _WORKBENCH_WALK_NAMES:
        raise _wb_http(
            WorkbenchValidationError(
                f"Rerun target must be one of {sorted(_WORKBENCH_WALK_NAMES)}"
            )
        )
    if request.scope not in ("book", "scenes"):
        raise _wb_http(
            WorkbenchValidationError("scope must be exactly 'book' or 'scenes'")
        )
    if request.scope == "scenes":
        if request.walk_name == "walk_2c_alias_resolution":
            raise _wb_http(
                WorkbenchValidationError(
                    "walk_2c_alias_resolution is book-global and rejects scenes scope"
                )
            )
        scene_ids = request.scene_ids or []
        if not scene_ids:
            raise _wb_http(
                WorkbenchValidationError("scenes scope requires a non-empty scene_ids")
            )
        # Every listed scene must be reachable by this book.
        placeholders = ", ".join("?" for _ in scene_ids)
        reachable = storage.execute_query(
            f"""SELECT cs.child_id AS scene_id
                FROM chapter_scene cs
                JOIN book_chapter bc ON bc.child_id = cs.parent_id
                WHERE bc.parent_id = ? AND cs.child_id IN ({placeholders})""",
            (book_id, *scene_ids),
        )
        reachable_ids = {r["scene_id"] for r in reachable}
        missing = [s for s in scene_ids if s not in reachable_ids]
        if missing:
            raise _wb_http(
                WorkbenchValidationError(
                    f"scene_ids not reachable from book: {missing}"
                )
            )

    # --- revision gate + revision allocation (503 under contention) ---------
    _guard(workbench.check_revision, book_id, request.base_revision)
    generation_revision = _guard(workbench.allocate_revision, book_id)

    # --- execute the run -----------------------------------------------------
    config: dict = {
        "preserve_manual_decisions": request.preserve_manual_decisions,
        "scope": request.scope,
    }
    if request.scene_ids:
        config["scene_ids"] = request.scene_ids
    result = runner.run_walk(request.walk_name, book_id, config)
    status = result.get("status", "failed") if isinstance(result, dict) else "failed"

    # Resolve the run_id from the most recent walk_run row for this book/walk.
    rows = storage.execute_query(
        "SELECT run_id FROM walk_run WHERE book_id = ? AND walk_name = ? "
        "ORDER BY created_ms DESC, run_id DESC LIMIT 1",
        (book_id, request.walk_name),
    )
    run_id = rows[0]["run_id"] if rows else None

    return {
        "run_id": run_id,
        "status": status,
        "scope": request.scope,
        "invalidated_walks": _RERUN_INVALIDATION.get(request.walk_name, []),
        "generation_revision": generation_revision,
    }


# ---------------------------------------------------------------------------
# GET|PUT /api/pipeline/workbench/{book_id}/boundary-overrides
# ---------------------------------------------------------------------------


@router.get("/workbench/{book_id}/boundary-overrides")
async def get_boundary_overrides(
    book_id: str,
    workbench: Workbench = Depends(get_workbench),
) -> list[dict]:
    """Return active boundary override DTOs for *book_id*."""
    _guard(workbench.require_book, book_id)
    return workbench.get_boundary_overrides(book_id)


@router.put("/workbench/{book_id}/boundary-overrides")
async def put_boundary_override(
    book_id: str,
    request: WorkbenchBoundaryOverrideWriteRequest,
    workbench: Workbench = Depends(get_workbench),
) -> dict:
    """Create or replace a boundary override for *book_id*."""
    return _guard(
        workbench.put_boundary_override,
        book_id=book_id,
        override_id=request.override_id,
        anchor=request.anchor.model_dump(exclude_none=True),
        payload=request.payload.model_dump(exclude_none=True),
        base_revision=request.base_revision,
    )


# ---------------------------------------------------------------------------
# POST /api/pipeline/workbench/{book_id}/boundary-overrides/{override_id}/apply
# ---------------------------------------------------------------------------


@router.post("/workbench/{book_id}/boundary-overrides/{override_id}/apply")
async def apply_boundary_override(
    book_id: str,
    override_id: str,
    workbench: Workbench = Depends(get_workbench),
) -> dict:
    """Apply an active boundary override, recording the effective decision."""
    return _guard(workbench.apply_boundary_override, book_id=book_id, override_id=override_id)


# ---------------------------------------------------------------------------
# DELETE /api/pipeline/workbench/{book_id}/boundary-overrides/{override_id}
# ---------------------------------------------------------------------------


@router.delete("/workbench/{book_id}/boundary-overrides/{override_id}")
async def deactivate_boundary_override(
    book_id: str,
    override_id: str,
    request: WorkbenchBoundaryDeleteRequest,
    workbench: Workbench = Depends(get_workbench),
) -> dict:
    """Deactivate a boundary override (path id authoritative, revision-checked)."""
    return _guard(
        workbench.deactivate_boundary_override,
        book_id=book_id,
        override_id=override_id,
        base_revision=request.base_revision,
    )


# ---------------------------------------------------------------------------
# Prompt/settings config (DD-voice-persona-prompt-parity, S3).
# PipelineWalkPromptConfigRevisionAPI.v1 — do not conflate with the protected
# /workbench/{book_id}/config combined-workbench route above.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Prompt config request DTOs
# ---------------------------------------------------------------------------


class PromptConfigWriteRequest(BaseModel):
    """Request body for POST /walks/{book_id}/config/validate and /revisions."""

    task: str
    settings: dict = {}
    prompt: str | None = None
    raw_json: str | None = None
    base_revision: str | None = None


def get_prompt_config_domain(
    storage: PipelineStorage = Depends(get_storage),
) -> PromptConfigDomain:
    """FastAPI dependency: return the prompt-config domain facade.

    Stateless over ``PipelineStorage``; constructed fresh per request (matching
    ``get_persona_domain``) so there is no cross-request module state, and
    tests can override ``get_storage`` directly.
    """
    return PromptConfigDomain(storage)


def _prompt_http(exc) -> HTTPException:
    """Map prompt-config domain errors to contracted HTTP statuses.

    The 409 stale ``base_revision`` branch returns the structured
    ``RevisionConflictDTO`` body (P6 amendment); ``Retry-After`` stays
    503-contention only.
    """
    if isinstance(exc, BookNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, StaleRevisionError):
        return revision_conflict_http(
            code=CODE_STALE,
            message=str(exc),
        )
    if isinstance(exc, ValidationError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


# ---------------------------------------------------------------------------
# GET /api/pipeline/walks/{book_id}/config
# ---------------------------------------------------------------------------


@router.get("/walks/{book_id}/config")
async def get_effective_walk_config(
    book_id: str,
    domain: PromptConfigDomain = Depends(get_prompt_config_domain),
) -> dict:
    """Return effective values + provenance for all nine walk tasks.

    Precedence: on-disk config -> ``llm.task_overrides`` -> DB ``walk_override``
    (DB wins).  DB prompt wins only when a non-empty string; temperature 0.0
    is honored.  Unknown book -> 404.
    """
    try:
        return domain.effective_config(book_id)
    except BookNotFoundError as exc:
        raise _prompt_http(exc) from exc


# ---------------------------------------------------------------------------
# POST /api/pipeline/walks/{book_id}/config/validate
# ---------------------------------------------------------------------------


@router.post("/walks/{book_id}/config/validate")
async def validate_prompt_config(
    book_id: str,
    request: PromptConfigWriteRequest,
    domain: PromptConfigDomain = Depends(get_prompt_config_domain),
) -> dict:
    """Side-effect-free validation of a prompt-config write."""
    # Book ownership is checked for scope parity, but validation itself is
    # a read-only no-op over the write.
    try:
        domain.require_book(book_id)
    except BookNotFoundError as exc:
        raise _prompt_http(exc) from exc
    return domain.validate(request.model_dump())


# ---------------------------------------------------------------------------
# POST /api/pipeline/walks/{book_id}/config/revisions
# ---------------------------------------------------------------------------


@router.post(
    "/walks/{book_id}/config/revisions", status_code=201
)
async def save_prompt_config_revision(
    book_id: str,
    request: PromptConfigWriteRequest,
    domain: PromptConfigDomain = Depends(get_prompt_config_domain),
) -> dict:
    """Append a prompt-config revision, applying allowed overrides atomically.

    Stale/cross-book base_revision -> 409; unknown book -> 404; invalid
    task/key/value/raw_json -> 422; transaction contention -> 503 +
    ``Retry-After`` (app-level handler).
    """
    try:
        return domain.save(
            book_id,
            write=request.model_dump(),
            base_revision=request.base_revision,
        )
    except (BookNotFoundError, StaleRevisionError, ValidationError) as exc:
        raise _prompt_http(exc) from exc


# ---------------------------------------------------------------------------
# POST /api/pipeline/walks/{book_id}/reruns
# (DD-voice-persona-prompt-parity, S4 — explicit scoped prompt rerun).
# ---------------------------------------------------------------------------

# Prompt task name -> workbench-native walk name for the combined-workbench
# invalidation DAG reuse.  Only these three tasks are 2b/2c/2d workbench walks.
_TASK_TO_WORKBENCH_WALK = {
    "character_discovery": "walk_2b_character_discovery",
    "script_alias_resolution": "walk_2c_alias_resolution",
    "scene_presence": "walk_2d_scene_presence",
}


class ScopedWalkRerunRequest(BaseModel):
    """Request body for POST /walks/{book_id}/reruns.

    A rerun is explicit and never implicit: it requires ``confirm=True`` plus a
    ``revision_id`` (an existing prompt-config revision to re-apply) and a
    ``scope`` (``book`` or ``scenes`` with reachable ``scene_ids``).  The task
    is derived from the referenced revision (a revision is per ``(book, task)``).
    """

    revision_id: str
    scope: str = "book"
    scene_ids: list[str] = []
    confirm: bool = False


def _prompt_override_value(
    storage: PipelineStorage, book_id: str, task: str, key: str
) -> object:
    """Return the decoded ``walk_override`` value for ``(book_id, task, key)``."""
    rows = storage.execute_query(
        "SELECT value_json FROM walk_override"
        " WHERE book_id = ? AND walk_name = ? AND key = ?",
        (book_id, task, key),
    )
    if not rows:
        return None
    try:
        return json.loads(rows[0]["value_json"])
    except (json.JSONDecodeError, TypeError):
        return None


def _reachable_scene_ids(
    storage: PipelineStorage, book_id: str, scene_ids: list[str]
) -> set[str]:
    """Return the subset of *scene_ids* reachable from *book_id*."""
    placeholders = ", ".join("?" for _ in scene_ids)
    rows = storage.execute_query(
        f"""SELECT cs.child_id AS scene_id
            FROM chapter_scene cs
            JOIN book_chapter bc ON bc.child_id = cs.parent_id
            WHERE bc.parent_id = ? AND cs.child_id IN ({placeholders})""",
        (book_id, *scene_ids),
    )
    return {r["scene_id"] for r in rows}


@router.post("/walks/{book_id}/reruns")
async def rerun_scoped_walk(
    book_id: str,
    request: ScopedWalkRerunRequest,
    domain: PromptConfigDomain = Depends(get_prompt_config_domain),
    storage: PipelineStorage = Depends(get_storage),
) -> dict:
    """Explicit, confirmed, scoped re-application of a prompt-config revision.

    Requires ``confirm=true``, an existing ``revision_id`` owned by
    ``{book_id}``, and a valid ``scope``.  Scenes scope requires non-empty,
    reachable ``scene_ids``; ``script_alias_resolution`` is book-global and
    rejects scenes scope (reuses the combined-workbench 2c contract).  The
    referenced revision's settings are re-applied through the existing
    ``walk_override`` single-writer (via ``PromptConfigDomain.save``) as a new
    head revision (the ``run``), so an identical revision+scope rerun is
    rejected ``409 already_ran`` — never a silent duplicate.  Reruns never
    auto-run a walk (no auto-cascade).  For the 2b/2c/2d tasks the
    combined-workbench invalidation DAG is reused to report the downstream
    walks a later run would invalidate.
    """
    # Book ownership (404).
    try:
        domain.require_book(book_id)
    except BookNotFoundError as exc:
        raise _prompt_http(exc) from exc

    # Explicit confirmation + valid scope.
    if not request.confirm:
        raise HTTPException(status_code=422, detail="walk rerun requires confirm=true")
    if request.scope not in ("book", "scenes"):
        raise HTTPException(
            status_code=422, detail="scope must be exactly 'book' or 'scenes'"
        )

    # The rerun derives from an existing prompt-config revision owned by this book.
    row = storage.get_prompt_config_revision(request.revision_id)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown prompt-config revision '{request.revision_id}'",
        )
    if row["book_id"] != book_id:
        raise revision_conflict_http(
            code=CODE_CROSS_BOOK,
            message=(
                f"prompt-config revision '{request.revision_id}' belongs to book"
                f" '{row['book_id']}', not '{book_id}'"
            ),
            detail={
                "revision_id": request.revision_id,
                "revision_book_id": row["book_id"],
                "requested_book_id": book_id,
            },
        )
    task = row["task"]
    # Valid nine-task scope (a revision is per (book, task)).
    if task not in TASK_NAMES:
        raise HTTPException(
            status_code=422,
            detail=f"unknown task: {task}; must be one of {sorted(TASK_NAMES)}",
        )

    # Scenes scope: non-empty reachable scene ids; 2c (alias resolution) is
    # book-global.
    if request.scope == "scenes":
        if not request.scene_ids:
            raise HTTPException(
                status_code=422, detail="scenes scope requires a non-empty scene_ids"
            )
        if task == "script_alias_resolution":
            raise HTTPException(
                status_code=422,
                detail="script_alias_resolution is book-global and rejects scenes scope",
            )
        reachable = _reachable_scene_ids(storage, book_id, request.scene_ids)
        missing = [s for s in request.scene_ids if s not in reachable]
        if missing:
            raise HTTPException(
                status_code=422,
                detail=f"scene_ids not reachable from book: {missing}",
            )

    # Dedupe: re-applying a revision that is no longer the (book, task) head
    # means this exact revision+scope already ran — 409 already_ran, never a
    # silent duplicate.  The first rerun of a live head is legitimate and
    # produces a new head revision.
    head = domain.list_revisions(book_id, task)
    if head and head[0]["revision_id"] != request.revision_id:
        raise revision_conflict_http(
            code=CODE_ALREADY_RAN,
            message=(
                f"walk rerun already_ran: revision {request.revision_id} scope"
                f" {request.scope} produced head '{head[0]['revision_id']}'"
            ),
            detail={
                "book_id": book_id,
                "task": task,
                "revision_id": request.revision_id,
                "scope": request.scope,
                "head_revision_id": head[0]["revision_id"],
            },
        )

    # Re-apply the referenced revision's settings through the existing
    # walk_override single-writer, creating a new head revision (the run).
    settings = json.loads(row["settings_json"] or "{}")
    write = {
        "task": task,
        "settings": settings,
        "prompt": _prompt_override_value(storage, book_id, task, "prompt"),
    }
    try:
        saved = domain.save(book_id, write=write, base_revision=request.revision_id)
    except (BookNotFoundError, StaleRevisionError, ValidationError) as exc:
        raise _prompt_http(exc) from exc

    # Reuse the combined-workbench invalidation DAG for 2b/2c/2d targets.
    walk_name = _TASK_TO_WORKBENCH_WALK.get(task)
    invalidated = _RERUN_INVALIDATION.get(walk_name, []) if walk_name else []

    return {
        "run_id": saved["revision_id"],
        "revision_id": request.revision_id,
        "scope": request.scope,
        "invalidated_walks": invalidated,
    }
