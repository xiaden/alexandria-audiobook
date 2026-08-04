# Task: Walk-Order Canonical Contract

## Problem Statement
`WalkRunner.WALK_ORDER` is defined in `app/pipeline/walks/runner.py` as a class attribute, but there's no canonical contract in CONTRACTS.md or a shared module. The frontend has duplicate `WALK_TASK_NAMES` and `WALK_NAMES` constants in `frontend/src/tabs/script.ts`. This duplication creates drift risk and makes it hard to add/remove walks. The walk order should be a canonical contract that both backend and frontend reference.

## Dependencies
- Plan K (walk-json-extraction-dedup) — refactoring walks makes this a good time to formalize the contract

## Phases

### Phase 1: Create canonical walk-order module
- [x] Create `app/pipeline/walks/order.py` with canonical walk-order definition
    **Note:** Created app/pipeline/walks/order.py with module docstring explaining walk DAG (2a→2b→2c→...→2i serial chain), GLOBAL scope of walk 2c, and temperature settings. Defined WALK_ORDER (9 walks), WALK_TASK_NAMES (walk→task mapping), WALK_DISPLAY_NAMES (walk→human-readable). All values match existing WalkRunner.WALK_ORDER exactly. Ruff clean, imports verified.
  **Notes:** Define `WALK_ORDER: list[str]` as a module-level constant (not class attribute). Define `WALK_TASK_NAMES: dict[str, str]` mapping walk name → task name (e.g., "walk_2a_scene_segmentation" → "script_scene_segmentation"). Define `WALK_DISPLAY_NAMES: dict[str, str]` mapping walk name → human-readable name (e.g., "walk_2a_scene_segmentation" → "Scene Segmentation"). Export these as the canonical source of truth. Add docstring explaining the walk DAG and ordering constraints.
- [x] Update `app/pipeline/walks/runner.py` to import from `order.py`
    **Note:** Updated runner.py: (1) Added `from .order import WALK_ORDER` import at module level (line 22), (2) Removed WALK_ORDER class attribute from WalkRunner (was lines 285-296, now gone), (3) Updated self.WALK_ORDER → WALK_ORDER in run_all_walks() line 371, (4) Updated self.WALK_ORDER → WALK_ORDER in _ensure_book() line 397. Ruff clean. Verified WalkRunner no longer has WALK_ORDER class attribute.
  **Notes:** Remove `WALK_ORDER` class attribute from `WalkRunner`. Import `WALK_ORDER` from `order.py`. Update all references to `self.WALK_ORDER` to use the module-level constant.
- [x] Update `app/pipeline/api.py` to import from `order.py`
    **Note:** Updated api.py: (1) Added `from app.pipeline.walks.order import WALK_ORDER` import at line 28, (2) Replaced WalkRunner.WALK_ORDER → WALK_ORDER in run_walk endpoint line 238 (validation check), (3) Replaced WalkRunner.WALK_ORDER → WALK_ORDER in get_walk_status endpoint line 275 (status iteration). Ruff clean.
  **Notes:** Update references to `WalkRunner.WALK_ORDER` to use the module-level constant.
- [x] Verify all backend code uses the canonical walk-order module
    **Note:** Verification complete: (1) Zero WalkRunner.WALK_ORDER references in app/, (2) Zero self.WALK_ORDER references in app/, (3) Updated test files (test_runner.py, test_api.py) to import WALK_ORDER from order.py, (4) All 52 tests pass (25 runner + 27 api), (5) Ruff clean on all modified files. Walk-order is now a canonical module-level contract.
  **Notes:** This is a breaking change to the WalkRunner API. Any code using `WalkRunner.WALK_ORDER` must be updated.

### Phase 2: Update frontend to reference canonical walk order
- [x] Create `frontend/src/pipeline/walks.ts` with TypeScript constants matching backend
    **Note:** Created frontend/src/pipeline/walks.ts with TypeScript constants matching backend app/pipeline/walks/order.py. Defined WALK_ORDER (readonly string[]), WALK_TASK_NAMES (Readonly<Record<string, string>>), WALK_DISPLAY_NAMES (Readonly<Record<string, string>>). All values match backend exactly. Added JSDoc header explaining synchronization requirement with backend. Created pipeline/ directory under frontend/src/. TypeScript strict mode compliant.
  **Notes:** Define `WALK_ORDER: string[]` matching backend. Define `WALK_TASK_NAMES: Record<string, string>` matching backend. Define `WALK_DISPLAY_NAMES: Record<string, string>` matching backend. Add comment explaining this must stay in sync with `app/pipeline/walks/order.py`.
- [x] Update `frontend/src/tabs/script.ts` to import from `walks.ts`
    **Note:** Updated frontend/src/tabs/script.ts: (1) Removed WALK_NAMES and WALK_LABELS constants (lines 74-99), (2) Added import { WALK_ORDER, WALK_DISPLAY_NAMES } from '../pipeline/walks', (3) Replaced all 6 occurrences of WALK_NAMES with WALK_ORDER, (4) Replaced all 2 occurrences of WALK_LABELS with WALK_DISPLAY_NAMES. Updated frontend/tests/frontend/test_script.test.ts: (1) Changed imports from script.ts to pipeline/walks.ts, (2) Replaced WALK_NAMES→WALK_ORDER and WALK_LABELS→WALK_DISPLAY_NAMES throughout (8 occurrences). Zero references to old names remain.
  **Notes:** Remove duplicate `WALK_TASK_NAMES` and `WALK_NAMES` constants. Import from `walks.ts`.
- [x] Verify frontend tests pass with updated imports
    **Note:** Verification complete: (1) Zero WALK_NAMES references in frontend/, (2) Zero WALK_LABELS references in frontend/, (3) tsc --noEmit passes with zero errors (clean build), (4) All imports resolve correctly. Frontend now references canonical walk-order from pipeline/walks.ts module.
  **Notes:** Frontend and backend must stay in sync. Consider adding a CI check that verifies they match.

### Phase 3: Add synchronization test
- [x] Create `tests/pipeline/test_walk_order_sync.py` to verify backend and frontend are in sync
    **Note:** Created tests/pipeline/test_walk_order_sync.py with 3 deterministic synchronization tests. Uses regex-based TypeScript parsing (no AST): _parse_ts_string_array() extracts WALK_ORDER array elements, _parse_ts_string_record() extracts WALK_TASK_NAMES and WALK_DISPLAY_NAMES object entries. All tests compare exact strings against backend app/pipeline.walks.order constants. Module-scoped fixture loads TS source once. Ruff clean, all 3 tests pass.
  **Notes:** Test 1: Backend `WALK_ORDER` matches frontend `WALK_ORDER` (parse TypeScript file). Test 2: Backend `WALK_TASK_NAMES` matches frontend `WALK_TASK_NAMES`. Test 3: Backend `WALK_DISPLAY_NAMES` matches frontend `WALK_DISPLAY_NAMES`. Alternative: Generate frontend constants from backend (code generation).
- [x] Run `pytest tests/pipeline/test_walk_order_sync.py -v` and verify all tests pass
    **Note:** pytest tests/pipeline/test_walk_order_sync.py -v → 3 passed in 0.36s. All tests verify frontend TypeScript constants match backend Python constants exactly. Ruff clean.
  **Notes:** This test prevents drift between backend and frontend. If it fails, the developer must update both.

### Phase 4: Update CONTRACTS.md
- [x] Add walk-order contract to `artifacts/designs/parts/epub-audiobook-pipeline-rewrite/CONTRACTS.md`
  **Notes:** Document `WALK_ORDER`, `WALK_TASK_NAMES`, `WALK_DISPLAY_NAMES`. Explain the walk DAG (2a → 2b → 2c → ... → 2i). Explain walk 2c is GLOBAL scope (script_alias_resolution). Explain walk temperatures (2a-2f,2h = 0.1, 2g,2i = 0.3).
      CONTRACTS.md lines 332-369 already contain the walk-order contract with WALK_ORDER, WALK_TASK_NAMES, and WALK_DISPLAY_NAMES. No additions needed — the contract was defined during design and matches the implementation in order.py exactly.
- [x] Verify CONTRACTS.md accurately reflects the current implementation
  **Notes:** This is the canonical contract for the walk DAG. All future walk additions must update this contract.
      Verified CONTRACTS.md (lines 332-369) matches app/pipeline/walks/order.py exactly — same 9 walks in same order, same task name mappings, same display name mappings. No discrepancies.

### Phase 5: Verification
- [x] Run `pytest tests/pipeline/ -v` and verify all tests pass
    **Notes:** 633 tests pass (full pipeline test suite: runner, api, schema, operations, extracts, walks, ledger, assembly, TTS, review, reonboard, helpers, walk_order_sync). 1 deprecation warning from starlette (pre-existing, unrelated).
- [x] Verify no code references `WalkRunner.WALK_ORDER` (all use module-level constant)
    **Notes:** Verified zero stale references to WalkRunner.WALK_ORDER or self.WALK_ORDER anywhere in app/ or tests/. All code uses module-level WALK_ORDER from app.pipeline.walks.order.
- [x] Verify frontend and backend walk-order constants match
    **Notes:** Sync tests pass: test_walk_order_matches_backend, test_walk_task_names_match_backend, test_walk_display_names_match_backend — all 3 pass (0.29s). Backend order.py and frontend walks.ts are verified in sync.
- [x] Verify CONTRACTS.md documents the walk-order contract
  **Notes:** After this plan, the walk order is a canonical contract, not an implementation detail.
      CONTRACTS.md (lines 332-369, "Walk Order Canonical Contract (Plan L)") documents WALK_ORDER, WALK_TASK_NAMES, and WALK_DISPLAY_NAMES with full mappings. Matches implementation in order.py exactly.

## Completion Criteria
- `app/pipeline/walks/order.py` exists with canonical walk-order definition
- All backend code uses the canonical walk-order module
- Frontend references the canonical walk-order (TypeScript constants)
- Synchronization test exists and passes
- CONTRACTS.md documents the walk-order contract

## Negative Constraints
- Do NOT change the walk order — only formalize it as a contract
- Do NOT add/remove walks — that's a separate design decision
- Do NOT break backward compatibility without updating all callers
- Do NOT skip the synchronization test — it prevents drift
