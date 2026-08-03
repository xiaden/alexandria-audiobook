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
- [ ] Create `app/pipeline/walks/walk_2g_voice_audition.py` with function `execute(book_id, storage, config)`
- [ ] Use `resolve_task_llm('voice_audition')` to get LLM config (temperature=0.3, LOCAL — interpretive walk)
- [ ] For each character in the book, collect character description + sample dialogue spans
- [ ] Send to LLM with prompt: "Based on this character's description and dialogue samples, suggest a voice profile. Return JSON with voice characteristics (age, gender, tone, accent, etc.)."
- [ ] Store voice profile suggestions in character_metadata table with key='voice_profile'
- [ ] Apply confidence filter: auto-accept ≥0.7, auto-reject <0.5, between → user review
- [ ] Write `tests/pipeline/test_walk_2g.py` — spec-first: test voice profile generation, metadata storage, temperature=0.3, confidence filtering
- [ ] Verify: run `pytest tests/pipeline/test_walk_2g.py -v` — all tests pass

### Phase 2: Walk 2h Voice Assignment
- [ ] Create `app/pipeline/walks/walk_2h_voice_assignment.py` with function `execute(book_id, storage, config)`
- [ ] Use `resolve_task_llm('voice_assignment')` to get LLM config (temperature=0.1, LOCAL)
- [ ] For each character, match voice_profile suggestions to available voices in voice_config table
- [ ] If voice_config has matching voices, assign voice_assignment_id in character table
- [ ] If no match, leave voice_assignment_id NULL (will be handled as UNKNOWN→NARRATOR at TTS boundary, or user can manually assign)
- [ ] Note: Voice assignment is NOT locked — user can change it via frontend
- [ ] Apply confidence filter: auto-accept ≥0.7, auto-reject <0.5, between → user review
- [ ] Write `tests/pipeline/test_walk_2h.py` — spec-first: test voice matching, assignment update, NULL handling for unmatched voices
- [ ] Verify: run `pytest tests/pipeline/test_walk_2h.py -v` — all tests pass

### Phase 3: Walk 2i Delivery
- [ ] Create `app/pipeline/walks/walk_2i_delivery.py` with function `execute(book_id, storage, config)`
- [ ] Use `resolve_task_llm('delivery')` to get LLM config (temperature=0.3, LOCAL — interpretive walk, MUST use LLM)
- [ ] For each span in the book (in presentation order), send span text + speaker character description + voice profile to LLM with prompt: "Generate delivery instructions for TTS. Return JSON with instruct field (e.g., 'slow and somber', 'fast and excited', 'whispered')."
- [ ] Store instruct field in span.instruct column (per DD: span table has instruct TEXT column for TTS delivery instructions)
- [ ] Apply confidence filter: auto-accept ≥0.7, auto-reject <0.5, between → user review
- [ ] Write `tests/pipeline/test_walk_2i.py` — spec-first: test instruct generation, metadata storage, temperature=0.3, LLM usage (not rule-based)
- [ ] Verify: run `pytest tests/pipeline/test_walk_2i.py -v` — all tests pass

## Completion Criteria
- Walk 2g generates voice profiles for characters
- Walk 2h assigns voices to characters (or leaves NULL for manual assignment)
- Walk 2i generates delivery instructions (instruct field) for each span
- All walks apply confidence filter correctly
- All tests pass
- Voice assignment is not locked (user can change via frontend)
