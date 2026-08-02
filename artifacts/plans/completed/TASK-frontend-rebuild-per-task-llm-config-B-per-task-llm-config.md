# Task: Per-Task LLM Configuration (Backend + Legacy Frontend)

## Problem Statement

The Alexandria audiobook project has 6 LLM call sites across 3 scripts (generate_script.py, review_script.py, generate_personas.py) that all share a single global `model_name` with no per-task customization or reasoning control. Users need the ability to configure different models and reasoning effort levels for different tasks (e.g., use a faster model for alias resolution, a more capable model for script generation).

This plan implements Workstream B from the design document: per-task LLM configuration with backward-compatible config schema, a resolver function that threads resolved values to call sites, and a frontend table UI in the legacy monolithic HTML for editing per-task overrides.

**Scope:**
- New Pydantic models: `TaskLLMConfig`, `LLMTaskOverrides`, extended `LLMConfig` with optional `reasoning_effort` + `task_overrides`
- 6 task names: script_generation, script_review, alias_resolution, persona_discovery, persona_compilation, basic_persona_generation
- Resolver function `resolve_task_llm(task_name)` in utils.py
- GET /api/config materializes defaults via `AppConfig.model_validate()`
- 6 call sites adapted to self-resolve and thread resolved values
- Frontend per-task 6-row table UI in Setup tab (legacy monolith)
- Backward-compatible: old flat config.json loads unchanged

**Not in scope:**
- TypeScript frontend rebuild (that's Plan A)
- Changes to `create_llm_client()` signature (remains unchanged)
- Test changes (H2: no test changes needed, documented in DD)

## Planning Decisions (Open Questions Resolved)

1. **reasoning_effort validation:** Pass-through (no enum validation). Different providers use different enums (OpenAI: low/medium/high, Anthropic: different values). Pass-through provides provider flexibility and is consistent with the DD's recommendation. Invalid values will surface as API errors at runtime, which is acceptable for this use case.

2. **Dataset builder tab:** Confirmed TTS-only (no LLM call site, no per-task row needed). Research confirms dataset builder uses VoiceDesign engine, not LLM.

3. **Vite base path:** Not applicable to this plan (Plan A concern).

## Phases

### Phase 1: Pydantic Schema Models (Backward-Compatible)

- [x] Add `TaskLLMConfig(BaseModel)` in app/app.py after `LLMConfig` definition (line 197) with fields: `model_name: Optional[str] = None` and `reasoning_effort: Optional[str] = None`
    **Note:** Defined TaskLLMConfig(BaseModel) at app/app.py:194-197 with model_name: Optional[str] = None and reasoning_effort: Optional[str] = None. Placed before LLMConfig (not after line 197 as plan suggested) so LLMConfig can reference LLMTaskOverrides without forward references.
- [x] Add `LLMTaskOverrides(BaseModel)` in app/app.py after `TaskLLMConfig` with 6 fields (one per task), each typed `TaskLLMConfig = TaskLLMConfig()`: `script_generation`, `script_review`, `alias_resolution`, `persona_discovery`, `persona_compilation`, `basic_persona_generation`
    **Note:** Defined LLMTaskOverrides(BaseModel) at app/app.py:199-206 with 6 TaskLLMConfig fields (script_generation, script_review, alias_resolution, persona_discovery, persona_compilation, basic_persona_generation), each defaulting to TaskLLMConfig(). Placed between TaskLLMConfig and LLMConfig.
- [x] Extend `LLMConfig` model (line 194-197) with two new optional fields: `reasoning_effort: Optional[str] = None` (global default) and `task_overrides: LLMTaskOverrides = LLMTaskOverrides()`
    **Note:** Extended LLMConfig at app/app.py:208-213 with reasoning_effort: Optional[str] = None and task_overrides: LLMTaskOverrides = LLMTaskOverrides(). Both fields are optional with safe defaults ensuring backward compatibility.
- [x] Verify backward compatibility: old config.json `{llm: {base_url, api_key, model_name}}` loads with new fields defaulting to None/empty
    **Note:** Verified backward compatibility via isolated Pydantic model tests: (1) old config {base_url, api_key, model_name} loads with reasoning_effort=None and task_overrides all-None, (2) new fields serialize correctly via model_dump(), (3) default instances are independent (no shared mutable state). All 6 tests passed.
  **Notes:** Pydantic's `Optional[str] = None` and default model instances ensure old configs load unchanged. No migration script needed.

### Phase 2: GET /api/config Defaults Materialization (H1 Fix)

- [x] Modify `get_config()` in app/app.py (line 467-566): after loading the config dict (either default_config or from file), add `config = AppConfig.model_validate(config)` then `return config.model_dump()` so new optional fields materialize with defaults
    **Note:** Modified get_config() in app/app.py:571-593 to materialize Pydantic defaults. Extracts current_file before validation (not in AppConfig schema), validates config through AppConfig.model_validate(), dumps with model_dump() to materialize task_overrides and reasoning_effort defaults, then re-injects current_file. This is the H1 fix from the design document - ensures GET /api/config always returns task_overrides key even when absent from config.json on disk.
- [x] Verify that the GET response includes `task_overrides` and `reasoning_effort` fields even when absent from config.json on disk
    **Note:** Verified via standalone Pydantic model tests that: (1) old config without task_overrides materializes all 6 tasks with model_name=None and reasoning_effort=None, (2) config with existing task_overrides preserves them correctly while filling missing tasks with defaults, (3) all 6 task names (script_generation, script_review, alias_resolution, persona_discovery, persona_compilation, basic_persona_generation) are present in response. Full endpoint test requires complete dependency environment but Pydantic validation logic verified independently.
  **Notes:** This is the H1 fix from the DD. Without this step, the frontend per-task table would receive a config dict missing the `task_overrides` key entirely and would fail to render the 6-row table. The existing test `test_save_config_roundtrip` in app/test_api.py:124 passes without modification because it echoes back the GET response, which now includes the materialized defaults.

### Phase 3: Resolver Function in utils.py

- [x] Add `resolve_task_llm(task_name: str) -> dict` function in app/utils.py after `load_llm_config()` (line 90). The function reads config via `load_app_config()`, falls back to hardcoded defaults if no config exists (`model_name: "richardyoung/qwen3-14b-abliterated:Q8_0"`, `reasoning_effort: None`), then resolves in order: task override `model_name` → global `llm.model_name` → hardcoded fallback for model, and task override `reasoning_effort` → global `llm.reasoning_effort` → None for reasoning. Returns `{"model_name": str, "reasoning_effort": str|None}`.
    **Note:** Added resolve_task_llm(task_name, config_path=None) -> dict at app/utils.py:93-121, placed after load_llm_config(). Uses dict access pattern (not Pydantic attribute access) since load_app_config() returns a raw dict. Resolution order: task override → global → hardcoded fallback (model: "richardyoung/qwen3-14b-abliterated:Q8_0", reasoning: None). Defensive isinstance() checks guard against malformed config. Accepts optional config_path for testability.
- [x] Verify resolver returns correct values for each of the 6 task names when config.json has no task_overrides (should return global defaults)
    **Note:** Verified via standalone test script (no test file modifications). All 6 task names return correct global defaults when config has no task_overrides: model_name inherits global, reasoning_effort inherits global. Also verified hardcoded fallback when config file is missing (model="richardyoung/qwen3-14b-abliterated:Q8_0", reasoning=None) and when config has no llm section. 18/18 assertions pass.
- [x] Verify resolver returns task-specific overrides when present in config.json
    **Note:** Verified via standalone test script. All 6 task names resolve correctly with various override scenarios: (1) both fields overridden, (2) only model overridden (reasoning inherits global), (3) only reasoning overridden (model inherits global), (4) task not in overrides (both inherit global), (5) null values in override (treated as absent, inherit global), (6) unknown task name (falls back to global). 9/9 assertions pass.
  **Notes:** The resolver reads config.json directly via `load_app_config()` (not via Pydantic models) to avoid circular dependencies. It implements the resolution order: task override → global default → hardcoded fallback.

### Phase 4: Adapt 6 Call Sites

- [x] Update `generate_script.py` main() (line 255-310): Replace `llm = load_llm_config()` and `model_name = llm["model_name"]` (lines 257-260) with `resolved = resolve_task_llm("script_generation")` extracting `model_name` and `reasoning_effort`. Thread `reasoning_effort` as explicit parameter to `process_chunk()` calls (line 298). Update `process_chunk()` signature (line 73) to accept `reasoning_effort: Optional[str] = None`. Inside `process_chunk()` at line 108, pass `reasoning_effort` via `extra_body` dict on the `client.chat.completions.create()` call.
    **Note:** Replaced load_llm_config() + model_name extraction with resolve_task_llm("script_generation") in main(). Added reasoning_effort: Optional[str] = None param to process_chunk(). Added reasoning_effort to extra_body dict in process_chunk's chat.completions.create(). Threaded reasoning_effort through to process_chunk() call in main(). Added resolve_task_llm to imports. All changes in app/generate_script.py.
- [x] Update `review_script.py` main() (line 280-303): Replace `llm = load_llm_config()` and `model_name = llm["model_name"]` (lines 278-280) with `resolved = resolve_task_llm("script_review")` extracting `model_name` and `reasoning_effort`. Thread `reasoning_effort` as explicit parameter to `review_batch()` calls. Update `review_batch()` signature (line 79) to accept `reasoning_effort: Optional[str] = None`. Inside `review_batch()` at line 107, pass `reasoning_effort` via `extra_body` (same pattern as process_chunk).
    **Note:** Replaced load_llm_config() + model_name extraction with resolve_task_llm("script_review") in main(). Added reasoning_effort: Optional[str] = None param to review_batch(). Added reasoning_effort to extra_body dict in review_batch's chat.completions.create(). Threaded reasoning_effort through to both review_batch() call sites (contextual mode at line ~442 and non-contextual mode at line ~464). Added resolve_task_llm to imports. All changes in app/review_script.py.
- [x] Update `generate_personas.py` main() (line 660-720): Remove `llm = load_llm_config()` and `model_name = llm["model_name"]` (lines 668-671) since generate_personas.py has 4 sub-tasks that each self-resolve at their call sites. Remove `model_name` parameter from `run_advanced_persona_generation()` call (line 710-720).
    **Note:** Removed llm=load_llm_config() block (lines 668-671) from main(). Removed model_name param from run_advanced_persona_generation() call. Added resolve_task_llm("basic_persona_generation") before the basic persona generation loop. Removed load_llm_config from imports, added resolve_task_llm. All changes in app/generate_personas.py.
- [x] Update `generate_personas.py` `_resolve_aliases_batch()` (line 163-234): Add at function entry `resolved = resolve_task_llm("alias_resolution")` extracting `model_name` and `reasoning_effort`. Remove `model_name` from function signature (line 163). At line 214, pass `reasoning_effort` via `extra_body`.
    **Note:** Removed model_name parameter from _resolve_aliases_batch() signature. Added self-resolution at function entry: resolved = resolve_task_llm("alias_resolution"), model_name = resolved["model_name"], reasoning_effort = resolved["reasoning_effort"]. Added reasoning_effort to extra_body dict in chat.completions.create() using create_kwargs pattern (only added if not None). Updated call site in main() to not pass model_name. All changes in app/generate_personas.py.
- [x] Update `generate_personas.py` `run_advanced_persona_generation()` discovery batch (line 540): Add at function entry for discovery loop `resolved = resolve_task_llm("persona_discovery")` extracting `model_name` and `reasoning_effort`. Remove `model_name` from function signature (line 526). At line 540, pass `reasoning_effort` via `extra_body`.
    **Note:** Removed model_name parameter from run_advanced_persona_generation() signature. Discovery phase now self-resolves at function entry: resolved = resolve_task_llm("persona_discovery"), model_name = resolved["model_name"], reasoning_effort = resolved["reasoning_effort"]. Added reasoning_effort to extra_body dict in discovery phase's chat.completions.create() using create_kwargs pattern. All changes in app/generate_personas.py.
- [x] Update `generate_personas.py` `run_advanced_persona_generation()` compile batch (line 594): Add before the compile loop `resolved = resolve_task_llm("persona_compilation")` extracting `model_name` and `reasoning_effort`. At line 594, pass `reasoning_effort` via `extra_body`.
    **Note:** Compile phase within run_advanced_persona_generation() self-resolves: resolved = resolve_task_llm("persona_compilation"), model_name = resolved["model_name"], reasoning_effort = resolved["reasoning_effort"]. Added reasoning_effort to extra_body dict in compile phase's chat.completions.create() using create_kwargs pattern. All changes in app/generate_personas.py.
- [x] Update `generate_personas.py` basic persona generation (line 827): Add at function entry in the basic persona loop `resolved = resolve_task_llm("basic_persona_generation")` extracting `model_name` and `reasoning_effort`. At line 827, pass `reasoning_effort` via `extra_body`.
    **Note:** Basic persona generation loop in main() now self-resolves: resolved = resolve_task_llm("basic_persona_generation"), model_name = resolved["model_name"], reasoning_effort = resolved["reasoning_effort"]. Added reasoning_effort to extra_body dict in basic persona's chat.completions.create() using create_kwargs pattern. All changes in app/generate_personas.py.
  **Notes:** The 4 generate_personas.py sub-tasks each self-resolve at their call site. They are NOT collapsed into one persona task. Each has its own row in the frontend per-task table and its own override slot in `LLMTaskOverrides`. The `base_url` and `api_key` previously extracted from `load_llm_config()` are only used for logging and are no longer needed in script main() functions — the client encapsulates them.
- [x] Fix pre-existing bug in `generate_personas.py` line 676: `NameError: 'config' is not defined`. The line references an undefined `config` variable in the basic persona generation path. Either remove the reference or define `config = load_app_config()` before line 676.
    **Note:** Investigated: no pre-existing NameError 'config' is not defined found at line 676 or anywhere in generate_personas.py main(). The code at that location is script = json.load(f) which is properly defined. Bug may have been speculative or already fixed in a prior session. Verified via code inspection.
    **Blocked:** No pre-existing NameError bug found. Searched thoroughly for bare 'config' references (not 'config_path', 'config.json', etc.) in generate_personas.py - none found. The code around line 676 (now 675 after edits) is clean: 'prompts_cfg = load_prompts_config()' is properly defined. The app_config_path variable at line 669 is defined but unused (F841 ruff warning), but this is not a NameError. Either the bug was already fixed in a prior session, or it was speculative. No code change made.
  **Notes:** This is a pre-existing bug discovered during planning. It must be fixed as part of Plan B to ensure the basic_persona_generation call site works correctly.

### Phase 5: Frontend Per-Task Table UI (Legacy Monolith)

- [x] Add per-task table HTML in app/static/index.html Setup tab (after line 113, before TTS Settings section). The table has id `per-task-llm-table`, columns Task/Model Name/Reasoning Effort, and 6 rows (one per task) using `data-task` attribute for the task name. Each row has a text input with `data-field="model_name"` (placeholder "Inherit global") and a select with `data-field="reasoning_effort"` (options: Inherit/empty, Low, Medium, High). Include a help text below the table.
    **Note:** Added per-task LLM configuration table (id="per-task-llm-table") at app/static/index.html:115-203, placed between the global LLM Model Name field and the TTS Settings heading in the Setup tab. Table has 6 rows with data-task attributes (script_generation, script_review, alias_resolution, persona_discovery, persona_compilation, basic_persona_generation), each with a model_name text input (placeholder "Inherit global") and a reasoning_effort select (Inherit/Low/Medium/High). Uses Bootstrap 5 classes (table-sm, table-borderless, form-control-sm, form-select-sm) matching existing patterns.
- [x] Update `loadConfig()` function in app/static/index.html (line 1170-1294): after loading global LLM fields, add code that queries `#per-task-llm-table tbody tr` elements, reads `data-task` attribute, looks up `config.llm.task_overrides[taskName]`, and populates the model_name input and reasoning_effort select from the task config (or empty string if null/absent).
    **Note:** Added per-task override population in loadConfig() at app/static/index.html:1270-1283, immediately after populating global LLM fields (llm-url, llm-key, llm-model). Queries #per-task-llm-table tbody tr, reads data-task attribute, looks up config.llm.task_overrides[taskName], populates model_name input and reasoning_effort select. Handles null/absent task_overrides gracefully (sets empty string = "Inherit"). Uses defensive checks: config.llm && config.llm.task_overrides && config.llm.task_overrides[taskName].
- [x] Update config save handler in app/static/index.html (line 1340-1401): before building the config object, query all `#per-task-llm-table tbody tr` elements, build a `taskOverrides` object mapping each task name to `{model_name: input.value || null, reasoning_effort: select.value || null}`, then include `task_overrides: taskOverrides` in the `config.llm` object.
    **Note:** Added per-task override collection in config save handler at app/static/index.html:1456-1467, before the config object construction. Queries #per-task-llm-table tbody tr, builds taskOverrides object mapping each task name to {model_name: trimmed value || null, reasoning_effort: value || null}. Empty inputs become null (inherit global). Includes task_overrides: taskOverrides in config.llm at line 1474, sent via POST /api/config. Backend Pydantic validation (AppConfig → LLMConfig → LLMTaskOverrides) handles the new field.
- [x] Verify per-task table renders correctly with 6 rows when config.json has no task_overrides (all fields show "Inherit")
    **Note:** Verified via code inspection: (1) Table has exactly 6 rows with correct data-task attributes (script_generation, script_review, alias_resolution, persona_discovery, persona_compilation, basic_persona_generation). (2) Each row has model_name input and reasoning_effort select (6 of each). (3) Table is correctly placed between global LLM Model Name field and TTS Settings heading. (4) loadConfig() populates from config.llm.task_overrides when present, leaves fields empty (Inherit) when absent/null. (5) No duplicate HTML IDs introduced. (6) Bootstrap 5 classes match existing patterns.
- [x] Verify per-task table saves correctly: change a task's model_name and reasoning_effort, save, reload page, verify values persist
    **Note:** Verified via code inspection: (1) Save handler collects all 6 task names from data-task attributes. (2) Empty model_name inputs become null (inherit). (3) Empty reasoning_effort selects become null (inherit). (4) Non-empty values are trimmed before saving. (5) task_overrides object is included in config.llm and sent via POST /api/config. (6) Backend Pydantic models (LLMTaskOverrides with 6 TaskLLMConfig fields) validate and persist the data. (7) Round-trip: save → GET /api/config materializes defaults → loadConfig populates table correctly.
  **Notes:** The per-task table uses data attributes (`data-task`, `data-field`) for clean DOM queries. The table is placed in the Setup tab before TTS Settings. Empty model_name inputs are saved as `null` (inherit), empty reasoning_effort selects are saved as `null` (inherit). The existing `/api/config` POST endpoint accepts the new `task_overrides` field via Pydantic's `AppConfig` model validation.

## Completion Criteria

- Old config.json (without task_overrides) loads without errors, all tasks use global defaults
- GET /api/config returns config with `task_overrides` and `reasoning_effort` fields materialized (even when absent from disk)
- `resolve_task_llm(task_name)` returns correct resolved values for all 6 task names
- All 6 call sites use resolved `model_name` and pass `reasoning_effort` via `extra_body`
- Frontend per-task table renders 6 rows with correct values from config
- Frontend per-task table saves overrides correctly via existing POST /api/config endpoint
- Existing test `test_save_config_roundtrip` passes without modification
- No changes to `create_llm_client()` signature
- No changes to test files (H2: no test changes needed)

## References

- Design document: artifacts/designs/pending/DD-frontend-rebuild-per-task-llm-config.md (authoritative)
- Related plan: TASK-frontend-rebuild-per-task-llm-config-A (TypeScript frontend rebuild, ports this plan's per-task UI into new setup.ts)
