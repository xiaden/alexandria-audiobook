/**
 * Pipeline editor functions.
 *
 * This module contains the span-based editing code that operates against the
 * pipeline /api/pipeline/* endpoints. The editor is always in pipeline mode;
 * there is no legacy chunk editor anymore.
 *
 * Pipeline endpoints used:
 *   - POST /api/pipeline/operation (split/merge/move/delete)
 *   - PUT  /api/pipeline/span/{span_id}/text  (span text edit)
 *   - GET  /api/pipeline/review/{book_id}
 *   - POST /api/pipeline/review/accept
 *   - POST /api/pipeline/review/reject
 *   - POST /api/pipeline/review/override
 *   - GET  /api/pipeline/export/{book_id}
 *   - POST /api/pipeline/render
 *   - POST /api/pipeline/cancel_render
 *   - GET  /api/pipeline/render_status/{job_id}
 *   - POST /api/pipeline/merge
 *   - GET  /api/pipeline/download/{job_id}
 *   - GET  /api/pipeline/export/jobs/{job_id}/chunks
 *   - GET  /api/pipeline/export/chunk/{job_id}/{idx}  (per-span preview;
 *          sequence playback — player queue of span chunk URLs with
 *          auto-advance on 'ended')
 *   - GET  /api/pipeline/export/audio/{job_id}   (whole-book playback)
 *   - POST /api/pipeline/export/m4b              (multipart form: metadata
 *          + optional cover; raw fetch + FormData, NOT JSON)
 */

import * as API from '../api';
import { state } from '../state';
import { showToast, showConfirm, escapeHtml } from '../utils';
import { getPreviewPlayer } from '../player';
import type { PreviewPlayer } from '../player';

// ---------------------------------------------------------------------------
// Pipeline types
// ---------------------------------------------------------------------------

/** A span from the pipeline export endpoint */
export interface PipelineSpan {
  id: string;
  global_index: number;
  speaker: string;
  text: string;
  instruct: string;
  /** Per-span pause-after override (Plan L). null/undefined = unset → resolve
   * the applicable default; 0 = intentional no-gap. Persisted via
   * PUT /api/pipeline/span/{id}/pause_after. */
  pause_after_ms?: number | null;
}

/** A confidence review item from the pipeline */
export interface ReviewItem {
  item_id: string;
  character_id: string;
  character_name: string;
  confidence: number;
  junction_table: string;
  related_entity_id: string;
  reason?: string;
  walk_name?: string;
  /** Contextual review (Plan J, Phase 6 / DD UX workflow #5): up to 2
   * neighboring span texts in presentation order around the item's target
   * span. Optional — absent on pre-upgrade payloads. */
  neighbors?: { before: string[]; after: string[] };
}

/** A render_chunk row from GET /api/pipeline/export/jobs/{job_id}/chunks */
export interface ChunkRow {
  job_id: string;
  idx: number;
  status: string;
  wav_path: string | null;
  error: string | null;
}

/**
 * Render job status (extended with mode + per-chunk counts for individual mode).
 * The `mode` field ('individual' | 'batch') comes from the render_job row; legacy
 * fallback responses may omit it.
 */
export interface RenderStatus {
  job_id: string;
  status: string;
  output_dir: string | null;
  error: string | null;
  mode?: 'individual' | 'batch';
  total_chunks?: number;
  completed_chunks?: number;
  failed_chunks?: number;
  /**
   * Plan L (P2-S3): resolved effective pause pair for the book (book override
   * -> config default -> 500/250 built-in fallback) plus the number of
   * per-span pause_after_ms overrides present, and the tri-state lifecycle of
   * pause assembly ('pending' until Phase 3 wires the postprocessor).
   */
  resolved_pause_between_speakers_ms?: number;
  resolved_pause_same_speaker_ms?: number;
  pause_override_count?: number;
  pauses_applied?: boolean;
  pauses_state?: 'pending' | 'applied' | 'failed';
  pauses_error?: string | null;
}

// ---------------------------------------------------------------------------
// Pipeline API functions
// ---------------------------------------------------------------------------

/**
 * Submit a structural operation to the pipeline
 * @param operation - Operation type: split, merge, move, delete
 * @param params - Operation-specific parameters
 */
export async function pipelineOperation(
  operation: 'split' | 'merge' | 'move' | 'delete',
  params: Record<string, unknown>,
): Promise<{ status: string; operation: string }> {
  return API.post('/api/pipeline/operation', {
    operation,
    book_id: state.pipelineBookId,
    ...params,
  });
}

/**
 * Load confidence review items for the current book
 */
export async function pipelineReviewItems(): Promise<ReviewItem[]> {
  if (!state.pipelineBookId) return [];
  return API.get<ReviewItem[]>(`/api/pipeline/review/${state.pipelineBookId}`);
}

/**
 * Accept a confidence review item
 */
export async function pipelineReviewAccept(itemId: string): Promise<{ status: string; item_id: string }> {
  return API.post('/api/pipeline/review/accept', { item_id: itemId });
}

/**
 * Reject a confidence review item
 */
export async function pipelineReviewReject(itemId: string): Promise<{ status: string; item_id: string }> {
  return API.post('/api/pipeline/review/reject', { item_id: itemId });
}

/**
 * Override a confidence review item with a new value
 */
export async function pipelineReviewOverride(
  itemId: string,
  newValue: unknown,
): Promise<{ status: string; item_id: string }> {
  return API.post('/api/pipeline/review/override', { item_id: itemId, new_value: newValue });
}

/**
 * Render the audiobook via the pipeline
 * @param useBatch - Whether to use batch rendering
 * @param batchSeed - Optional seed for batch rendering
 */
export async function pipelineRenderAudiobook(
  useBatch = true,
  batchSeed?: number,
): Promise<{ job_id: string; status?: string }> {
  return API.post('/api/pipeline/render', {
    book_id: state.pipelineBookId,
    use_batch: useBatch,
    batch_seed: batchSeed ?? null,
  });
}

/**
 * Poll the status of a render job
 */
export async function pipelineRenderStatus(
  jobId: string,
): Promise<RenderStatus> {
  return API.get(`/api/pipeline/render_status/${jobId}`);
}

/**
 * Cancel a running render job
 */
export async function pipelineCancelRender(
  jobId: string,
): Promise<{ status: string; job_id: string }> {
  // 503 + Retry-After (transaction() owner-thread contention) is retried
  // exactly once by the wrapper before the error surfaces (DD UX workflow #2).
  return API.postWithRetryOnce('/api/pipeline/cancel_render', { job_id: jobId });
}

/**
 * Merge rendered audio chunks into a single M4B file
 */
export async function pipelineMergeAudiobook(
  jobId: string,
): Promise<{ status: string; output_path: string }> {
  return API.post('/api/pipeline/merge', {
    book_id: state.pipelineBookId,
    job_id: jobId,
  });
}

/**
 * Update the text of a span via PUT
 */
export async function pipelineUpdateSpanText(
  spanId: string,
  text: string,
): Promise<{ status: string; span_id: string }> {
  return API.put(`/api/pipeline/span/${spanId}/text`, { text });
}

// ---------------------------------------------------------------------------
// Single-speaker render flag (Plan J, Phase 2)
// ---------------------------------------------------------------------------
// book.single_speaker is a UI write of the flag: enforcement happens ONLY at
// the render boundary (tts_integration._enforce_single_speaker) — the script
// stays faithful and the annotated export is untouched (CONTRACTS decision
// #9). The backend surface is GET/PUT /api/pipeline/book/{book_id}/single_speaker.

/** GET /api/pipeline/book/{bookId}/single_speaker → whether the book renders single-speaker. */
export async function pipelineGetSingleSpeaker(bookId: string): Promise<boolean> {
  const data = await API.get<{ single_speaker: number }>(
    `/api/pipeline/book/${bookId}/single_speaker`,
  );
  return data.single_speaker === 1;
}

/** PUT /api/pipeline/book/{bookId}/single_speaker — persist the flag. */
export async function pipelineSetSingleSpeaker(bookId: string, enabled: boolean): Promise<void> {
  await API.put(`/api/pipeline/book/${bookId}/single_speaker`, { single_speaker: enabled });
}

/**
 * GET /api/pipeline/span/{spanId}/pause_after — the span's per-span pause
 * override. ``pause_after_ms`` is null when the span is unset (resolve the
 * applicable default), 0 = intentional no-gap, else a positive ms value.
 */
export async function pipelineGetSpanPause(spanId: string): Promise<{ pause_after_ms: number | null }> {
  return API.get<{ pause_after_ms: number | null }>(`/api/pipeline/span/${spanId}/pause_after`);
}

/**
 * PUT /api/pipeline/span/{spanId}/pause_after — persist the per-span pause
 * override. ``null`` clears the override (resolve default); ``0`` is an
 * intentional no-gap. Bounded 0..10000 by the backend validate_pause_ms.
 */
export async function pipelineSetSpanPause(spanId: string, pauseAfterMs: number | null): Promise<void> {
  await API.put(`/api/pipeline/span/${spanId}/pause_after`, { pause_after_ms: pauseAfterMs });
}

/**
 * Reflect the book's saved single-speaker flag into the editor toggle.
 * No-ops when the toggle element is absent; defaults to off when no book is
 * onboarded or the read fails (column default is 0).
 */
export async function loadSingleSpeakerToggle(): Promise<void> {
  const toggle = document.getElementById('single-speaker-toggle') as HTMLInputElement | null;
  if (!toggle) return;
  if (!state.pipelineBookId) {
    toggle.checked = false;
    return;
  }
  try {
    toggle.checked = await pipelineGetSingleSpeaker(state.pipelineBookId);
  } catch (err) {
    console.error('Failed to load single-speaker flag:', err);
    toggle.checked = false;
    showToast('Failed to load single-speaker setting', 'error');
  }
}

/**
 * Toggle change handler: persist book.single_speaker through the backend
 * path; revert the toggle and surface an error when the write fails.
 */
export async function handleSingleSpeakerToggleChange(): Promise<void> {
  const toggle = document.getElementById('single-speaker-toggle') as HTMLInputElement | null;
  if (!toggle) return;
  if (!state.pipelineBookId) {
    toggle.checked = false;
    return;
  }
  const previous = !toggle.checked;
  try {
    await pipelineSetSingleSpeaker(state.pipelineBookId, toggle.checked);
  } catch (err) {
    console.error('Failed to save single-speaker flag:', err);
    toggle.checked = previous;
    showToast('Failed to save single-speaker setting', 'error');
  }
}

// ---------------------------------------------------------------------------
// Span-text undo (Plan J, Phase 3)
// ---------------------------------------------------------------------------
// Undo = transactional value-restore through the SAME server-validated
// pipelineUpdateSpanText path (PUT /span/{id}/text) — the DD UX workflow #7
// primitive. The audit-journal/replay mechanism was explicitly REJECTED in the
// DD evidence trail, so no new backend surface is needed: the existing
// update_span_text endpoint is the revert primitive. The stack lives next to
// the span cache so it can be cleared whenever the cache is replaced.

/** One undoable span-text edit: the span id + the text value BEFORE the edit. */
export interface UndoEntry {
  spanId: string;
  priorValue: string;
}

/** Push a {spanId, priorValue} entry onto the undo stack (most recent last). */
export function pushUndoEntry(spanId: string, priorValue: string): void {
  _undoStack.push({ spanId, priorValue });
  syncUndoButton();
}

/** Drop every undo entry (snapshot load / render start / spans reload). */
export function clearUndoStack(): void {
  _undoStack = [];
  syncUndoButton();
}

/** Read-only view of the undo stack (testability). */
export function getUndoStack(): UndoEntry[] {
  return _undoStack;
}

/**
 * Revert the most recent span-text edit through the pipeline PUT path. On
 * success the cached span + visible row are restored so the UI matches the
 * server (the focusout edit flow keeps the cache authoritative). On failure
 * the entry is re-pushed so a later retry can still revert it.
 */
export async function undoLastSpanEdit(): Promise<void> {
  const entry = _undoStack.pop();
  if (!entry) {
    syncUndoButton();
    return;
  }
  try {
    await pipelineUpdateSpanText(entry.spanId, entry.priorValue);
    // Restore the cached span text + the visible row (matched by data-index,
    // the same key the focusout handler uses).
    const span = _cachedSpans.find(s => s.id === entry.spanId);
    if (span) {
      span.text = entry.priorValue;
      const cell = document.querySelector(
        `div.span-text[data-index="${span.global_index}"]`,
      ) as HTMLElement | null;
      if (cell) cell.textContent = entry.priorValue;
    }
    showToast('Undo: span text reverted', 'success');
  } catch (err) {
    console.error('Failed to undo span text edit:', err);
    // Re-push so the failed revert is not lost; the button stays enabled.
    _undoStack.push(entry);
    showToast('Failed to undo span text edit', 'error');
  }
  syncUndoButton();
}

/** Reflect stack emptiness in the Undo button's disabled state. */
function syncUndoButton(): void {
  const btn = document.getElementById('btn-pipeline-undo') as HTMLButtonElement | null;
  if (btn) btn.disabled = _undoStack.length === 0;
}

/**
 * Build the download URL for a rendered audiobook
 */
export function pipelineDownloadUrl(jobId: string): string {
  return `/api/pipeline/download/${jobId}`;
}

/**
 * Export the annotated script for the current book (pipeline spans)
 */
export async function pipelineExportSpans(): Promise<Array<{ id: string; speaker: string; text: string; instruct: string | null }>> {
  if (!state.pipelineBookId) return [];
  return API.get(`/api/pipeline/export/${state.pipelineBookId}`);
}

/**
 * Success payload of POST /api/pipeline/export/m4b (backend contract,
 * api_export.py export_m4b — verified, unchanged).
 *
 * ``message`` is present only when libmp3lame is unavailable and the export
 * degraded to M4B-only (DD open item #8).
 */
export interface M4bExportResult {
  status: string;
  output_path: string;
  mp3: boolean;
  mp3_path: string | null;
  audacity: boolean;
  audacity_path: string | null;
  message?: string;
  /**
   * Truthful pause tri-state (Plan L, P4-S2): true iff the canonical paused
   * artifact (audiobook-paused.wav) was the export source. false with
   * pauses_state 'failed' when assembly was unavailable and the unpaused
   * per-chunk concat was exported instead. Absent/undefined on older
   * responses.
   */
  pauses_applied?: boolean;
  /** Concise message carried only when pauses_applied === true (the paused
   * artifact was used). Absent on the failed fallback. */
  pauses_message?: string;
  /** Plan L (P2-S3): resolved pause pair + override count + tri-state. */
  resolved_pause_between_speakers_ms?: number;
  resolved_pause_same_speaker_ms?: number;
  pause_override_count?: number;
  /** 'pending' = assembly not yet run; 'applied' = paused artifact used;
   * 'failed' = assembly unavailable (see pauses_error). */
  pauses_state?: 'pending' | 'applied' | 'failed';
  /** Bounded failure detail when pauses_state === 'failed' (no fs paths). */
  pauses_error?: string | null;
}

/** Form values for the Export M4B form (5 metadata fields + optional cover). */
export interface ExportM4bPayload {
  jobId: string;
  title: string;
  author: string;
  narrator: string;
  year: string;
  description: string;
  cover?: File | null;
}

/**
 * Export the rendered job as an M4B via POST /api/pipeline/export/m4b.
 *
 * MULTIPART form (FastAPI Form(...) + File(...) — NOT JSON): job_id + the 5
 * metadata fields (title/author/narrator/year/description, all default '' on
 * the backend) + an optional cover UploadFile. Must use raw fetch + FormData
 * — API.post JSON-stringifies and would break the route (same raw-fetch
 * pattern as pipelineOnboard in script.ts).
 *
 * Errors (404 unknown/non-completed job, 410 expired, 409 format mismatch,
 * 400 no chunks) surface the backend ``detail`` via a thrown Error.
 */
export async function pipelineExportM4b(payload: ExportM4bPayload): Promise<M4bExportResult> {
  const formData = new FormData();
  formData.append('job_id', payload.jobId);
  formData.append('title', payload.title ?? '');
  formData.append('author', payload.author ?? '');
  formData.append('narrator', payload.narrator ?? '');
  formData.append('year', payload.year ?? '');
  formData.append('description', payload.description ?? '');
  if (payload.cover) formData.append('cover', payload.cover);
  const res = await fetch('/api/pipeline/export/m4b', {
    method: 'POST',
    body: formData,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(body.detail || res.statusText);
  }
  return res.json();
}

// ---------------------------------------------------------------------------
// Module state
// ---------------------------------------------------------------------------

/** Pause bound (mirror of the backend validate_pause_ms / PAUSE_MAX_MS=10_000). */
export const PAUSE_MAX_MS = 10000;

/** Cached pipeline spans for the current book */
let _cachedSpans: PipelineSpan[] = [];

/** Cached review items */
let _cachedReviewItems: ReviewItem[] = [];

/** Currently selected spans for merge/move operations */
let _selectedIndices: Set<number> = new Set();

/**
 * Undo stack for span-text edits (Plan J, Phase 3): most-recent-last entries
 * of {spanId, priorValue}. Pushed only AFTER a PUT succeeds (a failed edit
 * restores the cached original, so an entry would be a no-op revert) and never
 * for empty-text edits. Cleared on snapshot load, render start, and spans
 * (re)load — the stack must not outlive the span set it refers to.
 */
let _undoStack: UndoEntry[] = [];

/**
 * Contract tooltip for batch-mode per-span preview (DD-universal-upgrade
 * decision #2): batch renders drift per-chunk (unset seed) so per-span preview
 * differs from the final whole-book output. Shown verbatim when the user
 * triggers preview on a batch job.
 */
export const BATCH_PREVIEW_TOOLTIP = 'preview differs from final — whole-book playback only';

/**
 * Preview player used by handlePreviewSpan. Initialized to the shared
 * singleton (createPreviewPlayer audio-less); setPreviewPlayer swaps it out
 * in tests (the factory is injectable per the Phase 3 contract).
 */
let _previewPlayer: PreviewPlayer = getPreviewPlayer();

/** Swap the singleton preview player used by the audio-surface entry points (handlePreviewSpan, playPipelineAudiobook, playSpanSequence) — test injection. */
export function setPreviewPlayer(player: PreviewPlayer): void {
  _previewPlayer = player;
}

// ---------------------------------------------------------------------------
// Pipeline span loading and display
// ---------------------------------------------------------------------------

/**
 * Convert raw export data to PipelineSpan array with global_index
 */
export function toPipelineSpans(
  raw: Array<{ id: string; speaker: string; text: string; instruct: string | null }>,
): PipelineSpan[] {
  return raw.map((item, idx) => ({
    id: item.id || '',
    global_index: idx + 1,
    speaker: item.speaker || '',
    text: item.text || '',
    instruct: item.instruct || '',
  }));
}

/**
 * Load spans from the pipeline and render the editor table (pipeline mode)
 */
export async function loadSpans(): Promise<void> {
  const tbody = document.getElementById('spans-table-body');
  if (!tbody) return;

  if (!state.pipelineBookId) {
    tbody.innerHTML = '<tr><td colspan="7" class="text-center text-muted">No book onboarded. Go to the Script tab to onboard an EPUB first.</td></tr>';
    _cachedSpans = [];
    // No book → no spans → nothing to undo; drop stale entries referencing the
    // previous book (Plan J, Phase 3).
    clearUndoStack();
    return;
  }

  if (tbody.children.length === 0) {
    tbody.innerHTML = '<tr><td colspan="7" class="text-center">Loading spans...</td></tr>';
  }

  try {
    const raw = await pipelineExportSpans();
    const spans = toPipelineSpans(raw);
    // Enrich each span with its per-span pause override (P5-S2) so the Pause
    // column shows the persisted value (null → 'default' placeholder). Fetched
    // in parallel via GET /api/pipeline/span/{id}/pause_after; a read failure
    // leaves the span unset (treats as default) rather than blocking the table.
    await Promise.all(spans.map(async (span) => {
      if (!span.id) return;
      try {
        const { pause_after_ms } = await pipelineGetSpanPause(span.id);
        span.pause_after_ms = pause_after_ms;
      } catch {
        span.pause_after_ms = null;
      }
    }));
    _cachedSpans = spans;
    // The cache was replaced — any undo entry references a stale span set
    // (book switch / tab re-load / snapshot load); clear it (Plan J, Phase 3).
    clearUndoStack();

    if (spans.length === 0) {
      tbody.innerHTML = '<tr><td colspan="7" class="text-center text-muted">No spans found. Run all walks in the Script tab first.</td></tr>';
      return;
    }

    // The progress bar is owned by RENDER progress (Plan F) — span loading
    // must not claim 100% (the static "N spans loaded" overstatement is gone).
    // Leave the bar in its neutral state until a render starts.
    resetRenderProgress();

    // Full redraw
    tbody.innerHTML = spans.map(span => renderSpanRow(span)).join('');
  } catch (e) {
    console.error('Error loading spans:', e);
    showToast('Failed to load spans: ' + (e instanceof Error ? e.message : String(e)), 'error');
  }
}

/**
 * Render a single span row HTML
 */
export function renderSpanRow(span: PipelineSpan): string {
  const isSelected = _selectedIndices.has(span.global_index);
  const rowClass = isSelected ? 'table-active' : '';
  const jobId = state.pipelineRenderJobId ?? _currentRenderJobId ?? '';
  // Individual-mode renders write one render_chunk per span in presentation
  // order with a 0-BASED idx (tts_integration.py: enumerate(script) →
  // _insert_chunk_row(job_id, i, ...)), so chunk idx = global_index − 1.
  const chunkIdx = span.global_index - 1;

  return `
    <tr data-index="${span.global_index}" class="span-row ${rowClass}">
      <td class="text-center align-middle" style="white-space:nowrap;">
        <span class="badge bg-secondary me-1" title="Presentation index">#${span.global_index}</span>
        <button class="btn btn-sm btn-outline-success btn-span-preview" aria-label="Preview span audio" data-index="${span.global_index}" data-job-id="${escapeHtml(jobId)}" data-chunk-idx="${chunkIdx}" title="${jobId ? 'Preview span audio' : 'No render job to preview'}" ${jobId ? '' : 'disabled'}>
          <i class="fas fa-play"></i>
        </button>
        <button class="btn btn-sm btn-outline-info btn-select-span" data-index="${span.global_index}" title="Select for merge/move">
          <i class="fas fa-check-square"></i>
        </button>
      </td>
      <td><span class="fw-bold">${escapeHtml(span.speaker)}</span></td>
      <td><div class="span-text" contenteditable="true" data-span-id="${escapeHtml(span.id)}" data-index="${span.global_index}" title="Click to edit span text" style="min-width: 200px; padding: 4px 8px; border: 1px solid transparent; border-radius: 4px; cursor: text;">${escapeHtml(span.text)}</div></td>
      <td>
        <input type="number" class="form-control form-control-sm span-pause" data-span-id="${escapeHtml(span.id)}" data-index="${span.global_index}" min="0" max="${PAUSE_MAX_MS}" step="1" placeholder="default" title="Pause after this span, in ms. Blank = use the resolved default; 0 = no pause. 0-10000 ms." value="${span.pause_after_ms != null ? span.pause_after_ms : ''}">
      </td>
      <td><div class="span-instruct text-muted small">${escapeHtml(span.instruct)}</div></td>
      <td class="text-center align-middle">
        <div class="btn-group btn-group-sm" role="group">
          <button class="btn btn-outline-primary btn-span-split" data-index="${span.global_index}" title="Split span">
            <i class="fas fa-cut"></i>
          </button>
          <button class="btn btn-outline-warning btn-span-move" data-index="${span.global_index}" title="Move span">
            <i class="fas fa-arrows-alt"></i>
          </button>
          <button class="btn btn-outline-danger btn-span-delete" data-index="${span.global_index}" title="Delete span">
            <i class="fas fa-trash"></i>
          </button>
        </div>
      </td>
    </tr>
  `;
}

// ---------------------------------------------------------------------------
// Per-span audio preview (audio surface)
// ---------------------------------------------------------------------------

/**
 * Preview one span's rendered audio (individual render mode only).
 *
 * Resolves the current render job (state.pipelineRenderJobId first, then the
 * module-level _currentRenderJobId), learns the job mode from
 * GET /api/pipeline/render_status/{job_id}, and plays the chunk URL
 * GET /api/pipeline/export/chunk/{job_id}/{idx} via the injected player
 * (stopThenPlay handles switching away from any current audio).
 *
 * Chunk idx mapping (verified against app/pipeline/tts_integration.py
 * render_audiobook, individual mode): one render_chunk per span in annotated-
 * script presentation order, idx 0-BASED — enumerate(script) →
 * _insert_chunk_row(job_id, i, ...) at L626-634, where script =
 * export_annotated_script is the SAME array GET /api/pipeline/export/{book_id}
 * serves (api_export.py L146). So chunk idx = span.global_index − 1. When the
 * render_chunk row list (GET /api/pipeline/export/jobs/{job_id}/chunks) is
 * available it confirms the idx; otherwise the presentation-order convention
 * stands.
 *
 * Batch jobs have NO per-chunk rows (only job-level rows), so per-span preview
 * is impossible: the contract tooltip (DD decision #2) is shown instead and
 * nothing plays.
 */
export async function handlePreviewSpan(index: number): Promise<void> {
  const jobId = state.pipelineRenderJobId ?? _currentRenderJobId;
  if (!jobId) {
    showToast('No render job to preview', 'warning');
    return;
  }

  // Learn the job mode — batch renders have no per-chunk rows.
  let mode: string | undefined;
  try {
    const status = await pipelineRenderStatus(jobId);
    mode = status.mode;
  } catch (e) {
    console.error('Error loading render status:', e);
    showToast('Failed to load render status: ' + (e instanceof Error ? e.message : String(e)), 'error');
    return;
  }
  if (mode === 'batch') {
    showToast(BATCH_PREVIEW_TOOLTIP, 'warning');
    return;
  }

  // Individual mode: chunk idx = 0-based presentation position. The chunk row
  // list is authoritative when available; fall back to the presentation-order
  // convention on failure or empty rows.
  let chunkIdx = index - 1;
  try {
    const rows = await API.get<ChunkRow[]>(`/api/pipeline/export/jobs/${jobId}/chunks`);
    const row = rows.find(r => r.idx === chunkIdx);
    if (row) chunkIdx = row.idx;
  } catch (e) {
    console.error('Error loading chunk list:', e);
  }

  const url = `/api/pipeline/export/chunk/${jobId}/${chunkIdx}`;
  try {
    await _previewPlayer.play(url);
  } catch (e) {
    // Non-benign playback failure (network error, 404/500 from the export
    // endpoint, decode error) — surface it like the other fetch-error toasts
    // instead of leaving an unhandled rejection from the click handler.
    console.error('Playback failed:', e);
    showToast('Playback failed: ' + (e instanceof Error ? e.message : String(e)), 'error');
  }
}

// ---------------------------------------------------------------------------
// Pipeline operations
// ---------------------------------------------------------------------------

/**
 * Handle split operation on a span
 * @param index - Presentation index of the span to split
 */
export async function handleSplit(index: number): Promise<void> {
  if (!state.pipelineBookId) {
    showToast('No book onboarded', 'warning');
    return;
  }

  // Prompt for split point (character offset)
  const span = _cachedSpans.find(s => s.global_index === index);
  if (!span) return;

  const splitPointStr = prompt(
    `Split span #${index} at character offset (1-${span.text.length}):\n"${span.text.substring(0, 40)}..."`,
    String(Math.floor(span.text.length / 2)),
  );
  if (splitPointStr === null) return; // cancelled

  const splitPoint = parseInt(splitPointStr, 10);
  if (isNaN(splitPoint) || splitPoint < 1 || splitPoint >= span.text.length) {
    showToast('Invalid split point', 'error');
    return;
  }

  try {
    await pipelineOperation('split', {
      presentation_index: index,
      split_point: splitPoint,
    });
    showToast(`Span #${index} split at offset ${splitPoint}`, 'success');
    await loadSpans();
  } catch (e) {
    showToast('Split failed: ' + (e instanceof Error ? e.message : String(e)), 'error');
  }
}

/**
 * Handle merge operation on selected spans
 */
export async function handleMerge(): Promise<void> {
  if (!state.pipelineBookId) {
    showToast('No book onboarded', 'warning');
    return;
  }

  const indices = Array.from(_selectedIndices).sort((a, b) => a - b);
  if (indices.length !== 2) {
    showToast('Select exactly 2 adjacent spans to merge', 'warning');
    return;
  }

  const [left, right] = indices;
  if (right - left !== 1) {
    showToast('Can only merge adjacent spans', 'warning');
    return;
  }

  try {
    await pipelineOperation('merge', {
      presentation_index_left: left,
      presentation_index_right: right,
    });
    showToast(`Merged spans #${left} and #${right}`, 'success');
    _selectedIndices.clear();
    await loadSpans();
  } catch (e) {
    showToast('Merge failed: ' + (e instanceof Error ? e.message : String(e)), 'error');
  }
}

/**
 * Handle move operation — move selected span to target position
 * @param toIndex - Target presentation index
 */
export async function handleMove(toIndex: number): Promise<void> {
  if (!state.pipelineBookId) {
    showToast('No book onboarded', 'warning');
    return;
  }

  const indices = Array.from(_selectedIndices);
  if (indices.length !== 1) {
    showToast('Select exactly 1 span to move', 'warning');
    return;
  }

  const fromIndex = indices[0];
  if (fromIndex === toIndex) {
    showToast('Span is already at that position', 'info');
    return;
  }

  try {
    await pipelineOperation('move', {
      presentation_index_from: fromIndex,
      presentation_index_to: toIndex,
    });
    showToast(`Moved span from #${fromIndex} to #${toIndex}`, 'success');
    _selectedIndices.clear();
    await loadSpans();
  } catch (e) {
    showToast('Move failed: ' + (e instanceof Error ? e.message : String(e)), 'error');
  }
}

/**
 * Handle delete operation on a span
 * @param index - Presentation index of the span to delete
 */
export async function handleDelete(index: number): Promise<void> {
  if (!state.pipelineBookId) {
    showToast('No book onboarded', 'warning');
    return;
  }

  const span = _cachedSpans.find(s => s.global_index === index);
  if (!span) return;

  const confirmed = await showConfirm(
    `Delete span #${index}?\n"${span.speaker}: ${span.text.substring(0, 60)}..."`,
  );
  if (!confirmed) return;

  try {
    await pipelineOperation('delete', {
      presentation_index: index,
    });
    showToast(`Deleted span #${index}`, 'success');
    _selectedIndices.delete(index);
    await loadSpans();
  } catch (e) {
    showToast('Delete failed: ' + (e instanceof Error ? e.message : String(e)), 'error');
  }
}

/**
 * Toggle span selection for merge/move operations
 * @param index - Presentation index to toggle
 */
export function toggleSpanSelection(index: number): void {
  if (_selectedIndices.has(index)) {
    _selectedIndices.delete(index);
  } else {
    _selectedIndices.add(index);
  }

  // Update row visual
  const row = document.querySelector(`tr[data-index="${index}"]`);
  if (row) {
    row.classList.toggle('table-active', _selectedIndices.has(index));
  }

  // Update merge button state
  updateMergeButtonState();
}

/**
 * Update the merge button enabled/disabled state based on selection
 */
function updateMergeButtonState(): void {
  const btnMerge = document.getElementById('btn-pipeline-merge') as HTMLButtonElement;
  if (!btnMerge) return;

  const count = _selectedIndices.size;
  btnMerge.disabled = count !== 2;
  btnMerge.title = count === 2
    ? 'Merge selected spans'
    : `Select exactly 2 adjacent spans to merge (${count} selected)`;
}

// ---------------------------------------------------------------------------
// Confidence review UI
// ---------------------------------------------------------------------------

/**
 * Load and display confidence review items
 */
export async function loadReviewItems(): Promise<void> {
  const container = document.getElementById('review-items-container');
  if (!container) return;

  if (!state.pipelineBookId) {
    container.innerHTML = '<p class="text-muted">No book onboarded.</p>';
    _cachedReviewItems = [];
    return;
  }

  try {
    const items = await pipelineReviewItems();
    _cachedReviewItems = items;

    if (items.length === 0) {
      container.innerHTML = '<div class="alert alert-success mb-0"><i class="fas fa-check-circle me-2"></i>No items need review. All attributions are confident!</div>';
      return;
    }

    container.innerHTML = items.map(item => renderReviewItem(item)).join('');
  } catch (e) {
    console.error('Error loading review items:', e);
    showToast('Failed to load review items: ' + (e instanceof Error ? e.message : String(e)), 'error');
  }
}

/**
 * Render a single review item card
 */
export function renderReviewItem(item: ReviewItem): string {
  const confPercent = Math.round(item.confidence * 100);
  const confBadgeClass = confPercent >= 60 ? 'bg-warning' : 'bg-danger';

  // Contextual review (Plan J, Phase 6 / DD UX workflow #5): a muted,
  // collapsible block listing up to 2 neighboring span texts (before/after)
  // when the payload carries any. No block when neighbors is empty/absent.
  const neighbors = item.neighbors;
  let neighborsBlock = '';
  if (neighbors && (neighbors.before.length > 0 || neighbors.after.length > 0)) {
    neighborsBlock = `
        <details class="review-neighbors mt-2">
          <summary class="text-muted small">Context — neighboring spans</summary>
          <div class="text-muted small mt-1">
            ${neighbors.before.map(t => `<div class="review-neighbor-before"><span class="text-secondary">↑ before</span> ${escapeHtml(t)}</div>`).join('')}
            ${neighbors.after.map(t => `<div class="review-neighbor-after"><span class="text-secondary">↓ after</span> ${escapeHtml(t)}</div>`).join('')}
          </div>
        </details>`;
  }

  return `
    <div class="card mb-2 review-item-card" data-item-id="${escapeHtml(item.item_id)}">
      <div class="card-body py-2 px-3">
        <div class="d-flex justify-content-between align-items-start">
          <div>
            <span class="badge ${confBadgeClass} me-2">${confPercent}% confidence</span>
            <strong>${escapeHtml(item.character_name || item.character_id)}</strong>
            <span class="text-muted ms-1">→ ${escapeHtml(item.junction_table)}</span>
            ${item.reason ? `<div class="text-muted small mt-1">${escapeHtml(item.reason)}</div>` : ''}
          </div>
          <div class="btn-group btn-group-sm">
            <button class="btn btn-success btn-review-accept" data-item-id="${escapeHtml(item.item_id)}" title="Accept attribution">
              <i class="fas fa-check"></i> Accept
            </button>
            <button class="btn btn-danger btn-review-reject" data-item-id="${escapeHtml(item.item_id)}" title="Reject attribution">
              <i class="fas fa-times"></i> Reject
            </button>
            <button class="btn btn-primary btn-review-override" data-item-id="${escapeHtml(item.item_id)}" title="Override with custom value">
              <i class="fas fa-edit"></i> Override
            </button>
          </div>
        </div>
        ${neighborsBlock}
      </div>
    </div>
  `;
}

/**
 * Handle accept action on a review item
 */
export async function handleReviewAccept(itemId: string): Promise<void> {
  try {
    await pipelineReviewAccept(itemId);
    showToast('Review item accepted', 'success');
    await loadReviewItems();
  } catch (e) {
    showToast('Accept failed: ' + (e instanceof Error ? e.message : String(e)), 'error');
  }
}

/**
 * Handle reject action on a review item
 */
export async function handleReviewReject(itemId: string): Promise<void> {
  try {
    await pipelineReviewReject(itemId);
    showToast('Review item rejected', 'success');
    await loadReviewItems();
  } catch (e) {
    showToast('Reject failed: ' + (e instanceof Error ? e.message : String(e)), 'error');
  }
}

/**
 * Handle override action on a review item
 */
export async function handleReviewOverride(itemId: string): Promise<void> {
  const newValueStr = prompt('Enter override value (JSON object, e.g. {"relation_type": "speaker"}):');
  if (newValueStr === null) return;

  let newValue: unknown;
  try {
    newValue = JSON.parse(newValueStr);
  } catch {
    showToast('Invalid JSON', 'error');
    return;
  }

  try {
    await pipelineReviewOverride(itemId, newValue);
    showToast('Review item overridden', 'success');
    await loadReviewItems();
  } catch (e) {
    showToast('Override failed: ' + (e instanceof Error ? e.message : String(e)), 'error');
  }
}

// ---------------------------------------------------------------------------
// TTS rendering — pipeline mode
// ---------------------------------------------------------------------------

/** Currently active render job ID (set when render starts, cleared when finished) */
let _currentRenderJobId: string | null = null;

/** Interval handle for render-status polling */
let _renderPollTimer: ReturnType<typeof setInterval> | null = null;

/**
 * Reset the render progress bar to its neutral state (Plan F).
 *
 * The bar is owned by RENDER progress — span loading no longer claims 100%
 * (the old static "N spans loaded" overstatement is gone). Called when spans
 * load and when a new render starts so a stale bar from a previous render
 * never lingers.
 */
function resetRenderProgress(): void {
  const bar = document.getElementById('full-progress-bar');
  if (bar) {
    bar.classList.remove('progress-bar-animated');
    bar.style.width = '0%';
    bar.innerText = 'Ready';
  }
  const badge = document.getElementById('render-failures-badge');
  if (badge) {
    badge.style.display = 'none';
    badge.innerText = '';
  }
  // Clear the resolved-pause/assembly surface so stale values never outlive a
  // render (P5-S3).
  const pauseInfo = document.getElementById('render-pause-info');
  if (pauseInfo) {
    pauseInfo.style.display = 'none';
    pauseInfo.textContent = '';
  }
}

/**
 * Render the progress bar from a render_status payload (Plan F).
 *
 * Individual mode: per-chunk counts derived from render_chunk rows (rows =
 * truth) — width = completed/total, label `${completed}/${total} chunks`,
 * and a red failure badge (count + job error text as title) when chunks
 * failed. Batch mode has NO per-chunk counts — job-level progress only:
 * running/pending → indeterminate animated striped bar ("Rendering..."),
 * completed → 100%, failed/cancelled → status label. A mode-less legacy
 * payload falls back to the job-level branch.
 */
function updateRenderProgress(status: RenderStatus): void {
  const bar = document.getElementById('full-progress-bar');
  const badge = document.getElementById('render-failures-badge');

  // Resolved-pause / pause-assembly surface (P5-S3): show the effective pause
  // values + override count + tri-state whenever the render_status payload
  // carries them; hide otherwise. The backend reports 'pending' until M4B
  // export runs assembly, so this confirms the values that will be inserted.
  const pauseInfo = document.getElementById('render-pause-info');
  if (pauseInfo && (status.resolved_pause_between_speakers_ms != null || status.pause_override_count != null || status.pauses_state)) {
    const between = status.resolved_pause_between_speakers_ms ?? 500;
    const same = status.resolved_pause_same_speaker_ms ?? 250;
    const overrides = status.pause_override_count ?? 0;
    pauseInfo.textContent =
      `Resolved pauses: ${between} ms between speakers · ${same} ms same speaker · ` +
      `${overrides} span override${overrides === 1 ? '' : 's'}. ` +
      `Assembly: ${status.pauses_state ?? 'pending'}.`;
    pauseInfo.style.display = 'block';
  } else if (pauseInfo) {
    pauseInfo.style.display = 'none';
    pauseInfo.textContent = '';
  }

  if (status.mode === 'individual' && status.total_chunks && status.total_chunks > 0) {
    const completed = status.completed_chunks ?? 0;
    const total = status.total_chunks;
    const failed = status.failed_chunks ?? 0;
    const pct = Math.min(100, Math.round((completed / total) * 100));
    if (bar) {
      bar.classList.remove('progress-bar-animated');
      bar.style.width = `${pct}%`;
      bar.innerText = `${completed}/${total} chunks`;
    }
    if (badge) {
      if (failed > 0) {
        badge.style.display = 'inline-block';
        badge.innerText = `${failed} chunk${failed === 1 ? '' : 's'} failed`;
        badge.title = status.error || `${failed} chunk${failed === 1 ? '' : 's'} failed`;
      } else {
        badge.style.display = 'none';
      }
    }
    return;
  }

  // Batch mode (or a mode-less legacy payload): job-level progress only.
  if (badge) badge.style.display = 'none';
  if (bar) {
    bar.classList.remove('progress-bar-animated');
    if (status.status === 'completed') {
      bar.style.width = '100%';
      bar.innerText = '100%';
    } else if (status.status === 'failed' || status.status === 'cancelled') {
      bar.style.width = '100%';
      bar.innerText = status.status === 'failed' ? 'Failed' : 'Cancelled';
    } else {
      // running / pending — indeterminate animated striped bar
      bar.classList.add('progress-bar-animated');
      bar.style.width = '100%';
      bar.innerText = 'Rendering...';
    }
  }
}

/**
 * Render the audiobook using the pipeline render endpoint.
 *
 * The backend executes the render as a background job and returns
 * immediately with a job_id.  This function polls
 * ``GET /api/pipeline/render_status/{job_id}`` every 2 seconds until
 * the job reaches a terminal state (``completed``, ``failed``, or
 * ``cancelled``).
 */
export async function pipelineRenderAll(): Promise<void> {
  if (!state.pipelineBookId) {
    showToast('No book onboarded', 'warning');
    return;
  }

  const btnRender = document.getElementById('btn-pipeline-render');
  const btnRegen = document.getElementById('btn-pipeline-regen');
  const btnCancel = document.getElementById('btn-pipeline-cancel');

  if (btnRender) btnRender.style.display = 'none';
  if (btnRegen) btnRegen.style.display = 'none';
  if (btnCancel) btnCancel.style.display = 'inline-block';

  // A fresh render invalidates any pending span-text undo — the stack must not
  // outlive the span set it would revert against (Plan J, Phase 3).
  clearUndoStack();

  // Fresh render → the bar starts neutral (0%, Ready) until the first tick.
  resetRenderProgress();
  // A fresh render invalidates the previous export — hide + reset the Export
  // M4B card until this render completes (Plan F, Phase 4).
  hideExportM4bForm();

  try {
    // Start the render — returns immediately with a job_id
    const result = await pipelineRenderAudiobook(true);
    _currentRenderJobId = result.job_id;
    showToast(`Render started — job ID: ${result.job_id}`, 'info');

    // Poll for status every 2 seconds
    await new Promise<void>((resolve, reject) => {
      _renderPollTimer = setInterval(async () => {
        if (!_currentRenderJobId) {
          if (_renderPollTimer) clearInterval(_renderPollTimer);
          _renderPollTimer = null;
          resolve();
          return;
        }
        try {
           const status = await pipelineRenderStatus(_currentRenderJobId);
           updateRenderProgress(status);
           if (status.status === 'completed') {
             if (_renderPollTimer) clearInterval(_renderPollTimer);
             _renderPollTimer = null;
             // Store job_id in global state for merge/download
             state.pipelineRenderJobId = _currentRenderJobId;
             showToast('Render complete', 'success');
             resolve();
          } else if (status.status === 'failed') {
            if (_renderPollTimer) clearInterval(_renderPollTimer);
            _renderPollTimer = null;
            reject(new Error(status.error || 'Render failed'));
          } else if (status.status === 'cancelled') {
            if (_renderPollTimer) clearInterval(_renderPollTimer);
            _renderPollTimer = null;
            reject(new Error('Render cancelled'));
          }
          // else: still running — keep polling
        } catch (e) {
          if (_renderPollTimer) clearInterval(_renderPollTimer);
          _renderPollTimer = null;
          reject(e instanceof Error ? e : new Error(String(e)));
        }
      }, 2000);
    });

    // Reveal the full result surface on success (Plan F): download + whole-book
    // play affordances and the Export M4B card (Phase 4).
    if (_currentRenderJobId) revealResultSurface(_currentRenderJobId);

  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    showToast('Render failed: ' + msg, 'error');
  } finally {
    _currentRenderJobId = null;
    await cancelPipelineRender(true);
  }
}

/**
 * Cancel pipeline rendering UI state and (optionally) the active job.
 */
export async function cancelPipelineRender(skipApi = false): Promise<void> {
  const btnRender = document.getElementById('btn-pipeline-render');
  const btnRegen = document.getElementById('btn-pipeline-regen');
  const btnCancel = document.getElementById('btn-pipeline-cancel');

  if (btnRender) btnRender.style.display = 'inline-block';
  if (btnRegen) btnRegen.style.display = 'inline-block';
  if (btnCancel) btnCancel.style.display = 'none';

  if (!skipApi && _currentRenderJobId) {
    try {
      await pipelineCancelRender(_currentRenderJobId);
    } catch (e) {
      console.error('Cancel error:', e);
    }
  }
}

/**
 * Download the rendered audiobook file for the given job.
 * Uses state.pipelineRenderJobId if no jobId is provided.
 */
export async function downloadPipelineRender(jobId?: string): Promise<void> {
  const id = jobId ?? state.pipelineRenderJobId ?? _currentRenderJobId;
  if (!id) {
    showToast('No render job to download', 'warning');
    return;
  }
  // Trigger a browser download via a temporary anchor
  const a = document.createElement('a');
  a.href = pipelineDownloadUrl(id);
  a.download = '';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

/**
 * Merge rendered audio chunks into a single M4B audiobook file.
 * After merge completes, the download button can retrieve the merged file.
 */
export async function mergePipelineAudiobook(): Promise<void> {
  const jobId = state.pipelineRenderJobId ?? _currentRenderJobId;
  if (!jobId) {
    showToast('No render job to merge. Render the audiobook first.', 'warning');
    return;
  }
  if (!state.pipelineBookId) {
    showToast('No book onboarded', 'warning');
    return;
  }

  showToast('Merging audiobook...', 'info');

  try {
    const result = await pipelineMergeAudiobook(jobId);
    if (result.status === 'ok') {
      showToast('Merge complete', 'success');
      // Ensure download button is enabled/visible
      const btnDownload = document.getElementById('btn-pipeline-download');
      if (btnDownload) {
        btnDownload.style.display = 'inline-block';
        btnDownload.setAttribute('data-job-id', jobId);
      }
    } else {
      showToast('Merge returned unexpected status: ' + result.status, 'error');
    }
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    showToast('Merge failed: ' + msg, 'error');
  }
}

/**
 * Play the whole rendered book via GET /api/pipeline/export/audio/{job_id}.
 *
 * Whole-book playback (Plan C contract) works for BOTH individual and batch
 * renders — batch jobs have no per-chunk rows, so this is their only audio
 * surface (see BATCH_PREVIEW_TOOLTIP). Resolves the job id exactly like
 * downloadPipelineRender / mergePipelineAudiobook: an explicit id first, then
 * state.pipelineRenderJobId (set when a render completes), then the
 * module-level _currentRenderJobId (in-flight render). Plays through the
 * injectable singleton player (setPreviewPlayer / getPreviewPlayer).
 *
 * This is the player wiring for whole-book playback only — the result surface
 * UI (progress/cancel/export) is Plan F scope.
 */
export async function playPipelineAudiobook(jobId?: string): Promise<void> {
  const id = jobId ?? state.pipelineRenderJobId ?? _currentRenderJobId;
  if (!id) {
    showToast('No render job to play. Render the audiobook first.', 'warning');
    return;
  }
  try {
    await _previewPlayer.play(`/api/pipeline/export/audio/${id}`);
  } catch (e) {
    // Non-benign playback failure — same catch-with-toast pattern as the
    // other playback entry points (no unhandled rejection from the handler).
    console.error('Playback failed:', e);
    showToast('Playback failed: ' + (e instanceof Error ? e.message : String(e)), 'error');
  }
}

/**
 * Play the spans of the current book as a sequence (player queue of chunk
 * URLs — DD UX Workflow #7).
 *
 * v1 semantics (DD open item #5): the sequence is the PRESENTATION ORDER of
 * the loaded spans (_cachedSpans sorted by global_index). When a non-empty
 * selection exists (_selectedIndices) the sequence is the selected spans
 * sorted by global_index (selection is a play filter, not a reorder).
 * Review-filtered spans are NOT excluded — _cachedSpans is the full export;
 * review items never remove spans from the table.
 *
 * Individual render mode only: batch jobs have NO per-chunk rows, so a batch
 * job shows the same contract tooltip as per-span preview (DD decision #2)
 * and nothing is queued.
 *
 * Chunk URLs use the Phase 4 idx convention (0-based presentation order =
 * global_index − 1, verified from tts_integration.py render_audiobook
 * enumerate(script) → _insert_chunk_row(job_id, i, ...)), confirmed by
 * GET /api/pipeline/export/jobs/{job_id}/chunks when available, else the
 * presentation-order convention stands. Resolves the job id exactly like
 * playPipelineAudiobook (explicit id first, then state.pipelineRenderJobId,
 * then the module-level _currentRenderJobId). Plays through the injectable
 * singleton player's playSequence() (stopThenPlay + auto-advance on 'ended').
 */
export async function playSpanSequence(jobId?: string): Promise<void> {
  const id = jobId ?? state.pipelineRenderJobId ?? _currentRenderJobId;
  if (!id) {
    showToast('No render job to play. Render the audiobook first.', 'warning');
    return;
  }

  // Learn the job mode — batch renders have no per-chunk rows.
  let mode: string | undefined;
  try {
    const status = await pipelineRenderStatus(id);
    mode = status.mode;
  } catch (e) {
    console.error('Error loading render status:', e);
    showToast('Failed to load render status: ' + (e instanceof Error ? e.message : String(e)), 'error');
    return;
  }
  if (mode === 'batch') {
    showToast(BATCH_PREVIEW_TOOLTIP, 'warning');
    return;
  }

  // v1 semantics: selected spans when a selection exists, else all loaded
  // spans — always in presentation order (global_index ascending).
  const spans =
    _selectedIndices.size > 0
      ? _cachedSpans.filter(s => _selectedIndices.has(s.global_index))
      : _cachedSpans;
  const ordered = [...spans].sort((a, b) => a.global_index - b.global_index);
  if (ordered.length === 0) {
    showToast('No spans to play', 'warning');
    return;
  }

  // Chunk row list is authoritative when available; fall back to the
  // presentation-order convention on failure (mirrors handlePreviewSpan).
  let chunkRows: ChunkRow[] | null = null;
  try {
    chunkRows = await API.get<ChunkRow[]>(`/api/pipeline/export/jobs/${id}/chunks`);
  } catch (e) {
    console.error('Error loading chunk list:', e);
  }

  const urls = ordered.map(span => {
    let chunkIdx = span.global_index - 1;
    if (chunkRows) {
      const row = chunkRows.find(r => r.idx === chunkIdx);
      if (row) chunkIdx = row.idx;
    }
    return `/api/pipeline/export/chunk/${id}/${chunkIdx}`;
  });

  try {
    await _previewPlayer.playSequence(urls);
  } catch (e) {
    // Non-benign sequence failure (first URL rejected hard — see player.ts
    // playSequence abort path) — surface it instead of an unhandled rejection.
    console.error('Sequence playback failed:', e);
    showToast('Sequence playback failed: ' + (e instanceof Error ? e.message : String(e)), 'error');
  }
}

// ---------------------------------------------------------------------------
// Export M4B form (Plan F, Phase 4)
// ---------------------------------------------------------------------------

/**
 * Reveal the result surface for a completed/exported render job: the
 * Download button (GET /api/pipeline/download/{job_id}), the whole-book Play
 * affordance (GET /api/pipeline/export/audio/{job_id} — after a successful
 * export the m4b is the served artifact), and the Export M4B card.
 */
function revealResultSurface(jobId: string): void {
  const btnDownload = document.getElementById('btn-pipeline-download');
  if (btnDownload) {
    btnDownload.style.display = 'inline-block';
    btnDownload.setAttribute('data-job-id', jobId);
  }
  const btnPlayBook = document.getElementById('btn-pipeline-play-book');
  if (btnPlayBook) btnPlayBook.style.display = 'inline-block';
  const exportCard = document.getElementById('export-m4b-card');
  if (exportCard) exportCard.style.display = 'block';
}

/**
 * Clear the Export M4B form values and hide the M4B-only info alert.
 * Visibility of the card itself is owned by hideExportM4bForm /
 * revealResultSurface, not by this helper.
 */
function resetExportM4bForm(): void {
  const form = document.getElementById('export-m4b-form') as HTMLFormElement | null;
  if (form) form.reset();
  const infoAlert = document.getElementById('export-m4b-info');
  if (infoAlert) infoAlert.style.display = 'none';
}

/**
 * Render the MP3/Audacity capability affordances from an export response
 * (Plan F, Phase 5 feature-detect; Plan K serving routes).
 *
 * The /export/m4b response IS the capability carrier — there is no separate
 * capability endpoint. The flags drive the affordances:
 *   mp3:true      → MP3 download link visible, href = serving route
 *   audacity:true → Audacity bundle link visible, href = serving route
 *   mp3:false     → MP3 link suppressed + the M4B-only degrade message
 *                   surfaced (response.message, or the frontend default
 *                   'MP3 export unavailable — M4B-only' when the key is
 *                   absent), so the degrade is explicit to the user.
 *
 * URL scheme (Plan K): the hrefs are same-origin serving routes built from
 * the completed render's job id — /api/pipeline/export/mp3/{job_id} and
 * /api/pipeline/export/audacity/{job_id} — served by api_export.py via
 * FileResponse404. The artifact paths in the response (mp3_path /
 * audacity_path) are server-side filesystem locations and are NEVER exposed
 * to the browser; they still gate visibility exactly as before (a link is
 * shown iff its flag AND its path are present — the path is the capability
 * signal, not the href). The `download` attribute lives on the anchors in
 * index.html and is preserved.
 */
function renderExportCapabilities(result: M4bExportResult, jobId: string): void {
  const mp3 = document.getElementById('export-mp3-link') as HTMLAnchorElement | null;
  if (mp3) {
    if (result.mp3 && result.mp3_path) {
      mp3.href = `/api/pipeline/export/mp3/${jobId}`;
      mp3.style.display = 'inline-block';
    } else {
      mp3.style.display = 'none';
      mp3.href = '';
    }
  }
  const audacity = document.getElementById('export-audacity-link') as HTMLAnchorElement | null;
  if (audacity) {
    if (result.audacity && result.audacity_path) {
      audacity.href = `/api/pipeline/export/audacity/${jobId}`;
      audacity.style.display = 'inline-block';
    } else {
      audacity.style.display = 'none';
      audacity.href = '';
    }
  }
  const infoAlert = document.getElementById('export-m4b-info');
  if (infoAlert) {
    if (!result.mp3) {
      infoAlert.textContent = result.message || 'MP3 export unavailable — M4B-only';
      infoAlert.style.display = 'block';
    } else {
      infoAlert.style.display = 'none';
    }
  }
  // Plan L: truthful pause-assembly tri-state (distinct from the M4B-only
  // degrade message). 'applied' → the canonical paused artifact was used; show
  // the resolved values + override count (+ pauses_message when the backend
  // carries it). 'failed' → bounded pauses_error (assembly unavailable; the
  // unpaused concat was exported). Absent (undefined) → no pause surface, no
  // breakage.
  const pauseInfo = document.getElementById('export-pauses-info');
  if (pauseInfo) {
    const applied = result.pauses_state === 'applied' || result.pauses_applied === true;
    if (applied) {
      const between = result.resolved_pause_between_speakers_ms ?? 500;
      const same = result.resolved_pause_same_speaker_ms ?? 250;
      const overrides = result.pause_override_count ?? 0;
      const base =
        `Pauses applied: ${between} ms between speakers · ${same} ms same speaker · ` +
        `${overrides} span override${overrides === 1 ? '' : 's'}.`;
      pauseInfo.textContent = result.pauses_message
        ? `${base} ${result.pauses_message}`
        : base;
      pauseInfo.style.display = 'block';
    } else if (result.pauses_state === 'failed') {
      pauseInfo.textContent = `Pause assembly unavailable: ${
        result.pauses_error ?? 'exported the concatenated source audio without inserted pauses.'
      }`;
      pauseInfo.style.display = 'block';
    } else {
      pauseInfo.textContent = '';
      pauseInfo.style.display = 'none';
    }
  }
}

/**
 * Hide + clear the export capability affordances (Plan F, Phase 5): the MP3
 * and Audacity download links from a previous export never linger. Called on
 * a new render start (hideExportM4bForm) so a stale capability row cannot
 * outlive its job.
 */
function resetExportCapabilities(): void {
  const mp3 = document.getElementById('export-mp3-link') as HTMLAnchorElement | null;
  if (mp3) {
    mp3.style.display = 'none';
    mp3.href = '';
  }
  const audacity = document.getElementById('export-audacity-link') as HTMLAnchorElement | null;
  if (audacity) {
    audacity.style.display = 'none';
    audacity.href = '';
  }
  // Plan L: clear + hide the pause-assembly tri-state surface so a stale
  // result from a previous export cannot outlive its job.
  const pauseInfo = document.getElementById('export-pauses-info');
  if (pauseInfo) {
    pauseInfo.textContent = '';
    pauseInfo.style.display = 'none';
  }
}

/**
 * Hide + reset the export card — called when a new render starts so a stale
 * export form from a previous completed render never lingers (idle until the
 * next completion reveals it again). Also resets the Phase 5 capability row.
 */
function hideExportM4bForm(): void {
  const card = document.getElementById('export-m4b-card');
  if (card) card.style.display = 'none';
  resetExportM4bForm();
  resetExportCapabilities();
}

/**
 * Gather the Export M4B form values and submit the multipart export.
 *
 * Bound to the form's submit event by initExportM4bForm; also exported for
 * direct testing. Requires a completed render (state.pipelineRenderJobId —
 * set when pipelineRenderAll reaches a terminal completed state). On success:
 * success toast, result surface revealed for the exported job, the M4B-only
 * degrade message surfaced when present, and the form reset. On error: the
 * backend ``detail`` is surfaced via an error toast and the form stays usable
 * (values preserved, nothing disabled, no reset).
 */
export async function handleExportM4bSubmit(e?: Event): Promise<void> {
  e?.preventDefault();
  const jobId = state.pipelineRenderJobId;
  if (!jobId) {
    showToast('No completed render to export. Render the audiobook first.', 'warning');
    return;
  }
  const value = (id: string): string =>
    (document.getElementById(id) as HTMLInputElement | null)?.value ?? '';
  const coverInput = document.getElementById('export-m4b-cover') as HTMLInputElement | null;
  const payload: ExportM4bPayload = {
    jobId,
    title: value('export-m4b-title'),
    author: value('export-m4b-author'),
    narrator: value('export-m4b-narrator'),
    year: value('export-m4b-year'),
    description: value('export-m4b-description'),
    cover: coverInput?.files?.[0] ?? null,
  };

  try {
    const result = await pipelineExportM4b(payload);
    showToast('M4B export complete', 'success');
    revealResultSurface(jobId);
    // Reset the consumed fields FIRST, THEN render the capability row —
    // resetExportM4bForm hides the info alert, so a fresh M4B-only message
    // must survive the reset (Plan F Phase 4).
    resetExportM4bForm();
    // Render the MP3/Audacity capability affordances from the response flags
    // (Phase 5 feature-detect: mp3/audacity flags gate the affordances; the
    // hrefs are the Plan K serving routes built from the job id — the
    // M4B-only degrade message is surfaced when mp3 is absent).
    renderExportCapabilities(result, jobId);
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    showToast('Export failed: ' + msg, 'error');
    // Keep the form usable: values stay, nothing disabled, no reset.
  }
}

/**
 * Wire the Export M4B form submit handler (Plan F, Phase 4).
 *
 * Called from initEditor's DOMContentLoaded handler. No-ops when the form is
 * absent from the current DOM — each DOMContentLoaded dispatch wires whatever
 * elements are present at that time (same model as the button wiring in
 * initEditor).
 */
export function initExportM4bForm(): void {
  const form = document.getElementById('export-m4b-form');
  if (!form) return;
  form.addEventListener('submit', handleExportM4bSubmit);
}

// ---------------------------------------------------------------------------
// Getters for testability
// ---------------------------------------------------------------------------

/** Get cached pipeline spans (for testing) */
export function getCachedSpans(): PipelineSpan[] {
  return _cachedSpans;
}

/** Get cached review items (for testing) */
export function getCachedReviewItems(): ReviewItem[] {
  return _cachedReviewItems;
}

/** Get selected span indices (for testing) */
export function getSelectedIndices(): Set<number> {
  return new Set(_selectedIndices);
}
