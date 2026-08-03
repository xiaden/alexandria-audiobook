# Implementation Notes

## Confidence Threshold

- **Default:** 0.7 auto-accept, < 0.5 auto-reject, 0.5-0.7 surface to user
- **Configurable:** `pipeline.confidence_threshold` in config.json
- **Per-walk override:** Optional — some walks may need stricter/looser thresholds (e.g., scene segmentation can be stricter, quotation attribution looser)

## Temperature Policy

### Config Flow

Temperature flows through the existing `resolve_task_llm()` pattern in `app/utils.py`:

```python
# utils.py — resolve_task_llm() extended with temperature
def resolve_task_llm(task_name: str, config_path=None) -> dict:
    """Returns {'model_name': str, 'reasoning_effort': str|None, 'temperature': float}."""
    # ... existing model/reasoning resolution ...
    
    # Temperature: task override -> global -> hardcoded fallback
    _FALLBACK_TEMPERATURE = 0.6
    global_temp = llm.get("temperature", _FALLBACK_TEMPERATURE)
    override_temp = task_override.get("temperature") if isinstance(task_override, dict) else None
    temperature = override_temp if override_temp is not None else global_temp
    
    return {"model_name": model, "reasoning_effort": reasoning, "temperature": temperature}
```

**Config model changes (app.py):**
- `TaskLLMConfig` (line 194): Add `temperature: Optional[float] = None`
- `LLMConfig` (line 208): Add `temperature: float = 0.6`
- `LLMTaskOverrides` (line 199): Add new task names for pipeline walks

### Per-Walk Temperature Defaults

| Walk | Task Name | Default Temp | Rationale |
|------|-----------|-------------|-----------|
| 2a: Scene Segmentation | `scene_segmentation` | 0.1 | Structural markers are factual |
| 2b: Character Discovery | `character_discovery` | 0.1 | Names are factual |
| 2c: Alias Resolution | `script_alias_resolution` | 0.1 | Exact matching is factual |
| 2d: Quotation Attribution | `quotation_attribution` | 0.1 | Speaker identity is factual |
| 2e: Character Description | `character_description` | 0.1 | Physical traits are factual |
| 2f: Delivery Context | `delivery_context` | 0.3 | Emotional interpretation allows variation |
| Step 7: Voice Description | `voice_description` | 0.3 | Creative synthesis from evidence |

### Why 0.1 for Extraction (6× Below Existing 0.6 Default)

The existing `temperature=0.6` (in `GenerationConfig` at app.py:251 and `process_chunk()` at generate_script.py:74) was tuned for the monolithic generate_script pass that did everything at once — scene detection, character discovery, alias resolution, and speaker attribution in a single LLM call per 3000-char chunk. That pass needed creative flexibility because it was doing everything at once.

The new pipeline separates extraction from creative work. Extraction walks (2a-2e) ask the LLM to identify facts that already exist in the text — scene boundaries, character names, who said what. These are deterministic tasks where hallucination is catastrophic (a hallucinated character name propagates through all downstream walks). Temperature 0.1 minimizes sampling variance while still allowing the model to handle edge cases.

The 0.6 default remains the global fallback for backward compatibility. New walk task names set their own temperatures via `task_overrides` in config.json or hardcoded defaults in each walk's task definition.

### How Temperature Reaches the LLM Call

Each walk subprocess:
1. Calls `resolve_task_llm("walk_task_name")` → gets `{model_name, reasoning_effort, temperature}`
2. Creates LLM client via `create_llm_client()` → gets `(OpenAI client, model_name)`
3. Passes `temperature=resolved_config["temperature"]` to `client.chat.completions.create()`

This mirrors the existing pattern in `generate_personas.py` where temperatures are hardcoded per call site (0.1, 0.2, 0.25, 0.3 at lines 224, 587, 650, 888). The new pipeline makes these configurable via config.json instead of hardcoded.

## Span Operations

### Operation Interface

Agents, the LLM, and humans issue operations against **presentation indices** (sequential numbers 1..N). The storage layer resolves index→UUID, executes, and renumbers.

```python
@dataclass
class SpanOperation:
    op_type: str          # "SPLIT" | "MERGE" | "MOVE" | "DELETE" | "RENAME"
    span_idx: int         # Presentation index (1-based)
    params: dict          # Operation-specific parameters

# Examples:
# SPLIT(span_idx=7, position="after word 'word_x'")
# MERGE(span_idx_a=3, span_idx_b=4)
# MOVE(span_idx=5, new_position=2)
# DELETE(span_idx=9)
# RENAME(scene_id="scene_abc", new_name="The Ballroom")
```

### Execution Flow

1. **Resolve index→UUID:** `presentation_indices[span_idx]` → `span_id`
2. **Execute operation:** Modify span(s), create new UUIDs if needed
3. **Reconcile annotations:** Content-overlap matching transfers annotations from old UUIDs to new UUIDs
4. **Renumber:** Recompute `seq` fields, presentation indices update automatically on next render

### Content-Overlap Reconciliation Algorithm

When spans are split or merged, annotations bound to old UUIDs must transfer to new UUIDs:

1. **Normalized-token Jaccard:** Tokenize (lowercase, strip punctuation), compute `|A ∩ B| / |A ∪ B|`. Threshold ≥ 0.6 → transfer.
2. **Longest-common-substring fallback:** If Jaccard below threshold, check LCS ≥ 50% of shorter span's length → transfer.
3. **Partial overlap:** If a single old annotation spans multiple new spans, assign to the span with highest overlap. Log ambiguity for human review.

**Caveat:** UUIDs make structural correction free but do NOT self-correct an annotation derived from wrong context. If the LLM attributed a quote to the wrong speaker because it lacked scene context, re-segmentation preserves that wrong attribution — targeted re-attribution or human correction is required.

## Auditability

UUIDs + operations create an immutable audit trail:

- **Every span operation is logged:** Operation type, presentation indices, resulting UUIDs, timestamp, operator (agent/LLM/human).
- **Annotations bind to UUIDs:** Attribution is traceable back to source text. An annotation's `span_id` points to the exact sentence/paragraph it was derived from.
- **Re-segmentation preserves assignments:** Content-overlap matching ensures annotations ride along when spans are split/merged. The audit log records which old UUID → new UUID mapping was used.
- **Walk outputs are versioned by re-run:** Each walk writes to `pipeline_state/annotations/walk_XX_name.json`. Re-running a walk overwrites the file, but the operation log preserves the history of what changed.

This enables:
- Debugging misattributions by tracing annotation → span → source text
- Understanding why a character was attributed to a quote (which walk, which evidence)
- Reverting structural edits by replaying operations in reverse

## Evidence Storage

- **Location:** `pipeline_state/annotations/` directory within project
- **Format:** JSON per walk (e.g., `walk_2a_scenes.json`, `walk_2d_attributions.json`)
- **Benefits:** Enables re-running individual walks, debugging, audit trail
- **Cleanup:** Old walk outputs overwritten on re-run (no versioning)

## Migration Coexistence

### Dual-Output Bridge (Option A: Write Compatible annotated_script.json)

After Step 6 (Deterministic Script Assembly), the new pipeline writes BOTH:
1. `pipeline_state/script.json` — new format with UUID span references
2. `annotated_script.json` — legacy format, for backward compatibility

The legacy format is a deterministic transformation of the new format — code, not LLM.

### Schema Mapping: ScriptLine → annotated_script.json

**New format (pipeline_state/script.json):**
```json
[
  {
    "line_id": "uuid-abc-123",
    "span_ids": ["uuid-sent-001", "uuid-sent-002"],
    "speaker": "ELIZABETH_BENNET",
    "text": "\"I do not know her.\"",
    "delivery": "sarcastic",
    "confidence": 0.92,
    "evidence_spans": ["uuid-sent-001"],
    "scene_id": "scene-ch3-ballroom"
  }
]
```

**Legacy format (annotated_script.json):**
```json
[
  {
    "speaker": "ELIZABETH_BENNET",
    "text": "\"I do not know her.\"",
    "instruct": "sarcastic"
  }
]
```

**Transformation code:**
```python
def write_legacy_annotated_script(script_lines: list[ScriptLine], output_path: str):
    """Deterministic transformation: new format → legacy annotated_script.json."""
    legacy_entries = []
    for line in script_lines:
        entry = {
            "speaker": line.speaker,
            "text": line.text,
        }
        if line.delivery:
            entry["instruct"] = line.delivery
        legacy_entries.append(entry)
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(legacy_entries, f, indent=2, ensure_ascii=False)
```

### What Reads annotated_script.json

| Consumer | Location | What It Reads |
|----------|----------|---------------|
| `ProjectManager.load_chunks()` | project.py:126 | Reads `annotated_script.json`, groups into chunks via `group_into_chunks()` |
| `ProjectManager.generate_chunk_audio()` | project.py:297 | Uses chunks from `load_chunks()` |
| `ProjectManager.merge_audio()` | project.py:431 | Uses chunks with audio paths |
| `ProjectManager.merge_m4b()` | project.py:531 | Uses chunks for M4B chapter structure |
| `/api/annotated_script` endpoint | app.py:876 | Returns raw `annotated_script.json` to frontend editor tab |
| `generate_personas.py` | generate_personas.py:694 | Reads script to extract character evidence |
| `review_script.py` | review_script.py:296 | Reads script for confidence review (REPLACED by new pipeline) |

All consumers expect the legacy schema: `{speaker, text, instruct?}`. The dual-output bridge ensures they continue to work unchanged.

### Deprecation Path

- **Phase 1-4:** New pipeline writes both formats. Old pipeline reads/writes legacy format only.
- **Phase 5:** Old pipeline endpoints deprecated. Frontend editor tab migrated to read new format (optional — legacy format works).
- **Post-migration:** `annotated_script.json` becomes a derived artifact (generated from `pipeline_state/script.json`). Can be removed once all consumers are migrated.

### User Toggle

Setup tab has "Use new pipeline (experimental)" checkbox. When enabled:
- New pipeline endpoints are active
- `annotated_script.json` is written by the new pipeline (dual-output)
- Old pipeline endpoints return 410 Gone

## Walk Parallelism

- **Sequential dependency:** Walks 2a-2f must run in order (each depends on prior)
- **Within-walk parallelism:** Each walk can process chapters/scenes in parallel
- **Example:** Walk 2b (Character Discovery) processes all chapters in parallel, then aggregates

## Frontend Integration

- **New tab:** "Annotation Review" between Extract and Voices
- **Shows:** Low-confidence items with neighbor context (±2 paragraphs)
- **Actions:** Accept, reject, edit, re-submit to LLM with corrected context
- **Character ledger editor:** View/edit canonical names, aliases, relationships
- **Span operations UI:** Present spans as sequential numbers (1..N). User issues SPLIT/MERGE/MOVE/DELETE/RENAME operations via UI. Backend resolves to UUIDs.
