# Task: Artifact-First Run Directories, GC & Export Backend

## Problem Statement

Today rendered audio lands in `tempfile.mkdtemp(prefix=f'audiobook_{book_id}_')` (tts_integration.py:287) — ephemeral, process-scoped, gone on restart. There is no RENDER_ROOT, no durable manifest, no cleanup (the in-memory `_render_jobs` dict was never evicted), no garbage collection, and no way to stream a single chunk or the whole book back to the browser. Export is a single `ffmpeg -f concat` call (api_export.py:335-346) with no metadata, no cover, no chapter markers, no MP3, no Audacity support, and zero FFMETADATA1 usage anywhere.

This plan (DD A2-r Artifact-First Run Directory with fsync Discipline, rows=truth/manifest=derived, tombstoning GC) makes renders durable under `RENDER_ROOT/book-{id}/{job_id}/`, gives each chunk a crash-safe fsync write, rebuilds manifest.json as a derived cache at startup, garbage-collects artifacts ≥ 7 days old (hourly, env-tunable, tombstoning rows + files in one sweep), and exposes the audio/chunk/export endpoints: bounded-range WAV streaming (`/export/chunk/{job_id}/{idx}`), whole-book playback (`/export/audio/{job_id}`), and the 3-phase FFMETADATA1 M4B export (`/export/m4b`) with MP3/Audacity where supported.

## Dependencies

- Plan A (transaction(), tables) and Plan B (render_job/render_chunk rows, endpoints, reconciliation) — completed and archived
- `artifacts/designs/parts/epub-audiobook-pipeline-rewrite/CONTRACTS.md` § Universal Upgrade — export/chunk/audio/m4b endpoint registration

## Phases

### Phase 1: RENDER_ROOT run directories + fsync discipline
- [ ] TDD RED: write tests in tests/pipeline/test_tts_integration.py asserting render_audiobook writes chunk WAVs under `RENDER_ROOT/book-{id}/{job_id}/chunk_{i:04d}.wav`, that each chunk file is written tmp→fsync→rename→fsync-parent before the render_chunk row is marked done, and that a crash between rename and row-update leaves a row status that reconciliation will catch (file exists, row pending)
- [ ] Implement: add `RENDER_ROOT` resolution (env `RENDER_ROOT`, default under `data/`); render_audiobook uses `RENDER_ROOT/book-{id}/{job_id}/` instead of mkdtemp; write chunks with the 2-fsync discipline (fsync file after write, rename into place, fsync parent dir)
- [ ] Implement: after render completes, write `manifest.json` in the run dir (job_id, book_id, mode, chunk count, per-chunk wav paths, status) — manifest is derived, never the authority
- [ ] TDD GREEN: run tts_integration tests with a file-backed tmp RENDER_ROOT; assert paths, fsync order (via mocked os.fsync call sequence), and manifest content
- [ ] Verify: `ruff check app/pipeline/tts_integration.py` clean; `pytest tests/pipeline/test_tts_integration.py -q` green

### Phase 2: Manifest rebuild at startup (manifest = derived)
- [ ] TDD RED: write tests asserting that after a simulated restart (new adapter instance over the same file-backed DB + RENDER_ROOT), a startup pass rebuilds manifest.json for every completed render_job that has a run dir but a missing/stale manifest; manifest regeneration is driven by rows, never by scanning the filesystem as authority
- [ ] Implement: extend the startup bootstrap (same reconciliation pass from Plan B) to rebuild manifests for completed render_job rows; if a render_job row is completed but its run dir is gone, mark the job status accordingly (artifact missing)
- [ ] TDD GREEN: run the manifest rebuild tests; assert a job with missing dir is flagged and a job with dir+stale manifest gets a fresh manifest
- [ ] Verify: `ruff check app/pipeline/adapter.py app/pipeline/tts_integration.py` clean

### Phase 3: Tombstoning GC (≥ 7 days, hourly, rows + files in one sweep)
- [ ] TDD RED: write tests for the GC sweep: a completed render_job older than retention has its run dir deleted and its render_chunk rows tombstoned (status evicted) + render_job row marked expired in the same sweep; jobs younger than retention are untouched; a project_snapshot that references artifacts in the run dir keeps the referenced artifacts in the eligibility union (snapshot refs prevent GC); rows are never time-deleted (tombstoned only)
- [ ] Implement the GC in an existing module (no new module per DD architectural rule — put the sweep in app/pipeline/adapter.py or api_export.py): env-tunable `job_retention_days` / `chunk_retention_days` (defaults ≥ 7 days), hourly schedule (background thread or APScheduler-style loop), never on the hot request path
- [ ] Implement the eligibility union: project_snapshot manifest artifact refs are collected before deletion so snapshots that reference a run dir keep it alive
- [ ] TDD GREEN: run GC tests with a file-backed fixture and a short retention override; assert files deleted, rows tombstoned/expired, snapshot-referenced artifacts survive
- [ ] Verify: `ruff check` on the GC module clean; `pytest tests/pipeline -q` green

### Phase 4: Bounded-range chunk streaming endpoint
- [ ] TDD RED: extend tests/pipeline/test_export.py with GET /export/chunk/{job_id}/{idx}: 200 with audio/wav and Content-Range for a valid byte range, 206 + Content-Range for a partial range, 416 + Content-Range: bytes */N for a malformed range, 404 for unknown job/idx, 409/410 for evicted chunks (GC tombstone)
- [ ] Implement GET /export/chunk/{job_id}/{idx} in api_export.py: resolve the render_chunk row → wav_path under RENDER_ROOT; serve with bounded-range WAV semantics (parse Range header, seek the file, stream the slice); guard against path traversal (resolve within the run dir)
- [ ] TDD GREEN: run the range tests; assert 200/206/416/404/410 status codes and correct Content-Range headers
- [ ] Verify: `ruff check app/pipeline/api_export.py tests/pipeline/test_export.py` clean

### Phase 5: Whole-book audio endpoint
- [ ] TDD RED: write tests for GET /export/audio/{job_id}: returns the assembled whole-book audio (the job's output artifact, e.g. audiobook.m4b or concatenated WAV) with correct media type; 404 for unknown job; 410 for expired job; supports Range (for seek in the browser audio element)
- [ ] Implement GET /export/audio/{job_id} in api_export.py: read render_job.output_artifact_path (or assemble from chunks for individual mode), stream with Range support, correct Content-Type per artifact extension
- [ ] TDD GREEN: run the audio endpoint tests; assert media types and range behavior
- [ ] Verify: `ruff check app/pipeline/api_export.py` clean

### Phase 6: 3-phase FFMETADATA1 M4B export (concat → metadata → mux)
- [ ] TDD RED: write tests for POST /export/m4b with a 2-chunk fixture: phase 1 concat (existing ffmpeg concat), phase 2 FFMETADATA1 generation (validate TIMEBASE, integer ms, chapter END clamped to duration), phase 3 mux with cover art and title/author/narrator/year/description tags; assert output is audio/mp4 and ffmetadata file content is CI-validated on the 2-chunk fixture
- [ ] Implement the FFMETADATA1 generator (in api_export.py or a helper within an existing module): auto chapter markers from a single ffprobe pass, END clamped to actual duration; embed cover art when provided; MP3 output where libmp3lame is available (feature-detect) and Audacity ZIP_STORED export where supported
- [ ] TDD GREEN: run the m4b export tests with the 2-chunk fixture; assert ffmetadata correctness (TIMEBASE format, integer ms, chapter END clamp) and final artifact media type
- [ ] Verify: `ruff check app/pipeline/api_export.py tests/pipeline/test_export.py` clean; guard suite 12/12 still green (no legacy /api/merge_m4b or /api/export_audacity routes resurrected — new paths are /api/pipeline/export/*)

### Phase 7: Regression gate, guard suite and verification
- [ ] Run `pytest tests/pipeline -q --cov=app/pipeline --cov-report=term-missing` and record pass count + coverage; api_export coverage must rise from 76% baseline
- [ ] Run `pytest tests/pipeline/test_legacy_removed.py -q` and verify 12/12 still green
- [ ] Security review: path traversal on RENDER_ROOT resolution (all new endpoints resolve paths within the run dir; reject `..`), Range header DoS (starlette≥0.49.1 pin preserved; malformed ranges → 416 not 500), ffmpeg argument injection (no user-controlled shell; subprocess list-args only), cover upload size/type limits; document findings
- [ ] Code review pass via exec-manager QA-Reviewer; fix MINOR findings
- [ ] Commit: `feat(pipeline): durable render artifacts, tombstoning GC and export backend`

## Completion Criteria

- All renders write under RENDER_ROOT/book-{id}/{job_id}/ with the 2-fsync discipline; render_chunk rows marked done only after WAV exists
- manifest.json rebuilt from rows at startup (derived, never authority); completed jobs with missing dirs flagged
- Hourly GC sweep: ≥ 7 day retention (env-tunable), tombstone rows + delete files in one sweep, snapshot-referenced artifacts in the eligibility union, rows never time-deleted
- GET /export/chunk/{job_id}/{idx} (bounded range, 206/416), GET /export/audio/{job_id} (whole-book, range-capable), POST /export/m4b (3-phase FFMETADATA1, chapters, cover, MP3/Audacity where supported) all implemented and tested
- Full pytest suite green, api_export coverage up, guard 12/12, ruff clean, security review documented

## References

- `artifacts/designs/pending/DD-universal-upgrade.md` — P2 A2-r phase, FP2/FP6, workflows (Render, Export), decision #10 (GC), open item #1 (retention defaults)
- `artifacts/designs/parts/universal-upgrade/CONTRACTS.md` — render_chunk done-only-after-fsync, GC rule, 3 export endpoints
- `artifacts/designs/parts/universal-upgrade/README.md` — Plan C row
- Prior: `TASK-universal-upgrade-B-render-walk-persistence.md` (completed)
