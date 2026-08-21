/**
 * Deterministic browser-style journey tests (Playwright-*analogous* under
 * Vitest/jsdom) — TASK-voice-persona-prompt-parity C, P4-S1.
 *
 * No real browser, no real TTS engine, no timing-dependent assertions: every
 * journey is driven with fixed media fixtures (`tests/fixtures/media.ts`),
 * mocked fetch / API helpers, and the already-installed jsdom media stubs
 * (vitest.setup.ts). Each journey exercises a real end-to-end UI flow through
 * the production tab modules:
 *
 *   1. Clone voice — upload (multipart + ref_text) → preview → assign → delete
 *   2. Persona     — edit → validate → protect → explicit scoped rerun
 *   3. Prompt cfg  — compare → structured edit → raw validate → save → confirm rerun
 *   4. Deterministic playback/download/form/marker/error (seek ordering, 4xx/5xx)
 *   5. No unavailable-engine false-green — unavailable capability surfaces an
 *      error, never a silent pass
 *
 * The fixture module also carries its own determinism tests (run twice =
 * identical bytes/duration), satisfying the "deterministic" gate.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
  createWavBytes,
  wavDurationMs,
  fakeAudioUrl,
  FakeGenerateEngine,
  EngineUnavailableError,
  WAV_HEADER_BYTES,
} from '../fixtures/media';
import {
  registerVoiceCatalog,
  renderCloneReferencePanel,
  resetCloneReferencePanel,
  uploadCloneReferenceFromPanel,
  deleteCloneReferenceConfirmed,
  handleCharacterVoiceChange,
  playCloneReference,
  createCloneReferenceRow,
  loadCloneReferences,
} from '../../src/tabs/voices';
import type { CloneReference } from '../../src/state';
import type { VoiceConfigRow } from '../../src/state';
import {
  openPersonaEditor,
  buildWriteRequest as buildPersonaWriteRequest,
  validatePersonaWrite,
  savePersonaWrite,
  rerunPersonaConfirmed,
  renderPersonaEditor,
  PERSONA_EDITOR_ID,
} from '../../src/tabs/persona';
import {
  loadPromptConfig,
  recordRevision,
  validatePromptWrite,
  savePromptWrite,
  rerunPromptConfirmed,
  buildWriteRequest as buildPromptWriteRequest,
  PROMPT_TASK_ORDER,
  PROMPT_CONFIG_TAB_ID,
} from '../../src/tabs/prompt-config';
import { previewVoice } from '../../src/tabs/voices';
import { renderSceneNavigator } from '../../src/tabs/workbench';
import { state, setPipelineBookId } from '../../src/state';
import type {
  Persona,
  PersonaRevision,
  EffectiveWalkTask,
  PromptConfigRevision,
  WorkbenchState,
} from '../../src/state';
import { showToast, showConfirm } from '../../src/utils';
import * as API from '../../src/api';

// Mock the API helpers the journeys hit, and utils (toast/confirm/escape).
vi.mock('../../src/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/api')>();
  return {
    ...actual,
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    del: vi.fn(),
    listCloneReferences: vi.fn(),
    uploadCloneReference: vi.fn(),
    deleteCloneReference: vi.fn(),
    getPersona: vi.fn(),
    listPersonaRevisions: vi.fn(),
    validatePersona: vi.fn(),
    getEffectiveWalkConfig: vi.fn(),
    validatePromptConfig: vi.fn(),
  };
});

vi.mock('../../src/utils', () => ({
  showToast: vi.fn(),
  showConfirm: vi.fn(),
  escapeHtml: (s: unknown) => String(s),
}));

// ---------------------------------------------------------------------------
// Shared fixtures
// ---------------------------------------------------------------------------

const MOCK_REF: CloneReference = {
  reference_id: 'ref-1',
  voice_id: 'voice-1',
  owner_id: 'local',
  relative_path: 'voices/voice-1/ref-1.wav',
  original_filename: 'sample.wav',
  media_type: 'audio/wav',
  byte_size: 2048,
  duration_ms: 2000,
  sha256: 'abc',
  created_ms: 1700000000000,
};

const MOCK_VOICES: VoiceConfigRow[] = [
  { id: 'voice-1', name: 'Alice', voice: 'Alice', type: 'clone' },
  { id: 'voice-2', name: 'Bob', voice: 'Bob', type: 'custom' },
  { id: 'NARRATOR', name: 'Narrator', voice: 'Ryan', type: null },
];

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
  voice_consequences: { assignment: null, explanation: 'identity: Alice the guide', style_hints: ['warm'] },
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
        { scene_id: 'scene-1', position: 1, paragraphs: [] },
        { scene_id: 'scene-2', position: 2, paragraphs: [] },
      ],
    },
  ],
  characters: [
    { id: 'char-1', name: 'Alice', aliases: '["Ali"]', voice_assignment_id: 'voice-1', description: null },
  ],
  aliases: [],
  presence: [],
  review_items: [],
  overrides: {},
  effective_config: {},
  conflicts: [],
  runs: [],
};

const TASK_CFG: EffectiveWalkTask = {
  values: { model_name: 'gpt-4o-mini', reasoning_effort: 'medium', temperature: 0, prompt: 'Segment the scenes.' },
  sources: { model_name: 'global', reasoning_effort: 'task', temperature: 'row', prompt: 'config' },
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

/** Reset the pipeline book id + workbench after each case. */
function resetPipeline(): void {
  setPipelineBookId(null);
  state.workbench = null;
}

// ---------------------------------------------------------------------------
// Media fixture — determinism (run twice ⇒ identical bytes/duration)
// ---------------------------------------------------------------------------

describe('media fixture (deterministic)', () => {
  it('produces byte-identical WAV output across runs', () => {
    const a = createWavBytes(2000, 8000);
    const b = createWavBytes(2000, 8000);
    expect(a.byteLength).toBe(b.byteLength);
    expect(Buffer.from(a).equals(Buffer.from(b))).toBe(true);
    expect(a.byteLength).toBeGreaterThan(WAV_HEADER_BYTES);
  });

  it('wavDurationMs round-trips the fixture duration', () => {
    expect(wavDurationMs(createWavBytes(2000, 8000))).toBe(2000);
    expect(wavDurationMs(createWavBytes(0, 8000))).toBe(0);
  });

  it('fakeAudioUrl is stable for a given voice+seed', () => {
    expect(fakeAudioUrl('voice-1', 1)).toBe(fakeAudioUrl('voice-1', 1));
    expect(fakeAudioUrl('voice-1', 1)).not.toBe(fakeAudioUrl('voice-2', 1));
  });

  it('FakeGenerateEngine returns stable audio when available', async () => {
    const engine = new FakeGenerateEngine('voice-1');
    const r1 = await engine.generate({ voiceId: 'voice-1', sampleText: 'hi' });
    const r2 = await engine.generate({ voiceId: 'voice-1', sampleText: 'hi' });
    expect(r1.durationMs).toBe(2000);
    expect(r1.mediaType).toBe('audio/wav');
    expect(Buffer.from(r1.bytes).equals(Buffer.from(r2.bytes))).toBe(true);
    expect(engine.calls).toHaveLength(2);
  });
});

// ---------------------------------------------------------------------------
// Journey 1 — clone voice: upload → preview → assign → delete
// ---------------------------------------------------------------------------

describe('Journey 1 — clone voice upload→preview→assign→delete', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    document.body.innerHTML = `
      <div id="toast-container"></div>
      <div id="pipeline-voices-section" style="display:none;">
        <div id="voice-catalog"></div>
        <div id="character-ledger"></div>
      </div>
    `;
    registerVoiceCatalog(MOCK_VOICES);
    // Default the list to empty so the panel's background load stays clean.
    vi.mocked(API.listCloneReferences).mockResolvedValue({ references: [] });
    // Select voice-1 so the upload flow has a resolved voice id.
    renderCloneReferencePanel('voice-1');
    vi.mocked(showConfirm).mockResolvedValue(true);
  });

  afterEach(() => {
    document.body.innerHTML = '';
    vi.resetAllMocks();
    resetPipeline();
  });

  it('uploads a fixture WAV (multipart) with required ref_text and refreshes the list', async () => {
    const file = new File([createWavBytes(2000, 8000)], 'sample.wav', { type: 'audio/wav' });
    const input = document.getElementById('clone-ref-audio-file') as HTMLInputElement;
    Object.defineProperty(input, 'files', { value: [file], configurable: true });
    (document.getElementById('clone-ref-text') as HTMLInputElement).value = 'aligned transcript';

    vi.mocked(API.uploadCloneReference).mockResolvedValue({
      reference: MOCK_REF,
      voice: { id: 'voice-1', name: 'Alice', voice: 'Alice', type: 'clone', ref_audio: 'voices/voice-1/ref-1.wav' },
    });
    vi.mocked(API.listCloneReferences).mockResolvedValue({ references: [MOCK_REF] });

    const button = document.querySelector<HTMLButtonElement>('[data-action="clone-ref-upload"]')!;
    await uploadCloneReferenceFromPanel(button);

    expect(API.uploadCloneReference).toHaveBeenCalledWith('voice-1', file, 'sample.wav', 'aligned transcript');
    expect(showToast).toHaveBeenCalledWith(expect.stringContaining('Reference uploaded'), 'success');
    expect(API.listCloneReferences).toHaveBeenCalledWith('voice-1');
    const list = document.getElementById('clone-reference-list');
    expect(list?.querySelector('[data-reference-id="ref-1"]')).not.toBeNull();
  });

  it('surfaces an upload error (5xx) accessibly instead of silently passing', async () => {
    const file = new File([createWavBytes(2000, 8000)], 'sample.wav', { type: 'audio/wav' });
    const input = document.getElementById('clone-ref-audio-file') as HTMLInputElement;
    Object.defineProperty(input, 'files', { value: [file], configurable: true });
    (document.getElementById('clone-ref-text') as HTMLInputElement).value = 'aligned transcript';
    vi.mocked(API.uploadCloneReference).mockRejectedValue(new Error('Failed to generate preview: 500'));

    const button = document.querySelector<HTMLButtonElement>('[data-action="clone-ref-upload"]')!;
    await uploadCloneReferenceFromPanel(button);
    expect(showToast).toHaveBeenCalledWith(expect.stringContaining('Failed to upload reference'), 'error');
  });

  it('assigns the clone voice by resolving the display name to a voice_config id', async () => {
    vi.mocked(API.put).mockResolvedValue(undefined);
    handleCharacterVoiceChange('char-1', 'Alice');
    expect(API.put).toHaveBeenCalledWith('/api/pipeline/characters/char-1/voice', { voice_assignment_id: 'voice-1' });
    await Promise.resolve(); // flush the put().then() toast microtask
    expect(showToast).toHaveBeenCalledWith(expect.stringContaining('Voice assigned'), 'success');
  });

  it('deletes a reference only after explicit confirmation', async () => {
    vi.mocked(API.deleteCloneReference).mockResolvedValue(undefined);
    vi.mocked(API.listCloneReferences).mockResolvedValue({ references: [] });
    await deleteCloneReferenceConfirmed('voice-1', 'ref-1');
    expect(showConfirm).toHaveBeenCalled();
    expect(API.deleteCloneReference).toHaveBeenCalledWith('voice-1', 'ref-1');
    expect(showToast).toHaveBeenCalledWith('Clone reference deleted', 'success');
  });

  it('aborts deletion when confirmation is declined', async () => {
    vi.mocked(showConfirm).mockResolvedValue(false);
    await deleteCloneReferenceConfirmed('voice-1', 'ref-1');
    expect(API.deleteCloneReference).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// Journey 2 — persona: edit → validate → protect → explicit scoped rerun
// ---------------------------------------------------------------------------

describe('Journey 2 — persona edit→validate→protect→explicit scoped rerun', () => {
  function mountEditor(): void {
    let el = document.getElementById(PERSONA_EDITOR_ID);
    if (!el) {
      el = document.createElement('div');
      el.id = PERSONA_EDITOR_ID;
      document.body.appendChild(el);
    }
  }

  beforeEach(() => {
    vi.resetAllMocks();
    mountEditor();
    setPipelineBookId('book-123');
    state.workbench = MOCK_WB;
    vi.mocked(API.getPersona).mockResolvedValue(MOCK_PERSONA);
    vi.mocked(API.listPersonaRevisions).mockResolvedValue(MOCK_REVISIONS);
    vi.mocked(showConfirm).mockResolvedValue(true);
  });

  afterEach(() => {
    document.getElementById(PERSONA_EDITOR_ID)?.remove();
    vi.resetAllMocks();
    resetPipeline();
  });

  it('edits a structured field, validates side-effect-free, then saves with base_revision', async () => {
    await openPersonaEditor('char-1');
    const el = document.getElementById(PERSONA_EDITOR_ID)!;
    const field = el.querySelector<HTMLInputElement>('[data-field="identity"]');
    expect(field).not.toBeNull();
    field!.value = 'Alice the wiser guide';

    vi.mocked(API.validatePersona).mockResolvedValue({
      valid: true,
      errors: [],
      voice_consequences: { assignment: null, explanation: 'identity: Alice the wiser guide', style_hints: ['warm'] },
    });
    await validatePersonaWrite();
    expect(API.validatePersona).toHaveBeenCalled();
    expect(showToast).toHaveBeenCalledWith(expect.stringContaining('valid'), 'success');

    // Save via the real fetch path — assert base_revision is sent.
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ ...MOCK_PERSONA, revision: 4 }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    vi.stubGlobal('fetch', fetchMock);
    await savePersonaWrite();
    const [, init] = fetchMock.mock.calls[0];
    expect((init as RequestInit).method).toBe('PUT');
    expect(JSON.parse((init as RequestInit).body as string).base_revision).toBe(3);
    expect(showToast).toHaveBeenCalledWith(expect.stringContaining('revision 4 saved'), 'success');
    vi.unstubAllGlobals();
  });

  it('protects a head and then blocks an explicit scoped rerun before any request', async () => {
    // Head becomes protected (edit save).
    vi.mocked(API.getPersona).mockResolvedValue({ ...MOCK_PERSONA, protected: true });
    await openPersonaEditor('char-1');
    document.getElementById(PERSONA_EDITOR_ID)!.innerHTML = renderPersonaEditor(
      { ...MOCK_PERSONA, protected: true },
      'Alice',
      MOCK_REVISIONS,
      [],
    );

    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    await rerunPersonaConfirmed();
    expect(fetchMock).not.toHaveBeenCalled();
    const el = document.getElementById(PERSONA_EDITOR_ID)!;
    expect(el.textContent).toContain('protected');
    expect(showToast).toHaveBeenCalledWith(expect.stringContaining('rerun blocked'), 'error');
    vi.unstubAllGlobals();
  });

  it('explicit scoped rerun sends confirm:true and never auto-cascades a voice assignment', async () => {
    await openPersonaEditor('char-1');
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({ run_id: 'run-1', revision_id: 'pers-1', scope: 'book' }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    );
    vi.stubGlobal('fetch', fetchMock);
    await rerunPersonaConfirmed();
    expect(showConfirm).toHaveBeenCalled();
    const [, init] = fetchMock.mock.calls[0];
    expect((init as RequestInit).method).toBe('POST');
    const body = JSON.parse((init as RequestInit).body as string);
    expect(body).toEqual({ revision_id: 'pers-1', scope: 'book', scene_ids: [], confirm: true });
    expect(body).not.toHaveProperty('voice_assignment_id');
    expect(showToast).toHaveBeenCalledWith(expect.stringContaining('run-1'), 'success');
    vi.unstubAllGlobals();
  });

  it('surfaces a structured PROTECTED_REVISION save conflict without [object Object]', async () => {
    await openPersonaEditor('char-1');
    document.getElementById(PERSONA_EDITOR_ID)!.innerHTML = renderPersonaEditor(
      MOCK_PERSONA,
      'Alice',
      MOCK_REVISIONS,
      [],
    );
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          error: 'revision_conflict',
          code: 'PROTECTED_REVISION',
          message: "protected persona revision 'pers-1' cannot be replaced by an edit",
          detail: { character_id: 'char-1', head_persona_id: 'pers-1' },
        }),
        { status: 409, headers: { 'Content-Type': 'application/json' } },
      ),
    );
    vi.stubGlobal('fetch', fetchMock);
    await savePersonaWrite();
    const el = document.getElementById(PERSONA_EDITOR_ID)!;
    // The structured conflict's message surfaces; no '[object Object]'.
    expect(el.textContent).toContain('Protected persona');
    expect(el.textContent).toContain('cannot be replaced by an edit');
    expect(el.textContent).not.toContain('[object Object]');
    expect(showToast).toHaveBeenCalledWith(expect.stringContaining('save rejected'), 'error');
    vi.unstubAllGlobals();
  });
});

// ---------------------------------------------------------------------------
// Journey 3 — prompt: compare → structured edit → raw validate → save → rerun
// ---------------------------------------------------------------------------

describe('Journey 3 — prompt compare→structured edit→raw validate→save→confirm rerun', () => {
  function mountTab(): void {
    let el = document.getElementById(PROMPT_CONFIG_TAB_ID);
    if (!el) {
      el = document.createElement('div');
      el.id = PROMPT_CONFIG_TAB_ID;
      document.body.appendChild(el);
    }
  }

  beforeEach(() => {
    vi.resetAllMocks();
    mountTab();
    setPipelineBookId('book-123');
    state.workbench = MOCK_WB;
    recordRevision(REV);
    vi.mocked(API.getEffectiveWalkConfig).mockResolvedValue({
      book_id: 'book-123',
      tasks: { scene_segmentation: TASK_CFG },
    });
    vi.mocked(showConfirm).mockResolvedValue(true);
  });

  afterEach(() => {
    document.getElementById(PROMPT_CONFIG_TAB_ID)?.remove();
    vi.resetAllMocks();
    resetPipeline();
  });

  it('compares all nine walks with source badges, then structured-edits temperature=0.0', async () => {
    await loadPromptConfig();
    const el = document.getElementById(PROMPT_CONFIG_TAB_ID)!;
    expect(PROMPT_TASK_ORDER).toHaveLength(9);
    expect(el.textContent).toContain('Scene Segmentation');
    expect(el.textContent).toContain('DB override'); // temperature source = row
    expect(el.textContent).toContain('global'); // model_name source

    const temp = el.querySelector<HTMLInputElement>('[data-role="pc-temperature"]');
    temp!.value = '0';
    const model = el.querySelector<HTMLInputElement>('[data-role="pc-model"]');
    model!.value = '';
    const write = buildPromptWriteRequest();
    expect(write.task).toBe('scene_segmentation');
    expect(write.settings['temperature']).toBe(0);
    expect(write.prompt).toBe('Segment the scenes.');
    expect(write.base_revision).toBe('prompt-abc');
  });

  it('raw-validates a guarded allow-listed JSON edit (side-effect-free)', async () => {
    await loadPromptConfig();
    const el = document.getElementById(PROMPT_CONFIG_TAB_ID)!;
    const mode = el.querySelector<HTMLSelectElement>('[data-role="pc-mode"]');
    mode!.value = 'raw';
    const raw = el.querySelector<HTMLTextAreaElement>('[data-role="pc-raw"]');
    raw!.value = '{"model_name":"gpt","temperature":0.0,"prompt":"go"}';
    vi.mocked(API.validatePromptConfig).mockResolvedValue({ valid: true, errors: [] });

    await validatePromptWrite();
    expect(API.validatePromptConfig).toHaveBeenCalled();
    expect(showToast).toHaveBeenCalledWith(expect.stringContaining('valid'), 'success');
  });

  it('rejects an unknown raw key before any network call (guarded allow-list)', async () => {
    await loadPromptConfig();
    const el = document.getElementById(PROMPT_CONFIG_TAB_ID)!;
    const mode = el.querySelector<HTMLSelectElement>('[data-role="pc-mode"]');
    mode!.value = 'raw';
    const raw = el.querySelector<HTMLTextAreaElement>('[data-role="pc-raw"]');
    raw!.value = '{"top_p":0.9}';

    await validatePromptWrite();
    expect(API.validatePromptConfig).not.toHaveBeenCalled();
    expect(showToast).toHaveBeenCalledWith(expect.stringContaining('Raw JSON rejected'), 'error');
  });

  it('saves a revision via the real fetch path and records the new head', async () => {
    await loadPromptConfig();
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ ...REV, revision_id: 'prompt-new' }), {
        status: 201,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    vi.stubGlobal('fetch', fetchMock);
    await savePromptWrite();
    const [, init] = fetchMock.mock.calls[0];
    expect((init as RequestInit).method).toBe('POST');
    expect(JSON.parse((init as RequestInit).body as string).base_revision).toBe('prompt-abc');
    expect(showToast).toHaveBeenCalledWith('Prompt config revision saved', 'success');
    vi.unstubAllGlobals();
  });

  it('confirm-reruns with confirm:true and never auto-runs a walk', async () => {
    await loadPromptConfig();
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({ run_id: 'prompt-run', revision_id: 'prompt-abc', scope: 'book', invalidated_walks: [] }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    );
    vi.stubGlobal('fetch', fetchMock);
    await rerunPromptConfirmed();
    expect(showConfirm).toHaveBeenCalled();
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toContain('/reruns');
    expect((init as RequestInit).method).toBe('POST');
    const sent = JSON.parse((init as RequestInit).body as string);
    expect(sent).toEqual({ revision_id: 'prompt-abc', scope: 'book', scene_ids: [], confirm: true });
    expect(sent).not.toHaveProperty('auto_run');
    expect(showToast).toHaveBeenCalledWith(expect.stringContaining('prompt-run'), 'success');
    vi.unstubAllGlobals();
  });
});

// ---------------------------------------------------------------------------
// Journey 4 — deterministic playback/download/form/marker/error behavior
// ---------------------------------------------------------------------------

describe('Journey 4 — deterministic playback/download/form/marker/error', () => {
  afterEach(() => {
    document.body.innerHTML = '';
    vi.resetAllMocks();
  });

  it('playback: clone preview awaits loadedmetadata before seeking currentTime (seek ordering)', async () => {
    document.body.innerHTML = `
      <div id="pipeline-voices-section">
        <audio id="clone-audio-ref-1" controls preload="metadata"></audio>
      </div>
    `;
    const audio = document.getElementById('clone-audio-ref-1') as HTMLAudioElement;
    const playSpy = vi.spyOn(audio, 'play').mockResolvedValue(undefined);
    const loadSpy = vi.spyOn(audio, 'load').mockImplementation(() => {
      audio.dispatchEvent(new Event('loadedmetadata'));
    });
    const currentTimeSpy = vi.spyOn(audio, 'currentTime', 'set');

    await playCloneReference('voice-1', 'ref-1');
    expect(loadSpy).toHaveBeenCalled();
    expect(currentTimeSpy).toHaveBeenCalledWith(0);
    expect(playSpy).toHaveBeenCalled();
    expect(audio.src).toContain('/references/ref-1/preview');
  });

  it('download: clone reference row exposes an attachment link to the download endpoint', () => {
    const html = createCloneReferenceRow(MOCK_REF);
    expect(html).toMatch(/<a[^>]*download[^>]*>/);
    expect(html).toContain('/references/ref-1/download');
    expect(html).toContain('sample.wav');
  });

  it('form: upload guards require a voice and a chosen file before any network call', async () => {
    document.body.innerHTML = `
      <div id="toast-container"></div>
      <div id="pipeline-voices-section" style="display:none;">
        <div id="voice-catalog"></div>
        <div id="character-ledger"></div>
      </div>
    `;
    renderCloneReferencePanel();
    resetCloneReferencePanel();
    const button = document.createElement('button');
    document.body.appendChild(button);

    // No voice selected.
    await uploadCloneReferenceFromPanel(button);
    expect(showToast).toHaveBeenCalledWith(expect.stringContaining('Select a clone voice'), 'warning');

    // Voice selected but no file chosen.
    registerVoiceCatalog(MOCK_VOICES);
    renderCloneReferencePanel('voice-1');
    await uploadCloneReferenceFromPanel(button);
    expect(showToast).toHaveBeenCalledWith(expect.stringContaining('Choose an audio file'), 'warning');
    expect(API.uploadCloneReference).not.toHaveBeenCalled();
    resetCloneReferencePanel();
  });

  it('marker: workbench selected scene is conveyed by aria-current and a non-color text marker', () => {
    const html = renderSceneNavigator(MOCK_WB, 'scene-1');
    const selBtn = html.match(/data-scene-id="scene-1"[^>]*>/)?.[0] ?? '';
    expect(selBtn).toContain('aria-current="true"');
    // Non-color state encoding: the selected scene carries a visually-hidden
    // 'selected' text marker in addition to the aria-current attribute.
    expect(html).toContain('visually-hidden">selected');
    const unselected = html.match(/data-scene-id="scene-2"[^>]*>/)?.[0] ?? '';
    expect(unselected).toContain('aria-current="false"');
  });

  it('error: previewVoice surfaces a 5xx engine failure instead of a false green', async () => {
    vi.mocked(API.post).mockRejectedValue(new Error('Failed to generate preview: 500'));
    const button = document.createElement('button');
    button.innerHTML = '<i class="fas fa-play"></i>';
    document.body.appendChild(button);
    await previewVoice('voice-1', button);
    expect(showToast).toHaveBeenCalledWith(expect.stringContaining('Failed to generate preview'), 'error');
    expect(showToast).not.toHaveBeenCalledWith(expect.stringContaining('success'), 'success');
  });

  it('error: a failed clone-reference list clears to empty and surfaces the error accessibly', async () => {
    document.body.innerHTML = `
      <div id="pipeline-voices-section">
        <div id="clone-reference-list"></div>
      </div>
    `;
    vi.mocked(API.listCloneReferences).mockRejectedValue(new Error('upstream 502'));
    await loadCloneReferences('voice-1');
    expect(showToast).toHaveBeenCalledWith(expect.stringContaining('upstream 502'), 'error');
    expect(document.getElementById('clone-reference-list')!.textContent).toContain('No clone references');
  });
});

// ---------------------------------------------------------------------------
// Journey 5 — no unavailable-engine false-green
// ---------------------------------------------------------------------------

describe('Journey 5 — no unavailable-engine false-green', () => {
  class MockAudio {
    src = '';
    static instances: MockAudio[] = [];
    playCount = 0;
    constructor(url?: string) {
      this.src = url ?? '';
      MockAudio.instances.push(this);
    }
    async play(): Promise<void> {
      this.playCount += 1;
    }
    pause(): void {
      /* no-op */
    }
  }

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.resetAllMocks();
    document.body.innerHTML = '';
  });

  it('available engine: preview generates and plays (success surfaced)', async () => {
    const engine = new FakeGenerateEngine('voice-1');
    vi.stubGlobal('Audio', MockAudio);
    MockAudio.instances = [];
    vi.mocked(API.post).mockImplementation(async () => {
      const r = await engine.generate({ voiceId: 'voice-1', sampleText: 'sample' });
      return { audio_url: r.audioUrl, voice_id: 'voice-1' };
    });
    const button = document.createElement('button');
    button.innerHTML = '<i class="fas fa-play"></i>';
    document.body.appendChild(button);

    await previewVoice('voice-1', button);
    expect(showToast).toHaveBeenCalledWith('Preview generated successfully', 'success');
    expect(MockAudio.instances.length).toBe(1);
    expect(MockAudio.instances[0].playCount).toBe(1);
    expect(engine.calls).toHaveLength(1);
  });

  it('unavailable engine: the generate seam surfaces an error — never a silent green pass', async () => {
    const engine = new FakeGenerateEngine('voice-1');
    engine.unavailable = true; // capability missing
    vi.stubGlobal('Audio', MockAudio);
    MockAudio.instances = [];
    vi.mocked(API.post).mockImplementation(async () => {
      const r = await engine.generate({ voiceId: 'voice-1', sampleText: 'sample' });
      return { audio_url: r.audioUrl, voice_id: 'voice-1' };
    });
    const button = document.createElement('button');
    button.innerHTML = '<i class="fas fa-play"></i>';
    document.body.appendChild(button);

    await previewVoice('voice-1', button);
    // The unavailable capability must be surfaced as an error, not swallowed.
    expect(showToast).toHaveBeenCalledWith(expect.stringContaining('unavailable'), 'error');
    expect(showToast).not.toHaveBeenCalledWith(expect.stringContaining('success'), 'success');
    // No audio was played — the UI did not silently "pass" an unavailable engine.
    expect(MockAudio.instances).toHaveLength(0);
    expect(engine.calls).toHaveLength(1);
  });

  it('FakeGenerateEngine throws EngineUnavailableError when unavailable (the fake contract)', async () => {
    const engine = new FakeGenerateEngine('voice-1');
    engine.unavailable = true;
    await expect(engine.generate({ voiceId: 'voice-1', sampleText: 'x' })).rejects.toBeInstanceOf(
      EngineUnavailableError,
    );
  });
});
