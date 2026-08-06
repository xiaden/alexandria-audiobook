# Walk Definitions

> **Status:** SUPERSEDED — stale v2 design. Do NOT use.
>
> **Superseded by:** the v3 9-walk serial DAG (2a→2i) defined in [CONTRACTS.md](./CONTRACTS.md).
> This file documents the v2 6-walk DAG (2a-2f), file-based `pipeline_state/` JSON storage,
> and content-overlap (Jaccard) re-attribution — all replaced by the SQLite-WAL two-graph
> model and the canonical walk order in `app/pipeline/walks/order.py`.
> Retained for historical reference only.

## Walk Table

| Walk | Input | Output | LLM Task Name | Temp | Confidence |
|------|-------|--------|---------------|------|-----------|
| 2a: Scene Segmentation | Raw chapter text (sentence span UUIDs) | Scene boundary annotations per paragraph UUID | `scene_segmentation` | 0.1 | High: structural markers; Low: ambiguous |
| 2b: Character Discovery | Scene annotations + text (span UUIDs) | Character mention annotations with span UUID refs | `character_discovery` | 0.1 | High: explicit names; Low: pronouns |
| 2c: Alias Resolution | Character roster from 2b | Alias groups → canonical IDs (ledger entries) | `script_alias_resolution` | 0.1 | High: exact; Low: fuzzy/partial |
| 2d: Quotation Attribution | Scenes + chars + aliases + text (span UUIDs) | Speaker attribution annotations per quotation span UUID | `quotation_attribution` | 0.1 | High: explicit "X said"; Low: implicit |
| 2e: Character Description | Narration near character intro spans | Physical/social/origin trait annotations | `character_description` | 0.1 | High: explicit; Low: inferred |
| 2f: Delivery Context | Scene-level text (span UUIDs) | Emotional/delivery annotations per span UUID | `delivery_context` | 0.3 | High: explicit emotion; Low: subtext |

## Temperature Threading

### Config Flow

```
config.json
  └─ llm.task_overrides.<task_name>.temperature  (optional, per-task)
  └─ llm.temperature                              (optional, global default)
  └─ hardcoded fallback: 0.6                      (backward-compatible)
```

**Resolution:** `resolve_task_llm(task_name)` returns `{model_name, reasoning_effort, temperature}`. Each walk calls `resolve_task_llm("scene_segmentation")` etc. and passes `temperature` to `client.chat.completions.create()`.

**Config model change:** Add `temperature: Optional[float] = None` to `TaskLLMConfig` (app.py:194). Add `temperature: float = 0.6` to `LLMConfig` (app.py:208). Add `temperature` to `resolve_task_llm()` return dict (utils.py:93).

### Per-Walk Temperature Defaults

| Walk | Default Temp | Rationale |
|------|-------------|-----------|
| 2a-2e (extraction) | 0.1 | Deterministic fact extraction. Scene boundaries, character names, alias groups, speaker attributions, physical descriptions — all factual. Low temperature prevents hallucinated characters or false attributions. |
| 2f (delivery context) | 0.3 | Interpretive task. Emotional tone is subjective; slight variation is acceptable and may capture nuance. |
| Voice description (Step 7) | 0.3 | Creative synthesis from evidence. |

### Why 0.1 for Extraction (6× Below Existing 0.6 Default)

The existing `temperature=0.6` in `GenerationConfig` (app.py:251) and `process_chunk()` (generate_script.py:74) was tuned for the **monolithic** generate_script pass that did scene detection, character discovery, alias resolution, and speaker attribution — all in one LLM call per 3000-char chunk. That pass needed creative flexibility because it was doing everything at once.

The new pipeline separates extraction from creative work. Extraction walks (2a-2e) ask the LLM to identify facts that already exist in the text — scene boundaries, character names, who said what. These are deterministic tasks where hallucination is catastrophic (a hallucinated character name propagates through all downstream walks). Temperature 0.1 minimizes sampling variance while still allowing the model to handle edge cases.

The 0.6 default remains the global fallback for backward compatibility with the existing pipeline. New walk task names set their own temperatures via `task_overrides` in config.json or hardcoded defaults in each walk's task definition.

### Config Example

```json
{
  "llm": {
    "base_url": "http://localhost:11434/v1",
    "api_key": "local",
    "model_name": "richardyoung/qwen3-14b-abliterated:Q8_0",
    "temperature": 0.6,
    "task_overrides": {
      "scene_segmentation": { "temperature": 0.1 },
      "character_discovery": { "temperature": 0.1 },
      "script_alias_resolution": { "temperature": 0.1 },
      "quotation_attribution": { "temperature": 0.1 },
      "character_description": { "temperature": 0.1 },
      "delivery_context": { "temperature": 0.3 }
    }
  }
}
```

If a task name is not in `task_overrides`, it inherits the global `llm.temperature` (default 0.6).

## Walk Execution

- **Sequential dependency:** 2a → 2b → 2c → 2d → 2e → 2f (each depends on prior output)
- **Within-walk parallelism:** Each walk can parallelize by chapter/scene where possible
- **Evidence storage:** JSON in `pipeline_state/annotations/` directory. Each walk writes its own file. Enables re-running individual walks.
- **Walk re-execution:** If user edits character ledger after walks 2a-2f, downstream walks must be manually re-triggered (not auto-re-run). See Targeted Re-Attribution below.

## Targeted Re-Attribution (Replaces Full Re-Walk)

There is NO full-book re-walk on edit. The UUID-based span model enables surgical re-processing:

| Edit Type | Re-Attribution Scope | Re-Walk Needed? |
|-----------|---------------------|-----------------|
| Scene boundary movement | Re-attribute ONLY the scenes involved (spans whose scene membership changed) | Targeted re-run of 2b-2f for affected scenes only |
| Split/Merge | Re-attribute ONLY the split/merged items via content-overlap reconciliation | No re-walk — mechanical content matching |
| Value edit (fix speaker/emotion tag) | Direct human correction | No re-walk |
| Structural edit (move/delete) | Annotations ride along (UUID binding) or are orphaned | No re-walk |

**Caveat:** UUIDs make structural correction free but do NOT self-correct an annotation derived from wrong context. If scene boundaries change such that a character mention is now in a different scene, the attribution walk (2d) should re-run for that scene — but only that scene, not the full book.
