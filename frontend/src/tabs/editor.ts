/**
 * Editor tab module — Chunk editor with expand/collapse, inline editing, insert/delete/restore,
 * per-chunk audio generation, batch generate, merge, export
 * Ported from app/static/index.html lines 1963-2633
 */

import * as API from '../api';
import { state, type Chunk } from '../state';
import { showToast, showConfirm, escapeHtml } from '../utils';
import { buildSpeakerSelect, updateChunkRow } from '../templates';
import { pollLogs } from './script';

// Module state
let isPlayingSequence = false;
let isRenderingAll = false;
let _lastDeleted: { chunk: Chunk; at_index: number } | null = null;
let _undoTimer: number | null = null;

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
 * Load chunks from the server and render the editor table
 * Supports incremental updates to avoid full redraws during playback
 * @param forceFullRedraw - Force complete table rebuild
 */
export async function loadChunks(forceFullRedraw = false): Promise<void> {
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
      // Only update status badges and progress indicators
      chunks.forEach(chunk => updateChunkRow(chunk));
      state.cachedChunks = chunks;

      // Continue polling if generating
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
      // Incremental update - only update changed rows
      chunks.forEach((chunk, i) => {
        const cached = state.cachedChunks[i];
        if (!cached || cached.status !== chunk.status || cached.audio_path !== chunk.audio_path) {
          updateChunkRow(chunk);
        }
      });
    } else {
      // Full redraw needed
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

    // If any chunk is generating, poll (without full redraw)
    if (chunks.some(c => c.status === 'generating')) {
      setTimeout(() => loadChunks(false), 2000);
    }

  } catch (e) {
    console.error("Error loading chunks:", e);
  }
}

/**
 * Toggle chunk row expand/collapse to show/hide pause_after control
 * @param btn - Toggle button element
 */
function toggleChunkExpand(btn: HTMLElement): void {
  const row = btn.closest('tr');
  if (!row) return;

  const expanding = !row.classList.contains('expanded');
  row.classList.toggle('expanded');

  row.querySelectorAll('.chunk-text, .chunk-instruct').forEach(ta => {
    const textarea = ta as HTMLTextAreaElement;
    if (expanding) {
      // Auto-size to content
      textarea.style.height = 'auto';
      textarea.style.height = textarea.scrollHeight + 'px';
      textarea.style.overflow = 'visible';
    } else {
      // Collapse back to 2 rows
      textarea.style.height = '';
      textarea.style.overflow = '';
    }
  });

  // Show/hide pause_after control
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
 * Insert a new chunk after the specified chunk ID
 * @param id - Chunk ID to insert after
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
 * Delete a chunk with undo support (8 second timeout)
 * @param id - Chunk ID to delete
 */
async function deleteChunk(id: number): Promise<void> {
  try {
    const res = await fetch(`/api/chunks/${id}`, { method: 'DELETE' });
    await API.handleError(res);
    const data = await res.json();

    // Store for undo
    _lastDeleted = { chunk: data.deleted, at_index: id };
    if (_undoTimer !== null) {
      clearTimeout(_undoTimer);
    }

    // Show toast with undo action
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

    // Clear undo data after timeout
    _undoTimer = window.setTimeout(() => { _lastDeleted = null; }, 8000);

    await loadChunks(true);
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    showToast('Failed to delete line: ' + msg, 'error');
  }
}

/**
 * Undo the last chunk deletion
 * @param toastId - Toast element ID to dismiss
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

    // Dismiss the toast
    const el = document.getElementById(toastId);
    if (el) {
      const bootstrap = window.bootstrap;
      if (bootstrap) {
        const toast = bootstrap.Toast.getInstance(el);
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
 * @param id - Chunk ID that is playing
 */
function stopOthers(id: number): void {
  if (isPlayingSequence) return; // Sequence player handles its own logic
  document.querySelectorAll('audio').forEach(audio => {
    if (audio.dataset.id != String(id)) {
      (audio as HTMLAudioElement).pause();
    }
  });
}

/**
 * Play all chunks in sequence with visual highlighting
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

    // Find next valid audio
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

    // Visual feedback
    document.querySelectorAll('tr').forEach(r => r.classList.remove('table-primary'));
    if (tr) {
      tr.classList.add('table-primary');
      tr.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }

    const playPromise = audio.play();

    if (playPromise !== undefined) {
      playPromise.catch(() => {
        // If play fails, move next
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
 * Update a chunk field on the server
 * @param id - Chunk ID
 * @param field - Field name to update
 * @param value - New value
 */
async function updateChunk(id: number, field: string, value: any): Promise<void> {
  try {
    const data: any = {};
    data[field] = value;
    await API.post(`/api/chunks/${id}`, data);
    // Don't reload entire table to preserve focus, but next loadChunks will show updated status
  } catch (e) {
    console.error("Update failed", e);
    showToast("Failed to update chunk", 'error');
  }
}

/**
 * Save all pending edits from a chunk row before generation
 * @param id - Chunk ID
 */
async function saveRowEdits(id: number): Promise<void> {
  const tr = document.querySelector(`tr[data-id="${id}"]`);
  if (!tr) return;

  const inputs = tr.querySelectorAll('input, textarea');
  const data: any = {};

  inputs.forEach(input => {
    const field = (input as HTMLElement).dataset.field;
    if (field) {
      data[field] = (input as HTMLInputElement).value;
    }
  });

  // Coerce pause_after: empty string means clear the override
  if ('pause_after' in data) {
    data.pause_after = data.pause_after === '' ? null : parseInt(data.pause_after);
  }

  // Save all fields at once
  if (Object.keys(data).length > 0) {
    await API.post(`/api/chunks/${id}`, data);
  }
}

/**
 * Generate audio for a single chunk
 * @param id - Chunk ID
 */
async function generateChunk(id: number): Promise<void> {
  try {
    // First, save any pending edits in this row
    await saveRowEdits(id);

    // Skip empty lines
    const tr = document.querySelector(`tr[data-id="${id}"]`);
    if (tr) {
      const textArea = tr.querySelector('.chunk-text') as HTMLTextAreaElement;
      if (textArea && !textArea.value.trim()) {
        showToast('Cannot generate audio for an empty line', 'error');
        return;
      }
    }

    // Optimistic UI update
    if (tr) {
      const statusBadge = tr.querySelector('.badge');
      if (statusBadge) {
        statusBadge.className = 'badge bg-warning';
        statusBadge.textContent = 'generating';
      }

      // Replace button with progress bar
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

    // Start polling with incremental updates (no full redraw)
    setTimeout(() => loadChunks(false), 1000);
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    showToast("Failed to start generation: " + msg, 'error');
    await loadChunks(true); // Revert UI with full redraw
  }
}

/**
 * Cancel the current render operation
 * @param skipApi - Skip the API call (used internally)
 */
async function cancelRender(skipApi = false): Promise<void> {
  isRenderingAll = false;
  const btnBatchFast = document.getElementById('btn-batch-fast');
  const btnRegenAll = document.getElementById('btn-regen-all');
  const btnCancel = document.getElementById('btn-cancel-render');

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
 * Start rendering chunks (batch or regenerate all)
 * @param regenerateAll - If true, regenerate all chunks; otherwise only pending
 */
function startRender(regenerateAll = false): void {
  const ttsModeEl = document.getElementById('tts-mode') as HTMLSelectElement;
  const mode = ttsModeEl?.value || 'external';

  if (mode === 'external') {
    renderAll(regenerateAll);
  } else {
    renderBatchFast(regenerateAll);
  }
}

/**
 * Render all chunks using the batch endpoint
 * @param regenerateAll - If true, regenerate all chunks; otherwise only pending
 */
async function renderAll(regenerateAll = false): Promise<void> {
  isRenderingAll = true;
  const btnBatchFast = document.getElementById('btn-batch-fast');
  const btnRegenAll = document.getElementById('btn-regen-all');
  const btnCancel = document.getElementById('btn-cancel-render');

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

    // Mark all chunks as generating in UI
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

    // Call batch endpoint for parallel processing
    const response = await API.post<{ total_chunks: number; workers: number }>('/api/generate_batch', { indices });

    // Poll for completion
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
          // Clear highlights
          document.querySelectorAll('tr').forEach(r => r.classList.remove('table-info'));
          await cancelRender(true);
          await loadChunks(false);

          // Show completion summary
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
 * Render chunks using the fast batch endpoint
 * @param regenerateAll - If true, regenerate all chunks; otherwise only pending
 */
async function renderBatchFast(regenerateAll = false): Promise<void> {
  isRenderingAll = true;
  const btnBatchFast = document.getElementById('btn-batch-fast');
  const btnRegenAll = document.getElementById('btn-regen-all');
  const btnCancel = document.getElementById('btn-cancel-render');

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

    // Mark all chunks as generating in UI
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

    // Call fast batch endpoint
    const response = await API.post<{ total_chunks: number; workers: number }>('/api/generate_batch_fast', { indices, mode });

    // Poll for completion
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
          // Clear highlights
          document.querySelectorAll('tr').forEach(r => r.classList.remove('table-info'));
          await cancelRender(true);
          await loadChunks(false);

          // Show completion summary
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
    // Switch to Result tab and poll
    const audioTabBtn = document.querySelector('[data-tab="audio"]') as HTMLElement;
    if (audioTabBtn) audioTabBtn.click();
    pollLogs('audio', 'audio-logs');
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    showToast("Merge failed: " + msg, 'error');
  }
}

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

    // Render Pending button
    const btnBatchFast = document.getElementById('btn-batch-fast');
    if (btnBatchFast) {
      btnBatchFast.removeAttribute('onclick');
      btnBatchFast.addEventListener('click', () => startRender(false));
    }

    // Regenerate All button
    const btnRegenAll = document.getElementById('btn-regen-all');
    if (btnRegenAll) {
      btnRegenAll.removeAttribute('onclick');
      btnRegenAll.addEventListener('click', () => startRender(true));
    }

    // Cancel Render button
    const btnCancelRender = document.getElementById('btn-cancel-render');
    if (btnCancelRender) {
      btnCancelRender.removeAttribute('onclick');
      btnCancelRender.addEventListener('click', () => cancelRender());
    }

    // Merge button
    const btnMerge = document.getElementById('btn-merge');
    if (btnMerge) {
      btnMerge.removeAttribute('onclick');
      btnMerge.addEventListener('click', () => mergeAudiobook());
    }

    // Tab-switch handler: load chunks when editor tab is activated
    const editorTabBtn = document.querySelector('[data-tab="editor"]');
    if (editorTabBtn) {
      editorTabBtn.addEventListener('click', () => {
        loadChunks();
      });
    }

    // Event delegation for chunk table actions
    const chunksTableBody = document.getElementById('chunks-table-body');
    if (chunksTableBody) {
      // Click events
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

      // Change events for textareas and inputs
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

      // Play events for audio elements
      chunksTableBody.addEventListener('play', (e) => {
        const target = e.target as HTMLElement;
        if (target.tagName === 'AUDIO' && target.dataset.action === 'stop-others') {
          const id = target.dataset.id ? parseInt(target.dataset.id, 10) : null;
          if (id !== null) stopOthers(id);
        }
      }, true);
    }

    // Event delegation for undo delete chunk links
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
