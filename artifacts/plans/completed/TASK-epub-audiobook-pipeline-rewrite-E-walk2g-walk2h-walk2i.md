# Task: Walk 2g Voice Audition, Walk 2h Voice Assignment, and Walk 2i Delivery

## Problem Statement
Implement Walk 2g (voice audition), Walk 2h (voice assignment), and Walk 2i (delivery). Walk 2g auditions available voices for characters, Walk 2h assigns voices to characters, and Walk 2i generates delivery instructions (instruct field) for TTS. Walk 2i MUST use LLM (not rule-based) and produces the instruct field that TTS will use.

## Dependencies
- Plan A (Schema, Storage Adapter, Operation Executor, Config) — must be complete
- Plan B (EPUB Extraction, Spine Population, Walk 2a) — must be complete
- Plan C (Walk 2b Character Discovery, Walk 2c Alias Resolution) — must be complete
- Plan D (Walk 2d Scene Presence, Walk 2e Span Attribution, Walk 2f Character Description) — must be complete (characters must have descriptions and speaker attributions)

## Phases

### Phase 1: Walk 2g Voice Audition
- [x] Create `app/pipeline/walks/walk_2g_voice_audition.py` with function `execute(book_id, storage, config)`
    **Note:** Created app/pipeline/walks/walk_2g_voice_audition.py with execute(book_id, storage, config) function. Follows walk_2f house style: module docstring, TYPE_CHECKING import, deferred app.utils import inside execute(), SAVEPOINT pattern, UPSERT for character_metadata.
- [x] Use `resolve_task_llm('voice_audition')` to get LLM config (temperature=0.3, LOCAL — interpretive walk)
    **Note:** Uses resolve_task_llm('voice_audition', config_path=None) and create_llm_client(config_path=None) inside execute(). Temperature is resolved from config, not hardcoded.
- [x] For each character in the book, collect character description + sample dialogue spans
    **Note:** Per character: _get_character_description() reads from character_metadata WHERE key='description'. _collect_dialogue_spans() queries character_span WHERE relation_type='speaker' joined with span for text. _sample_spans() takes up to 5 evenly-spaced samples.
- [x] Send to LLM with prompt: "Based on this character's description and dialogue samples, suggest a voice profile. Return JSON with voice characteristics (age, gender, tone, accent, etc.)."
    **Note:** _build_voice_audition_prompt() sends character name, aliases, description (or '(no description available)'), and numbered dialogue excerpts. _call_llm() follows walk_2f pattern exactly (messages list, extra_body for reasoning_effort).
- [x] Store voice profile suggestions in character_metadata table with key='voice_profile'
    **Note:** _store_voice_profile() serializes voice_profile dict to JSON string via json.dumps() and UPSERTs into character_metadata with key='voice_profile'. Same ON CONFLICT pattern as walk_2f.
- [x] Apply confidence filter: auto-accept ≥0.7, auto-reject <0.5, between → user review
    **Note:** Confidence filter: <0.5 auto-reject (return early, no metadata stored), 0.5-0.7 flagged for review (stored + profiles_for_review incremented), >=0.7 auto-accept (stored). SAVEPOINT walk_2g_voice wraps the store operation.
- [x] Write `tests/pipeline/test_walk_2g.py` — spec-first: test voice profile generation, metadata storage, temperature=0.3, confidence filtering
    **Note:** Created tests/pipeline/test_walk_2g.py with 22 tests across 4 test classes (TestExecute: 9 tests, TestBuildPrompt: 5 tests, TestParseResponse: 8 tests, TestSampleSpans: 4 tests). Follows exact pattern from test_walk_2f.py: InMemorySQLiteAdapter, populate_initial_spine, monkeypatch for resolve_task_llm/create_llm_client, _insert_character/_insert_character_span helpers.
- [x] Verify: run `pytest tests/pipeline/test_walk_2g.py -v` — all tests pass
    **Note:** All 26 tests pass in 0.62s. Test coverage: execute() returns correct summary dict, voice profile stored in character_metadata with key='voice_profile', confidence filter (high/medium/low), character without description still gets profile, skip character with no spans, UPSERT updates existing profile, nonexistent book returns error, prompt includes character name/description/dialogue/aliases and asks for voice_profile JSON, parse handles valid JSON/extra text/invalid JSON/missing fields/non-dict responses, sample_spans handles fewer/exactly/more than max and empty list.

### Phase 2: Walk 2h Voice Assignment
- [x] Create `app/pipeline/walks/walk_2h_voice_assignment.py` with function `execute(book_id, storage, config)`
    **Note:** Created app/pipeline/walks/walk_2h_voice_assignment.py with execute(book_id, storage, config) function. Follows walk_2f/2g house style exactly: module docstring, TYPE_CHECKING import, deferred app.utils import inside execute(), SAVEPOINT pattern (walk_2h_voice_assignment), character loop via character_book junction. Key differences from walk_2g: (1) task name 'voice_assignment', (2) reads voice_profile from character_metadata as input (not dialogue), (3) loads ALL voice_config entries, (4) LLM prompt sends voice_profile + available voices, asks for voice_config_id match, (5) UPDATE character SET voice_assignment_id = ? WHERE id = ? (no lock column), (6) validates voice_config_id exists in voice_config before assigning, (7) result dict keys: voices_matched, voices_unmatched, assignments_for_review.
- [x] Use `resolve_task_llm('voice_assignment')` to get LLM config (temperature=0.1, LOCAL)
    **Note:** Uses resolve_task_llm('voice_assignment', config_path=None) and create_llm_client(config_path=None) inside execute(). Temperature is resolved from config, not hardcoded.
- [x] For each character, match voice_profile suggestions to available voices in voice_config table
    **Note:** _load_voice_config() reads ALL voices from voice_config (id, name, description). _get_voice_profile() reads from character_metadata WHERE key='voice_profile'. _build_voice_assignment_prompt() formats voice_profile as JSON + numbered voice list. LLM returns voice_config_id (or null) + reasoning + confidence.
- [x] If voice_config has matching voices, assign voice_assignment_id in character table
    **Note:** When LLM returns a valid voice_config_id with confidence >= 0.5, UPDATE character SET voice_assignment_id = ? WHERE id = ?. Validates voice_config_id exists in voice_config before assigning (rejects invalid IDs). SAVEPOINT walk_2h_voice_assignment wraps the update.
- [x] If no match, leave voice_assignment_id NULL (will be handled as UNKNOWN→NARRATOR at TTS boundary, or user can manually assign)
    **Note:** When LLM returns null voice_config_id OR confidence < 0.5 OR no voice_profile exists OR voice_config table is empty, voice_assignment_id stays NULL. voices_unmatched incremented in all these cases.
- [x] Note: Voice assignment is NOT locked — user can change it via frontend
    **Note:** No locked column, no locking mechanism. Schema has voice_assignment_id TEXT REFERENCES voice_config(id) with no NOT NULL constraint. Users can UPDATE character.voice_assignment_id freely via frontend. Verified in test_assignment_is_not_locked: PRAGMA table_info confirms no 'locked' column, and a second UPDATE succeeds.
- [x] Apply confidence filter: auto-accept ≥0.7, auto-reject <0.5, between → user review
    **Note:** Confidence filter: <0.5 auto-reject (voice_assignment_id stays NULL, voices_unmatched++), 0.5-0.7 assigned but flagged (assignments_for_review++), >=0.7 auto-accept. Same pattern as walk_2f/2g.
- [x] Write `tests/pipeline/test_walk_2h.py` — spec-first: test voice matching, assignment update, NULL handling for unmatched voices
    **Note:** Created tests/pipeline/test_walk_2h.py with 23 tests across 3 test classes (TestExecute: 11 tests, TestBuildPrompt: 3 tests, TestParseResponse: 9 tests). Follows exact pattern from test_walk_2g.py: InMemorySQLiteAdapter, populate_initial_spine, monkeypatch for resolve_task_llm/create_llm_client, _insert_character/_insert_voice_config/_insert_voice_profile helpers. Tests cover: summary dict keys, high confidence match (assignment set), medium confidence (assigned + flagged for review), low confidence (NULL), null voice_config_id (NULL), no voice_profile (skipped, no LLM call), not locked (UPDATE succeeds, no locked column), empty voice_config (graceful), nonexistent book (error), invalid voice_config_id (rejected), multiple characters with different voices.
- [x] Verify: run `pytest tests/pipeline/test_walk_2h.py -v` — all tests pass
    **Note:** All 23 walk 2h tests pass. Full pipeline suite: 428 tests pass (including 23 new walk 2h tests). Ruff clean on both new files.

### Phase 3: Walk 2i Delivery
- [x] Create `app/pipeline/walks/walk_2i_delivery.py` with function `execute(book_id, storage, config)`
    **Note:** Created app/pipeline/walks/walk_2i_delivery.py with execute(book_id, storage, config). Follows walk_2f/2g/2h house style: module docstring, TYPE_CHECKING import, deferred app.utils import inside execute(), SAVEPOINT pattern (walk_2i_delivery). Key differences: (1) task name 'delivery', (2) iterates spans in presentation order via span_presentation VIEW (not characters), (3) _find_speaker_character() queries character_span WHERE relation_type='speaker', (4) collects character description + voice_profile from character_metadata + voice_assignment from voice_config via character join, (5) for narrative spans (no speaker), uses NARRATOR with narrative note in prompt, (6) stores via UPDATE span SET instruct = ? WHERE id = ?, (7) result dict keys: spans_processed, instructs_generated, instructs_for_review.
- [x] Use `resolve_task_llm('delivery')` to get LLM config (temperature=0.3, LOCAL — interpretive walk, MUST use LLM)
    **Note:** Uses resolve_task_llm('delivery', config_path=None) and create_llm_client(config_path=None) inside execute(). Temperature is resolved from config, not hardcoded.
- [x] For each span in the book (in presentation order), send span text + speaker character description + voice profile to LLM with prompt: "Generate delivery instructions for TTS. Return JSON with instruct field (e.g., 'slow and somber', 'fast and excited', 'whispered')."
    **Note:** _load_spans_in_presentation_order() queries span_presentation VIEW joined with paragraph_span/scene_paragraph/chapter_scene/book_chapter filtered by book_id, ordered by global_index. _find_speaker_character() finds speaker via character_span WHERE relation_type='speaker'. _get_character_description/voice_profile read from character_metadata. _get_character_voice_assignment joins character to voice_config. _build_delivery_prompt() includes span text, span_type, speaker context (or NARRATOR for narrative), and asks for instruct + confidence JSON.
- [x] Store instruct field in span.instruct column (per DD: span table has instruct TEXT column for TTS delivery instructions)
    **Note:** Stores via storage.execute_update("UPDATE span SET instruct = ? WHERE id = ?", (instruct, span_id)). instruct column already exists in schema — no migration needed. Wrapped in SAVEPOINT walk_2i_delivery.
- [x] Apply confidence filter: auto-accept ≥0.7, auto-reject <0.5, between → user review
    **Note:** Confidence filter: <0.5 auto-reject (instruct stays NULL, return early), 0.5-0.7 flagged for review (stored + instructs_for_review++), >=0.7 auto-accept (stored). Same pattern as walk_2f/2g/2h.
- [x] Write `tests/pipeline/test_walk_2i.py` — spec-first: test instruct generation, metadata storage, temperature=0.3, LLM usage (not rule-based)
    **Note:** Created tests/pipeline/test_walk_2i.py with 25 tests across 3 classes: TestExecute (10 tests covering summary dict, instruct stored, confidence filter high/low/medium, presentation order, speaker context, narrative without speaker, LLM usage verification, nonexistent book), TestBuildPrompt (6 tests covering prompt content for speaker/narrative/voice assignment/missing description), TestParseResponse (9 tests covering valid JSON, extra text, invalid JSON, missing/empty instruct, default confidence, non-dict/array responses). Uses InMemorySQLiteAdapter, monkeypatch for resolve_task_llm/create_llm_client, helper functions for inserting test data. All 25 tests pass.
- [x] Verify: run `pytest tests/pipeline/test_walk_2i.py -v` — all tests pass
    **Note:** All 25 walk 2i tests pass in 0.50s. Full pipeline suite: 453 tests pass (including 25 new walk 2i tests). Ruff clean on both new files. No lint errors. Walk 2i implementation complete.

## Completion Criteria
- Walk 2g generates voice profiles for characters
- Walk 2h assigns voices to characters (or leaves NULL for manual assignment)
- Walk 2i generates delivery instructions (instruct field) for each span
- All walks apply confidence filter correctly
- All tests pass
- Voice assignment is not locked (user can change via frontend)
