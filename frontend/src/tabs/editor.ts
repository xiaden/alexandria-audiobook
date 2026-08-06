/**
 * Editor tab module — Pipeline span editor.
 *
 * This file is the thin routing layer that:
 *   1. Re-exports the public API from editor-pipeline so that existing
 *      importers (script.ts, tests) continue to work.
 *   2. Contains `initEditor()` which wires up DOM event listeners for the
 *      pipeline span editor.
 *
 * The editor operates against the /api/pipeline/* endpoints only:
 *   - Loads spans via GET /api/pipeline/export/{book_id}
 *   - Operations via POST /api/pipeline/operation (split/merge/move/delete)
 *   - Confidence review via GET /api/pipeline/review/{book_id}
 *   - Render via POST /api/pipeline/render
 */

import { showToast } from '../utils';

// Import pipeline module functions
import {
  // Types
  type PipelineSpan,
  type ReviewItem,
  // API functions
  pipelineOperation,
  pipelineReviewItems,
  pipelineReviewAccept,
  pipelineReviewReject,
  pipelineReviewOverride,
  pipelineRenderAudiobook,
  pipelineExportSpans,
  pipelineUpdateSpanText,
  // Span display
  toPipelineSpans,
  loadSpans,
  renderSpanRow,
  // Operations
  handleSplit,
  handleMerge,
  handleMove,
  handleDelete,
  toggleSpanSelection,
  // Review UI
  loadReviewItems,
  renderReviewItem,
  handleReviewAccept,
  handleReviewReject,
  handleReviewOverride,
  // TTS rendering
  pipelineRenderAll,
  cancelPipelineRender,
  downloadPipelineRender,
  mergePipelineAudiobook,
  // Getters
  getCachedSpans,
  getCachedReviewItems,
  getSelectedIndices,
} from './editor-pipeline';

// ---------------------------------------------------------------------------
// Re-exports for backward compatibility
// ---------------------------------------------------------------------------
// Tests import all pipeline functions from '../../src/tabs/editor'

// Re-export pipeline types
export type { PipelineSpan, ReviewItem };

// Re-export pipeline API functions
export {
  pipelineOperation,
  pipelineReviewItems,
  pipelineReviewAccept,
  pipelineReviewReject,
  pipelineReviewOverride,
  pipelineRenderAudiobook,
  pipelineExportSpans,
};

// Re-export pipeline span display
export {
  toPipelineSpans,
  loadSpans,
  renderSpanRow,
};

// Re-export pipeline operations
export {
  handleSplit,
  handleMerge,
  handleMove,
  handleDelete,
  toggleSpanSelection,
};

// Re-export pipeline review UI
export {
  loadReviewItems,
  renderReviewItem,
  handleReviewAccept,
  handleReviewReject,
  handleReviewOverride,
};

// Re-export pipeline TTS rendering
export {
  pipelineRenderAll,
  cancelPipelineRender,
  downloadPipelineRender,
};

// Re-export pipeline getters
export {
  getCachedSpans,
  getCachedReviewItems,
  getSelectedIndices,
};

// ---------------------------------------------------------------------------
// Initialization
// ---------------------------------------------------------------------------

/**
 * Initialize the Editor tab
 * Attaches event listeners to buttons and sets up tab-switch handler
 */
export function initEditor(): void {
  document.addEventListener('DOMContentLoaded', () => {
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

    // Pipeline Download Audiobook button
    const btnPipelineDownload = document.getElementById('btn-pipeline-download');
    if (btnPipelineDownload) {
      btnPipelineDownload.addEventListener('click', () => downloadPipelineRender());
    }

    // Pipeline Merge Audiobook button
    const btnPipelineMergeAudiobook = document.getElementById('btn-pipeline-merge-audiobook');
    if (btnPipelineMergeAudiobook) {
      btnPipelineMergeAudiobook.addEventListener('click', () => mergePipelineAudiobook());
    }

    // Pipeline merge button (for merging selected spans)
    const btnPipelineMerge = document.getElementById('btn-pipeline-merge');
    if (btnPipelineMerge) {
      btnPipelineMerge.addEventListener('click', () => handleMerge());
    }

    // Tab-switch handler: load spans when editor tab is activated
    const editorTabBtn = document.querySelector('[data-tab="editor"]');
    if (editorTabBtn) {
      editorTabBtn.addEventListener('click', () => {
        loadSpans();
        loadReviewItems();
      });
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
          const cachedSpans = getCachedSpans();
          const toStr = prompt(`Move span #${index} to position (1-${cachedSpans.length}):`);
          if (toStr !== null) {
            const toIndex = parseInt(toStr, 10);
            if (!isNaN(toIndex) && toIndex >= 1 && toIndex <= cachedSpans.length) {
              handleMove(toIndex);
            } else {
              showToast('Invalid target position', 'error');
            }
          }
        } else if (btn.classList.contains('btn-span-delete')) {
          handleDelete(index);
        }
      });

      // Inline text editing: blur handler for contenteditable span-text elements
      spansTableBody.addEventListener('focusout', async (e) => {
        const target = e.target as HTMLElement;
        if (!target.classList.contains('span-text')) return;

        const spanId = target.dataset.spanId;
        if (!spanId) return;

        const newText = (target.textContent || '').trim();
        if (!newText) {
          showToast('Span text cannot be empty', 'error');
          // Restore original text from cache
          const cachedSpans = getCachedSpans();
          const idx = parseInt(target.dataset.index || '0', 10);
          const span = cachedSpans.find(s => s.global_index === idx);
          if (span) {
            target.textContent = span.text;
          }
          return;
        }

        try {
          await pipelineUpdateSpanText(spanId, newText);
          // Update local cache
          const cachedSpans = getCachedSpans();
          const idx = parseInt(target.dataset.index || '0', 10);
          const span = cachedSpans.find(s => s.global_index === idx);
          if (span) {
            span.text = newText;
          }
          showToast('Span text updated', 'success');
        } catch (err) {
          console.error('Failed to update span text:', err);
          showToast('Failed to update span text', 'error');
          // Restore original text
          const cachedSpans = getCachedSpans();
          const idx = parseInt(target.dataset.index || '0', 10);
          const span = cachedSpans.find(s => s.global_index === idx);
          if (span) {
            target.textContent = span.text;
          }
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
  });
}
