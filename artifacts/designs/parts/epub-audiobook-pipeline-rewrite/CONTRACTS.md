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

> 2026-08-07: PATCH /api/pipeline/projects/{name} appended (rename; 409 duplicate, 404 unknown) per DD design decision — rename PATCH.

### Behavioral contracts

- Rows = truth; `manifest.json` = derived cache rebuilt at startup reconciliation. Reconciliation is STARTUP-ONLY (single-process ⇒ race-free).
- `transaction()` owner-thread guard: writes from non-owner thread while txn open raise `ConcurrentTransactionError` → API 503 + Retry-After; walk-side retry (50–100 ms backoff ×3) on the idempotent write phase.
- SQLite: `isolation_level=None` explicit, BEGIN IMMEDIATE, explicit COMMIT/ROLLBACK, `PRAGMA busy_timeout=5000`, INTEGER unix ms.
- Cancel: single `is_cancel_requested(run_id)` dispatcher (DB row + stop-file + event). Batch renders: job-level cancel only; individual renders: per-chunk cancel.
- Review thresholds: ≥0.7 accept / <0.5 reject / 0.5–0.7 review (unchanged); no ×0.8 multiplier.
- GC: retention ≥7 days post-completion (env-tunable), hourly sweep, never on hot request path; eligibility union includes `project_snapshot` artifact refs; rows tombstoned (`evicted`/`expired`) in the same sweep as file deletion.
- Frontend: committed dist/ + CI `git diff --exit-code app/static/dist/`; starlette>=0.49.1 CI pin (Range DoS GHSA-7f5h-v6xp-fcq8).
- New endpoints must land in the correct `api_*` module and be registered here (this section) — future DD updates append, never rewrite.
