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
  return API.post('/api/pipeline/cancel_render', { job_id: jobId });
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

// ---------------------------------------------------------------------------
// Module state
// ---------------------------------------------------------------------------

/** Cached pipeline spans for the current book */
let _cachedSpans: PipelineSpan[] = [];

/** Cached review items */
let _cachedReviewItems: ReviewItem[] = [];

/** Currently selected spans for merge/move operations */
let _selectedIndices: Set<number> = new Set();

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
    return;
  }

  if (tbody.children.length === 0) {
    tbody.innerHTML = '<tr><td colspan="7" class="text-center">Loading spans...</td></tr>';
  }

  try {
    const raw = await pipelineExportSpans();
    const spans = toPipelineSpans(raw);
    _cachedSpans = spans;

    if (spans.length === 0) {
      tbody.innerHTML = '<tr><td colspan="7" class="text-center text-muted">No spans found. Run all walks in the Script tab first.</td></tr>';
      return;
    }

    // Update progress bar
    const total = spans.length;
    const progressBar = document.getElementById('full-progress-bar');
    if (progressBar) {
      progressBar.style.width = '100%';
      progressBar.innerText = `${total} spans loaded`;
    }

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

    // Show download button on success
    const btnDownload = document.getElementById('btn-pipeline-download');
    if (btnDownload && _currentRenderJobId) {
      btnDownload.style.display = 'inline-block';
      btnDownload.setAttribute('data-job-id', _currentRenderJobId);
    }

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
