/**
 * Pipeline-mode editor functions.
 *
 * This module contains the span-based editing code that operates against the
 * pipeline /api/pipeline/* endpoints. It is used when `state.pipelineEnabled`
 * is true.
 *
 * When pipeline mode is inactive, the editor-legacy module handles chunk-based
 * editing instead. The routing layer (editor.ts) decides which module to call.
 *
 * Pipeline endpoints used:
 *   - POST /api/pipeline/operation (split/merge/move/delete)
 *   - GET  /api/pipeline/review/{book_id}
 *   - POST /api/pipeline/review/accept
 *   - POST /api/pipeline/review/reject
 *   - POST /api/pipeline/review/override
 *   - GET  /api/pipeline/export/{book_id}
 *   - POST /api/pipeline/render
 *   - POST /api/cancel_audio
 */

import * as API from '../api';
import { state } from '../state';
import { showToast, showConfirm, escapeHtml } from '../utils';
import { setIsRenderingAll } from './editor-legacy';

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

/** Cached pipeline spans for the current book */
let _cachedSpans: PipelineSpan[] = [];

/** Cached review items */
let _cachedReviewItems: ReviewItem[] = [];

/** Currently selected spans for merge/move operations */
let _selectedIndices: Set<number> = new Set();

// ---------------------------------------------------------------------------
// Pipeline span loading and display
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

/**
 * Render the audiobook using the pipeline render endpoint
 */
export async function pipelineRenderAll(): Promise<void> {
  if (!state.pipelineBookId) {
    showToast('No book onboarded', 'warning');
    return;
  }

  setIsRenderingAll(true);
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
export async function cancelPipelineRender(skipApi = false): Promise<void> {
  setIsRenderingAll(false);
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
