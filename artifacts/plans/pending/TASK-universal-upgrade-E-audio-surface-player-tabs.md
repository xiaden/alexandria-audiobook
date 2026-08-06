# Task: Audio Surface, Singleton Player & Tab Navigation

## Problem Statement

Plan Q removed the legacy Result tab and audio.ts; today there is no way to play rendered audio in the pipeline UI. `editor-pipeline.ts` renders spans into a static table with a fake progress bar, the `#btn-pipeline-download` button referenced in code does not exist in index.html, and no `<audio>` element exists for playback. The Universal Upgrade design (DD cap1, FP1/FP2/FP4/FP7) restores the audio surface pipeline-natively: a singleton player with an injectable `createPreviewPlayer()` factory, per-span preview in individual render mode (seeking `GET /export/chunk/{job_id}/{idx}`), whole-book playback (`GET /export/audio/{job_id}`), and sequence playback (player queue of span URLs) — with stopThenPlay semantics, benign AbortError/NotAllowedError handling, and tap-to-continue autoplay.

**Evidence-based addition:** the frontend has no working tab navigation at all. Nav links are plain `<a data-tab="...">` with no click handler and no bootstrap tab wiring; every pane except `#setup-tab` is `display:none` in the DOM. All new UI in this and later plans lives in hidden tabs, so a tab-navigation foundation is required first (small, in main.ts + index.html). The DD's vitest media-stub claim is also false (only `MockAudio` in test_voices.test.ts exists), so media stubs are added to `vitest.setup.ts` here. Batch-mode renders show a tooltip "preview differs from final — whole-book playback only" because batch mode has no per-chunk rows.

## Dependencies

- Plan B (render_job/render_chunk rows, render_status per-chunk counts, chunk/audio endpoints in Plan C) and Plan C (GET /export/chunk/{job_id}/{idx}, GET /export/audio/{job_id}) — completed and archived
- `artifacts/designs/parts/epub-audiobook-pipeline-rewrite/CONTRACTS.md` § Universal Upgrade — endpoint registration

## Phases

### Phase 1: Tab-navigation foundation (evidence-based)
- [ ] TDD RED: write frontend/tests/frontend/test_main.test.ts (or extend an existing test) asserting that clicking a nav link with `data-tab="voices"` shows the voices pane and hides others, and that the active nav class moves
- [ ] Implement: add a global tab-switch handler in main.ts (DOMContentLoaded) that listens on nav links `[data-tab]`, toggles `.tab-content` visibility by id (`{tab}-tab`), sets the `.nav-link.active` class, and calls the per-tab load hook (e.g. editor's existing loadSpans/loadReviewItems listener contract preserved)
- [ ] TDD GREEN: run `npx tsc --noEmit` (exit 0) and `npm test` (vitest) with the new tab test; assert panes toggle correctly with jsdom
- [ ] Verify: `npm run build` succeeds and regenerates app/static/dist; run `git diff --exit-code app/static/dist/` and commit the dist update

### Phase 2: Media stubs in vitest.setup.ts (evidence-based)
- [ ] TDD RED: write a test that instantiates the player via the factory (Phase 3) under jsdom and asserts play()/pause()/currentTime/seek calls work without the real HTMLMediaElement (jsdom does not implement play())
- [ ] Implement: add media stubs to frontend/vitest.setup.ts (HTMLMediaElement.prototype.play/pause/currentTime mocks, Audio constructor stub if needed) per the DD test strategy; keep the existing localStorage fix
- [ ] TDD GREEN: run vitest; assert player tests pass with the stubs in place and the existing 167 tests stay green

### Phase 3: Singleton player + injectable createPreviewPlayer factory
- [ ] TDD RED: write frontend/tests/frontend/test_player.test.ts asserting createPreviewPlayer() returns a player with stop(), play(url), seek(seconds), pause(), and that the factory is injectable (can be swapped in tests); a singleton accessor returns the same instance across calls
- [ ] Implement: create frontend/src/player.ts with the createPreviewPlayer factory (returns a singleton-backed player wrapping a single shared `<audio>` element), stopThenPlay semantics (await stop before play; AbortError/NotAllowedError treated as benign; tap-to-continue autoplay resume), and a module-level singleton accessor
- [ ] TDD GREEN: run player tests with fake timers and media stubs; assert stop-then-play ordering, benign error swallowing, and singleton identity
- [ ] Verify: `npx tsc --noEmit` exit 0; vitest green; `npm run build` + dist commit

### Phase 4: Per-span preview in editor-pipeline.ts
- [ ] TDD RED: write/extend frontend/tests/frontend/test_editor.test.ts asserting each span row shows a ▶ preview affordance that resolves `GET /export/chunk/{job_id}/{idx}` and plays via the player; in batch mode (job.mode=batch) the ▶ is disabled or shows the tooltip "preview differs from final — whole-book playback only"
- [ ] Implement: extend renderSpanRow in editor-pipeline.ts with a preview button wired to the singleton player; resolve chunk idx per span (span index ↔ chunk idx mapping per DD open item #4 — use the render_chunk row list from GET /export/jobs/{job_id}/chunks when available); disable per-span preview for batch jobs with the tooltip
- [ ] TDD GREEN: run test_editor tests with fully mocked fetch (render_status, chunk URLs) and fake timers; assert preview click plays the right URL and batch-mode disables it
- [ ] Verify: `npx tsc --noEmit` exit 0; vitest green; `npm run build` + dist commit

### Phase 5: Whole-book playback
- [ ] TDD RED: write a test asserting the result surface (to be built in Plan F; here the player wiring) plays the whole book via `GET /export/audio/{job_id}` with correct URL and singleton seek support
- [ ] Implement: add a "Play book" affordance in editor-pipeline.ts (near the render job badge) that loads `GET /export/audio/{job_id}` into the singleton player; store the URL in state.pipelineRenderJobId flow
- [ ] TDD GREEN: run the playback tests; assert the audio element src is the /export/audio URL and play() is called
- [ ] Verify: `npx tsc --noEmit` exit 0; vitest green

### Phase 6: Sequence playback (player queue)
- [ ] TDD RED: write tests asserting sequence playback queues a list of span chunk URLs and plays them in order (presentation order of loaded spans), advancing on 'ended' events, and stopping cleanly on stop()
- [ ] Implement: add sequence playback to the player module (queue + auto-advance on ended + stop clears the queue); wire an entry point in editor-pipeline.ts for the selected spans
- [ ] TDD GREEN: run sequence tests with fake timers; assert ordered playback and clean stop
- [ ] Verify: `npx tsc --noEmit` exit 0; vitest green; `npm run build` + dist commit

### Phase 7: Regression gate, guard suite and verification
- [ ] Run `npm test` (vitest) and record the pass count (167 baseline + new); run `npx tsc --noEmit` (exit 0)
- [ ] Run `npm run build` and verify `git diff --exit-code app/static/dist/` is clean after committing dist
- [ ] Run `pytest tests/pipeline/test_legacy_removed.py -q` and verify 12/12 still green (frontend-only plan; guard must not regress via any HTML/JS references to legacy audio.ts / editor-legacy.ts)
- [ ] Security review: chunk/audio URLs are server-constructed paths (no user-supplied paths in the frontend); no new auth surface; audio element srcs come from the API only; document findings
- [ ] Code review pass via exec-manager QA-Reviewer; fix MINOR findings
- [ ] Commit: `feat(frontend): pipeline audio surface with singleton player and tab navigation`

## Completion Criteria

- Tab navigation works: clicking any nav link shows its pane; active class moves; per-tab load hooks preserved
- Media stubs present in vitest.setup.ts; player tests green under jsdom
- createPreviewPlayer() factory + singleton player implemented with stopThenPlay, benign error handling, tap-to-continue
- Per-span ▶ preview resolves /export/chunk/{job_id}/{idx} (individual mode); batch mode disabled with the "preview differs from final" tooltip
- Whole-book playback via /export/audio/{job_id}; sequence playback queue advances on ended
- tsc exit 0, vitest green (167 + new), build + dist committed, guard 12/12

## References

- `artifacts/designs/pending/DD-universal-upgrade.md` — P4 C1 phase, cap1, FP1/FP2/FP4/FP7, UX workflow, decision #2 (batch tooltip), open item #4 (span↔chunk offset)
- `artifacts/designs/parts/universal-upgrade/CONTRACTS.md` — createPreviewPlayer, per-span preview, whole-book playback
- `artifacts/designs/parts/universal-upgrade/README.md` — Plan E row + evidence-based adjustments #1/#2
- Prior: `TASK-universal-upgrade-C-artifacts-gc-export-backend.md` (completed)
