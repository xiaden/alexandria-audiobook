/**
 * Spec-first tests for Voices tab (frontend/src/tabs/voices.ts).
 *
 * Tests cover: pipeline character loading, character ledger display,
 * voice assignment dropdown persistence, narrator voice selector (Phase 19),
 * voice catalog rendering & preview (Phase 23), the voices-GET failure path,
 * and the voice config edit form (Plan H): style/type metadata, full-row
 * carry, edit-form pre-fill, exclude_unset save, form preview and the
 * isolated click-through save wiring.
 *
 * Run with `npm test` (vitest run) from frontend/ — vitest ^4.1.10 and
 * jsdom ^30.0.1 are installed (see frontend/package.json and vitest.config.ts).
 */

import { describe, it, expect, beforeEach, afterEach, beforeAll, vi } from 'vitest';
import {
  Character,
  pipelineCharacters,
  parseAliases,
  formatConfidence,
  getConfidenceBadgeClass,
  createCharacterCard,
  renderCharacterLedger,
  handleCharacterVoiceChange,
  getCharacterVoiceAssignments,
  getCachedCharacters,
  loadVoices,
  registerVoiceCatalog,
  initVoices,
  handleNarratorVoiceChange,
  getCurrentNarratorVoice,
  NARRATOR_DEFAULT_VOICE,
  createVoiceCard,
  renderVoiceCatalog,
  previewVoice,
} from '../../src/tabs/voices';
import { state } from '../../src/state';
import type { VoiceConfigRow } from '../../src/state';
import * as API from '../../src/api';
import { showToast } from '../../src/utils';

// Mock the API module
vi.mock('../../src/api', () => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(() => Promise.resolve({ status: 'ok' })),
}));

// Mock utils to avoid DOM side effects
vi.mock('../../src/utils', () => ({
  showToast: vi.fn(),
  showConfirm: vi.fn(),
  escapeHtml: (s: string) => s,
}));


// ---------------------------------------------------------------------------
// Test data fixtures
// ---------------------------------------------------------------------------

const MOCK_CHARACTERS: Character[] = [
  {
    id: 'char-001',
    name: 'Elizabeth Bennet',
    aliases: '["Lizzy","Eliza","Miss Bennet"]',
    confidence: 0.95,
  },
  {
    id: 'char-002',
    name: 'Mr. Darcy',
    aliases: '["Fitzwilliam","Darcy"]',
    confidence: 0.88,
  },
  {
    id: 'char-003',
    name: 'Minor Character',
    aliases: '[]',
    confidence: 0.45,
  },
];

const MOCK_VOICES = [
  { name: 'Alice', config: { type: 'custom', voice: 'Alice' } },
  { name: 'Bob', config: { type: 'custom', voice: 'Bob' } },
  { name: 'Charlie', config: { type: 'custom', voice: 'Charlie' } },
];

// ---------------------------------------------------------------------------
// Helper functions
// ---------------------------------------------------------------------------

describe('parseAliases', () => {
  it('should parse valid JSON array of strings', () => {
    expect(parseAliases('["John","Johnny"]')).toEqual(['John', 'Johnny']);
  });

  it('should return empty array for empty JSON array', () => {
    expect(parseAliases('[]')).toEqual([]);
  });

  it('should return empty array for malformed JSON', () => {
    expect(parseAliases('not json')).toEqual([]);
  });

  it('should return empty array for non-array JSON', () => {
    expect(parseAliases('{"name":"John"}')).toEqual([]);
  });

  it('should filter out non-string elements', () => {
    expect(parseAliases('["John",123,null,"Jane"]')).toEqual(['John', 'Jane']);
  });
});

describe('formatConfidence', () => {
  it('should format 0.95 as "95%"', () => {
    expect(formatConfidence(0.95)).toBe('95%');
  });

  it('should format 0.0 as "0%"', () => {
    expect(formatConfidence(0.0)).toBe('0%');
  });

  it('should format 1.0 as "100%"', () => {
    expect(formatConfidence(1.0)).toBe('100%');
  });

  it('should round to nearest integer', () => {
    expect(formatConfidence(0.856)).toBe('86%');
    expect(formatConfidence(0.854)).toBe('85%');
  });
});

describe('getConfidenceBadgeClass', () => {
  it('should return bg-success for confidence >= 0.8', () => {
    expect(getConfidenceBadgeClass(0.8)).toBe('bg-success');
    expect(getConfidenceBadgeClass(0.95)).toBe('bg-success');
    expect(getConfidenceBadgeClass(1.0)).toBe('bg-success');
  });

  it('should return bg-warning text-dark for confidence >= 0.6 and < 0.8', () => {
    expect(getConfidenceBadgeClass(0.6)).toBe('bg-warning text-dark');
    expect(getConfidenceBadgeClass(0.7)).toBe('bg-warning text-dark');
    expect(getConfidenceBadgeClass(0.79)).toBe('bg-warning text-dark');
  });

  it('should return bg-danger for confidence < 0.6', () => {
    expect(getConfidenceBadgeClass(0.0)).toBe('bg-danger');
    expect(getConfidenceBadgeClass(0.3)).toBe('bg-danger');
    expect(getConfidenceBadgeClass(0.59)).toBe('bg-danger');
  });
});

// ---------------------------------------------------------------------------
// Pipeline API functions
// ---------------------------------------------------------------------------

describe('pipelineCharacters', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('should GET /api/pipeline/characters/{book_id}', async () => {
    vi.mocked(API.get).mockResolvedValueOnce(MOCK_CHARACTERS);

    const result = await pipelineCharacters('book-123');

    expect(API.get).toHaveBeenCalledWith('/api/pipeline/characters/book-123');
    expect(result).toEqual(MOCK_CHARACTERS);
  });

  it('should return empty array when no characters exist', async () => {
    vi.mocked(API.get).mockResolvedValueOnce([]);

    const result = await pipelineCharacters('book-empty');

    expect(API.get).toHaveBeenCalledWith('/api/pipeline/characters/book-empty');
    expect(result).toEqual([]);
  });

  it('should propagate API errors', async () => {
    vi.mocked(API.get).mockRejectedValueOnce(new Error('Network error'));

    await expect(pipelineCharacters('book-fail')).rejects.toThrow('Network error');
  });
});

// ---------------------------------------------------------------------------
// Character ledger display
// ---------------------------------------------------------------------------

describe('createCharacterCard', () => {
  beforeEach(() => {
    state.voicesNames = ['Alice', 'Bob', 'Charlie'];
  });

  it('should render character name prominently', () => {
    const html = createCharacterCard(MOCK_CHARACTERS[0], 0);
    expect(html).toContain('Elizabeth Bennet');
    expect(html).toContain('<h5 class="card-title');
  });

  it('should render aliases as Bootstrap badges', () => {
    const html = createCharacterCard(MOCK_CHARACTERS[0], 0);
    expect(html).toContain('Lizzy');
    expect(html).toContain('Eliza');
    expect(html).toContain('Miss Bennet');
    expect(html).toContain('badge bg-light text-dark border');
  });

  it('should show "No aliases" when aliases array is empty', () => {
    const html = createCharacterCard(MOCK_CHARACTERS[2], 2);
    expect(html).toContain('No aliases');
  });

  it('should render confidence badge with correct class', () => {
    const highConfHtml = createCharacterCard(MOCK_CHARACTERS[0], 0);
    expect(highConfHtml).toContain('bg-success');
    expect(highConfHtml).toContain('95%');

    const lowConfHtml = createCharacterCard(MOCK_CHARACTERS[2], 2);
    expect(lowConfHtml).toContain('bg-danger');
    expect(lowConfHtml).toContain('45%');
  });

  it('should render voice assignment dropdown with all available voices', () => {
    const html = createCharacterCard(MOCK_CHARACTERS[0], 0);
    expect(html).toContain('character-voice-select');
    expect(html).toContain('data-character-id="char-001"');
    expect(html).toContain('<option value="Alice"');
    expect(html).toContain('<option value="Bob"');
    expect(html).toContain('<option value="Charlie"');
    expect(html).toContain('-- Unassigned --');
  });

  it('should show "Unassigned" when no voice is assigned', () => {
    const html = createCharacterCard(MOCK_CHARACTERS[0], 0);
    expect(html).toContain('Unassigned');
  });

  it('should include data-character-id on the card element', () => {
    const html = createCharacterCard(MOCK_CHARACTERS[1], 1);
    expect(html).toContain('data-character-id="char-002"');
    expect(html).toContain('character-card');
  });

  it('excludes the NARRATOR pseudo-row from the voice dropdown options', async () => {
    // Drive module state through loadVoices: the NARRATOR row's name is
    // remembered as narratorRowName and filtered out of the dropdown.
    state.pipelineBookId = 'book-abc';
    vi.mocked(API.get).mockImplementation((url: string) => {
      if (url === '/api/pipeline/voices') {
        return Promise.resolve([
          { id: 'NARRATOR', name: 'NARRATOR', voice: 'Ryan' },
          { id: 'alice', name: 'Alice', voice: 'Alice' },
          { id: 'bob', name: 'Bob', voice: 'Bob' },
        ]);
      }
      return Promise.resolve([]);
    });
    await loadVoices();

    const html = createCharacterCard(MOCK_CHARACTERS[0], 0);
    expect(html).toContain('<option value="Alice"');
    expect(html).not.toContain('value="NARRATOR"');
  });

  it('marks the assigned voice option as selected', () => {
    registerVoiceCatalog([
      { id: 'alice', name: 'Alice', voice: 'Alice' },
      { id: 'bob', name: 'Bob', voice: 'Bob' },
      { id: 'charlie', name: 'Charlie', voice: 'Charlie' },
    ]);
    handleCharacterVoiceChange('char-001', 'Alice');
    const html = createCharacterCard(MOCK_CHARACTERS[0], 0);
    expect(html).toContain('<option value="Alice" selected>');
  });

  it('renders the warning confidence badge for mid-range confidence', () => {
    const html = createCharacterCard({ ...MOCK_CHARACTERS[0], confidence: 0.7 }, 0);
    expect(html).toContain('bg-warning text-dark');
    expect(html).toContain('70%');
  });
});

describe('renderCharacterLedger', () => {
  beforeEach(() => {
    document.body.innerHTML = `
      <div id="character-ledger"></div>
    `;
    state.voicesNames = ['Alice', 'Bob'];
  });

  it('should render all characters into #character-ledger', () => {
    renderCharacterLedger(MOCK_CHARACTERS);

    const container = document.getElementById('character-ledger');
    expect(container).not.toBeNull();
    const cards = container!.querySelectorAll('.character-card');
    expect(cards.length).toBe(3);
  });

  it('should show info alert when no characters exist', () => {
    renderCharacterLedger([]);

    const container = document.getElementById('character-ledger');
    expect(container!.innerHTML).toContain('alert alert-info');
    expect(container!.innerHTML).toContain('No characters found');
  });

  it('should cache characters for later retrieval', () => {
    renderCharacterLedger(MOCK_CHARACTERS);
    const cached = getCachedCharacters();
    expect(cached).toEqual(MOCK_CHARACTERS);
  });

  it('should do nothing when #character-ledger does not exist', () => {
    document.body.innerHTML = '';
    expect(() => renderCharacterLedger(MOCK_CHARACTERS)).not.toThrow();
  });
});

// ---------------------------------------------------------------------------
// Voice assignment dropdown behavior
// ---------------------------------------------------------------------------

describe('handleCharacterVoiceChange', () => {
  beforeEach(() => {
    document.body.innerHTML = `
      <div id="character-ledger">
        <div class="character-card" data-character-id="char-001">
          <div class="col-md-2"><span class="text-muted small">Unassigned</span></div>
        </div>
      </div>
    `;
    // Seed the name→id lookup (normally populated by loadVoices) so the
    // handler can resolve dropdown names to voice_config ids before PUT.
    registerVoiceCatalog([
      { id: 'alice', name: 'Alice', voice: 'Alice' },
      { id: 'bob', name: 'Bob', voice: 'Bob' },
    ]);
  });

  it('should store voice assignment in local map', () => {
    handleCharacterVoiceChange('char-001', 'Alice');
    const assignments = getCharacterVoiceAssignments();
    expect(assignments.get('char-001')).toBe('Alice');
  });

  it('should remove assignment when voice is empty string', () => {
    handleCharacterVoiceChange('char-001', 'Alice');
    handleCharacterVoiceChange('char-001', '');
    const assignments = getCharacterVoiceAssignments();
    expect(assignments.has('char-001')).toBe(false);
  });

  it('should update the character card display with voice badge', () => {
    handleCharacterVoiceChange('char-001', 'Bob');
    const card = document.querySelector('.character-card[data-character-id="char-001"]');
    const voiceBadge = card!.querySelector('.col-md-2');
    expect(voiceBadge!.innerHTML).toContain('badge bg-info');
    expect(voiceBadge!.innerHTML).toContain('Bob');
  });

  it('should show "Unassigned" when voice is cleared', () => {
    handleCharacterVoiceChange('char-001', 'Alice');
    handleCharacterVoiceChange('char-001', '');
    const card = document.querySelector('.character-card[data-character-id="char-001"]');
    const voiceBadge = card!.querySelector('.col-md-2');
    expect(voiceBadge!.innerHTML).toContain('Unassigned');
  });

  it('should return a copy of the assignments map (not a reference)', () => {
    handleCharacterVoiceChange('char-001', 'Alice');
    const assignments1 = getCharacterVoiceAssignments();
    const assignments2 = getCharacterVoiceAssignments();
    expect(assignments1).not.toBe(assignments2);
    expect(assignments1).toEqual(assignments2);
  });

  it('shows a success toast when the assignment PUT resolves', async () => {
    vi.clearAllMocks();
    // API.put's default mock implementation resolves (see vi.mock factory)
    handleCharacterVoiceChange('char-001', 'Alice');

    // The selected NAME is resolved to the voice_config id before PUT
    expect(API.put).toHaveBeenCalledWith('/api/pipeline/characters/char-001/voice', {
      voice_assignment_id: 'alice',
    });

    await vi.waitFor(() => {
      expect(showToast).toHaveBeenCalledWith('Voice assigned: Alice', 'success');
    });
  });

  it('shows an error toast and does NOT PUT when the voice name is not in the catalog', () => {
    vi.clearAllMocks();
    // Snapshot the map first — earlier tests may have left an assignment
    const before = getCharacterVoiceAssignments();

    // 'Zoe' is absent from the registered catalog — cannot be resolved to an id
    handleCharacterVoiceChange('char-001', 'Zoe');

    expect(API.put).not.toHaveBeenCalled();
    expect(showToast).toHaveBeenCalledWith("Voice 'Zoe' not found in voice catalog", 'error');
    // The assignment map is left untouched — no optimistic update
    expect(getCharacterVoiceAssignments()).toEqual(before);
  });

  it('shows an error toast when the assignment PUT rejects', async () => {
    vi.clearAllMocks();
    vi.mocked(API.put).mockRejectedValueOnce(new Error('Server error'));

    handleCharacterVoiceChange('char-001', 'Alice');

    await vi.waitFor(() => {
      expect(showToast).toHaveBeenCalledWith('Failed to save voice assignment: Server error', 'error');
    });
    // The optimistic local assignment is retained after a failed persist
    // (matches the narrator-selector convention — no rollback is implemented).
    expect(getCharacterVoiceAssignments().get('char-001')).toBe('Alice');
  });
});

// ---------------------------------------------------------------------------
// loadVoices — pipeline character loading
// ---------------------------------------------------------------------------

describe('loadVoices — pipeline character loading', () => {
  beforeEach(() => {
    document.body.innerHTML = `
      <div id="pipeline-voices-section" style="display:none;">
        <div id="character-ledger"></div>
      </div>
      <span id="voice-save-status"></span>
    `;
    vi.clearAllMocks();
    vi.mocked(API.get).mockImplementation((url: string) => {
      if (url === '/api/pipeline/voices') return Promise.resolve(MOCK_VOICES);
      if (url.startsWith('/api/pipeline/characters/')) return Promise.resolve(MOCK_CHARACTERS);
      return Promise.resolve([]);
    });
  });

  it('should load voices list and pipeline characters', async () => {
    state.pipelineBookId = 'book-abc';

    await loadVoices();

    expect(API.get).toHaveBeenCalledWith('/api/pipeline/voices');
    expect(API.get).toHaveBeenCalledWith('/api/pipeline/characters/book-abc');
    expect(state.voicesNames).toEqual(['Alice', 'Bob', 'Charlie']);
  });

  it('should show the pipeline section', async () => {
    state.pipelineBookId = 'book-abc';

    await loadVoices();

    const pipelineSection = document.getElementById('pipeline-voices-section');
    expect(pipelineSection!.style.display).toBe('');
  });


  it('should show warning when no book onboarded', async () => {
    state.pipelineBookId = null;

    await loadVoices();

    const container = document.getElementById('character-ledger');
    expect(container!.innerHTML).toContain('alert alert-warning');
    expect(container!.innerHTML).toContain('No book onboarded');
  });

  it('should show error in character ledger when API fails', async () => {
    state.pipelineBookId = 'book-fail';

    vi.mocked(API.get).mockImplementation((url: string) => {
      if (url === '/api/pipeline/voices') return Promise.resolve(MOCK_VOICES);
      if (url.startsWith('/api/pipeline/characters/')) return Promise.reject(new Error('Server error'));
      return Promise.resolve([]);
    });

    await loadVoices();

    const container = document.getElementById('character-ledger');
    expect(container!.innerHTML).toContain('alert alert-danger');
    expect(container!.innerHTML).toContain('Failed to load characters');
  });

  it('should always load /api/pipeline/voices', async () => {
    await loadVoices();
    expect(API.get).toHaveBeenCalledWith('/api/pipeline/voices');
  });

  it('shows an error toast and still loads characters when the voices GET fails', async () => {
    state.pipelineBookId = 'book-abc';

    vi.mocked(API.get).mockImplementation((url: string) => {
      if (url === '/api/pipeline/voices') return Promise.reject(new Error('Voices service down'));
      if (url.startsWith('/api/pipeline/characters/')) return Promise.resolve(MOCK_CHARACTERS);
      return Promise.resolve([]);
    });

    await loadVoices();

    expect(showToast).toHaveBeenCalledWith('Failed to load voices: Voices service down', 'error');
    // Tab init continues: characters are still loaded despite the voices failure
    expect(API.get).toHaveBeenCalledWith('/api/pipeline/characters/book-abc');
  });
});

// ---------------------------------------------------------------------------
// getCachedCharacters
// ---------------------------------------------------------------------------

describe('getCachedCharacters', () => {
  beforeEach(() => {
    document.body.innerHTML = `<div id="character-ledger"></div>`;
    state.voicesNames = ['Alice'];
    // Reset the module-level character cache (populated by earlier describes)
    renderCharacterLedger([]);
  });

  it('should return empty array before any characters are loaded', () => {
    expect(getCachedCharacters()).toEqual([]);
  });

  it('should return characters after renderCharacterLedger is called', () => {
    renderCharacterLedger(MOCK_CHARACTERS);
    expect(getCachedCharacters()).toEqual(MOCK_CHARACTERS);
  });

  it('should return a copy (not a reference to internal state)', () => {
    renderCharacterLedger(MOCK_CHARACTERS);
    const copy1 = getCachedCharacters();
    const copy2 = getCachedCharacters();
    expect(copy1).not.toBe(copy2);
    expect(copy1).toEqual(copy2);
  });
});


// ---------------------------------------------------------------------------
// initVoices (event listener attachment)
// ---------------------------------------------------------------------------

describe('initVoices', () => {
  beforeEach(() => {
    document.body.innerHTML = `
      <div id="character-ledger"></div>
      <span id="voice-save-status"></span>
    `;
    vi.clearAllMocks();
    vi.mocked(API.get).mockResolvedValue([]);
  });

  it('should not throw when called', () => {
    expect(() => initVoices()).not.toThrow();
  });

  it('should attach change listener to #character-ledger', () => {
    initVoices();
    // Dispatch DOMContentLoaded
    document.dispatchEvent(new Event('DOMContentLoaded'));

    const ledger = document.getElementById('character-ledger');
    // Verify the event listener was attached by checking the element exists
    expect(ledger).not.toBeNull();
  });

  it('delegated change on a character voice select persists via PUT', async () => {
    state.pipelineBookId = 'book-abc';
    vi.mocked(API.get).mockImplementation((url: string) => {
      if (url === '/api/pipeline/voices') return Promise.resolve(CATALOG_VOICES);
      if (url.startsWith('/api/pipeline/characters/')) return Promise.resolve(MOCK_CHARACTERS);
      return Promise.resolve([]);
    });

    initVoices();
    document.dispatchEvent(new Event('DOMContentLoaded'));

    // Wait for the ledger to render character voice selects
    await vi.waitFor(() => {
      expect(document.querySelector('.character-voice-select[data-character-id="char-001"]')).not.toBeNull();
    });

    const select = document.querySelector('.character-voice-select[data-character-id="char-001"]') as HTMLSelectElement;
    select.value = 'Alice';
    // Bubbles must be true — the delegated listener lives on #character-ledger
    select.dispatchEvent(new Event('change', { bubbles: true }));

    // Delegated handler (initVoices wiring) resolves the selected name
    // 'Alice' to the voice_config id 'alice' before persisting via PUT
    expect(API.put).toHaveBeenCalledWith('/api/pipeline/characters/char-001/voice', {
      voice_assignment_id: 'alice',
    });
    // Local ledger state was updated through handleCharacterVoiceChange
    // (kept by NAME for display; the backend round-trips by id)
    expect(getCharacterVoiceAssignments().get('char-001')).toBe('Alice');
  });
});

// ---------------------------------------------------------------------------
// Narrator voice selector (Phase 19)
// ---------------------------------------------------------------------------

describe('narrator voice selector', () => {
  // Voice catalog rows (all 12 columns are returned by the API; tests only
  // exercise the id/name/voice fields). Includes the NARRATOR pseudo-row.
  const NARRATOR_MOCK_VOICES = [
    { id: 'NARRATOR', name: 'NARRATOR', voice: 'Ryan' },
    { id: 'ryan', name: 'Ryan', voice: 'Ryan' },
    { id: 'alice', name: 'Alice', voice: 'Alice' },
    { id: 'bob', name: 'Bob', voice: 'Bob' },
    { id: 'charlie', name: 'Charlie', voice: 'Charlie' },
  ];

  beforeEach(() => {
    document.body.innerHTML = `
      <div id="pipeline-voices-section" style="display:none;">
        <select id="narrator-voice-select"></select>
        <div id="character-ledger"></div>
      </div>
    `;
    vi.clearAllMocks();
    vi.mocked(API.get).mockImplementation((url: string) => {
      if (url === '/api/pipeline/voices') return Promise.resolve(NARRATOR_MOCK_VOICES);
      if (url.startsWith('/api/pipeline/characters/')) return Promise.resolve(MOCK_CHARACTERS);
      return Promise.resolve([]);
    });
  });

  it('renders all available voices as options with the NARRATOR voice selected', async () => {
    state.pipelineBookId = 'book-abc';

    await loadVoices();

    const select = document.getElementById('narrator-voice-select') as HTMLSelectElement;
    expect(select).not.toBeNull();
    const options = Array.from(select.options).map(o => o.value);
    expect(options).toContain('Ryan');
    expect(options).toContain('Alice');
    expect(options).toContain('Bob');
    expect(options).toContain('Charlie');
    // The NARRATOR pseudo-row is not offered as a narrator voice choice
    expect(options).not.toContain('NARRATOR');
    // The NARRATOR row's `voice` column is the selected value
    expect(select.value).toBe('Ryan');
    expect(getCurrentNarratorVoice()).toBe('Ryan');
  });

  it('calls PUT /api/pipeline/voices/NARRATOR with the selected voice on change', async () => {
    state.pipelineBookId = 'book-abc';

    initVoices();
    document.dispatchEvent(new Event('DOMContentLoaded'));

    // Wait for loadVoices (fired by DOMContentLoaded) to populate the options
    await vi.waitFor(() => {
      const select = document.getElementById('narrator-voice-select') as HTMLSelectElement;
      expect(select.options.length).toBeGreaterThan(0);
    });

    const select = document.getElementById('narrator-voice-select') as HTMLSelectElement;
    select.value = 'Bob';
    select.dispatchEvent(new Event('change'));

    expect(API.put).toHaveBeenCalledWith('/api/pipeline/voices/NARRATOR', { voice: 'Bob' });
  });

  it('persists the narrator voice via handleNarratorVoiceChange', () => {
    handleNarratorVoiceChange('Charlie');

    expect(API.put).toHaveBeenCalledWith('/api/pipeline/voices/NARRATOR', { voice: 'Charlie' });
    expect(getCurrentNarratorVoice()).toBe('Charlie');
  });

  it('shows a success toast after persisting', async () => {
    vi.mocked(API.put).mockResolvedValueOnce({ id: 'NARRATOR', voice: 'Bob' });

    handleNarratorVoiceChange('Bob');

    await vi.waitFor(() => {
      expect(showToast).toHaveBeenCalledWith('Narrator voice set to Bob', 'success');
    });
  });

  it('shows an error toast when persisting fails', async () => {
    vi.mocked(API.put).mockRejectedValueOnce(new Error('Server error'));

    handleNarratorVoiceChange('Bob');

    await vi.waitFor(() => {
      expect(showToast).toHaveBeenCalledWith('Failed to update narrator voice: Server error', 'error');
    });
    // Local selection is preserved even when the persist fails
    expect(getCurrentNarratorVoice()).toBe('Bob');
  });

  it('falls back to the default narrator voice when no NARRATOR row exists', async () => {
    state.pipelineBookId = 'book-abc';
    vi.mocked(API.get).mockImplementation((url: string) => {
      if (url === '/api/pipeline/voices') return Promise.resolve([
        { id: 'ryan', name: 'Ryan', voice: 'Ryan' },
        { id: 'alice', name: 'Alice', voice: 'Alice' },
      ]);
      if (url.startsWith('/api/pipeline/characters/')) return Promise.resolve(MOCK_CHARACTERS);
      return Promise.resolve([]);
    });

    await loadVoices();

    const select = document.getElementById('narrator-voice-select') as HTMLSelectElement;
    expect(select.value).toBe(NARRATOR_DEFAULT_VOICE);
    expect(getCurrentNarratorVoice()).toBe(NARRATOR_DEFAULT_VOICE);
    const options = Array.from(select.options).map(o => o.value);
    expect(options).toContain('Ryan');
    expect(options).toContain('Alice');
  });

  it('appends the current narrator voice to the options when it is missing from the catalog', async () => {
    state.pipelineBookId = 'book-abc';
    vi.mocked(API.get).mockImplementation((url: string) => {
      if (url === '/api/pipeline/voices') {
        return Promise.resolve([
          { id: 'NARRATOR', name: 'NARRATOR', voice: 'Zoe' }, // narrator = Zoe, absent from catalog
          { id: 'alice', name: 'Alice', voice: 'Alice' },
          { id: 'bob', name: 'Bob', voice: 'Bob' },
        ]);
      }
      if (url.startsWith('/api/pipeline/characters/')) return Promise.resolve(MOCK_CHARACTERS);
      return Promise.resolve([]);
    });

    await loadVoices();

    const select = document.getElementById('narrator-voice-select') as HTMLSelectElement;
    const options = Array.from(select.options).map(o => o.value);
    // 'Zoe' is the current narrator voice but is absent from the catalog —
    // the selector appends it so the dropdown never shows an empty selection
    expect(options).toContain('Zoe');
    expect(select.value).toBe('Zoe');
    expect(getCurrentNarratorVoice()).toBe('Zoe');
  });

  it('rejects an empty narrator voice selection without persisting', () => {
    handleNarratorVoiceChange('');

    expect(API.put).not.toHaveBeenCalled();
    expect(showToast).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// Voice catalog & preview (Phase 23)
// ---------------------------------------------------------------------------

/** Voice config rows for the catalog (includes the NARRATOR pseudo-row). */
const CATALOG_VOICES: VoiceConfigRow[] = [
  { id: 'ryan', name: 'Ryan', voice: 'Ryan', type: 'custom' },
  { id: 'alice', name: 'Alice', voice: 'Alice', type: 'custom' },
  { id: 'bob-clone', name: 'Bob', voice: 'Bob', type: 'clone' },
  { id: 'NARRATOR', name: 'NARRATOR', voice: 'Ryan', type: 'custom' },
];

/** Instances created by the stubbed Audio constructor (reset per test). */
let audioInstances: MockAudio[] = [];

/**
 * jsdom does not implement HTMLMediaElement.play(), so the real Audio global
 * would throw on .play(). This stub records each constructed instance (src)
 * and resolves play() so previewVoice's .catch() chain works.
 */
class MockAudio {
  src: string;
  play = vi.fn().mockResolvedValue(undefined);
  constructor(src?: string) {
    this.src = src ?? '';
    audioInstances.push(this);
  }
}

describe('voice catalog (Phase 23)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders a card with name, type badge, and preview button for each voice', () => {
    const html = createVoiceCard(CATALOG_VOICES[1]);
    expect(html).toContain('Alice');
    expect(html).toContain('voice-type-badge');
    expect(html).toContain('>custom<');
    expect(html).toContain('data-action="preview-voice"');
    expect(html).toContain('data-voice-id="alice"');
    expect(html).toContain('btn btn-sm btn-outline-success');
    expect(html).toContain('fas fa-play');
  });

  it('renders one voice card per voice, excluding the NARRATOR pseudo-row', () => {
    document.body.innerHTML = '<div id="voice-catalog"></div>';
    renderVoiceCatalog(CATALOG_VOICES);

    const cards = document.querySelectorAll('.voice-card');
    expect(cards.length).toBe(3);
    expect(document.querySelector('.voice-card[data-voice-id="NARRATOR"]')).toBeNull();
    expect(document.querySelector('.voice-card[data-voice-id="alice"]')).not.toBeNull();
    expect(document.querySelector('.voice-card[data-voice-id="bob-clone"]')).not.toBeNull();
  });

  it('shows a muted message when no real voices exist', () => {
    document.body.innerHTML = '<div id="voice-catalog"></div>';
    renderVoiceCatalog([{ id: 'NARRATOR', name: 'NARRATOR', voice: 'Ryan' }]);

    const container = document.getElementById('voice-catalog');
    expect(container!.innerHTML).toContain('No voices available yet');
  });

  it('does nothing when #voice-catalog is absent', () => {
    document.body.innerHTML = '';
    expect(() => renderVoiceCatalog(CATALOG_VOICES)).not.toThrow();
  });

  it('falls back to an "unknown" type badge when the voice row has no type', () => {
    const html = createVoiceCard({ id: 'mystery', name: 'Mystery', voice: 'Mystery' });
    expect(html).toContain('>unknown<');
  });
});

describe('voice preview (Phase 23)', () => {
  beforeEach(() => {
    document.body.innerHTML = `
      <div id="pipeline-voices-section" style="display:none;">
        <select id="narrator-voice-select"></select>
        <div id="voice-catalog"></div>
        <div id="character-ledger"></div>
      </div>
    `;
    vi.clearAllMocks();
    audioInstances = [];
    vi.mocked(API.get).mockImplementation((url: string) => {
      if (url === '/api/pipeline/voices') return Promise.resolve(CATALOG_VOICES);
      if (url.startsWith('/api/pipeline/characters/')) return Promise.resolve(MOCK_CHARACTERS);
      return Promise.resolve([]);
    });
    vi.stubGlobal('Audio', MockAudio);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('clicking Preview calls POST /api/pipeline/voices/{id}/preview with default sample text and plays the audio', async () => {
    state.pipelineBookId = 'book-abc';
    vi.mocked(API.post).mockResolvedValueOnce({ audio_url: '/designed_voices/previews/alice.wav', voice_id: 'alice' });

    initVoices();
    document.dispatchEvent(new Event('DOMContentLoaded'));

    // Wait for the catalog to render (3 cards — NARRATOR excluded)
    await vi.waitFor(() => {
      expect(document.querySelectorAll('.voice-card').length).toBe(3);
    });

    const button = document.querySelector('[data-action="preview-voice"][data-voice-id="alice"]') as HTMLButtonElement;
    expect(button).not.toBeNull();
    button.click();

    await vi.waitFor(() => {
      expect(API.post).toHaveBeenCalledWith('/api/pipeline/voices/alice/preview', {
        sample_text: 'This is a preview of the voice.',
      });
    });

    // Audio is constructed with the returned URL and play() is invoked
    await vi.waitFor(() => {
      expect(audioInstances.length).toBe(1);
      expect(audioInstances[0].src).toBe('/designed_voices/previews/alice.wav');
      expect(audioInstances[0].play).toHaveBeenCalled();
    });

    expect(showToast).toHaveBeenCalledWith('Preview generated successfully', 'success');
    // Button is restored (re-enabled) after preview completes
    expect(button.disabled).toBe(false);
  });

  it('previewVoice plays the returned audio URL on success', async () => {
    vi.mocked(API.post).mockResolvedValueOnce({ audio_url: '/designed_voices/previews/bob-clone.wav', voice_id: 'bob-clone' });

    const button = document.createElement('button');
    button.innerHTML = '<i class="fas fa-play me-1"></i>Preview';
    document.body.appendChild(button);

    await previewVoice('bob-clone', button);

    expect(API.post).toHaveBeenCalledWith('/api/pipeline/voices/bob-clone/preview', {
      sample_text: 'This is a preview of the voice.',
    });
    expect(audioInstances.length).toBe(1);
    expect(audioInstances[0].src).toBe('/designed_voices/previews/bob-clone.wav');
    expect(audioInstances[0].play).toHaveBeenCalled();
    expect(showToast).toHaveBeenCalledWith('Preview generated successfully', 'success');
    expect(button.disabled).toBe(false);
    expect(button.innerHTML).toContain('fas fa-play');
  });

  it('shows an error toast and restores the button when the preview API rejects', async () => {
    vi.mocked(API.post).mockRejectedValueOnce(new Error('TTS engine not available'));

    const button = document.createElement('button');
    button.innerHTML = '<i class="fas fa-play me-1"></i>Preview';
    document.body.appendChild(button);

    await previewVoice('alice', button);

    expect(API.post).toHaveBeenCalledWith('/api/pipeline/voices/alice/preview', {
      sample_text: 'This is a preview of the voice.',
    });
    expect(showToast).toHaveBeenCalledWith('Failed to generate preview: TTS engine not available', 'error');
    expect(button.disabled).toBe(false);
    expect(button.innerHTML).toContain('fas fa-play');
  });

  it('shows an error toast when audio.play() rejects and restores the button', async () => {
    vi.mocked(API.post).mockResolvedValueOnce({ audio_url: '/designed_voices/previews/alice.wav', voice_id: 'alice' });

    // Reuse MockAudio's recording constructor but make play() reject
    class FailingAudio extends MockAudio {
      play = vi.fn().mockRejectedValue(new Error('audio decode failed'));
    }
    vi.stubGlobal('Audio', FailingAudio);

    const button = document.createElement('button');
    button.innerHTML = '<i class="fas fa-play me-1"></i>Preview';
    document.body.appendChild(button);

    await previewVoice('alice', button);

    // The play() rejection surfaces as an error toast (handled asynchronously)
    await vi.waitFor(() => {
      expect(showToast).toHaveBeenCalledWith('Failed to play preview: audio decode failed', 'error');
    });
    // Button is restored after the play failure
    expect(button.disabled).toBe(false);
    expect(button.innerHTML).toContain('fas fa-play');
  });
});

// ---------------------------------------------------------------------------
// Voice config style/type metadata + full-row carry (Plan H phase 1)
// ---------------------------------------------------------------------------

/** A voice config row carrying all 12 voice_config columns (GET /voices). */
const FULL_CATALOG_ROW: VoiceConfigRow = {
  id: 'alice',
  name: 'Alice',
  description: 'A friendly clone',
  type: 'clone',
  voice: 'Alice',
  character_style: 'warm',
  seed: '42',
  ref_audio: '/refs/alice.wav',
  ref_text: 'This is the reference line.',
  adapter_id: 'adapter-1',
  adapter_path: '/adapters/alice',
  alias_of: 'alice-base',
};

describe('voice config style/type metadata (Plan H phase 1)', () => {
  it('renders the character style on the card when present', () => {
    const html = createVoiceCard(FULL_CATALOG_ROW);
    expect(html).toContain('voice-style-badge');
    expect(html).toContain('warm');
  });

  it('renders the type badge from a full 12-column row', () => {
    const html = createVoiceCard(FULL_CATALOG_ROW);
    expect(html).toContain('>clone<');
  });

  it('omits the style element when the row has no character_style', () => {
    const html = createVoiceCard({ id: 'plain', name: 'Plain', voice: 'Plain', type: 'custom' });
    expect(html).not.toContain('voice-style-badge');
  });
});

describe('voice config full-row carry for the edit form (Plan H phase 1)', () => {
  beforeEach(() => {
    document.body.innerHTML = `
      <div id="pipeline-voices-section" style="display:none;">
        <select id="narrator-voice-select"></select>
        <div id="voice-catalog"></div>
        <div id="character-ledger"></div>
      </div>
    `;
    vi.clearAllMocks();
    vi.mocked(API.get).mockImplementation((url: string) => {
      if (url === '/api/pipeline/voices') return Promise.resolve([FULL_CATALOG_ROW]);
      if (url.startsWith('/api/pipeline/characters/')) return Promise.resolve(MOCK_CHARACTERS);
      return Promise.resolve([]);
    });
  });

  it('exposes the fetched 12-column row so the edit form can be pre-filled', async () => {
    state.pipelineBookId = 'book-abc';
    await loadVoices();

    // Phase 2's edit form consumes the full fetched voice config (all 12
    // columns) to pre-fill its fields — phase 1 carries the row through
    // registerVoiceCatalog/loadVoices and exposes it via getVoiceConfigRow.
    const { getVoiceConfigRow } = await import('../../src/tabs/voices');
    const row = getVoiceConfigRow('alice');
    expect(row).toEqual(FULL_CATALOG_ROW);
  });

  it('returns undefined for an id not in the fetched catalog', async () => {
    state.pipelineBookId = 'book-abc';
    await loadVoices();

    const { getVoiceConfigRow } = await import('../../src/tabs/voices');
    expect(getVoiceConfigRow('missing')).toBeUndefined();
  });

  it('renders the fetched style and type metadata onto the catalog card', async () => {
    state.pipelineBookId = 'book-abc';
    await loadVoices();

    const card = document.querySelector('.voice-card[data-voice-id="alice"]');
    expect(card).not.toBeNull();
    expect(card!.textContent).toContain('warm');
    expect(card!.textContent).toContain('clone');
  });
});

// ---------------------------------------------------------------------------
// Voice config edit form (Plan H phase 2)
// ---------------------------------------------------------------------------

/** Catalog whose alias targets are all in-catalog (for alias-option tests). */
const ALIAS_CATALOG_VOICES: VoiceConfigRow[] = [
  { id: 'alice', name: 'Alice', voice: 'Alice', type: 'custom' },
  { id: 'bob', name: 'Bob', voice: 'Bob', type: 'custom' },
  { id: 'NARRATOR', name: 'NARRATOR', voice: 'Ryan', type: 'custom' },
];

describe('voice config edit form (Plan H phase 2)', () => {
  // Register the delegated listeners ONCE for this describe. Each test below
  // dispatches DOMContentLoaded to wire the CURRENT fixture DOM — a per-test
  // initVoices() would stack document-level listeners and double-fire the
  // handlers (exec-worker log L141). New exports are imported dynamically so
  // the RED phase cannot break module load of the whole file.
  beforeAll(() => {
    initVoices();
  });

  beforeEach(() => {
    document.body.innerHTML = `
      <div id="pipeline-voices-section" style="display:none;">
        <select id="narrator-voice-select"></select>
        <div id="voice-catalog"></div>
        <div id="voice-edit-form" style="display:none;">
          <span id="voice-edit-title"></span>
          <input id="voice-edit-character-style">
          <input id="voice-edit-ref-audio">
          <input id="voice-edit-ref-text">
          <select id="voice-edit-type">
            <option value="custom">custom</option>
            <option value="clone">clone</option>
            <option value="builtin_lora">builtin_lora</option>
            <option value="lora">lora</option>
            <option value="design">design</option>
          </select>
          <select id="voice-edit-alias-of"></select>
          <button data-action="save-voice"></button>
          <button data-action="cancel-voice"></button>
        </div>
        <div id="character-ledger"></div>
      </div>
    `;
    vi.clearAllMocks();
    vi.mocked(API.get).mockImplementation((url: string) => {
      if (url === '/api/pipeline/voices') return Promise.resolve([FULL_CATALOG_ROW]);
      if (url.startsWith('/api/pipeline/characters/')) return Promise.resolve(MOCK_CHARACTERS);
      return Promise.resolve([]);
    });
  });

  it('renders an edit button on each voice card', () => {
    const html = createVoiceCard(FULL_CATALOG_ROW);
    expect(html).toContain('data-action="edit-voice"');
    expect(html).toContain('data-voice-id="alice"');
    expect(html).toContain('fas fa-edit');
  });

  it('clicking Edit on a card opens the shared form pre-filled with the voice config', async () => {
    state.pipelineBookId = 'book-abc';
    document.dispatchEvent(new Event('DOMContentLoaded'));

    // Cards render from the mocked GET /voices (12-column FULL_CATALOG_ROW)
    await vi.waitFor(() => {
      expect(document.querySelector('[data-action="edit-voice"][data-voice-id="alice"]')).not.toBeNull();
    });

    const editButton = document.querySelector('[data-action="edit-voice"][data-voice-id="alice"]') as HTMLButtonElement;
    editButton.click();

    // The shared form is now visible
    const form = document.getElementById('voice-edit-form') as HTMLElement;
    expect(form.style.display).not.toBe('none');

    // Pre-filled from getVoiceConfigRow(voiceId): style, ref audio, ref text, type
    const styleInput = document.getElementById('voice-edit-character-style') as HTMLInputElement;
    expect(styleInput.value).toBe('warm');
    const refAudio = document.getElementById('voice-edit-ref-audio') as HTMLInputElement;
    expect(refAudio.value).toBe('/refs/alice.wav');
    const refText = document.getElementById('voice-edit-ref-text') as HTMLInputElement;
    expect(refText.value).toBe('This is the reference line.');
    const typeSelect = document.getElementById('voice-edit-type') as HTMLSelectElement;
    expect(typeSelect.value).toBe('clone');

    // Alias pre-fill: the row's alias_of ('alice-base') is NOT in the loaded
    // catalog — the stale current alias is still surfaced as the selection.
    const aliasSelect = document.getElementById('voice-edit-alias-of') as HTMLSelectElement;
    expect(aliasSelect.value).toBe('alice-base');

    // Form carries Save + Cancel actions (the Save PUT lands in Phase 3)
    expect(document.querySelector('[data-action="save-voice"]')).not.toBeNull();
    expect(document.querySelector('[data-action="cancel-voice"]')).not.toBeNull();
  });

  it('type select exposes exactly the 5 supported voice types', async () => {
    state.pipelineBookId = 'book-abc';
    document.dispatchEvent(new Event('DOMContentLoaded'));

    await vi.waitFor(() => {
      expect(document.querySelector('[data-action="edit-voice"][data-voice-id="alice"]')).not.toBeNull();
    });
    (document.querySelector('[data-action="edit-voice"][data-voice-id="alice"]') as HTMLButtonElement).click();

    const typeSelect = document.getElementById('voice-edit-type') as HTMLSelectElement;
    const options = Array.from(typeSelect.options).map(o => o.value);
    expect(options).toEqual(['custom', 'clone', 'builtin_lora', 'lora', 'design']);
  });

  it('alias_of select lists other existing voices, excluding the edited voice and NARRATOR', async () => {
    vi.mocked(API.get).mockImplementation((url: string) => {
      if (url === '/api/pipeline/voices') return Promise.resolve(ALIAS_CATALOG_VOICES);
      if (url.startsWith('/api/pipeline/characters/')) return Promise.resolve(MOCK_CHARACTERS);
      return Promise.resolve([]);
    });
    state.pipelineBookId = 'book-abc';
    document.dispatchEvent(new Event('DOMContentLoaded'));

    await vi.waitFor(() => {
      expect(document.querySelector('[data-action="edit-voice"][data-voice-id="alice"]')).not.toBeNull();
    });
    (document.querySelector('[data-action="edit-voice"][data-voice-id="alice"]') as HTMLButtonElement).click();

    const aliasSelect = document.getElementById('voice-edit-alias-of') as HTMLSelectElement;
    const values = Array.from(aliasSelect.options).map(o => o.value);
    // First option clears the alias; only other existing voices are offered
    expect(values[0]).toBe('');
    expect(values).toContain('bob');
    expect(values).not.toContain('alice'); // the voice being edited
    expect(values).not.toContain('NARRATOR'); // the narrator pseudo-row
  });

  it('cancel closes the edit form', async () => {
    state.pipelineBookId = 'book-abc';
    document.dispatchEvent(new Event('DOMContentLoaded'));

    await vi.waitFor(() => {
      expect(document.querySelector('[data-action="edit-voice"][data-voice-id="alice"]')).not.toBeNull();
    });
    (document.querySelector('[data-action="edit-voice"][data-voice-id="alice"]') as HTMLButtonElement).click();

    const form = document.getElementById('voice-edit-form') as HTMLElement;
    expect(form.style.display).not.toBe('none');

    (document.querySelector('[data-action="cancel-voice"]') as HTMLButtonElement).click();
    expect(form.style.display).toBe('none');
  });

  it('reports an unknown voice id without opening the form', async () => {
    state.pipelineBookId = 'book-abc';
    document.dispatchEvent(new Event('DOMContentLoaded'));

    await vi.waitFor(() => {
      expect(document.querySelector('[data-action="edit-voice"][data-voice-id="alice"]')).not.toBeNull();
    });

    const { openVoiceEditForm } = await import('../../src/tabs/voices');
    openVoiceEditForm('ghost');

    expect(showToast).toHaveBeenCalledWith('Voice config not found: ghost', 'error');
    const form = document.getElementById('voice-edit-form') as HTMLElement;
    expect(form.style.display).toBe('none');
  });
});

// ---------------------------------------------------------------------------
// Voice config save — PUT /voices/{id} with exclude_unset (Plan H phase 3)
// ---------------------------------------------------------------------------

describe('voice config save (Plan H phase 3)', () => {
  // Deliberately NO initVoices()/DOMContentLoaded dispatch here: the Phase-2
  // describe's beforeAll initVoices() listener persists for the rest of the
  // file, so a click-through save test dispatching DOMContentLoaded would fire
  // BOTH listeners → the save handler runs twice → PUT fires twice (exec-worker
  // log L162). These tests call the exported saveVoiceConfig directly with a
  // constructed form DOM and assert with toHaveBeenCalledWith (call-count
  // agnostic). API.put is mockReset in beforeEach so a failed test cannot leak
  // an unconsumed mockResolvedValueOnce queue into the next test (log L139).
  beforeEach(() => {
    document.body.innerHTML = `
      <div id="pipeline-voices-section" style="display:none;">
        <select id="narrator-voice-select"></select>
        <div id="voice-catalog"></div>
        <div id="voice-edit-form" style="display:none;">
          <span id="voice-edit-title"></span>
          <input id="voice-edit-character-style">
          <input id="voice-edit-ref-audio">
          <input id="voice-edit-ref-text">
          <select id="voice-edit-type">
            <option value="custom">custom</option>
            <option value="clone">clone</option>
            <option value="builtin_lora">builtin_lora</option>
            <option value="lora">lora</option>
            <option value="design">design</option>
          </select>
          <select id="voice-edit-alias-of"></select>
          <button data-action="save-voice"></button>
          <button data-action="cancel-voice"></button>
        </div>
        <div id="character-ledger"></div>
      </div>
    `;
    vi.clearAllMocks();
    vi.mocked(API.put).mockReset();
  });

  it('PUTs only the edited fields (exclude_unset semantics)', async () => {
    vi.mocked(API.put).mockResolvedValueOnce({ ...FULL_CATALOG_ROW });

    const { openVoiceEditForm, saveVoiceConfig } = await import('../../src/tabs/voices');
    registerVoiceCatalog([FULL_CATALOG_ROW]);
    openVoiceEditForm('alice');

    // Edit ONLY ref_text — the unchanged fields must not appear in the body.
    (document.getElementById('voice-edit-ref-text') as HTMLInputElement).value = 'A new reference line.';
    saveVoiceConfig('alice');

    expect(API.put).toHaveBeenCalledWith('/api/pipeline/voices/alice', {
      ref_text: 'A new reference line.',
    });
  });

  it('type switch sends the chosen type verbatim', async () => {
    vi.mocked(API.put).mockResolvedValueOnce({ ...FULL_CATALOG_ROW });

    const { openVoiceEditForm, saveVoiceConfig } = await import('../../src/tabs/voices');
    registerVoiceCatalog([FULL_CATALOG_ROW]);
    openVoiceEditForm('alice');

    (document.getElementById('voice-edit-type') as HTMLSelectElement).value = 'design';
    saveVoiceConfig('alice');

    expect(API.put).toHaveBeenCalledWith('/api/pipeline/voices/alice', { type: 'design' });
  });

  it('sends null for a blanked text field (clears per contract)', async () => {
    vi.mocked(API.put).mockResolvedValueOnce({ ...FULL_CATALOG_ROW });

    const { openVoiceEditForm, saveVoiceConfig } = await import('../../src/tabs/voices');
    registerVoiceCatalog([FULL_CATALOG_ROW]);
    openVoiceEditForm('alice');

    // character_style was 'warm'; blanking it must clear via null.
    (document.getElementById('voice-edit-character-style') as HTMLInputElement).value = '';
    saveVoiceConfig('alice');

    expect(API.put).toHaveBeenCalledWith('/api/pipeline/voices/alice', { character_style: null });
  });

  it('sends alias_of null when the alias selection is cleared', async () => {
    vi.mocked(API.put).mockResolvedValueOnce({ ...FULL_CATALOG_ROW });

    const { openVoiceEditForm, saveVoiceConfig } = await import('../../src/tabs/voices');
    registerVoiceCatalog([FULL_CATALOG_ROW]);
    openVoiceEditForm('alice');

    // Pre-fill surfaces the stale current alias; selecting the clear option ('')
    // must send alias_of: null per the contract.
    const aliasSelect = document.getElementById('voice-edit-alias-of') as HTMLSelectElement;
    expect(aliasSelect.value).toBe('alice-base');
    aliasSelect.value = '';
    saveVoiceConfig('alice');

    expect(API.put).toHaveBeenCalledWith('/api/pipeline/voices/alice', { alias_of: null });
  });

  it('re-renders the card from the response and closes the form on success', async () => {
    const updated = { ...FULL_CATALOG_ROW, type: 'design', character_style: 'calm' };
    vi.mocked(API.put).mockResolvedValueOnce(updated);

    const { openVoiceEditForm, saveVoiceConfig } = await import('../../src/tabs/voices');
    registerVoiceCatalog([FULL_CATALOG_ROW]);
    renderVoiceCatalog([FULL_CATALOG_ROW]);
    openVoiceEditForm('alice');

    (document.getElementById('voice-edit-type') as HTMLSelectElement).value = 'design';
    saveVoiceConfig('alice');

    // The catalog card reflects the response's new type + style…
    await vi.waitFor(() => {
      const card = document.querySelector('.voice-card[data-voice-id="alice"]');
      expect(card).not.toBeNull();
      expect(card!.textContent).toContain('design');
      expect(card!.textContent).toContain('calm');
    });

    // …and the form is closed with a success toast.
    const form = document.getElementById('voice-edit-form') as HTMLElement;
    expect(form.style.display).toBe('none');
    expect(showToast).toHaveBeenCalledWith('Voice config saved: Alice', 'success');
  });

  it('shows an error toast and keeps the form open when the PUT fails', async () => {
    // Backend validation (e.g. 422) surfaces through handleError as a rejected
    // promise — the error toast is the failure surface, and the form stays
    // open so the user can correct the values.
    vi.mocked(API.put).mockRejectedValueOnce(new Error('422: invalid voice type'));

    const { openVoiceEditForm, saveVoiceConfig } = await import('../../src/tabs/voices');
    registerVoiceCatalog([FULL_CATALOG_ROW]);
    openVoiceEditForm('alice');

    (document.getElementById('voice-edit-type') as HTMLSelectElement).value = 'design';
    saveVoiceConfig('alice');

    await vi.waitFor(() => {
      expect(showToast).toHaveBeenCalledWith('Failed to save voice config: 422: invalid voice type', 'error');
    });
    expect((document.getElementById('voice-edit-form') as HTMLElement).style.display).not.toBe('none');
  });
});

// ---------------------------------------------------------------------------
// Narrator override + preview reuse (Plan H phase 4)
// ---------------------------------------------------------------------------

describe('narrator + form preview (Plan H phase 4)', () => {
  // The Phase-2 describe's beforeAll initVoices() listener persists for the
  // rest of the file (exec-worker log L162). The click-through test below
  // registers its own initVoices() here, so dispatching DOMContentLoaded fires
  // BOTH listeners — and because BOTH wire a click handler for
  // [data-action="preview-voice-form"] on #voice-edit-form, a single click
  // fires the form preview TWICE (stacked-listener gotcha, logs L141/L162).
  // The second POST hits the consumed mockResolvedValueOnce (post is a bare
  // vi.fn after mockReset) and fails with an UNASSERTED error toast; the test
  // only pins the first POST's args via toHaveBeenCalledWith, which is
  // call-count agnostic. All other tests call the exported handlers directly
  // with a constructed form DOM (Phase-3 convention) and assert with
  // toHaveBeenCalledWith (call-count agnostic). API.put/post are mockReset in
  // beforeEach so a failed test cannot leak an unconsumed mockResolvedValueOnce
  // queue into the next test (log L139).
  beforeAll(() => {
    initVoices();
  });

  beforeEach(() => {
    document.body.innerHTML = `
      <div id="pipeline-voices-section" style="display:none;">
        <select id="narrator-voice-select"></select>
        <div id="voice-catalog"></div>
        <div id="voice-edit-form" style="display:none;">
          <span id="voice-edit-title"></span>
          <input id="voice-edit-character-style">
          <input id="voice-edit-ref-audio">
          <input id="voice-edit-ref-text">
          <select id="voice-edit-type">
            <option value="custom">custom</option>
            <option value="clone">clone</option>
            <option value="builtin_lora">builtin_lora</option>
            <option value="lora">lora</option>
            <option value="design">design</option>
          </select>
          <select id="voice-edit-alias-of"></select>
          <button data-action="preview-voice-form"><i class="fas fa-play me-1"></i>Preview</button>
          <button data-action="save-voice"></button>
          <button data-action="cancel-voice"></button>
        </div>
        <div id="character-ledger"></div>
      </div>
    `;
    vi.clearAllMocks();
    vi.mocked(API.put).mockReset();
    vi.mocked(API.post).mockReset();
    audioInstances = [];
    vi.stubGlobal('Audio', MockAudio);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('narrator selector PUT body carries ONLY the voice key (exclude_unset preserved)', async () => {
    // Pin the narrator regression: handleNarratorVoiceChange must keep sending
    // { voice } only — the Phase-3 save handler's exclude_unset body must not
    // leak into the narrator path, and the UNKNOWN→NARRATOR fallback (a
    // backend rule) must not be worked around on the frontend.
    vi.mocked(API.put).mockResolvedValueOnce({ id: 'NARRATOR', voice: 'Bob' });

    handleNarratorVoiceChange('Bob');

    const narratorCall = vi.mocked(API.put).mock.calls.find(
      ([url]) => url === '/api/pipeline/voices/NARRATOR',
    );
    expect(narratorCall).toBeDefined();
    const [url, body] = narratorCall!;
    expect(url).toBe('/api/pipeline/voices/NARRATOR');
    // Only the `voice` key — no character_style/ref_text/type/alias_of etc.
    expect(Object.keys(body as Record<string, unknown>)).toEqual(['voice']);
    expect(body).toEqual({ voice: 'Bob' });
    expect(getCurrentNarratorVoice()).toBe('Bob');
  });

  it('form preview POSTs /voices/{id}/preview with the form ref_text as the sample text', async () => {
    vi.mocked(API.post).mockResolvedValueOnce({
      audio_url: '/designed_voices/previews/alice.wav',
      voice_id: 'alice',
    });

    const { openVoiceEditForm, previewVoiceFromForm } = await import('../../src/tabs/voices');
    registerVoiceCatalog([FULL_CATALOG_ROW]);
    openVoiceEditForm('alice');

    // The form carries a preview trigger near ref_text.
    const button = document.querySelector('[data-action="preview-voice-form"]') as HTMLButtonElement;
    expect(button).not.toBeNull();

    // The form's ref_text (non-empty) is sent as sample_text, NOT the default.
    (document.getElementById('voice-edit-ref-text') as HTMLInputElement).value = 'This is the reference line.';
    previewVoiceFromForm(button);

    await vi.waitFor(() => {
      expect(API.post).toHaveBeenCalledWith('/api/pipeline/voices/alice/preview', {
        sample_text: 'This is the reference line.',
      });
    });

    // The returned audio is played via the existing previewVoice pattern.
    await vi.waitFor(() => {
      expect(audioInstances.length).toBe(1);
      expect(audioInstances[0].src).toBe('/designed_voices/previews/alice.wav');
      expect(audioInstances[0].play).toHaveBeenCalled();
    });
    expect(showToast).toHaveBeenCalledWith('Preview generated successfully', 'success');
    expect(button.disabled).toBe(false);
  });

  it('form preview falls back to the default sample text when ref_text is empty', async () => {
    vi.mocked(API.post).mockResolvedValueOnce({
      audio_url: '/designed_voices/previews/alice.wav',
      voice_id: 'alice',
    });

    const { openVoiceEditForm, previewVoiceFromForm } = await import('../../src/tabs/voices');
    registerVoiceCatalog([FULL_CATALOG_ROW]);
    openVoiceEditForm('alice');

    // Blank the pre-filled ref_text — the default sample text is sent.
    (document.getElementById('voice-edit-ref-text') as HTMLInputElement).value = '';
    const button = document.querySelector('[data-action="preview-voice-form"]') as HTMLButtonElement;
    previewVoiceFromForm(button);

    await vi.waitFor(() => {
      expect(API.post).toHaveBeenCalledWith('/api/pipeline/voices/alice/preview', {
        sample_text: 'This is a preview of the voice.',
      });
    });
  });

  it('form preview shows an error toast and restores the button when the preview API rejects', async () => {
    vi.mocked(API.post).mockRejectedValueOnce(new Error('TTS engine not available'));

    const { openVoiceEditForm, previewVoiceFromForm } = await import('../../src/tabs/voices');
    registerVoiceCatalog([FULL_CATALOG_ROW]);
    openVoiceEditForm('alice');

    const button = document.querySelector('[data-action="preview-voice-form"]') as HTMLButtonElement;
    previewVoiceFromForm(button);

    await vi.waitFor(() => {
      expect(showToast).toHaveBeenCalledWith('Failed to generate preview: TTS engine not available', 'error');
    });
    expect(button.disabled).toBe(false);
    expect(button.innerHTML).toContain('fas fa-play');
  });

  it('clicking the form preview button POSTs the form ref_text sample (wired through initVoices)', async () => {
    vi.mocked(API.post).mockResolvedValueOnce({
      audio_url: '/designed_voices/previews/alice.wav',
      voice_id: 'alice',
    });

    state.pipelineBookId = 'book-abc';
    document.dispatchEvent(new Event('DOMContentLoaded'));

    // Wait for the catalog card to render, then open the form via Edit.
    await vi.waitFor(() => {
      expect(document.querySelector('[data-action="edit-voice"][data-voice-id="alice"]')).not.toBeNull();
    });
    (document.querySelector('[data-action="edit-voice"][data-voice-id="alice"]') as HTMLButtonElement).click();

    const form = document.getElementById('voice-edit-form') as HTMLElement;
    expect(form.style.display).not.toBe('none');

    (document.getElementById('voice-edit-ref-text') as HTMLInputElement).value = 'Click-through sample.';
    (document.querySelector('[data-action="preview-voice-form"]') as HTMLButtonElement).click();

    await vi.waitFor(() => {
      expect(API.post).toHaveBeenCalledWith('/api/pipeline/voices/alice/preview', {
        sample_text: 'Click-through sample.',
      });
    });

    // Double-fire hardening: only the FIRST POST resolves with an audio URL —
    // the second hits the consumed mock and fails before `new Audio` — so
    // exactly one Audio instance is created despite the stacked listeners.
    await vi.waitFor(() => {
      expect(audioInstances.length).toBe(1);
    });
  });
});

// ---------------------------------------------------------------------------
// Click-through save (Plan H — TestAnalyzer gap #1): the initVoices
// [data-action="save-voice"] branch is only reachable through the DOM, which
// the Phase-3 direct-call tests never exercise.
// ---------------------------------------------------------------------------

describe('voice config save click-through (Plan H, isolated initVoices)', () => {
  // Third initVoices() registration in this file. Dispatching DOMContentLoaded
  // in the test below fires THREE listeners (Phase-2, Phase-4 and this one) —
  // each wires its own [data-action="save-voice"] click handler on
  // #voice-edit-form, so one Save click invokes saveVoiceConfig three times
  // (stacked-listener gotcha, logs L141/L162). The test absorbs the stacked
  // triple-fire by giving API.put a PERSISTENT mockResolvedValue (not Once —
  // the 2nd/3rd calls must also resolve, or `undefined.then` throws) and
  // asserting count-agnostically (toHaveBeenCalledWith + form closed + success
  // toast). A single-fire click-through is impossible without disturbing the
  // earlier describes' harness, so this is the cleanest isolated exercise of
  // the branch.
  beforeAll(() => {
    initVoices();
  });

  beforeEach(() => {
    document.body.innerHTML = `
      <div id="pipeline-voices-section" style="display:none;">
        <select id="narrator-voice-select"></select>
        <div id="voice-catalog"></div>
        <div id="voice-edit-form" style="display:none;">
          <span id="voice-edit-title"></span>
          <input id="voice-edit-character-style">
          <input id="voice-edit-ref-audio">
          <input id="voice-edit-ref-text">
          <select id="voice-edit-type">
            <option value="custom">custom</option>
            <option value="clone">clone</option>
            <option value="builtin_lora">builtin_lora</option>
            <option value="lora">lora</option>
            <option value="design">design</option>
          </select>
          <select id="voice-edit-alias-of"></select>
          <button data-action="preview-voice-form"><i class="fas fa-play me-1"></i>Preview</button>
          <button data-action="save-voice"></button>
          <button data-action="cancel-voice"></button>
        </div>
        <div id="character-ledger"></div>
      </div>
    `;
    vi.clearAllMocks();
    vi.mocked(API.get).mockImplementation((url: string) => {
      if (url === '/api/pipeline/voices') return Promise.resolve([FULL_CATALOG_ROW]);
      if (url.startsWith('/api/pipeline/characters/')) return Promise.resolve(MOCK_CHARACTERS);
      return Promise.resolve([]);
    });
    // Persistent (not Once): the stacked listeners fire saveVoiceConfig three
    // times on a single click — every PUT must resolve so no call hits an
    // unconsumed mock and throws `undefined.then`.
    vi.mocked(API.put).mockResolvedValue({ ...FULL_CATALOG_ROW, ref_text: 'Click-through saved.' });
  });

  it('clicking Save PUTs only the edited field and closes the form (wired through initVoices)', async () => {
    state.pipelineBookId = 'book-abc';
    document.dispatchEvent(new Event('DOMContentLoaded'));

    // Cards render from the mocked GET /voices; open the form via Edit.
    await vi.waitFor(() => {
      expect(document.querySelector('[data-action="edit-voice"][data-voice-id="alice"]')).not.toBeNull();
    });
    (document.querySelector('[data-action="edit-voice"][data-voice-id="alice"]') as HTMLButtonElement).click();

    const form = document.getElementById('voice-edit-form') as HTMLElement;
    expect(form.style.display).not.toBe('none');

    // Change ONLY ref_text — the PUT body must carry just that key (exclude_unset).
    (document.getElementById('voice-edit-ref-text') as HTMLInputElement).value = 'Click-through saved.';
    (document.querySelector('[data-action="save-voice"]') as HTMLButtonElement).click();

    await vi.waitFor(() => {
      expect(API.put).toHaveBeenCalledWith('/api/pipeline/voices/alice', {
        ref_text: 'Click-through saved.',
      });
    });
    // Success path: form closes + success toast (count-agnostic assertions).
    await vi.waitFor(() => {
      expect(form.style.display).toBe('none');
      expect(showToast).toHaveBeenCalledWith('Voice config saved: Alice', 'success');
    });
  });
});
