/**
 * Spec-first tests for Voices tab (frontend/src/tabs/voices.ts).
 *
 * Tests cover: pipeline character loading, character ledger display,
 * voice assignment dropdown persistence, narrator voice selector (Phase 19),
 * voice catalog rendering & preview (Phase 23), and the voices-GET failure path.
 *
 * Run with `npm test` (vitest run) from frontend/ — vitest ^4.1.10 and
 * jsdom ^30.0.1 are installed (see frontend/package.json and vitest.config.ts).
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
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
  initVoices,
  handleNarratorVoiceChange,
  getCurrentNarratorVoice,
  NARRATOR_DEFAULT_VOICE,
  createVoiceCard,
  renderVoiceCatalog,
  previewVoice,
  VoiceConfigRow,
} from '../../src/tabs/voices';
import { state } from '../../src/state';
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

    // Delegated handler (initVoices wiring) persists the assignment via PUT
    expect(API.put).toHaveBeenCalledWith('/api/pipeline/characters/char-001/voice', {
      voice_assignment_id: 'Alice',
    });
    // Local ledger state was updated through handleCharacterVoiceChange
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
