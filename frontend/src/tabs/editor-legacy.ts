/**
 * Legacy chunk-based editor functions.
 *
 * This module contains the original chunk-based editing code that operates
 * against the legacy /api/chunks/* endpoints. It is used when
 * `state.pipelineEnabled` is false (the default).
 *
 * When pipeline mode is active, the editor-pipeline module handles span-based
 * editing instead. The routing layer (editor.ts) decides which module to call.
 *
 * Legacy endpoints used:
 *   - GET  /api/chunks
 *   - POST /api/chunks/{id}
 *   - POST /api/chunks/{id}/insert
 *   - POST /api/chunks/{id}/generate
 *   - DELETE /api/chunks/{id}
 *   - POST /api/chunks/restore
 *   - POST /api/generate_batch
 *   - POST /api/generate_batch_fast
 *   - POST /api/merge
 *   - POST /api/cancel_audio
 */

import * as API from '../api';
import { state, type Chunk } from '../state';
import { showToast, showConfirm, escapeHtml } from '../utils';
import { buildSpeakerSelect, updateChunkRow } from '../templates';
import { pollLogs } from './script';

// ---------------------------------------------------------------------------
// Module state
// ---------------------------------------------------------------------------

/** Whether a sequence playback is in progress */
let isPlayingSequence = false;

/** Whether a batch render is in progress */
export let isRenderingAll = false;

/** Last deleted chunk info for undo support */
let _lastDeleted: { chunk: Chunk; at_index: number } | null = null;

/** Timer ID for undo toast auto-dismiss */
let _undoTimer: number | null = null;

// ---------------------------------------------------------------------------
// Setters for module state (used by initEditor in editor.ts)
// ---------------------------------------------------------------------------

/** Set the isRenderingAll flag */
export function setIsRenderingAll(value: boolean): void {
  isRenderingAll = value;
}

// ---------------------------------------------------------------------------
// Internal helpers
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

// ---------------------------------------------------------------------------
// Chunk loading and display
// ---------------------------------------------------------------------------

/**
 * Load chunks from the server and render the editor table (legacy mode)
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

// ---------------------------------------------------------------------------
// Chunk operations
// ---------------------------------------------------------------------------

/**
 * Toggle chunk row expand/collapse (legacy mode)
 */
export function toggleChunkExpand(btn: HTMLElement): void {
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
export async function insertChunkAfter(id: number): Promise<void> {
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
export async function deleteChunk(id: number): Promise<void> {
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
export async function undoDeleteChunk(toastId: string): Promise<void> {
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
export function stopOthers(id: number): void {
  if (isPlayingSequence) return;
  document.querySelectorAll('audio').forEach(audio => {
    if (audio.dataset.id != String(id)) {
      (audio as HTMLAudioElement).pause();
    }
  });
}

// ---------------------------------------------------------------------------
// Sequence playback
// ---------------------------------------------------------------------------

/**
 * Play all chunks in sequence with visual highlighting (legacy mode)
 */
export async function playSequence(): Promise<void> {
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
export function stopSequence(): void {
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

// ---------------------------------------------------------------------------
// Chunk field updates
// ---------------------------------------------------------------------------

/**
 * Update a chunk field on the server (legacy mode)
 */
export async function updateChunk(id: number, field: string, value: unknown): Promise<void> {
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
export async function saveRowEdits(id: number): Promise<void> {
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

// ---------------------------------------------------------------------------
// Audio generation
// ---------------------------------------------------------------------------

/**
 * Generate audio for a single chunk (legacy mode)
 */
export async function generateChunk(id: number): Promise<void> {
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
export async function cancelRender(skipApi = false): Promise<void> {
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
export function startRender(regenerateAll = false): void {
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
export async function renderAll(regenerateAll = false): Promise<void> {
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

    await API.post<{ total_chunks: number; workers: number }>('/api/generate_batch', { indices });

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
export async function renderBatchFast(regenerateAll = false): Promise<void> {
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

    await API.post<{ total_chunks: number; workers: number }>('/api/generate_batch_fast', { indices, mode });

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

// ---------------------------------------------------------------------------
// Merge
// ---------------------------------------------------------------------------

/**
 * Merge all chunks into final audiobook
 */
export async function mergeAudiobook(): Promise<void> {
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
