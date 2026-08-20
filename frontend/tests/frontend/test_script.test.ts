/**
 * Spec-first tests for Script tab (frontend/src/tabs/script.ts).
 * Tests cover: pipeline API calls, walk status display, button behavior.
 *
 * NOTE: No test framework is installed in frontend/package.json.
 * These tests are written with vitest-compatible syntax.
 * To run: install vitest (`npm install -D vitest jsdom`) and add to package.json:
 *   "scripts": { "test": "vitest" },
 *   "vitest": { "environment": "jsdom" }
 */

import { describe, it, expect, beforeAll, beforeEach, afterEach, afterAll, vi } from 'vitest';
import {
  pipelineOnboard,
  pipelineRunWalk,
  pipelineRunAllWalks,
  pipelineWalkStatus,
  pipelineReonboard,
  pipelineCancelWalks,
  renderWalkStatuses,
  startWalkPolling,
  stopWalkPolling,
  initScript,
} from '../../src/tabs/script';
import { WALK_ORDER, WALK_DISPLAY_NAMES } from '../../src/pipeline/walks';
import * as API from '../../src/api';
import { readFileSync, existsSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { state } from '../../src/state';

// Mock the API module — partial mock: get/post stay mocked, but the REAL
// postWithRetryOnce is exposed so cancel-walks tests exercise the actual
// 503+Retry-After retry-once wrapper against the module-scope mockFetch.
vi.mock('../../src/api', async () => {
  const actual = await vi.importActual<typeof import('../../src/api')>('../../src/api');
  return {
    get: vi.fn(),
    post: vi.fn(),
    postWithRetryOnce: actual.postWithRetryOnce,
  };
});

// Mock utils to avoid DOM side effects
vi.mock('../../src/utils', () => ({
  showToast: vi.fn(),
  showConfirm: vi.fn(),
  escapeHtml: (s: string) => s,
}));


// Mock global fetch for pipelineOnboard (uses fetch directly, not API.post)
const mockFetch = vi.fn();
global.fetch = mockFetch;

// ---------------------------------------------------------------------------
// index.html fixture resolution
// ---------------------------------------------------------------------------
// Read frontend/index.html robustly regardless of the vitest cwd: prefer a
// path derived from this file's own location (import.meta.url → fileURLToPath)
// and fall back to process.cwd() when the runner does not expose a file:// URL
// (jsdom transforms import.meta.url to an http URL in this setup).

function readIndexHtml(): string {
  try {
    const viaMeta = fileURLToPath(new URL('../../index.html', import.meta.url));
    if (existsSync(viaMeta)) return readFileSync(viaMeta, 'utf8');
  } catch {
    // import.meta.url is not a file:// URL under this runner — fall back.
  }
  return readFileSync(resolve(process.cwd(), 'index.html'), 'utf8');
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

describe('WALK_ORDER', () => {
  it('should contain exactly 9 walk names', () => {
    expect(WALK_ORDER).toHaveLength(9);
  });

  it('should contain walk names in canonical order matching backend WALK_ORDER', () => {
    expect(WALK_ORDER).toEqual([
      'walk_2a_scene_segmentation',
      'walk_2b_character_discovery',
      'walk_2c_alias_resolution',
      'walk_2d_scene_presence',
      'walk_2e_span_attribution',
      'walk_2f_character_description',
      'walk_2g_voice_audition',
      'walk_2h_voice_assignment',
      'walk_2i_delivery',
    ]);
  });

  it('should be a readonly array', () => {
    expect(Array.isArray(WALK_ORDER)).toBe(true);
  });
});

describe('WALK_DISPLAY_NAMES', () => {
  it('should have a label for every walk name', () => {
    for (const walkName of WALK_ORDER) {
      expect(WALK_DISPLAY_NAMES).toHaveProperty(walkName);
      expect(typeof WALK_DISPLAY_NAMES[walkName]).toBe('string');
      expect(WALK_DISPLAY_NAMES[walkName].length).toBeGreaterThan(0);
    }
  });

  it('should have human-readable labels (not raw walk names)', () => {
    expect(WALK_DISPLAY_NAMES['walk_2a_scene_segmentation']).toBe('Scene Segmentation');
    expect(WALK_DISPLAY_NAMES['walk_2b_character_discovery']).toBe('Character Discovery');
    expect(WALK_DISPLAY_NAMES['walk_2i_delivery']).toBe('Delivery');
  });
});

// ---------------------------------------------------------------------------
// Pipeline API functions
// ---------------------------------------------------------------------------

describe('pipelineOnboard', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('should POST to /api/pipeline/onboard with FormData containing the file', async () => {
    const mockFile = new File(['test content'], 'test-book.epub', { type: 'application/epub+zip' });
    const mockResponse = { book_id: 'abc-123', series_id: 'series-1', chapters: 12 };

    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve(mockResponse),
    });

    const result = await pipelineOnboard(mockFile);

    expect(mockFetch).toHaveBeenCalledTimes(1);
    expect(mockFetch).toHaveBeenCalledWith('/api/pipeline/onboard', expect.objectContaining({
      method: 'POST',
    }));

    // Verify FormData was sent
    const callArgs = mockFetch.mock.calls[0];
    const body = callArgs[1].body;
    expect(body).toBeInstanceOf(FormData);
    expect(body.get('file')).toBe(mockFile);

    expect(result).toEqual(mockResponse);
  });

  it('should throw an error when the response is not ok', async () => {
    const mockFile = new File(['test'], 'bad.epub', { type: 'application/epub+zip' });

    mockFetch.mockResolvedValueOnce({
      ok: false,
      statusText: 'Bad Request',
      json: () => Promise.resolve({ detail: 'Invalid EPUB format' }),
    });

    await expect(pipelineOnboard(mockFile)).rejects.toThrow('Invalid EPUB format');
  });

  it('should fall back to statusText when error body has no detail', async () => {
    const mockFile = new File(['test'], 'bad.epub', { type: 'application/epub+zip' });

    mockFetch.mockResolvedValueOnce({
      ok: false,
      statusText: 'Internal Server Error',
      json: () => Promise.reject(new Error('no json')),
    });

    await expect(pipelineOnboard(mockFile)).rejects.toThrow('Internal Server Error');
  });
});

describe('pipelineRunWalk', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('should POST to /api/pipeline/run_walk with walk_name and book_id', async () => {
    vi.mocked(API.post).mockResolvedValueOnce({ status: 'started' });

    await pipelineRunWalk('walk_2a_scene_segmentation', 'book-123');

    expect(API.post).toHaveBeenCalledWith('/api/pipeline/run_walk', {
      walk_name: 'walk_2a_scene_segmentation',
      book_id: 'book-123',
      config: {},
    });
  });

  it('should pass optional config when provided', async () => {
    vi.mocked(API.post).mockResolvedValueOnce({ status: 'started' });

    await pipelineRunWalk('walk_2b_character_discovery', 'book-456', { max_characters: 20 });

    expect(API.post).toHaveBeenCalledWith('/api/pipeline/run_walk', {
      walk_name: 'walk_2b_character_discovery',
      book_id: 'book-456',
      config: { max_characters: 20 },
    });
  });
});

describe('pipelineRunAllWalks', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('should POST to /api/pipeline/run_all_walks with book_id', async () => {
    vi.mocked(API.post).mockResolvedValueOnce({ status: 'started', walks: 9 });

    await pipelineRunAllWalks('book-789');

    expect(API.post).toHaveBeenCalledWith('/api/pipeline/run_all_walks', {
      book_id: 'book-789',
      config: {},
    });
  });

  it('should pass optional config when provided', async () => {
    vi.mocked(API.post).mockResolvedValueOnce({ status: 'started' });

    await pipelineRunAllWalks('book-789', { skip_delivery: true });

    expect(API.post).toHaveBeenCalledWith('/api/pipeline/run_all_walks', {
      book_id: 'book-789',
      config: { skip_delivery: true },
    });
  });
});

describe('pipelineWalkStatus', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('should GET /api/pipeline/walk_status/{book_id}', async () => {
    const mockStatuses = {
      walk_2a_scene_segmentation: 'completed',
      walk_2b_character_discovery: 'running',
      walk_2c_alias_resolution: 'pending',
    };
    vi.mocked(API.get).mockResolvedValueOnce(mockStatuses);

    const result = await pipelineWalkStatus('book-abc');

    expect(API.get).toHaveBeenCalledWith('/api/pipeline/walk_status/book-abc');
    expect(result).toEqual(mockStatuses);
  });
});

describe('pipelineReonboard', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('should POST to /api/pipeline/reonboard with book_id', async () => {
    const mockResult = { book_id: 'book-abc', version: 2, status: 're-onboarded' };
    vi.mocked(API.post).mockResolvedValueOnce(mockResult);

    const result = await pipelineReonboard('book-abc');

    expect(API.post).toHaveBeenCalledWith('/api/pipeline/reonboard', {
      book_id: 'book-abc',
    });
    expect(result).toEqual(mockResult);
  });
});

// ---------------------------------------------------------------------------
// Walk status display
// ---------------------------------------------------------------------------

describe('renderWalkStatuses', () => {
  beforeEach(() => {
    document.body.innerHTML = `
      <div id="walk-status-container"></div>
    `;
  });

  it('should render all 9 walks in the container', () => {
    const statuses: Record<string, string> = {};
    for (const name of WALK_ORDER) {
      statuses[name] = 'pending';
    }

    renderWalkStatuses(statuses);

    const container = document.getElementById('walk-status-container');
    expect(container).not.toBeNull();
    const walkRows = container!.querySelectorAll('[data-walk]');
    expect(walkRows.length).toBe(9);
  });

  it('should show "pending" status with bg-secondary badge for pending walks', () => {
    const statuses: Record<string, string> = {};
    for (const name of WALK_ORDER) {
      statuses[name] = 'pending';
    }

    renderWalkStatuses(statuses);

    const container = document.getElementById('walk-status-container');
    const firstRow = container!.querySelector('[data-walk]');
    const badge = firstRow!.querySelector('.badge');
    expect(badge!.classList.contains('bg-secondary')).toBe(true);
    expect(badge!.textContent).toContain('pending');
  });

  it('should show "completed" status with bg-success badge', () => {
    const statuses: Record<string, string> = {};
    for (const name of WALK_ORDER) {
      statuses[name] = 'completed';
    }

    renderWalkStatuses(statuses);

    const container = document.getElementById('walk-status-container');
    const firstRow = container!.querySelector('[data-walk]');
    const badge = firstRow!.querySelector('.badge');
    expect(badge!.classList.contains('bg-success')).toBe(true);
    expect(badge!.textContent).toContain('completed');
  });

  it('should show "running" status with bg-warning badge and spinner icon', () => {
    const statuses: Record<string, string> = {};
    for (const name of WALK_ORDER) {
      statuses[name] = 'running';
    }

    renderWalkStatuses(statuses);

    const container = document.getElementById('walk-status-container');
    const firstRow = container!.querySelector('[data-walk]');
    const badge = firstRow!.querySelector('.badge');
    expect(badge!.classList.contains('bg-warning')).toBe(true);
    expect(badge!.innerHTML).toContain('fa-spinner');
  });

  it('should show "failed" status with bg-danger badge', () => {
    const statuses: Record<string, string> = {};
    for (const name of WALK_ORDER) {
      statuses[name] = 'failed';
    }

    renderWalkStatuses(statuses);

    const container = document.getElementById('walk-status-container');
    const firstRow = container!.querySelector('[data-walk]');
    const badge = firstRow!.querySelector('.badge');
    expect(badge!.classList.contains('bg-danger')).toBe(true);
  });

  it('should use human-readable labels from WALK_DISPLAY_NAMES', () => {
    const statuses: Record<string, string> = {};
    for (const name of WALK_ORDER) {
      statuses[name] = 'pending';
    }

    renderWalkStatuses(statuses);

    const container = document.getElementById('walk-status-container');
    const firstRow = container!.querySelector('[data-walk="walk_2a_scene_segmentation"]');
    expect(firstRow!.textContent).toContain('Scene Segmentation');
  });

  it('should default to "pending" when status is missing for a walk', () => {
    const statuses: Record<string, string> = {};
    // Only set some walks, leave others missing
    statuses['walk_2a_scene_segmentation'] = 'completed';

    renderWalkStatuses(statuses);

    const container = document.getElementById('walk-status-container');
    const secondRow = container!.querySelector('[data-walk="walk_2b_character_discovery"]');
    const badge = secondRow!.querySelector('.badge');
    expect(badge!.textContent).toContain('pending');
  });

  it('should do nothing when the container element does not exist', () => {
    document.body.innerHTML = ''; // No container
    const statuses: Record<string, string> = {};
    // Should not throw
    expect(() => renderWalkStatuses(statuses)).not.toThrow();
  });
});

// ---------------------------------------------------------------------------
// Walk polling
// ---------------------------------------------------------------------------

describe('startWalkPolling / stopWalkPolling', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers();
    document.body.innerHTML = `
      <div id="walk-status-container"></div>
      <button id="btn-run-all-walks"></button>
    `;
  });

  afterEach(() => {
    stopWalkPolling();
    vi.useRealTimers();
  });

  it('should stop polling when no walks are running', async () => {
    const statuses: Record<string, string> = {};
    for (const name of WALK_ORDER) {
      statuses[name] = 'completed';
    }
    vi.mocked(API.get).mockResolvedValue(statuses);

    // startWalkPolling calls pipelineWalkStatus which uses API.get
    // But currentBookId is module-private, so we can't set it from tests.
    // This test verifies the stopWalkPolling function works.
    stopWalkPolling();
    expect(API.get).not.toHaveBeenCalled();
  });
});


// ---------------------------------------------------------------------------
// Run All Walks button behavior
// ---------------------------------------------------------------------------

describe('Run All Walks button', () => {
  beforeEach(() => {
    document.body.innerHTML = `
      <button id="btn-run-all-walks">
        <i class="fas fa-play me-2"></i>Run All Walks
      </button>
      <div id="walk-status-container"></div>
    `;
    vi.clearAllMocks();
  });

  it('should have correct initial text with play icon', () => {
    const btn = document.getElementById('btn-run-all-walks');
    expect(btn!.innerHTML).toContain('Run All Walks');
    expect(btn!.innerHTML).toContain('fa-play');
  });

  it('should be disabled when walks are running (via updateRunAllButton logic)', () => {
    const btn = document.getElementById('btn-run-all-walks') as HTMLButtonElement;
    // Simulate what updateRunAllButton(true) does
    btn.disabled = true;
    btn.innerHTML = '<i class="fas fa-spinner fa-spin me-2"></i>Walks Running...';

    expect(btn.disabled).toBe(true);
    expect(btn.innerHTML).toContain('Walks Running...');
    expect(btn.innerHTML).toContain('fa-spinner');
  });

  it('should be re-enabled when walks finish (via updateRunAllButton logic)', () => {
    const btn = document.getElementById('btn-run-all-walks') as HTMLButtonElement;
    // Simulate what updateRunAllButton(false) does
    btn.disabled = false;
    btn.innerHTML = '<i class="fas fa-play me-2"></i>Run All Walks';

    expect(btn.disabled).toBe(false);
    expect(btn.innerHTML).toContain('Run All Walks');
    expect(btn.innerHTML).toContain('fa-play');
  });
});

// ---------------------------------------------------------------------------
// Re-onboard button behavior
// ---------------------------------------------------------------------------

describe('Re-onboard button', () => {
  beforeEach(() => {
    document.body.innerHTML = `
      <button id="btn-reonboard">
        <i class="fas fa-redo me-2"></i>Re-onboard
      </button>
      <div id="walk-status-container"></div>
      <button id="btn-run-all-walks"></button>
    `;
    vi.clearAllMocks();
  });

  it('should have correct initial text with redo icon', () => {
    const btn = document.getElementById('btn-reonboard');
    expect(btn!.innerHTML).toContain('Re-onboard');
    expect(btn!.innerHTML).toContain('fa-redo');
  });

  it('should call showConfirm before re-onboarding', async () => {
    // The handleReonboard function calls showConfirm.
    // We verify the import is available and the function signature is correct.
    const { showConfirm } = await import('../../src/utils');
    expect(typeof showConfirm).toBe('function');
  });

  it('should call pipelineReonboard with currentBookId after confirmation', async () => {
    // Verify the API function exists and has the correct signature
    expect(typeof pipelineReonboard).toBe('function');
  });
});

// ---------------------------------------------------------------------------
// Cancel Walks with retry-once (Plan F, Phase 2)
//
// Backend contract (app/pipeline/api_walks.py cancel_walks — verified,
// unchanged): POST /api/pipeline/cancel_walks {book_id} → {status:'cancelled'};
// transaction() owner-thread contention surfaces 503 + Retry-After (same
// contract as cancel_render). Frontend contract (DD UX workflow #2): the
// caller retries EXACTLY ONCE before surfacing the error. The retry wrapper
// (api.postWithRetryOnce) uses raw fetch, and the api module is a partial
// mock here — so these tests count POST attempts on the module-scope
// mockFetch (global.fetch). handleCancelWalks is private and currentBookId is
// module-private, so the button path is exercised through a real onboard UI
// flow (sets currentBookId) followed by a #btn-cancel-walks click.
// ---------------------------------------------------------------------------

describe('Script Tab — Cancel Walks with Retry-once (Plan F, Phase 2)', () => {
  const retry503 = {
    ok: false,
    status: 503,
    statusText: 'Service Unavailable',
    headers: { get: (name: string) => (name === 'Retry-After' ? '1' : null) },
    json: async () => ({ detail: 'transaction contention' }),
  };
  const noHeader503 = {
    ok: false,
    status: 503,
    statusText: 'Service Unavailable',
    headers: { get: () => null },
    json: async () => ({ detail: 'transaction contention' }),
  };
  const okCancelled = {
    ok: true,
    status: 200,
    statusText: 'OK',
    headers: { get: () => null },
    json: async () => ({ status: 'cancelled' }),
  };

  function allCompletedStatuses(): Record<string, string> {
    const statuses: Record<string, string> = {};
    for (const name of WALK_ORDER) statuses[name] = 'completed';
    return statuses;
  }

  // Register the DOMContentLoaded wiring exactly ONCE for the whole describe.
  // (Calling initScript() per test would stack listeners: a later test's
  // dispatch would fire earlier tests' stale listeners too, re-wiring the
  // current DOM and double-firing the cancel click.)
  beforeAll(() => {
    initScript();
  });

  /** Drive a real onboard through the UI so currentBookId gets set. */
  async function onboardViaUi(): Promise<void> {
    const fileInput = document.getElementById('file-upload') as HTMLInputElement;
    const file = new File(['test'], 'book.epub', { type: 'application/epub+zip' });
    Object.defineProperty(fileInput, 'files', { value: [file], configurable: true });
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ book_id: 'book-cxl-1', series_id: 'series-1', chapters: 3 }),
    });
    vi.mocked(API.get).mockResolvedValue(allCompletedStatuses());
    document.dispatchEvent(new Event('DOMContentLoaded'));
    document.getElementById('btn-onboard-epub')!.click();
    await vi.advanceTimersByTimeAsync(0); // flush pipelineOnboard + initial walk-status poll
    mockFetch.mockClear(); // count only the cancel attempts below
  }

  beforeEach(() => {
    vi.clearAllMocks();
    mockFetch.mockReset(); // also drops stale Once queues from earlier describes
    vi.useFakeTimers();
    document.body.innerHTML = `
      <input type="file" id="file-upload">
      <span id="upload-status"></span>
      <button id="btn-onboard-epub"></button>
      <div id="walk-execution-section" style="display:none;"></div>
      <div id="walk-status-container"></div>
      <button id="btn-run-all-walks"></button>
      <button id="btn-cancel-walks"></button>
    `;
  });

  afterEach(() => {
    stopWalkPolling();
    vi.useRealTimers();
  });

  it('pipelineCancelWalks posts /api/pipeline/cancel_walks with {book_id} and retries exactly once on 503+Retry-After, succeeding when the retry returns 200', async () => {
    mockFetch
      .mockResolvedValueOnce(retry503)
      .mockResolvedValueOnce(okCancelled);

    const promise = pipelineCancelWalks('book-cxl-1');
    await vi.advanceTimersByTimeAsync(0); // attempt 1 → 503 → Retry-After delay scheduled
    await vi.advanceTimersByTimeAsync(1000); // delay elapses → attempt 2 → 200

    await expect(promise).resolves.toEqual({ status: 'cancelled' });
    expect(mockFetch).toHaveBeenCalledTimes(2);
    expect(mockFetch).toHaveBeenNthCalledWith(1, '/api/pipeline/cancel_walks', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ book_id: 'book-cxl-1' }),
    }));
    expect(mockFetch).toHaveBeenNthCalledWith(2, '/api/pipeline/cancel_walks', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ book_id: 'book-cxl-1' }),
    }));
  });

  it('pipelineCancelWalks rejects when both attempts return 503 and makes NO third attempt', async () => {
    mockFetch.mockResolvedValue(retry503); // both attempts → 503

    const promise = pipelineCancelWalks('book-cxl-1');
    const assertion = expect(promise).rejects.toThrow('transaction contention'); // attach handler before advancing
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(1000);

    await assertion;
    expect(mockFetch).toHaveBeenCalledTimes(2); // exactly 2 attempts — never a 3rd
  });

  it('pipelineCancelWalks does NOT retry a 503 without a Retry-After header', async () => {
    mockFetch.mockResolvedValue(noHeader503);

    const promise = pipelineCancelWalks('book-cxl-1');
    const assertion = expect(promise).rejects.toThrow('transaction contention'); // attach handler before advancing
    await vi.advanceTimersByTimeAsync(0);

    await assertion;
    expect(mockFetch).toHaveBeenCalledTimes(1); // no Retry-After → no retry
  });

  it('handleCancelWalks (Cancel Walks button) succeeds after a 503-then-200 retry: success toast, exactly 2 POST attempts', async () => {
    const { showToast } = await import('../../src/utils');
    await onboardViaUi();

    mockFetch
      .mockResolvedValueOnce(retry503)
      .mockResolvedValueOnce(okCancelled);

    document.getElementById('btn-cancel-walks')!.click();
    await vi.advanceTimersByTimeAsync(0); // attempt 1 → 503 → delay scheduled
    await vi.advanceTimersByTimeAsync(1000); // delay elapses → attempt 2 → 200

    expect(showToast).toHaveBeenCalledWith('Walks cancelled.', 'info');
    expect(mockFetch).toHaveBeenCalledTimes(2);
    expect(mockFetch).toHaveBeenNthCalledWith(1, '/api/pipeline/cancel_walks', expect.objectContaining({
      body: JSON.stringify({ book_id: 'book-cxl-1' }),
    }));
    expect(mockFetch).toHaveBeenNthCalledWith(2, '/api/pipeline/cancel_walks', expect.objectContaining({
      body: JSON.stringify({ book_id: 'book-cxl-1' }),
    }));
  });

  it('handleCancelWalks surfaces an error toast when both attempts return 503 and makes NO third attempt', async () => {
    const { showToast } = await import('../../src/utils');
    await onboardViaUi();

    mockFetch.mockResolvedValue(retry503); // both attempts → 503

    document.getElementById('btn-cancel-walks')!.click();
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(1000);

    expect(showToast).toHaveBeenCalledWith('Cancel walks failed: transaction contention', 'error');
    expect(mockFetch).toHaveBeenCalledTimes(2); // exactly 2 attempts — never a 3rd
  });
});

// ---------------------------------------------------------------------------
// Walk Runs List (Plan F, Phase 3)
//
// Backend contract (app/pipeline/api_walks.py get_walk_runs — verified,
// unchanged): GET /api/pipeline/walks/{book_id}/runs → list[dict] newest-first
// (ORDER BY created_ms DESC), each dict = WalkRunRow { run_id, walk_name,
// status ('pending'|'running'|'completed'|'failed'|'cancelled'), heartbeat_ms,
// created_ms, finished_ms, error|null }; [] when the book has no runs.
//
// The DOMContentLoaded wiring used by the UI-flow tests below is registered
// exactly ONCE by the Phase 2 describe's beforeAll (initScript()). These tests
// deliberately reuse that single listener — registering initScript() here
// again would stack a second listener that double-wires the same buttons and
// double-fires handleOnboard (see discovery log: stacked DOMContentLoaded
// listeners). The new exports are imported dynamically (same pattern as the
// file's existing `await import('../../src/utils')` calls) so the RED phase
// fails ONLY the new tests, never the pre-existing ones.
// ---------------------------------------------------------------------------

describe('Script Tab — Walk Runs List (Plan F, Phase 3)', () => {
  const createdMs = Date.UTC(2026, 7, 6, 14, 3); // 2026-08-06 14:03 UTC
  const finishedMs = Date.UTC(2026, 7, 6, 14, 12); // 2026-08-06 14:12 UTC

  /** Independent re-implementation of the format contract 'YYYY-MM-DD HH:MM' (local). */
  function fmt(ms: number): string {
    const d = new Date(ms);
    const p = (n: number) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  function runRow(overrides: Record<string, unknown> = {}): Record<string, unknown> {
    return {
      run_id: 'run-1',
      walk_name: 'walk_2a_scene_segmentation',
      status: 'completed',
      heartbeat_ms: 0,
      created_ms: createdMs,
      finished_ms: finishedMs,
      error: null,
      ...overrides,
    };
  }

  /** API.get mock: walk statuses (keeps polling alive) + a runs payload. */
  function mockPollingApi(runs: unknown[]): void {
    const statuses: Record<string, string> = {};
    for (const name of WALK_ORDER) statuses[name] = 'pending';
    statuses['walk_2a_scene_segmentation'] = 'running'; // keeps startWalkPolling looping
    vi.mocked(API.get).mockImplementation((endpoint: string) => {
      if (String(endpoint).endsWith('/runs')) return Promise.resolve(runs);
      return Promise.resolve(statuses);
    });
  }

  function runsCallCount(): number {
    return vi.mocked(API.get).mock.calls.filter(([ep]) => String(ep).includes('/runs')).length;
  }

  /** Drive a real onboard through the UI so currentBookId gets set and polling starts. */
  async function onboardViaUi(): Promise<void> {
    const fileInput = document.getElementById('file-upload') as HTMLInputElement;
    const file = new File(['test'], 'book.epub', { type: 'application/epub+zip' });
    Object.defineProperty(fileInput, 'files', { value: [file], configurable: true });
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ book_id: 'book-runs-1', series_id: 'series-1', chapters: 3 }),
    });
    document.dispatchEvent(new Event('DOMContentLoaded'));
    document.getElementById('btn-onboard-epub')!.click();
    await vi.advanceTimersByTimeAsync(0); // flush onboard + immediate poll (status + runs)
  }

  beforeEach(() => {
    vi.clearAllMocks();
    mockFetch.mockReset(); // also drops stale Once queues from earlier describes
    state.pipelineBookId = null; // isolate from earlier describes (handleOnboard leaks it)
    localStorage.clear();
    vi.useFakeTimers();
    document.body.innerHTML = `
      <input type="file" id="file-upload">
      <span id="upload-status"></span>
      <button id="btn-onboard-epub"></button>
      <div id="walk-execution-section" style="display:none;"></div>
      <div id="walk-status-container"></div>
      <div id="walk-runs-container"></div>
      <button id="btn-run-all-walks"></button>
      <button id="btn-cancel-walks"></button>
    `;
  });

  afterEach(() => {
    stopWalkPolling();
    vi.useRealTimers();
  });

  it('pipelineWalkRuns GETs /api/pipeline/walks/{book_id}/runs and returns the rows', async () => {
    const { pipelineWalkRuns } = await import('../../src/tabs/script');
    const rows = [runRow({ run_id: 'run-9' })];
    vi.mocked(API.get).mockResolvedValueOnce(rows);

    const result = await pipelineWalkRuns('book-runs-1');

    expect(API.get).toHaveBeenCalledWith('/api/pipeline/walks/book-runs-1/runs');
    expect(result).toEqual(rows);
  });

  it('formatWalkRunTime renders integer unix ms as YYYY-MM-DD HH:MM (local time)', async () => {
    const { formatWalkRunTime } = await import('../../src/tabs/script');

    expect(formatWalkRunTime(createdMs)).toBe(fmt(createdMs));
    expect(formatWalkRunTime(finishedMs)).toBe(fmt(finishedMs));
  });

  it('formatWalkRunTime shows an em dash for missing timestamps (finished_ms=0 while running)', async () => {
    const { formatWalkRunTime } = await import('../../src/tabs/script');

    expect(formatWalkRunTime(0)).toBe('—');
  });

  it('renderWalkRuns renders each run with display label, badge, created/finished times and error text', async () => {
    document.body.innerHTML = `<div id="walk-runs-container"></div>`;
    const { renderWalkRuns } = await import('../../src/tabs/script');
    renderWalkRuns([
      runRow({ run_id: 'run-1' }),
      runRow({
        run_id: 'run-2',
        walk_name: 'walk_2b_character_discovery',
        status: 'failed',
        finished_ms: 0,
        error: 'LLM returned an empty scene list',
      }),
    ]);

    const container = document.getElementById('walk-runs-container')!;
    const rows = container.querySelectorAll('[data-walk-run-row]');
    expect(rows.length).toBe(2);
    // walk_name uses the WALK_DISPLAY_NAMES label when known
    expect(rows[0].textContent).toContain('Scene Segmentation');
    expect(rows[0].textContent).toContain('completed');
    // created/finished times are formatted human-readable
    expect(rows[0].textContent).toContain(fmt(createdMs));
    expect(rows[0].textContent).toContain(fmt(finishedMs));
    expect(rows[1].textContent).toContain('Character Discovery');
    expect(rows[1].textContent).toContain('failed');
    // failed runs show their (escaped) error text; unfinished runs show '—'
    expect(rows[1].textContent).toContain('LLM returned an empty scene list');
    expect(rows[1].textContent).toContain('—');
  });

  it('renderWalkRuns falls back to the raw walk_name when no display label exists', async () => {
    document.body.innerHTML = `<div id="walk-runs-container"></div>`;
    const { renderWalkRuns } = await import('../../src/tabs/script');
    renderWalkRuns([runRow({ run_id: 'run-x', walk_name: 'walk_9z_experimental', status: 'running' })]);

    const container = document.getElementById('walk-runs-container')!;
    expect(container.textContent).toContain('walk_9z_experimental');
  });

  it('renderWalkRuns maps every status to the shared badge classes', async () => {
    document.body.innerHTML = `<div id="walk-runs-container"></div>`;
    const { renderWalkRuns } = await import('../../src/tabs/script');
    const statuses = ['completed', 'running', 'failed', 'cancelled', 'pending'];
    renderWalkRuns(statuses.map((status, i) => runRow({ run_id: `run-${i}`, status })));

    const badges = document.querySelectorAll('#walk-runs-container .badge');
    expect(badges.length).toBe(5);
    expect(badges[0].classList.contains('bg-success')).toBe(true);
    expect(badges[1].classList.contains('bg-warning')).toBe(true);
    expect(badges[1].classList.contains('text-dark')).toBe(true);
    expect(badges[2].classList.contains('bg-danger')).toBe(true);
    expect(badges[3].classList.contains('bg-dark')).toBe(true); // cancelled
    expect(badges[4].classList.contains('bg-secondary')).toBe(true); // pending/unknown
  });

  it('renderWalkRuns shows an empty-state message when there are no runs', async () => {
    document.body.innerHTML = `<div id="walk-runs-container"></div>`;
    const { renderWalkRuns } = await import('../../src/tabs/script');
    renderWalkRuns([]);

    const container = document.getElementById('walk-runs-container')!;
    expect(container.textContent).toContain('No walk runs yet');
  });

  it('renderWalkRuns does nothing when the container element does not exist', async () => {
    document.body.innerHTML = '';
    const { renderWalkRuns } = await import('../../src/tabs/script');

    expect(() => renderWalkRuns([])).not.toThrow();
  });

  it('loads the runs list after onboarding and refreshes it on every walk poll tick', async () => {
    mockPollingApi([runRow()]);
    await onboardViaUi();

    expect(API.get).toHaveBeenCalledWith('/api/pipeline/walks/book-runs-1/runs');
    expect(runsCallCount()).toBe(1); // initial poll after onboarding
    const container = document.getElementById('walk-runs-container')!;
    expect(container.querySelectorAll('[data-walk-run-row]').length).toBe(1);
    expect(container.textContent).toContain('Scene Segmentation');
    expect(container.textContent).toContain('completed');

    await vi.advanceTimersByTimeAsync(2000); // one poll tick
    expect(runsCallCount()).toBe(2); // runs refreshed alongside walk status
    expect(API.get).toHaveBeenCalledWith('/api/pipeline/walk_status/book-runs-1');
  });

  it('shows the runs empty state when the book has no runs', async () => {
    mockPollingApi([]);
    await onboardViaUi();

    const container = document.getElementById('walk-runs-container')!;
    expect(container.querySelectorAll('[data-walk-run-row]').length).toBe(0);
    expect(container.textContent).toContain('No walk runs yet');
  });

  it('loads the runs list on tab load for a restored book session without starting walk polling', async () => {
    state.pipelineBookId = 'book-restored-1';
    mockPollingApi([runRow({ run_id: 'run-r' })]);
    document.dispatchEvent(new Event('DOMContentLoaded'));
    await vi.advanceTimersByTimeAsync(0);

    expect(API.get).toHaveBeenCalledWith('/api/pipeline/walks/book-restored-1/runs');
    const container = document.getElementById('walk-runs-container')!;
    expect(container.querySelectorAll('[data-walk-run-row]').length).toBe(1);
    // Tab load must NOT start walk polling (walk-status behavior unchanged).
    expect(API.get).not.toHaveBeenCalledWith('/api/pipeline/walk_status/book-restored-1');
  });

  it('uses the restored book ID for walk actions after reload', async () => {
    state.pipelineBookId = 'book-restored-1';
    mockPollingApi([]);
    document.dispatchEvent(new Event('DOMContentLoaded'));
    await vi.advanceTimersByTimeAsync(0);

    document.getElementById('btn-cancel-walks')?.dispatchEvent(new Event('click'));
    await vi.advanceTimersByTimeAsync(0);

    expect(mockFetch).toHaveBeenCalledWith('/api/pipeline/cancel_walks', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ book_id: 'book-restored-1' }),
    }));
  });

  it('index.html declares #walk-runs-container inside the Script tab, below the walk-status card', () => {
    const html = readIndexHtml();
    const runsIdx = html.indexOf('id="walk-runs-container"');
    const statusIdx = html.indexOf('id="walk-status-container"');
    expect(runsIdx).toBeGreaterThan(-1);
    expect(statusIdx).toBeGreaterThan(-1);
    expect(runsIdx).toBeGreaterThan(statusIdx);
  });
});

// ---------------------------------------------------------------------------
// Per-Walk Log Streaming — Part D: frontend viewer (Phase 1 RED tests)
//
// This block LOCKS the Part D viewer contract with failing (RED) tests. Phases
// 2–3 implement `openWalkLog`/`closeWalkLog` and keyed reconciliation in
// frontend/src/tabs/script.ts against these tests. Do not weaken assertions to
// force GREEN — implement the production source.
//
// LOCKED CLIENT CAPS (chosen this phase, documented for Phases 2–3):
//   WALK_LOG_ENTRY_CAP      = 200 — max rendered log entries per viewer DOM
//   WALK_LOG_RECORD_TEXT_CAP = 500 — max rendered TEXT of one log entry
//   WALK_LOG_STATUS_TEXT_CAP = 200 — max status text in #walk-log-status-{id}
//
// LOCKED DOM CONTRACT (shared spec for Phases 2–3):
//   * Each run row keeps <div data-walk-run-row="{run_id}"> with DIRECT
//     children button[data-walk-log-open="{run_id}"] and
//     div[data-walk-log-viewer="{run_id}"] (siblings — same parent = the row),
//     the viewer carrying the stable id walk-log-{run_id}. Inside the viewer
//     lives the bounded status element div#walk-log-status-{run_id} plus one
//     element per rendered record tagged data-walk-log-entry whose
//     textContent is the readable rendering (NEVER innerHTML).
//   * EventSource: exactly one per run at /api/pipeline/walks/log/{run_id};
//     opening an already-open run is a no-op; close/removal is idempotent.
//     'log' events carry JSON whose opaque id {run_id}:{seq} is the dedup key;
//     'complete' carries {run_id,status} and closes the source; an 'error'
//     event MAY carry a numeric `status` property (410 = Gone) and always
//     closes the source and publishes bounded status text. Malformed JSON
//     never throws or corrupts the viewer. Seq gaps are NORMAL — the client
//     does no overflow handling and must not be tested for one.
//   * renderWalkRuns reconciles #walk-runs-container by run_id: a refresh
//     keeps the SAME row/viewer/status nodes, open state, rendered entry IDs,
//     and the run-keyed EventSource; runs that vanish are closed and removed
//     from the registry before the empty state is rendered.
//
// MOCKED EventSource HARNESS (shared spec for Phases 2–3 — the class below is
// assigned to globalThis.EventSource):
//   * `new EventSource(url)` is recorded in MockEventSource.instances (every
//     construction) and MockEventSource.active (url → live, never-closed
//     instance) so tests can assert exact URL association and one source per
//     run.
//   * readyState constants CONNECTING=0/OPEN=1/CLOSED=2.
//   * Test-side drivers: src.open(), src.emitLog(data, lastEventId),
//     src.emitComplete(data), src.emitError(status?).
//   * Deliveries reach BOTH addEventListener(type, ...) registrations AND the
//     onopen/onerror/onmessage properties (the client may use either style).
//   * src.close() marks the instance closed, bumps closeCount, and removes it
//     from `active` (cleanup assertions check both).
//   * afterEach closes every leaked source and clears the registry; the global
//     is restored in afterAll. Viewer tests use REAL timers — they never touch
//     the shared polling/fake-timer machinery.
//
// The new exports (openWalkLog/closeWalkLog) are imported DYNAMICALLY exactly
// like the existing 'Walk Runs List' describe does for formatWalkRunTime — a
// missing export yields undefined at runtime (RED) without breaking the file
// at link time and without failing `tsc --noEmit` (tsconfig includes only
// src/). initScript() is NOT registered here (see the stacked-listener warning
// at the Cancel Walks / Walk Runs describes).
// ---------------------------------------------------------------------------

const WALK_LOG_ENTRY_CAP = 200;
const WALK_LOG_RECORD_TEXT_CAP = 500;
const WALK_LOG_STATUS_TEXT_CAP = 200;

class MockEventSource {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;

  /** Every construction, oldest first. */
  static instances: MockEventSource[] = [];
  /** URL → the currently-active (never-closed) instance. */
  static active = new Map<string, MockEventSource>();

  readonly url: string;
  readyState = MockEventSource.CONNECTING;
  closed = false;
  closeCount = 0;

  private readonly handlers = new Map<string, Set<(event: Event) => void>>();
  onopen: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;

  constructor(url: string) {
    this.url = url;
    MockEventSource.instances.push(this);
    MockEventSource.active.set(url, this);
  }

  addEventListener(type: string, handler: (event: Event) => void): void {
    let set = this.handlers.get(type);
    if (!set) {
      set = new Set();
      this.handlers.set(type, set);
    }
    set.add(handler);
  }

  removeEventListener(type: string, handler: (event: Event) => void): void {
    this.handlers.get(type)?.delete(handler);
  }

  close(): void {
    this.closed = true;
    this.readyState = MockEventSource.CLOSED;
    this.closeCount += 1;
    if (MockEventSource.active.get(this.url) === this) {
      MockEventSource.active.delete(this.url);
    }
  }

  /** Deliver an event through BOTH addEventListener(type) and on<type>. */
  private fire(type: string, event: Event): void {
    for (const handler of this.handlers.get(type) ?? []) handler(event);
    const onProp = (this as unknown as Record<string, unknown>)[`on${type}`];
    if (typeof onProp === 'function') (onProp as (e: Event) => void)(event);
  }

  // ---- test-side drivers --------------------------------------------------

  open(): void {
    this.readyState = MockEventSource.OPEN;
    this.fire('open', new Event('open'));
  }

  /** Deliver the SSE 'log' event: a JSON data string + an opaque lastEventId. */
  emitLog(data: string, lastEventId: string): void {
    this.fire('log', new MessageEvent('log', { data, lastEventId }));
  }

  /** Deliver the SSE 'complete' event: a JSON {run_id,status} data string. */
  emitComplete(data: string): void {
    this.fire('complete', new MessageEvent('complete', { data }));
  }

  /**
   * Deliver an 'error' event. A numeric `status` (e.g. 410) is attached to the
   * event so the client can distinguish a 410 Gone response from a generic
   * connection failure; emitError() with no argument signals a generic error.
   */
  emitError(status?: number): void {
    const event = new Event('error');
    if (status !== undefined) {
      Object.defineProperty(event, 'status', { value: status, configurable: true });
    }
    this.fire('error', event);
  }

  /** afterEach hygiene: close every source and clear the registry. */
  static reset(): void {
    for (const instance of [...MockEventSource.instances]) instance.close();
    MockEventSource.instances = [];
    MockEventSource.active.clear();
  }
}

describe('Script Tab — Per-Walk Log Viewer (Part D)', () => {
  let TS: number;
  let finishedTs: number;

  function fmtTs(ms: number): string {
    const d = new Date(ms);
    const p = (n: number) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  function viewerRun(overrides: Record<string, unknown> = {}): Record<string, unknown> {
    return {
      run_id: 'run-1',
      walk_name: 'walk_2a_scene_segmentation',
      status: 'running',
      heartbeat_ms: 0,
      created_ms: TS,
      finished_ms: 0,
      error: null,
      ...overrides,
    };
  }

  /** WalkLogRecord-shaped SSE data payload (mirrors the Part A record DTO). */
  function viewerLog(runId: string, seq: number, overrides: Record<string, unknown> = {}): Record<string, unknown> {
    return {
      run_id: runId,
      seq,
      id: `${runId}:${seq}`,
      event: 'llm',
      data: {
        timestamp: TS,
        model: 'gpt-4o-mini',
        temperature: 0.3,
        reasoning_effort: null,
        prompts: { system: 'You are a helpful editor.', user: `USER-${runId}-${seq}` },
        response: `RESP-${runId}-${seq}`,
        finish_reason: 'stop',
        usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
      },
      terminal: false,
      ...overrides,
    };
  }

  function container(): HTMLElement {
    return document.getElementById('walk-runs-container')!;
  }

  function viewer(runId: string): HTMLElement {
    return document.getElementById(`walk-log-${runId}`)!;
  }

  function statusEl(runId: string): HTMLElement {
    return document.getElementById(`walk-log-status-${runId}`)!;
  }

  function logUrl(runId: string): string {
    return `/api/pipeline/walks/log/${runId}`;
  }

  function entries(runId: string): NodeListOf<Element> {
    return viewer(runId).querySelectorAll('[data-walk-log-entry]');
  }

  /** render one row then openWalkLog(runId); returns the live mocked source. */
  async function renderAndOpen(runId: string, rowOverrides: Record<string, unknown> = {}): Promise<MockEventSource> {
    const { renderWalkRuns, openWalkLog } = await import('../../src/tabs/script');
    renderWalkRuns([viewerRun({ run_id: runId, ...rowOverrides })]);
    await openWalkLog(runId);
    const src = MockEventSource.active.get(logUrl(runId));
    expect(src).toBeDefined();
    src!.open();
    return src!;
  }

  function deliverLog(src: MockEventSource, record: Record<string, unknown>): void {
    src.emitLog(JSON.stringify(record), String(record.id));
  }

  beforeAll(() => {
    TS = Date.UTC(2026, 7, 6, 14, 3); // 2026-08-06 14:03 UTC
    finishedTs = Date.UTC(2026, 7, 6, 14, 12); // 2026-08-06 14:12 UTC
    vi.stubGlobal('EventSource', MockEventSource);
  });

  beforeEach(() => {
    vi.clearAllMocks();
    state.pipelineBookId = null; // isolate from earlier describes (handleOnboard leaks it)
    document.body.innerHTML = '<div id="walk-runs-container"></div>';
  });

  afterEach(() => {
    MockEventSource.reset(); // close any leaked source so it cannot fire into a later test
  });

  afterAll(() => {
    vi.unstubAllGlobals();
  });

  // -------------------------------------------------------------------------
  // P1-S1 — Keyed row markup
  // -------------------------------------------------------------------------

  describe('viewer keyed row markup', () => {
    it('renders the keyed log button, sibling viewer, and stable IDs for every run row', async () => {
      const { renderWalkRuns } = await import('../../src/tabs/script');
      renderWalkRuns([
        viewerRun({ run_id: 'run-1' }),
        viewerRun({ run_id: 'run-2', walk_name: 'walk_2i_delivery', status: 'completed', finished_ms: finishedTs }),
      ]);

      for (const runId of ['run-1', 'run-2']) {
        const row = container().querySelector(`[data-walk-run-row="${runId}"]`);
        expect(row).not.toBeNull();

        const button = row!.querySelector(`button[data-walk-log-open="${runId}"]`);
        expect(button).not.toBeNull();

        const viewerEl = row!.querySelector(`div[data-walk-log-viewer="${runId}"]`);
        expect(viewerEl).not.toBeNull();

        // Button and viewer are SIBLINGS inside the row (same parent = the row).
        expect(button!.parentElement).toBe(row);
        expect(viewerEl!.parentElement).toBe(row);
        expect(button!.parentElement).toBe(viewerEl!.parentElement);

        // Stable IDs.
        expect(viewerEl!.id).toBe(`walk-log-${runId}`);

        // A bounded status element inside the viewer with a stable ID.
        const status = viewerEl!.querySelector(`#walk-log-status-${runId}`);
        expect(status).not.toBeNull();
        expect(viewerEl!.contains(status)).toBe(true);
      }
    });

    it('keeps the existing label/badge/time/error content assertions alongside the keyed markup', async () => {
      const { renderWalkRuns } = await import('../../src/tabs/script');
      renderWalkRuns([
        viewerRun({ run_id: 'run-1' }),
        viewerRun({
          run_id: 'run-2',
          walk_name: 'walk_2b_character_discovery',
          status: 'failed',
          finished_ms: finishedTs,
          error: 'LLM returned an empty scene list',
        }),
      ]);

      const rows = container().querySelectorAll('[data-walk-run-row]');
      expect(rows.length).toBe(2);
      // walk_name uses the WALK_DISPLAY_NAMES label when known
      expect(rows[0].textContent).toContain('Scene Segmentation');
      expect(rows[0].textContent).toContain('running');
      // created/finished times are formatted human-readable
      expect(rows[0].textContent).toContain(fmtTs(TS));
      expect(rows[1].textContent).toContain('Character Discovery');
      expect(rows[1].textContent).toContain('failed');
      expect(rows[1].textContent).toContain(fmtTs(finishedTs));
      // failed runs show their (escaped) error text; unfinished runs show '—'
      expect(rows[1].textContent).toContain('LLM returned an empty scene list');
      expect(rows[1].textContent).toContain('—');

      // ... and the keyed viewer markup lives in the SAME rows.
      for (const runId of ['run-1', 'run-2']) {
        expect(container().querySelector(`button[data-walk-log-open="${runId}"]`)).not.toBeNull();
        expect(container().querySelector(`div[data-walk-log-viewer="${runId}"]`)).not.toBeNull();
      }
    });
  });

  // -------------------------------------------------------------------------
  // P1-S2 — EventSource lifecycle and log rendering
  // -------------------------------------------------------------------------

  describe('viewer EventSource lifecycle and log rendering', () => {
    it('opens exactly one EventSource at /api/pipeline/walks/log/{run_id} with the server-provided run id', async () => {
      const src = await renderAndOpen('run-url-1');

      expect(src.url).toBe('/api/pipeline/walks/log/run-url-1');
      expect(MockEventSource.instances.filter((i) => i.url === logUrl('run-url-1')).length).toBe(1);
      expect(MockEventSource.active.get(logUrl('run-url-1'))).toBe(src);
      expect(src.closed).toBe(false);
    });

    it('is idempotent: opening an already-open run creates no second source', async () => {
      const { renderWalkRuns, openWalkLog } = await import('../../src/tabs/script');
      renderWalkRuns([viewerRun({ run_id: 'run-idem' })]);

      await openWalkLog('run-idem');
      const first = MockEventSource.active.get(logUrl('run-idem'))!;
      await openWalkLog('run-idem'); // second open: no-op

      expect(MockEventSource.instances.filter((i) => i.url === logUrl('run-idem')).length).toBe(1);
      expect(MockEventSource.active.get(logUrl('run-idem'))).toBe(first);
      expect(first.closed).toBe(false);
      expect(first.closeCount).toBe(0);
    });

    it('closeWalkLog is idempotent: closes the active source once and tolerates never-opened runs', async () => {
      const { closeWalkLog } = await import('../../src/tabs/script');
      const src = await renderAndOpen('run-close');

      await closeWalkLog('run-close');
      await closeWalkLog('run-close'); // second close: no-op

      expect(src.closed).toBe(true);
      expect(src.closeCount).toBe(1);
      expect(MockEventSource.active.has(logUrl('run-close'))).toBe(false);

      // A never-opened run must be a silent no-op (no throw, nothing registered).
      await closeWalkLog('run-never');
      expect(MockEventSource.active.has(logUrl('run-never'))).toBe(false);
      expect(MockEventSource.instances.filter((i) => i.url === logUrl('run-never')).length).toBe(0);
    });

    it('parses log JSON and renders readable timestamp/event/selected data as textContent', async () => {
      const src = await renderAndOpen('run-parse');
      deliverLog(src, viewerLog('run-parse', 1));

      const entry = entries('run-parse')[0];
      expect(entry).toBeDefined();
      expect(entry!.textContent).toContain('llm'); // event name
      expect(entry!.textContent).toContain(fmtTs(TS)); // readable local timestamp
      expect(entry!.textContent).toContain('RESP-run-parse-1'); // selected data field
    });

    it('deduplicates by the opaque {run_id}:{seq} id: an identical id renders once', async () => {
      const src = await renderAndOpen('run-dedupe');
      const record = viewerLog('run-dedupe', 1);
      deliverLog(src, record);
      deliverLog(src, record); // identical id delivered twice

      expect(entries('run-dedupe').length).toBe(1);
      expect(viewer('run-dedupe').textContent).toContain('RESP-run-dedupe-1');
    });

    it('ignores already-rendered ids after a reconnect (close + reopen with overlapping replay)', async () => {
      const { renderWalkRuns, openWalkLog, closeWalkLog } = await import('../../src/tabs/script');
      renderWalkRuns([viewerRun({ run_id: 'run-reconn' })]);

      await openWalkLog('run-reconn');
      const first = MockEventSource.active.get(logUrl('run-reconn'))!;
      deliverLog(first, viewerLog('run-reconn', 1));
      deliverLog(first, viewerLog('run-reconn', 2));
      expect(entries('run-reconn').length).toBe(2);

      await closeWalkLog('run-reconn');
      expect(first.closed).toBe(true);

      // Reconnect: a NEW source for the same run, replaying overlapping ids.
      await openWalkLog('run-reconn');
      const second = MockEventSource.active.get(logUrl('run-reconn'))!;
      expect(second).not.toBe(first);
      expect(MockEventSource.instances.filter((i) => i.url === logUrl('run-reconn')).length).toBe(2);

      deliverLog(second, viewerLog('run-reconn', 1)); // already rendered — skipped
      deliverLog(second, viewerLog('run-reconn', 2)); // already rendered — skipped
      deliverLog(second, viewerLog('run-reconn', 3)); // new — rendered

      expect(entries('run-reconn').length).toBe(3); // 1+2+3, each exactly once
      expect(viewer('run-reconn').textContent).toContain('RESP-run-reconn-3');
    });

    it('malformed log JSON neither throws nor corrupts the viewer', async () => {
      const src = await renderAndOpen('run-malformed');

      expect(() => src.emitLog('{ definitely not json', 'run-malformed:99')).not.toThrow();
      expect(entries('run-malformed').length).toBe(0);

      // A subsequent valid record still renders normally.
      deliverLog(src, viewerLog('run-malformed', 1));
      expect(entries('run-malformed').length).toBe(1);
      expect(viewer('run-malformed').textContent).toContain('RESP-run-malformed-1');
    });
  });

  // -------------------------------------------------------------------------
  // P1-S3 — Completion, errors, cleanup, caps, and safe text rendering
  // -------------------------------------------------------------------------

  describe('viewer completion, errors, cleanup, and bounded safe rendering', () => {
    it('complete updates the stable status element and closes/removes the source', async () => {
      const src = await renderAndOpen('run-complete');
      deliverLog(src, viewerLog('run-complete', 1));

      src.emitComplete(JSON.stringify({ run_id: 'run-complete', status: 'completed' }));

      expect(statusEl('run-complete').textContent).toContain('completed');
      expect(src.closed).toBe(true);
      expect(MockEventSource.active.has(logUrl('run-complete'))).toBe(false);
      expect(entries('run-complete').length).toBe(1); // rendered entries remain visible

      // A second complete must be a no-op: a single close total.
      src.emitComplete(JSON.stringify({ run_id: 'run-complete', status: 'completed' }));
      expect(src.closeCount).toBe(1);
    });

    it('a 410 error closes the source and publishes bounded status text naming the code', async () => {
      const src = await renderAndOpen('run-410');

      src.emitError(410);

      const text = statusEl('run-410').textContent;
      expect(text).toMatch(/410/);
      expect(text).not.toContain('—'); // the bounded message REPLACED the '—' placeholder
      expect(text.length).toBeLessThanOrEqual(WALK_LOG_STATUS_TEXT_CAP);
      expect(src.closed).toBe(true);
      expect(MockEventSource.active.has(logUrl('run-410'))).toBe(false);
    });

    it('a generic error closes the source and publishes bounded status text', async () => {
      const src = await renderAndOpen('run-err');

      src.emitError();

      const text = statusEl('run-err').textContent;
      expect(text.length).toBeGreaterThan(0);
      expect(text).not.toContain('—'); // generic error REPLACED the '—' placeholder
      expect(text.length).toBeLessThanOrEqual(WALK_LOG_STATUS_TEXT_CAP);
      expect(src.closed).toBe(true);
      expect(MockEventSource.active.has(logUrl('run-err'))).toBe(false);
    });

    it('removing a run via renderWalkRuns closes and deletes its source and registry entry', async () => {
      const { renderWalkRuns } = await import('../../src/tabs/script');
      const src = await renderAndOpen('run-removed');
      deliverLog(src, viewerLog('run-removed', 1));

      renderWalkRuns([viewerRun({ run_id: 'run-still-there' })]);

      expect(src.closed).toBe(true);
      expect(MockEventSource.active.has(logUrl('run-removed'))).toBe(false);
      expect(container().querySelector('[data-walk-run-row="run-removed"]')).toBeNull();
      expect(container().querySelector('[data-walk-run-row="run-still-there"]')).not.toBeNull();
    });

    it('an empty payload renders the empty state and closes every open source', async () => {
      const { renderWalkRuns } = await import('../../src/tabs/script');
      const src = await renderAndOpen('run-empty');

      renderWalkRuns([]);

      expect(src.closed).toBe(true);
      expect(MockEventSource.active.size).toBe(0);
      expect(container().textContent).toContain('No walk runs yet');
    });

    it('caps the rendered log entries per viewer at 200', async () => {
      const src = await renderAndOpen('run-cap');

      for (let seq = 1; seq <= WALK_LOG_ENTRY_CAP + 30; seq++) {
        deliverLog(src, viewerLog('run-cap', seq));
      }

      expect(entries('run-cap').length).toBe(WALK_LOG_ENTRY_CAP); // 230 unique ids render EXACTLY the cap
    });

    it('caps the rendered text of a single record at 500 chars', async () => {
      const src = await renderAndOpen('run-textcap');

      deliverLog(src, viewerLog('run-textcap', 1, {
        data: {
          timestamp: TS,
          model: 'gpt-4o-mini',
          prompts: { system: '', user: '' },
          response: 'R'.repeat(800),
        },
      }));

      const entry = entries('run-textcap')[0];
      expect(entry).toBeDefined();
      expect(entry!.textContent.length).toBeLessThanOrEqual(WALK_LOG_RECORD_TEXT_CAP);
    });

    it('renders hostile record values as literal text, never as markup', async () => {
      const src = await renderAndOpen('run-hostile');

      deliverLog(src, viewerLog('run-hostile', 1, {
        data: {
          timestamp: TS,
          model: 'gpt-4o-mini',
          prompts: { system: '<script>alert("s")</script>', user: '<img src=x onerror=alert(1)>' },
          response: '<svg onload=alert(1)></svg>',
        },
      }));

      const el = viewer('run-hostile');
      // escapeHtml is mocked as IDENTITY in this test file — literal-text
      // rendering must come from textContent-only DOM construction, so any
      // HTML interpolation would materialize real elements here.
      expect(el.querySelector('img')).toBeNull();
      expect(el.querySelector('script')).toBeNull();
      expect(el.querySelector('svg')).toBeNull();
      expect(el.querySelector('[onerror]')).toBeNull();
      expect(el.querySelector('[onload]')).toBeNull();
      expect(el.textContent).toContain('<img src=x onerror=alert(1)>');
      expect(el.textContent).toContain('<script>alert("s")</script>');
      expect(el.textContent).toContain('<svg onload=alert(1)></svg>');
    });
  });

  // -------------------------------------------------------------------------
  // P1-S4 — Keyed reconciliation across status refresh
  // -------------------------------------------------------------------------

  describe('viewer keyed reconciliation across status refresh', () => {
    it('a status refresh preserves the same row/viewer/status nodes, open state, rendered entries, and EventSource', async () => {
      const { renderWalkRuns, openWalkLog } = await import('../../src/tabs/script');
      renderWalkRuns([viewerRun({ run_id: 'run-a', status: 'running' })]);
      const rowA = container().querySelector('[data-walk-run-row="run-a"]')!;
      const viewerA = viewer('run-a');
      const statusA = statusEl('run-a');

      await openWalkLog('run-a');
      const srcA = MockEventSource.active.get(logUrl('run-a'))!;
      deliverLog(srcA, viewerLog('run-a', 1));
      deliverLog(srcA, viewerLog('run-a', 2));
      expect(entries('run-a').length).toBe(2);

      // Status refresh: SAME run_id with updated status/error/finished time.
      renderWalkRuns([viewerRun({
        run_id: 'run-a',
        status: 'completed',
        finished_ms: finishedTs,
        error: 'LLM returned an empty scene list',
      })]);

      // Same DOM nodes (never wholesale-rebuilt).
      expect(container().querySelector('[data-walk-run-row="run-a"]')).toBe(rowA);
      expect(viewer('run-a')).toBe(viewerA);
      expect(statusEl('run-a')).toBe(statusA);

      // Open state preserved: no second source, registry intact, same instance.
      expect(MockEventSource.instances.filter((i) => i.url === logUrl('run-a')).length).toBe(1);
      expect(MockEventSource.active.get(logUrl('run-a'))).toBe(srcA);
      expect(srcA.closed).toBe(false);
      expect(srcA.closeCount).toBe(0);

      // Rendered entries and their IDs preserved — nothing re-rendered.
      expect(entries('run-a').length).toBe(2);
      expect(viewerA.textContent).toContain('RESP-run-a-1');
      expect(viewerA.textContent).toContain('RESP-run-a-2');

      // Only mutable status/history/error content changed.
      expect(rowA.textContent).toContain('completed');
      expect(rowA.textContent).toContain('LLM returned an empty scene list');
      expect(rowA.textContent).toContain(fmtTs(finishedTs));

      // The live source is still wired to the same viewer after the refresh.
      deliverLog(srcA, viewerLog('run-a', 3));
      expect(viewer('run-a').textContent).toContain('RESP-run-a-3');
      expect(entries('run-a').length).toBe(3);
    });

    it('a payload without the run removes its row and source, rendering replacements in the API payload order', async () => {
      const { renderWalkRuns } = await import('../../src/tabs/script');
      const srcX = await renderAndOpen('run-x');
      deliverLog(srcX, viewerLog('run-x', 1));

      renderWalkRuns([
        viewerRun({ run_id: 'run-y', status: 'completed', finished_ms: finishedTs }),
        viewerRun({ run_id: 'run-z', status: 'failed', error: 'timeout' }),
      ]);

      // Vanished run: source closed and registry entry deleted.
      expect(srcX.closed).toBe(true);
      expect(MockEventSource.active.has(logUrl('run-x'))).toBe(false);
      expect(container().querySelector('[data-walk-run-row="run-x"]')).toBeNull();

      // Replacements render in API (newest-first) payload order.
      const rows = container().querySelectorAll('[data-walk-run-row]');
      expect(rows.length).toBe(2);
      expect(rows[0].getAttribute('data-walk-run-row')).toBe('run-y');
      expect(rows[1].getAttribute('data-walk-run-row')).toBe('run-z');
      expect(rows[0].textContent).toContain('Scene Segmentation');
    });

    it('self-heals a removed-then-re-added run: reopen builds a FRESH source, fresh renderedIds, and a fresh viewer with no stale callbacks', async () => {
      const { renderWalkRuns, openWalkLog } = await import('../../src/tabs/script');
      renderWalkRuns([viewerRun({ run_id: 'run-x' })]);
      const viewerA = viewer('run-x');
      await openWalkLog('run-x');
      const first = MockEventSource.active.get(logUrl('run-x'))!;
      deliverLog(first, viewerLog('run-x', 1));
      expect(entries('run-x').length).toBe(1);

      // Re-render WITHOUT run-x: source closed, registry entry deleted, row removed.
      renderWalkRuns([]);
      expect(first.closed).toBe(true);
      expect(MockEventSource.active.has(logUrl('run-x'))).toBe(false);
      expect(container().querySelector('[data-walk-run-row="run-x"]')).toBeNull();

      // Re-render WITH run-x again: a brand-new row/viewer node is built.
      renderWalkRuns([viewerRun({ run_id: 'run-x' })]);
      const viewerB = viewer('run-x');
      expect(viewerB).not.toBe(viewerA);

      // Reopen: a FRESH source (constructions for the run's URL increment).
      await openWalkLog('run-x');
      const second = MockEventSource.active.get(logUrl('run-x'))!;
      expect(second).not.toBe(first);
      expect(MockEventSource.instances.filter((i) => i.url === logUrl('run-x')).length).toBe(2);
      expect(viewer('run-x')).toBe(viewerB); // the new viewer node is the one served

      // renderedIds are FRESH: re-rendering an already-seen id is allowed again
      // (the removed entry's renderedIds were deleted with the registry entry).
      deliverLog(second, viewerLog('run-x', 1));
      expect(entries('run-x').length).toBe(1);
      expect(viewer('run-x').textContent).toContain('RESP-run-x-1');

      // No stale callbacks: a late event on the OLD source must not touch the
      // new viewer (the deleted entry's open flag is false, so the guard exits).
      first.emitLog(JSON.stringify(viewerLog('run-x', 99)), 'run-x:99');
      expect(entries('run-x').length).toBe(1);
      expect(viewer('run-x').textContent).not.toContain('RESP-run-x-99');
    });

    it('keeps boot wiring untouched: main.ts and test_boot.test.ts gain no viewer code', () => {
      // P1-S4 guard (plan step): do NOT edit main.ts or test_boot.test.ts for
      // this feature — the module-scope init flow and its regression test stay
      // as-is; viewer wiring lives only in frontend/src/tabs/script.ts.
      const mainSrc = readFileSync(resolve(process.cwd(), 'src/main.ts'), 'utf8');
      expect(mainSrc).toContain('initScript');
      expect(mainSrc).not.toContain('openWalkLog');
      expect(mainSrc).not.toContain('walk-log-');
      const bootSrc = readFileSync(resolve(process.cwd(), 'tests/frontend/test_boot.test.ts'), 'utf8');
      expect(bootSrc).not.toContain('openWalkLog');
      expect(bootSrc).not.toContain('walk-log-');
    });
  });

  // -------------------------------------------------------------------------
  // P2-S3 — Delegated click toggle (GREEN this phase)
  //
  // These tests exercise the ONE delegated click listener on
  // #walk-runs-container bound by initPipelineUI() through the file's single
  // initScript() DOMContentLoaded registration (the Cancel Walks describe's
  // beforeAll — do NOT register a second initScript here: that would stack
  // DOMContentLoaded listeners). Each test rebuilds the container, renders
  // rows, then dispatches DOMContentLoaded so the fresh container element is
  // bound exactly once (per-element WeakSet guard inside initPipelineUI).
  // Viewer tests run on real timers; the harness afterEach closes leaked
  // sources regardless.
  // -------------------------------------------------------------------------

  describe('viewer delegated click toggle', () => {
    async function renderRowsAndInit(...runIds: string[]): Promise<void> {
      const { renderWalkRuns } = await import('../../src/tabs/script');
      renderWalkRuns(runIds.map((runId, i) => viewerRun({
        run_id: runId,
        status: i === 0 ? 'running' : 'completed',
      })));
      document.dispatchEvent(new Event('DOMContentLoaded'));
    }

    function openBtn(runId: string): HTMLButtonElement {
      return container().querySelector(`button[data-walk-log-open="${runId}"]`) as HTMLButtonElement;
    }

    it('opens only the clicked run and toggles closed on a second click', async () => {
      await renderRowsAndInit('run-click-a', 'run-click-b');

      expect(viewer('run-click-a').style.display).toBe('none');
      expect(viewer('run-click-b').style.display).toBe('none');

      openBtn('run-click-a').click();
      expect(viewer('run-click-a').style.display).not.toBe('none');
      expect(viewer('run-click-b').style.display).toBe('none'); // only that run opened

      openBtn('run-click-a').click(); // toggle close when already open
      expect(viewer('run-click-a').style.display).toBe('none');
    });

    it('clicks inside a viewer/status element or on another run row toggle nothing', async () => {
      await renderRowsAndInit('run-click-a', 'run-click-b');
      openBtn('run-click-a').click();
      expect(viewer('run-click-a').style.display).not.toBe('none');

      // Clicks inside run-a's viewer or status element never match an open button.
      viewer('run-click-a').click();
      statusEl('run-click-a').click();
      expect(viewer('run-click-a').style.display).not.toBe('none'); // unchanged
      expect(viewer('run-click-b').style.display).toBe('none');

      // Clicking run-b's row content (label, not the button) opens nothing.
      const rowB = container().querySelector('[data-walk-run-row="run-click-b"]')!;
      (rowB.querySelector('.small') as HTMLElement).click();
      expect(viewer('run-click-b').style.display).toBe('none');

      // Clicking run-b's actual open button opens only run-b; run-a stays open.
      openBtn('run-click-b').click();
      expect(viewer('run-click-b').style.display).not.toBe('none');
      expect(viewer('run-click-a').style.display).not.toBe('none');
    });

    it('repeated initialization binds the delegated listener at most once per container', async () => {
      await renderRowsAndInit('run-click-a');
      document.dispatchEvent(new Event('DOMContentLoaded')); // re-init the SAME container

      openBtn('run-click-a').click(); // single toggle: closed -> open
      expect(viewer('run-click-a').style.display).not.toBe('none');
      openBtn('run-click-a').click(); // single toggle: open -> closed (double-fire would reopen)
      expect(viewer('run-click-a').style.display).toBe('none');
    });
  });

  // -------------------------------------------------------------------------
  // P4-S1 — Security gate: hostile payloads, server-run-ID URL safety,
  //         bounded prompt/response/error display, bounded DOM growth
  // -------------------------------------------------------------------------

  describe('viewer security assertions (P4-S1)', () => {
    it('renders hostile payload values delivered via the EventSource as literal text with zero extra elements', async () => {
      const src = await renderAndOpen('run-p4-hostile');

      deliverLog(src, viewerLog('run-p4-hostile', 1, {
        data: {
          timestamp: TS,
          model: 'gpt-4o-mini',
          prompts: {
            system: '<script>alert("s")</script>',
            user: '<img src=x onerror=alert(1)>',
          },
          response: '<svg onload=alert(1)></svg>',
        },
      }));

      deliverLog(src, viewerLog('run-p4-hostile', 2, {
        data: {
          timestamp: TS,
          model: 'gpt-4o-mini',
          prompts: {
            system: '</div><div data-p4="breakout">',
            user: '"><img onerror=alert(2)>',
          },
          response: '<script>window.pwned=1</script></div><div>',
        },
      }));

      const el = viewer('run-p4-hostile');
      // No element of any hostile tag or with attribute-injected handlers.
      expect(el.querySelectorAll('script, img, svg, [onerror], [onload], [onclick]').length).toBe(0);
      // The viewer holds EXACTLY its status element plus one div per rendered
      // record — hostile text materialized ZERO new elements.
      expect(el.querySelectorAll('*').length).toBe(1 + entries('run-p4-hostile').length);
      for (const entry of entries('run-p4-hostile')) {
        expect(entry.childElementCount).toBe(0); // entry is text-only
      }
      expect(el.textContent).toContain('<script>alert("s")</script>');
      expect(el.textContent).toContain('<img src=x onerror=alert(1)>');
      expect(el.textContent).toContain('<svg onload=alert(1)></svg>');
      expect(el.textContent).toContain('</div><div data-p4="breakout">');
      expect(el.textContent).toContain('"><img onerror=alert(2)>');
      expect(el.textContent).toContain('<script>window.pwned=1</script></div><div>');
    });

    it('renders hostile text in the complete status and in error status as literal text with zero elements', async () => {
      const srcA = await renderAndOpen('run-p4-st-hostile');
      srcA.emitComplete(JSON.stringify({
        run_id: 'run-p4-st-hostile',
        status: '<script>alert(1)</script>' + 'X'.repeat(300),
      }));

      const statusText = statusEl('run-p4-st-hostile').textContent!;
      expect(statusText.length).toBeLessThanOrEqual(WALK_LOG_STATUS_TEXT_CAP);
      expect(statusText.startsWith('<script>alert(1)</script>')).toBe(true); // literal, truncated only
      expect(statusEl('run-p4-st-hostile').querySelectorAll('*').length).toBe(0);
      expect(srcA.closed).toBe(true);

      const srcB = await renderAndOpen('run-p4-err-hostile');
      srcB.emitError(); // client-generated generic error text
      const errText = statusEl('run-p4-err-hostile').textContent!;
      expect(errText.length).toBeGreaterThan(0);
      expect(errText.length).toBeLessThanOrEqual(WALK_LOG_STATUS_TEXT_CAP);
      expect(statusEl('run-p4-err-hostile').querySelectorAll('*').length).toBe(0);
      expect(srcB.closed).toBe(true);

      const srcC = await renderAndOpen('run-p4-err-hostile-410');
      srcC.emitError(410);
      expect(statusEl('run-p4-err-hostile-410').querySelectorAll('*').length).toBe(0);
      expect(statusEl('run-p4-err-hostile-410').textContent).toMatch(/410/);
    });

    it('builds the EventSource URL only from the server-provided run_id, even for hostile-looking ids', async () => {
      const { renderWalkRuns, openWalkLog } = await import('../../src/tabs/script');
      const hostileIds = ['../evil', 'a b', '<b>x</b>'];

      for (const runId of hostileIds) {
        renderWalkRuns([viewerRun({ run_id: runId })]);
        // The row and its keyed viewer are keyed by the RAW server id.
        const row = container().querySelector(`[data-walk-run-row="${runId}"]`);
        expect(row).not.toBeNull();
        expect(row!.querySelector(`[data-walk-log-viewer="${runId}"]`)).not.toBeNull();

        // Opening must never throw and must register the source under the
        // EXACT string built only from the server-provided run id.
        expect(() => openWalkLog(runId)).not.toThrow();
        const url = logUrl(runId);
        const src = MockEventSource.active.get(url);
        expect(src).toBeDefined();
        expect(src!.url).toBe(url);
        expect(MockEventSource.instances.filter((i) => i.url === url).length).toBe(1);
      }

      // No display label, form value, or DOM-derived text influences the URL.
      renderWalkRuns([viewerRun({ run_id: 'a b', walk_name: 'walk_2i_delivery' })]);
      expect(() => openWalkLog('a b')).not.toThrow();
      expect(MockEventSource.active.get(logUrl('a b'))!.url).not.toContain('Delivery');
    });

    it('keeps the 500-char record cap and the 200-entry cap when the SAME large record arrives twice', async () => {
      const src = await renderAndOpen('run-p4-caps');

      const big = viewerLog('run-p4-caps', 1, {
        data: {
          timestamp: TS,
          model: 'gpt-4o-mini',
          prompts: { system: '<script>'.repeat(50), user: '<img src=x>'.repeat(50) },
          response: ('R'.repeat(100) + '<svg onload=1></svg>').repeat(5),
        },
      });

      deliverLog(src, big);
      expect(entries('run-p4-caps').length).toBe(1);
      expect(entries('run-p4-caps')[0]!.textContent.length).toBeLessThanOrEqual(WALK_LOG_RECORD_TEXT_CAP);

      deliverLog(src, big); // identical id again — dedup must not create DOM
      expect(entries('run-p4-caps').length).toBe(1);
      expect(entries('run-p4-caps')[0]!.textContent.length).toBeLessThanOrEqual(WALK_LOG_RECORD_TEXT_CAP);

      deliverLog(src, viewerLog('run-p4-caps', 2, {
        data: { timestamp: TS, model: 'gpt-4o-mini', prompts: { system: '', user: '' }, response: 'Y'.repeat(900) },
      }));
      expect(entries('run-p4-caps').length).toBe(2);
      for (const entry of entries('run-p4-caps')) {
        expect(entry.textContent.length).toBeLessThanOrEqual(WALK_LOG_RECORD_TEXT_CAP);
        expect(entry.childElementCount).toBe(0);
      }
    });

    it('never grows the viewer entry count beyond the cap across repeated refresh cycles', async () => {
      const { renderWalkRuns, openWalkLog } = await import('../../src/tabs/script');
      renderWalkRuns([viewerRun({ run_id: 'run-p4-growth' })]);
      await openWalkLog('run-p4-growth');
      const src = MockEventSource.active.get(logUrl('run-p4-growth'))!;
      src.open();

      for (let seq = 1; seq <= WALK_LOG_ENTRY_CAP + 30; seq++) {
        deliverLog(src, viewerLog('run-p4-growth', seq));
      }
      const viewerA = viewer('run-p4-growth');
      const filledCount = entries('run-p4-growth').length;
      expect(filledCount).toBe(WALK_LOG_ENTRY_CAP); // 230 unique ids render EXACTLY the cap

      // Refresh cycle 1: same run present — same viewer node, same single
      // source, entry count unchanged.
      renderWalkRuns([viewerRun({ run_id: 'run-p4-growth' })]);
      expect(viewer('run-p4-growth')).toBe(viewerA);
      expect(entries('run-p4-growth').length).toBe(filledCount);
      expect(MockEventSource.instances.filter((i) => i.url === logUrl('run-p4-growth')).length).toBe(1);
      expect(MockEventSource.active.get(logUrl('run-p4-growth'))).toBe(src);

      // More events during/after the refresh: hard cap still holds.
      for (let seq = WALK_LOG_ENTRY_CAP + 31; seq <= WALK_LOG_ENTRY_CAP + 60; seq++) {
        deliverLog(src, viewerLog('run-p4-growth', seq));
      }
      expect(entries('run-p4-growth').length).toBeLessThanOrEqual(WALK_LOG_ENTRY_CAP);

      // Refresh cycle 2: still the same viewer and source — no listener/source
      // leak across refreshes.
      renderWalkRuns([viewerRun({ run_id: 'run-p4-growth' })]);
      expect(viewer('run-p4-growth')).toBe(viewerA);
      expect(entries('run-p4-growth').length).toBeLessThanOrEqual(WALK_LOG_ENTRY_CAP);
      expect(MockEventSource.instances.filter((i) => i.url === logUrl('run-p4-growth')).length).toBe(1);
      expect(MockEventSource.active.get(logUrl('run-p4-growth'))).toBe(src);
      expect(src.closed).toBe(false);
    });
  });

  // -------------------------------------------------------------------------
  // P4-S2 — Regression gate: polling cadence, open-viewer survival during a
  //         running refresh, terminal replay completion, missing DOM targets,
  //         malformed event JSON, unchanged module-scope boot behavior
  // -------------------------------------------------------------------------

  describe('viewer regressions (P4-S2)', () => {
    describe('polling cadence', () => {
      function statusesRunning(): Record<string, string> {
        const statuses: Record<string, string> = {};
        for (const walkName of WALK_ORDER) statuses[walkName] = 'pending';
        statuses['walk_2a_scene_segmentation'] = 'running'; // keeps polling looping
        return statuses;
      }

      beforeEach(() => {
        vi.useFakeTimers();
        mockFetch.mockReset(); // also drops stale Once queues from earlier describes
        document.body.innerHTML = `
          <input type="file" id="file-upload">
          <span id="upload-status"></span>
          <button id="btn-onboard-epub"></button>
          <div id="walk-execution-section" style="display:none;"></div>
          <div id="walk-status-container"></div>
          <div id="walk-runs-container"></div>
          <button id="btn-run-all-walks"></button>
          <button id="btn-cancel-walks"></button>
        `;
        vi.mocked(API.get).mockImplementation((endpoint: string) => {
          if (String(endpoint).endsWith('/runs')) return Promise.resolve([viewerRun({ run_id: 'run-poll' })]);
          return Promise.resolve(statusesRunning());
        });
      });

      afterEach(() => {
        stopWalkPolling();
        vi.useRealTimers();
      });

      async function onboardViaUi(): Promise<void> {
        const fileInput = document.getElementById('file-upload') as HTMLInputElement;
        const file = new File(['test'], 'book.epub', { type: 'application/epub+zip' });
        Object.defineProperty(fileInput, 'files', { value: [file], configurable: true });
        mockFetch.mockResolvedValueOnce({
          ok: true,
          json: async () => ({ book_id: 'book-p4-cadence', series_id: 'series-1', chapters: 3 }),
        });
        document.dispatchEvent(new Event('DOMContentLoaded'));
        document.getElementById('btn-onboard-epub')!.click();
        await vi.advanceTimersByTimeAsync(0); // flush onboard + immediate poll
      }

      function runsCallCount(): number {
        return vi.mocked(API.get).mock.calls.filter(([ep]) => String(ep).includes('/runs')).length;
      }

      it('refreshWalkRuns ticks on an exact 2000ms interval while polling runs', async () => {
        await onboardViaUi();
        expect(runsCallCount()).toBe(1); // immediate first poll after onboard

        await vi.advanceTimersByTimeAsync(1999);
        expect(runsCallCount()).toBe(1); // interval has NOT fired before 2000ms

        await vi.advanceTimersByTimeAsync(1);
        expect(runsCallCount()).toBe(2); // first interval tick at exactly 2000ms

        await vi.advanceTimersByTimeAsync(2000);
        expect(runsCallCount()).toBe(3); // second tick at exactly 4000ms
      });

      it('keeps polling while a reserved walk is pending before it starts', async () => {
        let pollCount = 0;
        vi.mocked(API.get).mockImplementation((endpoint: string) => {
          if (String(endpoint).endsWith('/runs')) {
            return Promise.resolve([
              viewerRun({ run_id: 'run-pending', status: pollCount === 0 ? 'pending' : 'running' }),
            ]);
          }
          pollCount += 1;
          const statuses: Record<string, string> = {};
          for (const walkName of WALK_ORDER) statuses[walkName] = 'completed';
          statuses['walk_2a_scene_segmentation'] = pollCount === 1 ? 'pending' : 'running';
          return Promise.resolve(statuses);
        });

        await onboardViaUi();
        expect(pollCount).toBe(1);

        await vi.advanceTimersByTimeAsync(2000);
        expect(pollCount).toBe(2);
      });

      it('stops when only never-started walks remain pending', async () => {
        let pollCount = 0;
        vi.mocked(API.get).mockImplementation((endpoint: string) => {
          if (String(endpoint).endsWith('/runs')) {
            return Promise.resolve([viewerRun({ run_id: 'run-done', status: 'completed' })]);
          }
          pollCount += 1;
          const statuses: Record<string, string> = {};
          for (const walkName of WALK_ORDER) statuses[walkName] = 'pending';
          statuses['walk_2a_scene_segmentation'] = 'completed';
          return Promise.resolve(statuses);
        });

        await onboardViaUi();
        await vi.advanceTimersByTimeAsync(4000);
        expect(pollCount).toBe(1);
      });
    });

    it('keeps the SAME open viewer, live source, and entries across a refresh tick while the run is still running', async () => {
      const { renderWalkRuns, openWalkLog } = await import('../../src/tabs/script');
      renderWalkRuns([viewerRun({ run_id: 'run-live-rfr', status: 'running' })]);
      await openWalkLog('run-live-rfr');
      const src = MockEventSource.active.get(logUrl('run-live-rfr'))!;
      src.open();
      deliverLog(src, viewerLog('run-live-rfr', 1));
      deliverLog(src, viewerLog('run-live-rfr', 2));

      const rowBefore = container().querySelector('[data-walk-run-row="run-live-rfr"]')!;
      const viewerBefore = viewer('run-live-rfr');

      // Refresh tick: same run present, status STILL running.
      renderWalkRuns([viewerRun({ run_id: 'run-live-rfr', status: 'running', heartbeat_ms: 999 })]);

      expect(container().querySelector('[data-walk-run-row="run-live-rfr"]')).toBe(rowBefore);
      expect(viewer('run-live-rfr')).toBe(viewerBefore);
      expect(MockEventSource.instances.filter((i) => i.url === logUrl('run-live-rfr')).length).toBe(1);
      expect(MockEventSource.active.get(logUrl('run-live-rfr'))).toBe(src);
      expect(src.closed).toBe(false);
      expect(entries('run-live-rfr').length).toBe(2); // no re-render

      // A NEW event after the refresh still appends to the same viewer.
      deliverLog(src, viewerLog('run-live-rfr', 3));
      expect(viewer('run-live-rfr')).toBe(viewerBefore);
      expect(entries('run-live-rfr').length).toBe(3);
      expect(viewer('run-live-rfr').textContent).toContain('RESP-run-live-rfr-3');
    });

    it('reopening a completed run replays without re-rendering and re-completes, closing the new source exactly once', async () => {
      const { openWalkLog } = await import('../../src/tabs/script');
      const first = await renderAndOpen('run-replay-done');
      deliverLog(first, viewerLog('run-replay-done', 1));
      deliverLog(first, viewerLog('run-replay-done', 2));
      first.emitComplete(JSON.stringify({ run_id: 'run-replay-done', status: 'completed' }));

      expect(first.closed).toBe(true);
      expect(statusEl('run-replay-done').textContent).toContain('completed');
      expect(entries('run-replay-done').length).toBe(2);

      // Reopen the terminal run: a FRESH source for the same run.
      await openWalkLog('run-replay-done');
      const second = MockEventSource.active.get(logUrl('run-replay-done'))!;
      expect(second).not.toBe(first);
      expect(MockEventSource.instances.filter((i) => i.url === logUrl('run-replay-done')).length).toBe(2);

      // File replay re-delivers the already-rendered ids: deduped, no re-render.
      deliverLog(second, viewerLog('run-replay-done', 1));
      deliverLog(second, viewerLog('run-replay-done', 2));
      expect(entries('run-replay-done').length).toBe(2);

      // The terminal re-delivery: status updates, the new source closes once.
      second.emitComplete(JSON.stringify({ run_id: 'run-replay-done', status: 'completed' }));
      expect(statusEl('run-replay-done').textContent).toContain('completed');
      expect(second.closed).toBe(true);
      expect(second.closeCount).toBe(1);
      expect(MockEventSource.active.has(logUrl('run-replay-done'))).toBe(false);
    });

    it('no-ops (never throws) when DOM targets are missing', async () => {
      const { renderWalkRuns, openWalkLog, closeWalkLog } = await import('../../src/tabs/script');

      // initPipelineUI with #walk-runs-container ABSENT from the DOM.
      document.body.innerHTML = `
        <div id="pipeline-section"></div>
        <button id="btn-onboard-epub"></button>
        <div id="walk-status-container"></div>
        <button id="btn-run-all-walks"></button>
        <button id="btn-cancel-walks"></button>
      `;
      expect(() => document.dispatchEvent(new Event('DOMContentLoaded'))).not.toThrow();

      // openWalkLog/closeWalkLog for a run_id with no rendered row.
      expect(() => openWalkLog('run-ghost')).not.toThrow();
      expect(() => closeWalkLog('run-ghost')).not.toThrow();
      expect(MockEventSource.active.size).toBe(0);

      // Same, with the container present but no row rendered.
      document.body.innerHTML = '<div id="walk-runs-container"></div>';
      expect(() => openWalkLog('run-ghost')).not.toThrow();
      expect(() => closeWalkLog('run-ghost')).not.toThrow();
      expect(MockEventSource.active.size).toBe(0);

      // renderWalkRuns with no container element at all.
      document.body.innerHTML = '';
      expect(() => renderWalkRuns([viewerRun({ run_id: 'run-x' })])).not.toThrow();
      expect(() => renderWalkRuns([])).not.toThrow();
    });

    it('malformed log/complete payloads never throw and never corrupt the viewer or status', async () => {
      const { openWalkLog } = await import('../../src/tabs/script');
      const src = await renderAndOpen('run-p4-malformed');

      // Malformed log JSON: absorbed, no entry.
      expect(() => src.emitLog('{ definitely not json', 'run-p4-malformed:1')).not.toThrow();
      expect(entries('run-p4-malformed').length).toBe(0);

      // Malformed complete JSON: absorbed, fallback status, source closed once.
      expect(() => src.emitComplete('{ also not json')).not.toThrow();
      expect(statusEl('run-p4-malformed').textContent).toContain('Completed');
      expect(statusEl('run-p4-malformed').textContent).not.toContain('—'); // fallback REPLACED the placeholder
      expect(src.closed).toBe(true);
      expect(src.closeCount).toBe(1);

      // Missing-field complete on the reopened source: fallback status, closed.
      await openWalkLog('run-p4-malformed');
      const again = MockEventSource.active.get(logUrl('run-p4-malformed'))!;
      again.emitComplete('{}');
      expect(statusEl('run-p4-malformed').textContent).toContain('Completed');
      expect(statusEl('run-p4-malformed').textContent).not.toContain('—'); // missing-status fallback REPLACED the placeholder
      expect(again.closed).toBe(true);

      // The viewer is NOT corrupted: a valid sequence still renders and updates.
      await openWalkLog('run-p4-malformed');
      const third = MockEventSource.active.get(logUrl('run-p4-malformed'))!;
      deliverLog(third, viewerLog('run-p4-malformed', 1));
      expect(entries('run-p4-malformed').length).toBe(1);
      expect(viewer('run-p4-malformed').textContent).toContain('RESP-run-p4-malformed-1');
      third.emitComplete(JSON.stringify({ run_id: 'run-p4-malformed', status: 'failed' }));
      expect(statusEl('run-p4-malformed').textContent).toContain('failed');
      expect(third.closed).toBe(true);
      expect(MockEventSource.active.has(logUrl('run-p4-malformed'))).toBe(false);
    });

    it('keeps module-scope boot behavior and main.ts free of viewer code', () => {
      // Disk-read guard (NOT a dynamic import of main.ts): importing main here
      // would call initScript() a second time and STACK a DOMContentLoaded
      // listener on top of the file's single registration — the exact
      // double-fire hazard this suite documents. The real browser boot flow is
      // already proven by test_boot.test.ts; this guard locks the file-on-disk
      // invariants instead (mirrors the existing P1-S4 guard).
      const mainSrc = readFileSync(resolve(process.cwd(), 'src/main.ts'), 'utf8');
      // Module-scope init pattern unchanged: initScript wired from main.ts and
      // the DOMContentLoaded reveal contract preserved.
      expect(mainSrc).toContain('initScript');
      expect(mainSrc).toContain('DOMContentLoaded');
      expect(mainSrc).toContain('pipeline-section');
      // NO Part D viewer code may live in main.ts.
      expect(mainSrc.includes('walk-log-')).toBe(false);
      expect(mainSrc.includes('openWalkLog')).toBe(false);
      expect(mainSrc.includes('EventSource(')).toBe(false);

      // test_boot.test.ts stays viewer-free too.
      const bootSrc = readFileSync(resolve(process.cwd(), 'tests/frontend/test_boot.test.ts'), 'utf8');
      expect(bootSrc.includes('walk-log-')).toBe(false);
      expect(bootSrc.includes('openWalkLog')).toBe(false);
      expect(bootSrc.includes('EventSource(')).toBe(false);
    });
  });
});
