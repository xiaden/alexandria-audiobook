# Task: Deletion, Integration Tests, and Final Cleanup

## Problem Statement
DELETE the old pipeline files (generate_script.py, review_script.py, generate_personas.py) and all their references — no half-migrations, no deprecation-but-keep. Write end-to-end integration tests, verify presentation ordering correctness, and perform final cleanup. This plan closes out the v3 migration.

## Dependencies
- Plan A (Schema, Storage Adapter, Operation Executor, Config) — must be complete
- Plan B (EPUB Extraction, Spine Population, Walk 2a) — must be complete
- Plan C (Walk 2b Character Discovery, Walk 2c Alias Resolution) — must be complete
- Plan D (Walk 2d Scene Presence, Walk 2e Span Attribution, Walk 2f Character Description) — must be complete
- Plan E (Walk 2g Voice Audition, Walk 2h Voice Assignment, Walk 2i Delivery) — must be complete
- Plan F (Assembly, Export, TTS Integration, Confidence Review) — must be complete
- Plan G (API Endpoints, Frontend Rewiring) — must be complete

## Phases

### Phase 1: Old Pipeline Deletion
- [ ] DELETE `app/generate_script.py` entirely from the repository
- [ ] DELETE `app/review_script.py` entirely from the repository
- [ ] DELETE `app/generate_personas.py` entirely from the repository
- [ ] DELETE their associated test files (any script-specific tests under tests/)
- [ ] REMOVE all imports and references to these files from `app.py` — including any 410 fallback logic. No import-reachable old code remains.
- [ ] Remove any documentation references (README.md, inline docs) to the deleted files
- [ ] Write `tests/pipeline/test_old_pipeline_removed.py` — **temporary verification artifact**: spec-first: verify the three files are absent from the repo, verify no dangling imports in app.py, verify no test files reference the deleted modules. This file exists solely for one-time cutover verification and will be deleted after it passes.
- [ ] Verify: run `pytest tests/pipeline/test_old_pipeline_removed.py -v` — all tests pass
- [ ] DELETE `tests/pipeline/test_old_pipeline_removed.py` — it has served its one-time verification purpose; it is a throwaway artifact, not a permanent test file

### Phase 2: End-to-End Integration Tests
- [ ] Create `tests/pipeline/test_e2e.py` with integration tests that exercise the full pipeline
- [ ] Test 1: Onboard EPUB → run all 9 walks → export annotated script → verify output format
- [ ] Test 2: Onboard EPUB → run walks 2a-2c → verify character ledger → run walks 2d-2f → verify speaker attributions → run walks 2g-2i → verify voice assignments and instruct fields
- [ ] Test 3: Onboard EPUB → run all walks → perform operation (split/merge/move/delete) → verify presentation indices updated correctly
- [ ] Test 4: Onboard EPUB → run all walks → re-onboard → verify version incremented, walk outputs cleared
- [ ] Test 5: Onboard EPUB → run all walks → export → render via TTS integration → verify TTSEngine called with correct parameters
- [ ] Use in-memory SQLite adapter for all tests (per DD: in-memory SQLite per test session)
- [ ] Verify: run `pytest tests/pipeline/test_e2e.py -v` — all tests pass

### Phase 3: Presentation Ordering Tests
- [ ] Create `tests/pipeline/test_presentation.py` with tests for span_presentation VIEW ordering
- [ ] Test 1: Verify global_index is correct for nested sort (book.position, chapter.position, paragraph.position, span.position)
- [ ] Test 2: Verify ordering across multiple books in a series
- [ ] Test 3: Verify ordering after operations (split/merge/move/delete renumber positions correctly)
- [ ] Test 4: Verify ordering after re-onboarding (new version, same ordering)
- [ ] Write `tests/pipeline/test_presentation.py` — spec-first: test nested sort correctness, operation renumbering, multi-book ordering
- [ ] Verify: run `pytest tests/pipeline/test_presentation.py -v` — all tests pass

### Phase 4: Final Cleanup
- [ ] Remove any temporary code or markers from development (e.g., debug prints, TODO comments that are now resolved)
- [ ] Verify all tests pass: `pytest tests/pipeline/ -v`
- [ ] Verify code coverage meets targets: storage 100%, operation executor 100%, walks 80%, frontend 60%
- [ ] Update CONTRACTS.md with final method signatures
- [ ] Update README.md with final plan structure and dependency graph
- [ ] Archive old v2 design artifacts (if not already archived)
- [ ] Log final planner observations about v3 migration

## Completion Criteria
- Old pipeline files (generate_script.py, review_script.py, generate_personas.py) DELETED — not deprecated, not kept for reference
- All imports and references to deleted files removed from app.py — no 410 fallback, no dangling imports
- Old pipeline test files deleted
- Cutover verification ran and passed confirming old pipeline files are absent and no dangling imports remain; temporary verification file (`test_old_pipeline_removed.py`) was then deleted
- End-to-end integration tests pass (full pipeline exercise)
- Presentation ordering tests pass (nested sort correctness verified)
- All tests pass with coverage targets met
- CONTRACTS.md and README.md updated
- v3 migration complete
