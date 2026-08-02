/**
 * Script tab module — File upload, script generation, review, saved scripts, and log polling
 * Ported from app/static/index.html lines 1524-1602 (JS handlers), 2737-2871 (pollLogs, saved scripts)
 */

import * as API from '../api';
import { showToast, showConfirm, escapeHtml } from '../utils';
import { loadChunks } from './editor';
import { loadVoices } from './voices';
import { resetDesignerForm, loadDesignedVoices } from './designer';

/** Status response from /api/status/{taskName} */
interface TaskStatus {
  running: boolean;
  logs: string[];
}

/** Script entry from /api/scripts */
interface SavedScript {
  name: string;
  created: number;
  has_voice_config: boolean;
}

/** Response from /api/review_script_contextual */
interface ContextualReviewResult {
  estimated_calls?: number;
  total_entries?: number;
  batch_size?: number;
}

/** Response from /api/upload */
interface UploadResult {
  filename: string;
}

/**
 * Poll task logs from the server and display them in an element.
 * Polls GET /api/status/{taskName} every 1s until the task is no longer running.
 * Appends logs to the specified element and auto-scrolls to the bottom.
 * @param taskName - Task name to poll (e.g., 'script', 'review', 'audio')
 * @param elementId - DOM element ID to display logs in
 */
export function pollLogs(taskName: string, elementId: string): void {
  const el = document.getElementById(elementId);
  if (!el) return;

  const interval = setInterval(async () => {
    try {
      const status = await API.get<TaskStatus>(`/api/status/${taskName}`);
      el.innerText = status.logs.join('\n');
      el.scrollTop = el.scrollHeight;

      if (!status.running) {
        clearInterval(interval);
        // Audio tab specific: load audio player when complete
        if (taskName === 'audio' && status.logs.some(l => l.includes('complete'))) {
          const audio = document.getElementById('main-audio') as HTMLAudioElement;
          if (audio) {
            audio.src = `/api/audiobook?t=${new Date().getTime()}`;
            const playerContainer = document.getElementById('audio-player-container');
            if (playerContainer) playerContainer.style.display = 'block';
            const downloadLink = document.getElementById('download-link') as HTMLAnchorElement;
            if (downloadLink) downloadLink.href = audio.src;
          }
        }
        // Script/review completion: refresh editor chunks if editor tab is visible
        if ((taskName === 'script' || taskName === 'review') && status.logs.some(l => l.includes('completed successfully'))) {
          // Reload editor chunks to reflect updated script
          const editorTabBtn = document.querySelector('[data-tab="editor"]');
          if (editorTabBtn && (editorTabBtn as HTMLElement).classList.contains('active')) {
            loadChunks();
          }
        }
      }
    } catch (e) {
      console.error('Poll error', e);
      clearInterval(interval);
    }
  }, 1000);
}

/**
 * Load the list of saved scripts from the server and render them.
 * Fetches GET /api/scripts and populates #saved-scripts-list with script entries.
 */
async function loadSavedScripts(): Promise<void> {
  try {
    const scripts = await API.get<SavedScript[]>('/api/scripts');
    const container = document.getElementById('saved-scripts-list');
    if (!container) return;

    if (!scripts.length) {
      container.innerHTML = '<p class="text-muted mb-0">No saved scripts yet.</p>';
      return;
    }

    container.innerHTML = scripts.map(s => {
      const date = new Date(s.created * 1000).toLocaleDateString('en-US', {
        month: 'short', day: 'numeric', year: 'numeric',
      });
      const voiceBadge = s.has_voice_config
        ? '<span class="badge bg-info ms-2" title="Includes voice configuration">voices</span>'
        : '';
      return `
        <div class="d-flex align-items-center justify-content-between py-2 border-bottom">
          <div>
            <strong>${escapeHtml(s.name)}</strong>${voiceBadge}
            <small class="text-muted ms-2">${date}</small>
          </div>
          <div>
            <button class="btn btn-sm btn-outline-success me-1" data-action="load-script" data-name="${escapeHtml(s.name)}"><i class="fas fa-upload me-1"></i>Load</button>
            <button class="btn btn-sm btn-outline-danger" data-action="delete-script" data-name="${escapeHtml(s.name)}"><i class="fas fa-trash"></i></button>
          </div>
        </div>`;
    }).join('');
  } catch (e) {
    console.error('Failed to load saved scripts:', e);
  }
}

/**
 * Save the current script with a user-provided name.
 * Reads name from #save-script-name input, POSTs to /api/scripts/save.
 */
async function saveScript(): Promise<void> {
  const nameInput = document.getElementById('save-script-name') as HTMLInputElement;
  const name = nameInput?.value.trim();
  if (!name) {
    showToast('Please enter a name for the script.', 'warning');
    return;
  }
  try {
    await API.post('/api/scripts/save', { name });
    if (nameInput) nameInput.value = '';
    loadSavedScripts();
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    showToast('Error saving script: ' + msg, 'error');
  }
}

/**
 * Load a saved script by name.
 * Confirms with the user, POSTs to /api/scripts/load, then refreshes related UI.
 * @param name - Script name to load
 */
async function loadScript(name: string): Promise<void> {
  if (!await showConfirm(`Load "${name}"? This will replace your current script and chunks.`)) return;
  try {
    await API.post('/api/scripts/load', { name });
    showToast(`Script "${name}" loaded.`, 'success');
    // Reload dependent tabs
    loadChunks(true);
    loadVoices();
    resetDesignerForm();
    loadDesignedVoices();
    loadSavedScripts();
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    showToast('Error loading script: ' + msg, 'error');
  }
}

/**
 * Delete a saved script by name.
 * Confirms with the user, sends DELETE to /api/scripts/{name}.
 * @param name - Script name to delete
 */
async function deleteScript(name: string): Promise<void> {
  if (!await showConfirm(`Delete saved script "${name}"? This cannot be undone.`)) return;
  try {
    const res = await fetch(`/api/scripts/${encodeURIComponent(name)}`, { method: 'DELETE' });
    if (!res.ok) {
      const err = await res.json();
      showToast(err.detail || 'Failed to delete script.', 'error');
      return;
    }
    loadSavedScripts();
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    showToast('Error deleting script: ' + msg, 'error');
  }
}

/**
 * Initialize the Script tab.
 * Attaches event listeners for file upload, single-speaker toggle, generate/review buttons,
 * and saved scripts actions. Loads saved scripts on init.
 */
export function initScript(): void {
  document.addEventListener('DOMContentLoaded', () => {
    // File upload handler
    const fileInput = document.getElementById('file-upload') as HTMLInputElement;
    if (fileInput) {
      fileInput.addEventListener('change', async () => {
        const statusEl = document.getElementById('upload-status');
        if (!fileInput.files || fileInput.files.length === 0) return;

        if (statusEl) {
          statusEl.innerHTML = '<span class="text-info"><i class="fas fa-spinner fa-spin me-1"></i>Loading file...</span>';
        }
        try {
          const res = await API.upload<UploadResult>(fileInput.files[0]);
          if (statusEl) {
            statusEl.innerHTML = `<span class="text-success"><i class="fas fa-check me-1"></i>Loaded: ${escapeHtml(res.filename)}</span>`;
          }
        } catch (e) {
          const msg = e instanceof Error ? e.message : String(e);
          if (statusEl) {
            statusEl.innerHTML = `<span class="text-danger"><i class="fas fa-times me-1"></i>Failed to load file: ${escapeHtml(msg)}</span>`;
          }
        }
      });
    }

    // Single speaker toggle
    const singleSpeakerToggle = document.getElementById('single-speaker-toggle') as HTMLInputElement;
    if (singleSpeakerToggle) {
      singleSpeakerToggle.addEventListener('change', () => {
        const on = singleSpeakerToggle.checked;
        const optionsEl = document.getElementById('single-speaker-options');
        const hintEl = document.getElementById('btn-gen-script-hint');
        if (optionsEl) optionsEl.style.display = on ? '' : 'none';
        if (hintEl) {
          hintEl.textContent = on
            ? 'Skips the LLM. Splits the book at paragraph boundaries and attributes every chunk to one speaker.'
            : 'Sends the book to your LLM to split it into annotated chunks with speaker labels and voice directions.';
        }
      });
    }

    // Generate script button
    const btnGenScript = document.getElementById('btn-gen-script');
    if (btnGenScript) {
      btnGenScript.addEventListener('click', async () => {
        const statusEl = document.getElementById('upload-status');
        const fileInputEl = document.getElementById('file-upload') as HTMLInputElement;

        // Check if a file has been loaded (status shows success) or if one was previously uploaded
        const hasLoadedFile = statusEl ? statusEl.innerHTML.includes('text-success') : false;

        if (!hasLoadedFile && (!fileInputEl || fileInputEl.files?.length === 0)) {
          if (statusEl) {
            statusEl.innerHTML = '<span class="text-danger"><i class="fas fa-exclamation-triangle me-1"></i>Please select a text file first using the file picker above.</span>';
          }
          return;
        }

        const singleSpeaker = (document.getElementById('single-speaker-toggle') as HTMLInputElement)?.checked;
        const body: Record<string, unknown> = { single_speaker: singleSpeaker };
        if (singleSpeaker) {
          const nameEl = document.getElementById('single-speaker-name') as HTMLInputElement;
          const instructEl = document.getElementById('single-speaker-instruct') as HTMLInputElement;
          body.speaker_name = nameEl?.value.trim() || 'Narrator';
          body.instruct = instructEl?.value.trim() || 'Neutral narration.';
        }

        try {
          await API.post('/api/generate_script', body);
          pollLogs('script', 'script-logs');
        } catch (e) {
          const detail = e instanceof Error ? e.message : String(e);
          if (statusEl) {
            if (detail.includes('No input file')) {
              statusEl.innerHTML = '<span class="text-danger"><i class="fas fa-exclamation-triangle me-1"></i>No file loaded. Please select a text file first.</span>';
            } else {
              statusEl.innerHTML = `<span class="text-danger"><i class="fas fa-times me-1"></i>${escapeHtml(detail)}</span>`;
            }
          }
        }
      });
    }

    // Review script button
    const btnReviewScript = document.getElementById('btn-review-script');
    if (btnReviewScript) {
      btnReviewScript.addEventListener('click', async () => {
        try {
          await API.post('/api/review_script', {});
          pollLogs('review', 'script-logs');
        } catch (e) {
          const msg = e instanceof Error ? e.message : String(e);
          showToast('Failed to start review: ' + msg, 'error');
        }
      });
    }

    // Contextual review button
    const btnReviewContextual = document.getElementById('btn-review-script-contextual');
    if (btnReviewContextual) {
      btnReviewContextual.addEventListener('click', async () => {
        try {
          const windowEl = document.getElementById('review-context-window') as HTMLInputElement;
          const rawWindow = parseInt(windowEl?.value || '4', 10);
          const windowSize = Number.isFinite(rawWindow) ? Math.max(1, Math.min(rawWindow, 12)) : 4;
          const result = await API.post<ContextualReviewResult>('/api/review_script_contextual', { window_size: windowSize });
          const estimateEl = document.getElementById('review-context-estimate');
          if (estimateEl) {
            estimateEl.innerText = result.estimated_calls
              ? `Estimated LLM calls: ~${result.estimated_calls} for ${result.total_entries} entries with batches of ${result.batch_size}.`
              : 'Contextual review started.';
          }
          pollLogs('review', 'script-logs');
        } catch (e) {
          const msg = e instanceof Error ? e.message : String(e);
          showToast('Failed to start contextual review: ' + msg, 'error');
        }
      });
    }

    // Saved scripts: event delegation for load/delete buttons
    const savedScriptsList = document.getElementById('saved-scripts-list');
    if (savedScriptsList) {
      savedScriptsList.addEventListener('click', (e) => {
        const target = e.target as HTMLElement;
        const button = target.closest('button[data-action]') as HTMLButtonElement;
        if (!button) return;

        const action = button.getAttribute('data-action');
        const name = button.getAttribute('data-name');
        if (!name) return;

        if (action === 'load-script') {
          loadScript(name);
        } else if (action === 'delete-script') {
          deleteScript(name);
        }
      });
    }

    // Save script button (remove inline onclick, use addEventListener)
    const saveBtn = document.querySelector('button[onclick="saveScript()"]');
    if (saveBtn) {
      saveBtn.removeAttribute('onclick');
      saveBtn.addEventListener('click', () => saveScript());
    }

    // Load saved scripts on init
    loadSavedScripts();
  });
}
