# Task: Render & Walk-Run Persistence

## Problem Statement

Today render jobs live in a module-level dict `_render_jobs` (app/pipeline/api_export.py:69) that is never cleaned or evicted, walk status lives in WalkRunner in-memory `_status`/`_cancelled` dicts (walks/runner.py:290-292), and cancellation is a `threading.Event` that dies with the process. A process restart loses all job/walk state; there is no per-chunk progress; `GET /download/{job_id}` has zero test coverage; `POST /cancel_render`/`POST /cancel_walks` are ephemeral.

The Universal Upgrade design (DD-universal-upgrade, A1-r Schema-Native Jobs with Reconciliation) makes SQLite rows the source of truth: walk_run and render_job/render_chunk rows, startup-only reconciliation (stale `running` → `interrupted`), persisted cancellation via `walk_run.cancel_requested=1`, a single `is_cancel_requested(run_id)` dispatcher, walk-side retry on `ConcurrentTransactionError`, and the jobs/chunks/runs endpoint surface plus the download rewrite. This plan implements the persistence layer and API surface; the RENDER_ROOT run directories and fsync discipline land in Plan C.

## Dependencies

- Plan A (transaction(), tables, single_speaker) — completed and archived
- `artifacts/designs/parts/epub-audiobook-pipeline-rewrite/CONTRACTS.md` § Universal Upgrade — endpoint registration

## Phases

### Phase 1: Smoke-check harness re-establishment (baseline)
- [ ] Research where the smoke-check harness should live (DD test strategy requires smoke checks; none exist today) — propose `tests/pipeline/test_smoke.py` booting the FastAPI app via TestClient with stubbed TTS engine and asserting /api/pipeline/onboard, /api/pipeline/render 503-without-engine, /api/pipeline/export/{book_id} routes are reachable; document the chosen location in the step annotation
- [ ] Create the smoke harness file with the app boot (guard-compatible import path: preload app/utils.py + app/hf_utils.py before importing app.app) and the initial route-reachability checks; TDD GREEN: run it and confirm it passes with the current in-memory implementation
- [ ] Verify: `ruff check tests/pipeline/test_smoke.py` clean; `pytest tests/pipeline/test_smoke.py -q` passes

### Phase 2: walk_run persistence and is_cancel_requested dispatcher
- [ ] TDD RED: write tests in tests/pipeline/test_runner.py asserting run_walk creates a walk_run row (status running) at start, updates to completed with result_json at end, and records created_ms/finished_ms; run_all_walks does the same per walk; cancel_walks persists cancel_requested=1 and is_cancel_requested(run_id) returns True afterwards
- [ ] Implement: WalkRunner.run_walk writes walk_run row (running) before executing the walk module; on completion writes status=completed + result_json; on exception writes status=failed + error; heartbeat_ms updated inside each per-unit transaction
- [ ] Implement `WalkRunner.is_cancel_requested(run_id) -> bool` as the single dispatcher: reads walk_run.cancel_requested + stop-file + event; replace direct `_cancelled` dict reads in run_walk/run_all_walks/cancel_walks with it; keep clear_cancel semantics
- [ ] TDD GREEN: `pytest tests/pipeline/test_runner.py -q --cov=app/pipeline --cov-report=term-missing` passes with runner coverage rising from 67% baseline
- [ ] Verify: `ruff check app/pipeline/walks/runner.py` clean; guard suite still 12/12

### Phase 3: render_job / render_chunk rows and per-chunk progress
- [ ] TDD RED: write tests in tests/pipeline/test_tts_integration.py asserting render_audiobook(job_id=...) writes a render_job row (running), writes render_chunk rows per chunk in individual mode (status pending then done), and sets render_job completed + output_artifact_path in the final transaction; batch mode writes job-level status only (no chunk rows)
- [ ] Implement: api_export POST /render creates the render_job row + passes job_id into render_audiobook; render_audiobook (app/pipeline/tts_integration.py) writes render_chunk rows per chunk in individual mode (chunk status done only after the WAV exists — fsync discipline lands in Plan C; here set done after write returns), discards the old silent ignore of generate_batch's completed/failed lists and records failures
- [ ] Extend GET /render_status/{job_id} to read the render_job row and return per-chunk counts (completed_chunks/total_chunks/failed_chunks) for individual mode; keep the existing response fields for backward compatibility with the frontend poll
- [ ] TDD GREEN: run tts_integration + api_export tests; coverage of api_export.py rises from 76% baseline; all green
- [ ] Verify: `ruff check app/pipeline/api_export.py app/pipeline/tts_integration.py` clean

### Phase 4: Startup reconciliation (rows = truth)
- [ ] TDD RED: write tests asserting `reconcile_stale_runs()` flips running→interrupted for stale render_job and walk_run rows (stale = started before a cutoff, no heartbeat), leaves completed/failed/cancelled rows untouched, and is invoked once at startup before API requests are accepted
- [ ] Implement `SQLiteAdapter.reconcile_stale_runs() -> dict[str, int]` (one pass, per contract rule #5 — no on-read sweeper, no periodic reaper); wire it into the production storage bootstrap in api_onboard.py (or the startup path where _get_production_storage is called)
- [ ] TDD GREEN: run reconciliation tests with a file-backed SQLiteAdapter(tmp_path) fixture (per DD test strategy — :memory: conceals crash recovery); assert stale rows flipped and fresh rows untouched
- [ ] Verify: `ruff check app/pipeline/adapter.py app/pipeline/api_onboard.py` clean; `pytest tests/pipeline/test_adapter.py -q` green

### Phase 5: Persisted cancel + endpoints (render_status, jobs, chunks, runs)
- [ ] TDD RED: write tests/pipeline/test_api.py tests asserting POST /cancel_render marks the render_job row cancelling + persisted cancel flag; POST /cancel_walks writes walk_run.cancel_requested=1; GET /export/jobs/{job_id} returns job detail; GET /export/jobs/{job_id}/chunks returns chunk rows; GET /walks/{book_id}/runs returns walk_run rows newest-first
- [ ] Implement the endpoint surface in api_export.py (export/jobs/{job_id}, export/jobs/{job_id}/chunks) and api_walks.py (walks/{book_id}/runs), plus the persisted cancel behavior on cancel_render/cancel_walks; register routes via the existing pipeline router (api.py aggregator)
- [ ] TDD GREEN: run the new endpoint tests; assert 404 for unknown job_id/run_id and 200 for known; keep FastAPI TestClient usage consistent with existing test_api.py patterns
- [ ] Verify: `ruff check app/pipeline/api_export.py app/pipeline/api_walks.py app/pipeline/api.py` clean

### Phase 6: Download rewrite (FileResponse-404) and job-status endpoint mapping
- [ ] TDD RED: create tests/pipeline/test_export.py covering GET /download/{job_id}: present-file 200 (audio/mp4 when .m4b exists), zip fallback (no m4b, zips wav/mp3/m4a/flac), unknown job 404, and the new FileResponse-404 subclass behavior for missing artifact with known job
- [ ] Implement: GET /download/{job_id} reads the render_job row; serves output_artifact_path via a FileResponse-404 subclass (returns 404 with detail when the file is missing instead of a broken 200); keep the zip fallback; use rows, not the in-memory dict
- [ ] TDD GREEN: `pytest tests/pipeline/test_export.py -q --cov=app/pipeline --cov-report=term-missing` all pass (closes the pre-existing zero-test gap on download)
- [ ] Verify: `ruff check app/pipeline/api_export.py tests/pipeline/test_export.py` clean; full `pytest tests/pipeline -q` green (≥ 768 baseline + new)

### Phase 7: Walk-side retry on ConcurrentTransactionError
- [ ] TDD RED: write a test simulating a walk unit whose idempotent write raises ConcurrentTransactionError (non-owner thread) and asserting the walk retries the write with 50-100ms backoff up to 3 times before failing the unit
- [ ] Implement the retry wrapper at the walk-unit write boundary in walks/runner.py (or the shared walk helper used by 2a-2i) — applies only to the idempotent write phase, never re-invokes the LLM call; after 3 retries, fail the unit and record the error
- [ ] TDD GREEN: run the retry tests; assert backoff timestamps are 50-100ms apart and the write eventually succeeds on retry 2
- [ ] Verify: `ruff check app/pipeline/walks/runner.py` clean; `pytest tests/pipeline/test_runner.py -q` green

### Phase 8: Regression gate, guard suite and verification
- [ ] Run `pytest tests/pipeline -q --cov=app/pipeline --cov-report=term-missing` and record pass count + coverage (baseline 768 / 89%; runner and api_export coverage must rise)
- [ ] Run `pytest tests/pipeline/test_legacy_removed.py -q` and verify 12/12 still green (no legacy endpoint resurrection; new /api/pipeline/export/* and /api/pipeline/walks/* paths are guard-legal per pre-verified simulation)
- [ ] Security review: confirm all new queries are parameterized (no f-string SQL), download path resolution cannot escape the run directory (path traversal check), cancel endpoints require a valid job/run id (404 not 500 on unknown); document findings
- [ ] Code review pass via exec-manager QA-Reviewer; fix MINOR findings; escalate PLANNING_GAP items if any
- [ ] Commit: `feat(pipeline): persist render/walk jobs to SQLite with reconciliation and cancel`

## Completion Criteria

- walk_run, render_job, render_chunk rows are written and read by runner/api_export; rows = truth for status and progress
- `is_cancel_requested(run_id)` is the single cancel dispatcher; cancel_walks/cancel_render persist their intent
- `reconcile_stale_runs()` flips stale running→interrupted once at startup
- Endpoints: render_status per-chunk counts, export/jobs/{job_id}, export/jobs/{job_id}/chunks, walks/{book_id}/runs implemented and tested; download rewritten with FileResponse-404 and full test_export.py coverage
- Walk-side retry on ConcurrentTransactionError with 50-100ms backoff ×3
- Smoke harness re-established and green; full pytest suite green; guard 12/12; ruff clean

## References

- `artifacts/designs/pending/DD-universal-upgrade.md` — P1 A1-r phase, FP1/FP2, workflow (Render/Walk), decisions #1/#2/#3/#4
- `artifacts/designs/parts/universal-upgrade/CONTRACTS.md` — walk_run/render_job lifecycle, is_cancel_requested, 4 endpoints
- `artifacts/designs/parts/universal-upgrade/README.md` — Plan B row
- Prior: `TASK-universal-upgrade-A-schema-transaction-foundation.md` (completed)
