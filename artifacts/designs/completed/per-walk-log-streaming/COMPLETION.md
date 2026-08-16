# Per-Walk Log Streaming — Completion Manifest

**Completed:** 2026-08-16
**Design doc:** `artifacts/designs/pending/DD-per-walk-log-streaming.md`
**Parts README:** `artifacts/designs/parts/per-walk-log-streaming/README.md`
**Contracts ledger:** `artifacts/designs/parts/per-walk-log-streaming/CONTRACTS.md`

---

## Execution Summary

| Plan | Title | Rounds | Fix Cycles | Status |
| --- | --- | --- | --- | --- |
| A | Walk-Log Service Core | 7 (post-amendment) | 2 pre-amendment + 2 FIX_INLINE | PASS |
| B | Runner Integration | 2 | 2 (flaky-concurrency + protected-modules guard) | PASS |
| C | SSE Endpoint | 2 | 1 (3 reopened P4-S3 pre-QA) | PASS |
| D | Frontend Viewer | 3 | 2 (hostile-id selector bug + doc phrasing) | PASS |

---

## Design Deviations

The following deviation from the original design doc was **Director-approved mid-execution (2026-08-16)** after a web-research review of industry-standard SSE gap handling:

- **Synthetic broker `overflow` event removed.** The original design emitted a synthetic `overflow` event when the in-memory broker evicted oldest non-terminal records. During Part A implementation, QA reproduced a seq-collision defect: the marker's seq collided with a real record's `{run_id}:{seq}` id, and two fix attempts each shifted the collision one record later. Resolution (not a third seq fix): the event was removed entirely per industry-standard SSE gap handling (WHATWG #8297, django-eventstream, server-sent-events.com). Broker eviction now drops records **silently**; live stream seq gaps are normal and expected; the `id` is an opaque cursor, not an event count.
- **File-authoritative replay for active runs.** The original design replayed the broker's bounded snapshot for active runs and the file only for completed runs. Now `open_subscription` replays the authoritative JSONL file for **both active and completed runs**; the broker bridges only records after the file tail. This closes a data-loss hole for mid-run reconnects.
- **File-cap overflow marker retained as a real record.** The 10 MiB file-cap marker remains, but is defined as a real file record with a real sink seq (not a broker-side synthetic event).
- Pre-execution terminal children (batch aborted before a child starts) are **DB-only**: they receive a `cancelled` row with zero terminal log records, per runner.py's cancel-check-before-open_run ordering (DD line 66 amended).

## Key Decisions

- **`WalkRunner(storage, log_service=None)`** — `None` means no sink operations. Part C wires the process-owned `app.state.walk_log_service` into the runner; Part B made zero API/app.py changes.
- **`run_walk_reserved` verifies `(run_id, book_id)`, not `walk_name`** — run_id is the canonical PK and book_id is the cross-book guard; walk_name is always passed matching by Part C. QA validated as correct contract.
- **Cancelled-before-start emits zero terminal records** — DB-only `cancelled` status is the documented contract for unstarted runs.
- **Coverage gate for Part C measured on the full pipeline suite** (95.89–97%) — the focused `api_walks.py` 80% target was unsatisfiable since the module holds ~210 statements across unrelated route families.
- **Frontend caps locked in Phase 1:** `WALK_LOG_ENTRY_CAP=200`, `WALK_LOG_RECORD_TEXT_CAP=500`, `WALK_LOG_STATUS_TEXT_CAP=200`; `textContent`-only rendering (structural safety, escapeHtml is mock-identity in tests).
- **Hostile run_id selector bug fixed in Part D P4:** run_id interpolated into a CSS id selector threw DOMException; replaced with constant `data-walk-log-status` attribute lookups (interpolation-free `findWalkRunRow`).
- **17 pre-existing backend test failures are environmental baseline** (16× pydub/audioop under Py3.13 + 1× legacy-removal), verified identical on a pristine tree — zero feature failures.

## Files Created/Modified

### Services (backend)
- `app/pipeline/walks/log_service.py` (created — sink + broker + file-authoritative replay + subscriptions)
- `app/pipeline/walks/runner.py` (modified — reservation helpers, reserved runner lifecycle, sink terminal records)
- `app/pipeline/walks/_llm_helpers.py` (modified — `WALK_LOG_SINK` ContextVar + llm/parse record emission)
- `app/app.py` (modified — walk-log-service lifespan wiring: +22 lines)

### API (backend)
- `app/pipeline/api_walks.py` (modified — SSE route, `get_walk_log_service()` DI, reservation rewiring)

### Tests (backend)
- `tests/pipeline/test_walk_log_service.py` (created — 86 tests)
- `tests/pipeline/test_walk_log_service_lifespan.py` (created — 5 FastAPI-lifespan tests)
- `tests/pipeline/test_sse_endpoint.py` (created — 54 tests × 2 mountings)
- `tests/pipeline/test_sse_concurrency.py` (created — 7 tests)
- `tests/pipeline/test_sse_mounted_app.py` (created — 3 tests)
- `tests/pipeline/test_api.py` (modified — reservation contract tests)
- `tests/pipeline/test_runner.py` (modified — +~800 lines)
- `tests/pipeline/test_walk_helpers.py` (modified — +~280 lines)

### Frontend
- `frontend/src/tabs/script.ts` (modified — keyed viewer, EventSource lifecycle, reconciliation)
- `frontend/tests/frontend/test_script.test.ts` (modified — 35 new viewer tests)
- `app/static/dist/` (rebuilt — bundle `index-D_DcHfUz.js`, old bundle removed)

## Final Lint Status

- Backend: PASS (ruff clean on changed files; app.py 57 pre-existing errors, zero on added +22 lines)
- Frontend: PASS (npx tsc --noEmit exit 0; npm run build OK)
- Tests: backend focused 185/185 + full pipelne suite green except 17 pre-existing env baseline; frontend full suite 534/534 (13 files)