/**
 * Effective prompt-config viewer/editor (PipelineWalkPromptConfigRevisionAPI.v1).
 *
 * Backend contracts (app/pipeline, READ-ONLY for this sub-task):
 *   - GET    /api/pipeline/walks/{book_id}/config
 *            -> EffectiveWalkConfigDTO {book_id, tasks:{task:{values,sources}}}
 *   - POST   /api/pipeline/walks/{book_id}/config/validate
 *            (PromptConfigWriteRequest) -> PromptConfigValidationResponse
 *   - POST   /api/pipeline/walks/{book_id}/config/revisions
 *            (PromptConfigWriteRequest with base_revision) -> 201 PromptConfigRevisionDTO
 *   - POST   /api/pipeline/walks/{book_id}/reruns
 *            (ScopedWalkRerunRequest) -> {run_id, revision_id, scope, invalidated_walks}
 *
 * All nine fixed walks are surfaced with their effective values and per-field
 * source-layer badges ('row' | 'config' | 'task' | 'global' | 'fallback').
 * Editing is guarded: structured inputs and raw JSON are both restricted to the
 * exact allow-list {model_name, reasoning_effort, temperature, prompt}, and
 * temperature=0.0 is a valid override. Validate is side-effect free. Saves use
 * base_revision and surface 409 conflicts, 422 validation, and one 503 retry.
 * Reruns are explicit and confirmed with confirm:true and never run implicitly.
 *
 * Accessibility: every journey is keyboard-reachable (real <button>/<select>/
 * <input> elements, delegated actions), state is conveyed by text + badge (not
 * color alone), destructive reruns are confirmed, and errors are text surfaces.
 */

import * as API from '../api';
import { state } from '../state';
import { showToast, showConfirm, escapeHtml } from '../utils';
import {
  EffectiveWalkConfig,
  EffectiveWalkTask,
  PersonaSceneScope,
  PromptConfigRevision,
  PromptConfigWriteRequest,
} from '../state';
import { WALK_ORDER, WALK_TASK_NAMES, WALK_DISPLAY_NAMES } from '../pipeline/walks';
import { sceneOptionsFromWorkbench } from './persona';

// ---------------------------------------------------------------------------
// Tab identity
// ---------------------------------------------------------------------------

/** id of the tab pane this module owns (must equal `${data-tab}-tab`). */
export const PROMPT_CONFIG_TAB_ID = 'prompt-config-tab';

/** data-tab value for navigation; main.ts switchTab resolves `${name}-tab`. */
export const PROMPT_CONFIG_TAB_NAME = 'prompt-config';

// ---------------------------------------------------------------------------
// Contract constants (exact allow-list)
// ---------------------------------------------------------------------------

/** Exact allowed override keys from PipelineWalkPromptConfigRevisionAPI.v1. */
export const PROMPT_ALLOWED_KEYS = [
  'model_name',
  'reasoning_effort',
  'temperature',
  'prompt',
] as const;

export type PromptAllowedKey = (typeof PROMPT_ALLOWED_KEYS)[number];

export const PROMPT_REASONING_EFFORTS = ['low', 'medium', 'high'] as const;

export const PROMPT_TEMPERATURE_MIN = 0;
export const PROMPT_TEMPERATURE_MAX = 2;

/** The nine fixed task names in canonical walk order. */
export const PROMPT_TASK_ORDER: readonly string[] = WALK_ORDER.map(
  (walk) => WALK_TASK_NAMES[walk],
);

/** Human label per task (derived from the walk display name). */
export const PROMPT_TASK_LABELS: Record<string, string> = (() => {
  const map: Record<string, string> = {};
  for (const walk of WALK_ORDER) {
    map[WALK_TASK_NAMES[walk]] = WALK_DISPLAY_NAMES[walk];
  }
  return map;
})();

/** alias resolution is book-global and rejects a scenes-scoped rerun. */
export const PROMPT_BOOK_GLOBAL_TASKS: readonly string[] = [
  'script_alias_resolution',
];

// ---------------------------------------------------------------------------
// Module state
// ---------------------------------------------------------------------------

let _promptInitialized = false;

/** Currently selected task (one of PROMPT_TASK_ORDER). */
let _activeTask: string | null = null;

/** Latest EffectiveWalkConfig fetched for the current book. */
let _activeConfig: EffectiveWalkConfig | null = null;

/** base_revision sent with the next save (head revision_id or null). */
let _currentBaseRevision: string | null = null;

/** Most recent head revision_id (used to guard reruns). */
let _headRevisionId: string | null = null;

/** Revision history for the active task, newest first. */
let _revisions: PromptConfigRevision[] = [];

/**
 * Per-task session revision history and head (base_revision source). The
 * backend exposes no list route, so the current head is tracked from the
 * revisions/run ids this session's saves and reruns return; base_revision
 * stays null until the first confirmed write.
 */
const _sessionHistory: Record<string, PromptConfigRevision[]> = {};
const _sessionHead: Record<string, string | null> = {};

function syncFromSession(task: string): void {
  _revisions = _sessionHistory[task] ?? [];
  _headRevisionId = _sessionHead[task] ?? null;
  _currentBaseRevision = _headRevisionId;
}

// ---------------------------------------------------------------------------
// Small DOM helpers
// ---------------------------------------------------------------------------

function editorContainer(): HTMLElement | null {
  return document.getElementById(PROMPT_CONFIG_TAB_ID);
}

function requireBookId(): string {
  if (!state.pipelineBookId) throw new Error('No book selected');
  return state.pipelineBookId;
}

function inputValue(selector: string): string {
  const el = editorContainer()?.querySelector<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>(selector);
  return el ? el.value : '';
}

function renderErrors(message: string): void {
  const el = editorContainer()?.querySelector<HTMLElement>('[data-role="pc-errors"]');
  if (el) el.textContent = message;
}

// ---------------------------------------------------------------------------
// Pure helpers (exported for tests)
// ---------------------------------------------------------------------------

/**
 * Map a backend source-layer tier to a stable human badge label.
 * Tiers: 'row' (DB walk_override), 'config' (on-disk), 'task' (llm task
 * override), 'global' (llm global), 'fallback' (default).
 */
export function promptSourceLabel(source: string | null | undefined): string {
  if (!source) return 'default';
  switch (source) {
    case 'row':
      return 'DB override';
    case 'config':
      return 'on-disk config';
    case 'task':
      return 'task override';
    case 'global':
      return 'global';
    case 'fallback':
      return 'default';
    default:
      return source;
  }
}

/** Format a value for display in the effective table. */
export function formatEffectiveValue(value: unknown): string {
  if (value === null || value === undefined) return '—';
  return String(value);
}

/**
 * Guarded raw-JSON parser enforcing the exact allow-list. Unknown keys and
 * malformed JSON are rejected client-side before anything is sent.
 */
export function safeParseRawJson(
  text: string,
): { ok: true; parsed: Record<string, unknown> } | { ok: false; error: string } {
  if (text.trim() === '') return { ok: true, parsed: {} };
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return { ok: false, error: 'raw_json is not valid JSON' };
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return { ok: false, error: 'raw_json must decode to a JSON object' };
  }
  const obj = parsed as Record<string, unknown>;
  const unknown = Object.keys(obj).filter(
    (key) => !PROMPT_ALLOWED_KEYS.includes(key as PromptAllowedKey),
  );
  if (unknown.length > 0) {
    return {
      ok: false,
      error: `unknown override key(s): ${unknown.sort().join(', ')}`,
    };
  }
  return { ok: true, parsed: obj };
}

/** Non-prompt settings derived from a parsed raw object. */
export function settingsFromParsed(
  parsed: Record<string, unknown>,
): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const key of PROMPT_ALLOWED_KEYS) {
    if (key === 'prompt') continue;
    if (parsed[key] !== undefined) out[key] = parsed[key];
  }
  return out;
}

/** Top-level prompt field derived from a parsed raw object (null clears it). */
export function promptFromParsed(parsed: Record<string, unknown>): string | null {
  const p = parsed['prompt'];
  if (typeof p === 'string' && p.trim() !== '') return p;
  return null;
}

/** JSON text for the raw editor prefilled from effective values (allow-listed). */
export function rawJsonForValues(values: Record<string, unknown>): string {
  const obj: Record<string, unknown> = {};
  for (const key of PROMPT_ALLOWED_KEYS) {
    if (values[key] !== undefined && values[key] !== null) obj[key] = values[key];
  }
  return JSON.stringify(obj, null, 2);
}

// ---------------------------------------------------------------------------
// Rendering (pure, exported for tests)
// ---------------------------------------------------------------------------

function sourceBadge(source: string | null | undefined): string {
  return `<span class="badge bg-light text-dark border" data-role="source-badge">${escapeHtml(
    promptSourceLabel(source),
  )}</span>`;
}

/** One effective value row for the read-only table. */
export function renderEffectiveRow(
  field: string,
  value: unknown,
  source: string | null | undefined,
): string {
  return `<tr><th scope="row">${escapeHtml(field)}</th><td data-role="effective-${escapeHtml(
    field,
  )}">${escapeHtml(formatEffectiveValue(value))}</td><td>${sourceBadge(source)}</td></tr>`;
}

/** A selectable task row in the nine-walk list. */
export function renderTaskRow(
  task: string,
  label: string,
  values: Record<string, unknown>,
  sources: Record<string, string | null>,
  active: boolean,
): string {
  const temp = values['temperature'];
  const tempText = temp === undefined || temp === null ? 'default' : String(temp);
  const tempBadge = sourceBadge(sources['temperature']);
  return `<button type="button" class="list-group-item list-group-item-action ${
    active ? 'active' : ''
  } d-flex justify-content-between align-items-center" data-action="pc-select" data-task="${escapeHtml(
    task,
  )}">
    <span>${escapeHtml(label)} <small class="text-muted">${escapeHtml(task)}</small></span>
    <span class="d-flex align-items-center gap-2"><code>${escapeHtml(
      tempText,
    )}</code>${tempBadge}</span>
  </button>`;
}

/** Revision history list item. */
export function renderRevisionItem(rev: PromptConfigRevision): string {
  const time = new Date(rev.created_ms).toLocaleString();
  const head = rev.superseded_by === null;
  return `<li class="list-group-item small">
    <div class="d-flex justify-content-between">
      <code data-role="rev-id">${escapeHtml(rev.revision_id)}</code>
      <span class="text-muted">${escapeHtml(time)}</span>
    </div>
    <div>author: ${escapeHtml(rev.author_id || 'unknown')} ${
    head ? '<span class="badge bg-success">head</span>' : ''
  }</div>
    <div class="text-muted">base: ${escapeHtml(rev.base_revision ?? 'none')}</div>
  </li>`;
}

/**
 * Render the full editor for the active task. Re-rendered on selection and
 * after save/validate/rerun so base_revision, history, and source layers always
 * reflect the latest server truth.
 */
export function renderTaskEditor(
  task: string,
  cfg: EffectiveWalkTask,
  baseRevision: string | null,
  revisions: PromptConfigRevision[],
): string {
  const label = PROMPT_TASK_LABELS[task] ?? task;
  const values = cfg.values || {};
  const sources = cfg.sources || {};
  const model = values['model_name'] ?? '';
  const effort = values['reasoning_effort'] ?? '';
  const temp =
    values['temperature'] === undefined || values['temperature'] === null
      ? ''
      : String(values['temperature']);
  const prompt = values['prompt'] ?? '';
  const rawPrefill = rawJsonForValues(values);
  const sceneOptions = sceneOptionsFromWorkbench();
  const hasScenes = sceneOptions.length > 0;
  const bookGlobal = PROMPT_BOOK_GLOBAL_TASKS.includes(task);
  const sceneChecks = sceneOptions
    .map(
      (s) =>
        `<div class="form-check"><input class="form-check-input" type="checkbox" data-role="pc-scene" value="${escapeHtml(
          s.scene_id,
        )}" id="pc-scene-${escapeHtml(s.scene_id)}"><label class="form-check-label" for="pc-scene-${escapeHtml(
          s.scene_id,
        )}">${escapeHtml(s.label)}</label></div>`,
    )
    .join('');

  return `<div class="card mb-3" data-role="pc-editor">
    <div class="card-header d-flex justify-content-between align-items-center">
      <h5 class="mb-0">${escapeHtml(label)} <code>${escapeHtml(task)}</code></h5>
      <span class="small text-muted" data-role="pc-base">base: <code>${escapeHtml(
        baseRevision ?? 'none',
      )}</code></span>
    </div>
    <div class="card-body">
      <h6>Effective values</h6>
      <table class="table table-sm">
        <tbody>
          ${renderEffectiveRow('model_name', values['model_name'], sources['model_name'])}
          ${renderEffectiveRow('reasoning_effort', values['reasoning_effort'], sources['reasoning_effort'])}
          ${renderEffectiveRow('temperature', values['temperature'], sources['temperature'])}
          ${renderEffectiveRow('prompt', values['prompt'], sources['prompt'])}
        </tbody>
      </table>

      <hr>
      <div class="d-flex gap-3 align-items-center mb-2">
        <label class="fw-semibold" for="pc-mode">Edit mode</label>
        <select class="form-select form-select-sm w-auto" id="pc-mode" data-role="pc-mode">
          <option value="structured">Structured</option>
          <option value="raw">Raw JSON (allow-listed)</option>
        </select>
        <span class="text-muted small">Structured values win over raw JSON; both restrict to the exact allowed keys.</span>
      </div>

      <div data-role="pc-structured">
        <div class="mb-2">
          <label class="form-label" for="pc-model">model_name</label>
          <input class="form-control" id="pc-model" data-role="pc-model" type="text" value="${escapeHtml(
            String(model),
          )}" placeholder="leave empty to inherit / clear override">
        </div>
        <div class="mb-2">
          <label class="form-label" for="pc-effort">reasoning_effort</label>
          <select class="form-select" id="pc-effort" data-role="pc-effort">
            <option value="">inherit</option>
            ${PROMPT_REASONING_EFFORTS.map(
              (r) =>
                `<option value="${r}" ${r === effort ? 'selected' : ''}>${r}</option>`,
            ).join('')}
          </select>
        </div>
        <div class="mb-2">
          <label class="form-label" for="pc-temperature">temperature</label>
          <input class="form-control" id="pc-temperature" data-role="pc-temperature" type="number" min="${PROMPT_TEMPERATURE_MIN}" max="${PROMPT_TEMPERATURE_MAX}" step="0.1" value="${escapeHtml(
            temp,
          )}" placeholder="empty = inherit (0.0 is valid)">
        </div>
        <div class="mb-2">
          <label class="form-label" for="pc-prompt">prompt</label>
          <textarea class="form-control" id="pc-prompt" data-role="pc-prompt" rows="6">${escapeHtml(
            String(prompt),
          )}</textarea>
          <div class="form-text">Empty prompt clears the override (falls through to config/global).</div>
        </div>
      </div>

      <div data-role="pc-raw" style="display:none">
        <label class="form-label" for="pc-raw">raw_json</label>
        <textarea class="form-control font-monospace" id="pc-raw" data-role="pc-raw" rows="8">${escapeHtml(
          rawPrefill,
        )}</textarea>
        <div class="form-text">Allowed keys: ${PROMPT_ALLOWED_KEYS.join(
          ', ',
        )}. Unknown keys are rejected.</div>
      </div>

      <hr>
      <h6>Rerun scope</h6>
      <div class="mb-2">
        <select class="form-select form-select-sm w-auto" data-role="pc-scope">
          <option value="book">book</option>
          <option value="scenes" ${hasScenes ? '' : 'disabled'}>scenes</option>
        </select>
        ${
          bookGlobal
            ? '<span class="text-warning small">alias resolution is book-global; scenes scope is rejected.</span>'
            : ''
        }
        <div class="mt-2" data-role="pc-scenes" ${
          hasScenes ? '' : 'style="display:none"'
        }>
          ${sceneChecks}
        </div>
      </div>

      <div class="d-flex gap-2 mb-2">
        <button type="button" class="btn btn-outline-secondary" data-action="pc-validate">Validate</button>
        <button type="button" class="btn btn-primary" data-action="pc-save">Save revision</button>
        <button type="button" class="btn btn-warning" data-action="pc-rerun">Rerun scoped</button>
      </div>
      <div data-role="pc-errors" class="text-danger small"></div>

      <h6 class="mt-3">Revision history</h6>
      ${
        revisions.length
          ? `<ul class="list-group">${revisions.map(renderRevisionItem).join('')}</ul>`
          : '<p class="text-muted small">No revisions yet.</p>'
      }
    </div>
  </div>`;
}

/** Render the task list sidebar plus the active editor. */
export function renderPromptConfig(): string {
  const cfg = _activeConfig;
  if (!cfg) {
    return '<div class="alert alert-secondary">No effective config loaded.</div>';
  }
  const rows = PROMPT_TASK_ORDER.map((task) => {
    const tcfg = cfg.tasks[task];
    return renderTaskRow(
      task,
      PROMPT_TASK_LABELS[task] ?? task,
      tcfg?.values ?? {},
      tcfg?.sources ?? {},
      task === _activeTask,
    );
  }).join('');
  const active = _activeTask ? cfg.tasks[_activeTask] : null;
  const editor = active
    ? renderTaskEditor(_activeTask as string, active, _currentBaseRevision, _revisions)
    : '<p class="text-muted">Select a walk to edit its effective prompt config.</p>';
  return `<div class="row">
    <div class="col-md-4">
      <h6>Nine fixed walks</h6>
      <div class="list-group">${rows}</div>
    </div>
    <div class="col-md-8">${editor}</div>
  </div>`;
}

// ---------------------------------------------------------------------------
// Build the write request from the form (guarded, no side effects)
// ---------------------------------------------------------------------------

/**
 * Build a PromptConfigWriteRequest from the current editor state.
 * Structured mode emits a settings object plus a top-level prompt field; raw
 * mode emits the raw text verbatim and derives settings/prompt from its parsed
 * (allow-listed) content. Empty temperature/prompt mean "inherit/clear" and are
 * omitted rather than sent as overrides.
 */
export function buildWriteRequest(): PromptConfigWriteRequest {
  if (!_activeTask) throw new Error('No task selected');
  const mode = inputValue('[data-role="pc-mode"]') || 'structured';
  const base: PromptConfigWriteRequest = {
    task: _activeTask,
    settings: {},
    prompt: null,
    raw_json: null,
    base_revision: _currentBaseRevision,
  };
  if (mode === 'raw') {
    const rawText = inputValue('[data-role="pc-raw"]');
    base.raw_json = rawText;
    const parsed = safeParseRawJson(rawText);
    if (parsed.ok) {
      base.settings = settingsFromParsed(parsed.parsed);
      base.prompt = promptFromParsed(parsed.parsed);
    }
    return base;
  }
  const settings: Record<string, unknown> = {};
  const modelName = inputValue('[data-role="pc-model"]').trim();
  if (modelName) settings['model_name'] = modelName;
  const effort = inputValue('[data-role="pc-effort"]');
  if (effort) settings['reasoning_effort'] = effort;
  const tempRaw = inputValue('[data-role="pc-temperature"]').trim();
  if (tempRaw !== '') {
    const t = Number(tempRaw);
    if (Number.isFinite(t)) settings['temperature'] = t;
  }
  const prompt = inputValue('[data-role="pc-prompt"]').trim();
  return {
    task: _activeTask,
    settings,
    prompt: prompt === '' ? null : prompt,
    raw_json: null,
    base_revision: _currentBaseRevision,
  };
}

// ---------------------------------------------------------------------------
// Data loading
// ---------------------------------------------------------------------------

/** Fetch effective config + revisions for the active task and re-render. */
export async function loadPromptConfig(): Promise<void> {
  const container = editorContainer();
  if (!container) return;
  let bookId: string;
  try {
    bookId = requireBookId();
  } catch (err) {
    container.innerHTML = `<div class="alert alert-warning">${escapeHtml(
      err instanceof Error ? err.message : String(err),
    )}</div>`;
    return;
  }
  try {
    const cfg = await API.getEffectiveWalkConfig(bookId);
    _activeConfig = cfg;
    if (!_activeTask || !cfg.tasks[_activeTask]) {
      _activeTask = PROMPT_TASK_ORDER[0] ?? null;
    }
    // The backend exposes no GET list-revisions route for prompt-config, so
    // revision history and the current head are tracked client-side from the
    // revisions this session's save/rerun calls return. base_revision therefore
    // starts as null and advances only after a confirmed write; a stale server
    // head surfaces as a 409 conflict.
    syncFromSession(_activeTask);
    container.innerHTML = renderPromptConfig();
  } catch (err) {
    container.innerHTML = `<div class="alert alert-danger">Failed to load effective config: ${escapeHtml(
      err instanceof Error ? err.message : String(err),
    )}</div>`;
  }
}

/** Record a freshly-saved revision as the new head (newest first). */
export function recordRevision(rev: PromptConfigRevision): void {
  _sessionHistory[rev.task] = [
    rev,
    ...(_sessionHistory[rev.task] ?? []).filter((r) => r.revision_id !== rev.revision_id),
  ];
  _sessionHead[rev.task] = rev.revision_id;
  if (rev.task === _activeTask) syncFromSession(rev.task);
}

/** Advance the session head after a rerun created a new head (run_id). */
export function recordRerunHead(task: string, newHeadId: string): void {
  _sessionHead[task] = newHeadId;
  if (task === _activeTask) {
    _headRevisionId = newHeadId;
    _currentBaseRevision = newHeadId;
  }
}

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------

/**
 * POST a prompt-config revision with one 503/Retry-After retry, returning the
 * HTTP status so the caller can distinguish 201 / 409 / 422 / 503.
 * (The shared savePromptConfigRevision helper throws on non-2xx, which would
 * hide the 409 conflict / 422 validation detail.)
 */
export async function savePromptConfigChecked(
  bookId: string,
  write: PromptConfigWriteRequest,
): Promise<{ status: number; revision: PromptConfigRevision | null }> {
  const endpoint = `/api/pipeline/walks/${encodeURIComponent(bookId)}/config/revisions`;
  const init: RequestInit = {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(write),
  };
  let res = await fetch(endpoint, init);
  if (res.status === 503 && res.headers.get('Retry-After') != null) {
    const seconds = Math.max(0, Number(res.headers.get('Retry-After')) || 0);
    if (seconds > 0) await new Promise((r) => setTimeout(r, seconds * 1000));
    res = await fetch(endpoint, init);
  }
  if (!res.ok) return { status: res.status, revision: null };
  return { status: res.status, revision: (await res.json()) as PromptConfigRevision };
}

/** Client-side guard before any save: reject non-allow-listed raw JSON. */
function rawJsonGuardError(): string | null {
  if (inputValue('[data-role="pc-mode"]') !== 'raw') return null;
  const rawText = inputValue('[data-role="pc-raw"]');
  const parsed = safeParseRawJson(rawText);
  return parsed.ok ? null : parsed.error;
}

/** Side-effect-free validation of the current write. */
export async function validatePromptWrite(): Promise<void> {
  const container = editorContainer();
  if (!container || !_activeTask) return;
  let bookId: string;
  try {
    bookId = requireBookId();
  } catch (err) {
    renderErrors(err instanceof Error ? err.message : String(err));
    return;
  }
  const guard = rawJsonGuardError();
  if (guard) {
    renderErrors(`Raw JSON rejected: ${guard}`);
    showToast(`Raw JSON rejected: ${guard}`, 'error');
    return;
  }
  try {
    const write = buildWriteRequest();
    const res = await API.validatePromptConfig(bookId, write);
    if (res.valid) {
      renderErrors('');
      showToast('Prompt config valid — no side effects', 'success');
    } else {
      renderErrors(res.errors.join('; '));
      showToast('Prompt config validation failed', 'error');
    }
  } catch (err) {
    renderErrors(
      `Validation could not run: ${err instanceof Error ? err.message : String(err)}`,
    );
    showToast('Validation could not run', 'error');
  }
}

/** Save a new revision with base_revision; never auto-runs a walk. */
export async function savePromptWrite(): Promise<void> {
  const container = editorContainer();
  if (!container || !_activeTask) return;
  let bookId: string;
  try {
    bookId = requireBookId();
  } catch (err) {
    renderErrors(err instanceof Error ? err.message : String(err));
    return;
  }
  const guard = rawJsonGuardError();
  if (guard) {
    renderErrors(`Raw JSON rejected: ${guard}`);
    showToast(`Raw JSON rejected: ${guard}`, 'error');
    return;
  }
  const write = buildWriteRequest();
  const res = await savePromptConfigChecked(bookId, write);
  if (res.status === 409) {
    renderErrors(
      'Conflict: this revision is stale (base_revision no longer matches the head). Reload the config and re-apply your edit.',
    );
    showToast('Save conflicted (stale base_revision)', 'warning');
    return;
  }
  if (res.status === 422) {
    renderErrors('Save rejected by validation. Re-check the values and try again.');
    showToast('Save rejected (422 validation)', 'error');
    return;
  }
  if (res.status !== 201 && res.status !== 200) {
    renderErrors(`Save failed (HTTP ${res.status}).`);
    showToast(`Save failed (HTTP ${res.status})`, 'error');
    return;
  }
  if (!res.revision) {
    renderErrors('Save succeeded but returned no revision.');
    return;
  }
  recordRevision(res.revision);
  renderErrors('');
  showToast('Prompt config revision saved', 'success');
  await loadPromptConfig();
}

/**
 * Explicit, confirmed, scoped rerun. Requires a saved head revision and
 * confirm:true. Already-run (409) and invalid scope are surfaced, never
 * retried automatically.
 */
export async function rerunPromptConfirmed(): Promise<void> {
  const container = editorContainer();
  if (!container || !_activeTask) return;
  let bookId: string;
  try {
    bookId = requireBookId();
  } catch (err) {
    renderErrors(err instanceof Error ? err.message : String(err));
    return;
  }
  const task = _activeTask;
  const revisionId = _headRevisionId;
  if (!revisionId) {
    renderErrors('No saved revision to rerun. Save a revision first.');
    showToast('Save a revision before rerunning', 'warning');
    return;
  }
  const scope = (inputValue('[data-role="pc-scope"]') || 'book') as PersonaSceneScope;
  const sceneIds = Array.from(
    container.querySelectorAll<HTMLInputElement>('[data-role="pc-scene"]:checked'),
  ).map((cb) => cb.value);
  if (scope === 'scenes') {
    if (sceneIds.length === 0) {
      renderErrors('Scenes scope requires at least one selected scene.');
      showToast('Select at least one scene for scenes scope', 'error');
      return;
    }
    if (PROMPT_BOOK_GLOBAL_TASKS.includes(task)) {
      renderErrors('alias resolution is book-global and rejects scenes scope.');
      showToast('alias resolution is book-global; scenes scope rejected', 'error');
      return;
    }
  }
  const scopeText =
    scope === 'scenes'
      ? `scenes: ${sceneIds.join(', ')}`
      : 'book-wide';
  const ok = await showConfirm(
    `Rerun prompt config for task '${task}' (${PROMPT_TASK_LABELS[task] ?? task}) at ${scopeText} scope from revision ${revisionId}? This re-applies the revision as a new head.`,
  );
  if (!ok) return;
  try {
    const result = await API.rerunScopedWalk(bookId, {
      revision_id: revisionId,
      scope,
      scene_ids: scope === 'scenes' ? sceneIds : [],
      confirm: true,
    });
    // The rerun creates a new head revision (run_id) by re-applying the source
    // revision; advance base_revision so the next save does not 409.
    recordRerunHead(task, result.run_id);
    renderErrors('');
    showToast(`Rerun started (${result.run_id})`, 'success');
    await loadPromptConfig();
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    const alreadyRan = msg.includes('already_ran');
    renderErrors(
      alreadyRan
        ? `Rerun already ran for this revision+scope (409 already_ran). Save a new revision to rerun again.`
        : `Rerun rejected: ${msg}`,
    );
    showToast(alreadyRan ? 'Rerun already ran' : 'Rerun rejected', 'error');
  }
}

// ---------------------------------------------------------------------------
// Event wiring + init
// ---------------------------------------------------------------------------

let _promptEventsWired = false;

function wirePromptEvents(): void {
  if (_promptEventsWired) return;
  _promptEventsWired = true;
  document.addEventListener('click', async (e) => {
    const target = e.target as HTMLElement | null;
    const actionEl = target?.closest?.('[data-action]') as HTMLElement | null;
    if (!actionEl) return;
    const action = actionEl.getAttribute('data-action');
    if (action === 'pc-select') {
      const task = actionEl.getAttribute('data-task');
      if (task) await selectTask(task);
    } else if (action === 'pc-validate') {
      await validatePromptWrite();
    } else if (action === 'pc-save') {
      await savePromptWrite();
    } else if (action === 'pc-rerun') {
      await rerunPromptConfirmed();
    }
  });

  document.addEventListener('change', (e) => {
    const target = e.target as HTMLElement | null;
    if (!target) return;
    if (target.matches('[data-role="pc-mode"]')) {
      const container = editorContainer();
      if (!container) return;
      const mode = (target as HTMLSelectElement).value;
      const structured = container.querySelector<HTMLElement>('[data-role="pc-structured"]');
      const raw = container.querySelector<HTMLElement>('[data-role="pc-raw"]');
      if (structured) structured.style.display = mode === 'structured' ? '' : 'none';
      if (raw) raw.style.display = mode === 'raw' ? '' : 'none';
    } else if (target.matches('[data-role="pc-scope"]')) {
      const container = editorContainer();
      const scenes = container?.querySelector<HTMLElement>('[data-role="pc-scenes"]');
      if (scenes) {
        scenes.style.display = (target as HTMLSelectElement).value === 'scenes' ? '' : 'none';
      }
    }
  });

  document.addEventListener('click', (e) => {
    const link = (e.target as HTMLElement | null)?.closest?.('[data-tab]');
    if (link && link.getAttribute('data-tab') === PROMPT_CONFIG_TAB_NAME) {
      void loadPromptConfig();
    }
  });
}

/** Select a task and re-render the editor for it. */
export async function selectTask(task: string): Promise<void> {
  if (!_activeConfig || !_activeConfig.tasks[task]) return;
  _activeTask = task;
  // No list route: show the session-tracked history/head for this task (or none
  // if nothing was saved this session).
  syncFromSession(task);
  const container = editorContainer();
  if (container) container.innerHTML = renderPromptConfig();
}

/**
 * Create the tab pane and nav link (index.html is READ-ONLY for this sub-task,
 * so both are created here), wire delegated events, and load the effective
 * config. Idempotent.
 */
export function initPromptConfig(): void {
  if (_promptInitialized) return;
  _promptInitialized = true;

  // Tab pane after the workbench pane.
  if (!document.getElementById(PROMPT_CONFIG_TAB_ID)) {
    const pane = document.createElement('div');
    pane.id = PROMPT_CONFIG_TAB_ID;
    pane.className = 'tab-content';
    pane.style.display = 'none';
    const workbenchPane = document.getElementById('workbench-tab');
    if (workbenchPane && workbenchPane.parentElement) {
      workbenchPane.parentElement.appendChild(pane);
    } else {
      document.body.appendChild(pane);
    }
  }

  // Nav link after the workbench link.
  const navList = document.querySelector<HTMLElement>('ul.navbar-nav.me-auto');
  if (navList && !navList.querySelector(`[data-tab="${PROMPT_CONFIG_TAB_NAME}"]`)) {
    const item = document.createElement('li');
    item.className = 'nav-item ms-3 border-start border-secondary ps-3';
    item.innerHTML = `<a class="nav-link nav-advanced" href="#" role="button" data-tab="${PROMPT_CONFIG_TAB_NAME}">Prompts</a>`;
    const workbenchLink = navList.querySelector('[data-tab="workbench"]');
    if (workbenchLink && workbenchLink.closest('li')) {
      workbenchLink.closest('li')!.after(item);
    } else {
      navList.appendChild(item);
    }
  }

  wirePromptEvents();
  void loadPromptConfig();
}
