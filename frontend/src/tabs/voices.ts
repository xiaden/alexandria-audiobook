/**
 * Voices tab module — Voice configuration, persona generation, and auto-save
 * Ported from app/static/index.html lines 1604-1961 (JS logic)
 */

import * as API from '../api';
import { showToast } from '../utils';
import { state, type Voice, type VoiceConfig } from '../state';
import { createVoiceCard } from '../templates';

/** Status response from /api/status/persona */
interface PersonaStatus {
  running: boolean;
  logs: string[];
}

/** Voice config shape for save endpoint */
type VoiceConfigMap = Record<string, VoiceConfig & { alias_of?: string; seed?: string }>;

/** Debounce timer for voice config save */
let voiceSaveTimer: ReturnType<typeof setTimeout> | null = null;

/**
 * Toggle advanced persona options visibility.
 * Shows/hides the batch size input based on the advanced toggle checkbox.
 */
function toggleAdvancedPersonaOptions(): void {
  const advanced = document.getElementById('advanced-persona-toggle') as HTMLInputElement;
  const options = document.getElementById('advanced-persona-options');
  if (advanced && options) {
    options.style.display = advanced.checked ? 'flex' : 'none';
  }
}

/**
 * Generate personas via the backend.
 * POSTs to /api/generate_personas with advanced flag and batch_size.
 * Starts polling for persona status after successful submission.
 */
async function generatePersonas(): Promise<void> {
  const statusSpan = document.getElementById('persona-status');
  const cancelButton = document.getElementById('btn-cancel-personas');
  const advancedToggle = document.getElementById('advanced-persona-toggle') as HTMLInputElement;
  const batchInput = document.getElementById('persona-batch-size') as HTMLInputElement;
  const advanced = !!(advancedToggle && advancedToggle.checked);
  const batchSize = Math.max(1, Math.min(parseInt(batchInput?.value || '40', 10) || 40, 200));

  try {
    if (statusSpan) {
      statusSpan.innerHTML = `<i class="fas fa-spinner fa-spin me-1"></i>${advanced ? 'Starting advanced...' : 'Starting...'}`;
    }
    if (cancelButton) {
      cancelButton.style.display = '';
    }
    await API.post('/api/generate_personas', { advanced, batch_size: batchSize });
    pollPersonaStatus();
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    showToast('Failed to start persona generation: ' + msg, 'error');
    if (statusSpan) {
      statusSpan.innerText = '';
    }
    if (cancelButton) {
      cancelButton.style.display = 'none';
    }
  }
}

/**
 * Cancel ongoing persona generation.
 * POSTs to /api/cancel_persona to stop the current persona generation task.
 */
async function cancelPersonas(): Promise<void> {
  try {
    await API.post('/api/cancel_persona', {});
    const statusSpan = document.getElementById('persona-status');
    if (statusSpan) {
      statusSpan.innerText = 'Cancelling...';
    }
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    showToast('Failed to cancel persona generation: ' + msg, 'error');
  }
}

/**
 * Poll persona generation status from the server.
 * Polls GET /api/status/persona every 1.5s until the task is no longer running.
 * Updates status text, cancel button visibility, and logs display.
 * Refreshes voice caches and voices list when complete.
 */
function pollPersonaStatus(): void {
  const logEl = document.getElementById('voices-logs');
  const statusSpan = document.getElementById('persona-status');
  const cancelButton = document.getElementById('btn-cancel-personas');

  const interval = setInterval(async () => {
    try {
      const status = await API.get<PersonaStatus>('/api/status/persona');
      const advanced = !!(document.getElementById('advanced-persona-toggle') as HTMLInputElement)?.checked;

      if (statusSpan) {
        statusSpan.innerText = status.running ? (advanced ? 'Advanced running...' : 'Running...') : 'Finished';
      }
      if (cancelButton) {
        cancelButton.style.display = status.running ? '' : 'none';
      }
      if (logEl) {
        logEl.innerText = (status.logs || []).join('\n');
        logEl.scrollTop = logEl.scrollHeight;
      }

      if (!status.running) {
        clearInterval(interval);
        // Refresh voices and caches
        try { await loadVoices(); } catch (e) { /* ignore */ }
        try { state.designedVoices = await API.get('/api/voice_design/list'); } catch (e) { /* ignore */ }
        try { state.cloneVoices = await API.get('/api/clone_voices/list'); } catch (e) { /* ignore */ }
        showToast('Persona generation finished', 'success');
        if (statusSpan) {
          statusSpan.innerText = '';
        }
        if (cancelButton) {
          cancelButton.style.display = 'none';
        }
      }
    } catch (e) {
      clearInterval(interval);
      const msg = e instanceof Error ? e.message : String(e);
      showToast('Persona status poll failed: ' + msg, 'error');
      if (statusSpan) {
        statusSpan.innerText = '';
      }
      if (cancelButton) {
        cancelButton.style.display = 'none';
      }
    }
  }, 1500);
}

/**
 * Load voices from the server and render voice cards.
 * Fetches voice caches (designed voices, clone voices, LoRA models) and voices list.
 * Renders voice cards using createVoiceCard template function.
 * Triggers auto-save if any voice has no saved config.
 */
export async function loadVoices(): Promise<void> {
  // Refresh voice caches so dropdowns are populated
  try {
    state.designedVoices = await API.get('/api/voice_design/list');
  } catch (e) { /* ignore if designer not available */ }
  try {
    state.cloneVoices = await API.get('/api/clone_voices/list');
  } catch (e) { /* ignore if no uploads */ }
  try {
    state.loraModels = await API.get('/api/lora/models');
  } catch (e) { /* ignore if no adapters */ }

  const voices = await API.get<Voice[]>('/api/voices');
  // Cache voice names for alias dropdowns
  state.voicesNames = voices.map(v => v.name);

  const container = document.getElementById('voices-list');
  if (!container) return;

  if (voices.length === 0) {
    container.innerHTML = '<div class="alert alert-info">No voices found. Generate a script first.</div>';
    return;
  }

  container.innerHTML = voices.map((v, i) => createVoiceCard(v, i)).join('');

  // If any voice has no saved config, save defaults immediately
  if (voices.some(v => !v.config || Object.keys(v.config).length === 0)) {
    debouncedSaveVoices();
  }
}

/**
 * Collect voice configuration from all voice cards in the DOM.
 * Reads voice card DOM elements and builds a config object keyed by voice name.
 * Each voice config includes type-specific fields and optional alias_of.
 * @returns Voice configuration map ready for saving
 */
function collectVoiceConfig(): VoiceConfigMap {
  const cards = document.querySelectorAll<HTMLElement>('.voice-card');
  const config: VoiceConfigMap = {};

  cards.forEach(card => {
    const name = card.dataset.voice;
    if (!name) return;

    const aliasSelect = card.querySelector<HTMLSelectElement>('.alias-select');
    const alias = aliasSelect ? aliasSelect.value : '';
    const typeRadio = card.querySelector<HTMLInputElement>('.voice-type:checked');
    if (!typeRadio) return;
    const type = typeRadio.value;

    if (type === 'custom') {
      const voiceSelect = card.querySelector<HTMLSelectElement>('.voice-select');
      const characterStyle = card.querySelector<HTMLInputElement>('.character-style');
      config[name] = {
        type: 'custom',
        voice: voiceSelect?.value || '',
        character_style: characterStyle?.value || '',
        seed: '-1',
      };
    } else if (type === 'clone') {
      const refText = card.querySelector<HTMLInputElement>('.ref-text');
      const refAudio = card.querySelector<HTMLInputElement>('.ref-audio');
      config[name] = {
        type: 'clone',
        ref_text: refText?.value || '',
        ref_audio: refAudio?.value || '',
        seed: '-1',
      };
    } else if (type === 'builtin_lora') {
      const builtinLoraSelect = card.querySelector<HTMLSelectElement>('.builtin-lora-select');
      const builtinLoraStyle = card.querySelector<HTMLInputElement>('.builtin-lora-style');
      const adapterId = builtinLoraSelect?.value || '';
      const adapterEntry = state.loraModels.find(m => m.id === adapterId);
      config[name] = {
        type: 'builtin_lora',
        adapter_id: adapterId,
        adapter_path: adapterEntry?.adapter_path || '',
        character_style: builtinLoraStyle?.value || '',
        seed: '-1',
      };
    } else if (type === 'lora') {
      const loraAdapterSelect = card.querySelector<HTMLSelectElement>('.lora-adapter-select');
      const loraCharacterStyle = card.querySelector<HTMLInputElement>('.lora-character-style');
      const adapterId = loraAdapterSelect?.value || '';
      const adapterEntry = state.loraModels.find(m => m.id === adapterId);
      config[name] = {
        type: 'lora',
        adapter_id: adapterId,
        adapter_path: adapterEntry?.adapter_path || (adapterId ? `lora_models/${adapterId}` : ''),
        character_style: loraCharacterStyle?.value || '',
        seed: '-1',
      };
    } else if (type === 'design') {
      const designDescription = card.querySelector<HTMLInputElement>('.design-description');
      config[name] = {
        type: 'design',
        description: designDescription?.value || '',
        seed: '-1',
      };
    }

    // Include alias_of if set
    if (alias && config[name]) {
      config[name].alias_of = alias;
    }
  });

  return config;
}

/**
 * Debounced save of voice configuration to the server.
 * Shows "unsaved" status immediately, then waits 800ms before saving.
 * POSTs collected voice config to /api/save_voice_config.
 * Shows "saved" status on success, "save failed" on error.
 */
export function debouncedSaveVoices(): void {
  const statusEl = document.getElementById('voice-save-status');
  if (statusEl) {
    statusEl.innerHTML = '<i class="fas fa-circle text-warning" style="font-size:0.5em;"></i> unsaved';
  }

  if (voiceSaveTimer) {
    clearTimeout(voiceSaveTimer);
  }

  voiceSaveTimer = setTimeout(async () => {
    const cards = document.querySelectorAll('.voice-card');
    if (cards.length === 0) return;

    try {
      const config = collectVoiceConfig();
      await API.post('/api/save_voice_config', config);
      if (statusEl) {
        statusEl.innerHTML = '<i class="fas fa-check text-success me-1"></i>saved';
        setTimeout(() => {
          if (statusEl) statusEl.innerHTML = '';
        }, 2000);
      }
    } catch (e) {
      if (statusEl) {
        statusEl.innerHTML = '<i class="fas fa-times text-danger me-1"></i>save failed';
      }
    }
  }, 800);
}

/**
 * Toggle voice type options visibility within a voice card.
 * Shows/hides the appropriate options section based on the selected voice type.
 * Triggers auto-save after toggling.
 * @param radio - The selected radio button element
 */
function toggleVoiceType(radio: HTMLInputElement): void {
  const cardBody = radio.closest('.card-body');
  if (!cardBody) return;

  const customOpts = cardBody.querySelector<HTMLElement>('.custom-opts');
  const builtinLoraOpts = cardBody.querySelector<HTMLElement>('.builtin-lora-opts');
  const cloneOpts = cardBody.querySelector<HTMLElement>('.clone-opts');
  const loraOpts = cardBody.querySelector<HTMLElement>('.lora-opts');
  const designOpts = cardBody.querySelector<HTMLElement>('.design-opts');

  if (customOpts) customOpts.style.display = radio.value === 'custom' ? 'block' : 'none';
  if (builtinLoraOpts) builtinLoraOpts.style.display = radio.value === 'builtin_lora' ? 'block' : 'none';
  if (cloneOpts) cloneOpts.style.display = radio.value === 'clone' ? 'block' : 'none';
  if (loraOpts) loraOpts.style.display = radio.value === 'lora' ? 'block' : 'none';
  if (designOpts) designOpts.style.display = radio.value === 'design' ? 'block' : 'none';

  debouncedSaveVoices();
}

/**
 * Initialize the Voices tab.
 * Attaches event listeners for persona generation buttons, advanced toggle,
 * voice type radio buttons, and auto-save on voice card changes.
 * Loads voices on init.
 */
export function initVoices(): void {
  document.addEventListener('DOMContentLoaded', () => {
    // Generate personas button
    const btnGenPersonas = document.getElementById('btn-gen-personas');
    if (btnGenPersonas) {
      // Remove inline onclick if present
      btnGenPersonas.removeAttribute('onclick');
      btnGenPersonas.addEventListener('click', () => generatePersonas());
    }

    // Cancel personas button
    const btnCancelPersonas = document.getElementById('btn-cancel-personas');
    if (btnCancelPersonas) {
      // Remove inline onclick if present
      btnCancelPersonas.removeAttribute('onclick');
      btnCancelPersonas.addEventListener('click', () => cancelPersonas());
    }

    // Advanced persona toggle
    const advancedToggle = document.getElementById('advanced-persona-toggle');
    if (advancedToggle) {
      // Remove inline onchange if present
      advancedToggle.removeAttribute('onchange');
      advancedToggle.addEventListener('change', () => toggleAdvancedPersonaOptions());
    }

    // Voice type radio buttons and clone/design actions: event delegation on voices-list container
    const voicesList = document.getElementById('voices-list');
    if (voicesList) {
      voicesList.addEventListener('change', (e) => {
        const target = e.target as HTMLElement;
        const actionEl = target.closest('[data-action]') as HTMLElement;
        
        if (actionEl) {
          const action = actionEl.dataset.action;
          
          // Handle voice type radio button changes
          if (action === 'toggle-voice-type' && (target as HTMLInputElement).type === 'radio') {
            toggleVoiceType(target as HTMLInputElement);
            return;
          }
          
          // Handle designed voice select
          if (action === 'designed-voice-select') {
            // TODO: Implement onDesignedVoiceSelect functionality
            console.warn('designed-voice-select action not yet implemented');
            return;
          }
        }
        
        // Any other change in voices-list triggers auto-save
        debouncedSaveVoices();
      });

      // Input events also trigger auto-save
      voicesList.addEventListener('input', () => {
        debouncedSaveVoices();
      });

      // Click events for clone/design buttons
      voicesList.addEventListener('click', (e) => {
        const target = e.target as HTMLElement;
        const actionEl = target.closest('[data-action]') as HTMLElement;
        if (!actionEl) return;

        const action = actionEl.dataset.action;
        
        switch (action) {
          case 'upload-clone-voice':
            // TODO: Implement uploadCloneVoice functionality
            console.warn('upload-clone-voice action not yet implemented');
            break;
          case 'play-clone-voice':
            // TODO: Implement playCloneVoice functionality
            console.warn('play-clone-voice action not yet implemented');
            break;
          case 'delete-clone-voice':
            // TODO: Implement deleteCloneVoice functionality
            console.warn('delete-clone-voice action not yet implemented');
            break;
          case 'open-voice-design-editor':
            // TODO: Implement openVoiceDesignEditor functionality
            console.warn('open-voice-design-editor action not yet implemented');
            break;
        }
      });

      // Change events for file inputs
      voicesList.addEventListener('change', (e) => {
        const target = e.target as HTMLElement;
        const actionEl = target.closest('[data-action="handle-clone-voice-upload"]') as HTMLElement;
        if (actionEl) {
          // TODO: Implement handleCloneVoiceUpload functionality
          console.warn('handle-clone-voice-upload action not yet implemented');
        }
      });
    }

    // Load voices on init
    loadVoices();
  });
}
