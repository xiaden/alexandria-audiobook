# Universal Upgrade (DD-universal-upgrade) — Implementation Decomposition

**Design doc:** [`artifacts/designs/pending/DD-universal-upgrade.md`](../../pending/DD-universal-upgrade.md) (v1, 2026-08-06)
**Adversarial record:** [`artifacts/designs/process/ADVERSARIAL-universal-upgrade.md`](../../process/ADVERSARIAL-universal-upgrade.md) (FP1-FP8, open risks Q1-Q9)
**Upstream feature:** [`../epub-audiobook-pipeline-rewrite/README.md`](../epub-audiobook-pipeline-rewrite/README.md) — Plan Q terminal, pipeline-only mode active.
**Contracts ledger (authoritative schema/API registration):** [`../epub-audiobook-pipeline-rewrite/CONTRACTS.md`](../epub-audiobook-pipeline-rewrite/CONTRACTS.md) § Universal Upgrade (lines 906-948) — ALREADY REGISTERED by the DD. This feature's ledger is [`CONTRACTS.md`](CONTRACTS.md).
**Status:** Decomposed — 10 plans (A-J), 6 execution rounds, 4 ship groups. **Plan K (parity-gap closure) added as follow-up** closing three Plan F/QA gaps (mp3/audacity serving routes, 503 retry mapping, pause disclosure).
**Last updated:** 2026-08-07

---

## Feature Goal

Restore the 8 pre-rewrite utilities (audio surface, real progress/cancellation, polished export, voice-config editing, review surfacing, prompt/generation overrides, saved snapshots + iteration UX, pipeline-only discipline) as **pipeline-native capabilities**. No legacy endpoints, modules, toggles, or shims. `app/tts.py` remains byte-for-byte untouchable. Guard suite `tests/pipeline/test_legacy_removed.py` stays 12/12 green.

## Parts Table

| Letter | Title | Depends On | Layers | ~Weight |
|--------|-------|-----------|--------|---------|
| A | Schema & Transaction Foundation | — | adapter.py, schema.py, tests | ~8K |
| B | Render & Walk-Run Persistence | A | api_export.py, api_walks.py, walks/runner.py, adapter.py, tests | ~22K |
| C | Artifact-First Run Dirs, GC & Export Backend | A, B | tts_integration.py, api_export.py, app.py (RENDER_ROOT/GC wiring), tests | ~11K |
| D | Unified Review Queue & Supersede | A, B | review.py, api_review.py, walks/2g,2h,2i, tests | ~19K |
| E | Audio Surface, Singleton Player & Tab Navigation | B, C | editor-pipeline.ts, player.ts, main.ts, index.html, vitest.setup.ts, tests | ~16K |
| F | Progress, Cancel & Export UI | C, E | editor-pipeline.ts, script.ts, index.html, api.ts, README.md, tests | ~24K |
| G | Overlay Config & Walk Overrides | A, B | app.py, utils.py, walks/*, setup.ts, tests | ~14K |
| H | Voice Config Edit Form | E | voices.ts, index.html, README.md, tests | ~9K |
| I | Snapshot Projects | B, E | api_operations.py, api.py, projects.ts, index.html, adapter.py, tests | ~16K |
| J | Single-Speaker, Undo & Iteration UX | D, I | tts_integration.py, editor-pipeline.ts, review.py, index.html, tests | ~17K |
| K | Parity-Gap Closure (artifact serving routes, 503 retry mapping, pause disclosure) | F (gaps), J (pause decision) | api_export.py, app.py, editor-pipeline.ts, api.ts, index.html, setup.ts, tests, README.md | ~7K |

Total ≈ 161K weighted chars, ~42 unique files, matching the DD's LARGE estimate.

## Dependency Graph

```
A (schema + transaction foundation)
└── B (render/walk persistence)
    ├── C (run dirs, GC, export backend)
    │   └── E (audio surface + tab nav) ──┬── F (progress/export UI)
    │                                    ├── H (voice edit form)
    │                                    └── I (snapshot projects) ──┐
    ├── D (review union + supersede) ────────────────────────────────┴── J (single-speaker, undo, iteration)
    └── G (overlay config)
```

- **Max dependency depth:** 3 (J: A→B→D/I)
- **Max dependencies per plan:** 2
- **Contiguous letters in execution order:** A→B→C→D→E→F→G→H→I→J→K ✓ (K is a post-J follow-up closing gaps logged in Plan F)

## Execution Rounds

| Round | Plans | Ship Group | Notes |
|-------|-------|-----------|-------|
| 1 | A | Ship 1 (backend foundation) | transaction() guard, isolation_level=None, busy_timeout, 6 tables + 3 indices + book.single_speaker |
| 2 | B | Ship 1 | walk_run/render_job/render_chunk writes, startup reconciliation, persisted cancel, walk-side retry, jobs/chunks/runs endpoints, download rewrite |
| 3 | C, D | Ship 1 | C: RENDER_ROOT run dirs, fsync, manifest, tombstoning GC, audio/chunk range endpoints, 3-phase M4B export. D: walk_review_item writes, completion-time supersede, union queue, value-restore |
| 4 | E | Ship 2 (frontend audio) | singleton player (createPreviewPlayer), per-span preview, sequence playback, **tab-navigation foundation** (evidence-based addition, see below), vitest media stubs |
| 5 | F, G, H, I | Ships 2-4 | F: progress/cancel + export UI. G: raw-JSON config merge + walk_override. H: voice edit form + alias picker. I: project_snapshot endpoints + projects tab |
| 6 | J | Ship 4 | single-speaker render boundary + toggle, undo wiring (value-restore + snapshot restore), pause-after verification, doc-drift archive |
| 7 | K | Ship 4 (follow-up) | parity-gap closure: GET /export/mp3/{job_id} + GET /export/audacity/{job_id} serving routes (rows=truth), ConcurrentTransactionError → 503 + Retry-After: 5 app exception handler, pause capability disclosure (pauses_applied/pauses_message + honest UI wording) |

## Per-Part Scope

See [`SCOPE.md`](SCOPE.md) for the per-part scope matrix (purpose, files, contracts, in/out of scope, verification gates).

## Quality Gates (all plans)

Every plan embeds the full verification loop:

1. **TDD** — RED → GREEN → REFACTOR with coverage ≥ 80% on new code (backend `pytest --cov=app/pipeline`, frontend `vitest run --coverage`).
2. **Backend type check** — `python -m compileall` / `import` smoke where app import chain allows; `ruff check` on all touched files.
3. **Frontend type check** — `npx tsc --noEmit` (exit 0) in `frontend/`.
4. **Build** — `npm run build` regenerates `app/static/dist/`; CI gate `git diff --exit-code app/static/dist/` must hold.
5. **Guard suite** — `pytest tests/pipeline/test_legacy_removed.py -q` stays 12/12 green in EVERY plan.
6. **Code review** — exec-manager QA-Reviewer pass; MINOR fixes via exec-fixer; PLANNING_GAP escalates here.
7. **Security review** — required for every plan touching data/I/O (all A-J): SQL injection (parameterized adapter only), path traversal (RENDER_ROOT resolution), auth/rate limits, Range-DoS guard (starlette≥0.49.1), no legacy endpoint resurrection.
8. **Conventional commit** per plan completion.

## Evidence-Based Adjustments to DD Defaults

The DD is the source of truth; these adjustments come from verified codebase evidence at HEAD ba818de (2026-08-06) and are required for the design to be usable:

1. **Frontend tab navigation is broken (pre-existing):** nav links are plain `<a data-tab=...>` with no click handler and no bootstrap tab wiring — only `#setup-tab` is ever visible; all other tabs render into hidden DOM. Every new UI surface (voices edit form, projects tab, export UI, audio surface) lives in a hidden tab. **Fix folded into Plan E Phase 1** (tab-navigation foundation in main.ts + index.html) — evidence-based, small (~2K).
2. **vitest media stubs do not exist:** DD claims stubs in `frontend/vitest.setup.ts`; research found only `MockAudio` inside `test_voices.test.ts`. Plan E Phase 1 adds the stubs to `vitest.setup.ts` as the DD intends.
3. **`#btn-pipeline-download` is a dead DOM reference** (absent from index.html) — Plan F restores a reachable download/export surface.
4. **Smoke-check harness does not exist** (no `**/*smoke*` files) — Plan B Phase 1 re-establishes the harness location per DD test strategy.
5. **PATCH /projects/{name} (rename) not in the 10 registered endpoints** — registered here (Plan I) and appended to the upstream ledger as the DD's design decisions section specifies auto-named + rename.
6. **`render_audiobook` currently discards `generate_batch` return** (tts_integration.py:299) — Plan B/C use it for render_chunk rows (individual) / job-level (batch).
7. **setup.ts already submits `pause_between_speakers_ms`/`pause_same_speaker_ms`** (500/250 defaults) — pause-after is verification+polish in Plan J, not net-new backend.

## References

- `artifacts/designs/pending/DD-universal-upgrade.md` — source of truth
- `artifacts/designs/parts/epub-audiobook-pipeline-rewrite/CONTRACTS.md` § Universal Upgrade — registered schema/API
- `artifacts/plans/completed/TASK-epub-audiobook-pipeline-rewrite-{A..Q}-*.md` — prior feature history (Q terminal)
- `artifacts/plans/pending/TASK-universal-upgrade-{A..K}-*.md` — this decomposition's plans (A-J executed + archived; K pending)
