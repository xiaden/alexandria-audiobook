# Task: Schema, Storage Adapter, Operation Executor, and Config

## Problem Statement
Foundation plan: establish the SQLite-WAL two-graph database schema, the swappable storage adapter interface, the operation executor (split/merge/move/delete on presentation indices), and fix the LLMTaskOverrides config to include the 9 walk task names. All subsequent plans depend on this foundation.

## Dependencies
None — this is Plan A, the root of the dependency chain.

## Phases

### Phase 1: SQLite-WAL Two-Graph Schema
- [ ] Create `app/pipeline/__init__.py` as empty package init
- [ ] Create `app/pipeline/schema.py` with Graph1 TREE tables: series(id TEXT PK), book(id TEXT PK, series_id TEXT FK, book_number INTEGER, version INTEGER DEFAULT 1, position INTEGER), chapter(id TEXT PK, book_id TEXT FK), scene(id TEXT PK), paragraph(id TEXT PK), span(id TEXT PK, span_type TEXT CHECK(sentence|quotation))
- [ ] Create Graph1 edge tables: book_chapter(child_id TEXT UNIQUE FK, parent_id TEXT FK, position INTEGER, UNIQUE(parent_id, position)), chapter_scene(child_id TEXT UNIQUE FK, parent_id TEXT FK, position INTEGER, UNIQUE(parent_id, position)), scene_paragraph(child_id TEXT UNIQUE FK, parent_id TEXT FK, position INTEGER, UNIQUE(parent_id, position)), paragraph_span(child_id TEXT UNIQUE FK, parent_id TEXT FK, position INTEGER, UNIQUE(parent_id, position))
- [ ] Create Graph2 CHARACTER table: character(id TEXT PK UUID, name TEXT NOT NULL, aliases TEXT DEFAULT '[]' JSON array, voice_assignment_id TEXT FK to voice_config NULLABLE)
- [ ] Create Graph2 junction tables: character_series(character_id FK, series_id FK, source CHECK(walk|human|derived), confidence REAL CHECK 0-1, human_override INTEGER DEFAULT 0), character_book(character_id FK, book_id FK, source, confidence, human_override), character_scene(character_id FK, scene_id FK, relation_type CHECK(present|speaker), source, confidence, human_override), character_span(character_id FK, span_id FK, relation_type CHECK(speaker|mentioned|present), source, confidence, human_override)
- [ ] Create span_presentation VIEW: SELECT span.id, span.span_type, ROW_NUMBER() OVER (ORDER BY book.position, chapter.position, paragraph.position, span.position) AS global_index FROM span JOIN paragraph_span ON span.id = paragraph_span.child_id JOIN scene_paragraph ON paragraph_span.parent_id = scene_paragraph.child_id JOIN chapter_scene ON scene_paragraph.parent_id = chapter_scene.child_id JOIN book_chapter ON chapter_scene.parent_id = book_chapter.child_id JOIN book ON book_chapter.parent_id = book.id
- [ ] Create voice_config table: id TEXT PK, name TEXT, description TEXT (or reference existing voice config if present)
- [ ] Write `tests/pipeline/test_schema.py` — spec-first: test all table creation, all constraints (UNIQUE, CHECK, FK), VIEW correctness with sample data, nested sort ordering, 100% schema coverage target
- [ ] Verify: run `pytest tests/pipeline/test_schema.py -v` — all tests pass

### Phase 2: Storage Adapter Interface
- [ ] Create `app/pipeline/adapter.py` with abstract base class `PipelineStorage` defining: init_db(), get_connection(), close(), execute_query(), execute_insert(), execute_update(), execute_delete()
- [ ] Implement `SQLiteAdapter(PipelineStorage)` — opens `./data/pipeline.db` with WAL mode, foreign_keys=ON, journal_mode=WAL
- [ ] Implement in-memory adapter `InMemorySQLiteAdapter(PipelineStorage)` for testing — same schema, no disk
- [ ] Create `app/pipeline/db.py` with module-level `get_pipeline_db()` factory that returns the configured adapter (default: SQLiteAdapter)
- [ ] Write `tests/pipeline/test_adapter.py` — spec-first: test WAL mode enabled, FK enforcement, connection lifecycle, in-memory adapter parity, 100% adapter coverage target
- [ ] Verify: run `pytest tests/pipeline/test_adapter.py -v` — all tests pass

### Phase 3: Operation Executor
- [ ] Create `app/pipeline/operations.py` with class `OperationExecutor` taking a PipelineStorage instance
- [ ] Implement `execute_split(presentation_index, split_point)` — splits a span into two at the given text offset, redistributes character_span rows deterministically (left span keeps existing memberships, right span gets copy), renumbers positions
- [ ] Implement `execute_merge(presentation_index_left, presentation_index_right)` — merges two adjacent spans, combines character_span memberships (union), renumbers positions
- [ ] Implement `execute_move(presentation_index_from, presentation_index_to)` — moves a span to a new position, renumbers positions in affected range
- [ ] Implement `execute_delete(presentation_index)` — removes a span, redistributes character_span rows (removes memberships for deleted span), renumbers positions
- [ ] All operations use presentation indices only (via span_presentation VIEW) — LLM emits intent on indices, code performs assembly
- [ ] Write `tests/pipeline/test_operations.py` — spec-first: test each operation with various character_span configurations, verify position renumbering, verify membership redistribution, 100% operation executor coverage target
- [ ] Verify: run `pytest tests/pipeline/test_operations.py -v` — all tests pass

### Phase 4: LLMTaskOverrides Config Fix
- [ ] Update `LLMTaskOverrides` in `app/app.py` to replace the 6 old task names with 9 new walk task names: scene_segmentation, character_discovery, script_alias_resolution, scene_presence, span_attribution, character_description, voice_audition, voice_assignment, delivery
- [ ] Each new task name gets a `TaskLLMConfig()` default (all fields optional, inherit global)
- [ ] Update frontend `setup.ts` to display the 9 new task names in the task overrides configuration UI
- [ ] Verify: load app config with new task names, confirm `resolve_task_llm()` resolves each walk task name correctly

## Completion Criteria
- SQLite-WAL database at `./data/pipeline.db` with complete two-graph schema
- span_presentation VIEW produces correct global_index via nested sort
- Storage adapter interface supports swappable backends (SQLite + in-memory)
- Operation executor handles split/merge/move/delete on presentation indices
- LLMTaskOverrides contains all 9 walk task names
- All tests pass with 100% coverage on schema, adapter, and operations
- No timestamps in schema, no content_hash, no Jaccard, no reattribution_scope
