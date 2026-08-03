# Task: Walk 2b Character Discovery and Walk 2c Alias Resolution

## Problem Statement
Implement Walk 2b (character discovery) to identify characters in the book, and Walk 2c (alias resolution) to consolidate character references across the book. Walk 2c is GLOBAL scope (task name: script_alias_resolution) — it operates across the entire book, not per-scene.

## Dependencies
- Plan A (Schema, Storage Adapter, Operation Executor, Config) — must be complete
- Plan B (EPUB Extraction, Spine Population, Walk 2a) — must be complete (scenes must exist)

## Phases

### Phase 1: Walk 2b Character Discovery
- [x] Create `app/pipeline/walks/walk_2b_character_discovery.py` with function `execute(book_id, storage, config)`
    **Note:** Created app/pipeline/walks/walk_2b_character_discovery.py with execute(book_id, storage, config) function. Uses resolve_task_llm('character_discovery') for LLM config (temperature=0.1). Returns summary dict with keys: book_id, scenes_processed, characters_created, characters_for_review, errors. Follows walk_2a pattern exactly.
- [x] Use `resolve_task_llm('character_discovery')` to get LLM config (temperature=0.1, LOCAL)
    **Note:** Implemented per-scene LLM character discovery in _process_scene(). Queries scenes via chapter_scene JOIN chapter, then queries paragraphs via scene_paragraph JOIN paragraph. Builds prompt with paragraph text, calls LLM with temperature=0.1, parses JSON response.
- [x] For each scene, send scene text (all paragraphs in scene) to LLM with prompt: "Identify all characters mentioned or present in this scene. Return JSON array with character name, aliases, and whether they are a speaker or just mentioned/present."
    **Note:** Created character entities in _process_character(). Uses UUID4 for character IDs. Checks character_book junction to detect duplicates (name_to_id cache). Inserts character, character_book (source='walk'), and character_series (source='walk') junctions. Aliases stored as JSON array.
- [x] Parse LLM response, create character entities with UUIDs (if not already existing)
    **Note:** Inserted character_scene junctions with relation_type='present' for all discovered characters. CRITICAL: Walk 2b ALWAYS uses relation_type='present' for character_scene (TWO-MEMBERSHIP model). The 'speaker' relation_type in character_scene is reserved for Walk 2d/2e.
- [x] Insert character_scene junctions with relation_type=present (for all characters in scene)
    **Note:** Inserted character_span junctions with relation_type based on LLM role: 'speaker'→speaker, 'mentioned'→mentioned, 'present'→present. Seeds ALL spans in the scene for each character (Walk 2e will refine later). Uses SAVEPOINT for atomicity.
- [x] Insert character_span junctions for spans where characters are speakers (relation_type=speaker) or mentioned (relation_type=mentioned) or present (relation_type=present)
    **Note:** Applied confidence filter in _process_character(): ≥0.7 auto-accept, <0.5 auto-reject (skip), 0.5-0.7 create but flag for review. Default confidence 0.8 if LLM doesn't provide it. characters_for_review only incremented when character is first created (not per scene). All inserts use source='walk'.
- [x] Set source='walk', confidence from LLM output (or default 0.8 if not provided)
    **Note:** Created tests/pipeline/test_walk_2b.py with 20 tests following test_walk_2a.py patterns: InMemorySQLiteAdapter fixture, monkeypatch for resolve_task_llm/create_llm_client, Mock for LLM response. Tests cover: summary dict keys, scene processing, character creation, character_scene junctions (relation_type='present'), character_span junctions (correct relation_types), confidence filter (high/low/medium), character_book/character_series junctions, no duplicates, default confidence, error handling, prompt building, response parsing. All 20 tests pass.
- [x] Apply confidence filter: auto-accept ≥0.7, auto-reject <0.5, between → user review
    **Note:** Updated app/pipeline/walks/runner.py: added 'walk_2b_character_discovery' to WALK_ORDER after walk_2a_scene_segmentation. Added _verify_walk_2b() function that checks character_book and character_scene rows exist for the book. Registered in _VERIFICATIONS dict. All 269 pipeline tests pass.
- [x] Write `tests/pipeline/test_walk_2b.py` — spec-first: test character creation, junction insertion, relation_type values, confidence filtering, LLM call with correct temperature
- [x] Verify: run `pytest tests/pipeline/test_walk_2b.py -v` — all tests pass

### Phase 2: Walk 2c Alias Resolution
- [x] Create `app/pipeline/walks/walk_2c_alias_resolution.py` with function `execute(book_id, storage, config)`
- [x] Use `resolve_task_llm('script_alias_resolution')` to get LLM config (temperature=0.1, GLOBAL scope)
- [x] Collect all characters for the book from character table
- [x] Send full character list (names + aliases) to LLM with prompt: "Identify which characters are actually the same person. Return JSON array of merge groups, each group containing character UUIDs that should be merged."
- [x] Parse LLM response, merge characters: for each merge group, pick one canonical character, update all junctions to point to canonical, delete non-canonical characters
- [x] Update canonical character's aliases array to include all aliases from merged characters
- [x] Apply confidence filter: auto-accept ≥0.7, auto-reject <0.5, between → user review
- [x] Write `tests/pipeline/test_walk_2c.py` — spec-first: test character merging, junction updates, alias consolidation, GLOBAL scope (entire book at once), confidence filtering
- [x] Verify: run `pytest tests/pipeline/test_walk_2c.py -v` — all tests pass

### Phase 3: Character Ledger Construction
- [x] Create `app/pipeline/ledger.py` with class `CharacterLedger` that provides query methods over Graph2
- [x] Implement `get_characters_for_book(book_id)` — returns all characters with their aliases, voice_assignment, and junction counts
- [x] Implement `get_characters_for_scene(scene_id)` — returns characters present/speaking in scene
- [x] Implement `get_characters_for_span(span_id)` — returns characters with relation_type for span
- [x] Implement `get_review_items(book_id, walk_name)` — returns items with confidence between 0.5 and 0.7 (user review queue)
- [x] Write `tests/pipeline/test_ledger.py` — spec-first: test query methods, junction filtering, review item retrieval
- [x] Verify: run `pytest tests/pipeline/test_ledger.py -v` — all tests pass

## Completion Criteria
- Walk 2b discovers characters and creates character_scene + character_span junctions
- Walk 2c resolves aliases across the entire book (GLOBAL scope)
- Character ledger provides query methods for downstream walks and frontend
- All tests pass
- Confidence filter applied: ≥0.7 auto-accept, <0.5 auto-reject, between → user review
