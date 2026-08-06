# Task: Voice Config Edit Form

## Problem Statement

`PUT /api/pipeline/voices/{id}` exists in the backend but the Voices tab has **no edit form**: voices.ts renders cards with only name, type badge, and a Preview button (createVoiceCard L240). The `VoiceConfigRow` interface (L31) types only {id, name, voice, type?, description?} — the other 7 columns (character_style, ref_audio, ref_text, seed, adapter_id, adapter_path, alias_of) exist in the DB (voice_config table, 12 cols) and in the backend PUT contract but are unreachable from the UI. The only voice mutation in voices.ts is the NARRATOR selector (handleNarratorVoiceChange L445 sends {voice} only). Meanwhile the pre-rewrite app had a full voice edit surface that was lost — the design restores a **minimal** version first (DD cap4, FP5, decision #6): style (character_style), reference audio (ref_audio) and ref_text, the 5-type switch (custom/clone/builtin_lora/lora/design), narrator override, preview; the alias picker (dropdown of existing voices — no free-form map editor, DD open item #5) is included where feasible. Precedence: NARRATOR DB row wins over the UNKNOWN→NARRATOR fallback.

This is a pure frontend plan (Plan H): the backend PUT /voices/{id} and POST /voices/{id}/preview already exist and are tested. It depends on Plan E only (tab navigation must work before the Voices tab is reachable/editable).

## Dependencies

- Plan E (tab-navigation foundation — Voices tab must be reachable)
- `artifacts/designs/parts/universal-upgrade/README.md` — Plan H row
- Upstream: `artifacts/designs/parts/epub-audiobook-pipeline-rewrite/CONTRACTS.md` — VoiceConfig (12-col) + PUT /voices/{id} contract

## Phases

### Phase 1: Widen VoiceConfigRow and load full voice configs
- [ ] TDD RED: write tests in frontend/tests/frontend/test_voices.test.ts asserting voice cards render style and type metadata when present, and that the edit form is populated from a fetched voice config (mock GET /voices returning a 12-column row)
- [ ] Implement: extend `VoiceConfigRow` (voices.ts L31) to all 12 columns (type, character_style, ref_audio, ref_text, seed, adapter_id, adapter_path, alias_of); ensure loadVoices (L305) keeps the existing catalog+narrator+character ledger wiring
- [ ] TDD GREEN: run test_voices tests; assert the extended type renders existing cards unchanged (backward-compatible rendering)
- [ ] Verify: `npx tsc --noEmit` exit 0; `npm test` green

### Phase 2: Edit form markup and card wiring
- [ ] TDD RED: write tests asserting clicking an edit action on a voice card opens a form (in voices tab) pre-filled with the voice's current values (style, ref audio, type, alias)
- [ ] Implement: add edit-form markup in index.html (Voices tab region, L493) — fields: character_style, ref_audio (file or path text), ref_text, type select (custom/clone/builtin_lora/lora/design), alias_of select (populated from GET /voices — existing voices only, no free-form map), save + cancel buttons; wire each card's edit button in voices.ts (delegated click like preview-voice [data-action])
- [ ] TDD GREEN: run test_voices tests; assert form opens pre-filled and type select has the 5 options
- [ ] Verify: `npx tsc --noEmit` exit 0; `npm test` green

### Phase 3: PUT save with exclude_unset + type-aware validation
- [ ] TDD RED: write tests asserting saving the form PUTs only changed fields (exclude_unset semantics preserved like handleNarratorVoiceChange L445), and that the type switch changes the voice_config type
- [ ] Implement: add save handler calling PUT /voices/{id} with the edited fields only (character_style, ref_audio, ref_text, type, alias_of); re-render the card from the response; error toast on failure (utils.ts toast pattern)
- [ ] TDD GREEN: run test_voices tests; assert PUT body contains only edited keys and card re-renders
- [ ] Verify: `npx tsc --noEmit` exit 0; `npm test` green

### Phase 4: Narrator override + preview reuse
- [ ] TDD RED: write tests asserting the narrator selector still works (PUT /voices/NARRATOR {voice} unchanged), and the preview button in the edit form reuses POST /voices/{id}/preview with the form's sample text
- [ ] Implement: keep handleNarratorVoiceChange behavior; add preview-in-form using existing previewVoice pattern (voices.ts L472, new Audio(…).play() with spinner); ensure NARRATOR row wins over UNKNOWN→NARRATOR fallback is unaffected (backend rule, no frontend change needed beyond not blocking narrator editing)
- [ ] TDD GREEN: run test_voices tests; assert narrator PUT unchanged and form preview fires previewVoice
- [ ] Verify: `npx tsc --noEmit` exit 0; `npm test` green

### Phase 5: Regression gate, guard suite and verification
- [ ] Run `npm test` (vitest 167 baseline + new tests) and `npx tsc --noEmit` (exit 0)
- [ ] Run `npm run build` and verify `git diff --exit-code app/static/dist/` is clean after committing dist
- [ ] Run `pytest tests/pipeline/test_legacy_removed.py -q` and verify 12/12 green (no legacy alias-map editor modules; no /api/scripts endpoints)
- [ ] Security review: voice config edits are data writes — confirm the frontend sends only documented fields, ref_audio is a path/URL string (no file upload surface added here), and no new secrets are rendered into the DOM; document findings
- [ ] Code review pass via exec-manager QA-Reviewer; fix MINOR findings
- [ ] Commit: `feat(frontend): voice config edit form with style, reference audio, type switch and alias picker`

## Completion Criteria

- Voices tab is reachable (Plan E) and each voice card opens an edit form pre-filled from the fetched voice config
- Form edits character_style, ref_audio, ref_text, type (5 options), alias_of (dropdown of existing voices) and saves via PUT /voices/{id} with exclude_unset semantics
- Narrator override and preview (POST /voices/{id}/preview) continue to work; NARRATOR precedence untouched
- vitest green, tsc exit 0, build + dist committed, guard 12/12, security review documented

## References

- `artifacts/designs/pending/DD-universal-upgrade.md` — cap4, P6-C4, FP5, decision #6, open item #5
- `artifacts/designs/parts/universal-upgrade/CONTRACTS.md` — PUT /voices/{id} contract (existing), VoiceConfigRow extension
- Prior: Plan E (tab navigation), Plan Q (voice parity, voice_config 8 missing columns added)
