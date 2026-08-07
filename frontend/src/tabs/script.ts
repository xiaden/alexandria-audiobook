/**
 * Script tab module — Pipeline onboard, walk execution, and walk status display.
 *
 * The tab shows the pipeline UI:
 *   - Onboard EPUB → POST /api/pipeline/onboard
 *   - Walk execution buttons (individual + Run All)
 *   - Walk status display with polling
 *   - Re-onboard button
 */

import * as API from '../api';
import { showToast, showConfirm, escapeHtml } from '../utils';
import { state } from '../state';
import { WALK_ORDER, WALK_DISPLAY_NAMES } from '../pipeline/walks';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

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

/**
 * Row from GET /api/pipeline/walks/{book_id}/runs (WalkRunRow DTO).
 * `created_ms`/`finished_ms` are INTEGER unix milliseconds; `finished_ms` is 0
 * while a run is still in progress; `error` is null unless the run failed.
 */
export interface WalkRunRow {
  run_id: string;
  walk_name: string;
  status: string; // 'pending' | 'running' | 'completed' | 'failed' | 'cancelled'
  heartbeat_ms: number;
  created_ms: number;
  finished_ms: number;
  error: string | null;
}

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
 * Get walk run history for a book, newest-first.
 * GET /api/pipeline/walks/{book_id}/runs
 * @param bookId - Book UUID
 * @returns WalkRunRow rows; empty list when the book has no runs yet
 */
export async function pipelineWalkRuns(bookId: string): Promise<WalkRunRow[]> {
  return API.get<WalkRunRow[]>(`/api/pipeline/walks/${bookId}/runs`);
}

/**
 * Cancel running walks for a book.
 * POST /api/pipeline/cancel_walks
 * @param bookId - Book UUID
 */
export async function pipelineCancelWalks(bookId: string): Promise<unknown> {
  // 503 + Retry-After (transaction() owner-thread contention) is retried
  // exactly once by the wrapper before the error surfaces (DD UX workflow #2).
  return API.postWithRetryOnce('/api/pipeline/cancel_walks', {
    book_id: bookId,
  });
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
 * Build the status badge span shared by walk statuses and walk runs.
 * Status → badge classes (identical across both renderers):
 *   completed → bg-success, running → bg-warning text-dark, failed → bg-danger,
 *   cancelled → bg-dark, anything else → bg-secondary.
 */
function buildStatusBadge(status: string): string {
  const badgeClass =
    status === 'completed' ? 'bg-success' :
    status === 'running' ? 'bg-warning text-dark' :
    status === 'failed' ? 'bg-danger' :
    status === 'cancelled' ? 'bg-dark' :
    'bg-secondary';
  const icon =
    status === 'completed' ? '<i class="fas fa-check me-1"></i>' :
    status === 'running' ? '<i class="fas fa-spinner fa-spin me-1"></i>' :
    status === 'failed' ? '<i class="fas fa-times me-1"></i>' :
    status === 'cancelled' ? '<i class="fas fa-stop me-1"></i>' :
    '<i class="fas fa-clock me-1"></i>';
  return `<span class="badge ${badgeClass}">${icon}${escapeHtml(status)}</span>`;
}

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

    return `
      <div class="d-flex align-items-center justify-content-between py-1 border-bottom" data-walk="${escapeHtml(walkName)}">
        <span class="small">${escapeHtml(label)}</span>
        ${buildStatusBadge(status)}
      </div>`;
  }).join('');
}

/**
 * Format an integer unix-millisecond timestamp as 'YYYY-MM-DD HH:MM' (local time).
 * Returns '—' for falsy/missing timestamps (e.g. `finished_ms` = 0 while a run
 * is still in progress).
 */
export function formatWalkRunTime(ms: number): string {
  if (!ms) return '—';
  const d = new Date(ms);
  const p = (n: number): string => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/**
 * Render the walk run history into #walk-runs-container, below the per-walk
 * status badges. Newest-first, as returned by the backend.
 * Shows an empty-state message when there are no runs yet.
 * @param runs - WalkRunRow rows from GET /walks/{book_id}/runs
 */
export function renderWalkRuns(runs: WalkRunRow[]): void {
  const container = document.getElementById('walk-runs-container');
  if (!container) return;

  if (!runs || runs.length === 0) {
    container.innerHTML =
      '<div class="text-muted small"><i class="fas fa-history me-1"></i>No walk runs yet</div>';
    return;
  }

  container.innerHTML = runs.map(run => {
    const label = WALK_DISPLAY_NAMES[run.walk_name] || run.walk_name;
    const errorHtml = run.error
      ? `<div class="small text-danger"><i class="fas fa-exclamation-triangle me-1"></i>${escapeHtml(run.error)}</div>`
      : '';
    return `
      <div class="py-1 border-bottom" data-walk-run-row="${escapeHtml(run.run_id)}">
        <div class="d-flex align-items-center justify-content-between">
          <span class="small">${escapeHtml(label)}</span>
          ${buildStatusBadge(run.status)}
        </div>
        <div class="small text-muted">Created ${formatWalkRunTime(run.created_ms)} · Finished ${formatWalkRunTime(run.finished_ms)}</div>
        ${errorHtml}
      </div>`;
  }).join('');
}

/**
 * Fetch and render the walk runs list.
 * Uses `bookId` when given (restored-session tab load); otherwise the current
 * book. No-op without a book; failures are logged and leave the list as-is.
 */
async function refreshWalkRuns(bookId?: string): Promise<void> {
  const id = bookId ?? currentBookId;
  if (!id) return;
  try {
    const runs = await pipelineWalkRuns(id);
    renderWalkRuns(runs);
  } catch (e) {
    console.error('Walk runs fetch error', e);
  }
}

/**
 * Start polling walk status for the current book.
 * Polls GET /api/pipeline/walk_status/{book_id} every 2 seconds.
 * Stops when all walks are completed or failed (no more 'running').
 * Shows error toast if any walk fails.
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
      // Runs history refreshes alongside walk status on every poll tick.
      await refreshWalkRuns();

      // Detect failed walks and show error toast
      for (const [walkName, status] of Object.entries(statuses)) {
        if (status === 'failed') {
          const label = WALK_DISPLAY_NAMES[walkName] || walkName;
          showToast(`Walk "${label}" failed. Check logs for details.`, 'error');
          break; // Show only one error toast per poll cycle
        }
      }

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
    showToast('Walks started. Running in background...', 'info');
    startWalkPolling();
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    showToast('Run all walks failed: ' + msg, 'error');
    updateRunAllButton(false);
  }
}

/**
 * Handle the "Cancel Walks" button click.
 * POSTs to /api/pipeline/cancel_walks and shows confirmation toast.
 */
async function handleCancelWalks(): Promise<void> {
  if (!currentBookId) {
    showToast('No book onboarded yet.', 'warning');
    return;
  }

  try {
    await pipelineCancelWalks(currentBookId);
    showToast('Walks cancelled.', 'info');
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    showToast('Cancel walks failed: ' + msg, 'error');
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
// Initialization
// ---------------------------------------------------------------------------

/**
 * Initialize the Script tab.
 * Attaches event listeners for:
 *   - Pipeline onboard button (POST /api/pipeline/onboard)
 *   - Individual walk run buttons (data-walk-run attribute)
 *   - Run All Walks button
 *   - Re-onboard button
 *
 * The pipeline UI is the only UI — it is shown unconditionally.
 */
export function initScript(): void {
  document.addEventListener('DOMContentLoaded', () => {
    // Pipeline section is the only UI — make it visible
    const pipelineSection = document.getElementById('pipeline-section');
    if (pipelineSection) pipelineSection.style.display = '';

    initPipelineUI();
  });
}

/**
 * Initialize pipeline-specific UI event listeners.
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

  // Cancel Walks button
  const btnCancelWalks = document.getElementById('btn-cancel-walks');
  if (btnCancelWalks) {
    btnCancelWalks.addEventListener('click', () => handleCancelWalks());
  }

  // Re-onboard button
  const btnReonboard = document.getElementById('btn-reonboard');
  if (btnReonboard) {
    btnReonboard.addEventListener('click', () => handleReonboard());
  }

  // Restored session (initState persisted pipelineBookId): load the runs
  // history for the book without starting walk polling (walk-status behavior
  // is unchanged — polling only starts from an explicit run/onboard action).
  if (state.pipelineBookId) {
    void refreshWalkRuns(state.pipelineBookId ?? undefined);
  }
}
