# Task: Pipeline API Split by Responsibility

## Problem Statement
`app/pipeline/api.py` is 500 lines and handles 14+ endpoints across 5 responsibilities: onboarding, walks, operations, review, and export/render. This violates the single-responsibility principle and makes the file hard to maintain. The API should be split by responsibility into separate modules.

## Dependencies
- Plan J (book-scoping-fix) — API endpoints must pass book_id before splitting
- Plan L (walk-order-canonical-contract) — API must reference canonical walk order before splitting

## Phases

### Phase 1: Split API into responsibility modules
- [x] Create `app/pipeline/api_onboard.py` with onboarding endpoints
    **Note:** Created app/pipeline/api_onboard.py with POST /onboard and POST /reonboard endpoints. Moved storage singleton (_storage, _get_production_storage), get_storage() dependency, ReonboardRequest model, and imports (os, tempfile, uuid, UploadFile, File, HTTPException, PipelineStorage, SQLiteAdapter, extract_epub_text, populate_spine, reonboard_book). File: app/pipeline/api_onboard.py (created, 130 lines).
  **Notes:** `POST /api/pipeline/onboard` — onboard EPUB. `POST /api/pipeline/reonboard` — re-onboard book. Extract from api.py lines 1-100 (approximate).
- [x] Create `app/pipeline/api_walks.py` with walk endpoints
    **Note:** Created app/pipeline/api_walks.py with POST /run_walk, POST /run_all_walks, GET /walk_status/{book_id}, GET /characters/{book_id}. Moved RunWalkRequest, RunAllWalksRequest models, get_walk_runner(), get_character_ledger() dependencies, and imports (WalkRunner, CharacterLedger, WALK_ORDER, PipelineStorage). File: app/pipeline/api_walks.py (created, 115 lines).
  **Notes:** `POST /api/pipeline/walk/run` — run single walk. `POST /api/pipeline/walk/run_all` — run all walks. `GET /api/pipeline/walk/status` — get walk status. `GET /api/pipeline/characters` — get characters for book. Extract from api.py lines 100-250 (approximate).
- [x] Create `app/pipeline/api_operations.py` with operation endpoints
    **Note:** Created app/pipeline/api_operations.py with POST /operation (single endpoint dispatching by operation field). Moved OperationRequest model, get_operation_executor() dependency, and imports (OperationExecutor, PipelineStorage). File: app/pipeline/api_operations.py (created, 82 lines).
  **Notes:** `POST /api/pipeline/operation/split` — split span. `POST /api/pipeline/operation/merge` — merge spans. `POST /api/pipeline/operation/move` — move span. `POST /api/pipeline/operation/delete` — delete span. Extract from api.py lines 250-350 (approximate).
- [x] Create `app/pipeline/api_review.py` with review endpoints
    **Note:** Created app/pipeline/api_review.py with GET /review/{book_id}, POST /review/accept, POST /review/reject, POST /review/override. Moved ReviewActionRequest model, get_review_manager() dependency, and imports (ReviewManager, PipelineStorage, Optional, Any). File: app/pipeline/api_review.py (created, 86 lines).
  **Notes:** `GET /api/pipeline/review/{book_id}` — get review items. `POST /api/pipeline/review/accept` — accept review item. `POST /api/pipeline/review/reject` — reject review item. `POST /api/pipeline/review/override` — override review item. Extract from api.py lines 350-420 (approximate).
- [x] Create `app/pipeline/api_export.py` with export/render endpoints
    **Note:** Created app/pipeline/api_export.py with GET /export/{book_id} and POST /render. Moved RenderRequest model, get_tts_engine() dependency (with lazy import of app.app.project_manager to avoid circular import), and imports (PipelineStorage, export_annotated_script, render_audiobook). File: app/pipeline/api_export.py (created, 88 lines).
  **Notes:** `GET /api/pipeline/export/{book_id}` — export annotated script. `POST /api/pipeline/render` — render audiobook. `GET /api/pipeline/tts_engine` — get TTS engine. Extract from api.py lines 420-500 (approximate).
- [x] Update `app/pipeline/api.py` to import and combine all routers
    **Note:** Rewrote app/pipeline/api.py as thin entry point (65 lines). Imports 5 sub-routers, combines via include_router() on a prefix-less APIRouter. Re-exports all dependencies (get_storage, get_walk_runner, get_character_ledger, get_review_manager, get_operation_executor, get_tts_engine) and functions (extract_epub_text, populate_spine) for backward compatibility with tests. Combined router preserves all 13 endpoint paths exactly.
  **Notes:** Import routers from the new modules. Create a combined `pipeline_router` that includes all sub-routers. Keep api.py as the entry point for backward compatibility.
- [x] Verify all endpoints still work correctly
    **Note:** Verified: (1) pipeline router imports correctly with all 13 endpoints at correct paths, (2) all dependency re-exports work for test imports, (3) all 27 tests in tests/pipeline/test_api.py pass. Updated test patches from app.pipeline.api.extract_epub_text to app.pipeline.api_onboard.extract_epub_text (2 occurrences) since mock.patch must target the module where the name is used, not where it's defined. app/app.py import fails due to unrelated missing 'utils' module — not caused by this refactoring.
  **Notes:** This is a refactoring. No behavioral changes. All endpoints must work as before.

### Phase 2: Update imports and tests
- [x] Update `app/app.py` to import from the new API modules
    **Note:** No changes needed — api.py still exports combined router under the same name. Verified: `from app.pipeline.api import router` returns `APIRouter` successfully. app/app.py line 31 (`from app.pipeline.api import router as pipeline_router`) continues to work without modification.
  **Notes:** Change `from app.pipeline.api import pipeline_router` to import from the combined router. Verify the app starts correctly.
- [x] Update `tests/pipeline/test_api.py` to test all endpoints
    **Note:** Already fixed in Phase 1 — all 27 tests in tests/pipeline/test_api.py pass (1.47s). Mock patches correctly target sub-module namespaces (api_onboard, api_walks, etc.) per discovery from Phase 1.
  **Notes:** Verify all 27 existing tests still pass. Add tests for any new endpoints (if any).
- [x] Run `pytest tests/pipeline/test_api.py -v` and verify all tests pass
    **Note:** Full pipeline test suite passes: 633 passed, 0 failed, 1 warning (unrelated StarletteDeprecationWarning about httpx2). 6.67s runtime. All endpoints preserved at exact paths.
  **Notes:** The test file may need to be split as well, but that's optional.

### Phase 3: Update CONTRACTS.md
- [x] Update `artifacts/designs/parts/epub-audiobook-pipeline-rewrite/CONTRACTS.md` to document the API split
    **Note:** Updated CONTRACTS.md "API Split by Responsibility (Plan N)" section (lines 424-452 → 424-556). Changes: (1) Fixed incorrect endpoint URLs — replaced 4 non-existent separate operation endpoints (/operation/split, /operation/merge, /operation/move, /operation/delete) with the actual single POST /api/pipeline/operation dispatch endpoint. (2) Removed non-existent GET /api/pipeline/tts_engine endpoint. (3) Added dependency injection documentation for each module — get_storage (api_onboard), get_walk_runner/get_character_ledger (api_walks), get_operation_executor (api_operations), get_review_manager (api_review), get_tts_engine (api_export). (4) Added thin entry point documentation showing how api.py combines 5 sub-routers via include_router() and re-exports dependencies for backward compatibility. The "API Endpoints" section (lines 283-300) was already correct with all 13 endpoints at correct paths — no changes needed there.
  **Notes:** List all endpoints grouped by responsibility module. Document the request/response schemas for each endpoint. Explain the module structure.
- [x] Verify CONTRACTS.md accurately reflects the current implementation
    **Note:** Verified CONTRACTS.md accurately reflects current implementation. Cross-checked all 13 endpoints against source files (api_onboard.py, api_walks.py, api_operations.py, api_review.py, api_export.py) — all paths match. Module organization matches actual files (2+4+1+4+2 = 13 endpoints). No stale/incorrect endpoint paths remain. Dependency injection functions documented match actual get_*() functions in each module. Thin entry point pattern in api.py accurately documented. The "API Endpoints" summary section (lines 283-300) is consistent with the detailed Plan N section.
  **Notes:** This is the canonical contract for the API. All future API change must update this contract.

### Phase 4: Verification
- [x] Run `pytest tests/pipeline/ -v` and verify all tests pass
    **Note:** Full pipeline test suite: 633 passed, 0 failed, 1 warning (unrelated StarletteDeprecationWarning about httpx2). Runtime 6.52s. All endpoints preserved at exact paths.
- [x] Verify no endpoint behavior has changed
    **Note:** Verified: (1) All 5 sub-routers import correctly, (2) Combined router exposes exactly 13 endpoints at expected paths — no missing, no extra. Endpoint paths match CONTRACTS.md specification.
- [x] Verify all API modules are properly documented
    **Note:** All 6 API modules have module-level docstrings explaining their responsibility: api_onboard.py (onboard/reonboard), api_walks.py (walk execution/status/characters), api_operations.py (structural operations), api_review.py (confidence review), api_export.py (export/render), api.py (thin entry point combining 5 sub-routers).
- [x] Verify the combined router works correctly
    **Note:** Combined router works correctly: (1) FastAPI app includes 16 routes (13 endpoints + 3 OpenAPI/docs routes), (2) All 6 dependency functions re-exported from api.py (get_storage, get_walk_runner, get_review_manager, get_operation_executor, get_character_ledger, get_tts_engine), (3) Backward compatibility verified — existing imports from app.pipeline.api continue to work.
  **Notes:** After this plan, the API is split by responsibility and easier to maintain.

## Completion Criteria
- API is split into 5 responsibility modules (onboard, walks, operations, review, export)
- All endpoints still work correctly
- All tests pass
- CONTRACTS.md documents the API split

## Negative Constraints
- Do NOT change endpoint behavior — only reorganize the code
- Do NOT change endpoint URLs or request/response schemas
- Do NOT break backward compatibility
- Do NOT skip tests that fail — the refactoring must preserve behavior
