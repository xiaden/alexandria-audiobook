"""Pipeline API — Operation endpoints.

Provides HTTP endpoints for structural operations on the document spine:
- POST /api/pipeline/operation — execute split/merge/move/delete operations
- PUT /api/pipeline/span/{span_id}/text — update a span's text

Plus Plan I snapshot-project CRUD (all auto-named, no free-form name input):
- POST /api/pipeline/projects — save an auto-named snapshot of the book's
  current spans/script state
- GET /api/pipeline/projects — list snapshots newest-first (optional book_id)
- DELETE /api/pipeline/projects/{name} — delete a snapshot
- PATCH /api/pipeline/projects/{name} — rename a snapshot (409 on duplicate)
- POST /api/pipeline/projects/load — restore a snapshot into its book
  (merge-vs-replace; 409 + Retry-After while a walk/render is active;
  re-render notice when the snapshot's audio is missing)

The single operation endpoint dispatches by the ``operation`` field in the
request body.  Snapshot restore semantics (POST /projects/load) are Plan I
phase 2 (implemented here).
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.pipeline.adapter import PipelineStorage
from app.pipeline.api_onboard import get_storage
from app.pipeline.assembly import export_annotated_script, get_book_version
from app.pipeline.operations import OperationExecutor
from app.pipeline.tts_integration import (
    get_render_root,
    validate_pause_ms,
)


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


# ---------------------------------------------------------------------------
# PUT /api/pipeline/span/{span_id}/text
# ---------------------------------------------------------------------------


class SpanTextUpdateRequest(BaseModel):
    """Request body for PUT /api/pipeline/span/{span_id}/text."""

    text: str = Field(..., description="New text for the span")


@router.put("/span/{span_id}/text")
async def update_span_text(
    span_id: str,
    request: SpanTextUpdateRequest,
    storage: PipelineStorage = Depends(get_storage),
) -> dict:
    """Update the text of a span identified by *span_id*.

    Returns ``{status: 'ok', span_id: str}`` on success.
    Raises 400 if *text* is empty after stripping whitespace.
    Raises 404 if no span with *span_id* exists.
    """
    text = request.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Span text must not be empty")

    # Verify span exists
    rows = storage.execute_query("SELECT id FROM span WHERE id = ?", (span_id,))
    if not rows:
        raise HTTPException(status_code=404, detail=f"Span '{span_id}' not found")

    storage.execute_update("UPDATE span SET text = ? WHERE id = ?", (text, span_id))
    return {"status": "ok", "span_id": span_id}


# ---------------------------------------------------------------------------
# Plan J — Book single-speaker flag
# GET/PUT /api/pipeline/book/{book_id}/single_speaker
#
# The flag is a UI write of book.single_speaker (CONTRACTS decision #9):
# enforcement happens ONLY at the render boundary (tts_integration
# ``_enforce_single_speaker``), so toggling it never mutates the script and
# the annotated export stays faithful. Column default is 0 (multi-speaker).
# ---------------------------------------------------------------------------


class SingleSpeakerUpdateRequest(BaseModel):
    """Request body for PUT /api/pipeline/book/{book_id}/single_speaker."""

    single_speaker: bool = Field(
        ...,
        description="Force NARRATOR for every span at render time",
    )


@router.get("/book/{book_id}/single_speaker")
async def get_book_single_speaker(
    book_id: str,
    storage: PipelineStorage = Depends(get_storage),
) -> dict:
    """Return the book's single-speaker render flag as ``0`` or ``1``.

    Raises 404 if no book with *book_id* exists. An unset flag reads back as
    off (column default 0).
    """
    rows = storage.execute_query(
        "SELECT single_speaker FROM book WHERE id = ?", (book_id,)
    )
    if not rows:
        raise HTTPException(status_code=404, detail=f"Book '{book_id}' not found")

    return {"book_id": book_id, "single_speaker": 1 if rows[0]["single_speaker"] else 0}


@router.put("/book/{book_id}/single_speaker")
async def update_book_single_speaker(
    book_id: str,
    request: SingleSpeakerUpdateRequest,
    storage: PipelineStorage = Depends(get_storage),
) -> dict:
    """Persist the book's single-speaker render flag (``0`` or ``1``).

    Raises 404 if no book with *book_id* exists. Parameterized SQL only —
    *book_id* is user-supplied.
    """
    rows = storage.execute_query("SELECT id FROM book WHERE id = ?", (book_id,))
    if not rows:
        raise HTTPException(status_code=404, detail=f"Book '{book_id}' not found")

    value = 1 if request.single_speaker else 0
    storage.execute_update(
        "UPDATE book SET single_speaker = ? WHERE id = ?", (value, book_id)
    )
    return {"status": "ok", "book_id": book_id, "single_speaker": value}


# ---------------------------------------------------------------------------
# Plan L (P2-S2) — Book pause settings + per-span pause-after override
#
# GET/PUT /api/pipeline/book/{book_id}/pause_settings
#   book.pause_between_speakers_ms / book.pause_same_speaker_ms are nullable
#   INTEGER overrides: NULL means "resolve the default"; 0 means "intentional
#   no-gap override".  Never coerce NULL to 0.  PUT applies only the fields
#   the caller explicitly provides (partial update); an explicit null clears
#   the override back to resolve-default.
#
# GET/PUT /api/pipeline/span/{span_id}/pause_after
#   span.pause_after_ms (nullable INTEGER, CHECK NULL OR >= 0): NULL clears
#   the override, 0 is an intentional no-gap, any other value is validated by
#   ``validate_pause_ms``.  Containment check: the span must be reachable from
#   a book through the spine edges (mirrors the snapshot enrichment join) so
#   unknown/orphan spans are rejected with 404.
#
# All SQL is parameterized (book_id/span_id are user-supplied); writes ride
# the adapter's transaction() owner-thread discipline (ConcurrentTransactionError
# -> 503 + Retry-After at the app layer).  No new modules — these live in the
# existing api_operations router beside update_span_text / single_speaker.
# ---------------------------------------------------------------------------


class PauseSettingsUpdateRequest(BaseModel):
    """Request body for PUT /api/pipeline/book/{book_id}/pause_settings.

    Both fields are nullable and optional.  A field that is present in the
    payload is applied (``None`` clears the override to resolve-default, ``0``
    is an intentional no-gap, other values are validated by
    ``validate_pause_ms``).  A field absent from the payload is left untouched.
    """

    pause_between_speakers_ms: Optional[int] = None
    pause_same_speaker_ms: Optional[int] = None

    @field_validator("pause_between_speakers_ms", "pause_same_speaker_ms", mode="before")
    @classmethod
    def _validate_pause_field(cls, value):
        if value is None:
            return None
        return validate_pause_ms(value)


class SpanPauseUpdateRequest(BaseModel):
    """Request body for PUT /api/pipeline/span/{span_id}/pause_after.

    ``pause_after_ms`` is nullable: ``None`` clears the per-span override,
    ``0`` is an intentional no-gap, any other value is validated by
    ``validate_pause_ms`` (bounded by PAUSE_MAX_MS).
    """

    pause_after_ms: Optional[int] = None

    @field_validator("pause_after_ms", mode="before")
    @classmethod
    def _validate_pause_after(cls, value):
        if value is None:
            return None
        return validate_pause_ms(value)


# Per-book span containment query: a span must be reachable from a book through
# the spine edge chain so an unknown or orphan span is rejected with 404.
_SPAN_IN_BOOK_SQL = (
    "SELECT span.id FROM span"
    " JOIN paragraph_span AS span_edge ON span.id = span_edge.child_id"
    " JOIN scene_paragraph AS paragraph_edge"
    "     ON span_edge.parent_id = paragraph_edge.child_id"
    " JOIN chapter_scene AS scene_edge"
    "     ON paragraph_edge.parent_id = scene_edge.child_id"
    " JOIN book_chapter AS chapter_edge"
    "     ON scene_edge.parent_id = chapter_edge.child_id"
    " JOIN book ON chapter_edge.parent_id = book.id"
    " WHERE span.id = ? LIMIT 1"
)


@router.get("/book/{book_id}/pause_settings")
async def get_book_pause_settings(
    book_id: str,
    storage: PipelineStorage = Depends(get_storage),
) -> dict:
    """Return the book's pause override columns (NULL = resolve default).

    Raises 404 if no book with *book_id* exists.  Reads the raw nullable
    columns — ``None`` is reported as ``None`` (never coerced to a default).
    """
    rows = storage.execute_query(
        "SELECT pause_between_speakers_ms, pause_same_speaker_ms FROM book"
        " WHERE id = ?",
        (book_id,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail=f"Book '{book_id}' not found")

    return {
        "book_id": book_id,
        "pause_between_speakers_ms": rows[0]["pause_between_speakers_ms"],
        "pause_same_speaker_ms": rows[0]["pause_same_speaker_ms"],
    }


@router.put("/book/{book_id}/pause_settings")
async def update_book_pause_settings(
    book_id: str,
    request: PauseSettingsUpdateRequest,
    storage: PipelineStorage = Depends(get_storage),
) -> dict:
    """Persist the book's pause overrides (partial update on explicit fields).

    Raises 404 if no book with *book_id* exists; 422 if a provided value is
    invalid (via pydantic + ``validate_pause_ms``).  NULL persists as SQL NULL
    (resolve default); 0 persists as an intentional no-gap.  Returns the
    post-update persisted columns.
    """
    rows = storage.execute_query("SELECT id FROM book WHERE id = ?", (book_id,))
    if not rows:
        raise HTTPException(status_code=404, detail=f"Book '{book_id}' not found")

    set_clauses = []
    params: list = []
    for key in ("pause_between_speakers_ms", "pause_same_speaker_ms"):
        if key in request.model_fields_set:
            set_clauses.append(f"{key} = ?")
            params.append(getattr(request, key))
    if set_clauses:
        params.append(book_id)
        storage.execute_update(
            f"UPDATE book SET {', '.join(set_clauses)} WHERE id = ?", tuple(params)
        )

    current = storage.execute_query(
        "SELECT pause_between_speakers_ms, pause_same_speaker_ms FROM book"
        " WHERE id = ?",
        (book_id,),
    )[0]
    return {
        "status": "ok",
        "book_id": book_id,
        "pause_between_speakers_ms": current["pause_between_speakers_ms"],
        "pause_same_speaker_ms": current["pause_same_speaker_ms"],
    }


@router.get("/span/{span_id}/pause_after")
async def get_span_pause_after(
    span_id: str,
    storage: PipelineStorage = Depends(get_storage),
) -> dict:
    """Return the span's ``pause_after_ms`` override (NULL = resolve default).

    Raises 404 if no span with *span_id* exists or the span is not reachable
    from a book through the spine edges.
    """
    if not storage.execute_query(_SPAN_IN_BOOK_SQL, (span_id,)):
        raise HTTPException(status_code=404, detail=f"Span '{span_id}' not found")

    rows = storage.execute_query(
        "SELECT pause_after_ms FROM span WHERE id = ?", (span_id,)
    )
    return {
        "span_id": span_id,
        "pause_after_ms": rows[0]["pause_after_ms"],
    }


@router.put("/span/{span_id}/pause_after")
async def update_span_pause_after(
    span_id: str,
    request: SpanPauseUpdateRequest,
    storage: PipelineStorage = Depends(get_storage),
) -> dict:
    """Persist the span's ``pause_after_ms`` override.

    Raises 404 if the span is unknown or not reachable from a book; 422 if the
    provided value is invalid (via pydantic + ``validate_pause_ms``).  NULL
    clears the override to resolve-default; 0 is an intentional no-gap.
    """
    if not storage.execute_query(_SPAN_IN_BOOK_SQL, (span_id,)):
        raise HTTPException(status_code=404, detail=f"Span '{span_id}' not found")

    value = request.pause_after_ms
    storage.execute_update(
        "UPDATE span SET pause_after_ms = ? WHERE id = ?", (value, span_id)
    )
    return {"status": "ok", "span_id": span_id, "pause_after_ms": value}


# ---------------------------------------------------------------------------
# Plan I — Snapshot projects (auto-named; no free-form name input)
# ---------------------------------------------------------------------------


class CreateProjectRequest(BaseModel):
    """Request body for POST /api/pipeline/projects."""

    book_id: str = Field(..., description="Book to snapshot")


class RenameProjectRequest(BaseModel):
    """Request body for PATCH /api/pipeline/projects/{name}."""

    new_name: str = Field(..., description="New snapshot name")


class SnapshotLoadRequest(BaseModel):
    """Request body for POST /api/pipeline/projects/load."""

    name: str = Field(..., description="Snapshot to restore")
    book_id: str = Field(..., description="Book the snapshot belongs to")


_SNAPSHOT_NAME_MAX_LEN = 200
_AUTO_NAME_PREFIX = "Project "
_AUTO_NAME_FORMAT = "%Y-%m-%d %H:%M"

#: Restore is blocked while a walk/render row is in one of these statuses
#: (rule #10) — the same set reconciliation treats as alive.
_ACTIVE_RUN_STATUSES = ("pending", "running")

#: Seconds advertised in the 409 Retry-After header (no ConcurrentTransactionError
#: 503 mapping exists in the api layer yet, so we pick a reasonable value).
_RETRY_AFTER_SECONDS = 5

#: Current snapshot manifest schema version.  Load refuses manifests whose
#: ``schema_version`` does not match this — a future/unknown manifest shape
#: must never be merged blindly (hard 400, never a guess).
_SNAPSHOT_MANIFEST_SCHEMA_VERSION = 1

#: File suffixes that count as audio artifacts inside a run dir.
_AUDIO_SUFFIXES = (".wav", ".mp3", ".m4b")

#: Default per-snapshot payload cap in bytes (defense-in-depth: a snapshot
#: duplicates the book's full annotated script, so the cap bounds per-snapshot
#: disk amplification on very large books and repeated saves).  Mirrors the
#: Plan C ``PIPELINE_MAX_COVER_BYTES`` 20 MiB precedent; the SQLite TEXT column
#: itself has no practical size limit.
_DEFAULT_MAX_SNAPSHOT_JSON_BYTES = 20 * 1024 * 1024


def _max_snapshot_json_bytes() -> int:
    """Snapshot payload size cap in bytes, env-tunable like PIPELINE_MAX_COVER_BYTES."""
    try:
        return max(
            1,
            int(
                os.environ.get(
                    "PIPELINE_MAX_SNAPSHOT_BYTES",
                    str(_DEFAULT_MAX_SNAPSHOT_JSON_BYTES),
                )
            ),
        )
    except ValueError:
        return _DEFAULT_MAX_SNAPSHOT_JSON_BYTES


def _is_valid_snapshot_name(name: str) -> bool:
    """Return True when *name* is a safe snapshot identifier.

    Snapshot names must never be usable for path traversal: reject empty
    or whitespace-padded names, ``/`` and ``\\`` separators, ``..``,
    ``%`` (a literal percent sign is unaddressable via URL round-trip), and
    control characters.  Length is capped for storage hygiene.  This is
    the single validator for both auto-names (which always pass) and
    PATCH-rename input.
    """
    if not name or name != name.strip():
        return False
    if len(name) > _SNAPSHOT_NAME_MAX_LEN:
        return False
    if "/" in name or "\\" in name or ".." in name or "%" in name:
        return False
    return not any(ord(ch) < 32 or ord(ch) == 127 for ch in name)


def _auto_snapshot_name(created_ms: int, suffix: int = 0) -> str:
    """Generate an auto snapshot name from *created_ms* (unix ms).

    Base format is ``Project {YYYY-MM-DD HH:MM}`` (local time).  A ``(N)``
    suffix disambiguates snapshots created within the same minute — the
    name is the table PK, so a bare base name would collide.
    """
    stamp = datetime.fromtimestamp(created_ms / 1000).strftime(_AUTO_NAME_FORMAT)
    base = f"{_AUTO_NAME_PREFIX}{stamp}"
    return base if suffix == 0 else f"{base} ({suffix})"


@router.post("/projects")
async def create_project_snapshot(
    request: CreateProjectRequest,
    storage: PipelineStorage = Depends(get_storage),
) -> dict:
    """Save an auto-named snapshot of the book's current spans/script state.

    The name is generated server-side (never user-supplied): ``Project
    {YYYY-MM-DD HH:MM}`` with a ``(N)`` suffix when a same-minute name is
    already taken.  ``snapshot_json`` captures the annotated script in
    presentation order (span id / resolved speaker / text / instruct), the
    characters linked to the book (with their voice assignment), the
    current ``book.version`` as progress, and the run dir of the book's
    latest completed render as the audio reference (``audio_run_dir``,
    ``None`` when the book has never rendered).  Returns the ProjectSnapshot
    DTO ``{name, book_id, created_ms, size_bytes}`` where ``size_bytes`` is
    the UTF-8 byte length of ``snapshot_json`` as stored.
    """
    if not storage.execute_query(
        "SELECT id FROM book WHERE id = ?", (request.book_id,)
    ):
        raise HTTPException(
            status_code=404, detail=f"Book '{request.book_id}' not found"
        )

    created_ms = int(time.time() * 1000)
    script = export_annotated_script(request.book_id, storage)
    # Plan L (P1-S3): enrich each span entry with its nullable
    # ``pause_after_ms`` so the snapshot round-trips per-span overrides
    # without losing NULL (resolve default) versus 0 (intentional no-gap).
    # The script entries carry ``id``; a single parameterized query fetches
    # the pause column for every span of this book and we merge by id.
    span_pause_rows = storage.execute_query(
        "SELECT span.id, span.pause_after_ms FROM span"
        " JOIN paragraph_span AS span_edge ON span.id = span_edge.child_id"
        " JOIN scene_paragraph AS paragraph_edge"
        "     ON span_edge.parent_id = paragraph_edge.child_id"
        " JOIN chapter_scene AS scene_edge"
        "     ON paragraph_edge.parent_id = scene_edge.child_id"
        " JOIN book_chapter AS chapter_edge"
        "     ON scene_edge.parent_id = chapter_edge.child_id"
        " JOIN book ON chapter_edge.parent_id = book.id"
        " WHERE book.id = ?",
        (request.book_id,),
    )
    span_pause = {row["id"]: row["pause_after_ms"] for row in span_pause_rows}
    for entry in script:
        entry["pause_after_ms"] = span_pause.get(entry["id"])
    character_rows = storage.execute_query(
        "SELECT character.id, character.name, character.voice_assignment_id"
        " FROM character"
        " JOIN character_book ON character.id = character_book.character_id"
        " WHERE character_book.book_id = ?",
        (request.book_id,),
    )
    # Plan L (P1-S3): book-level pause override columns (NULL = resolve
    # default; 0 = intentional no-gap) ride in the manifest so a snapshot
    # round-trips the project's pause settings verbatim.
    book_pause_rows = storage.execute_query(
        "SELECT pause_between_speakers_ms, pause_same_speaker_ms"
        " FROM book WHERE id = ?",
        (request.book_id,),
    )
    book_pause = book_pause_rows[0] if book_pause_rows else {}
    manifest = {
        "schema_version": 1,
        "book_id": request.book_id,
        "book_version": get_book_version(request.book_id, storage),
        "created_ms": created_ms,
        "spans": script,
        "characters": character_rows,
        "audio_run_dir": _latest_completed_run_dir(storage, request.book_id),
        "pause_between_speakers_ms": book_pause.get("pause_between_speakers_ms"),
        "pause_same_speaker_ms": book_pause.get("pause_same_speaker_ms"),
    }
    snapshot_json = json.dumps(manifest, ensure_ascii=False, sort_keys=True)

    # Defensive size cap (security review, Plan I P5-S4): reject oversized
    # snapshots instead of duplicating a huge script into the DB.  The
    # manifest is bounded by the book's own content, so this only trips on
    # pathologically large books / amplification — 400 matches the Plan C
    # cover-upload cap convention (PIPELINE_MAX_COVER_BYTES).
    max_bytes = _max_snapshot_json_bytes()
    # The cap is a BYTE limit: len(str) counts characters, so a manifest
    # full of multi-byte (CJK) characters would be ~3-4x looser than named.
    if len(snapshot_json.encode("utf-8")) > max_bytes:
        raise HTTPException(
            status_code=400,
            detail=(
                "Snapshot too large: the book's script state exceeds the "
                f"{max_bytes} byte snapshot limit (PIPELINE_MAX_SNAPSHOT_BYTES)"
            ),
        )

    # Name is the PK: on a same-minute collision (IntegrityError) retry with
    # an incrementing ``(N)`` suffix.  Single-process SQLite makes the loop
    # race-free.
    name = _auto_snapshot_name(created_ms)
    suffix = 1
    while True:
        try:
            storage.create_project_snapshot(
                name, request.book_id, snapshot_json, created_ms
            )
            break
        except sqlite3.IntegrityError:
            name = _auto_snapshot_name(created_ms, suffix)
            suffix += 1

    return {
        "name": name,
        "book_id": request.book_id,
        "created_ms": created_ms,
        "size_bytes": len(snapshot_json.encode("utf-8")),
    }


@router.get("/projects")
async def list_project_snapshots(
    book_id: str | None = None,
    storage: PipelineStorage = Depends(get_storage),
) -> list[dict]:
    """List saved snapshots, newest first (``created_ms`` DESC).

    Each item is the ProjectSnapshot DTO ``{name, book_id, created_ms,
    size_bytes}``; ``size_bytes`` is the UTF-8 byte length of
    ``snapshot_json``.  An optional ``book_id`` query parameter filters to
    one book.
    """
    rows = storage.list_project_snapshots(book_id=book_id)
    return [
        {
            "name": row["name"],
            "book_id": row["book_id"],
            "created_ms": row["created_ms"],
            "size_bytes": len(row["snapshot_json"].encode("utf-8")),
        }
        for row in rows
    ]


@router.delete("/projects/{name}")
async def delete_project_snapshot(
    name: str,
    storage: PipelineStorage = Depends(get_storage),
) -> dict:
    """Delete the snapshot named *name*; 404 when it does not exist."""
    if not storage.delete_project_snapshot(name):
        raise HTTPException(status_code=404, detail=f"Snapshot '{name}' not found")
    return {"status": "ok", "name": name}


@router.patch("/projects/{name}")
async def rename_project_snapshot(
    name: str,
    request: RenameProjectRequest,
    storage: PipelineStorage = Depends(get_storage),
) -> dict:
    """Rename the snapshot *name* to ``request.new_name``.

    - 400 when *new_name* fails snapshot-name validation
      (empty / whitespace / ``/`` ``\\`` / ``..`` ``%`` / control characters)
    - 404 when *name* does not exist
    - 409 when *new_name* is already taken (name is the PK)
    - 200 no-op when renaming a snapshot to its own name
    """
    if not _is_valid_snapshot_name(request.new_name):
        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid snapshot name: must be non-empty, at most "
                f"{_SNAPSHOT_NAME_MAX_LEN} chars, and contain no '/', '\\\\', "
                "'..', '%', or control characters"
            ),
        )
    if request.new_name == name:
        return {"status": "ok", "name": name}

    if storage.get_project_snapshot(name) is None:
        raise HTTPException(status_code=404, detail=f"Snapshot '{name}' not found")
    if storage.get_project_snapshot(request.new_name) is not None:
        raise HTTPException(
            status_code=409, detail=f"Snapshot '{request.new_name}' already exists"
        )

    storage.rename_project_snapshot(name, request.new_name)
    return {"status": "ok", "name": request.new_name}


# ---------------------------------------------------------------------------
# Snapshot restore (Plan I phase 2) — POST /api/pipeline/projects/load
# ---------------------------------------------------------------------------


def _book_has_active_runs(storage: PipelineStorage, book_id: str) -> bool:
    """Return True when any walk_run/render_job row for *book_id* is active.

    Restore is blocked while a walk or render is in flight for the book
    (contract rule #10): merging span/character state under an active run
    would corrupt the rows and files that run is producing.  The active
    statuses are ``pending`` and ``running`` — the same set reconciliation
    treats as alive (``pending`` rows are queued work, ``running`` rows are
    in-flight; everything else is terminal).  Rows are the truth: the
    check is a plain existence query via the adapter.
    """
    for table in ("walk_run", "render_job"):
        rows = storage.execute_query(
            f"SELECT 1 FROM {table} WHERE book_id = ?"
            " AND status IN ('pending', 'running') LIMIT 1",
            (book_id,),
        )
        if rows:
            return True
    return False


def _latest_completed_run_dir(storage: PipelineStorage, book_id: str) -> str | None:
    """Return the run dir of *book_id*'s latest completed render job.

    ``None`` when the book has never rendered to completion.  This is the
    snapshot's audio reference: it is recorded into the manifest at save
    time so a later restore can report whether the audio that matched the
    saved state is still present.
    """
    rows = storage.execute_query(
        "SELECT output_dir FROM render_job"
        " WHERE book_id = ? AND status = 'completed'"
        " ORDER BY finished_ms DESC LIMIT 1",
        (book_id,),
    )
    return rows[0]["output_dir"] if rows else None


def _audio_reference_missing(run_dir: str | None) -> bool:
    """Return True when the snapshot's referenced audio is absent.

    The snapshot records the run dir of the book's latest completed render
    at save time (``manifest.audio_run_dir``).  The referenced audio is
    "present" when that run dir still exists under ``RENDER_ROOT`` and
    contains audio files.  A ``None`` reference (never rendered), a dir
    outside ``RENDER_ROOT`` (defensive: never trust a manifest path), a
    missing dir (e.g. GC'd), or an empty dir all mean the user must
    re-render.
    """
    if not run_dir:
        return True
    render_root = os.path.abspath(get_render_root())
    abs_dir = os.path.abspath(run_dir)
    if abs_dir != render_root and not abs_dir.startswith(render_root + os.sep):
        return True
    if not os.path.isdir(abs_dir):
        return True
    return not any(
        name.endswith(_AUDIO_SUFFIXES) and os.path.isfile(os.path.join(abs_dir, name))
        for name in os.listdir(abs_dir)
    )


def _apply_snapshot_merge(
    storage: PipelineStorage, manifest: dict, book_id: str
) -> None:
    """Apply the snapshot's state onto the current book (merge-vs-replace).

    Runs inside a single ``transaction()`` (BEGIN IMMEDIATE, parameterized
    SQL only, rows = truth):

    - **Spans**: ``text`` / ``instruct`` are replaced from the snapshot for
      every span it captured.  Spans absent from the snapshot keep their
      current content; spans captured but since deleted from the spine are
      skipped (the manifest does not carry the spine position needed to
      re-insert them, so a deleted span cannot be resurrected).
    - **Characters**: never deleted.  Characters absent from the snapshot
      are left untouched.  Characters present in the snapshot but missing
      from the book are restored — the character row is created when it
      does not exist, and the ``character_book`` junction is (re)created
      when missing.  Existing character rows are kept verbatim: their
      voice assignment is shared state across the series and must not be
      clobbered by one book's snapshot.  A restored character whose
      ``voice_assignment_id`` references a ``voice_config`` row deleted
      since save is inserted with ``NULL`` instead (FK-safe).

    ``snapshot_json`` is inert data: parsed with ``json.loads`` and
    applied field by field — never evaluated.
    """
    with storage.transaction():
        for span in manifest.get("spans", []):
            if not isinstance(span, dict) or not span.get("id"):
                continue
            # Plan L (P1-S3): round-trip the per-span ``pause_after_ms``.
            # The key is present on snapshots saved after the pause feature
            # landed (value is ``None`` = resolve default, ``0`` = no-gap
            # override, or a bounded int).  It is intentionally NOT coerced:
            # ``None`` stays NULL and ``0`` stays 0.  Older snapshots (key
            # absent) leave the span's current pause untouched.  Out-of-range
            # values from a corrupt snapshot are defensively dropped (the
            # invariant "no unbounded pause in the DB" wins; text/instruct
            # still restore).
            span_update = "text = ?, instruct = ?"
            span_params: list = [span.get("text") or "", span.get("instruct") or ""]
            if "pause_after_ms" in span:
                pause = span.get("pause_after_ms")
                if pause is None:
                    span_update += ", pause_after_ms = ?"
                    span_params.append(None)
                elif isinstance(pause, bool):
                    pass  # drop invalid boolean
                else:
                    try:
                        validated = validate_pause_ms(pause)
                    except ValueError:
                        pass  # drop out-of-range/fractional; keep text/instruct
                    else:
                        span_update += ", pause_after_ms = ?"
                        span_params.append(validated)
            span_params.append(span["id"])
            storage.execute_update(
                f"UPDATE span SET {span_update} WHERE id = ?", tuple(span_params)
            )
        # Plan L (P1-S3): round-trip the book-level pause override columns.
        # Only present on snapshots saved after the pause feature landed; key
        # absent → leave current book pause overrides untouched.  NULL vs 0 is
        # preserved verbatim; out-of-range values are defensively dropped.
        book_pause_updates: list[str] = []
        book_pause_params: list = []
        for pause_key in ("pause_between_speakers_ms", "pause_same_speaker_ms"):
            if pause_key not in manifest:
                continue
            pause = manifest.get(pause_key)
            if pause is None:
                book_pause_updates.append(f"{pause_key} = ?")
                book_pause_params.append(None)
            elif isinstance(pause, bool):
                continue
            else:
                try:
                    validated = validate_pause_ms(pause)
                except ValueError:
                    continue
                book_pause_updates.append(f"{pause_key} = ?")
                book_pause_params.append(validated)
        if book_pause_updates:
            book_pause_params.append(book_id)
            storage.execute_update(
                f"UPDATE book SET {', '.join(book_pause_updates)} WHERE id = ?",
                tuple(book_pause_params),
            )
        for char in manifest.get("characters", []):
            if not isinstance(char, dict) or not char.get("id"):
                continue
            character_id = char["id"]
            character_rows = storage.execute_query(
                "SELECT id FROM character WHERE id = ?", (character_id,)
            )
            if not character_rows:
                # A snapshot character's voice assignment may reference a
                # voice_config row deleted since the snapshot was saved.
                # Under PRAGMA foreign_keys=ON the INSERT would raise
                # IntegrityError and fail the whole restore, so a dangling
                # reference is dropped (NULL) — the character still comes
                # back, just unassigned.
                voice_assignment_id = char.get("voice_assignment_id")
                if voice_assignment_id is not None and not storage.execute_query(
                    "SELECT id FROM voice_config WHERE id = ?",
                    (voice_assignment_id,),
                ):
                    voice_assignment_id = None
                storage.execute_insert(
                    "INSERT INTO character (id, name, voice_assignment_id)"
                    " VALUES (?, ?, ?)",
                    (
                        character_id,
                        char.get("name") or character_id,
                        voice_assignment_id,
                    ),
                )
            junction_rows = storage.execute_query(
                "SELECT 1 FROM character_book WHERE character_id = ? AND book_id = ?",
                (character_id, book_id),
            )
            if not junction_rows:
                storage.execute_insert(
                    "INSERT INTO character_book"
                    " (character_id, book_id, source, confidence)"
                    " VALUES (?, ?, 'derived', 1.0)",
                    (character_id, book_id),
                )


@router.post("/projects/load")
async def load_project_snapshot(
    request: SnapshotLoadRequest,
    storage: PipelineStorage = Depends(get_storage),
) -> dict:
    """Restore the snapshot *name* into the book *book_id* (merge-vs-replace).

    - 404 when the book or the snapshot does not exist, or when the
      snapshot belongs to a different book (no cross-book loads — the
      detail message deliberately matches the unknown-snapshot case so a
      snapshot's book cannot be probed)
    - 409 + ``Retry-After`` while any active walk_run/render_job row exists
      for the book (status pending/running) — restoring mid-run would
      corrupt the rows/files that run is producing
    - 400 when the manifest's ``schema_version`` is unknown/unexpected — a
      future manifest shape is never merged blindly
    - Merge semantics: span text/instructions are replaced from the
      snapshot; characters are never deleted (absent-from-snapshot
      characters survive); characters present in the snapshot but missing
      from the book are restored (character row + book junction)

    Returns ``{status, name, book_id, re_render_required}``.  The flag is
    true when the snapshot's referenced audio artifacts are absent under
    RENDER_ROOT — the user must re-render the restored book.
    """
    if not storage.execute_query(
        "SELECT id FROM book WHERE id = ?", (request.book_id,)
    ):
        raise HTTPException(
            status_code=404, detail=f"Book '{request.book_id}' not found"
        )

    row = storage.get_project_snapshot(request.name)
    if row is None or row["book_id"] != request.book_id:
        raise HTTPException(
            status_code=404, detail=f"Snapshot '{request.name}' not found"
        )

    if _book_has_active_runs(storage, request.book_id):
        raise HTTPException(
            status_code=409,
            detail=(
                "Cannot restore while a walk or render is active for this book; "
                f"retry after {_RETRY_AFTER_SECONDS}s"
            ),
            headers={"Retry-After": str(_RETRY_AFTER_SECONDS)},
        )

    try:
        manifest = json.loads(row["snapshot_json"])
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Snapshot '{request.name}' is corrupt and cannot be loaded",
        ) from exc

    if manifest.get("schema_version") != _SNAPSHOT_MANIFEST_SCHEMA_VERSION:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Snapshot '{request.name}' uses manifest schema version "
                f"{manifest.get('schema_version')!r}, which cannot be loaded "
                f"(supported version: {_SNAPSHOT_MANIFEST_SCHEMA_VERSION})"
            ),
        )

    _apply_snapshot_merge(storage, manifest, row["book_id"])

    re_render_required = _audio_reference_missing(manifest.get("audio_run_dir"))
    return {
        "status": "ok",
        "name": request.name,
        "book_id": request.book_id,
        "re_render_required": re_render_required,
    }
