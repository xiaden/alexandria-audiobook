/**
 * Preparer tab module — Audio preparation UI (TTS-only, no LLM calls)
 * Ported from app/static/index.html lines 4060-4202 (JS logic)
 * HTML: lines 600-691 (preparer-tab)
 */

import * as API from '../api';
import { showToast } from '../utils';

/** Module-level state for preparer tab */
let prepBatchQueue: Array<{ audio: string; file: File }> = [];
let prepPoller: ReturnType<typeof setInterval> | null = null;

/**
 * Toggle between single-file and batch-mode preparation UIs.
 * Reads #prep-batch-mode checkbox state and swaps visibility.
 */
function togglePrepBatchMode(): void {
  const isBatch = (document.getElementById('prep-batch-mode') as HTMLInputElement)?.checked;
  const singleArea = document.getElementById('prep-single-area');
  const batchArea = document.getElementById('prep-batch-area');
  if (singleArea) singleArea.style.display = isBatch ? 'none' : 'block';
  if (batchArea) batchArea.style.display = isBatch ? 'block' : 'none';
}

/**
 * Handle batch file selection — populate the processing queue table.
 * Reads #prep-batch-files input, creates a row per file with pending status badge.
 */
function onPrepBatchFilesChange(): void {
  const fileInput = document.getElementById('prep-batch-files') as HTMLInputElement;
  const tbody = document.getElementById('prep-batch-queue-body');
  const queueContainer = document.getElementById('prep-batch-queue-container');
  if (!fileInput || !tbody || !queueContainer) return;

  const files = fileInput.files;
  tbody.innerHTML = '';
  prepBatchQueue = [];

  if (!files || !files.length) {
    queueContainer.style.display = 'none';
    return;
  }
  queueContainer.style.display = 'block';

  for (let i = 0; i < files.length; i++) {
    const file = files[i];
    const row = document.createElement('tr');
    row.innerHTML = `
      <td class="text-truncate" style="max-width:350px;">${escapeHtmlLocal(file.name)}</td>
      <td id="prep-batch-status-${i}"><span class="badge bg-secondary">Pending</span></td>
    `;
    tbody.appendChild(row);
    prepBatchQueue.push({ audio: file.name, file });
  }
}

/**
 * Start the preparer — dispatches to single or batch mode based on checkbox.
 * Single mode: uploads audio file + config via FormData POST to /api/preparer/start.
 * Batch mode: delegates to _startBatchPreparer().
 */
async function startPreparer(): Promise<void> {
  const isBatch = (document.getElementById('prep-batch-mode') as HTMLInputElement)?.checked;
  if (isBatch) {
    await startBatchPreparer();
    return;
  }

  const fileInput = document.getElementById('prep-audio-file') as HTMLInputElement;
  const audioFile = fileInput?.files?.[0];
  if (!audioFile) {
    showToast('Audio file required', 'error');
    return;
  }

  const btn = document.getElementById('btn-prep-start') as HTMLButtonElement;
  const cancelBtn = document.getElementById('btn-prep-cancel');
  const progressSection = document.getElementById('preparer-progress-section');
  const statusMsg = document.getElementById('prep-status-msg');

  if (btn) btn.disabled = true;
  if (cancelBtn) cancelBtn.style.display = 'inline-block';
  if (progressSection) progressSection.style.display = 'block';
  if (statusMsg) statusMsg.innerHTML = '<span class="text-info">Starting…</span>';

  const config = {
    audio_filename: audioFile.name,
    output_filename: (document.getElementById('prep-output') as HTMLInputElement)?.value || 'my_dataset.zip',
    lang: (document.getElementById('prep-lang') as HTMLSelectElement)?.value || 'en',
    min_confidence: parseFloat((document.getElementById('prep-confidence') as HTMLInputElement)?.value || '0.85'),
    min_snr: parseInt((document.getElementById('prep-snr') as HTMLInputElement)?.value || '25', 10),
  };

  const fd = new FormData();
  fd.append('config_json', JSON.stringify(config));
  fd.append('audio_file', audioFile);

  try {
    const res = await fetch('/api/preparer/start', { method: 'POST', body: fd });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || res.statusText);
    }
    pollPreparerLogs('preparer');
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    showToast('Failed to start: ' + msg, 'error');
    if (btn) btn.disabled = false;
    if (cancelBtn) cancelBtn.style.display = 'none';
  }
}

/**
 * Cancel the current preparer task (single or batch).
 * POSTs to /api/preparer/cancel or /api/preparer/batch/cancel.
 */
async function cancelPreparer(): Promise<void> {
  const isBatch = (document.getElementById('prep-batch-mode') as HTMLInputElement)?.checked;
  const url = isBatch ? '/api/preparer/batch/cancel' : '/api/preparer/cancel';
  try {
    await API.post(url, {});
  } catch {
    /* ignore — cancel is best-effort */
  }
}

/**
 * Start batch preparation — POSTs task list to /api/preparer/batch/start.
 * Each file in prepBatchQueue becomes a task with auto-generated output filename.
 */
async function startBatchPreparer(): Promise<void> {
  if (!prepBatchQueue.length) {
    showToast('No files selected', 'warning');
    return;
  }

  const btn = document.getElementById('btn-prep-start') as HTMLButtonElement;
  const cancelBtn = document.getElementById('btn-prep-cancel');
  const progressSection = document.getElementById('preparer-progress-section');
  const statusMsg = document.getElementById('prep-status-msg');

  if (btn) btn.disabled = true;
  if (cancelBtn) cancelBtn.style.display = 'inline-block';
  if (progressSection) progressSection.style.display = 'block';
  if (statusMsg) statusMsg.innerHTML = '<span class="text-info">Starting batch…</span>';

  const tasks = prepBatchQueue.map(t => ({
    audio_filename: t.audio,
    output_filename: `voice_dataset_${t.audio.replace(/\.[^.]+$/, '')}.zip`,
  }));
  const body = new FormData();
  body.append('tasks', JSON.stringify(tasks));
  body.append('lang', (document.getElementById('prep-lang') as HTMLSelectElement)?.value || 'en');
  body.append('min_confidence', (document.getElementById('prep-confidence') as HTMLInputElement)?.value || '0.85');
  body.append('min_snr', (document.getElementById('prep-snr') as HTMLInputElement)?.value || '25');
  prepBatchQueue.forEach(t => body.append('audio_files', t.file, t.audio));

  try {
    const res = await fetch('/api/preparer/batch/start', { method: 'POST', body });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || res.statusText);
    }
    pollPreparerLogs('batch_preparer');
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    showToast('Failed to start batch: ' + msg, 'error');
    if (btn) btn.disabled = false;
    if (cancelBtn) cancelBtn.style.display = 'none';
  }
}

/**
 * Poll preparer logs and update the UI.
 * @param taskName - 'preparer' for single mode, 'batch_preparer' for batch mode
 */
function pollPreparerLogs(taskName: string): void {
  if (prepPoller) clearInterval(prepPoller);
  const logEl = document.getElementById('preparer-logs');
  let offset = 0;

  prepPoller = setInterval(async () => {
    try {
      const statusData = await API.get<{
        logs: string[];
        running: boolean;
        status?: string;
        tasks?: Array<{ status: string }>;
      }>(`/api/preparer/status/${taskName}`);

      const newLines = statusData.logs.slice(offset);
      offset = statusData.logs.length;

      if (logEl) {
        newLines.forEach(line => {
          const div = document.createElement('div');
          div.textContent = line;
          logEl.appendChild(div);
        });
        logEl.scrollTop = logEl.scrollHeight;
      }

      // Update batch queue status badges
      if (taskName === 'batch_preparer' && statusData.tasks) {
        const colours: Record<string, string> = {
          pending: 'secondary',
          running: 'primary',
          done: 'success',
          failed: 'danger',
          cancelled: 'warning',
        };
        statusData.tasks.forEach((t, i) => {
          const el = document.getElementById(`prep-batch-status-${i}`);
          if (!el) return;
          const colour = colours[t.status] || 'secondary';
          el.innerHTML = `<span class="badge bg-${colour}">${escapeHtmlLocal(t.status)}</span>`;
        });
      }

      if (!statusData.running) {
        clearInterval(prepPoller!);
        prepPoller = null;
        const btn = document.getElementById('btn-prep-start') as HTMLButtonElement;
        const cancelBtn = document.getElementById('btn-prep-cancel');
        const statusMsg = document.getElementById('prep-status-msg');

        if (btn) btn.disabled = false;
        if (cancelBtn) cancelBtn.style.display = 'none';
        const msg = taskName === 'preparer' ? (statusData.status || 'Done') : 'Batch finished';
        if (statusMsg) statusMsg.innerHTML = `<span class="text-muted">${escapeHtmlLocal(msg)}</span>`;
      }
    } catch {
      /* network hiccup — keep polling */
    }
  }, 1000);
}

/**
 * Minimal HTML escaping for dynamic text inserted via innerHTML.
 * Local to this module to avoid circular dependency on utils.ts.
 */
function escapeHtmlLocal(str: unknown): string {
  if (str == null) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/**
 * Initialize the Preparer tab.
 * Attaches event listeners for batch mode toggle, batch file selection,
 * start/cancel buttons. Replaces inline onclick handlers from the monolith.
 */
export function initPreparer(): void {
  document.addEventListener('DOMContentLoaded', () => {
    // Batch mode toggle
    const batchToggle = document.getElementById('prep-batch-mode');
    if (batchToggle) {
      batchToggle.removeAttribute('onchange');
      batchToggle.addEventListener('change', () => togglePrepBatchMode());
    }

    // Batch file selection
    const batchFiles = document.getElementById('prep-batch-files');
    if (batchFiles) {
      batchFiles.removeAttribute('onchange');
      batchFiles.addEventListener('change', () => onPrepBatchFilesChange());
    }

    // Start preparation button
    const btnStart = document.getElementById('btn-prep-start');
    if (btnStart) {
      btnStart.removeAttribute('onclick');
      btnStart.addEventListener('click', () => startPreparer());
    }

    // Cancel preparation button
    const btnCancel = document.getElementById('btn-prep-cancel');
    if (btnCancel) {
      btnCancel.removeAttribute('onclick');
      btnCancel.addEventListener('click', () => cancelPreparer());
    }
  });
}
