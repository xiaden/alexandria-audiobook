/**
 * Voices tab module — Voice configuration, character ledger display, and auto-save.
 *
 * When state.pipelineEnabled is true:
 *   - Loads characters from GET /api/pipeline/characters/{book_id}
 *   - Displays character ledger with name, aliases, voice assignment, confidence
 *   - Provides voice assignment dropdown per character
 *
 * When state.pipelineEnabled is false:
 *   - Shows a notice to enable pipeline mode (old persona endpoints removed)
 *   - Still loads available TTS voices from /api/voices for cache population
 *
 * Ported from app/static/index.html lines 1604-1961 (JS logic).
 * Phase 5: Connected to pipeline character ledger.
 */

import * as API from '../api';
import { showToast, escapeHtml } from '../utils';
import { state, type Voice, type VoiceConfig } from '../state';
import { createVoiceCard } from '../templates';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** Character from the pipeline character ledger. */
export interface Character {
  id: string;
  name: string;
  /** JSON string of alias names, e.g. '["John","Johnny"]' */
  aliases: string;
  /** Confidence score for character detection (0.0–1.0) */
  confidence: number;
}

/** Voice config shape for save endpoint */
type VoiceConfigMap = Record<string, VoiceConfig & { alias_of?: string; seed?: string }>;

/** Character voice assignment stored in local state */
export interface CharacterVoiceAssignment {
  characterId: string;
  voiceName: string;
}

// ---------------------------------------------------------------------------
// Module-level state
// ---------------------------------------------------------------------------

/** Debounce timer for voice config save */
let voiceSaveTimer: ReturnType<typeof setTimeout> | null = null;

/** Cached characters loaded from the pipeline */
let cachedCharacters: Character[] = [];

/** Local voice assignments for characters (characterId → voiceName) */
let characterVoiceAssignments: Map<string, string> = new Map();

// ---------------------------------------------------------------------------
// Pipeline API functions
// ---------------------------------------------------------------------------

/**
 * Load characters from the pipeline character ledger.
 * GET /api/pipeline/characters/{book_id}
 * @param bookId - Book UUID from the last successful onboard
 * @returns Array of character objects with id, name, aliases, confidence
 */
export async function pipelineCharacters(bookId: string): Promise<Character[]> {
  return API.get<Character[]>(`/api/pipeline/characters/${bookId}`);
}

// ---------------------------------------------------------------------------
// Character ledger display
// ---------------------------------------------------------------------------

/**
 * Parse the aliases JSON string into an array of strings.
 * Handles malformed JSON gracefully by returning an empty array.
 * @param aliasesJson - JSON string of aliases, e.g. '["John","Johnny"]'
 * @returns Array of alias strings
 */
export function parseAliases(aliasesJson: string): string[] {
  try {
    const parsed = JSON.parse(aliasesJson);
    if (Array.isArray(parsed)) {
      return parsed.filter((a): a is string => typeof a === 'string');
    }
    return [];
  } catch {
    return [];
  }
}

/**
 * Format a confidence score as a percentage string.
 * @param confidence - Confidence value between 0.0 and 1.0
 * @returns Formatted string like "85%"
 */
export function formatConfidence(confidence: number): string {
  return `${Math.round(confidence * 100)}%`;
}

/**
 * Get the confidence badge CSS class based on the confidence score.
 * @param confidence - Confidence value between 0.0 and 1.0
 * @returns Bootstrap badge class name
 */
export function getConfidenceBadgeClass(confidence: number): string {
  if (confidence >= 0.8) return 'bg-success';
  if (confidence >= 0.6) return 'bg-warning text-dark';
  return 'bg-danger';
}

/**
 * Create HTML for a character card in the ledger display.
 * Shows character name, aliases, voice assignment dropdown, and confidence.
 * @param character - Character object from the pipeline
 * @param index - Index for unique element naming
 * @returns HTML string for the character card
 */
export function createCharacterCard(character: Character, index: number): string {
  const aliases = parseAliases(character.aliases);
  const confidence = character.confidence ?? 0;
  const confidenceBadgeClass = getConfidenceBadgeClass(confidence);
  const assignedVoice = characterVoiceAssignments.get(character.id) || '';

  const aliasesHtml = aliases.length > 0
    ? aliases.map(a => `<span class="badge bg-light text-dark border me-1">${escapeHtml(a)}</span>`).join('')
    : '<span class="text-muted small">No aliases</span>';

  // Build voice options from available voices
  const voiceOptions = state.voicesNames.map(v =>
    `<option value="${escapeHtml(v)}" ${v === assignedVoice ? 'selected' : ''}>${escapeHtml(v)}</option>`
  ).join('');

  return `
    <div class="card character-card mb-3" data-character-id="${escapeHtml(character.id)}">
      <div class="card-body">
        <div class="row align-items-center">
          <div class="col-md-3">
            <h5 class="card-title mb-1">${escapeHtml(character.name)}</h5>
            <span class="badge ${confidenceBadgeClass}" title="Character detection confidence">
              ${formatConfidence(confidence)}
            </span>
          </div>
          <div class="col-md-4">
            <div class="form-text small text-muted mb-1">Aliases:</div>
            <div class="character-aliases">${aliasesHtml}</div>
          </div>
          <div class="col-md-3">
            <label class="form-text small text-muted mb-1 d-block">Voice Assignment:</label>
            <select class="form-select form-select-sm character-voice-select"
                    data-character-id="${escapeHtml(character.id)}"
                    data-action="character-voice-change">
              <option value="">-- Unassigned --</option>
              ${voiceOptions}
            </select>
          </div>
          <div class="col-md-2 text-end">
            ${assignedVoice
              ? `<span class="badge bg-info" title="Assigned voice">${escapeHtml(assignedVoice)}</span>`
              : '<span class="text-muted small">Unassigned</span>'}
          </div>
        </div>
      </div>
    </div>`;
}

/**
 * Render the character ledger into the #character-ledger container.
 * Displays all characters with their aliases, voice assignments, and confidence.
 * @param characters - Array of character objects from the pipeline
 */
export function renderCharacterLedger(characters: Character[]): void {
  const container = document.getElementById('character-ledger');
  if (!container) return;

  cachedCharacters = characters;

  if (characters.length === 0) {
    container.innerHTML = '<div class="alert alert-info">No characters found. Run the character discovery walks first.</div>';
    return;
  }

  container.innerHTML = characters.map((c, i) => createCharacterCard(c, i)).join('');
}

// ---------------------------------------------------------------------------
// Voice loading (branches on pipelineEnabled)
// ---------------------------------------------------------------------------

/**
 * Load voices from the server and render voice cards or character ledger.
 *
 * When pipelineEnabled is true:
 *   - Loads available TTS voices from /api/voices (for dropdown population)
 *   - Loads characters from /api/pipeline/characters/{book_id}
 *   - Renders character ledger with voice assignment dropdowns
 *
 * When pipelineEnabled is false:
 *   - Loads voice caches (designed voices, clone voices, LoRA models)
 *   - Loads voices from /api/voices
 *   - Renders voice cards using createVoiceCard template
 */
export async function loadVoices(): Promise<void> {
  // Always load available TTS voices for dropdown population
  const voices = await API.get<Voice[]>('/api/voices');
  state.voicesNames = voices.map(v => v.name);

  if (state.pipelineEnabled) {
    // Pipeline mode: load characters from the ledger
    await loadPipelineCharacters();
  } else {
    // Legacy mode: load voice caches and render voice cards
    await loadLegacyVoices(voices);
  }
}

/**
 * Load characters from the pipeline and render the character ledger.
 * Called when state.pipelineEnabled is true.
 */
async function loadPipelineCharacters(): Promise<void> {
  const pipelineNotice = document.getElementById('pipeline-voices-disabled-notice');
  const pipelineSection = document.getElementById('pipeline-voices-section');
  const legacySection = document.getElementById('legacy-voices-section');

  // Show pipeline section, hide legacy
  if (pipelineNotice) pipelineNotice.style.display = 'none';
  if (pipelineSection) pipelineSection.style.display = '';
  if (legacySection) legacySection.style.display = 'none';

  const bookId = state.pipelineBookId;
  if (!bookId) {
    const container = document.getElementById('character-ledger');
    if (container) {
      container.innerHTML = '<div class="alert alert-warning">No book onboarded yet. Please onboard an EPUB in the Script tab first.</div>';
    }
    return;
  }

  try {
    const characters = await pipelineCharacters(bookId);
    renderCharacterLedger(characters);
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    showToast('Failed to load characters: ' + msg, 'error');
    const container = document.getElementById('character-ledger');
    if (container) {
      container.innerHTML = `<div class="alert alert-danger">Failed to load characters: ${escapeHtml(msg)}</div>`;
    }
  }
}

/**
 * Load legacy voice caches and render voice cards.
 * Called when state.pipelineEnabled is false.
 * @param voices - Pre-loaded voices from /api/voices
 */
async function loadLegacyVoices(voices: Voice[]): Promise<void> {
  // Load voice caches for dropdowns
  try {
    state.designedVoices = await API.get('/api/voice_design/list');
  } catch (e) { /* ignore if designer not available */ }
  try {
    state.cloneVoices = await API.get('/api/clone_voices/list');
  } catch (e) { /* ignore if no uploads */ }
  try {
    state.loraModels = await API.get('/api/lora/models');
  } catch (e) { /* ignore if no adapters */ }

  const pipelineNotice = document.getElementById('pipeline-voices-disabled-notice');
  const pipelineSection = document.getElementById('pipeline-voices-section');
  const legacySection = document.getElementById('legacy-voices-section');

  // Show legacy section, hide pipeline
  if (pipelineNotice) pipelineNotice.style.display = '';
  if (pipelineSection) pipelineSection.style.display = 'none';
  if (legacySection) legacySection.style.display = '';

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

// ---------------------------------------------------------------------------
// Voice config collection and saving (preserved from previous version)
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// Character voice assignment handlers
// ---------------------------------------------------------------------------

/**
 * Handle a character voice assignment change.
 * Updates the local assignment map and re-renders the character card.
 * @param characterId - The character ID
 * @param voiceName - The selected voice name (empty string = unassigned)
 */
export function handleCharacterVoiceChange(characterId: string, voiceName: string): void {
  if (voiceName) {
    characterVoiceAssignments.set(characterId, voiceName);
  } else {
    characterVoiceAssignments.delete(characterId);
  }

  // Update the card's assigned voice display
  const card = document.querySelector(`.character-card[data-character-id="${characterId}"]`);
  if (card) {
    const voiceBadge = card.querySelector('.col-md-2');
    if (voiceBadge) {
      if (voiceName) {
        voiceBadge.innerHTML = `<span class="badge bg-info" title="Assigned voice">${escapeHtml(voiceName)}</span>`;
      } else {
        voiceBadge.innerHTML = '<span class="text-muted small">Unassigned</span>';
      }
    }
  }

  // Trigger debounced save
  debouncedSaveVoices();
}

/**
 * Get the current character voice assignments.
 * @returns Map of characterId → voiceName
 */
export function getCharacterVoiceAssignments(): Map<string, string> {
  return new Map(characterVoiceAssignments);
}

/**
 * Get the cached characters.
 * @returns Array of characters from the last pipeline load
 */
export function getCachedCharacters(): Character[] {
  return [...cachedCharacters];
}

// ---------------------------------------------------------------------------
// Voice type toggle (preserved for legacy voice cards)
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// Initialization
// ---------------------------------------------------------------------------

/**
 * Initialize the Voices tab.
 * Attaches event listeners for:
 *   - Character voice assignment dropdowns (pipeline mode)
 *   - Voice type radio buttons (legacy mode)
 *   - Auto-save on voice card changes (legacy mode)
 *
 * Shows character ledger when state.pipelineEnabled is true;
 * otherwise shows legacy voice cards with a notice about pipeline mode.
 */
export function initVoices(): void {
  document.addEventListener('DOMContentLoaded', () => {
    // ----- Pipeline mode: character voice assignment -----
    const characterLedger = document.getElementById('character-ledger');
    if (characterLedger) {
      characterLedger.addEventListener('change', (e) => {
        const target = e.target as HTMLElement;
        const select = target.closest('.character-voice-select') as HTMLSelectElement;
        if (select) {
          const characterId = select.dataset.characterId;
          if (characterId) {
            handleCharacterVoiceChange(characterId, select.value);
          }
        }
      });
    }

    // ----- Legacy mode: voice type radio buttons and clone/design actions -----
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
