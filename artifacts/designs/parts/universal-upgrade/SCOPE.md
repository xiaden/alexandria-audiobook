# Universal Upgrade — Per-Part Scope Matrix

Source of truth: [`artifacts/designs/pending/DD-universal-upgrade.md`](../../pending/DD-universal-upgrade.md). This matrix is a navigation aid; the plan files are authoritative for step-level detail.

---

## Plan A — Schema & Transaction Foundation (P0 cross-cutting)

- **Purpose:** Make SQLite safe for concurrent walk/render writers: owner-thread `transaction()`, explicit `isolation_level=None`, `busy_timeout=5000`, and create the 6 new tables + 3 indices + `book.single_speaker` column.
- **Files:** `app/pipeline/adapter.py`, `app/pipeline/schema.py`, `tests/pipeline/test_adapter.py`, `tests/pipeline/test_schema.py`, `tests/pipeline/test_legacy_removed.py` (guard re-run).
- **Contracts:** `SQLiteAdapter.transaction()`, `ConcurrentTransactionError`, `init_db` busy_timeout, schema additions.
- **In scope:** transaction context manager + owner-thread guard; BEGIN IMMEDIATE; isolation_level=None; busy_timeout; 6 tables/3 indices; single_speaker ALTER (guarded by pragma check per populate.py precedent); tests incl. file-backed fixture for crash recovery.
- **Out of scope:** render/walk row writes (B), reconciliation (B), RENDER_ROOT (C), GC (C).
- **Gates:** ruff, pytest 768+new green, guard 12/12, coverage ≥80% on new code.

## Plan B — Render & Walk-Run Persistence (P1 A1-r jobs)

- **Purpose:** Jobs become rows, not memory. walk_run/render_job/render_chunk writes, startup reconciliation, persisted cancel, walk-side retry, jobs/chunks/runs endpoints, download rewrite.
- **Files:** `app/pipeline/api_export.py`, `app/pipeline/api_walks.py`, `app/pipeline/walks/runner.py`, `app/pipeline/adapter.py` (reconcile_stale_runs), `app/pipeline/api.py` (router), `tests/pipeline/` (test_runner, test_api, test_export NEW), smoke harness re-establishment.
- **Contracts:** `reconcile_stale_runs()`, `is_cancel_requested(run_id)`, walk_run lifecycle, render_job/render_chunk writes, 4 endpoints (render_status extended, cancel_render extended, cancel_walks persisted, GET export/jobs, GET export/jobs/chunks, GET walks/runs), download FileResponse-404.
- **In scope:** walk_run rows (pending/running/completed/failed/interrupted/cancelled), heartbeat in per-unit txn, is_cancel_requested dispatcher, render_job rows + per-chunk rows (individual mode), startup reconciliation flip, cancel persistence, walk-side retry on ConcurrentTransactionError, endpoint surface, download rewrite, smoke harness re-establishment.
- **Out of scope:** RENDER_ROOT/fsync (C), GC (C), audio/chunk range endpoints (C), review items (D).
- **Gates:** ruff, pytest green (768 baseline + new), guard 12/12, coverage, security review (SQL parameterization, path traversal in download).

## Plan C — Artifact-First Run Dirs, GC & Export Backend (P2 A2-r artifacts)

- **Purpose:** Run dirs under RENDER_ROOT with fsync discipline, manifest-as-derived-cache, tombstoning GC (≥7 days hourly), and the audio/chunk/export backend endpoints.
- **Files:** `app/pipeline/tts_integration.py` (RENDER_ROOT + fsync + chunk rows), `app/pipeline/api_export.py` (range/chunk/audio/m4b endpoints), `app/pipeline/gc.py` NEW (or in adapter — no new module per DD: put GC in existing module), `app/pipeline/export.py` NEW only if needed for FFMETADATA1 (DD allows new module? "NO new module" refers to adapter/API layers — FFMETADATA1 generator may live in api_export or a helper module), `app.py` (env wiring, startup GC scheduling — actually startup reconciliation is B; GC hourly sweep scheduling), `tests/pipeline/test_export.py` NEW (closes download/{job_id} zero-test gap).
- **Contracts:** render_chunk done-only-after-fsync; RENDER_ROOT env; manifest.json derived; GC eligibility union with snapshot refs; GET /export/chunk/{job_id}/{idx} (bounded range 206/416), GET /export/audio/{job_id}, POST /export/m4b (3-phase FFMETADATA1); MP3/Audacity derived where supported.
- **In scope:** RENDER_ROOT resolution (env `RENDER_ROOT` default under data/), fsync discipline (2 fsyncs: tmp→fsync→rename→fsync parent), manifest rebuild at startup, hourly GC with retention ≥7 days, range-request WAV serving, whole-book audio, 3-phase export, libmp3lame feature-detect degrade to M4B-only.
- **Out of scope:** job row writes (B), review (D), frontend (E/F).
- **Gates:** ruff, pytest green + test_export.py coverage of download gap, guard 12/12, security review (path traversal, Range DoS, ffmpeg arg injection).

## Plan D — Unified Review Queue & Supersede (P3 A3-r review union)

- **Purpose:** 2g/2h/2i low-confidence items surface honestly. walk_review_item writes in-walk-transaction, completion-time per-target supersede, union queue with prefix dispatch, value-restore undo backend.
- **Files:** `app/pipeline/review.py`, `app/pipeline/api_review.py`, `app/pipeline/walks/walk_2g_voice_audition.py`, `walk_2h_voice_assignment.py`, `walk_2i_delivery.py`, `tests/pipeline/test_review.py`, `test_walk_2g/2h/2i.py`.
- **Contracts:** walk_review_item writes (3 kinds), supersede in FINAL txn, ReviewManager union query, prefix dispatch, value-restore.
- **In scope:** per-walk review item writes in existing unit transactions; prior_value capture; completion-time supersede; union GET /review; accept/reject/override on walkitem: prefix; transactional value-restore; counters no longer discarded.
- **Out of scope:** frontend review UI (F), config (G).
- **Gates:** ruff, pytest green, guard 12/12, coverage ≥80% new code, security review (IDOR on cross-book item dispatch).

## Plan E — Audio Surface, Singleton Player & Tab Navigation (P4 C1)

- **Purpose:** Bring back playback: singleton player with injectable `createPreviewPlayer()` factory, per-span preview (individual mode), whole-book playback, sequence playback; plus the evidence-based tab-navigation foundation and vitest media stubs.
- **Files:** `frontend/src/main.ts` (tab nav + player init), `frontend/index.html` (nav wiring + audio elements), `frontend/src/player.ts` NEW (singleton + createPreviewPlayer), `frontend/src/tabs/editor-pipeline.ts` (▶ per-span, stopThenPlay), `frontend/src/state.ts`, `frontend/src/api.ts`, `frontend/vitest.setup.ts` (media stubs), `frontend/tests/frontend/test_editor.test.ts`.
- **Contracts:** createPreviewPlayer() factory; singleton audio; per-span ▶ resolving GET /export/chunk/{job_id}/{idx}; whole-book via GET /export/audio/{job_id}; stopThenPlay (await stop before play; AbortError/NotAllowedError benign; tap-to-continue autoplay); sequence playback queue; batch-mode tooltip "preview differs from final".
- **In scope:** tab-navigation foundation (click handler on [data-tab] links toggling .tab-content visibility + active class — replaces broken plain-<a> nav), media stubs in vitest.setup.ts, player module, per-span preview affordance, sequence playback, polling tests with fake timers.
- **Out of scope:** export UI (F), projects tab (I), voice edit form (H).
- **Gates:** `npx tsc --noEmit` exit 0, vitest green (167 + new), `npm run build` + dist commit, guard 12/12, security review (audio URL path traversal).

## Plan F — Progress, Cancel & Export UI (P4 C3)

- **Purpose:** Real per-chunk progress, working cancel buttons, polished export UI (M4B form with metadata+cover+chapters, MP3, Audacity ZIP_STORED).
- **Files:** `frontend/src/tabs/editor-pipeline.ts` (result surface, progress, cancel, export), `frontend/src/tabs/script.ts` (walk runs polling), `frontend/index.html` (export form, result surface, download button restoration), `frontend/src/api.ts`, `frontend/src/state.ts`, `README.md` (progress-UI doc drift fix), `frontend/tests/frontend/test_editor.test.ts`, `test_script.test.ts`.
- **Contracts:** Result tab/surface plays whole book via GET /export/audio/{job_id}; progress = completed/total/failures per chunk (individual) or job-level (batch); Cancel buttons call cancel_walks/cancel_render; collisions 503+Retry-After → frontend retries once; Export M4B form (title/author/narrator/year/description) + cover upload + auto chapter markers (single ffprobe, END clamped); MP3 + Audacity ZIP_STORED where supported; feature-detect libmp3lame → degrade to M4B-only with messaging; #btn-pipeline-download restored.
- **In scope:** progress polling render_status per-chunk counts, walk runs list, cancel wiring, export form + cover upload, chapter markers, MP3/Audacity buttons, README doc-drift correction (per-chunk progress real; remove false voice review surface claim if present).
- **Out of scope:** player internals (E), voice edit (H), projects (I).
- **Gates:** tsc exit 0, vitest green, build + dist commit, guard 12/12, security review (cover upload validation, ffmpeg invocation).

## Plan G — Overlay Config & Walk Overrides (P5 A4-r)

- **Purpose:** Stop config wiping generation/prompts keys; raw-JSON merge with byte-stable round-trip; resolve_task_config single helper; walk_override rows; setup.ts per-walk override fields.
- **Files:** `app/app.py` (POST /api/config raw-JSON merge + schema_version stamp), `app/utils.py` (resolve_task_config replacing resolve_task_llm dead param), `app/pipeline/walks/*` (9 walks call resolve_task_config), `frontend/src/tabs/setup.ts` (per-walk override UI), `frontend/index.html`, `frontend/tests/frontend/test_setup.test.ts` (LOCKED-IN wipe test L296 must be updated to byte-stable), `app/test_api.py`.
- **Contracts:** raw-JSON merge; AppConfig extra='ignore' validation-only; schema_version stamp; resolve_task_config(task, storage, book_id); walk_override rows read/write; byte-stable round-trip CI test with unknown keys in fixture.
- **In scope:** POST /api/config merge semantics, GET /api/config passthrough of unknown keys, resolve_task_config in 9 walks, walk_override CRUD, setup.ts override fields, byte-stable test, update test_setup.test.ts L296 behavior lock.
- **Out of scope:** voice edit (H), snapshots (I).
- **Gates:** ruff, pytest green, guard 12/12, tsc, vitest, build + dist, security review (config file path traversal, secret handling).

## Plan H — Voice Config Edit Form (P4/P6 C4)

- **Purpose:** Minimal voice-config edit form per card: style, ref-audio, type switch (custom/clone/builtin_lora/lora/design), narrator override, preview; alias picker = dropdown of existing voices (feasible).
- **Files:** `frontend/src/tabs/voices.ts` (edit form + VoiceConfigRow extended to 12 columns), `frontend/index.html` (form markup), `frontend/tests/frontend/test_voices.test.ts`, README.md (false alias editing claim removal if present).
- **Contracts:** PUT /voices/{id} with full voice config payload (exclude_unset preserves unknown); preview via existing POST /voices/{id}/preview; narrator override (NARRATOR DB row wins; UNKNOWN→NARRATOR); alias picker dropdown.
- **In scope:** VoiceConfigRow full typing, edit form modal/inline, type switch, ref-audio/ref-text, narrator override, preview reuse, alias picker.
- **Out of scope:** designer/other tabs, backend voice endpoints (existing).
- **Gates:** tsc, vitest, build + dist, guard 12/12.

## Plan I — Snapshot Projects (P6 C7a)

- **Purpose:** Saved projects: save (auto-named), list, load (merge, restore blocked during active runs), delete, rename. project_snapshot rows.
- **Files:** `app/pipeline/api_operations.py` (projects endpoints), `app/pipeline/api.py` (router registration), `app/pipeline/adapter.py` (snapshot row access), `frontend/src/tabs/projects.ts` NEW, `frontend/index.html` (projects tab + nav), `frontend/src/api.ts`, `frontend/src/state.ts`, `tests/pipeline/test_api.py`/new test_operations_projects.py, `frontend/tests/frontend/test_projects.test.ts` NEW.
- **Contracts:** POST /projects (auto-named e.g. `book-{id}-{timestamp}`), GET /projects, POST /projects/load (merge-vs-replace; characters never deleted), DELETE /projects/{name}, PATCH /projects/{name} (rename); restore blocked while active walk_run/render_job rows exist → 409 with Retry-After; audio missing → "re-render" notice.
- **In scope:** project_snapshot backend, snapshot_json manifest (spans/voice assignments/progress), restore merge semantics, blocked-during-active-runs check, projects tab UI, rename PATCH registered in upstream CONTRACTS.md.
- **Out of scope:** undo wiring (J), single-speaker (J).
- **Gates:** ruff, pytest green, guard 12/12, tsc, vitest, build + dist, security review (snapshot JSON path traversal, cross-book access).

## Plan J — Single-Speaker, Undo & Iteration UX (P6 C7b)

- **Purpose:** Undo (transactional value-restore + snapshot restore), single-speaker toggle writing book.single_speaker enforced at render boundary, pause-after verification, doc-drift archive.
- **Files:** `app/pipeline/tts_integration.py` (single_speaker render boundary), `app/pipeline/review.py` (value-restore integration), `frontend/src/tabs/editor-pipeline.ts` (undo buttons wiring), `frontend/src/tabs/voices.ts` or editor (single-speaker toggle), `frontend/index.html`, `artifacts/designs/completed/DD-frontend-rebuild-per-task-llm-config.md` (stale archive), README.md (voice review surface claim removal), `tests/pipeline/` + frontend tests.
- **Contracts:** book.single_speaker read at render boundary (all spans forced to NARRATOR config; export_annotated_script stays faithful); undo = transactional value-restore + snapshot restore (frontend wiring); pause-after via TTS pause_*_ms (already in setup.ts — verify + polish).
- **In scope:** single-speaker render-boundary enforcement + toggle UI, undo wiring (value-restore for review actions + snapshot restore), pause-after verification, stale DD archive + README doc-drift fixes.
- **Out of scope:** everything above.
- **Gates:** ruff, pytest green, guard 12/12, tsc, vitest, build + dist, security review.

---

## Cross-Plan Coordination Notes

- **Shared file `editor-pipeline.ts`:** touched by E (player/preview), F (progress/export), J (undo). Sequential rounds E→F→J prevent conflicts.
- **Shared file `walks/runner.py`:** B (walk_run rows) then G (resolve_task_config snapshot) then D (walk_review_item via 2g/2h/2i modules, not runner). B precedes D/G.
- **Shared file `app.py`:** C (env wiring, GC schedule) and G (config). C precedes G; no overlap in code regions.
- **Shared file `tts_integration.py`:** B (chunk rows), C (RENDER_ROOT/fsync), J (single_speaker boundary). Order B→C→J.
- **Shared file `adapter.py`:** A (transaction), B (reconcile), I (snapshot access). Order A→B→I.
- **Frontend `api.ts`:** generic client; E/F/G/H/I add endpoints as needed — each plan adds its own typed helpers; no conflict if sequential.
- **`test_setup.test.ts` L296** locks in the config-wipe behavior — Plan G must update it (contract change), otherwise vitest fails on the new byte-stable behavior.
