/**
 * Voices tab module — Pipeline character ledger, narrator voice, and voice assignment.
 *
 * - Loads available TTS voices from GET /api/pipeline/voices (for dropdown population)
 * - Loads characters from GET /api/pipeline/characters/{book_id}
 * - Displays character ledger with name, aliases, voice assignment, confidence
 * - Persists voice assignments via PUT /api/pipeline/characters/{id}/voice
 * - Narrator voice selector persists via PUT /api/pipeline/voices/NARRATOR
 * - Voice catalog cards with Preview buttons (POST /api/pipeline/voices/{id}/preview)
 */

import * as API from '../api';
import { showToast, escapeHtml } from '../utils';
import { state } from '../state';

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

/** Voice config row from GET /api/pipeline/voices (all 12 columns returned). */
export interface VoiceConfigRow {
  id: string;
  name: string;
  /** TTS engine voice name used by this config (the NARRATOR row's `voice` column). */
  voice: string | null;
  /** Voice type: custom | clone | lora | builtin_lora | design. */
  type?: string | null;
  /** Optional human-readable description of the voice. */
  description?: string | null;
}

/** Default narrator TTS voice when no NARRATOR row exists (matches backend NARRATOR_VOICE). */
export const NARRATOR_DEFAULT_VOICE = 'Ryan';

// ---------------------------------------------------------------------------
// Module-level state
// ---------------------------------------------------------------------------

/** Cached characters loaded from the pipeline */
let cachedCharacters: Character[] = [];

/** Local voice assignments for characters (characterId → voiceName) */
let characterVoiceAssignments: Map<string, string> = new Map();

/** Current narrator TTS voice (voice column of the NARRATOR row; defaults to Ryan). */
let currentNarratorVoice: string = NARRATOR_DEFAULT_VOICE;

/** Name of the NARRATOR pseudo-row, excluded from the narrator dropdown options. */
let narratorRowName: string | null = null;

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
 * @param _index - Reserved for unique element naming (unused)
 * @returns HTML string for the character card
 */
export function createCharacterCard(character: Character, _index: number): string {
  const aliases = parseAliases(character.aliases);
  const confidence = character.confidence ?? 0;
  const confidenceBadgeClass = getConfidenceBadgeClass(confidence);
  const assignedVoice = characterVoiceAssignments.get(character.id) || '';

  const aliasesHtml = aliases.length > 0
    ? aliases.map(a => `<span class="badge bg-light text-dark border me-1">${escapeHtml(a)}</span>`).join('')
    : '<span class="text-muted small">No aliases</span>';

  // Build voice options from available voices, excluding the NARRATOR
  // pseudo-row — assigning it would deliver voice='NARRATOR' to the TTS
  // engine instead of the real TTS voice (see _build_voice_config).
  const voiceOptions = state.voicesNames
    .filter(v => v !== narratorRowName)
    .map(v =>
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
// Narrator voice selector
// ---------------------------------------------------------------------------

/**
 * Render the narrator voice selector dropdown.
 * Lists every available TTS voice (state.voicesNames), excluding the NARRATOR
 * pseudo-row itself, and selects the current narrator voice. The current voice
 * is always present as an option (added if missing) so the dropdown never shows
 * an empty selection. No-op when the #narrator-voice-select element is absent.
 */
export function renderNarratorSelector(): void {
  const select = document.getElementById('narrator-voice-select') as HTMLSelectElement | null;
  if (!select) return;

  const voices = state.voicesNames.filter(v => v !== narratorRowName);
  if (currentNarratorVoice !== '' && !voices.includes(currentNarratorVoice)) {
    voices.push(currentNarratorVoice);
  }

  const options = voices.map(v =>
    `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`
  ).join('');
  select.innerHTML = options;
  select.value = currentNarratorVoice;
}

// ---------------------------------------------------------------------------
// Voice catalog display
// ---------------------------------------------------------------------------

/**
 * Create HTML for a voice catalog card.
 * Shows the voice name, a type badge, and a Preview button that triggers
 * POST /api/pipeline/voices/{id}/preview via the delegated click handler.
 * @param voice - Voice config row from GET /api/pipeline/voices
 * @returns HTML string for the voice card
 */
export function createVoiceCard(voice: VoiceConfigRow): string {
  const type = voice.type || 'unknown';
  return `
    <div class="card voice-card mb-2" data-voice-id="${escapeHtml(voice.id)}">
      <div class="card-body d-flex justify-content-between align-items-center">
        <div>
          <h6 class="card-title mb-1">${escapeHtml(voice.name)}</h6>
          <span class="badge bg-secondary voice-type-badge">${escapeHtml(type)}</span>
        </div>
        <button type="button" class="btn btn-sm btn-outline-success"
                data-action="preview-voice"
                data-voice-id="${escapeHtml(voice.id)}"
                title="Preview this voice">
          <i class="fas fa-play me-1"></i>Preview
        </button>
      </div>
    </div>`;
}

/**
 * Render the voice catalog into the #voice-catalog container — one card per
 * available TTS voice. The NARRATOR pseudo-row (id='NARRATOR') is excluded:
 * it is not a real TTS voice (its `voice` column holds the narrator's chosen
 * engine voice, which is already listed as its own row) and previewing it
 * would synthesize with speaker 'NARRATOR'. No-op when the container is absent.
 * @param voices - Voice config rows from GET /api/pipeline/voices
 */
export function renderVoiceCatalog(voices: VoiceConfigRow[]): void {
  const container = document.getElementById('voice-catalog');
  if (!container) return;

  const catalogVoices = voices.filter(v => v.id !== 'NARRATOR');

  if (catalogVoices.length === 0) {
    container.innerHTML = '<div class="text-muted small">No voices available yet.</div>';
    return;
  }

  container.innerHTML = catalogVoices.map(createVoiceCard).join('');
}

// ---------------------------------------------------------------------------
// Voice loading
// ---------------------------------------------------------------------------

/**
 * Load voices from the server and render the character ledger.
 * Always loads available TTS voices from /api/pipeline/voices (for dropdown
 * population), then loads pipeline characters and renders the ledger.
 */
export async function loadVoices(): Promise<void> {
  // Always load available TTS voices for dropdown population
  let voices: VoiceConfigRow[] = [];
  try {
    voices = await API.get<VoiceConfigRow[]>('/api/pipeline/voices');
    state.voicesNames = voices.map(v => v.name);

    // Track the narrator's TTS voice (the NARRATOR row's `voice` column),
    // falling back to the default when the row is missing.
    const narratorRow = voices.find(v => v.id === 'NARRATOR');
    currentNarratorVoice = narratorRow?.voice || NARRATOR_DEFAULT_VOICE;
    narratorRowName = narratorRow?.name ?? null;

    renderNarratorSelector();
    renderVoiceCatalog(voices);
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    showToast('Failed to load voices: ' + msg, 'error');
  }

  await loadPipelineCharacters();
}

/**
 * Load characters from the pipeline and render the character ledger.
 */
async function loadPipelineCharacters(): Promise<void> {
  const pipelineSection = document.getElementById('pipeline-voices-section');
  if (pipelineSection) pipelineSection.style.display = '';

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

// ---------------------------------------------------------------------------
// Character voice assignment handlers
// ---------------------------------------------------------------------------

/**
 * Handle a character voice assignment change.
 * Updates the local assignment map, re-renders the character card, and
 * persists the assignment to the pipeline API
 * (PUT /api/pipeline/characters/{id}/voice).
 *
 * @param characterId - The character ID
 * @param voiceName - The selected voice name (empty string = unassigned)
 */
export function handleCharacterVoiceChange(characterId: string, voiceName: string): void {
  if (voiceName) {
    characterVoiceAssignments.set(characterId, voiceName);
  } else {
    characterVoiceAssignments.delete(characterId);
  }

  // Update the card's assigned voice display (immediate UX feedback)
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

  // Persist to the pipeline character voice endpoint
  API.put(`/api/pipeline/characters/${characterId}/voice`, {
    voice_assignment_id: voiceName || null,
  }).then(() => {
    showToast(`Voice assigned: ${voiceName || '(cleared)'}`, 'success');
  }).catch((e: unknown) => {
    const msg = e instanceof Error ? e.message : String(e);
    showToast(`Failed to save voice assignment: ${msg}`, 'error');
  });
}

/**
 * Get the current character voice assignments.
 * @returns Map of characterId → voiceName
 */
export function getCharacterVoiceAssignments(): Map<string, string> {
  return new Map(characterVoiceAssignments);
}

/**
 * Get the current narrator TTS voice name.
 * @returns The narrator voice from the last voices load (default 'Ryan').
 */
export function getCurrentNarratorVoice(): string {
  return currentNarratorVoice;
}

/**
 * Handle a narrator voice selection change.
 * Keeps the local value in sync so the UI reflects the chosen voice
 * immediately, then persists to the pipeline API
 * (PUT /api/pipeline/voices/NARRATOR). Only the `voice` field is sent — the
 * backend's exclude_unset handling preserves the rest of the NARRATOR row.
 *
 * @param voiceName - The selected TTS voice name
 */
export function handleNarratorVoiceChange(voiceName: string): void {
  if (!voiceName) return;

  currentNarratorVoice = voiceName;

  API.put('/api/pipeline/voices/NARRATOR', { voice: voiceName }).then(() => {
    showToast(`Narrator voice set to ${voiceName}`, 'success');
  }).catch((e: unknown) => {
    const msg = e instanceof Error ? e.message : String(e);
    showToast(`Failed to update narrator voice: ${msg}`, 'error');
  });
}

/**
 * Get the cached characters.
 * @returns Array of characters from the last pipeline load
 */
export function getCachedCharacters(): Character[] {
  return [...cachedCharacters];
}

/**
 * Preview a voice by generating audio and playing it.
 * Calls POST /api/pipeline/voices/{voice_id}/preview with sample text.
 * @param voiceId - The voice ID to preview
 * @param button - The button element (for loading state)
 */
export async function previewVoice(voiceId: string, button: HTMLButtonElement): Promise<void> {
  const originalHtml = button.innerHTML;
  const icon = button.querySelector('i');

  // Show loading state
  button.disabled = true;
  button.classList.add('disabled');
  if (icon) {
    icon.className = 'fas fa-spinner fa-spin me-1';
  }

  try {
    const response = await API.post<{ audio_url: string; voice_id: string }>(
      `/api/pipeline/voices/${encodeURIComponent(voiceId)}/preview`,
      { sample_text: 'This is a preview of the voice.' }
    );

    // Play the audio
    const audio = new Audio(response.audio_url);
    audio.play().catch((err) => {
      showToast(`Failed to play preview: ${err.message}`, 'error');
    });

    showToast('Preview generated successfully', 'success');
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    showToast(`Failed to generate preview: ${msg}`, 'error');
  } finally {
    // Restore button state
    button.disabled = false;
    button.classList.remove('disabled');
    button.innerHTML = originalHtml;
  }
}

// ---------------------------------------------------------------------------
// Initialization
// ---------------------------------------------------------------------------

/**
 * Initialize the Voices tab.
 * Attaches event listeners for:
 *   - Narrator voice selection dropdown (#narrator-voice-select change)
 *   - Character voice assignment dropdowns (#character-ledger, delegated change)
 *   - Voice catalog preview buttons (#voice-catalog, delegated click on
 *     [data-action="preview-voice"])
 *
 * Loads available TTS voices and the character ledger on init.
 */
export function initVoices(): void {
  document.addEventListener('DOMContentLoaded', () => {
    // ----- Narrator voice selection -----
    const narratorSelect = document.getElementById('narrator-voice-select') as HTMLSelectElement | null;
    if (narratorSelect) {
      narratorSelect.addEventListener('change', () => {
        handleNarratorVoiceChange(narratorSelect.value);
      });
    }

    // ----- Character voice assignment -----
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

    // ----- Voice preview -----
    // Event delegation: any [data-action="preview-voice"] button inside the
    // voice catalog triggers a preview of its data-voice-id voice.
    const voiceCatalog = document.getElementById('voice-catalog');
    if (voiceCatalog) {
      voiceCatalog.addEventListener('click', (e) => {
        const target = e.target as HTMLElement;
        const button = target.closest('[data-action="preview-voice"]') as HTMLButtonElement | null;
        if (button) {
          const voiceId = button.dataset.voiceId;
          if (voiceId) {
            previewVoice(voiceId, button);
          }
        }
      });
    }

    // Load voices on init
    loadVoices();
  });
}
