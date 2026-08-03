# Task: Walk 2d Scene Presence, Walk 2e Span Attribution, and Walk 2f Character Description

## Problem Statement
Implement Walk 2d (scene-presence binding), Walk 2e (span speaker attribution), and Walk 2f (character description). These walks refine the character graph by binding characters to scenes, attributing speakers to spans, and generating character descriptions.

## Dependencies
- Plan A (Schema, Storage Adapter, Operation Executor, Config) — must be complete
- Plan B (EPUB Extraction, Spine Population, Walk 2a) — must be complete
- Plan C (Walk 2b Character Discovery, Walk 2c Alias Resolution) — must be complete (characters must exist)

## Phases

### Phase 1: Walk 2d Scene-Presence Binding
- [x] Create `app/pipeline/walks/walk_2d_scene_presence.py` with function `execute(book_id, storage, config)`
    **Note:** Created app/pipeline/walks/walk_2d_scene_presence.py with execute(book_id, storage, config) -> dict. Follows walk_2b house style: module docstring, TYPE_CHECKING import, _process_scene / _process_presence / _build_prompt / _call_llm / _parse_llm_response helpers, SAVEPOINT per scene, confidence filter.
- [x] Use `resolve_task_llm('scene_presence')` to get LLM config (temperature=0.1, LOCAL)
    **Note:** Uses resolve_task_llm('scene_presence', config_path=None) and create_llm_client(config_path=None). Extracts model_name, temperature, reasoning_effort from llm_config dict.
- [x] For each scene, send scene text to LLM with prompt: "Which characters are present in this scene? Return JSON array of character UUIDs with relation_type='present'."
    **Note:** For each scene: queries paragraphs via scene_paragraph, loads existing characters (id+name) via character_book+character, concatenates paragraph text, sends to LLM with system message 'You are a literary analyst specializing in character presence in narrative scenes.' Prompt lists characters as 'name (UUID: id)' and asks for JSON array of {character_id, confidence}.
- [x] Parse LLM response, update character_scene junctions: ensure relation_type=present for all characters in scene
    **Note:** _parse_llm_response returns list of {character_id, confidence}. _process_presence inserts character_scene with relation_type='present', source='walk', human_override=0.
- [x] Note: Walk 2b already created character_scene with relation_type=present. Walk 2d refines this by re-checking presence with more context. If Walk 2b missed a character, Walk 2d adds them.
    **Note:** Walk 2d refines walk 2b: _load_existing_junctions(scene_id) returns set of character_ids already having a junction for this scene. _process_presence skips if character_id in existing_junctions. Also tracks newly created junctions in the set to prevent duplicates within the same walk execution.
- [x] Apply confidence filter: auto-accept ≥0.7, auto-reject <0.5, between → user review
    **Note:** Confidence filter in _process_presence: <0.5 → return (auto-reject, no junction), 0.5-0.7 → create junction + increment junctions_for_review, >=0.7 → create junction only. Confidence clamped to [0,1], defaults to 0.8 if not numeric.
- [x] Write `tests/pipeline/test_walk_2d.py` — spec-first: test scene-presence binding, junction updates, confidence filtering
    **Note:** Created tests/pipeline/test_walk_2d.py with 16 tests across TestExecute, TestBuildPrompt, and TestParseResponse classes. All tests pass.
- [x] Verify: run `pytest tests/pipeline/test_walk_2d.py -v` — all tests pass
    **Note:** Registered walk_2d_scene_presence in runner.py: added to WALK_ORDER after walk_2c_alias_resolution, added _verify_walk_2d function that checks character_scene junctions exist for the book's scenes, added to _VERIFICATIONS dict. All 335 pipeline tests pass (319 existing + 16 new). Ruff lint clean.

### Phase 2: Walk 2e Span Speaker Attribution
- [x] Create `app/pipeline/walks/walk_2e_span_attribution.py` with function `execute(book_id, storage, config)`
    **Note:** Created app/pipeline/walks/walk_2e_span_attribution.py with execute(book_id, storage, config) -> dict. Follows walk_2b/2d house style: module docstring, TYPE_CHECKING import, _process_span / _process_attribution / _build_speaker_attribution_prompt / _call_llm / _parse_llm_response helpers, SAVEPOINT per span, confidence filter. Added text column to span table (schema.py + populate.py migration) to store quotation text for LLM prompts.
- [x] Use `resolve_task_llm('span_attribution')` to get LLM config (temperature=0.1, LOCAL)
    **Note:** Uses resolve_task_llm('span_attribution', config_path=None) and create_llm_client(config_path=None). Extracts model_name, temperature, reasoning_effort from llm_config dict.
- [x] For each span with span_type=quotation, send span text + surrounding context to LLM with prompt: "Who is speaking this quotation? Return JSON with character UUID and relation_type='speaker'."
    **Note:** For each quotation span: queries span text + surrounding context (2-3 spans before/after in same paragraph via _get_surrounding_context), loads existing characters (id+name) via character_book+character, sends to LLM with system message 'You are a literary analyst specializing in dialogue attribution in fiction.' Prompt includes quotation text labeled 'QUOTATION', context spans labeled 'BEFORE' and 'AFTER', and character list as 'name (UUID: id)'. Asks for JSON object with character_id (UUID or null) and confidence.
- [x] Parse LLM response, create/update character_span junctions with relation_type=speaker
    **Note:** _parse_llm_response returns dict with {character_id, confidence}. _process_attribution inserts character_span with relation_type='speaker', source='walk', human_override=0. Handles null character_id (unknown speaker) by incrementing speakers_unknown and returning without creating junction.
- [x] For spans where LLM cannot determine speaker, leave character_span empty (will be handled as UNKNOWN→NARRATOR at TTS boundary)
    **Note:** When LLM returns null/empty character_id or confidence < 0.5, _process_attribution increments speakers_unknown and returns without creating junction. No character_span row is inserted for unknown speakers.
- [x] Apply confidence filter: auto-accept ≥0.7, auto-reject <0.5, between → user review
    **Note:** Confidence filter in _process_attribution: <0.5 → return (auto-reject, no junction), 0.5-0.7 → create junction + increment attributions_for_review, >=0.7 → create junction only. Confidence clamped to [0,1], defaults to 0.8 if not numeric.
- [x] Write `tests/pipeline/test_walk_2e.py` — spec-first: test speaker attribution, character_span junction creation, UNKNOWN handling, confidence filtering
    **Note:** Created tests/pipeline/test_walk_2e.py with 16 tests across TestExecute, TestBuildPrompt, and TestParseResponse classes. Tests cover: summary dict keys, quotation span processing count, speaker junction creation with relation_type='speaker', unknown speaker handling (no junction created, speakers_unknown incremented), confidence filter (high/medium/low), prompt includes quotation+context+character list, JSON parsing (valid/invalid/null character_id). All tests pass.
- [x] Verify: run `pytest tests/pipeline/test_walk_2e.py -v` — all tests pass
    **Note:** Registered walk_2e_span_attribution in runner.py: added to WALK_ORDER after walk_2d_scene_presence, added _verify_walk_2e function that checks quotation spans exist for the book and at least one has character_span junction with relation_type='speaker' (empty books with no quotations are acceptable). Added to _VERIFICATIONS dict. Updated test_schema.py and test_operations.py to include text column in span INSERT statements (schema change). All 351 pipeline tests pass (335 existing + 16 new). Ruff lint clean.

### Phase 3: Walk 2f Character Description
- [x] Create `app/pipeline/walks/walk_2f_character_description.py` with function `execute(book_id, storage, config)`
    **Note:** Created app/pipeline/walks/walk_2f_character_description.py with execute(book_id, storage, config) -> dict. Follows walk_2b/2d/2e house style: module docstring, TYPE_CHECKING import, _process_character / _build_description_prompt / _call_llm / _parse_llm_response / _store_description / _collect_character_spans / _sample_spans helpers, SAVEPOINT per character, confidence filter.
- [x] Use `resolve_task_llm('character_description')` to get LLM config (temperature=0.1, LOCAL)
    **Note:** Uses resolve_task_llm('character_description', config_path=None) and create_llm_client(config_path=None). Extracts model_name, temperature, reasoning_effort from llm_config dict.
- [x] For each character in the book, collect all spans where character is speaker or mentioned
    **Note:** _collect_character_spans(character_id, storage) queries character_span junction with relation_type IN ('speaker', 'mentioned', 'present'), joins to span table for text. _sample_spans takes up to 5 evenly-spaced samples from the collected spans to cover different parts of the book. If no spans found, character is skipped with a warning.
- [x] Send character name + sample spans to LLM with prompt: "Generate a concise description of this character based on the provided text excerpts. Return JSON with description field."
    **Note:** _build_description_prompt includes character name, aliases (parsed from JSON), and sampled span texts labeled with their relation_type (speaker/mentioned/present). System message: 'You are a literary analyst specializing in character analysis and description.' LLM returns JSON object with 'description' and 'confidence' fields.
- [x] Store description in character_metadata table (key='description') — character table has no description column per DD schema
    **Note:** _store_description uses SQLite UPSERT: INSERT INTO character_metadata ... ON CONFLICT(character_id, key) DO UPDATE SET value = excluded.value. This handles both insert and update cases without duplicates.
- [x] Apply confidence filter: auto-accept ≥0.7, auto-reject <0.5, between → user review
    **Note:** Confidence filter in _process_character: <0.5 → return (auto-reject, no storage), 0.5-0.7 → store description + increment descriptions_for_review, >=0.7 → store description only. Confidence clamped to [0,1], defaults to 0.8 if not numeric.
- [x] Write `tests/pipeline/test_walk_2f.py` — spec-first: test description generation, metadata storage, confidence filtering
    **Note:** Created tests/pipeline/test_walk_2f.py with 17 tests covering: execute() summary dict structure, description storage in character_metadata, confidence filter (high/medium/low), skip character with no spans, description update (UPSERT), prompt building (character name, aliases, span texts), JSON parsing (valid/invalid/missing fields). All 17 tests pass.
- [x] Verify: run `pytest tests/pipeline/test_walk_2f.py -v` — all tests pass
    **Note:** Registered walk_2f_character_description in runner.py: added to WALK_ORDER after walk_2e_span_attribution, added _verify_walk_2f function that checks character_metadata for descriptions (empty books with no characters return True, books with characters require at least one description), added to _VERIFICATIONS dict. All 368 pipeline tests pass (351 existing + 17 new). Ruff lint clean.

## Completion Criteria
- Walk 2d refines scene-presence bindings
- Walk 2e attributes speakers to quotation spans
- Walk 2f generates character descriptions
- All walks apply confidence filter correctly
- All tests pass
- UNKNOWN speakers left unattributed (handled at TTS boundary)
