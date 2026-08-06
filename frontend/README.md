# Frontend

The Alexandria audiobook frontend is a multi-tab web application. The **Editor** tab is the primary UI for script manipulation and TTS rendering.

## Tab Structure

The frontend is organized into core pipeline tabs (script, voices, editor, setup) and advanced tool tabs (designer, preparer, dataset-builder, training):

| Tab | Module | Purpose |
|-----|--------|---------|
| Setup | `src/tabs/setup.ts` | LLM endpoint config (base URL / API key / model / reasoning / temperature) and TTS settings (mode, device, language, parallel workers, batch seed, codec compilation, sub-batching, pauses) |
| Script | `src/tabs/script.ts` | Book onboarding (`POST /api/pipeline/onboard`), run walks (`POST /api/pipeline/run_walks`, `run_all_walks`), walk status polling (`GET /api/pipeline/walk_status/{book_id}`), cancel walks, re-onboard |
| Voices | `src/tabs/voices.ts` | Character list with voice assignment dropdowns (`GET /api/pipeline/characters/{book_id}`, `PUT /api/pipeline/characters/{id}/voice`) and voice catalog management (`GET/POST/PUT/DELETE /api/pipeline/voices`, preview) |
| Editor | `src/tabs/editor.ts` + `src/tabs/editor-pipeline.ts` | Span-based editing against `/api/pipeline/*` endpoints: structural operations, inline span text edits, confidence review, render, merge, download |
| Designer | `src/tabs/designer.ts` | Voice Designer — describe a voice, generate and preview it, save to the library (`/api/voice_design/*`) |
| Preparer | `src/tabs/preparer.ts` | Voice Training Dataset Preparer — upload and prepare LoRA training datasets (`/api/preparer/*`) |
| Dataset Builder | `src/tabs/dataset-builder.ts` | Build LoRA training datasets with per-sample preview (`/api/dataset_builder/*`) |
| Training | `src/tabs/training.ts` | Upload/generate datasets and run LoRA training (`/api/lora/*`, `/api/clone_voices/*`) |

## Editor Tab Architecture

| File | Role |
|------|------|
| `src/tabs/editor.ts` | Routing layer. Re-exports `initEditor` and delegates rendering to `editor-pipeline.ts`. |
| `src/tabs/editor-pipeline.ts` | The single editor implementation. Span-based editing against `/api/pipeline/*` endpoints. |

There is no mode routing — the pipeline is the only editor path.

## Editor Operations

The editor submits structural operations to the pipeline:

| Operation | Description | Params |
|-----------|-------------|--------|
| `split` | Split a span at a character offset | `span_id`, `offset` |
| `merge` | Merge a span into its predecessor | `span_id` |
| `move` | Move a span up/down in the scene | `span_id`, `direction` |
| `delete` | Delete a span | `span_id` |

```ts
POST /api/pipeline/operation
{ "operation": "split", "book_id": "123", "span_id": "...", "offset": 12 }
```

Other pipeline endpoints used by the editor:

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/pipeline/export/{book_id}` | GET | Load the annotated script for a book |
| `/api/pipeline/span/{span_id}/text` | PUT | Inline edit a span's text |
| `/api/pipeline/review/{book_id}` | GET | Load confidence review items |
| `/api/pipeline/review/accept` | POST | Accept a review item |
| `/api/pipeline/review/reject` | POST | Reject a review item |
| `/api/pipeline/review/override` | POST | Override a review item |
| `/api/pipeline/render` | POST | Start a render job (returns `job_id`) |
| `/api/pipeline/render_status/{job_id}` | GET | Poll render progress |
| `/api/pipeline/cancel_render` | POST | Cancel a running render job |
| `/api/pipeline/merge` | POST | Merge rendered chunks into `audiobook.m4b` |
| `/api/pipeline/download/{job_id}` | GET | Download the merged M4B (or a ZIP of raw chunks) |

## Confidence Review System

The review system surfaces spans the annotation walks flagged as low-confidence:

- Items between the auto-accept threshold (≥ 0.7) and auto-reject threshold (< 0.5) are shown for human review
- Each item shows the character name, confidence %, and the junction/reasoning that triggered the flag
- The reviewer can **Accept**, **Reject**, or **Override** each item
- Items are loaded from `GET /api/pipeline/review/{book_id}` into `#review-items-container`

## TTS Rendering

Rendering is pipeline-only:

| Step | Endpoint | Frontend |
|------|----------|----------|
| Render | `POST /api/pipeline/render` → `renderPipeline()` | starts a background job, returns `job_id` |
| Progress | `GET /api/pipeline/render_status/{job_id}` | polls every ~2s, updates progress bar and log |
| Cancel | `POST /api/pipeline/cancel_render` → `cancelPipelineRender()` | cancels a running render job |
| Merge | `POST /api/pipeline/merge` → `mergePipelineAudiobook()` | combines rendered chunks into `audiobook.m4b` |
| Download | `GET /api/pipeline/download/{job_id}` → `downloadPipelineRender()` | downloads the merged M4B (or `audiobook.zip` of raw chunks) |

The render request body is `{ book_id, use_batch, output_dir?, batch_seed? }`; batch mode (default) renders all pending spans in a single batched TTS call.
