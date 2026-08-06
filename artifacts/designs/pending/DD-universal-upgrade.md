# Design: Universal Upgrade — Restoring Pre-Rewrite Utility as Pipeline-Native Capabilities

- **Status:** Pending
- **Author:** rnd-dd-author
- **Date:** 2026-08-06
- **Supersedes:** the Refiner's placeholder skeleton of the same name. Full adversarial record: [ADVERSARIAL-universal-upgrade.md](../process/ADVERSARIAL-universal-upgrade.md)
- **Consistency gates:** all schema/API changes registered in [CONTRACTS.md](../parts/epub-audiobook-pipeline-rewrite/CONTRACTS.md); active pipeline contract is [DD-epub-audiobook-pipeline-rewrite-v3.md](../completed/DD-epub-audiobook-pipeline-rewrite-v3.md)

## Problem Statement

The Plan-Q rewrite removed the legacy audio/result surface along with 29 legacy endpoints. Capabilities users relied on pre-rewrite — in-app playback, per-span preview, real progress, cancellation, polished export, voice-config editing, review surfacing, prompt/generation overrides, saved scripts, single-speaker, undo, sequence playback — were either lost or left as in-memory approximations. This design restores all eight required capabilities as pipeline-native features on the walk-based pipeline, without reintroducing any legacy endpoint, module, toggle, or shim. All decisions below survive the 8-turn adversarial refinement (evidence trail linked).

## Requirements

1. **Result/audio surface:** in-app audiobook playback + per-span/per-chunk preview while editing.
2. **Real render progress** (completed/total, failures) + useful cancellation, including walk cancellation where practical.
3. **Polished audiobook export:** M4B metadata (title, author, narrator, year, description), cover, chapter markers, MP3/Audacity export where supported.
4. **Voice configuration editing:** style, alias mapping, reference audio/text, custom/clone/builtin_lora/lora/design types, narrator override, preview.
5. **Surface Walk 2g/2h/2i low-confidence/review items** in the review UI.
6. **Optional prompt and generation-parameter overrides** without reintroducing duplicated legacy architecture.
7. **Saved script/project workflow, single-speaker mode if still useful, contextual review, undo, pause-after, sequence playback** where they improve iteration speed.
8. **Preserve pipeline-only architecture:** no old endpoints/modules/project manager, no legacy toggles, no compatibility shims. Capabilities go into pipeline APIs/services + current frontend modules.

## Capability Mapping

| # | Capability | Current state (HEAD 2026-08-06) | Target design | Carrier / Phase |
|---|---|---|---|---|
| 1 | Audio surface: playback + per-span preview | None. Result tab deleted; audio lives in ephemeral `mkdtemp` | Singleton player; per-span preview (individual mode) / whole-book playback (batch) via bounded-range endpoints | FP1+FP2+FP4+FP7 / P1, P2, P4 |
| 2 | Real progress + cancellation | In-memory `_render_jobs` (api_export), `WalkRunner._status/_cancelled`; cancel checked between walks / once pre-dispatch | SQLite job rows = truth; startup reconciliation; persisted cancel requests; per-chunk cancel in individual mode | FP1+FP2 / P1, P2 |
| 3 | Polished export | `POST /merge` = ffmpeg concat only (no metadata/cover/chapters) | FFMETADATA1 3-phase export: concat → metadata → mux; MP3; Audacity ZIP_STORED bundle | FP6+FP2 / P2, P4 |
| 4 | Voice-config editing | PUT `/api/pipeline/voices/{id}` exists but **no UI** (voices.ts: card = name+type badge+preview only) | Minimal edit form on voices.ts: style, ref-audio, 5-type switch, narrator override, preview; alias picker follow-up | FP5 backend + C4 UI / P5, P6 |
| 5 | Review surfacing 2g/2h/2i | Junction-only queue; for_review counters discarded in walk `execute()` | Honest union: junction live query + `walk_review_item` rows written in-walk-transaction | FP3 / P3 |
| 6 | Prompt/gen overrides | `POST /api/config` **wipes** generation/prompts keys (AppConfig = llm+tts only, `model_dump`); `resolve_task_llm(task, config_path=None)` dead param | Raw-JSON merge config + `walk_override` rows + `resolve_task_config()` single helper | FP5 / P5 |
| 7 | Saved scripts, single-speaker, undo, sequence, pause | None (snapshot-variant regressed) | Snapshot projects (auto-named, rename PATCH); `book.single_speaker` at render boundary; undo = transactional value-restore + snapshot; sequence playback; pause via TTS pause_*_ms | FP3+FP4+FP1+A4-r / P4, P6 |
| 8 | Pipeline-only | Guard suite green (12/12), 29 legacy endpoints 404 | All above land in 7 `api_*` modules + current tabs; new endpoints registered in CONTRACTS.md; no legacy symbols | FP8+FP1 / P0–P6 |

## Architecture

### Layer Mapping

| Component | Layer | Responsibility |
|---|---|---|
| `app/pipeline/api_export.py` | API | Render jobs/chunks/audio/export endpoints |
| `app/pipeline/api_walks.py` | API | Walk run history + persisted cancel |
| `app/pipeline/api_review.py` | API | Unified review queue + dispatch |
| `app/pipeline/api_operations.py` | API | Project snapshot save/load/delete |
| `app/app.py` (POST /api/config) | API | Raw-JSON merge config |
| `app/pipeline/adapter.py` (SQLiteAdapter) | Service | `transaction()` owner-thread guard, `reconcile_stale_runs()`, `walk_override` access — extends the existing PipelineStorage ABC adapter, no new module |
| `app/pipeline/walks/runner.py` | Service | Persisted `walk_run` rows; `is_cancel_requested(run_id)` single dispatcher |
| `app/pipeline/tts_integration.py` | Service | Render jobs → `render_job`/`render_chunk` rows; run dirs under RENDER_ROOT |
| `app/pipeline/review.py` | Service | Union queue, `walkitem:` item-id dispatch, supersede, value-restore |
| `app/utils.py` | Service | `resolve_task_config(task, storage, book_id)`; raw-JSON merge helpers |
| `app/static/` TS tabs | Frontend | voices.ts (cap4), setup.ts (cap6), editor-pipeline.ts (cap1/cap2), script.ts (cap2/cap7), result surface in editor-pipeline.ts (cap3) |

### Data Model (6 new tables + 3 indices + `book.single_speaker`)

```sql
ALTER TABLE book ADD COLUMN single_speaker INTEGER NOT NULL DEFAULT 0;

CREATE TABLE render_job ( -- replaces in-memory _render_jobs; rows = truth
  job_id TEXT PRIMARY KEY, book_id TEXT NOT NULL,
  mode TEXT NOT NULL CHECK (mode IN ('batch','individual')),
  status TEXT NOT NULL CHECK (status IN ('pending','running','completed','failed','cancelled','interrupted','expired')),
  error TEXT, output_dir TEXT, output_artifact_path TEXT,
  created_ms INTEGER NOT NULL, started_ms INTEGER, finished_ms INTEGER);
CREATE INDEX idx_render_job_book_status ON render_job(book_id, status);

CREATE TABLE render_chunk ( -- individual mode ONLY; done only after WAV exists+fsynced
  job_id TEXT NOT NULL REFERENCES render_job(job_id), idx INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending','done','failed','evicted')),
  wav_path TEXT, error TEXT, PRIMARY KEY (job_id, idx));

CREATE TABLE walk_run ( -- replaces WalkRunner._status
  run_id TEXT PRIMARY KEY, book_id TEXT NOT NULL, walk_name TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending','running','completed','failed','interrupted','cancelled')),
  cancel_requested INTEGER NOT NULL DEFAULT 0, heartbeat_ms INTEGER,
  result_json TEXT, error TEXT, created_ms INTEGER NOT NULL, finished_ms INTEGER);
CREATE INDEX idx_walk_run_book_status ON walk_run(book_id, status);

CREATE TABLE walk_review_item ( -- 2g/2h/2i review items, written in walk txn
  id TEXT PRIMARY KEY, book_id TEXT NOT NULL, run_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('voice_profile','voice_assignment','instruction')),
  target_table TEXT NOT NULL, target_id TEXT NOT NULL, prior_value TEXT,
  status TEXT NOT NULL CHECK (status IN ('pending','resolved','superseded','stale')),
  created_ms INTEGER NOT NULL);
CREATE INDEX idx_walk_review_item_book_status ON walk_review_item(book_id, status);

CREATE TABLE walk_override ( -- per-book per-walk overrides (prompts/generation params)
  book_id TEXT NOT NULL, walk_name TEXT NOT NULL, key TEXT NOT NULL,
  value_json TEXT NOT NULL, PRIMARY KEY (book_id, walk_name, key));

CREATE TABLE project_snapshot ( -- saved scripts; auto-named book+timestamp, rename PATCH
  name TEXT PRIMARY KEY, book_id TEXT NOT NULL, snapshot_json TEXT NOT NULL, created_ms INTEGER NOT NULL);
```

### API Surface (7 modified, 10 new)

**Modified:** `GET /render_status/{job_id}` → reads `render_job` rows + per-chunk counts (api_export) · `POST /cancel_render` → status `cancelling` + persisted (api_export) · `GET /download/{job_id}` → reads rows, FileResponse-404 subclass (api_export) · `POST /cancel_walks` → writes `walk_run.cancel_requested=1` (api_walks) · `GET /review/{book_id}` → union with `walkitem:` prefixed item_ids (api_review) · `POST /review/accept|reject|override` → prefix dispatch, walk-side value-restore (api_review) · `POST /api/config` → raw-JSON merge, validation-only AppConfig, `schema_version` stamp (app.py).

**New (registered in CONTRACTS.md):** `GET /export/jobs/{job_id}` + `GET /export/jobs/{job_id}/chunks` (api_export) · `GET /export/chunk/{job_id}/{idx}` bounded-range WAV (api_export) · `GET /export/audio/{job_id}` whole-book playback (api_export) · `POST /export/m4b` 3-phase polished export (api_export) · `GET /walks/{book_id}/runs` (api_walks) · `POST /projects`, `GET /projects`, `POST /projects/load`, `DELETE /projects/{name}` (api_operations).

### Workflows

- **Render:** `POST /render` → `render_job` row (running) + run dir `RENDER_ROOT/book-{id}/{job_id}/` → individual mode writes `render_chunk` rows per chunk (2 fsyncs: tmp→fsync→rename→fsync parent) → final txn sets `completed` + `output_artifact_path`; rows=truth, `manifest.json`=derived cache rebuilt at startup.
- **Walk:** `run_walk` → `walk_run` row → per-unit txn `SELECT(outside) → LLM(no txn) → with storage.transaction(): UPSERT + walk_review_item + heartbeat + COMMIT`; LLM never inside a transaction. Cancel dispatcher reads `walk_run.cancel_requested` + stop-file + event. On `ConcurrentTransactionError`: 50–100 ms backoff, retry idempotent write phase ×3, then fail unit.
- **Reconciliation (startup-only):** one pass flips `running→interrupted` for stale `render_job`/`walk_run` before the API accepts requests; no on-read sweeper, no periodic reaper (single-process ⇒ race-free).
- **Supersede:** in the walk's **final** transaction, per regenerated kind: `UPDATE walk_review_item SET status='superseded' WHERE book_id=? AND run_id<>? AND status='pending' AND kind=? AND target_id IN (new run's committed targets)`; on failure/cancel, nothing is superseded; items from interrupted runs stay reviewable.
- **Config:** parse raw JSON → validate known keys through AppConfig (`extra='ignore'`, output never serialized) → recursive deep-merge unknown paths → stamp `schema_version` → atomic write; `resolve_task_config()` applies on-disk config → `llm.task_overrides` → `walk_override` rows, snapshotted per walk-unit start.
- **Restore:** blocked by default while any active `walk_run`/`render_job` row exists; snapshot load = merge-vs-replace (characters never deleted); audio missing → explicit "re-render" notice.

## UX Workflow (per capability)

1. **Audio:** editor-pipeline.ts span row gains ▶ that resolves `GET /export/chunk/{job_id}/{idx}` and seeks a singleton `<audio>` (stopThenPlay: awaits stop before play; AbortError/NotAllowedError benign; tap-to-continue for autoplay). Result surface plays whole book via `GET /export/audio/{job_id}`. Batch renders: tooltip "preview differs from final — whole-book playback only".
2. **Progress/cancel:** Script tab polls walk run rows; Result tab shows completed/total + failures per chunk (individual) or job-level (batch); Cancel buttons call `cancel_walks`/`cancel_render`; collisions surface 503+Retry-After, frontend retries once.
3. **Export:** Result tab "Export M4B" form (title/author/narrator/year/description) + cover upload + auto chapter markers (single ffprobe, END clamped); MP3 and Audacity ZIP_STORED bundle where supported.
4. **Voice config:** Voices tab edit form on each card: style (character_style), ref-audio/ref-text, type switch (custom/clone/builtin_lora/lora/design), narrator override (NARRATOR DB row wins; UNKNOWN→NARRATOR), preview; alias picker = dropdown of existing voices (follow-up).
5. **Review:** Review tab renders junction items + walk items with kind badges; accept/reject/override dispatches on `junction:`/`walkitem:` prefix; contextual review shows ±2 neighboring spans.
6. **Overrides:** Setup tab per-walk override fields (temperature/prompts); save preserves unknown keys (round-trip loss fixed).
7. **Iteration:** Projects tab: Save (auto-named) / Load / Delete / Rename; undo = transactional value-restore + snapshot restore; single-speaker toggle writes `book.single_speaker`; pause-after via TTS `pause_between_speakers_ms`/`pause_same_speaker_ms` (global, per-span variant not restorable); sequence playback = player queue of span URLs.

## Migration Constraints

- Pipeline-only: no legacy endpoints/modules/toggles/shims; `tests/pipeline/test_legacy_removed.py` 12/12 stays green; no `/api/chunks/*`, `/api/merge_m4b`, `/api/scripts/*`, `/api/export_audacity`, persona endpoints.
- `app/tts.py` byte-for-byte untouchable (duck-typed `generate_batch`/`generate_voice` contract; `_build_voice_config`/`_build_chunks` boundary in tts_integration.py is the only allowed seam).
- Walks 2g/2i strictly serial; walk re-execution only explicit user trigger; no ×0.8 degraded-confidence auto-accept.
- New endpoints in correct `api_*` module + registered in CONTRACTS.md (done as part of this DD).
- SQLite mechanics: `isolation_level=None` explicit, BEGIN IMMEDIATE via `transaction()` with explicit COMMIT/ROLLBACK, INTEGER unix ms, PRAGMA busy_timeout=5000, negative-space reindex.
- Frontend: committed dist/ at `app/static/dist/` with CI `npm run build && git diff --exit-code app/static/dist/`; starlette>=0.49.1 CI pin (Range DoS GHSA-7f5h-v6xp-fcq8).
- **Doc drift fixes (in scope):** README progress-UI overstatement ("N spans loaded" static bar) corrected to real per-chunk progress; false claims of voice review surface + alias editing removed; stale artifact (`DD-frontend-rebuild-per-task-llm-config.md`, 276-line version at `artifacts/designs/completed/`) archived/cleaned.

## Test Strategy

- Baseline: 768 backend pytest (pipeline ~89% cov), 167 vitest, 12/12 guard, smoke checks — all stay green. Note: no smoke test files currently exist in the repo; the smoke-check harness location must be re-established during implementation.
- Spec-first per DD conventions; `InMemorySQLiteAdapter` fresh per test + cleared `dependency_overrides` for logic; **one file-backed `SQLiteAdapter(tmp_path)` fixture** for reconciliation/crash tests (`:memory:` conceals crash recovery).
- `transaction()` tests: owner-thread guard (txn on A → B write raises `ConcurrentTransactionError`), nested join, commit/rollback, walk-side retry.
- New `tests/pipeline/test_export.py` closes the **zero-test `download/{job_id}` gap**: present-file 200, zip fallback, unknown 404, range 206+Content-Range, malformed 416, missing-file 404, M4B 200 audio/mp4.
- Config: byte-stable round-trip CI test — fixture MUST include unknown keys; AppConfig validation `extra='ignore'`; FFmetadata generator CI-validated on 2-chunk fixture (TIMEBASE, integer ms, chapter END clamp).
- Frontend: coverage via already-installed `@vitest/coverage-v8` (^4.1.10, `test:coverage` script); media stubs in `frontend/vitest.setup.ts`; polling tests `vi.useFakeTimers({toFake:['setTimeout','setInterval']})` + fully mocked fetch; audio singleton via injectable `createPreviewPlayer()` factory.
- Documented non-coverage: WAL/busy_timeout/multi-connection stress; `soundfile` not installed → render 503s in smoke (expected, documented).

## Prioritized Phases

Estimator: **LARGE** ~154K weighted chars, ~40 unique files, 4 ship groups. Phases are structural layers of one cohesive commit, not incremental rollouts.

| Phase | Content | Size / Files | Ship group |
|---|---|---|---|
| P0 | `transaction()` owner-thread guard + `isolation_level=None`/busy_timeout + 6 tables/3 indices + `book.single_speaker` | cross-cutting SMALL ~8K/5 | 1 |
| P1 | A1-r jobs: `walk_run`/`render_job`/`render_chunk` writes, startup reconciliation, persisted cancel, walk-side retry, jobs/chunks/runs endpoints, download rewrite | FP1 MEDIUM ~22K/10 | 1 |
| P2 | A2-r artifacts: RENDER_ROOT run dirs, fsync discipline, manifest-derived, tombstoning GC (≥7 days, hourly; snapshot refs in eligibility union), audio/chunk range endpoints | FP2 SMALL ~11K/6 | 1 |
| P3 | A3-r review union: `walk_review_item` writes in 2g/2h/2i txns, completion-time per-target supersede, union queue + dispatch, value-restore undo | FP3 SMALL ~19K/10 | 1 |
| P4 | cap1+cap3 frontend: singleton player, per-span preview, progress/cancel UI, M4B/MP3/Audacity export UI, sequence playback | C1 SMALL ~14K/7 + C3 MEDIUM ~24K/7 | 2 |
| P5 | A4-r config backend + setup.ts overrides: raw-JSON merge, `resolve_task_config`, `walk_override` CRUD, byte-stable test (parallel-trackable) | C6 SMALL ~14K/6 | 3 |
| P6 | cap4 voices.ts edit form + cap7 snapshots: project save/load/delete/rename UI, single-speaker toggle, undo wiring | C4 SMALL ~9K/4 + C7 MEDIUM ~33K/9 (riskiest: undo/snapshot semantics; per-chunk cancel individual-only) | 4 |

## Cannot-Restore Decisions (explicit)

1. **Any change to `app/tts.py`** (byte-for-byte untouchable) — implies: no per-chunk regeneration/cancel on the **batch** path; per-chunk preview exists only in individual mode.
2. **Per-chunk regeneration on the batch path** — `generate_batch` has no per-chunk callback; batch renders expose whole-book playback only (UI tooltip).
3. **Legacy saved-script format compatibility** (`/api/scripts*` JSON) — replaced by `project_snapshot` manifests.
4. **Prompt-file module resurrection** (`default_prompts.py`/`review_prompts.py`/`persona_prompts.py` + `.txt`) — replaced by config.json sections + `walk_override` rows.
5. **`pause_after` as a separate per-span field** — replaced by global TTSConfig `pause_between_speakers_ms`/`pause_same_speaker_ms` (verified: no per-span pause field in TTSConfig; tts.py `pause_overrides` exists but is untouchable).
6. **Persona endpoints** (`/api/generate_personas`, `/api/cancel_persona`) — replaced by walks 2b/2g/2h/2i.
7. **Degraded-confidence auto-accept (×0.8)** — deliberately removed in rewrite; not restored.
8. **Parallelizing walks 2g/2i** — strictly serial (contract).
9. **Legacy endpoints as-is** (`/api/chunks/*`, `/api/merge_m4b`, `/api/audiobook*`, `/api/export_audacity`, `/api/cancel_audio`, `/api/save_voice_config`, `/api/scripts/*`) — pipeline-native equivalents only.
10. **Legacy modules/files** — `app/project.py` (ProjectManager), chunks.json, `editor-legacy.ts`, `audio.ts`, toggles, shims.
11. **Timestamps in the schema** — no timestamp columns on spine/graph; only new job/snapshot tables use INTEGER unix ms; re-onboard via `book.version`.
12. **Batch-mode per-chunk audio surface** — per-chunk rows exist in individual mode only.
13. **Config keys as validated AppConfig fields** — generation/prompts keys survive only as raw JSON, never as pydantic fields.
14. **On-read heartbeat reconciliation sweeper** — startup-only reconciliation is the race-free single-process choice; heartbeat is observability-only.

## Design Decisions

| Decision | Rationale |
|---|---|
| Owner-thread `transaction()` guard + walk-side retry (NOT per-thread connections) | Single-connection topology preserved; collisions are fail-fast 503+retry; per-thread connections would invalidate test fidelity |
| Individual render default for audiobook-length renders; batch only when drift-insensitive | Per-span preview needs `render_chunk` rows; batch drifts per-chunk (unset seed); UI-level "preview differs from final" tooltip is the contract |
| Rows = truth, manifest = derived | One authority per artifact class; manifest rebuilt at startup reconciliation |
| Raw-JSON merge config (validation-only AppConfig, byte-stable CI test) | Fixes round-trip data loss; pydantic extras handling has a history of dropping keys on dump |
| Startup-only reconciliation | On-read heartbeat false-positives on multi-minute LLM units; single-process ⇒ race-free |
| Completion-time per-target supersede with per-walk coverage gate | Supersede-at-start loses candidates; per-target (only targets the new run produced) is the safe default |
| Snapshot projects (auto-named, rename PATCH), restore blocked during active runs | Restore-while-running corrupts; characters never deleted (shared series graphs) |
| Single 0.5–0.7 review band v1; log outcomes for future per-kind calibration | No ×0.8; no uncalibrated band multiplication |
| `book.single_speaker` enforced at render boundary only | Preserves audition-multi-voice-then-ship-single workflow; `export_annotated_script` stays faithful for editor |
| Startup `PRAGMA busy_timeout=5000`; GC ≥7 days hourly, rows never time-deleted | Sweep never on hot request path; snapshot manifests join the GC reference union |
| New endpoints in correct `api_*` module, registered in CONTRACTS.md | Contract gate: every endpoint/schema change must be registered |

## Open Items (implementation decisions)

1. **Retention numbers to lock:** exact `job_retention_days`/`chunk_retention_days` env defaults (≥7 days, hourly sweep) — confirm against disk-budget expectations.
2. **Walk coverage proofs for supersede-all:** per-kind re-coverage proofs needed before ever allowing supersede-all; v1 ships per-target only.
3. **BEGIN IMMEDIATE × busy_timeout stress:** untested in `:memory:`; add a file-backed stress fixture during P1.
4. **Span→chunk offset mapping for per-span seek:** needs stable text-hash → offset alignment between `span_presentation` and `render_chunk.idx`.
5. **Alias-map picker + sequence UX:** alias_of = dropdown of existing voices (no free-form map editor, Q5); sequence play-order = presentation order of loaded spans — confirm whether review-filtered spans are included.
6. **cap4 edit form scope:** minimal viable per Q5 (style + ref-audio + type + narrator override + preview); ref-text and adapter fields follow-up.
7. **`export_annotated_script` speaker fidelity** under single-speaker: keep faithful (editor contract) — verify no UI path leaks render-only normalization.
8. **Result-surface placement + ffmpeg availability:** cap1/cap3 surface lands in `editor-pipeline.ts` (no new tab) unless UX review says otherwise; feature-detect `libmp3lame` and degrade to M4B-only with clear messaging.

## Evidence Trail

Full adversarial refinement log: [ADVERSARIAL-universal-upgrade.md](../process/ADVERSARIAL-universal-upgrade.md)

- Surviving approaches: A1-r Schema-Native Jobs with Reconciliation (walk-side retry, owner-thread guard), A2-r Artifact-First Run Directory with fsync Discipline (rows=truth/manifest=derived, tombstoning GC), A3-r Unified Review Queue Honest Union + Completion-Time Supersede, A4-r Overlay Config + Snapshot Projects (raw-JSON merge, recursive preservation).
- Rejected: materialized review queue (dual-write drift), audit-journal replay undo (value-restore + snapshot instead), standalone audio surface without FP1/FP2 (ephemeral audio, no progress), on-read heartbeat reconciliation (false positives).
- Sources: rnd-architect.log L10 (blueprints, Q1–Q9, 14 cannot-restore items), support-researcher.log L4/L5 (cutover comparison, regression inventory), CONTRACTS.md, DD-v3.
