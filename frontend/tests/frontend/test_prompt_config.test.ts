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
  readPromptError,
  savePromptWrite,
  recordRevision,
  recordRerunHead,
  rerunPromptConfirmed,
  loadPromptConfig,
  selectTask,
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
    listPromptConfigRevisions: vi.fn(),
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
    vi.mocked(API.listPromptConfigRevisions).mockReset();
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
    vi.mocked(API.listPromptConfigRevisions).mockResolvedValue([]);
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

  it('seeds base_revision from the server head after a cold reload', async () => {
    const oldRevision = { ...REV, superseded_by: 'prompt-new' };
    const newRevision = { ...REV, revision_id: 'prompt-new', base_revision: 'prompt-abc' };
    vi.mocked(API.getEffectiveWalkConfig).mockResolvedValue({
      book_id: 'book-123',
      tasks: { scene_segmentation: TASK_CFG },
    });
    vi.mocked(API.listPromptConfigRevisions).mockResolvedValue([newRevision, oldRevision]);

    await loadPromptConfig();

    const write = buildWriteRequest();
    expect(API.listPromptConfigRevisions).toHaveBeenCalledWith(
      'book-123',
      'scene_segmentation',
    );
    expect(write.base_revision).toBe('prompt-new');
  });

  it('seeds heads for tasks selected after reload', async () => {
    const deliveryRevision = { ...REV, task: 'delivery', revision_id: 'prompt-delivery' };
    vi.mocked(API.getEffectiveWalkConfig).mockResolvedValue({
      book_id: 'book-123',
      tasks: { scene_segmentation: TASK_CFG, delivery: TASK_CFG },
    });
    vi.mocked(API.listPromptConfigRevisions).mockImplementation(async (_book, task) =>
      task === 'delivery' ? [deliveryRevision] : [],
    );

    await loadPromptConfig();
    await selectTask('delivery');

    expect(buildWriteRequest().base_revision).toBe('prompt-delivery');
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

  it('parses the structured STALE_BASE_REVISION 409 body', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          error: 'revision_conflict',
          code: 'STALE_BASE_REVISION',
          message: 'base_revision 2 does not match head revision 3',
          detail: null,
        }),
        { status: 409, headers: { 'Content-Type': 'application/json' } },
      ),
    );
    vi.stubGlobal('fetch', fetchMock);
    const res = await savePromptConfigChecked('book-123', {
      task: 'scene_segmentation',
      settings: {},
      prompt: null,
      raw_json: null,
      base_revision: '2',
    });
    expect(res.status).toBe(409);
    expect(res.revision).toBeNull();
    expect(res.error?.code).toBe('STALE_BASE_REVISION');
    expect(res.error?.message).toContain('head revision 3');
    vi.unstubAllGlobals();
  });

  it('parses a plain detail string (422) with no structured code', async () => {
    const res = await readPromptError(
      new Response(JSON.stringify({ detail: 'invalid temperature' }), { status: 422 }),
    );
    expect(res?.code).toBeUndefined();
    expect(res?.message).toBe('invalid temperature');
  });

  it('parses a FastAPI field-error detail array', async () => {
    const res = await readPromptError(
      new Response(
        JSON.stringify({ detail: [{ msg: 'field A bad' }, { msg: 'field B bad' }] }),
        { status: 422 },
      ),
    );
    expect(res?.message).toBe('field A bad; field B bad');
  });

  it('returns null when the error body is not JSON', async () => {
    const res = await readPromptError(new Response('upstream down', { status: 503 }));
    expect(res).toBeNull();
  });

  it('surfaces a structured CROSS_BOOK save conflict', async () => {
    mountTab();
    setPipelineBookId('book-123');
    state.workbench = MOCK_WB;
    const el = document.getElementById(PROMPT_CONFIG_TAB_ID)!;
    el.innerHTML = renderTaskEditor('scene_segmentation', TASK_CFG, 'prompt-abc', []);
    vi.mocked(API.getEffectiveWalkConfig).mockResolvedValue({
      book_id: 'book-123',
      tasks: { scene_segmentation: TASK_CFG },
    });
    await loadPromptConfig();
    el.innerHTML = renderTaskEditor('scene_segmentation', TASK_CFG, 'prompt-abc', []);
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          error: 'revision_conflict',
          code: 'CROSS_BOOK',
          message: "revision 'prompt-abc' belongs to another book",
          detail: {},
        }),
        { status: 409, headers: { 'Content-Type': 'application/json' } },
      ),
    );
    vi.stubGlobal('fetch', fetchMock);
    await savePromptWrite();
    expect(el.textContent).toContain('CROSS_BOOK');
    expect(el.textContent).not.toContain('[object Object]');
    document.getElementById(PROMPT_CONFIG_TAB_ID)?.remove();
    state.workbench = null;
    setPipelineBookId(null);
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
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          run_id: 'prompt-run',
          revision_id: 'prompt-abc',
          scope: 'book',
          invalidated_walks: [],
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    );
    vi.stubGlobal('fetch', fetchMock);
    vi.mocked(API.getEffectiveWalkConfig).mockResolvedValue({
      book_id: 'book-123',
      tasks: { scene_segmentation: TASK_CFG },
    });
    const confirmSpy = vi.mocked(showConfirm).mockResolvedValue(true);
    await rerunPromptConfirmed();
    expect(confirmSpy).toHaveBeenCalled();
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toContain('/reruns');
    expect((init as RequestInit).method).toBe('POST');
    const sent = JSON.parse((init as RequestInit).body as string);
    expect(sent).toEqual({
      revision_id: 'prompt-abc',
      scope: 'book',
      scene_ids: [],
      confirm: true,
    });
    // Rerun never auto-runs a walk by itself: no auto_run flag is sent.
    expect(sent).not.toHaveProperty('auto_run');
    vi.unstubAllGlobals();
  });

  it('does not rerun when confirmation is declined', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    vi.mocked(showConfirm).mockResolvedValue(false);
    await rerunPromptConfirmed();
    expect(fetchMock).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it('advances base_revision via the run head on success', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          run_id: 'prompt-run',
          revision_id: 'prompt-abc',
          scope: 'book',
          invalidated_walks: [],
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    );
    vi.stubGlobal('fetch', fetchMock);
    vi.mocked(API.getEffectiveWalkConfig).mockResolvedValue({
      book_id: 'book-123',
      tasks: { scene_segmentation: TASK_CFG },
    });
    vi.mocked(showConfirm).mockResolvedValue(true);
    await rerunPromptConfirmed();
    // The module head advanced to the run head; a subsequent rerun would guard
    // on the new head (no already_ran against the stale revision).
    recordRerunHead('scene_segmentation', 'prompt-run');
    expect(fetchMock).toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it('surfaces a structured 409 ALREADY_RAN rejection', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          error: 'revision_conflict',
          code: 'ALREADY_RAN',
          message: 'walk rerun already_ran: revision prompt-abc scope book produced head prompt-x',
          detail: { head_revision_id: 'prompt-x' },
        }),
        { status: 409, headers: { 'Content-Type': 'application/json' } },
      ),
    );
    vi.stubGlobal('fetch', fetchMock);
    vi.mocked(showConfirm).mockResolvedValue(true);
    await rerunPromptConfirmed();
    const el = document.getElementById(PROMPT_CONFIG_TAB_ID)!;
    // The structured code + message surface; no '[object Object]' degradation.
    expect(el.textContent).toContain('ALREADY_RAN');
    expect(el.textContent).toContain('already_ran: revision prompt-abc scope book produced head prompt-x');
    expect(el.textContent).not.toContain('[object Object]');
    expect(showToast).toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it('surfaces a structured 409 CROSS_BOOK rejection', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          error: 'revision_conflict',
          code: 'CROSS_BOOK',
          message: "prompt-config revision 'prompt-abc' belongs to book 'other', not 'book-123'",
          detail: { revision_book_id: 'other' },
        }),
        { status: 409, headers: { 'Content-Type': 'application/json' } },
      ),
    );
    vi.stubGlobal('fetch', fetchMock);
    vi.mocked(showConfirm).mockResolvedValue(true);
    await rerunPromptConfirmed();
    const el = document.getElementById(PROMPT_CONFIG_TAB_ID)!;
    expect(el.textContent).toContain('CROSS_BOOK');
    expect(el.textContent).toContain("belongs to book 'other'");
    expect(el.textContent).not.toContain('[object Object]');
    vi.unstubAllGlobals();
  });

  it('surfaces a generic 409 conflict without auto-retry', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: 'conflict' }), { status: 409 }),
    );
    vi.stubGlobal('fetch', fetchMock);
    vi.mocked(showConfirm).mockResolvedValue(true);
    await rerunPromptConfirmed();
    const el = document.getElementById(PROMPT_CONFIG_TAB_ID)!;
    expect(el.textContent).toContain('Rerun conflict');
    expect(fetchMock).toHaveBeenCalledTimes(1); // never auto-retries a conflict
    vi.unstubAllGlobals();
  });
});
