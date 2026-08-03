# Task: API Endpoints and Frontend Rewiring

## Problem Statement
Rewire app.py to expose /api/pipeline/* endpoints replacing the old /api/generate_script, /api/review_script, /api/generate_personas endpoints. Rewire the 4 frontend tabs (Setup, Script, Voices, Editor) to use the new pipeline API. The config toggle is a ONE-TIME cutover switch: when flipped ON, the old endpoints are REMOVED entirely — not left returning 410. No persistent dual-path, no toggle-gated coexistence.

## Dependencies
- Plan A (Schema, Storage Adapter, Operation Executor, Config) — must be complete
- Plan B (EPUB Extraction, Spine Population, Walk 2a) — must be complete
- Plan C (Walk 2b Character Discovery, Walk 2c Alias Resolution) — must be complete
- Plan D (Walk 2d Scene Presence, Walk 2e Span Attribution, Walk 2f Character Description) — must be complete
- Plan E (Walk 2g Voice Audition, Walk 2h Voice Assignment, Walk 2i Delivery) — must be complete
- Plan F (Assembly, Export, TTS Integration, Confidence Review) — must be complete

## Phases

### Phase 1: Pipeline API Endpoints
- [ ] Create `app/pipeline/api.py` with FastAPI router for /api/pipeline/* endpoints
- [ ] Implement POST /api/pipeline/onboard — accepts EPUB file, calls extract_epub_text, populates spine, returns book_id
- [ ] Implement POST /api/pipeline/run_walk — accepts walk_name + book_id, runs walk via WalkRunner, returns status
- [ ] Implement POST /api/pipeline/run_all_walks — accepts book_id, runs all 9 walks serially via WalkRunner
- [ ] Implement GET /api/pipeline/walk_status/{book_id} — returns walk status (pending/running/completed/failed) for each walk
- [ ] Implement GET /api/pipeline/characters/{book_id} — returns character ledger for book
- [ ] Implement GET /api/pipeline/review/{book_id} — returns review items (confidence 0.5-0.7)
- [ ] Implement POST /api/pipeline/review/accept — accepts review item
- [ ] Implement POST /api/pipeline/review/reject — rejects review item
- [ ] Implement POST /api/pipeline/review/override — overrides review item
- [ ] Implement POST /api/pipeline/operation — accepts operation type (split/merge/move/delete) + presentation indices, calls OperationExecutor
- [ ] Implement GET /api/pipeline/export/{book_id} — calls export_annotated_script, returns JSON
- [ ] Implement POST /api/pipeline/render — calls tts_integration.render_audiobook, returns job_id
- [ ] Implement POST /api/pipeline/reonboard — re-onboards book (version++, clear walks, re-run)
- [ ] Write `tests/pipeline/test_api.py` — spec-first: test each endpoint, request/response format, error handling
- [ ] Verify: run `pytest tests/pipeline/test_api.py -v` — all tests pass

### Phase 2: Old Endpoint Removal at Cutover
- [ ] The config toggle is a ONE-TIME cutover switch, not a persistent mode selector. When flipped ON, the old pipeline is proven and the old endpoints are DELETED.
- [ ] REMOVE the following endpoints from app.py entirely: /api/generate_script, /api/review_script, /api/review_script_contextual, /api/generate_personas. They are not left returning 410 — they are gone.
- [ ] Remove all imports and handler functions for the old endpoints from app.py
- [ ] There is no "old endpoints continue to work" mode. Once cutover happens, the old pipeline is gone in a single cutover commit.
- [ ] Write `tests/pipeline/test_cutover.py` — spec-first: test that the old endpoints are ABSENT (return 404) after cutover, test that no handler functions for old endpoints remain in app.py. This is a temporary verification artifact — it exists solely for one-time cutover verification and will be deleted after it passes.
- [ ] Verify: run `pytest tests/pipeline/test_cutover.py -v` — all tests pass
- [ ] DELETE `tests/pipeline/test_cutover.py` — it has served its one-time cutover verification purpose; it is a throwaway artifact, not a permanent test file

### Phase 3: Frontend Setup Tab Rewiring
- [ ] Update `frontend/src/tabs/setup.ts` to display 9 walk task names in task overrides UI
- [ ] Update loadConfig and collectTaskOverrides to handle new walk task names
- [ ] Add pipeline toggle switch to Setup tab (enables/disables pipeline mode)
- [ ] Write `tests/frontend/test_setup.test.ts` — spec-first: test 9 walk task names display, pipeline toggle behavior
- [ ] Verify: run frontend tests — all pass

### Phase 4: Frontend Script Tab Rewiring
- [ ] Update `frontend/src/tabs/script.ts` to use /api/pipeline/* endpoints
- [ ] Replace old script generation UI with pipeline onboard + walk execution UI
- [ ] Display walk status for each walk (pending/running/completed/failed)
- [ ] Add "Run All Walks" button
- [ ] Add "Re-onboard" button
- [ ] Write `tests/frontend/test_script.test.ts` — spec-first: test pipeline API calls, walk status display, button behavior
- [ ] Verify: run frontend tests — all pass

### Phase 5: Frontend Voices Tab Rewiring
- [ ] Update `frontend/src/tabs/voices.ts` to use /api/pipeline/characters/{book_id} endpoint
- [ ] Replace old generatePersonas with character ledger display
- [ ] Display characters with aliases, voice assignment, confidence
- [ ] Add voice assignment editor (dropdown of available voices)
- [ ] Write `tests/frontend/test_voices.test.ts` — spec-first: test character ledger display, voice assignment editing
- [ ] Verify: run frontend tests — all pass

### Phase 6: Frontend Editor Tab Rewiring
- [ ] Update `frontend/src/tabs/editor.ts` to use /api/pipeline/* endpoints
- [ ] Replace old chunk-based editing with presentation-index-based operations
- [ ] Add operation buttons: split, merge, move, delete (call /api/pipeline/operation)
- [ ] Display span_presentation VIEW data with global_index, speaker, text, instruct
- [ ] Add confidence review UI: display review items, accept/reject/override buttons
- [ ] Preserve existing TTS rendering functionality (renderAll, renderBatchFast, mergeAudiobook) — these call /api/pipeline/render
- [ ] Write `tests/frontend/test_editor.test.ts` — spec-first: test operation buttons, confidence review UI, TTS rendering
- [ ] Verify: run frontend tests — all pass

## Completion Criteria
- All /api/pipeline/* endpoints implemented and tested
- Old endpoints (/api/generate_script, /api/review_script, /api/review_script_contextual, /api/generate_personas) REMOVED at cutover — not returning 410, not toggle-gated, just gone
- No handler functions or imports for old endpoints remain in app.py
- Frontend 4 tabs (Setup, Script, Voices, Editor) rewired to use pipeline API
- Frontend 5 tabs (Preparer, Dataset, Training, Audio, Designer) unchanged
- All tests pass (API: 100%, frontend: 60% coverage target)
- Cutover verification (`test_cutover.py`) ran and passed confirming old endpoints are absent; the temporary verification file was then deleted
