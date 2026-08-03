# epub-audiobook-pipeline-rewrite (v3)

> **Design Document:** `artifacts/designs/pending/DD-epub-audiobook-pipeline-rewrite-v3.md`
> **Status:** ACTIVE — v3 design (SQLite-WAL two-graph model, 9-walk serial DAG)
> **Supersedes:** All prior v2 plans and contracts referencing file-based `pipeline_state/` JSON storage, 6-walk DAG, content_hash, Jaccard reconciliation.

## Plan Dependency Graph

```
Plan A (Schema + Adapter + Operations + Config)
    ↓
Plan B (EPUB Extraction + Walk 2a)
    ↓
Plan C (Walk 2b + Walk 2c)
    ↓
Plan D (Walk 2d + Walk 2e + Walk 2f)
    ↓
Plan E (Walk 2g + Walk 2h + Walk 2i)
    ↓
Plan F (Assembly + Export + TTS + Review)
    ↓
Plan G (API Endpoints + Frontend Rewiring)
    ↓
Plan H (Deprecation + Integration Tests + Cleanup)
```

## Plan List

| Letter | Title | Phases | Steps | Weighted Chars (est.) |
|--------|-------|--------|-------|----------------------|
| A | Schema, Storage Adapter, Operation Executor, Config | 4 | 27 | ~21K |
| B | EPUB Extraction, Spine Population, Walk 2a | 4 | 31 | ~24K |
| C | Walk 2b Character Discovery, Walk 2c Alias Resolution | 3 | 26 | ~20K |
| D | Walk 2d Scene Presence, Walk 2e Span Attribution, Walk 2f Character Description | 3 | 24 | ~25K |
| E | Walk 2g Voice Audition, Walk 2h Voice Assignment, Walk 2i Delivery | 3 | 24 | ~19K |
| F | Assembly, Export, TTS Integration, Confidence Review | 4 | 29 | ~22K |
| G | API Endpoints, Frontend Rewiring | 6 | 47 | ~26K |
| H | Deprecation, Integration Tests, Final Cleanup | 4 | 29 | ~13K |

**Total:** 8 plans, 31 phases, 237 steps, ~170K weighted chars

## Execution Order

Plans are sequentially ordered A→H. Each plan depends on all prior plans. No parallel execution of plans (each builds on the previous).

**Recommended execution:** Sequential, one plan at a time. Each plan should be validated (all tests pass) before proceeding to the next.

## Key Design Decisions (from DD v3)

1. **SQLite-WAL two-graph model** — Graph1 TREE (series→book→chapter→scene→paragraph→span) + Graph2 CHARACTER (character + junctions)
2. **9-walk serial DAG** — Walks 2a-2i run sequentially, each consumes prior walk's output
3. **Operation-executor-owned ordering** — LLM emits intent on presentation indices, code performs assembly
4. **UNKNOWN→NARRATOR at TTS boundary** — unowned spans presented as NARRATOR's voice config
5. **Confidence filter** — ≥0.7 auto-accept, <0.5 auto-reject, between → user review
6. **Voice = assignment, not lock** — user can change voice assignments via frontend
7. **Speaker attribution = character_span membership** — relation_type CHECK(speaker|mentioned|present)
8. **No Jaccard, no content_hash, no dirty-flag, no timestamps**
9. **TTS generation MAY be parallelized IF configured** — preserve TTSEngine's existing parallel/batch behavior
10. **Re-onboarding = new book (version++)** — memberships NOT carried over by default

## Blocker Fixes (validated in plans)

1. ✅ `TaskLLMConfig.temperature` — DONE in code (app.py:198)
2. ✅ `resolve_task_llm` returns temperature — DONE in code (utils.py:139)
3. ⚠️ `LLMTaskOverrides` missing 9 walk task names — **Plan A Phase 4** adds them
4. ✅ `extract_epub_text` hierarchy — interim marker-based accepted (Plan B Phase 1)

## Testing Strategy (spec-first)

| Category | Coverage Target |
|----------|----------------|
| Schema | 100% |
| Operation Executor | 100% |
| Walks | 80% |
| Frontend | 60% |
| Storage Adapter | 100% |
| API Endpoints | 100% |

- In-memory SQLite per test session
- Spec-first: write tests before implementation
- 5 test categories: schema, walk, assembly, integration, presentation

## File Structure (new files)

```
app/pipeline/
├── __init__.py
├── schema.py          # Graph1 + Graph2 DDL
├── adapter.py         # PipelineStorage ABC + SQLiteAdapter + InMemorySQLiteAdapter
├── api.py              # storage dependency and /api/pipeline/* router
├── operations.py      # OperationExecutor (split/merge/move/delete)
├── extract.py         # extract_epub_text()
├── populate.py        # populate_initial_spine(), insert_scene()
├── ledger.py          # CharacterLedger
├── assembly.py        # export_annotated_script()
├── tts_integration.py # render_audiobook()
├── review.py          # ReviewManager
└── walks/
    ├── __init__.py
    ├── runner.py      # WalkRunner
    ├── walk_2a_scene_segmentation.py
    ├── walk_2b_character_discovery.py
    ├── walk_2c_alias_resolution.py
    ├── walk_2d_scene_presence.py
    ├── walk_2e_span_attribution.py
    ├── walk_2f_character_description.py
    ├── walk_2g_voice_audition.py
    ├── walk_2h_voice_assignment.py
    └── walk_2i_delivery.py

tests/pipeline/
├── test_schema.py
├── test_adapter.py
├── test_operations.py
├── test_extract.py
├── test_populate.py
├── test_runner.py
├── test_walk_2a.py through test_walk_2i.py
├── test_ledger.py
├── test_assembly.py
├── test_tts_integration.py
├── test_review.py
├── test_reonboard.py
├── test_api.py
├── test_deprecation.py
├── test_e2e.py
└── test_presentation.py
```

## Superseded Artifacts (v2 — DO NOT USE)

The following artifacts reference the OLD v2 design and are superseded:
- Old 6-walk DAG definitions (walk-definitions.md)
- Old data model with content_hash, Jaccard, reconcile_annotations (data-model.md)
- Old file-based pipeline_state/ JSON storage
- Old plan files (if any remain in artifacts/plans/pending/)

## Sizing Notes

All plans are sized by **weighted context** (estimated chars × cognitive weight), NOT by calendar time or person-weeks. Each phase is sized for an agent worker (~30K weighted char limit per phase). No time estimates appear anywhere in these plans.
