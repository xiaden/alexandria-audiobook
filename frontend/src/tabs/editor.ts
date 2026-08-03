/**
 * Editor tab module — Span editor with pipeline operations, confidence review,
 * and TTS rendering. Supports both pipeline mode (presentation-index-based)
 * and legacy mode (chunk-based editing).
 *
 * Pipeline mode (state.pipelineEnabled = true):
 *   - Loads spans via GET /api/pipeline/export/{book_id}
 *   - Operations via POST /api/pipeline/operation (split/merge/move/delete)
 *   - Confidence review via GET /api/pipeline/review/{book_id}
 *   - Render via POST /api/pipeline/render
 *
 * Legacy mode (state.pipelineEnabled = false):
 *   - Loads chunks via GET /api/chunks
 *   - Operations via /api/chunks/{id}/* endpoints
 *   - Render via /api/generate_batch and /api/generate_batch_fast
 */

import * as API from '../api';
import { state, type Chunk } from '../state';
import { showToast, showConfirm, escapeHtml } from '../utils';
import { buildSpeakerSelect, updateChunkRow } from '../templates';
import { pollLogs } from './script';

// ---------------------------------------------------------------------------
// Pipeline types
// ---------------------------------------------------------------------------

/** A span from the pipeline export endpoint */
export interface PipelineSpan {
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

// ---------------------------------------------------------------------------
// Pipeline API functions (P6-S1)
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
): Promise<{ job_id: string }> {
  return API.post('/api/pipeline/render', {
    book_id: state.pipelineBookId,
    use_batch: useBatch,
    batch_seed: batchSeed ?? null,
  });
}

/**
 * Export the annotated script for the current book (pipeline spans)
 */
export async function pipelineExportSpans(): Promise<Array<{ speaker: string; text: string; instruct: string | null }>> {
  if (!state.pipelineBookId) return [];
  return API.get(`/api/pipeline/export/${state.pipelineBookId}`);
}

// ---------------------------------------------------------------------------
// Module state
// ---------------------------------------------------------------------------

let isPlayingSequence = false;
let isRenderingAll = false;
let _lastDeleted: { chunk: Chunk; at_index: number } | null = null;
let _undoTimer: number | null = null;

/** Cached pipeline spans for the current book */
let _cachedSpans: PipelineSpan[] = [];

/** Cached review items */
let _cachedReviewItems: ReviewItem[] = [];

/** Currently selected spans for merge/move operations */
let _selectedIndices: Set<number> = new Set();

// ---------------------------------------------------------------------------
// Pipeline span loading and display (P6-S2, P6-S4)
// ---------------------------------------------------------------------------

/**
 * Convert raw export data to PipelineSpan array with global_index
 */
export function toPipelineSpans(
  raw: Array<{ speaker: string; text: string; instruct: string | null }>,
): PipelineSpan[] {
  return raw.map((item, idx) => ({
    global_index: idx,
    speaker: item.speaker || '',
    text: item.text || '',
    instruct: item.instruct || '',
  }));
}

/**
 * Load spans from the pipeline and render the editor table (pipeline mode)
 */
export async function loadSpans(forceFullRedraw = false): Promise<void> {
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

  return `
    <tr data-index="${span.global_index}" class="span-row ${rowClass}">
      <td class="text-center align-middle" style="white-space:nowrap;">
        <span class="badge bg-secondary me-1" title="Presentation index">#${span.global_index}</span>
        <button class="btn btn-sm btn-outline-info btn-select-span" data-index="${span.global_index}" title="Select for merge/move">
          <i class="fas fa-check-square"></i>
        </button>
      </td>
      <td><span class="fw-bold">${escapeHtml(span.speaker)}</span></td>
      <td><div class="span-text">${escapeHtml(span.text)}</div></td>
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
// Pipeline operations (P6-S3)
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
    await loadSpans(true);
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
    await loadSpans(true);
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
    await loadSpans(true);
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
    await loadSpans(true);
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
// Confidence review UI (P6-S5)
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
// TTS rendering — pipeline mode (P6-S6)
// ---------------------------------------------------------------------------

/**
 * Render the audiobook using the pipeline render endpoint
 */
export async function pipelineRenderAll(): Promise<void> {
  if (!state.pipelineBookId) {
    showToast('No book onboarded', 'warning');
    return;
  }

  isRenderingAll = true;
  const btnRender = document.getElementById('btn-pipeline-render');
  const btnRegen = document.getElementById('btn-pipeline-regen');
  const btnCancel = document.getElementById('btn-pipeline-cancel');

  if (btnRender) btnRender.style.display = 'none';
  if (btnRegen) btnRegen.style.display = 'none';
  if (btnCancel) btnCancel.style.display = 'inline-block';

  try {
    const result = await pipelineRenderAudiobook(true);
    showToast(`Render complete — job ID: ${result.job_id}`, 'success');

    // Show job ID to user
    const jobDisplay = document.getElementById('pipeline-render-job');
    if (jobDisplay) {
      jobDisplay.textContent = `Job: ${result.job_id}`;
      jobDisplay.classList.remove('d-none');
    }

    // Render completed synchronously — restore controls immediately
    await cancelPipelineRender(true);

  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    showToast('Render failed: ' + msg, 'error');
    await cancelPipelineRender(true);
  }
}

/**
 * Cancel pipeline rendering UI state
 */
async function cancelPipelineRender(skipApi = false): Promise<void> {
  isRenderingAll = false;
  const btnRender = document.getElementById('btn-pipeline-render');
  const btnRegen = document.getElementById('btn-pipeline-regen');
  const btnCancel = document.getElementById('btn-pipeline-cancel');

  if (btnRender) btnRender.style.display = 'inline-block';
  if (btnRegen) btnRegen.style.display = 'inline-block';
  if (btnCancel) btnCancel.style.display = 'none';

  if (!skipApi) {
    try {
      await API.post('/api/cancel_audio', {});
    } catch (e) {
      console.error('Cancel error:', e);
    }
  }
}

// ---------------------------------------------------------------------------
// Legacy chunk-based functions (preserved for non-pipeline mode)
// ---------------------------------------------------------------------------

/**
 * Check if any audio is currently playing
 */
function isAudioPlaying(): boolean {
  const audios = document.querySelectorAll('audio');
  for (const audio of audios) {
    if (!audio.paused && !audio.ended) return true;
  }
  return false;
}

/**
 * Load chunks from the server and render the editor table (legacy mode)
 */
export async function loadChunks(forceFullRedraw = false): Promise<void> {
  // If pipeline mode is active, delegate to loadSpans
  if (state.pipelineEnabled) {
    await loadSpans(forceFullRedraw);
    return;
  }

  const tbody = document.getElementById('chunks-table-body');
  if (!tbody) return;

  // Show loading only if empty
  if (tbody.children.length === 0 || (tbody.children.length === 1 && tbody.children[0].children.length === 1)) {
    tbody.innerHTML = '<tr><td colspan="6" class="text-center">Loading chunks...</td></tr>';
    forceFullRedraw = true;
  }

  try {
    const chunks = await API.get<Chunk[]>('/api/chunks');
    if (chunks.length === 0) {
      tbody.innerHTML = '<tr><td colspan="6" class="text-center">No chunks found. Please generate script first.</td></tr>';
      state.cachedChunks = [];
      return;
    }

    // Update Full Progress Bar
    const completed = chunks.filter(c => c.status === 'done').length;
    const total = chunks.length;
    const percentage = total > 0 ? Math.round((completed / total) * 100) : 0;
    const progressBar = document.getElementById('full-progress-bar');
    if (progressBar) {
      progressBar.style.width = `${percentage}%`;
      progressBar.innerText = `${percentage}% (${completed}/${total})`;
    }

    // Skip redraw if playing audio (unless forced)
    if (!forceFullRedraw && (isPlayingSequence || isAudioPlaying())) {
      chunks.forEach(chunk => updateChunkRow(chunk));
      state.cachedChunks = chunks;

      if (chunks.some(c => c.status === 'generating')) {
        setTimeout(() => loadChunks(false), 2000);
      }
      return;
    }

    // Check if we can do incremental update
    const canIncrement = !forceFullRedraw &&
                        state.cachedChunks.length === chunks.length &&
                        tbody.children.length === chunks.length;

    if (canIncrement) {
      chunks.forEach((chunk, i) => {
        const cached = state.cachedChunks[i];
        if (!cached || cached.status !== chunk.status || cached.audio_path !== chunk.audio_path) {
          updateChunkRow(chunk);
        }
      });
    } else {
      tbody.innerHTML = chunks.map(chunk => {
        const statusColor = chunk.status === 'done' ? 'success' :
                          chunk.status === 'generating' ? 'warning' :
                          chunk.status === 'error' ? 'danger' : 'secondary';

        const audioPlayer = chunk.audio_path ?
          `<audio class="chunk-audio" data-id="${chunk.id}" controls src="/${chunk.audio_path}?t=${Date.now()}" style="width: 200px; height: 30px;" data-action="stop-others"></audio>` :
          '<span class="text-muted small">No audio</span>';

        const actionArea = chunk.status === 'generating' ?
          `<div class="progress" style="width: 100px; height: 20px;">
            <div class="progress-bar progress-bar-striped progress-bar-animated bg-warning" role="progressbar" style="width: 100%"></div>
           </div>` :
          `<button class="btn btn-sm btn-primary" data-action="generate-chunk" data-chunk-id="${chunk.id}"><i class="fas fa-play"></i> Gen</button>`;

        return `
          <tr data-id="${chunk.id}" class="chunk-row">
            <td class="text-center align-middle" style="white-space:nowrap;">
              <button class="chunk-action-btn chunk-toggle-btn" data-action="toggle-chunk-expand" title="Expand/collapse"><i class="fas fa-chevron-down"></i></button>
              <button class="chunk-action-btn" data-action="insert-chunk-after" data-chunk-id="${chunk.id}" title="Insert line below"><i class="fas fa-plus"></i></button>
              <button class="chunk-action-btn" data-action="delete-chunk" data-chunk-id="${chunk.id}" title="Delete line"><i class="fas fa-trash" style="color:#dc3545;"></i></button>
            </td>
            <td>${buildSpeakerSelect(chunk)}</td>
            <td><textarea class="form-control form-control-sm chunk-text" rows="2" data-action="update-chunk" data-chunk-id="${chunk.id}" data-field="text">${escapeHtml(chunk.text)}</textarea></td>
            <td>
              <textarea class="form-control form-control-sm chunk-instruct" rows="2" data-action="update-chunk" data-chunk-id="${chunk.id}" data-field="instruct" title="Short TTS direction (3-8 words)">${escapeHtml(chunk.instruct || '')}</textarea>
              <div class="chunk-pause-row d-none mt-1 align-items-center gap-1">
                <small class="text-muted text-nowrap">Pause after (ms):</small>
                <input type="number" class="form-control form-control-sm chunk-pause-after" style="width:80px;" value="${chunk.pause_after ?? ''}" placeholder="default" min="0" step="50" data-action="update-chunk-pause" data-chunk-id="${chunk.id}">
              </div>
            </td>
            <td><span class="badge bg-${statusColor}">${escapeHtml(chunk.status)}</span></td>
            <td>
              <div class="d-flex align-items-center gap-2">
                ${actionArea}
                ${audioPlayer}
              </div>
            </td>
          </tr>
        `;
      }).join('');
    }

    state.cachedChunks = chunks;

    if (chunks.some(c => c.status === 'generating')) {
      setTimeout(() => loadChunks(false), 2000);
    }

  } catch (e) {
    console.error("Error loading chunks:", e);
  }
}

/**
 * Toggle chunk row expand/collapse (legacy mode)
 */
function toggleChunkExpand(btn: HTMLElement): void {
  const row = btn.closest('tr');
  if (!row) return;

  const expanding = !row.classList.contains('expanded');
  row.classList.toggle('expanded');

  row.querySelectorAll('.chunk-text, .chunk-instruct').forEach(ta => {
    const textarea = ta as HTMLTextAreaElement;
    if (expanding) {
      textarea.style.height = 'auto';
      textarea.style.height = textarea.scrollHeight + 'px';
      textarea.style.overflow = 'visible';
    } else {
      textarea.style.height = '';
      textarea.style.overflow = '';
    }
  });

  row.querySelectorAll('.chunk-pause-row').forEach(el => {
    if (expanding) {
      el.classList.remove('d-none');
      el.classList.add('d-flex');
    } else {
      el.classList.remove('d-flex');
      el.classList.add('d-none');
    }
  });
}

/**
 * Insert a new chunk after the specified chunk ID (legacy mode)
 */
async function insertChunkAfter(id: number): Promise<void> {
  try {
    await API.post(`/api/chunks/${id}/insert`, {});
    await loadChunks(true);
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    showToast('Failed to insert line: ' + msg, 'error');
  }
}

/**
 * Delete a chunk with undo support (legacy mode)
 */
async function deleteChunk(id: number): Promise<void> {
  try {
    const res = await fetch(`/api/chunks/${id}`, { method: 'DELETE' });
    await API.handleError(res);
    const data = await res.json();

    _lastDeleted = { chunk: data.deleted, at_index: id };
    if (_undoTimer !== null) {
      clearTimeout(_undoTimer);
    }

    const toastId = 'toast-undo-' + Date.now();
    const container = document.getElementById('toast-container');
    if (container) {
      container.insertAdjacentHTML('beforeend', `
        <div id="${toastId}" class="toast align-items-center text-white bg-warning border-0" role="alert">
          <div class="d-flex">
            <div class="toast-body text-dark">
              Line deleted (${escapeHtml(data.deleted.speaker)}: "${escapeHtml((data.deleted.text || '').substring(0, 40))}...")
              <a href="#" class="ms-2 fw-bold text-dark" data-action="undo-delete-chunk" data-toast-id="${toastId}">Undo</a>
            </div>
            <button type="button" class="btn-close me-2 m-auto" data-bs-dismiss="toast"></button>
          </div>
        </div>`);
      const el = document.getElementById(toastId);
      const bootstrap = window.bootstrap;
      if (el && bootstrap) {
        const toast = new bootstrap.Toast(el, { delay: 8000 });
        toast.show();
        el.addEventListener('hidden.bs.toast', () => { el.remove(); });
      }
    }

    _undoTimer = window.setTimeout(() => { _lastDeleted = null; }, 8000);

    await loadChunks(true);
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    showToast('Failed to delete line: ' + msg, 'error');
  }
}

/**
 * Undo the last chunk deletion (legacy mode)
 */
async function undoDeleteChunk(toastId: string): Promise<void> {
  if (!_lastDeleted) {
    showToast('Nothing to undo', 'warning');
    return;
  }

  try {
    await API.post('/api/chunks/restore', {
      chunk: _lastDeleted.chunk,
      at_index: _lastDeleted.at_index
    });

    const el = document.getElementById(toastId);
    if (el) {
      const bootstrap = window.bootstrap;
      if (bootstrap && 'getInstance' in bootstrap.Toast) {
        const toast = (bootstrap.Toast as any).getInstance(el);
        if (toast) toast.hide();
      }
    }

    _lastDeleted = null;
    if (_undoTimer !== null) {
      clearTimeout(_undoTimer);
      _undoTimer = null;
    }
    showToast('Line restored', 'success');
    await loadChunks(true);
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    showToast('Undo failed: ' + msg, 'error');
  }
}

/**
 * Stop all other audio players when one starts playing
 */
function stopOthers(id: number): void {
  if (isPlayingSequence) return;
  document.querySelectorAll('audio').forEach(audio => {
    if (audio.dataset.id != String(id)) {
      (audio as HTMLAudioElement).pause();
    }
  });
}

/**
 * Play all chunks in sequence with visual highlighting (legacy mode)
 */
async function playSequence(): Promise<void> {
  isPlayingSequence = true;
  const btn = document.getElementById('btn-play-seq');
  if (btn) {
    btn.innerHTML = '<i class="fas fa-stop me-1"></i>Stop';
    btn.removeEventListener('click', playSequence);
    btn.addEventListener('click', stopSequence);
    btn.classList.replace('btn-primary', 'btn-danger');
  }

  const audios = Array.from(document.querySelectorAll('.chunk-audio')) as HTMLAudioElement[];
  if (audios.length === 0) {
    stopSequence();
    return;
  }

  let currentIndex = 0;

  const playNext = () => {
    if (!isPlayingSequence) return;

    while (currentIndex < audios.length) {
      const audio = audios[currentIndex];
      if (audio.getAttribute('src')) {
        break;
      }
      currentIndex++;
    }

    if (currentIndex >= audios.length) {
      stopSequence();
      return;
    }

    const audio = audios[currentIndex];
    const tr = audio.closest('tr');

    document.querySelectorAll('tr').forEach(r => r.classList.remove('table-primary'));
    if (tr) {
      tr.classList.add('table-primary');
      tr.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }

    const playPromise = audio.play();

    if (playPromise !== undefined) {
      playPromise.catch(() => {
        currentIndex++;
        playNext();
      });
    }

    audio.onended = () => {
      currentIndex++;
      playNext();
    };

    audio.onerror = () => {
      currentIndex++;
      playNext();
    };
  };

  playNext();
}

/**
 * Stop the sequence playback
 */
function stopSequence(): void {
  isPlayingSequence = false;
  document.querySelectorAll('audio').forEach(a => {
    const audio = a as HTMLAudioElement;
    audio.pause();
    audio.currentTime = 0;
    audio.onended = null;
  });
  document.querySelectorAll('tr').forEach(r => r.classList.remove('table-primary'));

  const btn = document.getElementById('btn-play-seq');
  if (btn) {
    btn.innerHTML = '<i class="fas fa-play me-1"></i>Play Sequence';
    btn.removeEventListener('click', stopSequence);
    btn.addEventListener('click', playSequence);
    btn.classList.replace('btn-danger', 'btn-primary');
  }
}

/**
 * Update a chunk field on the server (legacy mode)
 */
async function updateChunk(id: number, field: string, value: unknown): Promise<void> {
  try {
    const data: Record<string, unknown> = {};
    data[field] = value;
    await API.post(`/api/chunks/${id}`, data);
  } catch (e) {
    console.error("Update failed", e);
    showToast("Failed to update chunk", 'error');
  }
}

/**
 * Save all pending edits from a chunk row before generation (legacy mode)
 */
async function saveRowEdits(id: number): Promise<void> {
  const tr = document.querySelector(`tr[data-id="${id}"]`);
  if (!tr) return;

  const inputs = tr.querySelectorAll('input, textarea');
  const data: Record<string, unknown> = {};

  inputs.forEach(input => {
    const field = (input as HTMLElement).dataset.field;
    if (field) {
      data[field] = (input as HTMLInputElement).value;
    }
  });

  if ('pause_after' in data) {
    data.pause_after = data.pause_after === '' ? null : parseInt(data.pause_after as string);
  }

  if (Object.keys(data).length > 0) {
    await API.post(`/api/chunks/${id}`, data);
  }
}

/**
 * Generate audio for a single chunk (legacy mode)
 */
async function generateChunk(id: number): Promise<void> {
  try {
    await saveRowEdits(id);

    const tr = document.querySelector(`tr[data-id="${id}"]`);
    if (tr) {
      const textArea = tr.querySelector('.chunk-text') as HTMLTextAreaElement;
      if (textArea && !textArea.value.trim()) {
        showToast('Cannot generate audio for an empty line', 'error');
        return;
      }
    }

    if (tr) {
      const statusBadge = tr.querySelector('.badge');
      if (statusBadge) {
        statusBadge.className = 'badge bg-warning';
        statusBadge.textContent = 'generating';
      }

      const container = tr.querySelector('.d-flex');
      const btn = container?.querySelector('button');
      if (btn && container) {
        const progressBar = document.createElement('div');
        progressBar.className = 'progress';
        progressBar.style.width = '100px';
        progressBar.style.height = '20px';
        progressBar.innerHTML = '<div class="progress-bar progress-bar-striped progress-bar-animated bg-warning" role="progressbar" style="width: 100%"></div>';
        container.replaceChild(progressBar, btn);
      }
    }

    await API.post(`/api/chunks/${id}/generate`, {});

    setTimeout(() => loadChunks(false), 1000);
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    showToast("Failed to start generation: " + msg, 'error');
    await loadChunks(true);
  }
}

/**
 * Cancel the current render operation (legacy mode)
 */
async function cancelRender(skipApi = false): Promise<void> {
  isRenderingAll = false;
  const btnBatchFast = document.getElementById('btn-batch-fast-legacy');
  const btnRegenAll = document.getElementById('btn-regen-all-legacy');
  const btnCancel = document.getElementById('btn-cancel-render-legacy');

  if (btnBatchFast) btnBatchFast.style.display = 'inline-block';
  if (btnRegenAll) btnRegenAll.style.display = 'inline-block';
  if (btnCancel) btnCancel.style.display = 'none';

  if (!skipApi) {
    try {
      await API.post('/api/cancel_audio', {});
      await loadChunks(false);
    } catch (e) {
      console.error('Cancel error:', e);
    }
  }
}

/**
 * Start rendering chunks (legacy mode)
 */
function startRender(regenerateAll = false): void {
  // If pipeline mode, use pipeline render
  if (state.pipelineEnabled) {
    pipelineRenderAll();
    return;
  }

  const ttsModeEl = document.getElementById('tts-mode') as HTMLSelectElement;
  const mode = ttsModeEl?.value || 'external';

  if (mode === 'external') {
    renderAll(regenerateAll);
  } else {
    renderBatchFast(regenerateAll);
  }
}

/**
 * Render all chunks using the batch endpoint (legacy mode)
 */
async function renderAll(regenerateAll = false): Promise<void> {
  isRenderingAll = true;
  const btnBatchFast = document.getElementById('btn-batch-fast-legacy');
  const btnRegenAll = document.getElementById('btn-regen-all-legacy');
  const btnCancel = document.getElementById('btn-cancel-render-legacy');

  if (btnBatchFast) btnBatchFast.style.display = 'none';
  if (btnRegenAll) btnRegenAll.style.display = 'none';
  if (btnCancel) btnCancel.style.display = 'inline-block';

  try {
    const chunks = await API.get<Chunk[]>('/api/chunks');
    const toProcess = (regenerateAll ? chunks : chunks.filter(c => c.status !== 'done'))
      .filter(c => c.text && c.text.trim());

    if (toProcess.length === 0) {
      showToast("No non-empty chunks to render!", 'warning');
      await cancelRender(true);
      return;
    }

    if (regenerateAll && !await showConfirm(`Regenerate all ${toProcess.length} non-empty chunks? This will replace existing audio.`)) {
      await cancelRender(true);
      return;
    }

    const indices = toProcess.map(c => c.id);
    for (const id of indices) {
      const tr = document.querySelector(`tr[data-id="${id}"]`);
      if (tr) {
        tr.classList.add('table-info');
        const badge = tr.querySelector('.badge');
        if (badge) {
          badge.className = 'badge bg-warning';
          badge.textContent = 'generating';
        }
      }
    }

    const response = await API.post<{ total_chunks: number; workers: number }>('/api/generate_batch', { indices });

    const pollInterval = setInterval(async () => {
      if (!isRenderingAll) {
        clearInterval(pollInterval);
        return;
      }

      try {
        await loadChunks(false);
        const updated = await API.get<Chunk[]>('/api/chunks');
        const stillGenerating = updated.filter(c =>
          indices.includes(c.id) && c.status === 'generating'
        );

        if (stillGenerating.length === 0) {
          clearInterval(pollInterval);
          document.querySelectorAll('tr').forEach(r => r.classList.remove('table-info'));
          await cancelRender(true);
          await loadChunks(false);

          const completed = updated.filter(c => indices.includes(c.id) && c.status === 'done').length;
          const failed = updated.filter(c => indices.includes(c.id) && c.status === 'error').length;
          if (failed > 0) {
            showToast(`Batch complete: ${completed} succeeded, ${failed} failed`, 'warning');
          }
        }
      } catch (e) {
        console.error("Polling error", e);
      }
    }, 2000);

  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    console.error("Render All error:", e);
    showToast("Error during batch rendering: " + msg, 'error');
    await cancelRender(true);
  }
}

/**
 * Render chunks using the fast batch endpoint (legacy mode)
 */
async function renderBatchFast(regenerateAll = false): Promise<void> {
  isRenderingAll = true;
  const btnBatchFast = document.getElementById('btn-batch-fast-legacy');
  const btnRegenAll = document.getElementById('btn-regen-all-legacy');
  const btnCancel = document.getElementById('btn-cancel-render-legacy');

  if (btnBatchFast) btnBatchFast.style.display = 'none';
  if (btnRegenAll) btnRegenAll.style.display = 'none';
  if (btnCancel) btnCancel.style.display = 'inline-block';

  try {
    const chunks = await API.get<Chunk[]>('/api/chunks');
    const toProcess = (regenerateAll ? chunks : chunks.filter(c => c.status !== 'done'))
      .filter(c => c.text && c.text.trim());

    if (toProcess.length === 0) {
      showToast("No non-empty chunks to render!", 'warning');
      await cancelRender(true);
      return;
    }

    if (regenerateAll && !await showConfirm(`Regenerate all ${toProcess.length} non-empty chunks? This will replace existing audio.`)) {
      await cancelRender(true);
      return;
    }

    const indices = toProcess.map(c => c.id);
    for (const id of indices) {
      const tr = document.querySelector(`tr[data-id="${id}"]`);
      if (tr) {
        tr.classList.add('table-info');
        const badge = tr.querySelector('.badge');
        if (badge) {
          badge.className = 'badge bg-warning';
          badge.textContent = 'generating';
        }
      }
    }

    const ttsModeEl = document.getElementById('tts-mode') as HTMLSelectElement;
    const mode = ttsModeEl?.value || 'local';

    const response = await API.post<{ total_chunks: number; workers: number }>('/api/generate_batch_fast', { indices, mode });

    const pollInterval = setInterval(async () => {
      if (!isRenderingAll) {
        clearInterval(pollInterval);
        return;
      }

      try {
        await loadChunks(false);
        const updated = await API.get<Chunk[]>('/api/chunks');
        const stillGenerating = updated.filter(c =>
          indices.includes(c.id) && c.status === 'generating'
        );

        if (stillGenerating.length === 0) {
          clearInterval(pollInterval);
          document.querySelectorAll('tr').forEach(r => r.classList.remove('table-info'));
          await cancelRender(true);
          await loadChunks(false);

          const completed = updated.filter(c => indices.includes(c.id) && c.status === 'done').length;
          const failed = updated.filter(c => indices.includes(c.id) && c.status === 'error').length;
          if (failed > 0) {
            showToast(`Batch complete: ${completed} succeeded, ${failed} failed`, 'warning');
          }
        }
      } catch (e) {
        console.error("Polling error", e);
      }
    }, 2000);

  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    console.error("Batch Fast error:", e);
    showToast("Error during batch rendering: " + msg, 'error');
    await cancelRender(true);
  }
}

/**
 * Merge all chunks into final audiobook
 */
async function mergeAudiobook(): Promise<void> {
  if (!await showConfirm("Merge all valid audio chunks into final audiobook?")) return;

  try {
    await API.post('/api/merge', {});
    const audioTabBtn = document.querySelector('[data-tab="audio"]') as HTMLElement;
    if (audioTabBtn) audioTabBtn.click();
    pollLogs('audio', 'audio-logs');
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    showToast("Merge failed: " + msg, 'error');
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

// ---------------------------------------------------------------------------
// Initialization
// ---------------------------------------------------------------------------

/**
 * Initialize the Editor tab
 * Attaches event listeners to buttons and sets up tab-switch handler
 */
export function initEditor(): void {
  document.addEventListener('DOMContentLoaded', () => {
    // Play Sequence button
    const btnPlaySeq = document.getElementById('btn-play-seq');
    if (btnPlaySeq) {
      btnPlaySeq.removeAttribute('onclick');
      btnPlaySeq.addEventListener('click', playSequence);
    }

    // Legacy Render Pending button
    const btnBatchFast = document.getElementById('btn-batch-fast-legacy');
    if (btnBatchFast) {
      btnBatchFast.removeAttribute('onclick');
      btnBatchFast.addEventListener('click', () => startRender(false));
    }

    // Legacy Regenerate All button
    const btnRegenAll = document.getElementById('btn-regen-all-legacy');
    if (btnRegenAll) {
      btnRegenAll.removeAttribute('onclick');
      btnRegenAll.addEventListener('click', () => startRender(true));
    }

    // Legacy Cancel Render button
    const btnCancelRender = document.getElementById('btn-cancel-render-legacy');
    if (btnCancelRender) {
      btnCancelRender.removeAttribute('onclick');
      btnCancelRender.addEventListener('click', () => cancelRender());
    }

    // Legacy Merge Audiobook button
    const btnMerge = document.getElementById('btn-merge-legacy');
    if (btnMerge) {
      btnMerge.removeAttribute('onclick');
      btnMerge.addEventListener('click', () => mergeAudiobook());
    }

    // Pipeline Render button
    const btnPipelineRender = document.getElementById('btn-pipeline-render');
    if (btnPipelineRender) {
      btnPipelineRender.addEventListener('click', () => pipelineRenderAll());
    }

    // Pipeline Re-render All button
    const btnPipelineRegen = document.getElementById('btn-pipeline-regen');
    if (btnPipelineRegen) {
      btnPipelineRegen.addEventListener('click', () => pipelineRenderAll());
    }

    // Pipeline Cancel Render button
    const btnCancelPipeline = document.getElementById('btn-pipeline-cancel');
    if (btnCancelPipeline) {
      btnCancelPipeline.addEventListener('click', () => cancelPipelineRender());
    }

    // Pipeline Merge Audiobook button
    const btnPipelineMergeAudiobook = document.getElementById('btn-pipeline-merge-audiobook');
    if (btnPipelineMergeAudiobook) {
      btnPipelineMergeAudiobook.addEventListener('click', () => mergeAudiobook());
    }

    // Pipeline merge button (for merging selected spans)
    const btnPipelineMerge = document.getElementById('btn-pipeline-merge');
    if (btnPipelineMerge) {
      btnPipelineMerge.addEventListener('click', () => handleMerge());
    }

    // Tab-switch handler: load chunks/spans when editor tab is activated
    const editorTabBtn = document.querySelector('[data-tab="editor"]');
    if (editorTabBtn) {
      editorTabBtn.addEventListener('click', () => {
        if (state.pipelineEnabled) {
          loadSpans(true);
          loadReviewItems();
        } else {
          loadChunks();
        }
      });
    }

    // Event delegation for legacy chunk table actions
    const chunksTableBody = document.getElementById('chunks-table-body');
    if (chunksTableBody) {
      chunksTableBody.addEventListener('click', (e) => {
        const target = e.target as HTMLElement;
        const actionEl = target.closest('[data-action]') as HTMLElement;
        if (!actionEl) return;

        const action = actionEl.dataset.action;
        const chunkId = actionEl.dataset.chunkId ? parseInt(actionEl.dataset.chunkId, 10) : null;

        switch (action) {
          case 'toggle-chunk-expand':
            toggleChunkExpand(actionEl);
            break;
          case 'insert-chunk-after':
            if (chunkId !== null) insertChunkAfter(chunkId);
            break;
          case 'delete-chunk':
            if (chunkId !== null) deleteChunk(chunkId);
            break;
          case 'generate-chunk':
            if (chunkId !== null) generateChunk(chunkId);
            break;
        }
      });

      chunksTableBody.addEventListener('change', (e) => {
        const target = e.target as HTMLElement;
        const actionEl = target.closest('[data-action]') as HTMLElement;
        if (!actionEl) return;

        const action = actionEl.dataset.action;
        const chunkId = actionEl.dataset.chunkId ? parseInt(actionEl.dataset.chunkId, 10) : null;

        if (action === 'update-chunk' && chunkId !== null) {
          const field = actionEl.dataset.field;
          const value = (target as HTMLInputElement | HTMLTextAreaElement).value;
          if (field) updateChunk(chunkId, field, value);
        } else if (action === 'update-chunk-pause' && chunkId !== null) {
          const input = target as HTMLInputElement;
          const value = input.value === '' ? null : parseInt(input.value, 10);
          updateChunk(chunkId, 'pause_after', value);
        } else if (action === 'update-chunk-speaker' && chunkId !== null) {
          const value = (target as HTMLSelectElement).value;
          updateChunk(chunkId, 'speaker', value);
        }
      });

      chunksTableBody.addEventListener('play', (e) => {
        const target = e.target as HTMLElement;
        if (target.tagName === 'AUDIO' && target.dataset.action === 'stop-others') {
          const id = target.dataset.id ? parseInt(target.dataset.id, 10) : null;
          if (id !== null) stopOthers(id);
        }
      }, true);
    }

    // Event delegation for pipeline span table actions
    const spansTableBody = document.getElementById('spans-table-body');
    if (spansTableBody) {
      spansTableBody.addEventListener('click', (e) => {
        const target = e.target as HTMLElement;
        const btn = target.closest('button') as HTMLElement;
        if (!btn) return;

        const index = btn.dataset.index !== undefined ? parseInt(btn.dataset.index, 10) : null;
        if (index === null) return;

        if (btn.classList.contains('btn-select-span')) {
          toggleSpanSelection(index);
        } else if (btn.classList.contains('btn-span-split')) {
          handleSplit(index);
        } else if (btn.classList.contains('btn-span-move')) {
          // For move: prompt for target index
          const toStr = prompt(`Move span #${index} to position (0-${_cachedSpans.length - 1}):`);
          if (toStr !== null) {
            const toIndex = parseInt(toStr, 10);
            if (!isNaN(toIndex) && toIndex >= 0 && toIndex < _cachedSpans.length) {
              handleMove(toIndex);
            } else {
              showToast('Invalid target position', 'error');
            }
          }
        } else if (btn.classList.contains('btn-span-delete')) {
          handleDelete(index);
        }
      });
    }

    // Event delegation for review item actions
    const reviewContainer = document.getElementById('review-items-container');
    if (reviewContainer) {
      reviewContainer.addEventListener('click', (e) => {
        const target = e.target as HTMLElement;
        const btn = target.closest('button') as HTMLElement;
        if (!btn) return;

        const itemId = btn.dataset.itemId;
        if (!itemId) return;

        if (btn.classList.contains('btn-review-accept')) {
          handleReviewAccept(itemId);
        } else if (btn.classList.contains('btn-review-reject')) {
          handleReviewReject(itemId);
        } else if (btn.classList.contains('btn-review-override')) {
          handleReviewOverride(itemId);
        }
      });
    }

    // Event delegation for undo delete chunk links (legacy mode)
    document.addEventListener('click', (e) => {
      const target = e.target as HTMLElement;
      const link = target.closest('[data-action="undo-delete-chunk"]') as HTMLElement;
      if (link) {
        e.preventDefault();
        const toastId = link.dataset.toastId;
        if (toastId) undoDeleteChunk(toastId);
      }
    });
  });
}
