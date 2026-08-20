/**
 * Script tab module — Pipeline onboard, walk execution, and walk status display.
 *
 * The tab shows the pipeline UI:
 *   - Onboard EPUB → POST /api/pipeline/onboard
 *   - Walk execution buttons (individual + Run All)
 *   - Walk status display with polling
 *   - Re-onboard button
 *   - Per-run log viewer (EventSource at /api/pipeline/walks/log/{run_id}) with
 *     keyed reconciliation, opaque-id dedup, and bounded textContent-only rendering
 */

import * as API from '../api';
import { showToast, showConfirm, escapeHtml } from '../utils';
import { state, setPipelineBookId } from '../state';
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
// Per-walk log viewer registry (Part D)
// ---------------------------------------------------------------------------

/**
 * Per-run log viewer state keyed by the server-provided WalkRunRow.run_id.
 * The registry owns the live EventSource (nulled by every close path), the
 * rendered opaque `{run_id}:{seq}` IDs (persist across close/reopen
 * so SSE replay never re-renders), the bounded rendered count, the open state,
 * and stable references to the run's row/viewer/status DOM nodes.
 */
interface WalkLogEntry {
  rowEl: HTMLElement | null;
  viewerEl: HTMLElement | null;
  statusEl: HTMLElement | null;
  source: EventSource | null;
  renderedIds: Set<string>;
  renderedCount: number;
  open: boolean;
  idleTimer: ReturnType<typeof setTimeout> | null;
}

/** Walk log registry keyed by run_id (server-provided IDs only). */
const walkLogRegistry = new Map<string, WalkLogEntry>();

/** Containers that already have the delegated walk-log click listener. */
const boundWalkRunsContainers = new WeakSet<HTMLElement>();

/**
 * Fixed client caps (locked by the Phase 1 viewer tests): at most this many
 * rendered log entries per viewer DOM, at most this many rendered TEXT chars
 * per log entry, and at most this many status chars in #walk-log-status-{id}.
 */
const WALK_LOG_ENTRY_CAP = 200;
const WALK_LOG_RECORD_TEXT_CAP = 500;
const WALK_LOG_STATUS_TEXT_CAP = 200;
const WALK_LOG_IDLE_TIMEOUT_MS = 60_000;

/**
 * WalkLogRecord-shaped SSE `log` payload (mirrors the Part A record DTO:
 * run_id, seq, opaque `id: {run_id}:{seq}`, event, data, terminal). Only read
 * through defensive casts — hostile field values must render as literal text.
 */
interface WalkLogRecordPayload {
  run_id?: unknown;
  seq?: unknown;
  id?: unknown;
  event?: unknown;
  data?: Record<string, unknown> | null;
  terminal?: unknown;
}

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
 * Build the failed-run error line (icon + escaped error text).
 */
function buildErrorLine(error: string): string {
  return `<div class="small text-danger" data-walk-run-error><i class="fas fa-exclamation-triangle me-1"></i>${escapeHtml(error)}</div>`;
}

/** Parse a single-element HTML fragment (input is escapeHtml-derived only). */
function elementFromHtml(html: string): HTMLElement {
  const tpl = document.createElement('div');
  tpl.innerHTML = html;
  return tpl.firstElementChild as HTMLElement;
}

/**
 * Find a run row by matching the RAW server-provided run_id against its
 * data-walk-run-row attribute value (the same safe pattern as the removal
 * loop below). The run_id is never interpolated into a CSS selector — a
 * hostile-looking id (e.g. one containing a double-quote or backslash) would
 * throw a SyntaxError or silently miss inside an attribute selector.
 */
function findWalkRunRow(container: HTMLElement, runId: string): HTMLElement | null {
  for (const child of container.children) {
    const childEl = child as HTMLElement;
    if (childEl.getAttribute('data-walk-run-row') === runId) return childEl;
  }
  return null;
}

/**
 * Render the walk run history into #walk-runs-container, below the per-walk
 * status badges. Newest-first, as returned by the backend. Rows are keyed by
 * run_id and reconciled in place: a refresh updates only the mutable
 * label/badge/times/error content and never rebuilds the keyed
 * button/viewer/status nodes, so an open log viewer keeps its state, rendered
 * entries, and EventSource. Rows that vanish from the payload are closed
 * (source + registry entry deleted through the registry close path) and
 * removed; the empty state is rendered when there are no runs.
 * @param runs - WalkRunRow rows from GET /walks/{book_id}/runs
 */
export function renderWalkRuns(runs: WalkRunRow[]): void {
  const container = document.getElementById('walk-runs-container');
  if (!container) return;

  // Self-heal the registry: entries whose row node is no longer inside this
  // container belong to a removed run, an earlier render, or a rebuilt DOM
  // (e.g. a fresh page/test) — close them so a later open cannot wrongly
  // treat a freshly rendered run as already open.
  purgeDetachedRegistryEntries();

  if (!runs || runs.length === 0) {
    // Empty state: close/delete every remaining entry (detach rows) first,
    // then render the existing empty-state message.
    closeAllRegistryEntries();
    container.innerHTML =
      '<div class="text-muted small"><i class="fas fa-history me-1"></i>No walk runs yet</div>';
    return;
  }

  const payloadIds = new Set(runs.map((run) => run.run_id));

  // Remove rows that vanished from the payload (closing/deleting their
  // registry entry AND their source via the registry close path) plus any
  // non-row children such as a previously rendered empty state.
  for (const child of [...container.children]) {
    const childEl = child as HTMLElement;
    const runId = childEl.getAttribute('data-walk-run-row');
    if (!runId || !payloadIds.has(runId)) {
      if (runId) {
        const entry = walkLogRegistry.get(runId);
        if (entry) closeWalkLogEntry(runId, entry, true);
      }
      childEl.remove();
    }
  }

  // Create missing rows, update existing ones in place, and keep the API
  // (newest-first) payload ordering.
  let prev: HTMLElement | null = null;
  for (const run of runs) {
    let rowEl = findWalkRunRow(container, run.run_id);
    if (!rowEl) {
      rowEl = createWalkRunRow(run);
      container.insertBefore(rowEl, prev ? prev.nextSibling : container.firstChild);
    } else {
      updateWalkRunRow(rowEl, run);
      if (prev) {
        if (rowEl.previousSibling !== prev) container.insertBefore(rowEl, prev.nextSibling);
      } else if (container.firstChild !== rowEl) {
        container.insertBefore(rowEl, container.firstChild);
      }
    }
    prev = rowEl;
  }
}

/**
 * Create a brand-new walk run row: display label, status badge, log-open
 * button, created/finished times, optional error line, and the keyed viewer
 * shell (hidden until opened) with its bounded status element.
 */
function createWalkRunRow(run: WalkRunRow): HTMLElement {
  const runId = run.run_id;
  const label = WALK_DISPLAY_NAMES[run.walk_name] || run.walk_name;

  const row = document.createElement('div');
  row.className = 'py-1 border-bottom';
  row.setAttribute('data-walk-run-row', runId);

  const head = document.createElement('div');
  head.className = 'd-flex align-items-center justify-content-between';

  const labelEl = document.createElement('span');
  labelEl.className = 'small';
  labelEl.setAttribute('data-walk-run-label', '');
  labelEl.textContent = label;
  head.appendChild(labelEl);

  head.insertAdjacentHTML('beforeend', buildStatusBadge(run.status));
  row.appendChild(head);

  // Log-open button: a DIRECT sibling of the viewer inside the row (the
  // viewer shell is appended at the end of the row).
  const openBtn = document.createElement('button');
  openBtn.type = 'button';
  openBtn.className = 'btn btn-sm btn-outline-secondary';
  openBtn.setAttribute('data-walk-log-open', runId);
  openBtn.textContent = 'View Log';
  row.appendChild(openBtn);

  const timesEl = document.createElement('div');
  timesEl.className = 'small text-muted';
  timesEl.setAttribute('data-walk-run-times', '');
  timesEl.textContent = `Created ${formatWalkRunTime(run.created_ms)} · Finished ${formatWalkRunTime(run.finished_ms)}`;
  row.appendChild(timesEl);

  if (run.error) {
    row.insertAdjacentHTML('beforeend', buildErrorLine(run.error));
  }

  // Keyed viewer shell (hidden until opened) with its stable status element.
  // The status starts as '—' (no terminal status recorded yet); the
  // complete/410/error handlers replace it and the 'log' handler renders
  // entries into the viewer.
  const viewerEl = document.createElement('div');
  viewerEl.setAttribute('data-walk-log-viewer', runId);
  viewerEl.id = `walk-log-${runId}`;
  viewerEl.style.display = 'none';
  const statusEl = document.createElement('div');
  statusEl.id = `walk-log-status-${runId}`;
  statusEl.setAttribute('data-walk-log-status', ''); // selector-safe lookup handle (P4-S1 URL-safety gate)
  statusEl.textContent = '—';
  viewerEl.appendChild(statusEl);
  row.appendChild(viewerEl);

  return row;
}

/**
 * Update ONLY the mutable content of an existing run row in place: display
 * label, status badge, created/finished times, and error line. The keyed
 * button/viewer/status nodes are never rebuilt, so an open viewer keeps its
 * rendered entries and EventSource across status refresh.
 */
function updateWalkRunRow(rowEl: HTMLElement, run: WalkRunRow): void {
  const labelEl = rowEl.querySelector<HTMLElement>('[data-walk-run-label]');
  if (labelEl) labelEl.textContent = WALK_DISPLAY_NAMES[run.walk_name] || run.walk_name;

  const badgeEl = rowEl.querySelector('.badge');
  if (badgeEl) badgeEl.outerHTML = buildStatusBadge(run.status);

  const timesEl = rowEl.querySelector<HTMLElement>('[data-walk-run-times]');
  if (timesEl) {
    timesEl.textContent = `Created ${formatWalkRunTime(run.created_ms)} · Finished ${formatWalkRunTime(run.finished_ms)}`;
  }

  const errorEl = rowEl.querySelector<HTMLElement>('[data-walk-run-error]');
  const viewerEl = rowEl.querySelector<HTMLElement>('[data-walk-log-viewer]');
  if (run.error) {
    if (errorEl) {
      errorEl.outerHTML = buildErrorLine(run.error);
    } else if (viewerEl) {
      rowEl.insertBefore(elementFromHtml(buildErrorLine(run.error)), viewerEl);
    }
  } else if (errorEl) {
    errorEl.remove();
  }
}

/**
 * Close a registry entry's source exactly once, reset its open view state,
 * and optionally delete the entry. Kept entries (closeWalkLog) preserve their
 * rendered replay IDs so a reconnect never re-renders; removed runs delete
 * the entry entirely. Source close is guarded so externally-closed sources
 * (e.g. the test harness afterEach) are not closed twice.
 */
function closeWalkLogEntry(runId: string, entry: WalkLogEntry, deleteEntry: boolean): void {
  if (entry.idleTimer !== null) {
    clearTimeout(entry.idleTimer);
    entry.idleTimer = null;
  }
  if (entry.source && (entry.source as { closed?: boolean }).closed !== true) {
    entry.source.close();
  }
  entry.source = null;
  entry.open = false;
  if (entry.viewerEl) entry.viewerEl.style.display = 'none';
  if (deleteEntry) walkLogRegistry.delete(runId);
}

/** Close + delete every registry entry (used by the empty-payload path). */
function closeAllRegistryEntries(): void {
  for (const [runId, entry] of [...walkLogRegistry]) {
    closeWalkLogEntry(runId, entry, true);
  }
}

/**
 * Self-heal the registry: close + delete entries whose row node is no longer
 * inside #walk-runs-container (removed run, or the DOM was rebuilt — e.g. a
 * fresh page/test wiped document.body). Without this a stale open entry from
 * a previous render would make idempotent-open wrongly treat a freshly
 * rendered run as already open.
 */
function purgeDetachedRegistryEntries(): void {
  const container = document.getElementById('walk-runs-container');
  if (!container) return;
  for (const [runId, entry] of [...walkLogRegistry]) {
    if (!entry.rowEl || !container.contains(entry.rowEl)) {
      closeWalkLogEntry(runId, entry, true);
    }
  }
}

/**
 * Compose the readable text for one rendered log entry: the local timestamp,
 * the event name, and the record's prompt/response data fields. The values
 * are concatenated into one string (then bounded by WALK_LOG_RECORD_TEXT_CAP
 * at the call site) — never interpolated as HTML, so hostile prompt/response
 * values render as literal text.
 */
function buildLogEntryText(record: WalkLogRecordPayload): string {
  const eventName = typeof record.event === 'string' ? record.event : 'log';
  const data = (record.data ?? {}) as Record<string, unknown>;
  const ts = typeof data.timestamp === 'number' ? data.timestamp : 0;
  const prompts = (data.prompts ?? {}) as Record<string, unknown>;
  const system = typeof prompts.system === 'string' ? prompts.system : '';
  const user = typeof prompts.user === 'string' ? prompts.user : '';
  const response = typeof data.response === 'string' ? data.response : '';
  return (
    `[${formatWalkRunTime(ts)}] ${eventName}\n` +
    `prompt[system]: ${system}\n` +
    `prompt[user]: ${user}\n` +
    `response: ${response}`
  );
}

/**
 * Open the log viewer for one run: mark the registry entry open, reveal its
 * viewer element, and open exactly ONE EventSource at
 * `/api/pipeline/walks/log/{run_id}` built from the server-provided run ID.
 * Idempotent for an already-open run (no second source), a silent no-op when
 * the run is not rendered (stale rows cannot attach callbacks), and safe to
 * re-open: a closed/completed run replays its file and re-completes, with
 * renderedIds deduping any overlapping replay.
 */
export function openWalkLog(runId: string): void {
  const container = document.getElementById('walk-runs-container');
  if (!container) return;

  // A stale entry whose row was detached belongs to an earlier render — drop
  // it instead of treating the freshly rendered run as already open.
  const stale = walkLogRegistry.get(runId);
  if (stale && stale.rowEl && !container.contains(stale.rowEl)) {
    closeWalkLogEntry(runId, stale, true);
  }

  const rowEl = findWalkRunRow(container, runId);
  const viewerEl = rowEl?.querySelector<HTMLElement>('[data-walk-log-viewer]') ?? null;
  // Every run_id lookup is interpolation-free: the row resolves by iterating
  // the container matching the RAW data-walk-run-row value, the viewer via the
  // constant [data-walk-log-viewer] selector (one per row), and the status via
  // the constant [data-walk-log-status] attribute (the locked
  // `#walk-log-status-{id}` id is ALSO tagged with it) — hostile-looking
  // server run_ids (e.g. '../evil', 'a b', '<b>x</b>') can never be
  // interpolated into a CSS selector and never make openWalkLog throw or
  // silently no-op.
  const statusEl = viewerEl?.querySelector<HTMLElement>('[data-walk-log-status]');
  if (!rowEl || !viewerEl || !statusEl) return;

  let entry = walkLogRegistry.get(runId);
  if (entry?.open) return; // idempotent: an already-open run is a no-op
  if (!entry) {
    entry = {
      rowEl,
      viewerEl,
      statusEl,
      source: null,
      renderedIds: new Set(),
      renderedCount: 0,
      open: false,
      idleTimer: null,
    };
    walkLogRegistry.set(runId, entry);
  } else {
    // Reopened run: refresh node references, keep renderedIds/source state.
    entry.rowEl = rowEl;
    entry.viewerEl = viewerEl;
    entry.statusEl = statusEl;
  }
  const logEntry: WalkLogEntry = entry;
  logEntry.open = true;
  viewerEl.style.display = '';

  // Open exactly one EventSource per run at the server-provided run ID URL.
  // `source` is nulled by every close path (closeWalkLogEntry), so a source is
  // created only when this run has none live — an already-open run returned
  // above. A completed run replayed on reconnect re-completes immediately;
  // renderedIds stops the overlapping replay from re-rendering.
  if (logEntry.source) return;
  const source = new EventSource(`/api/pipeline/walks/log/${runId}`);
  logEntry.source = source;

  const refreshIdleTimer = (): void => {
    if (logEntry.idleTimer !== null) clearTimeout(logEntry.idleTimer);
    logEntry.idleTimer = setTimeout(() => {
      if (!logEntry.open || logEntry.source !== source) return;
      if (logEntry.statusEl) {
        logEntry.statusEl.textContent = 'Log stream timed out.'.slice(0, WALK_LOG_STATUS_TEXT_CAP);
      }
      closeWalkLogEntry(runId, logEntry, false);
    }, WALK_LOG_IDLE_TIMEOUT_MS);
  };
  refreshIdleTimer();

  // 'log': JSON-parse the record, dedup by the opaque {run_id}:{seq} id, and
  // append ONE bounded, text-only rendering (data-walk-log-entry) to the
  // viewer. Malformed JSON never throws and never corrupts the viewer; ids
  // already rendered (replay across reconnect/refresh) are skipped; once the
  // entry cap is reached ids are still retained (dedup stays correct) but no
  // further elements are created.
  source.addEventListener('log', (evt: Event) => {
    if (!walkLogRegistry.get(runId) || !logEntry.open) return; // removed/closed guard
    refreshIdleTimer();
    let record: WalkLogRecordPayload;
    try {
      const parsed: unknown = JSON.parse(String((evt as MessageEvent).data));
      if (typeof parsed !== 'object' || parsed === null) return;
      record = parsed as WalkLogRecordPayload;
    } catch {
      return; // malformed JSON — absorb silently, viewer stays usable
    }
    const id =
      typeof record.id === 'string' && record.id !== ''
        ? record.id
        : `${typeof record.run_id === 'string' ? record.run_id : runId}:${typeof record.seq === 'number' ? record.seq : ''}`;
    if (!id) return;
    if (logEntry.renderedIds.has(id)) return; // already rendered — dedup
    logEntry.renderedIds.add(id);
    if (logEntry.renderedCount >= WALK_LOG_ENTRY_CAP) return; // bounded DOM
    logEntry.renderedCount += 1;
    const entryEl = document.createElement('div');
    entryEl.setAttribute('data-walk-log-entry', '');
    entryEl.textContent = buildLogEntryText(record).slice(0, WALK_LOG_RECORD_TEXT_CAP);
    logEntry.viewerEl?.appendChild(entryEl);
  });

  source.addEventListener('heartbeat', () => refreshIdleTimer());

  // 'complete': {run_id,status} — announce the bounded status text, close and
  // remove the source exactly once, and mark the entry closed so a later
  // click can replay the file (renderedIds prevents re-render). A second
  // complete (or any late event after cleanup) is a no-op.
  source.addEventListener('complete', (evt: Event) => {
    if (!walkLogRegistry.get(runId) || !logEntry.open) return; // removed/closed guard
    let status = '';
    try {
      const parsed: unknown = JSON.parse(String((evt as MessageEvent).data));
      if (typeof parsed === 'object' && parsed !== null) {
        const payload = parsed as { status?: unknown };
        if (typeof payload.status === 'string' && payload.status) status = payload.status;
      }
    } catch {
      // Malformed complete payload — still close and clean up below.
    }
    if (logEntry.statusEl) {
      logEntry.statusEl.textContent = (status || 'Completed').slice(0, WALK_LOG_STATUS_TEXT_CAP);
    }
    closeWalkLogEntry(runId, logEntry, false);
  });

  // 'error': the server contract signals 410 Gone (and other terminal HTTP
  // codes) by attaching a NUMERIC `status` to the error event; a genuine
  // browser EventSource ErrorEvent carries no such property, so read it
  // defensively. Both paths publish bounded status text and close/remove the
  // source exactly once.
  source.addEventListener('error', (evt: Event) => {
    if (!walkLogRegistry.get(runId) || !logEntry.open) return; // removed/closed guard
    const numericStatus = (evt as Event & { status?: unknown }).status;
    const message =
      typeof numericStatus === 'number'
        ? `Log stream ended (HTTP ${numericStatus})`
        : 'Log stream connection failed.';
    if (logEntry.statusEl) {
      logEntry.statusEl.textContent = message.slice(0, WALK_LOG_STATUS_TEXT_CAP);
    }
    closeWalkLogEntry(runId, logEntry, false);
  });
}

/**
 * Close a run's log viewer: close its source (once) and hide its viewer.
 * Idempotent and a silent no-op for never-opened runs. The registry entry
 * (and its rendered replay IDs) is kept so a reconnect never re-renders.
 */
export function closeWalkLog(runId: string): void {
  const entry = walkLogRegistry.get(runId);
  if (!entry) return; // never opened — silent no-op
  closeWalkLogEntry(runId, entry, false);
}

/**
 * Fetch and render the walk runs list.
 * Uses `bookId` when given (restored-session tab load); otherwise the current
 * book. No-op without a book; failures are logged and leave the list as-is.
 */
async function refreshWalkRuns(bookId?: string): Promise<WalkRunRow[] | null> {
  const id = bookId ?? currentBookId;
  if (!id) return null;
  try {
    const runs = await pipelineWalkRuns(id);
    renderWalkRuns(runs);
    return runs;
  } catch (e) {
    console.error('Walk runs fetch error', e);
    return null;
  }
}

/**
 * Start polling walk status for the current book.
 * Polls GET /api/pipeline/walk_status/{book_id} every 2 seconds.
 * Stops when all walks are terminal (no 'pending' or 'running' walks).
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
      const runs = await refreshWalkRuns();

      // Detect failed walks and show error toast
      for (const [walkName, status] of Object.entries(statuses)) {
        if (status === 'failed') {
          const label = WALK_DISPLAY_NAMES[walkName] || walkName;
          showToast(`Walk "${label}" failed. Check logs for details.`, 'error');
          break; // Show only one error toast per poll cycle
        }
      }

      // A reserved run is reported as pending until its background task starts.
      // Use persisted run rows to distinguish that state from walks that have
      // never run; otherwise single-walk polling would never stop on the other
      // walks' default pending statuses.
      const anyActive = Object.values(statuses).some(s => s === 'running')
        || (runs?.some(run => run.status === 'pending' || run.status === 'running') ?? false);
      if (!anyActive) {
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
    setPipelineBookId(result.book_id);

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
 *   - Cancel Walks button listener (btn-cancel-walks)
 *   - Re-onboard button
 *   - Delegated per-run log viewer toggle (button[data-walk-log-open]) on
 *     #walk-runs-container
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
    currentBookId = state.pipelineBookId;
    void refreshWalkRuns(state.pipelineBookId ?? undefined);
  }

  // Delegated per-run log viewer open/close toggle. One listener per
  // container element: the WeakSet guard keeps repeated initPipelineUI()
  // calls (or stacked DOMContentLoaded dispatches) from duplicating the
  // listener on the SAME container, while a freshly built container still
  // gets bound. The handler only delegates to openWalkLog/closeWalkLog.
  const walkRunsContainer = document.getElementById('walk-runs-container');
  if (walkRunsContainer && !boundWalkRunsContainers.has(walkRunsContainer)) {
    boundWalkRunsContainers.add(walkRunsContainer);
    walkRunsContainer.addEventListener('click', (e) => {
      const target = e.target as HTMLElement | null;
      const btn = target?.closest('button[data-walk-log-open]') as HTMLButtonElement | null;
      if (!btn) return;
      const runId = btn.getAttribute('data-walk-log-open');
      if (!runId) return;
      if (walkLogRegistry.get(runId)?.open) closeWalkLog(runId);
      else openWalkLog(runId);
    });
  }
}
