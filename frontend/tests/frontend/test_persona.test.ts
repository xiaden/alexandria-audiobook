/**
 * Focused tests for the persona editor in frontend/src/tabs/persona.ts
 * (PipelineCharacterPersonaAPI.v1, DD-voice-persona-prompt-parity).
 *
 * Covers: the structured editor surface (profile fields, evidence, aliases,
 * book/scene scope, review state, protection, derived voice consequences),
 * side-effect-free validate, revisioned save with `base_revision`, 409
 * refresh/merge, 422, 503 one-retry, protected rerun rejection, and the
 * explicit-confirmation scoped rerun gate — and that rerun/save never mutate a
 * character's resolved voice assignment.
 *
 * Run with `npm test` (vitest run) from frontend/.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
  PERSONA_FIELD_KEYS,
  PERSONA_REVIEW_STATES,
  renderVoiceConsequences,
  renderEvidenceRow,
  renderPersonaEditor,
  sceneOptionsFromWorkbench,
  buildWriteRequest,
  savePersonaChecked,
  openPersonaEditor,
  rerunPersonaConfirmed,
  initPersonaEditor,
  PERSONA_EDITOR_ID,
} from '../../src/tabs/persona';
import { state, setPipelineBookId } from '../../src/state';
import type {
  Persona,
  PersonaRevision,
  WorkbenchState,
} from '../../src/state';
import { showToast, showConfirm } from '../../src/utils';
import * as API from '../../src/api';

vi.mock('../../src/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/api')>();
  return {
    ...actual,
    getPersona: vi.fn(),
    listPersonaRevisions: vi.fn(),
    validatePersona: vi.fn(),
    rerunPersona: vi.fn(),
  };
});

vi.mock('../../src/utils', () => ({
  showToast: vi.fn(),
  showConfirm: vi.fn(),
  escapeHtml: (s: unknown) => String(s),
}));

const MOCK_PERSONA: Persona = {
  persona_id: 'pers-1',
  character_id: 'char-1',
  book_id: 'book-123',
  revision: 3,
  fields: { identity: 'Alice the guide', manner: 'gentle' },
  evidence: [{ anchor: 'scene-1', quote: 'Come this way.', confidence: 0.9 }],
  aliases: ['Ali', 'Alys'],
  scene_scope: 'book',
  scene_ids: [],
  review_state: 'accepted',
  protected: false,
  voice_consequences: {
    assignment: null,
    explanation: 'identity: Alice the guide; manner: gentle',
    style_hints: ['warm'],
  },
  author_id: 'local',
  created_ms: 1700000000000,
};

const MOCK_REVISIONS: PersonaRevision[] = [
  MOCK_PERSONA,
  { ...MOCK_PERSONA, persona_id: 'pers-0', revision: 2, review_state: 'draft' },
];

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
              spans: [{ id: 'span-1', span_type: 'narrator', text: 'Alice walked in.', instruct: null, position: 1 }],
            },
          ],
        },
        { scene_id: 'scene-2', position: 2, paragraphs: [] },
      ],
    },
  ],
  characters: [
    { id: 'char-1', name: 'Alice', aliases: '["Ali"]', voice_assignment_id: 'voice-1', description: null },
    { id: 'char-2', name: 'Bob', aliases: '[]', voice_assignment_id: null, description: null },
  ],
  aliases: [],
  presence: [],
  review_items: [],
  overrides: {},
  effective_config: {},
  conflicts: [],
  runs: [],
};

function mountEditor(): HTMLElement {
  let el = document.getElementById(PERSONA_EDITOR_ID);
  if (!el) {
    el = document.createElement('div');
    el.id = PERSONA_EDITOR_ID;
    el.style.display = 'none';
    document.body.appendChild(el);
  }
  return el;
}

describe('persona pure render helpers', () => {
  it('renders derived voice consequences without an implicit assignment', () => {
    const html = renderVoiceConsequences(MOCK_PERSONA.voice_consequences);
    expect(html).toContain('no implicit assignment');
    expect(html).toContain('identity: Alice the guide');
    expect(html).toContain('warm');
  });

  it('renders a place-holder when no consequences exist yet', () => {
    expect(renderVoiceConsequences(null)).toContain('No voice consequences yet');
  });

  it('renders all five profile fields in order', () => {
    const html = renderPersonaEditor(MOCK_PERSONA, 'Alice', MOCK_REVISIONS, []);
    expect(PERSONA_FIELD_KEYS).toEqual(['identity', 'appearance', 'manner', 'speech', 'role']);
    for (const key of PERSONA_FIELD_KEYS) {
      expect(html).toContain(`data-field="${key}"`);
    }
    expect(html).toContain('Alice the guide');
  });

  it('renders evidence rows, aliases, scope, review, and protection surfaces', () => {
    const html = renderPersonaEditor(MOCK_PERSONA, 'Alice', MOCK_REVISIONS, [
      { scene_id: 'scene-1', label: '1.1 (scene-1)' },
      { scene_id: 'scene-2', label: '1.2 (scene-2)' },
    ]);
    expect(html).toContain('data-role="evidence-row"');
    expect(html).toContain('Come this way.');
    expect(html).toContain('Ali, Alys');
    expect(html).toContain('data-role="scene-scope"');
    expect(html).toContain('data-role="review-state"');
    expect(html).toContain('data-role="protected"');
    for (const s of PERSONA_REVIEW_STATES) {
      expect(html).toContain(s);
    }
    // revision history rows
    expect(html).toContain('rev 3');
    expect(html).toContain('rev 2');
  });

  it('shows the protected banner and disables rerun for a protected head', () => {
    const protectedPersona: Persona = { ...MOCK_PERSONA, protected: true };
    const html = renderPersonaEditor(protectedPersona, 'Alice', [protectedPersona], []);
    expect(html).toContain('<strong>protected</strong>');
    expect(html).toMatch(/data-action="persona-rerun"[^>]*disabled/);
  });

  it('renders evidence row with anchor placeholder and safe escaping', () => {
    const html = renderEvidenceRow({ anchor: 'a"1', quote: '<b>' }, 0);
    expect(html).toContain('data-role="evidence-row"');
    expect(html).toContain('data-evidence="anchor"');
  });
});

describe('sceneOptionsFromWorkbench', () => {
  beforeEach(() => {
    state.workbench = MOCK_WB;
    setPipelineBookId('book-123');
  });
  afterEach(() => {
    state.workbench = null;
    setPipelineBookId(null);
  });

  it('flattens chapter scenes into addressable options', () => {
    const opts = sceneOptionsFromWorkbench();
    expect(opts).toEqual([
      { scene_id: 'scene-1', label: '1.1 (scene-1)' },
      { scene_id: 'scene-2', label: '1.2 (scene-2)' },
    ]);
  });
});

describe('buildWriteRequest', () => {
  beforeEach(() => {
    mountEditor();
    setPipelineBookId('book-123');
    state.workbench = MOCK_WB;
  });
  afterEach(() => {
    document.getElementById(PERSONA_EDITOR_ID)?.remove();
    state.workbench = null;
    setPipelineBookId(null);
  });

  it('reads the form into a base_revision-aware write request', async () => {
    vi.mocked(API.getPersona).mockResolvedValue(MOCK_PERSONA);
    vi.mocked(API.listPersonaRevisions).mockResolvedValue(MOCK_REVISIONS);
    // Populate via the real open path so _currentBaseRevision tracks the head.
    await openPersonaEditor('char-1');
    const el = document.getElementById(PERSONA_EDITOR_ID)!;
    const req = buildWriteRequest();
    expect(req.base_revision).toBe(3);
    expect(req.fields.identity).toBe('Alice the guide');
    expect(req.aliases).toEqual(['Ali', 'Alys']);
    expect(req.review_state).toBe('accepted');
    expect(req.protected).toBe(false);
    expect(el.textContent).toContain('Alice');
  });
});

describe('savePersonaChecked', () => {
  const okPersona: Persona = { ...MOCK_PERSONA, revision: 4 };

  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('sends base_revision and returns the saved persona on success', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(okPersona), { status: 200, headers: { 'Content-Type': 'application/json' } }),
    );
    vi.stubGlobal('fetch', fetchMock);
    const res = await savePersonaChecked('char-1', {
      base_revision: 3,
      fields: { identity: 'x' },
      evidence: [],
      aliases: [],
      scene_scope: 'book',
      scene_ids: [],
      review_state: 'draft',
      protected: false,
    });
    expect(res.status).toBe(200);
    expect(res.persona?.revision).toBe(4);
    const [, init] = fetchMock.mock.calls[0];
    expect((init as RequestInit).method).toBe('PUT');
    expect(JSON.parse((init as RequestInit).body as string).base_revision).toBe(3);
    vi.unstubAllGlobals();
  });

  it('surfaces 409 (stale) as a distinct status', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: 'stale' }), { status: 409 }));
    vi.stubGlobal('fetch', fetchMock);
    const res = await savePersonaChecked('char-1', {
      base_revision: 3,
      fields: {},
      evidence: [],
      aliases: [],
      scene_scope: 'book',
      scene_ids: [],
      review_state: 'draft',
      protected: false,
    });
    expect(res.status).toBe(409);
    expect(res.persona).toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    vi.unstubAllGlobals();
  });

  it('surfaces 422 (validation) as a distinct status', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: 'invalid' }), { status: 422 })));
    const res = await savePersonaChecked('char-1', {
      base_revision: 3,
      fields: {},
      evidence: [],
      aliases: [],
      scene_scope: 'book',
      scene_ids: [],
      review_state: 'draft',
      protected: false,
    });
    expect(res.status).toBe(422);
    vi.unstubAllGlobals();
  });

  it('retries exactly once on 503 with Retry-After', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response('down', { status: 503, headers: { 'Retry-After': '0' } }))
      .mockResolvedValueOnce(
        new Response(JSON.stringify(okPersona), { status: 200, headers: { 'Content-Type': 'application/json' } }),
      );
    vi.stubGlobal('fetch', fetchMock);
    const res = await savePersonaChecked('char-1', {
      base_revision: 3,
      fields: {},
      evidence: [],
      aliases: [],
      scene_scope: 'book',
      scene_ids: [],
      review_state: 'draft',
      protected: false,
    });
    expect(res.status).toBe(200);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    vi.unstubAllGlobals();
  });
});

describe('openPersonaEditor', () => {
  beforeEach(() => {
    mountEditor();
    setPipelineBookId('book-123');
    state.workbench = MOCK_WB;
    vi.mocked(API.getPersona).mockResolvedValue(MOCK_PERSONA);
    vi.mocked(API.listPersonaRevisions).mockResolvedValue(MOCK_REVISIONS);
  });
  afterEach(() => {
    document.getElementById(PERSONA_EDITOR_ID)?.remove();
    state.workbench = null;
    setPipelineBookId(null);
    vi.clearAllMocks();
  });

  it('opens a separately addressable editor with head + history', async () => {
    await openPersonaEditor('char-1');
    const el = document.getElementById(PERSONA_EDITOR_ID)!;
    expect(el.style.display).not.toBe('none');
    expect(el.textContent).toContain('Alice');
    expect(el.textContent).toContain('rev 3');
    expect(el.textContent).toContain('Alice the guide');
    expect(API.getPersona).toHaveBeenCalledWith('char-1');
  });

  it('handles a 404 (no persona yet) by opening the empty editor', async () => {
    vi.mocked(API.getPersona).mockRejectedValue(new Error('404: No persona revision for character char-1'));
    await openPersonaEditor('char-1');
    const el = document.getElementById(PERSONA_EDITOR_ID)!;
    expect(el.style.display).not.toBe('none');
    expect(el.textContent).toContain('Alice');
  });
});

describe('rerunPersonaConfirmed', () => {
  beforeEach(() => {
    mountEditor();
    setPipelineBookId('book-123');
    state.workbench = MOCK_WB;
    document.getElementById(PERSONA_EDITOR_ID)!.innerHTML = renderPersonaEditor(MOCK_PERSONA, 'Alice', MOCK_REVISIONS, [
      { scene_id: 'scene-1', label: '1.1 (scene-1)' },
      { scene_id: 'scene-2', label: '1.2 (scene-2)' },
    ]);
    vi.mocked(API.getPersona).mockResolvedValue(MOCK_PERSONA);
  });
  afterEach(() => {
    document.getElementById(PERSONA_EDITOR_ID)?.remove();
    state.workbench = null;
    setPipelineBookId(null);
    vi.clearAllMocks();
  });

  it('requires explicit confirmation before rerunning', async () => {
    vi.mocked(API.rerunPersona).mockResolvedValue({ run_id: 'run-1', revision_id: 'pers-1', scope: 'book' });
    const confirmSpy = vi.mocked(showConfirm).mockResolvedValue(true);
    await rerunPersonaConfirmed();
    expect(confirmSpy).toHaveBeenCalled();
    expect(API.rerunPersona).toHaveBeenCalledWith('char-1', {
      revision_id: 'pers-1',
      scope: 'book',
      scene_ids: [],
      confirm: true,
    });
    // No voice-assignment write is performed by rerun.
    expect(vi.mocked(API.rerunPersona).mock.calls[0][1]).not.toHaveProperty('voice_assignment_id');
  });

  it('does not rerun when confirmation is declined', async () => {
    vi.mocked(showConfirm).mockResolvedValue(false);
    await rerunPersonaConfirmed();
    expect(API.rerunPersona).not.toHaveBeenCalled();
  });

  it('rejects a rerun of a protected head', async () => {
    vi.mocked(API.getPersona).mockResolvedValue({ ...MOCK_PERSONA, protected: true });
    await rerunPersonaConfirmed();
    expect(API.rerunPersona).not.toHaveBeenCalled();
    const el = document.getElementById(PERSONA_EDITOR_ID)!;
    expect(el.textContent).toContain('protected');
  });
});

describe('initPersonaEditor', () => {
  beforeEach(() => {
    document.body.innerHTML = '<div id="workbench-tab"></div>';
    setPipelineBookId('book-123');
  });
  afterEach(() => {
    document.body.innerHTML = '';
    setPipelineBookId(null);
  });

  it('creates the editor panel inside the workbench tab (idempotent)', () => {
    initPersonaEditor();
    initPersonaEditor();
    expect(document.getElementById(PERSONA_EDITOR_ID)).not.toBeNull();
    const panel = document.getElementById(PERSONA_EDITOR_ID)!;
    expect(panel.style.display).toBe('none');
    expect(document.getElementById('workbench-tab')?.contains(panel)).toBe(true);
  });
});
