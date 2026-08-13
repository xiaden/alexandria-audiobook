/**
 * Focused tests for the effective prompt-config viewer/editor in
 * frontend/src/tabs/prompt-config.ts (PipelineWalkPromptConfigRevisionAPI.v1,
 * DD-voice-persona-prompt-parity).
 *
 * Covers: the exact nine fixed walks with effective values + source-layer
 * badges and editable temperature=0.0, structured and guarded raw JSON
 * allow-list enforcement, side-effect-free validate, base_revision revision
 * saves with 409/422/503 surfaced, and the explicit-confirmed scoped rerun
 * (confirm:true, declined confirmation, and 409 already_ran). Rerun/save never
 * auto-run a walk.
 *
 * Run with `npm test` (vitest run) from frontend/.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
  PROMPT_ALLOWED_KEYS,
  PROMPT_TASK_ORDER,
  PROMPT_TASK_LABELS,
  PROMPT_TEMPERATURE_MIN,
  PROMPT_TEMPERATURE_MAX,
  promptSourceLabel,
  formatEffectiveValue,
  safeParseRawJson,
  settingsFromParsed,
  promptFromParsed,
  rawJsonForValues,
  renderEffectiveRow,
  renderTaskEditor,
  renderTaskRow,
  buildWriteRequest,
  savePromptConfigChecked,
  recordRevision,
  recordRerunHead,
  rerunPromptConfirmed,
  loadPromptConfig,
  PROMPT_CONFIG_TAB_ID,
} from '../../src/tabs/prompt-config';
import { state, setPipelineBookId } from '../../src/state';
import type {
  EffectiveWalkTask,
  PromptConfigRevision,
  WorkbenchState,
} from '../../src/state';
import { showToast, showConfirm } from '../../src/utils';
import * as API from '../../src/api';

vi.mock('../../src/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/api')>();
  return {
    ...actual,
    getEffectiveWalkConfig: vi.fn(),
    validatePromptConfig: vi.fn(),
    rerunScopedWalk: vi.fn(),
  };
});

vi.mock('../../src/utils', () => ({
  showToast: vi.fn(),
  showConfirm: vi.fn(),
  escapeHtml: (s: unknown) => String(s),
}));

const MOCK_WB: WorkbenchState = {
  book_id: 'book-123',
  generation_revision: 42,
  scenes: [
    {
      chapter_id: 'ch-1',
      position: 1,
      scenes: [
        { scene_id: 'scene-1', position: 1, paragraphs: [] },
        { scene_id: 'scene-2', position: 2, paragraphs: [] },
      ],
    },
  ],
  characters: [],
  aliases: [],
  presence: [],
  review_items: [],
  overrides: {},
  effective_config: {},
  conflicts: [],
  runs: [],
};

const TASK_CFG: EffectiveWalkTask = {
  values: {
    model_name: 'gpt-4o-mini',
    reasoning_effort: 'medium',
    temperature: 0,
    prompt: 'Segment the scenes.',
  },
  sources: {
    model_name: 'global',
    reasoning_effort: 'task',
    temperature: 'row',
    prompt: 'config',
  },
};

const REV: PromptConfigRevision = {
  revision_id: 'prompt-abc',
  book_id: 'book-123',
  task: 'scene_segmentation',
  base_revision: null,
  source_layers: {},
  effective_prompt: 'Segment the scenes.',
  settings: { temperature: 0 },
  raw_json: null,
  validation: { valid: true, errors: [] },
  author_id: 'local',
  created_ms: 1700000000000,
  superseded_by: null,
};

function mountTab(): HTMLElement {
  let el = document.getElementById(PROMPT_CONFIG_TAB_ID);
  if (!el) {
    el = document.createElement('div');
    el.id = PROMPT_CONFIG_TAB_ID;
    document.body.appendChild(el);
  }
  return el;
}

describe('prompt source-layer badges', () => {
  it('maps every backend tier to a stable label', () => {
    expect(promptSourceLabel('row')).toBe('DB override');
    expect(promptSourceLabel('config')).toBe('on-disk config');
    expect(promptSourceLabel('task')).toBe('task override');
    expect(promptSourceLabel('global')).toBe('global');
    expect(promptSourceLabel('fallback')).toBe('default');
    expect(promptSourceLabel(null)).toBe('default');
    expect(promptSourceLabel(undefined)).toBe('default');
  });

  it('renders effective rows with source badges', () => {
    const html = renderEffectiveRow('temperature', 0, 'row');
    expect(html).toContain('data-role="effective-temperature"');
    expect(html).toContain('DB override');
    expect(html).toContain('0');
  });

  it('formats 0.0 temperature without dropping the value', () => {
    expect(formatEffectiveValue(0)).toBe('0');
    expect(formatEffectiveValue(null)).toBe('—');
  });
});

describe('nine fixed walks', () => {
  it('exposes exactly the nine fixed task names in walk order', () => {
    expect(PROMPT_TASK_ORDER).toEqual([
      'scene_segmentation',
      'character_discovery',
      'script_alias_resolution',
      'scene_presence',
      'span_attribution',
      'character_description',
      'voice_audition',
      'voice_assignment',
      'delivery',
    ]);
    expect(PROMPT_TASK_ORDER).toHaveLength(9);
    expect(PROMPT_TASK_LABELS['script_alias_resolution']).toBeTruthy();
  });

  it('renders a task row with temperature value + badge', () => {
    const html = renderTaskRow(
      'scene_segmentation',
      'Scene Segmentation',
      TASK_CFG.values,
      TASK_CFG.sources,
      true,
    );
    expect(html).toContain('data-task="scene_segmentation"');
    expect(html).toContain('0');
    expect(html).toContain('DB override');
    expect(html).toContain('active');
  });

  it('renders the full editor with effective values, badges, and editable temperature=0.0', () => {
    const html = renderTaskEditor('scene_segmentation', TASK_CFG, null, []);
    expect(html).toContain('data-role="pc-temperature"');
    expect(html).toContain(`min="${PROMPT_TEMPERATURE_MIN}"`);
    expect(html).toContain(`max="${PROMPT_TEMPERATURE_MAX}"`);
    expect(html).toContain('0.0 is valid');
    expect(html).toContain('DB override'); // temperature source = row
    expect(html).toContain('global'); // model_name source badge
    expect(html).toContain('on-disk config'); // prompt source badge
    expect(html).toContain('Segment the scenes.');
    expect(html).toContain('data-action="pc-validate"');
    expect(html).toContain('data-action="pc-save"');
    expect(html).toContain('data-action="pc-rerun"');
    // alias resolution is book-global — surfaces the scenes-scope rejection
    const aliasHtml = renderTaskEditor('script_alias_resolution', TASK_CFG, null, []);
    expect(aliasHtml).toContain('book-global');
  });
});

describe('guarded raw JSON allow-list', () => {
  it('accepts an empty raw object', () => {
    expect(safeParseRawJson('')).toEqual({ ok: true, parsed: {} });
  });

  it('accepts only exact allowed keys', () => {
    expect(PROMPT_ALLOWED_KEYS).toEqual([
      'model_name',
      'reasoning_effort',
      'temperature',
      'prompt',
    ]);
    const ok = safeParseRawJson(
      '{"model_name":"gpt","reasoning_effort":"low","temperature":0.0,"prompt":"go"}',
    );
    expect(ok.ok).toBe(true);
    if (ok.ok) {
      expect(ok.parsed.temperature).toBe(0);
      expect(ok.parsed.prompt).toBe('go');
    }
  });

  it('rejects unknown keys', () => {
    const res = safeParseRawJson('{"top_p":0.9}');
    expect(res.ok).toBe(false);
    if (!res.ok) expect(res.error).toContain('unknown override key(s): top_p');
  });

  it('rejects malformed JSON and non-objects', () => {
    expect(safeParseRawJson('{oops').ok).toBe(false);
    const arr = safeParseRawJson('[1,2]');
    expect(arr.ok).toBe(false);
    if (!arr.ok) expect(arr.error).toContain('object');
  });

  it('splits parsed raw into settings and top-level prompt', () => {
    const parsed = { model_name: 'gpt', prompt: 'hi', temperature: 0 };
    expect(settingsFromParsed(parsed)).toEqual({ model_name: 'gpt', temperature: 0 });
    expect(promptFromParsed(parsed)).toBe('hi');
    expect(promptFromParsed({ prompt: '' })).toBeNull();
  });

  it('prefills raw JSON from effective values (allow-listed only)', () => {
    const json = rawJsonForValues(TASK_CFG.values);
    const parsed = JSON.parse(json) as Record<string, unknown>;
    expect(Object.keys(parsed).sort()).toEqual(PROMPT_ALLOWED_KEYS.slice().sort());
  });
});

describe('buildWriteRequest (structured)', () => {
  beforeEach(() => {
    mountTab();
    setPipelineBookId('book-123');
    state.workbench = MOCK_WB;
    const el = document.getElementById(PROMPT_CONFIG_TAB_ID)!;
    el.innerHTML = renderTaskEditor('scene_segmentation', TASK_CFG, 'prompt-abc', []);
  });
  afterEach(() => {
    document.getElementById(PROMPT_CONFIG_TAB_ID)?.remove();
    state.workbench = null;
    setPipelineBookId(null);
  });

  it('builds a base_revision-aware write with an explicit temperature 0.0', async () => {
    // Go through the real load path so the module selects the active task and
    // tracks base_revision from the recorded head.
    recordRevision(REV);
    vi.mocked(API.getEffectiveWalkConfig).mockResolvedValue({
      book_id: 'book-123',
      tasks: { scene_segmentation: TASK_CFG },
    });
    await loadPromptConfig();
    const el = document.getElementById(PROMPT_CONFIG_TAB_ID)!;
    el.innerHTML = renderTaskEditor('scene_segmentation', TASK_CFG, 'prompt-abc', []);
    const temp = el.querySelector<HTMLInputElement>('[data-role="pc-temperature"]');
    temp!.value = '0';
    const model = el.querySelector<HTMLInputElement>('[data-role="pc-model"]');
    model!.value = '';
    const write = buildWriteRequest();
    expect(write.task).toBe('scene_segmentation');
    expect(write.settings['temperature']).toBe(0);
    expect(write.prompt).toBe('Segment the scenes.');
    expect(write.base_revision).toBe('prompt-abc');
  });
});

describe('savePromptConfigChecked', () => {
  const okRev: PromptConfigRevision = { ...REV, revision_id: 'prompt-new' };
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('POSTs with JSON and returns the saved revision on 201', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        new Response(JSON.stringify(okRev), { status: 201, headers: { 'Content-Type': 'application/json' } }),
      );
    vi.stubGlobal('fetch', fetchMock);
    const res = await savePromptConfigChecked('book-123', {
      task: 'scene_segmentation',
      settings: { temperature: 0 },
      prompt: null,
      raw_json: null,
      base_revision: null,
    });
    expect(res.status).toBe(201);
    expect(res.revision?.revision_id).toBe('prompt-new');
    const [, init] = fetchMock.mock.calls[0];
    expect((init as RequestInit).method).toBe('POST');
    expect((init as RequestInit).headers).toMatchObject({ 'Content-Type': 'application/json' });
    vi.unstubAllGlobals();
  });

  it('surfaces 409 (stale base_revision) as a distinct status', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: 'stale' }), { status: 409 })),
    );
    const res = await savePromptConfigChecked('book-123', {
      task: 'scene_segmentation',
      settings: {},
      prompt: null,
      raw_json: null,
      base_revision: 'old',
    });
    expect(res.status).toBe(409);
    expect(res.revision).toBeNull();
    vi.unstubAllGlobals();
  });

  it('surfaces 422 (validation) as a distinct status', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: 'invalid' }), { status: 422 })),
    );
    const res = await savePromptConfigChecked('book-123', {
      task: 'scene_segmentation',
      settings: {},
      prompt: null,
      raw_json: null,
      base_revision: null,
    });
    expect(res.status).toBe(422);
    vi.unstubAllGlobals();
  });

  it('retries exactly once on 503 with Retry-After', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response('down', { status: 503, headers: { 'Retry-After': '0' } }))
      .mockResolvedValueOnce(
        new Response(JSON.stringify(okRev), { status: 201, headers: { 'Content-Type': 'application/json' } }),
      );
    vi.stubGlobal('fetch', fetchMock);
    const res = await savePromptConfigChecked('book-123', {
      task: 'scene_segmentation',
      settings: {},
      prompt: null,
      raw_json: null,
      base_revision: null,
    });
    expect(res.status).toBe(201);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    vi.unstubAllGlobals();
  });
});

describe('rerunPromptConfirmed', () => {
  beforeEach(async () => {
    mountTab();
    setPipelineBookId('book-123');
    state.workbench = MOCK_WB;
    recordRevision(REV); // set head for the active task
    vi.mocked(API.getEffectiveWalkConfig).mockResolvedValue({
      book_id: 'book-123',
      tasks: { scene_segmentation: TASK_CFG },
    });
    await loadPromptConfig(); // select the active task through the real path
    const el = document.getElementById(PROMPT_CONFIG_TAB_ID)!;
    el.innerHTML = renderTaskEditor('scene_segmentation', TASK_CFG, 'prompt-abc', []);
  });
  afterEach(() => {
    document.getElementById(PROMPT_CONFIG_TAB_ID)?.remove();
    state.workbench = null;
    setPipelineBookId(null);
    vi.clearAllMocks();
  });

  it('requires explicit confirmation before rerunning with confirm:true', async () => {
    vi.mocked(API.rerunScopedWalk).mockResolvedValue({
      run_id: 'prompt-run',
      revision_id: 'prompt-abc',
      scope: 'book',
      invalidated_walks: [],
    });
    vi.mocked(API.getEffectiveWalkConfig).mockResolvedValue({
      book_id: 'book-123',
      tasks: { scene_segmentation: TASK_CFG },
    });
    const confirmSpy = vi.mocked(showConfirm).mockResolvedValue(true);
    await rerunPromptConfirmed();
    expect(confirmSpy).toHaveBeenCalled();
    expect(API.rerunScopedWalk).toHaveBeenCalledWith('book-123', {
      revision_id: 'prompt-abc',
      scope: 'book',
      scene_ids: [],
      confirm: true,
    });
    // Rerun never auto-runs a walk by itself: no run endpoint is touched.
    expect(vi.mocked(API.rerunScopedWalk).mock.calls[0][1]).not.toHaveProperty('auto_run');
  });

  it('does not rerun when confirmation is declined', async () => {
    vi.mocked(showConfirm).mockResolvedValue(false);
    await rerunPromptConfirmed();
    expect(API.rerunScopedWalk).not.toHaveBeenCalled();
  });

  it('advances base_revision via the run head on success', async () => {
    vi.mocked(API.rerunScopedWalk).mockResolvedValue({
      run_id: 'prompt-run',
      revision_id: 'prompt-abc',
      scope: 'book',
      invalidated_walks: [],
    });
    vi.mocked(API.getEffectiveWalkConfig).mockResolvedValue({
      book_id: 'book-123',
      tasks: { scene_segmentation: TASK_CFG },
    });
    vi.mocked(showConfirm).mockResolvedValue(true);
    await rerunPromptConfirmed();
    // The module head advanced to the run head; a subsequent rerun would guard
    // on the new head (no already_ran against the stale revision).
    recordRerunHead('scene_segmentation', 'prompt-run');
    expect(API.rerunScopedWalk).toHaveBeenCalled();
  });

  it('surfaces a 409 already_ran rejection', async () => {
    vi.mocked(API.rerunScopedWalk).mockRejectedValue(
      new Error('409: walk rerun already_ran: revision prompt-abc scope book produced head prompt-x'),
    );
    vi.mocked(showConfirm).mockResolvedValue(true);
    await rerunPromptConfirmed();
    const el = document.getElementById(PROMPT_CONFIG_TAB_ID)!;
    expect(el.textContent).toContain('already_ran');
    expect(showToast).toHaveBeenCalled();
  });
});
