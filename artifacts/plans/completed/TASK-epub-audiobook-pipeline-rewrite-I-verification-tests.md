# Task: Verification Tests (E2E + Presentation)

## Problem Statement
Plan H claimed to create `test_e2e.py` and `test_presentation.py` with passing tests, but these files do not exist in the repository. This plan creates the missing verification tests to validate the pipeline's end-to-end behavior and presentation ordering semantics.

## Dependencies
- Plan H (deprecation-cleanup) — old pipeline removed, but verification tests never created

## Phases

### Phase 1: End-to-End Integration Tests
- [x] Create `tests/pipeline/test_e2e.py` with 5 test cases covering the full pipeline flow
  **Notes:** Test 1: `test_full_pipeline_fresh_book` — onboard EPUB → run all walks → export → verify structure. Test 2: `test_full_pipeline_reonboard` — re-onboard existing book → verify version bump and walk output clearing. Test 3: `test_walk_rerun_after_ledger_edit` — run walks → edit ledger → rerun affected walks → verify changes. Test 4: `test_operation_then_export` — run walks → perform split/merge/move/delete operations → export → verify presentation order. Test 5: `test_confidence_filter_integration` — run walks with low-confidence responses → verify review items filtered correctly.
- [x] Each test must use `InMemorySQLiteAdapter` for isolation
- [x] Each test must mock LLM responses with deterministic JSON
- [x] Each test must verify the final export structure matches the expected schema
- [x] Run `pytest tests/pipeline/test_e2e.py -v` and verify all 5 tests pass
  **Notes:** These tests validate the integration contract, not individual unit behavior.

### Phase 2: Presentation Ordering Tests
- [x] Create `tests/pipeline/test_presentation.py` with 4 test cases covering presentation semantics
    **Note:** Created tests/pipeline/test_presentation.py with 4 test cases: test_presentation_view_nested_sort (multi-level spine with 2 books × 2 chapters, verifying book.position ordering), test_presentation_index_stability_across_reexport (modify span text, verify indices unchanged), test_presentation_index_after_split (split sp2, verify original keeps index 2, new span gets 3, sp3 shifts to 4), test_presentation_index_after_merge (merge sp2+sp3, verify merged span at index 2, sp4 shifts to 3). All 4 tests pass.
  **Notes:** Test 1: `test_presentation_view_nested_sort` — insert spans with various positions → query span_presentation VIEW → verify ROW_NUMBER() ordering matches nested sort (series.position, book.book_number, chapter.position, scene.position, paragraph.position, span.position). Test 2: `test_presentation_index_stability_across_reexport` — export → modify span → re-export → verify presentation indices are stable (no gaps, no reordering of unchanged spans). Test 3: `test_presentation_index_after_split` — split span at offset → verify new spans have correct presentation indices (original index, original index + 1). Test 4: `test_presentation_index_after_merge` — merge adjacent spans → verify merged span has correct presentation index (minimum of original indices).
- [x] Each test must use `InMemorySQLiteAdapter` for isolation
    **Note:** All 4 tests use InMemorySQLiteAdapter with init_db() called in the storage fixture. Each test builds its own isolated spine via direct SQL INSERTs.
- [x] Each test must verify the span_presentation VIEW returns the expected order
    **Note:** All tests query span_presentation VIEW and verify global_index ordering. Test 1 verifies nested sort across 2 books with different book.position values (b2 position=1 before b1 position=2). Tests 2-4 verify index stability/correctness after export, split, and merge operations.
- [x] Run `pytest tests/pipeline/test_presentation.py -v` and verify all 4 tests pass
    **Note:** pytest tests/pipeline/test_presentation.py -v: 4 passed in 0.48s. Zero lint errors, no production code modified.
  **Notes:** These tests validate the presentation VIEW semantics, not the operation logic.

### Phase 3: Final Verification
- [x] Run `pytest tests/pipeline/ -v --tb=short` and verify all tests pass (including new e2e and presentation tests)
- [x] Verify test coverage for `app/pipeline/assembly.py` and `app/pipeline/operations.py` is ≥80%
- [x] Document any tests that require `soundfile` dependency and mark them with `@pytest.mark.skipif(not HAS_SOUNDFILE, reason="soundfile not installed")`

## Completion Criteria
- `tests/pipeline/test_e2e.py` exists with 5 passing tests
- `tests/pipeline/test_presentation.py` exists with 4 passing tests
- All pipeline tests pass (592 existing + 9 new = 601 total)
- Coverage for assembly.py and operations.py ≥80%
- No tests claim to pass when they don't exist

## Negative Constraints
- Do NOT mock the database — use InMemorySQLiteAdapter
- Do NOT skip tests that fail — fix them or document why they're skipped
- Do NOT fabricate passing test results — if a test fails, investigate and fix the root cause
- Do NOT modify production code to make tests pass — tests validate existing behavior
