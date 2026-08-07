/**
 * Spec-first tests for Projects tab (frontend/src/tabs/projects.ts) — Plan I Phase 3.
 *
 * Backend contract (app/pipeline/api_operations.py, verified — Plan I phases 1-2):
 *   - POST   /api/pipeline/projects {book_id}        → 200 ProjectSnapshot
 *     {name, book_id, created_ms, size_bytes}; name is auto-generated SERVER-SIDE
 *     ("Project {YYYY-MM-DD HH:MM}" + optional " (N)" same-minute suffix) — the
 *     frontend never proposes a name.
 *   - GET    /api/pipeline/projects?book_id=         → 200 ProjectSnapshot[],
 *     newest first (created_ms DESC).
 *   - POST   /api/pipeline/projects/load {name, book_id} → 200
 *     {status:"ok", name, book_id, re_render_required:boolean} | 404 | 409 +
 *     Retry-After: 5 while a walk/render is active (rule #10).
 *   - DELETE /api/pipeline/projects/{name}           → 200 {status:"ok", name} | 404
 *   - PATCH  /api/pipeline/projects/{name} {new_name} → 200 {status:"ok", name}
 *     | 400 invalid | 404 | 409 duplicate.
 *
 * Frontend contract (DD UX workflow #7): Save (auto-named) / Load / Delete /
 * Rename; Load surfaces an explicit "re-render" notice when
 * re_render_required=true; 409 + Retry-After is retried exactly once with a
 * toast (the generic postWithRetryOnce helper, parameterized to 409).
 *
 * Run with `npm test` (vitest run) from frontend/.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { readFileSync, existsSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  ProjectSnapshot,
  formatSnapshotSize,
  formatSnapshotDate,
  renderProjectsList,
  loadProjects,
  saveProject,
  loadProject,
  deleteProject,
  renameProject,
  initProjects,
} from '../../src/tabs/projects';
import { state } from '../../src/state';
import * as API from '../../src/api';
import { showToast, showConfirm } from '../../src/utils';
import { loadSpans } from '../../src/tabs/editor-pipeline';

// Mock the API module. postWithRetryOnce is a vi.fn by default (resolves a
// successful load); the 409-retry integration test re-implements it with the
// REAL helper against a spied global fetch (test_editor convention).
vi.mock('../../src/api', () => ({
  get: vi.fn(),
  post: vi.fn(),
  del: vi.fn(),
  patch: vi.fn(),
  postWithRetryOnce: vi.fn(() =>
    Promise.resolve({ status: 'ok', name: '', book_id: '', re_render_required: false }),
  ),
}));

// Mock utils to avoid DOM side effects. escapeHtml mirrors the real contract
// (escaping) so render output escaping is observable.
vi.mock('../../src/utils', () => ({
  showToast: vi.fn(),
  showConfirm: vi.fn(),
  escapeHtml: (s: unknown) =>
    s == null
      ? ''
      : String(s)
          .replace(/&/g, '&amp;')
          .replace(/</g, '&lt;')
          .replace(/>/g, '&gt;')
          .replace(/"/g, '&quot;')
          .replace(/'/g, '&#39;'),
}));

// Mock the editor-pipeline module so cross-tab refresh (loadSpans) is
// observable without pulling in the real editor module.
vi.mock('../../src/tabs/editor-pipeline', () => ({
  loadSpans: vi.fn(() => Promise.resolve()),
}));

// ---------------------------------------------------------------------------
// Test data fixtures
// ---------------------------------------------------------------------------

const MOCK_SNAPSHOTS: ProjectSnapshot[] = [
  {
    name: 'Project 2026-08-07 03:13',
    book_id: 'book-123',
    created_ms: 1750000000000,
    size_bytes: 2048,
  },
  {
    name: 'Project 2026-08-07 03:12',
    book_id: 'book-123',
    created_ms: 1749999999000,
    size_bytes: 1024,
  },
];

// ---------------------------------------------------------------------------
// index.html fixture resolution (same helper as test_editor.test.ts)
// ---------------------------------------------------------------------------

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
// Test suites
// ---------------------------------------------------------------------------

describe('formatSnapshotSize', () => {
  it('formats bytes with human-readable units', () => {
    expect(formatSnapshotSize(0)).toBe('0 B');
    expect(formatSnapshotSize(1023)).toBe('1023 B');
    expect(formatSnapshotSize(1024)).toBe('1.0 KB');
    expect(formatSnapshotSize(1536)).toBe('1.5 KB');
    expect(formatSnapshotSize(1048576)).toBe('1.0 MB');
  });
});

describe('formatSnapshotDate', () => {
  it('formats unix ms as a local date string', () => {
    expect(formatSnapshotDate(1750000000000)).toBe(new Date(1750000000000).toLocaleString());
  });
});

describe('renderProjectsList', () => {
  it('renders one row per snapshot with name, date and size', () => {
    const html = renderProjectsList(MOCK_SNAPSHOTS);

    expect(html).toContain('Project 2026-08-07 03:13');
    expect(html).toContain('Project 2026-08-07 03:12');
    expect(html).toContain(formatSnapshotDate(1750000000000));
    expect(html).toContain('2.0 KB');
    expect(html).toContain('1.0 KB');
    // Each row exposes Load / Delete / Rename affordances carrying the name.
    expect(html).toContain('data-action="project-load"');
    expect(html).toContain('data-action="project-delete"');
    expect(html).toContain('data-action="project-rename"');
  });

  it('escapes HTML in snapshot names (server-supplied content)', () => {
    const hostile: ProjectSnapshot[] = [
      {
        name: '<script>alert(1)</script> & "quoted"',
        book_id: 'book-123',
        created_ms: 1750000000000,
        size_bytes: 1,
      },
    ];
    const html = renderProjectsList(hostile);

    expect(html).not.toContain('<script>');
    expect(html).toContain('&lt;script&gt;alert(1)&lt;/script&gt;');
    expect(html).toContain('&amp;');
    expect(html).toContain('&quot;quoted&quot;');
  });

  it('renders an empty state when there are no snapshots', () => {
    const html = renderProjectsList([]);
    expect(html).toContain('No saved projects');
  });
});

describe('loadProjects', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    state.pipelineBookId = 'book-123';
    document.body.innerHTML = '<div id="projects-list"></div>';
    vi.mocked(API.get).mockResolvedValue(MOCK_SNAPSHOTS);
  });

  afterEach(() => {
    state.pipelineBookId = null;
    document.body.innerHTML = '';
  });

  it('GETs /api/pipeline/projects filtered by the active book_id and renders the list', async () => {
    await loadProjects();

    expect(API.get).toHaveBeenCalledWith('/api/pipeline/projects?book_id=book-123');
    const list = document.getElementById('projects-list');
    expect(list?.textContent).toContain('Project 2026-08-07 03:13');
    expect(list?.textContent).toContain('Project 2026-08-07 03:12');
  });

  it('renders an empty state when the server returns no snapshots', async () => {
    vi.mocked(API.get).mockResolvedValue([]);

    await loadProjects();

    const list = document.getElementById('projects-list');
    expect(list?.textContent).toContain('No saved projects');
  });

  it('shows an error toast when the GET fails', async () => {
    vi.mocked(API.get).mockRejectedValue(new Error('boom'));

    await loadProjects();

    expect(showToast).toHaveBeenCalledWith(expect.stringContaining('Failed to load projects'), 'error');
  });
});

describe('saveProject', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    state.pipelineBookId = 'book-123';
    document.body.innerHTML = '<div id="projects-list"></div>';
    vi.mocked(API.get).mockResolvedValue([]);
    vi.mocked(API.post).mockResolvedValue({
      name: 'Project 2026-08-07 03:13',
      book_id: 'book-123',
      created_ms: 1750000000000,
      size_bytes: 2048,
    });
  });

  afterEach(() => {
    state.pipelineBookId = null;
    document.body.innerHTML = '';
  });

  it('POSTs {book_id} to /api/pipeline/projects and refreshes the list', async () => {
    await saveProject();

    expect(API.post).toHaveBeenCalledWith('/api/pipeline/projects', { book_id: 'book-123' });
    expect(API.get).toHaveBeenCalledWith('/api/pipeline/projects?book_id=book-123');
  });

  it('toasts the server-generated auto name on success', async () => {
    await saveProject();

    expect(showToast).toHaveBeenCalledWith(
      expect.stringContaining('Project 2026-08-07 03:13'),
      'success',
    );
  });

  it('refuses to save with an error toast when no book is onboarded (no POST)', async () => {
    state.pipelineBookId = null;

    await saveProject();

    expect(API.post).not.toHaveBeenCalled();
    expect(showToast).toHaveBeenCalledWith(expect.stringContaining('No book'), 'error');
  });

  it('shows an error toast when the POST fails', async () => {
    vi.mocked(API.post).mockRejectedValue(new Error('book not found'));

    await saveProject();

    expect(showToast).toHaveBeenCalledWith(expect.stringContaining('Failed to save'), 'error');
  });
});

describe('loadProject', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    state.pipelineBookId = 'book-123';
    document.body.innerHTML = '<div id="projects-list"></div>';
    vi.mocked(API.get).mockResolvedValue([]);
    vi.mocked(API.postWithRetryOnce).mockResolvedValue({
      status: 'ok',
      name: 'Project 2026-08-07 03:13',
      book_id: 'book-123',
      re_render_required: false,
    });
  });

  afterEach(() => {
    state.pipelineBookId = null;
    document.body.innerHTML = '';
  });

  it('calls POST /api/pipeline/projects/load with {name, book_id} through the 409-retry helper', async () => {
    await loadProject('Project 2026-08-07 03:13');

    expect(API.postWithRetryOnce).toHaveBeenCalledWith(
      '/api/pipeline/projects/load',
      { name: 'Project 2026-08-07 03:13', book_id: 'book-123' },
      409,
    );
  });

  it('surfaces the re-render notice and refreshes editor state when re_render_required=true', async () => {
    vi.mocked(API.postWithRetryOnce).mockResolvedValue({
      status: 'ok',
      name: 'Project 2026-08-07 03:13',
      book_id: 'book-123',
      re_render_required: true,
    });

    await loadProject('Project 2026-08-07 03:13');

    expect(showToast).toHaveBeenCalledWith(
      expect.stringContaining('re-render'),
      'warning',
    );
    // List refreshed + editor spans reloaded via the cross-tab refresh hook.
    expect(API.get).toHaveBeenCalledWith('/api/pipeline/projects?book_id=book-123');
    expect(loadSpans).toHaveBeenCalled();
  });

  it('shows a success toast and refreshes when no re-render is required', async () => {
    // Backend echoes request.name — mirror that so the toast assertion is stable.
    vi.mocked(API.postWithRetryOnce).mockResolvedValue({
      status: 'ok',
      name: 'Project 2026-08-07 03:12',
      book_id: 'book-123',
      re_render_required: false,
    });

    await loadProject('Project 2026-08-07 03:12');

    expect(showToast).toHaveBeenCalledWith(
      expect.stringContaining('Project 2026-08-07 03:12'),
      'success',
    );
    expect(API.get).toHaveBeenCalledWith('/api/pipeline/projects?book_id=book-123');
    expect(loadSpans).toHaveBeenCalled();
  });

  it('shows an error toast when the load fails (e.g. both attempts 409)', async () => {
    vi.mocked(API.postWithRetryOnce).mockRejectedValue(new Error('Cannot restore while a walk or render is active'));

    await loadProject('Project 2026-08-07 03:13');

    expect(showToast).toHaveBeenCalledWith(expect.stringContaining('Failed to load'), 'error');
  });

  it('retries a 409+Retry-After response exactly once (real helper, spied fetch)', async () => {
    const originalFetch = globalThis.fetch;
    const retry409 = {
      ok: false,
      status: 409,
      statusText: 'Conflict',
      headers: { get: (name: string) => (name === 'Retry-After' ? '5' : null) },
      json: async () => ({ detail: 'Cannot restore while a walk or render is active' }),
    };
    const okLoad = {
      ok: true,
      status: 200,
      statusText: 'OK',
      headers: { get: () => null },
      json: async () => ({
        status: 'ok',
        name: 'Project 2026-08-07 03:13',
        book_id: 'book-123',
        re_render_required: false,
      }),
    };
    const fetchSpy = vi.fn()
      .mockResolvedValueOnce(retry409)
      .mockResolvedValueOnce(okLoad);

    // Re-implement postWithRetryOnce with the REAL helper so the retry logic
    // itself is exercised (test_editor convention).
    const actual = await vi.importActual<typeof import('../../src/api')>('../../src/api');
    vi.mocked(API.postWithRetryOnce).mockImplementation(actual.postWithRetryOnce);

    globalThis.fetch = fetchSpy;
    vi.useFakeTimers();

    try {
      const promise = loadProject('Project 2026-08-07 03:13');
      await vi.advanceTimersByTimeAsync(0); // attempt 1 → 409 → Retry-After delay scheduled
      await vi.advanceTimersByTimeAsync(5000); // delay elapses → attempt 2 → 200

      await promise;
      expect(fetchSpy).toHaveBeenCalledTimes(2); // exactly one retry, never a 3rd attempt
      expect(fetchSpy).toHaveBeenNthCalledWith(
        1,
        '/api/pipeline/projects/load',
        expect.objectContaining({
          method: 'POST',
          body: JSON.stringify({ name: 'Project 2026-08-07 03:13', book_id: 'book-123' }),
        }),
      );
      expect(showToast).toHaveBeenCalledWith(
        expect.stringContaining('Project 2026-08-07 03:13'),
        'success',
      );
    } finally {
      globalThis.fetch = originalFetch;
      vi.useRealTimers();
    }
  });
});

describe('deleteProject', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    state.pipelineBookId = 'book-123';
    document.body.innerHTML = '<div id="projects-list"></div>';
    vi.mocked(API.get).mockResolvedValue([]);
    vi.mocked(API.del).mockResolvedValue({ status: 'ok', name: 'Project 2026-08-07 03:13' });
  });

  afterEach(() => {
    state.pipelineBookId = null;
    document.body.innerHTML = '';
  });

  it('confirms first, then DELETEs the snapshot by name and refreshes the list', async () => {
    vi.mocked(showConfirm).mockResolvedValue(true);

    await deleteProject('Project 2026-08-07 03:13');

    expect(showConfirm).toHaveBeenCalledWith(expect.stringContaining('Project 2026-08-07 03:13'));
    expect(API.del).toHaveBeenCalledWith(
      '/api/pipeline/projects/' + encodeURIComponent('Project 2026-08-07 03:13'),
    );
    expect(showToast).toHaveBeenCalledWith(expect.stringContaining('deleted'), 'success');
    expect(API.get).toHaveBeenCalledWith('/api/pipeline/projects?book_id=book-123');
  });

  it('does not DELETE when the user cancels the confirmation', async () => {
    vi.mocked(showConfirm).mockResolvedValue(false);

    await deleteProject('Project 2026-08-07 03:13');

    expect(API.del).not.toHaveBeenCalled();
  });

  it('shows an error toast when the DELETE fails', async () => {
    vi.mocked(showConfirm).mockResolvedValue(true);
    vi.mocked(API.del).mockRejectedValue(new Error('Snapshot not found'));

    await deleteProject('Project 2026-08-07 03:13');

    expect(showToast).toHaveBeenCalledWith(expect.stringContaining('Failed to delete'), 'error');
  });
});

describe('renameProject', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    state.pipelineBookId = 'book-123';
    document.body.innerHTML = '<div id="projects-list"></div>';
    vi.mocked(API.get).mockResolvedValue([]);
    vi.mocked(API.patch).mockResolvedValue({ status: 'ok', name: 'Renamed' });
  });

  afterEach(() => {
    state.pipelineBookId = null;
    document.body.innerHTML = '';
  });

  it('prompts for a new name, PATCHes {new_name} and refreshes the list', async () => {
    global.prompt = vi.fn().mockReturnValue('Project 2026-08-07 09:00');

    await renameProject('Project 2026-08-07 03:13');

    expect(global.prompt).toHaveBeenCalledWith(expect.stringContaining('Project 2026-08-07 03:13'));
    expect(API.patch).toHaveBeenCalledWith(
      '/api/pipeline/projects/' + encodeURIComponent('Project 2026-08-07 03:13'),
      { new_name: 'Project 2026-08-07 09:00' },
    );
    expect(showToast).toHaveBeenCalledWith(expect.stringContaining('Project 2026-08-07 09:00'), 'success');
    expect(API.get).toHaveBeenCalledWith('/api/pipeline/projects?book_id=book-123');
  });

  it('does not PATCH when the user cancels the prompt', async () => {
    global.prompt = vi.fn().mockReturnValue(null);

    await renameProject('Project 2026-08-07 03:13');

    expect(API.patch).not.toHaveBeenCalled();
  });

  it('shows an error toast when the PATCH fails (409 duplicate name)', async () => {
    global.prompt = vi.fn().mockReturnValue('Taken');
    vi.mocked(API.patch).mockRejectedValue(new Error("Snapshot 'Taken' already exists"));

    await renameProject('Project 2026-08-07 03:13');

    expect(showToast).toHaveBeenCalledWith(expect.stringContaining('already exists'), 'error');
  });
});

describe('initProjects', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    state.pipelineBookId = 'book-123';
    document.body.innerHTML = `
      <button id="btn-project-save"></button>
      <div id="projects-list">
        <button data-action="project-load" data-name="Project 2026-08-07 03:13">Load</button>
        <button data-action="project-delete" data-name="Project 2026-08-07 03:12">Delete</button>
        <button data-action="project-rename" data-name="Project 2026-08-07 03:11">Rename</button>
      </div>
    `;
    vi.mocked(API.get).mockResolvedValue([]);
    vi.mocked(API.post).mockResolvedValue({
      name: 'Project 2026-08-07 03:13',
      book_id: 'book-123',
      created_ms: 1750000000000,
      size_bytes: 1,
    });
    vi.mocked(API.postWithRetryOnce).mockResolvedValue({
      status: 'ok',
      name: 'Project 2026-08-07 03:13',
      book_id: 'book-123',
      re_render_required: false,
    });
    vi.mocked(API.del).mockResolvedValue({ status: 'ok', name: 'Project 2026-08-07 03:12' });
    vi.mocked(API.patch).mockResolvedValue({ status: 'ok', name: 'Renamed' });
    vi.mocked(showConfirm).mockResolvedValue(true);
  });

  afterEach(() => {
    state.pipelineBookId = null;
    document.body.innerHTML = '';
  });

  it('wires the Save button, loads the list on DOMContentLoaded, and delegates row actions', async () => {
    initProjects();
    document.dispatchEvent(new Event('DOMContentLoaded'));

    // Initial list load on init.
    expect(API.get).toHaveBeenCalledWith('/api/pipeline/projects?book_id=book-123');

    // Save button → POST /projects.
    const saveBtn = document.getElementById('btn-project-save') as HTMLButtonElement;
    saveBtn.click();
    expect(API.post).toHaveBeenCalledWith('/api/pipeline/projects', { book_id: 'book-123' });

    // Row action: Load.
    (document.querySelector('[data-action="project-load"]') as HTMLButtonElement).click();
    expect(API.postWithRetryOnce).toHaveBeenCalledWith(
      '/api/pipeline/projects/load',
      { name: 'Project 2026-08-07 03:13', book_id: 'book-123' },
      409,
    );

    // Row action: Rename (prompt is synchronous, so the PATCH fires
    // immediately — assert before any async re-render replaces the rows).
    global.prompt = vi.fn().mockReturnValue('Renamed');
    (document.querySelector('[data-action="project-rename"]') as HTMLButtonElement).click();
    expect(API.patch).toHaveBeenCalledWith(
      '/api/pipeline/projects/' + encodeURIComponent('Project 2026-08-07 03:11'),
      { new_name: 'Renamed' },
    );

    // Row action: Delete (confirms first). deleteProject awaits showConfirm
    // before calling API.del, so flush the microtask chain before asserting.
    (document.querySelector('[data-action="project-delete"]') as HTMLButtonElement).click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(API.del).toHaveBeenCalledWith(
      '/api/pipeline/projects/' + encodeURIComponent('Project 2026-08-07 03:12'),
    );
  });
});

describe('index.html projects tab structure', () => {
  it('declares the Projects nav link and tab pane with the Save affordance', () => {
    const html = readIndexHtml();

    const navIdx = html.indexOf('data-tab="projects"');
    expect(navIdx).toBeGreaterThan(-1);

    const paneIdx = html.indexOf('id="projects-tab"');
    expect(paneIdx).toBeGreaterThan(-1);
    expect(paneIdx).toBeGreaterThan(navIdx);

    const saveIdx = html.indexOf('id="btn-project-save"');
    expect(saveIdx).toBeGreaterThan(-1);
    expect(saveIdx).toBeGreaterThan(paneIdx);

    const listIdx = html.indexOf('id="projects-list"');
    expect(listIdx).toBeGreaterThan(-1);
  });
});
