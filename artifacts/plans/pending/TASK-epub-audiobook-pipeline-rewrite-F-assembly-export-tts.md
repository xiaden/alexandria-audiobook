# Task: Assembly, Export, TTS Integration, and Confidence Review

## Problem Statement
Implement deterministic assembly (export_annotated_script), TTS integration contract (reuse TTSEngine), UNKNOWN→NARRATOR resolution at TTS boundary, and confidence review UI support. This plan bridges the pipeline output to the existing TTS infrastructure.

## Dependencies
- Plan A (Schema, Storage Adapter, Operation Executor, Config) — must be complete
- Plan B (EPUB Extraction, Spine Population, Walk 2a) — must be complete
- Plan C (Walk 2b Character Discovery, Walk 2c Alias Resolution) — must be complete
- Plan D (Walk 2d Scene Presence, Walk 2e Span Attribution, Walk 2f Character Description) — must be complete
- Plan E (Walk 2g Voice Audition, Walk 2h Voice Assignment, Walk 2i Delivery) — must be complete (all walks done)

## Phases

### Phase 1: Deterministic Assembly
- [ ] Create `app/pipeline/assembly.py` with function `export_annotated_script(book_id, storage)` that returns list of dicts: [{speaker, text, instruct}]
- [ ] Query span_presentation VIEW for book_id, ordered by global_index
- [ ] For each span, look up character_span junction with relation_type=speaker to get speaker character_id
- [ ] If no speaker found (UNKNOWN), set speaker='NARRATOR' (resolved at TTS boundary per DD)
- [ ] Look up character's voice_assignment_id to get voice config
- [ ] Look up span.instruct column for delivery instructions
- [ ] Return list in presentation order: [{speaker: character_name or 'NARRATOR', text: span_text, instruct: instruct_text or ''}]
- [ ] Write `tests/pipeline/test_assembly.py` — spec-first: test export_annotated_script output format, UNKNOWN→NARRATOR resolution, presentation ordering, voice config lookup
- [ ] Verify: run `pytest tests/pipeline/test_assembly.py -v` — all tests pass

### Phase 2: TTS Integration Contract
- [ ] Create `app/pipeline/tts_integration.py` with function `render_audiobook(book_id, storage, tts_engine)` that bridges pipeline output to TTSEngine
- [ ] Call `export_annotated_script(book_id, storage)` to get annotated script
- [ ] For each entry, map speaker to voice config: if speaker='NARRATOR', use default narrator voice; otherwise use character's voice_assignment
- [ ] Pass text + instruct + voice_config to TTSEngine's existing methods (generate_batch or _local_batch_custom or _local_batch_clone)
- [ ] Preserve TTSEngine's existing parallel/batch behavior (generate_chunks_parallel / generate_batch) when configured
- [ ] Do NOT rewrite TTSEngine internals — reuse as-is per DD constraint
- [ ] Write `tests/pipeline/test_tts_integration.py` — spec-first: test voice mapping, NARRATOR fallback, TTSEngine method calls, parallel rendering when configured
- [ ] Verify: run `pytest tests/pipeline/test_tts_integration.py -v` — all tests pass

### Phase 3: Confidence Review Support
- [ ] Create `app/pipeline/review.py` with class `ReviewManager` that manages user review queue
- [ ] Implement `get_review_items(book_id, walk_name=None)` — returns all items with confidence between 0.5 and 0.7
- [ ] Implement `accept_review_item(item_id)` — sets confidence to 1.0, clears review flag
- [ ] Implement `reject_review_item(item_id)` — sets confidence to 0.0, removes item (or marks as rejected)
- [ ] Implement `override_review_item(item_id, new_value)` — sets human_override=1, updates value
- [ ] Write `tests/pipeline/test_review.py` — spec-first: test review item retrieval, accept/reject/override actions, confidence updates
- [ ] Verify: run `pytest tests/pipeline/test_review.py -v` — all tests pass

### Phase 4: Re-onboarding and Version Management
- [ ] Implement `reonboard_book(book_id, storage)` — increments book.version, clears all walk outputs (character junctions, span metadata, scene entities), re-runs walks from scratch
- [ ] Note: memberships NOT carried over by default per DD
- [ ] Implement `get_book_version(book_id)` — returns current version number
- [ ] Write `tests/pipeline/test_reonboard.py` — spec-first: test version increment, walk output clearing, re-run behavior
- [ ] Verify: run `pytest tests/pipeline/test_reonboard.py -v` — all tests pass

## Completion Criteria
- export_annotated_script produces [{speaker, text, instruct}] in presentation order
- UNKNOWN→NARRATOR resolved at TTS boundary
- TTS integration reuses TTSEngine internals (no gratuitous rewriting)
- Confidence review queue supports accept/reject/override
- Re-onboarding increments version and clears walk outputs
- All tests pass
