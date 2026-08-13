"""Pipeline API — Voice catalog endpoints.

Provides HTTP endpoints for managing the voice catalog:
- GET /api/pipeline/voices — list all voice configs (optional type filter)
- POST /api/pipeline/voices — create a new voice config
- PUT /api/pipeline/voices/{id} — partial update of an existing voice config
- DELETE /api/pipeline/voices/{id} — delete a voice config
- POST /api/pipeline/voices/{id}/preview — generate a TTS audio preview

Uses dependency injection for storage so tests can inject InMemorySQLiteAdapter.
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Literal, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.pipeline.adapter import PipelineStorage
from app.pipeline.api_export import get_tts_engine
from app.pipeline.api_onboard import get_storage
from app.pipeline.clone_reference_media import (
    CloneReferenceMediaError,
    canonical_contain,
    cleanup_expired_references,
    configured_max_bytes,
    configured_max_duration_ms,
    reference_root,
    remove_if_exists,
    validate_and_copy,
)

# Previews directory: stored inside designed_voices/previews/ which is already
# served as static via the /designed_voices mount in app/app.py.
_PREVIEWS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "designed_voices",
    "previews",
)


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------

VoiceType = Literal["custom", "clone", "builtin_lora", "lora", "design"]


class VoiceCreateRequest(BaseModel):
    """Request body for POST /api/pipeline/voices."""

    id: Optional[str] = None
    name: str
    description: Optional[str] = None
    type: VoiceType = "custom"
    voice: Optional[str] = None
    character_style: Optional[str] = None
    seed: Optional[str] = "-1"
    ref_audio: Optional[str] = None
    ref_text: Optional[str] = None
    adapter_id: Optional[str] = None
    adapter_path: Optional[str] = None
    alias_of: Optional[str] = None


class VoiceUpdateRequest(BaseModel):
    """Request body for PUT /api/pipeline/voices/{id}.

    All fields are Optional — only fields explicitly set in the request
    body are updated.  ``model_dump(exclude_unset=True)`` distinguishes
    "field not sent" from "field sent as None".
    """

    name: Optional[str] = None
    description: Optional[str] = None
    type: Optional[VoiceType] = None
    voice: Optional[str] = None
    character_style: Optional[str] = None
    seed: Optional[str] = None
    ref_audio: Optional[str] = None
    ref_text: Optional[str] = None
    adapter_id: Optional[str] = None
    adapter_path: Optional[str] = None
    alias_of: Optional[str] = None


class VoicePreviewRequest(BaseModel):
    """Request body for POST /api/pipeline/voices/{voice_id}/preview."""

    sample_text: str


# ---------------------------------------------------------------------------
# Clone-reference DTOs (PipelineVoiceCloneReferenceAPI.v1)
# ---------------------------------------------------------------------------


class CloneReferenceDTO(BaseModel):
    """A single owner-scoped clone-reference row (contract v1).

    ``relative_path`` is the contained, application-relative path stored in
    ``clone_reference.relative_path`` — never an absolute filesystem path.
    """

    reference_id: str
    voice_id: str
    owner_id: str
    relative_path: str
    original_filename: str
    media_type: str
    byte_size: int
    duration_ms: int
    sha256: str
    created_ms: int
    deleted_ms: Optional[int] = None


class CloneReferenceUploadRequest(BaseModel):
    """Multipart upload contract (not a JSON body).

    The ``audio`` part carries the reference media file and ``ref_text`` is an
    optional aligned transcript.  Bound by the endpoint via ``File``/``Form``.
    """

    ref_text: Optional[str] = None


class CloneReferenceListResponse(BaseModel):
    """Owner-scoped reference list response (contract v1)."""

    references: list[CloneReferenceDTO]


class CloneReferenceDeleteResponse(BaseModel):
    """204 deletion response — no body (contract v1)."""


class VoiceConfigDTO(BaseModel):
    """Voice-config DTO returned alongside a created clone reference."""

    id: str
    name: Optional[str] = None
    description: Optional[str] = None
    type: Optional[str] = None
    voice: Optional[str] = None
    character_style: Optional[str] = None
    seed: Optional[str] = None
    ref_audio: Optional[str] = None
    ref_text: Optional[str] = None
    adapter_id: Optional[str] = None
    adapter_path: Optional[str] = None
    alias_of: Optional[str] = None


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])


# ---------------------------------------------------------------------------
# GET /api/pipeline/voices
# ---------------------------------------------------------------------------


@router.get("/voices")
async def list_voices(
    type: str | None = None,
    storage: PipelineStorage = Depends(get_storage),
) -> list[dict]:
    """Return all voice configs, optionally filtered by type.

    Query parameters
    ----------------
    type : str, optional
        Filter voices by type (e.g. ``clone``, ``custom``, ``design``,
        ``lora``, ``builtin_lora``).  When omitted, all voices are returned.

    Returns
    -------
    list[dict]
        Each dict contains all columns from the ``voice_config`` table:
        id, name, description, type, voice, character_style, seed,
        ref_audio, ref_text, adapter_id, adapter_path, alias_of.
    """
    if type is not None:
        rows = storage.execute_query(
            "SELECT * FROM voice_config WHERE type = ?", (type,)
        )
    else:
        rows = storage.execute_query("SELECT * FROM voice_config")
    return rows


# ---------------------------------------------------------------------------
# POST /api/pipeline/voices
# ---------------------------------------------------------------------------


@router.post("/voices", status_code=status.HTTP_201_CREATED)
async def create_voice(
    request: VoiceCreateRequest,
    storage: PipelineStorage = Depends(get_storage),
) -> dict:
    """Create a new voice config and insert it into the voice_config table.

    The ``id`` is derived from ``request.id`` if provided, otherwise from
    ``request.name`` (voice name is the unique identifier, matching the
    legacy voice_config.json convention where JSON keys are voice names).

    Returns the created voice config with all 12 columns.

    Raises
    ------
    HTTPException 409
        If a voice with the same id already exists.
    """
    voice_id = request.id or request.name

    # Check for duplicate id
    existing = storage.execute_query(
        "SELECT id FROM voice_config WHERE id = ?", (voice_id,)
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Voice config with id '{voice_id}' already exists",
        )

    storage.execute_insert(
        "INSERT INTO voice_config "
        "(id, name, description, type, voice, character_style, seed, "
        "ref_audio, ref_text, adapter_id, adapter_path, alias_of) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            voice_id,
            request.name,
            request.description,
            request.type,
            request.voice,
            request.character_style,
            request.seed,
            request.ref_audio,
            request.ref_text,
            request.adapter_id,
            request.adapter_path,
            request.alias_of,
        ),
    )

    # Return the created row
    rows = storage.execute_query(
        "SELECT * FROM voice_config WHERE id = ?", (voice_id,)
    )
    return rows[0]


# ---------------------------------------------------------------------------
# PUT /api/pipeline/voices/{voice_id}
# ---------------------------------------------------------------------------

# Columns that may be updated via PUT.  The ``id`` column is the path
# parameter and is never updatable through this endpoint.
_UPDATABLE_COLUMNS = frozenset({
    "name", "description", "type", "voice", "character_style", "seed",
    "ref_audio", "ref_text", "adapter_id", "adapter_path", "alias_of",
})


@router.put("/voices/{voice_id}")
async def update_voice(
    voice_id: str,
    request: VoiceUpdateRequest,
    storage: PipelineStorage = Depends(get_storage),
) -> dict:
    """Partially update an existing voice config.

    Only fields explicitly present in the request body are written.
    This allows callers to change a single field without clobbering the
    rest, and also allows setting a field to ``null`` to clear it.

    Raises
    ------
    HTTPException 404
        If no voice config with the given *voice_id* exists.
    """
    # Verify the row exists
    existing = storage.execute_query(
        "SELECT id FROM voice_config WHERE id = ?", (voice_id,)
    )
    if not existing:
        raise HTTPException(
            status_code=404,
            detail=f"Voice config '{voice_id}' not found",
        )

    # Build dynamic UPDATE from only the fields the caller actually sent.
    updates = request.model_dump(exclude_unset=True)

    if not updates:
        # Nothing to update — return current row as-is.
        rows = storage.execute_query(
            "SELECT * FROM voice_config WHERE id = ?", (voice_id,)
        )
        return rows[0]

    # Filter to known columns (Pydantic validates known fields; unknown
    # keys are silently ignored).
    set_clauses: list[str] = []
    params: list = []
    for col, value in updates.items():
        if col in _UPDATABLE_COLUMNS:
            set_clauses.append(f"{col} = ?")
            params.append(value)

    if not set_clauses:
        # All provided keys were unknown — nothing to update.
        rows = storage.execute_query(
            "SELECT * FROM voice_config WHERE id = ?", (voice_id,)
        )
        return rows[0]

    params.append(voice_id)
    sql = f"UPDATE voice_config SET {', '.join(set_clauses)} WHERE id = ?"
    storage.execute_update(sql, tuple(params))

    # Return the updated row
    rows = storage.execute_query(
        "SELECT * FROM voice_config WHERE id = ?", (voice_id,)
    )
    return rows[0]


# ---------------------------------------------------------------------------
# DELETE /api/pipeline/voices/{id}
# ---------------------------------------------------------------------------


@router.delete("/voices/{voice_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_voice(
    voice_id: str,
    storage: PipelineStorage = Depends(get_storage),
) -> Response:
    """Delete a voice config from the voice_config table.

    Parameters
    ----------
    voice_id : str
        The id of the voice config to delete (path parameter).

    Returns
    -------
    Response
        204 No Content on success (no response body).

    Raises
    ------
    HTTPException 404
        If no voice config with the given id exists.
    """
    # Check if the voice exists
    existing = storage.execute_query(
        "SELECT id FROM voice_config WHERE id = ?", (voice_id,)
    )
    if not existing:
        raise HTTPException(
            status_code=404,
            detail=f"Voice config with id '{voice_id}' not found",
        )

    storage.execute_delete(
        "DELETE FROM voice_config WHERE id = ?", (voice_id,)
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# POST /api/pipeline/voices/{voice_id}/preview
# ---------------------------------------------------------------------------


@router.post("/voices/{voice_id}/preview")
async def preview_voice(
    voice_id: str,
    request: VoicePreviewRequest,
    storage: PipelineStorage = Depends(get_storage),
    tts_engine: object | None = Depends(get_tts_engine),
) -> dict:
    """Generate a voice preview audio file for the given voice config.

    Loads the voice config from the ``voice_config`` table, calls the TTS
    engine's ``generate_voice`` method, and saves the resulting audio to
    ``./designed_voices/previews/{voice_id}.wav`` (served via the existing
    ``/designed_voices`` static mount in ``app/app.py``).

    Parameters
    ----------
    voice_id : str
        The id of the voice config to preview (path parameter).
    request : VoicePreviewRequest
        JSON body with ``sample_text`` — the text to synthesize.

    Returns
    -------
    dict
        ``{"audio_url": "/designed_voices/previews/{voice_id}.wav", "voice_id": voice_id}``

    Raises
    ------
    HTTPException 404
        If no voice config with the given *voice_id* exists.
    HTTPException 503
        If the TTS engine is not available.
    HTTPException 500
        If TTS generation fails.
    """
    # Load voice config from DB
    rows = storage.execute_query(
        "SELECT * FROM voice_config WHERE id = ?", (voice_id,)
    )
    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"Voice config '{voice_id}' not found",
        )

    if tts_engine is None:
        raise HTTPException(
            status_code=503,
            detail="TTS engine not available",
        )

    row = rows[0]

    # Build voice_config dict keyed by voice_id (speaker name)
    voice_config = {
        voice_id: {
            "type": row["type"],
            "voice": row["voice"],
            "description": row["description"],
            "ref_audio": row["ref_audio"],
            "ref_text": row["ref_text"],
            "adapter_id": row["adapter_id"],
            "adapter_path": row["adapter_path"],
            "character_style": row["character_style"],
            "seed": row["seed"],
            "alias_of": row["alias_of"],
        }
    }

    # Ensure previews directory exists (use designed_voices/previews for static serving)
    os.makedirs(_PREVIEWS_DIR, exist_ok=True)

    # Sanitize voice_id to prevent path traversal
    safe_voice_id = voice_id.replace("/", "_").replace("\\", "_").replace("..", "_")
    output_path = os.path.join(_PREVIEWS_DIR, f"{safe_voice_id}.wav")

    try:
        tts_engine.generate_voice(
            text=request.sample_text,
            instruct_text="",
            speaker=voice_id,
            voice_config=voice_config,
            output_path=output_path,
        )
    except Exception as exc:  # noqa: BLE001 — surface TTS errors
        raise HTTPException(
            status_code=500,
            detail=f"TTS generation failed: {exc}",
        ) from exc

    return {
        "audio_url": f"/designed_voices/previews/{safe_voice_id}.wav",
        "voice_id": voice_id,
    }


# ---------------------------------------------------------------------------
# Clone-reference resources (PipelineVoiceCloneReferenceAPI.v1)
# ---------------------------------------------------------------------------
# Owner sentinel: single-user local deployment with no auth/principal
# subsystem — every reference is owned by the stable ``"local"`` principal
# (see CONTRACTS.md and the schema's ``owner_id`` column).
_LOCAL_OWNER_ID = "local"
# Tombstone retention window for the bounded cleanup sweep.
_CLONE_RETENTION_MS = 7 * 24 * 60 * 60 * 1000


def _now_ms() -> int:
    return int(time.time() * 1000)


def _load_voice_or_404(storage: PipelineStorage, voice_id: str) -> dict:
    rows = storage.execute_query(
        "SELECT * FROM voice_config WHERE id = ?", (voice_id,)
    )
    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"Voice config '{voice_id}' not found",
        )
    return rows[0]


def _voice_config_dto(row: dict) -> VoiceConfigDTO:
    return VoiceConfigDTO(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        type=row["type"],
        voice=row["voice"],
        character_style=row["character_style"],
        seed=row["seed"],
        ref_audio=row["ref_audio"],
        ref_text=row["ref_text"],
        adapter_id=row["adapter_id"],
        adapter_path=row["adapter_path"],
        alias_of=row["alias_of"],
    )


def _safe_original_name(raw: str | None) -> str:
    """Basename-only, traversal-safe original filename (rejects empty)."""
    if not raw:
        raise HTTPException(
            status_code=400, detail="Reference audio filename is required"
        )
    base = os.path.basename(raw.replace("\\", "/"))
    if not base:
        raise HTTPException(
            status_code=400, detail="Reference audio filename is required"
        )
    return base


def _resolve_reference_path(
    storage: PipelineStorage, voice_id: str, reference_id: str
) -> tuple[dict, str]:
    """Owner-scoped, voice-scoped reference row + its contained file path.

    Raises HTTPException 404 on missing, cross-owner, or cross-voice access.
    """
    row = storage.get_clone_reference(reference_id, _LOCAL_OWNER_ID)
    if row is None or row["voice_id"] != voice_id:
        raise HTTPException(status_code=404, detail="Clone reference not found")
    root = reference_root()
    dest = os.path.join(root, row["relative_path"])
    canonical = canonical_contain(root, dest)
    if canonical is None:
        raise HTTPException(status_code=404, detail="Clone reference not found")
    return row, canonical


# POST /api/pipeline/voices/{voice_id}/references


@router.post(
    "/voices/{voice_id}/references",
    status_code=status.HTTP_201_CREATED,
)
async def create_clone_reference(
    voice_id: str,
    audio: UploadFile = File(...),
    ref_text: str | None = Form(None),
    storage: PipelineStorage = Depends(get_storage),
    tts_engine: object | None = Depends(get_tts_engine),
) -> dict:
    """Upload a validated clone-voice reference audio sample.

    The multipart ``audio`` part is validated (allow-listed extension, magic-
    byte content sniff, byte-size and decoded-duration bounds), written under
    the reference root, inserted into ``clone_reference``, and selected as the
    voice's ``ref_audio`` (plus ``ref_text`` when provided).  Returns the new
    ``CloneReferenceDTO`` and the updated ``VoiceConfigDTO``.

    Raises
    ------
    HTTPException 404
        If the voice config does not exist.
    HTTPException 400
        If the media/path is rejected or the voice type is invalid.
    """
    voice_row = _load_voice_or_404(storage, voice_id)
    if voice_row["type"] not in VoiceType.__args__:
        raise HTTPException(
            status_code=400, detail="Voice config has invalid type"
        )

    # Bounded invoke-time cleanup of expired, tombstoned, unreferenced files.
    cleanup_expired_references(
        storage,
        reference_root(),
        older_than_ms=_CLONE_RETENTION_MS,
        now_ms=_now_ms(),
    )

    original_name = _safe_original_name(audio.filename)
    reference_id = uuid.uuid4().hex
    root = reference_root()
    try:
        metadata = validate_and_copy(
            audio.file,
            root,
            reference_id,
            original_name,
            max_bytes=configured_max_bytes(),
            max_duration_ms=configured_max_duration_ms(),
            tts_engine=tts_engine,
        )
    except CloneReferenceMediaError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    relative_path = str(metadata["relative_path"])
    record = {
        "reference_id": reference_id,
        "voice_id": voice_id,
        "owner_id": _LOCAL_OWNER_ID,
        "relative_path": relative_path,
        "original_filename": str(metadata["original_filename"]),
        "media_type": str(metadata["media_type"]),
        "byte_size": int(metadata["byte_size"]),
        "duration_ms": int(metadata["duration_ms"]),
        "sha256": str(metadata["sha256"]),
        "created_ms": _now_ms(),
        "deleted_ms": None,
    }
    try:
        with storage.transaction():
            storage.insert_clone_reference(record)
            set_sql = "UPDATE voice_config SET ref_audio = ?"
            params: list = [relative_path]
            if ref_text is not None:
                set_sql += ", ref_text = ?"
                params.append(ref_text)
            set_sql += " WHERE id = ?"
            params.append(voice_id)
            storage.execute_update(set_sql, tuple(params))
    except BaseException:
        # A failed insert must never orphan the just-written media file.
        remove_if_exists(os.path.join(root, relative_path))
        raise

    updated = storage.execute_query(
        "SELECT * FROM voice_config WHERE id = ?", (voice_id,)
    )[0]
    return {
        "reference": CloneReferenceDTO(**record).model_dump(),
        "voice": _voice_config_dto(updated).model_dump(),
    }


# GET /api/pipeline/voices/{voice_id}/references


@router.get("/voices/{voice_id}/references")
async def list_clone_references(
    voice_id: str,
    storage: PipelineStorage = Depends(get_storage),
) -> CloneReferenceListResponse:
    """Return the owner's clone references for *voice_id*."""
    _load_voice_or_404(storage, voice_id)
    rows = storage.list_clone_references(voice_id, _LOCAL_OWNER_ID)
    return CloneReferenceListResponse(
        references=[CloneReferenceDTO(**row) for row in rows]
    )


# GET /api/pipeline/voices/{voice_id}/references/{reference_id}/preview


@router.get("/voices/{voice_id}/references/{reference_id}/preview")
async def preview_clone_reference(
    voice_id: str,
    reference_id: str,
    storage: PipelineStorage = Depends(get_storage),
) -> FileResponse:
    """Stream the reference audio inline (with Range support)."""
    row, path = _resolve_reference_path(storage, voice_id, reference_id)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Clone reference media missing")
    return FileResponse(path, media_type=row["media_type"])


# GET /api/pipeline/voices/{voice_id}/references/{reference_id}/download


@router.get("/voices/{voice_id}/references/{reference_id}/download")
async def download_clone_reference(
    voice_id: str,
    reference_id: str,
    storage: PipelineStorage = Depends(get_storage),
) -> FileResponse:
    """Stream the reference audio as an attachment download only."""
    row, path = _resolve_reference_path(storage, voice_id, reference_id)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Clone reference media missing")
    return FileResponse(
        path,
        media_type=row["media_type"],
        filename=row["original_filename"],
        content_disposition_type="attachment",
    )


# DELETE /api/pipeline/voices/{voice_id}/references/{reference_id}


@router.delete(
    "/voices/{voice_id}/references/{reference_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_clone_reference(
    voice_id: str,
    reference_id: str,
    storage: PipelineStorage = Depends(get_storage),
) -> Response:
    """Tombstone the reference metadata and remove the owned file (idempotent).

    A reference already tombstoned by its owner is a no-op 204; cross-owner or
    cross-voice access is a 404.
    """
    _load_voice_or_404(storage, voice_id)
    row = storage.get_clone_reference(reference_id, _LOCAL_OWNER_ID)
    if row is None or row["voice_id"] != voice_id:
        # Cross-owner / cross-voice access is indistinguishable from absence.
        raise HTTPException(status_code=404, detail="Clone reference not found")
    try:
        with storage.transaction():
            storage.tombstone_clone_reference(
                reference_id, _LOCAL_OWNER_ID, _now_ms()
            )
    except KeyError:
        raise HTTPException(status_code=404, detail="Clone reference not found")
    remove_if_exists(os.path.join(reference_root(), row["relative_path"]))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
