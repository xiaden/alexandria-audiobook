# Task: Overlay Config & Walk Overrides

## Problem Statement

`POST /api/config` (app.py:475-482) is destructive: it validates the request through `AppConfig` (which is llm+tts only) and `json.dump(config.model_dump(), CONFIG_PATH)` — pydantic `model_dump()` strips any generation/prompts keys the user had, so saving config **wipes** unknown sections (the L4 data-loss bug, locked in by test_setup.test.ts L296 "ignoring legacy prompts/generation keys"). `resolve_task_llm(task_name, config_path=None)` (app/utils.py:102-139) has a dead `config_path` param that is always None from all 9 walks — per-request config dicts are never consulted for LLM resolution. There is no way to override prompt/generation settings per walk from the UI.

This plan (DD A4-r Overlay Config + Snapshot Projects; here config half, FP5) makes config overlay-based: `POST /api/config` parses raw JSON, validates known keys through `AppConfig(extra='ignore')` (validation only, output never serialized), recursively deep-merges unknown paths, stamps `schema_version`, and atomically writes — byte-stable round-trip guaranteed (CI test fixture MUST include unknown keys). A single `resolve_task_config(task, storage, book_id)` helper replaces the dead-param function: it applies on-disk config → `llm.task_overrides` → `walk_override` rows, snapshotted per walk-unit start, and is called from all 9 walks. The Setup tab gains per-walk override fields (temperature/prompts) and saving preserves unknown keys. Test `test_setup.test.ts` L296 behavior lock is updated to the new byte-stable contract.

## Dependencies

- Plan A (walk_override table) and Plan B (walk_run lifecycle, runner integration) — completed and archived
- `artifacts/designs/parts/epub-audiobook-pipeline-rewrite/CONTRACTS.md` § Universal Upgrade — config contract

## Phases

### Phase 1: Raw-JSON merge for POST /api/config (byte-stable)
- [ ] TDD RED: write tests in app/test_api.py (or tests/pipeline) asserting POST /api/config with unknown top-level keys (e.g. generation, prompts) round-trips byte-stable: GET /api/config returns the same unknown keys with the same values after a save; known keys are validated through AppConfig; schema_version is stamped; atomic write (no partial file on failure)
- [ ] Implement: rewrite POST /api/config in app.py — parse raw JSON, validate known keys via AppConfig(extra='ignore') (never serialize the validation output), recursive deep-merge unknown paths onto the current file, stamp schema_version, atomic write (tmp+rename per existing atomic_json_write in app/utils.py); keep reset_tts_engine() call
- [ ] TDD GREEN: run the config tests; assert unknown keys preserved byte-stable and schema_version present
- [ ] Verify: `ruff check app/app.py app/utils.py` clean; guard suite 12/12 still green (no legacy prompt-file modules resurrected — unknown keys live in config.json only)

### Phase 2: resolve_task_config single helper
- [ ] TDD RED: write tests for resolve_task_config(task, storage, book_id) asserting precedence on-disk config → llm.task_overrides → walk_override rows (walk_override wins), and that the result is snapshotted per walk-unit start (a mid-walk config change does not alter in-flight units)
- [ ] Implement: replace resolve_task_llm's dead config_path param usage with resolve_task_config in app/utils.py; apply the 3-tier precedence; return the effective LLM config dict for the task
- [ ] TDD GREEN: run resolve_task_config tests with a file-backed storage fixture containing walk_override rows; assert precedence and snapshot behavior
- [ ] Verify: `ruff check app/utils.py` clean; existing tests/pipeline/test_resolve_task_llm.py (app/test_resolve_task_llm.py) updated to the new helper or kept passing via compatibility

### Phase 3: Wire all 9 walks to resolve_task_config
- [ ] TDD RED: update walk tests (2a-2i) asserting each walk's LLM resolution now consults walk_override rows for its book; a walk_override row overrides the task temperature/model
- [ ] Implement: change the 9 call sites (walk_2a:70, 2b:72, 2c:81, 2d:76, 2e:79, 2f:76, 2g:77, 2h:80, 2i:81) from `resolve_task_llm(task_name, config_path=None)` to `resolve_task_config(task_name, storage, book_id)`; keep the per-unit snapshot semantics (resolve once per unit start)
- [ ] TDD GREEN: run all walk tests; assert override-driven resolution and no behavioral regression in the 9 walks
- [ ] Verify: `ruff check app/pipeline/walks/*.py app/utils.py` clean; `pytest tests/pipeline -q` green

### Phase 4: walk_override CRUD
- [ ] TDD RED: write tests for walk_override row access (read/write/delete) via the adapter (PK book_id+walk_name+key, value_json column); assert upsert semantics and JSON round-trip
- [ ] Implement: add walk_override access methods to SQLiteAdapter (or a small helper in utils.py using execute_*) — read all for a book, upsert one, delete one
- [ ] TDD GREEN: run the override CRUD tests; assert upsert + JSON round-trip + delete
- [ ] Verify: `ruff check app/pipeline/adapter.py app/utils.py` clean

### Phase 5: Setup tab per-walk override fields
- [ ] TDD RED: update frontend/tests/frontend/test_setup.test.ts — REPLACE the L296 lock that asserts "ignoring legacy prompts/generation keys" with a test asserting the Setup tab preserves unknown keys on save and renders per-walk override fields (temperature + prompt overrides) from the config
- [ ] Implement: extend setup.ts (loadConfig + handleConfigSubmit) to render per-walk override inputs (temperature/prompts) that map to walk_override sections in the config payload; saving sends the raw merged config (unknown keys preserved); update the test setup.ts uses to reflect byte-stable behavior
- [ ] TDD GREEN: run test_setup tests; assert override fields render, save payload preserves unknown keys, and the old wipe lock is gone
- [ ] Verify: `npx tsc --noEmit` exit 0; vitest green (167 baseline, test_setup updated); `npm run build` + dist commit

### Phase 6: Regression gate, guard suite and verification
- [ ] Run `pytest tests/pipeline -q --cov=app/pipeline --cov-report=term-missing` and record pass count + coverage; all green
- [ ] Run `pytest tests/pipeline/test_legacy_removed.py -q` and verify 12/12 still green (no default/review/persona prompt modules resurrected)
- [ ] Run `npm test` + `npx tsc --noEmit` + `npm run build` with `git diff --exit-code app/static/dist/` clean after commit
- [ ] Security review: config file path resolution (CONFIG_PATH env respected, no traversal), api_key handling (secrets stored in config.json — no logging of full config, redact api_key in any error output), byte-stable test proves no key loss; document findings
- [ ] Code review pass via exec-manager QA-Reviewer; fix MINOR findings
- [ ] Commit: `feat(config): overlay config merge with walk overrides and byte-stable round-trip`

## Completion Criteria

- POST /api/config raw-JSON merges (unknown keys preserved byte-stable), validates known keys via AppConfig(extra='ignore'), stamps schema_version, atomic write
- resolve_task_config(task, storage, book_id) single helper with on-disk → task_overrides → walk_override precedence, snapshotted per walk-unit start; all 9 walks call it
- walk_override CRUD implemented and tested
- Setup tab renders per-walk override fields and preserves unknown keys on save; test_setup.test.ts L296 lock updated
- Full pytest + vitest green, guard 12/12, ruff + tsc clean, build + dist committed, security review documented

## References

- `artifacts/designs/pending/DD-universal-upgrade.md` — P5 A4-r phase, FP5, cap6, workflow (Config), decision #6, cannot-restore #4/#13
- `artifacts/designs/parts/universal-upgrade/CONTRACTS.md` — resolve_task_config, raw-JSON merge, walk_override table
- `artifacts/designs/parts/universal-upgrade/README.md` — Plan G row
- Prior: `TASK-universal-upgrade-B-render-walk-persistence.md` (completed)
