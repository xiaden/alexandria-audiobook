# EPUB-to-Audiobook Pipeline Rewrite — Design Document

**Status:** Completed (Amended)  
**Author:** rnd-manager  
**Created:** 2026-08-02  
**Amended:** 2026-08-02 — UUID span model, temperature threading, annotated_script.json bridge  

---

## Scope

Rewrite the EPUB-to-audiobook pipeline to fix misattribution, mislabeling, and segmentation failures. The rewrite decomposes the monolithic LLM pass into an 11-step pipeline with: UUID-based span identity with presentation indices, single-purpose walks, a canonical character ledger, confidence-scored extraction, deterministic script assembly, human-in-the-loop review, and targeted re-attribution (no full re-walk). The TTS engine (tts.py) is reused unchanged. The rewrite unifies configuration loading, bridges to the existing annotated_script.json format, and provides a migration path from the current linear pipeline.

---

## Problem Statement

The current EPUB-to-audiobook pipeline suffers from high speech misattribution, mislabeling, and poor text segmentation. Specifically: (1) minor characters are frequently missed entirely, and (2) a character first introduced by name much later in the story is treated as a different character from their earlier unnamed mentions. This persists even with high-strength LLMs and improved prompting — proving it's a structural/architectural defect, not a prompt-tuning problem.

Root cause: `generate_script.py::process_chunk` sends 3000-char chunks to the LLM with only a character roster built from previous entries' speaker labels and last 3 entries for continuity. The LLM is asked to produce `{speaker, text, instruct}` per chunk — a single model is asked to "turn this book into an audiobook" in one pass. No scene boundaries, no character discovery before attribution, no alias resolution. The character roster propagates early mislabels, and late-introduced characters have no mechanism to link to earlier unnamed mentions.

---

## Architecture

### Span Model: UUID Identity with Presentation Indices

Spans are the immutable text units of the source EPUB. **Identity is decoupled from presentation:**

- **Storage:** UUIDs (immutable identity). Text content never changes.
- **Presentation:** Sequential indices `1..N` derived from mutable `seq` ordering. Agents, LLM, and humans operate ONLY on presentation indices.
- **Annotations:** Bind to UUIDs only — never to presentation indices.
- **Renumbering:** Free (recomputed per render). Non-cascading in identity space.

This makes structural edits (split/merge/move) free in identity space — new UUIDs are created, annotations transfer via content-overlap matching, and presentation renumbers. Immutability means TEXT content never changes; segmentation is a mutable overlay.

Full data model: [data-model.md](../parts/epub-audiobook-pipeline-rewrite/data-model.md).

### Walk DAG

```
[1] Span Extraction (UUIDs) → [2a] Scene Seg → [2b] Character Discovery → [2c] Alias Resolution
→ [2d] Quotation Attribution → [2e] Character Description → [2f] Delivery Context
→ [3] Confidence Review → [4] Character Ledger → [5] Speaker Attribution
→ [6] Deterministic Assembly → [6b] Write annotated_script.json bridge
→ [7] Persona Discovery → [8] Voice Generation (audition gate)
→ [9] TTS Rendering (tts.py REUSED) → [10] Audio QA → [11] Final Assembly
```

Walks 2a-2f are single-purpose LLM passes. Segmentation reviewed BEFORE dependent walks run. Attribution uses canonical ledger as primary truth; scene is context, not source of truth.

### Temperature Threading

Per-walk temperatures flow through `resolve_task_llm(task_name)` → `{model_name, reasoning_effort, temperature}`. Extraction walks (2a-2e) use **0.1** (deterministic fact extraction). Creative walks (2f, step 7) use **0.3**. The existing 0.6 default in `GenerationConfig` remains the global fallback for backward compatibility.

**Config flow:** Add `temperature: Optional[float] = None` to `TaskLLMConfig`. Add `temperature: float = 0.6` to `LLMConfig`. Each walk's task name maps to a temperature via `task_overrides` in config.json or hardcoded defaults.

**Why 0.1:** The existing 0.6 was for the monolithic pass that did everything at once. Extraction walks ask the LLM to identify facts already in the text — hallucination is catastrophic. Temperature 0.1 minimizes sampling variance.

Full temperature details: [walk-definitions.md](../parts/epub-audiobook-pipeline-rewrite/walk-definitions.md).

### Annotated Script Bridge

The new pipeline writes BOTH `pipeline_state/script.json` (new format with UUID span references) AND `annotated_script.json` (legacy format). The legacy format is a deterministic transformation — code, not LLM.

**Schema mapping:** `{line_id, span_ids, speaker, text, delivery, confidence, evidence_spans, scene_id}` → `{speaker, text, instruct}`. The `delivery` field maps to `instruct`. All other fields are dropped.

**Consumers:** `ProjectManager.load_chunks()`, editor tab (`/api/annotated_script`), `generate_personas.py`, M4B export, TTS rendering chain — all expect legacy schema. The bridge ensures they work unchanged.

Full bridge details: [implementation-notes.md](../parts/epub-audiobook-pipeline-rewrite/implementation-notes.md).

### Targeted Re-Attribution (No Full Re-Walk)

There is NO full-book re-walk on edit. The UUID span model enables surgical re-processing:

| Edit Type | Scope |
|-----------|-------|
| Scene boundary movement | Re-attribute ONLY scenes involved |
| Split/Merge | Content-overlap reconciliation only (mechanical) |
| Value edit (fix speaker/emotion) | Direct human correction, no re-walk |
| Structural edit (move/delete) | Annotations ride along via UUID binding |

**Caveat:** UUIDs make structural correction free but do NOT self-correct an annotation derived from wrong context — that requires targeted re-attribution or human correction.

### Confidence-Filtered Review Flow

**Threshold:** 0.7 auto-accept, < 0.7 surface to user. Configurable via `pipeline.confidence_threshold`.

**UI:** New "Annotation Review" tab between Extract and Voices. Shows low-confidence items with neighbor context (±2 paragraphs). User can accept, reject, edit, or re-submit to LLM with corrected context.

### Voice Pipeline (Qwen3-TTS Constraints)

Qwen3-TTS bundles identity + emotion in clone reference. Pipeline:
1. Character evidence → textual voice description (LLM, `voice_description`, temp 0.3)
2. Voice description → VoiceDesign → generated reference sample
3. Human auditions → accepts or regenerates
4. Accepted sample becomes clone reference
5. Reference text: verbatim character speech, 8-30 words, emotionally representative

### Deterministic Assembly

Code combines immutable text spans (UUID-referenced) + scene annotations + speaker attribution + delivery annotations into ScriptLine objects. No LLM in assembly. After assembly, writes both new format and legacy `annotated_script.json`.

### Reuse Map

- **REUSED UNCHANGED:** `tts.py::TTSEngine` (1706 lines)
- **REUSED + EXTENDED:** `utils.py` (add temperature), `app.py::TaskLLMConfig` (add temperature field)
- **REUSED (mostly):** `project.py::ProjectManager` (reads annotated_script.json via bridge)
- **REPLACED:** `generate_script.py`, `review_script.py`, `generate_personas.py`, `extract_epub_text()`
- **EXTENDED:** `app.py` endpoints (new pipeline endpoints)

### Config Unification

`find_config_path()` returns `ALEXANDRIA_CONFIG_PATH` if set, else `app/config.json`. All subprocesses use `resolve_task_llm(task_name)` which calls `find_config_path()` internally.

**Bug:** Docker mounts `./data/config:/alexandria/config` AND sets `ALEXANDRIA_CONFIG_PATH=/alexandria/config/config.json`. But `find_config_path()` falls back to `app/config.json` (relative to script). `ProjectManager.__init__` (project.py:99) uses `ALEXANDRIA_CONFIG_PATH or os.path.join(root_dir, "app", "config.json")` — a THIRD path resolution.

**Fix:** Unify to single path. All code paths use `find_config_path()` from utils.py. Remove the fallback in `ProjectManager.__init__` — use `find_config_path()` instead (currently uses `os.environ.get("ALEXANDRIA_CONFIG_PATH") or os.path.join(root_dir, "app", "config.json")` which can resolve to a different path than `find_config_path()`). No code path bypasses this.

### API Endpoint Changes

**Deprecated:** `/api/generate_script`, `/api/review_script`, `/api/review_script_contextual`, `/api/generate_personas`

**New:** `/api/pipeline/extract`, `/api/pipeline/walk/{walk_name}`, `/api/pipeline/annotations`, `/api/pipeline/annotations/{id}/review`, `/api/pipeline/assemble`, `/api/pipeline/persona_discovery`, `/api/pipeline/characters`, `/api/pipeline/span_op` (SPLIT/MERGE/MOVE/DELETE/RENAME)

**Untouched:** All LoRA/dataset_builder/preparer endpoints, `/api/voice_design/*`, `/api/clone_voices/*`, `/api/chunks/*`, `/api/merge*`, `/api/export_audacity`

### Migration Strategy

**Phased with parallel run capability:**

**Phase 1 (1w):** Data model (UUID spans) + EPUB extraction rewrite preserving structure
**Phase 2 (2w):** Walk DAG core — walks 2a-2f as subprocesses, confidence scoring, temperature threading
**Phase 3 (1w):** Deterministic assembly + annotated_script.json bridge + Annotation Review tab
**Phase 4 (1w):** Persona discovery from evidence + voice generation with audition gate
**Phase 5 (1w):** TTS integration + Audio QA + deprecate old endpoints + config unification

Old and new pipelines coexist during migration. User toggles in Setup tab.

---

## Design Goals

- Fix misattribution and late-introduction failures via architectural decomposition, not prompt tuning
- Build authoritative annotation layers one narrowly defined task at a time, then assemble deterministically
- Preserve immutable source text — LLM annotations attach to span UUIDs, never rewrite original
- UUID identity with presentation indices — structural edits are free, annotations survive re-segmentation
- Human-in-the-loop review for low-confidence items — not unsupervised adversarial loop
- Reuse tts.py unchanged — most valuable asset
- Maintain backward compatibility during migration (dual-output bridge for annotated_script.json)

---

## Constraints

- tts.py (TTSEngine, 1706 lines) MUST be reused unchanged — handles CustomVoice, Clone, VoiceDesign, LoRA, batch optimization, ROCm/NVIDIA, sub-batching, codec compilation
- LoRA training, dataset builder, preparer features are orthogonal — must not be touched
- All LLM subprocesses read config via ALEXANDRIA_CONFIG_PATH; extraction tasks temp=0.1, creative tasks temp=0.3; empty responses must be null-safe
- `TaskLLMConfig` extended with `temperature: Optional[float] = None` — backward-compatible (None = inherit global 0.6)
- Config.json split bug must be unified — single source of truth via `find_config_path()`
- Frontend is TypeScript+Vite, 13 modules, committed dist/ — pipeline UI integrates with existing tab structure
- Distribution channels (Docker, Pinokio, Colab) all run python app.py from app/ — no build step added

---

## Open Questions

1. **Confidence threshold tuning:** Is 0.7 the right default, or should it be configurable per walk?
2. ~~**Walk re-execution policy:** If user edits character ledger after walks 2a-2f, should downstream walks auto-re-run or be manually triggered?~~ **RESOLVED:** Targeted re-attribution, not full re-walk. Only affected scenes re-processed. Manual trigger for targeted re-run.
3. ~~**Span ID versioning:** If user re-uploads same EPUB, should span IDs be preserved (requires diffing) or is full reset acceptable?~~ **RESOLVED:** UUID model makes this moot. Re-upload = new UUIDs, full pipeline reset. No diffing needed — UUIDs are cheap and content-hash reconciliation handles annotation transfer within a session.
4. **Voice reference text length:** 8-30 words — should this be configurable per character?
5. **Audio QA automation:** Should optional STT-based sanity checks (clipping, silence, volume) be added as pre-filter before human review?
6. **Migration deprecation timeline:** How long should old pipeline coexist? 1 release? 3 months?

---

## Design Rationale

1. **UUID span model** makes structural edits free. Annotations bind to UUIDs, so re-segmentation preserves attribution via content-overlap matching. Presentation indices are ephemeral.
2. **Walk decomposition** fixes misattribution by separating concerns: scene segmentation → character discovery → alias resolution → attribution. Each walk is narrowly scoped.
3. **Late-introduction failure** fixed by canonical character ledger. Alias resolution builds roster before attribution, so unnamed mentions link to later-introduced characters.
4. **Confidence scoring** surfaces uncertainty. Low-confidence items shown to user with neighbor context.
5. **Dual-output bridge** ensures backward compatibility. `annotated_script.json` becomes derived artifact.

---

## Implementation Details

Implementation notes (confidence thresholds, temperature policy, span operations, auditability, annotated_script.json bridge, migration coexistence, walk parallelism, frontend integration) in [implementation-notes.md](../parts/epub-audiobook-pipeline-rewrite/implementation-notes.md).

Edge cases and risks (walk 2d accuracy, ledger merge conflicts, voice reference selection, audio QA regeneration, span operation edge cases, migration bridge risks, config unification, temperature departure) in [edge-cases.md](../parts/epub-audiobook-pipeline-rewrite/edge-cases.md).

---
