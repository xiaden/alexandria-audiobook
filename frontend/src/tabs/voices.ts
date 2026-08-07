/**
 * Voices tab module — Pipeline character ledger, narrator voice, and voice assignment.
 *
 * - Loads available TTS voices from GET /api/pipeline/voices (for dropdown population)
 * - Loads characters from GET /api/pipeline/characters/{book_id}
 * - Displays character ledger with name, aliases, voice assignment, confidence
 * - Persists voice assignments via PUT /api/pipeline/characters/{id}/voice
 * - Narrator voice selector persists via PUT /api/pipeline/voices/NARRATOR
 * - Voice catalog cards with Preview buttons (POST /api/pipeline/voices/{id}/preview)
 * - Voice config edit form surface (openVoiceEditForm/saveVoiceConfig/
 *   previewVoiceFromForm): edit style, reference audio/text, type and alias,
 *   persist via PUT /api/pipeline/voices/{id} and preview from the form
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

/**
 * Voice config row from GET /api/pipeline/voices (all 12 voice_config columns).
 *
 * The 12 DB columns map 1:1 to the backend VoiceCreateRequest/VoiceUpdateRequest
 * fields (CONTRACTS.md § Voice Catalog): id, name, description, type, voice,
 * character_style, seed, ref_audio, ref_text, adapter_id, adapter_path, alias_of.
 * All config columns except id/name/voice are optional on the wire; a row may
 * omit them (null or absent) depending on its type.
 */
export interface VoiceConfigRow {
  id: string;
  name: string;
  /** TTS engine voice name used by this config (the NARRATOR row's `voice` column). */
  voice: string | null;
  /** Voice type: custom | clone | lora | builtin_lora | design. */
  type?: string | null;
  /** Optional human-readable description of the voice. */
  description?: string | null;
  /** Character style hint for the TTS engine (e.g. 'warm', 'cheerful'). */
  character_style?: string | null;
  /** Random seed used when synthesizing this voice (string; default '-1'). */
  seed?: string | null;
  /** Path/URL of the reference audio sample (clone voices). */
  ref_audio?: string | null;
  /** Reference transcript aligned with ref_audio (clone voices). */
  ref_text?: string | null;
  /** Adapter id for LoRA/builtin_lora voices. */
  adapter_id?: string | null;
  /** Adapter path for LoRA/builtin_lora voices. */
  adapter_path?: string | null;
  /** Id of the canonical voice this row aliases (alias voices). */
  alias_of?: string | null;
}

/** Default narrator TTS voice when no NARRATOR row exists (matches backend NARRATOR_VOICE). */
export const NARRATOR_DEFAULT_VOICE = 'Ryan';

/**
 * Default sample text used for voice previews (card preview button and the
 * edit form's preview trigger when ref_text is empty). Matches the contract's
 * VoicePreviewRequest example and keeps previewVoice's original behavior.
 */
const DEFAULT_PREVIEW_SAMPLE_TEXT = 'This is a preview of the voice.';

// ---------------------------------------------------------------------------
// Module-level state
// ---------------------------------------------------------------------------

/** Cached characters loaded from the pipeline */
let cachedCharacters: Character[] = [];

/** Local voice assignments for characters (characterId → voiceName) */
let characterVoiceAssignments: Map<string, string> = new Map();

/**
 * Lookup from voice NAME → voice_config id, populated by registerVoiceCatalog
 * (called from loadVoices). The backend validates voice_assignment_id against
 * voice_config.id, so the UI must translate dropdown names to ids before PUT.
 */
let voiceNameToId: Map<string, string> = new Map();

/**
 * Lookup from voice_config id → full row, populated by registerVoiceCatalog
 * (called from loadVoices). Phase 2's edit form consumes getVoiceConfigRow
 * (backed by this map) to pre-fill its fields from the fetched config.
 */
let voiceRowsById: Map<string, VoiceConfigRow> = new Map();

/** Current narrator TTS voice (voice column of the NARRATOR row; defaults to Ryan). */
let currentNarratorVoice: string = NARRATOR_DEFAULT_VOICE;

/** Name of the NARRATOR pseudo-row, excluded from the narrator dropdown options. */
let narratorRowName: string | null = null;

/**
 * Id of the voice whose config is currently open in #voice-edit-form
 * (null when the form is closed). Set by openVoiceEditForm, cleared by
 * closeVoiceEditForm. Phase 3's save handler reads it to PUT the right row.
 */
let editingVoiceId: string | null = null;

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
 * Shows the voice name, a type badge, and Preview + Edit buttons. Preview
 * triggers POST /api/pipeline/voices/{id}/preview and Edit opens the shared
 * #voice-edit-form pre-filled from the fetched config, both via the delegated
 * click handler. When the row carries a character_style, a voice-style-badge
 * is rendered next to the type badge (omitted when character_style is absent).
 * @param voice - Voice config row from GET /api/pipeline/voices
 * @returns HTML string for the voice card
 */
export function createVoiceCard(voice: VoiceConfigRow): string {
  const type = voice.type || 'unknown';
  const style = voice.character_style
    ? `<span class="badge bg-light text-dark border voice-style-badge me-1" title="Character style">${escapeHtml(voice.character_style)}</span>`
    : '';
  return `
    <div class="card voice-card mb-2" data-voice-id="${escapeHtml(voice.id)}">
      <div class="card-body d-flex justify-content-between align-items-center">
        <div>
          <h6 class="card-title mb-1">${escapeHtml(voice.name)}</h6>
          <span class="badge bg-secondary voice-type-badge">${escapeHtml(type)}</span>
          ${style}
        </div>
        <div class="d-flex gap-2">
          <button type="button" class="btn btn-sm btn-outline-success"
                  data-action="preview-voice"
                  data-voice-id="${escapeHtml(voice.id)}"
                  title="Preview this voice">
            <i class="fas fa-play me-1"></i>Preview
          </button>
          <button type="button" class="btn btn-sm btn-outline-secondary"
                  data-action="edit-voice"
                  data-voice-id="${escapeHtml(voice.id)}"
                  title="Edit voice config">
            <i class="fas fa-edit me-1"></i>Edit
          </button>
        </div>
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
 * Register the voice catalog for name→id resolution.
 *
 * Populates state.voicesNames (dropdown options) and the module-level
 * name→id lookup used by handleCharacterVoiceChange when persisting
 * assignments. Side effect: also populates the module-level voiceRowsById
 * map (id → full row) backing getVoiceConfigRow, which the voice config
 * edit form consumes to pre-fill its fields. The backend validates
 * voice_assignment_id against voice_config.id, so names must be translated
 * to ids before PUT.
 * Exported for tests; loadVoices delegates to it.
 * @param voices - Voice config rows from GET /api/pipeline/voices
 */
export function registerVoiceCatalog(voices: VoiceConfigRow[]): void {
  state.voicesNames = voices.map(v => v.name);
  voiceNameToId = new Map(voices.map(v => [v.name, v.id]));
  voiceRowsById = new Map(voices.map(v => [v.id, v]));
}

/**
 * Get the full voice config row for a voice_config id.
 *
 * Returns the complete 12-column row fetched from GET /api/pipeline/voices
 * (populated by registerVoiceCatalog via loadVoices). The edit form consumes
 * this to pre-fill its fields; undefined for ids not in the loaded catalog.
 * @param voiceId - voice_config id
 */
export function getVoiceConfigRow(voiceId: string): VoiceConfigRow | undefined {
  return voiceRowsById.get(voiceId);
}

// ---------------------------------------------------------------------------
// Voice config edit form
// ---------------------------------------------------------------------------

/**
 * Populate the #voice-edit-alias-of select with every OTHER existing voice.
 *
 * Option VALUE is the voice_config id (the same name→id translation convention
 * used for character assignments); the label is the voice name. The edited
 * voice itself and the NARRATOR pseudo-row are excluded — aliasing a voice to
 * itself is meaningless and NARRATOR is not a real TTS voice. The first option
 * is a cleared alias ('—'); selecting it leaves alias_of empty (Phase 3 maps
 * the empty selection to `alias_of: null`). A current alias that points at a
 * voice absent from the catalog (stale) is appended so the select still shows
 * the row's actual alias_of value.
 * @param select - The alias_of select element
 * @param voiceId - Id of the voice being edited (excluded from options)
 * @param currentAliasOf - The row's current alias_of id (or null/undefined)
 */
function populateAliasOptions(
  select: HTMLSelectElement,
  voiceId: string,
  currentAliasOf: string | null | undefined,
): void {
  const others = [...voiceRowsById.values()]
    .filter(r => r.id !== 'NARRATOR' && r.id !== voiceId)
    .map(r =>
      `<option value="${escapeHtml(r.id)}" ${r.id === currentAliasOf ? 'selected' : ''}>${escapeHtml(r.name)}</option>`
    )
    .join('');

  select.innerHTML =
    `<option value="" ${!currentAliasOf ? 'selected' : ''}>— None —</option>` + others;

  if (currentAliasOf && !voiceRowsById.has(currentAliasOf)) {
    select.insertAdjacentHTML(
      'beforeend',
      `<option value="${escapeHtml(currentAliasOf)}" selected>${escapeHtml(currentAliasOf)}</option>`
    );
  }
}

/**
 * Open the shared edit form pre-filled with a voice's current config values.
 *
 * Reads the full fetched row via getVoiceConfigRow(voiceId) and populates the
 * #voice-edit-form fields (character_style, ref_audio, ref_text, type,
 * alias_of). An unknown id reports an error toast and leaves the form closed.
 * @param voiceId - The voice_config id to edit
 */
export function openVoiceEditForm(voiceId: string): void {
  const row = getVoiceConfigRow(voiceId);
  if (!row) {
    showToast(`Voice config not found: ${voiceId}`, 'error');
    return;
  }

  const form = document.getElementById('voice-edit-form');
  if (!form) return;

  const styleInput = document.getElementById('voice-edit-character-style') as HTMLInputElement | null;
  const refAudioInput = document.getElementById('voice-edit-ref-audio') as HTMLInputElement | null;
  const refTextInput = document.getElementById('voice-edit-ref-text') as HTMLInputElement | null;
  const typeSelect = document.getElementById('voice-edit-type') as HTMLSelectElement | null;
  const aliasSelect = document.getElementById('voice-edit-alias-of') as HTMLSelectElement | null;
  const title = document.getElementById('voice-edit-title');

  if (styleInput) styleInput.value = row.character_style ?? '';
  if (refAudioInput) refAudioInput.value = row.ref_audio ?? '';
  if (refTextInput) refTextInput.value = row.ref_text ?? '';
  if (typeSelect) typeSelect.value = row.type ?? 'custom';
  if (aliasSelect) populateAliasOptions(aliasSelect, voiceId, row.alias_of);
  if (title) title.textContent = row.name;

  editingVoiceId = voiceId;
  form.style.display = '';
}

/**
 * Close the shared edit form without saving.
 * Hides #voice-edit-form and forgets which voice was being edited.
 */
export function closeVoiceEditForm(): void {
  const form = document.getElementById('voice-edit-form');
  if (form) form.style.display = 'none';
  editingVoiceId = null;
}

/**
 * Save the currently open voice config edit form.
 *
 * PUTs /api/pipeline/voices/{id} with ONLY the edited fields — the backend
 * VoiceUpdateRequest contract is exclude_unset: only keys explicitly present
 * in the body are updated, and a field set to null is cleared. The handler
 * diffs each form field against the voice's current fetched row
 * (getVoiceConfigRow) and skips untouched fields; blanked text inputs and a
 * cleared alias selection send null. The type select only offers the 5 valid
 * types (custom/clone/builtin_lora/lora/design), so its value is sent verbatim
 * when changed. On success the catalog card is re-rendered from the response
 * (the updated 12-column voice config dict), the module row cache is refreshed
 * for the next edit, and the form closes with a success toast; on failure an
 * error toast keeps the form open so the user can correct the values.
 * @param voiceId - The voice_config id being edited
 */
export function saveVoiceConfig(voiceId: string): void {
  const row = getVoiceConfigRow(voiceId);
  if (!row) {
    showToast(`Voice config not found: ${voiceId}`, 'error');
    return;
  }

  const styleInput = document.getElementById('voice-edit-character-style') as HTMLInputElement | null;
  const refAudioInput = document.getElementById('voice-edit-ref-audio') as HTMLInputElement | null;
  const refTextInput = document.getElementById('voice-edit-ref-text') as HTMLInputElement | null;
  const typeSelect = document.getElementById('voice-edit-type') as HTMLSelectElement | null;
  const aliasSelect = document.getElementById('voice-edit-alias-of') as HTMLSelectElement | null;

  // Build the PUT body from EDITED keys only (exclude_unset semantics): a key
  // the user left untouched is not sent at all; a blanked text field or a
  // cleared alias is sent as null so the backend clears the stored value.
  const body: Record<string, string | null> = {};

  const diffText = (
    input: HTMLInputElement | null,
    key: 'character_style' | 'ref_audio' | 'ref_text',
  ): void => {
    if (!input) return;
    const value = input.value.trim();
    const current = row[key] ?? '';
    if (value !== current) {
      body[key] = value === '' ? null : value;
    }
  };

  diffText(styleInput, 'character_style');
  diffText(refAudioInput, 'ref_audio');
  diffText(refTextInput, 'ref_text');

  if (typeSelect) {
    const currentType = row.type ?? 'custom';
    if (typeSelect.value !== currentType) {
      // The select only offers the 5 valid types — send the choice verbatim.
      body.type = typeSelect.value;
    }
  }

  if (aliasSelect) {
    const currentAlias = row.alias_of ?? '';
    if (aliasSelect.value !== currentAlias) {
      body.alias_of = aliasSelect.value === '' ? null : aliasSelect.value;
    }
  }

  API.put<VoiceConfigRow>(`/api/pipeline/voices/${encodeURIComponent(voiceId)}`, body)
    .then((updated) => {
      // Refresh the module row cache so a subsequent edit pre-fills the new
      // values (getVoiceConfigRow is backed by voiceRowsById).
      voiceRowsById.set(voiceId, updated);
      // Re-render the single card in place from the response — the catalog's
      // delegated click listener lives on #voice-catalog, so replacing the
      // card element keeps Edit/Preview wiring intact. Iterate the cards and
      // compare dataset.voiceId instead of interpolating voiceId into a CSS
      // selector — an operator-created id with selector metacharacters would
      // otherwise throw SyntaxError here (parse-only, no XSS, but a misleading
      // error toast while the PUT succeeded).
      const card = Array.from(document.querySelectorAll<HTMLElement>('.voice-card'))
        .find(el => el.dataset.voiceId === voiceId);
      if (card) card.outerHTML = createVoiceCard(updated);
      closeVoiceEditForm();
      showToast(`Voice config saved: ${updated.name || voiceId}`, 'success');
    })
    .catch((e: unknown) => {
      // Backend validation (e.g. 422 invalid type) surfaces through handleError
      // as a rejected promise — report it and keep the form open.
      const msg = e instanceof Error ? e.message : String(e);
      showToast(`Failed to save voice config: ${msg}`, 'error');
    });
}

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
    registerVoiceCatalog(voices);

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
 * The selected dropdown value is the voice NAME; it is resolved to the
 * voice_config id before persisting, because the backend validates
 * voice_assignment_id against voice_config.id and rejects names with 400
 * (CONTRACTS.md "voice-id"). A name that cannot be resolved produces an
 * error toast and is NOT persisted (null clears the assignment).
 *
 * @param characterId - The character ID
 * @param voiceName - The selected voice name (empty string = unassigned)
 */
export function handleCharacterVoiceChange(characterId: string, voiceName: string): void {
  // Resolve the selected voice NAME to its voice_config id. An unresolvable
  // name (not in the loaded catalog) is reported and left unpersisted — it
  // must not be sent as-is, nor silently clear the assignment.
  let voiceId: string | null = null;
  if (voiceName) {
    voiceId = voiceNameToId.get(voiceName) ?? null;
    if (voiceId === null) {
      showToast(`Voice '${voiceName}' not found in voice catalog`, 'error');
      return;
    }
  }

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

  // Persist to the pipeline character voice endpoint — send the resolved id
  // (null clears the assignment).
  API.put(`/api/pipeline/characters/${characterId}/voice`, {
    voice_assignment_id: voiceId,
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
 * The sample text is optional — when omitted (card Preview buttons) the
 * contract's default sample text is sent, keeping the original behavior.
 * @param voiceId - The voice ID to preview
 * @param button - The button element (for loading state)
 * @param sampleText - Optional sample text; defaults to the contract default
 */
export async function previewVoice(
  voiceId: string,
  button: HTMLButtonElement,
  sampleText?: string,
): Promise<void> {
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
      { sample_text: sampleText ?? DEFAULT_PREVIEW_SAMPLE_TEXT }
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

/**
 * Preview the voice currently open in the edit form using the form's sample
 * text. Reuses the existing previewVoice logic (POST
 * /api/pipeline/voices/{id}/preview + new Audio(...).play() with the button
 * spinner) — the sample_text sent is the form's #voice-edit-ref-text value
 * when non-empty, otherwise the default preview sample text. No-op when no
 * voice is being edited (the form is closed) or when the ref_text field is
 * absent from the DOM.
 * @param button - The preview trigger button in the edit form (loading state)
 */
export function previewVoiceFromForm(button: HTMLButtonElement): void {
  if (!editingVoiceId) return;

  const refTextInput = document.getElementById('voice-edit-ref-text') as HTMLInputElement | null;
  const sampleText = refTextInput?.value.trim();
  previewVoice(
    editingVoiceId,
    button,
    sampleText ? sampleText : DEFAULT_PREVIEW_SAMPLE_TEXT,
  );
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
 *     [data-action="preview-voice"]) and edit buttons ([data-action="edit-voice"])
 *   - Voice config edit form Preview/Save/Cancel (#voice-edit-form, delegated
 *     click on [data-action="preview-voice-form"] / [data-action="save-voice"] /
 *     [data-action="cancel-voice"])
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

    // ----- Voice catalog: preview + edit (delegated) -----
    // Event delegation: [data-action="preview-voice"] synthesizes a preview of
    // its data-voice-id voice; [data-action="edit-voice"] opens the shared
    // edit form pre-filled from that voice's fetched config.
    const voiceCatalog = document.getElementById('voice-catalog');
    if (voiceCatalog) {
      voiceCatalog.addEventListener('click', (e) => {
        const target = e.target as HTMLElement;

        const editButton = target.closest('[data-action="edit-voice"]') as HTMLButtonElement | null;
        if (editButton) {
          const voiceId = editButton.dataset.voiceId;
          if (voiceId) openVoiceEditForm(voiceId);
          return;
        }

        const button = target.closest('[data-action="preview-voice"]') as HTMLButtonElement | null;
        if (button) {
          const voiceId = button.dataset.voiceId;
          if (voiceId) {
            previewVoice(voiceId, button);
          }
        }
      });
    }

    // ----- Voice config edit form: preview + save + cancel (delegated) -----
    // The form is a sibling of #voice-catalog, so it gets its own delegated
    // click listener. Preview synthesizes the edited voice with the form's
    // ref_text as sample text (Phase 4); save invokes the exported
    // saveVoiceConfig (Phase 3 implements the PUT); cancel just closes the form.
    const voiceEditForm = document.getElementById('voice-edit-form');
    if (voiceEditForm) {
      voiceEditForm.addEventListener('click', (e) => {
        const target = e.target as HTMLElement;

        // Preview-from-form: synthesize the currently edited voice with the
        // form's ref_text as sample text (default when empty), reusing the
        // card preview's previewVoice logic and spinner.
        const previewButton = target.closest('[data-action="preview-voice-form"]') as HTMLButtonElement | null;
        if (previewButton) {
          previewVoiceFromForm(previewButton);
          return;
        }

        const cancelButton = target.closest('[data-action="cancel-voice"]');
        if (cancelButton) {
          closeVoiceEditForm();
          return;
        }

        const saveButton = target.closest('[data-action="save-voice"]') as HTMLButtonElement | null;
        if (saveButton && editingVoiceId) {
          saveVoiceConfig(editingVoiceId);
        }
      });
    }

    // Load voices on init
    loadVoices();
  });
}
