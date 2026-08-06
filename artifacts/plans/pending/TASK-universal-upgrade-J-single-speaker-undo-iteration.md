# Task: Single-Speaker, Undo & Iteration UX

## Problem Statement

Four iteration utilities from the pre-rewrite app are still missing and land here (DD cap7, P6-C7b, FP3+FP4+A4-r):

1. **Single-speaker mode** — the `book.single_speaker` column (created in Plan A) is never written or enforced. Enforced at the **render boundary only** (tts_integration.py): when single_speaker=1, render_audiobook forces all spans to the NARRATOR voice config; the annotated-script export stays faithful (DD decision #11, open item #7). The UI gains a toggle that writes book.single_speaker.
2. **Undo** — no undo exists anywhere in editor-pipeline.ts. The design's undo = **transactional value-restore** (backend, built in Plan D: walkitem reject/override restores prior_value within a transaction) + **snapshot restore** (Plan I) + a frontend Undo button wired to those primitives for span-text edits (the only reversible mutation surface in the editor today is span text, saved via PUT /span/{id}/text on focusout).
3. **Pause-after** — the pre-rewrite per-span pause_after field is NOT restored (cannot-restore #5); pause is global via TTSConfig `pause_between_speakers_ms`/`pause_same_speaker_ms` (setup.ts already submits these with 500/250 ms defaults — verified in Plan G). This plan **verifies** the global pause path end-to-end (config → tts_integration → generated audio) and polishes the Setup tab UI if needed; no per-span field.
4. **Doc drift cleanup** — archive the stale `designs/completed/DD-frontend-rebuild-per-task-llm-config.md` (276 lines) and fix remaining README claims that don't match the delivered UI.

This plan is the terminal plan of the feature: after it, all 8 DD capabilities are delivered and the whole feature can be smoke-tested end to end.

## Dependencies

- Plan D (transactional value-restore primitive) and Plan I (snapshot restore)
- `artifacts/designs/parts/universal-upgrade/CONTRACTS.md` — book.single_speaker, value-restore, snapshot restore

## Phases

### Phase 1: single_speaker render-boundary enforcement (backend)
- [ ] TDD RED: write tests in tests/pipeline/test_tts_integration.py asserting render_audiobook with a book where single_speaker=1 forces every chunk's voice config to NARRATOR (the NARRATOR_VOICE config), while export_annotated_script output remains faithful (unchanged); with single_speaker=0 behavior is unchanged
- [ ] Implement: in tts_integration.py at the _build_voice_config/_build_chunks boundary (the only allowed seam — app/tts.py untouchable), read book.single_speaker via storage and override the per-chunk voice config to NARRATOR when set; keep _build_voice_config and _build_chunks signatures compatible
- [ ] TDD GREEN: run test_tts_integration tests; assert NARRATOR enforcement and export fidelity
- [ ] Verify: `ruff check app/pipeline/tts_integration.py` clean; `pytest tests/pipeline -q` green

### Phase 2: Single-speaker toggle UI
- [ ] TDD RED: write frontend tests asserting the toggle (in editor-pipeline.ts or a settings row) writes book.single_speaker via a backend write path (PUT /operation or a small endpoint in api_operations.py) and reflects the saved value on load
- [ ] Implement: add the toggle + label ('Single-speaker render' with tooltip 'forces NARRATOR at render; script stays faithful'); wire load/save; add the minimal backend write (PUT /operation for book attributes or dedicated PATCH) to api_operations.py if not already covered
- [ ] TDD GREEN: run the toggle tests; assert round-trip persistence and render-boundary effect documented
- [ ] Verify: `npx tsc --noEmit` exit 0; `npm test` green; `npm run build` + dist committed

### Phase 3: Undo wiring (span-text edits)
- [ ] TDD RED: write frontend tests asserting an Undo button in the editor reverts the last span-text edit to its prior value (calls the Plan D value-restore / Plan I snapshot-restore primitives or PUT /span/{id}/text with the prior value) and shows a toast; disabled when there is nothing to undo
- [ ] Implement: maintain an in-memory undo stack of span-text edits (prior value per span id); Undo button reverts via the existing pipelineUpdateSpanText path (PUT /span/{id}/text); clear stack on snapshot load/render
- [ ] TDD GREEN: run the undo tests; assert revert, disabled state, and stack clearing on load
- [ ] Verify: `npx tsc --noEmit` exit 0; `npm test` green

### Phase 4: Pause-after verification + Setup polish
- [ ] TDD RED: write backend tests asserting pause_between_speakers_ms/pause_same_speaker_ms flow from config.json (via resolve_task_config, Plan G) into the TTSConfig used by render_audiobook, and that generated audio includes the pause (or that the TTSConfig carries the fields unchanged through _build_voice_config); assert defaults 500/250 ms
- [ ] Implement: only if the tests expose a break — fix the passthrough at the tts_integration boundary (never in app/tts.py); polish the Setup tab pause fields (labels/tooltips) to make them discoverable
- [ ] TDD GREEN: run the pause tests; assert global pause config reaches TTS with defaults intact
- [ ] Verify: `pytest tests/pipeline -q` green; `npx tsc --noEmit` exit 0; `npm test` green

### Phase 5: Doc drift cleanup + feature-wide smoke verification
- [ ] Archive `artifacts/designs/completed/DD-frontend-rebuild-per-task-llm-config.md` (stale, superseded by the universal-upgrade config work) — move to an archive location or annotate superseded per repo convention
- [ ] Fix any remaining README.md claims that mismatch delivered UI (verify voice review surface, alias editing, per-chunk progress wording post-Plans D/F/H)
- [ ] Feature-wide verification: run the full backend suite (`pytest tests/pipeline -q --cov=app/pipeline --cov-report=term-missing`, record count/coverage), the frontend suite (`npm test`, `npx tsc --noEmit`), `npm run build` + `git diff --exit-code app/static/dist/` clean, and the guard `pytest tests/pipeline/test_legacy_removed.py -q` 12/12
- [ ] Security review: single-speaker toggle writes a book attribute (validate book_id ownership, parameterized SQL), undo uses server-validated primitives only (no client-supplied targets), pause config passes through pydantic validation; document findings
- [ ] Code review pass via exec-manager QA-Reviewer; fix MINOR findings
- [ ] Commit: `feat(pipeline): single-speaker render, undo, global pause and doc cleanup`

## Completion Criteria

- book.single_speaker enforced at render boundary (NARRATOR override) with faithful annotated-script export; toggle UI round-trips the value
- Undo button reverts span-text edits via transactional/snapshot primitives with disabled state
- Global pause-after verified end-to-end (config → TTS), defaults 500/250 ms; no per-span field resurrected
- Stale frontend-rebuild design doc archived; README claims match delivered UI
- All suites green, guard 12/12, build + dist committed, security review documented — feature complete (all 8 DD capabilities delivered)

## References

- `artifacts/designs/pending/DD-universal-upgrade.md` — cap7, P6-C7b, FP3+FP4+A4-r, decisions #5/#11, open items #5/#7, doc-drift section
- `artifacts/designs/parts/universal-upgrade/CONTRACTS.md` — single_speaker, value-restore, snapshot restore
- Prior: Plans D, I, G (primitives), Plan Q (audio surface removal baseline)
