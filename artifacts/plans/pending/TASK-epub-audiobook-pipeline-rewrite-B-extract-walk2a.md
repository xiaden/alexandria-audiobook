# Task: EPUB Extraction, Spine Population, and Walk 2a Scene Segmentation

## Problem Statement
Build the EPUB text extraction pipeline (interim marker-based using CHAP_MARKER/PARA_MARKER), populate the Graph1 TREE spine (series→book→chapter→paragraph→span), and implement Walk 2a scene segmentation. This plan establishes the walk runner infrastructure that all subsequent walks will use.

## Dependencies
- Plan A (Schema, Storage Adapter, Operation Executor, Config) — must be complete

## Phases

### Phase 1: EPUB Text Extraction Enhancement
- [ ] Create `app/pipeline/extract.py` with function `extract_epub_text(epub_path, book_id, storage)` that reads EPUB and builds spine
- [ ] Use interim marker-based extraction: CHAP_MARKER for chapter boundaries, PARA_MARKER for paragraph boundaries (accepted per DD — structured spine is Phase-1 target, not this plan)
- [ ] Generate UUIDs for all spine entities (series, book, chapter, paragraph, span)
- [ ] Split paragraph text into sentence spans (span_type=sentence) using sentence tokenization
- [ ] Detect quotation spans (span_type=quotation) within sentence spans using quotation mark detection
- [ ] Write `tests/pipeline/test_extract.py` — spec-first: test EPUB parsing, UUID generation, spine entity creation, span type detection, sentence splitting
- [ ] Verify: run `pytest tests/pipeline/test_extract.py -v` — all tests pass

### Phase 2: Spine Population
- [ ] Create `app/pipeline/populate.py` with function `populate_spine(series_id, book_id, chapters, storage)` that inserts spine entities into Graph1
- [ ] Insert series row (if not exists), book row with version=1, position from series ordering
- [ ] Insert chapter rows with dense integer positions (UNIQUE(parent_id, position))
- [ ] Before Walk 2a, spine is flat: series→book→chapter→paragraph→span (no scenes yet). Walk 2a inserts scenes between chapters and paragraphs.
- [ ] Implement `populate_initial_spine(series_id, book_id, chapters_data, storage)` — creates series, book, chapters, paragraphs, spans with chapter_paragraph edges (temporary, will be replaced by chapter→scene→paragraph after Walk 2a)
- [ ] Implement scene insertion: `insert_scene(scene_id, chapter_id, paragraph_ids, storage)` — creates scene, creates chapter_scene edge, creates scene_paragraph edges, removes old chapter_paragraph edges
- [ ] Write `tests/pipeline/test_populate.py` — spec-first: test initial spine creation, scene insertion, edge table constraints, position renumbering
- [ ] Verify: run `pytest tests/pipeline/test_populate.py -v` — all tests pass

### Phase 3: Walk Runner Infrastructure
- [ ] Create `app/pipeline/walks/__init__.py` as empty package init
- [ ] Create `app/pipeline/walks/runner.py` with class `WalkRunner` that executes walks serially
- [ ] Implement `run_walk(walk_name, book_id, storage, config)` — loads walk module by name, calls its `execute()` function, logs progress
- [ ] Implement serial execution enforcement: walks run one at a time, each consumes prior walk's output
- [ ] Implement per-walk verification: after each walk, validate expected output exists (e.g., after 2a, scenes exist)
- [ ] Implement walk status tracking: pending/running/completed/failed per walk per book
- [ ] Write `tests/pipeline/test_runner.py` — spec-first: test serial execution, walk status transitions, error handling, verification checks
- [ ] Verify: run `pytest tests/pipeline/test_runner.py -v` — all tests pass

### Phase 4: Walk 2a Scene Segmentation
- [ ] Create `app/pipeline/walks/walk_2a_scene_segmentation.py` with function `execute(book_id, storage, config)`
- [ ] Use `resolve_task_llm('scene_segmentation')` to get LLM config (temperature=0.1, LOCAL)
- [ ] For each chapter, send paragraph text to LLM with prompt: "Identify scene boundaries in this chapter. Return JSON array of scene breaks with paragraph indices."
- [ ] Parse LLM response, create scene entities with UUIDs
- [ ] Insert scenes between chapter and paragraphs: create chapter_scene edges, create scene_paragraph edges, remove old chapter_paragraph edges
- [ ] Apply confidence filter: auto-accept ≥0.7, auto-reject <0.5, between → user review (store in scene metadata or separate review table)
- [ ] Write `tests/pipeline/test_walk_2a.py` — spec-first: test scene boundary detection, edge creation, confidence filtering, LLM call with correct temperature
- [ ] Verify: run `pytest tests/pipeline/test_walk_2a.py -v` — all tests pass

## Completion Criteria
- EPUB extraction produces complete spine (series→book→chapter→paragraph→span)
- Walk 2a inserts scenes between chapters and paragraphs
- Walk runner infrastructure supports serial execution of all 9 walks
- All tests pass
- No parallel walk execution (except TTS render in later plans)
