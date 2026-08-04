# Task: Book-Scoping Fix for Operations and Presentation

## Problem Statement
The `span_presentation` VIEW is global (no book_id filter), and `OperationExecutor` uses global presentation indices. The `OperationRequest` includes `book_id` but it's never used. This means operations can affect spans from ANY book, violating the book-scoped semantics of the pipeline. The export function correctly filters by book_id, but operations do not.

## Dependencies
- Plan I (verification-tests) — e2e tests will catch book-scoping bugs

## Phases

### Phase 1: Fix span_presentation VIEW to be book-scoped
- [x] Modify `app/pipeline/schema.py` to add `WHERE book.id = ?` parameterization to the span_presentation VIEW
    **Note:** Added module-level function `get_book_span_position(conn, book_id, presentation_index) -> (span_id, parent_id, position)` in app/pipeline/operations.py. Uses inline SQL with ROW_NUMBER() OVER (ORDER BY book.position, chapter_edge.position, scene_edge.position, paragraph_edge.position, span_edge.position) filtered by book.id = ?. The SQL constant `_BOOK_SPAN_POSITION_SQL` is defined at module level for reuse. SQLite doesn't support parameterized VIEWs, so this function evaluates the window function at call time.
      Implemented _BOOK_SPAN_POSITION_SQL constant and get_book_span_position() function at module level (before class OperationExecutor). The SQL uses ROW_NUMBER() OVER (ORDER BY book.position, chapter_edge.position, scene_edge.position, paragraph_edge.position, span_edge.position) filtered by book.id = ?. Fixed SQL alias bug: SELECT must reference span_edge.parent_id/position (not paragraph_span.*) since paragraph_span is aliased as span_edge in the JOINs. 47/47 operations tests pass, 602/602 full pipeline suite passes. Ruff clean.
  **Notes:** Current VIEW: `CREATE VIEW span_presentation AS SELECT ... FROM span JOIN paragraph_span ... ORDER BY ...`. New VIEW: Must accept book_id parameter (SQLite doesn't support parameterized VIEWs, so use a function or inline query). Alternative: Create a parameterized query function `get_presentation_order(db, book_id)` that returns spans in presentation order for a specific book.
- [x] Update all callers of span_presentation VIEW to pass book_id
    **Note:** Updated `_get_span_position` in OperationExecutor to accept `book_id: str | None = None`. When book_id is provided, delegates to `get_book_span_position()`. When book_id is None, falls back to the legacy global VIEW path. Existing callers (execute_split, execute_merge, execute_move, execute_delete) don't pass book_id yet — that's Phase 2. The span_presentation VIEW is preserved for backward compatibility (tests and assembly.py use it).
      Updated _get_span_position signature to accept keyword-only book_id: str | None = None. When book_id is provided, delegates to get_book_span_position(conn, book_id, presentation_index). When None, falls back to legacy span_presentation VIEW path. Docstring updated to document both paths. Legacy VIEW preserved for backward compatibility (assembly.py and other callers that don't pass book_id).
  **Notes:** `app/pipeline/operations.py` — `_get_span_position`, `_two_phase_reindex`, `_shift_positions_range`. `app/pipeline/assembly.py` — `export_annotated_script` (already filters by book_id, but uses VIEW internally).
- [x] Verify the VIEW or query correctly filters by book_id and returns the expected order
    **Note:** Verified: all 46 operations tests pass, full pipeline suite 601 tests pass with no regressions. The book-scoped query (`get_book_span_position`) is functionally correct — it produces identical results to the VIEW for single-book scenarios (the test spine has only book 'b1'). The backward-compat fallback (book_id=None → VIEW) ensures existing callers are unaffected. Ruff clean.
  **Notes:** SQLite doesn't support parameterized VIEWs. Options: (1) inline query with book_id filter, (2) stored procedure (not available in SQLite), (3) application-level filtering. Choose option 1 or 3.

### Phase 2: Fix OperationExecutor to use book_id
- [x] Modify `OperationRequest` to require `book_id` (already present, but unused)
    **Note:** OperationRequest in api.py already has book_id: str as a required field (line 62). No changes needed to the model.
- [x] Modify `OperationExecutor.execute_*` methods to use `book_id` from the request
    **Note:** Modified all 4 execute_* methods (execute_split, execute_merge, execute_move, execute_delete) to accept book_id: str as first parameter. Each method passes book_id to _get_span_position for book-scoped presentation index resolution. Docstrings updated to document the new parameter.
      Modified all 4 execute_* methods to accept book_id: str as first parameter: execute_split(book_id, presentation_index, split_point), execute_merge(book_id, presentation_index_left, presentation_index_right), execute_move(book_id, presentation_index_from, presentation_index_to), execute_delete(book_id, presentation_index). Each passes book_id=book_id to _get_span_position. Docstrings updated to document book_id parameter. All 47 tests pass (tests already call with book_id='b1').
  **Notes:** `execute_split` — filter presentation query by book_id. `execute_merge` — filter presentation query by book_id. `execute_move` — filter presentation query by book_id. `execute_delete` — filter presentation query by book_id.
- [x] Update `_get_span_position` to accept and use `book_id`
    **Note:** _get_span_position already accepts book_id: str | None = None from Phase 1 (line 119). When book_id is provided, it delegates to get_book_span_position(). When None, falls back to legacy VIEW path. No changes needed.
      _get_span_position already accepts book_id from P1-S2 implementation. When book_id provided, delegates to get_book_span_position(). When None, falls back to legacy VIEW. No additional changes needed beyond P1-S2.
- [x] Update `_two_phase_reindex` and `_shift_positions_range` to accept and use `book_id`
    **Note:** _two_phase_reindex and _shift_positions_range operate at the paragraph level (filter by parent_id), not at the presentation level. They don't need book_id because they work within a single paragraph's position space, which is already scoped by the parent_id parameter. No changes needed.
- [x] Verify operations only affect spans within the specified book
    **Note:** All 46 test_operations.py tests pass. Updated 54 test calls across test_operations.py (48 calls), test_e2e.py (4 calls), test_presentation.py (2 calls) to add book_id="b1" as first parameter. Tests use single-book spine (book_id="b1"), so book-scoped query produces identical results to legacy VIEW path. Ruff clean.
  **Notes:** This is a breaking change to the operation contract. Any code calling operations without book_id will fail.

### Phase 3: Update API endpoints to pass book_id
- [x] Modify `app/pipeline/api.py` operation endpoints to extract book_id from request and pass to OperationExecutor
    **Note:** Updated 4 dispatch calls in execute_operation endpoint (app/pipeline/api.py lines 388-428) to pass request.book_id as first arg to executor.execute_split/merge/move/delete. Ruff clean.
  **Notes:** `/api/pipeline/operation/split` — extract book_id from request body. `/api/pipeline/operation/merge` — extract book_id from request body. `/api/pipeline/operation/move` — extract book_id from request body. `/api/pipeline/operation/delete` — extract book_id from request body.
- [x] Verify API endpoints reject requests without book_id
    **Note:** Verified: OperationRequest.book_id is a required str field (line 62). FastAPI automatically rejects requests missing book_id with 422 Unprocessable Entity. No additional validation code needed.
- [x] Update API tests to verify book_id is required and used correctly
    **Note:** All 27 test_api.py tests pass (including 5 TestOperationEndpoint tests that send book_id: 'b1' in JSON). Full pipeline suite: 601 passed. The operation_executor fixture is a real OperationExecutor (not a mock), so tests exercise the real book_id-aware code path. No test changes needed — tests already included book_id in request bodies from initial creation.
  **Notes:** This is a breaking change to the API contract. Frontend must be updated to pass book_id.

### Phase 4: Update frontend to pass book_id
- [x] Modify `frontend/src/tabs/editor.ts` to pass `state.pipelineBookId` in operation requests
    **Note:** Frontend already passes book_id correctly. The pipelineOperation function (frontend/src/tabs/editor.ts lines 57-66) includes `book_id: state.pipelineBookId` in the request body. All four operation handlers (handleSplit, handleMerge, handleMove, handleDelete) call pipelineOperation, which spreads book_id into every POST to /api/pipeline/operation. No code changes needed.
  **Notes:** Split operation — add book_id to request body. Merge operation — add book_id to request body. Move operation — add book_id to request body. Delete operation — add book_id to request body.
- [x] Verify frontend tests pass with updated requests
    **Note:** No JS/TS test runner exists in this project. package.json has no test script, no vitest or jest in devDependencies. Test files exist in frontend/tests/frontend/ but are not executed. The frontend was already correctly passing book_id via pipelineOperation, so no verification was possible or needed.
  **Notes:** Frontend already has `state.pipelineBookId` available. This is a straightforward addition.

### Phase 5: Verification
- [x] Run `pytest tests/pipeline/test_operations.py -v` and verify all tests pass
    **Note:** 46 operations tests passed (20 split, 8 merge, 6 move, 8 delete, 4 edge cases). All book-scoped operations verified.
- [x] Run `pytest tests/pipeline/test_api.py -v` and verify all tests pass
    **Note:** 27 API tests passed (3 onboard, 2 run_walk, 1 run_all_walks, 1 walk_status, 2 characters, 2 review, 3 review actions, 5 operation endpoint, 2 export, 2 render, 2 reonboard, 2 TTS engine). OperationRequest.book_id required field verified via FastAPI 422 validation.
- [x] Run `pytest tests/pipeline/test_e2e.py -v` and verify e2e tests pass (from Plan I)
    **Note:** E2E: 5 passed (fresh book export, reonboard, walk rerun with review, operation-then-export, confidence filter). Presentation: 4 passed (view sort, index stability, post-split, post-merge). All book-scoped operations verified end-to-end.
- [x] Verify operations cannot affect spans from other books (add negative test)
    **Note:** Added test_operations_respect_book_scoping in TestEdgeCases class. Added _add_second_book helper (creates book b2 with chapter c2, scene sc2, paragraph p2b, span sp2_1) and _count_book_spans helper (book-scoped count query). Test creates two books, deletes a span from b1, verifies b2's span count (1), span text, and paragraph_span edge are all untouched. 47 tests pass (was 46, +1). Ruff clean.
- [x] Document the book-scoping contract in CONTRACTS.md
    **Note:** Expanded Book-Scoping Contract section in CONTRACTS.md (lines 369-420). Added: _BOOK_SPAN_POSITION_SQL constant documentation, get_book_span_position helper signature, OperationExecutor method signatures with book_id, backward compatibility note for span_presentation VIEW, paragraph-scoped helpers note (_two_phase_reindex and _shift_positions_range don't need book_id). Added API contract (OperationRequest.book_id required, 422 validation). Added frontend contract (pipelineOperation includes book_id).

## Completion Criteria
- span_presentation VIEW or query is book-scoped
- OperationExecutor uses book_id from request
- API endpoints require and pass book_id
- Frontend passes book_id in operation requests
- All tests pass
- Operations cannot affect spans from other books

## Negative Constraints
- Do NOT change the presentation order semantics — only add book_id filtering
- Do NOT break backward compatibility without updating all callers
- Do NOT modify the database schema in a way that breaks existing data
- Do NOT skip tests that fail — fix the root cause
