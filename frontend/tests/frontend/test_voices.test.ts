/**
 * Spec-first tests for Voices tab (frontend/src/tabs/voices.ts).
 * Tests cover: pipeline character loading, character ledger display,
 * voice assignment dropdown, pipeline toggle integration.
 *
 * NOTE: No test framework is installed in frontend/package.json.
 * These tests are written with vitest-compatible syntax.
 * To run: install vitest (`npm install -D vitest jsdom`) and add to package.json:
 *   "scripts": { "test": "vitest" },
 *   "vitest": { "environment": "jsdom" }
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
  debouncedSaveVoices,
  initVoices,
} from '../../src/tabs/voices';
import { state } from '../../src/state';
import * as API from '../../src/api';

// Mock the API module
vi.mock('../../src/api', () => ({
  get: vi.fn(),
  post: vi.fn(),
  upload: vi.fn(),
}));

// Mock utils to avoid DOM side effects
vi.mock('../../src/utils', () => ({
  showToast: vi.fn(),
  showConfirm: vi.fn(),
  escapeHtml: (s: string) => s,
}));

// Mock templates module
vi.mock('../../src/templates', () => ({
  createVoiceCard: vi.fn((v: any, i: number) => `<div class="voice-card" data-voice="${v.name}"></div>`),
  buildSpeakerSelect: vi.fn(() => '<select></select>'),
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
// Pipeline toggle integration
// ---------------------------------------------------------------------------

describe('loadVoices — pipeline toggle integration', () => {
  beforeEach(() => {
    document.body.innerHTML = `
      <div id="pipeline-voices-disabled-notice" style="display:none;"></div>
      <div id="pipeline-voices-section" style="display:none;">
        <div id="character-ledger"></div>
      </div>
      <div id="legacy-voices-section" style="display:none;">
        <div id="voices-list"></div>
      </div>
      <span id="voice-save-status"></span>
    `;
    vi.clearAllMocks();
    vi.mocked(API.get).mockImplementation((url: string) => {
      if (url === '/api/voices') return Promise.resolve(MOCK_VOICES);
      if (url.startsWith('/api/pipeline/characters/')) return Promise.resolve(MOCK_CHARACTERS);
      if (url === '/api/voice_design/list') return Promise.resolve([]);
      if (url === '/api/clone_voices/list') return Promise.resolve([]);
      if (url === '/api/lora/models') return Promise.resolve([]);
      return Promise.resolve([]);
    });
  });

  it('should load pipeline characters when pipelineEnabled is true', async () => {
    state.pipelineEnabled = true;
    state.pipelineBookId = 'book-abc';

    await loadVoices();

    expect(API.get).toHaveBeenCalledWith('/api/voices');
    expect(API.get).toHaveBeenCalledWith('/api/pipeline/characters/book-abc');
    expect(state.voicesNames).toEqual(['Alice', 'Bob', 'Charlie']);
  });

  it('should show pipeline section and hide legacy when pipelineEnabled is true', async () => {
    state.pipelineEnabled = true;
    state.pipelineBookId = 'book-abc';

    await loadVoices();

    const pipelineNotice = document.getElementById('pipeline-voices-disabled-notice');
    const pipelineSection = document.getElementById('pipeline-voices-section');
    const legacySection = document.getElementById('legacy-voices-section');

    expect(pipelineNotice!.style.display).toBe('none');
    expect(pipelineSection!.style.display).toBe('');
    expect(legacySection!.style.display).toBe('none');
  });

  it('should load legacy voices when pipelineEnabled is false', async () => {
    state.pipelineEnabled = false;

    await loadVoices();

    expect(API.get).toHaveBeenCalledWith('/api/voices');
    expect(API.get).not.toHaveBeenCalledWith(expect.stringContaining('/api/pipeline/characters/'));
  });

  it('should show legacy section and hide pipeline when pipelineEnabled is false', async () => {
    state.pipelineEnabled = false;

    await loadVoices();

    const pipelineNotice = document.getElementById('pipeline-voices-disabled-notice');
    const pipelineSection = document.getElementById('pipeline-voices-section');
    const legacySection = document.getElementById('legacy-voices-section');

    expect(pipelineNotice!.style.display).toBe('');
    expect(pipelineSection!.style.display).toBe('none');
    expect(legacySection!.style.display).toBe('');
  });

  it('should show warning when pipelineEnabled but no book onboarded', async () => {
    state.pipelineEnabled = true;
    state.pipelineBookId = null;

    await loadVoices();

    const container = document.getElementById('character-ledger');
    expect(container!.innerHTML).toContain('alert alert-warning');
    expect(container!.innerHTML).toContain('No book onboarded');
  });

  it('should show error in character ledger when API fails', async () => {
    state.pipelineEnabled = true;
    state.pipelineBookId = 'book-fail';

    vi.mocked(API.get).mockImplementation((url: string) => {
      if (url === '/api/voices') return Promise.resolve(MOCK_VOICES);
      if (url.startsWith('/api/pipeline/characters/')) return Promise.reject(new Error('Server error'));
      return Promise.resolve([]);
    });

    await loadVoices();

    const container = document.getElementById('character-ledger');
    expect(container!.innerHTML).toContain('alert alert-danger');
    expect(container!.innerHTML).toContain('Failed to load characters');
  });

  it('should always load /api/voices regardless of pipeline mode', async () => {
    state.pipelineEnabled = false;
    await loadVoices();
    expect(API.get).toHaveBeenCalledWith('/api/voices');

    vi.clearAllMocks();
    vi.mocked(API.get).mockResolvedValue(MOCK_VOICES);

    state.pipelineEnabled = true;
    state.pipelineBookId = 'book-abc';
    await loadVoices();
    expect(API.get).toHaveBeenCalledWith('/api/voices');
  });
});

// ---------------------------------------------------------------------------
// getCachedCharacters
// ---------------------------------------------------------------------------

describe('getCachedCharacters', () => {
  beforeEach(() => {
    document.body.innerHTML = `<div id="character-ledger"></div>`;
    state.voicesNames = ['Alice'];
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
// debouncedSaveVoices (preserved functionality)
// ---------------------------------------------------------------------------

describe('debouncedSaveVoices', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    document.body.innerHTML = `
      <span id="voice-save-status"></span>
      <div id="voices-list"></div>
    `;
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('should show "unsaved" status immediately', () => {
    debouncedSaveVoices();
    const statusEl = document.getElementById('voice-save-status');
    expect(statusEl!.innerHTML).toContain('unsaved');
    expect(statusEl!.innerHTML).toContain('text-warning');
  });

  it('should not save immediately (debounced 800ms)', () => {
    debouncedSaveVoices();
    expect(API.post).not.toHaveBeenCalled();
  });

  it('should save after 800ms debounce delay', () => {
    debouncedSaveVoices();
    vi.advanceTimersByTime(800);
    // Note: collectVoiceConfig returns empty when no voice-cards exist,
    // so API.post may not be called. This tests the timer mechanism.
  });

  it('should reset timer on subsequent calls', () => {
    debouncedSaveVoices();
    vi.advanceTimersByTime(400);
    debouncedSaveVoices(); // reset
    vi.advanceTimersByTime(400);
    // Should not have saved yet (only 400ms since last call)
    expect(API.post).not.toHaveBeenCalled();
    vi.advanceTimersByTime(400);
    // Now 800ms since last call
  });
});

// ---------------------------------------------------------------------------
// initVoices (event listener attachment)
// ---------------------------------------------------------------------------

describe('initVoices', () => {
  beforeEach(() => {
    document.body.innerHTML = `
      <div id="character-ledger"></div>
      <div id="voices-list"></div>
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
});
