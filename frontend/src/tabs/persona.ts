/**
 * Persona editor for the Characters & Scenes workbench
 * (PipelineCharacterPersonaAPI.v1, DD-voice-persona-prompt-parity).
 *
 * A separately addressable editor opened from the character ledger. It owns:
 *   - structured profile fields (identity/appearance/manner/speech/role)
 *   - evidence citations/anchors, normalized aliases, book/scene scope
 *   - review state, protection, and derived (never assigned) voice consequences
 *   - side-effect-free validation, revisioned saves with `base_revision`,
 *     409 refresh/merge, 422, and 503 one-retry surfacing
 *   - explicit, confirmed scoped reruns that never mutate a character's
 *     resolved voice assignment
 *
 * The backend (app/pipeline/api_characters.py + persona.py) is READ-ONLY for
 * this module. Persona writes never carry a `voice_assignment_id` and reruns
 * never replace a protected head; this UI surfaces those guarantees.
 *
 * Accessibility: real <button>/<select>/<input> elements with delegated
 * actions; state is conveyed by text + badge labels, destructive/rerun acts
 * are explicitly confirmed, and protected state is shown as text, not color.
 */

import * as API from '../api';
import { state, selectWorkbench } from '../state';
import { showToast, showConfirm, escapeHtml } from '../utils';
import type {
  Persona,
  PersonaRevision,
  PersonaWriteRequest,
  PersonaRerunRequest,
  PersonaRerunResult,
  PersonaFieldKey,
  PersonaEvidence,
  PersonaReviewState,
  PersonaSceneScope,
  VoiceConsequences,
} from '../state';

// ---------------------------------------------------------------------------
// Module state
// ---------------------------------------------------------------------------

let _personaInitialized = false;
let _activeCharacterId: string | null = null;
let _currentBaseRevision = 0;

/** The five bounded profile field keys, in display order. */
export const PERSONA_FIELD_KEYS: PersonaFieldKey[] = [
  'identity',
  'appearance',
  'manner',
  'speech',
  'role',
];

/** The four review states, in display order. */
export const PERSONA_REVIEW_STATES: PersonaReviewState[] = [
  'draft',
  'needs_review',
  'accepted',
  'rejected',
];

/** DOM container id for the editor panel (created dynamically). */
export const PERSONA_EDITOR_ID = 'persona-editor';

// ---------------------------------------------------------------------------
// Pure render helpers
// ---------------------------------------------------------------------------

/** Render the derived voice consequences (explainable; never an assignment). */
export function renderVoiceConsequences(vc: VoiceConsequences | null | undefined): string {
  if (!vc) {
    return '<p class="text-muted small mb-0">No voice consequences yet. Validate to preview.</p>';
  }
  const assignment = vc.assignment
    ? `<span class="badge bg-info text-dark me-2">assigned: ${escapeHtml(vc.assignment)}</span>`
    : '<span class="badge bg-light text-dark border me-2">derived — no implicit assignment</span>';
  const hints = (vc.style_hints ?? [])
    .map((h) => `<li>${escapeHtml(h)}</li>`)
    .join('');
  return (
    `<div class="small mb-1">${assignment}<span class="text-muted">${escapeHtml(vc.explanation || '')}</span></div>` +
    (hints ? `<ul class="small text-muted mb-0">${hints}</ul>` : '')
  );
}

/** Render one evidence input row (anchor + optional quote/source/confidence). */
export function renderEvidenceRow(ev: PersonaEvidence | null, index: number): string {
  const anchor = ev?.anchor ?? '';
  const quote = ev?.quote ?? '';
  const source = ev?.source ?? '';
  const conf = ev?.confidence == null ? '' : String(ev.confidence);
  return (
    `<div class="row g-1 mb-1 align-items-center" data-role="evidence-row">` +
    `<div class="col-12 col-md-4"><input type="text" class="form-control form-control-sm" data-evidence="anchor" value="${escapeHtml(anchor)}" placeholder="anchor" aria-label="Evidence anchor ${index + 1}"></div>` +
    `<div class="col-12 col-md-3"><input type="text" class="form-control form-control-sm" data-evidence="quote" value="${escapeHtml(quote)}" placeholder="quote (optional)"></div>` +
    `<div class="col-12 col-md-2"><input type="text" class="form-control form-control-sm" data-evidence="source" value="${escapeHtml(source)}" placeholder="source (optional)"></div>` +
    `<div class="col-6 col-md-2"><input type="number" min="0" max="1" step="0.01" class="form-control form-control-sm" data-evidence="confidence" value="${escapeHtml(conf)}" placeholder="conf"></div>` +
    `<div class="col-6 col-md-1 text-end"><button type="button" class="btn btn-sm btn-outline-danger" data-action="persona-evidence-remove" aria-label="Remove evidence row">✕</button></div>` +
    `</div>`
  );
}

/** Render the editor body for a persona (or an empty new persona). */
export function renderPersonaEditor(
  persona: Persona | null,
  characterName: string,
  revisions: PersonaRevision[],
  sceneOptions: { scene_id: string; label: string }[],
): string {
  const fields = persona?.fields ?? {};
  const sceneScope: PersonaSceneScope = persona?.scene_scope ?? 'book';
  const sceneIds = persona?.scene_ids ?? [];
  const reviewState: PersonaReviewState = persona?.review_state ?? 'draft';
  const aliases = (persona?.aliases ?? []).join(', ');
  const evidence = persona?.evidence ?? [];
  const protectedFlag = Boolean(persona?.protected);

  const fieldInputs = PERSONA_FIELD_KEYS.map((key) => {
    const value = fields[key] ?? '';
    return (
      `<div class="mb-2">` +
      `<label class="form-label small mb-0" for="persona-field-${key}">${escapeHtml(key)}</label>` +
      `<textarea class="form-control form-control-sm" id="persona-field-${key}" data-field="${key}" rows="2">${escapeHtml(value)}</textarea>` +
      `</div>`
    );
  }).join('');

  const evidenceRows = (evidence.length ? evidence : [null]).map((ev, i) => renderEvidenceRow(ev, i)).join('');
  const evidenceBlock =
    `<div class="mb-2" data-role="evidence-list">${evidenceRows}</div>` +
    `<button type="button" class="btn btn-sm btn-outline-primary" data-action="persona-evidence-add">+ Add evidence</button>`;

  const sceneOptionsHtml = sceneOptions
    .map((s) => {
      const checked = sceneScope === 'scenes' && sceneIds.includes(s.scene_id) ? ' checked' : '';
      return (
        `<div class="form-check small">` +
        `<input class="form-check-input" type="checkbox" data-role="scene-check" value="${escapeHtml(s.scene_id)}" id="scene-check-${escapeHtml(s.scene_id)}"${checked}>` +
        `<label class="form-check-label" for="scene-check-${escapeHtml(s.scene_id)}">${escapeHtml(s.label)}</label></div>`
      );
    })
    .join('');

  const scopeSelect =
    `<select class="form-select form-select-sm" data-role="scene-scope" aria-label="Persona scene scope">` +
    `<option value="book"${sceneScope === 'book' ? ' selected' : ''}>Book-wide</option>` +
    `<option value="scenes"${sceneScope === 'scenes' ? ' selected' : ''}>Specific scenes</option>` +
    `</select>`;

  const reviewSelect =
    `<select class="form-select form-select-sm" data-role="review-state" aria-label="Review state">` +
    PERSONA_REVIEW_STATES.map((s) => `<option value="${s}"${reviewState === s ? ' selected' : ''}>${escapeHtml(s)}</option>`).join('') +
    `</select>`;

  const protectionBanner = protectedFlag
    ? '<div class="alert alert-warning py-1 small mb-2">This persona is <strong>protected</strong>. Edits preserve protection by default; uncheck Protected to unlock the new revision. Rerun is blocked.</div>'
    : '';

  const revisionRows = revisions.map((r) => (
    `<li class="list-group-item d-flex justify-content-between align-items-center small">` +
    `<span>rev ${escapeHtml(String(r.revision))} · ${escapeHtml(r.review_state)}${r.protected ? ' · <span class="badge bg-warning text-dark">protected</span>' : ''}</span>` +
    `<span class="text-muted ms-2">${escapeHtml(r.persona_id)}</span></li>`
  )).join('');
  const revisionHistory = revisions.length
    ? `<ul class="list-group">${revisionRows}</ul>`
    : '<p class="text-muted small mb-0">No revisions yet.</p>';

  return (
    `<h5 class="mt-1">${escapeHtml(characterName)}</h5>` +
    `${protectionBanner}` +
    `<div class="row">` +
    `<div class="col-md-6">` +
    `<h6>Profile fields</h6>${fieldInputs}` +
    `<h6 class="mt-2">Evidence</h6>${evidenceBlock}` +
    `</div>` +
    `<div class="col-md-6">` +
    `<h6>Aliases (comma-separated, normalized)</h6>` +
    `<input type="text" class="form-control form-control-sm mb-2" data-role="aliases" value="${escapeHtml(aliases)}" aria-label="Normalized aliases">` +
    `<h6>Scene scope</h6>${scopeSelect}` +
    `<div class="mt-2" data-role="scene-options">${sceneOptionsHtml}</div>` +
    `<h6 class="mt-3">Review state</h6>${reviewSelect}` +
    `<div class="form-check mt-2">` +
    `<input class="form-check-input" type="checkbox" data-role="protected" id="persona-protected"${protectedFlag ? ' checked' : ''}>` +
    `<label class="form-check-label" for="persona-protected">Protected (cannot be replaced by a rerun)</label></div>` +
    `<div class="mt-3">` +
    `<h6>Voice consequences</h6>` +
    `<div data-role="voice-consequences">${renderVoiceConsequences(persona?.voice_consequences)}</div>` +
    `</div>` +
    `</div>` +
    `</div>` +
    `<hr>` +
    `<div class="d-flex flex-wrap gap-2 align-items-center">` +
    `<button type="button" class="btn btn-outline-secondary btn-sm" data-action="persona-validate">Validate (side-effect free)</button>` +
    `<button type="button" class="btn btn-success btn-sm" data-action="persona-save">Save revision</button>` +
    `<button type="button" class="btn btn-outline-primary btn-sm" data-action="persona-rerun"${protectedFlag ? ' disabled' : ''}>Rerun scoped</button>` +
    `<span class="small text-muted ms-auto">base revision <code data-role="base-revision-display">${escapeHtml(String(persona?.revision ?? 0))}</code></span>` +
    `</div>` +
    `<div class="mt-2" data-role="persona-errors" aria-live="polite"></div>` +
    `<div class="mt-2">` +
    `<h6>Revision history</h6>${revisionHistory}` +
    `</div>`
  );
}

/** Build the reachable scene options from the workbench read-model. */
export function sceneOptionsFromWorkbench(): { scene_id: string; label: string }[] {
  const wb = selectWorkbench();
  if (!wb) return [];
  const out: { scene_id: string; label: string }[] = [];
  for (const chapter of wb.scenes) {
    for (const scene of chapter.scenes) {
      out.push({ scene_id: scene.scene_id, label: `${chapter.position}.${scene.position} (${scene.scene_id})` });
    }
  }
  return out;
}

// ---------------------------------------------------------------------------
// DOM helpers
// ---------------------------------------------------------------------------

function editorContainer(): HTMLElement | null {
  return document.getElementById(PERSONA_EDITOR_ID);
}

function requireBookId(): string {
  if (!state.pipelineBookId) throw new Error('No book selected');
  return state.pipelineBookId;
}

/** Read the current form into a PersonaWriteRequest (never mutates anything). */
export function buildWriteRequest(): PersonaWriteRequest {
  const container = editorContainer();
  const fields: Partial<Record<PersonaFieldKey, string>> = {};
  for (const key of PERSONA_FIELD_KEYS) {
    const el = container?.querySelector<HTMLTextAreaElement>(`[data-field="${key}"]`);
    const value = el?.value.trim();
    if (value) fields[key] = value;
  }
  const evidence: PersonaEvidence[] = [];
  container?.querySelectorAll<HTMLElement>('[data-role="evidence-row"]').forEach((row) => {
    const anchor = (row.querySelector<HTMLInputElement>('[data-evidence="anchor"]')?.value ?? '').trim();
    if (!anchor) return;
    const quote = (row.querySelector<HTMLInputElement>('[data-evidence="quote"]')?.value ?? '').trim() || null;
    const source = (row.querySelector<HTMLInputElement>('[data-evidence="source"]')?.value ?? '').trim() || null;
    const confRaw = (row.querySelector<HTMLInputElement>('[data-evidence="confidence"]')?.value ?? '').trim();
    let confidence: number | null = null;
    if (confRaw !== '') {
      const n = Number(confRaw);
      if (Number.isFinite(n)) confidence = n;
    }
    evidence.push({ anchor, quote, source, confidence });
  });
  const aliasesRaw = container?.querySelector<HTMLInputElement>('[data-role="aliases"]')?.value ?? '';
  const aliases = aliasesRaw
    .split(',')
    .map((a) => a.trim())
    .filter(Boolean);
  const sceneScope = (container?.querySelector<HTMLSelectElement>('[data-role="scene-scope"]')?.value ?? 'book') as PersonaSceneScope;
  const sceneIds = Array.from(
    container?.querySelectorAll<HTMLInputElement>('[data-role="scene-check"]:checked') ?? [],
  ).map((c) => c.value);
  const reviewState = (container?.querySelector<HTMLSelectElement>('[data-role="review-state"]')?.value ?? 'draft') as PersonaReviewState;
  const protectedFlag = Boolean(container?.querySelector<HTMLInputElement>('[data-role="protected"]')?.checked);
  return {
    base_revision: _currentBaseRevision,
    book_id: state.pipelineBookId,
    fields,
    evidence,
    aliases,
    scene_scope: sceneScope,
    scene_ids: sceneScope === 'scenes' ? sceneIds : [],
    review_state: reviewState,
    protected: protectedFlag,
  };
}

// ---------------------------------------------------------------------------
// API / action functions
// ---------------------------------------------------------------------------

/**
 * Shape of the backend 409 conflict body (``RevisionConflictDTO``):
 * ``{error, code, message, detail}`` where ``code`` discriminates
 * STALE_BASE_REVISION / PROTECTED_REVISION / ALREADY_RAN / CROSS_BOOK and
 * ``message`` carries the human-readable description. ``detail`` may be null.
 */
export interface PersonaConflict {
  error?: string;
  code?: string;
  message?: string;
  detail?: unknown;
}

/**
 * Read the structured error body from a failed persona Response. Prefers the
 * ``RevisionConflictDTO`` shape (``{error, code, message, detail}``), falls
 * back to a plain ``detail`` string (422 validation) or the FastAPI field-error
 * array, then to statusText. Returns null when no JSON body is present.
 */
async function readPersonaError(res: Response): Promise<PersonaConflict | null> {
  try {
    const body = (await res.json()) as PersonaConflict;
    if (body && typeof body === 'object') {
      if (typeof body.code === 'string' && typeof body.message === 'string') return body;
      if (typeof body.detail === 'string') return { message: body.detail };
      if (Array.isArray(body.detail)) {
        const msgs = (body.detail as { msg?: string }[])
          .map((d) => d?.msg ?? '')
          .filter(Boolean)
          .join('; ');
        if (msgs) return { message: msgs };
      }
    }
  } catch {
    /* no parseable JSON body */
  }
  return null;
}

/**
 * PUT a persona write with exactly ONE 503 retry (honoring Retry-After) and
 * return the HTTP status plus any parsed error body so the caller can
 * distinguish 409 (stale/protected, structured ``code``+``message``) from 422
 * (validation) from 503. Mirrors the shared one-retry convention.
 */
export async function savePersonaChecked(
  characterId: string,
  write: PersonaWriteRequest,
): Promise<{ status: number; persona: Persona | null; error: PersonaConflict | null }> {
  const endpoint = `/api/pipeline/characters/${encodeURIComponent(characterId)}/persona`;
  const init: RequestInit = {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(write),
  };
  let res = await fetch(endpoint, init);
  if (res.status === 503 && res.headers.get('Retry-After') != null) {
    const seconds = parseInt(res.headers.get('Retry-After') as string, 10);
    const delayMs = Number.isFinite(seconds) && seconds >= 0 ? seconds * 1000 : 1000;
    await new Promise<void>((resolve) => setTimeout(resolve, delayMs));
    res = await fetch(endpoint, init);
  }
  if (!res.ok) return { status: res.status, persona: null, error: await readPersonaError(res) };
  return { status: res.status, persona: (await res.json()) as Persona, error: null };
}

/**
 * POST a confirmed scoped persona rerun and return the HTTP status plus any
 * parsed error body. The rerun route rejects already-applied / protected
 * reruns with the structured ``RevisionConflictDTO`` (ALREADY_RAN /
 * PROTECTED_REVISION / CROSS_BOOK) — surfaced here instead of degrading to a
 * ``[object Object]`` via the shared ``post`` helper.
 */
export async function rerunPersonaChecked(
  characterId: string,
  rerun: PersonaRerunRequest,
): Promise<{ status: number; result: PersonaRerunResult | null; error: PersonaConflict | null }> {
  const endpoint = `/api/pipeline/characters/${encodeURIComponent(characterId)}/persona/rerun`;
  const init: RequestInit = {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(rerun),
  };
  const res = await fetch(endpoint, init);
  if (!res.ok) return { status: res.status, result: null, error: await readPersonaError(res) };
  return { status: res.status, result: (await res.json()) as PersonaRerunResult, error: null };
}

/** Load a persona head + revision history for a character into the editor. */
export async function openPersonaEditor(characterId: string): Promise<void> {
  const bookId = requireBookId();
  const wb = selectWorkbench();
  const character = wb?.characters.find((c) => c.id === characterId);
  const characterName = character?.name ?? characterId;
  _activeCharacterId = characterId;
  const container = editorContainer();
  if (!container) return;

  let persona: Persona | null = null;
  let revisions: PersonaRevision[] = [];
  try {
    try {
      persona = await API.getPersona(characterId);
    } catch (e) {
      // 404 → no persona yet; leave null and show the empty editor.
      const msg = e instanceof Error ? e.message : String(e);
      if (!msg.includes('404') && !/No persona revision/.test(msg)) throw e;
    }
    try {
      revisions = await API.listPersonaRevisions(characterId);
    } catch {
      revisions = [];
    }
    _currentBaseRevision = persona?.revision ?? 0;
    container.innerHTML = renderPersonaEditor(persona, characterName, revisions, sceneOptionsFromWorkbench());
    container.style.display = '';
    container.scrollIntoView?.({ behavior: 'smooth', block: 'nearest' });
    void bookId;
  } catch (e) {
    showToast('Failed to open persona editor: ' + (e instanceof Error ? e.message : String(e)), 'error');
  }
}

/** Close and reset the persona editor. */
export function closePersonaEditor(): void {
  const container = editorContainer();
  if (container) {
    container.style.display = 'none';
    container.innerHTML = '';
  }
  _activeCharacterId = null;
  _currentBaseRevision = 0;
}

function renderErrors(message: string): void {
  const el = editorContainer()?.querySelector('[data-role="persona-errors"]');
  if (el) el.innerHTML = `<div class="alert alert-danger py-1 small mb-0">${escapeHtml(message)}</div>`;
}

/** Side-effect-free validation of the current form; renders result inline. */
export async function validatePersonaWrite(): Promise<void> {
  if (!_activeCharacterId) return;
  const characterId = _activeCharacterId;
  try {
    const write = buildWriteRequest();
    const res = await API.validatePersona(characterId, write);
    const vc = editorContainer()?.querySelector('[data-role="voice-consequences"]');
    if (vc) vc.innerHTML = renderVoiceConsequences(res.voice_consequences);
    if (res.valid) {
      renderErrors('');
      showToast('Persona valid — no side effects', 'success');
    } else {
      renderErrors(res.errors.join('; '));
      showToast('Persona validation failed', 'error');
    }
  } catch (e) {
    showToast('Validation failed: ' + (e instanceof Error ? e.message : String(e)), 'error');
  }
}

/**
 * Save the current form as a new revision (sends base_revision). Surfaces:
 *   409 → stale base_revision (offer refresh/merge of the latest head)
 *   422 → validation errors inline
 *   503 → one automatic retry; if it persists, surfaced as an error
 */
export async function savePersonaWrite(): Promise<void> {
  if (!_activeCharacterId) return;
  const characterId = _activeCharacterId;
  const write = buildWriteRequest();
  const res = await savePersonaChecked(characterId, write);
  if (res.status === 409) {
    const code = res.error?.code;
    const conflictMsg =
      res.error?.message ??
      'Stale revision (conflict): the persona changed since you loaded it. Refresh to load the latest head, then re-apply your changes, or merge manually.';
    if (code === 'PROTECTED_REVISION') {
      renderErrors('Protected persona — this revision cannot be replaced by an edit. ' + conflictMsg);
      showToast('Protected persona — save rejected', 'error');
    } else {
      renderErrors(conflictMsg);
      showToast('Conflict — persona changed; refresh to re-apply', 'warning');
    }
    return;
  }
  if (res.status === 422) {
    renderErrors('Validation rejected the save: ' + (res.error?.message ?? 'fix the highlighted issues and retry.'));
    showToast('Persona save rejected (422)', 'error');
    return;
  }
  if (res.status !== 200 && res.status !== 201) {
    renderErrors(`Persona save failed with status ${res.status} (503 retry exhausted). Try again shortly.`);
    showToast(`Persona save failed (${res.status})`, 'error');
    return;
  }
  // Success: reload the head + history so base_revision advances.
  renderErrors('');
  showToast(`Persona revision ${res.persona?.revision ?? ''} saved`, 'success');
  await openPersonaEditor(characterId);
}

/**
 * Explicit, confirmed scoped rerun of the current head persona.
 * Requires explicit confirmation naming the affected scenes; never mutates a
 * character's resolved voice assignment; never replaces a protected head.
 */
export async function rerunPersonaConfirmed(): Promise<void> {
  if (!_activeCharacterId) return;
  const characterId = _activeCharacterId;
  const container = editorContainer();
  let head: Persona | null = null;
  try {
    try {
      head = await API.getPersona(characterId);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      if (!msg.includes('404') && !/No persona revision/.test(msg)) throw e;
    }
  } catch (e) {
    showToast('Failed to load persona: ' + (e instanceof Error ? e.message : String(e)), 'error');
    return;
  }
  if (!head) {
    showToast('No persona to rerun; save one first', 'warning');
    return;
  }
  if (head.protected) {
    renderErrors('This persona is protected and cannot be replaced by a rerun.');
    showToast('Protected persona — rerun blocked', 'error');
    return;
  }
  const scope = (container?.querySelector<HTMLSelectElement>('[data-role="scene-scope"]')?.value ?? 'book') as PersonaSceneScope;
  const sceneIds = Array.from(
    container?.querySelectorAll<HTMLInputElement>('[data-role="scene-check"]:checked') ?? [],
  ).map((c) => c.value);
  if (scope === 'scenes' && sceneIds.length === 0) {
    renderErrors('Scenes scope requires at least one selected scene for the rerun.');
    showToast('Select at least one scene for the rerun', 'error');
    return;
  }
  const sceneNames = sceneIds
    .map((id) => sceneOptionsFromWorkbench().find((s) => s.scene_id === id)?.label ?? id)
    .join(', ');
  const scopeText = scope === 'book' ? 'book-wide' : `scenes: ${sceneNames}`;
  const ok = await showConfirm(
    `Rerun persona for character '${head.character_id}' at scope ${scopeText}? ` +
    `This re-applies the persona as a new revision. It does NOT change the character's voice assignment.`,
  );
  if (!ok) return;

  try {
    const res = await rerunPersonaChecked(characterId, {
      revision_id: head.persona_id,
      scope,
      scene_ids: scope === 'scenes' ? sceneIds : [],
      confirm: true,
    });
    if (res.status === 409) {
      const code = res.error?.code;
      if (code === 'PROTECTED_REVISION') {
        renderErrors('Protected persona — rerun blocked: ' + (res.error?.message ?? 'cannot replace a protected head.'));
        showToast('Protected persona — rerun blocked', 'error');
      } else if (code === 'ALREADY_RAN') {
        renderErrors('Rerun already applied: ' + (res.error?.message ?? 'this revision+scope already produced the current head.'));
        showToast('Rerun already ran — no duplicate', 'warning');
      } else {
        renderErrors('Rerun conflict: ' + (res.error?.message ?? 'the head changed; refresh and retry.'));
        showToast('Rerun conflict', 'warning');
      }
      return;
    }
    if (res.status !== 200 && res.status !== 201) {
      renderErrors(`Rerun failed with status ${res.status}: ${res.error?.message ?? 'try again shortly.'}`);
      showToast(`Persona rerun failed (${res.status})`, 'error');
      return;
    }
    showToast(`Persona rerun started (${res.result?.run_id})`, 'success');
    await openPersonaEditor(characterId);
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    renderErrors(`Rerun rejected: ${msg}`);
    showToast('Persona rerun rejected: ' + msg, 'error');
  }
}

// ---------------------------------------------------------------------------
// initPersonaEditor
// ---------------------------------------------------------------------------

/** Create the editor container + wire delegated actions. Idempotent. */
export function initPersonaEditor(): void {
  if (_personaInitialized) return;
  _personaInitialized = true;

  const createPanel = (): void => {
    if (document.getElementById(PERSONA_EDITOR_ID)) return;
    const host = document.getElementById('workbench-tab');
    if (host) {
      const panel = document.createElement('div');
      panel.id = PERSONA_EDITOR_ID;
      panel.className = 'card mb-3';
      panel.style.display = 'none';
      host.appendChild(panel);
    }
  };
  const wireClicks = (): void => {
    const container = editorContainer();
    if (!container) return;

    container.addEventListener('click', async (e) => {
      const btn = (e.target as HTMLElement).closest<HTMLElement>('[data-action]');
      if (!btn) return;
      const action = btn.getAttribute('data-action');
      if (action === 'persona-close') {
        closePersonaEditor();
      } else if (action === 'persona-validate') {
        void validatePersonaWrite();
      } else if (action === 'persona-save') {
        await savePersonaWrite();
      } else if (action === 'persona-rerun') {
        await rerunPersonaConfirmed();
      } else if (action === 'persona-evidence-add') {
        const list = container.querySelector('[data-role="evidence-list"]');
        if (list) list.insertAdjacentHTML('beforeend', renderEvidenceRow(null, list.children.length));
      } else if (action === 'persona-evidence-remove') {
        const row = btn.closest('[data-role="evidence-row"]');
        row?.remove();
      }
    });
  };
  // Create the panel + wire clicks immediately when the DOM is already parsed
  // (e.g. tests / readyState beyond 'loading'), otherwise on DOMContentLoaded.
  const onReady = (): void => {
    createPanel();
    wireClicks();
  };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', onReady);
  } else {
    onReady();
  }
}
