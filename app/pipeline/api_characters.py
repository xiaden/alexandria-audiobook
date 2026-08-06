"""Pipeline API — Character endpoints.

Provides HTTP endpoints for managing character voice assignments:
- PUT /api/pipeline/characters/{id}/voice — set or clear voice assignment

Uses dependency injection for storage so tests can inject InMemorySQLiteAdapter.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.pipeline.adapter import PipelineStorage
from app.pipeline.api_onboard import get_storage


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------


class CharacterVoiceUpdateRequest(BaseModel):
    """Request body for PUT /api/pipeline/characters/{id}/voice.

    ``voice_assignment_id`` may be ``null`` to clear the assignment.
    """

    voice_assignment_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])


# ---------------------------------------------------------------------------
# PUT /api/pipeline/characters/{id}/voice
# ---------------------------------------------------------------------------


@router.put("/characters/{character_id}/voice")
async def update_character_voice(
    character_id: str,
    request: CharacterVoiceUpdateRequest,
    storage: PipelineStorage = Depends(get_storage),
) -> dict:
    """Set or clear a character's voice assignment.

    Parameters
    ----------
    character_id : str
        The character's primary key.
    request : CharacterVoiceUpdateRequest
        JSON body with ``voice_assignment_id`` (may be ``null`` to clear).

    Returns
    -------
    dict
        The updated character row (id, name, aliases, voice_assignment_id,
        description).

    Raises
    ------
    HTTPException 404
        If the character does not exist.
    HTTPException 400
        If ``voice_assignment_id`` references a non-existent voice config.
    """
    # Verify character exists
    characters = storage.execute_query(
        "SELECT id, name, aliases, voice_assignment_id, description "
        "FROM character WHERE id = ?",
        (character_id,),
    )
    if not characters:
        raise HTTPException(
            status_code=404,
            detail=f"Character '{character_id}' not found",
        )

    # If a voice_assignment_id is provided, verify it exists in voice_config
    if request.voice_assignment_id is not None:
        voices = storage.execute_query(
            "SELECT id FROM voice_config WHERE id = ?",
            (request.voice_assignment_id,),
        )
        if not voices:
            raise HTTPException(
                status_code=400,
                detail=f"Voice config '{request.voice_assignment_id}' not found",
            )

    # Update the character's voice_assignment_id
    storage.execute_update(
        "UPDATE character SET voice_assignment_id = ? WHERE id = ?",
        (request.voice_assignment_id, character_id),
    )

    # Return the updated character
    updated = storage.execute_query(
        "SELECT id, name, aliases, voice_assignment_id, description "
        "FROM character WHERE id = ?",
        (character_id,),
    )
    return updated[0]
