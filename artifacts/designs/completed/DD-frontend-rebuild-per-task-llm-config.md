# Frontend Rebuild (TypeScript + Vite) & Per-Task LLM Configuration — Design Document

> **Status:** SUPERSEDED — stale pre-universal-upgrade design. Do NOT use.
> **Superseded by:** [DD-universal-upgrade](../pending/DD-universal-upgrade.md) (Universal Upgrade decomposition, Plan G) — the per-task LLM configuration it scoped (resolve_task_llm) was replaced by the Plan G overlay config work (resolve_task_config), and the frontend rebuild it scoped was delivered as the pipeline-native frontend (Plans E–I). Archived in place per Plan J P5-S1 (TASK-universal-upgrade-J-single-speaker-undo-iteration); this repo has no designs/archived/ directory and prior stale artifacts (walk-definitions.md, DD-epub-audiobook-pipeline-rewrite-v3) are handled with an in-place SUPERSEDED header, so this file remains in artifacts/designs/completed/.

**Status:** Superseded (originally Completed; see SUPERSEDED header above)  
**Author:** rnd-dd-author  
**Created:** 2026-08-02  

---

## Scope

Workstream A: Rebuild app/static/index.html (4,085 lines: 3,046 JS, 49 CSS, 1,239 HTML) as TypeScript + Vite project producing committed dist/ artifact. 10 tabs preserved. Vanilla DOM. Bootstrap 5.3 + Font Awesome from CDN.

Workstream B: Add per-task LLM model_name + reasoning_effort configuration. 6 call sites adapted. Shared base_url/api_key with per-task overrides inheriting global defaults. Frontend table UI in Setup tab. Backward-compatible config.json schema.

---

## Problem Statement

The frontend is a 4,085-line monolithic HTML file with no build step, making it unmaintainable. Simultaneously, all 6 LLM call sites share one global model_name with no per-task customization or reasoning control. Both problems share the config.json surface and frontend UI layer, making them natural to solve together.

---

## Architecture

## Workstream A: Frontend Rebuild (TypeScript + Vite)

### Module Decomposition — Tab-per-Module with Global State Singleton

One TypeScript module per tab + one shared state singleton (`AppState`). Each module exports an `init()` function called from `main.ts`. HTML extracted to template strings in a `templates.ts` module colocated with logic. Single bundle output — no code splitting (unnecessary for a ~40KB desktop tool).

```
src/
  main.ts              # Bootstrap entry: init nav, mount all tabs
  state.ts             # Singleton: typed AppState interface
  api.ts               # Extracted API helper (typed endpoints)
  utils.ts             # showToast, showConfirm, escapeHtml
  tabs/
    setup.ts           # Config panel + per-task LLM table (Workstream B integration)
    script.ts          # Script generation
    voices.ts          # Voice config + persona generation
    designer.ts        # Voice designer
    preparer.ts        # Single/batch preparer
    dataset-builder.ts # Dataset builder
    training.ts        # LoRA training
    editor.ts          # Audio chunk editor
    audio.ts           # Result player + export
  templates.ts         # HTML template strings for all tab bodies
```

### Build Pipeline & Distribution Strategy

**Decision: Pre-built committed `dist/`.** Build locally with `npm run build`, commit `dist/` to repo. Vite outputs to `app/static/dist/`. `app.py` serves `dist/index.html` via existing FileResponse and mounts `/static` on `dist/`.

| Channel | Change Required | Risk |
|---------|----------------|------|
| Docker | None — `COPY app/` picks up `app/static/dist/` automatically | None |
| Pinokio | None — `git clone` gets committed `dist/` | None |
| Colab | None — `git clone` gets committed `dist/` | None |

**Stale-build mitigation:** CI check runs `npm run build && git diff --exit-code app/static/dist/` to catch forgotten rebuilds before merge. Pre-commit hook as optional local guard.

**app.py change:** Single line — `STATIC_DIR` path updated to `app/static/dist/`. No route changes.

### Migration Path (Incremental)
1. Scaffold Vite project alongside existing `index.html` (both coexist temporarily)
2. Extract `api.ts` + `utils.ts` (already isolated by function boundaries)
3. Extract `state.ts` (consolidate `window._*` globals into typed singleton)
4. Extract tabs one at a time — each tab is a self-contained, independently mergeable extraction
5. Replace `app/static/index.html` with Vite's `dist/index.html`
6. Update `STATIC_DIR` in `app.py` to point at `app/static/dist/`
7. Delete old `index.html`, add `.gitignore` exception for `dist/`

## Workstream B: Per-Task LLM Configuration

### Config Schema Evolution (Backward-Compatible)

```python
class TaskLLMConfig(BaseModel):                    # NEW
    model_name: Optional[str] = None               # None = inherit global
    reasoning_effort: Optional[str] = None          # None = inherit global

class LLMTaskOverrides(BaseModel):                 # NEW
    script_generation: TaskLLMConfig = TaskLLMConfig()
    script_review: TaskLLMConfig = TaskLLMConfig()
    alias_resolution: TaskLLMConfig = TaskLLMConfig()
    persona_discovery: TaskLLMConfig = TaskLLMConfig()
    persona_compilation: TaskLLMConfig = TaskLLMConfig()
    basic_persona_generation: TaskLLMConfig = TaskLLMConfig()

# Extended LLMConfig (backward-compatible — new fields optional):
class LLMConfig(BaseModel):
    base_url: str = "http://localhost:11434/v1"
    api_key: str = "local"
    model_name: str = "richardyoung/qwen3-14b-abliterated:Q8_0"
    reasoning_effort: Optional[str] = None          # NEW: global default
    task_overrides: LLMTaskOverrides = LLMTaskOverrides()  # NEW
```

**Backward compatibility:** Old config `{llm: {base_url, api_key, model_name}}` loads with all new fields defaulting to None/empty. No migration script needed.

### GET /api/config: Materializing Defaults

`GET /api/config` (in `app/app.py:467-566`, `get_config`) must deserialize the loaded config dict through `AppConfig.model_validate()` before returning it. This ensures that new optional fields (`task_overrides`, `reasoning_effort`) materialize with their Pydantic defaults even when absent from `config.json` on disk. Without this step, the frontend per-task table would receive a config dict missing the `task_overrides` key entirely and would fail to render the 6-row table.

```python
@app.get("/api/config")
async def get_config():
    raw = load_config_file()           # existing: reads config.json as dict
    config = AppConfig.model_validate(raw)  # NEW: materialize defaults
    return config.model_dump()         # returns dict with all defaults filled in
```

**Test impact (no changes needed):** `test_save_config_roundtrip` in `app/test_api.py:124` echoes back `original["llm"]` from the GET response. Once GET materializes defaults via `model_validate()`, the roundtrip naturally preserves them — the test passes without modification. No test changes are required for this workstream.

### LLM Task Resolver (Self-Loading, No Signature Change)

**Decision:** `create_llm_client()` in `utils.py` keeps its existing signature unchanged. A new small resolver function, `resolve_task_llm(task_name)`, reads `config.json` via `load_llm_config()` and returns the resolved `{model_name, reasoning_effort}` for that task.

```python
def resolve_task_llm(task_name: str) -> dict:
    """Returns {'model_name': str, 'reasoning_effort': str | None}.
    Resolution order: task-specific override → global default → hardcoded fallback.
    """
    config = load_llm_config()
    override = getattr(config.llm.task_overrides, task_name, None)
    model = (
        override.model_name if override and override.model_name
        else config.llm.model_name
    )
    reasoning = (
        override.reasoning_effort if override and override.reasoning_effort
        else config.llm.reasoning_effort
    )
    return {"model_name": model, "reasoning_effort": reasoning}
```

**Resolution path for model_name:** Scripts currently call `load_llm_config()` in their `main()` functions to extract `model_name`, then pass that `model_name` to their LLM call functions. Under this design, each task's entry point calls `resolve_task_llm(its_task)` ONCE and threads the resolved plain values (`model_name`, `reasoning_effort`) down to sub-functions as explicit parameters — matching the existing pattern in `generate_script.py:257-260` and `generate_personas.py:668-671`, which already load config at entry and thread `client, model_name` down.

**Script main() pattern under new design:**

```python
def main():
    # ... argument parsing, file loading ...

    # Resolve per-task LLM config ONCE at entry
    resolved = resolve_task_llm("script_generation")
    model_name = resolved["model_name"]
    reasoning_effort = resolved["reasoning_effort"]
    client, _ = create_llm_client()   # unchanged signature

    # Load non-model config separately
    prompts_config = load_prompts_config()
    generation_config = load_generation_config()

    # ... rest of main() threads model_name + reasoning_effort down ...

    for i, chunk in enumerate(chunks, 1):
        entries = process_chunk(
            client, model_name, chunk, i, total_chunks,
            reasoning_effort=reasoning_effort,   # explicit param
            # ... other params ...
        )
```

**Inside the LLM call site**, `reasoning_effort` is passed to `chat.completions.create()` via `extra_body` when the provider needs it:

```python
create_kwargs = {"model": model_name, "messages": messages, ...}
if reasoning_effort:
    create_kwargs["extra_body"] = {"reasoning_effort": reasoning_effort}
response = client.chat.completions.create(**create_kwargs)
```

All three scripts (`generate_script.py`, `review_script.py`, `generate_personas.py`) follow this pattern. The `base_url` and `api_key` previously extracted from `load_llm_config()` are only used for logging and are no longer needed in script `main()` functions — the client encapsulates them.

### 6 Call Sites Adaptation

Each call site self-resolves its own task name via `resolve_task_llm()` at its call site, loading "the correct thing" itself. This is consistent with the resolver design: each sub-function in `generate_personas.py` (alias resolution, discovery, compilation, basic persona gen) independently calls `resolve_task_llm(its_task)` so that each gets its own per-task override — they are NOT collapsed into one persona task.

| Call Site | File:Line | Task Name | Resolver Call |
|-----------|-----------|-----------|---------------|
| process_chunk | `generate_script.py:108` | `script_generation` | `resolve_task_llm("script_generation")` in main() |
| review_batch | `review_script.py:107` | `script_review` | `resolve_task_llm("script_review")` in main() |
| _resolve_aliases_batch | `generate_personas.py:214` | `alias_resolution` | `resolve_task_llm("alias_resolution")` at call site |
| discovery batch | `generate_personas.py:540` | `persona_discovery` | `resolve_task_llm("persona_discovery")` at call site |
| compile persona | `generate_personas.py:594` | `persona_compilation` | `resolve_task_llm("persona_compilation")` at call site |
| basic persona per speaker | `generate_personas.py:827` | `basic_persona_generation` | `resolve_task_llm("basic_persona_generation")` at call site |

The four `generate_personas.py` sub-tasks remain separate — each has its own row in the frontend per-task table and its own override slot in `LLMTaskOverrides`.

### Frontend Per-Task Model+Reasoning Table UI

In the Setup tab (config panel), add a table below the global LLM config section:

| Task | Model | Reasoning Effort |
|------|-------|-----------------|
| Script Generation | [inherit ▾] | [inherit ▾] |
| Script Review | [inherit ▾] | [inherit ▾] |
| Alias Resolution | [inherit ▾] | [inherit ▾] |
| Persona Discovery | [inherit ▾] | [inherit ▾] |
| Persona Compilation | [inherit ▾] | [inherit ▾] |
| Basic Persona Gen | [inherit ▾] | [inherit ▾] |

Each model dropdown shows "Inherit (global_model_name)" by default, or allows typing a specific model. Reasoning effort dropdown: none / low / medium / high / inherit. Changes save via existing `/api/config` POST endpoint.

---

## Design Goals

- Zero distribution breakage across Docker, Pinokio, and Colab
- Backward-compatible config schema (old flat config loads, new fields optional)
- Incremental migration path (each tab extraction independently mergeable)
- Per-task LLM config inherits global defaults unless explicitly overridden

---

## Constraints

- Backward compatibility: old config.json must load without errors or migration
- Distribution: no build step added to Docker/Pinokio/Colab runtime
- Bundle size: keep dist/ under 500KB (CDN Bootstrap, no framework)
- Migration: incremental — each tab extraction independently mergeable
- Single shared base_url/api_key — per-task overrides are model_name + reasoning_effort only
- Vanilla DOM only — no React/Vue/Svelte

---

## Effort Estimates

### Workstream A (Frontend Rebuild)
- **Scaffold Vite + extract api/utils/state:** 1.5 days
- **Extract 9 tabs (one at a time):** 1.5 days
- **Integration testing + dist/ commit + CI guard:** 1 day
- **Subtotal:** 4 days (SMALL-MEDIUM)

### Workstream B (Per-Task LLM Config)
- **Schema evolution (TaskLLMConfig, LLMTaskOverrides, extend LLMConfig):** 0.5 days
- **Resolver function (resolve_task_llm) + GET /api/config defaults materialization:** 0.5 days
- **6 call sites adaptation:** 0.25 days
- **Frontend table UI in Setup tab:** 0.5 days
- **Backward-compat testing:** 0.25 days
- **Subtotal:** 1.75 days (SMALL)

### Combined
- **Total:** ~6 days (MEDIUM)
- **Parallelism:** Workstream B can begin after Vite scaffold is in place (day 2). The Setup tab extraction (day 3-4) is where Workstream B's frontend table UI gets built into the new setup.ts module.
- **Risk buffer:** Add 1 day for Vite output path resolution (`--base /static/` question) and any CDN/asset path issues discovered during integration.

---

## Alternatives Considered

| Criterion | A: Committed dist/ | B: Multi-stage Docker | C: Build-at-Startup |
|-----------|-------------------|----------------------|---------------------|
| Docker | ✅ YES | ✅ YES (fresh) | ❌ NO (needs node) |
| Pinokio | ✅ YES | ⚠️ HYBRID | ❌ NO (no npm) |
| Colab | ✅ YES | ⚠️ HYBRID | ❌ NO (no npm) |
| Files touched | 2 | 3 | 5+ |
| Stale build risk | MEDIUM (CI mitigates) | LOW (Docker only) | LOW |
| Distribution change | None | Dockerfile +10 lines | All channels |

### Why Committed dist/ Wins
1. **Only approach working identically across all 3 channels.** Pinokio is primary — its non-technical users can't run npm.
2. **Stale-build risk solved by one CI check** — cheaper than maintaining two delivery paths.
3. **Industry precedent** — Pinokio ecosystem and AI tooling projects pre-commit built artifacts.
4. **Uniformity** — Docker/Pinokio/Colab all serve the same artifact. No divergence.

---

## Open Questions

1. **Vite base path:** Does Vite need `--base /static/` for asset paths to resolve through the existing `/static` StaticFiles mount? Must verify during scaffold phase — if yes, Vite config needs `base: '/static/'` and STATIC_DIR stays at `app/static/` with dist/ inside it.
2. **Reasoning effort validation:** Should `reasoning_effort` values be validated against a known set (none/low/medium/high) or passed through to the API? Different providers use different enums — OpenAI uses low/medium/high, Anthropic uses different values. Pass-through is more flexible but risks silent failures.
3. **Dataset builder tab:** Research confirms dataset builder uses TTS engine (VoiceDesign), NOT LLM. It has no LLM call site and doesn't need a per-task config row. Confirm this is correct before finalizing the table.

---
