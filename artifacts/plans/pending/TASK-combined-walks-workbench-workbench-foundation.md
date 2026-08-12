# Task: Combined Characters & Scenes Workbench (Walks 2b–2d)

## Problem Statement

The pipeline has character rows and junctions for character discovery (walk_2b), alias resolution (walk_2c), and scene presence (walk_2d), but no scene-facing API, no safe alias-management workflow, no durable manual-decision protection across reruns, and no unified frontend for inspecting generated character/scene evidence. Existing review items are unified but incomplete, per-book walk_override has no HTTP surface, and 2b reruns can duplicate junctions. This plan delivers the combined Characters & Scenes workbench from `artifacts/designs/pending/DD-combined-walks-2b-2d-workbench.md`: a pipeline-native backend contract set plus one accessible frontend tab, keeping generated values separate from manual decisions, allocating revisions from a sole per-book `workbench_generation` row, and making reruns idempotent and non-destructive.

The build is structured as five dependency-ordered sub-tasks (S1 backend schema/domain → S2 API routes + review dispatch + S3 rerun-safe walks + S4 frontend → S5 contract registration + build output). It implements TDD, tsc, pytest with coverage, `npm run build` with a clean committed-dist diff gate, and re-runs the 12-check legacy guard to prove no pipeline-only or universal-upgrade regression.

## Dependencies

- Upstream contract: `artifacts/designs/parts/epub-audiobook-pipeline-rewrite/CONTRACTS.md` § Workbench (schema/API registration, lines 954-1071) is the authoritative schema/route contract.
- Design: `artifacts/designs/pending/DD-combined-walks-2b-2d-workbench.md` (the governing DD).
- Active pipeline contract: `artifacts/designs/completed/DD-epub-audiobook-pipeline-rewrite-v3.md`; universal-upgrade features (transaction(), BEGIN IMMEDIATE + 503 Retry-After:5, review-band thresholds, run cancellation/heartbeat, committed dist) must be preserved.
- Walk names (v3): `walk_2b_character_discovery`, `walk_2c_alias_resolution` (GLOBAL), `walk_2d_scene_presence` (LOCAL).
- Invariants: routes stay under `/api/pipeline` in existing api_* modules; `app/tts.py` untouched; legacy guard 12/12; backend walks kept separate for dependency/rerun purposes; no walk-tab reorganization, clone/persona/prompt parity, or browser E2E work from the other two pending DDs.

## Phases

### Phase 1: Workbench schema and domain foundation (S1)
- [x] Add normative workbench tables + indexes + guarded migrations to `app/pipeline/schema.py`: workbench_generation (sole per-book revision allocator, no book FK, `ON CONFLICT(book_id) DO UPDATE SET revision=revision+1 ... RETURNING revision` inside BEGIN IMMEDIATE), append-only workbench_decision (target_kind presence|alias_merge|review|boundary; status active|undone|superseded|conflict; undone_by/supersedes_id refts), workbench_provenance, character_scene_absence tombstone (PK book_id,scene_id,character_id; active flag), non-destructive character_alias_merge (partial unique index ux_alias_active_member WHERE status='active'; stores prior voice assignment + consequences), boundary_override (chapter/scene/paragraph anchors, unconditional workbench scope), and SEPARATE character_scene_generated vs character_scene_manual projections (independent uniqueness; manual includes 'absent'; generated unique by stable target key only, never source_run_id). Include the additive/idempotent walk_review_item kind (`'alias_merge'`) migration and per-book generated-row backfill (rev 0).
    **Notes:** Implemented in app/pipeline/schema.py + new app/pipeline/workbench.py (1472 lines). All tables/constraints match CONTRACTS.md. Migration rules: additive/idempotent, dedup generated rows deterministically (lowest rowid) before uniqueness index, backfill one generation row per book rev 0, no migration deletes human rows, human_override stays as compatibility flag. walk_review_item kind rebuild guarded via PRAGMA table_info introspection; no human data deleted/rewritten.
- [x] Implement `Workbench` domain service (app/pipeline/workbench.py): allocation (GET/generation/revision, allocate_revision, check_revision for stale-write 409), stable anchors, record_decision + record_provenance (append-only), set_presence/get_presence (generated/manual/conflict resolution), get_conflicts, alias preview_alias_conversion + commit_alias_conversion + unmerge_alias, boundary override get/put/apply/deactivate, override get/put/delete, and resolve_effective_config with the split precedence chain. Typed error hierarchy (BookNotFoundError→404, StaleRevisionError/ConflictError→409, ValidationError/PreviewExpiredError→422, ConcurrentTransactionError→503+Retry-After:5).
    **Notes:** Workbench class methods: require_book, get_generation, get_revision, allocate_revision, check_revision, get_stable_anchors, record_decision, record_provenance, list_decisions, _require_scene_character, get_generated_rows, get_manual_rows, _active_absences, set_presence, get_presence, get_conflicts, _member_scene_keys, _affected_rows, _protected_decisions, preview_alias_conversion, commit_alias_conversion, unmerge_alias, _validate_boundary_anchor, _validate_boundary_payload, get_boundary_overrides, put_boundary_override, apply_boundary_override, deactivate_boundary_override, get_overrides, _validate_override_value, put_override, delete_override, _source_for, resolve_effective_config. 16 focused tests in test_workbench_schema.py + test_workbench_domain.py cover every constraint.
- [x] Verify: `pytest -q tests/pipeline/test_workbench_schema.py tests/pipeline/test_workbench_domain.py` green; schema/domain coverage strong.
    **Notes:** 77 total new workbench backend tests pass (schema 16, domain, API, review, walks subsets).

### Phase 2: Pipeline-native API surfaces and review dispatch (S2)
- [x] Add routes in existing api_* modules (all under /api/pipeline): api_walks.py — GET /workbench/{book_id} read model, GET /workbench/{book_id}/config, PUT/DELETE /workbench/{book_id}/overrides, POST /workbench/{book_id}/alias-conversions/preview + /commit (short-lived book-scoped single-use 10-min token; preview==commit affected-row set), POST /workbench/{book_id}/reruns (scope book|scenes, 2c rejects scenes 422), GET/PUT /workbench/{book_id}/boundary-overrides, POST .../apply, DELETE .../{override_id}; api_review.py — POST /workbench/{book_id}/decisions/{decision_id}/undo (409 on newer state); api_characters.py — PUT /workbench/{book_id}/presence.
    **Notes:** All routes pipeline-prefixed and registered. Error mapping verified: 404 unknown/cross-book, 409 stale revision + conflict, 422 validation + preview expiry + 2c-scenes-scope, 503 Retry-After:5 on transaction contention. ThContained in api_walks.py (26 HTTP decorators incl. pre-existing), api_review.py, api_characters.py. No new module introduced — routes distributed across existing api_* modules per constraint.
- [x] Extend review action dispatch to all required ID forms while preserving existing accept/reject/override as resolution authority: decision:{uuid} (active workbench decision), junction:{table}:{character_id}:{entity_id} (allow-listed live junction; accept→confidence 1, reject→human absence/removal decision, override→typed value), walkitem:{id} (book-scoped walk_review_item; accept→resolved, reject→restore prior_value, override→validate/write+resolved). Each action one transaction, creates a decision record, transitions target, returns ActionResultDTO.
    **Notes:** decision: (13 refs), junction: (9), walkitem: (3), dispatch (6) in api_review.py. Test test_legacy_bare_junction_and_walkitem_unchanged confirms backward compat. walk_review_item kind now includes 'alias_merge'.
- [x] Verify: `pytest -q tests/pipeline/test_api_workbench.py tests/pipeline/test_api_review_workbench.py` green; all status codes and DTO shapes match CONTRACTS.md.
    **Notes:** test_junction_stale_revision_409_and_unknown_404, test_concurrent_transaction_maps_to_503, test_rerun_scenes_scope_and_rejection, cross-book preview-token rejection, undo 409-on-newer-state all pass.

### Phase 3: Rerun-safe walks (S3)
- [x] Make walk_2b idempotent: generated rows upserted by stable key (never source_run_id), source_run_id stored as provenance only; downstream invalidation 2b→2c+2d; explicit absence tombstone blocks re-add.
    **Notes:** test_walk_2b rerun-safety covers no-duplicates; legacy junction + span inserts guarded by existence checks.
- [x] Make walk_2c non-destructive: member characters NO LONGER deleted on merge; merge recorded as character_alias_merge relation + generated decision storing prior voice assignments and downstream impact for unmerge; 2c stays GLOBAL (book-level alias graph; rejects scenes scope 422). Invalidation 2c→2d.
    **Notes:** test_walk_2c rewritten from test_non_canonical_character_is_deleted_after_merge to survival assertion. TestWalk2cReversible covers consequences/unmerge.
- [x] Make walk_2d presence-safe: generated/manual projections coexist; upsert by stable key (character_id, scene_id, relation_type) with in-walk dedup set; absence tombstone never re-added by 2d. Invalidation 2d→none.
    **Notes:** TestWalk2dRerunSafe covers no-duplicates + never-re-adds-absence. test_invalidation_dag_is_contractual confirms 2b→2c+2d, 2c→2d, 2d→none.
- [x] Verify: `pytest -q tests/pipeline/test_workbench_walks.py tests/pipeline/test_walk_2b.py tests/pipeline/test_walk_2c.py tests/pipeline/test_walk_2d.py` green.
    **Notes:** pass (part of 279 targeted walk/legacy/schema/review tests + full suite).

### Phase 4: Accessible combined workbench frontend (S4)
- [x] Wire the workbench tab: frontend/index.html (nav + #workbench-tab pane), frontend/src/main.ts (initWorkbench()), frontend/src/api.ts (typed client, reuse postWithRetryOnce one-retry semantics; add putWithRetryOnce/delWithRetryOnce for 503), frontend/src/state.ts (WorkbenchState types + selectors), new frontend/src/tabs/workbench.ts (815 lines).
    **Notes:** tsc --noEmit exits 0; build emits index-CES4J2FO.js (112 kB, 23 modules).
- [x] Implement workbench UI: scene navigator + span-evidence/source-text highlights, character ledger, alias ledger with preview→confirm→commit→unmerge, per-scene presence editing (present/speaker/absent), collapsible walk setup (CollapsibleWalkSetup) with per-field effective-config source badges, conflict display, run summaries/counters, explicit rerun protection (confirm dialog naming downstream invalidations, scenes-scope guard, 2c-scenes rejection, preserve_manual_decisions default true), undo stack.
    **Notes:** WCAG 2.2 AA keyboard-equivalent core journeys, non-color-only state, confirmation, undo, error/conflict display, stable-anchor rendering.
- [x] Add frontend tests: new frontend/tests/frontend/test_workbench.test.ts covering IA, keyboard/disclosure/focus, highlight synchronization, typed walk-item actions, confirmation/undo/errors, config source badges, conflicts, run polling/cancellation.
    **Notes:** 44 vitest tests pass in 1 file.
- [x] Verify: `cd frontend && npx tsc --noEmit -p tsconfig.json` (exit 0) and `npx vitest run tests/frontend/test_workbench.test.ts` green.
    **Notes:** tsc exit 0; 44 passed.

### Phase 5: Contract registration and committed build output (S5)
- [x] Register all delivered routes, DTOs, tables, behaviors, and the pre-registered config-shape deviation in `artifacts/designs/parts/epub-audiobook-pipeline-rewrite/CONTRACTS.md` (append-only; already had baseline § Workbench registration; add +51 delivery lines).
    **Notes:** Documents GET /config returning global/task_overrides/top_level_walk_override as null/empty with effective/source split per-walk — intentional deviation, registered with rationale. api_operations.py untouched (routes landed in other three api_* modules).
- [x] Build and commit the frontend dist output under app/static/dist/ (hash-renamed index-CES4J2FO.js + index.html); pass the committed-dist diff gate.
    **Notes:** npm run build succeeds; index-CoEHB2gz.js replaced by index-CES4J2FO.js; dist committed.
- [x] Run final verification gates on the committed state: `pytest -q` (full backend), legacy guard `pytest -q tests/pipeline/test_legacy_removed.py`, `cd frontend && npx tsc --noEmit -p tsconfig.json`, `cd frontend && npm run build && git diff --exit-code app/static/dist/`.
    **Notes:** full suite 1447 passed; legacy guard 12/12; tsc exit 0; build + dist diff gate clean. Independent qa-reviewer returned PASS with 0 findings. Scoped commits: b09b9ff (S1), 96f168c (S2), a6ca7fc (S3), 1e32eda (S4), 308cf7a (S5).

## Completion Criteria

- `pytest -q` from repo root passes (1447 passed at final gate; includes 77 new workbench backend tests).
- All workbench reads expose scene_id and stable anchors; every stale write returns 409; alias preview enumerates every affected row + voice consequence + preview token; preview and commit affected-row sets match; protected decisions survive rerun; 2b/2d reruns produce no duplicates and never re-add explicit absence; effective config matches and reports precedence/source.
- `cd frontend && npx tsc --noEmit -p tsconfig.json` exits 0; `npx vitest run tests/frontend/test_workbench.test.ts` passes (44 tests); `npm run build && git diff --exit-code app/static/dist/` passes.
- Legacy guard `pytest -q tests/pipeline/test_legacy_removed.py` = 12/12.
- All new routes and DTOs are pipeline-prefixed and registered in CONTRACTS.md.
- No changes to app/tts.py, no walk-tab reorganization, no clone/persona/prompt parity, no browser E2E; backend walks kept separate for dependency/rerun purposes; universal-upgrade features (BEGIN IMMEDIATE + 503 Retry-After:5, review bands, run cancellation, committed dist) preserved.

## References

- `artifacts/designs/pending/DD-combined-walks-2b-2d-workbench.md`
- `artifacts/designs/parts/epub-audiobook-pipeline-rewrite/CONTRACTS.md` (§ Workbench)
- `artifacts/designs/completed/DD-epub-audiobook-pipeline-rewrite-v3.md`
- Independent QA review: delegation `fat-apricot-swan` (qa-reviewer) — PASS, 0 findings
