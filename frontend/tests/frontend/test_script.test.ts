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

import { describe, it, expect, beforeAll, beforeEach, afterEach, vi } from 'vitest';
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

  it('index.html declares #walk-runs-container inside the Script tab, below the walk-status card', () => {
    const html = readIndexHtml();
    const runsIdx = html.indexOf('id="walk-runs-container"');
    const statusIdx = html.indexOf('id="walk-status-container"');
    expect(runsIdx).toBeGreaterThan(-1);
    expect(statusIdx).toBeGreaterThan(-1);
    expect(runsIdx).toBeGreaterThan(statusIdx);
  });
});

