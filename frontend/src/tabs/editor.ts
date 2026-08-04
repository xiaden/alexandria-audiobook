/**
 * Editor tab module — Routing layer for span/pipeline and legacy/chunk editing.
 *
 * This file is the thin routing layer that:
 *   1. Re-exports all public API from editor-pipeline and editor-legacy
 *      so that existing importers (script.ts, tests) continue to work.
 *   2. Contains `initEditor()` which wires up DOM event listeners,
 *      delegating to the appropriate sub-module functions.
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

import { state } from '../state';
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
  // Getters
  getCachedSpans,
  getCachedReviewItems,
  getSelectedIndices,
} from './editor-pipeline';

// Import legacy module functions
import {
  loadChunks,
  toggleChunkExpand,
  insertChunkAfter,
  deleteChunk,
  undoDeleteChunk,
  stopOthers,
  playSequence,
  updateChunk,
  generateChunk,
  cancelRender,
  startRender,
  mergeAudiobook,
} from './editor-legacy';

// ---------------------------------------------------------------------------
// Re-exports for backward compatibility
// ---------------------------------------------------------------------------
// script.ts imports loadChunks from './editor'
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
};

// Re-export pipeline getters
export {
  getCachedSpans,
  getCachedReviewItems,
  getSelectedIndices,
};

// Re-export legacy functions (loadChunks is imported by script.ts)
export {
  loadChunks,
  mergeAudiobook,
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
          loadSpans();
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
          const cachedSpans = getCachedSpans();
          const toStr = prompt(`Move span #${index} to position (0-${cachedSpans.length - 1}):`);
          if (toStr !== null) {
            const toIndex = parseInt(toStr, 10);
            if (!isNaN(toIndex) && toIndex >= 0 && toIndex < cachedSpans.length) {
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
