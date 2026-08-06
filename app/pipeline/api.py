"""Pipeline API — FastAPI router for /api/pipeline/* endpoints.

Provides HTTP endpoints for the audiobook pipeline: onboard EPUBs, run walks,
query characters, review low-confidence items, perform structural operations,
export annotated scripts, render audiobooks, and re-onboard books.

This module is the thin entry point that combines sub-routers from
responsibility-specific modules:

- ``api_onboard`` — onboard/reonboard endpoints + storage singleton
- ``api_walks`` — walk execution and status endpoints
- ``api_operations`` — structural operation endpoints
- ``api_review`` — confidence review endpoints
- ``api_export`` — export and render endpoints
- ``api_characters`` — character ledger endpoints
- ``api_voices`` — voice catalog and preview endpoints

Uses dependency injection for storage so tests can inject InMemorySQLiteAdapter.
"""

from __future__ import annotations

from fastapi import APIRouter

# Import sub-routers
from app.pipeline.api_onboard import router as _onboard_router
from app.pipeline.api_walks import router as _walks_router
from app.pipeline.api_operations import router as _operations_router
from app.pipeline.api_review import router as _review_router
from app.pipeline.api_export import router as _export_router
from app.pipeline.api_characters import router as _characters_router
from app.pipeline.api_voices import router as _voices_router

# Re-export dependencies for backward compatibility with tests
from app.pipeline.api_onboard import get_storage  # noqa: F401
from app.pipeline.api_onboard import extract_epub_text, populate_spine  # noqa: F401
from app.pipeline.api_walks import get_walk_runner, get_character_ledger  # noqa: F401
from app.pipeline.api_operations import get_operation_executor  # noqa: F401
from app.pipeline.api_review import get_review_manager  # noqa: F401
from app.pipeline.api_export import get_tts_engine  # noqa: F401


# ---------------------------------------------------------------------------
# Combined router
# ---------------------------------------------------------------------------

# The combined router has NO prefix of its own — each sub-router already
# declares prefix="/api/pipeline" and tags=["pipeline"]. Including them
# directly preserves their routes as-is.
router = APIRouter()

router.include_router(_onboard_router)
router.include_router(_walks_router)
router.include_router(_operations_router)
router.include_router(_review_router)
router.include_router(_export_router)
router.include_router(_characters_router)
router.include_router(_voices_router)
