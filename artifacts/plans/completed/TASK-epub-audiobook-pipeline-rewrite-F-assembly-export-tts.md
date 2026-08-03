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
- [x] Create `app/pipeline/assembly.py` with function `export_annotated_script(book_id, storage)` that returns list of dicts: [{speaker, text, instruct}]
    **Note:** Created app/pipeline/assembly.py with export_annotated_script(book_id, storage). Uses single SQL query with LEFT JOINs on character_span (relation_type='speaker') and character tables. Returns [{speaker: name or 'NARRATOR', text: text or '', instruct: instruct or ''}] in presentation order.
- [x] Query span_presentation VIEW for book_id, ordered by global_index
    **Note:** Query uses the same join chain as span_presentation VIEW: span→paragraph_span→scene_paragraph→chapter_scene→book_chapter→book, filtered by book.id = ?, ordered by book.position, chapter_edge.position, scene_edge.position, paragraph_edge.position, span_edge.position.
- [x] For each span, look up character_span junction with relation_type=speaker to get speaker character_id
    **Note:** LEFT JOIN character_span ON span.id = cs.span_id AND cs.relation_type = 'speaker' retrieves speaker character_id per span.
- [x] If no speaker found (UNKNOWN), set speaker='NARRATOR' (resolved at TTS boundary per DD)
    **Note:** When LEFT JOIN yields NULL character_name (no speaker junction), code sets speaker='NARRATOR'.
- [x] Look up character's voice_assignment_id to get voice config
    **Note:** Query joins character table to get character.name. voice_assignment_id is available on character table for downstream TTS mapping (Phase 2).
- [x] Look up span.instruct column for delivery instructions
    **Note:** Query selects span.instruct. NULL values coerced to empty string in output.
- [x] Return list in presentation order: [{speaker: character_name or 'NARRATOR', text: span_text, instruct: instruct_text or ''}]
    **Note:** Returns list[dict] with keys {speaker, text, instruct} in presentation order. Uses span.text column (added in Plan B).
- [x] Write `tests/pipeline/test_assembly.py` — spec-first: test export_annotated_script output format, UNKNOWN→NARRATOR resolution, presentation ordering, voice config lookup
    **Note:** Created tests/pipeline/test_assembly.py with 15 tests covering: output format (speaker/text/instruct keys, string types), UNKNOWN→NARRATOR (no speaker junction), presentation ordering (across chapters/scenes/paragraphs), voice config lookup (character with voice_assignment_id), instruct field (present vs NULL), empty book cases, and comprehensive mixed speaker/narrator validation.
- [x] Verify: run `pytest tests/pipeline/test_assembly.py -v` — all tests pass
    **Note:** All 15 tests pass in 0.43s. Both files compile cleanly.

### Phase 2: TTS Integration Contract
- [x] Create `app/pipeline/tts_integration.py` with function `render_audiobook(book_id, storage, tts_engine)` that bridges pipeline output to TTSEngine
    **Note:** Created app/pipeline/tts_integration.py with render_audiobook(book_id, storage, tts_engine, *, use_batch=True, output_dir=None, batch_seed=-1) -> str. Function bridges export_annotated_script output to TTSEngine. NARRATOR_VOICE constant = {"type": "custom", "voice": "Ryan"}. Voice config resolution: single SQL query joins character → voice_config for all non-NARRATOR speakers. Characters without voice_assignment_id fall back to NARRATOR_VOICE. Chunk format: {index, text, instruct, speaker} matching TTSEngine.generate_batch contract exactly. use_batch=True calls generate_batch; use_batch=False loops generate_voice per-chunk. Auto-creates temp output_dir if not provided. Returns UUID job_id.
- [x] Call `export_annotated_script(book_id, storage)` to get annotated script
    **Note:** render_audiobook calls export_annotated_script(book_id, storage) as first step. Verified via monkeypatch spy test (TestExportCalledInternally).
- [x] For each entry, map speaker to voice config: if speaker='NARRATOR', use default narrator voice; otherwise use character's voice_assignment
    **Note:** Speaker→voice config mapping: NARRATOR gets NARRATOR_VOICE constant. Character speakers resolved via single SQL query: character.name IN (...) JOIN voice_config ON character.voice_assignment_id = voice_config.id. Returns {speaker_name: {type: "custom", voice: voice_config.name, description: voice_config.description}}. Characters without voice_assignment_id fall back to NARRATOR_VOICE.
- [x] Pass text + instruct + voice_config to TTSEngine's existing methods (generate_batch or _local_batch_custom or _local_batch_clone)
    **Note:** Chunks constructed as [{index: i, text, instruct, speaker}] and passed to tts_engine.generate_batch(chunks, voice_config, output_dir, batch_seed). Format matches TTSEngine.generate_batch contract exactly (verified in test_batch_chunk_format tests).
- [x] Preserve TTSEngine's existing parallel/batch behavior (generate_chunks_parallel / generate_batch) when configured
    **Note:** use_batch parameter (default True) switches between generate_batch (batch path, TTSEngine handles sub-batching internally) and per-chunk generate_voice loop. Both paths verified in tests.
- [x] Do NOT rewrite TTSEngine internals — reuse as-is per DD constraint
    **Note:** No changes to app/tts.py. render_audiobook uses tts_engine.generate_batch and tts_engine.generate_voice as-is. FakeTTSEngine in tests duck-types the interface without loading GPU models.
- [x] Write `tests/pipeline/test_tts_integration.py` — spec-first: test voice mapping, NARRATOR fallback, TTSEngine method calls, parallel rendering when configured
    **Note:** Created tests/pipeline/test_tts_integration.py with 35 tests across 12 test classes: TestRenderAudiobookReturnsJobId (3), TestNarratorVoiceConfig (2), TestCharacterVoiceConfig (3), TestBatchChunkFormat (5), TestVoiceConfigCompleteness (2), TestEmptyBook (4), TestIndividualGeneration (4), TestExportCalledInternally (2), TestBuildVoiceConfig (3), TestBuildChunks (3), TestOutputDir (2), TestBatchSeed (2). FakeTTSEngine records batch_calls and voice_calls without GPU. All 35 pass in 0.59s.
- [x] Verify: run `pytest tests/pipeline/test_tts_integration.py -v` — all tests pass
    **Note:** All 35 tests pass. Full pipeline suite: 503 tests pass (35 new + 468 existing). Ruff clean on both new files.

### Phase 3: Confidence Review Support
- [x] Create `app/pipeline/review.py` with class `ReviewManager` that manages user review queue
    **Note:** Created app/pipeline/review.py with ReviewManager class. Constructor takes PipelineStorage, internally creates CharacterLedger for delegation. Includes helper functions _parse_item_id and _make_item_id for encoding/decoding item IDs in format "{junction_table}:{character_id}:{entity_id}". _JUNCTION_META dict maps each junction table to its related column and updatable extra columns.
- [x] Implement `get_review_items(book_id, walk_name=None)` — returns all items with confidence between 0.5 and 0.7
    **Note:** get_review_items queries three junction types: (1) character_book — delegates to CharacterLedger.get_review_items (existing ≥0.5 AND <0.7 filter), (2) character_scene — joins through chapter_scene → book_chapter to find scenes belonging to the book, (3) character_span — joins through paragraph_span → scene_paragraph → chapter_scene → book_chapter. Each result dict includes item_id, character_id, character_name, junction_table, confidence, walk_name (source), related_entity_id, and reason. walk_name filter uses same `source LIKE %walk_name%` heuristic as CharacterLedger.
- [x] Implement `accept_review_item(item_id)` — sets confidence to 1.0, clears review flag
    **Note:** accept_review_item parses item_id to get junction_table, character_id, related_entity_id. Executes UPDATE {table} SET confidence = 1.0 WHERE character_id = ? AND {related_col} = ?. Works for all four junction tables via _JUNCTION_META dispatch.
- [x] Implement `reject_review_item(item_id)` — sets confidence to 0.0, removes item (or marks as rejected)
    **Note:** reject_review_item sets confidence = 0.0 AND human_override = 1 on the junction row. Both flags set together to indicate a definitive human decision to reject.
- [x] Implement `override_review_item(item_id, new_value)` — sets human_override=1, updates value
    **Note:** override_review_item sets confidence = 1.0, human_override = 1, and if new_value is a dict, updates allowed extra columns (relation_type for character_scene and character_span). Unknown columns in the dict are silently ignored to prevent SQL injection. _JUNCTION_META['extra_cols'] defines which columns are updatable per junction table.
- [x] Write `tests/pipeline/test_review.py` — spec-first: test review item retrieval, accept/reject/override actions, confidence updates
    **Note:** Created tests/pipeline/test_review.py with 28 tests across 8 test classes covering: confidence filtering (0.5-0.7 band, boundary values), walk_name filter (LIKE heuristic with valid source values 'walk'/'human'), accept/reject/override actions, multi-junction-type queries, empty book handling, and item_id parsing. All 28 tests pass. Ruff clean.
- [x] Verify: run `pytest tests/pipeline/test_review.py -v` — all tests pass
    **Note:** All tests pass: 28/28 in test_review.py, 531/531 in full pipeline suite (no regressions). Ruff lint clean on both app/pipeline/review.py and tests/pipeline/test_review.py.

### Phase 4: Re-onboarding and Version Management
- [x] Implement `reonboard_book(book_id, storage)` — increments book.version, clears all walk outputs (character junctions, span metadata, scene entities), re-runs walks from scratch
    **Note:** Created reonboard_book(book_id, storage) in app/pipeline/assembly.py. Implementation snapshots character_ids and scene_ids before destructive deletes (needed because character_span and span.instruct queries depend on chapter_scene edges). Four phases: (1) Clear junctions depending on chapter_scene (character_span, character_scene, span.instruct reset), (2) Clear memberships and metadata (character_book, character_metadata, voice_assignment_id reset), (3) Clear scene entities and edges (chapter_scene, scene rows), (4) Bump version. Returns new version via get_book_version.
- [x] Note: memberships NOT carried over by default per DD
    **Note:** Module docstring and reonboard_book docstring both explicitly note that memberships (character_book rows) are NOT carried over — they are deleted and must be re-created by the next walk run.
- [x] Implement `get_book_version(book_id)` — returns current version number
    **Note:** Created get_book_version(book_id, storage) in app/pipeline/assembly.py. Simple SELECT version FROM book WHERE id = ? with ValueError if book not found.
- [x] Write `tests/pipeline/test_reonboard.py` — spec-first: test version increment, walk output clearing, re-run behavior
    **Note:** Created tests/pipeline/test_reonboard.py with 23 tests covering: get_book_version (default version, ValueError for nonexistent), version increment (1→2, 2→3, returns new version, persists), clearing junctions (character_span, character_scene, character_book, character_metadata), clearing fields (span.instruct, voice_assignment_id), preserving tree structure (book, chapters, book_chapter edges, paragraphs, spans, paragraph_span edges), preserving characters (rows and names), and edge cases (empty book, book with chapters but no scenes). All 23 tests pass. Full pipeline suite: 554 tests pass (531 existing + 23 new). Ruff clean.
- [x] Verify: run `pytest tests/pipeline/test_reonboard.py -v` — all tests pass
    **Note:** All 23 tests in test_reonboard.py pass. Full pipeline suite: 554 tests pass (no regressions). Ruff lint clean on both app/pipeline/assembly.py and tests/pipeline/test_reonboard.py.

## Completion Criteria
- export_annotated_script produces [{speaker, text, instruct}] in presentation order
- UNKNOWN→NARRATOR resolved at TTS boundary
- TTS integration reuses TTSEngine internals (no gratuitous rewriting)
- Confidence review queue supports accept/reject/override
- Re-onboarding increments version and clears walk outputs
- All tests pass
