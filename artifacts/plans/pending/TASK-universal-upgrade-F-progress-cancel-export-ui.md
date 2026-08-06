# Task: Progress, Cancel & Export UI

## Problem Statement

The pipeline frontend shows a static progress bar (`#full-progress-bar` set to 100% with "{total} spans loaded" — editor-pipeline.ts:233-239), polls render status without per-chunk counts, and has no working cancel affordance in the result flow. The `#btn-pipeline-download` button referenced by editor.ts:147 and editor-pipeline.ts:653/730 is absent from index.html — the download/export surface is unreachable. There is no export UI at all: no metadata form, no cover upload, no chapter markers, no MP3/Audacity options. The Script tab shows static per-walk status badges with no run history.

This plan (DD cap3, FP6+FP2) builds the real progress/cancel/export UI on top of the Plan B/C backend: per-chunk progress (completed/total/failures) for individual renders, job-level for batch; Cancel buttons wired to `cancel_walks`/`cancel_render` with 503+Retry-After retry-once; the result surface playing the whole book; the Export M4B form (title/author/narrator/year/description + cover upload + auto chapter markers); MP3 + Audacity ZIP_STORED buttons where supported (feature-detect libmp3lame, degrade to M4B-only with clear messaging); the walk-runs list in the Script tab; and the README doc-drift correction (progress-UI overstatement). It also restores a reachable download button.

## Dependencies

- Plan C (export/chunk, export/audio, export/m4b endpoints, range serving) and Plan E (singleton player, tab navigation, media stubs) — completed and archived
- `artifacts/designs/parts/epub-audiobook-pipeline-rewrite/CONTRACTS.md` § Universal Upgrade — endpoint registration

## Phases

### Phase 1: Real per-chunk progress + result surface
- [ ] TDD RED: write/extend frontend/tests/frontend/test_editor.test.ts asserting the render poll consumes render_status per-chunk counts (completed_chunks/total_chunks/failed_chunks) and renders a real progress bar (width = completed/total, failures shown as a count/error badge) for individual mode; job-level progress for batch mode
- [ ] Implement: replace the static bar logic in editor-pipeline.ts (loadSpans + pipelineRenderAll poll) with per-chunk progress rendering; poll GET /render_status/{job_id} on the existing 2000ms timer (_renderPollTimer), stop when terminal, surface failures per chunk
- [ ] Add the result surface: a "Play book" area using GET /export/audio/{job_id} via the singleton player from Plan E, plus a reachable Download button (restore #btn-pipeline-download in index.html wired to GET /download/{job_id})
- [ ] TDD GREEN: run test_editor tests with fake timers (vi.advanceTimersByTimeAsync(2000)) and mocked fetch (render_status with counts); assert progress bar width, failure badge, and download/play wiring
- [ ] Verify: `npx tsc --noEmit` exit 0; vitest green; `npm run build` + dist commit

### Phase 2: Cancel wiring with retry-once
- [ ] TDD RED: write tests asserting the Cancel button calls POST /cancel_render (render) and POST /cancel_walks (walks), and that a 503 + Retry-After response triggers exactly one automatic retry before surfacing an error
- [ ] Implement: wire cancel buttons in editor-pipeline.ts (render cancel) and script.ts (walk cancel) to the endpoints; add the retry-once wrapper around cancel calls (collision handling per DD workflow: 503+Retry-After → frontend retries once)
- [ ] TDD GREEN: run cancel tests with mocked fetch returning 503 then 200; assert retry-once and correct terminal state
- [ ] Verify: `npx tsc --noEmit` exit 0; vitest green

### Phase 3: Walk runs list in Script tab
- [ ] TDD RED: write/extend frontend/tests/frontend/test_script.test.ts asserting the Script tab fetches GET /walks/{book_id}/runs and renders a run list (walk_name, status badge, created/finished times) below the existing per-walk status badges; empty state when no runs
- [ ] Implement: add the runs list to script.ts (fetch on tab load + after each walk poll tick), reuse the existing status badge renderer
- [ ] TDD GREEN: run test_script tests with mocked fetch (runs payload) and fake timers for polling; assert list rendering and empty state
- [ ] Verify: `npx tsc --noEmit` exit 0; vitest green

### Phase 4: Export M4B form + cover upload + chapter markers
- [ ] TDD RED: write tests asserting the Export M4B form submits title/author/narrator/year/description + optional cover file to POST /export/m4b (multipart) and renders the resulting audio artifact; chapter marker generation is backend (Plan C) — frontend asserts the form payload shape and success state
- [ ] Implement: add the Export M4B form to the result surface in index.html + editor-pipeline.ts (title/author/narrator/year/description inputs + cover file input + submit), POST multipart to /export/m4b, handle success (play/download the artifact) and error states
- [ ] TDD GREEN: run export form tests with mocked fetch; assert multipart body shape (FormData with the 5 fields + cover) and success rendering
- [ ] Verify: `npx tsc --noEmit` exit 0; vitest green; `npm run build` + dist commit

### Phase 5: MP3 + Audacity ZIP_STORED (feature-detect, degrade)
- [ ] TDD RED: write tests asserting the MP3/Audacity buttons appear only when the backend reports support (feature-detect endpoint or capability flag in the export response); when unsupported, a clear message explains M4B-only
- [ ] Implement: capability feature-detect (libmp3lame availability surfaced via a backend capability check or the /export/m4b response); MP3 button requests MP3 output, Audacity button requests ZIP_STORED audacity export; unsupported → disabled buttons + messaging
- [ ] TDD GREEN: run the capability tests with mocked capability responses; assert button visibility and degrade messaging
- [ ] Verify: `npx tsc --noEmit` exit 0; vitest green; `npm run build` + dist commit

### Phase 6: README doc-drift correction
- [ ] Update README.md: replace the progress-UI overstatement with the real per-chunk progress description (individual mode) and job-level (batch mode); remove any false claim of a voice review surface or alias editing in the UI (actual surfacing lands in Plan H/D; alias picker is Plan H); note the new export surface
- [ ] Verify: grep README.md for the corrected sections; confirm no other stale claims about the removed Result tab remain

### Phase 7: Regression gate, guard suite and verification
- [ ] Run `npm test` (vitest) and record the pass count (167 baseline + new); `npx tsc --noEmit` exit 0
- [ ] Run `npm run build` and verify `git diff --exit-code app/static/dist/` is clean after committing dist
- [ ] Run `pytest tests/pipeline/test_legacy_removed.py -q` and verify 12/12 still green (frontend-only plan; no legacy /api/export_audacity or /api/merge_m4b references introduced)
- [ ] Security review: cover upload validation (size/type limits enforced client-side, backend validates too), ffmpeg invocation stays server-side list-args (no shell), no secrets in the export form; document findings
- [ ] Code review pass via exec-manager QA-Reviewer; fix MINOR findings
- [ ] Commit: `feat(frontend): real progress, cancel and export UI for pipeline renders`

## Completion Criteria

- Progress bar reflects real per-chunk counts (individual) / job-level (batch); failures surfaced
- Result surface plays the whole book and provides a reachable Download button
- Cancel buttons wired to cancel_render/cancel_walks with retry-once on 503+Retry-After
- Script tab shows the walk runs list; export M4B form (5 fields + cover) submits multipart; MP3/Audacity feature-detected with M4B-only degrade messaging
- README progress/voice-review claims corrected
- tsc exit 0, vitest green, build + dist committed, guard 12/12

## References

- `artifacts/designs/pending/DD-universal-upgrade.md` — P4 C3 phase, cap3, FP6+FP2, UX workflow (Progress, Export), decision #2, open item #8 (result-surface placement)
- `artifacts/designs/parts/universal-upgrade/CONTRACTS.md` — export/m4b, export/audio, walks/runs contracts
- `artifacts/designs/parts/universal-upgrade/README.md` — Plan F row
- Prior: `TASK-universal-upgrade-E-audio-surface-player-tabs.md` (completed)
