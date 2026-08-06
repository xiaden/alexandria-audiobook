# Task: Snapshot Projects

## Problem Statement

The pre-rewrite app had saved-script projects (with a legacy format and localStorage script persistence); the rewrite deleted them and today there is NO persistence of edited script state — `alexandria-pipeline-book-id` (state.ts L176) is the only pipeline persistence, and `a12n-pipeline` edits live only in memory/DB. This plan (DD A4-r Snapshot Projects half, cap7, FP3+FP4+A4-r) restores **snapshot projects** as a pipeline-native feature: a `project_snapshot` table (created in Plan A) stores auto-named snapshots of the script/spans state per book; the API (api_operations.py) gains POST /projects (auto-named, returns name), GET /projects, POST /projects/load (merge-vs-replace: characters never deleted), DELETE /projects/{name}, and PATCH /projects/{name} (rename). Restore is blocked while any active walk_run/render_job row exists (409 + Retry-After); audio missing after restore → explicit 're-render' notice. The frontend gains a Projects tab (Save auto-named / Load / Delete / Rename) wired to these endpoints. The DD user defaults are adopted: **generated snapshot names** for saved projects v1 (no free-form name input).

Contract note: the upstream CONTRACTS.md § Universal Upgrade registration lists 10 new endpoints but NOT the rename PATCH — this plan registers PATCH /projects/{name} in the universal-upgrade CONTRACTS.md and appends it to the upstream registration (append-only rule; the upstream section is an uncommitted working-tree diff).

## Dependencies

- Plan B (active-run blocking check via walk_run/render_job rows; adapter snapshot access builds on Plan A's project_snapshot table)
- Plan E (tab navigation — Projects tab must be reachable)
- `artifacts/designs/parts/universal-upgrade/CONTRACTS.md` — project_snapshot + 5 projects endpoints

## Phases

### Phase 1: Snapshot CRUD backend (api_operations.py)
- [ ] TDD RED: write tests in tests/pipeline/test_operations.py (or new test_snapshots.py) asserting POST /projects creates an auto-named snapshot (e.g. `Project {YYYY-MM-DD HH:MM}`) with the current spans/script state; GET /projects lists them newest-first; DELETE removes; PATCH renames (409 on duplicate name)
- [ ] Implement: add snapshot endpoints to api_operations.py (already owns project-related operations) — POST /projects (auto-generate name from book_id + timestamp; store snapshot_json = current span/script manifest), GET /projects, POST /projects/load, DELETE /projects/{name}, PATCH /projects/{name}; all behind the existing /api/pipeline router (api.py aggregator already includes operations)
- [ ] TDD GREEN: run the snapshot tests; assert auto-naming, list order, delete, rename
- [ ] Verify: `ruff check app/pipeline/api_operations.py` clean; guard suite 12/12 still green (no /api/scripts/* endpoints resurrected — new paths /api/pipeline/projects/* are guard-legal)

### Phase 2: Restore semantics (merge-vs-replace, active-run block, re-render notice)
- [ ] TDD RED: write tests asserting POST /projects/load (a) returns 409+Retry-After while any active walk_run or render_job row exists, (b) merges snapshot state into the current book without deleting characters, (c) flags missing audio → explicit 're-render' notice in the response
- [ ] Implement: load handler — check active walk_run/render_job rows (query via adapter, same statuses as reconciliation: pending/running), 409 on active; apply snapshot_json merge (spans text/instructions updated; characters kept, never deleted); include re_render_required flag when referenced audio artifacts are absent (RENDER_ROOT check)
- [ ] TDD GREEN: run the load tests; assert 409, merge-not-replace, and re-render flag
- [ ] Verify: `ruff check app/pipeline/api_operations.py` clean; pytest green

### Phase 3: Projects tab UI
- [ ] TDD RED: write frontend tests (new tests/frontend/test_projects.test.ts) asserting the Projects tab lists snapshots, Save auto-names via POST /projects, Load calls POST /projects/load and surfaces the re-render notice, Delete confirms then DELETEs, Rename PATCHes
- [ ] Implement: add projects.ts (new tab module) + index.html nav link + tab pane + api.ts typed helpers (get/post/del; add del helper to api.ts — it has none today, only get/post/put); wire Save/Load/Delete/Rename buttons; handle 409 Retry-After with a toast and one retry
- [ ] TDD GREEN: run test_projects tests; assert each action hits the right endpoint with the right payload
- [ ] Verify: `npx tsc --noEmit` exit 0; `npm test` green; `npm run build` + dist committed

### Phase 4: Register rename PATCH in contracts
- [ ] Append PATCH /projects/{name} (rename, 409 on duplicate, 404 unknown) to the Universal Upgrade registration in artifacts/designs/parts/epub-audiobook-pipeline-rewrite/CONTRACTS.md (append-only, date-stamped)
- [ ] Confirm the universal-upgrade CONTRACTS.md delta ledger already lists all 5 projects endpoints consistently
- [ ] Verify: re-read both ledger sections; confirm no duplicate entries and full signatures

### Phase 5: Regression gate, guard suite and verification
- [ ] Run `pytest tests/pipeline -q --cov=app/pipeline --cov-report=term-missing` and record pass count + coverage (api_operations coverage maintained)
- [ ] Run `pytest tests/pipeline/test_legacy_removed.py -q` and verify 12/12 green (no legacy saved-script format compatibility, no /api/scripts/*)
- [ ] Run `npm test` + `npx tsc --noEmit` + `npm run build` with `git diff --exit-code app/static/dist/` clean after commit
- [ ] Security review: snapshot_json is data (spans/instructions) — confirm size limits, no HTML/script injection rendered from snapshot content (escape when rendering), name validation on PATCH (no path traversal via snapshot name), merge-vs-replace cannot delete characters; document findings
- [ ] Code review pass via exec-manager QA-Reviewer; fix MINOR findings
- [ ] Commit: `feat(pipeline): snapshot projects with save, load, delete, rename`

## Completion Criteria

- POST/GET/load/DELETE/PATCH /projects endpoints live in api_operations.py, registered in both ledgers (rename PATCH appended upstream)
- Auto-named snapshots (no free-form name input); load is merge-vs-replace (characters never deleted); 409 while active runs; re-render notice when audio missing
- Projects tab in the frontend (Save/Load/Delete/Rename) with 409 retry-once; api.ts gains del helper
- Full pytest + vitest green, guard 12/12, ruff + tsc clean, build + dist committed, security review documented

## References

- `artifacts/designs/pending/DD-universal-upgrade.md` — cap7 (saved scripts), P6-C7, A4-r, FP3+FP4, cannot-restore #3/#10, open item #5
- `artifacts/designs/parts/universal-upgrade/CONTRACTS.md` — project_snapshot table + 5 endpoints
- Prior: Plan B (walk_run/render_job rows), Plan E (tab navigation)
