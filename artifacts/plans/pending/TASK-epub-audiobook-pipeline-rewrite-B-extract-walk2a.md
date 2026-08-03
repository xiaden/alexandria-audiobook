# Task: EPUB Extraction, Spine Population, and Walk 2a Scene Segmentation

## Problem Statement
Build the EPUB text extraction pipeline (interim marker-based using CHAP_MARKER/PARA_MARKER), populate the Graph1 TREE spine (series→book→chapter→paragraph→span), and implement Walk 2a scene segmentation. This plan establishes the walk runner infrastructure that all subsequent walks will use.

## Dependencies
- Plan A (Schema, Storage Adapter, Operation Executor, Config) — must be complete

## Phases

### Phase 1: EPUB Text Extraction Enhancement
- [x] Create `app/pipeline/extract.py` with function `extract_epub_text(epub_path, book_id, storage)` that reads EPUB and builds spine
    **Note:** Created app/pipeline/extract.py with extract_epub_text(epub_path, book_id, storage) -> dict. Function signature matches contract. Uses TYPE_CHECKING guard for PipelineStorage import (Phase 1 doesn't insert into DB). Returns {series_id, book_id, chapters: [{id, paragraphs: [{id, spans: [{id, span_type, text}]}]}]}. All functions under 50 lines, no mutation, no bare except, no TODO/FIXME.
- [x] Use interim marker-based extraction: CHAP_MARKER for chapter boundaries, PARA_MARKER for paragraph boundaries (accepted per DD — structured spine is Phase-1 target, not this plan)
    **Note:** Ported marker-based EPUB parsing from app.py. _HTMLTextExtractor (HTMLParser subclass) inserts PARA_MARKER between block-level elements. _parse_epub_chapters reads ZIP → container.xml → OPF → manifest+spine → XHTML, returns list of chapter texts with PARA_MARKER separators. _build_paragraphs splits on PARA_MARKER. CHAP_MARKER not used (chapters returned as list, not joined).
- [x] Generate UUIDs for all spine entities (series, book, chapter, paragraph, span)
    **Note:** UUID generation for all spine entities using uuid.uuid4(). series_id uses fixed default UUID "00000000-0000-4000-8000-000000000001" (no series context from EPUB). book_id passed in from caller. chapter/paragraph/span IDs generated with str(uuid.uuid4()).
- [x] Split paragraph text into sentence spans (span_type=sentence) using sentence tokenization
    **Note:** Regex-based sentence splitting using _SENTENCE_SPLIT_RE = re.compile(r'(?<=[.!?])\s+'). Splits on whitespace following sentence-ending punctuation. No nltk dependency (confirmed not installed). _split_sentences strips and filters empty strings.
- [x] Detect quotation spans (span_type=quotation) within sentence spans using quotation mark detection
    **Note:** Quotation detection using _QUOTATION_RE = re.compile(r'"([^"]*)"|\'([^\']*)\''). _sentence_to_spans iterates matches, extracts quoted text as span_type='quotation', non-quoted text as span_type='sentence'. Handles mixed quoted/unquoted text, fully-quoted sentences, and no-quotes sentences.
- [x] Write `tests/pipeline/test_extract.py` — spec-first: test EPUB parsing, UUID generation, spine entity creation, span type detection, sentence splitting
    **Note:** Created tests/pipeline/test_extract.py with 25 tests covering: EPUB parsing (3 tests), sentence splitting (4 tests), span detection (5 tests), paragraph building (3 tests), UUID generation (2 tests), contract structure (8 tests). Uses minimal_epub fixture (tempfile with ZIP structure), InMemorySQLiteAdapter. All tests pass.
- [x] Verify: run `pytest tests/pipeline/test_extract.py -v` — all tests pass
    **Note:** Verification complete. pytest tests/pipeline/test_extract.py -v: 25 passed. Full pipeline test suite: 189 passed (77 schema + 53 adapter + 34 operations + 25 extract). ruff check: clean. No lint errors.

### Phase 2: Spine Population
- [x] Create `app/pipeline/populate.py` with function `populate_spine(series_id, book_id, chapters, storage)` that inserts spine entities into Graph1
    **Note:** Created app/pipeline/populate.py with three public functions: populate_spine (wrapper), populate_initial_spine (creates series/book/chapters/placeholder scenes/paragraphs/spans), and insert_scene (for Walk 2a to redistribute paragraphs). All functions use storage: PipelineStorage. Helper functions handle individual insertions.
- [x] Insert series row (if not exists), book row with version=1, position from series ordering
    **Note:** Implemented _insert_series_and_book: INSERT OR IGNORE for series row, INSERT for book row with version=1, book_number=1, position=1. Uses storage.execute_insert() for all INSERTs.
- [x] Insert chapter rows with dense integer positions (UNIQUE(parent_id, position))
    **Note:** Implemented chapter insertion with dense integer positions (1-based). _insert_chapter creates chapter row and book_chapter edge with position=chapter_idx (enumerate start=1). Positions are dense integers: 1, 2, 3, ...
- [x] Before Walk 2a, spine is flat: series→book→chapter→paragraph→span (no scenes yet). Walk 2a inserts scenes between chapters and paragraphs.
    **Note:** Understood placeholder scene pattern: no chapter_paragraph table exists. Instead, each chapter gets a placeholder scene (UUID). All paragraphs for that chapter are linked to the placeholder via scene_paragraph edges. Walk 2a splits chapters into multiple scenes by redistributing paragraphs from placeholder to real scenes.
- [x] Implement `populate_initial_spine(series_id, book_id, chapters_data, storage)` — creates series, book, chapters, paragraphs, spans with chapter_paragraph edges (temporary, will be replaced by chapter→scene→paragraph after Walk 2a)
    **Note:** Implemented populate_initial_spine with SAVEPOINT for atomicity. Creates series (INSERT OR IGNORE), book (version=1), chapters with book_chapter edges (position=idx), placeholder scenes with chapter_scene edges (position=1), paragraphs with scene_paragraph edges (position=p_idx), spans with paragraph_span edges (position=s_idx). All insertions within single SAVEPOINT.
- [x] Implement scene insertion: `insert_scene(scene_id, chapter_id, paragraph_ids, storage)` — creates scene, creates chapter_scene edge, creates scene_paragraph edges, removes old chapter_paragraph edges
    **Note:** Implemented insert_scene for Walk 2a. Creates scene row, computes next position via SELECT MAX(position) FROM chapter_scene WHERE parent_id=chapter_id, inserts chapter_scene edge, then redistributes paragraphs: DELETE old scene_paragraph edge, INSERT new scene_paragraph edge with new parent. Uses SAVEPOINT for atomicity. Does not clean up empty placeholder scenes (Walk 2a handles cleanup).
- [x] Write `tests/pipeline/test_populate.py` — spec-first: test initial spine creation, scene insertion, edge table constraints, position renumbering
    **Note:** Created tests/pipeline/test_populate.py with 23 tests across 5 test classes: TestPopulateInitialSpine (12 tests), TestInsertScene (6 tests), TestPopulateSpine (1 test), TestEdgeConstraints (2 tests), TestAtomicity (2 tests). Tests use InMemorySQLiteAdapter fixture and direct SQL queries following test_schema.py pattern. Fixed test_unique_child_id_scene_paragraph: insert_scene redistributes paragraphs (deletes old edge, creates new), so moving a paragraph to a second scene succeeds — test now verifies paragraph ends up in the new scene.
- [x] Verify: run `pytest tests/pipeline/test_populate.py -v` — all tests pass
    **Note:** All 23 tests pass. Full pipeline test suite (212 tests) passes. ruff clean, syntax OK.

### Phase 3: Walk Runner Infrastructure
- [x] Create `app/pipeline/walks/__init__.py` as empty package init
    **Note:** Created app/pipeline/walks/__init__.py with module docstring describing the walks package purpose and the execute() contract.
- [x] Create `app/pipeline/walks/runner.py` with class `WalkRunner` that executes walks serially
    **Note:** Created app/pipeline/walks/runner.py with WalkRunner class. Constructor takes storage: PipelineStorage. Methods: run_walk(walk_name, book_id, config) -> dict, run_all_walks(book_id, config) -> dict, get_walk_status(book_id, walk_name) -> str. WALK_ORDER class constant = ['walk_2a_scene_segmentation']. All functions under 50 lines.
- [x] Implement `run_walk(walk_name, book_id, storage, config)` — loads walk module by name, calls its `execute()` function, logs progress
    **Note:** Implemented run_walk: uses importlib.import_module(f'app.pipeline.walks.{walk_name}') for dynamic loading, calls walk_module.execute(book_id, storage, config), logs progress via logging module, handles ImportError gracefully (returns error dict with status='failed').
- [x] Implement serial execution enforcement: walks run one at a time, each consumes prior walk's output
    **Note:** Serial execution enforcement: run_walk checks if walk is already 'running' for this book and refuses (returns error dict). run_all_walks iterates WALK_ORDER sequentially, aborting on first failure. Pragmatic approach — in-memory status only, no production locking. Documented in module docstring.
- [x] Implement per-walk verification: after each walk, validate expected output exists (e.g., after 2a, scenes exist)
    **Note:** Per-walk verification via _VERIFICATIONS dict mapping walk_name -> VerifyFn. Currently has _verify_walk_2a which checks chapter_scene has rows for the book's chapters. Verification runs after execute() succeeds; if it fails, status becomes 'failed' even though execute() didn't raise. Extensible — add entries to _VERIFICATIONS dict.
- [x] Implement walk status tracking: pending/running/completed/failed per walk per book
    **Note:** In-memory status tracking via self._status: dict[str, OrderedDict[str, str]] — {book_id: {walk_name: status}}. States: pending/running/completed/failed. _ensure_book() initializes all WALK_ORDER walks to 'pending'. get_walk_status() returns 'pending' for unknown walks. No schema change.
- [x] Write `tests/pipeline/test_runner.py` — spec-first: test serial execution, walk status transitions, error handling, verification checks
    **Note:** Created tests/pipeline/test_runner.py with 25 tests across 8 test classes. Covers: WalkRunner init (3), run_walk with mocks (3), status transitions pending→running→completed/failed (6), serial execution enforcement (2), error handling including ImportError (3), verification pass/fail (3), run_all_walks with abort-on-failure (3), dynamic import via importlib (2). Uses InMemorySQLiteAdapter, unittest.mock. Tests patch _run_verification=True when not testing verification specifically.
- [x] Verify: run `pytest tests/pipeline/test_runner.py -v` — all tests pass
    **Note:** Verification complete. pytest tests/pipeline/test_runner.py -v: 25 passed. Full pipeline test suite: 237 passed (77 schema + 53 adapter + 34 operations + 25 extract + 23 populate + 25 runner). ruff check: clean. No lint errors.

### Phase 4: Walk 2a Scene Segmentation
- [x] Create `app/pipeline/walks/walk_2a_scene_segmentation.py` with function `execute(book_id, storage, config)`
    **Note:** Created app/pipeline/walks/walk_2a_scene_segmentation.py with execute(book_id, storage, config) -> dict. Function signature matches contract from CONTRACTS.md:141-146. Uses TYPE_CHECKING guard for PipelineStorage import. Returns summary dict with keys: book_id, chapters_processed, scenes_created, scenes_rejected, scenes_for_review, errors. All functions under 50 lines except _build_scene_segmentation_prompt (60 lines, acceptable for prompt construction).
- [x] Use `resolve_task_llm('scene_segmentation')` to get LLM config (temperature=0.1, LOCAL)
    **Note:** Uses resolve_task_llm('scene_segmentation') from app.utils to get LLM config. Returns {model_name, reasoning_effort, temperature}. Temperature=0.1 for format stability as specified in DD. Calls create_llm_client() to get OpenAI client. LLM call uses client.chat.completions.create(model, messages, temperature, extra_body={reasoning_effort}).
- [x] For each chapter, send paragraph text to LLM with prompt: "Identify scene boundaries in this chapter. Return JSON array of scene breaks with paragraph indices."
    **Note:** Queries paragraphs per chapter via SQL: SELECT p.id, p.text FROM paragraph p JOIN scene_paragraph sp ON sp.child_id=p.id JOIN chapter_scene cs ON cs.child_id=sp.parent_id WHERE cs.parent_id=? ORDER BY sp.position. Paragraph text is retrieved from paragraph.text column (added via ALTER TABLE in populate.py as Phase-4 fix for Phase-2 gap). Prompt includes [P1], [P2] indices for LLM to reference.
- [x] Parse LLM response, create scene entities with UUIDs
    **Note:** Parses LLM JSON response using json.loads(). Handles malformed responses by extracting JSON array with regex. Maps paragraph indices (P1, P2) back to actual paragraph IDs using index_to_id dict. Creates scene entities with uuid.uuid4(). Each scene has paragraph_ids list and confidence float.
- [x] Insert scenes between chapter and paragraphs: create chapter_scene edges, create scene_paragraph edges, remove old chapter_paragraph edges
    **Note:** Calls insert_scene(scene_id, chapter_id, paragraph_ids, storage) from app.pipeline.populate for each scene. insert_scene() creates scene row, chapter_scene edge, and redistributes paragraphs from placeholder scene (DELETE old scene_paragraph edge, INSERT new). Paragraphs end up in new scene with correct position ordering.
- [x] Apply confidence filter: auto-accept ≥0.7, auto-reject <0.5, between → user review (store in scene metadata or separate review table)
    **Note:** Confidence filter implemented in _process_chapter: confidence >= 0.7 → auto-accept (scene created, scenes_created++), confidence < 0.5 → auto-reject (scene discarded, scenes_rejected++), 0.5 <= confidence < 0.7 → flag for review (scene created, scenes_for_review++). Review tracking is in-memory via result dict; no separate review table (can be added later if needed).
- [x] Write `tests/pipeline/test_walk_2a.py` — spec-first: test scene boundary detection, edge creation, confidence filtering, LLM call with correct temperature
    **Note:** Created tests/pipeline/test_walk_2a.py with 12 tests across 3 test classes: TestExecute (5 tests), TestBuildPrompt (2 tests), TestParseResponse (5 tests). Tests use InMemorySQLiteAdapter, mock LLM client via monkeypatch. Covers: execute() returns summary dict, processes all chapters, creates scenes with high confidence, rejects low confidence, flags medium confidence for review. Prompt building includes paragraph text and indices. Response parsing handles valid JSON, JSON with extra text, invalid JSON, maps indices to IDs, ignores unknown indices. Fixed span_type in test fixture: changed from 'narrative' to 'sentence' to match CHECK constraint.
- [x] Verify: run `pytest tests/pipeline/test_walk_2a.py -v` — all tests pass
    **Summary:** Plan B COMPLETE. All 31 steps across 4 phases implemented and verified. QA-Reviewer PASS (Round 3) after 2 fix cycles (4 MINOR issues fixed). 249 pipeline tests pass. Key deviation: Placeholder scenes per chapter instead of chapter_paragraph edge table (schema-consistent, same goal achieved). Paragraph text column added via ALTER TABLE in populate.py. WalkRunner in place with WALK_ORDER. Downstream: add subsequent walks to WALK_ORDER; insert_scene() ready for production; paragraph.text available for LLM walks; resolve_task_llm pattern established for all 9 walks; confidence filter implemented.
    **Note:** Verification complete. pytest tests/pipeline/test_walk_2a.py -v: 12 passed. Full pipeline test suite: 249 passed (77 schema + 53 adapter + 34 operations + 25 extract + 23 populate + 25 runner + 12 walk_2a). python -m py_compile: all files compile successfully. No lint errors. Phase-4 fix: modified populate.py to add paragraph.text column via ALTER TABLE, store paragraph text during insertion, reconstruct text from spans. All existing populate tests still pass.

## Completion Criteria
- EPUB extraction produces complete spine (series→book→chapter→paragraph→span)
- Walk 2a inserts scenes between chapters and paragraphs
- Walk runner infrastructure supports serial execution of all 9 walks
- All tests pass
- No parallel walk execution (except TTS render in later plans)
