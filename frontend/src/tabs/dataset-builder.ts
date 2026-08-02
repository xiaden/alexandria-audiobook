/**
 * Dataset Builder tab module — Build LoRA training datasets with per-sample generation
 * Ported from app/static/index.html lines 3519-4014 (JS logic)
 * HTML: lines 880-968 (dataset-builder-tab)
 * TTS-only — no per-task LLM config UI.
 */

import * as API from '../api';
import { state } from '../state';
import { showToast, showConfirm, escapeHtml } from '../utils';
import { dsbBuildRowHtml } from '../templates';

/** Module-level state for dataset builder tab */
let dsbPolling: ReturnType<typeof setInterval> | null = null;
let dsbBatchRunning = false;
let dsbSaveMetaTimer: ReturnType<typeof setTimeout> | null = null;
let dsbSaveRowsTimer: ReturnType<typeof setTimeout> | null = null;
let dsbLastDoneCount = -1;

// Clean up legacy localStorage
try { localStorage.removeItem('alexandria-dsb-form'); } catch { /* ignore */ }

/**
 * Load the list of dataset builder projects and populate the select dropdown.
 * @param selectName - Optional project name to auto-select after loading
 */
async function dsbLoadProjects(selectName?: string): Promise<void> {
  try {
    const projects = await API.get<Array<{ name: string; done_count: number; sample_count: number }>>('/api/dataset_builder/list');
    state.dsbProjects = projects;
    const select = document.getElementById('dsb-project-select') as HTMLSelectElement;
    if (!select) return;
    select.innerHTML = '<option value="">-- Select project --</option>' +
      projects.map(p => `<option value="${escapeHtml(p.name)}">${escapeHtml(p.name)} (${p.done_count}/${p.sample_count})</option>`).join('');
    if (selectName) {
      select.value = selectName;
      dsbOnProjectChange();
    }
  } catch (e) {
    console.error('Failed to load projects:', e);
  }
}

/**
 * Handle project selection change — load project data or clear the form.
 */
async function dsbOnProjectChange(): Promise<void> {
  const name = (document.getElementById('dsb-project-select') as HTMLSelectElement)?.value || '';
  const formArea = document.getElementById('dsb-form-area');
  const deleteBtn = document.getElementById('dsb-btn-delete-project');
  if (!name) {
    state.dsbCurrentProject = '';
    if (formArea) formArea.style.display = 'none';
    if (deleteBtn) deleteBtn.style.display = 'none';
    state.dsbRows = [];
    dsbRenderTable();
    return;
  }
  state.dsbCurrentProject = name;
  if (formArea) formArea.style.display = '';
  if (deleteBtn) deleteBtn.style.display = '';
  await dsbLoadProject(name);
}

/**
 * Load a specific project's data from the backend.
 * @param name - Project name
 */
async function dsbLoadProject(name: string): Promise<void> {
  try {
    const result = await API.get<{
      description?: string;
      global_seed?: number | string;
      samples?: Array<{
        emotion?: string;
        description?: string;
        text?: string;
        seed?: number | string;
        status?: string;
        audio_url?: string;
      }>;
      running?: boolean;
    }>(`/api/dataset_builder/status/${encodeURIComponent(name)}`);

    const descInput = document.getElementById('dsb-description') as HTMLInputElement;
    const seedInput = document.getElementById('dsb-global-seed') as HTMLInputElement;
    if (descInput) descInput.value = result.description || '';
    if (seedInput) seedInput.value = result.global_seed != null ? String(result.global_seed) : '';

    state.dsbRows = (result.samples || []).map(s => ({
      emotion: s.emotion || s.description || '',
      text: s.text || '',
      seed: s.seed ?? '',
      status: (s.status as 'pending' | 'generating' | 'done' | 'error') || 'pending',
      audio_url: s.audio_url || null,
    }));
    if (state.dsbRows.length === 0) dsbAddRow();
    dsbRenderTable();

    // Resume polling if batch is running
    if (result.running) {
      dsbBatchRunning = true;
      dsbStartPolling(name);
      const genAllBtn = document.getElementById('dsb-btn-gen-all');
      const regenAllBtn = document.getElementById('dsb-btn-regen-all');
      const cancelBtn = document.getElementById('dsb-btn-cancel');
      if (genAllBtn) genAllBtn.style.display = 'none';
      if (regenAllBtn) regenAllBtn.style.display = 'none';
      if (cancelBtn) cancelBtn.style.display = '';
    }
  } catch (e) {
    state.dsbRows = [];
    dsbAddRow();
  }
}

/**
 * Create a new dataset project via prompt dialog.
 */
async function dsbCreateProject(): Promise<void> {
  const name = prompt('Dataset name:');
  if (!name || !name.trim()) return;
  try {
    const result = await API.post<{ name: string }>('/api/dataset_builder/create', { name: name.trim() });
    await dsbLoadProjects(result.name);
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    showToast('Failed to create project: ' + msg, 'error');
  }
}

/**
 * Delete the current dataset project after confirmation.
 */
async function dsbDeleteProject(): Promise<void> {
  if (!state.dsbCurrentProject) return;
  if (!await showConfirm(`Delete project "${state.dsbCurrentProject}" and all its samples?`)) return;
  try {
    await fetch(`/api/dataset_builder/${encodeURIComponent(state.dsbCurrentProject)}`, { method: 'DELETE' });
    state.dsbCurrentProject = '';
    const formArea = document.getElementById('dsb-form-area');
    const deleteBtn = document.getElementById('dsb-btn-delete-project');
    if (formArea) formArea.style.display = 'none';
    if (deleteBtn) deleteBtn.style.display = 'none';
    state.dsbRows = [];
    dsbRenderTable();
    await dsbLoadProjects();
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    showToast('Delete failed: ' + msg, 'error');
  }
}

/**
 * Debounced save of form metadata (description, global seed).
 */
function dsbSaveForm(): void {
  if (!state.dsbCurrentProject) return;
  if (dsbSaveMetaTimer) clearTimeout(dsbSaveMetaTimer);
  dsbSaveMetaTimer = setTimeout(async () => {
    try {
      await API.post('/api/dataset_builder/update_meta', {
        name: state.dsbCurrentProject,
        description: (document.getElementById('dsb-description') as HTMLInputElement)?.value || '',
        global_seed: (document.getElementById('dsb-global-seed') as HTMLInputElement)?.value || '',
      });
    } catch (e) {
      console.error('Failed to save meta:', e);
    }
  }, 500);
}

/**
 * Debounced save of row data to the backend.
 */
function dsbSaveRows(): void {
  if (!state.dsbCurrentProject) return;
  if (dsbSaveRowsTimer) clearTimeout(dsbSaveRowsTimer);
  dsbSaveRowsTimer = setTimeout(async () => {
    try {
      await API.post('/api/dataset_builder/update_rows', {
        name: state.dsbCurrentProject,
        rows: state.dsbRows.map(r => ({ emotion: r.emotion || '', text: (r.text || '').trim(), seed: r.seed ?? '' })),
      });
    } catch (e) {
      console.error('Failed to save rows:', e);
    }
  }, 500);
}

/**
 * Add a new row to the dataset.
 * @param emotion - Emotion/style description (default '')
 * @param text - Sample text (default '')
 * @param seed - Seed value (default '')
 */
function dsbAddRow(emotion = '', text = '', seed: number | string = ''): void {
  state.dsbRows.push({ emotion, text, seed, status: 'pending', audio_url: null });
  dsbRenderTable();
  dsbSaveRows();
  // Focus the new emotion field
  setTimeout(() => {
    const rows = document.querySelectorAll('#dsb-table-body tr');
    const last = rows[rows.length - 1];
    if (last) (last.querySelector('input') as HTMLInputElement)?.focus();
  }, 50);
}

/**
 * Remove a row from the dataset by index.
 * @param index - Row index to remove
 */
function dsbRemoveRow(index: number): void {
  state.dsbRows.splice(index, 1);
  dsbRenderTable();
  dsbSaveRows();
  dsbUpdateRefDropdown();
}

/**
 * Update a specific field of a row.
 * @param index - Row index
 * @param field - Field name ('emotion' | 'text' | 'seed')
 * @param value - New value
 */
function dsbUpdateRow(index: number, field: 'emotion' | 'text' | 'seed', value: string): void {
  if (state.dsbRows[index]) {
    const row = state.dsbRows[index];
    if (field === 'emotion') row.emotion = value;
    else if (field === 'text') row.text = value;
    else if (field === 'seed') row.seed = value;
    dsbSaveRows();
  }
}

/**
 * Stop all other audio elements when one starts playing.
 * @param index - Index of the row whose audio is playing
 */
function dsbStopOthers(index: number): void {
  document.querySelectorAll('#dsb-table-body audio').forEach(audio => {
    const row = audio.closest('tr');
    if (row && parseInt(row.getAttribute('data-dsb-idx') || '-1') !== index) {
      (audio as HTMLAudioElement).pause();
    }
  });
}

/**
 * Render the dataset builder table.
 * @param changedIndices - Optional array of indices that changed (for targeted updates)
 */
function dsbRenderTable(changedIndices?: number[]): void {
  const tbody = document.getElementById('dsb-table-body');
  if (!tbody) return;

  // Full rebuild if no specific indices or row count changed
  if (!changedIndices || tbody.children.length !== state.dsbRows.length) {
    tbody.innerHTML = state.dsbRows.map((row, i) => dsbBuildRowHtml(row, i)).join('');
    dsbUpdateProgress();
    return;
  }

  // Targeted update: only re-render changed rows
  for (const i of changedIndices) {
    const existing = tbody.children[i];
    if (!existing) continue;
    const row = state.dsbRows[i];
    const oldStatus = existing.getAttribute('data-dsb-status');
    const oldAudio = existing.getAttribute('data-dsb-audio');
    if (oldStatus === (row.status || 'pending') && oldAudio === (row.audio_url || '')) continue;
    const temp = document.createElement('tbody');
    temp.innerHTML = dsbBuildRowHtml(row, i);
    existing.replaceWith(temp.firstElementChild!);
  }
  dsbUpdateProgress();
}

/**
 * Update the progress bar and reference dropdown based on row statuses.
 */
function dsbUpdateProgress(): void {
  const done = state.dsbRows.filter(r => r.status === 'done').length;
  const total = state.dsbRows.length;
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;
  const wrap = document.getElementById('dsb-progress-wrap');
  const bar = document.getElementById('dsb-progress-bar');
  if (done > 0 || dsbBatchRunning) {
    if (wrap) wrap.style.display = '';
    if (bar) {
      bar.style.width = pct + '%';
      bar.innerText = `${pct}% (${done}/${total})`;
    }
  } else {
    if (wrap) wrap.style.display = 'none';
  }
  // Only rebuild dropdown when done count actually changes
  if (done !== dsbLastDoneCount) {
    dsbLastDoneCount = done;
    dsbUpdateRefDropdown();
  }
}

/**
 * Update the reference sample dropdown with completed samples.
 */
function dsbUpdateRefDropdown(): void {
  const select = document.getElementById('dsb-ref-select') as HTMLSelectElement;
  if (!select) return;
  const doneSamples = state.dsbRows.map((r, i) => ({ index: i, row: r })).filter(x => x.row.status === 'done');
  select.innerHTML = doneSamples.length === 0
    ? '<option value="0">No completed samples yet</option>'
    : doneSamples.map(x => `<option value="${x.index}">${x.index + 1}. ${escapeHtml((x.row.emotion || 'neutral').substring(0, 30))} - "${escapeHtml((x.row.text || '').substring(0, 40))}..."</option>`).join('');
}

/**
 * Generate a single sample at the given index.
 * @param index - Row index to generate
 */
async function dsbGenSample(index: number): Promise<void> {
  const name = state.dsbCurrentProject;
  const rootDesc = ((document.getElementById('dsb-description') as HTMLInputElement)?.value || '').trim();
  if (!name) { showToast('Select or create a project first.', 'warning'); return; }
  if (!rootDesc) { showToast('Enter a root voice description first.', 'warning'); return; }

  const row = state.dsbRows[index];
  if (!row || !row.text.trim()) { showToast('This row has no text.', 'warning'); return; }

  const emotion = row.emotion.trim();
  const description = emotion ? `${rootDesc}, ${emotion}` : rootDesc;

  // Resolve seed: per-line > global > random
  const globalSeed = parseInt((document.getElementById('dsb-global-seed') as HTMLInputElement)?.value || '');
  const lineSeed = row.seed !== '' ? parseInt(String(row.seed)) : NaN;
  const seed = !isNaN(lineSeed) && lineSeed >= 0 ? lineSeed : (!isNaN(globalSeed) && globalSeed >= 0 ? globalSeed : -1);

  // Optimistic UI
  state.dsbRows[index].status = 'generating';
  dsbRenderTable([index]);

  try {
    const result = await API.post<{ audio_url: string }>('/api/dataset_builder/generate_sample', {
      description,
      text: row.text.trim(),
      dataset_name: name,
      sample_index: index,
      seed,
    });
    state.dsbRows[index].status = 'done';
    state.dsbRows[index].audio_url = result.audio_url;
  } catch (e) {
    state.dsbRows[index].status = 'error';
    console.error('Sample generation failed:', e);
  }
  dsbRenderTable([index]);
}

/**
 * Generate all pending samples or regenerate all samples.
 * @param regenAll - If true, regenerate all samples; otherwise only generate pending ones
 */
async function dsbGenerateAll(regenAll = false): Promise<void> {
  const name = state.dsbCurrentProject;
  const rootDesc = ((document.getElementById('dsb-description') as HTMLInputElement)?.value || '').trim();
  if (!name) { showToast('Select or create a project first.', 'warning'); return; }
  if (!rootDesc) { showToast('Enter a root voice description first.', 'warning'); return; }

  const samples = state.dsbRows.filter(r => r.text.trim());
  if (samples.length === 0) { showToast('Add at least one sample with text.', 'warning'); return; }

  const indices = regenAll
    ? state.dsbRows.map((_, i) => i).filter(i => state.dsbRows[i].text.trim())
    : state.dsbRows.map((r, i) => i).filter(i => state.dsbRows[i].text.trim() && state.dsbRows[i].status !== 'done');

  if (indices.length === 0) { showToast('All samples are already generated.', 'warning'); return; }
  if (regenAll && !await showConfirm(`Regenerate all ${indices.length} samples?`)) return;

  // Mark as generating
  indices.forEach(i => { state.dsbRows[i].status = 'generating'; });
  dsbRenderTable();
  dsbBatchRunning = true;
  const genAllBtn = document.getElementById('dsb-btn-gen-all');
  const regenAllBtn = document.getElementById('dsb-btn-regen-all');
  const cancelBtn = document.getElementById('dsb-btn-cancel');
  const logsEl = document.getElementById('dsb-logs');
  if (genAllBtn) genAllBtn.style.display = 'none';
  if (regenAllBtn) regenAllBtn.style.display = 'none';
  if (cancelBtn) cancelBtn.style.display = '';
  if (logsEl) logsEl.style.display = '';

  const globalSeed = parseInt((document.getElementById('dsb-global-seed') as HTMLInputElement)?.value || '');
  const perSeeds = state.dsbRows.map(r => r.seed !== '' && r.seed !== undefined ? parseInt(String(r.seed)) : -1);

  try {
    await API.post('/api/dataset_builder/generate_batch', {
      name,
      description: rootDesc,
      samples: state.dsbRows.map(r => ({ emotion: r.emotion || '', text: r.text || '' })),
      indices,
      global_seed: !isNaN(globalSeed) && globalSeed >= 0 ? globalSeed : -1,
      seeds: perSeeds,
    });

    // Start polling
    dsbStartPolling(name);
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    showToast('Batch generation failed: ' + msg, 'error');
    dsbStopBatch();
  }
}

/**
 * Start polling for batch generation status.
 * @param name - Project name
 */
function dsbStartPolling(name: string): void {
  if (dsbPolling) clearInterval(dsbPolling);
  dsbPolling = setInterval(() => dsbPollStatus(name), 2000);
}

/**
 * Poll the dataset builder status endpoint and update the UI.
 * @param name - Project name
 * @param silent - If true, suppress error logging
 */
async function dsbPollStatus(name: string, silent = false): Promise<void> {
  try {
    const result = await API.get<{
      samples?: Array<{
        emotion?: string;
        description?: string;
        text?: string;
        seed?: number | string;
        status?: string;
        audio_url?: string;
      }>;
      logs?: string[];
      running?: boolean;
    }>(`/api/dataset_builder/status/${encodeURIComponent(name)}`);
    const serverSamples = result.samples || [];

    // Merge server state into local rows, creating missing rows
    const changed: number[] = [];
    let added = false;
    serverSamples.forEach((s, i) => {
      if (i < state.dsbRows.length) {
        const oldStatus = state.dsbRows[i].status;
        const oldAudio = state.dsbRows[i].audio_url;
        if (s.status) state.dsbRows[i].status = s.status as 'pending' | 'generating' | 'done' | 'error';
        if (s.audio_url) state.dsbRows[i].audio_url = s.audio_url;
        if (state.dsbRows[i].status !== oldStatus || state.dsbRows[i].audio_url !== oldAudio) changed.push(i);
      } else {
        state.dsbRows.push({
          emotion: s.description || '',
          text: s.text || '',
          seed: s.seed ?? '',
          status: (s.status as 'pending' | 'generating' | 'done' | 'error') || 'pending',
          audio_url: s.audio_url || null,
        });
        added = true;
      }
    });

    if (added) {
      dsbRenderTable();
    } else if (changed.length > 0) {
      dsbRenderTable(changed);
    }

    // Update logs
    if (result.logs && result.logs.length > 0) {
      const logsEl = document.getElementById('dsb-logs');
      if (logsEl) {
        logsEl.style.display = '';
        logsEl.innerText = result.logs.join('\n');
        logsEl.scrollTop = logsEl.scrollHeight;
      }
    }

    // Resume polling if server is still running (e.g. after page reload)
    if (result.running && !dsbBatchRunning) {
      dsbBatchRunning = true;
      dsbStartPolling(name);
      const genAllBtn = document.getElementById('dsb-btn-gen-all');
      const regenAllBtn = document.getElementById('dsb-btn-regen-all');
      const cancelBtn = document.getElementById('dsb-btn-cancel');
      if (genAllBtn) genAllBtn.style.display = 'none';
      if (regenAllBtn) regenAllBtn.style.display = 'none';
      if (cancelBtn) cancelBtn.style.display = '';
    }

    // Check if batch is done
    if (!result.running && dsbBatchRunning) {
      dsbStopBatch();
    }

    // If not running and this was a one-time check, stop polling
    if (!result.running && silent && dsbPolling) {
      clearInterval(dsbPolling);
      dsbPolling = null;
    }
  } catch (e) {
    if (!silent) console.error('Poll error:', e);
    // Status endpoint may 404 if no state.json yet — ignore silently
    if (silent && dsbPolling) {
      clearInterval(dsbPolling);
      dsbPolling = null;
    }
  }
}

/**
 * Stop batch generation and reset UI state.
 */
function dsbStopBatch(): void {
  dsbBatchRunning = false;
  if (dsbPolling) { clearInterval(dsbPolling); dsbPolling = null; }
  const genAllBtn = document.getElementById('dsb-btn-gen-all');
  const regenAllBtn = document.getElementById('dsb-btn-regen-all');
  const cancelBtn = document.getElementById('dsb-btn-cancel');
  if (genAllBtn) genAllBtn.style.display = '';
  if (regenAllBtn) regenAllBtn.style.display = '';
  if (cancelBtn) cancelBtn.style.display = 'none';
  dsbRenderTable();
}

/**
 * Cancel the current dataset builder operation.
 */
async function dsbCancel(): Promise<void> {
  try {
    await API.post('/api/dataset_builder/cancel', {});
  } catch (e) {
    console.error('Cancel error:', e);
  }
}

/**
 * Import rows from a JSON file.
 * @param event - File input change event
 */
function dsbImport(event: Event): void {
  const file = (event.target as HTMLInputElement).files?.[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = (e) => {
    try {
      const data = JSON.parse(e.target?.result as string);
      if (!Array.isArray(data)) throw new Error('Expected JSON array');
      state.dsbRows = data.map((item: any) => ({
        emotion: item.emotion || item.instruct || '',
        text: item.text || '',
        seed: item.seed ?? '',
        status: 'pending' as const,
        audio_url: null,
      }));
      dsbRenderTable();
      dsbSaveRows();
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      showToast('Import failed: ' + msg, 'error');
    }
  };
  reader.readAsText(file);
  (event.target as HTMLInputElement).value = ''; // reset file input
}

/**
 * Export rows as a JSON file.
 */
function dsbExport(): void {
  const data = state.dsbRows.map(r => {
    const entry: { emotion: string; text: string; seed?: number } = { emotion: r.emotion, text: r.text };
    if (r.seed !== '' && r.seed !== undefined) entry.seed = parseInt(String(r.seed));
    return entry;
  });
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  const name = state.dsbCurrentProject || 'dataset';
  a.download = `${name}_script.json`;
  a.click();
  URL.revokeObjectURL(url);
}

/**
 * Save the current dataset as a training dataset.
 */
async function dsbSave(): Promise<void> {
  const name = state.dsbCurrentProject;
  if (!name) { showToast('Select or create a project first.', 'warning'); return; }

  const doneSamples = state.dsbRows.filter(r => r.status === 'done');
  if (doneSamples.length === 0) { showToast('No completed samples to save. Generate some first.', 'warning'); return; }

  const refIdx = parseInt((document.getElementById('dsb-ref-select') as HTMLSelectElement)?.value || '0') || 0;

  if (!await showConfirm(`Save "${name}" as training dataset with ${doneSamples.length} samples?`)) return;

  const statusEl = document.getElementById('dsb-save-status');
  if (statusEl) statusEl.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i>Saving...';

  try {
    const result = await API.post<{ sample_count: number }>('/api/dataset_builder/save', {
      name,
      ref_index: refIdx,
    });
    if (statusEl) statusEl.innerHTML = `<span class="text-success"><i class="fas fa-check me-1"></i>Saved! ${result.sample_count} samples.</span>`;
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    if (statusEl) statusEl.innerHTML = `<span class="text-danger">Save failed: ${escapeHtml(msg)}</span>`;
  }
}

/**
 * Initialize the Dataset Builder tab.
 * Attaches event listeners for project selection, form inputs, and action buttons.
 * Exposes global functions for inline handlers in templates.ts (dsbBuildRowHtml).
 */
export function initDatasetBuilder(): void {
  document.addEventListener('DOMContentLoaded', () => {
    // Event delegation for dataset builder table actions
    const dsbTableBody = document.getElementById('dsb-table-body');
    if (dsbTableBody) {
      // Click events
      dsbTableBody.addEventListener('click', (e) => {
        const target = e.target as HTMLElement;
        const actionEl = target.closest('[data-action]') as HTMLElement;
        if (!actionEl) return;

        const action = actionEl.dataset.action;
        const idx = actionEl.dataset.dsbIdx != null ? parseInt(actionEl.dataset.dsbIdx, 10) : null;

        switch (action) {
          case 'dsb-gen-sample':
            if (idx !== null) dsbGenSample(idx);
            break;
          case 'dsb-remove-row':
            if (idx !== null) dsbRemoveRow(idx);
            break;
        }
      });

      // Change events for inputs/textareas
      dsbTableBody.addEventListener('change', (e) => {
        const target = e.target as HTMLElement;
        const actionEl = target.closest('[data-action]') as HTMLElement;
        if (!actionEl) return;

        const action = actionEl.dataset.action;
        const idx = actionEl.dataset.dsbIdx != null ? parseInt(actionEl.dataset.dsbIdx, 10) : null;

        if (action === 'dsb-update-row' && idx !== null) {
          const field = actionEl.dataset.field as 'emotion' | 'text' | 'seed';
          const value = (target as HTMLInputElement | HTMLTextAreaElement).value;
          if (field) dsbUpdateRow(idx, field, value);
        }
      });

      // Play events for audio elements
      dsbTableBody.addEventListener('play', (e) => {
        const target = e.target as HTMLElement;
        if (target.tagName === 'AUDIO' && target.dataset.action === 'dsb-stop-others') {
          const idx = target.dataset.dsbIdx != null ? parseInt(target.dataset.dsbIdx, 10) : null;
          if (idx !== null) dsbStopOthers(idx);
        }
      }, true);
    }
    // Project selector
    const projectSelect = document.getElementById('dsb-project-select');
    if (projectSelect) {
      projectSelect.removeAttribute('onchange');
      projectSelect.addEventListener('change', () => dsbOnProjectChange());
    }

    // Create project button
    const createBtn = document.querySelector('[onclick="dsbCreateProject()"]');
    if (createBtn) {
      createBtn.removeAttribute('onclick');
      createBtn.addEventListener('click', () => dsbCreateProject());
    }

    // Delete project button
    const deleteBtn = document.getElementById('dsb-btn-delete-project');
    if (deleteBtn) {
      deleteBtn.removeAttribute('onclick');
      deleteBtn.addEventListener('click', () => dsbDeleteProject());
    }

    // Form inputs — debounced save
    const descInput = document.getElementById('dsb-description');
    if (descInput) descInput.addEventListener('input', dsbSaveForm);
    const seedInput = document.getElementById('dsb-global-seed');
    if (seedInput) seedInput.addEventListener('input', dsbSaveForm);

    // Add Row button
    const addRowBtn = document.querySelector('[onclick="dsbAddRow()"]');
    if (addRowBtn) {
      addRowBtn.removeAttribute('onclick');
      addRowBtn.addEventListener('click', () => dsbAddRow());
    }

    // Import JSON file input
    const importInput = document.querySelector('input[accept=".json"]') as HTMLInputElement;
    if (importInput) {
      importInput.removeAttribute('onchange');
      importInput.addEventListener('change', (e) => dsbImport(e));
    }

    // Export JSON button
    const exportBtn = document.querySelector('[onclick="dsbExport()"]');
    if (exportBtn) {
      exportBtn.removeAttribute('onclick');
      exportBtn.addEventListener('click', () => dsbExport());
    }

    // Generate Pending button
    const genAllBtn = document.getElementById('dsb-btn-gen-all');
    if (genAllBtn) {
      genAllBtn.removeAttribute('onclick');
      genAllBtn.addEventListener('click', () => dsbGenerateAll(false));
    }

    // Regen All button
    const regenAllBtn = document.getElementById('dsb-btn-regen-all');
    if (regenAllBtn) {
      regenAllBtn.removeAttribute('onclick');
      regenAllBtn.addEventListener('click', () => dsbGenerateAll(true));
    }

    // Cancel button
    const cancelBtn = document.getElementById('dsb-btn-cancel');
    if (cancelBtn) {
      cancelBtn.removeAttribute('onclick');
      cancelBtn.addEventListener('click', () => dsbCancel());
    }

    // Save as Training Dataset button
    const saveBtn = document.getElementById('dsb-btn-save');
    if (saveBtn) {
      saveBtn.removeAttribute('onclick');
      saveBtn.addEventListener('click', () => dsbSave());
    }

    // Initial load of projects
    dsbLoadProjects();
  });
}
