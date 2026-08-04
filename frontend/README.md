# Frontend

The Alexandria audiobook frontend is a multi-tab web application. The **Editor** tab is the primary UI for script manipulation and TTS rendering.

## Editor Tab Architecture

The editor tab is split into three files:

| File | Role |
|------|------|
| `src/tabs/editor.ts` | **Routing layer.** Re-exports all public API from the two sub-modules and contains `initEditor()` which wires DOM event listeners. |
| `src/tabs/editor-pipeline.ts` | **Pipeline mode.** Span-based editing against `/api/pipeline/*` endpoints. Active when `state.pipelineEnabled = true`. |
| `src/tabs/editor-legacy.ts` | **Legacy mode.** Chunk-based editing against `/api/chunks/*` endpoints. Active when `state.pipelineEnabled = false` (current default). |

### Mode Routing

The `pipelineEnabled` toggle (set in Setup tab, persisted in `state.ts`) determines which mode is active:

- **Pipeline mode** (`pipelineEnabled = true`): loads spans via `GET /api/pipeline/export/{book_id}`, shows the `#pipeline-editor-section`, and routes operations through `/api/pipeline/*`.
- **Legacy mode** (`pipelineEnabled = false`): loads chunks via `GET /api/chunks`, shows the `#legacy-editor-section`, and routes operations through `/api/chunks/*`.

The tab-switch handler in `initEditor()` calls `loadSpans()` + `loadReviewItems()` or `loadChunks()` depending on the toggle.

## Pipeline Mode Operations

All structural operations go through a single endpoint:

```
POST /api/pipeline/operation
Body: { operation, book_id, ...params }
```

| Operation | Params | Description |
|-----------|--------|-------------|
| `split` | `presentation_index`, `split_point` | Split a span at a character offset |
| `merge` | `presentation_index_left`, `presentation_index_right` | Merge two adjacent spans |
| `move` | `presentation_index_from`, `presentation_index_to` | Move a span to a new position |
| `delete` | `presentation_index` | Delete a span |

Other pipeline endpoints:

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/pipeline/export/{book_id}` | GET | Load spans for the editor |
| `/api/pipeline/review/{book_id}` | GET | Load confidence review items |
| `/api/pipeline/review/accept` | POST | Accept a review item |
| `/api/pipeline/review/reject` | POST | Reject a review item |
| `/api/pipeline/review/override` | POST | Override a review item with a new value |
| `/api/pipeline/render` | POST | Render audiobook via pipeline TTS |

## Legacy Mode Operations

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/chunks` | GET | Load all chunks |
| `/api/chunks/{id}` | POST | Update a chunk field |
| `/api/chunks/{id}/insert` | POST | Insert a new chunk after the given ID |
| `/api/chunks/{id}` | DELETE | Delete a chunk (with undo support) |
| `/api/chunks/{id}/generate` | POST | Generate audio for a single chunk |
| `/api/chunks/restore` | POST | Restore a deleted chunk (undo) |
| `/api/generate_batch` | POST | Batch render all pending chunks (sequential TTS) |
| `/api/generate_batch_fast` | POST | Batch render with fast/parallel TTS |
| `/api/merge` | POST | Merge all rendered chunks into final M4B audiobook |

## Confidence Review System

Pipeline mode includes a confidence review UI. When the pipeline produces low-confidence character attributions (between the auto-accept ≥0.7 and auto-reject <0.5 thresholds), they are surfaced to the user as review items.

Each review item shows:
- Character name and confidence percentage
- The junction table (e.g., `character_span`) and reason
- Three actions: **Accept**, **Reject**, or **Override** (with a custom JSON value)

Review items are loaded from `GET /api/pipeline/review/{book_id}` and rendered in the `#review-items-container`.

## TTS Rendering

Both modes support batch TTS rendering and final audiobook merging:

| Mode | Render | Cancel | Merge |
|------|--------|--------|-------|
| Pipeline | `POST /api/pipeline/render` → `pipelineRenderAll()` | `POST /api/cancel_audio` → `cancelPipelineRender()` | `POST /api/merge` → `mergeAudiobook()` |
| Legacy | `POST /api/generate_batch` or `/api/generate_batch_fast` → `renderAll()` / `renderBatchFast()` | `POST /api/cancel_audio` → `cancelRender()` | `POST /api/merge` → `mergeAudiobook()` |

The legacy mode selects between batch endpoints based on the TTS mode dropdown (`#tts-mode`): "external" uses `generate_batch`, "local" uses `generate_batch_fast`.
