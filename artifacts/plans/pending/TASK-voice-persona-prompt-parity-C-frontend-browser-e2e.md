# Task: Voice-Persona-Prompt Parity C — frontend clone/persona/prompt UI and deterministic browser-style journeys

## Problem Statement

This is part C of the implementation plan for `artifacts/designs/pending/DD-voice-persona-prompt-parity-browser-external-validation.md`. Parts A and B (backend) are prerequisites. This part delivers the browser-visible parity: (1) clone reference upload/list/inline-preview/download/delete UI integrated with the existing Voices tab, (2) a manual persona editor surfaced from the Characters & Scenes workbench (structured fields/evidence/aliases/scene-scope/review/protection/voice-consequences), and (3) an effective-prompt/settings viewer+editor (structured and guarded raw modes) wired to the prompt-config API, plus (4) deterministic browser-style (Playwright-*analogous*) Vitest/jsdom journeys using fake TTS engines and fixed media fixtures. No new real-engine dependency and no legacy manifest/route. This part also wires committed-dist build output so the deterministic frontend build gate stays green.

## Dependencies

- **Prerequisite:** `artifacts/plans/pending/TASK-voice-persona-prompt-parity-A-...` and `-B-...` (backend contracts + routes).
- Design: `artifacts/designs/pending/DD-voice-persona-prompt-parity-browser-external-validation.md`.
- Frontend: `frontend/src/api.ts` (`get/post/put/putWithRetryOnce/delWithRetryOnce/postWithRetryOnce`), `frontend/src/state.ts` (`WorkbenchState`, `WorkbenchConfig`, selectors, `Voice`/`VoiceConfig`), `frontend/src/tabs/voices.ts` (895 lines), `frontend/src/tabs/workbench.ts` (815 lines; Characters & Scenes), `frontend/src/main.ts`, `frontend/index.html`, `frontend/vitest.config.ts` (jsdom, `http://localhost:3000/`), `frontend/vitest.setup.ts` (HTMLMediaElement play/pause recording stubs already installed), `frontend/tests/frontend/test_workbench.test.ts` (773 lines convention).
- Vitest 4.1.10, jsdom 30, vite 8.2.0; `npm run build` emits into `app/static/dist/` (committed-dist equality gate `git diff --exit-code app/static/dist/`).
- Invariants: no real engines/fixtures in CI; deterministic fake engines + fixed media fixtures; `app/tts.py` byte-identical; combined-walks workbench UI (workbench.ts) unchanged where protected, extended only by the persona editor hook; no new tab family in index.html beyond wiring; accessible (keyboard, non-color, confirmation, undo, no secret/path/raw prompt exposure).

## Phases

### Phase 1: Clone reference UI (S1)
- [ ] Extend `frontend/src/tabs/voices.ts` with a clone-reference panel (collapsible when voice type is `clone` or always for clone-capable voices): upload via `<input type="file">` (multipart) + optional `ref_text`, listing owned references (metadata: filename, media_type, byte_size, duration_ms, created), inline preview via the `/preview` audio endpoint (HTMLAudioElement), download via attachment endpoint (`<a download>` or blob), delete with explicit confirmation (owner-safe; cross-owner error surfaced). Resolve display-name→resolved-ID before any assignment. Surface size/duration/material limits and accessible error toasts.
    **Notes:** Reuse `API.post/get/del`; upload is multipart (FormData), not JSON. Inline preview must use the existing player seam (`frontend/src/player.ts`) or a scoped audio element; assert `load`/`loadedmetadata` ordering before `currentTime` for seek (vitest media stubs already installed in `vitest.setup.ts`).
- [ ] Add `frontend/src/api.ts` typed helpers for clone references (`uploadCloneReference(voiceId, file, refText)`, `listCloneReferences(voiceId)`, `previewCloneReferenceUrl(voiceId, referenceId)`, `downloadCloneReference(voiceId, referenceId)`, `deleteCloneReference(voiceId, referenceId)`), persona, and prompt-config endpoints to keep `state.ts`/tabs thin. Update `state.ts` types (`CloneReference`, `Persona`, `PersonaRevision`, `EffectiveWalkConfig`, `PromptConfigRevision`).
    **Notes:** Follow the existing `putWithRetryOnce`/`delWithRetryOnce` 503-retry convention for persona/prompt writes; `get` for reads.
- [ ] Verify: `cd frontend && npx tsc --noEmit -p tsconfig.json` exit 0; new `frontend/tests/frontend/test_clone_references.test.ts` covers upload (FixtureAudio file), inline preview load/`loadedmetadata`/seek ordering, download attachment, delete confirm + owner-error, limits display, keyboard reachability.

### Phase 2: Persona editor UI (S2)
- [ ] Wire a persona editor into the workbench tab (`frontend/src/tabs/workbench.ts` / a new focused module imported by it): open a character from the Characters & Scenes ledger → edit structured fields (identity/appearance/manner/speech/role), evidence (anchor/quote/source/confidence using reachable stable anchors), aliases (normalized + provenance), scene scope (book|scenes + reachable scene_ids), review state (draft|needs_review|accepted|rejected), protected flag, and preview derived voice consequences. Save via PUT with `base_revision`; show validation/revision history; handle stale 409 (refresh + merge), 422, 503 retry.
    **Notes:** The editor is a separately addressable capability integrated with the workbench ledger — it does NOT become a second discovery pipeline and does not modify `character.voice_assignment_id` (voice consequence is show-only unless the user explicitly assigns). Protected-edit is rejected with a clear message; rerun requires explicit confirmation naming affected scenes.
- [ ] Verify: `cd frontend && npx tsc --noEmit -p tsconfig.json` exit 0; extend workbench or new `frontend/tests/frontend/test_persona.test.ts` covering open-from-ledger, field/evidence/alias/scope/protection editing, validation, save with base_revision, stale 409, protected rejection, revision list, side-effect-free validate call, keyboard + non-color state encoding.

### Phase 3: Effective prompt/settings editor UI (S3)
- [ ] Implement a prompt/settings viewer+editor (new module `frontend/src/tabs/prompt-config.ts` or within an existing tab pane): for each of the nine fixed walks, show effective values + provenance/source badges (reuse `sourceLabel`/`resolve_effective_config` shape from `state.ts`); toggle structured vs guarded-raw JSON mode (raw is validated JSON only, unknown keys rejected); validate before save (side-effect-free POST); save a revision with optimistic conflict protection (base_revision); list revisions; explicit scoped-rerun confirmation naming affected walk scope and scene scope, with `confirm:true`. Inline `temperature=0.0` handling.
    **Notes:** Do NOT import any legacy prompt-file module. Provenance badges mirror the workbench `_source_for` tiers (row/config/task/global/fallback) surfaced via the GET effective config. Writes use `postWithRetryOnce`-style 503 retry. Rerun never auto-fires from a save.
- [ ] Verify: `cd frontend && npx tsc --noEmit -p tsconfig.json` exit 0; new `frontend/tests/frontend/test_prompt_config.test.ts` covering effective/source display for the nine tasks, structured/raw toggle, guarded-raw rejection of unknown keys, validate call, revision save + conflict, rerun confirmation + already-ran error, keyboard/reachability.

### Phase 4: Deterministic browser-style journeys + build gate (S4)
- [ ] Add a browser-journey test file (`frontend/tests/frontend/test_browser_journeys.test.ts`) using deterministic fake engines and fixed media fixtures (a fixture `FixtureAudio`/`MediaFixture` module under `frontend/tests/` providing stable byte content, duration metadata, and fake `generate` behavior implementing the existing TTS generate contract). Cover the user journeys: upload+preview+assign+delete clone voice; persona edit→validate→protect→explicit scoped rerun; prompt compare→structured edit→raw validate→save→confirm rerun; deterministic playback/download/form/marker/error behavior incl. seek ordering and 4xx/5xx error states. No unavailable-engine false-green: if a capability is unavailable it renders unavailable, never passed.
    **Notes:** "Playwright-style" here means journey-level Vitest/jsdom tests (no real Playwright dependency exists in the repo; the DD's "Playwright-style" = browser-visible journeys with deterministic fixtures). Media stubs already present in `vitest.setup.ts`.
- [ ] Build and committed-dist: `cd frontend && npm run build` then `git diff --exit-code app/static/dist/` (new assets under `app/static/dist/`). Confirm `npm run build` is deterministic and the committed-dist gate passes.
    **Notes:** Commit the built `index-*.js` + `index.html` under `app/static/dist/` exactly as the combined-walks delivery did.

### Phase 5: Gates and scoped commit (S5)
- [ ] Run gates: `cd frontend && npx tsc --noEmit -p tsconfig.json` (exit 0), `cd frontend && npx vitest run` (all frontend tests incl. 378 existing base + new), `cd frontend && npm run build && git diff --exit-code app/static/dist/`, `cd /repo-root && pytest -q` (backend still green) + legacy guard 12/12.
    **Notes:** Record exact vitest counts and dist gate output. No real-engine dependency; no backend surface changes in part C.
- [ ] Commit part C as scoped commits (e.g. `feat(frontend): clone reference UI`, `feat(frontend): persona editor`, `feat(frontend): prompt config editor`, `test(frontend): deterministic browser journeys + committed dist`). Note commit SHAs.

## Completion Criteria

- Clone upload/list/inline-preview/download/delete UI enforces ownership/bounded-validated-media/limits, resolves display→resolved-ID before assignment, and surfaces errors accessibly.
- Persona editor round-trips exact structured fields/evidence/aliases/scope/protection/consequences with revisions, validation, stale-409, protected-rejection, and explicit confirmed scoped rerun (no auto-cascade).
- Every fixed walk shows effective prompt/settings + provenance; structured and guarded-raw modes; exact allow-list validation; versioned save with conflict protection; explicit confirmed rerun (already-ran handled).
- Deterministic browser journeys cover all five DD user journeys with fake engines/media; seek ordering, downloads, forms, errors; no unavailable-engine false-green.
- `tsc --noEmit` exit 0; `vitest run` green (base 378 + new); `npm run build && git diff --exit-code app/static/dist/` clean; backend suite + legacy guard green.
- No `app/tts.py` change, no legacy module import, no real-engine dependency, no walk-tab reorganization.

## References

- `artifacts/designs/pending/DD-voice-persona-prompt-parity-browser-external-validation.md`
- `artifacts/plans/pending/TASK-voice-persona-prompt-parity-A-...` / `-B-...`
- `artifacts/plans/completed/TASK-combined-walks-workbench-workbench-foundation.md` (frontend + dist conventions)
- `frontend/vitest.setup.ts`, `frontend/tests/frontend/test_workbench.test.ts`
