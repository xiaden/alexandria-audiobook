/**
 * Combined Characters & Scenes workbench (walks 2b character discovery, 2c
 * alias resolution, 2d scene presence).
 *
 * Backend contracts (app/pipeline, READ-ONLY for this sub-task):
 *   - GET    /api/pipeline/workbench/{book_id}          → WorkbenchState
 *   - GET    /api/pipeline/workbench/{book_id}/config   → WorkbenchConfig
 *   - PUT    /api/pipeline/workbench/{book_id}/overrides {walk_name,key,value,base_revision}
 *   - DELETE /api/pipeline/workbench/{book_id}/overrides {walk_name,key,base_revision}
 *   - POST   /api/pipeline/workbench/{book_id}/alias-conversions/preview
 *            {canonical_id,member_ids,base_revision} → {preview_token,...}
 *   - POST   /api/pipeline/workbench/{book_id}/alias-conversions/commit
 *            {preview_token,base_revision,confirm_consequences}
 *   - PUT    /api/pipeline/workbench/{book_id}/presence
 *            {scene_id,character_id,relation_type,base_revision}
 *   - POST   /api/pipeline/workbench/{book_id}/reruns
 *            {walk_name,scope:'book'|'scenes',scene_ids?,preserve_manual_decisions,base_revision}
 *   - POST   /api/pipeline/workbench/{book_id}/decisions/{decision_id}/undo {base_revision}
 *   - Review resolution stays on the existing surface (accept/reject/override)
 *     with {item_id,base_revision} bodies.
 *
 * Accessibility: every journey is keyboard-reachable (real <button>/<select>
 * elements, delegated actions), state is conveyed by text + badge label (not
 * color alone), destructive acts are confirmed, and decisions are undoable.
 */

import * as API from '../api';
import { state } from '../state';
import { showToast, showConfirm, escapeHtml } from '../utils';
import {
  WorkbenchState,
  WorkbenchConfig,
  WorkbenchWalkName,
  WORKBENCH_WALK_NAMES,
  WORKBENCH_WALK_LABELS,
  RERUN_INVALIDATION,
  WorkbenchReviewItem,
  selectReviewItems,
  selectScenePresence,
  selectCanonicalCharacters,
  selectActiveAliases,
  sourceLabel,
} from '../state';

// ---------------------------------------------------------------------------
// Module state
// ---------------------------------------------------------------------------

let _workbenchInitialized = false;

/** Currently selected scene_id in the navigator. */
let _selectedSceneId: string | null = null;

/** In-progress alias preview token + summary (single-use, ten-minute TTL). */
let _aliasPreview: { token: string; summary: string } | null = null;

/** Undo stack of {label, decision_id} for the most recent human decisions. */
let _undoStack: { label: string; decisionId: string }[] = [];

// ---------------------------------------------------------------------------
// Small shared helpers
// ---------------------------------------------------------------------------

/** Parse a JSON-encoded aliases string into a display list. */
export function parseAliases(aliasesJson: string | null | undefined): string[] {
  if (!aliasesJson) return [];
  try {
    const parsed = JSON.parse(aliasesJson);
    if (Array.isArray(parsed)) {
      return parsed.map((a) => String(a));
    }
  } catch {
    /* malformed — fall through to empty */
  }
  return [];
}

/** Format a confidence value as a percentage string. */
export function formatConfidence(confidence: number | null | undefined): string {
  if (confidence == null || !Number.isFinite(confidence)) return '—';
  return `${Math.round(confidence * 100)}%`;
}

/** Whether a decision is undoable (active = not yet terminal). */
export function isUndoable(status: string | null | undefined): boolean {
  return status === 'active';
}

/** Reset the in-memory undo stack (used on load/refresh and in tests). */
export function clearUndoStack(): void {
  _undoStack = [];
}

/** The current undo stack (for tests). */
export function getUndoStack(): { label: string; decisionId: string }[] {
  return _undoStack.slice();
}

function pushUndo(label: string, decisionId: string): void {
  _undoStack.push({ label, decisionId });
}

// ---------------------------------------------------------------------------
// Pure render helpers
// ---------------------------------------------------------------------------

/**
 * Render the scene navigator: a flat keyboard-focusable list of
 * chapter → scene entries. Selected scene is conveyed by `aria-current` AND a
 * text marker (non-color-only), not by background color alone.
 */
export function renderSceneNavigator(wb: WorkbenchState | null, selectedSceneId: string | null): string {
  if (!wb || wb.scenes.length === 0) {
    return '<p class="text-muted">No scenes available. Run a walk to populate the workbench.</p>';
  }
  const parts: string[] = [];
  for (const chapter of wb.scenes) {
    for (const scene of chapter.scenes) {
      const sel = selectedSceneId === scene.scene_id;
      const selectedMarker = sel ? ' · <span class="visually-hidden">selected </span><i class="fas fa-check ms-1" aria-hidden="true"></i>' : '';
      parts.push(
        `<button type="button" class="list-group-item list-group-item-action d-flex justify-content-between align-items-center${sel ? ' active' : ''}"` +
        ` data-action="scene-select" data-scene-id="${escapeHtml(scene.scene_id)}"` +
        ` aria-current="${sel ? 'true' : 'false'}">` +
        `<span>${escapeHtml(chapter.position)}.${escapeHtml(scene.position)}</span>${selectedMarker}</button>`,
      );
    }
  }
  return `<div class="list-group" role="list">${parts.join('')}</div>`;
}

/**
 * Render span evidence for the selected scene. Spans carry their immutable
 * `id` as data attributes so the anchor rendering is stable (never a
 * presentation index).
 */
export function renderSpanEvidence(wb: WorkbenchState | null, sceneId: string | null): string {
  if (!wb || !sceneId) return '';
  const scene = wb.scenes
    .flatMap((ch) => ch.scenes.map((s) => ({ scene: s, chapterId: ch.chapter_id })))
    .find(({ scene }) => scene.scene_id === sceneId);
  if (!scene) return '<p class="text-muted">Select a scene to inspect its highlighted evidence.</p>';
  const chapterId = scene.chapterId;
  const sceneObj = scene.scene;

  const paras = sceneObj.paragraphs.map((para) => {
    const spans = para.spans.map((span) => {
      const typeLabel = span.span_type ? `<span class="badge bg-secondary me-1">${escapeHtml(span.span_type)}</span>` : '';
      const instruct = span.instruct ? `<span class="text-muted small">— ${escapeHtml(span.instruct)}</span>` : '';
      return (
        `<span class="badge bg-light text-dark border me-1 mb-1 p-2 text-start"` +
        ` data-span-id="${escapeHtml(span.id)}" data-paragraph-id="${escapeHtml(para.paragraph_id)}"` +
        ` data-chapter-id="${escapeHtml(chapterId)}">${typeLabel}${escapeHtml(span.text)} ${instruct}</span>`
      );
    });
    return `<div class="mb-2"><div class="small text-muted">Paragraph ${escapeHtml(para.position)}</div><div class="d-flex flex-wrap">${spans.join('')}</div></div>`;
  });
  return `<div>${paras.join('')}</div>`;
}

/** Render the character ledger with a confidence badge and voice assignment. */
export function renderCharacterLedger(wb: WorkbenchState | null): string {
  if (!wb || wb.characters.length === 0) {
    return '<p class="text-muted">No characters yet. Run 2b character discovery.</p>';
  }
  const rows = wb.characters.map((c) => {
    const aliases = parseAliases(c.aliases);
    const aliasBadges = aliases.length
      ? aliases.map((a) => `<span class="badge bg-light text-dark border me-1">${escapeHtml(a)}</span>`).join('')
      : '<span class="text-muted small">no aliases</span>';
    const voice = c.voice_assignment_id ? `<span class="badge bg-info text-dark">${escapeHtml(c.voice_assignment_id)}</span>` : '<span class="text-muted small">no voice</span>';
    return (
      `<li class="list-group-item d-flex justify-content-between align-items-start">` +
      `<div><div class="fw-semibold">${escapeHtml(c.name)}</div><div class="small">${aliasBadges}</div></div>` +
      `<div class="text-end">${voice}</div></li>`
    );
  });
  return `<ul class="list-group">${rows.join('')}</ul>`;
}

/** Render active alias-merge rows. */
export function renderAliasLedger(wb: WorkbenchState | null): string {
  if (!wb) return '';
  const active = selectActiveAliases(wb);
  if (active.length === 0) {
    return '<p class="text-muted">No alias merges. Use the alias conversion panel below to unify names.</p>';
  }
  const rows = active.map((a) => (
    `<li class="list-group-item d-flex justify-content-between align-items-center">` +
    `<span>${escapeHtml(a.member_name)} → <strong>${escapeHtml(a.canonical_name)}</strong></span>` +
    `<button type="button" class="btn btn-sm btn-outline-secondary" data-action="alias-unmerge" data-merge-id="${escapeHtml(a.merge_id)}" data-decision-label="${escapeHtml(a.member_name)} → ${escapeHtml(a.canonical_name)}">Unmerge</button>` +
    `</li>`
  ));
  return `<ul class="list-group">${rows.join('')}</ul>`;
}

/** Render presence rows for a scene with non-color relation state. */
export function renderScenePresence(wb: WorkbenchState | null, sceneId: string | null): string {
  if (!wb || !sceneId) return '<p class="text-muted">Select a scene to edit its presence.</p>';
  const rows = selectScenePresence(wb, sceneId);
  if (rows.length === 0) {
    return '<p class="text-muted">No presence entries for this scene.</p>';
  }
  const items = rows.map((p) => {
    const char = wb.characters.find((c) => c.id === p.character_id);
    const name = char ? char.name : p.character_id;
    const relationLabel = p.relation_type;
    const sourceText = p.human_override ? 'manual' : p.source;
    const manualMark = p.human_override ? ' · <span class="fw-semibold">manual</span>' : '';
    const relationMark = `<span class="badge bg-primary text-white">${escapeHtml(relationLabel)}</span>`;
    return (
      `<li class="list-group-item d-flex justify-content-between align-items-center">` +
      `<div><span class="fw-semibold">${escapeHtml(name)}</span> ${relationMark}` +
      `<div class="small text-muted">source: ${escapeHtml(sourceText)} · conf ${formatConfidence(p.confidence)}${manualMark}</div></div>` +
      `<div class="btn-group" role="group" aria-label="Presence for ${escapeHtml(name)}">` +
      `<select class="form-select form-select-sm" data-action="presence-change" data-scene-id="${escapeHtml(p.scene_id)}" data-character-id="${escapeHtml(p.character_id)}" aria-label="Set presence relation for ${escapeHtml(name)}">` +
      `<option value="present"${p.relation_type === 'present' ? ' selected' : ''}>Present</option>` +
      `<option value="speaker"${p.relation_type === 'speaker' ? ' selected' : ''}>Speaker</option>` +
      `<option value="absent"${p.relation_type === 'absent' ? ' selected' : ''}>Absent</option>` +
      `</select></div></li>`
    );
  });
  return `<ul class="list-group">${items.join('')}</ul>`;
}

/** Render effective config source badges for a walk. */
export function renderConfigSources(wb: WorkbenchState | null, walkName: string): string {
  if (!wb) return '';
  const cfg = wb.effective_config[walkName];
  if (!cfg || !cfg.sources) return '<p class="text-muted">No effective configuration.</p>';
  const keys = Object.keys(cfg.sources).sort();
  if (keys.length === 0) return '<p class="text-muted">No effective configuration.</p>';
  const rows = keys.map((key) => {
    const label = sourceLabel(cfg.sources[key]);
    const value = cfg.values?.[key];
    const valText = value == null ? '' : typeof value === 'object' ? JSON.stringify(value) : String(value);
    return (
      `<li class="list-group-item d-flex justify-content-between align-items-center">` +
      `<span class="fw-semibold">${escapeHtml(key)}</span>` +
      `<span><code class="me-2">${escapeHtml(valText)}</code>` +
      `<span class="badge bg-secondary text-white">${escapeHtml(label)}</span></span></li>`
    );
  });
  return `<ul class="list-group">${rows.join('')}</ul>`;
}

/** Render conflicts with clear non-color code + description. */
export function renderConflicts(wb: WorkbenchState | null): string {
  if (!wb) return '';
  if (wb.conflicts.length === 0) {
    return '<p class="text-muted">No conflicts between manual and generated decisions.</p>';
  }
  const items = wb.conflicts.map((c) => (
    `<li class="list-group-item">` +
    `<div class="fw-semibold">${escapeHtml(c.code)}</div>` +
    `<div class="small text-muted">current: <code>${escapeHtml(c.current_value == null ? '' : String(c.current_value))}</code>` +
    ` · requested: <code>${escapeHtml(c.requested_value == null ? '' : String(c.requested_value))}</code>` +
    `${c.decision_id ? ` · decision: <code>${escapeHtml(c.decision_id)}</code>` : ''}</div></li>`
  ));
  return `<ul class="list-group">${items.join('')}</ul>`;
}

/** Render recent walk runs with explicit status text. */
export function renderRuns(wb: WorkbenchState | null): string {
  if (!wb) return '';
  if (wb.runs.length === 0) return '<p class="text-muted">No runs yet.</p>';
  const items = wb.runs.slice().reverse().map((r) => (
    `<li class="list-group-item d-flex justify-content-between align-items-center">` +
    `<span>${escapeHtml(WORKBENCH_WALK_LABELS[r.walk_name] ?? r.walk_name)}</span>` +
    `<span class="badge bg-primary text-white">${escapeHtml(r.status)}</span>` +
    `<span class="small text-muted ms-2">${escapeHtml(r.run_id)}</span>` +
    `${r.error ? `<span class="text-danger small ms-2">${escapeHtml(r.error)}</span>` : ''}</li>`
  ));
  return `<ul class="list-group">${items.join('')}</ul>`;
}

// ---------------------------------------------------------------------------
// Data functions (pipeline-prefixed endpoints; 503 one-retry writes)
// ---------------------------------------------------------------------------

/** Base path helper: guards on book, throws if none selected. */
function requireBookId(): string {
  if (!state.pipelineBookId) {
    throw new Error('No book selected');
  }
  return state.pipelineBookId;
}

/** Load the full workbench read-model. */
export async function loadWorkbench(resetUndo: boolean = false): Promise<void> {
  const bookId = requireBookId();
  try {
    const wb = await API.get<WorkbenchState>(`/api/pipeline/workbench/${bookId}`);
    state.workbench = wb;
    // Only a full top-level load (tab open / refresh / book change) resets the
    // undo stack. Action-triggered reloads (resetUndo=false) must preserve it
    // so the user can undo the decision they just made.
    if (resetUndo) clearUndoStack();
    const nav = document.getElementById('workbench-navigator');
    if (nav) nav.innerHTML = renderSceneNavigator(wb, _selectedSceneId);
    const ledger = document.getElementById('workbench-ledger');
    if (ledger) ledger.innerHTML = renderCharacterLedger(wb);
    const aliases = document.getElementById('workbench-aliases');
    if (aliases) aliases.innerHTML = renderAliasLedger(wb);
    const conflicts = document.getElementById('workbench-conflicts');
    if (conflicts) conflicts.innerHTML = renderConflicts(wb);
    const runs = document.getElementById('workbench-runs');
    if (runs) runs.innerHTML = renderRuns(wb);
    // If a scene is selected, refresh its evidence + presence.
    refreshSceneDetail(wb);
  } catch (e) {
    showToast('Failed to load workbench: ' + (e instanceof Error ? e.message : String(e)), 'error');
  }
}

/** Refresh the selected scene's evidence + presence panes. */
export function refreshSceneDetail(wb: WorkbenchState | null): void {
  if (!wb || !_selectedSceneId) return;
  const evidence = document.getElementById('workbench-span-evidence');
  if (evidence) evidence.innerHTML = renderSpanEvidence(wb, _selectedSceneId);
  const presence = document.getElementById('workbench-presence');
  if (presence) presence.innerHTML = renderScenePresence(wb, _selectedSceneId);
}

/** Load the per-walk config edit model and render the setup panel. */
export async function loadWorkbenchConfig(): Promise<void> {
  const bookId = requireBookId();
  try {
    const cfg = await API.get<WorkbenchConfig>(`/api/pipeline/workbench/${bookId}/config`);
    state.workbenchConfig = cfg;
    const setup = document.getElementById('workbench-setup');
    if (setup) setup.innerHTML = renderSetupPanel(cfg);
    const sources = document.getElementById('workbench-config-sources');
    if (sources) sources.innerHTML = renderAllConfigSources(cfg);
  } catch (e) {
    showToast('Failed to load workbench config: ' + (e instanceof Error ? e.message : String(e)), 'error');
  }
}

/** Render the collapsible per-walk setup panel (Journey D). */
export function renderSetupPanel(cfg: WorkbenchConfig): string {
  if (!cfg) return '';
  const walkCards = WORKBENCH_WALK_NAMES.map((walkName) => {
    const label = WORKBENCH_WALK_LABELS[walkName] ?? walkName;
    const keys = ['model_name', 'reasoning_effort', 'temperature'];
    const fields = keys.map((key) => {
      const effective = cfg.effective?.[walkName]?.[key];
      const effectiveText = effective == null ? '' : typeof effective === 'object' ? JSON.stringify(effective) : String(effective);
      const source = cfg.source?.[walkName]?.[key];
      return (
        `<div class="mb-2 row align-items-center">` +
        `<label class="col-3 col-form-label" for="wb-cfg-${walkName}-${key}">${escapeHtml(key)}</label>` +
        `<div class="col-5"><input type="text" class="form-control form-control-sm" id="wb-cfg-${walkName}-${key}"` +
        ` data-walk="${walkName}" data-key="${key}" value="${escapeHtml(effectiveText)}" autocomplete="off"></div>` +
        `<div class="col-4"><span class="badge bg-secondary text-white" data-role="source-badge" data-walk="${walkName}" data-key="${key}">${escapeHtml(sourceLabel(source))}</span></div>` +
        `</div>`
      );
    }).join('');
    // Prompt is a textarea (raw prompt omitted by backend; editable for override).
    const promptEffective = cfg.effective?.[walkName]?.prompt;
    const promptSource = cfg.source?.[walkName]?.prompt;
    const promptField =
      `<div class="mb-2 row align-items-center">` +
      `<label class="col-3 col-form-label" for="wb-cfg-${walkName}-prompt">prompt</label>` +
      `<div class="col-5"><textarea class="form-control form-control-sm" id="wb-cfg-${walkName}-prompt" rows="2"` +
      ` data-walk="${walkName}" data-key="prompt" autocomplete="off">${escapeHtml(promptEffective == null ? '' : String(promptEffective))}</textarea></div>` +
      `<div class="col-4"><span class="badge bg-secondary text-white" data-role="source-badge" data-walk="${walkName}" data-key="prompt">${escapeHtml(sourceLabel(promptSource))}</span></div>` +
      `</div>`;
    const validation = cfg.validation_errors && cfg.validation_errors.length
      ? `<div class="alert alert-warning py-1 small mb-2">${cfg.validation_errors.map((v) => escapeHtml(v)).join('<br>')}</div>`
      : '';
    return (
      `<div class="card mb-3">` +
      `<div class="card-header d-flex justify-content-between align-items-center">` +
      `<button type="button" class="btn btn-link p-0 text-decoration-none" data-action="walk-setup-toggle" data-walk="${walkName}" aria-expanded="false">` +
      `<i class="fas fa-chevron-down me-1" aria-hidden="true"></i>${escapeHtml(label)}</button>` +
      `<span class="badge bg-secondary text-white">${escapeHtml(sourceLabel(cfg.source?.[walkName]?.model_name))}</span></div>` +
      `<div class="card-body" data-role="walk-setup-body" style="display:none">` +
      `${validation}${fields}${promptField}` +
      `<div class="mt-2"><button type="button" class="btn btn-success btn-sm" data-action="save-override" data-walk="${walkName}"><i class="fas fa-save me-1"></i>Save</button>` +
      ` <button type="button" class="btn btn-outline-danger btn-sm" data-action="clear-override" data-walk="${walkName}">Reset to default</button></div>` +
      `</div></div>`
    );
  });
  return walkCards.join('');
}

/** Render effective config source badges for all walks. */
export function renderAllConfigSources(cfg: WorkbenchConfig): string {
  if (!cfg) return '';
  const walks = Object.keys(cfg.effective ?? {});
  if (walks.length === 0) return '<p class="text-muted">No effective configuration.</p>';
  const items = walks.map((walkName) => {
    const sources = cfg.source?.[walkName] ?? {};
    const keys = Object.keys(sources).sort();
    const badges = keys.map((key) => (
      `<span class="badge bg-light text-dark border me-2 mb-1"><span class="fw-semibold">${escapeHtml(key)}</span>: ${escapeHtml(sourceLabel(sources[key]))}</span>`
    )).join('');
    return `<li class="list-group-item"><span class="fw-semibold">${escapeHtml(WORKBENCH_WALK_LABELS[walkName] ?? walkName)}</span><div class="mt-1">${badges}</div></li>`;
  });
  return `<ul class="list-group">${items.join('')}</ul>`;
}

// ---------------------------------------------------------------------------
// Actions (Journeys A–D)
// ---------------------------------------------------------------------------

/**
 * Journey A — resolve a review item (accept / reject / typed override).
 * Stays on the existing review surface with base_revision.
 */
export async function resolveReviewItem(
  itemId: string,
  action: 'accept' | 'reject' | 'override',
  baseRevision: number,
  newValue?: string,
): Promise<void> {
  const bookId = requireBookId();
  const wb = state.workbench;
  if (!wb) {
    showToast('Workbench is not loaded; refresh first', 'error');
    return;
  }
  try {
    let res: { item_id: string; decision_id: string; status: string; conflict?: unknown };
    if (action === 'accept') {
      res = await API.post('/api/pipeline/review/accept', { item_id: itemId, base_revision: baseRevision });
    } else if (action === 'reject') {
      res = await API.post('/api/pipeline/review/reject', { item_id: itemId, base_revision: baseRevision });
    } else {
      res = await API.post('/api/pipeline/review/override', { item_id: itemId, new_value: newValue, base_revision: baseRevision });
    }
    if (res.conflict) {
      showToast(`Conflict: ${JSON.stringify(res.conflict)}`, 'warning');
    } else {
      showToast(`Review ${action === 'override' ? 'override' : action} recorded`, 'success');
    }
    if (res.decision_id) pushUndo(`${action} review item`, res.decision_id);
    await loadWorkbench();
  } catch (e) {
    showToast(`Failed to ${action} review item: ` + (e instanceof Error ? e.message : String(e)), 'error');
  }
}

/**
 * Journey B — preview an alias conversion (book-scoped, single-use token).
 * Returns the preview summary string for display and stores the token.
 */
export async function previewAliasConversion(
  canonicalId: string,
  memberIds: string[],
): Promise<string | null> {
  const bookId = requireBookId();
  const wb = state.workbench;
  if (!wb) return null;
  try {
    const preview = await API.post<{
      preview_token: string;
      expires_ms: number;
      affected_rows: unknown[];
      protected_decisions: unknown[];
      voice_assignments: unknown[];
      downstream_invalidations: unknown[];
      conflicts: unknown[];
    }>('/api/pipeline/workbench/' + bookId + '/alias-conversions/preview', {
      canonical_id: canonicalId,
      member_ids: memberIds,
      base_revision: wb.generation_revision,
    });
    _aliasPreview = {
      token: preview.preview_token,
      summary:
        `Alias conversion preview: ${preview.affected_rows.length} affected rows, ` +
        `${preview.voice_assignments.length} voice assignment(s), ` +
        `${preview.downstream_invalidations.length} downstream walk(s), ` +
        `${preview.conflicts.length} conflict(s)`,
    };
    return _aliasPreview.summary;
  } catch (e) {
    showToast('Failed to preview alias conversion: ' + (e instanceof Error ? e.message : String(e)), 'error');
    return null;
  }
}

/** Journey B — commit a previously previewed alias conversion (confirmed). */
export async function commitAliasConversion(confirmConsequences: boolean): Promise<void> {
  const bookId = requireBookId();
  const wb = state.workbench;
  if (!wb) return;
  if (!_aliasPreview) {
    showToast('No alias preview to commit; preview first', 'warning');
    return;
  }
  if (!confirmConsequences) {
    showToast('Alias conversion requires confirmation of consequences', 'warning');
    return;
  }
  try {
    const res = await API.post<{ decision_id: string; status: string; conflict: boolean }>(
      '/api/pipeline/workbench/' + bookId + '/alias-conversions/commit',
      { preview_token: _aliasPreview.token, base_revision: wb.generation_revision, confirm_consequences: true },
    );
    _aliasPreview = null;
    if (res.decision_id) pushUndo('alias conversion', res.decision_id);
    showToast('Alias conversion applied', 'success');
    await loadWorkbench();
  } catch (e) {
    showToast('Failed to commit alias conversion: ' + (e instanceof Error ? e.message : String(e)), 'error');
  }
}

/** Journey C — save a scene presence decision. */
export async function savePresence(
  sceneId: string,
  characterId: string,
  relationType: 'present' | 'speaker' | 'absent',
): Promise<void> {
  const bookId = requireBookId();
  const wb = state.workbench;
  if (!wb) return;
  // Destructive removal (absent) requires explicit confirmation.
  if (relationType === 'absent') {
    const ok = await showConfirm('Removing this character from the scene (absent) creates a tombstone so the walk cannot re-add it. Continue?');
    if (!ok) return;
  }
  try {
    const res = await API.putWithRetryOnce<{ decision_id: string; status: string; conflict: unknown }>(
      `/api/pipeline/workbench/${bookId}/presence`,
      { scene_id: sceneId, character_id: characterId, relation_type: relationType, base_revision: wb.generation_revision },
    );
    if (res.conflict) {
      showToast(`Presence conflict: ${JSON.stringify(res.conflict)}`, 'warning');
    } else {
      showToast(`Presence set to ${relationType}`, 'success');
    }
    if (res.decision_id) pushUndo(`presence ${relationType}`, res.decision_id);
    await loadWorkbench();
  } catch (e) {
    showToast('Failed to save presence: ' + (e instanceof Error ? e.message : String(e)), 'error');
  }
}

/** Journey D — save a per-walk config override (PUT with one 503 retry). */
export async function saveOverride(walkName: string, key: string, value: unknown): Promise<void> {
  const bookId = requireBookId();
  const wb = state.workbench;
  if (!wb) return;
  try {
    await API.putWithRetryOnce(`/api/pipeline/workbench/${bookId}/overrides`, {
      walk_name: walkName,
      key,
      value,
      base_revision: wb.generation_revision,
    });
    showToast(`Saved ${key} override`, 'success');
    await loadWorkbench();
    await loadWorkbenchConfig();
  } catch (e) {
    showToast(`Failed to save ${key} override: ` + (e instanceof Error ? e.message : String(e)), 'error');
  }
}

/** Journey D — clear a per-walk config override (DELETE with one 503 retry). */
export async function clearOverride(walkName: string, key: string): Promise<void> {
  const bookId = requireBookId();
  const wb = state.workbench;
  if (!wb) return;
  try {
    await API.delWithRetryOnce(`/api/pipeline/workbench/${bookId}/overrides`, {
      walk_name: walkName,
      key,
      base_revision: wb.generation_revision,
    });
    showToast(`Reset ${key} to default`, 'success');
    await loadWorkbench();
    await loadWorkbenchConfig();
  } catch (e) {
    showToast(`Failed to reset ${key}: ` + (e instanceof Error ? e.message : String(e)), 'error');
  }
}

/** Rerun a walk with an explicit scope ('book' or explicit 'scenes'). */
export async function rerunWalk(
  walkName: WorkbenchWalkName,
  scope: 'book' | 'scenes',
  sceneIds: string[] = [],
): Promise<void> {
  const bookId = requireBookId();
  const wb = state.workbench;
  if (!wb) return;
  if (scope === 'scenes') {
    if (sceneIds.length === 0) {
      showToast('Scenes-scoped rerun requires at least one selected scene', 'error');
      return;
    }
    if (walkName === 'walk_2c_alias_resolution') {
      showToast('Alias resolution (2c) is book-global and cannot be scenes-scoped', 'error');
      return;
    }
  }
  const invalidated = RERUN_INVALIDATION[walkName] ?? [];
  const msg = `Run ${WORKBENCH_WALK_LABELS[walkName] ?? walkName} (${scope} scope)?` +
    (invalidated.length ? ` This invalidates downstream: ${invalidated.join(', ')}.` : '') +
    ' Manual decisions are preserved.';
  const ok = await showConfirm(msg);
  if (!ok) return;
  try {
    const res = await API.post<{ run_id: string; status: string }>(`/api/pipeline/workbench/${bookId}/reruns`, {
      walk_name: walkName,
      scope,
      scene_ids: scope === 'scenes' ? sceneIds : undefined,
      preserve_manual_decisions: true,
      base_revision: wb.generation_revision,
    });
    showToast(`Run started (${res.run_id})`, 'success');
    await loadWorkbench();
  } catch (e) {
    showToast('Failed to start rerun: ' + (e instanceof Error ? e.message : String(e)), 'error');
  }
}

/** Undo a decision by id (revision-checked; 409 when a newer decision exists). */
export async function undoDecision(decisionId: string): Promise<void> {
  const bookId = requireBookId();
  const wb = state.workbench;
  if (!wb) return;
  try {
    await API.post(`/api/pipeline/workbench/${bookId}/decisions/${encodeURIComponent(decisionId)}/undo`, {
      base_revision: wb.generation_revision,
    });
    showToast('Decision undone', 'success');
    // Remove from local undo stack by decision id.
    _undoStack = _undoStack.filter((u) => u.decisionId !== decisionId);
    await loadWorkbench();
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    showToast(`Failed to undo decision: ${msg}`, 'error');
  }
}

/** Unmerge an alias member (delegates to the undo route for alias-merge decisions). */
export async function unmergeAlias(mergeId: string): Promise<void> {
  const bookId = requireBookId();
  const wb = state.workbench;
  if (!wb) return;
  const ok = await showConfirm('Unmerge this alias? This reverses the alias conversion.');
  if (!ok) return;
  try {
    const res = await API.post<{ decision_id?: string }>(`/api/pipeline/workbench/${bookId}/decisions/${encodeURIComponent(mergeId)}/undo`, {
      base_revision: wb.generation_revision,
    });
    showToast('Alias unmerged', 'success');
    if (res.decision_id) pushUndo('unmerge', res.decision_id);
    await loadWorkbench();
  } catch (e) {
    showToast('Failed to unmerge alias: ' + (e instanceof Error ? e.message : String(e)), 'error');
  }
}

// ---------------------------------------------------------------------------
// initWorkbench
// ---------------------------------------------------------------------------

/**
 * Wire up the workbench tab: loads on tab activation, delegates row actions,
 * and exposes the undo button. Idempotent.
 */
export function initWorkbench(): void {
  if (_workbenchInitialized) return;
  _workbenchInitialized = true;

  document.addEventListener('DOMContentLoaded', () => {
    const nav = document.getElementById('workbench-navigator');
    if (nav) {
      nav.addEventListener('click', (e) => {
        const target = (e.target as HTMLElement).closest<HTMLElement>('[data-action="scene-select"]');
        if (!target) return;
        _selectedSceneId = target.getAttribute('data-scene-id');
        const wb = state.workbench;
        if (!wb) return;
        const navEl = document.getElementById('workbench-navigator');
        if (navEl) navEl.innerHTML = renderSceneNavigator(wb, _selectedSceneId);
        refreshSceneDetail(wb);
      });
    }

    const presence = document.getElementById('workbench-presence');
    if (presence) {
      presence.addEventListener('change', (e) => {
        const sel = (e.target as HTMLSelectElement);
        if (!sel.getAttribute('data-action')?.includes('presence-change')) return;
        const sceneId = sel.getAttribute('data-scene-id');
        const characterId = sel.getAttribute('data-character-id');
        if (!sceneId || !characterId) return;
        void savePresence(sceneId, characterId, sel.value as 'present' | 'speaker' | 'absent');
      });
    }

    const setup = document.getElementById('workbench-setup');
    if (setup) {
      setup.addEventListener('click', async (e) => {
        const btn = (e.target as HTMLElement).closest<HTMLElement>('[data-action]');
        if (!btn) return;
        const action = btn.getAttribute('data-action');
        const walk = btn.getAttribute('data-walk') as WorkbenchWalkName;
        if (action === 'walk-setup-toggle') {
          const card = btn.closest('.card');
          const body2 = card?.querySelector<HTMLElement>('[data-role="walk-setup-body"]');
          if (body2) {
            const expanded = body2.style.display !== 'none';
            body2.style.display = expanded ? 'none' : '';
            btn.setAttribute('aria-expanded', String(!expanded));
          }
        } else if (action === 'save-override') {
          const card = btn.closest('.card');
          if (!card) return;
          const inputs = card.querySelectorAll<HTMLInputElement | HTMLTextAreaElement>('[data-walk]');
          for (const input of Array.from(inputs)) {
            if (!input.getAttribute('data-key')) continue;
            const key = input.getAttribute('data-key') as string;
            const raw = input.value.trim();
            let value: unknown = raw;
            if (key === 'temperature') {
              const num = Number(raw);
              if (!Number.isNaN(num)) value = num;
            }
            await saveOverride(walk, key, value);
          }
        } else if (action === 'clear-override') {
          const wbk = state.workbench;
          if (!wbk) return;
          const card = btn.closest('.card');
          if (!card) return;
          const inputs = card.querySelectorAll<HTMLInputElement | HTMLTextAreaElement>('[data-walk]');
          for (const input of Array.from(inputs)) {
            const key = input.getAttribute('data-key');
            if (key) await clearOverride(walk, key);
          }
        }
      });
    }

    const actions = document.getElementById('workbench-actions');
    if (actions) {
      actions.addEventListener('click', (e) => {
        const btn = (e.target as HTMLElement).closest<HTMLElement>('[data-action]');
        if (!btn) return;
        const action = btn.getAttribute('data-action');
        if (action === 'refresh-workbench') {
          void loadWorkbench(true);
          void loadWorkbenchConfig();
        } else if (action === 'undo-last') {
          const last = _undoStack[_undoStack.length - 1];
          if (last) void undoDecision(last.decisionId);
          else showToast('Nothing to undo', 'info');
        } else if (action === 'rerun-book') {
          const walk = btn.getAttribute('data-walk') as WorkbenchWalkName;
          void rerunWalk(walk, 'book');
        } else if (action === 'rerun-selected-scene') {
          const walk = btn.getAttribute('data-walk') as WorkbenchWalkName;
          void rerunWalk(walk, 'scenes', _selectedSceneId ? [_selectedSceneId] : []);
        }
      });
    }

    const aliasPanel = document.getElementById('workbench-alias-panel');
    if (aliasPanel) {
      aliasPanel.addEventListener('click', async (e) => {
        const btn = (e.target as HTMLElement).closest<HTMLElement>('[data-action]');
        if (!btn) return;
        const action = btn.getAttribute('data-action');
        if (action === 'alias-preview') {
          const wb = state.workbench;
          if (!wb) return;
          const canonical = (document.getElementById('alias-canonical') as HTMLSelectElement | null)?.value;
          const members = Array.from(
            (document.querySelectorAll<HTMLSelectElement>('#alias-members option:checked')) as unknown as NodeListOf<HTMLOptionElement>,
          ).map((o) => o.value);
          if (!canonical || members.length === 0) {
            showToast('Select a canonical character and at least one member to merge', 'warning');
            return;
          }
          const summary = await previewAliasConversion(canonical, members);
          const summaryEl = document.getElementById('alias-preview-summary');
          if (summaryEl && summary) summaryEl.textContent = summary;
        } else if (action === 'alias-commit') {
          const summaryEl = document.getElementById('alias-preview-summary');
          if (!_aliasPreview) {
            showToast('Preview the alias conversion first', 'warning');
            return;
          }
          const ok = await showConfirm(_aliasPreview.summary + ' — apply this alias conversion?');
          if (ok) await commitAliasConversion(true);
        } else if (action === 'alias-unmerge') {
          const mergeId = btn.getAttribute('data-merge-id');
          if (mergeId) await unmergeAlias(mergeId);
        }
      });
    }

    // Tab activation hook: load the workbench when the tab is opened.
    document.querySelector('[data-tab="workbench"]')?.addEventListener('click', () => {
      void loadWorkbench(true);
      void loadWorkbenchConfig();
    });

    // Undo button in the action bar header.
    const undoBtn = document.getElementById('btn-workbench-undo');
    undoBtn?.addEventListener('click', () => {
      const last = _undoStack[_undoStack.length - 1];
      if (last) void undoDecision(last.decisionId);
      else showToast('Nothing to undo', 'info');
    });
  });
}
