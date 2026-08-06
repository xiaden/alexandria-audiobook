# Task: Unified Review Queue & Supersede

## Problem Statement

Today the review queue is junction-only: `ReviewManager.get_review_items` (app/pipeline/review.py:112-149) queries character_book/character_scene/character_span junctions with confidence in [0.5, 0.7) and returns `junction_table:character_id:entity_id` ids. The walks 2g (voice audition), 2h (voice assignment), and 2i (delivery) compute `profiles_for_review` / `assignments_for_review` / `instructs_for_review` counters and return them from `execute()` — but the return dict is dropped by the background task, so low-confidence voice-profile, voice-assignment, and instruction items never reach the queue. There is also no supersede mechanism (re-running a walk leaves stale low-confidence items from earlier runs in the queue) and no undo/value-restore.

This plan (DD A3-r Unified Review Queue Honest Union + Completion-Time Supersede) makes the queue honest: walks 2g/2h/2i write `walk_review_item` rows in the same transaction as their junction writes (kind voice_profile/voice_assignment/instruction, prior_value captured pre-write); at completion the walk's FINAL transaction supersedes prior pending items of the same kind for targets it regenerated; `GET /review/{book_id}` returns the union (junction live query + pending walk_review_item rows) with `walkitem:`-prefixed ids; accept/reject/override dispatch on the prefix and walk-side actions transactionally restore `prior_value` (undo). This is the backend half — frontend review surfacing lands in Plan F.

## Dependencies

- Plan A (transaction(), walk_review_item table) and Plan B (walk_run rows, walk-side retry, runner lifecycle) — completed and archived
- `artifacts/designs/parts/epub-audiobook-pipeline-rewrite/CONTRACTS.md` § Universal Upgrade — review endpoint registration

## Phases

### Phase 1: walk_review_item writes in walks 2g/2h/2i
- [ ] TDD RED: write tests for walk_2g (voice audition), walk_2h (voice assignment), walk_2i (delivery) asserting each writes a walk_review_item row in the SAME transaction as its junction writes (kind voice_profile / voice_assignment / instruction respectively), with prior_value captured from the pre-write state of the target row, status pending, created_ms set
- [ ] Implement: extend the per-unit savepoint blocks in walk_2g_voice_audition.py, walk_2h_voice_assignment.py, walk_2i_delivery.py to insert walk_review_item rows when the walk marks an item for review; keep LLM calls outside the transaction (per architectural rule #6)
- [ ] TDD GREEN: run walk 2g/2h/2i tests; assert item rows exist with correct kind/target/prior_value and that a rollback (simulated unit failure) removes the item row (same transaction)
- [ ] Verify: `ruff check app/pipeline/walks/walk_2g_voice_audition.py app/pipeline/walks/walk_2h_voice_assignment.py app/pipeline/walks/walk_2i_delivery.py` clean

### Phase 2: Completion-time per-target supersede
- [ ] TDD RED: write tests asserting that when a walk run completes, its FINAL transaction supersedes prior pending walk_review_item rows of the same kind whose target_id is in the new run's committed targets (status → superseded); items for targets NOT regenerated stay pending; nothing is superseded on failure/cancel
- [ ] Implement: in the walk's FINAL transaction (after the last unit), run the supersede update: `UPDATE walk_review_item SET status='superseded' WHERE book_id=? AND run_id<>? AND status='pending' AND kind=? AND target_id IN (new run's committed targets)`; add a shared helper in review.py (e.g. `supersede_targets(...)`) called by 2g/2h/2i
- [ ] TDD GREEN: run the supersede tests; assert superseded rows, retained pending rows, and no supersede on simulated failure/cancel
- [ ] Verify: `ruff check app/pipeline/review.py app/pipeline/walks/walk_2g_voice_audition.py` (and 2h/2i) clean

### Phase 3: Union review queue (junction live query + walk items)
- [ ] TDD RED: write tests asserting ReviewManager.get_review_items returns the honest union: existing junction items (unchanged behavior) PLUS pending walk_review_item rows with ids `walkitem:{id}`; walk items carry kind, target_table, target_id, prior_value, created_ms; filter by walk_name when provided
- [ ] Implement: extend ReviewManager.get_review_items to query walk_review_item (status=pending) for the book and merge with the junction query; item ids get the `walkitem:` prefix; keep junction ids unchanged (backward compatible with existing frontend)
- [ ] TDD GREEN: run review queue tests with a fixture that has both junction items and walk items; assert union order (e.g. junction first, then walk items), ids, and the walk_name filter
- [ ] Verify: `ruff check app/pipeline/review.py` clean

### Phase 4: Prefix dispatch on accept/reject/override
- [ ] TDD RED: write tests asserting POST /review/accept|reject|override dispatch by id prefix: `junction:` → existing junction behavior; `walkitem:` → walk-side action; unknown/malformed prefix → 400; unknown item id → 404
- [ ] Implement: extend api_review.py action handlers to branch on the id prefix; junction branch keeps current logic; walkitem branch calls a review.py helper that updates the walk_review_item row (status resolved for accept/reject; resolved + new_value stored for override)
- [ ] TDD GREEN: run the dispatch tests; assert correct status transitions per action and correct error codes
- [ ] Verify: `ruff check app/pipeline/api_review.py app/pipeline/review.py` clean; guard suite 12/12 still green

### Phase 5: Transactional value-restore (undo backend)
- [ ] TDD RED: write tests asserting that rejecting/overriding a walkitem: item transactionally restores `prior_value` into `target_table.target_id` (e.g. restoring a pre-walk voice_config value), with the restore committed atomically with the walk_review_item status update; restore failure rolls back both
- [ ] Implement: in review.py add the value-restore helper used by the walkitem branch: within `storage.transaction()`, write prior_value back to target_table/target_id and update the item row to resolved
- [ ] TDD GREEN: run the value-restore tests; assert the target row has the prior value restored and the item is resolved; simulate a restore failure and assert rollback of both the restore and the status change
- [ ] Verify: `ruff check app/pipeline/review.py` clean; `pytest tests/pipeline/test_review.py -q --cov=app/pipeline --cov-report=term-missing` green (review coverage stays 100%)

### Phase 6: Regression gate, guard suite and verification
- [ ] Run `pytest tests/pipeline -q --cov=app/pipeline --cov-report=term-missing` and record pass count + coverage (review.py baseline 100% maintained; walk 2g/2h/2i coverage rises from 82% baseline)
- [ ] Run `pytest tests/pipeline/test_legacy_removed.py -q` and verify 12/12 still green (no legacy review endpoints resurrected)
- [ ] Security review: walkitem id dispatch must scope to the book_id (no cross-book item access — verify item lookup joins book_id), junction vs walkitem prefix cannot be spoofed to reach junction tables via walkitem branch, prior_value restore cannot write outside target_table/target_id (validated table allowlist); document findings
- [ ] Code review pass via exec-manager QA-Reviewer; fix MINOR findings
- [ ] Commit: `feat(pipeline): unified review queue with walk items, supersede and value-restore`

## Completion Criteria

- Walks 2g/2h/2i write walk_review_item rows in-transaction (3 kinds, prior_value captured); rollback removes them
- Completion-time per-target supersede in the FINAL transaction; nothing superseded on failure/cancel
- GET /review/{book_id} returns the honest union (junction + walkitem: prefixed items), filterable by walk_name
- accept/reject/override dispatch by prefix; junction branch unchanged; walkitem branch transactional with value-restore (undo) committed atomically
- Full pytest suite green, review coverage 100%, walk 2g/2h/2i coverage up, guard 12/12, ruff clean, security review documented

## References

- `artifacts/designs/pending/DD-universal-upgrade.md` — P3 A3-r phase, FP3, workflows (Walk, Supersede, Review), decisions #5/#8
- `artifacts/designs/parts/universal-upgrade/CONTRACTS.md` — walk_review_item schema, supersede rule, union queue, prefix dispatch, value-restore
- `artifacts/designs/parts/universal-upgrade/README.md` — Plan D row
- Prior: `TASK-universal-upgrade-B-render-walk-persistence.md` (completed)
