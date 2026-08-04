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
  **Notes:** Schema extended in Plan O (Voice Workflow Parity) to include all 11 fields from `VoiceConfigItem` model. Supports 5 voice types: custom, clone, builtin_lora, lora, design.

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
GET  /api/pipeline/walk_status/{book_id}
GET  /api/pipeline/characters/{book_id}
GET  /api/pipeline/review/{book_id}
POST /api/pipeline/review/accept
POST /api/pipeline/review/reject
POST /api/pipeline/review/override
POST /api/pipeline/operation
GET  /api/pipeline/export/{book_id}
POST /api/pipeline/render
POST /api/pipeline/reonboard
```

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

The API is split into 5 responsibility modules, each defining its own
`APIRouter(prefix="/api/pipeline", tags=["pipeline"])` and its own
FastAPI dependency injection functions (`get_*()`).

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
```

Dependency injection:
```python
def get_tts_engine() -> object | None
# Returns the TTS engine from project_manager.get_engine().
# Uses a lazy import of app.app.project_manager at call time to avoid
# circular imports (app.app imports app.pipeline.api at module level).
# Tests override this via FastAPI dependency_overrides.
```

#### Thin entry point: `app/pipeline/api.py`

`api.py` is the thin entry point that combines all 5 sub-routers into a
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
```

`app/app.py` imports `router` from `api.py` unchanged:
```python
from app.pipeline.api import router as pipeline_router
```

This preserves backward compatibility — no changes needed in the app
entry point or in test imports that reference `app.pipeline.api.*`.

## Superseded Contracts (v2 — DO NOT USE)
- File-based `pipeline_state/` JSON storage
- 6-walk DAG (2a-2f)
- `content_hash` field on Span
- `reconcile_annotations` / token_jaccard
- `reattribution_scope`
- Old dataclasses: Span, Chapter, Character with `seq`, `parent_span_id`, `content_hash`
