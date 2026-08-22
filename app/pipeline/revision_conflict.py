"""Structured 409 conflict body shared by the two parity surfaces.

``RevisionConflictDTO`` is advertised as a schema by both
``PipelineCharacterPersonaAPI.v1`` and ``PipelineWalkPromptConfigRevisionAPI.v1``
in ``CONTRACTS.md``.  This module makes that registration truthful in code: every
affected 409 conflict branch in ``api_characters.py`` and ``api_walks.py`` returns
a body of the exact shape ``{error, code, message, detail}`` while preserving the
HTTP 409 status and existing semantics (``Retry-After`` remains 503-contention
only, unchanged).

Distinction:

* ``error`` — coarse, stable machine slug (always ``revision_conflict`` here).
* ``code``  — specific discriminator for the conflict (``STALE_BASE_REVISION``,
  ``PROTECTED_REVISION``, ``ALREADY_RAN``, ``CROSS_BOOK``).
* ``message`` — human-readable description (carries forward the prior detail text).
* ``detail`` — optional extra structured context; ``null`` when the branch yields
  no additional structure.

This module introduces no router family and no new routes; it only changes the
body of already-existing 409 responses.
"""

from __future__ import annotations

from fastapi import HTTPException
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Conflict codes
# ---------------------------------------------------------------------------

CODE_STALE = "STALE_BASE_REVISION"
"""A ``base_revision`` did not match the current head revision."""

CODE_PROTECTED = "PROTECTED_REVISION"
"""A protected revision cannot be replaced (e.g. by a rerun write)."""

CODE_ALREADY_RAN = "ALREADY_RAN"
"""An identical revision+scope was already applied; never duplicated silently."""

CODE_CROSS_BOOK = "CROSS_BOOK"
"""A revision cited for a write belongs to a different book."""


class RevisionConflictDTO(BaseModel):
    """Structured 409 conflict body (CONTRACTS ``RevisionConflictDTO``).

    Serialized directly as the HTTP 409 JSON body::

        {error: str, code: str, message: str, detail: dict | None}
    """

    error: str = "revision_conflict"
    code: str
    message: str
    detail: dict | None = None


def revision_conflict_http(
    *,
    code: str,
    message: str,
    detail: dict | None = None,
    error: str = "revision_conflict",
) -> HTTPException:
    """Return an HTTP 409 whose body is a :class:`RevisionConflictDTO`."""
    dto = RevisionConflictDTO(
        error=error,
        code=code,
        message=message,
        detail=detail,
    )
    return HTTPException(status_code=409, detail=dto.model_dump())
