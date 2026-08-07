/**
 * Projects tab (Plan I, Phase 3) — snapshot projects UI.
 *
 * Saves the current book as an auto-named snapshot, and lists / loads /
 * deletes / renames saved snapshots against the /api/pipeline/projects/*
 * endpoints (api_operations.py). This is a net-new surface wired ONLY to
 * the pipeline snapshot API — no legacy /api/scripts/* UI.
 *
 * Endpoints used:
 *   - POST   /api/pipeline/projects {book_id}          → auto-named snapshot
 *   - GET    /api/pipeline/projects?book_id=           → newest-first list
 *   - POST   /api/pipeline/projects/load {name, book_id} → restore (409 +
 *     Retry-After while a walk/render is active — retried exactly once)
 *   - DELETE /api/pipeline/projects/{name}
 *   - PATCH  /api/pipeline/projects/{name} {new_name}
 *
 * The snapshot NAME is always generated server-side ("Project {YYYY-MM-DD
 * HH:MM}" + optional " (N)" suffix); the frontend never proposes a name.
 */

import * as API from '../api';
import { state } from '../state';
import { showToast, showConfirm, escapeHtml } from '../utils';
import { loadSpans } from './editor-pipeline';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** A saved snapshot row (backend ProjectSnapshot DTO). */
export interface ProjectSnapshot {
  name: string;
  book_id: string;
  created_ms: number;
  size_bytes: number;
}

/** Response of POST /api/pipeline/projects/load. */
export interface LoadProjectResult {
  status: string;
  name: string;
  book_id: string;
  re_render_required: boolean;
}

// ---------------------------------------------------------------------------
// Formatting helpers
// ---------------------------------------------------------------------------

/**
 * Format a byte count as a compact human-readable string ("1.5 KB").
 */
export function formatSnapshotSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

/**
 * Format a unix-ms timestamp as a local date string.
 */
export function formatSnapshotDate(createdMs: number): string {
  return new Date(createdMs).toLocaleString();
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

/**
 * Render snapshot rows (name / created / size + Load / Delete / Rename
 * affordances) as an HTML string. All server-supplied content (the name) is
 * escaped — no raw innerHTML injection.
 */
export function renderProjectsList(snapshots: ProjectSnapshot[]): string {
  if (snapshots.length === 0) {
    return '<p class="text-muted mb-0">No saved projects yet. Save the current book to create a snapshot.</p>';
  }

  return snapshots
    .map((s) => {
      const safeName = escapeHtml(s.name);
      const safeDataName = escapeHtml(s.name);
      return `
        <div class="project-row d-flex justify-content-between align-items-center border rounded p-2 mb-2">
          <div class="me-3 overflow-hidden">
            <div class="fw-semibold text-truncate" title="${safeDataName}">${safeName}</div>
            <div class="small text-muted">${escapeHtml(formatSnapshotDate(s.created_ms))} &middot; ${escapeHtml(formatSnapshotSize(s.size_bytes))}</div>
          </div>
          <div class="d-flex gap-1 flex-shrink-0">
            <button type="button" class="btn btn-sm btn-outline-success" data-action="project-load" data-name="${safeDataName}" title="Load this snapshot into the current book"><i class="fas fa-download me-1"></i>Load</button>
            <button type="button" class="btn btn-sm btn-outline-primary" data-action="project-rename" data-name="${safeDataName}" title="Rename this snapshot"><i class="fas fa-edit me-1"></i>Rename</button>
            <button type="button" class="btn btn-sm btn-outline-danger" data-action="project-delete" data-name="${safeDataName}" title="Delete this snapshot"><i class="fas fa-trash me-1"></i>Delete</button>
          </div>
        </div>`;
    })
    .join('');
}

// ---------------------------------------------------------------------------
// List / Save / Load / Delete / Rename
// ---------------------------------------------------------------------------

/** Load and render the snapshot list for the active book. */
export async function loadProjects(): Promise<void> {
  const listEl = document.getElementById('projects-list');
  if (!listEl) return;

  const qs = state.pipelineBookId ? `?book_id=${encodeURIComponent(state.pipelineBookId)}` : '';
  try {
    const snapshots = await API.get<ProjectSnapshot[]>(`/api/pipeline/projects${qs}`);
    listEl.innerHTML = renderProjectsList(snapshots);
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    showToast('Failed to load projects: ' + msg, 'error');
    listEl.innerHTML = '<p class="text-muted mb-0">Failed to load projects.</p>';
  }
}

/**
 * Save the current book as an auto-named snapshot.
 * POSTs {book_id} — the server generates the name; on success the returned
 * auto-name is surfaced and the list refreshes.
 */
export async function saveProject(): Promise<void> {
  if (!state.pipelineBookId) {
    showToast('No book onboarded. Go to the Script tab to onboard an EPUB first.', 'error');
    return;
  }

  try {
    const created = await API.post<ProjectSnapshot>('/api/pipeline/projects', {
      book_id: state.pipelineBookId,
    });
    showToast(`Saved snapshot "${created.name}"`, 'success');
    await loadProjects();
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    showToast('Failed to save snapshot: ' + msg, 'error');
  }
}

/**
 * Load (restore) a snapshot into the current book.
 *
 * The 409 + Retry-After response (active walk/render — rule #10) is handled
 * by postWithRetryOnce with retryStatus 409: exactly ONE automatic retry
 * after the advertised delay. On success the snapshot list refreshes and the
 * editor spans reload (cross-tab refresh hook). When the backend reports
 * re_render_required=true (snapshot audio artifacts missing) an explicit
 * "re-render" notice is surfaced.
 */
export async function loadProject(name: string): Promise<void> {
  if (!state.pipelineBookId) {
    showToast('No book onboarded. Go to the Script tab to onboard an EPUB first.', 'error');
    return;
  }

  try {
    const result = await API.postWithRetryOnce<LoadProjectResult>(
      '/api/pipeline/projects/load',
      { name, book_id: state.pipelineBookId },
      409,
    );

    if (result.re_render_required) {
      showToast(
        `Snapshot "${result.name}" loaded — audio is missing, re-render required`,
        'warning',
      );
    } else {
      showToast(`Snapshot "${result.name}" loaded`, 'success');
    }

    await loadProjects();
    // Cross-tab refresh: reload the editor span table so the restored
    // snapshot's script is immediately visible on the Editor tab.
    await loadSpans();
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    showToast('Failed to load snapshot: ' + msg, 'error');
  }
}

/**
 * Delete a snapshot (after confirmation) and refresh the list.
 */
export async function deleteProject(name: string): Promise<void> {
  const confirmed = await showConfirm(`Delete snapshot "${name}"? This cannot be undone.`);
  if (!confirmed) return;

  try {
    await API.del<{ status: string; name: string }>(
      `/api/pipeline/projects/${encodeURIComponent(name)}`,
    );
    showToast(`Snapshot "${name}" deleted`, 'success');
    await loadProjects();
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    showToast('Failed to delete snapshot: ' + msg, 'error');
  }
}

/**
 * Rename a snapshot. Prompts for the new name, then PATCHes {new_name}.
 * A 409 duplicate surfaces the backend detail (already exists) via toast.
 */
export async function renameProject(name: string): Promise<void> {
  const newName = prompt(`Rename snapshot "${name}" to:`);
  if (newName === null) return; // user cancelled

  try {
    await API.patch<{ status: string; name: string }>(
      `/api/pipeline/projects/${encodeURIComponent(name)}`,
      { new_name: newName },
    );
    showToast(`Snapshot renamed to "${newName}"`, 'success');
    await loadProjects();
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    showToast('Failed to rename snapshot: ' + msg, 'error');
  }
}

// ---------------------------------------------------------------------------
// Initialization
// ---------------------------------------------------------------------------

let _projectsInitialized = false;

/**
 * Initialize the Projects tab.
 *
 * Idempotent: a module flag ensures the DOMContentLoaded handler is
 * registered at most once (matches initEditor's pattern).
 *
 * Wires:
 *   - #btn-project-save click → saveProject()
 *   - Delegated clicks on #projects-list for [data-action="project-load" |
 *     "project-delete" | "project-rename"], reading the snapshot name from
 *     the button's data-name attribute
 *   - Initial snapshot list load
 */
export function initProjects(): void {
  if (_projectsInitialized) return;
  _projectsInitialized = true;

  document.addEventListener('DOMContentLoaded', () => {
    const saveBtn = document.getElementById('btn-project-save');
    if (saveBtn) {
      saveBtn.addEventListener('click', () => {
        saveProject();
      });
    }

    const listEl = document.getElementById('projects-list');
    if (listEl) {
      listEl.addEventListener('click', (e) => {
        const target = e.target as HTMLElement;
        const btn = target.closest('button') as HTMLButtonElement | null;
        if (!btn) return;

        const action = btn.dataset.action;
        const name = btn.dataset.name;
        if (!action || !name) return;

        if (action === 'project-load') {
          loadProject(name);
        } else if (action === 'project-delete') {
          deleteProject(name);
        } else if (action === 'project-rename') {
          renameProject(name);
        }
      });
    }

    loadProjects();
  });
}
