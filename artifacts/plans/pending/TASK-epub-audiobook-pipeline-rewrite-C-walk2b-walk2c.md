# Task: Walk 2b Character Discovery and Walk 2c Alias Resolution

## Problem Statement
Implement Walk 2b (character discovery) to identify characters in the book, and Walk 2c (alias resolution) to consolidate character references across the book. Walk 2c is GLOBAL scope (task name: script_alias_resolution) — it operates across the entire book, not per-scene.

## Dependencies
- Plan A (Schema, Storage Adapter, Operation Executor, Config) — must be complete
- Plan B (EPUB Extraction, Spine Population, Walk 2a) — must be complete (scenes must exist)

## Phases

### Phase 1: Walk 2b Character Discovery
- [ ] Create `app/pipeline/walks/walk_2b_character_discovery.py` with function `execute(book_id, storage, config)`
- [ ] Use `resolve_task_llm('character_discovery')` to get LLM config (temperature=0.1, LOCAL)
- [ ] For each scene, send scene text (all paragraphs in scene) to LLM with prompt: "Identify all characters mentioned or present in this scene. Return JSON array with character name, aliases, and whether they are a speaker or just mentioned/present."
- [ ] Parse LLM response, create character entities with UUIDs (if not already existing)
- [ ] Insert character_scene junctions with relation_type=present (for all characters in scene)
- [ ] Insert character_span junctions for spans where characters are speakers (relation_type=speaker) or mentioned (relation_type=mentioned) or present (relation_type=present)
- [ ] Set source='walk', confidence from LLM output (or default 0.8 if not provided)
- [ ] Apply confidence filter: auto-accept ≥0.7, auto-reject <0.5, between → user review
- [ ] Write `tests/pipeline/test_walk_2b.py` — spec-first: test character creation, junction insertion, relation_type values, confidence filtering, LLM call with correct temperature
- [ ] Verify: run `pytest tests/pipeline/test_walk_2b.py -v` — all tests pass

### Phase 2: Walk 2c Alias Resolution
- [ ] Create `app/pipeline/walks/walk_2c_alias_resolution.py` with function `execute(book_id, storage, config)`
- [ ] Use `resolve_task_llm('script_alias_resolution')` to get LLM config (temperature=0.1, GLOBAL scope)
- [ ] Collect all characters for the book from character table
- [ ] Send full character list (names + aliases) to LLM with prompt: "Identify which characters are actually the same person. Return JSON array of merge groups, each group containing character UUIDs that should be merged."
- [ ] Parse LLM response, merge characters: for each merge group, pick one canonical character, update all junctions to point to canonical, delete non-canonical characters
- [ ] Update canonical character's aliases array to include all aliases from merged characters
- [ ] Apply confidence filter: auto-accept ≥0.7, auto-reject <0.5, between → user review
- [ ] Write `tests/pipeline/test_walk_2c.py` — spec-first: test character merging, junction updates, alias consolidation, GLOBAL scope (entire book at once), confidence filtering
- [ ] Verify: run `pytest tests/pipeline/test_walk_2c.py -v` — all tests pass

### Phase 3: Character Ledger Construction
- [ ] Create `app/pipeline/ledger.py` with class `CharacterLedger` that provides query methods over Graph2
- [ ] Implement `get_characters_for_book(book_id)` — returns all characters with their aliases, voice_assignment, and junction counts
- [ ] Implement `get_characters_for_scene(scene_id)` — returns characters present/speaking in scene
- [ ] Implement `get_characters_for_span(span_id)` — returns characters with relation_type for span
- [ ] Implement `get_review_items(book_id, walk_name)` — returns items with confidence between 0.5 and 0.7 (user review queue)
- [ ] Write `tests/pipeline/test_ledger.py` — spec-first: test query methods, junction filtering, review item retrieval
- [ ] Verify: run `pytest tests/pipeline/test_ledger.py -v` — all tests pass

## Completion Criteria
- Walk 2b discovers characters and creates character_scene + character_span junctions
- Walk 2c resolves aliases across the entire book (GLOBAL scope)
- Character ledger provides query methods for downstream walks and frontend
- All tests pass
- Confidence filter applied: ≥0.7 auto-accept, <0.5 auto-reject, between → user review
