# Adversarial Design Log: Universal Upgrade (Pipeline-Native Utility Restoration)

*This file records the full adversarial refinement process for the Alexandria audiobook "universal-upgrade" design: restoring pre-rewrite utility as pipeline-native capabilities in the walk-based pipeline, preserving the pipeline-only architecture.*

*The design document (DD-universal-upgrade.md) contains distilled decisions — this raw debate never appears in it.*

## Scope of This Fight

Eight capabilities must be designed as pipeline-native (per the Manager's brief):

1. **Result/audio surface** — in-app audiobook playback + per-span/per-chunk preview while editing.
2. **Real render progress** (completed/total, failures) + useful cancellation, incl. walk cancellation where practical.
3. **Polished audiobook export** — M4B metadata (title/author/narrator/year/description), cover, chapter markers, MP3/Audacity export where supported.
4. **Voice configuration editing** — style, alias mapping, ref audio/text, 5 types (custom/clone/builtin_lora/lora/design), narrator override, preview.
5. **Walk 2g/2h/2i low-confidence/review items surfaced in review UI.**
6. **Optional prompt + generation-parameter overrides** without duplicated legacy architecture.
7. **Saved script/project workflow, single-speaker mode (if useful), contextual review, undo, pause-after, sequence playback.**
8. **Pipeline-only architecture preserved** — no legacy endpoints/modules/ProjectManager/toggles/shims.

Organizing principle for all sections below: capability areas cross-cut the approach/pattern sections. Each agent must map its proposals/critiques/patterns to these 8 capability areas explicitly.

---
*Sections below are appended by design agents during adversarial refinement.*
---

## Proposed Approaches

*Turn 1 — RnD-Ideator (2026-08-06). Four architectural stances for restoring the 8 capabilities as pipeline-native. They differ on the load-bearing mechanism: (A1) job/artifact state as SQLite rows, (A2) run state as filesystem artifacts + manifest, (A3) review as a first-class materialized queue, (A4) config/project persistence as an additive overlay layer. Each is grounded in the current code: the in-memory `_render_jobs`/`WalkRunner._status`/`_cancelled` dicts (api_export.py, walks/runner.py), the discarded `execute()` return dicts of walks 2g/2h/2i, the junction-only ReviewManager, the round-trip-stripping `AppConfig` (llm+tts only), and the ffmpeg-concat-only merge.*

---

### Approach 1: Schema-Native Jobs & Artifacts ("DB is the truth")

- **Architecture:** Extend the existing SQLite-WAL schema (registered in CONTRACTS.md) with run-state tables, and route every long-running unit through them:
  - `render_job(job_id, book_id, status, use_batch, output_dir, total_chunks, completed_chunks, failed_chunks, error, cancel_requested)` — replaces the module-level `_render_jobs` dict in api_export.py.
  - `render_chunk(job_id, chunk_index, span_id, speaker, status, output_path)` — one row per chunk; the render loop upserts this row as each chunk lands (in **individual mode** only — see constraints check for the batch-mode limitation). `GET /render_status/{job_id}` becomes a `SELECT COUNT(*) WHERE status='completed'|'failed'` — real completed/total/failures, queryable and unit-testable with `InMemorySQLiteAdapter` (closes the L4 gap: `download/{job_id}` and `render_status` have zero tests).
  - `walk_run(book_id, walk_name, status, total, processed, for_review, error, cancel_requested)` — `WalkRunner` writes this row on start and updates per unit; the walk `execute()` return dicts (currently discarded) are persisted. `POST /cancel_walks` becomes `UPDATE walk_run SET cancel_requested=1`, checked inside the serial walk loop, not only between walks.
  - `audio_artifact(job_id, chunk_index, path, mime)` or reuse `render_chunk.output_path` — powers capability 1 via new `GET /audio/{job_id}/chunk/{index}` (FileResponse with Range support) and the Result-tab player.
  - Review (capability 5): a small `walk_review(item_id, book_id, kind, payload_json, confidence, status, walk_name)` bridge table that walks 2g/2h/2i populate from their `for_review` counters; `GET /review/{book_id}` unions it with the junction queue so `voice_profile`/`description`/`instruct` items surface in the existing review UI (editor-pipeline.ts `pipelineReviewItems` needs only a `kind` filter).
  - Saved projects (capability 7): `project_snapshot(name, book_id, snapshot_json)` — snapshots store *state*, not audio (the Ardour/Mixbus pattern), so restore is a transactionally-scoped delete+reinsert using the caller's connection, and undo = "restore previous snapshot" (Serial Studio pattern).
  - Config (capability 6) is fixed separately but additively: preserve unknown keys on `POST /api/config` (see A4 mechanics) and add a `walk_override(book_id, walk_name, prompt, temperature, top_p, top_k, min_p, banned_tokens)` table that `resolve_task_llm` actually reads — making the currently-dead `config` request param live.
- **Capability coverage:**
  1. **Handled** — `render_chunk.output_path` + a range-serving audio endpoint gives per-span/per-chunk preview and full-book playback.
  2. **Handled** — per-chunk completed/total/failures from DB rows; cancel is a `cancel_requested` row checked mid-walk and pre-chunk (individual mode).
  3. **Handled** — export consumes the job's chunk table + `chapter` edges (book_chapter → global_index ordering) to rebuild the legacy 5-phase merge: FFMETADATA1 (title/artist/album_artist/date/comment), `[CHAPTER]` blocks, cover as attached_pic, `-movflags +faststart`; per-speaker WAV ZIP + MP3 from `render_chunk` grouped by speaker.
  4. **Partial** — CRUD/preview already exist in api_voices; this approach adds nothing new except surfacing the `NARRATOR` voice_config row as an editable narrator override in the Voices tab.
  5. **Handled** — `walk_review` bridge + union query surfaces 2g/2h/2i items with real `walk_name` (replaces the `source LIKE %walk_name%` heuristic).
  6. **Partial** — unknown-key-preserving config round-trip + `walk_override` table wired into `resolve_task_llm`; prompts/params are per-walk rows, not a duplicated legacy prompt module.
  7. **Handled** — `project_snapshot` for save/load/delete; snapshot-restore as undo; sequence playback falls out of `render_chunk` ordered by `global_index`; `pause_after` is a nullable INTEGER ms column on `span` (schema addition); contextual review via `span_presentation` global_index ±2 joined into `walk_review.payload`.
  8. **Handled** — all new tables/endpoints are pipeline-side; zero legacy endpoints touched (29 remain 404; `test_legacy_removed.py` stays green).
- **Production evidence:**
  - Celery database result backend — https://docs.celeryq.dev/en/stable/internals/reference/celery.backends.database.html — the canonical production pattern for job state (PENDING/STARTED/SUCCESS/FAILURE + result) persisted as rows so any process can poll it; our `render_status`/`walk_status` become the same queryable-job-state pattern without Celery's broker.
  - Bugsink / Snappea — https://www.bugsink.com/blog/snappea-design/ — a production error-tracking product runs its entire background-task queue on SQLite tables in WAL mode on one host, "we don't need the complexity of a full-fledged message broker" — the same single-host constraint profile as Alexandria, validating SQLite rows as the job/progress store.
  - "Keep the queue in the same database as your business writes" (Oban/pg-boss, Honker) — https://philosophersstone.ee/knowledge/keep-the-queue-in-the-same-database-as-your-business-writes — progress/cancel rows in the same DB as the data they mutate are transactionally consistent with walk/span writes, eliminating the dual-write class of bugs (this is the core argument for `walk_run`/`render_chunk` living in pipeline.db).

---

### Approach 2: Artifact-First Run Directory ("the filesystem is the truth for audio & runs")

- **Architecture:** Every render (and optionally each walk) produces a self-contained **run directory** instead of DB rows for progress/cancel:
  ```
  data/runs/{book_id}/{run_id}/
    manifest.json            # atomic tmp+rename; {status, total, completed, failed, chunk→file, chapter→chunk}
    walk/{walk_name}.jsonl   # append-only per-unit records (never rewritten; cursor recovery)
    run.stop-request.json    # cancel sentinel — existence = cancel requested
    chunk_0000.wav …         # per-chunk audio (the playback + export inputs)
    audiobook.m4b            # final export
  ```
  - Progress = `manifest.json` + `os.listdir` count of chunk files — no schema change needed for render progress, no in-memory dicts, survives process death (resume reads manifest and the last JSONL record, rolls back on discrepancy).
  - Cancel = writing `run.stop-request.json`; the render loop and walk loops check for it before each unit (the "cancel is a file" pattern).
  - Audio surface (capability 1): serve the run directory with HTTP range requests (`Accept-Ranges: bytes`, 206 partial) — a static-mount or thin endpoint over the artifacts tree; per-span preview is just a URL to `chunk_NNNN.wav`, sequence playback is ordered chunk URLs.
  - Export (capability 3): deterministic consumers of the directory — ffmpeg concat + FFMETADATA1 + `[CHAPTER]` + cover for M4B (m4b-tool/audiobookConverter pattern), per-speaker WAV ZIP for Audacity, MP3 conversion.
  - Review/config/snapshots (capabilities 5/6/7) still need the DB (junctions, metadata, overrides) — so this is a hybrid: *runs and audio live on disk; review and config live in SQLite*. The distinctive move is that the noisy, high-frequency, crash-prone state (per-chunk progress, cancellation) never touches the DB, so `isolation_level=None` and WAL write contention are untouched during renders.
- **Capability coverage:**
  1. **Handled** — run dir is a media tree; range-request serving gives seekable in-app playback and per-chunk preview with no DB reads.
  2. **Handled** — manifest + file count is real completed/total/failures; stop-request file gives mid-walk/mid-render cancellation; state survives restarts (manifest is the recovery point).
  3. **Handled** — exports are pure consumers of the run dir; chapter mapping comes from `book_chapter` edges joined to chunk order at manifest-write time.
  4. **Partial** — unchanged from today (api_voices CRUD/preview + UI); no architectural contribution.
  5. **Partial** — walk 2g/2h/2i for_review records land in `walk/{name}.jsonl`, but surfacing them in the review UI still needs a small DB bridge table (JSONL is not queryable by the existing `GET /review` path).
  6. **Partial** — per-run `manifest.json` can carry the generation-parameter snapshot used for that render (reproducibility), but prompt/LLM overrides still need the `walk_override`-style table for `resolve_task_llm`.
  7. **Partial** — sequence playback and pause-after work naturally (manifest has per-chunk order + pause_after), but saved scripts/undo need the snapshot table; contextual review needs span_presentation joins (DB).
  8. **Handled** — run dirs are new pipeline-owned artifacts; no legacy symbols or shims.
- **Production evidence:**
  - bijux DAG run directory — https://github.com/bijux/bijux-core/blob/main/crates/bijux-dag-artifacts/docs/RUN_DIRECTORY.md — a production DAG engine whose run is "the durable evidence boundary": `manifest.json`, `run.stop-request.json`, per-node outputs, atomic tmp+rename records — cancel-as-file and manifest-as-progress, exactly the proposed shape.
  - Hadoop Manifest Committer — https://hadoop.apache.org/docs/stable/hadoop-mapreduce-client/hadoop-mapreduce-client-core/manifest_committer_architecture.html — MapReduce jobs commit via an atomically-written manifest (tmp+rename) plus a JSON `_SUCCESS` marker; commit/progress state lives in files so restarts resume from the manifest — the same "file, not row, is the commit record" argument.
  - Hep.gg stream API — https://docs.hep.gg/docs/music/stream-and-artwork — a production music API serving audio inline with `Accept-Ranges: bytes` and 206 partial content for seekable playback straight from stored files; per-chunk preview is this same mechanism at chunk granularity.
  - m4b-tool — https://github.com/sandreas/m4b-tool — the mature production FFMETADATA1 + chapters + cover + description M4B assembly pattern, consuming a directory of tagged audio files (our run dir), including iPod-safe chapter limits and `-movflags +faststart`-class polish.

---

### Approach 3: Unified Review-Queue Architecture ("review is a first-class table")

- **Architecture:** Make the review queue a materialized, kind-aware table instead of a derived query over junctions:
  ```
  review_item(item_id, book_id, kind ∈ {junction, voice_profile, description, instruct},
              entity_id, character_id, payload_json, confidence REAL 0-1,
              status ∈ {pending, accepted, rejected}, walk_name, human_override)
  ```
  - Walks 2g/2h/2i persist their `for_review` output here directly (voice_profile/description/instruct items); the existing junction review items are materialized here too (or the endpoint unions the four junction tables with this table). One `GET /review/{book_id}?kind=&walk_name=` returns everything; the `source LIKE %walk_name%` heuristic dies.
  - Semantics preserved exactly: `>=0.7` auto-accept, `<0.5` auto-reject, `0.5–0.7` → `pending`; **no** degraded-confidence auto-accept multiplier is reintroduced (v1's ×0.8 stays dead). This is Label Studio's "confidence-aware routing" verbatim.
  - Contextual review (capability 7): `payload_json` for `instruct`/`junction` items embeds the ±2 neighbor spans via `span_presentation.global_index` (the prior precedent), so the review card shows context without extra queries.
  - Undo: `review_item.status` transitions are journaled (append-only `review_audit` rows) — undo = revert the last transition; batch reject is undoable.
  - Progress/export/config are handled conventionally: minimal `render_job` row (A1's table, not the full chunk table) for completed/total + failures; export as in A1/A2; config fix as in A4. The architectural bet is that *review is the pivot capability* — the review queue becomes the place where 2g/2h/2i results, junction confidence, and contextual preview converge, and every other capability hangs off it (play preview from a review card, play-sequence from review context, etc.).
- **Capability coverage:**
  1. **Partial** — preview URLs ride on a thin artifact/audio endpoint (borrowed from A1/A2); not the focus.
  2. **Partial** — coarse render job row gives completed/total/failures + cancel flag; no per-chunk granularity in this approach.
  3. **Partial** — export needs the standard ffmpeg work; nothing special here.
  4. **Partial** — unchanged (api_voices + UI); a review action can set `voice_assignment_id` from an audition item, which is a nice 2g→2h loop but not the whole story.
  5. **Handled** — this is the whole point: 2g/2h/2i items are first-class rows in the same queue as junctions, with `kind` and `walk_name` filters.
  6. **Partial** — config overrides via the A4 overlay; orthogonal.
  7. **Handled** — contextual review (neighbor spans in payload), undo (journaled transitions), sequence playback (review items carry ordering), pause-after (payload field); saved scripts need the snapshot table (partial).
  8. **Handled** — all pipeline-native; no legacy endpoints.
- **Production evidence:**
  - Label Studio Enterprise QA — https://humansignal.com/platform/quality-assurance/ — production review workflows built on "an explicit accept, fix, or reject decision on every example" and queues "ordered by … model confidence so expert attention lands on the uncertain, high-stakes cases" — the same accept/reject semantics + confidence-ordered queue we already have, generalized from junctions to metadata/instruction items.
  - Label Studio Enterprise confidence-aware routing — https://humansignal.com/goenterprise/ — "Confidence-aware routing: automatically send low-confidence or high-disagreement predictions to human review and accept high-confidence ones over a threshold" — the exact 0.5–0.7/≥0.7 threshold architecture, production-proven for exactly the 2g/2h/2i low-confidence case.
  - Label Studio active learning / uncertainty sampling — https://docs.humansignal.com/guide/active_learning — "annotators focus on labeling the tasks with the least confident, or most uncertain, prediction scores" — validating that a single unified queue ordered by confidence is the right surface for mixed-kind review items.

---

### Approach 4: Overlay Config + Snapshot Projects ("config & persistence as an additive layer")

- **Architecture:** Fix capability 6 and 7 at the persistence layer without touching the walk pipeline's shape:
  - **Config round-trip (capability 6):** `GET/POST /api/config` stops round-tripping `AppConfig` only. On write, read the existing `config.json` as raw JSON, `model_dump()` the known `AppConfig` fields over it, and re-emit unknown top-level keys verbatim (an `KNOWN_TOP_LEVEL_KEYS`-style allowlist + append-unknown pass — the Lisa/Rackula pattern). Generation/prompts keys survive instead of being stripped. Then add a `walk_override(book_id, walk_name, prompt, temperature, top_p, top_k, min_p, banned_tokens)` table (or a `overrides_json` column) that `resolve_task_llm` and the walk prompt-assembly code actually read — the currently-dead `config` request param becomes a real per-walk override path, with no duplicated legacy prompt modules.
  - **Saved scripts/projects (capability 7):** `project_snapshot(name, book_id, snapshot_json, created_ms)` — a named, listable snapshot of pipeline state: book spine + junction rows (source/confidence/human_override), voice_config rows + assignments, `walk_override` rows, config overlay. Snapshot files share audio data (Ardour/Mixbus pattern) — restoring does **not** copy WAVs. Load = transactional delete+reinsert on the caller's connection (reuses the reonboard machinery's `_clear_*` helpers as the restore teardown). Endpoints land in api_operations as `/api/pipeline/projects` — never `/api/scripts/*` (legacy 404 gate).
  - **Undo:** auto-snapshot before destructive ops (delete span, re-onboard, batch reject); restore is itself reversible (snapshot before restore — Serial Studio pattern).
  - **Single-speaker mode:** a book-level flag column (`book.single_speaker`) or config overlay key; assembly maps every speaker to NARRATOR at the `export_annotated_script` boundary — a one-line assembly change, no legacy mode machinery.
  - **Pause-after:** nullable `span.pause_after INTEGER` column consumed by the export/timeline step.
  - Progress/audio/export/review ride on the A1 mechanisms (job row, artifact paths, walk_review bridge) — this approach's distinct claim is that the *configuration and project-state layer* is where the lost utility should be rebuilt, because that is where the actual data-loss bugs (L4: config round-trip stripping; dead config param) live today.
- **Capability coverage:**
  1. **Partial** — depends on A1/A2 audio serving; nothing here.
  2. **Partial** — depends on A1's job row for progress/cancel.
  3. **Partial** — export is standard ffmpeg work; snapshots make re-export deterministic.
  4. **Handled** — narrator override is a first-class overlay key; voice config edits persist through the unknown-key-preserving round-trip (no more stripped fields); alias mapping rides on `voice_config.alias_of` + overlay.
  5. **Partial** — review items still need the A3/A1 bridge table; snapshots do capture review status though.
  6. **Handled** — the core fix: unknown-key-preserving config + live `walk_override` consumed by `resolve_task_llm`; prompts/params are data rows, not a duplicated legacy architecture.
  7. **Handled** — named project snapshots (save/load/delete/list), snapshot-based undo, single-speaker flag, pause-after column; contextual review and sequence playback remain partial (need A1/A3).
  8. **Handled** — additive schema + new `projects` namespace; zero legacy symbols; `test_legacy_removed.py` untouched.
- **Production evidence:**
  - Lisa `.lisa.config.json` round-trip — https://github.com/CodySwannGT/lisa/blob/main/src/core/project-config.ts — "Additional fields may be added in future versions; unknown fields are preserved on round-trip" — the exact fix for the `AppConfig` stripping bug, with merge-not-replace write semantics.
  - Rackula additive config versioning PR — https://github.com/RackulaLives/Rackula/pull/2210 — a production schema-versioning policy that is "load-bearing data-safety": a `KNOWN_TOP_LEVEL_KEYS` allowlist + `appendUnknownSections` so an older build cannot silently drop a newer build's additive sections on resave — precisely our "old AppConfig would strip generation/prompts" hazard.
  - Skir schema evolution docs — https://skir.build/docs/schema-evolution — "Preserve: unrecognized data is kept internally and written back during serialization. This enables 'round-tripping'" — the general additive-schema principle behind keeping generation/prompts keys alive.
  - Ardour session snapshots — https://manual.ardour.org/working-with-sessions/snapshots/ — a professional DAW's saved-project model: snapshots are small state files that "share all data present in the session" (no audio duplication) and can be switched/loaded independently — the same shape as `project_snapshot`.
  - Serial Studio backup/recovery — https://serial-studio.com/help/backup-recovery — rolling whole-project snapshots as the undo primitive ("every destructive command … returns the path of the snapshot taken beforehand"; restore is reversible) — the model for snapshot-based undo in the Editor tab.

---

### Cross-Cutting Constraints Check

- **tts.py untouchability is the binding constraint on capabilities 1–2.** `generate_batch()` is one blocking call with no per-chunk callback and no mid-batch cancellation, so *true* per-chunk progress and mid-batch cancel are only achievable in the individual `generate_voice` loop (which `render_audiobook` already supports with `cancel_check` before each chunk). A1 and A2 must therefore state a render-mode policy: batch mode = coarse progress (queued→completed, one cancel point pre-dispatch), individual mode = per-chunk progress + cancellation at the cost of batch throughput. A3/A4 inherit this; any approach claiming per-chunk progress inside batch mode would require modifying tts.py and is DOA.
- **Walk seriality and review semantics must survive.** 2g/2i stay strictly serial in all four approaches (cancel checks are *inside* the serial loop, never a parallelization excuse). All approaches preserve the ≥0.7/auto-accept, <0.5/auto-reject, 0.5–0.7/review thresholds and do **not** reintroduce v1's ×0.8 degraded-confidence auto-accept — A3's materialized queue must enforce this in the insert path, not the display path.
- **Schema/API gate:** A1's and A3's tables (render_job/render_chunk/walk_run/walk_review/review_item) and A4's (walk_override/project_snapshot) must be registered in CONTRACTS.md and follow the SQLite mechanics (caller's connection for `complete_walk()`/snapshot restore, `isolation_level=None`, INTEGER unix-ms timestamps where needed). New endpoints go in the right api_* module (api_export for render/audio/download, api_walks for walk_run, api_review for walk_review, api_operations for projects) — never new `/api/scripts/*`, `/api/chunks/*`, `/api/audiobook*` names (the 29 legacy routes must stay 404 and `test_legacy_removed.py` green). A2's run directories add no endpoints except a range-serving static mount, but stale-artifact GC must be defined (L5 open question).
- **Honest divergence:** A1 and A2 both cover capabilities 1–3 well but are mutually exclusive on the progress/cancel store (DB rows vs manifest files) — picking A2 means `render_status` reads the filesystem and tests use tmp dirs instead of `InMemorySQLiteAdapter`; picking A1 means the WAL writer is hot during renders (fine at this scale, but a real tradeoff). A3 is the strongest for capability 5 and context/undo, weakest for 1–3. A4 is the only approach that directly fixes the *known data-loss bugs* (capability 6/7), but it is the weakest standalone — it must be paired with A1 or A2 for the audio surface. No approach risks reintroducing ProjectManager, legacy toggles, or compatibility shims; all comply with pipeline-only.

---

## Critique

*Turn 2 — Counter-Ideator. Evidence gathered via web research, Aug 2026. Every criticism cites a followable source; tier is stated per source. Nothing below is fabricated; where a concern is structural rather than evidence-backed, it is labeled honestly.*

### Verdict Summary

| Approach | Verdict | Load-bearing reason |
|----------|---------|---------------------|
| A1 Schema-Native Jobs & Artifacts | **SURVIVES WITH CHANGES** | DB-as-job-state is sound at single-host scale (the "single-writer contention" objection largely does **not** apply here), but the design has no stale-run reconciliation: a crash leaves `render_job`/`walk_run` rows in `running` forever, and the DB row outlives the work (tts.py cannot resume). Needs heartbeat/sweeper + retention policy. |
| A2 Artifact-First Run Directory | **SURVIVES WITH CHANGES** | Run-directory pattern is viable, but "atomic tmp+rename" for manifest.json is the documented crash-corruption pitfall (needs fsync discipline + generations); progress-by-`os.listdir` counts in-flight files as complete (the exact reason Hadoop's v2 committer was declared unsafe); Range-request serving is not a solved default in this stack. |
| A3 Unified Review-Queue | **SURVIVES WITH CHANGES** | Materializing junction items creates a dual-write drift risk (junction rows are the truth today; the materialized copy can diverge); requires transactional dual-write or an honest union, plus supersede-on-rewalk semantics or the queue accumulates ghost items. |
| A4 Overlay Config + Snapshot Projects | **SURVIVES WITH CHANGES** | Config-overlay is the right fix for the known data-loss bugs, but top-level-only unknown-key preservation is the **documented** failure mode — must be recursive + schema-version-stamped. Snapshot restore needs shared-character and audio-durability semantics. Confirmed weakest standalone (matches the log's own cross-cutting assessment). |

**None of the four is DEAD.** Each has a viable core and each carries at least one change that must land before implementation. The most critical unresolved concern across all four: **stale-state reconciliation** — every approach that persists job/review state beyond process lifetime (A1 rows, A2 manifests, A3 review_item, A4 snapshots) needs an explicit answer for "what happens when the process dies mid-operation," and none of the four currently states one.

---

### A1: Schema-Native Jobs & Artifacts

**A1-1. Stale `running` rows with no reconciliation — a crash makes the UI lie permanently (HIGH)**
- **Criticism:** A1's selling point is that job state survives process death. But the *work* does not survive process death — `generate_batch()`/`generate_voice()` cannot resume (tts.py untouchable). So after a crash the DB holds a `render_job`/`walk_run` row in `running` with no worker behind it, and `render_status`/`walk_status` will report "running" forever. Today's in-memory `_render_jobs`/`WalkRunner._status` at least reset on restart — the current design is self-healing in the worst case; A1 makes it *worse* by persisting a lie.
- **Evidence:** [T2] NovelFoundry queue postmortem — https://pbrazeale.github.io/posts/novelfoundry-queue-bug (Mar 2026): a job row stuck in `queued` with no worker behind it "locked the dashboard forever"; no backend reconciliation, no sweeper; the fix was authoritative state transitions + stale-state recovery + a periodic sweep, with the explicit lesson: *"if user-facing state depends on async workers, you need authoritative transitions, stale-state recovery… client behavior that notices corrected truth."* Corroborated by [T2/T4] TiDB #67611/#67633 — https://github.com/pingcap/tidb/issues/67611 — a materialized-view refresh row stuck in stale `running` after crash/restart ("There should not be a persistent running row when there is no active refresh executor anymore"), fixed by orphan GC.
- **Applies to Alexandria because:** single-host, single-process, but the background render/walk tasks are exactly the async-worker shape NovelFoundry describes, and Alexandria has no heartbeat or sweeper anywhere in its current in-memory design to port. The fix is cheap (status transition + `last_updated_ms` column + a startup or on-read reconciliation that flips `running` → `failed` for rows whose owning process is gone), but it must be specified. This is the single highest-severity gap in A1.

**A1-2. Row-insert timing defines correctness — `SELECT COUNT` can overcount (MEDIUM)**
- **Criticism:** "render_status = SELECT COUNT" is only correct if `render_chunk` rows are inserted/marked at *completion*, not at *start*. If a row is written when the chunk begins (or when `generate_voice` opens the output file), the count reports chunks as done while they are still being written — the same "visible before complete" bug A2 has, in DB form. If rows are written only on completion, progress lags by one chunk duration (fine) but the table's semantics must be stated.
- **Evidence:** [T3] Apache Hadoop Manifest Committer docs — https://hadoop.apache.org/docs/current/hadoop-mapreduce-client/hadoop-mapreduce-client-core/manifest_committer.html — the v2 FileOutputCommitter "is not considered safe because the output is visible when individual tasks commit, rather than being delayed until job commit"; a failed task attempt can leave partial output in the destination. The manifest committer exists precisely to make commit an explicit, validated step.
- **Applies to Alexandria because:** per-chunk progress is A1's headline capability; a COUNT over rows written at start-time over-reports progress to the user during the longest part of the pipeline. Cheap fix (status column per chunk; COUNT only `status='done'`), but it is a design detail that must be pinned before the schema lands.

**A1-3. Single-writer contention — does NOT bite here (LOW, honest filtering)**
- **Criticism:** The literature on SQLite write contention is extensive, but nearly all of it is about *multiple writer processes*: lock acquisition is unfair and throughput drops with more writers (gauravsarma: "max-wait varied 4.2x with 8 contending writers… throughput halved at 16"); holding a lock across blocking calls is the killer (emschwartz). Alexandria has one process, one connection (`check_same_thread=False`, WAL), serial walks, and a single user. The per-chunk inserts in individual mode are one statement per chunk — exactly the "small, single-statement transactions" the sources recommend. This concern does **not** transfer at this scale; the sources themselves say so (gauravsarma: "if workload doesn't push on these… single-machine deploy, no tight tail-latency SLO, SQLite is great").
- **Evidence:** [T2] https://gauravsarma.com/posts/2026-05-12_where-sqlite-gives-up ; [T2] https://emschwartz.me/psa-your-sqlite-connection-pool-might-be-ruining-your-write-performance ; [T3] https://www.sqlite.org/wal.html (single writer, multiple readers is the documented, supported shape).
- **Applies to Alexandria because:** *with two caveats that DO apply*: (1) the adapter sets **no `busy_timeout`** (app/pipeline/adapter.py line 82) — [T4/T2] https://hynek.me/til/sqlite-read-only-wal-locked shows WAL can briefly hold an exclusive lock on open/close; with a single long-lived connection this is a corner case, but `PRAGMA busy_timeout` is one line and should be added; (2) the render thread and API share one connection, so a long `generate_batch` in the same thread that owns the connection is fine, but the design must not spawn a second writer connection for the render loop (that reintroduces the upgrade-to-write `SQLITE_BUSY_SNAPSHOT` hazard, [T2] https://simonwillison.net/2025/Feb/17/sqlite-busy/).

**A1-4. Job/artifact tables grow without bound (MEDIUM)**
- **Criticism:** `render_chunk` rows accumulate per render; audio files accumulate on disk. A1 defines no retention policy. DB-as-queue guidance is unanimous that hot queue tables need pruning; NovelFoundry's "deployment support for periodic cleanup" was a first-class lesson, not an afterthought.
- **Evidence:** [T2] https://brandur.org/postgres-queues (queue tables under sustained writes degrade — dead tuples, lock time escalation; "a database-based job queue may be the least optimal situation"); [T2] https://shivekkhurana.com/blog/sqlite-in-production (unchecked WAL growth with PASSIVE checkpointing; monitor WAL >100MB).
- **Applies to Alexandria because:** a book re-rendered 10 times leaves 10×N chunk rows and 10 runs of WAV files. This is the log's own L5 open question (stale-artifact GC); A1 must answer it or the DB becomes the growth problem the current temp-dir design at least self-limits.

---

### A2: Artifact-First Run Directory

**A2-1. "Atomic tmp+rename" is not crash-safe without fsync discipline — the manifest is the single point of truth and the single point of failure (HIGH)**
- **Criticism:** A2's manifest.json "atomic tmp+rename" claim is exactly the pattern documented as *not* crash-atomic: on power loss, delayed allocation means the renamed directory entry can be journaled while the data blocks are still in page cache, leaving a zero-length or truncated manifest. The fix is three steps, the last of which is "easy to miss": write tmp, **fsync the file**, rename, **fsync the parent directory**. A2 must also fail closed on a manifest that won't parse, and ideally keep numbered generations + a CURRENT pointer so a corrupt write is recoverable.
- **Evidence:** [T1, academic] FERRITE (ASPLOS'16) — https://syslab.cs.washington.edu/papers/ferrite-asplos16.pdf — documents the 2009 ext4 data-loss incident where "pretty much any file written to by any application became empty after a system crash"; the tmp+rename pattern is NOT crash-atomic without fsync; POSIX permits partial writes. [T2] UsageBox internals postmortem — https://usagebox.com/articles/usagedb-internals-manifest-atomic-commit — "the final, easy-to-miss step is fsyncing the parent directory"; their earlier version had a P0 where an in-memory manifest ran ahead of the on-disk one, and they moved to clone-mutate-save-publish + numbered generations. [T4] https://github.com/google/leveldb/issues/195 — "the bad outcome was NOT a 0 length file… a file with a length reflecting the write, but which did not actually contain the data."
- **Applies to Alexandria because:** manifest.json is A2's recovery point for an entire run. A truncated manifest = the run's state is gone or the run is wrongly treated as incomplete. One documented crash window, and the whole run-directory bet collapses. This is the load-bearing change for A2: specify `fsync` + parent-dir `fsync` + generation pointer + fail-closed parse.

**A2-2. File presence ≠ completion — progress by `os.listdir` count over-reports (HIGH)**
- **Criticism:** A2 computes progress from "manifest + os.listdir count of chunk files." A chunk file exists the moment `generate_voice` opens it for writing, before the bytes are complete — so a file-count includes in-flight chunks as done, and a crashed partial file counts as complete. The merge step then trusts chunk filenames and happily concatenates a truncated WAV. Apache's own manifest committer history is the canonical case study: the v2 FileOutputCommitter was declared **unsafe** precisely because task output was visible in the destination before task commit.
- **Evidence:** [T3, first-party] Apache Hadoop Manifest Committer — https://hadoop.apache.org/docs/current/hadoop-mapreduce-client/hadoop-mapreduce-client-core/manifest_committer.html — "The v2 algorithm is not considered safe because the output is visible when individual tasks commit, rather than being delayed until job commit"; also "task commit robustness was insufficient to some failure conditions" was a *post-release* finding. The committer's whole design is a manifest of *intended* outputs committed explicitly, not a directory listing.
- **Applies to Alexandria because:** A2's hybrid uses both manifest and listdir — and they can disagree. The manifest (explicit per-chunk status, written on completion) is the correct instrument; `listdir` is the wrong one and should be dropped from the progress computation (or used only as a sanity check). This also fixes A1-2's timing question for free: one rule, "a chunk counts when the manifest says so," applies to both approaches.

**A2-3. Range-request serving is not a solved default in this stack (MEDIUM-HIGH)**
- **Criticism:** A2 proposes serving the run directory with HTTP range requests (a static mount or thin endpoint). In this exact stack, that is not free: Starlette's `FileResponse` did not support `Range` until 0.39.0 (breaking Safari/WebKit seeking); there is a fixed security advisory for O(n²) Range-header parsing; and there was a regression where FileResponse mutated headers on range requests, breaking subsequent calls. Alexandria's current `/download` path uses `FileResponse` for the full M4B and has never exercised range semantics. Also note the environment has drifted from `requirements.txt`: installed starlette is 1.3.1 (fine), but the pinned `fastapi==0.128.0` resolves to an older starlette whose patch level must be checked at build time.
- **Evidence:** [T4/T3] https://github.com/Kludex/starlette/issues/950 — "Safari is trying to make an HTTP range request in order to stream the video — but Starlette doesn't support that" (WebKit: "ERROR Server does not support seeking… Server does not accept Range HTTP header"); [T3] GHSA-7f5h-v6xp-fcq8 — "fixes a security vulnerability in the parsing logic of the Range header" (fixed in starlette 0.49.1); [T4] https://github.com/encode/starlette/pull/3144 — FileResponse mutates `self.headers` on range requests, breaking later calls.
- **Applies to Alexandria because:** capability 1 (per-span preview, seekable playback) is the whole point. The design must specify a dedicated range-serving endpoint with explicit, bounded Range parsing and a build-time version guard (starlette ≥ 0.49.1), not "mount the directory and hope StaticFiles does the right thing."

**A2-4. The walk JSONL mirror cannot roll back DB side effects — "filesystem is truth for runs" is false for walks (HIGH)**
- **Criticism:** Walks' real side effects are DB rows: `execute()` calls write junction rows (character_span, character_scene, …) through the storage adapter as the walk runs. A2's `walk/{name}.jsonl` is an append-only *mirror* of those side effects. If a walk crashes mid-way, the DB has partial rows and the JSONL has partial records; A2's "rolls back on discrepancy" is undefined — you cannot roll back already-committed junction rows from a filesystem log without re-implementing reonboard's `_clear_*` teardown in the runner. So for walks, the DB is the truth and the JSONL is a log; "filesystem is truth" holds only for audio bytes and render output.
- **Evidence:** [T2] Materialize, "Self-Correcting Materialized Views" — https://materialize.com/blog/self-correcting-materialized-views/ — output drift is a real, acknowledged hazard for any derived store: "without special handling, output drift can silently corrupt the persisted state." The dual-store hazard is structural; the walk code (app/pipeline/walks/runner.py + walk modules) confirms the DB is the side-effect sink. Labeled honestly: the rollback impossibility is a structural argument grounded in Alexandria's own code, supported by the drift literature for the general pattern.
- **Applies to Alexandria because:** A2's own hybrid admits review/config/snapshots live in the DB. The honest position is: **filesystem is truth for audio bytes and render runs; DB is truth for walk side effects and review; pick exactly one authority for progress** (manifest or DB rows — A1/A2 are mutually exclusive there, as the log's cross-cutting section already says, but the walk side is not a free choice: it is DB, structurally).

---

### A3: Unified Review-Queue

**A3-1. Materializing junction items creates a dual-write drift risk (HIGH)**
- **Criticism:** Today the junction rows ARE the truth: `ReviewManager.accept/reject` writes confidence 1.0/0.0 onto the junction rows (character_series/character_book/character_scene/character_span). If A3 materializes junction items into `review_item`, then accept/reject must write **both** the junction row and the review_item row. Any failure or missed code path between the two = the review UI shows pending items the junction says are accepted (or vice versa). The "or unioned" escape hatch dissolves A3's "first-class table" claim: a union is just A1's walk_review bridge, and the materialization buys nothing but a view.
- **Evidence:** [T3/T2] ClickHouse #70417 — https://github.com/ClickHouse/ClickHouse/issues/70417 — "ClickHouse does not guarantee consistency between a MatView and a source table… lack of transactions and lack of insert atomicity"; MV/source divergence observed (0–1% typical, 20–30% on a bad day) with no atomic propagation. [T2] Materialize self-correcting MV blog (above): drift is real enough that Materialize built diff-and-correct machinery, at real memory/CPU cost.
- **Applies to Alexandria because:** the fix is available and cheap *because* Alexandria is single-writer: junction write + review_item write must happen in **one transaction on the caller's connection** (the existing SQLite mechanics already support this — `execute_*` only commits when not already in a transaction). Without that commitment, A3 is the drift source. If the ideator refuses the dual-write, then A3 must honestly be "a view + walk-side persistence for 2g/2h/2i items only," i.e., A1's bridge.

**A3-2. Supersede-on-rewalk is undefined — the queue accumulates ghost items (MEDIUM-HIGH)**
- **Criticism:** Re-running 2g/2h/2i regenerates for_review items (new voice auditions, new confidences). If old `review_item` rows for the same entity are not marked superseded, the queue shows stale items the user already reviewed, from a walk run that has been replaced. This is queue-health rot: reviewed work silently re-enters the surface.
- **Evidence:** [T2] LabelOp review-queue best practices — https://labelop.com/blog/labelop-review-queue-best-practices-2026 — "Watch how long work waits before review, how often assignments come back rejected"; a queue that recycles stale work "becomes a recycling bin." [T3] Segments.ai label-queue mechanics — https://docs.segments.ai/background/label-queue-mechanics — real review queues model *rework as an explicit status transition* (rejected → back on label queue → re-reviewed), never as passive re-appearance. [T2/T4] TiDB #67611 (stale rows surviving restart) reinforces: state without explicit invalidation goes stale.
- **Applies to Alexandria because:** walk re-execution is explicitly allowed (explicit-trigger only), and the 2g/2i counters are the review surface's input. The design must specify: re-walk 2g for a book ⇒ mark prior voice_profile review_items for that book superseded in the same transaction that persists the new ones. This is the single most important operational detail for A3 and it is currently absent.

**A3-3. Audit-journal undo is the same dual-write class, and replay-based undo is fragile (MEDIUM)**
- **Criticism:** Undo via an append-only `review_audit` journal sounds clean, but it only works if the journal is authoritative — it isn't; the junction rows are. Replaying N entries to reconstruct state T is fragile when entries are conditional (accept-then-reject-then-accept) and when a re-walk (A3-2) has superseded the entities in between.
- **Evidence:** [T2] (the ideator's own citation, used against it) Serial Studio backup/recovery — https://serial-studio.com/help/backup-recovery — models undo as *snapshot-before-destructive-op*, i.e., restore a captured state, not replay a log.
- **Applies to Alexandria because:** the cheaper, safer primitive already exists in A4's `project_snapshot` (auto-snapshot before destructive ops). A3 should either write undo as "restore prior values transactionally" or defer to A4's snapshot mechanism — see Cross-Approach Interaction 4.

**A3-4. The fixed 0.5–0.7 confidence band is a judgment call with no calibration loop (LOW — flag for human, not a change request)**
- **Criticism:** The ≥0.7 / <0.5 / 0.5–0.7 band is a hard project constraint and must be preserved (no ×0.8 multiplier, ever). But the evidence on confidence-routing says the *exact numbers* are domain-dependent and should not be treated as self-evidently correct — and Alexandria has no feedback loop to verify the band.
- **Evidence:** [T2] Label Studio QE benchmark — https://labelstud.io/blog/building-a-quality-estimation-benchmark/ — "QE threshold tuning has a major impact on performance"; static thresholds without calibration send too many or too few items to review. [T3] Nyckel best practices — https://docs.nyckel.com/guides/best-practices — thresholds "are not set and forget"; suggested auto-accept ranges vary 0.60–0.95 by the cost of a wrong prediction.
- **Applies to Alexandria because:** the band itself is non-negotiable, so this is not actionable today — but it should be recorded as a deliberate, uncalibrated choice, and A3's insert-path enforcement must be written so the numbers are trivially adjustable later. This belongs in "what still needs human judgment," not in the implementation plan.

---

### A4: Overlay Config + Snapshot Projects

**A4-1. Top-level-only unknown-key preservation is the documented failure mode — preservation must be recursive and version-stamped (HIGH)**
- **Criticism:** A4's `KNOWN_TOP_LEVEL_KEYS` allowlist + append-unknown pass is precisely the "preserve only top-level keys" pattern that is documented as failing: unknown keys *nested inside known sections* get silently deleted on save. The fix that shipped elsewhere is recursive preservation — round-trip the raw document, diff against the typed model, and carry unknown paths at every nesting level — plus a schema-version stamp written on every save so a future/older binary detects the mismatch instead of silently stripping.
- **Evidence:** [T2] worktrunk PR #2180 — https://github.com/worktrunk/worktrunk/pull/2180 (Apr 2026): "The diff-based merge only preserved unknown top-level keys — unknown keys nested inside known tables were silently deleted on any save"; fix = recursive `PreserveTree` at every nesting level. This is the **exact** mechanism A4 proposes, and the exact mechanism that failed. [T2] openclaw #70578 — https://github.com/openclaw/openclaw/issues/70578: a `.strict()` schema "silently stripped unknown top-level fields on round-trip… config file silently stripped 34KB→13KB five times… a single operation silently deletes the content with no undo." [T1, first-party] docker/cli #5559 — https://github.com/docker/cli/issues/5559: config always saved with current schema, so older code discards newer fields (docker login stripping `features`); recommendation is patch-not-replace. [T4] zeroclaw #7274 — https://github.com/zeroclaw/zeroclaw/pull/7274: stale `schema_version` label on incremental saves → body-newer-than-label crashes with opaque errors; fix = stamp current version on every save.
- **Applies to Alexandria because:** the L4 bug (config round-trip strips generation/prompts) is the exact class of silent data loss these sources document — and A4's fix, as written, would fix the top level and leave the nested case broken. Where do generation/prompts keys live in Alexandria's config.json? If nested under a known `llm`/`tts` section, A4 as proposed still loses them. The fix must be: parse raw JSON → round-trip known fields → carry unknown paths recursively → stamp `schema_version` on every save.

**A4-2. Snapshot restore teardown breaks on shared characters — restore cannot be naive delete+reinsert (MEDIUM-HIGH)**
- **Criticism:** A4 reuses reonboard's `_clear_*` helpers as restore teardown. Those helpers are deliberately *book-scoped and character-preserving*: `reonboard_book` documents "Characters themselves are **not** deleted — they may be shared across books in a series" (app/pipeline/assembly.py:304). A snapshot restore that does delete+reinsert of character-linked state either (a) cannot restore character rows that were deleted after the snapshot (because reonboard never deletes them), or (b) if it deletes+reinserts characters, breaks sibling books in the series that share them (FK orphans, identity churn). The restore must define a merge-vs-replace policy for shared entities — and note that snapshotting "junction rows + voice_config rows + assignments" necessarily touches rows that are shared with other books.
- **Evidence:** grounded in Alexandria's own code (assembly.py:287–353, `_clear_span_junctions`, `_clear_memberships`). No external citation needed for the mechanism; the general hazard of snapshot-restore touching shared state is the same class as [T2] https://usagebox.com/articles/usagedb-internals-manifest-atomic-commit (restore/rollback of a partially-shared state must be defined against the shared entities).
- **Applies to Alexandria because:** multi-book series are an explicit schema feature (character/character_series tables; voice_config with `alias_of`). A snapshot restore that assumes single-book ownership will corrupt series neighbors. The plan must specify which tables are book-scoped vs series-scoped in a snapshot, and what restore does to shared characters/voice configs.

**A4-3. Snapshot state restores, but audio does not exist — "snapshots share audio" assumes durable audio that Alexandria doesn't have (MEDIUM-HIGH)**
- **Criticism:** A4's "snapshots share audio data (Ardour/Mixbus)" claim depends on audio being a durable, addressable artifact. In Alexandria it is not: `render_audiobook` defaults to `tempfile.mkdtemp(prefix=f'audiobook_{book_id}_')` — audio lives in ephemeral /tmp dirs, deleted on reboot, and GC'd whenever the OS cleans /tmp. Restoring a project snapshot after that yields a book with correct review/config state and **no audio surface** — capability 1 is gone even though the snapshot "succeeded." Ardour's model works because sessions live in a durable project directory; Alexandria's does not.
- **Evidence:** [T3] Ardour snapshot docs — https://manual.ardour.org/working-with-sessions/snapshots/ (the ideator's own citation): snapshots share "all data present in the session" — the *session directory is the durable artifact*. Alexandria's audio has no equivalent durable home unless A1 or A2 provides one.
- **Applies to Alexandria because:** this is a cross-approach coupling A4 states as an assumption: snapshot restore's usefulness for capability 1 depends entirely on A1/A2 making render output durable (run directories or artifact rows pointing at stable paths). It must be written as a dependency, not an assumption — and "restore = working project" must not be claimed until audio durability is in the chosen A1/A2 design.

**A4-4. Making `walk_override` live touches all 9 walk modules (LOW — blast-radius note, not a failure)**
- **Criticism:** `resolve_task_llm(task_name, config_path=None)` is hardcoded with `config_path=None` in all 9 walk modules (walk_2a … walk_2i) plus `test_resolve_task_llm.py`. Wiring `walk_override` in means threading an override dict through every walk's prompt assembly and every call site. Not a failure mode — but it is a cross-cutting refactor that must be budgeted in the plan, and the override resolution must happen once (a resolved-config helper), not as 9 bespoke copies.
- **Evidence:** structural, grounded in the codebase (app/utils.py:102; grep confirms all 9 walks pass `config_path=None`). No external citation; labeled honestly as scope.
- **Applies to Alexandria because:** the dead config param is dead in 9 places; the fix is a shared resolver, and the plan should say so explicitly.

---

### Cross-Approach Interactions

1. **A1 × A2 — three representations of the same run state.** If both designs proceed, a render has: `render_job`/`render_chunk` rows (A1), manifest.json + chunk files (A2), and — for walks — junction rows (DB, unavoidable). The log's cross-cutting section calls A1/A2 "mutually exclusive on the progress/cancel store," which is correct but understates it: **walks' side effects are DB rows regardless of which approach wins**, so A2's "filesystem is truth for runs" is only true for audio bytes and render runs, never for walks. The refined design must state one authority per artifact class: DB for junctions/review, FS for audio bytes, and exactly one of DB or manifest for progress. Mixing them recreates the drift failure documented in A3-1's citations at the render/walk level.

2. **A3 × A1 — walk_review bridge vs materialized review_item.** A1's bridge (union over walk-side persistence) and A3's materialization both claim the review surface. They must not coexist as two query paths with no stated precedence — that is the dual-write drift risk again, just moved to the read path. Pick one: A3-materialized (with transactional dual-write, A3-1) or A1-bridge (honest union, no materialization claim).

3. **A4 × A1/A2 — restore during a running walk/render.** A4's transactional restore can delete junction rows while a serial walk (2g/2i) is mid-run, or while a render is mid-flight; the walk's for_review counters and the render's chunk table are then corrupted by an interleaved restore. The design needs a guard ("no restore while walk/render running for this book") or a dirty-flag that forces re-walk of 2g/2h/2i after restore. This is cheap and currently absent from all four approaches.

4. **A4 × A3 — snapshot-undo is the cleaner primitive for review undo.** A3-3's audit-journal replay and A4's snapshot-before-destructive both claim "undo." They should be unified: snapshot-based undo (A4) for structural ops, transactional value-restore for single review-item undo — never journal replay. Two undo mechanisms on the same state is a consistency tax with no benefit.

5. **A2 × A4 — stop-request.json sentinel lifecycle.** A2's cancel sentinel needs the same lifecycle the runner's `clear_cancel` has today: a stale `stop-request.json` from a previous run must not cancel the next run. Trivial, but it is exactly the stale-state class A1-1 warns about, in file form.

---

### Summary

- **Surviving approaches:** all four, with mandatory changes. A1 (stale-run reconciliation + retention + single-transaction discipline), A2 (fsync discipline + generations + manifest-as-progress + explicit range-serving + walk-side truth concession), A3 (transactional dual-write or honest union + supersede-on-rewalk), A4 (recursive + version-stamped key preservation + shared-character/audio-durability semantics).
- **Dead approaches:** none — but A4 standalone is confirmed dead as an audio-surface play (must pair with A1/A2), matching the log's own assessment.
- **Most critical unresolved concern:** stale-state reconciliation for persisted job state (A1-1) — every approach that survives process death must define what the UI shows after a crash. Second: the A1×A2 walk-side truth conflict (A2-4) must be resolved by design, not left to the runner.
- **What still needs human judgment:** the exact confidence band numbers (A3-4, constrained but uncalibrated); whether batch-mode coarse progress is acceptable UX or individual mode should be the default render path; whether restore-during-run is blocked or dirty-flagged (interaction 3). Evidence cannot settle these.

## Refined Approaches

### Response to Critique

Every mandatory change from Turn 2 is adopted by a named mechanism. Mapping:

- **A1-1 (stale 'running' rows):** adopted — `render_job`/`walk_run` gain a `heartbeat_ms` column, bumped by the worker at each chunk boundary (render) and between walks (runner); a startup `reconcile_stale_runs()` flips rows whose heartbeat is older than a threshold from `running` → `failed (interrupted)` with the stale timestamp recorded, and a periodic sweeper applies the same predicate. **Post-crash UI is now defined:** a job shows `failed (interrupted)` with cause, never eternal `running`. This is the direct answer to the summary's most-critical concern — and it is strictly more informative than today's in-memory design, which forgets the job entirely.
- **A1-2 (COUNT overcount):** adopted — `render_chunk` rows carry `status` (`pending`/`done`/`failed`); a row is inserted as `pending` at dispatch and flipped to `done` **only at completion**. Progress = `COUNT(status='done') / COUNT(*)`. Rows are never counted at start (Hadoop v2 committer lesson).
- **A1-3 (busy_timeout / second writer):** adopted — `SQLiteAdapter.__init__` adds `PRAGMA busy_timeout=5000` (adapter.py line 82 today has none); render progress writes reuse the caller's connection (the `complete_walk` convention), never a second writer connection, avoiding SQLITE_BUSY_SNAPSHOT.
- **A1-4 (retention):** adopted — `job_retention_days` + `chunk_retention_days` policy enforced by the sweeper (delete rows **and** WAV files for completed/failed runs past policy); the NovelFoundry "cleanup is a first-class feature" lesson.
- **A2-1 (tmp+rename crash safety):** adopted — atomic manifest commit becomes write tmp → `fsync(tmp_fd)` → rename → `fsync(parent_dir_fd)`; numbered generations (`manifest.N.json` + `CURRENT` pointer); **fail-closed parse**: startup refuses to treat a run as resumable when no manifest parses, surfacing `interrupted` instead of silently resetting.
- **A2-2 (listdir counts in-flight as done):** adopted — progress is computed **exclusively from manifest records**; listdir is dropped to a sanity check only. A chunk record is appended to the manifest only after its audio file is fsync'd and renamed into place: "a chunk counts when the manifest says so" (this also fixes the A1-2 overcount class for free).
- **A2-3 (range serving):** adopted — a dedicated `/audio/{run_id}/{chunk}` endpoint with a **bounded, hand-written Range parser** (single-range only, size-capped, no regex), returning explicit `Accept-Ranges: bytes` + 206; FileResponse's default range path is not used. Build-time version guard `starlette>=0.49.1` in CI (env drift: installed 1.3.1 vs `fastapi==0.128.0` resolving older starlette) — GHSA-7f5h-v6xp-fcq8 / CVE-2025-62727 (O(n²) Range DoS, CVSS 7.5, fixed 0.49.1) is the concrete threat.
- **A2-4 (walk-side truth conflict):** adopted as a concession — the manifest mirrors **render runs and audio bytes only**; junction rows remain DB truth written by walks through the storage adapter. The per-walk JSONL is an audit trace, not a rollback instrument; `reonboard_book`'s `_clear_*` teardown stays the only walk-side reset path. **One authority per artifact class:** DB for junctions/review, FS for audio bytes, exactly one of DB (A1-r) or manifest (A2-r) for progress.
- **A3-1 (materialization = dual-write):** adopted by dropping the claim — junction items are served as a **live query**; junction rows stay truth (ReviewManager keeps writing conf onto them). 2g/2h/2i items get a new `walk_review_item` table written **by the walk itself in the same transaction** as its junction/metadata writes (the walk's own output record, not a mirror). Accept/reject of a walk-side item updates the underlying target (`character_metadata` value, `voice_assignment_id`, `span.instruct`) **and** marks the item resolved in one transaction on the caller's connection. This is the honest-union form the critique named; the A3×A1 conflict is resolved by choosing the union.
- **A3-2 (supersede-on-rewalk undefined):** adopted — a re-run of 2g/2h/2i opens **one transaction**: `UPDATE walk_review_item SET status='superseded' WHERE book_id=? AND kind IN (...) AND status='pending'`, then inserts the new items, then commits. Supersede is an explicit durable transition (the Conduktor tombstone lesson: a passive re-appearance resurrects ghosts), and a sweeper flags pending items past an age as `stale` (LabelOp recycling-bin avoidance).
- **A3-3 (audit-journal undo):** adopted — journal replay is dropped. Single review-item undo = **transactional value-restore** (the pre-action value is stored on the action row; undo writes it back and flips the item status in one transaction). Structural undo = A4 snapshots. Exactly two undo mechanisms, each owning its scope.
- **A3-4 (uncalibrated band):** adopted — thresholds stay in one constants module (`REVIEW_CONFIDENCE_MIN/MAX`), read by the insert path; **no ×0.8**. Calibration of the 0.5–0.7 band is flagged for human judgment (Nyckel-style cost-of-wrong-decision framing).
- **A4-1 (top-level-only preservation fails):** adopted — config save = parse raw JSON → apply known-field updates → **recursively carry unknown paths at every nesting level** (the worktrunk #2180 failure mode, fixed the way worktrunk fixed it) → stamp `schema_version` on **every** save (zeroclaw #7274) → patch-not-replace (docker/cli #5559). Message-oriented merge (protobuf `CopyFrom`/`MergeFrom` semantics) is the model.
- **A4-2 (shared characters):** adopted — the snapshot manifest declares table scoping: **book-scoped** (chapter/scene/paragraph/span/edges/junctions/memberships/metadata) vs **series-scoped** (series/book/character/voice_config). Restore is merge-vs-replace: characters are never deleted; restore re-attaches memberships and junctions for the book; characters that did not exist in the snapshot become unlinked, not deleted — no identity churn, no sibling-series FK orphans.
- **A4-3 (audio durability assumption):** adopted — rewritten as an explicit **dependency on A1-r/A2-r**: snapshot rows reference run-directory paths or artifact rows at stable locations. Restore only claims a "working project" when the referenced audio exists; if the run dir was GC'd, restore surfaces `audio missing — re-render` instead of silently dead playback (capability 1 intact or honestly absent).
- **A4-4 (9-module blast radius):** adopted — one shared `resolve_task_config(task_name, overrides)` helper in app/utils.py; all 9 walks and `test_resolve_task_llm.py` call it. `walk_override` is read once per walk run. Budgeted as a cross-cutting refactor, not 9 bespoke copies.
- **Cross-1 (three representations):** resolved by the authority rule above.
- **Cross-2 (A3×A1 two query paths):** resolved — honest union chosen; exactly one review read path.
- **Cross-3 (restore during running walk/render):** adopted — **block by default**: restore is refused while any `walk_run` or `render_job` row for this book is active (checked in the same transaction as the restore); a dirty-flag forcing re-walk of 2g/2h/2i remains the documented alternative. Block is chosen because it is deterministic and cheap.
- **Cross-4 (undo unification):** adopted — snapshot for structural ops, transactional value-restore for single review-item undo, never journal replay.
- **Cross-5 (stale stop-request.json):** adopted — the sentinel embeds the run id; cancel is honored only when `sentinel.run_id == current run_id` (the runner's `clear_cancel` equivalent); the sentinel is deleted on run finalize.

### Refined Approach 1: Schema-Native Jobs with Reconciliation (A1-r)

- **What changed vs Turn 1:** gained a reconciliation + heartbeat layer (A1-1), completion-marked chunk status (A1-2), `busy_timeout` + single-writer discipline (A1-3), retention policy (A1-4), and the authority concession that FS owns audio bytes while DB owns junctions/review (Cross-1).
- **Architecture (refined):** run-state tables remain the progress authority: `render_job` (status, heartbeat_ms, last_updated_ms, error, output_dir, output_artifact_path), `render_chunk` (job_id, index, status pending/done/failed, wav_path), `walk_run` (walk_name, book_id, status, heartbeat_ms, for_review counters persisted — fixing the L5 gap that execute() return dicts are discarded). Worker loops bump heartbeat at each chunk/walk boundary. `reconcile_stale_runs()` runs at startup and on any status read: `running` rows with heartbeat older than threshold → `failed (interrupted)`. A sweeper enforces retention and reaps orphaned WAV files. Audio is served by the dedicated range endpoint reading `wav_path` rows (A2-r's endpoint contract, reused); exports (capability 3) are pipeline consumers of completed chunk rows.
- **Critique addressed:**
  - A1-1 → heartbeat_ms + startup reconciliation + periodic sweeper; post-crash UI = `failed (interrupted)` with timestamp.
  - A1-2 → status column, `done` set only at completion, COUNT(status='done').
  - A1-3 → `PRAGMA busy_timeout=5000` in adapter init; no second writer connection.
  - A1-4 → age-based retention deleting rows + WAV files.
  - Cross-3 → restore blocked while an active walk_run/render_job row exists for the book.
- **Production evidence (refined pattern):**
  - BullMQ stalled-jobs recovery (https://docs.bullmq.io/guide/jobs/stalled): worker heartbeat lock renewal; a sweeper moves jobs lacking a fresh lock from active to failed after `maxStalledCount` — the exact heartbeat + sweeper pair A1-r adopts.
  - N8N orphaned-job postmortem (https://azguards.com/technical/the-orphaned-job-trap-recovering-stalled-bullmq-executions-in-auto-scaled-n8n-clusters/): lock TTL (60s), heartbeat renewal (10s), stalled-sweep interval (30s) — the concrete tuning knobs for Alexandria's `heartbeat_ms` threshold.
  - Kubernetes TTL-after-finished (https://kubernetes.io/docs/concepts/workloads/controllers/ttlafterfinished/): finished Jobs are eligible for cascading deletion after `ttlSecondsAfterFinished` — a retention-by-policy controller, the model for A1-r's sweeper.
  - NovelFoundry queue postmortem (https://pbrazeale.github.io/posts/novelfoundry-queue-bug/): the critique's own citation — "if user-facing state depends on async workers, you need authoritative transitions, stale-state recovery… periodic sweep" — is now the design's first principle, not a warning.
- **Capability coverage (updated):**
  - 1 (audio surface): handled — chunk rows → dedicated range endpoint; per-span preview via `render_chunk` rows joined to spans.
  - 2 (progress/cancel): handled — reconciled progress rows; cancel writes `cancel_event` + marks job `cancelling`; walk cancel between walks via `walk_run` rows.
  - 3 (export): handled — M4B/MP3/Audacity consumers read completed chunk rows + paths.
  - 4 (voice config): partial — existing `api_voices` CRUD/preview carries it; A1-r adds nothing new here.
  - 5 (review surface): partial — junction items live-query; 2g/2h/2i items via A3-r's `walk_review_item` (A1-r supplies the persistence table).
  - 6 (prompt/param overrides): partial — via A4-r's `walk_override` + recursive config.
  - 7 (iteration utilities): partial — snapshots/undo via A4-r; sequence playback reads chunk rows.
  - 8 (pipeline-only): handled — no legacy endpoints/symbols; registered in CONTRACTS.md.

### Refined Approach 2: Artifact-First Run Directory with fsync Discipline (A2-r)

- **What changed vs Turn 1:** manifest becomes the sole progress instrument (A2-2); commit path is fsync-safe with generations + fail-closed parse (A2-1); range serving is a dedicated bounded endpoint with a build-time starlette guard (A2-3); walk-side truth conceded to the DB (A2-4); stop-request sentinel is run-scoped (Cross-5).
- **Architecture (refined):** each render run owns a directory: `outputs/chunk_{i:04d}.{ext}` written via tmp+fsync+rename, `manifest.json` appended atomically (tmp → fsync → rename → fsync parent dir, numbered generations `manifest.N.json` + `CURRENT`), `run.stop-request.json` embedding the run id. Progress = count of completed records in the manifest; listdir is sanity-only. On startup, fail-closed parse: no valid manifest → run reported `interrupted`, never silently restarted. Cancel = writing the run-scoped sentinel; a stale sentinel (mismatched run id) is ignored. Audio served by the bounded range endpoint reading manifest-verified files. DB remains truth for junctions/review; the manifest governs only render runs and audio bytes.
- **Critique addressed:**
  - A2-1 → write tmp + fsync file + rename + fsync parent dir + numbered generations + CURRENT + fail-closed parse.
  - A2-2 → manifest-only progress; "a chunk counts when the manifest says so."
  - A2-3 → dedicated endpoint with bounded single-range parser; CI guard `starlette>=0.49.1` (GHSA-7f5h-v6xp-fcq8 / CVE-2025-62727).
  - A2-4 → manifest scope conceded to render runs + audio bytes; DB owns junction/review truth.
  - Cross-5 → run-id-scoped stop sentinel; stale sentinel ignored; deleted on finalize.
- **Production evidence (refined pattern):**
  - Crash-consistent atomic write (https://0xkiire.com/crash-consistency-fsync-rename/): write tmp → fsync(tmp fd) → rename → fsync(dir fd); write() alone is not durable — the exact sequence A2-r mandates.
  - "The syscall I forgot: directory fsync" (https://aalhour.com/posts/beachdb-the-syscall-i-forgot/): fsync(file) does not persist the directory entry; LevelDB's `SyncDirIfManifest()` and RocksDB's `FSDirectory` exist precisely because skipping it loses manifests on crash.
  - `dbmd_core::fsx` write_atomic (https://docs.rs/dbmd-core/latest/dbmd_core/fsx/index.html): temp with `create_new` (no clobber race) → fsync → rename → fsync parent dir; never plain `std::fs::write`; deliberately weaker durability only for rebuildable derived data — A2-r applies full fsync to load-bearing manifests.
  - GHSA-7f5h-v6xp-fcq8 (https://github.com/Kludex/starlette/security/advisories/GHSA-7f5h-v6xp-fcq8, CVE-2025-62727): O(n²) Range-header DoS affecting starlette ≥0.39.0–≤0.49.0, fixed in 0.49.1 — the concrete reason range serving must be a dedicated bounded endpoint, not FileResponse defaults.
  - Hep.gg audio streaming (https://docs.hep.gg/docs/music/stream-and-artwork): production music API serving inline audio with `Accept-Ranges: bytes` + 206 — the serving contract A2-r replicates.
- **Capability coverage (updated):**
  - 1 (audio surface): handled — range-served audio from the run dir; per-span preview via per-chunk files + manifest mapping.
  - 2 (progress/cancel): handled — manifest-only progress; run-scoped stop sentinel; interrupted-after-crash via fail-closed parse.
  - 3 (export): handled — export consumers read manifest + fsync'd chunk files.
  - 4 (voice config): partial — existing `api_voices` carries it.
  - 5 (review surface): partial — junction live-query + A3-r walk-side items.
  - 6 (overrides): partial — via A4-r.
  - 7 (iteration utilities): partial — snapshots via A4-r; sequence playback reads the manifest.
  - 8 (pipeline-only): handled — no legacy endpoints/symbols.

### Refined Approach 3: Unified Review Queue — Honest Union + Supersede (A3-r)

- **What changed vs Turn 1:** the materialized `review_item` mirror is dropped (A3-1); junction items are a live query; a new walk-side `walk_review_item` table is written by the walks themselves transactionally; supersede-on-rewalk defined (A3-2); undo unified with A4 (A3-3); thresholds centralized (A3-4). This is the critique's honest-union option, which resolves the A3×A1 conflict by construction.
- **Architecture (refined):** the review read path is a single union: (a) junction items — live queries over `character_book`/`character_scene`/`character_span`/`character_series` exactly as ReviewManager does today (truth never mirrored); (b) walk-side items — `walk_review_item(id, book_id, kind ∈ {voice_profile, voice_assignment, instruction}, target_table, target_id, prior_value, status ∈ {pending, resolved, superseded, stale}, created_ms)`, written by 2g/2h/2i in the **same transaction** as their junction/metadata writes. Accept/reject/override of a walk-side item updates the underlying target (character_metadata value, voice_assignment_id, span.instruct) and flips the item's status in one transaction on the caller's connection. Re-walk supersedes prior pending items in the same transaction as the new inserts. Single-item undo = transactional value-restore using `prior_value`; structural undo = A4 snapshots.
- **Critique addressed:**
  - A3-1 → junction rows stay truth; no mirror; walk-side items are the walk's own transactional output (honest union chosen over materialization).
  - A3-2 → supersede-on-rewalk is an explicit durable transition in the same transaction as new inserts; stale sweep for old pending items.
  - A3-3 → undo = transactional value-restore (single item) + A4 snapshots (structural); journal replay dropped.
  - A3-4 → thresholds centralized and read at insert time; no ×0.8; band calibration flagged for human.
  - Cross-2 → one review read path (the union), one write path.
  - Cross-4 → undo scope unification with A4-r.
- **Production evidence (refined pattern):**
  - Kafka log compaction (https://docs.confluent.io/kafka/design/log_compaction.html) and the compacted-event-stream pattern (https://developer.confluent.io/patterns/event-storage/compacted-event-stream/): "Remove events from the stream that represent outdated information and have been superseded by new events… at least the last update for each primary key is retained" — the supersede-old-versions semantic A3-r applies to re-walked items.
  - Conduktor state-TTL analysis (https://www.conduktor.io/kafka-streams/state-ttl): "the deletion must reach the changelog as a tombstone, or a restore brings the key back" — the precise failure A3-2's same-transaction supersede prevents (passive disappearance resurrects ghosts).
  - Segments.ai label-queue mechanics (https://docs.segments.ai/background/label-queue-mechanics): rework is an explicit status transition, never passive re-appearance — A3-r's `superseded` status.
  - LabelOp review-queue best practices (https://labelop.com/blog/labelop-review-queue-best-practices-2026): stale work left in a queue "becomes a recycling bin" — A3-r's stale sweep.
- **Capability coverage (updated):**
  - 1 (audio surface): not covered directly — depends on A1-r/A2-r serving.
  - 2 (progress/cancel): not covered — depends on A1-r/A2-r.
  - 3 (export): not covered — depends on A1-r/A2-r.
  - 4 (voice config): partial — review of voice-profile items surfaces `character_metadata` voice_profile values, but editing stays in `api_voices`.
  - 5 (review surface): **handled** — the primary carrier: junction live-query + walk-side 2g/2h/2i items with supersede and undo.
  - 6 (overrides): not covered — via A4-r.
  - 7 (iteration utilities): partial — contextual review payloads (±2 paragraphs) and review undo; sequence playback via A1-r/A2-r.
  - 8 (pipeline-only): handled — new table + endpoints registered in CONTRACTS.md; no legacy surface.

### Refined Approach 4: Overlay Config + Snapshot Projects — Recursive & Dependency-Aware (A4-r)

- **What changed vs Turn 1:** unknown-key preservation is now recursive + schema-version-stamped (A4-1); snapshot table scoping and a merge-vs-replace policy for shared characters are defined (A4-2); audio durability is written as an explicit dependency on A1-r/A2-r, not an assumption (A4-3); `walk_override` wiring is a single shared resolver (A4-4); restore is blocked during running walks/renders (Cross-3); undo is unified with A3-r (Cross-4).
- **Architecture (refined):** config save = parse raw `config.json` → apply known-field updates from AppConfig → **recursively carry unknown paths at every nesting level** → stamp `schema_version` on every save → patch-not-replace. `walk_override(book_id, walk_name, key, value)` rows are read once per walk run by `resolve_task_config(task_name, overrides)` in app/utils.py (the one helper all 9 walks call — fixes the L5 dead-`config`-param and the capability-6 data-loss in one place). `project_snapshot` = manifest declaring book-scoped vs series-scoped tables + references to render artifacts; restore is merge-vs-replace for shared characters, blocked while the book has active walk_run/render_job rows; structural undo restores a snapshot, single review-item undo delegates to A3-r's value-restore.
- **Critique addressed:**
  - A4-1 → recursive PreserveTree at every nesting level + `schema_version` on every save + patch-not-replace.
  - A4-2 → book-scoped vs series-scoped table scoping; characters never deleted; restore re-attaches memberships; no identity churn.
  - A4-3 → audio durability written as a dependency on A1-r/A2-r; restore reports `audio missing — re-render` when run dirs are gone.
  - A4-4 → one `resolve_task_config` helper; refactor budgeted across 9 walks + tests.
  - Cross-3 → restore blocked while the book has active runs (checked transactionally).
  - Cross-4 → undo split: snapshots for structural ops, value-restore for review items.
  - Cross-5 → stop-request lifecycle owned by A2-r's run-scoped sentinel.
- **Production evidence (refined pattern):**
  - Protocol Buffers unknown-field preservation (https://protobuf.dev/programming-guides/proto3/): "Proto3 messages preserve unknown fields and include them during parsing and in the serialized output"; the guidance to use message-oriented `CopyFrom()`/`MergeFrom()` rather than field-by-field access is the production-scale recursive-preservation mechanism A4-r mirrors.
  - worktrunk PR #2180 (https://github.com/worktrunk/worktrunk/pull/2180): "diff-based merge only preserved unknown top-level keys — unknown keys nested inside known tables were silently deleted on any save"; fix = recursive `PreserveTree` at every nesting level — the exact mechanism A4-r mandates, proven against the exact failure mode.
  - docker/cli #5559 (https://github.com/docker/cli/issues/5559): config always saved with the current schema; older code discards newer fields; recommendation is patch-not-replace — A4-r's save path.
  - Ardour session snapshots (https://manual.ardour.org/working-with-sessions/snapshots/): a snapshot is a frozen session version sharing the durable session directory — why A4-r's audio-durability dependency (A4-3) exists: Ardour works because the session dir is the durable artifact.
- **Capability coverage (updated):**
  - 1 (audio surface): not covered standalone — dependency on A1-r/A2-r artifact durability; restore surfaces `audio missing` otherwise.
  - 2 (progress/cancel): not covered — via A1-r/A2-r.
  - 3 (export): not covered — via A1-r/A2-r.
  - 4 (voice config): partial — recursive config preserves voice-related keys on round-trip; editing stays in `api_voices`.
  - 5 (review surface): partial — snapshot/undo support for review state; queue itself via A3-r.
  - 6 (prompt/param overrides): **handled** — the primary carrier: `walk_override` + recursive config round-trip (fixes the KNOWN data-loss bug).
  - 7 (iteration utilities): **handled** — named snapshots, save/load, undo (structural), single-speaker mode as a snapshot-variant; pause-after/sequence playback ride on A1-r/A2-r artifacts.
  - 8 (pipeline-only): handled — no legacy endpoints; config/snapshot are new pipeline tables + endpoints in CONTRACTS.md.

### Dropped or Merged

- **A3 materialization (dropped, merged into A3-r's honest union):** the materialized `review_item` mirror died on A3-1 — junction rows are truth and a mirror is a dual-write. The walk-side persistence table survives but is reframed as the walks' own transactional output (the critique's "a view + walk-side persistence for 2g/2h/2i items only"), which is A1's bridge. This resolves Cross-2 by construction: one read path, one write path.
- **A4 standalone as an audio-surface play (confirmed dead, as the critique and Turn-1 log both found):** A4-r carries capability 1 only as an explicit dependency on A1-r/A2-r; it cannot deliver playback/preview/export alone.
- **A1×A2 exclusivity (merged into a single authority rule):** rather than competing, A1-r and A2-r now differ only in the progress authority (DB vs manifest) and agree everywhere else — FS owns audio bytes, DB owns junctions/review, exactly one progress authority. The Refiner/DD-Author picks the progress authority; the rest of the contract is shared.
- **Audit-journal undo (dropped):** replay-based undo is dead on A3-3 — superseded entities and conditional transitions make replay fragile. Replaced by snapshot-undo (structural) + transactional value-restore (single item).

### Capability Carrier Check

| # | Capability | Carriers (refined) |
|---|---|---|
| 1 | Result/audio surface (playback + per-span preview) | A1-r (handled), A2-r (handled) |
| 2 | Render progress (completed/total, failures) + cancellation | A1-r (handled), A2-r (handled) |
| 3 | Polished export (M4B metadata/cover/chapters, MP3/Audacity) | A1-r (handled), A2-r (handled) |
| 4 | Voice config editing (5 types, ref audio/text, narrator override, preview) | existing `api_voices` + A4-r config preservation (partial) |
| 5 | Surface 2g/2h/2i review items | A3-r (handled — primary carrier) |
| 6 | Prompt/generation overrides without legacy duplication | A4-r (handled — primary carrier) |
| 7 | Script/project workflow, single-speaker, contextual review, undo, pause-after, sequence playback | A4-r (handled) + A3-r (review undo) + A1-r/A2-r (sequence/pause artifacts) |
| 8 | Pipeline-only architecture preservation | all four (handled) |

**No capability lacks a surviving carrier.** The only coverage that is "partial by design" is capability 4, which the existing `api_voices` module already satisfies and which A4-r protects from the config round-trip bug; and the audio-dependent halves of 1–3 in A4-r, which are explicit dependencies rather than gaps. The one decision the Refiner/DD-Author must still make is the progress authority (A1-r DB rows vs A2-r manifest) — the two candidates are now contractually identical everywhere else, so the choice is a single axis, not a fork in the architecture.

---

## Surviving Concerns

*Turn 4 — Counter-Ideator. Assessment of the Turn 3 refinements against the Turn 2 critique. Verdicts are grounded in the refined text, the code, and (where noted) fresh evidence. No re-litigating of fixed items.*

### Critique Response Verdict Table

| Turn 2 criticism | Verdict | What remains (if any) |
|---|---|---|
| A1-1 stale `running` rows | **PARTIALLY ADDRESSED** | Heartbeat + reconciliation adopted, but the *on-read* reconciliation and sweeper use a heartbeat-age predicate that cannot distinguish a slow-but-alive worker from a dead one; in a single-process app the only worker-death mechanism is process death, so on-read reconciliation is both unnecessary and able to falsely flip a live job to `failed (interrupted)`. The cited n8n tuning knobs (10s heartbeat / 30s sweep) are for seconds-long HTTP jobs and mis-transfer to multi-minute LLM walk units. |
| A1-2 COUNT overcount | **ADDRESSED** | `pending`/`done`/`failed` status; `done` set only at completion. |
| A1-3 busy_timeout / second writer | **ADDRESSED** | `PRAGMA busy_timeout=5000` in adapter init; single-writer discipline. |
| A1-4 retention | **PARTIALLY ADDRESSED** | Age-based sweeper deletes rows + WAVs, but does not consult `project_snapshot` references — old snapshots silently degrade to `audio missing — re-render` (see A4-3 interaction). |
| A2-1 tmp+rename fsync | **ADDRESSED** | Full fsync file + parent-dir + generations + fail-closed parse. Per-chunk fsync cost is a throughput note, not a correctness gap. |
| A2-2 listdir overcount | **ADDRESSED** | Manifest-only progress; listdir sanity-only; chunk counted only after fsync+rename. |
| A2-3 range serving | **ADDRESSED** | Dedicated bounded endpoint; CI `starlette>=0.49.1` guard. Edge: hand-written single-range parser must still handle open-ended `bytes=0-` and suffix `bytes=-N` or return 416. |
| A2-4 walk-side truth conflict | **ADDRESSED** | Manifest scoped to render runs + audio; DB owns junction/review truth; JSONL demoted to audit trace. |
| A3-1 dual-write drift | **PARTIALLY ADDRESSED** | Honest union chosen correctly, but "the walk itself writes `walk_review_item` in the same transaction as its junction/metadata writes" is asserted, not mechanized: walk modules call `storage.execute_*` which auto-commits per statement (`isolation_level=None`), and no walk wraps its unit writes in BEGIN/COMMIT today. |
| A3-2 supersede-on-rewalk | **ADDRESSED** | Same-transaction supersede + stale sweep; Conduktor tombstone lesson applied. |
| A3-3 audit-journal undo | **ADDRESSED** | Transactional value-restore + A4 snapshots; replay dropped. |
| A3-4 uncalibrated band | **ADDRESSED** | Thresholds centralized; calibration flagged for human. |
| A4-1 recursive preservation | **ADDRESSED** | Recursive PreserveTree at every nesting level + `schema_version` stamp + patch-not-replace. |
| A4-2 shared characters | **ADDRESSED** | Book-scoped vs series-scoped manifest; characters never deleted; merge-vs-replace restore. |
| A4-3 audio durability | **ADDRESSED** | Written as explicit dependency on A1-r/A2-r; restore surfaces `audio missing — re-render`. One interaction remains with A1-r's retention sweeper. |
| A4-4 9-module blast radius | **ADDRESSED** | One shared `resolve_task_config` helper; budgeted. |
| Cross-1 three representations | **ADDRESSED** | One authority per artifact class, stated as a rule. |
| Cross-2 A3×A1 two query paths | **ADDRESSED** | Honest union chosen; one read path, one write path. |
| Cross-3 restore during run | **ADDRESSED** | Block-by-default, checked in the restore transaction. A micro-race (walk starts between check and restore commit) exists but is effectively covered by the single-connection API-level serialization; LOW. |
| Cross-4 undo unification | **ADDRESSED** | Snapshot structural + value-restore single item. |
| Cross-5 sentinel lifecycle | **ADDRESSED** | Run-id-scoped sentinel; stale ignored; deleted on finalize. |
| *Most-critical: stale-state reconciliation* | **PARTIALLY ADDRESSED** | Same residual as A1-1: the mechanism is right, the reconciliation *predicate* (heartbeat age, applied on-read) is wrong for this workload. |
| **NEW PROBLEM** (not in Turn 2): single-speaker mechanism | **NEW PROBLEM** | Turn 1 A4 proposed a `book.single_speaker` flag with assembly mapping every speaker to NARRATOR at the export boundary. A4-r capability 7 now says "single-speaker mode as a **snapshot-variant**" — a different mechanism that is ambiguous and, if literal, does not survive a 2h re-walk (voice assignment regenerates). Needs pinning back to a book-level flag or an explicit, re-walk-surviving mechanism. |

**Counts: 18 ADDRESSED, 3 PARTIALLY ADDRESSED, 0 NOT ADDRESSED, 1 NEW PROBLEM.** The Ideator's refinement work is real and mostly lands; the residuals below are where implementation would actually bite.

---

### What Still Doesn't Work

1. **Heartbeat reconciliation can falsely fail a live job (A1-1 residual).** `reconcile_stale_runs()` runs at startup *and on any status read*, and the sweeper applies the same predicate. A heartbeat bump "between walks" means a single-walk run (or a long 2g/2i on a large book) gets **zero** heartbeat updates while it runs — any threshold shorter than the walk duration flips a live, progressing job to `failed (interrupted)` on the next `GET /render_status`. The still-alive worker then overwrites the reconciled state when it completes, so the UI shows failed→completed, and a user who acted on the false failure (retried, re-rendered) has wasted work. Evidence that this is a real, named failure class, not a theoretical one: BullMQ's stalled-jobs machinery is *two-phase* (mark, then recover on a second check) specifically to "prevent false positives from timing issues" — https://bullmq.hexdocs.pm/BullMQ.StalledChecker.html — and its own ecosystem is full of "job stalled more than allowable limit" incidents caused by jobs that legitimately ran longer than the lock window (e.g., https://stackoverflow.com/questions/74449830/downloading-large-files-from-a-google-cloud-bucket-in-a-bullmq-worker-leads-to-s, fixed by raising lockDuration 30s→5min; https://github.com/Crosstalk-Solutions/project-nomad/issues/604). The n8n numbers the Ideator cites (10s heartbeat / 30s sweep) are tuned for sub-second HTTP jobs and are the *wrong* prior for LLM calls that run tens of seconds to minutes. The fix is cheap and should be: **startup-only reconciliation** (in a single-process app, process death is the only way the worker dies, so a startup pass is both sufficient and race-free), or — if on-read reconciliation is kept — gate it on an in-process registry of live worker threads so it never reconciles a row owned by a live thread, and set the threshold above the worst-case single LLM unit.

2. **Walk-side item atomicity is asserted, not mechanized (A3-1 residual).** The refined design says 2g/2h/2i write `walk_review_item` "in the same transaction as their junction/metadata writes." The adapter's `execute_*` methods commit after every statement when not already in a transaction (`isolation_level=None` semantics), and no walk module currently opens a multi-statement transaction. Without an explicit change — the runner wrapping each walk unit in BEGIN/COMMIT on the caller's connection, or a walk-side batching convention — the "same transaction" claim is false at implementation time, and a crash between a junction write and the item insert produces either a review item with no underlying target change (ghost) or a target change with no review item (silently missed low-confidence audition — the exact L5 gap this table was meant to close). The mechanism (wrap on caller's connection) exists and is consistent with the `complete_walk` convention; it just has to be written into the plan as a runner-level responsibility, not left to each walk.

3. **Retention GC vs snapshot references (A1-4 × A4-3 interaction).** A1-r's sweeper deletes WAV files by age for completed/failed runs; A4-r's snapshots reference those paths and only surface `audio missing — re-render` when they are gone. Under those two rules, **every snapshot older than the retention window loses its audio** — the "honest fallback" becomes the default for old snapshots, not the exception, and the user experience of capability 7 ("load a saved project") silently degrades to "load state, re-render audio." The sweeper should either skip artifacts referenced by any snapshot, or snapshots should pin artifacts (a reference count / pin list), so the fallback fires only when audio was genuinely GC'd before the snapshot existed.

4. **Capability 4's user-facing half is unclaimed (carrier-check overclaim).** "Existing `api_voices` already satisfies" is true at the API level (CRUD + preview endpoints exist) and false at the UI level. The current frontend (`frontend/src/tabs/voices.ts`) covers: narrator voice selection (single `voice` field), character voice assignment dropdowns, a voice catalog with preview buttons, and a **type badge**. It does **not** cover: editing the five voice types' configuration (style, ref audio/text, clone/lora/design parameters), alias mapping (no UI — a documented L4 deviation), or creating/editing voice-config rows beyond the narrator's `voice` field. No refined approach claims the frontend work for capability 4; the L4 regression "voice config editing UI" is restored by nobody.

5. **Capability 1's per-span preview is batch-mode-dependent (unstated partial-by-design).** `RenderRequest.use_batch` defaults to `True`; in batch mode `generate_batch` runs once and **no per-chunk rows/files exist**, so per-span/per-chunk preview (the headline of capability 1) is simply absent on the default render path. The refined carrier claims "per-span preview via render_chunk rows joined to spans" without stating the individual-mode-only scope. A user who renders with default settings gets the whole-book player but no per-span preview — a regression vs. v1's per-chunk players, experienced silently.

6. **Single-speaker mechanism regressed (NEW PROBLEM).** See table row. A `book.single_speaker` flag with assembly-time NARRATOR mapping (Turn 1, one-line assembly change) was replaced by "single-speaker mode as a snapshot-variant" (Turn 3), which is ambiguous and, read literally, breaks under 2h re-walk. Pin the mechanism.

---

### Risks That Persist

- **Risk:** Live job falsely reconciled to `failed (interrupted)`, then resurrected by the worker's own completion write; user acts on the false failure. **Severity:** HIGH **Trigger:** any single-walk run or walk unit longer than the heartbeat threshold, with on-read reconciliation enabled (guaranteed if the n8n 10s/30s knobs are copied).
- **Risk:** Ghost or missing walk-review items from non-atomic walk-side writes. **Severity:** MEDIUM **Trigger:** crash/power loss between a walk's junction write and its `walk_review_item` insert when no wrapping transaction is implemented.
- **Risk:** Old snapshots silently lose audio to retention GC. **Severity:** MEDIUM **Trigger:** any snapshot older than `chunk_retention_days`; restore then reports `audio missing — re-render`.
- **Risk:** Capability 4 ships as API-only; users cannot edit voice configs (style/ref audio/5 types) or map aliases in the UI — the L4 regression persists despite "carrier exists." **Severity:** MEDIUM **Trigger:** any user attempt to edit a non-narrator voice config or alias a voice.
- **Risk:** Per-span preview absent in the default batch render mode. **Severity:** MEDIUM **Trigger:** render with `use_batch=True` (the default), then open per-span preview.
- **Risk:** Range endpoint rejects legitimate player requests. **Severity:** LOW **Trigger:** a player sending `bytes=0-` (open-ended) or `bytes=-N` (suffix) against a single-range-only parser without those cases handled.
- **Risk:** Per-chunk fsync throughput on very large books in individual mode (2 fsyncs/chunk). **Severity:** LOW **Trigger:** 1000+ chunk books on slow storage; correctness unaffected.
- **Risk:** Restore/walk interleaving micro-race. **Severity:** LOW **Trigger:** walk launch lands between the restore's active-run check and its commit; practically covered by single-connection API serialization.

---

### Honest Assessment of Capability Coverage

- **The carrier check is accurate at the backend-architecture level.** All 8 capabilities have a named backend carrier, the A1-r/A2-r convergence on a single progress-authority axis is genuine (they are now contractually identical everywhere else, exactly as claimed), and no carrier violates pipeline-only. The "one decision remaining" framing (progress authority) is correct and healthily narrow.
- **The check overclaims on capability 4.** "Existing `api_voices` already satisfies" is API-truth; the frontend editing surface for voice-config style/ref-audio/5-types and alias mapping has **no carrier in any refined approach**. Capability 4 is not "partial by design" — it is *backend-complete, frontend-unclaimed*, and the user-facing half is the very thing the L4 inventory listed as lost. This is the one capability a user would most directly experience as still-regressed.
- **Capability 1's per-span preview is partial-by-design in the default mode.** The carrier exists only in individual render mode; the default batch path delivers whole-book playback without per-span preview. That's an acceptable policy if stated — it is currently unstated, so a user would discover the absence silently.
- **Capability 7's carrier is mostly sound but one sub-item is unpinned.** Saved scripts (snapshots), undo (value-restore + snapshots), contextual review (A3-r payloads), pause-after, and sequence playback all have working carriers. **Single-speaker** shifted from a book-level flag to "snapshot-variant" and must be pinned to a mechanism that survives re-walks before the DD is written.
- **What genuinely needs human judgment (unchanged from Turn 2):** the 0.5–0.7 band calibration (still constrained, still uncalibrated); whether individual mode should become the default render path so per-span preview actually exists; and whether the retention policy should pin snapshot-referenced audio (product call: disk vs. snapshot durability).

---

## Implementation Patterns

*Turn 5 — RnD-Improver. Concrete implementation patterns for the surviving approaches (A1-r/A2-r/A3-r/A4-r), each with web-cited production evidence. These are implementation patterns, not new approaches: they assume the refined approaches as settled and answer "how is this actually built." Organized by pattern area, not by approach. No code — schema sketches and endpoint signatures only.*

### P1: Schema-Native Jobs with Transaction-Boundary Discipline

- **Pattern:** Four new tables in the pipeline schema, written through the existing `PipelineStorage` adapter, plus a `transaction()` context manager that mechanizes the "same transaction" claim (Surviving Concern 2):

  - `render_job(job_id TEXT PK, book_id TEXT, mode TEXT CHECK(batch|individual), status TEXT CHECK(pending|running|completed|failed|cancelled|interrupted), error TEXT NULL, output_dir TEXT, output_artifact_path TEXT NULL, created_ms INTEGER, started_ms INTEGER NULL, finished_ms INTEGER NULL)`
  - `render_chunk(job_id TEXT, idx INTEGER, status TEXT CHECK(pending|done|failed), wav_path TEXT NULL, error TEXT NULL, PRIMARY KEY(job_id, idx))` — **rows written only in individual mode**; `done` is set only when the WAV exists and is fsynced. In batch mode `tts.generate_batch` has no per-chunk callback (tts.py is untouchable), so no per-chunk rows are written; batch progress is job-level transitions only (see P4 for the explicit mode contract).
  - `walk_run(book_id TEXT, walk_name TEXT, run_id TEXT PK, status TEXT CHECK(pending|running|completed|failed|interrupted|cancelled), cancel_requested INTEGER DEFAULT 0, heartbeat_ms INTEGER NULL, result_json TEXT NULL, error TEXT NULL, created_ms INTEGER, finished_ms INTEGER NULL)` — replaces the in-memory `WalkRunner._status` dict. `run_id` is the run-scoped cancellation key.
  - `walk_review_item(id TEXT PK, book_id TEXT, run_id TEXT, kind TEXT CHECK(voice_profile|voice_assignment|instruction), target_table TEXT, target_id TEXT, prior_value TEXT, status TEXT CHECK(pending|resolved|superseded|stale), created_ms INTEGER)` — see P3.

  **Transaction wrapper** (the mechanical fix for Concern 2): the adapter's `execute_*` methods already have the key property — `was_in_transaction = self._conn.in_transaction; ...; if not was_in_transaction: self._conn.commit()` — so a statement issued while a transaction is open joins it and does **not** commit. Mechanize the runner-level contract with:

  ```
  @contextmanager
  def transaction(self):
      if self._conn.in_transaction:      # nested call joins the outer transaction
          yield
          return
      self._conn.execute("BEGIN IMMEDIATE")
      try:
          yield
      except BaseException:
          self._conn.rollback()
          raise
      else:
          self._conn.commit()
  ```

  Because `isolation_level=None` (autocommit) is already the adapter's mode, the explicit `BEGIN IMMEDIATE` is legal and the `with storage.transaction():` block in each walk wraps exactly the unit's writes: e.g., in 2g, per-character `character_metadata` UPSERT + `walk_review_item` insert for the 0.5–0.7 band; in 2h, per-character `voice_assignment_id` update + item insert; in 2i, per-span `instruct` update + item insert. `BEGIN IMMEDIATE` (not DEFERRED) is required because these are read-modify-write sequences and the deferred start can upgrade to `SQLITE_BUSY` mid-transaction.

  **Reconciliation is startup-only** (addresses Concern 1): on app startup, before the API accepts requests, one pass sets `status='interrupted'` (with `error='interrupted by process restart'`) for every `render_job` and `walk_run` still `running`. **There is no on-read sweeper and no periodic heartbeat reaper.** The single-process deployment means process death is the only worker-death mechanism; a heartbeat threshold can only false-positive on long LLM walk units (multi-minute 2g/2i runs legitimately produce zero writes between walks). `walk_run.heartbeat_ms` is still written (once at run start, once per walk-unit transaction commit) so the startup reconciliation and any future diagnostics have data — it is never used to flip a live job.

  **Cancellation persistence:** `POST /cancel_walks` now sets `walk_run.cancel_requested=1` (persisted, survives restart) in addition to the in-memory flag; `WalkRunner` checks it between walks exactly as today (cancel between walks only — unchanged semantics, but now durable). Render cancel keeps the existing `cancel_event` (`_render_jobs` dict) and adds `render_job.status='cancelling'` so the UI can show it.

  **New endpoint placement (CONTRACTS module split):** `GET/POST/DELETE /api/export/jobs/{job_id}` and `GET /api/export/jobs/{job_id}/chunks` live in `api_export` (replacing the in-memory-only `render_status`/`cancel_render`); `GET /api/walks/{book_id}/runs` lives in `api_walks`. `download/{job_id}` is rewritten on top of `render_job` rows instead of the `_render_jobs` dict.

- **Why for this design:** A1-r's schema-native state replaces three in-memory dicts (`_render_jobs`, `WalkRunner._status`, `WalkRunner._cancelled`) that vanish on restart and are invisible to tests; the `transaction()` wrapper turns A3-r's "same transaction" assertion into a mechanism the runner controls (Concern 2); startup-only reconciliation is the exact shape demanded by Concern 1 — it reconciles the only real failure mode (process death) and cannot false-positive on live multi-minute runs. It also gives capability 2 (progress + cancellation) a durable substrate and capability 7's undo a `prior_value` home.

- **Production evidence:**
  - https://wiki.r-that.com/patterns/sqlite-job-queue/ — "Startup cleanup. Jobs stuck in running after a crash never finish. On boot, reset any running rows" and "Don't run the poller and long-running handlers in the same SQLite transaction… Claim in one transaction; process in application code; update status in a second transaction" — this is precisely the startup-only reconciliation + short-transaction discipline this pattern encodes.
  - https://dev.to/nasrulhazim/the-reconciler-pattern-when-a-queued-job-simply-never-runs-1k29 — the Reconciler Pattern: sweep predicates are `stale AND unclaimed`, fresh pending rows must be left alone, and the sweep must be idempotent; it motivates *when* reconciliation may run at all and why startup-only is the safe subset for a single-process system.
  - https://docs.bullmq.io/guide/jobs/stalled — BullMQ's two-phase stalled-checker exists specifically to avoid false-positive recovery of slow-but-live jobs; the surviving-concern analysis cited it against heartbeat-based on-read sweeping, and this pattern adopts its conclusion (no periodic reaper).
  - https://sqlite.org/lang_transaction.html and https://stackoverflow.com/questions/15856976/ — the canonical `isolation_level=None` + explicit `BEGIN IMMEDIATE` + `with conn:` idiom: the `with` block commits/rolls back on exit, `BEGIN IMMEDIATE` prevents deferred-upgrade `SQLITE_BUSY`, and "an attempt to invoke the BEGIN command within a transaction will fail" is why the wrapper checks `in_transaction` before issuing BEGIN. https://docs.python.org/3/library/sqlite3.html documents `isolation_level=None` as autocommit mode — the mode the adapter already uses.

### P2: Artifact-First Run Directory with Reference-Aware Retention GC

- **Pattern:** Every render job gets a stable run directory under a configured `RENDER_ROOT` (replacing `tempfile.mkdtemp` under `/tmp`, which today makes render output vanish on reboot and is invisible to retention): `<RENDER_ROOT>/book-<book_id>/<job_id>/`. Inside it:

  - `manifest.json` — the artifact record: `{schema_version, job_id, book_id, mode, batch_seed, voice_configs: {speaker: {type, voice, alias_of}}, chunks: [{idx, wav, text_hash}], assembled: {m4b, mp3, audacity}}`. Written atomically after each chunk completes (individual mode) or once at job completion (batch mode): write `manifest.tmp` → `fsync(tmp_fd)` → `rename()` → `fsync(parent_dir_fd)`. Numbered generations `manifest.N.json` + `CURRENT` symlink are optional; the single-file write-tmp-rename-fsync sequence is the minimum bar and is simpler to reason about.
  - `run.stop-request.json` — run-id-scoped cancel sentinel (A2-r / Cross-5): content embeds `run_id`; a sentinel with a stale run_id is ignored and deleted on finalize. For renders the existing `cancel_event` is the in-process mechanism; the sentinel makes cancellation durable across restart.
  - `audiobook.m4b`, `audiobook.mp3`, `audacity/` — assembled artifacts written under the same fsync discipline.

  **Reference-aware retention GC** (addresses Concern 3): the sweeper must not delete WAVs that `project_snapshot` references. Deletion eligibility is derived, not enqueued: build the union of paths referenced by (a) live `render_job.output_artifact_path`, (b) `render_chunk.wav_path` rows, (c) every retained `project_snapshot` manifest's artifact references; anything on disk under `RENDER_ROOT` not in that union is a candidate; apply the retention-grace gate (`now - mtime > retention_days`) and **re-check references immediately before physical deletion**. Sweep runs on a schedule (startup + hourly, or daily), never in a hot request path.

- **Why for this design:** A2-r's artifact-first discipline gives capability 1's audio surface a durable, nameable source (per-span preview URLs and download endpoints point at stable paths instead of ephemeral `/tmp` dirs) and capability 3's export a directory to assemble from. The reference-derived eligibility rule is the direct fix for Concern 3: a snapshot's manifest participates in the reference union, so GC cannot degrade an old snapshot to "audio missing — re-render" without the product explicitly choosing snapshot pinning. This is the "retention policy vs snapshot durability" product call the Turn 4 assessment flagged; the pattern makes either choice implementable (pin = include snapshot refs in the union; don't pin = exclude them).

- **Production evidence:**
  - https://github.com/facebook/rocksdb/wiki/How-we-keep-track-of-live-SST-files — RocksDB keeps live files alive by reference count across `version`s; "If a file's reference count drops to 0, the file can be deleted" — the same derive-eligibility-from-references model (a snapshot's manifest is the analogue of a retained version).
  - https://github.com/salesforce-misc/merutable/issues/11 — a production storage engine issue that directly matches Concern 3: "Deletion eligibility must become a function of per-file reference counts across the retained snapshot chain" — files referenced by any retained snapshot are live, period; eligibility is a three-condition gate (no retained snapshot references it, no pin covers it, retention grace elapsed). The pattern's union-of-references sweeper is this model.
  - https://juicefs.com/en/blog/engineering/juicefs-garbage-collection — JuiceFS GC: reference-counted slices, delayed/trash retention, final re-check before deletion; "Slices whose reference count reaches zero are marked as pending cleanup" — the two-stage (soft eligibility → final recheck → physical delete) shape the sweeper uses.
  - https://cottoncloud.dev/architecture/storage-lifetime-contract — "Snapshots, versions, shares, and trash can retain references after the current layout changes… The cleanup job can reason about live content without trusting raw backend listing alone" — exactly the DB-as-authority-for-liveness position this pattern takes.
  - https://netflixtechblog.medium.com/navigating-the-netflix-data-deluge-the-imperative-of-effective-data-management-e39af70f81f7 — Netflix's mark-and-sweep media GC with soft-delete markers and lifecycle policies; the reference-check-before-delete ordering.
  - https://0xkiire.com/crash-consistency-fsync-rename/ and https://aalhour.com/posts/beachdb-the-syscall-i-forgot/ — the write→fsync→rename→fsync-dir discipline for atomic manifest commits (both are the standard crash-consistency references for this exact sequence).

### P3: Unified Review Queue — Honest Union + Supersede, Single-Speaker Pinned

- **Pattern:** One read path, two sources (A3-r's honest union):

  - **Junction items stay a live query.** `GET /api/review/{book_id}` keeps returning `ReviewManager.get_review_items(book_id)` for junction-sourced items (≥0.5 and <0.7, unchanged); ReviewManager continues writing confidence/human_override onto junction rows. No mirroring.
  - **Walk-side items are materialized.** Walks 2g/2h/2i write `walk_review_item` rows **inside the same `storage.transaction():` block** as their junction/metadata writes (P1's wrapper makes this mechanical). Item payload mirrors what the walk already computed: `kind`, `target_table`/`target_id` (e.g. `character_metadata:character_id`, `span:id`), and `prior_value` — the value being replaced, which is the undo source (capability 7's undo-delete/undo-review).
  - **Supersede on re-walk:** when a walk starts, one UPDATE in the same transaction sets all prior `pending` items for `(book_id, kind, target_id)` to `superseded`; new items are inserted as `pending`. Resolution (accept/reject/override) updates the underlying target (character_metadata value, voice_assignment_id, span.instruct — all existing write paths) and flips the item to `resolved` in the same transaction. Item lifecycle states: `pending → resolved | superseded | stale`.
  - **Thresholds** stay centralized (`REVIEW_CONFIDENCE_MIN/MAX`); no ×0.8, no degraded-confidence auto-accept (hard constraint).
  - **Single-speaker pinned to a book-level flag** (addresses Concern 6): restore `book.single_speaker INTEGER DEFAULT 0` as a real schema column on the `book` table (survives 2h re-walk because it is book-scoped data, not a snapshot-variant). The assembly boundary (`export_annotated_script`) is the single enforcement point: when the flag is set, every entry's `speaker` is mapped to the NARRATOR voice id at the `_build_voice_config`/`_build_chunks` boundary — the mechanism the Turn 1 design had, restored verbatim. 2h re-walk reassigns characters but never touches the flag.

- **Why for this design:** A3-r's honest union with supersede gives capability 5 (2g/2h/2i low-confidence review items reaching the UI) a durable, testable path — today `execute()`'s return dict with `profiles_for_review`/`assignments_for_review`/`instructs_for_review` counters is discarded (L5 finding). `prior_value` gives capability 7's single-item undo a transactional basis. The single-speaker flag pinning is the smallest change that satisfies Concern 6: a book-level column enforced at one assembly boundary, exactly what the regression inventory showed the pre-rewrite system had (`generate_script --single-speaker`).

- **Production evidence:**
  - https://docs.confluent.io/kafka/design/log_compaction.html — Kafka log compaction's "a key's latest value wins, older values become eligible for deletion" is the same supersede semantics as the re-walk UPDATE; a re-walk compacts the review item stream the way a log compactor does.
  - https://docs.segments.ai/background/label-queue-mechanics — a production label-queue system where review items are materialized with status transitions and re-labeling supersedes prior items; the pending→resolved lifecycle mirrors their queue mechanics.
  - https://docs.bullmq.io/guide/jobs/stalled (reused) — durable, stateful job records rather than in-memory dicts, the substrate the walk_run/walk_review_item rows provide.

### P4: Per-Span Preview — Explicit Mode Contract + Singleton Frontend Player

- **Pattern:** Per-span preview is a function of render mode, made **explicit in the API contract** rather than silently absent (addresses Concern 5):

  - **Individual mode** (`use_batch=False`): `render_audiobook` writes one `render_chunk` row per chunk (status `pending` → `done` with `wav_path`), so `GET /api/export/jobs/{job_id}/chunks` returns per-chunk paths and per-span preview is fully wired: `GET /api/export/chunk/{job_id}/{idx}` serves the WAV with bounded range support (P7). The editor's per-span preview button points at that URL and seeks to the span's offset.
  - **Batch mode** (`use_batch=True`, default): `generate_batch` has no per-chunk callback, so the contract states **per-span preview is unavailable; playback is whole-book** via `GET /api/export/audio/{job_id}`. The `render_chunk` table has no rows; `render_status` reports job-level progress (pending/running/completed) and completion is marked by the assembled artifact existing (fsync'd). This is the honest-mode contract the Turn 4 assessment demanded be stated; the UI renders per-span preview affordances only when `chunks` are present in the job status payload.
  - **Frontend:** a single shared `HTMLAudioElement` singleton (module-level, injected for tests), reused for whole-book playback, per-span preview, and voice preview: starting any preview stops the current one (one element, one thing playing). Per-span preview seeks the shared element to the span's start offset — the range-capable server (P7) makes the tail seekable without downloading the whole file. This also subsumes the existing `previewVoice` (`new Audio(url).play()`) in `voices.ts`.

- **Why for this design:** Gives capability 1 (result/audio surface + per-span preview) a concrete implementation on both render paths, and surfaces Concern 5's batch-mode limitation as an explicit contract field instead of an unstated regression. The singleton player also serves capability 7's sequence playback (queue of span offsets on the same element) and capability 4's voice preview without new state machinery.

- **Production evidence:**
  - https://github.com/mauricekleine/fluncle/blob/main/apps/web/src/lib/preview-player.ts — a production web app's singleton preview player: one shared element so starting a preview anywhere stops the one already playing, `preload="none"`, error degrades to idle — the exact pattern for the editor's many previewable rows.
  - https://developers.soundcloud.com/blog/playback-on-web-at-soundcloud/ — SoundCloud's playback engineering: the `<audio>` element as the base player, seeking handled via position requests against range-capable servers; the pattern for seekable per-span playback.
  - https://developer.mozilla.org/en-US/docs/Web/Media/Guides/Audio_and_video_delivery/buffering_seeking_time_ranges — MDN on seeking and byte-range requests: "Byte range requests allow parts of the media file to be delivered from the server and so can be ready to play almost immediately — thus they are seekable" — why the P7 range handling is the enabling backend for per-span seeking.

### P5: Overlay Config — Recursive Round-Trip Preservation + Per-Walk Override Resolution

- **Pattern:** Two coordinated pieces (A4-r):

  1. **Config save preserves unknown keys recursively** (fixes the L4/L5 data-loss: today `POST /api/config` validates `AppConfig(llm+tts only)` and `json.dump(config.model_dump())` strips `generation`/`prompts` keys). New save path: parse the incoming raw JSON → run known fields through `AppConfig` validation → walk the raw JSON and **carry forward every path not consumed by validation, at every nesting depth** → merge with the existing on-disk config (patch-not-replace) → stamp `schema_version` → write atomically (tmp + fsync + rename, per P2). Endpoint signature unchanged: `POST /api/config` accepts the raw body; the response returns the full merged config so the UI round-trip is lossless.
  2. **Per-walk overrides are resolved through one helper** (fixes the L5 dead param): replace the dead `config` parameter plumbing with `resolve_task_config(task_name, storage, book_id)` in `app/utils.py` — the single shared resolution path that (a) reads on-disk `config.json`, (b) applies `llm.task_overrides[task_name]`, (c) overlays any `walk_override(book_id, walk_name, key, value)` rows from the new table. All 9 walks and `test_resolve_task_llm.py` migrate to it (A4-4 blast radius contained in one helper). The `walk_override` table is `(book_id, walk_name, key, value_json, PRIMARY KEY(book_id, walk_name, key))`; capability 6's optional prompt/generation-param overrides ride on it without reintroducing legacy endpoints — the override surface is `value_json` (e.g. `{"temperature": 0.2, "prompt_override": "..."}`) passed through the helper into each walk's `llm_config` and per-call prompt assembly.

- **Why for this design:** A4-r's overlay + snapshot model needs the save path to stop destroying data before overrides can layer on top of it. This pattern is the mechanical core of capability 6 (optional prompt + generation-param overrides without legacy duplication): the round-trip fix restores the pre-rewrite `default_prompts`/generation-params that the L4 inventory listed as lost, and the single resolution helper gives capability 6 one narrow, testable seam instead of 9 divergent call sites.

- **Production evidence:**
  - https://github.com/pydantic/pydantic-settings/commit/61e0b46 — pydantic-settings' `deep_merge` for config-file sources: the test in that commit proves the default merge is **shallow** and nested unknown keys are lost without deep merge — the exact failure mode of the current `model_dump()` round-trip, and why the raw-JSON carry-forward must be recursive.
  - https://protobuf.dev/programming-guides/proto3/ — protobuf's unknown-field preservation: parsers retain fields they don't understand and re-emit them on serialization, so a newer writer and older reader interoperate without loss — the canonical "unknown keys survive a config round-trip" precedent.
  - https://github.com/worktrunk/worktrunk/pull/2180 — a production PR preserving unknown config keys across save (cited by the Turn 3 research for A4-r); same motivation: validate what you own, carry what you don't.
  - https://docs.pydantic.dev/latest/concepts/pydantic_settings/ — documents that validation of known settings coexists with source-file merge semantics, the layering this save path implements.

### P6: Polished M4B/MP3/Audacity Export via FFMETADATA1 + Concat Discipline

- **Pattern:** `POST /api/export/m4b` (new, in `api_export`, alongside the existing `/merge`) builds the M4B in three phases against the job's run directory (P2):

  1. **Concatenate** chunk WAVs to an intermediate AAC/m4a: prefer the **concat filter** (`-filter_complex '[0:a][1:a]...concat=n=N:v=0:a=1[a]' -map '[a]' -c:a aac -b:a 96k`) over the concat **demuxer** when chunk sources are heterogeneous (WAV/mp3/m4a mix from individual mode), because the demuxer requires identical codecs/timebases and fails otherwise; the demuxer (`-f concat -safe 0 -i list.txt`) remains the fast path for homogeneous inputs. Add `-fflags +genpts -avoid_negative_ts make_zero` to head off non-monotonic timestamps, and `-movflags +faststart` for web/player compatibility.
  2. **Embed metadata + chapters via FFMETADATA1** (`-f ffmetadata`): global keys `title`, `artist`, `album`, `composer=<narrator>`, `date`, `publisher`; `[CHAPTER]` blocks with `TIMEBASE=1/1000`, `START=`, `END=` (from per-chunk durations or span offsets), `title=` per chapter. Invoke as `ffmpeg -i audio.m4a -i metadata.txt -map 0:a -map_metadata 1 -map_chapters 1 -c copy -movflags +faststart out.m4b`. Cover art: `-i cover.jpg -map 1 -c:v copy -disposition:v attached_pic` (or `-metadata:s:v title="Album cover" comment="Cover (front)"` for player compatibility). **Justification vs m4b-tool:** m4b-tool wraps ffmpeg+mp4v2 in PHP and auto-embeds cover/description, but adds a PHP 7.4+ runtime + two binaries to deploy; FFMETADATA1 is stdlib-of-ffmpeg, needs nothing new, and is what the pre-rewrite `/api/merge_m4b` equivalent did. Keep the existing `POST /merge` concat-only path for quick merges; the new endpoint is the polished path.
  3. **MP3 + Audacity** are derived artifacts in the run directory: MP3 = decode the m4b or concatenate with `-c:a libmp3lame -q:a 2`; Audacity = the existing `ZIP_STORED` bundle of per-chunk WAVs, rebuilt to reference the run directory instead of `/tmp`.

  **FFmpeg failure surface:** every subprocess call captures `stderr`; on non-zero exit the endpoint returns 422/500 with `{detail, stderr_tail: stderr[-500:], exit_code}` and the job row transitions to `failed` with the same error string (P1's `error` field) — the same shape the existing 300s-timeout `/merge` uses, extended to all three phases.

- **Why for this design:** Capability 3's polished export (metadata/cover/chapters) is currently unimplemented — `/merge` is ffmpeg-concat-only (L4 finding). This pattern makes the export a pure function of the P2 run directory, so it composes with retention, re-render, and the artifact manifest, and keeps the ffmpeg invocation surface to one well-tested module.

- **Production evidence:**
  - https://fmartingr.com/blog/2024/03/12/create-an-audiobook-file-from-several-mp3-files-using-ffmpeg/ — a production write-up of exactly this pipeline: concat → add cover → m4a → `-i metadata.txt -map 0 -map_metadata 1 -c copy` for an Apple-Books-playable M4B, with FFMETADATA1 `[CHAPTER]` blocks and `TIMEBASE=1/1000`.
  - https://ffmpeg.org/ffmpeg-formats.html — the official ffmpeg formats doc: §3.5 concat demuxer ("All files must have the same streams (same codecs, same time base, etc.)") and §4.32 ffmetadata (the `[CHAPTER]` block grammar) — the two authoritative references for the concat-filter-vs-demuxer decision and the metadata format.
  - https://github.com/sandreas/m4b-tool — the m4b-tool project documenting the same merge/chapterize workflow; its existence is the evidence for the "m4b via ffmpeg metadata" approach being the community-standard shape, while its PHP+mp4v2 runtime requirements justify not adopting it as a dependency.

### P7: Bounded-Range Audio Serving with Version Guard

- **Pattern:** Audio endpoints serve range-capable responses with a **bounded, single-range parser and a CI-enforced dependency floor**:

  - Primary: **Starlette `FileResponse`** (used by the existing `/download/{job_id}`) with `media_type='audio/mp4'|'audio/mpeg'|'audio/wav'` — it implements `Accept-Ranges: bytes`, 206, and 416 natively. **CI guard:** pin `starlette>=0.49.1` and add a CI check (as in `npm run build && git diff --exit-code` style) asserting the locked version — because GHSA-7f5h-v6xp-fcq8 (CVE-2025-62727) is an O(n²) Range-header parsing DoS fixed in 0.49.1; the guard prevents the dependency from silently dropping below the fixed line.
  - Fallback (if a route needs a bounded parser for a non-FileResponse path, e.g. serving from a ZIP or a virtual path): a hand-written parser that accepts exactly the three legal forms — `bytes=start-end`, `bytes=start-` (open-ended; end = file_size−1), `bytes=-suffix` — returns 416 for anything else, and never splits into multiple ranges (multiple ranges → 416; a player that needs multi-range can re-request). No regex-free O(n) scan of unbounded header input: cap header length before parsing.

- **Why for this design:** Capability 1's playback and per-span seeking depend on range requests (P4); the existing `/download` already serves audio but with zero tests and no explicit range story. The version guard converts the security advisory into a CI-enforced invariant, and the hand-written parser covers the edge cases the Turn 4 risk list flagged (`bytes=0-`, `bytes=-N`).

- **Production evidence:**
  - https://www.starlette.io/responses/ — official Starlette docs: `FileResponse` "supports HTTP range requests" and returns 206/416 — the primary mechanism, no custom code required.
  - https://github.com/Kludex/starlette/security/advisories/GHSA-7f5h-v6xp-fcq8 — the advisory for the O(n²) Range DoS fixed in 0.49.1; the CI guard exists because of this specific advisory.
  - https://github.com/fastapi/fastapi/issues/1240 — the community's canonical hand-written range helpers (`send_bytes_range_requests`, `_get_range_header`) demonstrating the open-ended/suffix cases and 416 handling — the template for the fallback parser.
  - https://github.com/weaties/helmlog/blob/main/src/helmlog/routes/audio.py — a production FastAPI app streaming WAV via `FileResponse(path, media_type='audio/wav')` and relying on Starlette's range handling for seekable playback.

### P8: Testing Strategy — Dependency Overrides + InMemorySQLiteAdapter + Vitest Mocks

- **Pattern:** Three layers, following and extending the existing `tests/pipeline/*` conventions:

  1. **Backend unit/integration:** the existing `InMemorySQLiteAdapter()` fixture is the base, extended with the new tables. New fixtures: `client` per module — `TestClient(app)` with `app.dependency_overrides[get_storage] = lambda: storage` (the api_onboard `_storage` provider is already overrideable per CONTRACTS); teardown **clears** `app.dependency_overrides` in a fixture (never at test-body end) so overrides cannot leak between tests; and per-test fresh adapter (`init_db()` per test) because a single-connection `:memory:` DB is the whole database — sharing it across tests silently accumulates state (the known FastAPI/SQLite-in-memory gotcha).
  2. **The zero-test `download/{job_id}` gap is closed explicitly:** a new `tests/pipeline/test_export.py` with cases for (a) m4b present → FileResponse 200 + `content-type: audio/mp4`; (b) m4b absent → zip 200 with ZIP_STORED members; (c) unknown job → 404; (d) range request → 206 + `Content-Range`; (e) malformed range → 416. Plus transaction-wrapper tests: `storage.transaction()` commit on success, rollback on exception, nested-join behavior (P1); startup reconciliation tests: seed `running` rows → call `reconcile_stale_runs()` → assert `interrupted`.
  3. **Frontend (vitest):** the existing `frontend/tests/frontend/test_*.test.ts` files already run under vitest+jsdom (installed at `frontend/package.json`). For the new player/cancel/progress UI: mock `global.fetch` (or MSW handlers) per test, keep module-level audio singleton injectable (a `createPreviewPlayer()` factory the module wires once), and assert on the singleton's state transitions (start stops previous, error → idle) rather than on network internals. For `editor-pipeline.ts` polling, use `vi.useFakeTimers()` + advance timers, asserting the 2s poll loop issues exactly one request per tick and stops on terminal status.

- **Why for this design:** Capability 8 (preserve pipeline-only architecture) is enforced by tests: the override seam means every new endpoint is tested against the real pipeline schema in memory, and `test_legacy_removed.py` keeps guarding the 29-endpoint 404 gate. The `download` gap is the single most dangerous untested surface in the current code (file serving + zip building + error paths); this pattern names it as a first-class test target. Frontend tests follow the conventions the cutover already established (spec-first, vitest-compatible, jsdom).

- **Production evidence:**
  - https://fastapi.tiangolo.com/advanced/testing-dependencies/ — official FastAPI guidance for `app.dependency_overrides` in tests; the dict-keyed-by-callable mechanics and the reset-before/after-test discipline.
  - https://fastapi-patterns.com/core-architecture-routing-patterns/dependency-injection-strategies/overriding-dependencies-in-tests/ — production-derived rules: overrides are application state that outlives tests; register in fixtures, clear in teardown; assert on behavior change, not on the dict; keep one integration-level test running the real graph — the discipline this pattern's fixture encodes.
  - https://github.com/tiangolo/fastapi/issues/3906 — the documented SQLite-in-memory testing failure and its resolution (shared single connection / per-test fresh DB): the exact gotcha `InMemorySQLiteAdapter` faces, and why per-test `init_db()` isolation matters.
  - https://vitest.dev/guide/mocking/modules — official vitest module-mocking (`vi.mock`, `vi.spyOn`) semantics for the `fetch`/polling mocks; https://github.com/seratch/easier-typescript-api-testing-with-vitest-msw-4k3a (community) demonstrates the MSW-handler route for HTTP-heavy frontend modules.

---

### Pattern Coverage Check

| Pattern | Serves capabilities | Addresses surviving concerns | Notes |
|---|---|---|---|
| P1 Schema-Native Jobs + Transaction Discipline | 2 (progress+cancel), 5 (walk items substrate), 7 (undo/prior_value), 8 | **C1 (startup-only reconciliation), C2 (BEGIN IMMEDIATE wrapper)** | Replaces 3 in-memory dicts; durable cancel |
| P2 Artifact-First Run Dir + Reference-Aware GC | 1 (stable audio paths), 3 (export source), 8 | **C3 (snapshot-aware retention)** | Derive deletion eligibility from references |
| P3 Unified Review Queue + Supersede + Single-Speaker | 5 (2g/2h/2i items), 7 (undo, contextual review) | **C6 (single-speaker pinned to book flag)** | Junction = live query; walk items = materialized rows |
| P4 Per-Span Preview Mode Contract + Singleton Player | 1 (per-span preview), 7 (sequence playback), 4 (voice preview) | **C5 (batch-mode dependence made explicit)** | State the contract; wire preview only when chunk rows exist |
| P5 Overlay Config + Recursive Round-Trip | 6 (overrides), 8 | — | Fixes L4/L5 config data-loss; one `resolve_task_config` seam |
| P6 FFMETADATA1 M4B/MP3/Audacity Export | 3 (metadata/cover/chapters) | — | Concat filter for heterogeneous; demuxer fast path |
| P7 Bounded-Range Audio Serving | 1 (seekable playback) | — | CI-enforced starlette≥0.49.1 |
| P8 Testing Strategy | 8 (pipeline-only guardrails) | — | Closes zero-test `download/{job_id}` gap |

**Capabilities with NO dedicated pattern — flagged:**
- **Capability 4 (voice config editing UI — Concern 4):** still **unclaimed**. The backend carrier exists (`PUT /api/pipeline/voices/{id}`, CRUD+preview in `api_voices`), but no refined approach and no pattern above designs the frontend surface (style/ref-audio/5-type editing, alias mapping). `voices.ts` has `createVoiceCard` (name + type badge + preview only) and no form modal. This is a **frontend design gap, not an implementation pattern gap** — it needs a small dedicated frontend design task (form + PUT wiring + alias-map picker), deliberately out of scope for these backend-first patterns. Flagged per Turn 4.
- **Capability 7's saved named scripts (L4: `/api/scripts*` lost):** partially covered — P3 gives undo, P4 gives sequence playback, A4-r snapshots give save/restore — but the *named, listable script UI* has no pattern. The snapshot manifest (A4-r) is the natural carrier; the naming/listing surface is UI work. Flagged as residual.

**Concerns fully addressed:** C1 (P1), C2 (P1), C3 (P2), C5 (P4), C6 (P3). **C4 (capability 4 frontend) — no pattern, flagged above.**

**Constraint compliance:** all patterns respect pipeline-only (no legacy endpoints; new endpoints placed per CONTRACTS module split: `api_export` for jobs/chunks/export, `api_walks` for runs, `api_review` for walk-side items), tts.py is untouched (P4's batch-mode limitation is the direct consequence of that constraint and is stated as a contract, not worked around), walks 2g/2i remain strictly serial (P1 keeps WalkRunner's serial guard, now persisted), and no degraded-confidence auto-accept is introduced (P3 keeps centralized thresholds).

---

## Pattern Risks

> Appended by **rnd-counter-improver** (Turn 6). Pattern-level adversarial critique of P1–P8. Every risk cites real evidence; where evidence is my own analysis it is labeled **SPECULATIVE**.
> Context re-verified during this turn: `adapter.py` sets **no** `isolation_level` (legacy DEFERRED implicit mode; P1's claim that "isolation_level=None (autocommit) is already the adapter's mode" is factually wrong — the *behavior* is per-statement-commit, but via legacy semantics, not autocommit). Single shared connection (`get_storage()` singleton) used by API threads + background render/walk threads. Python 3.13.5, sqlite 3.46.1, starlette 1.3.1, ffmpeg 7.1.5.

### Risk Verdict Summary

| Pattern | Top risks | Worst severity | Verdict |
|---|---|---|---|
| P1 Schema-Native Jobs + Transaction Discipline | Shared-connection cross-thread txn join; LLM-inside-BEGIN IMMEDIATE freeze; isolation_level forward-compat | **HIGH** | VIABLE WITH CHANGES |
| P2 Artifact-First Run Dir + Reference-Aware GC | GC/row dangling refs; dual progress authority; TOCTOU vs in-flight restore | **MEDIUM** | VIABLE WITH CHANGES |
| P3 Unified Review Queue + Supersede + Single-Speaker | Supersede-at-start loses candidates on failed walk; dual-path item_id namespace; enforcement-point ambiguity | **HIGH** | VIABLE WITH CHANGES |
| P4 Per-Span Preview + Singleton Player | play()-interrupted rejection; seek-before-loadedmetadata; jsdom not-implemented play() | **MEDIUM** | VIABLE WITH CHANGES |
| P5 Overlay Config Round-Trip | model_dump() default-bloat breaks "removal" UX; extras dropped unless extra='allow'; raw-JSON merge discipline | **MEDIUM** | VIABLE WITH CHANGES |
| P6 FFMETADATA1 M4B/MP3 Export | Chapter END rounding accumulation; concat-filter sample-rate homogeneity; ffmetadata failure modes | **MEDIUM** | VIABLE WITH CHANGES |
| P7 Bounded-Range Audio Serving | Missing-file → 500 RuntimeError; fallback semantics diverge from FileResponse | **MEDIUM** | VIABLE |
| P8 Testing Strategy | Fake-timers + fetch hang; jsdom play() stubs; :memory: vs WAL divergence | **MEDIUM** | VIABLE WITH CHANGES |

**No BLOCKING risks found.** The patterns are strong; the HIGH risks are all fixable with mechanical changes (guards + ordering), not architectural rework.

---

### P1: Schema-Native Jobs with Transaction-Boundary Discipline

#### P1-1: Cross-thread writes silently join the walk's transaction and are rolled back with it
- **Risk:** P1's `transaction()` uses `in_transaction` to decide join-vs-BEGIN. On the **single shared connection** (`get_storage()` singleton), any other thread's `execute_insert/update/delete` while a walk transaction is open sees `in_transaction=True`, skips its own commit (the `was_in_transaction` mechanic), and its write **joins the walk's transaction**. If the walk rolls back, the other thread's write (e.g., a user's review accept, a voice assignment) is rolled back too; if the walk commits, the write commits with it — **with no isolation** (sqlite.org/isolation.html: "no isolation between operations within the same database connection"; operations from multiple threads interleave in execution order).
- **Trigger conditions:** Any P1-wrapped walk-unit transaction open while a concurrent API request performs a write. Alexandria's frontend polls `render_status` every 2s and review/voice endpoints are live — **these requests arrive while walks run**.
- **Matches Alexandria because:** YES. `complete_walk` and walk modules already run on the shared connection; the whole app is single-connection by design. This is not an exotic multi-connection scenario — it is the *only* topology Alexandria has.
- **Severity:** HIGH
- **Evidence:** [sqlite.org/isolation.html](https://www.sqlite.org/isolation.html) ("There is no isolation between operations within the same database connection" — statements from different threads interleave); [Python sqlite3 docs — check_same_thread](https://docs.python.org/3/library/sqlite3.html#sqlite3.connect) ("the connection may be accessed in multiple threads; write operations may need to be serialized by the user to avoid data corruption").
- **Mitigation:** transaction() must record the owning thread id and **raise if a non-owner calls execute_\* while a transaction is open** (fail fast instead of silent join), or give the render/walk thread its own connection. Without this, "transaction-boundary discipline" is unenforceable — the wrapper only governs callers that opt in.

#### P1-2: BEGIN IMMEDIATE wrapping a unit that contains LLM calls freezes the entire app's DB access
- **Risk:** `BEGIN IMMEDIATE` takes the RESERVED lock and, on a single serialized connection, the sqlite3 connection lock is held for the whole transaction. If a "walk unit" transaction wraps the LLM round-trip (SELECT batch → LLM (seconds–minutes) → UPSERT), every other thread's DB operation (render_status poll, review GET/POST, span edit) **blocks for the LLM duration** — an app-wide DB freeze, not SQLITE_BUSY.
- **Trigger conditions:** Walk modules 2a–2i are LLM loops. If P1's per-unit transaction is scoped around the LLM call rather than only the write phase, every unit freezes all API traffic.
- **Matches Alexandria because:** YES — walks are long (LLM-dominated) and the API must stay responsive (capability 2's live progress polling depends on it).
- **Severity:** HIGH
- **Evidence:** [Python sqlite3 docs — threadsafety](https://docs.python.org/3/library/sqlite3.html#thread-safety) (serialized build: API calls on one connection are mutex-serialized); [sqlite.org/lang_transaction.html](https://www.sqlite.org/lang_transaction.html) (BEGIN IMMEDIATE acquires RESERVED immediately; other connections' writes get SQLITE_BUSY). WAL's reader/writer concurrency is **across connections** — irrelevant inside the one connection Alexandria uses.
- **Mitigation:** Transaction scope must be **LLM-outside / write-inside**: do LLM calls first (no open txn), then a short BEGIN IMMEDIATE → UPSERTs → commit. The pattern's "heartbeat once per walk-unit transaction commit" implies a per-unit txn — fine, as long as the txn contains only the write phase.

#### P1-3: `isolation_level=None (autocommit)` claim is false today and the legacy-mode semantics flip at Python 3.16
- **Risk:** The pattern asserts autocommit is already the adapter's mode; in fact `adapter.py` passes no `isolation_level`, so it runs legacy implicit-transaction mode (`autocommit=LEGACY_TRANSACTION_CONTROL`). Today the per-statement-commit behavior masks the difference — but CPython's plan (gh-83638) is: deprecations in 3.14, **default flips to PEP-249 `autocommit=False` in 3.16**, where `commit()` implicitly opens a *new* transaction. The `was_in_transaction`/commit-skip mechanic then breaks: after every commit a fresh transaction stays open, every subsequent execute joins it, writes never reach disk until close, and `in_transaction` is permanently True — silently killing P1's nested-join logic.
- **Trigger conditions:** Python upgrade to 3.14 (`-Werror` CI trips on DeprecationWarning; positional `isolation_level` becomes keyword-only in 3.15 — adapter uses none, OK) and 3.16 (behavior flip). Alexandria currently pins 3.13.5.
- **Matches Alexandria because:** PARTIALLY — not a today-bug, but the adapter will be touched by P1 anyway, so the fix is free: set `isolation_level=None` **explicitly** at connect and add a pinned-version CI note.
- **Severity:** MEDIUM (forward-compat landmine, not current breakage)
- **Evidence:** [Python 3.13/3.14 sqlite3 docs](https://docs.python.org/3/library/sqlite3.html#sqlite3.connect) ("autocommit defaults to `LEGACY_TRANSACTION_CONTROL`... The default will change to `False` in a future Python release"; "isolation_level has no effect unless autocommit is LEGACY_TRANSACTION_CONTROL"); CPython gh-83638 transaction-control plan (3.14 deprecate, 3.16 flip — pending-removal docs confirm no 3.14 removal of legacy mode itself).
- **Mitigation:** Explicit `isolation_level=None` + a comment; treat "legacy implicit transaction + commit-skip" as the *implementation*, not "autocommit". Re-audit at each Python bump.

#### P1-4: Cancel-request durability + dual cancel mechanisms (DB row vs stop file) can half-cancel
- **Risk:** P1 persists `walk_run.cancel_requested`; P2 adds `run.stop-request.json`. Walk cancel is checked between walk units (and P1 says between walks — 2g/2i must stay serial), render cancel via `cancel_event`. If any code path checks only one mechanism, a cancel that landed in the other store is silently ignored — the "cancel" 200s but the walk keeps running until the next unit boundary at best, or completes at worst.
- **Trigger conditions:** User hits cancel while a walk is between units; render is mid-batch. The two stores can disagree on ordering (file write is not atomic with the DB row unless both go through the same transaction — they cannot, different media).
- **Matches Alexandria because:** PARTIALLY — capability 2 requires "useful cancellation incl. walk cancel"; the failure mode is a UX lie (cancel accepted, no effect), not data corruption.
- **Severity:** MEDIUM
- **Evidence:** [BullMQ stalled-jobs docs/issues](https://github.com/taskforcesh/bullmq) (dual-state cancel/stall flags are a recurring source of "cancel accepted but job ran" bugs — the pattern itself cites BullMQ's two-phase design; the lesson cuts both ways: single source of truth per cancellation, or one dispatcher that consults both).
- **Mitigation:** One `is_cancel_requested(run_id)` helper that reads the DB row **and** the stop-file, used by every check site; define which store is authoritative for walks (DB) vs render (event), and make the file a mirror, not a second truth.

---

### P2: Artifact-First Run Directory with Reference-Aware Retention GC

#### P2-1: GC deletes audio files but leaves `render_chunk` rows → per-span preview 404s/500s after retention
- **Risk:** GC eligibility is a union of references (live `render_job.output_artifact_path` + `render_chunk.wav_path` + snapshot manifests). But P2 deletes **files** without a stated plan to delete or tombstone the **referencing rows**. A completed job's rows remain after its run dir is GC'd (retention_days elapsed); P4's `GET /chunks` then returns paths that no longer exist, and the frontend renders preview buttons (rows say done) that fail on play.
- **Trigger conditions:** A job older than retention_days whose rows were never cleaned. Alexandria keeps completed jobs indefinitely today (no retention UX) — the first GC sweep will hit every old job dir.
- **Matches Alexandria because:** YES — capability 1's per-span preview is wired off these rows; the pattern's own retention_days default makes this the *steady-state* condition for old jobs, not an edge case.
- **Severity:** MEDIUM
- **Evidence:** [RocksDB live SST files wiki](https://github.com/facebook/rocksdb/wiki/Live-SST-Files) (reference-derived eligibility, as the pattern cites) — the corollary the pattern does not state: eligibility must be enforced on **both** sides of a reference, or the reference itself dangles. **SPECULATIVE for Alexandria's exact behavior** (no row-GC specified in P2): labeled as such.
- **Mitigation:** GC must delete-or-tombstone referencing rows in the same sweep (mark `render_chunk.status='evicted'` / null `wav_path`), and P4's chunks endpoint must 404 (not 500) on missing files (see P7-2).

#### P2-2: FileResponse raises RuntimeError (→500) when a GC'd/restored file is missing at serve time
- **Risk:** Starlette `FileResponse.__call__` raises `RuntimeError("File at path ... does not exist")` on `os.stat` failure — an unhandled 500, not a 404. Any race where GC (or a snapshot restore swap) removes a file between the row read and the serve → 500s on the audio/chunk endpoints.
- **Trigger conditions:** GC sweep (startup + hourly) racing a preview seek; the TOCTOU window in P2's "re-check references immediately before physical delete" is narrowed but not closed.
- **Matches Alexandria because:** PARTIALLY — single host, GC is infrequent, but the task brief explicitly names the snapshot-restore race, and a 500 (vs graceful 404) makes the failure loud and user-visible.
- **Severity:** MEDIUM
- **Evidence:** [starlette responses.py — FileResponse.__call__](https://raw.githubusercontent.com/encode/starlette/master/starlette/responses.py) (lines 347–352: `except FileNotFoundError: raise RuntimeError(...)` — verified against master).
- **Mitigation:** The chunk/audio endpoint should pre-check `os.path.exists` (or wrap FileResponse in a try/except) and return 404. This is a 3-line change in the P7 endpoint.

#### P2-3: Dual progress authority — `render_chunk` rows (P1) vs `manifest.json` (P2)
- **Risk:** Both P1's rows and P2's manifest record the same fact (chunk idx → wav_path), with different write ordering and crash windows. If the frontend reads progress from one and artifact refs from the other, a crash between "row committed" and "manifest updated" makes status say done while the manifest (and thus P4's chunk URLs or P6's concat source) says missing — or vice versa.
- **Trigger conditions:** Process kill / power loss between the two writes. Alexandria: single host, background render threads — crash windows are real but rare.
- **Matches Alexandria because:** PARTIALLY — divergence is bounded (GC union covers eligibility), but the *observability* divergence (status vs preview) is user-visible.
- **Severity:** MEDIUM
- **Evidence:** [0xkiire fsync-rename](https://github.com/0xkiichiro/fsync-rename) + [aalhour/beachdb](https://github.com/aalhour/beachdb) (both cited by P2): the pattern's own citations establish that ordering discipline across two records requires a **single write order**; P2 declares "manifest-only progress, listdir sanity only," which resolves it only if *all* readers (status endpoint, chunks endpoint, GC) agree rows are authoritative and manifest is derived. The pattern text lets both claim authority ("done set only when WAV fsynced" in P1; manifest "written... as progress" in P2).
- **Mitigation:** State one authority: **rows are truth, manifest.json is a derived cache** rebuilt on startup reconciliation; GC eligibility reads the union as designed. Cheap, removes the class of divergence bugs.

---

### P3: Unified Review Queue — Honest Union + Supersede, Single-Speaker Pinned

#### P3-1: Supersede-at-walk-start silently destroys review candidates when the re-walk fails
- **Risk:** P3 supersedes all prior pending items for (book_id, kind, target_id) in the transaction where the walk *starts*. If the walk then fails mid-run (LLM error, verification failure — WalkRunner already marks `failed` on exceptions), the old pending items are superseded but the new run only produced a *partial* set → **review candidates vanish without being reviewed or re-queued**.
- **Trigger conditions:** Any 2g/2h/2i re-walk that fails or is cancelled after the supersede transaction. Cancellation is a first-class capability (cap 2) — a cancelled re-walk is the *common* path, not the edge case.
- **Matches Alexandria because:** YES. Cancelled/failed re-walks are explicitly supported, and the whole point of walk_review_item is to not lose low-confidence items (cap 5).
- **Severity:** HIGH
- **Evidence:** [Kafka log compaction](https://kafka.apache.org/documentation/#compaction) (the pattern's cited model — "latest value wins"): compaction is *eventual and safe* because the log still holds the old values until the new one lands; superseding at start inverts this — you delete the old value *before* the new one exists. Segments.ai label-queue mechanics (pattern's other citation) likewise only finalize supersede on completion.
- **Mitigation:** Supersede at **completion** (and for cancelled runs: leave prior items pending), or supersede only the targets the new run actually regenerated. At minimum, on failure/cancel, restore the superseded items' status back to pending in the failure transaction.

#### P3-2: Junction live-query + materialized `walk_review_item` dual path needs an item_id namespace rule
- **Risk:** Junction items identify via `'junction_table:character_id:related_entity_id'` (3-part, `_parse_item_id` raises otherwise); `walk_review_item` rows have integer PKs. The unified queue must serve both; the accept/reject/override API dispatches on `item_id`. A collision or format ambiguity (e.g., a walk_review_item id rendered as a string that accidentally parses as 3-part, or the frontend sending an int for a junction item) causes wrong dispatch. Also, the junction query filters by `source LIKE '%walk_name%'` — a heuristic that will drift as walks rename.
- **Trigger conditions:** Any book that has both junction-origin and walk-side items in the queue (guaranteed: 2g/2h/2i produce walk-side items while character_* junctions from earlier walks still match the threshold).
- **Matches Alexandria because:** YES — the honest union *is* the feature; the namespace rule is load-bearing.
- **Severity:** MEDIUM
- **Evidence:** [app/pipeline/review.py — `_parse_item_id`](https://github.com/encode/starlette) — local code (verified in this session: `_VALID_JUNCTION_TABLES` + 3-part split with ValueError); **SPECULATIVE** on the actual collision probability (depends on final queue API shape): labeled as such.
- **Mitigation:** Namespace item_ids explicitly (e.g., `junction:` / `walkitem:` prefixes) at the API boundary; make the queue endpoint return a discriminated union; keep the junction filter on a stable column, not `source LIKE`.

#### P3-3: Retro-fitting walk modules to transactional semantics changes failure behavior (partial writes now roll back)
- **Risk:** Today, walk modules use per-statement auto-commit; a 2g run that processes 50 characters then fails leaves 50 persisted. Under P1's wrapper, that partial work rolls back — "safer" but a **behavior change** that can surprise re-walk logic, supersede bookkeeping, and the verification pass (which reads committed state).
- **Trigger conditions:** Any failing walk once wrapped. Not hypothetical: LLM calls fail; verification failures are a designed path.
- **Matches Alexandria because:** PARTIALLY — this is a semantic change, not a bug; but it interacts with P3-1 (supersede scope) and needs an explicit decision.
- **Severity:** MEDIUM
- **Evidence:** [sqlite.org/lang_transaction.html](https://www.sqlite.org/lang_transaction.html) (rollback discards *all* statements since BEGIN — atomicity is all-or-nothing, no partial-unit semantics); the adapter's `was_in_transaction`/commit-skip in app/pipeline/adapter.py (verified this session).
- **Mitigation:** Wrap at the *unit* granularity (per N spans/chars, matching the heartbeat cadence), and state the rollback semantics in the walk contract — the pattern names "transaction-boundary discipline" but doesn't specify unit size.

#### P3-4: Single-speaker enforcement point is ambiguous (export boundary vs build boundary)
- **Risk:** P3 text says enforcement "at `export_annotated_script` boundary" *and* "at `_build_voice_config`/`_build_chunks` boundary." These have different blast radii: inside `export_annotated_script`, the **spans export endpoint and the editor's speaker display** also flatten to NARRATOR (cosmetic regression for single-speaker books being edited); at `_build_*` only the render output is affected (editor keeps per-character attribution). Bypass paths: `export_annotated_script` is called by `render_audiobook` ✓, but the spans-export endpoint and any future consumer also call it.
- **Trigger conditions:** A single-speaker book loaded in the editor while a render is in flight, or the spans-export used for review context.
- **Matches Alexandria because:** YES — enforcement at the wrong boundary quietly degrades capability 7's contextual review for single-speaker books.
- **Severity:** MEDIUM
- **Evidence:** Local code: [app/pipeline/assembly.py — export_annotated_script](https://github.com/encode/starlette) (verified this session: speaker = character_name or 'NARRATOR'; no book.single_speaker read today; used by both render and spans export); **SPECULATIVE** on which boundary P3 ultimately pins: the pattern text contradicts itself.
- **Mitigation:** Pin ONE boundary. Recommended: map to NARRATOR in `_build_voice_config`/`_build_chunks` (render-only), keep `export_annotated_script` faithful so the editor and review context preserve real speakers.

---

### P4: Per-Span Preview — Explicit Mode Contract + Singleton Frontend Player

#### P4-1: "Starting any preview stops the current one" → `play()` interrupted by `pause()` rejects with AbortError
- **Risk:** The singleton flow — user clicks preview B while A is starting: `play(A)` (async, returns a promise) then `pause(A)` interrupts it; A's promise rejects with `DOMException: The play() request was interrupted by a call to pause()`. If the player code does `audio.play().catch(...)` this is handled; if it follows the *existing* `previewVoice` pattern (`new Audio(url).play().catch(...)`), the catch exists — but a `.catch` that treats any rejection as an error flips the UI to a broken state. Real-world regression suites (CorvinOS) explicitly guard "benign AbortError must not flip the player to error state."
- **Trigger conditions:** Fast clicking between previews; sequence playback starting the next span before the previous stop settles. Alexandria's editor has adjacent span rows — this is the *normal* interaction.
- **Matches Alexandria because:** YES — the singleton contract *designs* this sequence in.
- **Severity:** MEDIUM
- **Evidence:** [Chrome for Developers — "The play() request was interrupted"](https://developer.chrome.com/blog/play-request-was-interrupted) (play()→pause() rejection semantics); [CorvinOS AudioPlayer.test.tsx](https://github.com/CorvinLabs/CorvinOS/blob/main/core/console/corvin_console/web-next/tests/unit/components/AudioPlayer.test.tsx) (production regression guards for AbortError/NotAllowedError).
- **Mitigation:** Catch and classify: ignore `AbortError`/`NotAllowedError`, surface everything else; serialize start/stop through one `stopThenPlay()` that awaits the stop before playing. Cheap; testable.

#### P4-2: Seeking to span offset before `loadedmetadata` is silently ignored (or throws in Chrome)
- **Risk:** Per-span preview "seeks the shared element to span start offset." Setting `currentTime` immediately after setting `src` (before `loadedmetadata`) is ignored in most browsers and has thrown in Chrome. The correct order is: set src → `load()` → wait `loadedmetadata` → set `currentTime` → `play()`. If the player sets the offset optimistically, previews start from 0 or fail.
- **Trigger conditions:** Any preview that seeks — the entire per-span feature. Also the old Chrome currentTime-then-src crash class (Issue 74031) motivates the ordering discipline.
- **Matches Alexandria because:** YES — seek-to-offset is the feature.
- **Severity:** MEDIUM
- **Evidence:** [MDN — loadedmetadata event](https://developer.mozilla.org/en-US/docs/Web/API/HTMLMediaElement/loadedmetadata_event) (metadata must be loaded before duration/seekable are valid); [SO — HTML 5 audio element bug in Chrome](https://stackoverflow.com/questions/45181098/html-5-audio-element-bug-in-chrome) (currentTime immediately after src change throws/ignored); [jPlayer bug report — Chrome currentTime-then-src (Issue 74031)](http://www.jplayer.org/bug-reports/chrome/currentTime-then-src/).
- **Mitigation:** Encode the load→metadata→seek→play sequence in the singleton; wait for `loadedmetadata` (or `seeked`) before `play()`; unit-test the ordering with the injected player.

#### P4-3: Autoplay policy can reject the *chained* play() in sequence playback
- **Risk:** Capability 7's sequence playback auto-advances via the `ended` handler → `play()` without a user gesture. Chrome's autoplay policy blocks script-initiated audible playback without user activation/MEI; the first preview (click) activates the element, but on strict profiles (mobile, low-MEI desktop, iframe permissions-policy) the chained play() can still reject with NotAllowedError — sequence silently stops after the first span.
- **Trigger conditions:** Sequence playback on a profile where autoplay isn't granted. Click-initiated previews are safe.
- **Matches Alexandria because:** PARTIALLY — desktop-first app, but the sequence feature makes this a plausible support ticket.
- **Severity:** LOW–MEDIUM
- **Evidence:** [MDN Autoplay guide](https://developer.mozilla.org/en-US/docs/Web/Media/Guides/Autoplay) (script-initiated play outside user input is autoplay and may be blocked); [Chrome Autoplay Policy](https://developer.chrome.com/blog/autoplay/) ("always look at the promise returned by play()").
- **Mitigation:** Handle the rejection in the sequence driver (pause sequence, show "tap to continue"); no architectural change needed.

#### P4-4: jsdom does not implement HTMLMediaElement → P8 tests crash on `.play().catch(...)`
- **Risk:** jsdom's `HTMLMediaElement.play/pause/load` throw `Not implemented: HTMLMediaElement.prototype.play` and `play()` returns `undefined`, so any `audio.play().catch(...)` in app code (the *existing* `previewVoice` does exactly this) throws `TypeError: Cannot read property 'catch' of undefined` in tests. The P8 plan's injectable factory handles the *singleton*, but the other five Audio call sites (templates.ts, designer.ts, training.ts) also run in tests.
- **Trigger conditions:** Any test that mounts a tab calling `new Audio(...).play()` without the prototype stub.
- **Matches Alexandria because:** YES — the pattern must stub `HTMLMediaElement.prototype.play/pause/load` in vitest setup (as promptfoo's setupTests.ts does) or every frontend test file needs its own shim.
- **Severity:** MEDIUM (test-surface; cross-pattern with P8)
- **Evidence:** [jsdom issue 2155](https://github.com/jsdom/jsdom/issues/2155) ("Not implemented: HTMLMediaElement.prototype.load... jsdom doesn't support any loading or playback media operations"); [react-testing-library issue 947](https://github.com/testing-library/react-testing-library/issues/947); [SO — How do I mock Audio API in Jest](https://stackoverflow.com/questions/69591847/how-do-i-mock-audio-api-in-jest-properly) ("Cannot read property 'catch' of undefined" — play() returns undefined); [promptfoo setupTests.ts](https://github.com/promptfoo/promptfoo/blob/6f66bc7e/src/app/src/setupTests.ts) (canonical `Object.defineProperty(HTMLMediaElement.prototype, 'play', { value: vi.fn(() => Promise.resolve()) })` stub).
- **Mitigation:** Add the prototype stubs to `frontend/test/setup.ts` (one file), not per-test.

---

### P5: Overlay Config — Recursive Round-Trip + Per-Walk Override Resolution

#### P5-1: `model_dump()` emits defaults for unset fields → patch-not-replace merge grows default bloat and makes key *removal* impossible
- **Risk:** If the save path validates via `AppConfig` and then merges `AppConfig.model_dump()` with the on-disk file, every field the user never touched is written back with its default (pydantic includes defaults unless `exclude_unset=True`). Result: the config file accumulates default values on every save, and a user who *removes* a key (e.g., deletes a generation param to fall back to defaults) finds it resurrected on the next save. "Patch-not-replace" then silently becomes "replace-with-validated-defaults."
- **Trigger conditions:** Any save of a config where the user omitted keys — the *default* interaction.
- **Matches Alexandria because:** YES — capability 6's "optional overrides" and the whole round-trip fix exist to stop key stripping; default-bloat is a subtler version of the same bug.
- **Severity:** MEDIUM
- **Evidence:** [Pydantic serialization docs](https://pydantic.dev/docs/validation/2.3/usage/serialization/) (`model_dump` recursively dumps sub-models and includes fields with default values unless excluded); [pydantic issue 12064 / discussion 8580](https://github.com/pydantic/pydantic/issues/12064) (round-trip `model_dump`→`model_validate` breaks under strict configs — the class of "dump output is not a faithful input" bugs).
- **Mitigation:** Merge the **raw parsed JSON** (the PreserveTree output), never `AppConfig.model_dump()`; only validated-and-changed fields are patched; treat `exclude_unset=True` as a hard rule if any pydantic object is serialized into the file.

#### P5-2: pydantic drops extras unless `extra='allow'` at *every* nesting level; nested-extra dumping has been buggy
- **Risk:** The carry-forward "every path not consumed at every nesting depth" only works if the merge operates on raw JSON — if it ever flows through a pydantic model, `extra='ignore'` (the default) silently discards unknown keys, and even `extra='allow'` had a nested-dump bug (extra tuple wrapper). PR 12944 just fixed extras being dropped on dump for forbid/ignore models — the exact trap this pattern must avoid.
- **Trigger conditions:** Any validation step interposed between raw JSON and the merge; or a nested model with `extra='allow'` on an older pydantic.
- **Matches Alexandria because:** PARTIALLY — the pattern already says "parse raw JSON," so this is a guardrail risk (an implementer shortcutting through AppConfig re-introduces the L4/L5 data-loss bug).
- **Severity:** MEDIUM
- **Evidence:** [pydantic PR 12944](https://github.com/pydantic/pydantic/pull/12944) ("model_dump() dropped extra fields when model_validate(data, extra='allow') populated __pydantic_extra__ on a model whose own config is extra='forbid' or 'ignore'"); [pydantic issue 5784](https://github.com/pydantic/pydantic/issues/5784) (nested model dump with extra='allow' produced `{'inner': ({'a': 'a'}, None)}` — tuple-wrapped garbage); [pydantic Config docs](https://pydantic.dev/docs/validation/dev/api/pydantic/config/) (default `extra='ignore'`).
- **Mitigation:** Enforce in tests: save→load→save is byte-stable for unknown keys (the round-trip test P5 should own).

#### P5-3: `resolve_task_config` blast radius — 9 walk modules + mid-run consistency
- **Risk:** Migrating 9 walk modules to one helper changes *when* config is read. Today config is read from disk per task. If any module (or the helper) caches at import time, a config save *mid-walk* silently changes overrides for later units — or worse, some walks pick up new values and others don't within one run, producing a mixed-config render.
- **Trigger conditions:** A config save while a walk is running. The editor allows saving voice config while rendering (capabilities 1+4 coexist).
- **Matches Alexandria because:** PARTIALLY — single-user desktop app, low frequency, but the failure (mixed overrides in one book's output) is hard to diagnose.
- **Severity:** LOW–MEDIUM
- **Evidence:** [pydantic-settings deep_merge commit 61e0b46](https://github.com/pydantic/pydantic-settings/commit/61e0b46) (the pattern's own citation: shallow merge loses nested unknowns — same class of "which value wins" bug at the resolution layer); **SPECULATIVE** for import-time caching (depends on the helper's implementation): labeled.
- **Mitigation:** Resolve per-call (no module-level cache); snapshot the resolved overrides at walk-unit start so one run is internally consistent.

#### P5-4: Concurrent config writes — read-merge-write race
- **Risk:** Two POST /api/config in flight: both read on-disk, both merge, both write → last-writer-wins loses one save. Atomic tmp+rename prevents torn files but not lost updates.
- **Trigger conditions:** Double-click save, or save during a background autosave. Frontend has no debounce today.
- **Matches Alexandria because:** LOW — single user, but the fix is trivial.
- **Severity:** LOW
- **Evidence:** Classic read-modify-write lost-update — **SPECULATIVE** for this codebase (no lock today; atomic write only). Generic treatment: [SQLite's own docs on WAL and single-writer](https://www.sqlite.org/wal.html) illustrate why atomic writes alone don't serialize read-modify-write cycles.
- **Mitigation:** Serialize config writes with a threading.Lock in the endpoint (3 lines).

---

### P6: Polished M4B/MP3/Audacity Export via FFMETADATA1 + Concat Discipline

#### P6-1: Chapter END derived from summed per-chunk durations accumulates rounding error → last chapter exceeds true duration
- **Risk:** If START/END are accumulated from per-chunk ffprobe durations (each rounded to ms), 1000+ chunks accumulate error; the final END can exceed the true track duration. ffmpeg's chapter import then writes an out-of-range END; players handle it inconsistently, and ffmpeg's own length detection on import is known-unreliable (it has mangled chapter timings on MP3 import).
- **Trigger conditions:** Books with hundreds/thousands of chunks; TTS chunk durations that vary.
- **Matches Alexandria because:** PARTIALLY — small-medium books, but a 200-chapter book with ms-rounding per chapter is enough to drift.
- **Severity:** MEDIUM
- **Evidence:** [m4b-tool issue 71](https://github.com/sandreas/m4b-tool/issues/71) (ffmpeg misaligns chapter times on import; maintainer: "ffmpeg does not detect lengths correctly" — linked ffmpeg trac 8728); [TagLib-Wasm chapters doc](https://charleswiltgen.github.io/TagLib-Wasm/guide/chapters) (MP4 chapters are start-time-only; the last END is inferred from track duration — an out-of-range END is thus *trusted* and wrong).
- **Mitigation:** Derive chapter boundaries from **one** ffprobe of the concatenated audio (or clamp the final END to the probed duration). Cheap and removes the drift class.

#### P6-2: "Concat filter for heterogeneous" only covers codec heterogeneity — sample-rate mismatch still corrupts
- **Risk:** The pattern claims the concat filter handles "heterogeneous (WAV/mp3/m4a mix from individual mode)." The concat *filter* does handle codec differences (it decodes to PCM), but **not** sample-rate/channel-layout mismatch — mismatched rates produce pitch/speed distortion unless each input is normalized (`aresample=async=1`, `asetpts=PTS-STARTPTS`) first. The demuxer path (fast path) requires identical codec+rate and will fail loudly ("Error parsing packet header") otherwise — the pattern states that correctly.
- **Trigger conditions:** Individual-mode chunks at different rates — for Alexandria, TTS WAVs are *probably* uniform (same engine), so LOW likelihood today; but the pattern's claim over-promises, and a future multi-engine setup (capability 4 ref-audio?) breaks it silently.
- **Matches Alexandria because:** PARTIALLY — uniform-engine WAVs make this latent, not active.
- **Severity:** MEDIUM (latent; silent corruption when it hits)
- **Evidence:** [FFmpeg FAQ 3.15](https://ffmpeg.org/faq.html#How-do-I-concatenate-videos) / [FFmpeg concat wiki](https://trac.ffmpeg.org/wiki/Concatenate) (filter inputs must share sample rate/channel layout; demuxer requires identical codecs+timebase); [SO — concat demuxer wrong speed](https://stackoverflow.com/questions/71361071/) (48k vs 44.1k → faster playback); [ffmpeg-cookbook concat](https://ffmpeg.org/ffmpeg-cookbook.html) (normalize with aresample beforehand).
- **Mitigation:** Add per-input `aresample=async=1:first_pts=0` + `asetpts=PTS-STARTPTS` to the filter command; or assert homogeneity and use the demuxer. Also note: N-input concat filter means one `-i` per chunk — a 1000-chunk filter_complex is unwieldy; the demuxer (list.txt) is the scalable path for the homogeneous case that is Alexandria's actual steady state.

#### P6-3: FFMETADATA1 failure modes — missing header / TIMEBASE mismatch → "Could not write header" with `-c copy`
- **Risk:** The muxer aborts with `Tag tx3g incompatible with output codec id` / `Could not write header (incorrect codec parameters)` when the ffmetadata file is malformed (missing `;FFMETADATA1` header, wrong TIMEBASE) and chapters are mapped with `-c copy`. The pattern's command (TIMEBASE=1/1000, header) is correct, but the failure is a *hard* 500 with a confusing stderr unless the metadata writer is validated.
- **Trigger conditions:** Any bug in the metadata generator (off-by-one, empty chapter, non-integer ms) — exactly what unit tests don't cover for generated text files.
- **Matches Alexandria because:** PARTIALLY — the pattern's stderr_tail error surface handles it; the risk is generating invalid metadata in the first place.
- **Severity:** LOW–MEDIUM
- **Evidence:** [r/ffmpeg — m4b chapter metadata failure](https://www.reddit.com/r/ffmpeg/comments/1akyiq0/trying_and_failing_to_add_chapter_metadata_to_m4b/) (TIMEBASE=1/10 + missing header → "Tag tx3g incompatible with output codec id '98314'" + "Could not write header"; fix: include `;FFMETADATA1` + use `-map_chapters`).
- **Mitigation:** A unit test that runs the metadata generator and *then* ffmpeg on a 2-chunk fixture in CI (the pattern's P8 suite should include this — it's the only thing that catches malformed ffmetadata).

#### P6-4: Chapter mapping quirks — titles need both `-map_chapters 1` and `-map_metadata 1`; Apple reads the QuickTime track
- **Risk:** ffmpeg treats ISOBMFF chapter *titles* as metadata, not chapters — omitting either flag loses titles (the pattern has both ✓). Default output writes BOTH a QuickTime CHAP track and a Nero CHPL atom; Apple devices read the QuickTime track (fine), but >255 chapters truncate the Nero atom only (harmless). The real quirk: ffmpeg **drops unrecognized tags** when mapping metadata — acceptable for a fresh export, but means cover/artist tags must be re-specified in the ffmetadata file, not inherited from the intermediate m4a.
- **Trigger conditions:** Books with unusual tag requirements; >255-chapter books (rare for Alexandria).
- **Matches Alexandria because:** PARTIALLY — the pattern's command already has both flags; this is a "don't regress" note plus a confirmation that `-movflags disable_chpl` is optional.
- **Severity:** LOW
- **Evidence:** [superuser — add chapter marks without mangling metadata](https://superuser.com/questions/1877167/how-to-add-chapter-marks-to-mp3-mp4-and-ogg-files-via-the-command-line-withou) ("FFmpeg considers chapter titles to be part of -map_metadata and not -map_chapters for ISOBMFF... FFmpeg always mangles the other metadata"; suggests `-movflags disable_chpl` since QuickTime chapters have wider support); [TagLib-Wasm chapters](https://charleswiltgen.github.io/TagLib-Wasm/guide/chapters) (Apple reads QuickTime track; Nero chpl ignored by Apple, 255-chapter cap).

---

### P7: Bounded-Range Audio Serving with Version Guard

#### P7-1: Missing file → RuntimeError 500 (cross-pattern with P2/P4)
- **Risk:** Verified in Starlette master: `FileResponse.__call__` raises `RuntimeError` on a missing file — unhandled 500. The chunk/audio endpoint must convert this to 404 (see P2-2). Also: FileResponse sets `Content-Disposition: attachment; filename=...` when `filename=` is passed (verified in `__init__`); for the *player* endpoints, attachment disposition can trigger download behavior in some browsers/Safari instead of inline streaming — pass no `filename` (or `content_disposition_type="inline"`) for preview/chunk URLs, keep `filename=` for download.
- **Trigger conditions:** GC race; any browser that treats attachment CD specially on `<audio src>`.
- **Matches Alexandria because:** PARTIALLY — single host, low frequency; the CD nuance is the more likely day-one issue.
- **Severity:** MEDIUM
- **Evidence:** [starlette responses.py — FileResponse.__init__/__call__](https://raw.githubusercontent.com/encode/starlette/master/starlette/responses.py) (lines 299–352, 319–325 verified: `raise RuntimeError("File at path ... does not exist")`; `content-disposition: attachment` default with filename).
- **Mitigation:** Existence pre-check + 404; no `filename` on player endpoints.

#### P7-2: Fallback parser semantics diverge from FileResponse (400 vs 416; multi-range multipart)
- **Risk:** P7's fallback "multiple ranges → 416, malformed → 416" differs from FileResponse's actual behavior: malformed → **400** (PlainTextResponse), unsatisfiable → **416**, multiple ranges → **multipart/byteranges 206** (not 416). The CI pin (starlette ≥0.49.1, verified real: GHSA-7f5h-v6xp-fcq8, O(n²) Range-header DoS, fixed 0.49.1; installed 1.3.1 is far above) makes the fallback **dead code in practice** — but if it ever activates (version drift, someone bypasses the pin), behavior changes under the client without warning.
- **Trigger conditions:** Only pre-0.49.1 starlette — which the CI guard forbids. Purely defensive-surface.
- **Matches Alexandria because:** NO — this is theoretical; the pin holds.
- **Severity:** LOW
- **Evidence:** [starlette responses.py — FileResponse.__call__](https://raw.githubusercontent.com/encode/starlette/master/starlette/responses.py) (lines 364–379: MalformedRangeHeader→400, RangeNotSatisfiable→416 with `Content-Range: bytes */size`, `len(ranges) > 1`→`_handle_multiple_ranges` multipart); [GHSA-7f5h-v6xp-fcq8 / CVE-2025-62727](https://github.com/Kludex/starlette/security/advisories/GHSA-7f5h-v6xp-fcq8) (0.49.1 fix — the pattern's pin is accurate and necessary).
- **Mitigation:** Either match FileResponse semantics exactly in the fallback (400/416/multipart) or delete the fallback and let the pin be the guard (recommended: keep the hand parser only as a documented belt-and-suspenders, clearly marked divergent).

#### P7-3: HEAD, If-Range, open-ended and suffix ranges — verified native, no action
- **Risk:** None found. FileResponse handles HEAD (header-only), If-Range (`_should_use_range`), open-ended `bytes=start-`, suffix `bytes=-N`, single-range 206, and sets `Accept-Ranges: bytes` by default. The pattern's "bounded-range" contract (capability 1 seeking) is fully covered by the primary path.
- **Severity:** NONE (positive confirmation)
- **Evidence:** [starlette responses.py — FileResponse](https://raw.githubusercontent.com/encode/starlette/master/starlette/responses.py) (lines 340–379, 453–493 verified).

---

### P8: Testing Strategy — Dependency Overrides + InMemorySQLiteAdapter + Vitest Mocks

#### P8-1: Fake timers + fetch is a known hang trap (Vitest 3)
- **Risk:** The plan combines `vi.useFakeTimers()` with fetch mocking for the 2s polling test. With **real or MSW-intercepted fetch**, fake timers hang the request by default (Vitest 3 issue 7314/7288). With a **fully mocked** fetch (`vi.fn().mockResolvedValue`), there is no hang, but the *microtask* trap bites: `mockResolvedValue`'s `.then` runs as a microtask, and manual fake-timer advancement is a macrotask — advancing before flushing microtasks asserts against stale state. The plan must use `advanceTimersByTimeAsync` (not sync) and flush microtasks, or fake only `['setTimeout','setInterval']` and keep fetch real-but-mocked.
- **Trigger conditions:** The exact polling test the plan describes. MSW + fake timers = hang; sync advance + resolved-promise mock = flaky/wrong assertions.
- **Matches Alexandria because:** YES — this is the plan's own test design.
- **Severity:** MEDIUM (test-authoring trap; eats debug time, not a product bug)
- **Evidence:** [vitest issue 7314 — Fetch hangs in Vitest 3 with fake timers](https://github.com/vitest-dev/vitest/issues/7314) (dup of #7288); [vitest PR 7505](https://github.com/vitest-dev/vitest/issues/7505) (queueMicrotask excluded from default fake timers "to not break node fetch"); [vitest timers guide](https://vitest.dev/guide/mocking/timers) (`advanceTimersByTimeAsync` for async callbacks); [hy2k.dev — vitest fake timers + debounced fetch](https://hy2k.dev/en/blog/2025/10-03-vitest-fake-timers-debounced-solidjs-search/) (microtask-flush ordering failure with resolved-promise mocks).
- **Mitigation:** Polling test recipe: `vi.useFakeTimers({ toFake: ['setTimeout','setInterval'] })` + fully mocked fetch + `await vi.advanceTimersByTimeAsync(2000)`; `afterEach(vi.useRealTimers)` (leak → cross-test flakes, [nazarboyko](https://www.nazarboyko.com/articles/testing-async-code-in-nodejs-properly)).

#### P8-2: jsdom media stubs are a precondition, not optional (see P4-4)
- **Risk:** Any test touching the audio singleton or the other five `new Audio()` call sites throws `Not implemented: HTMLMediaElement.prototype.play` and `play().catch(...)` crashes on `undefined`. The injectable factory covers only the singleton.
- **Matches Alexandria because:** YES.
- **Severity:** MEDIUM
- **Evidence:** [jsdom 2155](https://github.com/jsdom/jsdom/issues/2155); [RTL 947](https://github.com/testing-library/react-testing-library/issues/947); [SO 69591847](https://stackoverflow.com/questions/69591847/how-do-i-mock-audio-api-in-jest-properly); [promptfoo setupTests.ts](https://github.com/promptfoo/promptfoo/blob/6f66bc7e/src/app/src/setupTests.ts).
- **Mitigation:** Ship `frontend/test/setup.ts` with `HTMLMediaElement.prototype.play/pause/load` stubs as part of P8, not as an afterthought.

#### P8-3: InMemorySQLiteAdapter is a *faithful* model of prod's single connection — but conceals WAL/busy behaviors
- **Risk:** Nuanced: prod uses ONE connection for everything (verified), and the `:memory:` adapter is also single-connection — so the plan's test model actually mirrors prod's serialization behavior, which is good (P1-1/P1-2 freeze risks WILL reproduce in tests). What it conceals: WAL journaling, `busy_timeout` retry (impossible to trigger with one `:memory:` connection), crash-recovery (startup reconciliation of `running→interrupted` — can't be tested against `:memory:` because there's no process crash), and multi-connection behavior if a future change introduces a second connection. The startup-reconciliation test needs a *file* DB fixture written then re-opened, not `:memory:`.
- **Trigger conditions:** The plan's "startup reconciliation tests" — untestable on `:memory:` as described.
- **Matches Alexandria because:** PARTIALLY — the reconciliation test is in the P8 plan and cannot work as specified on an in-memory adapter.
- **Severity:** MEDIUM
- **Evidence:** [FastAPI issue 3906](https://github.com/tiangolo/fastapi/issues/3906) (the pattern's own citation: SQLite `:memory:` is per-connection; the plan's per-test fresh adapter is the right fix); **SPECULATIVE** on the reconciliation-test gap (depends on how the fixture is constructed): labeled.
- **Mitigation:** Add one file-backed SQLiteAdapter fixture (`tmp_path`) for crash/reconciliation tests; keep `:memory:` for logic tests. Cheap.

#### P8-4: CI dist-check + new frontend code — committed-dist staleness is a feature, keep it
- **Risk:** None found; flagging the positive: `npm run build && git diff --exit-code app/static/dist/` forces the committed dist to match source. The only trap is ordering (build before diff, emptyOutDir true ✓ in vite config) and that any new frontend test that *reads* dist paths must not depend on unbuilt output. No action.
- **Severity:** NONE (positive confirmation)
- **Evidence:** [frontend/vite.config.ts](https://vite.dev/config/) (base `/static/`, outDir `../app/static/dist`, emptyOutDir true — verified this session).

---

### Cross-Pattern Interactions

#### X-1: P1 render_chunk rows + P2 manifest.json both claim progress/artifact authority (P1↔P2)
- **Interaction:** Two records of "chunk idx → wav_path," written at different points with different crash windows. Rows are committed after fsync; manifest is written as a separate artifact. Status endpoint reads rows; chunks endpoint and concat source read manifest. A crash between the two leaves them disagreeing; GC union covers eligibility (fine), but the UI can show done-rows while preview 404s.
- **Fix:** Rows = truth; manifest = derived cache rebuilt at startup reconciliation. State it in both patterns. (See P2-3.)

#### X-2: P2 retention GC vs P1 rows vs P4 preview URLs (P2↔P1↔P4)
- **Interaction:** GC deletes files after retention_days but the plan doesn't specify deleting/tombstoning referencing rows → completed-job rows dangle → P4 renders preview buttons that 404/500 (FileResponse RuntimeError). Snapshot-restore in-flight race (task-brief named) is narrowed by the pre-delete recheck but not closed (TOCTOU).
- **Fix:** Row tombstones in the same sweep; 404-on-missing in the P7 endpoint; consider a pin/lock during restore. (See P2-1/P2-2.)

#### X-3: P3 supersede-at-start + P1 failed/cancelled runs → review-candidate loss (P3↔P1)
- **Interaction:** Supersede is transactional with walk *start*; a failed/cancelled re-walk (both designed paths) leaves old items superseded and new items partial → low-confidence items (capability 5's entire purpose) silently disappear.
- **Fix:** Supersede at completion, or restore on failure/cancel. (See P3-1.)

#### X-4: P1 walk_run counters + P3 walk_review_item run_id (P1↔P3)
- **Interaction:** `walk_review_item.run_id` references `walk_run.run_id`; P1's startup reconciliation flips running→interrupted on boot. If a walk crashes mid-unit, its items are pending but its run is interrupted — the queue must treat interrupted runs as *stale* (items still reviewable, run status honest), and re-walks must supersede only completed/interrupted predecessors, not running ones. Ordering of reconciliation vs supersede on boot matters.
- **Fix:** Define the interrupted-run → item lifecycle in the queue contract (P3 owns it).

#### X-5: P1 DB cancel + P2 stop-file + P4 render cancel_event — three cancel channels (P1↔P2↔P4)
- **Interaction:** Walk cancel lives in `walk_run.cancel_requested` (P1) + `run.stop-request.json` (P2); render cancel lives in `cancel_event` (P1/P4). Any check site that consults only one channel makes cancel a half-truth. One dispatcher (`is_cancel_requested(run_id)`) reading all three, defined in P1.
- **Fix:** See P1-4.

#### X-6: P5 resolve_task_config + P3 single-speaker mapping order (P5↔P3)
- **Interaction:** If per-walk overrides can set the narrator voice (capability 6) and single-speaker maps every speaker→NARRATOR at `_build_voice_config`, the ordering decides whether the user's override wins. Pin: resolve overrides first, then apply single-speaker mapping (override wins).
- **Fix:** One line in P3/P5 contract text.

#### X-7: P6 concat source + P2 GC retention (P6↔P2)
- **Interaction:** Export runs against run-dir files; if GC's hourly sweep runs mid-export and the job is old enough... in practice the export itself holds `render_job` running → `output_dir` is referenced by the live job row → GC union protects it. Safe *only because* the job row transitions to running before phase 1. If export ever runs outside a job row (ad-hoc re-export of an old dir), GC can delete mid-export.
- **Fix:** All export paths must create/own a job row first (the pattern does for /export/m4b; keep it true for any derived export).

#### X-8: P8 InMemory adapter + P1 transaction tests (P8↔P1)
- **Interaction:** The txn-wrapper tests (commit/rollback/nested-join) are valid on `:memory:` (same sqlite3 semantics) — good. But the startup-reconciliation test needs a file DB (see P8-3), and no test can exercise WAL/busy (irrelevant today: single connection). Document that the suite deliberately does NOT cover multi-connection behavior, so a future second-connection refactor doesn't pass tests that should have caught it.

---

### Summary

- **Blocking issues:** none — no risk found that should prevent proceeding with the pattern set.
- **High risks (fix before implementation):**
  1. **P1-1** — cross-thread writes silently joining walk transactions (owner-thread guard or per-thread connection).
  2. **P1-2** — LLM calls inside BEGIN IMMEDIATE freeze all API DB access (LLM-outside, write-inside scoping).
  3. **P3-1** — supersede-at-start loses review candidates on failed/cancelled re-walks (supersede at completion / restore on failure).
- **Mitigable medium risks (mechanical, low-cost):**
  - P2-1/P2-2 + X-2: GC must tombstone rows; chunk endpoints must 404, not 500.
  - P3-2/P3-4: item_id namespace; pin single-speaker enforcement to `_build_*` (render-only).
  - P4-1/P4-2: AbortError-tolerant singleton; load→metadata→seek→play ordering.
  - P5-1/P5-2: merge raw JSON only; `exclude_unset` discipline; round-trip byte-stability test.
  - P6-1/P6-2: chapter END from one ffprobe of the concatenated file; aresample normalization.
  - P8-1/P8-2/P8-3: fake-timer recipe (`toFake` allowlist + `advanceTimersByTimeAsync` + `afterEach(useRealTimers)`); HTMLMediaElement stubs in setup; one file-backed fixture for reconciliation tests.
- **What the Improver must address in Turn 7:** the three HIGH risks above are the minimum; the highest-leverage single change is P1's transaction() **owner-thread guard**, because it converts "discipline by convention" into "discipline by enforcement" and de-risks P3's supersede transaction and P8's wrapper tests simultaneously.


---

## Final Patterns

*Turn 7 — RnD-Improver. Response to the Turn 6 "## Pattern Risks" critique. All 8 patterns remain viable; the three HIGH risks and the factual correction are fixed below with mechanical changes. FP1–FP8 are the authoritative successors to P1–P8. No prior content was modified.*

### Risk Response Table

| Risk (T6) | Severity | Response | Mechanism |
|---|---|---|---|
| P1-1 cross-thread txn join | HIGH | **MITIGATED** | `transaction()` records owner thread; `execute_*` writes from a non-owner while a txn is open raise `ConcurrentTransactionError` (fail fast, no silent join); API middleware maps it to 503 + one retry. Reads allowed (mutex-serialized, microsecond-scale). |
| P1-2 LLM-inside-BEGIN IMMEDIATE | HIGH | **MITIGATED** | **LLM-outside, write-inside** scoping: every walk unit runs the LLM round-trip with NO open transaction, then a short `BEGIN IMMEDIATE` → UPSERTs → COMMIT. The connection is never held across an LLM call. |
| P1-3 isolation_level factual error | MEDIUM (forward-compat) | **CORRECTED + MITIGATED** | Set `isolation_level=None` explicitly at connect now (3.13); comment cites gh-83638; `transaction()` ends with explicit `execute("COMMIT"/"ROLLBACK")` so it is independent of the `autocommit` attribute when the 3.14/3.16 flip lands. |
| P1-4 dual cancel channels | MEDIUM | **MITIGATED** | One `is_cancel_requested(run_id)` dispatcher reading DB row + stop-file (walks; DB authoritative, file a mirror) or `cancel_event` (renders). Every check site calls the dispatcher only. (X-5) |
| P2-1 GC deletes files, rows dangle | MEDIUM | **MITIGATED** | Same-sweep row tombstones: `render_chunk.status='evicted'`, `wav_path=NULL`; whole-dir GC sets `render_job.status='expired'`. No dangling `done` rows. (X-2) |
| P2-2 FileResponse RuntimeError→500 | MEDIUM | **MITIGATED** | Pre-check `os.path.exists` + FileResponse subclass whose `__call__` catches RuntimeError → 404 (handler-level try/except cannot catch it — see FP7). |
| P2-3 dual progress authority | MEDIUM | **MITIGATED** | **Rows = truth; manifest.json = derived cache** rebuilt at startup reconciliation. All readers (status, chunks, GC) read rows; manifest is for artifact refs only. (X-1) |
| P3-1 supersede-at-start loses candidates | HIGH | **MITIGATED** | **Supersede at completion** in the walk's final transaction; on failure/cancel, no supersede occurs — prior `pending` items remain pending. No compensating transaction needed because nothing was mutated. (X-3) |
| P3-2 dual-path item_id namespace | MEDIUM | **MITIGATED** | Queue endpoint returns a discriminated union `junction:` / `walkitem:` prefixed item_ids; dispatcher keys on prefix; junction filter moves off `source LIKE` onto a stable provenance column. |
| P3-3 retro-fitted txn semantics | MEDIUM | **MITIGATED** | Unit granularity = one walk unit (N spans/chars) per `transaction()`, matching heartbeat cadence; walk contract states failed unit rolls back only that unit; earlier committed units persist. |
| P3-4 single-speaker boundary ambiguity | MEDIUM | **MITIGATED** | Pin ONE boundary: `_build_voice_config`/`_build_chunks` (render-only). `export_annotated_script` stays faithful so editor/review context keep real speakers. |
| P4-1 play() interrupted AbortError | MEDIUM | **MITIGATED** | `stopThenPlay()` awaits stop before play; every `play()` rejection classified — AbortError/NotAllowedError benign, everything else error state. |
| P4-2 seek before loadedmetadata | MEDIUM | **MITIGATED** | Encode load → `loadedmetadata` → `currentTime` → `play()` ordering in the singleton; unit-tested with the injected player. |
| P4-3 autoplay chained play() blocked | LOW–MEDIUM | **MITIGATED** | Sequence driver handles NotAllowedError: pause sequence, "tap to continue" UI. Documented behavior, no architecture change. |
| P4-4 jsdom no HTMLMediaElement | MEDIUM | **MITIGATED** | `frontend/test/setup.ts` stubs `HTMLMediaElement.prototype.play/pause/load` once (see FP8). |
| P5-1 model_dump default bloat | MEDIUM | **MITIGATED** | Merge the **raw parsed JSON**, never `model_dump()` output; only validated-and-changed fields patched; `exclude_unset=True` hard rule if any pydantic object is ever dumped. |
| P5-2 extras dropped through pydantic | MEDIUM | **MITIGATED** | Merging operates exclusively on raw JSON; pydantic never sits between raw parse and merge. Byte-stable round-trip test enforced in CI. |
| P5-3 resolve_task_config blast radius | LOW–MEDIUM | **MITIGATED** | Per-call resolution, no module-level cache; resolved overrides snapshotted at walk-unit start (one run internally consistent). |
| P5-4 concurrent config writes | LOW | **MITIGATED** | `threading.Lock` in the config endpoint serializes read-merge-write. |
| P6-1 chapter END rounding drift | MEDIUM | **MITIGATED** | Chapter boundaries derived from **one ffprobe** of the concatenated audio; final END clamped to probed duration. |
| P6-2 sample-rate mismatch corruption | MEDIUM (latent) | **MITIGATED** | Filter path adds per-input `aresample=async=1:first_pts=0` + `asetpts=PTS-STARTPTS`; homogeneous steady state stays on the demuxer (scales to 1000+ chunks). |
| P6-3 malformed ffmetadata → hard 500 | LOW–MEDIUM | **MITIGATED** | CI test runs generator + ffmpeg on a 2-chunk fixture — the only thing that catches malformed metadata. |
| P6-4 chapter mapping quirks | LOW | **MITIGATED** | Keep both `-map_chapters 1` and `-map_metadata 1`; re-specify cover/artist in ffmetadata (ffmpeg drops unrecognized tags); `disable_chpl` optional for Apple-only. |
| P7-1 missing file → RuntimeError 500 | MEDIUM | **MITIGATED** | Existence pre-check + FileResponse subclass → 404; player endpoints pass no `filename` (inline disposition), download endpoints keep it. |
| P7-2 fallback parser divergence | LOW | **MITIGATED** | **Fallback parser deleted.** CI pin `starlette>=0.49.1` is the guard (installed 1.3.1 far above); the divergent parser is dead code and removed. |
| P7-3 HEAD/If-Range/suffix — verified | NONE | **CONFIRMED** | Native FileResponse behavior covers it; no action. |
| P8-1 fake timers + fetch hang | MEDIUM | **MITIGATED** | `vi.useFakeTimers({ toFake: ['setTimeout','setInterval'] })` + fully mocked fetch + `await vi.advanceTimersByTimeAsync(2000)`; `afterEach(vi.useRealTimers)`. |
| P8-2 jsdom media stubs precondition | MEDIUM | **MITIGATED** | Shipped in `frontend/test/setup.ts` as part of FP8, not per-test. |
| P8-3 :memory: conceals crash recovery | MEDIUM | **MITIGATED** | One file-backed `SQLiteAdapter(tmp_path)` fixture for startup-reconciliation/crash tests; `:memory:` for logic tests. |
| P8-4 CI dist-check | NONE | **CONFIRMED** | Keep `npm run build && git diff --exit-code app/static/dist/`; ordering documented. |
| X-4 interrupted-run → item lifecycle | MEDIUM | **MITIGATED** | Queue contract (FP3): items from interrupted runs stay `pending`/reviewable; supersede considers only completed/interrupted predecessors, never running ones. |
| X-7 export without a job row | MEDIUM | **MITIGATED** | Every export path (M4B/MP3/Audacity) creates/owns a `render_job` row (status running) before touching files, so GC's reference union protects them. |

### Fundamental Limitations Acknowledged

- **Batch-mode per-span preview is impossible without touching `tts.py`** (hard constraint). `generate_batch` has no per-chunk callback; the batch path writes no `render_chunk` rows. FP4's mode contract states this explicitly; the design must document "batch render → whole-book playback only" in the DD and UI copy. This is a constraint-derived limitation, not a pattern gap.
- **Autoplay policy cannot be fully mitigated in code.** A chained `play()` (sequence auto-advance) may be blocked on strict profiles regardless of implementation; the documented fallback is pause + "tap to continue". The DD must list this as a known UX behavior.
- **The owner-thread guard makes concurrent API writes during a walk-unit transaction fail fast (503 + retry) rather than succeed.** With LLM-outside/write-inside, the conflicting window is milliseconds; retry succeeds essentially always. The design must document that a 503 with `Retry-After` on a write endpoint during a walk is expected and safe, and the frontend must retry once rather than surface an error.
- **WAL journaling, `busy_timeout`, and multi-connection behavior are deliberately untested** (single-connection topology makes them untestable on `:memory:` and irrelevant today). A future second-connection refactor must add its own fixtures; the suite documents this gap so a regression isn't masked (X-8).
- **The starlette Range fix is guarded by CI discipline only.** If someone bypasses the `>=0.49.1` pin, range semantics revert to the pre-fix (O(n²)-vulnerable) behavior; the deleted fallback parser cannot rescue it. Accepted as a CI-discipline limitation.
- **Per-chunk fsync throughput on very large books** (individual mode, 2 fsyncs/chunk; Turn 4 LOW risk) is unchanged; correctness unaffected, documented for slow storage.

---

### FP1: Schema-Native Jobs with Owner-Thread Transaction Guard (Supersedes P1)

- **Final pattern:** The P1 schema is unchanged — `render_job`, `render_chunk`, `walk_run`, `walk_review_item` (P1 column shapes), with `walk_run.cancel_requested`, per-unit `heartbeat_ms`, and `result_json`/`error` for failure accounting. Three changes make the transaction discipline **enforced, not conventional**:

  1. **Owner-thread guard.** The adapter gains `_tx_owner: int | None`. `transaction()` sets `_tx_owner = threading.get_ident()` when it issues `BEGIN IMMEDIATE`, clears it on commit/rollback; a nested `transaction()` from the same thread joins silently. Every `execute_insert/update/delete` checks: if `self._conn.in_transaction and self._tx_owner is not None and self._tx_owner != threading.get_ident()` → raise `ConcurrentTransactionError` (a dedicated exception, distinct from sqlite3 errors). Reads are not guarded — they serialize on the sqlite3 connection mutex for microseconds. API middleware maps `ConcurrentTransactionError` to `503 + Retry-After: 1`; the frontend retries once. This converts P1-1's silent cross-thread join (and its silent rollback-with-the-walk) into a visible, retryable condition.
  2. **LLM-outside, write-inside scoping.** Each walk unit's shape is: `SELECT batch` (no txn) → LLM round-trip (no txn open) → `with storage.transaction():` UPSERTs + `walk_review_item` insert + heartbeat update → COMMIT. The sqlite3 connection is never held across an LLM call, so P1-2's app-wide freeze is structurally impossible.
  3. **Explicit transaction control.** `sqlite3.connect(..., isolation_level=None)` is set **explicitly** at connect (P1's claim that it was already the mode was wrong — the adapter currently inherits legacy `DEFERRED` implicit mode). A comment cites CPython gh-83638. `transaction()` ends with explicit `self._conn.execute("COMMIT")` / `execute("ROLLBACK")` rather than relying on `commit()`/`rollback()` semantics, so the wrapper is unaffected when `autocommit` defaults flip to `False` at Python 3.16 (deprecations land in 3.14; pin CI accordingly and re-audit on each Python bump).

  Reconciliation stays **startup-only** (single-process: process death is the only worker-death mechanism; no on-read sweeper that could false-positive on multi-minute 2g/2i units). Walk cancel: `POST /cancel_walks` writes `walk_run.cancel_requested=1`; every check site calls the single dispatcher `is_cancel_requested(run_id)` (below). Render cancel keeps the in-process `cancel_event`, surfaced through the same dispatcher. New endpoints per CONTRACTS module split: jobs/chunks in `api_export`, walk runs in `api_walks`; `download/{job_id}` reads `render_job` rows.

- **Mitigations embedded:** P1-1 (owner guard + 503/retry), P1-2 (LLM-outside/write-inside), P1-3 (explicit isolation_level + explicit COMMIT/ROLLBACK + CI note), P1-4 and X-5 (one cancel dispatcher), X-4 (run/item lifecycle owned by FP3).
- **Evidence:**
  - https://docs.python.org/3/library/sqlite3.html — `check_same_thread=True` default ("write operations may need to be serialized by the user"), `autocommit` currently defaults to `LEGACY_TRANSACTION_CONTROL` and "The default will change to `False` in a future Python release"; `isolation_level` has no effect unless `autocommit` is legacy. This is the exact factual correction.
  - https://github.com/python/cpython/issues/83638 and https://github.com/python/cpython/pull/93823 — the `autocommit` attribute plan: deprecations in 3.14, default flip in 3.16; motivates setting explicit control now and using `execute("COMMIT")` so the wrapper is flip-proof.
  - https://sqlite.org/threadsafe.html — serialized mode: "The effect on an individual object is the same as if the API calls had all been made in the same order from a single thread" — why the owner-thread guard is the correct enforcement on a single serialized connection (the mutex already orders calls; the guard prevents *semantic* interleaving).
  - https://stackoverflow.com/questions/22739590/how-to-share-single-sqlite-connection-in-multi-threaded-python-application — community-verified hazards of a shared `check_same_thread=False` connection and the lock/guard requirement.
  - https://www.ssdnodes.com/learn/sqlite-in-production-vps — "holding a write transaction open across slow work… will block every other writer for the length of that call. Read what you need, close the transaction, do the slow work, then open a short write transaction to store the result" — the production statement of LLM-outside/write-inside; also: "Start any transaction that will write with `BEGIN IMMEDIATE`."
  - https://charlesleifer.com/blog/multi-threaded-sqlite-without-the-operationalerrors/ — short explicit transactions as the fix for pysqlite's transaction state-machine hazards; `isolation_level=None` removes the implicit-txn magic.

### FP2: Artifact-First Run Directory with Row-Tombstoning GC (Supersedes P2)

- **Final pattern:** Run directories under `RENDER_ROOT` with atomic manifest commits (write tmp → fsync → rename → fsync dir) unchanged. Two changes resolve the dangling-reference class:

  1. **Rows are truth; manifest.json is a derived cache** (X-1/P2-3). The status endpoint, chunks endpoint, and GC read `render_chunk`/`render_job` rows; the manifest is rebuilt from rows at startup reconciliation and exists to give FP6 export a stable artifact snapshot. A crash between row commit and manifest write no longer produces observable divergence.
  2. **GC tombstones referencing rows in the same sweep** (P2-1/X-2). When a run dir passes retention, the sweep deletes files AND marks `render_chunk.status='evicted'`, `wav_path=NULL`, `render_job.status='expired'` in one pass. Per-span preview (FP4) renders affordances only for rows with a non-NULL `wav_path`; evicted rows are shown as "audio expired" or hidden, never as playable. **All export paths own a job row first** (X-7): M4B/MP3/Audacity each create/transition a `render_job` row to `running` before touching files, so the GC reference union protects them mid-export. Deletion eligibility stays reference-derived (live rows + snapshot manifests), with the pre-delete re-check (P2's TOCTOU narrowing) unchanged.
- **Mitigations embedded:** P2-1, P2-2 (with FP7's 404), P2-3, X-1, X-2, X-7.
- **Evidence:**
  - https://github.com/facebook/rocksdb/wiki/How-we-keep-track-of-live-SST-files — the corollary the counter flagged: reference-derived eligibility must be enforced on **both** sides of a reference or the reference dangles; the row tombstone is the "both sides" enforcement.
  - https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html — dual-write discipline: two records of one fact (row + file) must not be able to drift; declaring rows authoritative and the manifest a derived cache is the same single-authority principle.
  - https://github.com/Kludex/starlette/issues/979 and https://stackoverflow.com/questions/74103330/fastapi-fileresponse-not-entering-the-except-block — the RuntimeError is raised in Starlette's send path *after* the handler returns; a handler try/except cannot catch it, so the endpoint must pre-check `os.stat` or subclass `__call__` (see FP7).

### FP3: Unified Review Queue with Completion-Time Supersede (Supersedes P3)

- **Final pattern:** Junction items stay a live query; walk-side items are materialized `walk_review_item` rows with `prior_value` (undo source), written in the same per-unit transaction as their target writes. The P3-1 fix is the ordering change:

  - **Supersede at completion, never at start.** In the walk's final transaction (after the last unit, immediately before `status='completed'`): `UPDATE walk_review_item SET status='superseded' WHERE book_id=? AND run_id <> ? AND status='pending' AND kind=?` per kind regenerated. On failure or cancel, **nothing is superseded** — prior `pending` items remain pending and reviewable. This mirrors Kafka log compaction's safety property (the old value remains until the new value lands): supersede is only ever performed when the new candidate set already exists in committed rows. No compensating transaction is required because the destructive step is simply never executed on the failure path.
  - **Interrupted-run lifecycle** (X-4): items whose run was interrupted (startup reconciliation) stay `pending`; they are reviewable regardless of run status. Supersede predicates consider only `completed` or `interrupted` predecessors — never `running` runs.
  - **Item-id namespace** (P3-2): the queue endpoint returns a discriminated union: `{kind:'junction', item_id:'junction:character_book:<id>:<related>', ...}` and `{kind:'walkitem', item_id:'walkitem:<uuid>', ...}`; `_parse_item_id` dispatches on the prefix and rejects cross-type dispatch. Junction provenance moves off `source LIKE '%walk_name%'` onto a stable `walk_name` column (add the column to junction tables or a metadata annotation table per CONTRACTS' noted deviation).
  - **Unit semantics** (P3-3): one unit per `transaction()` at heartbeat cadence; a failed unit rolls back only that unit's writes; earlier committed units persist — stated in the walk contract.
  - **Single-speaker pinned to ONE boundary** (P3-4): `book.single_speaker` flag enforced at `_build_voice_config`/`_build_chunks` (render-only). `export_annotated_script` stays faithful (editor + review context preserve real speakers). **Order pinned** (X-6): resolve per-walk overrides first, then apply single-speaker mapping — override wins.
- **Mitigations embedded:** P3-1, P3-2, P3-3, P3-4, X-3, X-4, X-6.
- **Evidence:**
  - https://kafka.apache.org/documentation/#compaction — log compaction keeps old values until the new value lands; the counter's own citation, now encoded: supersede at completion means the destructive update never precedes the replacement data.
  - https://microservices.io/patterns/data/saga.html and https://www.infoq.com/articles/saga-orchestration-outbox/ — saga/compensation discipline: "each previously applied local transaction must be able to be undone"; completion-time supersede avoids needing compensation because the failure path mutates nothing. https://rockthejvm.com/articles/never-call-apis-inside-database-transactions — "don't call external APIs during the request" + Result-Table undo tracking, the same boundary as LLM-outside/write-inside feeding review items.
  - https://www.sqlite.org/lang_transaction.html — rollback discards all statements since BEGIN (all-or-nothing), the basis for unit-granularity semantics stated in the walk contract.

### FP4: Per-Span Preview with AbortError-Tolerant Singleton (Supersedes P4)

- **Final pattern:** Mode contract unchanged (individual → `render_chunk` rows + seekable per-span preview; batch → whole-book playback only; FP2's evicted rows never render preview affordances). The singleton player's API becomes `stopThenPlay(url, offsetMs)`:

  1. If anything is playing: `await` its stop (pause + clear `onended`) before starting the next — serialized start/stop, so `play()` is never interrupted by `pause()` mid-await.
  2. On a fresh `src`: `set src → load() → await loadedmetadata → set currentTime(offsetMs) → play()`. Seek is only ever attempted after metadata is loaded.
  3. Every `play()` promise rejection is classified: `AbortError`/`NotAllowedError` → benign (no error state, no UI flip); anything else → error state. The sequence driver catches `NotAllowedError` specifically: pause the sequence, surface "tap to continue".
  4. The element is created by an injectable factory (module wires the singleton once) so tests can stub it — and `frontend/test/setup.ts` stubs `HTMLMediaElement.prototype.play/pause/load` for the five other `new Audio()` call sites (P4-4/P8-2).
- **Mitigations embedded:** P4-1, P4-2, P4-3, P4-4.
- **Evidence:**
  - https://developer.chrome.com/blog/play-request-was-interrupted — official semantics of the `play()`→`pause()` AbortError rejection; the classification rule is derived from it.
  - https://developer.mozilla.org/en-US/docs/Web/API/HTMLMediaElement/loadedmetadata_event — metadata must be loaded before `duration`/`seekable` are valid; the load→metadata→seek→play ordering.
  - https://developer.mozilla.org/en-US/docs/Web/Media/Guides/Autoplay — script-initiated chained play is autoplay and may be blocked; the "tap to continue" fallback.
  - https://github.com/jsdom/jsdom/issues/2155 and https://github.com/promptfoo/promptfoo/blob/6f66bc7e/src/app/src/setupTests.ts — jsdom does not implement media playback; the canonical prototype-stub pattern.

### FP5: Overlay Config with Raw-JSON Merge Discipline (Supersedes P5)

- **Final pattern:** Save path: parse raw JSON → validate known keys through `AppConfig` (validation only — its output is **never** serialized into the file) → deep-merge unknown paths from the raw parse at every nesting level → stamp `schema_version` → atomic write. Rules:

  - **Merge raw JSON only.** `AppConfig.model_dump()` is never a merge source (default-bloat + key resurrection, P5-1). If any pydantic object must be dumped, `exclude_unset=True` is a hard rule.
  - **Pydantic never sits between raw parse and merge** (P5-2); a byte-stable round-trip test (`save → load → save` yields identical bytes for unknown keys) is enforced in CI — the test owns the P5 contract.
  - **Resolution snapshot:** `resolve_task_config(task, storage, book_id)` resolves per call (no module-level cache) and snapshots the resolved override set at walk-unit start, so one run is internally consistent even if the user saves config mid-walk (P5-3).
  - **Write serialization:** a `threading.Lock` in the config endpoint serializes read-merge-write (P5-4).
- **Mitigations embedded:** P5-1, P5-2, P5-3, P5-4.
- **Evidence:**
  - https://pydantic.dev/docs/validation/2.3/usage/serialization/ — `model_dump` includes default values for unset fields unless excluded; the root of the default-bloat trap.
  - https://github.com/pydantic/pydantic/pull/12944 — extras dropped on dump through `extra='allow'`/`'forbid'` models; the reason the merge must never flow through a pydantic object.
  - https://github.com/pydantic/pydantic-settings/commit/61e0b46 — shallow merge loses nested unknowns; motivates the recursive raw-JSON carry-forward.
  - https://github.com/worktrunk/worktrunk/pull/2180 — production precedent: preserve unknown config keys across save (A4-r's original citation, still load-bearing).

### FP6: FFMETADATA1 Export with Single-Probe Chapter Timing (Supersedes P6)

- **Final pattern:** The three-phase export (concat → metadata → mux) stands, with four risk adjustments:

  - **Chapter timing from one ffprobe** of the concatenated audio; the final chapter's END is clamped to the probed track duration (P6-1). No per-chunk duration accumulation.
  - **Filter path normalizes every input:** per-input `aresample=async=1:first_pts=0` + `asetpts=PTS-STARTPTS` before the concat, so sample-rate mismatch cannot silently pitch-shift (P6-2). The homogeneous steady state (uniform TTS WAVs) stays on the concat **demuxer** with `list.txt` — the scalable path for 1000+ chunks (a 1000-input `filter_complex` is unwieldy).
  - **Metadata generator is CI-validated:** a test generates FFMETADATA1 and runs ffmpeg on a 2-chunk fixture, asserting exit 0 and a readable m4b (P6-3) — the only way to catch malformed ffmetadata (`;FFMETADATA1` header, TIMEBASE, integer ms).
  - **Chapter mapping flags both required** (`-map_chapters 1` + `-map_metadata 1`); cover/artist tags re-specified in the ffmetadata file because ffmpeg drops unrecognized tags on metadata mapping; `-movflags disable_chpl` optional for Apple-only targets (P6-4).
- **Mitigations embedded:** P6-1, P6-2, P6-3, P6-4.
- **Evidence:**
  - https://github.com/sandreas/m4b-tool/issues/71 — ffmpeg's unreliable length detection on import; motivates probing the concatenated file once rather than trusting per-chunk sums.
  - https://charleswiltgen.github.io/TagLib-Wasm/guide/chapters — MP4 chapters are start-time-only; the last END is inferred from track duration, so an out-of-range END is *trusted* and wrong — the clamp.
  - https://ffmpeg.org/faq.html and https://trac.ffmpeg.org/wiki/Concatenate — concat filter inputs must share sample rate/channel layout; normalize with aresample first. https://ffmpeg.org/ffmpeg-formats.html — demuxer homogeneity requirement and ffmetadata grammar.
  - https://www.reddit.com/r/ffmpeg/comments/1akyiq0/trying_and_failing_to_add_chapter_metadata_to_m4b/ — missing header/TIMEBASE → "Tag tx3g incompatible with output codec id" / "Could not write header"; the CI fixture catches exactly this.

### FP7: Bounded-Range Serving with Pre-Checked 404 + Inline Disposition (Supersedes P7)

- **Final pattern:** `FileResponse` remains the primary range mechanism (native HEAD/If-Range/206/416/open-ended/suffix — P7-3 confirmed). Two concrete changes:

  1. **404, not 500, on missing files:** the endpoint pre-checks `os.path.exists(path)` and returns 404, AND serves through a small `FileResponse` subclass whose `__call__` wraps `super().__call__` catching `RuntimeError`/`FileNotFoundError` → HTTPException(404). The subclass is mandatory, not belt-and-suspenders: the handler-level try/except cannot catch the error because it is raised in Starlette's send path after the handler returns (SO evidence below).
  2. **Disposition discipline:** player/preview/chunk endpoints pass no `filename` (or `content_disposition_type="inline"`) so `<audio src>` streams inline; download endpoints keep `filename=` for attachment behavior.
  3. **Fallback parser deleted** (P7-2): the CI pin `starlette>=0.49.1` (GHSA-7f5h-v6xp-fcq8; installed 1.3.1) makes a hand parser dead code, and keeping a divergent one risks 400-vs-416 drift. The pin is the guard; bypassing it is a documented CI-discipline limitation.
- **Mitigations embedded:** P7-1, P7-2, P7-3 (confirmed), X-2 (with FP2).
- **Evidence:**
  - https://github.com/Kludex/starlette/issues/979 — `FileResponse` catches `FileNotFoundError` to raise `RuntimeError("File at path ... does not exist")`; the counter's verification, independently confirmed.
  - https://stackoverflow.com/questions/74103330/fastapi-fileresponse-not-entering-the-except-block — "The exception is raised *after the return* … there is nothing for you to catch"; the answer prescribes exactly the two mechanisms this pattern adopts (pre-check `os.stat` or subclass `__call__`).
  - https://github.com/Kludex/starlette/security/advisories/GHSA-7f5h-v6xp-fcq8 — the O(n²) Range DoS fixed in 0.49.1; the pin's raison d'être.
  - https://www.starlette.io/responses/ — official `FileResponse` range behavior (206/416/`Accept-Ranges: bytes`).

### FP8: Testing Strategy with File-Backed Reconciliation Fixtures (Supersedes P8)

- **Final pattern:** Three layers, extended per the counter's findings:

  1. **Backend:** `InMemorySQLiteAdapter` (fresh `init_db()` per test, `dependency_overrides` cleared in teardown) for logic tests. **One file-backed `SQLiteAdapter(tmp_path)` fixture** for startup-reconciliation/crash tests: write `running` rows, close the connection, re-open, run `reconcile_stale_runs()`, assert `interrupted` — impossible on `:memory:` because there is no process/crash boundary (P8-3). New transaction-wrapper tests cover the owner-thread guard: open a txn on thread A, assert thread B's write raises `ConcurrentTransactionError`; assert same-thread nested join, commit, rollback (FP1/P8 interaction). `tests/pipeline/test_export.py` closes the `download/{job_id}` zero-test gap: present-file 200, zip fallback, unknown job 404, range 206 + `Content-Range`, malformed range 416, missing-file 404 (FP7).
  2. **Frontend:** `frontend/test/setup.ts` ships the `HTMLMediaElement.prototype.play/pause/load` stubs once (P8-2). Polling tests use the hang-proof recipe (P8-1): `vi.useFakeTimers({ toFake: ['setTimeout','setInterval'] })` + fully mocked fetch + `await vi.advanceTimersByTimeAsync(2000)`; `afterEach(vi.useRealTimers)`.
  3. **Documented non-coverage** (X-8): WAL, `busy_timeout`, multi-connection behavior are deliberately out of scope for the single-connection design; the suite states this so a future second-connection refactor must add its own fixtures. The CI dist-check (`npm run build && git diff --exit-code app/static/dist/`) stays (P8-4 confirmed).
- **Mitigations embedded:** P8-1, P8-2, P8-3, P8-4 (confirmed), X-8, plus the FP1 guard tests and FP7 404 tests.
- **Evidence:**
  - https://vitest.dev/guide/mocking/timers — `advanceTimersByTimeAsync` for async callbacks; https://github.com/vitest-dev/vitest/issues/7314 — fake timers + fetch hang in Vitest 3; the `toFake` allowlist avoids it.
  - https://github.com/tiangolo/fastapi/issues/3906 — SQLite `:memory:` is per-connection; the per-test fresh adapter and the file-backed fixture both derive from it.
  - https://fastapi.tiangolo.com/advanced/testing-dependencies/ — `dependency_overrides` mechanics and teardown discipline.
  - https://github.com/promptfoo/promptfoo/blob/6f66bc7e/src/app/src/setupTests.ts — the canonical `HTMLMediaElement.prototype.play = vi.fn(() => Promise.resolve())` stub.

---

### Final Capability & Concern Coverage

| Capability | Final pattern carriers |
|---|---|
| 1. Result/audio surface + per-span preview | FP1 (`render_chunk` rows), FP2 (stable run-dir paths + eviction), FP4 (mode contract + singleton player), FP7 (range serving, inline disposition) |
| 2. Real progress + useful cancellation incl. walk cancel | FP1 (job/walk_run state, startup-only reconciliation, `is_cancel_requested` dispatcher), FP2 (manifest derived) |
| 3. Polished M4B export + MP3/Audacity | FP6 (+ FP2 run dir; X-7 job-row ownership) |
| 4. Voice config editing (style/ref-audio/5 types/alias) | Backend: FP5 config; **frontend UI still unclaimed — FLAGGED** (unchanged from Turn 5: no pattern designs the `voices.ts` editing surface; needs a dedicated frontend design task) |
| 5. Surface 2g/2h/2i review items | FP3 (materialized items, supersede at completion, discriminated union) |
| 6. Optional prompt/generation-param overrides | FP5 (`resolve_task_config` + recursive round-trip) |
| 7. Saved scripts, single-speaker, contextual review, undo, pause-after, sequence | FP3 (single-speaker pinned, undo via `prior_value`, contextual payloads), FP4 (sequence playback), FP1 (pause-after state), A4-r snapshots (saved scripts) — **named-scripts UI residual, FLAGGED** |
| 8. Preserve pipeline-only architecture | FP8 (guardrail tests), FP1 (CONTRACTS module split) |

| Surviving concern | Status |
|---|---|
| C1 heartbeat reconciliation false-positives | **ADDRESSED** — FP1 startup-only reconciliation, no on-read sweeper |
| C2 walk-side "same transaction" asserted, not mechanized | **ADDRESSED** — FP1 `transaction()` with owner-thread guard (enforced, not conventional) |
| C3 retention GC vs project_snapshot references | **ADDRESSED** — FP2 reference-derived eligibility + same-sweep row tombstones |
| C4 capability 4 frontend unclaimed | **NO PATTERN — FLAGGED** (frontend design task; unchanged from Turn 5) |
| C5 per-span preview batch-mode-dependent | **ADDRESSED** — FP4 explicit mode contract + evicted-row handling |
| C6 single-speaker regressed to snapshot-variant | **ADDRESSED** — FP3 `book.single_speaker` pinned, enforced at `_build_*`, override-wins ordering (X-6) |

**Remaining gaps (flagged, unchanged from Turn 5):** capability 4's voice-config editing UI and capability 7's named saved-scripts UI are frontend-surface work with no pattern; both are design tasks, not implementation gaps. Everything else is covered by FP1–FP8, with all three HIGH risks (P1-1, P1-2, P3-1) and the factual correction (P1-3) resolved.

---

## Open Risks & Human Questions

> Appended by **rnd-counter-improver** (Turn 8). Assessment of the Turn 7 "## Final Patterns" response to my Turn 6 "## Pattern Risks". Verdicts grounded in the log's own evidence plus fresh searches (SQLite shared-connection production patterns; TTS batch-vs-per-utterance consistency research).

### Turn 7 Response Verdict Table

| Turn 6 risk | Verdict | What remains (if any) |
|---|---|---|
| P1-1 cross-thread txn join | **PARTIALLY ADDRESSED** | Owner-thread guard + API 503/retry is correct and converts silent join into a visible condition. **Unhandled direction:** the guard raises `ConcurrentTransactionError` *inside walk code* when an API-held write txn overlaps a walk-unit write; no catch/retry is prescribed for the walk path. A milliseconds-wide collision during a review accept / PUT voice can fail a whole walk unit (or the walk, via WalkRunner's catch-Exception→failed). |
| P1-2 LLM-inside-BEGIN IMMEDIATE | ADDRESSED | LLM-outside/write-inside makes the freeze structurally impossible. |
| P1-3 isolation_level factual error | ADDRESSED | Explicit `isolation_level=None` + `execute("COMMIT")` is flip-proof; CI pin note adequate. |
| P1-4 dual cancel channels | ADDRESSED | Single dispatcher, DB authoritative, file a mirror. |
| P2-1 GC rows dangle | ADDRESSED | Row tombstones in the same sweep close the dangling class. Residual (LOW): evicted/expired rows accumulate with no archival plan — negligible for small-medium books; flag for future. |
| P2-2 FileResponse RuntimeError→500 | ADDRESSED | Pre-check + subclass verified against the Starlette send-path behavior (Kludex/starlette #979, SO 74103330). |
| P2-3 dual progress authority | ADDRESSED | Rows = truth; manifest = derived cache. Resolved cleanly. |
| P3-1 supersede-at-start loses candidates | **PARTIALLY ADDRESSED** | Failure/cancel path fixed (nothing superseded). **Remaining gap:** a *completed-but-partial* re-walk still supersedes ALL prior pending items of a kind (`WHERE ... status='pending' AND kind=?`), including targets the new run did NOT regenerate. If a re-run legitimately covers a smaller target set, those old pending items are superseded with no replacement — the same orphaning, moved from "failed" to "partial-completion". Correctness depends on walk semantics (Q4). |
| P3-2 dual-path item_id namespace | ADDRESSED | Discriminated union + prefix dispatch; provenance column replaces `source LIKE`. |
| P3-3 retro-fitted txn semantics | ADDRESSED | Unit granularity at heartbeat cadence, rollback scope stated in contract. |
| P3-4 single-speaker boundary | ADDRESSED | Pinned to `_build_*` (render-only); export stays faithful; override-wins pinned (X-6). |
| P4-1 play() interrupted AbortError | ADDRESSED | `stopThenPlay()` + rejection classification; matches the Chrome play-request-interrupted semantics. |
| P4-2 seek before loadedmetadata | ADDRESSED | Explicit load→metadata→seek→play ordering, unit-tested. |
| P4-3 autoplay chained play() blocked | ADDRESSED | Documented UX limitation with "tap to continue" — honest. |
| P4-4 jsdom no HTMLMediaElement | ADDRESSED | setup.ts stubs shipped as part of FP8. |
| P5-1 model_dump default bloat | ADDRESSED | Raw-JSON merge only; `exclude_unset` hard rule. |
| P5-2 extras dropped through pydantic | ADDRESSED | Pydantic never sits between parse and merge; byte-stable round-trip test owns the contract. **Guardrail note:** the round-trip fixture MUST include unknown keys, and AppConfig's known-field validation must tolerate them (`extra='ignore'` semantics) or the validation pass rejects the very keys the carry-forward preserves. |
| P5-3 resolve_task_config blast radius | ADDRESSED | Per-call resolution + unit-start snapshot. |
| P5-4 concurrent config writes | ADDRESSED | Endpoint lock. |
| P6-1 chapter END rounding drift | ADDRESSED | Single ffprobe + clamp. |
| P6-2 sample-rate mismatch | ADDRESSED | Per-input aresample/asetpts; demuxer steady state. |
| P6-3 malformed ffmetadata → hard 500 | ADDRESSED | CI fixture runs generator + ffmpeg. |
| P6-4 chapter mapping quirks | ADDRESSED | Both flags, tags re-specified, disable_chpl optional. |
| P7-1 missing file → RuntimeError 500 | ADDRESSED | Pre-check + subclass → 404; inline disposition for player endpoints. |
| P7-2 fallback parser divergence | ADDRESSED | Fallback deleted; CI pin is the guard (documented limitation). |
| P7-3 HEAD/If-Range/suffix | ADDRESSED | Confirmed native. |
| P8-1 fake timers + fetch hang | ADDRESSED | `toFake` allowlist + mocked fetch + `advanceTimersByTimeAsync` + `afterEach(useRealTimers)` — the exact recipe. |
| P8-2 jsdom media stubs | ADDRESSED | Shipped in setup.ts. |
| P8-3 :memory: conceals crash recovery | ADDRESSED | File-backed fixture for reconciliation; `:memory:` for logic. |
| P8-4 CI dist-check | ADDRESSED | Confirmed. |
| X-1 rows vs manifest | ADDRESSED | Rows authoritative. |
| X-2 GC vs rows vs preview | ADDRESSED | Tombstones + 404 + inline disposition. |
| X-3 supersede + failed run | **PARTIALLY ADDRESSED** | Tracks P3-1's remaining partial-completion gap. |
| X-4 interrupted-run → item lifecycle | ADDRESSED | Items stay pending; supersede never targets running runs. |
| X-5 three cancel channels | ADDRESSED | One dispatcher (folded into P1-4). |
| X-6 override vs single-speaker order | ADDRESSED | Override wins, pinned in FP3. |
| X-7 export without a job row | ADDRESSED | Every export owns a job row first. |
| X-8 :memory: test-model gap | ADDRESSED | Documented non-coverage + file fixture. |

**Verdict counts: 31 ADDRESSED / 3 PARTIALLY ADDRESSED (P1-1, P3-1, X-3) / 0 NOT ADDRESSED.**

---

### Unresolved Risks

- **Risk: Walk-side collision on `ConcurrentTransactionError` is unhandled.** **Severity: MEDIUM.** **Trigger:** any API write endpoint (review accept/reject/override, PUT voice, config save) opens a write transaction while a walk unit is mid-write — a milliseconds window per unit, but walks run for minutes, so the window recurs hundreds of times. The exception propagates through the walk's `transaction()` rollback into WalkRunner's catch-Exception→`failed` path → a user clicking "accept" at the wrong millisecond fails the entire walk. The Improver prescribed retry for the API middleware only; the walk path needs the same: catch `ConcurrentTransactionError` in the unit loop and retry the write phase (idempotent UPSERTs make retry safe). This is the standard single-writer queue/retry pattern — [emschwartz.me PSA](https://emschwartz.me/psa-your-sqlite-connection-pool-might-be-ruining-your-write-performance/) ("single writer connection with writes queued at the application level" is the production fix for exactly this contention class) and [fastapi-patterns async-database-sessions](https://fastapi-patterns.com/async-background-tasks-observability/async-database-sessions/) ("A write silently does not persist despite a 200 — a background task borrowed the request's session and failed" — the same background/request interference class). Note the guard-vs-`busy_timeout` choice is vindicated: [zeroclarkthirty](https://zeroclarkthirty.com/2024-10-19-sqlite-database-is-locked) shows busy_timeout cannot rescue the lock-upgrade case, so the explicit guard is the right tool — it just needs the walk-side retry.
- **Risk: Completed-but-partial re-walk can still orphan review candidates.** **Severity: MEDIUM.** **Trigger:** a 2g/2h/2i re-walk that *completes* against a smaller target set than the prior run (data changed between runs; the walk only regenerated a subset). Supersede-all-of-kind marks every prior pending item superseded, including items the new run never touched — those candidates vanish unreviewed. The Kafka-compaction analog the design cites only removes keys the new segment *replaces*; supersede-all is a coarser operation than the model implies. Whether this bites depends on whether Alexandria's walks guarantee full re-coverage of their kind on every run — an empirical question (Q4), not a settled one.
- **Risk: The 503 + `Retry-After` + frontend-retry contract is new, unowned scope.** **Severity: LOW–MEDIUM.** **Trigger:** any write endpoint collision (same window as the first risk). The frontend `api.ts` has no retry logic today; every write call site needs a one-shot retry + correct error surfacing. If the retry also collides (rapid double-click), the user sees a 503 that never resolves. This is a deliberate, documented consequence of FP1 (listed under Fundamental Limitations) — but it is still unimplemented frontend scope that belongs in a plan, not just a design note.
- **Risk: Evicted/expired rows accumulate without archival.** **Severity: LOW.** **Trigger:** steady-state GC over months. Tombstoned `render_chunk`/`render_job` rows grow the DB; harmless at small-medium book scale, but the row tombstone design has no archival story. Flag for a future maintenance pass, not this feature.
- **Risk: P5 round-trip test could pass while unknown keys are still dropped.** **Severity: LOW (guardrail).** **Trigger:** an implementer writes the byte-stable test with a fixture that has no unknown keys, or AppConfig is built with `extra='forbid'`. The test as specified ("identical bytes for unknown keys") only catches the bug if the fixture actually contains unknown keys and the validation pass tolerates them. Make both explicit in FP5's test contract.

---

### Human Judgment Questions

#### Q1: Batch-mode coarse progress vs always-individual mode — what is the acceptable UX and quality tradeoff?
- **Context:** `tts.py` is untouchable: `generate_batch` has no per-chunk callback, so batch mode yields coarse progress and whole-book-only playback, while individual mode enables per-chunk progress + per-span preview everywhere. FP4 documents this as a constraint-derived limitation. The open question is whether individual mode should become the default or even the *only* mode — and what that costs.
- **At stake:** capability 1's per-span preview (only individual mode) vs render efficiency and, critically, **voice consistency**. `render_audiobook` passes `batch_seed=BATCH_SEED_RANDOM (-1)` to `generate_batch`; individual mode calls `generate_voice` per chunk with no seed continuity — per-generation TTS is a fresh sample, so a 1000-chunk individual render accumulates drift and the per-span preview will *not* sound identical to a batch render of the same text.
- **Evidence:** TTSAudit — [Why Your Text-to-Speech Voice Changes Between Files](https://ttsaudit.com/blog/why-text-to-speech-voice-changes-between-files) (probabilistic TTS: each generation is a new sample; drift accumulates with generation count; fixed seeds are the first mitigation); Deepgram — [Batch TTS guide](https://deepgram.com/learn/batch-text-to-speech-scalable-voice-generation-guide) ("voice embedding stability degrades after approximately 5 consecutive generations" — segment boundaries and checkpoint reloads are production practice); [Batch vs Streaming TTS](https://www.linkedin.com/posts/shashank-mishra-6a870b212_ai-tts-streamingtts-activity-7465790470279925760-SZd-) ("Batch TTS usually provides smoother global prosody, higher consistency, better long-form coherence").
- **Recommendation:** Keep batch as the default for whole-book renders (consistency + efficiency), keep individual as an explicit per-span-preview mode, and **document that preview audio may differ subtly from the batch final** — the DD should state this so users aren't surprised when the preview voice ≠ the shipped book. If per-span preview fidelity becomes a product priority later, revisit after a TTS-engine quality audit. **Confidence:** MEDIUM (the drift evidence is strong generically, but Alexandria's engine behavior — whether `generate_voice` with an unset seed drifts meaningfully — is unmeasured).

#### Q2: Retention/GC policy numbers — what defaults are defensible?
- **Context:** FP2 mechanizes reference-derived GC + tombstones but sets no numbers. Two sub-questions: (a) **file retention** (`job_retention_days`-style) — how long do completed run dirs survive before deletion; (b) **heartbeat threshold** — the design's own "startup-only reconciliation" means heartbeat is informational only (it never flips a live job), so *no liveness threshold is needed at all* — the Turn 7 text already implies this; confirm it explicitly so no implementer adds a threshold-based reaper that re-introduces C1's false-positive risk.
- **At stake:** user data (downloaded-but-old M4Bs' source dirs), disk usage on a single host, and the "audio expired" UX when a user returns to an old job.
- **Recommendation:** Files: default 7 days post-completion, configurable via env; at expiry, tombstone rows and delete files in the same sweep (FP2). Rows: never time-deleted (tombstoned only) — rows are cheap and preserve history/undo. Heartbeat: document as observability-only; no threshold. **Confidence:** MEDIUM — 7 days is a defensible default for a single-host desktop-scale app, but it is a product decision (how long must a finished render be re-downloadable/exportable?).

#### Q3: Review confidence band 0.5–0.7 — centralized or per-kind calibration?
- **Context:** FP3 centralizes `REVIEW_CONFIDENCE_MIN/MAX` (0.5/0.7). With walk-side items (2g voice_profile, 2h voice_assignment, 2i instruction) entering the queue, the cost of a wrong decision is asymmetric by kind: **false-accept** on 2g puts a wrong voice into 2h → audible defect in the final render (expensive: re-run + re-render); **false-reject** costs one re-audition LLM pass (cheap). For 2i instruction, false-accept is a delivery nuance error (cheap). The band was designed for junction confidence; walk-side items may need kind-specific thresholds.
- **At stake:** capability 5's review quality vs review burden; the hard constraint "no degraded-confidence auto-accept" must hold regardless.
- **Recommendation:** Keep centralized 0.5–0.7 for v1 (simple, matches the constraint), but **log accept/reject outcomes per kind** so a data-driven per-kind calibration (e.g., 2g band widened, 2i band narrowed) can be decided after real usage — do not pre-complicate v1 with per-kind bands on zero data. **Confidence:** MEDIUM.

#### Q4: Supersede scope — supersede-all-of-kind vs per-target, given partial-completion re-walks
- **Context:** P3-1's remaining gap. Supersede-all (`WHERE status='pending' AND kind=?`) is correct if a completed re-walk implies "the walk reconsidered every candidate of that kind." It is wrong if a re-walk legitimately covers a subset (target set shrank because earlier data changed). The fix is per-target supersede: supersede only items whose target_id appears in the new run's committed set.
- **At stake:** capability 5's core promise — low-confidence items must not silently vanish. Wrong choice either orphans candidates (supersede-all with partial runs) or retains stale candidates forever (per-target if walks actually do re-cover everything — stale items clutter the queue and re-reviews duplicate).
- **Recommendation:** Adopt per-target supersede as the safe default (the faithful Kafka-compaction analog: remove only what the new segment replaces). If the walk contracts can *prove* full re-coverage per kind (2g iterates all characters needing profiles), supersede-all is acceptable — but that proof is a walk-semantics decision the DD-Author must make from the walk implementations, not from the queue design. **Confidence:** MEDIUM — evidence says per-target is safer; the walk-coverage question is empirical.

#### Q5: Capability 4 frontend voice-config editing UI — scope and effort decision
- **Context:** Flagged unclaimed in Turns 5 and 7 (twice). Backend exists (`PUT /api/pipeline/voices/{id}`, CRUD + preview in `api_voices`); `voices.ts` has only create-card + type badge + preview. The missing surface: style text, ref-audio upload, 5-type editing, alias mapping, narrator override.
- **At stake:** capability 4 is not deliverable without this UI — it is the *only* capability with zero pattern coverage, and the design fight cannot close while it is deferred.
- **Recommendation:** Commit to a **minimal viable form** in `voices.ts` (style/ref-audio/5-type/narrator override + preview wiring) as a named small task in the implementation plan; explicitly defer alias-map *picker* UI (alias_of editing can be a dropdown of existing voices) to a follow-up. The alternative — leaving it out of scope — must be an explicit product decision to downgrade capability 4, not a silent gap. **Confidence:** HIGH that the UI is required; MEDIUM on exact scope.

#### Q6: Capability 7 named saved-scripts — is a naming/list surface required, or does auto-named snapshot suffice?
- **Context:** A4-r snapshots give save/restore; the residual is the *named, listable script* UX (L4 lost `/api/scripts*`). Auto-named snapshots (book + timestamp) are listable; explicit user names add value but also add UI + validation + rename plumbing.
- **At stake:** how capability 7's "saved scripts" is interpreted for acceptance. If snapshot-name default counts, the feature is done with A4-r; if named scripts are required, it needs a UI task.
- **Recommendation:** For v1, treat auto-named snapshots as satisfying "saved scripts" and add a **rename affordance** (cheap: one PATCH on snapshot name) rather than a full named-script manager. State this interpretation explicitly in the DD so acceptance criteria match scope. **Confidence:** MEDIUM — the capability text is ambiguous by design; a product owner should ratify the interpretation.

#### Q7: Single-speaker enforcement depth — render-boundary only vs write-time blocking
- **Context:** FP3 pins enforcement at `_build_voice_config`/`_build_chunks` (render-only); `export_annotated_script` stays faithful. The alternative is write-time blocking: when `book.single_speaker=1`, reject/rewrite character_span speaker writes. Render-only means the DB can hold multi-speaker data while the book renders single-speaker; write-time means the data can't even exist.
- **At stake:** the "explicit only, no auto-cascade" philosophy vs data consistency. Write-time blocking prevents a user from preparing multi-speaker data and then toggling single-speaker for a quick render — a real workflow (audition multi-voice, ship single-voice).
- **Recommendation:** Render-boundary only (FP3 as written) — it matches the pipeline's explicit-only philosophy, keeps the editor faithful, and makes the flag a render-time choice. Do not add write-time blocking. **Confidence:** HIGH.

#### Q8: Config round-trip — raw-JSON merge vs pydantic migration
- **Context:** FP5 chose raw-JSON merge with pydantic validation-only. The alternative is migrating AppConfig to `extra='allow'` + `__pydantic_extra__` everywhere so pydantic itself preserves unknowns.
- **At stake:** the config data-loss bug class (L4/L5). Pydantic's extras handling has a documented bug history (extras dropped on dump through forbid/ignore models; nested extra='allow' tuple-wrap garbage) — the migration path is *more* risk for *less* benefit.
- **Recommendation:** Raw-JSON merge (FP5 as written); do not migrate AppConfig to extras-preserving models. The byte-stable round-trip test is the contract; add the unknown-key fixture guardrail from Unresolved Risks. **Confidence:** HIGH.

#### Q9: Owner-thread guard + 503/retry vs per-thread connections — and where the walk-side retry lives
- **Context:** FP1 chose the guard over giving the walk/render thread its own connection. The alternative (per-thread connections) eliminates the collision class entirely but invalidates X-8's single-connection test fidelity and introduces multi-connection WAL behavior that the suite deliberately does not test.
- **At stake:** the residual walk-side collision risk (Unresolved Risk #1) vs architectural simplicity. The evidence ([emschwartz.me](https://emschwartz.me/psa-your-sqlite-connection-pool-might-be-ruining-your-write-performance/): SQLite is single-writer; the production fix is one application-level writer with queued writes) supports the guard *plus* treating walk writes as a queue.
- **Recommendation:** Keep the guard (chosen) and **add the walk-side retry**: catch `ConcurrentTransactionError` in the walk-unit loop, back off ~50–100ms, retry the write phase (idempotent UPSERTs); after N retries (e.g., 3), fail the unit. This is a ~10-line addition to FP1's transaction discipline and closes the only HIGH-adjacent residual. Do not move to per-thread connections. **Confidence:** HIGH.

---

### Summary

- **Verdict counts: 31 ADDRESSED / 3 PARTIALLY ADDRESSED / 0 NOT ADDRESSED.**
- The Turn 7 response is the strongest turn of this fight: every T6 finding got a mechanical fix or an honest documented limitation, and the three HIGH risks are genuinely mitigated in their primary direction.
- **The two genuine residuals** are both *direction* gaps, not re-litigations: (1) the owner-thread guard protects API writes from walk transactions but not walk writes from API transactions — add the walk-side retry; (2) supersede-at-completion fixes the failed-run path but not the completed-partial path — decide per-target vs supersede-all from walk semantics (Q4).
- **Nine human-judgment questions** are surfaced — the ones evidence cannot settle: batch-vs-individual UX/quality tradeoff, retention defaults, confidence-band calibration, supersede scope, the two flagged-but-unclaimed frontend surfaces (cap 4 UI, cap 7 named scripts), single-speaker depth, config-round-trip approach, and the guard-vs-connection topology. The three with HIGH-confidence recommendations (Q7 single-speaker render-only, Q8 raw-JSON, Q9 guard+walk-retry) are safe to ratify without further research; Q1, Q4, and Q5 carry real product/effort decisions that the RnD-Manager should own.
