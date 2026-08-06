# Universal Upgrade — Contracts Ledger

**Design doc:** [`artifacts/designs/pending/DD-universal-upgrade.md`](../../pending/DD-universal-upgrade.md)
**Authoritative schema/API registration:** [`../epub-audiobook-pipeline-rewrite/CONTRACTS.md`](../epub-audiobook-pipeline-rewrite/CONTRACTS.md) § Universal Upgrade (lines 906-948) — registered by the DD; this ledger carries method-level and behavioral contracts per plan.
**Last updated:** 2026-08-06
**Status:** Decomposition complete — plans A-J created. Ledger entries filled per plan. **Plan A (Schema & Transaction Foundation) IMPLEMENTED + QA-VALIDATED 2026-08-06** — transaction() owner-thread guard, ConcurrentTransactionError, isolation_level=None, busy_timeout=5000, 6 tables + 3 indices + book.single_speaker all live in app/pipeline/adapter.py + schema.py; 841 tests green, guard 12/12, adapter/schema coverage 100%. **Plan B (Render & Walk-Run Persistence) IMPLEMENTED + QA-VALIDATED 2026-08-06** — commit 6636c78; walk_run/render_job/render_chunk rows = truth written+read by runner/tts_integration/api_export; is_cancel_requested(run_id) single dispatcher (DB row + stop-file + event); reconcile_stale_runs() startup-only one-pass (running→interrupted, 5-min grace); persisted cancel via walk_run.cancel_requested=1 + stop-files; endpoints render_status per-chunk counts, export/jobs/{job_id}, export/jobs/{job_id}/chunks, walks/{book_id}/runs, download rewritten row-backed w/ FileResponse404; walk-side ConcurrentTransactionError retry ×3 50-100ms backoff (write phase only); smoke harness re-established (tests/pipeline/test_smoke.py); 894 tests green, guard 12/12, runner 75% cov (was 67%), api_export 95% (was 76%). Contract note: cancel_render sets no `cancelling` status (not storable under Plan A render_job.status CHECK); in-process event carries intent, row reaches terminal `cancelled`, crash-survival via reconciliation (exec-manager log L20). **Plan C (Artifacts, GC & Export Backend) IMPLEMENTED + QA-VALIDATED 2026-08-06** — commit 742abe5; RENDER_ROOT run dirs (env RENDER_ROOT default data/render_root) with 2-fsync chunk discipline (tmp→fsync→rename→fsync-dir); derived manifest.json (rows=truth, atomic, best-effort) + startup rebuild (rebuild_manifests, row-driven, dir-gone→'expired' with error 'artifact missing: run dir not found'); tombstoning GC (gc_expired_artifacts, JOB_RETENTION_DAYS/CHUNK_RETENTION_DAYS default 7.0, effective = longer of the two, hourly scheduler GC_INTERVAL_HOURS=1 via app.py lifespan, PIPELINE_GC_SCHEDULER=0 opt-out, snapshot-reference eligibility union, rows tombstoned evicted/expired never time-deleted); endpoints GET /export/chunk/{job_id}/{idx} (bounded-range, PIPELINE_MAX_RANGE_BYTES default 4MiB, 200/206/400/416, 409/410), GET /export/audio/{job_id} (artifact or synthesized streaming concat, Range across chunk boundaries, HEAD), POST /export/m4b (3-phase FFMETADATA1 concat→metadata→mux, TIMEBASE=1/1000 int-ms chapters END-clamped, cover attached_pic + PIPELINE_MAX_COVER_BYTES default 20MiB, MP3 libmp3lame feature-detect with M4B-only degrade messaging, Audacity ZIP_STORED, output_artifact_path updated on success only); 1013 tests green (was 894 Plan B), guard 12/12, coverage TOTAL 91% (api_export 92% was 76% Plan B baseline — ROSE as required; adapter 97%, tts_integration 97%).

## Architectural Rules

1. **Pipeline-only surface.** All new endpoints live in the 7 existing `api_*` modules (`api_onboard`, `api_walks`, `api_operations`, `api_review`, `api_export`, `api_characters`, `api_voices`) behind `APIRouter(prefix="/api/pipeline")`. No new module files. `tests/pipeline/test_legacy_removed.py` stays 12/12 green; 29 legacy endpoints keep 404ing.
2. **`app/tts.py` byte-for-byte untouchable.** `_build_voice_config` / `_build_chunks` in `app/pipeline/tts_integration.py` is the only allowed seam. No per-chunk regeneration on the batch path; batch = whole-book playback only.
3. **Rows = truth, manifest = derived.** One authority per artifact class. `manifest.json` is a derived cache rebuilt at startup reconciliation, never written on the hot render path.
4. **SQLite discipline:** `isolation_level=None` explicit, `BEGIN IMMEDIATE` via `transaction()` with explicit `COMMIT`/`ROLLBACK`, owner-thread guard (writes from non-owner thread → `ConcurrentTransactionError` → API 503 + `Retry-After`), `PRAGMA busy_timeout=5000` at startup, INTEGER unix ms timestamps (new tables only), single-connection topology preserved.
5. **Startup-only reconciliation.** One pass at startup flips stale `running` → `interrupted` for render_job/walk_run before the API accepts requests. No on-read sweeper, no periodic reaper.
6. **LLM never inside a transaction.** Per-unit pattern: SELECT (outside txn) → LLM call (no txn) → `with storage.transaction():` UPSERT + walk_review_item + heartbeat + COMMIT. `ConcurrentTransactionError` → 50-100ms backoff retry of idempotent write ×3, then fail unit.
7. **Cancel:** single dispatcher `is_cancel_requested(run_id)` reads DB row (`cancel_requested=1`) + stop-file + event. Persisted cancel survives process restart. Batch renders: job-level cancel only; individual mode: per-chunk.
8. **Review:** single 0.5–0.7 band v1; ≥0.7 accept, <0.5 reject, 0.5–0.7 review. No ×0.8 degraded auto-accept. `walkitem:`-prefixed IDs for walk-derived review items; junction items use `{junction_table}:{character_id}:{entity_id}` ids (unprefixed); dispatch on the `walkitem:` prefix, else junction.
9. **Supersede:** completion-time per-target only (in walk's FINAL transaction). Nothing superseded on failure/cancel.
10. **Snapshots:** restore blocked while any active walk_run/render_job row exists; snapshot load merges (characters never deleted); audio missing → explicit "re-render" notice.
11. **Config:** raw-JSON merge, validation-only AppConfig (`extra='ignore'`, output never serialized), `schema_version` stamp, byte-stable round-trip guaranteed.
12. **GC:** ≥7 days post-completion, env-tunable (`job_retention_days`/`chunk_retention_days`), hourly sweep, never on hot request path; eligibility union includes project_snapshot artifact refs; rows tombstoned (evicted/expired) in the same sweep as file deletion.
13. **Frontend:** committed `app/static/dist/` + CI gate `npm run build && git diff --exit-code app/static/dist/`; starlette≥0.49.1 pin preserved (Range DoS GHSA-7f5h-v6xp-fcq8).

## Collections & Methods

### SQLiteAdapter (Plan A)
| Method | Signature | Notes |
|--------|-----------|-------|
| `transaction` | `transaction() -> TransactionContext` | Context manager. Sets `isolation_level=None` on connect; issues `BEGIN IMMEDIATE`; records owner thread; explicit `COMMIT` on exit, `ROLLBACK` on exception. Writes from a non-owner thread raise `ConcurrentTransactionError`. |
| `reconcile_stale_runs` | `reconcile_stale_runs() -> dict[str, int]` | Startup-only. Flips stale `running` render_job/walk_run rows to `interrupted` in one pass. Returns counts per table. (Plan B) |
| `rebuild_manifests` | `rebuild_manifests(render_root) -> dict[str,int]` | Startup-only. Row-driven manifest regeneration for completed render_job rows; run dir derived when output_dir NULL; completed rows with missing run dir → status 'expired' + error 'artifact missing: run dir not found'. Called from api_onboard startup after reconcile_stale_runs. (Plan C) |
| `gc_expired_artifacts` | `gc_expired_artifacts(render_root, *, job_retention_days, chunk_retention_days) -> dict` | Tombstoning GC sweep: deletes run dir files for completed jobs older than effective retention (longer of job/chunk), tombstones render_chunk→'evicted' and render_job→'expired' in the same sweep; snapshot-reference eligibility union keeps referenced run dirs alive; rows never time-deleted. (Plan C) |
| `init_db` (extended) | `init_db() -> None` | Now also issues `PRAGMA busy_timeout=5000` and enables `transaction()` mode. (Plan A) |

### GC scheduler module functions (Plan C)
| Function | Notes |
|----------|-------|
| `start_gc_scheduler` / `stop_gc_scheduler` / `_gc_scheduler_loop` | Hourly daemon thread (`GC_INTERVAL_HOURS` default 1h), started from app.py FastAPI lifespan (never at import; TestClient-without-context-manager safe), `PIPELINE_GC_SCHEDULER=0` opt-out, first sweep deferred one interval. (Plan C) |

### New exception (Plan A)
| Name | Notes |
|------|-------|
| `ConcurrentTransactionError(RuntimeError)` | Raised when a write is attempted from a thread that does not own the open transaction, or when `BEGIN IMMEDIATE` times out under contention. Mapped to 503 + `Retry-After` in api layer. |

### Schema additions (Plan A) — registered in upstream CONTRACTS.md § Universal Upgrade
- `ALTER TABLE book ADD COLUMN single_speaker INTEGER NOT NULL DEFAULT 0` (render-boundary enforcement only)
- `render_job(job_id TEXT PK, book_id, mode CHECK(batch|individual), status CHECK(pending|running|completed|failed|cancelled|interrupted|expired), error, output_dir, output_artifact_path, created_ms, started_ms, finished_ms)` + `idx_render_job_book_status(book_id, status)`
- `render_chunk(job_id FK, idx, status CHECK(pending|done|failed|evicted), wav_path, error, PK(job_id, idx))` — individual-mode only; `done` only after WAV exists+fsynced; `evicted` = GC tombstone
- `walk_run(run_id TEXT PK, book_id, walk_name, status CHECK(pending|running|completed|failed|interrupted|cancelled), cancel_requested INT DEFAULT 0, heartbeat_ms, result_json, error, created_ms, finished_ms)` + `idx_walk_run_book_status(book_id, status)`
- `walk_review_item(id TEXT PK, book_id, run_id, kind CHECK(voice_profile|voice_assignment|instruction), target_table, target_id, prior_value, status CHECK(pending|resolved|superseded|stale), created_ms)` + `idx_walk_review_item_book_status(book_id, status)`
- `walk_override(book_id, walk_name, key, value_json, PK(book_id, walk_name, key))`
- `project_snapshot(name TEXT PK, book_id, snapshot_json, created_ms)`

### Walks 2g/2h/2i (Plan D)
| Method | Notes |
|--------|-------|
| walk_review_item writes | Each of 2g/2h/2i writes a `walk_review_item` row in the same transaction as its junction writes: kind `voice_profile` (2g), `voice_assignment` (2h), `instruction` (2i); `prior_value` captured from the pre-write row. |
| completion-time supersede | In the walk's FINAL transaction, per regenerated kind: `UPDATE walk_review_item SET status='superseded' WHERE book_id=? AND run_id<>? AND status='pending' AND kind=? AND target_id IN (new run's committed targets)`. |

### ReviewManager (Plan D)
| Method | Notes |
|--------|-------|
| `get_review_items(book_id, walk_name=None)` (extended) | Honest union: junction live query (existing) + `walk_review_item` rows where status='pending'. Walk items carry ids `walkitem:{id}`. |
| `resolve_review_action(action, item_id, new_value)` (extended) | Prefix dispatch: `junction:` → existing behavior; `walkitem:` → walk-side value-restore: restore `prior_value` into `target_table.target_id` transactionally + mark row `resolved`. |
| supersede helper | `supersede_targets(storage, *, book_id, run_id, kind, target_ids)` — module-level helper (NOT a ReviewManager method), used by the walk's final transaction. |

### WalkRunner (Plan B)
| Method | Signature | Notes |
|--------|-----------|-------|
| `is_cancel_requested` | `is_cancel_requested(run_id) -> bool` | Single dispatcher: reads `walk_run.cancel_requested` + stop-file + event. Replaces direct `_cancelled` dict reads. |
| `run_walk` (extended) | `run_walk(walk_name, book_id, config) -> dict` | Writes `walk_run` row (running) at start; heartbeats inside per-unit transaction; final status + result_json at end; `interrupted` on reconciliation flip. |
| `run_all_walks` (extended) | unchanged signature | Same walk_run lifecycle per walk; abort-on-first-failure preserved. |

### tts_integration (Plans B/C)
| Method | Notes |
|--------|-------|
| `render_audiobook` (extended) | Now takes `job_id`; writes render_job row (running) + per-chunk render_chunk rows in individual mode; final transaction sets completed + output_artifact_path. Rows = truth. |
| render_chunk discipline | WAV written to tmp → fsync → rename → fsync parent dir; only then render_chunk row marked `done`. |
| `get_render_root` | Reads env `RENDER_ROOT` at call time; default `data/render_root` under cwd (`data/` gitignored). (Plan C) |
| `_write_manifest` | Derived cache write: atomic tmp → fsync → rename → fsync-dir; chunk `wav_path` entries relative to run dir; written after row completed. (Plan C) |

### Config (Plan G)
| Method | Notes |
|--------|-------|
| `resolve_task_config(task, storage, book_id) -> dict` | Single helper replacing dead `resolve_task_llm(task, config_path=None)` param. Applies on-disk config → llm.task_overrides → walk_override rows, snapshotted per walk-unit start. Called from all 9 walks. |
| raw-JSON merge (app.py POST /api/config) | Parse raw JSON → validate known keys through AppConfig(extra='ignore', output never serialized) → recursive deep-merge unknown paths → stamp schema_version → atomic write. Byte-stable round-trip. |

## API Contracts

### Modified (Plan B: jobs/walks; Plan D: review; Plan G: config)
| METHOD path | Module | Change |
|-------------|--------|--------|
| GET `/api/pipeline/render_status/{job_id}` | api_export | Reads render_job row; adds per-chunk counts (done/total/failed) for individual mode. |
| POST `/api/pipeline/cancel_render` | api_export | Sets render_job status `cancelling` + persisted cancel flag (survives restart). |
| GET `/api/pipeline/download/{job_id}` | api_export | Reads rows; FileResponse-404 subclass for missing artifacts. |
| POST `/api/pipeline/cancel_walks` | api_walks | Writes `walk_run.cancel_requested=1` (persisted). |
| GET `/api/pipeline/review/{book_id}` | api_review | Union queue with `walkitem:` prefixed ids. |
| POST `/api/pipeline/review/accept\|reject\|override` | api_review | Prefix dispatch; walk-side value-restore. |
| POST `/api/config` | app.py | Raw-JSON merge + validation-only AppConfig + schema_version stamp. |

### New (Plan B: runs/jobs/chunks list; Plan C: audio/chunk/export; Plan I: projects)
| METHOD path | Module | Plan |
|-------------|--------|------|
| GET `/api/pipeline/export/jobs/{job_id}` | api_export | B — job row detail |
| GET `/api/pipeline/export/jobs/{job_id}/chunks` | api_export | B — chunk rows |
| GET `/api/pipeline/export/chunk/{job_id}/{idx}` | api_export | C — bounded-range WAV (206/416) |
| GET `/api/pipeline/export/audio/{job_id}` | api_export | C — whole-book playback |
| POST `/api/pipeline/export/m4b` | api_export | C — 3-phase FFMETADATA1 export |
| GET `/api/pipeline/walks/{book_id}/runs` | api_walks | B — walk_run rows |
| POST `/api/pipeline/projects` | api_operations | I — save snapshot (auto-named) |
| GET `/api/pipeline/projects` | api_operations | I — list snapshots |
| POST `/api/pipeline/projects/load` | api_operations | I — load snapshot (merge) |
| DELETE `/api/pipeline/projects/{name}` | api_operations | I — delete snapshot |
| PATCH `/api/pipeline/projects/{name}` | api_operations | I — rename snapshot (DD design: auto-named + rename) |

## DTOs Created

| DTO | Plan | Fields |
|-----|------|--------|
| RenderJobStatus (frontend type) | B | job_id, status, mode, completed_chunks, total_chunks, failed_chunks, error, output_dir |
| WalkRunRow (frontend type) | B | run_id, walk_name, status, heartbeat_ms, created_ms, finished_ms, error |
| ExportJobDetail | B | job_id, book_id, mode, status, error, output_dir, output_artifact_path, created_ms, started_ms, finished_ms |
| ChunkRow | B/C | job_id, idx, status, wav_path, error |
| ReviewItem (extended) | D | item_id (junction:/walkitem: prefixed), kind, target_table, target_id, prior_value, created_ms; confidence/human_override are junction-only fields — walk items carry exactly {item_id, kind, target_table, target_id, prior_value, created_ms} |
| ProjectSnapshot (frontend type) | I | name, book_id, created_ms, size_bytes |
| SnapshotLoadRequest | I | name, book_id |
| RenameProjectRequest | I | new_name |
| OverrideRow (frontend type) | G | book_id, walk_name, key, value_json |

## Decisions Made

| # | Decision | Plan | Rationale |
|---|----------|------|-----------|
| 1 | Owner-thread transaction() guard + walk-side retry, NOT per-thread connections | A | Single-connection topology preserved; fail-fast 503+Retry-After; idempotent write retry ×3 with 50-100ms backoff |
| 2 | Individual render DEFAULT; batch only where drift-insensitive | B/C | Per-span preview needs render_chunk rows; batch drifts per-chunk unset seed; UI tooltip "preview differs from final" is the contract |
| 3 | rows=truth, manifest=derived | B/C | One authority per artifact class; manifest rebuilt at startup reconciliation |
| 4 | Startup-only reconciliation | B | On-read heartbeat false-positives multi-minute LLM units; single-process race-free |
| 5 | Completion-time per-target supersede with per-walk coverage gate | D | Supersede-at-start loses candidates; v1 per-target only (coverage proofs for supersede-all are open item #2) |
| 6 | Raw-JSON merge config; validation-only AppConfig; byte-stable CI test | G | Fixes round-trip loss; pydantic extras drops keys on dump; fixture MUST include unknown keys |
| 7 | Snapshot projects auto-named + rename PATCH; restore blocked during active runs; characters never deleted | I | No legacy saved-script format; merge-vs-replace load |
| 8 | Single 0.5-0.7 review band v1; log outcomes for future per-kind calibration | D | Matches existing thresholds; no ×0.8 |
| 9 | book.single_speaker enforced at render boundary only | J | Preserves audition-multi-voice-then-ship-single; export_annotated_script stays faithful |
| 10 | GC ≥7 days hourly; rows never time-deleted; snapshot manifests join GC reference union | C | Retention env-tunable; tombstone rows + delete files in same sweep |
| 11 | PATCH /projects/{name} for rename | I | DD design decision "auto-named, rename PATCH" — appended beyond the 10 registered endpoints |
| 12 | Tab-navigation foundation fixed in Plan E | E | Evidence-based: frontend tabs are unreachable today (no click handler); all new UI lives in hidden tabs |
