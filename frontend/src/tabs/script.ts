/**
 * Script tab module — Pipeline onboard, walk execution, walk status display,
 * saved scripts, and log polling.
 *
 * When state.pipelineEnabled is true, the tab shows the pipeline UI:
 *   - Onboard EPUB → POST /api/pipeline/onboard
 *   - Walk execution buttons (individual + Run All)
 *   - Walk status display with polling
 *   - Re-onboard button
 *
 * When state.pipelineEnabled is false, a notice is shown directing the user
 * to enable pipeline mode in the Setup tab (old endpoints have been removed).
 *
 * Preserved from previous version:
 *   - File upload handler (still uses /api/upload for initial file load)
 *   - Saved scripts list (load, save, delete)
 *   - pollLogs (used by Audio tab and still available for script logs)
 */

import * as API from '../api';
import { showToast, showConfirm, escapeHtml } from '../utils';
import { state } from '../state';
import { loadChunks } from './editor';
import { loadVoices } from './voices';
import { resetDesignerForm, loadDesignedVoices } from './designer';
import { WALK_ORDER, WALK_DISPLAY_NAMES } from '../pipeline/walks';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

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

/** Response from /api/upload */
interface UploadResult {
  filename: string;
}

/** Response from POST /api/pipeline/onboard */
interface OnboardResult {
  book_id: string;
  series_id: string;
  chapters: number;
}

/** Response from POST /api/pipeline/reonboard */
interface ReonboardResult {
  book_id: string;
  version: number;
  status: string;
}

/** Walk status map: walk_name → 'pending' | 'running' | 'completed' | 'failed' */
type WalkStatusMap = Record<string, string>;

// ---------------------------------------------------------------------------
// Module-level state
// ---------------------------------------------------------------------------

/** Current book ID from the last successful onboard. */
let currentBookId: string | null = null;

/** Interval ID for walk status polling. */
let walkStatusInterval: ReturnType<typeof setInterval> | null = null;

// ---------------------------------------------------------------------------
// Pipeline API functions
// ---------------------------------------------------------------------------

/**
 * Upload an EPUB file to the pipeline onboard endpoint.
 * POST /api/pipeline/onboard — accepts multipart form data with 'file' field.
 * @param file - EPUB file to upload
 * @returns Onboard result with book_id, series_id, chapters
 */
export async function pipelineOnboard(file: File): Promise<OnboardResult> {
  const formData = new FormData();
  formData.append('file', file);
  const res = await fetch('/api/pipeline/onboard', {
    method: 'POST',
    body: formData,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(body.detail || res.statusText);
  }
  return res.json();
}

/**
 * Run a single walk for a book.
 * POST /api/pipeline/run_walk
 * @param walkName - Walk name (e.g. 'walk_2a_scene_segmentation')
 * @param bookId - Book UUID
 * @param config - Optional walk configuration
 */
export async function pipelineRunWalk(
  walkName: string,
  bookId: string,
  config: Record<string, unknown> = {},
): Promise<unknown> {
  return API.post('/api/pipeline/run_walk', {
    walk_name: walkName,
    book_id: bookId,
    config,
  });
}

/**
 * Run all 9 walks serially for a book.
 * POST /api/pipeline/run_all_walks
 * @param bookId - Book UUID
 * @param config - Optional walk configuration
 */
export async function pipelineRunAllWalks(
  bookId: string,
  config: Record<string, unknown> = {},
): Promise<unknown> {
  return API.post('/api/pipeline/run_all_walks', {
    book_id: bookId,
    config,
  });
}

/**
 * Get walk status for a book.
 * GET /api/pipeline/walk_status/{book_id}
 * @param bookId - Book UUID
 * @returns Map of walk_name → status string
 */
export async function pipelineWalkStatus(bookId: string): Promise<WalkStatusMap> {
  return API.get<WalkStatusMap>(`/api/pipeline/walk_status/${bookId}`);
}

/**
 * Re-onboard a book: clear walk outputs, bump version.
 * POST /api/pipeline/reonboard
 * @param bookId - Book UUID
 */
export async function pipelineReonboard(bookId: string): Promise<ReonboardResult> {
  return API.post<ReonboardResult>('/api/pipeline/reonboard', {
    book_id: bookId,
  });
}

// ---------------------------------------------------------------------------
// Walk status display
// ---------------------------------------------------------------------------

/**
 * Render the walk status list into #walk-status-container.
 * Each walk is shown with its human-readable label and a status badge.
 * @param statuses - Map of walk_name → status
 */
export function renderWalkStatuses(statuses: WalkStatusMap): void {
  const container = document.getElementById('walk-status-container');
  if (!container) return;

  container.innerHTML = WALK_ORDER.map(walkName => {
    const status = statuses[walkName] || 'pending';
    const label = WALK_DISPLAY_NAMES[walkName] || walkName;
    const badgeClass =
      status === 'completed' ? 'bg-success' :
      status === 'running' ? 'bg-warning text-dark' :
      status === 'failed' ? 'bg-danger' :
      'bg-secondary';
    const icon =
      status === 'completed' ? '<i class="fas fa-check me-1"></i>' :
      status === 'running' ? '<i class="fas fa-spinner fa-spin me-1"></i>' :
      status === 'failed' ? '<i class="fas fa-times me-1"></i>' :
      '<i class="fas fa-clock me-1"></i>';

    return `
      <div class="d-flex align-items-center justify-content-between py-1 border-bottom" data-walk="${escapeHtml(walkName)}">
        <span class="small">${escapeHtml(label)}</span>
        <span class="badge ${badgeClass}">${icon}${escapeHtml(status)}</span>
      </div>`;
  }).join('');
}

/**
 * Start polling walk status for the current book.
 * Polls GET /api/pipeline/walk_status/{book_id} every 2 seconds.
 * Stops when all walks are completed or failed (no more 'running').
 */
export function startWalkPolling(): void {
  stopWalkPolling();
  if (!currentBookId) return;

  const poll = async (): Promise<void> => {
    if (!currentBookId) return;
    try {
      const statuses = await pipelineWalkStatus(currentBookId);
      renderWalkStatuses(statuses);
      updateWalkButtons(statuses);

      // Stop polling if no walks are running
      const anyRunning = Object.values(statuses).some(s => s === 'running');
      if (!anyRunning) {
        stopWalkPolling();
        updateRunAllButton(false);
      }
    } catch (e) {
      console.error('Walk status poll error', e);
      stopWalkPolling();
    }
  };

  // Immediate first poll, then interval
  poll();
  walkStatusInterval = setInterval(poll, 2000);
}

/**
 * Stop walk status polling.
 */
export function stopWalkPolling(): void {
  if (walkStatusInterval !== null) {
    clearInterval(walkStatusInterval);
    walkStatusInterval = null;
  }
}

/**
 * Enable/disable individual walk run buttons based on current statuses.
 * A walk button is disabled if that walk is currently 'running'.
 */
function updateWalkButtons(statuses: WalkStatusMap): void {
  for (const walkName of WALK_ORDER) {
    const btn = document.querySelector(
      `button[data-walk-run="${walkName}"]`,
    ) as HTMLButtonElement | null;
    if (btn) {
      btn.disabled = statuses[walkName] === 'running';
    }
  }
}

/**
 * Enable/disable the "Run All Walks" button.
 * @param running - Whether walks are currently running
 */
function updateRunAllButton(running: boolean): void {
  const btn = document.getElementById('btn-run-all-walks') as HTMLButtonElement | null;
  if (!btn) return;
  btn.disabled = running;
  btn.innerHTML = running
    ? '<i class="fas fa-spinner fa-spin me-2"></i>Walks Running...'
    : '<i class="fas fa-play me-2"></i>Run All Walks';
}

// ---------------------------------------------------------------------------
// Pipeline event handlers
// ---------------------------------------------------------------------------

/**
 * Handle the "Onboard EPUB" button click.
 * Reads the file from #file-upload, POSTs to /api/pipeline/onboard,
 * stores the book_id, and renders walk status UI.
 */
async function handleOnboard(): Promise<void> {
  const fileInput = document.getElementById('file-upload') as HTMLInputElement;
  const statusEl = document.getElementById('upload-status');

  if (!fileInput?.files || fileInput.files.length === 0) {
    if (statusEl) {
      statusEl.innerHTML = '<span class="text-danger"><i class="fas fa-exclamation-triangle me-1"></i>Please select an EPUB file first.</span>';
    }
    return;
  }

  const file = fileInput.files[0];
  if (!file.name.toLowerCase().endsWith('.epub')) {
    if (statusEl) {
      statusEl.innerHTML = '<span class="text-danger"><i class="fas fa-exclamation-triangle me-1"></i>Pipeline onboard requires an EPUB file (.epub).</span>';
    }
    return;
  }

  if (statusEl) {
    statusEl.innerHTML = '<span class="text-info"><i class="fas fa-spinner fa-spin me-1"></i>Onboarding EPUB...</span>';
  }

  try {
    const result = await pipelineOnboard(file);
    currentBookId = result.book_id;
    state.pipelineBookId = result.book_id;

    if (statusEl) {
      statusEl.innerHTML = `<span class="text-success"><i class="fas fa-check me-1"></i>Onboarded: ${escapeHtml(file.name)} — Book ID: <code>${escapeHtml(result.book_id)}</code> (${result.chapters} chapters)</span>`;
    }

    // Show walk execution UI
    showWalkExecutionUI();

    // Fetch initial walk status
    startWalkPolling();

    showToast(`EPUB onboarded successfully. Book ID: ${result.book_id}`, 'success');
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    if (statusEl) {
      statusEl.innerHTML = `<span class="text-danger"><i class="fas fa-times me-1"></i>Onboard failed: ${escapeHtml(msg)}</span>`;
    }
    showToast('Onboard failed: ' + msg, 'error');
  }
}

/**
 * Show the walk execution UI (walk status list, run buttons, re-onboard).
 * Called after a successful onboard.
 */
function showWalkExecutionUI(): void {
  const walkSection = document.getElementById('walk-execution-section');
  if (walkSection) {
    walkSection.style.display = '';
  }

  // Render initial walk statuses (all pending)
  const initialStatuses: WalkStatusMap = {};
  for (const walkName of WALK_ORDER) {
    initialStatuses[walkName] = 'pending';
  }
  renderWalkStatuses(initialStatuses);
}

/**
 * Handle a single walk "Run" button click.
 * @param walkName - The walk to run
 */
async function handleRunWalk(walkName: string): Promise<void> {
  if (!currentBookId) {
    showToast('No book onboarded yet. Please onboard an EPUB first.', 'warning');
    return;
  }

  const label = WALK_DISPLAY_NAMES[walkName] || walkName;
  const btn = document.querySelector(
    `button[data-walk-run="${walkName}"]`,
  ) as HTMLButtonElement | null;
  if (btn) btn.disabled = true;

  try {
    await pipelineRunWalk(walkName, currentBookId);
    showToast(`Walk "${label}" started.`, 'info');
    startWalkPolling();
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    showToast(`Failed to start walk "${label}": ${msg}`, 'error');
    if (btn) btn.disabled = false;
  }
}

/**
 * Handle the "Run All Walks" button click.
 * POSTs to /api/pipeline/run_all_walks and starts polling.
 */
async function handleRunAllWalks(): Promise<void> {
  if (!currentBookId) {
    showToast('No book onboarded yet. Please onboard an EPUB first.', 'warning');
    return;
  }

  updateRunAllButton(true);

  try {
    await pipelineRunAllWalks(currentBookId);
    showToast('All walks completed.', 'success');
    startWalkPolling();
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    showToast('Run all walks failed: ' + msg, 'error');
    updateRunAllButton(false);
  }
}

/**
 * Handle the "Re-onboard" button click.
 * Confirms with user, POSTs to /api/pipeline/reonboard, resets walk state.
 */
async function handleReonboard(): Promise<void> {
  if (!currentBookId) {
    showToast('No book onboarded yet.', 'warning');
    return;
  }

  if (!await showConfirm(
    `Re-onboard book ${currentBookId}? This will clear all walk outputs and create a new version. This cannot be undone.`,
  )) return;

  try {
    const result = await pipelineReonboard(currentBookId);
    showToast(
      `Re-onboarded successfully. New version: ${result.version}.`,
      'success',
    );

    // Reset walk status display
    const initialStatuses: WalkStatusMap = {};
    for (const walkName of WALK_ORDER) {
      initialStatuses[walkName] = 'pending';
    }
    renderWalkStatuses(initialStatuses);
    updateRunAllButton(false);
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    showToast('Re-onboard failed: ' + msg, 'error');
  }
}

// ---------------------------------------------------------------------------
// Poll logs (preserved from previous version — used by Audio tab)
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// Saved scripts (preserved from previous version)
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// Initialization
// ---------------------------------------------------------------------------

/**
 * Initialize the Script tab.
 * Attaches event listeners for:
 *   - File upload (still uses /api/upload for initial file load)
 *   - Pipeline onboard button (POST /api/pipeline/onboard)
 *   - Individual walk run buttons (data-walk-run attribute)
 *   - Run All Walks button
 *   - Re-onboard button
 *   - Saved scripts actions (load, delete)
 *   - Save script button
 *
 * Shows pipeline UI when state.pipelineEnabled is true; otherwise shows
 * a notice to enable pipeline mode.
 */
export function initScript(): void {
  document.addEventListener('DOMContentLoaded', () => {
    // ----- File upload handler (preserved — still uses /api/upload) -----
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

    // ----- Pipeline mode toggle -----
    const pipelineSection = document.getElementById('pipeline-section');
    const legacySection = document.getElementById('legacy-script-section');
    const pipelineNotice = document.getElementById('pipeline-disabled-notice');

    if (state.pipelineEnabled) {
      // Show pipeline UI
      if (pipelineSection) pipelineSection.style.display = '';
      if (legacySection) legacySection.style.display = 'none';
      if (pipelineNotice) pipelineNotice.style.display = 'none';
      initPipelineUI();
    } else {
      // Show notice that pipeline mode must be enabled
      if (pipelineSection) pipelineSection.style.display = 'none';
      if (pipelineNotice) pipelineNotice.style.display = '';
      // Legacy section: old endpoints are removed, so hide it
      if (legacySection) legacySection.style.display = 'none';
    }

    // ----- Saved scripts (always visible) -----
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

    // Save script button
    const saveBtn = document.querySelector('button[onclick="saveScript()"]');
    if (saveBtn) {
      saveBtn.removeAttribute('onclick');
      saveBtn.addEventListener('click', () => saveScript());
    }

    // Load saved scripts on init
    loadSavedScripts();
  });
}

/**
 * Initialize pipeline-specific UI event listeners.
 * Called when state.pipelineEnabled is true.
 */
function initPipelineUI(): void {
  // Onboard button
  const btnOnboard = document.getElementById('btn-onboard-epub');
  if (btnOnboard) {
    btnOnboard.addEventListener('click', () => handleOnboard());
  }

  // Individual walk run buttons (delegated from walk-status-container parent)
  const walkContainer = document.getElementById('walk-status-container');
  if (walkContainer) {
    walkContainer.addEventListener('click', (e) => {
      const target = e.target as HTMLElement;
      const btn = target.closest('button[data-walk-run]') as HTMLButtonElement;
      if (!btn) return;
      const walkName = btn.getAttribute('data-walk-run');
      if (walkName) handleRunWalk(walkName);
    });
  }

  // Run All Walks button
  const btnRunAll = document.getElementById('btn-run-all-walks');
  if (btnRunAll) {
    btnRunAll.addEventListener('click', () => handleRunAllWalks());
  }

  // Re-onboard button
  const btnReonboard = document.getElementById('btn-reonboard');
  if (btnReonboard) {
    btnReonboard.addEventListener('click', () => handleReonboard());
  }
}
