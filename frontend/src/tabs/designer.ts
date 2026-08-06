/**
 * Designer tab module — Voice design form, preview generation, save, and list management
 * Ported from app/static/index.html lines 2873-3098 (JS logic)
 */

import * as API from '../api';
import { showToast, showConfirm, escapeHtml } from '../utils';
import { state, type DesignedVoice } from '../state';

/** Response from /api/voice_design/preview */
interface DesignPreviewResult {
  audio_url: string;
}

/** Module-level state for designer tab */
let editingDesignedVoiceId: string | null = null;
let currentPreviewFile: string | null = null;

/**
 * Load designed voices from the server and render the list table.
 * Fetches GET /api/voice_design/list and renders a table with play/edit/delete actions.
 * Updates state.designedVoices cache.
 */
export async function loadDesignedVoices(): Promise<void> {
  try {
    const voices = await API.get<DesignedVoice[]>('/api/voice_design/list');
    state.designedVoices = voices;
    const container = document.getElementById('designed-voices-list');
    if (!container) return;

    if (!voices.length) {
      container.innerHTML = '<p class="text-muted mb-0">No designed voices yet. Generate and save a preview above.</p>';
      return;
    }

    container.innerHTML = `
      <table class="table table-sm table-hover mb-0">
        <thead><tr><th>Name</th><th>Description</th><th style="width:120px">Actions</th></tr></thead>
        <tbody>
          ${voices.map(v => `
            <tr>
              <td><strong>${escapeHtml(v.name)}</strong></td>
              <td class="text-muted" style="max-width:400px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(v.description || '')}</td>
              <td>
                <button class="btn btn-sm btn-outline-primary me-1" data-action="play" data-filename="${escapeHtml(v.filename)}" title="Play"><i class="fas fa-play"></i></button>
                <button class="btn btn-sm btn-outline-secondary me-1" data-action="edit" data-id="${escapeHtml(v.id)}" title="Edit"><i class="fas fa-edit"></i></button>
                <button class="btn btn-sm btn-outline-danger" data-action="delete" data-id="${escapeHtml(v.id)}" title="Delete"><i class="fas fa-trash"></i></button>
              </td>
            </tr>
          `).join('')}
        </tbody>
      </table>`;
  } catch (e) {
    console.error('Failed to load designed voices:', e);
  }
}

/**
 * Reset the designer form to its initial state.
 * Clears all input fields, hides the preview container, and resets editing state.
 */
export function resetDesignerForm(): void {
  const voiceName = document.getElementById('design-voice-name') as HTMLInputElement;
  const sourceName = document.getElementById('design-source-name') as HTMLInputElement;
  const description = document.getElementById('design-description') as HTMLTextAreaElement;
  const sampleText = document.getElementById('design-sample-text') as HTMLTextAreaElement;
  const aliasSelect = document.getElementById('design-alias-select') as HTMLSelectElement;
  const previewContainer = document.getElementById('design-preview-container');
  const statusEl = document.getElementById('design-status');
  const previewButton = document.getElementById('btn-design-preview');

  if (voiceName) voiceName.value = '';
  if (sourceName) sourceName.value = '';
  if (description) description.value = '';
  if (sampleText) sampleText.value = '';
  if (aliasSelect) aliasSelect.innerHTML = '<option value="">-- None --</option>';
  if (previewContainer) previewContainer.style.display = 'none';
  if (statusEl) statusEl.innerHTML = '';

  editingDesignedVoiceId = null;
  currentPreviewFile = null;

  if (previewButton) {
    previewButton.innerHTML = '<i class="fas fa-wand-magic-sparkles me-1"></i>Generate Preview';
  }
}

/**
 * Generate a voice design preview.
 * POSTs to /api/voice_design/preview with description and sample_text.
 * Shows the audio preview player on success.
 */
async function generateDesignPreview(): Promise<void> {
  const descriptionEl = document.getElementById('design-description') as HTMLTextAreaElement;
  const sampleTextEl = document.getElementById('design-sample-text') as HTMLTextAreaElement;
  const statusEl = document.getElementById('design-status');
  const previewContainer = document.getElementById('design-preview-container');
  const description = descriptionEl?.value.trim() || '';
  const sampleText = sampleTextEl?.value.trim() || '';

  if (!description) {
    showToast('Please enter a voice description.', 'warning');
    return;
  }
  if (!sampleText) {
    showToast('Please enter sample text.', 'warning');
    return;
  }

  const btn = document.getElementById('btn-design-preview') as HTMLButtonElement;
  if (btn) btn.disabled = true;

  if (statusEl) {
    statusEl.innerHTML = editingDesignedVoiceId
      ? '<i class="fas fa-spinner fa-spin me-1"></i>Re-designing preview (this may take a moment)...'
      : '<i class="fas fa-spinner fa-spin me-1"></i>Generating preview (this may take a moment on first run)...';
  }
  if (previewContainer) previewContainer.style.display = 'none';

  try {
    const result = await API.post<DesignPreviewResult>('/api/voice_design/preview', {
      description,
      sample_text: sampleText,
    });

    const audio = document.getElementById('design-preview-audio') as HTMLAudioElement;
    if (audio) {
      audio.src = result.audio_url + '?t=' + Date.now();
    }
    if (previewContainer) previewContainer.style.display = 'block';
    if (statusEl) {
      statusEl.innerHTML = editingDesignedVoiceId
        ? '<span class="text-success"><i class="fas fa-check me-1"></i>Voice re-designed</span>'
        : '<span class="text-success"><i class="fas fa-check me-1"></i>Preview ready</span>';
    }

    // Extract filename from URL for save
    currentPreviewFile = result.audio_url.split('/').pop()?.split('?')[0] || null;
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    if (statusEl) {
      statusEl.innerHTML = `<span class="text-danger"><i class="fas fa-times me-1"></i>Failed: ${escapeHtml(msg)}</span>`;
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

/**
 * Save the designed voice to the server.
 * POSTs to /api/voice_design/save with name, description, sample_text, and preview_file.
 * After saving, propagates alias choice to the source voice card if editing a persona.
 */
async function saveDesignedVoice(): Promise<void> {
  const nameEl = document.getElementById('design-voice-name') as HTMLInputElement;
  const name = nameEl?.value.trim() || '';

  if (!name) {
    showToast('Please enter a name for the voice.', 'warning');
    return;
  }
  if (!currentPreviewFile) {
    showToast('Generate a preview first.', 'warning');
    return;
  }

  const descriptionEl = document.getElementById('design-description') as HTMLTextAreaElement;
  const sampleTextEl = document.getElementById('design-sample-text') as HTMLTextAreaElement;

  try {
    await API.post('/api/voice_design/save', {
      name,
      description: descriptionEl?.value.trim() || '',
      sample_text: sampleTextEl?.value.trim() || '',
      preview_file: currentPreviewFile,
    });

    if (nameEl) nameEl.value = '';
    editingDesignedVoiceId = null;
    loadDesignedVoices();

  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    showToast('Error saving voice: ' + msg, 'error');
  }
}

/**
 * Play a designed voice audio file.
 * Creates a new Audio element and plays it.
 * @param filename - The audio filename to play
 */
function playDesignedVoice(filename: string): void {
  const audio = new Audio(`/designed_voices/${filename}?t=${Date.now()}`);
  audio.play();
}

/**
 * Delete a designed voice.
 * Confirms with the user, sends DELETE to /api/voice_design/{id}.
 * @param voiceId - The voice ID to delete
 */
async function deleteDesignedVoice(voiceId: string): Promise<void> {
  if (!await showConfirm('Delete this designed voice?')) return;
  try {
    const res = await fetch(`/api/voice_design/${encodeURIComponent(voiceId)}`, { method: 'DELETE' });
    if (!res.ok) {
      let detail = 'Failed to delete.';
      try {
        const err = await res.json();
        detail = err.detail || detail;
      } catch { /* ignore parse error */ }
      showToast(detail, 'error');
      return;
    }
    loadDesignedVoices();
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    showToast('Error deleting voice: ' + msg, 'error');
  }
}

/**
 * Open a designed voice for editing.
 * Loads the voice data into the designer form fields and shows the preview.
 * Populates the alias dropdown with available voice names.
 * @param voiceId - The voice ID to edit
 */
async function openDesignedVoiceForEdit(voiceId: string): Promise<void> {
  try {
    const voice = (state.designedVoices || []).find(v => v.id === voiceId);
    if (!voice) {
      showToast('Designed voice not found', 'error');
      return;
    }

    // Switch to Designer tab
    const designerTabBtn = document.querySelector('[data-tab="designer"]') as HTMLElement;
    if (designerTabBtn) designerTabBtn.click();

    // Populate fields
    const voiceNameEl = document.getElementById('design-voice-name') as HTMLInputElement;
    const sourceNameEl = document.getElementById('design-source-name') as HTMLInputElement;
    const descriptionEl = document.getElementById('design-description') as HTMLTextAreaElement;
    const sampleTextEl = document.getElementById('design-sample-text') as HTMLTextAreaElement;

    if (voiceNameEl) voiceNameEl.value = voice.name || '';
    if (sourceNameEl) sourceNameEl.value = voice.name || '';
    if (descriptionEl) descriptionEl.value = voice.description || '';
    if (sampleTextEl) sampleTextEl.value = voice.sample_text || '';

    // Populate alias dropdown
    const aliasSelect = document.getElementById('design-alias-select') as HTMLSelectElement;
    if (aliasSelect) {
      aliasSelect.innerHTML = '<option value="">-- None --</option>';
      const names = (state.voicesNames || []).filter(n => n !== voice.name);
      names.forEach(n => {
        const opt = document.createElement('option');
        opt.value = n;
        opt.text = n;
        aliasSelect.appendChild(opt);
      });
    }

    // Try to read existing alias from voices config
    try {
      const voices = await API.get<Array<{ name: string; alias_of?: string | null }>>('/api/pipeline/voices');
      const entry = voices.find(v => v.name === voice.name);
      if (aliasSelect) {
        aliasSelect.value = entry?.alias_of || '';
      }
    } catch {
      if (aliasSelect) aliasSelect.value = '';
    }

    // Update preview audio and current preview file
    const audio = document.getElementById('design-preview-audio') as HTMLAudioElement;
    if (audio) {
      audio.src = `/designed_voices/${voice.filename}?t=${Date.now()}`;
    }
    currentPreviewFile = voice.filename;
    editingDesignedVoiceId = voice.id;

    const previewContainer = document.getElementById('design-preview-container');
    if (previewContainer) previewContainer.style.display = 'block';

    const previewButton = document.getElementById('btn-design-preview');
    if (previewButton) {
      previewButton.innerHTML = '<i class="fas fa-wand-magic-sparkles me-1"></i>Re-design Voice';
    }

    // Focus description for quick edits
    if (descriptionEl) descriptionEl.focus();
    showToast('Loaded designed voice for editing', 'info');
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    showToast('Failed to load voice for edit: ' + msg, 'error');
  }
}

/**
 * Open the voice design editor from a voice card in the Voices tab.
 * Populates the designer form with the voice name and description.
 * @param button - The button element that triggered the action
 */
function openVoiceDesignEditor(button: HTMLElement): void {
  const card = button.closest('.card-body');
  const cardRoot = button.closest('.voice-card') as HTMLElement;
  const voiceName = cardRoot?.dataset.voice || '';
  const description = card ? ((card.querySelector('.design-description') as HTMLTextAreaElement)?.value || '') : '';

  // Switch to Designer tab
  const designerTabBtn = document.querySelector('[data-tab="designer"]') as HTMLElement;
  if (designerTabBtn) designerTabBtn.click();

  const voiceNameEl = document.getElementById('design-voice-name') as HTMLInputElement;
  const sourceNameEl = document.getElementById('design-source-name') as HTMLInputElement;
  const descriptionEl = document.getElementById('design-description') as HTMLTextAreaElement;

  if (voiceNameEl) voiceNameEl.value = voiceName;
  if (sourceNameEl) sourceNameEl.value = voiceName;
  if (descriptionEl) descriptionEl.value = description;

  editingDesignedVoiceId = voiceName || null;
  currentPreviewFile = null;

  const previewContainer = document.getElementById('design-preview-container');
  if (previewContainer) previewContainer.style.display = 'none';

  const previewButton = document.getElementById('btn-design-preview');
  if (previewButton) {
    previewButton.innerHTML = '<i class="fas fa-wand-magic-sparkles me-1"></i>Re-design Voice';
  }

  const statusEl = document.getElementById('design-status');
  if (statusEl) {
    statusEl.innerHTML = '<span class="text-muted"><i class="fas fa-info me-1"></i>Edit the description, then generate a preview.</span>';
  }
}

/**
 * Initialize the Designer tab.
 * Attaches event listeners for preview generation, save, refresh, and action buttons.
 * Uses event delegation on #designed-voices-list for play/edit/delete actions.
 * Loads designed voices on init.
 */
export function initDesigner(): void {
  document.addEventListener('DOMContentLoaded', () => {
    // Generate preview button
    const btnPreview = document.getElementById('btn-design-preview');
    if (btnPreview) {
      btnPreview.removeAttribute('onclick');
      btnPreview.addEventListener('click', () => generateDesignPreview());
    }

    // Save voice button
    const btnSave = document.querySelector('#design-preview-container .btn-success');
    if (btnSave) {
      btnSave.removeAttribute('onclick');
      btnSave.addEventListener('click', () => saveDesignedVoice());
    }

    // Refresh designed voices button
    const btnRefresh = document.querySelector('#designed-voices-list')?.closest('.card')?.querySelector('.btn-outline-primary');
    if (btnRefresh) {
      btnRefresh.removeAttribute('onclick');
      btnRefresh.addEventListener('click', () => loadDesignedVoices());
    }

    // Event delegation on designed voices list for play/edit/delete
    const voicesList = document.getElementById('designed-voices-list');
    if (voicesList) {
      voicesList.addEventListener('click', (e) => {
        const target = e.target as HTMLElement;
        const button = target.closest('button[data-action]') as HTMLElement;
        if (!button) return;

        const action = button.dataset.action;
        if (action === 'play') {
          const filename = button.dataset.filename || '';
          playDesignedVoice(filename);
        } else if (action === 'edit') {
          const id = button.dataset.id || '';
          openDesignedVoiceForEdit(id);
        } else if (action === 'delete') {
          const id = button.dataset.id || '';
          deleteDesignedVoice(id);
        }
      });
    }

    // Load designed voices on init
    loadDesignedVoices();
  });
}
