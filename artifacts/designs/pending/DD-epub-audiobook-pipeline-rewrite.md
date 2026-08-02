# EPUB-to-Audiobook Pipeline Rewrite — Design Document

**Status:** Draft  
**Author:** rnd-manager  
**Created:** 2026-08-02  

---

## Scope

Rewrite the EPUB-to-audiobook pipeline in the Alexandria application to fix misattribution, mislabeling, and segmentation failures. The rewrite decomposes the monolithic LLM pass into an 11-step pipeline with single-purpose walks, a canonical character ledger, confidence-scored extraction, deterministic script assembly, and human-in-the-loop review. The TTS engine (tts.py) is reused unchanged. The rewrite addresses the config.json split bug, unifies configuration loading, and provides a migration path from the current linear pipeline.

---

## Problem Statement

The current EPUB-to-audiobook pipeline suffers from high speech misattribution, mislabeling, and poor text segmentation. Specifically: (1) minor characters are frequently missed entirely, and (2) a character first introduced by name much later in the story is treated as a different character from their earlier unnamed mentions. This persists even with high-strength LLMs and improved prompting — proving it's a structural/architectural defect, not a prompt-tuning problem.

Root cause: `generate_script.py::process_chunk` sends 3000-char chunks to the LLM with only a character roster built from previous entries' speaker labels and last 3 entries for continuity. The LLM is asked to produce `{speaker, text, instruct}` per chunk — a single model is asked to "turn this book into an audiobook" in one pass. No scene boundaries, no character discovery before attribution, no alias resolution. The character roster propagates early mislabels, and late-introduced characters have no mechanism to link to earlier unnamed mentions.

---

## Architecture

## Pipeline Architecture

### Walk DAG (Dependency Graph)

```
EPUB Extract → [1] Immutable Span Extraction
                       ↓
              [2a] Scene Segmentation (raw text)
                       ↓
              [2b] Character Discovery (scenes + text)
                       ↓
              [2c] Alias/Duplicate Resolution (character roster)
                       ↓
              [2d] Quotation Attribution (scenes + characters + aliases + text)
                       ↓
              [2e] Character Description Extraction (narration near character intros)
                       ↓
              [2f] Delivery/Emotional Context Extraction (scene-level)
                       ↓
              [3] Confidence-Filtered Review (human-in-the-loop)
                       ↓
              [4] Canonical Character Ledger (evidence accumulation)
                       ↓
              [5] Speaker Attribution (every quotation → narrator | character_id | unknown | disputed)
                       ↓
              [6] Deterministic Script Assembly (code, not LLM)
                       ↓
              [7] Persona Discovery (audible-only properties from evidence)
                       ↓
              [8] Voice Generation (VoiceDesign → clone reference → human audition)
                       ↓
              [9] TTS Rendering (tts.py::TTSEngine — REUSED UNCHANGED)
                       ↓
              [10] Audio QA (human listening, per-line regeneration)
                       ↓
              [11] Final Assembly (order by source IDs, chapter boundaries, M4B export)
```

Walks 2a-2f are single-purpose LLM passes. Each receives prior walks' accumulated output as context. Only 2a operates on raw text; all others receive structured annotations.

### Data Model

Span hierarchy (immutable source), character ledger, and script line schema defined in [data-model.md](../parts/epub-audiobook-pipeline-rewrite/data-model.md).

### Walk Definitions

6 single-purpose LLM walks (2a-2f) with sequential dependency. Walk inputs, outputs, task names, temperatures, and confidence criteria in [walk-definitions.md](../parts/epub-audiobook-pipeline-rewrite/walk-definitions.md).

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

Code combines immutable text spans + scene annotations + speaker attribution + delivery annotations into ScriptLine objects. No LLM in assembly.

### Reuse Map

| Component | Status | Notes |
|-----------|--------|-------|
| `tts.py::TTSEngine` | REUSED UNCHANGED | 1706 lines |
| `project.py::ProjectManager` | REUSED (mostly) | Chunk mgmt, parallel gen, merge, M4B |
| `utils.py` | REUSED + EXTENDED | Add new task names |
| `generate_script.py` | REPLACED | Root cause |
| `review_script.py` | REPLACED | Confidence review replaces text-loss |
| `generate_personas.py` | REPLACED | Canonical ledger + persona discovery |
| `extract_epub_text()` | REPLACED | Must preserve hierarchy |
| `app.py` endpoints | EXTENDED | New pipeline endpoints |

### Migration Strategy

**Phased with parallel run capability:**

**Phase 1 (1w):** Data model + EPUB extraction rewrite preserving structure
**Phase 2 (2w):** Walk DAG core — walks 2a-2f as subprocesses, confidence scoring
**Phase 3 (1w):** Deterministic assembly + Annotation Review tab
**Phase 4 (1w):** Persona discovery from evidence + voice generation with audition gate
**Phase 5 (1w):** TTS integration + Audio QA + deprecate old endpoints

Old and new pipelines coexist during migration. User toggles in Setup tab.

### Incremental vs. Full Rewrite

**Incremental (patch generate_script.py):** 2-3 weeks. Adds scene/character pre-passes but doesn't fix circular dependency in character roster construction. Late-introduction problem persists. **Insufficient.**

**Full Walk DAG (this design):** 6-8 weeks. Fixes root cause by decomposing into single-purpose walks. Late-introduction solved by alias resolution before attribution. **Justified** — failure persists across model upgrades, proving structural defect.

### Config Unification

`find_config_path()` returns `ALEXANDRIA_CONFIG_PATH` if set, else `app/config.json`. All subprocesses use `resolve_task_llm(task_name)` which calls `find_config_path()` internally. No code path bypasses this. Docker sets `ALEXANDRIA_CONFIG_PATH=/alexandria/config/config.json`. Remove duplicate fallback paths.

### API Endpoint Changes

**Deprecated:** `/api/generate_script`, `/api/review_script`, `/api/review_script_contextual`, `/api/generate_personas`

**New:** `/api/pipeline/extract`, `/api/pipeline/walk/{walk_name}`, `/api/pipeline/annotations`, `/api/pipeline/annotations/{id}/review`, `/api/pipeline/assemble`, `/api/pipeline/persona_discovery`, `/api/pipeline/characters`

**Untouched:** All LoRA/dataset_builder/preparer endpoints, `/api/voice_design/*`, `/api/clone_voices/*`, `/api/chunks/*`, `/api/merge*`, `/api/export_audacity`

---

## Design Goals

- Fix misattribution and late-introduction failures via architectural decomposition, not prompt tuning
- Build authoritative annotation layers one narrowly defined task at a time, then assemble deterministically
- Preserve immutable source text — LLM annotations attach to source IDs, never rewrite original
- Human-in-the-loop review for low-confidence items — not unsupervised adversarial loop
- Reuse tts.py unchanged — most valuable asset
- Maintain backward compatibility during migration (chunks.json, voice_config.json, annotated_script.json)

---

## Constraints

- tts.py (TTSEngine, 1706 lines) MUST be reused unchanged — handles CustomVoice, Clone, VoiceDesign, LoRA, batch optimization, ROCm/NVIDIA, sub-batching, codec compilation
- LoRA training, dataset builder, preparer features are orthogonal — must not be touched
- All LLM subprocesses read config via ALEXANDRIA_CONFIG_PATH; extraction tasks temp=0.1, creative tasks temp=0.3; empty responses must be null-safe
- Config.json split bug must be unified — single source of truth
- Frontend is TypeScript+Vite, 13 modules, committed dist/ — pipeline UI integrates with existing tab structure
- Distribution channels (Docker, Pinokio, Colab) all run python app.py from app/ — no build step added

---

## Open Questions

1. **Confidence threshold tuning:** Is 0.7 the right default, or should it be configurable per walk?
2. **Walk re-execution policy:** If user edits character ledger after walks 2a-2f, should downstream walks auto-re-run or be manually triggered?
3. **Span ID versioning:** If user re-uploads same EPUB, should span IDs be preserved (requires diffing) or is full reset acceptable?
4. **Voice reference text length:** 8-30 words — should this be configurable per character?
5. **Audio QA automation:** Should optional STT-based sanity checks (clipping, silence, volume) be added as pre-filter before human review?
6. **Migration deprecation timeline:** How long should old pipeline coexist? 1 release? 3 months?

---

## Design Rationale

1. **Walk decomposition fixes misattribution** by separating concerns: scene segmentation (2a) provides structural context, character discovery (2b) builds roster before attribution, alias resolution (2c) unifies name variants before quotation attribution (2d). Each walk is narrowly scoped — the LLM is not asked to "turn this book into an audiobook" in one pass.

2. **Late-introduction failure** is fixed by the canonical character ledger (step 4). When a character is first mentioned by name in chapter 10, alias resolution (2c) has already built a roster from chapters 1-9. The attribution walk (2d) receives the full roster + alias map, so it can link "the stranger" in chapter 3 to "Mr. Darcy" introduced in chapter 10.

3. **Confidence scoring** surfaces uncertainty rather than hiding it. Low-confidence attributions are shown to the user with neighbor context. The user is the ground truth for disputed items.

4. **Qwen3-TTS constraint** (bundled identity+emotion) is worked around by human audition at step 8. The voice casting decision is made once per character, with awareness that weaknesses propagate.

---

## Implementation Details

Implementation notes (confidence thresholds, temperature policy, evidence storage, migration coexistence, walk parallelism, frontend integration) in [implementation-notes.md](../parts/epub-audiobook-pipeline-rewrite/implementation-notes.md).

Edge cases and risks (walk 2d accuracy, ledger merge conflicts, voice reference selection, audio QA regeneration, migration risks) in [edge-cases.md](../parts/epub-audiobook-pipeline-rewrite/edge-cases.md).

---
