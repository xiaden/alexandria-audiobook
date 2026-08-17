# Contracts: epub-audiobook-pipeline-rewrite (v3)

> **Status:** ACTIVE for v3 design (SQLite-WAL two-graph model, 9-walk serial DAG)
> **Supersedes:** All prior contracts referencing file-based `pipeline_state/` JSON storage, 6-walk DAG, content_hash, Jaccard reconciliation.

## Storage Layer

### PipelineStorage (Abstract Base)
```python
class PipelineStorage(ABC):
    def init_db(self) -> None
    def get_connection(self) -> sqlite3.Connection
    def close(self) -> None
    def execute_query(self, sql: str, params: tuple = ()) -> list[dict]
    def execute_insert(self, sql: str, params: tuple = ()) -> int  # returns lastrowid
    def execute_update(self, sql: str, params: tuple = ()) -> int  # returns rowcount
    def execute_delete(self, sql: str, params: tuple = ()) -> int  # returns rowcount
```

### SQLiteAdapter(PipelineStorage)
```python
class SQLiteAdapter(PipelineStorage):
    def __init__(self, db_path: str = "./data/pipeline.db")
    # WAL mode, foreign_keys=ON, journal_mode=WAL
```

### InMemorySQLiteAdapter(PipelineStorage)
```python
class InMemorySQLiteAdapter(PipelineStorage):
    # For testing — same schema, no disk
```

### Production storage dependency
```python
def get_storage() -> PipelineStorage
# FastAPI dependency backed by the process-level SQLiteAdapter.
# Tests override this dependency with InMemorySQLiteAdapter.
```

## Schema (Graph1 TREE)

### Tables
- `series(id TEXT PK)`
- `book(id TEXT PK, series_id TEXT FK, book_number INTEGER, version INTEGER DEFAULT 1, position INTEGER)`
- `chapter(id TEXT PK, book_id TEXT FK)`
- `scene(id TEXT PK)`
- `paragraph(id TEXT PK)`
- `span(id TEXT PK, span_type TEXT CHECK(sentence|quotation), instruct TEXT)`

### Edge Tables (parent-owned ordering)
- `book_chapter(child_id TEXT UNIQUE FK, parent_id TEXT FK, position INTEGER, UNIQUE(parent_id, position))`
- `chapter_scene(child_id TEXT UNIQUE FK, parent_id TEXT FK, position INTEGER, UNIQUE(parent_id, position))`
- `scene_paragraph(child_id TEXT UNIQUE FK, parent_id TEXT FK, position INTEGER, UNIQUE(parent_id, position))`
- `paragraph_span(child_id TEXT UNIQUE FK, parent_id TEXT FK, position INTEGER, UNIQUE(parent_id, position))`

### Presentation VIEW
```sql
CREATE VIEW span_presentation AS
SELECT span.id, span.span_type, span.instruct,
       ROW_NUMBER() OVER (
         ORDER BY book.position, chapter.position,
                  paragraph.position, span.position
       ) AS global_index
FROM span
JOIN paragraph_span ON span.id = paragraph_span.child_id
JOIN scene_paragraph ON paragraph_span.parent_id = scene_paragraph.child_id
JOIN chapter_scene ON scene_paragraph.parent_id = chapter_scene.child_id
JOIN book_chapter ON chapter_scene.parent_id = book_chapter.child_id
JOIN book ON book_chapter.parent_id = book.id;
```

## Schema (Graph2 CHARACTER)

### Tables
- `character(id TEXT PK UUID, name TEXT NOT NULL, aliases TEXT DEFAULT '[]', voice_assignment_id TEXT FK NULLABLE, description TEXT)`
- `character_metadata(character_id TEXT FK, key TEXT, value TEXT, UNIQUE(character_id, key))`
- `character_series(character_id FK, series_id FK, source CHECK(walk|human|derived), confidence REAL CHECK 0-1, human_override INTEGER DEFAULT 0)`
- `character_book(character_id FK, book_id FK, source, confidence, human_override)`
- `character_scene(character_id FK, scene_id FK, relation_type CHECK(present|speaker), source, confidence, human_override)`
- `character_span(character_id FK, span_id FK, relation_type CHECK(speaker|mentioned|present), source, confidence, human_override)`
- `voice_config(id TEXT PK, name TEXT, description TEXT, type TEXT DEFAULT 'custom', voice TEXT, character_style TEXT, seed TEXT DEFAULT '-1', ref_audio TEXT, ref_text TEXT, adapter_id TEXT, adapter_path TEXT, alias_of TEXT)`
  **Notes:** Schema extended in Plan O (Voice Workflow Parity) from 3 columns (id, name, description) to 12 columns. The 12 DB columns map 1:1 to the `VoiceCreateRequest` Pydantic model fields (app/pipeline/api_voices.py); `VoiceUpdateRequest` is the same set minus `id`, which is the PUT path parameter. `default_style` is NOT a field on either request model (removed in Plan O) — it survives only as a read-side fallback: the TTS engine falls back from `character_style` to `default_style` when reading legacy voice data (app/tts.py) and the frontend voice-config state type still carries an optional `default_style` (frontend/src/state.ts). It is never stored in the DB and is not accepted by the voice API. Supports 5 voice types: custom, clone, builtin_lora, lora, design. Migration: `scripts/migrate_voice_config_schema.py` (idempotent ALTER TABLE).

## Extraction

### extract_epub_text
```python
def extract_epub_text(epub_path: str, book_id: str, storage: PipelineStorage) -> dict
# Interim marker-based (CHAP_MARKER/PARA_MARKER)
# Returns: {series_id, book_id, chapters: [{id, paragraphs: [{id, spans: [{id, span_type, text}]}]}]}
```

### populate_initial_spine
```python
def populate_initial_spine(series_id: str, book_id: str, chapters_data: list, storage: PipelineStorage) -> None
# Creates series, book (version=1), chapters, paragraphs, spans
# DEVIATION (Plan B): NO chapter_paragraph edge table exists in schema.
# Instead creates ONE PLACEHOLDER SCENE per chapter, linking all paragraphs
# via scene_paragraph edges. Walk 2a insert_scene redistributes paragraphs
# from placeholder scenes to real scenes. Schema intact. IMPLEMENTED (Plan B)
```

### insert_scene
```python
def insert_scene(scene_id: str, chapter_id: str, paragraph_ids: list, storage: PipelineStorage) -> None
# Creates scene, chapter_scene edge, scene_paragraph edges
# Redistributes paragraphs from placeholder scenes (per Plan B deviation).
# NOTE: paragraph.text column added via ALTER TABLE in populate.py (Plan B
# deviation — not in schema.py). IMPLEMENTED (Plan B)
```

## Operation Executor

### OperationExecutor
```python
class OperationExecutor:
    def __init__(self, storage: PipelineStorage)
    def execute_split(self, book_id: str, presentation_index: int, split_point: int) -> None
    def execute_merge(self, book_id: str, presentation_index_left: int, presentation_index_right: int) -> None
    def execute_move(self, book_id: str, presentation_index_from: int, presentation_index_to: int) -> None
    def execute_delete(self, book_id: str, presentation_index: int) -> None
    # All operations use book-scoped presentation indices.
    # The book_id parameter scopes the presentation index to a specific book.
    # Without book_id, spans from ALL books share the same global index space.
    # LLM emits intent on indices, code performs assembly
    # Re-attribution is a SIDE EFFECT: split/merge redistributes character_span rows deterministically
```

## Walk Runner

### WalkRunner
```python
class WalkRunner:
    def __init__(self, storage: PipelineStorage)
    def run_walk(self, walk_name: str, book_id: str, config: dict) -> dict
    def run_all_walks(self, book_id: str, config: dict) -> dict
    # Serial execution — walks run one at a time
    # Each walk consumes prior walk's output
    # Walk status: pending/running/completed/failed
```

## Walks (9 serial walks, 2a-2i)

### Walk 2a: Scene Segmentation
```python
def execute(book_id: str, storage: PipelineStorage, config: dict) -> dict
# resolve_task_llm('scene_segmentation') → temperature=0.1, LOCAL
# Identifies scene boundaries, creates scenes between chapters and paragraphs
```

### Walk 2b: Character Discovery
```python
def execute(book_id: str, storage: PipelineStorage, config: dict) -> dict
# resolve_task_llm('character_discovery') → temperature=0.1, LOCAL
# Creates character entities, character_scene + character_span junctions
# IMPLEMENTED (Plan C, Phase 1)
```

### Walk 2c: Alias Resolution
```python
def execute(book_id: str, storage: PipelineStorage, config: dict) -> dict
# resolve_task_llm('script_alias_resolution') → temperature=0.1, GLOBAL scope
# Merges duplicate characters, consolidates aliases
# IMPLEMENTED (Plan C, Phase 2)
# DEVIATION: junction 'source' column is CHECK-constrained to walk|human|derived (not free-form walk name);
#   get_review_items walk_name filter uses source LIKE %walk_name% as a lightweight heuristic —
#   true per-walk provenance needs an annotation/metadata table in a downstream plan.
```

### Walk 2d: Scene Presence
```python
def execute(book_id: str, storage: PipelineStorage, config: dict) -> dict
# resolve_task_llm('scene_presence') → temperature=0.1, LOCAL
# Refines character_scene junctions (relation_type=present)
# IMPLEMENTED (Plan D, Phase 1)
# DEVIATION: refines walk_2b's character_scene junctions (checks existing to avoid duplicates, adds missed)
```

### Walk 2e: Span Speaker Attribution
```python
def execute(book_id: str, storage: PipelineStorage, config: dict) -> dict
# resolve_task_llm('span_attribution') → temperature=0.1, LOCAL
# Creates character_span junctions with relation_type=speaker for quotations
# IMPLEMENTED (Plan D, Phase 2)
# SCHEMA ADDITION: span.text TEXT column added (DDL in schema.py + migration in populate.py) —
#   needed to store quotation text for LLM attribution prompts.
# UNKNOWN speakers left unattributed (no junction); resolved to NARRATOR at TTS boundary.
```

### Walk 2f: Character Description
```python
def execute(book_id: str, storage: PipelineStorage, config: dict) -> dict
# resolve_task_llm('character_description') → temperature=0.1, LOCAL
# Generates character descriptions, stores in character_metadata (key='description') via UPSERT
# IMPLEMENTED (Plan D, Phase 3)
# NOTE: per plan, description lives in character_metadata not character.description (takes precedence over CONTRACTS listing)
```

### Walk 2g: Voice Audition
```python
def execute(book_id: str, storage: PipelineStorage, config: dict) -> dict
# resolve_task_llm('voice_audition') → temperature=0.3, LOCAL (interpretive)
# Generates voice profiles, stores in character_metadata (key='voice_profile')
# IMPLEMENTED (Plan E, Phase 1)
```

### Walk 2h: Voice Assignment
```python
def execute(book_id: str, storage: PipelineStorage, config: dict) -> dict
# resolve_task_llm('voice_assignment') → temperature=0.1, LOCAL
# Assigns voice_assignment_id in character table (NOT locked); NULL if no voice match (NARRATOR/manual)
# IMPLEMENTED (Plan E, Phase 2)
```

### Walk 2i: Delivery
```python
def execute(book_id: str, storage: PipelineStorage, config: dict) -> dict
# resolve_task_llm('delivery') → temperature=0.3, LOCAL (interpretive, MUST LLM)
# Generates instruct field per span, stores in span.instruct column (existed in schema)
# IMPLEMENTED (Plan E, Phase 3)
```

## Character Ledger

### CharacterLedger
```python
class CharacterLedger:
    def __init__(self, storage: PipelineStorage)
    def get_characters_for_book(self, book_id: str) -> list[dict]
    def get_characters_for_scene(self, scene_id: str) -> list[dict]
    def get_characters_for_span(self, span_id: str) -> list[dict]
    def get_review_items(self, book_id: str, walk_name: str = None) -> list[dict]
# IMPLEMENTED (Plan C, Phase 3)
# DEVIATION: get_review_items confidence filter uses ≥0.5 AND <0.7 (0.5 inclusive, 0.7 exclusive),
#   consistent with walk confidence-filter behavior (plan said '0.5-0.7' inclusively).
```

## Assembly + Export

### export_annotated_script
```python
def export_annotated_script(book_id: str, storage: PipelineStorage) -> list[dict]
# Returns [{speaker: str, text: str, instruct: str}] in presentation order
# UNKNOWN→NARRATOR: unowned span presented as NARRATOR's voice config
```

### render_audiobook
```python
def render_audiobook(book_id: str, storage: PipelineStorage, tts_engine: object, *, use_batch: bool = True, output_dir: str | None = None, batch_seed: int = -1) -> str
# Bridges pipeline output to TTSEngine
# Maps speaker to voice config, NARRATOR fallback
# Preserves TTSEngine's parallel/batch behavior when configured
# tts_engine is duck-typed (must have generate_batch and generate_voice methods)
# Returns job_id
```

**_build_voice_config behavior (Plan D, Phase 1):** Queries `voice_config` columns and returns complete voice config dicts — the character path selects all 12 columns (id, name, description, type, voice, character_style, seed, ref_audio, ref_text, adapter_id, adapter_path, alias_of); the NARRATOR path selects 10 of the 12, omitting id and name (not needed for the NARRATOR config dict). Voice type is no longer hardcoded to "custom" — it comes from the DB `type` column, enabling TTSEngine to route to clone/design/LoRA/builtin_lora/custom methods based on actual voice configuration.

**Narrator voice configurability (Plan F, Phase 2):** The NARRATOR voice is configurable via the `voice_config` table. `_build_voice_config` queries for `id='NARRATOR'` and uses the DB row if present (all 10 voice config fields). Falls back to the hardcoded `NARRATOR_VOICE` constant (type=custom, voice=Ryan) if no DB row exists. The seed script `scripts/seed_voice_catalog.py` inserts a NARRATOR row by default (configurable via `--narrator-voice` flag).

## Review Manager

### ReviewManager
```python
class ReviewManager:
    def __init__(self, storage: PipelineStorage)
    def get_review_items(self, book_id: str, walk_name: str = None) -> list[dict]
    def accept_review_item(self, item_id: str) -> None
    def reject_review_item(self, item_id: str) -> None
    def override_review_item(self, item_id: str, new_value: any) -> None
    # Confidence filter: ≥0.7 auto-accept, <0.5 auto-reject, between → user review
```

## Re-onboarding

### reonboard_book
```python
def reonboard_book(book_id: str, storage: PipelineStorage) -> int
# Increments book.version, clears walk outputs, returns new version
# Memberships NOT carried over by default
```

### get_book_version
```python
def get_book_version(book_id: str, storage: PipelineStorage) -> int
```

## API Endpoints

### Pipeline Router (/api/pipeline/*)
```
POST /api/pipeline/onboard
POST /api/pipeline/run_walk
POST /api/pipeline/run_all_walks
POST /api/pipeline/cancel_walks          ← Plan P (background walk cancellation)
GET  /api/pipeline/walk_status/{book_id}
GET  /api/pipeline/characters/{book_id}
PUT  /api/pipeline/characters/{id}/voice ← Plan E (character voice assignment)
GET  /api/pipeline/review/{book_id}
POST /api/pipeline/review/accept
POST /api/pipeline/review/reject
POST /api/pipeline/review/override
POST /api/pipeline/operation
PUT  /api/pipeline/span/{span_id}/text   ← Plan P (span text editing)
GET  /api/pipeline/export/{book_id}
POST /api/pipeline/render
GET  /api/pipeline/render_status/{job_id} ← Plan P (render status polling)
POST /api/pipeline/cancel_render         ← Plan P (render cancellation)
GET  /api/pipeline/download/{job_id}     ← Plan P (audiobook download)
POST /api/pipeline/merge                 ← Plan P (chunk merge to M4B)
POST /api/pipeline/reonboard
GET  /api/pipeline/voices                ← Plan O (voice catalog CRUD)
POST /api/pipeline/voices                ← Plan O (voice catalog CRUD)
PUT  /api/pipeline/voices/{id}           ← Plan O (voice catalog CRUD)
DELETE /api/pipeline/voices/{id}         ← Plan O (voice catalog CRUD)
POST /api/pipeline/voices/{id}/preview   ← Plan O (voice preview generation)
```

### Character Voice Assignment (Plan E)

#### PUT /api/pipeline/characters/{id}/voice

Set or clear a character's voice assignment.

**Request body:**
```json
{
  "voice_assignment_id": "voice-id"  // optional, null to clear
}
```

**Response (200):**
```json
{
  "id": "character-uuid",
  "name": "Alice",
  "aliases": "[\"Ally\",\"Aly\"]",
  "voice_assignment_id": "ryan",
  "description": "..."
}
```

**Error codes:**
- `404` — Character not found
- `400` — Invalid voice_assignment_id (voice config does not exist in voice_config table)

**Implementation:** `app/pipeline/api_characters.py`
- `CharacterVoiceUpdateRequest` Pydantic model with `voice_assignment_id: Optional[str] = None`
- Endpoint flow: (1) query character by id → 404 if not found, (2) if voice_assignment_id provided, verify voice_config row exists → 400 if invalid, (3) UPDATE character SET voice_assignment_id, (4) return updated character
- Registered in `app/pipeline/api.py` via `include_router`

**Frontend integration:** `frontend/src/tabs/voices.ts` `handleCharacterVoiceChange`
- The character-card dropdown value is the voice NAME; the handler resolves the name to the voice_config **id** via the module-level `voiceNameToId` Map (populated by the exported `registerVoiceCatalog()`, called from `loadVoices()`) before PUT
- PUTs `API.put('/api/pipeline/characters/{characterId}/voice', { voice_assignment_id: <resolved id> })`; an empty/null selection clears the assignment (sends `null`)
- An unresolvable name shows an error toast ("Voice 'X' not found in voice catalog") and does NOT PUT and does not optimistically update
- Shows success toast on success, error toast on failure
- Local assignment Map update and character-card badge update preserved for immediate UX feedback

## Config

### LLMTaskOverrides (updated for v3)
```python
class LLMTaskOverrides(BaseModel):
    scene_segmentation: TaskLLMConfig = TaskLLMConfig()
    character_discovery: TaskLLMConfig = TaskLLMConfig()
    script_alias_resolution: TaskLLMConfig = TaskLLMConfig()
    scene_presence: TaskLLMConfig = TaskLLMConfig()
    span_attribution: TaskLLMConfig = TaskLLMConfig()
    character_description: TaskLLMConfig = TaskLLMConfig()
    voice_audition: TaskLLMConfig = TaskLLMConfig()
    voice_assignment: TaskLLMConfig = TaskLLMConfig()
    delivery: TaskLLMConfig = TaskLLMConfig()
```

## New Contracts (Plans I-N)

### Walk JSON Extraction Helper (Plan K)
```python
# app/pipeline/walks/_llm_helpers.py
def extract_json_from_llm_response(response_text: str, expected_type: str = "auto") -> dict | list | None
# Parameters:
#   response_text: Raw LLM response text
#   expected_type: "auto" (try dict then list), "dict", or "list"
# Returns: Parsed JSON object, or None if parsing fails
# Logic: (1) Try json.loads, (2) Fall back to regex extraction, (3) Return None if all fail
```

### Walk Order Canonical Contract (Plan L)
```python
# app/pipeline/walks/order.py
WALK_ORDER: list[str] = [
    "walk_2a_scene_segmentation",
    "walk_2b_character_discovery",
    "walk_2c_alias_resolution",
    "walk_2d_scene_presence",
    "walk_2e_span_attribution",
    "walk_2f_character_description",
    "walk_2g_voice_audition",
    "walk_2h_voice_assignment",
    "walk_2i_delivery",
]

WALK_TASK_NAMES: dict[str, str] = {
    "walk_2a_scene_segmentation": "scene_segmentation",
    "walk_2b_character_discovery": "character_discovery",
    "walk_2c_alias_resolution": "script_alias_resolution",
    "walk_2d_scene_presence": "scene_presence",
    "walk_2e_span_attribution": "span_attribution",
    "walk_2f_character_description": "character_description",
    "walk_2g_voice_audition": "voice_audition",
    "walk_2h_voice_assignment": "voice_assignment",
    "walk_2i_delivery": "delivery",
}

WALK_DISPLAY_NAMES: dict[str, str] = {
    "walk_2a_scene_segmentation": "Scene Segmentation",
    "walk_2b_character_discovery": "Character Discovery",
    "walk_2c_alias_resolution": "Alias Resolution",
    "walk_2d_scene_presence": "Scene Presence",
    "walk_2e_span_attribution": "Span Attribution",
    "walk_2f_character_description": "Character Description",
    "walk_2g_voice_audition": "Voice Audition",
    "walk_2h_voice_assignment": "Voice Assignment",
    "walk_2i_delivery": "Delivery",
}
```

### Book-Scoping Contract (Plan J)
```python
# app/pipeline/operations.py

# Module-level SQL constant: per-book presentation order via ROW_NUMBER().
# Replaces the global span_presentation VIEW when a book_id is known.
_BOOK_SPAN_POSITION_SQL: str
# Inline query with WHERE book.id = ? parameterization.
# Computes ROW_NUMBER() OVER (ORDER BY book.position, chapter_edge.position,
# scene_edge.position, paragraph_edge.position, span_edge.position) at call time.
# SQLite does not support parameterised VIEWs, so this is evaluated per-call.

# Helper function: resolve a presentation index to (span_id, parent_id, position)
# within a specific book. Raises ValueError if index not found.
def get_book_span_position(
    conn: sqlite3.Connection, book_id: str, presentation_index: int
) -> tuple[str, str, int]

# OperationExecutor methods now require book_id as first parameter.
# All presentation index resolution goes through _get_span_position which
# delegates to get_book_span_position when book_id is provided.
class OperationExecutor:
    def execute_split(self, book_id: str, presentation_index: int, split_point: int) -> None
    def execute_merge(self, book_id: str, presentation_index_left: int, presentation_index_right: int) -> None
    def execute_move(self, book_id: str, presentation_index_from: int, presentation_index_to: int) -> None
    def execute_delete(self, book_id: str, presentation_index: int) -> None

# Backward compatibility: the global span_presentation VIEW is preserved.
# _get_span_position falls back to the VIEW when book_id is None.
# Assembly (export_annotated_script) and tests use the VIEW directly.

# Paragraph-scoped helpers (no book_id needed):
# _two_phase_reindex and _shift_positions_range operate within a single
# paragraph's position space (filtered by parent_id), so they are already
# scoped correctly and do not need book_id.
```

```python
# app/pipeline/api.py
# OperationRequest.book_id is a required str field.
# FastAPI rejects requests missing book_id with 422 Unprocessable Entity.
# All 4 operation dispatch calls pass request.book_id to executor.
class OperationRequest(BaseModel):
    operation: str  # split, merge, move, delete
    book_id: str    # REQUIRED — scopes presentation indices to this book
```

```python
# frontend/src/tabs/editor-pipeline.ts (re-exported from editor.ts)
# pipelineOperation() includes book_id: state.pipelineBookId in every
# operation request body. All 4 handlers (split/merge/move/delete) use it.
```

### API Split by Responsibility (Plan N)

The API is split into 7 responsibility modules (api_onboard, api_walks, api_operations, api_review, api_export, api_characters, api_voices), each defining its own `APIRouter(prefix="/api/pipeline", tags=["pipeline"])` and its own FastAPI dependency injection functions (`get_*()`). `app/pipeline/api.py` aggregates all 7 sub-routers into one combined router.

#### Module: `app/pipeline/api_onboard.py`
Owns the production storage singleton and onboarding endpoints.

Endpoints:
```
POST /api/pipeline/onboard    — accept EPUB, extract text, populate spine
POST /api/pipeline/reonboard  — clear walk outputs, bump version
```

Dependency injection:
```python
# Module-level singleton for production use.
_storage: PipelineStorage | None = None

def get_storage() -> PipelineStorage
# FastAPI dependency: returns the process-level SQLiteAdapter singleton.
# Tests override this via FastAPI dependency_overrides with InMemorySQLiteAdapter.
# This is the canonical owner of get_storage; all other modules import it from here.
```

#### Module: `app/pipeline/api_walks.py`
Walk execution and character ledger query endpoints.

Endpoints:
```
POST /api/pipeline/run_walk          — run a single walk for a book
POST /api/pipeline/run_all_walks     — run all 9 walks serially for a book
POST /api/pipeline/cancel_walks      — cancel a running walk cycle
GET  /api/pipeline/walk_status/{book_id} — per-walk status for a book
GET  /api/pipeline/characters/{book_id}  — character ledger for a book
```

Dependency injection:
```python
def get_walk_runner(storage: PipelineStorage = Depends(get_storage)) -> WalkRunner
# Returns the WalkRunner singleton. Imports get_storage from api_onboard.

def get_character_ledger(storage: PipelineStorage = Depends(get_storage)) -> CharacterLedger
# Returns a new CharacterLedger per request. Imports get_storage from api_onboard.
```

#### Module: `app/pipeline/api_operations.py`
Single dispatch endpoint for structural operations on the document spine.

Endpoints:
```
POST /api/pipeline/operation — dispatches by request.operation field
                               (split | merge | move | delete)
PUT  /api/pipeline/span/{span_id}/text — inline edit a span's text
```

Dependency injection:
```python
def get_operation_executor(storage: PipelineStorage = Depends(get_storage)) -> OperationExecutor
# Returns a new OperationExecutor per request. Imports get_storage from api_onboard.
```

#### Module: `app/pipeline/api_review.py`
Confidence review workflow endpoints.

Endpoints:
```
GET  /api/pipeline/review/{book_id} — get review items (confidence 0.5–0.7)
POST /api/pipeline/review/accept    — accept a review item (confidence → 1.0)
POST /api/pipeline/review/reject    — reject a review item (confidence → 0.0)
POST /api/pipeline/review/override  — override a review item (confidence → 1.0, human_override=1)
```

Dependency injection:
```python
def get_review_manager(storage: PipelineStorage = Depends(get_storage)) -> ReviewManager
# Returns a new ReviewManager per request. Imports get_storage from api_onboard.
```

#### Module: `app/pipeline/api_export.py`
Export and render endpoints.

Endpoints:
```
GET  /api/pipeline/export/{book_id} — export annotated script for a book
POST /api/pipeline/render           — render an audiobook from the pipeline's script
GET  /api/pipeline/render_status/{job_id} — poll a render job's progress
POST /api/pipeline/cancel_render    — cancel a running render job
GET  /api/pipeline/download/{job_id} — download the merged M4B (or a ZIP of raw chunks)
POST /api/pipeline/merge            — merge rendered chunks into audiobook.m4b
```

Dependency injection:
```python
def get_tts_engine() -> object | None
# Returns the TTS engine from app.engine.get_tts_engine().
# Uses a lazy import of app.engine at call time to avoid circular imports
# (app.app imports app.pipeline.api at module level).
# Tests override this via FastAPI dependency_overrides.
```

**Engine factory — `app/engine.py`** (introduced in Plan Q; `app/project.py` is deleted):
```python
def get_tts_engine() -> object | None
# Module-level _tts_engine cache. Config path: ALEXANDRIA_CONFIG_PATH env or
# app/config.json (identical resolution to the legacy engine factory).
# Returns TTSEngine(config) from app.tts on success, None on failure.

def reset_tts_engine() -> None
# Sets the module cache to None (tears down the cached engine).
```
All non-pipeline engine consumers in app/app.py (voice_design preview, lora generate_dataset/train/test/preview, dataset_builder generate_sample/generate_batch) call `app.engine.get_tts_engine()` / `reset_tts_engine()` directly; behavior unchanged.

#### Module: `app/pipeline/api_voices.py`
Voice catalog CRUD endpoints (Plan O).

Endpoints:
```
GET    /api/pipeline/voices          — list all voice configs (optional type filter)
POST   /api/pipeline/voices          — create a new voice config
PUT    /api/pipeline/voices/{id}     — partial update of an existing voice config
DELETE /api/pipeline/voices/{id}     — delete a voice config
POST   /api/pipeline/voices/{id}/preview — generate TTS audio preview
```

Request/Response shapes:
```python
# GET /api/pipeline/voices
# Query params: type (optional) — filter by voice type
# Response: list[dict] — each dict contains all 12 voice_config columns
# Example: [{"id": "ryan", "name": "Ryan", "type": "custom", ...}]

# POST /api/pipeline/voices
# Request body: VoiceCreateRequest
{
    "id": "optional-explicit-id",  # Optional, derived from name if omitted
    "name": "Voice Name",          # Required
    "description": "Description",  # Optional
    "type": "custom",              # Optional, default "custom"
                                   # Valid: custom, clone, builtin_lora, lora, design
    "voice": "BaseVoice",          # Optional
    "character_style": "cheerful", # Optional
    "seed": "42",                  # Optional, default "-1"
    "ref_audio": "/path/to/ref.wav", # Optional
    "ref_text": "Reference text",  # Optional
    "adapter_id": "adapter-1",     # Optional
    "adapter_path": "/path/to/adapter", # Optional
    "alias_of": "canonical-id"     # Optional
}
# Response: 201 Created — returns created voice config dict (all 12 columns)
# Error: 409 Conflict — if voice with same id already exists
# Error: 422 Unprocessable Entity — if type is invalid or name is missing

# PUT /api/pipeline/voices/{voice_id}
# Request body: VoiceUpdateRequest — all fields Optional
{
    "name": "New Name",            # Optional
    "description": "New desc",     # Optional
    "type": "clone",               # Optional (must be valid type if provided)
    "voice": "NewBase",            # Optional
    "character_style": "warm",     # Optional
    "seed": "99",                  # Optional
    "ref_audio": "/new/ref.wav",   # Optional
    "ref_text": "New text",        # Optional
    "adapter_id": "adapter-2",     # Optional
    "adapter_path": "/new/path",   # Optional
    "alias_of": "other-id"         # Optional
}
# Only fields explicitly present in the request body are updated.
# Setting a field to null clears it.
# Response: 200 OK — returns updated voice config dict (all 12 columns)
# Error: 404 Not Found — if voice_id does not exist
# Error: 422 Unprocessable Entity — if type is invalid

# DELETE /api/pipeline/voices/{voice_id}
# Response: 204 No Content — no response body
# Error: 404 Not Found — if voice_id does not exist

# POST /api/pipeline/voices/{voice_id}/preview
# Request body: VoicePreviewRequest
{
    "sample_text": "This is a preview of the voice."  # Required
}
# Response: 200 OK
{
    "audio_url": "/designed_voices/previews/{voice_id}.wav",
    "voice_id": "voice-id"
}
# Error: 404 Not Found — if voice_id does not exist
# Error: 503 Service Unavailable — if TTS engine is not available
# Error: 500 Internal Server Error — if TTS generation fails

# Audio Serving:
# Preview audio files are saved to designed_voices/previews/{voice_id}.wav
# and served via the existing /designed_voices static mount in app/app.py.
# The voice_id is sanitized to prevent path traversal (slashes, backslashes,
# and ".." are replaced with underscores).
```

Frontend integration:
```typescript
// Preview button appears on each voice card in frontend/src/tabs/voices.ts
// Button has data-action="preview-voice" and data-voice-id attributes
// Click handler in frontend/src/tabs/voices.ts:
// 1. Shows loading state (disabled button, spinner icon)
// 2. Calls POST /api/pipeline/voices/{voiceId}/preview with sample text
// 3. Plays returned audio_url via HTML5 Audio element: new Audio(audio_url).play()
// 4. Shows success/error toasts
// 5. Restores button state in finally block
```

Error codes summary:
- **404 Not Found**: PUT/DELETE on non-existent voice_id
- **409 Conflict**: POST with duplicate voice id
- **422 Unprocessable Entity**: Invalid voice type or missing required fields (Pydantic validation)

Dependency injection:
```python
# Uses get_storage from api_onboard (same as other pipeline modules)
# Tests override via FastAPI dependency_overrides with InMemorySQLiteAdapter
```

#### Module: `app/pipeline/api_characters.py`
Character voice-assignment endpoint (Plan O).

Endpoints:
```
PUT /api/pipeline/characters/{character_id}/voice — set or clear a character's voice assignment
```

Request/Response shapes:
```python
# PUT /api/pipeline/characters/{character_id}/voice
# Request body: CharacterVoiceUpdateRequest
{
    "voice_assignment_id": "voice-id"   # Required; null clears the assignment
}
# Response: 200 OK — the updated character row
# (id, name, aliases, voice_assignment_id, description)
# Error: 404 Not Found — if the character does not exist
# Error: 400 Bad Request — if voice_assignment_id references a non-existent voice config
```

Dependency injection:
```python
# Uses get_storage from api_onboard (same as other pipeline modules)
# Tests override via FastAPI dependency_overrides with InMemorySQLiteAdapter
```

#### Thin entry point: `app/pipeline/api.py`

`api.py` is the thin entry point that combines all sub-routers into a
single `router` export. It has NO prefix of its own — each sub-router
already declares `prefix="/api/pipeline"` and `tags=["pipeline"]`, so
including them directly preserves their routes as-is.

```python
# app/pipeline/api.py
from app.pipeline.api_onboard import router as _onboard_router
from app.pipeline.api_walks import router as _walks_router
from app.pipeline.api_operations import router as _operations_router
from app.pipeline.api_review import router as _review_router
from app.pipeline.api_export import router as _export_router
from app.pipeline.api_characters import router as _characters_router
from app.pipeline.api_voices import router as _voices_router

# Re-export dependencies for backward compatibility with tests
from app.pipeline.api_onboard import get_storage, extract_epub_text, populate_spine
from app.pipeline.api_walks import get_walk_runner, get_character_ledger
from app.pipeline.api_operations import get_operation_executor
from app.pipeline.api_review import get_review_manager
from app.pipeline.api_export import get_tts_engine

router = APIRouter()
router.include_router(_onboard_router)
router.include_router(_walks_router)
router.include_router(_operations_router)
router.include_router(_review_router)
router.include_router(_export_router)
router.include_router(_characters_router)
router.include_router(_voices_router)
```

`app/app.py` imports `router` from `api.py` unchanged:
```python
from app.pipeline.api import router as pipeline_router
```

This preserves backward compatibility — no changes needed in the app
entry point or in test imports that reference `app.pipeline.api.*`.

## Regression Tests (Plan O — Voice Workflow Parity)

All regression tests added by Plan O (Voice Workflow Parity). Coverage proves
voice type routing, manual character voice assignment, voice catalog CRUD,
preview generation, and narrator configurability end-to-end (backend API/DB →
TTS engine → frontend persistence). Plan H named some tests generically (e.g.
`test_voice_catalog_crud`, `test_voice_preview_generation`); the real class and
method names below are authoritative.

### Backend: `tests/pipeline/test_tts_integration.py`

- `TestVoiceTypeRouting::test_voice_type_routing` — seeds one voice per type
  (custom, clone, builtin_lora, lora, design), calls `render_audiobook`, and
  asserts the `voice_config` delivered to the engine carries the correct `type`
  plus type-specific fields per speaker (clone → `ref_audio`/`ref_text`;
  lora/builtin_lora → `adapter_path`; lora also `character_style`; design →
  `description`). Proves the TTSEngine dispatch contract: clone →
  `generate_clone_voice`, lora/builtin_lora → `generate_lora_voice`, design →
  `generate_design_voice`, custom → `generate_custom_voice`.
- `TestNarratorFromDatabase::test_narrator_voice_from_db_overrides_constant` —
  a `NARRATOR` row in `voice_config` wins over the hardcoded `NARRATOR_VOICE`
  constant (all DB values used, including type-specific fields).
- `TestNarratorFromDatabase::test_narrator_fallback_to_constant_when_not_in_db` —
  with no `NARRATOR` row, `_build_voice_config` falls back to the `NARRATOR_VOICE`
  constant (type `custom`, voice `Ryan`).
- `TestCloneVoiceIntegration::test_clone_voice_type_flows_to_tts_engine` —
  `render_audiobook` resolves a character's assigned voice via
  `voice_assignment_id` and passes clone type-specific fields to the engine.
- `TestCloneVoiceIntegration::test_clone_voice_individual_mode` — the assigned
  voice also flows through individual (non-batch) `generate_voice` calls.

### Backend: `tests/pipeline/test_characters.py`

- `TestUpdateCharacterVoice::test_set_voice_assignment` — `PUT
  /api/pipeline/characters/{id}/voice` persists `voice_assignment_id` in the DB
  and returns the updated character. (Plan H named this
  `test_manual_character_voice_assignment`; the persistence half lives here, the
  render half in `TestCloneVoiceIntegration` above.)
- `TestUpdateCharacterVoice::test_clear_voice_assignment` — empty assignment
  clears the DB column; `test_invalid_voice_id_returns_400` — unknown voice id →
  400; `test_seeded_voice_id_is_accepted` — a seeded voice_config row's id
  (e.g. `ryan`) is accepted and stored; `test_voice_name_is_rejected` — the
  display name (`Ryan`) is NOT a valid assignment id → 400, row unchanged;
  confirms the id contract that the frontend must resolve name→id before PUT;
  `test_returns_all_character_fields` — response contains the full
  character shape.
- `TestUpdateCharacterVoiceNotFound` — 404 for a nonexistent character, checked
  before voice validation.

### Backend: `tests/pipeline/test_voices.py`

- `TestVoiceCRUDIntegration::test_full_crud_flow` / `test_crud_with_filter` —
  full GET/POST/PUT/DELETE lifecycle with DB-state assertions after each
  operation. (Plan H named this `test_voice_catalog_crud`; the real class is
  `TestVoiceCRUDIntegration`.)
- `TestListVoicesEndpoint` — lists all voices with all 12 `voice_config`
  columns; empty table → empty list. `TestListVoicesFilterByType` — `?type=`
  filter for each of the 5 voice types; unknown type → empty list.
- `TestCreateVoiceEndpoint` — create with all fields, explicit id, and minimal
  fields; duplicate id → 409. `TestCreateVoiceInvalidType` — invalid/empty type
  or missing name → 422.
- `TestUpdateVoiceEndpoint` — partial update preserves other fields; null
  clears a field; invalid type → 422; empty body leaves the row unchanged.
  `TestUpdateVoiceNotFound` — 404 and no row created.
- `TestDeleteVoiceEndpoint` — row removed and count reduced; 204 no body.
  `TestDeleteVoiceNotFound` — 404 for nonexistent/already-deleted id.
- `TestPreviewVoiceEndpoint::test_preview_returns_audio_url` — `POST
  /api/pipeline/voices/{id}/preview` writes a wav via the TTS engine and returns
  the audio URL; `test_preview_audio_file_is_accessible` — the file is served
  via the `/designed_voices` static mount;
  `test_preview_tts_engine_none_returns_503` — no engine → 503.
  `TestPreviewVoiceNotFound` — 404 before the TTS check.

### Backend: seed & migration (`tests/pipeline/`)

- `test_seed_voice_catalog.py::TestSeedDefaultVoices` — seeding inserts the
  narrator and Ryan default voices. `TestSeedWithSamples` — sample catalog
  inserts 6 voices covering all types. `TestSeedIdempotency` — re-seeding does
  not duplicate rows. `TestCustomNarratorVoice` — a custom narrator voice is
  seeded.
- `test_voice_config_json_migration.py::TestReadVoiceConfigJson` — legacy
  `voice_config.json` parsing (valid / empty / invalid / missing / non-dict).
  `TestMigrationWithSampleData` — JSON → DB migration inserts rows and is
  idempotent. `TestMigrationEdgeCases` — empty / missing / invalid JSON handled
  without error.
- `test_schema_migration.py::TestMigrationOldSchema` — schema migration adds
  missing `voice_config` columns, preserves existing data, and applies correct
  defaults. `TestMigrationIdempotency` — migration runs twice without error and
  is a no-op on the new schema. `TestMigrationEdgeCases` — nonexistent table.
  `TestIntegrationWithInMemoryAdapter` — migration pattern verified against the
  in-memory adapter.

### Frontend: `frontend/tests/frontend/test_voices.test.ts`

- Character voice assignment persistence — `handleCharacterVoiceChange` updates
  the local assignments map, refreshes the character-card voice badge, shows
  "Unassigned" when cleared, and returns a defensive copy via
  `getCharacterVoiceAssignments`. The handler issues `PUT
  /api/pipeline/characters/{id}/voice` (`frontend/src/tabs/voices.ts`), whose DB
  persistence is proven by `TestUpdateCharacterVoice` above. `initVoices`
  attaches the change listener to `#character-ledger`; `createCharacterCard`
  renders the per-character voice dropdown with all available voices. Dropdown
  values are voice names — resolved to voice_config ids via
  `registerVoiceCatalog` before the PUT; an unresolvable name shows an error
  toast and does NOT PUT (covered by `frontend/tests/frontend/test_voices.test.ts`
  "shows an error toast and does NOT PUT when the voice name is not in the
  catalog").
- Narrator voice selector (Phase 19) — dropdown renders all available voices
  (excluding the `NARRATOR` pseudo-row) with the `NARRATOR` row's `voice`
  selected; change → `PUT /api/pipeline/voices/NARRATOR` with `{voice}`;
  success/error toasts; falls back to the default narrator voice when no
  `NARRATOR` row exists.
- Voice catalog + preview (Phase 23) — `createVoiceCard` renders a card with
  name, type badge, and preview button per voice (excluding `NARRATOR`); click
  → `POST /api/pipeline/voices/{id}/preview` with default sample text → returned
  `audio_url` plays via `new Audio(url).play()`; error path shows a toast and
  restores the button.

## Legacy API removed in Plan Q

Plan Q deleted the legacy dual-path surface. These endpoints are removed (all now return 404) and the orphan files are deleted; the **Pipeline Router** section above is the only script/voice/render API surface:

- **Deleted endpoints (29):** `GET /api/default_prompts`, `POST /api/upload`, `GET /api/annotated_script`, `POST /api/status/{task_name}`, `GET /api/voices`, `POST /api/cancel_persona`, `POST /api/save_voice_config`, `GET /api/audiobook`, `GET /api/chunks`, `POST /api/chunks/restore`, `POST /api/chunks/{index}`, `POST /api/chunks/{index}/insert`, `DELETE /api/chunks/{index}`, `POST /api/chunks/{index}/generate`, `POST /api/merge`, `POST /api/unload`, `POST /api/export_audacity`, `GET /api/export_audacity`, `POST /api/merge_m4b`, `GET /api/audiobook_m4b`, `POST /api/m4b_cover`, `DELETE /api/m4b_cover`, `POST /api/generate_batch`, `POST /api/generate_batch_fast`, `POST /api/cancel_audio`, `GET /api/scripts`, `POST /api/scripts/save`, `POST /api/scripts/load`, `DELETE /api/scripts/{name}`.
- **Orphan files deleted:** `app/project.py`, `app/default_prompts.py`, `app/review_prompts.py`, `app/persona_prompts.py`, `default_prompts.txt`, `review_prompts.txt`, `persona_prompts.txt`, `frontend/src/tabs/editor-legacy.ts`, `frontend/src/tabs/audio.ts`.

## Superseded Contracts (v2 — DO NOT USE)
- File-based `pipeline_state/` JSON storage
- 6-walk DAG (2a-2f)
- `content_hash` field on Span
- `reconcile_annotations` / token_jaccard
- `reattribution_scope`
- Old dataclasses: Span, Chapter, Character with `seq`, `parent_span_id`, `content_hash`

---

## Universal Upgrade (DD-universal-upgrade) — Schema & API Registration

> Contract-gate registration for [DD-universal-upgrade](../../pending/DD-universal-upgrade.md) (2026-08-06). Every endpoint/schema change below is part of the pipeline-only surface; no legacy endpoint is reintroduced. All new endpoints live in the 7 `api_*` modules behind `APIRouter(prefix='/api/pipeline')`.

### Schema additions (Phase 0)

- `ALTER TABLE book ADD COLUMN single_speaker INTEGER NOT NULL DEFAULT 0` — render-boundary enforcement only; `export_annotated_script` stays faithful.
- `render_job(job_id TEXT PK, book_id, mode CHECK(batch|individual), status CHECK(pending|running|completed|failed|cancelled|interrupted|expired), error, output_dir, output_artifact_path, created_ms, started_ms, finished_ms)` + `idx_render_job_book_status(book_id, status)`.
- `render_chunk(job_id FK, idx, status CHECK(pending|done|failed|evicted), wav_path, error, PK(job_id, idx))` — individual mode only; `done` only after WAV exists + fsynced; `evicted` = GC tombstone.
- `walk_run(run_id TEXT PK, book_id, walk_name, status CHECK(pending|running|completed|failed|interrupted|cancelled), cancel_requested INTEGER DEFAULT 0, heartbeat_ms, result_json, error, created_ms, finished_ms)` + `idx_walk_run_book_status(book_id, status)`.
- `walk_review_item(id TEXT PK, book_id, run_id, kind CHECK(voice_profile|voice_assignment|instruction), target_table, target_id, prior_value, status CHECK(pending|resolved|superseded|stale), created_ms)` + `idx_walk_review_item_book_status(book_id, status)` — written by walks 2g/2h/2i in the same transaction as junction writes; supersede at walk completion, per-target.
- `walk_override(book_id, walk_name, key, value_json, PK(book_id, walk_name, key))`.
- `project_snapshot(name TEXT PK, book_id, snapshot_json, created_ms)` — saved scripts; restore blocked during active runs.

### Endpoint changes

Modified (same path, new row-backed behavior):
- `GET /api/pipeline/render_status/{job_id}` — reads `render_job` rows (api_export)
- `POST /api/pipeline/cancel_render` — sets `cancelling` (api_export)
- `GET /api/pipeline/download/{job_id}` — reads rows; FileResponse-404 subclass (api_export)
- `POST /api/pipeline/cancel_walks` — persists `walk_run.cancel_requested=1` (api_walks)
- `GET /api/pipeline/review/{book_id}` — union: junction live query + `walk_review_item`; `walkitem:` prefixed item_ids (api_review)
- `POST /api/pipeline/review/accept|reject|override` — prefix dispatch (`junction:`/`walkitem:`); walk-side value-restore (api_review)
- `POST /api/config` — raw-JSON merge, validation-only AppConfig, `schema_version` stamp (app.py)

New:
- `GET /api/pipeline/export/jobs/{job_id}` · `GET /api/pipeline/export/jobs/{job_id}/chunks` (api_export)
- `GET /api/pipeline/export/chunk/{job_id}/{idx}` — bounded-range WAV serving (api_export)
- `GET /api/pipeline/export/audio/{job_id}` — whole-book playback (api_export)
- `POST /api/pipeline/export/m4b` — FFMETADATA1 3-phase export (concat → metadata → mux); MP3/Audacity derived (api_export)
- `GET /api/pipeline/walks/{book_id}/runs` (api_walks)
- `POST /api/pipeline/projects` · `GET /api/pipeline/projects` · `POST /api/pipeline/projects/load` · `DELETE /api/pipeline/projects/{name}` · `PATCH /api/pipeline/projects/{name}` (api_operations)
- `GET /api/pipeline/book/{book_id}/single_speaker` · `PUT /api/pipeline/book/{book_id}/single_speaker` (api_operations)

> 2026-08-07: PATCH /api/pipeline/projects/{name} appended (rename; 409 duplicate, 404 unknown) per DD design decision — rename PATCH.

> 2026-08-07: GET+PUT /api/pipeline/book/{book_id}/single_speaker appended (single-speaker render toggle; 404 unknown book, 422 invalid body, parameterized SQL, 0/1 normalization) per DD decision #9 — render-boundary enforcement only.

### Behavioral contracts

- Rows = truth; `manifest.json` = derived cache rebuilt at startup reconciliation. Reconciliation is STARTUP-ONLY (single-process ⇒ race-free).
- `transaction()` owner-thread guard: writes from non-owner thread while txn open raise `ConcurrentTransactionError` → API 503 + Retry-After; walk-side retry (50–100 ms backoff ×3) on the idempotent write phase.
- SQLite: `isolation_level=None` explicit, BEGIN IMMEDIATE, explicit COMMIT/ROLLBACK, `PRAGMA busy_timeout=5000`, INTEGER unix ms.
- Cancel: single `is_cancel_requested(run_id)` dispatcher (DB row + stop-file + event). Batch renders: job-level cancel only; individual renders: per-chunk cancel.
- Review thresholds: ≥0.7 accept / <0.5 reject / 0.5–0.7 review (unchanged); no ×0.8 multiplier.
- GC: retention ≥7 days post-completion (env-tunable), hourly sweep, never on hot request path; eligibility union includes `project_snapshot` artifact refs; rows tombstoned (`evicted`/`expired`) in the same sweep as file deletion.
- Frontend: committed dist/ + CI `git diff --exit-code app/static/dist/`; starlette>=0.49.1 CI pin (Range DoS GHSA-7f5h-v6xp-fcq8).
- New endpoints must land in the correct `api_*` module and be registered here (this section) — future DD updates append, never rewrite.

## Combined Walks 2b–2d Workbench (DD-combined-walks-2b-2d-workbench) — Schema & API Registration

> Contract-gate registration for [DD-combined-walks-2b-2d-workbench](../../pending/DD-combined-walks-2b-2d-workbench.md) (2026-08-12). This is a pipeline-only frontend workbench; it does not alter `app/tts.py`, legacy routes, or walk ordering.

### Schema additions

- Stable workbench decision/provenance records keyed by `book_id`, target anchor, decision type, base generation revision, payload, status, and created time; records must preserve manual decisions and support reversible alias decisions.
- The convergent character-alias representation is `character_alias_merge`, including voice-assignment consequence data; no destructive 2c merge may bypass it.
- Explicit human scene-presence absence/tombstone state, so Walk 2d cannot re-add a user decision on rerun.
- Human boundary-override records for stable chapter/scene/paragraph anchors are unconditionally in workbench scope; Walk 2a remains outside execution but every 2a rerun must consume active overrides or be rejected.
- Generation/source-run provenance and any merge-review `walk_review_item` kind required to represent Walk 2c decisions; existing `walk_review_item` kinds remain compatible.

#### Concrete workbench tables and invariants

The following names are normative (SQLite INTEGER timestamps are unix milliseconds). `book`, `scene`, `chapter`, `paragraph`, `character`, `character_scene`, `character_span`, `walk_run`, and `walk_review_item` are existing tables and are not renamed.

```sql
workbench_generation(
  generation_id TEXT PRIMARY KEY,
  book_id TEXT NOT NULL UNIQUE,
  revision INTEGER NOT NULL CHECK(revision >= 0),
  updated_ms INTEGER NOT NULL
);
workbench_decision(
  decision_id TEXT PRIMARY KEY, book_id TEXT NOT NULL REFERENCES book(id) ON DELETE CASCADE,
  target_kind TEXT NOT NULL CHECK(target_kind IN ('presence','alias_merge','review','boundary')),
  target_key TEXT NOT NULL, decision_type TEXT NOT NULL,
  base_revision INTEGER NOT NULL CHECK(base_revision >= 0), payload_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('active','undone','superseded','conflict')),
  source TEXT NOT NULL CHECK(source IN ('human','generated')), created_ms INTEGER NOT NULL,
  undone_by TEXT REFERENCES workbench_decision(decision_id), supersedes_id TEXT REFERENCES workbench_decision(decision_id)
);
workbench_provenance(
  provenance_id TEXT PRIMARY KEY, book_id TEXT NOT NULL REFERENCES book(id) ON DELETE CASCADE,
  target_kind TEXT NOT NULL, target_key TEXT NOT NULL, run_id TEXT REFERENCES walk_run(run_id),
  generation_revision INTEGER NOT NULL, source TEXT NOT NULL CHECK(source IN ('walk','human','derived')),
  created_ms INTEGER NOT NULL
);
character_scene_absence(
  book_id TEXT NOT NULL REFERENCES book(id) ON DELETE CASCADE, scene_id TEXT NOT NULL REFERENCES scene(id) ON DELETE CASCADE,
  character_id TEXT NOT NULL REFERENCES character(id) ON DELETE CASCADE, decision_id TEXT NOT NULL REFERENCES workbench_decision(decision_id),
  active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)), created_ms INTEGER NOT NULL,
  PRIMARY KEY(book_id, scene_id, character_id)
);
character_alias_merge(
  merge_id TEXT PRIMARY KEY, book_id TEXT NOT NULL REFERENCES book(id) ON DELETE CASCADE,
  canonical_id TEXT NOT NULL REFERENCES character(id), member_id TEXT NOT NULL REFERENCES character(id),
  merge_revision INTEGER NOT NULL CHECK(merge_revision >= 0),
  decision_id TEXT NOT NULL REFERENCES workbench_decision(decision_id), status TEXT NOT NULL CHECK(status IN ('active','undone')),
  prior_member_name TEXT NOT NULL, prior_member_aliases_json TEXT NOT NULL,
  prior_member_voice_assignment_id TEXT REFERENCES voice_config(id), consequence_json TEXT NOT NULL, created_ms INTEGER NOT NULL,
  UNIQUE(book_id, merge_id)
);
boundary_override(
  override_id TEXT PRIMARY KEY, book_id TEXT NOT NULL REFERENCES book(id) ON DELETE CASCADE,
  chapter_id TEXT REFERENCES chapter(id), scene_id TEXT REFERENCES scene(id), paragraph_id TEXT REFERENCES paragraph(id),
  decision_id TEXT NOT NULL REFERENCES workbench_decision(decision_id), payload_json TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)), created_ms INTEGER NOT NULL,
  CHECK(chapter_id IS NOT NULL OR scene_id IS NOT NULL OR paragraph_id IS NOT NULL)
);
```

`workbench_generation` is the sole revision allocator and has no foreign-key/reference dependency on `book`; `book.version` is never read or incremented for workbench writes. Allocation runs inside `BEGIN IMMEDIATE` using one atomic upsert: `INSERT INTO workbench_generation(generation_id,book_id,revision,updated_ms) VALUES(?,?,0,?) ON CONFLICT(book_id) DO UPDATE SET revision=revision+1,updated_ms=excluded.updated_ms RETURNING revision`; the returned integer is the new revision and commits with the associated write. The caller validates book scope separately.

Generated/manual coexistence uses separate projections, not a shared uniqueness constraint: `character_scene_generated(id TEXT PRIMARY KEY, book_id TEXT NOT NULL REFERENCES book(id) ON DELETE CASCADE, character_id TEXT NOT NULL REFERENCES character(id), scene_id TEXT NOT NULL REFERENCES scene(id), relation_type TEXT NOT NULL CHECK(relation_type IN ('present','speaker')), confidence REAL NOT NULL CHECK(confidence>=0 AND confidence<=1), generation_revision INTEGER NOT NULL, source_run_id TEXT REFERENCES walk_run(run_id), UNIQUE(book_id,character_id,scene_id,relation_type))` and `character_scene_manual(id TEXT PRIMARY KEY, book_id TEXT NOT NULL REFERENCES book(id) ON DELETE CASCADE, character_id TEXT NOT NULL REFERENCES character(id), scene_id TEXT NOT NULL REFERENCES scene(id), relation_type TEXT NOT NULL CHECK(relation_type IN ('present','speaker','absent')), decision_id TEXT NOT NULL REFERENCES workbench_decision(decision_id), UNIQUE(book_id,character_id,scene_id,relation_type))`. The read projection uses the manual row when present, otherwise generated; disagreement writes a conflict and manual remains effective. `source_run_id` and `generation_revision` are provenance, never uniqueness. Add partial unique index `ux_alias_active_member ON character_alias_merge(book_id,member_id) WHERE status='active'`; merge history identity is `(merge_id,merge_revision)`, so merge→unmerge→merge creates a new merge_id/revision and never uses `UNIQUE(book_id,member_id,status)`.

Boundary DTOs are exact: `BoundaryAnchorDTO={chapter_id?,scene_id?,paragraph_id?}` with at least one non-null reachable ID; `BoundaryOverrideWriteDTO={override_id?,anchor,payload,base_revision}`; `BoundaryPayloadDTO={operation:'split'|'merge'|'resegment',boundary_offsets:[integer,...],label?:string}` with ordered, non-negative, reachable offsets; `BoundaryOverrideDTO={override_id,book_id,anchor,payload,decision_id,active,created_ms,generation_revision}`. GET returns active DTOs; PUT creates or replaces the addressed override atomically; apply validates anchor/payload and records the effective decision. DELETE is `DELETE /api/pipeline/workbench/{book_id}/boundary-overrides/{override_id}` with `{base_revision}`: the path identifier is authoritative, ownership/revision are checked, `active` is set to 0, inverse decision/provenance is retained, and the inactive DTO plus new revision is returned. No hard delete or silent 2a bypass is permitted.

`walk_review_item` is migrated from its existing `kind CHECK(voice_profile|voice_assignment|instruction)` to `CHECK(voice_profile|voice_assignment|instruction|alias_merge)`; the migration rebuilds the table transactionally while preserving rows/indexes, and `target_id` for alias_merge is the merge ID. Existing junction IDs and `walkitem:` dispatch remain valid.

Migrations are additive, transactional, and idempotent: create tables/indexes with `IF NOT EXISTS`, add columns only after `PRAGMA table_info`, add the generated-row uniqueness index only after deduplicating identical generated rows deterministically (lowest rowid retained), and backfill one generation row per book at revision 0. Reopening a partially migrated DB reruns safely; no migration deletes human rows. `human_override` remains the compatibility flag on live junctions, while `workbench_decision` is the durable history and `walk_review_item` is the actionable queue projection.

### Endpoint changes

New, all under `/api/pipeline` and registered in the existing `api_*` modules:

- `GET /workbench/{book_id}` — normalized scenes, stable anchors, text/highlights, characters, aliases, presence, unified review state, conflicts, run summaries/results, overrides, and effective configuration.
- `GET /workbench/{book_id}/config`, `PUT /workbench/{book_id}/overrides`, `DELETE /workbench/{book_id}/overrides` — typed per-book overrides with effective-value source reporting.
- `POST /workbench/{book_id}/alias-conversions/preview` and `POST /workbench/{book_id}/alias-conversions/commit` — short-lived, book-scoped preview token; preview enumerates affected rows and consequences; commit requires matching revision and explicit confirmation.
- `PUT /workbench/{book_id}/presence` — typed present/speaker/absent decision; removal must persist the selected human absence representation.
- `POST /workbench/{book_id}/reruns` — explicit `book`- or `scenes`-scoped 2b/2c/2d rerun with `preserve_manual_decisions` defaulting true; no review action auto-runs a walk.
- `GET|PUT /workbench/{book_id}/boundary-overrides`, `DELETE /workbench/{book_id}/boundary-overrides/{override_id}`, and `POST /workbench/{book_id}/boundary-overrides/{override_id}/apply` — unconditional boundary override read/write/delete/apply surface; 2a reruns consume active overrides or return 409.
- `POST /workbench/{book_id}/decisions/{decision_id}/undo` — revision-checked reversible undo; returns `409` with `ConflictDTO` when a newer decision or assignment exists.

Existing `GET /review/{book_id}` and review action routes remain the resolution authority; workbench clients use typed controls rather than free-form JSON prompts. Action bodies are `{item_id,base_revision}` for accept/reject and `{item_id,new_value,base_revision}` for override. `decision:{uuid}` targets an active workbench decision; `junction:{table}:{character_id}:{entity_id}` targets an allow-listed live junction; `walkitem:{id}` targets a book-scoped walk_review_item. Accept/reject/override respectively resolve the target, with walk-item reject restoring `prior_value`; every action writes a workbench decision in one transaction and returns `ActionResultDTO`. Undo is `POST /workbench/{book_id}/decisions/{decision_id}/undo` with `{base_revision}`, creates an inverse decision, and returns 409 if newer state exists. Malformed or cross-book IDs are 404/422.

### Workbench DTOs and exact behavior

`WorkbenchStateDTO` has `{book_id,generation_revision,scenes,characters,aliases,presence,review_items,overrides,effective_config,conflicts,runs}`. `StableAnchorDTO` has `{book_id,scene_id,chapter_id,paragraph_id,span_id,start_offset,end_offset}` with nullable fields only where the target type makes them inapplicable. `ReviewItemDTO` preserves the existing fields and adds `{item_id,kind,target_table,target_id,status,decision_id,source_run_id,anchor,neighbors}`; existing fields are not renamed. `ConflictDTO` is `{code,current_revision,current_value,requested_value,decision_id,item_id}`. `ActionResultDTO` is `{item_id,decision_id,status,generation_revision,superseded_item_ids,conflict}`.

- `GET /workbench/{book_id}` returns the normalized DTO and never uses presentation index as identity.
- `GET /workbench/{book_id}/config` returns `{global,task_overrides,top_level_walk_override,db_overrides,effective,source,validation_errors}`; secrets and raw prompts are omitted from non-owner views.
- `PUT /workbench/{book_id}/overrides` request is `{walk_name,key,value,base_revision}`; allowed keys are `model_name`, `reasoning_effort`, `temperature`, and `prompt`, with `prompt` allowed for 2b/2c/2d. `DELETE` request is `{walk_name,key,base_revision}`.
- Preview request is `{canonical_id,member_ids,base_revision}` and response is `{preview_token,expires_ms,base_revision,affected_rows,protected_decisions,voice_assignments,review_items,downstream_invalidations,conflicts}`. Commit request is `{preview_token,base_revision,confirm_consequences}`; response is `ActionResultDTO` plus `merge_id`. Preview tokens are random, single-use, book-scoped, bind the exact member set and revision, and expire after 10 minutes.
- Presence request is `{scene_id,character_id,relation_type,decision_id,base_revision}` where `relation_type` is `present|speaker|absent`; response is `ActionResultDTO` plus `{scene_id,character_id,relation_type}`. `absent` creates/activates `character_scene_absence`; restoring presence deactivates it in the same transaction.
- Boundary reads return `list[BoundaryOverrideDTO]` for active rows only. Writes use `BoundaryOverrideWriteDTO`; DELETE uses the URL `override_id` and `{base_revision}` and returns the retained inactive `BoundaryOverrideDTO` plus the new revision. Applying validates the stable anchor and payload, writes the decision and generation revision atomically, and makes it visible to the next 2a run; deletion records an inverse decision and never removes history.
- Rerun request is `{walk_name,scope,scene_ids,preserve_manual_decisions,base_revision}` and response is `{run_id,status,scope,invalidated_walks,generation_revision}`. `scope` is exactly `book|scenes`; `scenes` requires a non-empty reachable scene list; 2c rejects `scenes` with 422 because alias resolution is book-global.

Rerun reconciliation is exact: mark only affected generated provenance/review rows stale, execute one run, upsert by stable target key, preserve `human_override=1` and active absence, and supersede only prior generated `walk_review_item` rows for touched targets after successful commit. A failed/partial run leaves prior successful rows and revision live; its generated writes are rolled back when atomic, or tagged to the failed run and excluded from reads when a walk reports partial progress. 2b invalidates 2c+2d, 2c invalidates 2d, and 2d invalidates neither upstream walk. `walk_run` follows pending→running→completed|failed|interrupted|cancelled; cancellation sets `cancel_requested`, and stale running rows are reconciled at startup. `POST /cancel_walks` and all contention paths return 503 with `Retry-After: 5` where the universal contract requires it.

Alias commit is reversible: it records both member voice assignments and all affected target keys in `consequence_json`; unmerge creates a new `workbench_decision` that changes the merge to `undone`, restores member projection and voice assignments only when no newer human assignment exists, reactivates affected review items, and returns 409 otherwise. Snapshot load/restore remains blocked during active workbench runs and uses existing `project_snapshot` schema/version validation. Frontend integration is `frontend/index.html`, `frontend/src/main.ts`, `frontend/src/api.ts`, `frontend/src/state.ts`, `frontend/src/tabs/workbench.ts`, and `frontend/tests/frontend/test_workbench.test.ts`; compiled output is committed in `app/static/dist/` and must pass the existing build/diff gate.

Decision status transitions are `active → undone|superseded|conflict`; `undone` and `superseded` are terminal, while `conflict` is terminal until a new human decision is created. `walk_review_item` transitions remain `pending → resolved|superseded|stale`; generated items are superseded only after successful run commit, and existing human-overridden junctions remain live. `human_override=1` is never cleared by reconciliation. `workbench_provenance` and `walk_run` rows are retained for failed, partial, cancelled, and interrupted runs; only the successful generation is projected by default. The existing `GET /api/pipeline/walks/{book_id}/runs`, `POST /api/pipeline/cancel_walks`, review union/action routes, and `project_snapshot` load/save contracts are consumed unchanged by the workbench.

### Behavioral contracts

- Rows remain truth and manifests remain derived. Every workbench read returns durable scene/entity IDs and stable anchors; presentation indices are display metadata only.
- Writes validate book scope and anchor reachability, use parameterized SQL and whitelisted fields, and reject stale `base_revision`/preview tokens with 409. Transaction contention follows the universal-upgrade contract: 503 plus `Retry-After: 5`.
- Alias preview and commit must agree on the affected-row set. A merge cannot silently discard a member's voice assignment, protected decision, or reversible history.
- Walk 2b/2d reruns reconcile generated rows idempotently, preserve human decisions, and never re-add explicit absence. Generated/manual disagreement becomes a visible conflict. Walk 2c merge review is persisted, not inferred from a discarded counter.
- Effective configuration reports field-specific precedence identically to the DD: `model_name`, `reasoning_effort`, and `temperature` use DB row → `llm.task_overrides[task]` → `llm` global → hardcoded fallback; `prompt` uses DB row → top-level `config.walk_override[task].prompt` → `llm.task_overrides[task].prompt` → `llm.prompt` → hardcoded fallback, with empty/non-string values falling through. Unknown keys and invalid values are rejected.
- UI must provide keyboard-equivalent actions, non-color state encoding, confirmation for destructive changes, revision-aware undo, actionable errors, and no exposure of secrets, SQL, filesystem paths, or raw prompts.
- New frontend assets update committed `app/static/dist/` and pass `npm run build && git diff --exit-code app/static/dist/`; the legacy guard remains 12/12.

## Combined Workbench 2b–2d — DELIVERED (2026-08-12, S5 registration)

> Appended confirmation that the schema/API/behavior registered above matches the shipped implementation. Route names, request DTOs, and status codes below are the exact delivered surface (verified against `app/pipeline/api_walks.py`, `api_review.py`, `api_characters.py`, `app/pipeline/workbench.py`, and `app/pipeline/schema.py`). No prior contract lines are amended.

### Delivered routes (all under `/api/pipeline`)

- `GET /workbench/{book_id}` → `WorkbenchStateDTO`: `{book_id,generation_revision,scenes,characters,aliases,presence,review_items,overrides,effective_config,conflicts,runs}`. `scenes` is the normalized chapter→scene→paragraph→span hierarchy; `characters` is `{id,name,aliases,voice_assignment_id,description}`; `effective_config[walk_name]` is the per-walk `resolve_effective_config` result.
- `GET /workbench/{book_id}/config` → `{global: null, task_overrides: {}, top_level_walk_override: {}, db_overrides, effective: {walk_name: values}, source: {walk_name: sources}, validation_errors: []}`. DELIVERED DEVIATION: `global`, `task_overrides`, and `top_level_walk_override` are returned as `null`/empty and effective/source are split per-walk; the old shape is intentionally not populated because the domain resolves precedence directly.
- `PUT /workbench/{book_id}/overrides` — request `WorkbenchOverrideWriteRequest{walk_name,key,value,base_revision}`; allowed keys are `model_name`, `reasoning_effort`, `temperature`, `prompt` (2b/2c/2d).
- `DELETE /workbench/{book_id}/overrides` — request `WorkbenchOverrideDeleteRequest{walk_name,key,base_revision}`.
- `POST /workbench/{book_id}/alias-conversions/preview` — request `AliasPreviewRequest{canonical_id,member_ids,base_revision}`.
- `POST /workbench/{book_id}/alias-conversions/commit` — request `AliasCommitRequest{preview_token,base_revision,confirm_consequences=false}`.
- `POST /workbench/{book_id}/reruns` — request `WorkbenchRerunRequest{walk_name,scope='book',scene_ids=null,preserve_manual_decisions=true,base_revision}`; 2c rejects `scenes` scope with 422; response `{run_id,status,scope,invalidated_walks,generation_revision}` with `invalidated_walks` from the invalidation DAG (2b→[2c,2d], 2c→[2d], 2d→[]).
- `PUT /workbench/{book_id}/presence` — defined in `api_characters.py`; request `CharacterPresenceRequest{scene_id,character_id,relation_type,decision_id?,base_revision}`; response is the contracted `ActionResultDTO` plus `{scene_id,character_id,relation_type}`, with `item_id` = `presence:{scene_id}:{character_id}`.
- `GET /workbench/{book_id}/boundary-overrides` → `list[BoundaryOverrideDTO]` (active rows only).
- `PUT /workbench/{book_id}/boundary-overrides` — request `WorkbenchBoundaryOverrideWriteRequest{override_id?,anchor:{chapter_id?,scene_id?,paragraph_id?},payload:{operation,boundary_offsets,label?},base_revision}`.
- `POST /workbench/{book_id}/boundary-overrides/{override_id}/apply` — applies an active override and records the effective decision.
- `DELETE /workbench/{book_id}/boundary-overrides/{override_id}` — request `WorkbenchBoundaryDeleteRequest{base_revision}`; path id is authoritative; `active` set to 0; inverse decision retained.
- `POST /workbench/{book_id}/decisions/{decision_id}/undo` — request `ReviewUndoRequest{base_revision}`; `alias_merge:merge` decisions delegate to the domain's reversible `unmerge_alias`; 409 when non-`active`.
- `POST /review/accept|reject|override` — request `ReviewActionRequest{item_id,new_value?,base_revision?}`. DELIVERED DEVIATION: `base_revision` is optional and only enforced for `decision:`/`junction:` dispatch targets; legacy bare-junction and `walkitem:` ids keep their prior behavior. `decision:{uuid}` dispatches to `_resolve_decision_action`, `junction:{table}:{character_id}:{entity_id}` to `_resolve_junction_action`, otherwise `ReviewManager.resolve_review_action`.

### Delivered request/DTO field notes

- `WorkbenchOverrideWriteRequest.value` is typed `object` (unvalidated at the Pydantic boundary; validated by `Workbench._validate_override_value`).
- `WorkbenchBoundaryAnchor` requires at least one non-null id; the route passes `model_dump(exclude_none=True)`. `WorkbenchBoundaryPayload.operation` is `split|merge|resegment`.
- Presence `decision_id` is an optional client reference; the domain records the authoritative decision. `absent` creates/activates `character_scene_absence`; `present`/`speaker` deactivates the tombstone in the same transaction.
- `Workbench` domain API (in `app/pipeline/workbench.py`): `require_book`, `get_generation`, `get_revision`, `allocate_revision` (BEGIN IMMEDIATE sole per-book revision allocator), `check_revision`, `get_stable_anchors`, `record_decision`, `record_provenance`, `list_decisions`, `get_generated_rows`, `get_manual_rows`, `set_presence`, `get_presence`, `get_conflicts`, `preview_alias_conversion`, `commit_alias_conversion`, `unmerge_alias`, `get_boundary_overrides`, `put_boundary_override`, `apply_boundary_override`, `deactivate_boundary_override`, `get_overrides`, `put_override`, `delete_override`, `resolve_effective_config`. Domain errors: `WorkbenchError` with subclasses `BookNotFoundError`, `ValidationError`, `StaleRevisionError`, `ConflictError`, `PreviewExpiredError`.
- Contention/`base_revision`/preview-token failures map to 409 (stale) and 503 + `Retry-After: 5` (transaction contention) via the universal contract; unknown/cross-book ids map to 404; validation to 422.

### Delivered tables (schema.py `_WORKBENCH_DDL`)

- `workbench_generation(generation_id PK, book_id UNIQUE, revision>=0, updated_ms)` — sole allocator, no FK to `book`.
- `workbench_decision(decision_id PK, book_id FK, target_kind CHECK(presence|alias_merge|review|boundary), target_key, decision_type, base_revision>=0, payload_json, status CHECK(active|undone|superseded|conflict), source CHECK(human|generated), created_ms, undone_by, supersedes_id)` with `idx_workbench_decision_book_status(book_id,status)`.
- `workbench_provenance(provenance_id PK, book_id FK, target_kind, target_key, run_id FK walk_run, generation_revision, source CHECK(walk|human|derived), created_ms)`.
- `character_scene_absence(book_id FK, scene_id FK, character_id FK, decision_id FK, active CHECK(0|1), created_ms, PK(book_id,scene_id,character_id))`.
- `character_alias_merge(merge_id PK, book_id FK, canonical_id FK, member_id FK, merge_revision>=0, decision_id FK, status CHECK(active|undone), prior_member_name, prior_member_aliases_json, prior_member_voice_assignment_id FK voice_config, consequence_json, created_ms, UNIQUE(book_id,merge_id))` + partial unique index `ux_alias_active_member ON character_alias_merge(book_id,member_id) WHERE status='active'`.
- `boundary_override(override_id PK, book_id FK, chapter_id?, scene_id?, paragraph_id?, decision_id FK, payload_json, active CHECK(0|1), created_ms, CHECK(at least one anchor non-null))`.
- `character_scene_generated(id PK, book_id FK, character_id FK, scene_id FK, relation_type CHECK(present|speaker), confidence 0-1, generation_revision, source_run_id FK walk_run, UNIQUE(book_id,character_id,scene_id,relation_type))` + `idx_character_scene_generated_book`.
- `character_scene_manual(id PK, book_id FK, character_id FK, scene_id FK, relation_type CHECK(present|speaker|absent), decision_id FK, UNIQUE(book_id,character_id,scene_id,relation_type))` + `idx_character_scene_manual_book`.
- Migration `_migrate_walk_review_item_kind` rebuilds `walk_review_item` transactionally to extend `kind` CHECK to `('voice_profile','voice_assignment','instruction','alias_merge')`; `_backfill_workbench_generation` inserts one revision-0 row per book. Both idempotent; no human rows deleted.

### Walk behavior (delivered)

- Walk 2b (`walk_2b_character_discovery.py`) and 2d (`walk_2d_scene_presence.py`) both guard generated presence with `_active_absence(...)` (never re-add active human absence), record `workbench_provenance`, and upsert the generated projection by stable key `(book_id, character_id, scene_id, relation_type)`.
- Walk 2c (`walk_2c_alias_resolution.py`) remains GLOBAL scope; confidence ≥0.7 auto-accept (merge applied), <0.5 auto-reject, 0.5–0.7 flagged for review. Tracked in `character_alias_merge` with `consequence_json` for reversible unmerge; review kind `alias_merge`.

### Frontend delivery (confirmed)

- `frontend/src/api.ts` adds `putWithRetryOnce` and `delWithRetryOnce` (exactly ONE retry on `retryStatus` default 503 with `Retry-After`); `postWithRetryOnce` (default 503, `retryStatus=409` for snapshot-load restore).
- `frontend/src/tabs/workbench.ts` implements the workbench navigator/highlights/ledger/aliases/presence/setup/conflicts/rerun-protection; `frontend/src/main.ts` calls `initWorkbench()`. Built output committed in `app/static/dist/`.

## Voice / Persona / Prompt Parity Contracts (DD-voice-persona-prompt-parity-browser-external-validation)

> Design-time registration for [DD-voice-persona-prompt-parity-browser-external-validation](../../pending/DD-voice-persona-prompt-parity-browser-external-validation.md) (2026-08-13). These three entries are parity-owned append-only registrations. They do not register or duplicate any 2b–2d workbench contract owned by `DD-combined-walks-2b-2d-workbench.md`, and no prior contract lines above are amended.

CONTRACT ID: PipelineVoiceCloneReferenceAPI.v1
SCHEMAS: CloneReferenceDTO; CloneReferenceUploadRequest; CloneReferenceListResponse; CloneReferenceDeleteResponse
REQUEST/RESPONSE: POST /api/pipeline/voices/{voice_id}/references (multipart audio + optional ref_text) -> 201 {reference: CloneReferenceDTO, voice: VoiceConfigDTO}; GET /api/pipeline/voices/{voice_id}/references -> 200 CloneReferenceListResponse; GET /api/pipeline/voices/{voice_id}/references/{reference_id}/preview -> inline audio; GET .../{reference_id}/download -> attachment audio; DELETE .../{reference_id} -> 204
OWNER: endpoint/module app/pipeline/api_voices.py; persistence owner pipeline voice-reference storage adapter
COMPATIBILITY: Pipeline-only, resolved voice_config IDs, existing five voice types and voice CRUD unchanged; ownership/path/content/size/duration checks and no filesystem-path exposure; no legacy manifest or route.

CONTRACT ID: PipelineCharacterPersonaAPI.v1
SCHEMAS: PersonaDTO; PersonaWriteRequest; PersonaValidationResponse; PersonaRevisionListResponse; PersonaRerunRequest; RevisionConflictDTO
REQUEST/RESPONSE: GET|PUT /api/pipeline/characters/{character_id}/persona -> PersonaDTO; GET .../persona/revisions -> PersonaRevisionListResponse; POST .../persona/validate (PersonaWriteRequest) -> validation result, side-effect free; POST .../persona/rerun (PersonaRerunRequest) -> {run_id, revision_id, scope}
OWNER: endpoint/module app/pipeline/api_characters.py and app/pipeline/api_review.py for validation/review integration; persistence owner character ledger/persona revision adapter
COMPATIBILITY: Character ledger identity remains authoritative; manual persona is revisioned and separately addressable, preserves evidence/protection, never replaces discovery or 2b–2d workbench state, and never auto-cascades.

CONTRACT ID: PipelineWalkPromptConfigRevisionAPI.v1
SCHEMAS: EffectiveWalkConfigDTO; PromptConfigWriteRequest; PromptConfigValidationResponse; PromptConfigRevisionDTO; PromptConfigRevisionListResponse; ScopedWalkRerunRequest; RevisionConflictDTO
REQUEST/RESPONSE: GET /api/pipeline/walks/{book_id}/config -> EffectiveWalkConfigDTO; POST .../config/validate (PromptConfigWriteRequest) -> PromptConfigValidationResponse; POST .../config/revisions (PromptConfigWriteRequest with base_revision) -> 201 PromptConfigRevisionDTO; POST /api/pipeline/walks/{book_id}/reruns (ScopedWalkRerunRequest) -> {run_id, revision_id, scope}
OWNER: endpoint/module existing pipeline walk/config API module (app/pipeline/api_walks.py); persistence owner walk_override/config-revision adapter
COMPATIBILITY: Fixed nine task names and `script_alias_resolution` alias resolution remain canonical; precedence is on-disk config -> llm.task_overrides -> DB walk_override (DB wins); exact allowed keys are model_name, reasoning_effort, temperature, prompt; validation is side-effect free; explicit rerun is revision/scope protected and never implicit. For 2b–2d, prompt/config writes and reruns delegate to the existing combined-walks workbench contracts and single writer; this record does not duplicate their registrations.

### Delivery status (voice / persona / prompt parity — all three contracts delivered)

Appended on closure of plans TASK-voice-persona-prompt-parity-A/B/C/D (all four archived complete). This note is a delivery-status addition appended below the parity registrations above; it does not amend or restate any prior contract line. The combined-walks workbench section (the `## Workbench`/2b–2d block immediately above this parity section) is byte-unchanged.

- PipelineVoiceCloneReferenceAPI.v1 — DELIVERED. Endpoint `app/pipeline/api_voices.py`, persistence via the pipeline voice-reference storage adapter. Clone references resolved to voice_config IDs; ownership/path/content/size/duration checks enforced; responses expose only resolved IDs and audio bytes — never `relative_path`/absolute filesystem paths; no legacy manifest or route.
- PipelineCharacterPersonaAPI.v1 — DELIVERED. Endpoint `app/pipeline/api_characters.py` (+ `app/pipeline/api_review.py` validation/review integration); persona revisions via the character ledger/persona revision adapter. Manual persona is revisioned and separately addressable; evidence/protection preserved; never replaces discovery or 2b–2d workbench state; rerun requires explicit `confirm:true` and never auto-cascades (no `voice_assignment_id` in rerun payloads).
- PipelineWalkPromptConfigRevisionAPI.v1 — DELIVERED. Endpoint `app/pipeline/api_walks.py`; persistence via the walk_override/config-revision adapter. On-disk config -> llm.task_overrides -> DB walk_override precedence (DB wins); exact allowed keys are model_name, reasoning_effort, temperature, prompt; validation side-effect free; explicit rerun is revision/scope protected and never implicit. Prompt/config writes and reruns delegate to the combined-walks workbench single writer.
- Parity endpoints accept no arbitrary engine arguments — only the existing public seams (engine factory + `tts_integration` generate contract). `app/tts.py` byte-identical throughout (git diff empty; blob 50256798bed3d1fdbb7794c80c433b8490b94751, last touched at commit bdec340f). Legacy guard 12/12 (no legacy endpoint/manifest/prompt-file module resurrected). No legacy prompt-module references exist in repo docs or code.
- Verification: backend `pytest -q` 1653 passed (1625 baseline + 17 capability discovery + 11 integration, no regressions); frontend vitest 498 passed; `tests/external/` 28 deterministic tests green. See plan annotations for exact gate output and coverage numbers.
