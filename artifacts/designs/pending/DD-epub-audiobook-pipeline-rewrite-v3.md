# Design: EPUB-to-Audiobook Pipeline Rewrite (v3)

**Status:** Draft  
**Supersedes:** DD-epub-audiobook-pipeline-rewrite-v2 (Draft)  
**Author:** rnd-dd-author  
**Date:** 2026-08-03

## Problem Statement

The current pipeline sends 3000-char chunks to an LLM with only positional context and a character roster built from previous entries' speaker labels. This produces cascading misattributions: early mislabels propagate, late-introduced characters are assigned to earlier speakers, scene boundaries are invisible, and there is no way to re-attribute a character without re-running the entire script. The dual-write bridge between `annotated_script.json` and the LLM's output creates TOCTOU hazards and makes re-attribution a full re-extraction.

This design replaces the chunk-based pipeline with a two-graph SQLite-WAL model: a structural spine (TREE) that preserves chapter/paragraph/sentence hierarchy, and a character identity graph (GRAPH) that decouples character discovery from speaker attribution. Nine serial LLM walks replace the single monolithic call, including a GLOBAL alias-resolution walk that resolves cross-chapter aliases (the late-introduction fix) and a delivery walk that produces the `instruct` field for TTS. Legacy `annotated_script.json` becomes a derived export, not a source of truth.

## Architecture

### Two-Graph Model

One SQLite database with WAL journaling at `./data/pipeline.db`. Storage layer is swappable (SQLite today, Postgres/other tomorrow) via adapter pattern.

**Graph 1 — Structural Spine (TREE):**
- Entities: `series`, `book`, `chapter`, `scene`, `paragraph`, `span`
- Edge tables: `book_chapter`, `chapter_scene`, `scene_paragraph`, `paragraph_span`
- Each edge table: `UNIQUE(child_id)` + `UNIQUE(parent_id, position)` with dense integer position (no lexorank)
- Book identity: `(series_id, book_number)` — character graph keyed to identity, not version
- Book version: `version INTEGER` — collision on re-onboarding = `version++`, fresh spine+spans, memberships+voice carry over
- No timestamps in schema (ordering is code-owned, `book.position` for series ordering)

**Graph 2 — Character Identity (GRAPH):**
- `character` table: uuid, name, aliases[], voice_assignment (FK to voice_config row)
- Junction tables: `character_series`, `character_book`, `character_scene`, `character_span`
- Each junction: `source CHECK(walk|human|derived)`, `confidence REAL 0-1`, `human_override BOOLEAN`
- `character_span` has `relation_type CHECK(speaker|mentioned|present)` — distinguishes speaking role from mere presence
- `character_scene` has `relation_type CHECK(present|speaker)` — PRESENT = may have voice role, SPEAKERS = actually speaks in scene
- Voice assignment is NOT locked: `character.voice_assignment` references `voice_config` row, re-cast = re-audition + reassign (no two-tier override, no `locked` flag)

**Presentation:**
- `span_presentation` VIEW: `SELECT span.*, ROW_NUMBER() OVER (ORDER BY book.position, chapter.position, paragraph.position, span.position) AS global_index`
- Nested sort is correct (verified), needs test coverage

**Span model:**
- UUID primary key, immutable text content, `span_type CHECK(sentence|quotation)`
- No `content_hash` (Jaccard reconciliation removed)
- No `reattribution_scope` (dirty-flag machinery removed)

**Annotation model:**
- `annotation_type` enum: `alias`, `scene_boundary`, `tone_shift` (NO `speaker_attribution` — speaker attribution IS `character_span` membership with `relation_type='speaker'`)

### Pipeline Stages

Thirteen-stage pipeline: Extract, nine single-purpose LLM walks executed serially, Assembly, Review, Export:

1. **Extract** — EPUB → spine (chapters, paragraphs, sentences)
2. **Walk 2a: Scene Segmentation** — paragraphs → scenes (boundary detection). Temp 0.1, LOCAL. Produces `chapter_scene` edges.
3. **Walk 2b: Character Discovery** — scenes → character roster (initial alias map built BEFORE attribution). Temp 0.1, LOCAL. Produces `character` rows.
4. **Walk 2c: Alias Resolution** — resolves aliases across the WHOLE book/series, linking late-introduced names (e.g., "the old man" in ch1 to "Gandalf" in ch3). Temp 0.1, GLOBAL, task name `script_alias_resolution`. Produces resolved character graph with cross-chapter alias links. This is the structural fix for the late-introduction failure.
5. **Walk 2d: Scene-Presence Binding** — for each scene, which characters are PRESENT (limited cast, may have voice role). Temp 0.1, LOCAL. Produces `character_scene` memberships with `relation_type='present'`.
6. **Walk 2e: Span-Level Speaker Attribution** — for each span, which characters SPEAK, are MENTIONED, or are PRESENT. Temp 0.1, LOCAL. Produces `character_span` memberships with `relation_type` (speaker|mentioned|present).
7. **Walk 2f: Character Description** — generates character descriptions. Temp 0.1, LOCAL. Produces character description text.
8. **Walk 2g: Voice Audition** — for each character with speaking role, generates voice reference text for VoiceDesign. Temp 0.3, LOCAL. Produces voice reference text.
9. **Walk 2h: Voice Assignment** — assigns voice_config rows to characters (not locked, re-castable). Temp 0.1, LOCAL. Produces `character.voice_assignment` FK.
10. **Walk 2i: Delivery** — produces the `instruct` field for each spoken span (interpretive, scene-relative). Temp 0.3, LOCAL, MUST be LLM. Produces `instruct` text consumed by TTS/export.
11. **Assembly** — deterministic assembly of `export_annotated_script` from spine + character graph, including `instruct` field from walk 2i. Export contract: `[{speaker, text, instruct}]`.
12. **Review** — confidence-filtered review (auto-accept ≥0.7, auto-reject <0.5, between surfaced to user)
13. **Export** — overwrite-in-place atomic rename (no strict 409 gate, never refuse mid-run)

**Two-membership model (layered, not replaced):** The pipeline produces two complementary membership types:
- `character_scene` (scene-presence): produced by walk 2d, binds characters to scenes with `relation_type='present'` (limited cast per scene)
- `character_span` (span-speaker): produced by walk 2e, binds characters to spans with `relation_type` (speaker|mentioned|present)

Both are produced by the walks; neither replaces the other.

**Implicit rule:** UNKNOWN → NARRATOR. If a span has no `character_span` membership with `relation_type='speaker'`, the speaker is NARRATOR. This is deterministic, encoded at assembly time, not stored as a separate annotation.

**Walk temperatures:**
- 2a (scene segmentation): 0.1
- 2b (character discovery): 0.1
- 2c (alias resolution): 0.1
- 2d (scene-presence binding): 0.1
- 2e (span attribution): 0.1
- 2f (character description): 0.1
- 2g (voice audition): 0.3
- 2h (voice assignment): 0.1
- 2i (delivery): 0.3

Temperature 0.1 justified for format stability, not accuracy. Walk 2c is GLOBAL scope to resolve cross-chapter aliases (the late-introduction fix). Walks 2g and 2i are 0.3 because they are interpretive tasks (voice audition produces reference text; delivery produces the `instruct` field for TTS).

### Per-Walk Verification

Each walk produces a verification report: row counts, confidence distribution, sample outputs. Walks are serial to prevent task interference (research confirmed 9 walks justified by interference patterns).

## Design Goals

1. **Decouple character discovery from speaker attribution** — build alias map before attribution
2. **Preserve structural hierarchy** — chapter/paragraph/sentence, not flat chunks
3. **Make re-attribution cheap** — update `character_span` membership, re-export
4. **Make re-casting cheap** — update `character.voice_assignment`, re-export
5. **Support series** — book identity = `(series_id, book_number)`, character graph keyed to identity
6. **Support versioning** — book version increments on re-onboarding, fresh spine+spans, memberships+voice carry over
7. **Eliminate dual-write** — `annotated_script.json` is derived export, not source
8. **Eliminate Jaccard reconciliation** — UUID+content model makes reconciliation unnecessary
9. **Eliminate dirty-flag machinery** — split/merge redistributes spans deterministically, character change = direct UPDATE, scene shift re-tags on the fly
10. **Eliminate voice locking** — voice assignment is a reference, not a lock

## Constraints

1. **Two-graph one SQLite-WAL DB** at `./data/pipeline.db`, swappable storage via adapter
2. **Spine TREE** with edge tables, dense int position, no lexorank, `UNIQUE(parent,position)` + `UNIQUE(child)`
3. **Span UUID immutable text**, `span_type CHECK(sentence|quotation)`, presentation via VIEW
4. **Graph2 junctions** with `source+confidence+human_override`
5. **9-walk serial pipeline** with walk names/temps/scopes as defined (2a scene segmentation, 2b character discovery, 2c alias resolution [GLOBAL], 2d scene-presence binding, 2e span attribution, 2f character description, 2g voice audition, 2h voice assignment, 2i delivery [instruct])
6. **Per-walk verification** reports
7. **Derived `export_annotated_script`** — replace `generate_script`/`review_script`/`generate_personas`
8. **Rewire `app.py`** to `/api/pipeline/*` endpoints
9. **Fix 3 BLOCKER bugs** (config split, NameError, extract_epub_text hierarchy loss)
10. **Frontend 4 tabs rewired** (Setup, Script, Voices, Editor), **5 unchanged** (Preparer, Dataset, Training, Audio, Designer)
11. **`find_config_path()` single source** — all subprocesses use `resolve_task_llm()` which calls it internally
12. **LoRA/dataset/preparer untouched**
13. **8-phase migration** (see Migration Strategy)
14. **TTS: no gratuitous rewriting** — working internals reused, integration contract open (downgrade from "UNCHANGED" to "reuse with open contract")
15. **Ordering is code-owned** — `book.position` for series ordering, nested sort for presentation index
16. **No timestamps in schema** — ordering via position columns, not created_at/updated_at

## Codebase Integration

**Reused:**
- `tts.py::TTSEngine` — working internals reused, integration contract open
- `project.py::ProjectManager` — chunk management, parallel gen, merge, M4B, Audacity export
- `utils.py::resolve_task_llm()`, `find_config_path()` — extended with new walk task names
- Subprocess pattern (`background_tasks.add_task(run_process, cmd)`)

**Replaced:**
- `generate_script.py` — root cause of misattribution
- `review_script.py` — replaced by confidence-filtered review
- `generate_personas.py` — replaced by canonical ledger + persona discovery
- `extract_epub_text()` — must preserve chapter/paragraph hierarchy. Interim: marker-based seam preservation (CHAP_MARKER/PARA_MARKER) is ACCEPTED for now; the chunker is being rewritten as part of this rewrite anyway, and the structured spine is the Phase-1 target. Sentence/span-level extraction lands with the new spine, not the interim extractor.

**Frontend:**
- 4 tabs rewired: Setup (pipeline config), Script (walk progress), Voices (audition/assignment), Editor (re-attribution/re-casting)
- 5 tabs unchanged: Preparer, Dataset Builder, Training, Audio (Result), Designer

## Migration Strategy

8-phase migration with schema versioning:

1. **Phase 1: Schema + Adapter** — SQLite WAL, two-graph schema, adapter interface
2. **Phase 2: Extract + Walk 2a** — EPUB → spine, scene segmentation
3. **Phase 3: Walk 2b + 2c** — character discovery, GLOBAL alias resolution
4. **Phase 4: Walk 2d + 2e + 2f** — scene-presence binding, span attribution, character description
5. **Phase 5: Walk 2g + 2h + 2i** — voice audition, voice assignment, delivery (instruct)
6. **Phase 6: Assembly + Export** — deterministic assembly, overwrite-in-place export, `instruct` field included
7. **Phase 7: Frontend Rewiring** — 4 tabs rewired to `/api/pipeline/*`
8. **Phase 8: Deprecation** — old pipeline deprecated, `annotated_script.json` becomes derived-only

Each phase is independently testable. Schema versioning allows migration without data loss.

## Testing Strategy

Spec-first, 5 categories:

1. **Schema tests** — edge table uniqueness, position density, junction constraints
2. **Walk tests** — per-walk input/output contracts, confidence distributions, verification reports
3. **Assembly tests** — deterministic export, UNKNOWN→NARRATOR rule, nested sort order
4. **Integration tests** — end-to-end EPUB → export, re-attribution, re-casting, re-onboarding
5. **Presentation tests** — nested sort correctness, global index stability across re-exports

## CHANGE_LOG (v2 → v3)

**Removed:** `voice_casting.locked`, two-tier voice override, `speaker_attribution` from annotation_type, `character_span.reattribution_scope`, `span.content_hash`, `reconcile_annotations` walk, Jaccard threshold, timestamps from schema, strict 409 gate on export.

**Added:** `character.voice_assignment` FK to voice_config, `character_span.relation_type CHECK(speaker|mentioned|present)`, `character_scene.relation_type CHECK(present|speaker)`, `book.version INTEGER`, UNKNOWN→NARRATOR implicit rule, presentation sort test, GLOBAL alias-resolution walk (2c, task name `script_alias_resolution`), delivery walk (2i, produces `instruct` field).

**Changed:** TTS constraint "UNCHANGED" → "no gratuitous rewriting"; book identity `(series_id, book_number)` keyed to identity not version; spine keyed to specific version; export strict 409 → overwrite-in-place atomic rename; walk DAG corrected from 6 walks to 9 walks (2a-2i) to restore GLOBAL alias resolution and delivery.

**Unchanged:** Two-graph SQLite-WAL DB, spine TREE edge tables, Graph2 junctions, per-walk verification, derived export, replaced scripts, rewired app.py, 3 BLOCKER fixes, frontend 4+5 tabs, find_config_path(), LoRA/dataset/preparer untouched, 8-phase migration (was 7, now 8 due to walk grouping).

**Walk DAG correction (this revision):** The prior v3 CHANGE_LOG incorrectly stated the 6-walk serial pipeline was 'unchanged'. The walk set is now corrected to the agreed spec: 9 walks (2a-2i) including GLOBAL alias resolution (2c) and delivery (2i). The two-membership model (scene-presence + span-speaker) is preserved as a layered requirement, not a replacement.

## Resolved Design Decisions

Previously listed as Open Questions; all five are now decided (2026-08-03):

1. **UNKNOWN → NARRATOR encoding location** — DECIDED: resolved at the **TTS integration boundary**. Any span with no owner is presented to the TTS engine as **NARRATOR's voice config**. Not a materialized VIEW column, not a separate annotation — the TTS integration substitutes NARRATOR's config for unowned spans.
2. **Re-onboarding carry-over granularity** — DECIDED: **none by default.** Re-onboarding a book is treated as a **new book** (version increments). Memberships CAN be reincluded (explicit user action), but are NOT carried over automatically. No implicit inheritance of walk-authored or human-authored memberships on re-onboard.
3. **Walk 2g/2i parallelization** — DECIDED: **no.** Voice audition (2g) and delivery (2i) remain strictly serial. Never parallelize as a future optimization.
4. **Confidence threshold tuning** — DECIDED: **global with per-walk overrides**, same pattern as every other configuration option (task override → global default → hardcoded fallback). Global default 0.7/0.5; per-walk override in config when needed.
5. **Walk re-execution policy after ledger edits** — DECIDED: **only the affected walks, in very limited scope, and ONLY when the user explicitly triggers it via a button.** Do NOT assume user edits invalidate anything. No automatic cascade; re-execution is user-initiated and scoped to affected walks.

## Open Questions

No open design questions remain — all decisions recorded above.

## Evidence Trail

Full adversarial refinement log: [ADVERSARIAL-pipeline-rewrite.md](../process/ADVERSARIAL-pipeline-rewrite.md)

Key adversarial findings: 9 walks justified by task interference; GLOBAL alias resolution (2c) is the structural fix for late-introduction failures; delivery walk (2i) produces the `instruct` field consumed by TTS/export; walk 2g (voice audition) and 2i (delivery) are interpretive tasks requiring temperature 0.3; temperature 0.1 for format stability on all other walks; UUID+content model eliminates need for content_hash reconciliation.
