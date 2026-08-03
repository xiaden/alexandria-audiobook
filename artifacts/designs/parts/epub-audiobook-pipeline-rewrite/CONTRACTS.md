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

### get_pipeline_db()
```python
def get_pipeline_db() -> PipelineStorage
# Module-level factory, returns configured adapter
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
- `voice_config(id TEXT PK, name TEXT, description TEXT)`

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
# Creates chapter_paragraph edges (temporary until Walk 2a creates scenes)
```

### insert_scene
```python
def insert_scene(scene_id: str, chapter_id: str, paragraph_ids: list, storage: PipelineStorage) -> None
# Creates scene, chapter_scene edge, scene_paragraph edges
# Removes old chapter_paragraph edges for those paragraphs
```

## Operation Executor

### OperationExecutor
```python
class OperationExecutor:
    def __init__(self, storage: PipelineStorage)
    def execute_split(self, presentation_index: int, split_point: int) -> None
    def execute_merge(self, presentation_index_left: int, presentation_index_right: int) -> None
    def execute_move(self, presentation_index_from: int, presentation_index_to: int) -> None
    def execute_delete(self, presentation_index: int) -> None
    # All operations use presentation indices only (via span_presentation VIEW)
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
```

### Walk 2c: Alias Resolution
```python
def execute(book_id: str, storage: PipelineStorage, config: dict) -> dict
# resolve_task_llm('script_alias_resolution') → temperature=0.1, GLOBAL scope
# Merges duplicate characters, consolidates aliases
```

### Walk 2d: Scene Presence
```python
def execute(book_id: str, storage: PipelineStorage, config: dict) -> dict
# resolve_task_llm('scene_presence') → temperature=0.1, LOCAL
# Refines character_scene junctions (relation_type=present)
```

### Walk 2e: Span Speaker Attribution
```python
def execute(book_id: str, storage: PipelineStorage, config: dict) -> dict
# resolve_task_llm('span_attribution') → temperature=0.1, LOCAL
# Creates character_span junctions with relation_type=speaker for quotations
```

### Walk 2f: Character Description
```python
def execute(book_id: str, storage: PipelineStorage, config: dict) -> dict
# resolve_task_llm('character_description') → temperature=0.1, LOCAL
# Generates character descriptions, stores in character_metadata
```

### Walk 2g: Voice Audition
```python
def execute(book_id: str, storage: PipelineStorage, config: dict) -> dict
# resolve_task_llm('voice_audition') → temperature=0.3, LOCAL (interpretive)
# Generates voice profiles, stores in character_metadata
```

### Walk 2h: Voice Assignment
```python
def execute(book_id: str, storage: PipelineStorage, config: dict) -> dict
# resolve_task_llm('voice_assignment') → temperature=0.1, LOCAL
# Assigns voice_assignment_id in character table (NOT locked)
```

### Walk 2i: Delivery
```python
def execute(book_id: str, storage: PipelineStorage, config: dict) -> dict
# resolve_task_llm('delivery') → temperature=0.3, LOCAL (interpretive, MUST LLM)
# Generates instruct field per span, stores in span.instruct or span_metadata
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
def render_audiobook(book_id: str, storage: PipelineStorage, tts_engine: TTSEngine) -> str
# Bridges pipeline output to TTSEngine
# Maps speaker to voice config, NARRATOR fallback
# Preserves TTSEngine's parallel/batch behavior when configured
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

## Superseded Contracts (v2 — DO NOT USE)
- File-based `pipeline_state/` JSON storage
- 6-walk DAG (2a-2f)
- `content_hash` field on Span
- `reconcile_annotations` / token_jaccard
- `reattribution_scope`
- Old dataclasses: Span, Chapter, Character with `seq`, `parent_span_id`, `content_hash`
