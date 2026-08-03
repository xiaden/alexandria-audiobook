# EPUB-to-Audiobook Pipeline Rewrite v2 — Two-Graph Database Model — Design Document

**Status:** Draft  
**Author:** rnd-dd-author  
**Created:** 2026-08-03  

---

## Scope

Complete rewrite of the EPUB-to-audiobook pipeline from file-based monolithic LLM pass to SQLite-backed two-graph model (structural spine tree + character identity graph). Replaces generate_script.py, review_script.py, generate_personas.py. Rewires app.py pipeline endpoints and 4 frontend tabs (Script, Voices, Editor, Result). Reuses tts.py (1706 lines) unchanged, project.py merge/M4B/Audacity unchanged, LoRA/dataset_builder/preparer untouched. Introduces 6-walk serial pipeline with per-walk local/global re-attribution, deterministic operation executor, confidence-filtered human review, series-capable character identity with voice locking, and derived legacy export for backward compatibility.

---

## Problem Statement

The current EPUB-to-audiobook pipeline suffers from high speech misattribution, mislabeling, and poor text segmentation. Root cause: generate_script.py::process_chunk sends 3000-char chunks to the LLM with only a character roster from previous speaker labels + last 3 entries for continuity. A single model is asked to "turn this book into an audiobook" in one pass — no scene boundaries, no character discovery before attribution, no alias resolution. Minor characters are frequently missed entirely. A character first introduced by name much later in the story is treated as a different character from their earlier unnamed mentions. This persists even with high-strength LLMs and improved prompting — proving it's a structural/architectural defect, not a prompt-tuning problem.

The file-based pipeline also suffers from: (1) dual-write bridge atomicity gap (pipeline_state/script.json + annotated_script.json can never be atomically consistent), (2) TOCTOU on span operations (asyncio.Lock is insufficient for concurrent reads/writes), (3) re-attribution requires full re-walk (no targeted scope), (4) no series support (characters cannot be reused across books without contaminating a single book's source).

This rewrite fixes all four root causes by replacing the file-based pipeline with a SQLite-backed two-graph model where the database is the single source of truth, ordering is code-owned (never in LLM hands), and legacy annotated_script.json becomes a derived export from one consistent DB snapshot.

---

## Architecture

## Two-Graph Model

Two SEPARATE structures joined by membership edges, stored in ONE SQLite-WAL database as two clearly-bounded schema groups:

### Graph 1: Structural Spine (TREE)
series → book → chapter → scene → paragraph → span

Strict containment, single-parent, one-to-many, ordered. Immutable per-book text structure. "Never contaminate a single book's source" enforced physically by this boundary.

**Edge tables for ordering:** scene_paragraph(scene_id FK, paragraph_id FK UNIQUE, position INT, PRIMARY KEY(scene_id, position)). UNIQUE(child_id) enforces strict-tree single-parentage. Dense integer position (small sibling counts, local reindex on split/merge) — NO lexorank. Child tables carry NO position column; ordering is parent-owned via edge rows.

### Graph 2: Character Identity (many-to-many GRAPH)
Characters as nodes (globally-stable IDs, aliases, voice persona, relationships) connected to each other AND to the spine via membership edges at series/book/scene/span level. Bipartite membership graph modeled with join tables: character_series, character_book, character_scene, character_span. Each junction carries source (walk|human|derived) + confidence (0.0-1.0) + optional human_override flag. Membership is order-free (a set) — no position columns.

**Series payoff:** Series is a COLLECTION + shared character store, NOT a containment node. book.series_id is nullable (standalone books exist). Globally-stable character IDs in INITIAL schema — enables cross-book character reuse without contaminating a single book's source. Voice identity locks at first casting for series continuity.

### Span Model: UUID Storage / Presentation Decoupling
Spans stored as UUIDs (immutable identity). Presented as derived sequential numbers 1..N, computed per-render via SQL VIEW with ROW_NUMBER() OVER (PARTITION BY parent_id ORDER BY position). "Immutable" = TEXT content never changes (LLM never rewrites source). Segmentation (span boundaries) is a MUTABLE overlay. Scene is a CONTAINER node (like Chapter), NEVER a leaf span. Span granularity: span = finest informational unit (sentence OR quotation within a sentence). Paragraph references any mix.

### Ordering Is Code-Owned, NEVER in LLM Hands
LLM writes ANNOTATIONS and MEMBERSHIP PROPOSALS only. NO write path to spine ordering tables. Ordering mutated EXCLUSIVELY by deterministic operation executor: execute_split / execute_merge / execute_move / execute_delete in storage layer. LLM/human emit operation INTENT (e.g. SPLIT(span 7, at word boundary)) against presentation indices; executor performs list surgery + local reindex. LLM operates on presentation indices, never on stored order or UUIDs. Scene-boundary changes (LLM-proposed via walk 2a) are ANNOTATIONS passing through confidence filter + human review; only APPROVED changes become structural re-parent operations executed by executor.

**Core philosophy:** "The LLM contributes annotations. Code performs assembly."

### DB Dissolves Prior Problems
- **Dual-write bridge atomicity gap** → DISAPPEARS. DB is single source of truth; legacy annotated_script.json becomes a derived EXPORT from one consistent DB snapshot. Atomicity guaranteed by transaction.
- **TOCTOU on span operations** → solved by transactions + row locks (SQLite WAL mode), not asyncio.Lock.
- **Re-attribution** → becomes queryable ("which scenes' memberships changed after this boundary move?" = SQL query). Per-walk local/global re-attribution scope column on annotations.
- **Invariants enforced by schema:** UNIQUE(parent, position) for ordering, single-parent FK for tree, source/confidence/override on memberships.

### Storage Layer Interface
SQLite WAL mode as target (single local user, Docker, ./data/, LLM is bottleneck not DB). Storage layer is an interface (swappable) so PostgreSQL migration possible later. Python ABC defines the contract; SQLite implementation is the initial backend.

---

## Design Goals

- Fix misattribution and late-introduction failures via architectural decomposition (6 single-purpose walks), not prompt tuning
- Build authoritative annotation layers one narrowly defined task at a time, then assemble deterministically (code, not LLM)
- Preserve immutable source text — LLM annotations attach to span UUIDs, never rewrite original
- UUID identity with presentation indices — structural edits are free in identity space, annotations survive re-segmentation via content-overlap matching
- Two-graph separation enables series support: same characters reused across books without contaminating a single book's source
- Ordering is code-owned — LLM emits operation intent, deterministic executor performs list surgery
- Human-in-the-loop review for low-confidence items (threshold 0.7) — not unsupervised adversarial loop
- Reuse tts.py unchanged — most valuable asset (1706 lines, handles CustomVoice, Clone, VoiceDesign, LoRA, batch optimization, ROCm/NVIDIA, sub-batching, codec compilation)
- DB is single source of truth; legacy annotated_script.json becomes derived export for backward compatibility
- Spec-first testing: contracts/behavior specs clear enough for tests; no phased rollouts bypassing spec tests

---

## Constraints

- **tts.py::TTSEngine (1706 lines) MUST be reused UNCHANGED.** Input contract: generate_batch(chunks, voice_config, output_dir, batch_seed) where chunks = [{index: int, text: str, instruct: str, speaker: str}] and voice_config = {speaker_name: {type, voice, character_style, ...}}. New pipeline must produce output in this exact format.
- **LoRA training, dataset builder, preparer features are orthogonal — UNTOUCHED.** Their endpoints, workflows, data flows out of scope.
- **Docker, Pinokio, Colab all run `python app.py` from app/ directory** — no build step added. Frontend pre-built, committed dist/ at app/static/dist/.
- **Frontend is TypeScript+Vite, 13 modules, committed dist/** — Bootstrap 5.3 + Font Awesome CDN. 9 tabs: Setup, Script, Voices, Editor, Result, Designer, Preparer, Dataset, Training.
- **LLM scripts run as subprocesses** — config flows through filesystem, not memory. Per-walk config serialized to config.json.
- **Task name 'script_alias_resolution'** must be used for walk 2c (NOT 'alias_resolution' which collides with existing persona pipeline).
- **Some models reject temperature 0.1** — try/except fallback required (wrap LLM calls, retry without temperature on rejection). Justification: not about accuracy (statistically insignificant delta), but FORMAT STABILITY (8-15% JSON parse failure rates at higher temperatures).
- **Per-walk completion verification MANDATORY** — verify all expected spans annotated, all referenced UUIDs valid, output not truncated/empty. File existence alone is insufficient.
- **6 walks is minimum viable decomposition** — walk merging REJECTED by evidence. Task interference research proves combining single-purpose prompts degrades output.
- **Walk 2f (delivery context) MUST be LLM-based** — deterministic heuristics fail on subtext. Temperature 0.3, scene-relative delivery with persona carrying baseline.
- **Walk 2c (alias resolution) is GLOBAL** — must see all mentions across whole book/series to resolve late-introduction aliases. Cannot be re-run "for affected scenes only" without destroying cross-scene alias groups.
- **M5: Walks 2b and 2c have deterministic CODE pre-passes** (proper-noun/name extraction, exact normalized-string alias match) with LLM reserved for interpretive remainder (fuzzy/partial, pronouns).
- **Confidence × reconciliation degradation:** annotation confidence 0.9 → 0.72 after 0.8× multiplier, still above 0.7 auto-accept. reconciliation_warning flag surfaces ALL degraded annotations for human review.
- **Evidence rules for character ledger:** narration primary for physical traits, dialogue primary for delivery habits — do NOT infer acoustics from personality without text evidence.
- **Voice identity locked at first casting for series** — once a character is cast in book 1, book 2+ reuses the same clone reference. Only explicit user action can unlock and regenerate.

---

## Open Questions

1. **Confidence threshold tuning:** Is 0.7 the right default, or should it be configurable per walk? Current design: global default 0.7, per-walk override via task_overrides in config.json.
2. **Voice reference text length:** 8-30 words for clone reference — should this be configurable per character? Current design: global default, per-character override in voice_config.
3. **Audio QA automation:** Should optional STT-based sanity checks (clipping, silence, volume) be added as pre-filter before human review? Current design: human listening only, no STT.
4. **Migration deprecation timeline:** How long should old pipeline coexist with new? Current design: toggle in config.json, old endpoints return 410 Gone when new pipeline active. No fixed timeline — user-controlled.
5. **Cross-series characters (crossover events, shared universes):** How to handle a character appearing in multiple unrelated series? Current design: not addressed in initial schema — defer to future plan.
6. **Context window capacity for global alias resolution (walk 2c):** A 100K-word novel has thousands of character mentions. Does it fit in 128K context? Current design: M5 pre-pass filters to top N names + all pronoun/descriptor references; if still too large, two-pass LLM approach (first pass: extract names + aliases; second pass: resolve groups with full context).
7. **SQLite VIEW performance with ROW_NUMBER() on >1,000 spans:** Is renumbering truly instantaneous? Current design: index on (parent_id, position) makes it O(log n); if performance degrades, materialized view or application-level caching.
8. **Jaccard threshold 0.6 for content-overlap reconciliation on split spans:** Does it correctly handle quotation boundaries? Current design: parameterized tests on actual novel text; if edge cases found, quotation-aware reconciliation algorithm.

---

## Pipeline Stages (Full Walk DAG)

```
[1] EPUB Extraction → [2a] Scene Segmentation → [2b] Character Discovery → [2c] Alias Resolution (GLOBAL)
→ [2d] Quotation Attribution → [2e] Character Description → [2f] Delivery Context
→ [3] Canonical Character Ledger → [4] Speaker Attribution (decisions only)
→ [5] Deterministic Script Assembly (code, not LLM)
→ [6] Persona Discovery (audible-only properties after canonicalization)
→ [7] VoiceDesign → Human Audition → Clone Reference (casting decision; voice identity locked at first casting for series)
→ [8] Qwen3-TTS Rendering (parallelizable if configured; Qwen3-TTS does NOT support separable timbre/prosody conditioning — identity+emotion bundled in clone reference)
→ [9] Audio QA by Human Listening (no STT), bad lines regenerated in fresh context
→ [10] Chaptered M4B Assembly (project.py merge_m4b, line 531, UNCHANGED)
```

Walks 2a-2f are single-purpose LLM passes, run serially. Each walk writes annotations to DB with confidence scores. Confidence filter gates: ≥0.7 auto-accept, <0.7 surface to human review. Scene segmentation (2a) reviewed BEFORE dependent walks run. Attribution (2d) uses canonical ledger as primary truth; scene is context, not source of truth. Deterministic assembly (5) combines immutable text spans + scene annotations + speaker attribution + delivery annotations into ScriptLine objects. No LLM in assembly.

**Re-attribution scope is PER-WALK with local/global column, NOT uniform:** Walk 2c (alias) is global and cannot be re-run "for affected scenes only" without destroying cross-scene alias groups. Walks 2a, 2d, 2e, 2f are local (per-scene or per-span). When a user edits scene boundaries, only local walks re-run for affected scenes. When a user fixes a speaker label, it's a direct human correction, no re-walk.

---

## Walk Definitions (Detailed Per-Walk)

**Walk 2a: Scene Segmentation**
- Purpose: Identify scene boundaries within chapters
- Input: Paragraph spans from EPUB extraction
- Output: scene_boundary annotations with confidence, proposing structural re-parent operations
- Temperature: 0.1 (deterministic fact extraction)
- Re-attribution scope: LOCAL (per-scene)
- Flow: LLM annotation → confidence filter → human review → APPROVED changes become structural re-parent operations executed by deterministic executor
- M5 pre-pass: Chapter breaks, section markers, explicit "CHAPTER X" / "SCENE" headers are deterministic — no LLM needed. LLM handles implicit scene boundaries only.

**Walk 2b: Character Discovery**
- Purpose: Identify all character mentions (named, unnamed, pronouns)
- Input: All spans in book
- Output: character_mention annotations linking spans to provisional character IDs
- Temperature: 0.1
- Re-attribution scope: LOCAL (per-scene)
- M5 pre-pass: Proper-noun extraction via regex [A-Z][a-z]+ patterns, speaker tags like "X said", "X asked". Deterministic, fast (<1s per chapter). LLM handles interpretive remainder: unnamed character mentions ("the tall man"), pronoun resolution, emotional scene transitions.

**Walk 2c: Alias Resolution**
- Purpose: Resolve aliases into canonical character records (link "the old man" in ch1 to "Gandalf" in ch3)
- Task name: 'script_alias_resolution' (NOT 'alias_resolution' — collides with existing persona pipeline)
- Input: ALL character mentions from walk 2b across entire book/series
- Output: canonical character records with aliases, globally-stable character IDs
- Temperature: 0.1
- Re-attribution scope: GLOBAL (entire book/series — cannot be re-run "for affected scenes only")
- M5 pre-pass: Exact normalized-string alias match (lowercase, de-diacritic, strip whitespace). Deterministic. LLM handles fuzzy/partial matches, pronoun resolution, cross-book alias linking.
- Evidence: Late-introduction failure fixed by canonical character ledger. Alias resolution builds roster before attribution, so unnamed mentions link to later-introduced characters.

**Walk 2d: Quotation Attribution**
- Purpose: Assign speaker to each quotation (decisions only, no text modification)
- Input: Spans with quotation marks + canonical character ledger from 2c
- Output: speaker attribution annotations with confidence
- Temperature: 0.1
- Re-attribution scope: LOCAL (per-scene)
- Flow: LLM proposes speaker for each quotation → confidence filter → human review for <0.7

**Walk 2e: Character Description**
- Purpose: Build canonical character descriptions with evidence citations
- Input: All character mentions + attributions from 2b/2d
- Output: character description annotations with evidence spans, source (narration|dialogue)
- Temperature: 0.1
- Re-attribution scope: LOCAL (per-character)
- Evidence rules: Narration primary for physical traits, dialogue primary for delivery habits. Do NOT infer acoustics from personality without text evidence. Example: "has a deep voice (narration: 'his voice rumbled like distant thunder')" vs "wears a red cloak (dialogue: 'your red cloak is showing')".

**Walk 2f: Delivery Context**
- Purpose: Generate voice direction (instruct text) for each spoken line
- Input: Quotation attributions from 2d + character descriptions from 2e + scene context
- Output: delivery annotations (instruct text, 1-2 sentences, ~8-15 words)
- Temperature: 0.3 (creative walk)
- Re-attribution scope: LOCAL (per-span)
- MUST be LLM-based — deterministic heuristics fail on subtext. Scene-relative delivery with persona carrying baseline. Example: "Firm quiet authority, low and controlled."

---

## DB Schema (SQLite WAL)

**Schema Group 1: Structural Spine (TREE)**

```sql
-- Series (collection, not containment)
CREATE TABLE series (
    series_id TEXT PRIMARY KEY,  -- UUID
    title TEXT NOT NULL,
    metadata JSON,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Book (belongs to series optionally)
CREATE TABLE book (
    book_id TEXT PRIMARY KEY,  -- UUID
    series_id TEXT REFERENCES series(series_id),  -- NULLABLE for standalone
    title TEXT NOT NULL,
    epub_path TEXT NOT NULL,
    metadata JSON,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Chapter (container)
CREATE TABLE chapter (
    chapter_id TEXT PRIMARY KEY,  -- UUID
    book_id TEXT NOT NULL REFERENCES book(book_id),
    title TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Chapter ordering (edge table)
CREATE TABLE book_chapter (
    book_id TEXT NOT NULL REFERENCES book(book_id),
    chapter_id TEXT NOT NULL REFERENCES chapter(chapter_id) UNIQUE,  -- single-parent
    position INT NOT NULL,
    PRIMARY KEY (book_id, position)
);

-- Scene (container, NEVER leaf span)
CREATE TABLE scene (
    scene_id TEXT PRIMARY KEY,  -- UUID
    chapter_id TEXT NOT NULL REFERENCES chapter(chapter_id),
    title TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Scene ordering
CREATE TABLE chapter_scene (
    chapter_id TEXT NOT NULL REFERENCES chapter(chapter_id),
    scene_id TEXT NOT NULL REFERENCES scene(scene_id) UNIQUE,
    position INT NOT NULL,
    PRIMARY KEY (chapter_id, position)
);

-- Paragraph (container)
CREATE TABLE paragraph (
    paragraph_id TEXT PRIMARY KEY,  -- UUID
    scene_id TEXT NOT NULL REFERENCES scene(scene_id),
    created_at TEXT DEFAULT (datetime('now'))
);

-- Paragraph ordering
CREATE TABLE scene_paragraph (
    scene_id TEXT NOT NULL REFERENCES scene(scene_id),
    paragraph_id TEXT NOT NULL REFERENCES paragraph(paragraph_id) UNIQUE,
    position INT NOT NULL,
    PRIMARY KEY (scene_id, position)
);

-- Span (leaf node, immutable text)
CREATE TABLE span (
    span_id TEXT PRIMARY KEY,  -- UUID, immutable identity
    span_type TEXT NOT NULL CHECK (span_type IN ('sentence', 'quotation')),
    text TEXT NOT NULL,  -- immutable content
    content_hash TEXT NOT NULL,  -- for content-overlap reconciliation
    created_at TEXT DEFAULT (datetime('now'))
);

-- Span ordering (paragraph owns position)
CREATE TABLE paragraph_span (
    paragraph_id TEXT NOT NULL REFERENCES paragraph(paragraph_id),
    span_id TEXT NOT NULL REFERENCES span(span_id) UNIQUE,
    position INT NOT NULL,
    PRIMARY KEY (paragraph_id, position)
);

-- Presentation indices (SQL VIEW, never stored)
CREATE VIEW span_presentation AS
SELECT
    s.span_id,
    s.text,
    s.span_type,
    ROW_NUMBER() OVER (ORDER BY bc.position, csc.position, sp.position, ps.position) AS presentation_idx
FROM span s
JOIN paragraph_span ps ON s.span_id = ps.span_id
JOIN paragraph p ON ps.paragraph_id = p.paragraph_id
JOIN scene_paragraph sp ON p.paragraph_id = sp.paragraph_id
JOIN scene sc ON sp.scene_id = sc.scene_id
JOIN chapter_scene csc ON sc.scene_id = csc.scene_id
JOIN chapter c ON csc.chapter_id = c.chapter_id
JOIN book_chapter bc ON c.chapter_id = bc.chapter_id;
```

**Schema Group 2: Character Identity (GRAPH)**

```sql
-- Character (globally-stable ID)
CREATE TABLE character (
    character_id TEXT PRIMARY KEY,  -- UUID, globally stable
    canonical_name TEXT NOT NULL,
    aliases JSON,  -- ["the old man", "Gandalf", "Mithrandir"]
    voice_persona JSON,  -- {description, accent, speech_patterns}
    series_id TEXT REFERENCES series(series_id),  -- NULL for standalone-book characters
    first_seen_book_id TEXT REFERENCES book(book_id),
    created_at TEXT DEFAULT (datetime('now'))
);

-- Character membership edges (order-free sets)
CREATE TABLE character_series (
    character_id TEXT NOT NULL REFERENCES character(character_id),
    series_id TEXT NOT NULL REFERENCES series(series_id),
    source TEXT NOT NULL CHECK (source IN ('walk', 'human', 'derived')),
    confidence REAL NOT NULL CHECK (confidence BETWEEN 0.0 AND 1.0),
    human_override BOOLEAN DEFAULT 0,
    PRIMARY KEY (character_id, series_id)
);

CREATE TABLE character_book (
    character_id TEXT NOT NULL REFERENCES character(character_id),
    book_id TEXT NOT NULL REFERENCES book(book_id),
    source TEXT NOT NULL CHECK (source IN ('walk', 'human', 'derived')),
    confidence REAL NOT NULL CHECK (confidence BETWEEN 0.0 AND 1.0),
    human_override BOOLEAN DEFAULT 0,
    PRIMARY KEY (character_id, book_id)
);

CREATE TABLE character_scene (
    character_id TEXT NOT NULL REFERENCES character(character_id),
    scene_id TEXT NOT NULL REFERENCES scene(scene_id),
    source TEXT NOT NULL CHECK (source IN ('walk', 'human', 'derived')),
    confidence REAL NOT NULL CHECK (confidence BETWEEN 0.0 AND 1.0),
    human_override BOOLEAN DEFAULT 0,
    PRIMARY KEY (character_id, scene_id)
);

CREATE TABLE character_span (
    character_id TEXT NOT NULL REFERENCES character(character_id),
    span_id TEXT NOT NULL REFERENCES span(span_id),
    source TEXT NOT NULL CHECK (source IN ('walk', 'human', 'derived')),
    confidence REAL NOT NULL CHECK (confidence BETWEEN 0.0 AND 1.0),
    human_override BOOLEAN DEFAULT 0,
    evidence_text TEXT,  -- citation: "narration says 'his voice rumbled'"
    walk_name TEXT,  -- '2a', '2b', '2c', '2d', '2e', '2f'
    reattribution_scope TEXT CHECK (reattribution_scope IN ('local', 'global')),
    reconciliation_warning BOOLEAN DEFAULT 0,  -- flag when ×0.8 multiplier crosses 0.7 threshold
    PRIMARY KEY (character_id, span_id)
);

-- Voice casting ledger (series-level lock)
CREATE TABLE voice_casting (
    character_id TEXT NOT NULL REFERENCES character(character_id),
    series_id TEXT REFERENCES series(series_id),  -- NULL for standalone
    book_id TEXT NOT NULL REFERENCES book(book_id),
    voice_config JSON NOT NULL,  -- {type, voice, ref_audio, ref_text, ...}
    cast_at TEXT DEFAULT (datetime('now')),
    locked BOOLEAN DEFAULT 1,  -- immutable after first casting
    PRIMARY KEY (character_id, book_id)
);

-- Annotations (walk outputs, confidence-scored)
CREATE TABLE annotation (
    annotation_id TEXT PRIMARY KEY,  -- UUID
    span_id TEXT REFERENCES span(span_id),
    scene_id TEXT REFERENCES scene(scene_id),
    annotation_type TEXT NOT NULL,  -- 'scene_boundary', 'speaker_attribution', 'delivery_context', 'character_description'
    walk_name TEXT NOT NULL,  -- '2a', '2b', '2c', '2d', '2e', '2f'
    confidence REAL NOT NULL CHECK (confidence BETWEEN 0.0 AND 1.0),
    value JSON NOT NULL,  -- walk-specific payload
    evidence_text TEXT,
    reconciliation_warning BOOLEAN DEFAULT 0,
    reviewed BOOLEAN DEFAULT 0,
    review_decision TEXT CHECK (review_decision IN ('accepted', 'rejected', 'edited', NULL)),
    created_at TEXT DEFAULT (datetime('now'))
);

-- Indexes for common queries
CREATE INDEX idx_span_presentation ON paragraph_span(paragraph_id, position);
CREATE INDEX idx_character_span ON character_span(span_id);
CREATE INDEX idx_annotation_span ON annotation(span_id);
CREATE INDEX idx_annotation_walk ON annotation(walk_name, confidence);
```

**Derived Export (legacy annotated_script.json):**
```python
def export_annotated_script(book_id: str) -> list[dict]:
    """Deterministic transformation from DB to legacy format for tts.py consumption."""
    spans = db.query("""
        SELECT sp.span_id, sp.text, cs.character_id, c.canonical_name, a.value
        FROM span_presentation sp
        LEFT JOIN character_span cs ON sp.span_id = cs.span_id
        LEFT JOIN character c ON cs.character_id = c.character_id
        LEFT JOIN annotation a ON sp.span_id = a.span_id AND a.annotation_type = 'delivery_context'
        ORDER BY sp.presentation_idx
    """)
    return [
        {"speaker": row.canonical_name or "NARRATOR", "text": row.text, "instruct": row.value.get("instruct", "Neutral narration.")}
        for row in spans
    ]
```

---

## Codebase Integration (Reuse Map)

**REUSED UNCHANGED:**
- `tts.py::TTSEngine` (1706 lines) — CustomVoice, Clone, VoiceDesign, LoRA, batch optimization, ROCm/NVIDIA, sub-batching, codec compilation. Input contract: generate_batch(chunks, voice_config, output_dir, batch_seed) where chunks = [{index, text, instruct, speaker}].
- `project.py::ProjectManager` — merge_audio() (line 431), merge_m4b() (line 531), export_audacity() (line 453). Reads annotated_script.json (now derived export from DB).
- LoRA training endpoints (`/api/lora/*`), dataset builder (`/api/dataset_builder/*`), preparer (`/api/preparer/*`) — all orthogonal, untouched.
- Voice Designer (`/api/voice_design/*`), Clone Voices (`/api/clone_voices/*`) — independent of pipeline.

**REPLACED:**
- `generate_script.py` — monolithic LLM pass replaced by 6-walk pipeline
- `review_script.py` — batch review replaced by confidence-filtered human review
- `generate_personas.py` — persona generation replaced by canonical character ledger + voice casting

**REWIRED:**
- `app.py` pipeline endpoints:
  - OLD: `POST /api/generate_script`, `POST /api/review_script`, `POST /api/review_script_contextual`, `POST /api/generate_personas`
  - NEW: `POST /api/pipeline/extract`, `POST /api/pipeline/walk/{walk_name}`, `GET /api/pipeline/annotations`, `POST /api/pipeline/annotations/{id}/review`, `POST /api/pipeline/assemble`, `POST /api/pipeline/persona_discovery`, `GET /api/pipeline/characters`, `POST /api/pipeline/span_op` (SPLIT/MERGE/MOVE/DELETE)
  - Old endpoints return 410 Gone when new pipeline active (toggle in config.json)
- Editor/Result endpoints adapt to read from DB-backed chunk data instead of chunks.json

**FRONTEND TABS:**
- **Unchanged (5):** Setup, Designer, Preparer, Dataset, Training
- **Rewired (4):** Script (pipeline controls for 6-walk DAG), Voices (character graph display), Editor (DB-backed span editing with presentation indices), Result (DB-backed merge/M4B)

**CONFIG UNIFICATION:**
- `find_config_path()` (utils.py:68-72) is the single source of truth. All code paths call it.
- Docker: `ALEXANDRIA_CONFIG_PATH=/alexandria/config/config.json` (env var takes priority)
- Pinokio/Colab: falls back to `app/config.json`
- Per-walk task_overrides: extend `resolve_task_llm()` to return `{model_name, reasoning_effort, temperature}`. Add 6 new walk names to `LLMTaskOverrides` (scene_segmentation, character_discovery, script_alias_resolution, quotation_attribution, character_description, delivery_context).

---

## Migration Strategy

**Phase 1: DB Schema + Storage Interface (foundation)**
- Create SQLite WAL database at `./data/pipeline.db`
- Implement StorageInterface ABC with SQLite backend
- Define all 14 tables + indexes + VIEW
- Migration utility: import existing `annotated_script.json` → DB (infer paragraph/scene boundaries from flat array via deterministic heuristics)

**Phase 2: Operation Executor**
- Implement execute_split / execute_merge / execute_move / execute_delete
- Presentation index ↔ UUID translation
- Local reindex on dense integer positions
- Transaction + row locking for TOCTOU prevention

**Phase 3: Walk Pipeline + 6 Walks**
- Walk orchestration framework (serial execution, per-walk verification)
- Implement walks 2a-2f with M5 pre-passes for 2b/2c
- Confidence filtering + human review integration
- Per-walk local/global re-attribution column

**Phase 4: Character Ledger + Voice Casting**
- Canonical ledger assembly from walk outputs
- Evidence rules (narration/dialogue primary)
- VoiceDesign → audition → clone flow
- Series voice locking

**Phase 5: Script Assembly + TTS Integration**
- Deterministic assembly (code, not LLM)
- Derived export: annotated_script.json from DB snapshot
- TTS rendering integration (reuses TTSEngine unchanged)

**Phase 6: Frontend Rewrite**
- Script/Voices/Editor/Result tabs rewired to new DB-backed endpoints
- Human review UI for confidence-filtered annotations
- Old-endpoint 410-gate + toggle

**Phase 7: Config Unification + Deprecation**
- find_config_path() consolidation
- Per-walk task_overrides in config.json
- Old pipeline coexists during transition (toggle in config)
- Old endpoints return 410 Gone when new pipeline active

**Backward Compatibility:**
- Legacy annotated_script.json remains a derived export from DB
- ProjectManager.load_chunks() reads it unchanged
- TTS engine consumes it unchanged
- No breaking changes to downstream consumers

---

## Testing Strategy (Spec-First)

**Spec-First Testing:** Contracts/behavior specs clear enough for tests can be written before implementation. Plans may include spec-test phase. No phased rollouts bypassing spec tests.

**Test Categories:**

1. **Storage Layer Unit Tests:**
   - All 14 tables: insert, update, delete, constraint violations
   - Edge table ordering: dense integer position, local reindex on split/merge/move/delete
   - Transaction atomicity: partial writes roll back
   - Presentation index VIEW: correct 1..N numbering after every operation

2. **Operation Executor Tests:**
   - 4 operations × N edge cases (same-parent move, cross-parent move, boundary positions, empty parents, cascade scenarios) ≈ 40-60 test cases
   - Presentation index ↔ UUID translation: round-trip correctness
   - Cascading reindex: verify no orphan spans, no duplicate positions
   - Content-overlap reconciliation on split: Jaccard threshold 0.6 calibration

3. **Walk Contract Tests:**
   - Each walk: input shape, output shape, confidence range, DB state after completion
   - Per-walk completion verification: all expected spans annotated, all referenced UUIDs valid, output not truncated/empty
   - M5 pre-pass tests: deterministic extraction correctness (proper-noun regex, normalized alias match)
   - Mock LLM responses: record real LLM outputs from test books, use as fixtures (not hand-crafted mocks)

4. **Integration Tests:**
   - End-to-end pipeline: EPUB → 6 walks → ledger → assembly → TTS input format
   - Contract test: verify assembly output matches tts.py's exact input contract [{index, text, instruct, speaker}]
   - Derived export test: annotated_script.json from DB matches expected legacy format
   - Migration test: annotated_script.json → DB → annotated_script.json round-trip preserves data

5. **Frontend Tests:**
   - API client: new endpoints return expected shapes
   - Editor tab: span editing with presentation indices, operation executor API calls
   - Human review UI: confidence-filtered annotation queue, approve/reject/edit actions

**Existing Test Infrastructure:**
- test_api.py has 91 HTTP-level integration tests — extend with new endpoint tests
- Mock LLM client pattern: record real responses, replay in tests
- In-memory SQLite DB per test session for isolation

**Coverage Goals:**
- Storage layer: 100% (critical infrastructure)
- Operation executor: 100% (critical infrastructure)
- Walk implementations: 80%+ (LLM quality is empirical, but pipeline logic must be tested)
- Frontend: 60%+ (API integration, not visual regression)

---
