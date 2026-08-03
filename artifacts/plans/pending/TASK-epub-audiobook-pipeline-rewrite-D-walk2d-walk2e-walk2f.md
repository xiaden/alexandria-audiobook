# Task: Walk 2d Scene Presence, Walk 2e Span Attribution, and Walk 2f Character Description

## Problem Statement
Implement Walk 2d (scene-presence binding), Walk 2e (span speaker attribution), and Walk 2f (character description). These walks refine the character graph by binding characters to scenes, attributing speakers to spans, and generating character descriptions.

## Dependencies
- Plan A (Schema, Storage Adapter, Operation Executor, Config) — must be complete
- Plan B (EPUB Extraction, Spine Population, Walk 2a) — must be complete
- Plan C (Walk 2b Character Discovery, Walk 2c Alias Resolution) — must be complete (characters must exist)

## Phases

### Phase 1: Walk 2d Scene-Presence Binding
- [ ] Create `app/pipeline/walks/walk_2d_scene_presence.py` with function `execute(book_id, storage, config)`
- [ ] Use `resolve_task_llm('scene_presence')` to get LLM config (temperature=0.1, LOCAL)
- [ ] For each scene, send scene text to LLM with prompt: "Which characters are present in this scene? Return JSON array of character UUIDs with relation_type='present'."
- [ ] Parse LLM response, update character_scene junctions: ensure relation_type=present for all characters in scene
- [ ] Note: Walk 2b already created character_scene with relation_type=present. Walk 2d refines this by re-checking presence with more context. If Walk 2b missed a character, Walk 2d adds them.
- [ ] Apply confidence filter: auto-accept ≥0.7, auto-reject <0.5, between → user review
- [ ] Write `tests/pipeline/test_walk_2d.py` — spec-first: test scene-presence binding, junction updates, confidence filtering
- [ ] Verify: run `pytest tests/pipeline/test_walk_2d.py -v` — all tests pass

### Phase 2: Walk 2e Span Speaker Attribution
- [ ] Create `app/pipeline/walks/walk_2e_span_attribution.py` with function `execute(book_id, storage, config)`
- [ ] Use `resolve_task_llm('span_attribution')` to get LLM config (temperature=0.1, LOCAL)
- [ ] For each span with span_type=quotation, send span text + surrounding context to LLM with prompt: "Who is speaking this quotation? Return JSON with character UUID and relation_type='speaker'."
- [ ] Parse LLM response, create/update character_span junctions with relation_type=speaker
- [ ] For spans where LLM cannot determine speaker, leave character_span empty (will be handled as UNKNOWN→NARRATOR at TTS boundary)
- [ ] Apply confidence filter: auto-accept ≥0.7, auto-reject <0.5, between → user review
- [ ] Write `tests/pipeline/test_walk_2e.py` — spec-first: test speaker attribution, character_span junction creation, UNKNOWN handling, confidence filtering
- [ ] Verify: run `pytest tests/pipeline/test_walk_2e.py -v` — all tests pass

### Phase 3: Walk 2f Character Description
- [ ] Create `app/pipeline/walks/walk_2f_character_description.py` with function `execute(book_id, storage, config)`
- [ ] Use `resolve_task_llm('character_description')` to get LLM config (temperature=0.1, LOCAL)
- [ ] For each character in the book, collect all spans where character is speaker or mentioned
- [ ] Send character name + sample spans to LLM with prompt: "Generate a concise description of this character based on the provided text excerpts. Return JSON with description field."
- [ ] Store description in character_metadata table (key='description') — character table has no description column per DD schema
- [ ] Apply confidence filter: auto-accept ≥0.7, auto-reject <0.5, between → user review
- [ ] Write `tests/pipeline/test_walk_2f.py` — spec-first: test description generation, metadata storage, confidence filtering
- [ ] Verify: run `pytest tests/pipeline/test_walk_2f.py -v` — all tests pass

## Completion Criteria
- Walk 2d refines scene-presence bindings
- Walk 2e attributes speakers to quotation spans
- Walk 2f generates character descriptions
- All walks apply confidence filter correctly
- All tests pass
- UNKNOWN speakers left unattributed (handled at TTS boundary)
