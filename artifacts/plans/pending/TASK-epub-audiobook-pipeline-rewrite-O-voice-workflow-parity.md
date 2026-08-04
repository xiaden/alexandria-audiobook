# Task: Voice Workflow Parity Restoration

## Problem Statement

The pipeline voice workflow has critical regressions that prevent capability-preserving cutover from legacy:

1. **CRITICAL**: `voice_config` table schema lacks voice type metadata (type, voice, ref_audio, adapter_id, etc.), so Walk 2h sees no voices and pipeline TTS falls back to narrator for every character.
2. **CRITICAL**: `_build_voice_config` hardcodes `type=custom` regardless of actual voice type in DB.
3. **HIGH**: Frontend manual character assignment updates in-memory Map then calls legacy `debouncedSaveVoices` → writes `voice_config.json` file, does NOT persist `character.voice_assignment_id` to DB.
4. **HIGH**: `/api/voices` reads legacy `annotated_script.json` speakers and `voice_config.json` file, not pipeline DB tables.
5. **MEDIUM**: Walk 2g generates only structured voice profile JSON, no audio preview; narrator is hardcoded Ryan; clone/design/LoRA capabilities are disconnected from pipeline.

**Goal**: Restore full voice workflow parity (all 5 voice types, manual assignment, catalog browsing, voice preview) in pipeline mode before legacy cutover. Do not delete legacy code in this plan.

## Dependencies

- **Plan A** (Schema Migration) must complete before all other plans (provides DB schema for voice catalog).
- **Plan B** (Voice Catalog Seeding) depends on Plan A (needs schema).
- **Plan C** (Pipeline Voice API) depends on Plan A (needs schema).
- **Plan D** (TTS Integration Fix) depends on Plan A (needs schema).
- **Plan E** (Frontend Persistence) depends on Plan C (needs API endpoints).
- **Plan F** (Narrator Configurability) depends on Plan A (needs schema).
- **Plan G** (Voice Preview Generation) depends on Plan C (needs API endpoints).
- **Plan H** (Regression Tests) depends on all prior plans (needs full pipeline working).

## Phase Dependency Graph

```
Plan A (Schema)
  ↓
Plan B (Seeding) ──┐
Plan C (API) ──────┼─→ Plan E (Frontend)
Plan D (TTS Fix) ──┤
Plan F (Narrator) ─┘
                    ↓
Plan G (Preview) ──→ Plan H (Tests)
```

## Negative Constraints

- **DO NOT** delete legacy code (voice_config.json, /api/voices legacy path, /api/save_voice_config).
- **DO NOT** invent incompatible voice_config fields without reading `app/tts.py` and `VoiceConfigItem` model.
- **DO NOT** modify TTSEngine contract (generate_batch/generate_voice signatures).
- **DO NOT** claim soundfile/full-suite coverage unless executable in current environment.
- **DO NOT** hardcode Ryan as narrator if existing product contract indicates otherwise (research shows Ryan is hardcoded default, not from config).

---

# Plan A: Schema Migration

## Problem Statement

The `voice_config` table has only 3 columns (id, name, description) but `VoiceConfigItem` model requires 11 fields (type, voice, character_style, seed, ref_audio, ref_text, adapter_id, adapter_path, description, alias_of). Without these columns, the pipeline cannot store or retrieve voice type metadata.

## Phases

### Phase 1: Add missing columns to voice_config table

- [ ] Read `app/pipeline/schema.py` to confirm current `voice_config` DDL (lines 93-97).
- [ ] Read `app/app.py` `VoiceConfigItem` model (lines 290-301) to confirm required fields.
- [ ] Update `_GRAPH2_CHARACTER_DDL` in `app/pipeline/schema.py` to add columns: `type TEXT DEFAULT 'custom'`, `voice TEXT`, `character_style TEXT`, `seed TEXT DEFAULT '-1'`, `ref_audio TEXT`, `ref_text TEXT`, `adapter_id TEXT`, `adapter_path TEXT`, `alias_of TEXT`.
  **Notes:** All columns match `VoiceConfigItem` model fields. `type` defaults to 'custom' for backward compat. `seed` defaults to '-1' (random).
- [ ] Write migration script `scripts/migrate_voice_config_schema.py` that connects to existing pipeline DB (WAL mode), adds missing columns via `ALTER TABLE` (idempotent, checks if column exists), and logs migration status.
- [ ] Test migration script on a test DB with old schema (3 columns) → verify new columns added.
- [ ] Test migration script on a test DB with new schema (11 columns) → verify idempotent (no errors).

### Phase 2: Update CONTRACTS.md

- [ ] Update `artifacts/designs/parts/epub-audiobook-pipeline-rewrite/CONTRACTS.md` to document new `voice_config` schema (11 columns).
- [ ] Add migration script path to CONTRACTS.md.

## Completion Criteria

- `voice_config` table has all 11 columns matching `VoiceConfigItem` model.
- Migration script runs successfully on old and new schema.
- CONTRACTS.md updated with new schema.

---

# Plan B: Voice Catalog Seeding

## Problem Statement

The `voice_config` table is empty in production, so Walk 2h has no voices to assign. Legacy `voice_config.json` file contains user-created voices that must be migrated to the pipeline DB.

## Phases

### Phase 1: Migrate legacy voice_config.json to pipeline DB

- [ ] Read `app/app.py` to understand `voice_config.json` structure (lines 850-867, 893-912).
- [ ] Write migration script `scripts/migrate_voice_config_json_to_db.py` that reads `voice_config.json` from project root (if exists), inserts a row into `voice_config` table for each speaker with all fields from `VoiceConfigItem`, skips speakers already in DB (idempotent), and logs migration status (count of voices migrated).
- [ ] Test migration script with sample `voice_config.json` → verify rows inserted.
- [ ] Test migration script with empty `voice_config.json` → verify no errors.

### Phase 2: Seed default voice catalog

- [ ] Write seed script `scripts/seed_voice_catalog.py` that inserts a default "Ryan" voice (type=custom, voice=Ryan) if not present, and optionally inserts sample clone/design/LoRA voices for testing (controlled by flag).
- [ ] Test seed script → verify default voices inserted.

### Phase 3: Update CONTRACTS.md

- [ ] Document migration scripts in CONTRACTS.md.
- [ ] Document seed script usage in CONTRACTS.md.

## Completion Criteria

- Legacy `voice_config.json` migrated to pipeline DB.
- Default voice catalog seeded (at least Ryan).
- Migration scripts are idempotent and logged.

---

# Plan C: Pipeline Voice Catalog API

## Problem Statement

The pipeline has no API to browse, create, edit, or delete voices in the `voice_config` table. Frontend cannot load available voices for assignment.

## Phases

### Phase 1: Add GET /api/pipeline/voices endpoint

- [ ] Read `app/pipeline/api_onboard.py` to understand pipeline API pattern (dependency injection, storage).
- [ ] Add `GET /api/pipeline/voices` endpoint in `app/pipeline/api_voices.py` (new file) that queries `voice_config` table for all rows and returns list of voice config dicts (all 11 fields). Supports optional `type` query param to filter by voice type.
- [ ] Add unit test for `GET /api/pipeline/voices` → verify returns all voices.
- [ ] Add unit test for `GET /api/pipeline/voices?type=clone` → verify filters correctly.

### Phase 2: Add POST /api/pipeline/voices endpoint

- [ ] Add `POST /api/pipeline/voices` endpoint that accepts voice config JSON (all 11 fields from `VoiceConfigItem`), validates `type` is one of: custom, clone, builtin_lora, lora, design, inserts row into `voice_config` table, and returns created voice config.
- [ ] Add unit test for `POST /api/pipeline/voices` → verify row inserted.
- [ ] Add unit test for invalid voice type → verify 400 error.

### Phase 3: Add PUT /api/pipeline/voices/{id} endpoint

- [ ] Add `PUT /api/pipeline/voices/{id}` endpoint that accepts voice config JSON (partial update, exclude_unset), updates row in `voice_config` table, and returns updated voice config.
- [ ] Add unit test for `PUT /api/pipeline/voices/{id}` → verify row updated.
- [ ] Add unit test for non-existent id → verify 404 error.

### Phase 4: Add DELETE /api/pipeline/voices/{id} endpoint

- [ ] Add `DELETE /api/pipeline/voices/{id}` endpoint that deletes row from `voice_config` table and returns 204 on success.
- [ ] Add unit test for `DELETE /api/pipeline/voices/{id}` → verify row deleted.
- [ ] Add unit test for non-existent id → verify 404 error.

### Phase 5: Register API in pipeline router

- [ ] Read `app/pipeline/__init__.py` to understand API registration pattern.
- [ ] Register `api_voices` router in pipeline API.
- [ ] Test full API flow (GET, POST, PUT, DELETE) via integration test.

### Phase 6: Update CONTRACTS.md

- [ ] Document all 4 endpoints in CONTRACTS.md (request/response shapes, error codes).

## Completion Criteria

- All 4 CRUD endpoints working for pipeline voice catalog.
- Unit tests pass for all endpoints.
- CONTRACTS.md updated with API contracts.

---

# Plan D: TTS Integration Fix

## Problem Statement

`_build_voice_config` hardcodes `type=custom` for all character voices, breaking clone/design/LoRA routing in TTSEngine.

## Phases

### Phase 1: Fix _build_voice_config to use actual voice type

- [ ] Read `app/pipeline/tts_integration.py` lines 100-120 to understand current implementation.
- [ ] Update `_build_voice_config` to query all 11 columns from `voice_config` table (not just name/description) and build voice config dict with actual `type` field from DB, including all voice type metadata (voice, ref_audio, adapter_id, etc.).
  **Notes:** Must preserve all fields that TTSEngine expects for each voice type. See `VoiceConfigItem` model and `app/tts.py` dispatch logic.
- [ ] Update unit test `test_build_voice_config` in `tests/pipeline/test_tts_integration.py` to insert test voices with different types (custom, clone, design) and verify `_build_voice_config` returns correct type for each.
- [ ] Add integration test: render audiobook with clone voice → verify TTSEngine routes to `generate_clone_voice`.

### Phase 2: Update CONTRACTS.md

- [ ] Document `_build_voice_config` behavior in CONTRACTS.md (queries all fields, preserves type).

## Completion Criteria

- `_build_voice_config` returns actual voice type from DB.
- TTSEngine routes clone/design/LoRA voices correctly.
- Tests verify type preservation.

---

# Plan E: Frontend Persistence

## Problem Statement

Frontend `handleCharacterVoiceChange` updates in-memory Map then calls legacy `debouncedSaveVoices` → writes `voice_config.json` file. Does NOT persist `character.voice_assignment_id` to DB.

## Phases

### Phase 1: Add PUT /api/pipeline/characters/{id}/voice endpoint

- [ ] Add `PUT /api/pipeline/characters/{id}/voice` endpoint in `app/pipeline/api_characters.py` (new or existing file) that accepts `{ "voice_assignment_id": "voice-id" }` JSON, updates `character.voice_assignment_id` in DB, and returns updated character.
- [ ] Add unit test for `PUT /api/pipeline/characters/{id}/voice` → verify DB updated.
- [ ] Add unit test for non-existent character → verify 404 error.

### Phase 2: Update frontend to call new endpoint

- [ ] Read `frontend/src/tabs/voices.ts` lines 300-500 to understand `handleCharacterVoiceChange`.
- [ ] Update `handleCharacterVoiceChange` to call `PUT /api/pipeline/characters/{id}/voice` with selected voice ID, remove call to legacy `debouncedSaveVoices` for pipeline mode, and show success/error toast.
- [ ] Test frontend manually: assign voice to character → verify DB updated.

### Phase 3: Update CONTRACTS.md

- [ ] Document `PUT /api/pipeline/characters/{id}/voice` endpoint in CONTRACTS.md.

## Completion Criteria

- Frontend persists `character.voice_assignment_id` to DB.
- Legacy `debouncedSaveVoices` not called in pipeline mode.
- Tests verify DB persistence.

---

# Plan F: Narrator Configurability

## Problem Statement

`NARRATOR_VOICE` is hardcoded to `{'type': 'custom', 'voice': 'Ryan'}`. No way to configure narrator voice via UI or API.

## Phases

### Phase 1: Add narrator voice to voice_config table

- [ ] Update seed script `scripts/seed_voice_catalog.py` to insert a "NARRATOR" voice config (type=custom, voice=Ryan by default) and allow overriding narrator voice via seed script flag.
- [ ] Test seed script → verify NARRATOR voice inserted.

### Phase 2: Update _build_voice_config to use DB narrator

- [ ] Update `_build_voice_config` in `app/pipeline/tts_integration.py` to query `voice_config` table for `id='NARRATOR'` row and use DB narrator config if present, else fall back to `NARRATOR_VOICE` constant.
- [ ] Add unit test: narrator voice in DB → verify `_build_voice_config` uses DB config.
- [ ] Add unit test: narrator voice not in DB → verify fallback to constant.

### Phase 3: Add narrator voice UI (optional, low priority)

- [ ] Add narrator voice selector to frontend voices tab (dropdown of all voices).
- [ ] On change, call `PUT /api/pipeline/voices/NARRATOR` to update narrator config.
- [ ] Test frontend: change narrator voice → verify DB updated.

### Phase 4: Update CONTRACTS.md

- [ ] Document narrator voice configurability in CONTRACTS.md.

## Completion Criteria

- Narrator voice configurable via DB.
- `_build_voice_config` uses DB narrator if present.
- Ryan is default fallback if not configured.

---

# Plan G: Voice Preview Generation

## Problem Statement

Walk 2g generates only structured voice profile JSON, no audio preview. Users cannot hear what a voice sounds like before assigning it to a character.

## Phases

### Phase 1: Add POST /api/pipeline/voices/{id}/preview endpoint

- [ ] Add `POST /api/pipeline/voices/{id}/preview` endpoint in `app/pipeline/api_voices.py` that accepts `{ "sample_text": "text to synthesize" }` JSON, loads voice config from DB, calls `TTSEngine.generate_voice` with voice config, saves audio to `previews/{voice_id}.wav`, and returns `{ "audio_url": "/previews/{voice_id}.wav" }`.
- [ ] Add unit test for `POST /api/pipeline/voices/{id}/preview` → verify audio generated.
- [ ] Add unit test for non-existent voice → verify 404 error.

### Phase 2: Add preview audio serving

- [ ] Mount `/previews` static directory in pipeline API (or reuse existing `/designed_voices/previews`).
- [ ] Test preview audio accessible via URL.

### Phase 3: Add frontend preview button

- [ ] Add "Preview" button to each voice card in frontend voices tab.
- [ ] On click, call `POST /api/pipeline/voices/{id}/preview` with default sample text.
- [ ] Play returned audio URL.
- [ ] Test frontend: click preview → verify audio plays.

### Phase 4: Update CONTRACTS.md

- [ ] Document preview endpoint and audio serving in CONTRACTS.md.

## Completion Criteria

- Voice preview audio can be generated and played.
- Frontend has preview button for each voice.
- Tests verify preview generation.

---

# Plan H: Regression Tests

## Problem Statement

Need comprehensive regression tests to prove voice workflow parity before legacy cutover.

## Phases

### Phase 1: Add voice type routing tests

- [ ] Add test `test_voice_type_routing` in `tests/pipeline/test_tts_integration.py` that inserts test voices with all 5 types (custom, clone, builtin_lora, lora, design), calls `render_audiobook` with script using each voice type, and verifies TTSEngine routes to correct method for each type.
- [ ] Add test `test_narrator_voice_from_db` that inserts NARRATOR voice with non-default config, calls `render_audiobook`, and verifies NARRATOR uses DB config, not hardcoded constant.

### Phase 2: Add manual assignment tests

- [ ] Add test `test_manual_character_voice_assignment` in `tests/pipeline/test_voices.py` (new file) that inserts test voices and characters, calls `PUT /api/pipeline/characters/{id}/voice`, verifies `character.voice_assignment_id` updated in DB, calls `render_audiobook`, and verifies character uses assigned voice.

### Phase 3: Add voice catalog API tests

- [ ] Add test `test_voice_catalog_crud` in `tests/pipeline/test_voices.py` that tests all 4 CRUD endpoints (GET, POST, PUT, DELETE) and verifies DB state after each operation.

### Phase 4: Add voice preview tests

- [ ] Add test `test_voice_preview_generation` in `tests/pipeline/test_voices.py` that inserts test voice, calls `POST /api/pipeline/voices/{id}/preview`, and verifies audio file created and audio URL accessible.

### Phase 5: Update CONTRACTS.md

- [ ] Document all regression tests in CONTRACTS.md.

## Completion Criteria

- All regression tests pass.
- Tests prove voice type routing, manual assignment, catalog CRUD, and preview generation.
- CONTRACTS.md updated with test coverage.

---

## Acceptance Criteria

1. **Schema**: `voice_config` table has all 11 columns matching `VoiceConfigItem` model.
2. **Seeding**: Legacy `voice_config.json` migrated to DB; default voice catalog seeded.
3. **API**: All 4 CRUD endpoints working for pipeline voice catalog.
4. **TTS Routing**: `_build_voice_config` uses actual voice type from DB; TTSEngine routes clone/design/LoRA correctly.
5. **Persistence**: Frontend persists `character.voice_assignment_id` to DB.
6. **Narrator**: Narrator voice configurable via DB; Ryan is default fallback.
7. **Preview**: Voice preview audio can be generated and played.
8. **Tests**: All regression tests pass (voice type routing, manual assignment, catalog CRUD, preview).
9. **Docs**: CONTRACTS.md updated with all new schemas, APIs, and behaviors.

## Unresolved Product Decisions

1. **Narrator voice default**: Ryan is hardcoded default. Should this remain the product default, or should narrator be configurable per-project? **Decision**: Keep Ryan as default fallback, but allow DB override (Plan F).
2. **Voice preview requirement**: Walk 2g generates voice profile JSON only. Is audio preview required for parity? **Decision**: Yes, include preview generation (Plan G) because legacy has preview for clone/design/LoRA voices.
3. **Clone/design/LoRA integration**: All 5 voice types are fully implemented in legacy. Should pipeline support all 5? **Decision**: Yes, preserve all 5 types (no deferral).

## Files/Symbols in Scope

### Schema Migration
- `app/pipeline/schema.py` (voice_config DDL)
- `scripts/migrate_voice_config_schema.py` (new)

### Voice Catalog Seeding
- `scripts/migrate_voice_config_json_to_db.py` (new)
- `scripts/seed_voice_catalog.py` (new)

### Pipeline Voice API
- `app/pipeline/api_voices.py` (new)
- `app/pipeline/__init__.py` (register router)

### TTS Integration Fix
- `app/pipeline/tts_integration.py` (_build_voice_config)

### Frontend Persistence
- `app/pipeline/api_characters.py` (new or existing)
- `frontend/src/tabs/voices.ts` (handleCharacterVoiceChange)

### Narrator Configurability
- `scripts/seed_voice_catalog.py` (update)
- `app/pipeline/tts_integration.py` (_build_voice_config)

### Voice Preview
- `app/pipeline/api_voices.py` (preview endpoint)
- `frontend/src/tabs/voices.ts` (preview button)

### Regression Tests
- `tests/pipeline/test_tts_integration.py` (update)
- `tests/pipeline/test_voices.py` (new)

### Documentation
- `artifacts/designs/parts/epub-audiobook-pipeline-rewrite/CONTRACTS.md` (update)

## Execution Order

Plans A → B/C/D/F (parallel) → E → G → H

Each plan is sized for separate Exec-Manager execution and QA review.
