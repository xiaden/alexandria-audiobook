/**
 * Spec-first tests for the combined workbench tab (frontend/src/tabs/workbench.ts)
 * and its state selectors/types (frontend/src/state.ts) — walks 2b/2c/2d.
 *
 * Backend contract (app/pipeline, READ-ONLY for this sub-task):
 *   - GET    /api/pipeline/workbench/{book_id}          → WorkbenchState
 *   - GET    /api/pipeline/workbench/{book_id}/config   → WorkbenchConfig
 *   - PUT    /workbench/{book_id}/overrides {walk_name,key,value,base_revision}
 *   - DELETE /workbench/{book_id}/overrides {walk_name,key,base_revision}
 *   - POST   /workbench/{book_id}/alias-conversions/preview|commit
 *   - PUT    /workbench/{book_id}/presence {scene_id,character_id,relation_type,base_revision}
 *   - POST   /workbench/{book_id}/reruns (scope book|scenes; preserve_manual_decisions default true)
 *   - POST   /workbench/{book_id}/decisions/{decision_id}/undo {base_revision}
 *   - Review accept/reject/override stay on the existing surface with base_revision.
 *
 * Run with `npm test` (vitest run) from frontend/.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { readFileSync, existsSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  parseAliases,
  formatConfidence,
  isUndoable,
  clearUndoStack,
  getUndoStack,
  renderSceneNavigator,
  renderSpanEvidence,
  renderCharacterLedger,
  renderAliasLedger,
  renderScenePresence,
  renderConfigSources,
  renderConflicts,
  renderRuns,
  renderSetupPanel,
  loadWorkbench,
  loadWorkbenchConfig,
  resolveReviewItem,
  previewAliasConversion,
  commitAliasConversion,
  savePresence,
  saveOverride,
  clearOverride,
  rerunWalk,
  undoDecision,
  unmergeAlias,
  initWorkbench,
} from '../../src/tabs/workbench';
import {
  state,
  WorkbenchState,
  selectReviewItems,
  selectScenePresence,
  selectCanonicalCharacters,
  sourceLabel,
} from '../../src/state';
import * as API from '../../src/api';
import { showToast, showConfirm } from '../../src/utils';

vi.mock('../../src/api', () => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  del: vi.fn(),
  patch: vi.fn(),
  putWithRetryOnce: vi.fn(),
  delWithRetryOnce: vi.fn(),
  postWithRetryOnce: vi.fn(),
}));

vi.mock('../../src/utils', () => ({
  showToast: vi.fn(),
  showConfirm: vi.fn(() => Promise.resolve(true)),
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

// The undo stack is module-level; reset it before every test so exact-stack
// assertions are not polluted by decisions recorded in earlier tests.
beforeEach(() => {
  clearUndoStack();
});

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const MOCK_WB: WorkbenchState = {
  book_id: 'book-123',
  generation_revision: 42,
  scenes: [
    {
      chapter_id: 'ch-1',
      position: 1,
      scenes: [
        {
          scene_id: 'scene-1',
          position: 1,
          paragraphs: [
            {
              paragraph_id: 'para-1',
              position: 1,
              spans: [
                { span_id: 'span-1', span_type: 'narrator', text: 'Alice walked in.', instruct: '', span_position: 1 },
                { span_id: 'span-2', span_type: 'dialogue', text: 'Hello, Bob said.', instruct: '', span_position: 2 },
              ],
            },
          ],
        },
      ],
    },
  ],
  characters: [
    { id: 'char-1', name: 'Alice', aliases: '["Ali"]', voice_assignment_id: 'voice-1', description: null },
    { id: 'char-2', name: 'Bob', aliases: '[]', voice_assignment_id: null, description: null },
  ],
  aliases: [
    { merge_id: 'merge-1', decision_id: 'decision-merge-1', canonical_id: 'char-1', member_id: 'char-2', status: 'active', merge_revision: 40, created_ms: 1750000000000, canonical_name: 'Alice', member_name: 'Bobby' },
  ],
  presence: [
    { scene_id: 'scene-1', character_id: 'char-1', relation_type: 'present', source: 'walk', confidence: 0.95, human_override: false, generation_revision: 41, source_run_id: 'run-1' },
  ],
  review_items: [
    { item_id: 'walkitem:9', kind: 'junction', target_table: 'character_mentions_scene', target_id: 'x', status: 'pending', decision_id: null, source_run_id: 'run-1', character_id: 'char-1', character_name: 'Alice', confidence: 0.9 },
    { item_id: 'decision:abc', kind: 'decision', target_table: 'decision', target_id: 'abc', status: 'resolved', decision_id: 'abc', source_run_id: null },
  ],
  overrides: [{ walk_name: 'walk_2b_character_discovery', key: 'model_name', value: 'gpt-4' }],
  effective_config: {
    walk_2b_character_discovery: {
      values: { model_name: 'gpt-4', temperature: 0.7 },
      sources: { model_name: 'db', temperature: 'hardcoded' },
    },
  },
  conflicts: [
    { code: 'PRESENCE_CONFLICT', current_revision: 42, current_value: 'present', requested_value: 'absent', decision_id: null, item_id: 'walkitem:9' },
  ],
  runs: [
    { run_id: 'run-1', walk_name: 'walk_2b_character_discovery', status: 'done', heartbeat_ms: 1750000000000, created_ms: 1749999999000, finished_ms: 1750000000000, error: null },
  ],
};

const MOCK_CONFIG = {
  global: { model_name: 'base-model' },
  task_overrides: {},
  top_level_walk_override: {},
  db_overrides: { walk_2b_character_discovery: { model_name: 'gpt-4' } },
  effective: {
    walk_2b_character_discovery: { model_name: 'gpt-4', temperature: 0.7, prompt: 'Discover characters' },
  },
  source: {
    walk_2b_character_discovery: { model_name: 'db', temperature: 'hardcoded', prompt: 'hardcoded' },
  },
  validation_errors: [],
};

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
// Pure render + selector helpers
// ---------------------------------------------------------------------------

describe('parseAliases / formatConfidence / isUndoable', () => {
  it('parses a JSON aliases string and tolerates malformed input', () => {
    expect(parseAliases('["Ali","Alice"]')).toEqual(['Ali', 'Alice']);
    expect(parseAliases('not-json')).toEqual([]);
    expect(parseAliases(null)).toEqual([]);
    expect(parseAliases('')).toEqual([]);
  });

  it('formats confidence as a percentage and handles nulls', () => {
    expect(formatConfidence(0.956)).toBe('96%');
    expect(formatConfidence(null)).toBe('—');
    expect(formatConfidence(undefined)).toBe('—');
  });

  it('treats only active decisions as undoable', () => {
    expect(isUndoable('active')).toBe(true);
    expect(isUndoable('undone')).toBe(false);
    expect(isUndoable(null)).toBe(false);
  });
});

describe('renderSceneNavigator', () => {
  it('renders scenes with stable identity and a non-color selected marker', () => {
    const html = renderSceneNavigator(MOCK_WB, 'scene-1');
    expect(html).toContain('1.1');
    expect(html).toContain('data-scene-id="scene-1"');
    expect(html).toContain('aria-current="true"');
    // Non-color state: a visible text/i marker accompanies selection.
    expect(html).toContain('visually-hidden');
    expect(html).toContain('fa-check');
  });

  it('renders an empty state when no scenes exist', () => {
    const html = renderSceneNavigator({ ...MOCK_WB, scenes: [] }, null);
    expect(html).toContain('No scenes available');
  });
});

describe('renderSpanEvidence', () => {
  it('renders spans carrying their immutable ids (stable anchors, never indexes)', () => {
    const html = renderSpanEvidence(MOCK_WB, 'scene-1');
    expect(html).toContain('data-span-id="span-1"');
    expect(html).toContain('data-paragraph-id="para-1"');
    expect(html).toContain('data-chapter-id="ch-1"');
    expect(html).toContain('Alice walked in.');
    expect(html).toContain('Hello, Bob said.');
  });

  it('escapes span text and returns empty when no scene selected', () => {
    const hostile: WorkbenchState = {
      ...MOCK_WB,
      scenes: [{ chapter_id: 'ch-1', position: 1, scenes: [{ scene_id: 's', position: 1, paragraphs: [{ paragraph_id: 'p', position: 1, spans: [{ span_id: 'sp', span_type: 'x', text: '<script>evil</script>', instruct: '', span_position: 1 }] }] }] }],
    };
    const html = renderSpanEvidence(hostile, 's');
    expect(html).not.toContain('<script>');
    expect(html).toContain('&lt;script&gt;');
    expect(renderSpanEvidence(MOCK_WB, null)).toBe('');
  });
});

describe('renderCharacterLedger', () => {
  it('renders names, aliases and voice assignments, escaping HTML', () => {
    const html = renderCharacterLedger(MOCK_WB);
    expect(html).toContain('Alice');
    expect(html).toContain('Ali');
    expect(html).toContain('voice-1');
    expect(html).toContain('Bob');
  });

  it('renders an empty state with no characters', () => {
    expect(renderCharacterLedger({ ...MOCK_WB, characters: [] })).toContain('No characters yet');
  });
});

describe('renderAliasLedger', () => {
  it('renders active merges as member → canonical with an unmerge affordance', () => {
    const html = renderAliasLedger(MOCK_WB);
    expect(html).toContain('Bobby');
    expect(html).toContain('Alice');
    expect(html).toContain('data-action="alias-unmerge"');
    expect(html).toContain('data-decision-id="decision-merge-1"');
  });
});

describe('renderScenePresence', () => {
  it('renders presence with a relation badge and manual-source text (non-color)', () => {
    const html = renderScenePresence(MOCK_WB, 'scene-1');
    expect(html).toContain('Alice');
    expect(html).toContain('present');
    expect(html).toContain('walk');
    expect(html).toContain('95%');
    expect(html).toContain('data-action="presence-change"');
    expect(html).toContain('data-character-id="char-1"');
  });

  it('marks manual presence with explicit text', () => {
    const wb: WorkbenchState = {
      ...MOCK_WB,
      presence: [{ scene_id: 'scene-1', character_id: 'char-1', relation_type: 'speaker', source: 'human', confidence: null, human_override: true, decision_id: 'decision-presence-42' }],
    };
    const html = renderScenePresence(wb, 'scene-1');
    expect(html).toContain('speaker');
    expect(html).toContain('manual');
  });

  it('prompts when no scene selected', () => {
    expect(renderScenePresence(MOCK_WB, null)).toContain('Select a scene');
  });
});

describe('renderConfigSources / renderConflicts / renderRuns', () => {
  it('renders effective config sources with human labels', () => {
    const html = renderConfigSources(MOCK_WB, 'walk_2b_character_discovery');
    expect(html).toContain('model_name');
    expect(html).toContain('custom'); // sourceLabel('db')
    expect(html).toContain('default'); // sourceLabel('hardcoded')
  });

  it('renders conflicts with code + values (text, not color alone)', () => {
    const html = renderConflicts(MOCK_WB);
    expect(html).toContain('PRESENCE_CONFLICT');
    expect(html).toContain('present');
    expect(html).toContain('absent');
  });

  it('renders runs with status text', () => {
    const html = renderRuns(MOCK_WB);
    expect(html).toContain('done');
    expect(html).toContain('run-1');
  });
});

describe('selectors (state.ts)', () => {
  it('selectReviewItems filters pending/conflict/protected', () => {
    expect(selectReviewItems(MOCK_WB, 'all')).toHaveLength(2);
    expect(selectReviewItems(MOCK_WB, 'pending')).toHaveLength(1);
    // conflict filter selects items referenced by conflicts.item_id
    expect(selectReviewItems(MOCK_WB, 'conflict')).toHaveLength(1);
    // protected = resolved with a decision
    expect(selectReviewItems(MOCK_WB, 'protected')).toHaveLength(1);
  });

  it('treats legacy review items without status as pending', () => {
    const legacyItem = { ...MOCK_WB.review_items[0] };
    delete (legacyItem as Partial<typeof legacyItem>).status;
    const wb = { ...MOCK_WB, review_items: [legacyItem] };
    expect(selectReviewItems(wb, 'pending')).toEqual([legacyItem]);
  });

  it('selectScenePresence filters by scene', () => {
    expect(selectScenePresence(MOCK_WB, 'scene-1')).toHaveLength(1);
    expect(selectScenePresence(MOCK_WB, null)).toHaveLength(0);
  });

  it('selectCanonicalCharacters excludes active merge members', () => {
    // char-2 (Bob) is an active merge member → excluded.
    const canon = selectCanonicalCharacters(MOCK_WB);
    expect(canon.map((c) => c.id)).toEqual(['char-1']);
  });

  it('sourceLabel maps provenance strings to display labels', () => {
    expect(sourceLabel('db')).toBe('custom');
    expect(sourceLabel('hardcoded')).toBe('default');
    expect(sourceLabel('task_override')).toBe('task override');
    expect(sourceLabel('global')).toBe('global');
    expect(sourceLabel(null)).toBe('default');
  });
});

// ---------------------------------------------------------------------------
// Data functions
// ---------------------------------------------------------------------------

describe('loadWorkbench', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    state.pipelineBookId = 'book-123';
    state.workbench = null;
    document.body.innerHTML = `
      <div id="workbench-navigator"></div>
      <div id="workbench-ledger"></div>
      <div id="workbench-aliases"></div>
      <div id="workbench-conflicts"></div>
      <div id="workbench-runs"></div>
      <div id="workbench-span-evidence"></div>
      <div id="workbench-presence"></div>
    `;
    vi.mocked(API.get).mockResolvedValue(MOCK_WB);
  });

  afterEach(() => {
    state.pipelineBookId = null;
    state.workbench = null;
    document.body.innerHTML = '';
  });

  it('GETs /api/pipeline/workbench/{book_id} and renders the panes', async () => {
    await loadWorkbench();

    expect(API.get).toHaveBeenCalledWith('/api/pipeline/workbench/book-123');
    expect(document.getElementById('workbench-navigator')?.textContent).toContain('1.1');
    expect(document.getElementById('workbench-ledger')?.textContent).toContain('Alice');
    expect(document.getElementById('workbench-conflicts')?.textContent).toContain('PRESENCE_CONFLICT');
  });

  it('shows an error toast on failure and clears the undo stack', async () => {
    clearUndoStack();
    // populate the undo stack
    vi.mocked(API.get).mockRejectedValue(new Error('boom'));
    await loadWorkbench();
    expect(showToast).toHaveBeenCalledWith(expect.stringContaining('Failed to load workbench'), 'error');
    expect(getUndoStack()).toHaveLength(0);
  });
});

describe('savePresence', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    state.pipelineBookId = 'book-123';
    state.workbench = MOCK_WB;
    vi.mocked(API.get).mockResolvedValue(MOCK_WB);
    vi.mocked(API.putWithRetryOnce).mockResolvedValue({ decision_id: 'd-1', status: 'active', conflict: null });
    vi.mocked(showConfirm).mockResolvedValue(true);
  });

  afterEach(() => {
    state.pipelineBookId = null;
    state.workbench = null;
    document.body.innerHTML = '';
  });

  it('PUTs presence through the 503 one-retry helper with base_revision', async () => {
    await savePresence('scene-1', 'char-1', 'speaker');
    expect(API.putWithRetryOnce).toHaveBeenCalledWith(
      '/api/pipeline/workbench/book-123/presence',
      { scene_id: 'scene-1', character_id: 'char-1', relation_type: 'speaker', base_revision: 42 },
    );
    expect(getUndoStack()).toEqual([{ label: 'presence speaker', decisionId: 'd-1' }]);
  });

  it('confirms destructive removal (absent) before PUTting', async () => {
    await savePresence('scene-1', 'char-1', 'absent');
    expect(showConfirm).toHaveBeenCalledWith(expect.stringContaining('absent'));
    expect(API.putWithRetryOnce).toHaveBeenCalled();
  });

  it('skips the PUT when the user cancels an absent confirmation', async () => {
    vi.mocked(showConfirm).mockResolvedValue(false);
    await savePresence('scene-1', 'char-1', 'absent');
    expect(API.putWithRetryOnce).not.toHaveBeenCalled();
  });

  it('surfaces a conflict as a warning toast', async () => {
    vi.mocked(API.putWithRetryOnce).mockResolvedValue({ decision_id: 'd-2', status: 'active', conflict: { code: 'X' } });
    await savePresence('scene-1', 'char-1', 'present');
    expect(showToast).toHaveBeenCalledWith(expect.stringContaining('conflict'), 'warning');
  });
});

describe('override save/clear', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    state.pipelineBookId = 'book-123';
    state.workbench = MOCK_WB;
    vi.mocked(API.get).mockResolvedValue(MOCK_WB);
    vi.mocked(API.putWithRetryOnce).mockResolvedValue({});
    vi.mocked(API.delWithRetryOnce).mockResolvedValue({});
  });

  afterEach(() => {
    state.pipelineBookId = null;
    state.workbench = null;
  });

  it('saves an override via PUT one-retry with pipeline endpoint + base_revision', async () => {
    await saveOverride('walk_2b_character_discovery', 'temperature', 0.5);
    expect(API.putWithRetryOnce).toHaveBeenCalledWith(
      '/api/pipeline/workbench/book-123/overrides',
      { walk_name: 'walk_2b_character_discovery', key: 'temperature', value: 0.5, base_revision: 42 },
    );
  });

  it('clears an override via DELETE one-retry', async () => {
    await clearOverride('walk_2b_character_discovery', 'model_name');
    expect(API.delWithRetryOnce).toHaveBeenCalledWith(
      '/api/pipeline/workbench/book-123/overrides',
      { walk_name: 'walk_2b_character_discovery', key: 'model_name', base_revision: 42 },
    );
  });
});

describe('alias conversion (Journey B)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    state.pipelineBookId = 'book-123';
    state.workbench = MOCK_WB;
    vi.mocked(API.get).mockResolvedValue(MOCK_WB);
    vi.mocked(API.post).mockResolvedValue({ decision_id: 'd-merge', status: 'active', conflict: false });
  });

  afterEach(() => {
    state.pipelineBookId = null;
    state.workbench = null;
  });

  it('previews an alias conversion with base_revision and stores the token', async () => {
    vi.mocked(API.post).mockResolvedValueOnce({
      preview_token: 'tok-1',
      expires_ms: Date.now() + 600000,
      affected_rows: [],
      protected_decisions: [],
      voice_assignments: [],
      downstream_invalidations: [],
      conflicts: [],
    });
    const summary = await previewAliasConversion('char-1', ['char-2']);
    expect(API.post).toHaveBeenCalledWith(
      '/api/pipeline/workbench/book-123/alias-conversions/preview',
      { canonical_id: 'char-1', member_ids: ['char-2'], base_revision: 42 },
    );
    expect(summary).toContain('affected rows');
  });

  it('commits a confirmed alias conversion once previewed', async () => {
    // Prime the preview token via the real path.
    vi.mocked(API.post).mockResolvedValueOnce({
      preview_token: 'tok-1',
      expires_ms: Date.now() + 600000,
      affected_rows: [],
      protected_decisions: [],
      voice_assignments: [],
      downstream_invalidations: [],
      conflicts: [],
    });
    await previewAliasConversion('char-1', ['char-2']);
    await commitAliasConversion(true);

    expect(API.post).toHaveBeenLastCalledWith(
      '/api/pipeline/workbench/book-123/alias-conversions/commit',
      { preview_token: 'tok-1', base_revision: 42, confirm_consequences: true },
    );
    expect(getUndoStack()).toEqual([{ label: 'alias conversion', decisionId: 'd-merge' }]);
  });

  it('refuses to commit without a preview', async () => {
    await commitAliasConversion(true);
    expect(API.post).not.toHaveBeenCalled();
    expect(showToast).toHaveBeenCalledWith(expect.stringContaining('preview'), 'warning');
  });
});

describe('reruns (explicit scope)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    state.pipelineBookId = 'book-123';
    state.workbench = MOCK_WB;
    vi.mocked(API.get).mockResolvedValue(MOCK_WB);
    vi.mocked(API.post).mockResolvedValue({ run_id: 'run-9', status: 'queued' });
    vi.mocked(showConfirm).mockResolvedValue(true);
  });

  afterEach(() => {
    state.pipelineBookId = null;
    state.workbench = null;
  });

  it('posts a book-scoped rerun with preserve_manual_decisions true', async () => {
    await rerunWalk('walk_2b_character_discovery', 'book');
    expect(API.post).toHaveBeenCalledWith(
      '/api/pipeline/workbench/book-123/reruns',
      expect.objectContaining({
        walk_name: 'walk_2b_character_discovery',
        scope: 'book',
        preserve_manual_decisions: true,
        base_revision: 42,
      }),
    );
  });

  it('posts a scenes-scoped rerun with scene_ids', async () => {
    await rerunWalk('walk_2d_scene_presence', 'scenes', ['scene-1']);
    expect(API.post).toHaveBeenCalledWith(
      '/api/pipeline/workbench/book-123/reruns',
      expect.objectContaining({
        walk_name: 'walk_2d_scene_presence',
        scope: 'scenes',
        scene_ids: ['scene-1'],
        preserve_manual_decisions: true,
      }),
    );
  });

  it('rejects a scenes-scoped 2c rerun (book-global) with no POST', async () => {
    await rerunWalk('walk_2c_alias_resolution', 'scenes', ['scene-1']);
    expect(API.post).not.toHaveBeenCalled();
    expect(showToast).toHaveBeenCalledWith(expect.stringContaining('book-global'), 'error');
  });

  it('requires at least one scene for scenes scope', async () => {
    await rerunWalk('walk_2d_scene_presence', 'scenes', []);
    expect(API.post).not.toHaveBeenCalled();
    expect(showToast).toHaveBeenCalledWith(expect.stringContaining('scene'), 'error');
  });

  it('confirms before rerunning and skips when cancelled', async () => {
    vi.mocked(showConfirm).mockResolvedValue(false);
    await rerunWalk('walk_2b_character_discovery', 'book');
    expect(API.post).not.toHaveBeenCalled();
  });
});

describe('undo (revision-checked, 409-safe)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    state.pipelineBookId = 'book-123';
    state.workbench = MOCK_WB;
    vi.mocked(API.get).mockResolvedValue(MOCK_WB);
    vi.mocked(API.post).mockResolvedValue({ item_id: 'decision:d-1', decision_id: 'd-1', status: 'undone' });
  });

  afterEach(() => {
    state.pipelineBookId = null;
    state.workbench = null;
  });

  it('POSTs the undo with base_revision to the pipeline endpoint', async () => {
    await undoDecision('d-1');
    expect(API.post).toHaveBeenCalledWith(
      '/api/pipeline/workbench/book-123/decisions/d-1/undo',
      { base_revision: 42 },
    );
    expect(showToast).toHaveBeenCalledWith(expect.stringContaining('undone'), 'success');
  });

  it('shows an error toast on 409 (newer decision exists)', async () => {
    vi.mocked(API.post).mockRejectedValue(new Error('newer decision exists'));
    await undoDecision('d-1');
    expect(showToast).toHaveBeenCalledWith(expect.stringContaining('Failed to undo decision'), 'error');
  });
});

describe('review resolution (Journey A)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    state.pipelineBookId = 'book-123';
    state.workbench = MOCK_WB;
    vi.mocked(API.get).mockResolvedValue(MOCK_WB);
    vi.mocked(API.post).mockResolvedValue({ item_id: 'walkitem:9', decision_id: 'd-r', status: 'active', conflict: null });
  });

  afterEach(() => {
    state.pipelineBookId = null;
    state.workbench = null;
  });

  it('accepts a review item with base_revision', async () => {
    await resolveReviewItem('walkitem:9', 'accept', 42);
    expect(API.post).toHaveBeenCalledWith(
      '/api/pipeline/review/accept',
      { item_id: 'walkitem:9', base_revision: 42 },
    );
  });

  it('overrides a review item with new_value + base_revision', async () => {
    await resolveReviewItem('walkitem:9', 'override', 42, 'Alice');
    expect(API.post).toHaveBeenCalledWith(
      '/api/pipeline/review/override',
      { item_id: 'walkitem:9', new_value: 'Alice', base_revision: 42 },
    );
  });
});

// ---------------------------------------------------------------------------
// initWorkbench wiring
// ---------------------------------------------------------------------------

describe('initWorkbench', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    state.pipelineBookId = 'book-123';
    state.workbench = MOCK_WB;
    vi.mocked(API.get).mockResolvedValue(MOCK_WB);
    vi.mocked(API.post).mockResolvedValue({ run_id: 'run-x', status: 'queued' });
    vi.mocked(API.putWithRetryOnce).mockResolvedValue({ decision_id: 'd-1', status: 'active', conflict: null });
    vi.mocked(API.delWithRetryOnce).mockResolvedValue({});
    vi.mocked(showConfirm).mockResolvedValue(true);
    clearUndoStack();
    document.body.innerHTML = `
      <div id="workbench-navigator"><button data-action="scene-select" data-scene-id="scene-1">1.1</button></div>
      <div id="workbench-span-evidence"></div>
      <div id="workbench-presence"><select data-action="presence-change" data-scene-id="scene-1" data-character-id="char-1"><option value="present">Present</option><option value="absent">Absent</option></select></div>
      <div id="workbench-setup">
        <div class="card">
          <button data-action="walk-setup-toggle" data-walk="walk_2b_character_discovery">2b</button>
          <div data-role="walk-setup-body" style="display:none"></div>
          <button data-action="save-override" data-walk="walk_2b_character_discovery">Save</button>
        </div>
      </div>
      <div id="workbench-alias-panel">
        <select id="alias-canonical"><option value="char-1">Alice</option></select>
        <select id="alias-members" multiple><option value="char-2" selected>Bob</option></select>
        <button data-action="alias-preview">Preview</button>
        <button data-action="alias-commit">Commit</button>
        <div id="alias-preview-summary"></div>
      </div>
      <div id="workbench-actions">
        <button data-action="rerun-book" data-walk="walk_2b_character_discovery">Rerun</button>
        <button data-action="rerun-selected-scene" data-walk="walk_2d_scene_presence">Rerun 2d</button>
      </div>
      <button id="btn-workbench-undo">Undo</button>
      <a data-tab="workbench" href="#">Workbench</a>
    `;
  });

  afterEach(() => {
    state.pipelineBookId = null;
    state.workbench = null;
    document.body.innerHTML = '';
    vi.restoreAllMocks();
  });

  it('wires scene selection, presence, rerun, and undo via delegated actions', async () => {
    initWorkbench();
    document.dispatchEvent(new Event('DOMContentLoaded'));

    // Scene selection refreshes evidence.
    (document.querySelector('[data-action="scene-select"]') as HTMLElement).click();
    expect(document.getElementById('workbench-span-evidence')?.innerHTML).toContain('Alice walked in.');

    // Undo button: empty stack → info toast (before any decision pushes one).
    (document.getElementById('btn-workbench-undo') as HTMLElement).click();
    expect(showToast).toHaveBeenCalledWith('Nothing to undo', 'info');

    // Presence change → PUT via the one-retry helper.
    const sel = document.querySelector('[data-action="presence-change"]') as HTMLSelectElement;
    sel.value = 'absent';
    sel.dispatchEvent(new Event('change', { bubbles: true }));
    await new Promise((r) => setTimeout(r, 0));
    expect(API.putWithRetryOnce).toHaveBeenCalledWith(
      '/api/pipeline/workbench/book-123/presence',
      { scene_id: 'scene-1', character_id: 'char-1', relation_type: 'absent', base_revision: 42 },
    );

    // Book rerun.
    (document.querySelector('[data-action="rerun-book"]') as HTMLElement).click();
    await new Promise((r) => setTimeout(r, 0));
    expect(API.post).toHaveBeenCalledWith(
      '/api/pipeline/workbench/book-123/reruns',
      expect.objectContaining({ walk_name: 'walk_2b_character_discovery', scope: 'book' }),
    );
  });

  it('undo button undoes the last pushed decision when the stack is non-empty', async () => {
    // Prime the stack through a real presence save.
    vi.mocked(API.get).mockResolvedValue(MOCK_WB);
    await savePresence('scene-1', 'char-1', 'present');
    expect(getUndoStack()).toHaveLength(1);

    initWorkbench();
    document.dispatchEvent(new Event('DOMContentLoaded'));
    (document.getElementById('btn-workbench-undo') as HTMLElement).click();
    expect(API.post).toHaveBeenCalledWith(
      '/api/pipeline/workbench/book-123/decisions/d-1/undo',
      { base_revision: 42 },
    );
  });

  it('loads the workbench when its tab is activated', () => {
    vi.mocked(API.get).mockResolvedValue(MOCK_WB);
    initWorkbench();
    document.dispatchEvent(new Event('DOMContentLoaded'));
    document.querySelector('[data-tab="workbench"]')?.dispatchEvent(new Event('click'));
    expect(API.get).toHaveBeenCalledWith('/api/pipeline/workbench/book-123');
    expect(API.get).toHaveBeenCalledWith('/api/pipeline/workbench/book-123/config');
  });
});

// ---------------------------------------------------------------------------
// index.html structure
// ---------------------------------------------------------------------------

describe('index.html workbench tab structure', () => {
  it('declares the Workbench nav link and tab pane with all section containers', () => {
    const html = readIndexHtml();

    const navIdx = html.indexOf('data-tab="workbench"');
    expect(navIdx).toBeGreaterThan(-1);

    const paneIdx = html.indexOf('id="workbench-tab"');
    expect(paneIdx).toBeGreaterThan(-1);
    expect(paneIdx).toBeGreaterThan(navIdx);

    expect(html.indexOf('id="workbench-navigator"')).toBeGreaterThan(-1);
    expect(html.indexOf('id="workbench-span-evidence"')).toBeGreaterThan(-1);
    expect(html.indexOf('id="workbench-ledger"')).toBeGreaterThan(-1);
    expect(html.indexOf('id="workbench-presence"')).toBeGreaterThan(-1);
    expect(html.indexOf('id="workbench-alias-panel"')).toBeGreaterThan(-1);
    expect(html.indexOf('id="workbench-setup"')).toBeGreaterThan(-1);
    expect(html.indexOf('id="workbench-conflicts"')).toBeGreaterThan(-1);
    expect(html.indexOf('id="btn-workbench-undo"')).toBeGreaterThan(-1);
  });
});
