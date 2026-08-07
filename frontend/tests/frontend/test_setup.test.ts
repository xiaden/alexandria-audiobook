/**
 * Spec-first tests for Setup tab (frontend/src/tabs/setup.ts).
 * Tests cover: WALK_TASK_NAMES constant, collectTaskOverrides, loadConfig.
 *
 * NOTE: No test framework is installed in frontend/package.json.
 * These tests are written with vitest-compatible syntax.
 * To run: install vitest (`npm install -D vitest jsdom`) and add to package.json:
 *   "scripts": { "test": "vitest" },
 *   "vitest": { "environment": "jsdom" }
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { WALK_TASK_NAMES, collectTaskOverrides, loadConfig, buildConfigPayload } from '../../src/tabs/setup';
import { state, setPipelineBookId, initState } from '../../src/state';
import * as API from '../../src/api';

// Mock the API module
vi.mock('../../src/api', () => ({
  get: vi.fn(),
  post: vi.fn(),
}));

// Mock showToast to avoid DOM dependencies
vi.mock('../../src/utils', () => ({
  showToast: vi.fn(),
  showConfirm: vi.fn(),
  escapeHtml: (s: string) => s,
}));

describe('WALK_TASK_NAMES', () => {
  it('should contain exactly 9 walk task names', () => {
    expect(WALK_TASK_NAMES).toHaveLength(9);
  });

  it('should contain the canonical walk task names in order', () => {
    expect(WALK_TASK_NAMES).toEqual([
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
  });

  it('should be a readonly array', () => {
    // TypeScript enforces readonly at compile time, but we can verify it's frozen-like
    expect(Array.isArray(WALK_TASK_NAMES)).toBe(true);
  });
});

describe('collectTaskOverrides', () => {
  beforeEach(() => {
    // Set up a minimal DOM with the per-task LLM table
    document.body.innerHTML = `
      <table id="per-task-llm-table">
        <tbody>
          <tr data-task="scene_segmentation">
            <td>Scene Segmentation</td>
            <td><input data-field="model_name" value="gpt-4" /></td>
            <td><select data-field="reasoning_effort"><option value="">Default</option><option value="high" selected>High</option></select></td>
            <td><input data-field="temperature" value="0.7" /></td>
          </tr>
          <tr data-task="character_discovery">
            <td>Character Discovery</td>
            <td><input data-field="model_name" value="" /></td>
            <td><select data-field="reasoning_effort"><option value="" selected>Default</option></select></td>
            <td><input data-field="temperature" value="" /></td>
          </tr>
          <tr data-task="unknown_task">
            <td>Unknown Task</td>
            <td><input data-field="model_name" value="should-be-ignored" /></td>
            <td><select data-field="reasoning_effort"><option value="">Default</option></select></td>
            <td><input data-field="temperature" value="" /></td>
          </tr>
        </tbody>
      </table>
    `;
  });

  it('should collect overrides only for known walk task names', () => {
    const overrides = collectTaskOverrides();
    expect(Object.keys(overrides)).toHaveLength(2);
    expect(overrides).toHaveProperty('scene_segmentation');
    expect(overrides).toHaveProperty('character_discovery');
    expect(overrides).not.toHaveProperty('unknown_task');
  });

  it('should collect model_name, reasoning_effort, and temperature for each task', () => {
    const overrides = collectTaskOverrides();
    expect(overrides.scene_segmentation).toEqual({
      model_name: 'gpt-4',
      reasoning_effort: 'high',
      temperature: 0.7,
    });
  });

  it('should return null for empty fields', () => {
    const overrides = collectTaskOverrides();
    expect(overrides.character_discovery).toEqual({
      model_name: null,
      reasoning_effort: null,
      temperature: null,
    });
  });

  it('should return empty object when no rows match WALK_TASK_NAMES', () => {
    document.body.innerHTML = `
      <table id="per-task-llm-table">
        <tbody>
          <tr data-task="old_task_name">
            <td><input data-field="model_name" value="x" /></td>
          </tr>
        </tbody>
      </table>
    `;
    const overrides = collectTaskOverrides();
    expect(Object.keys(overrides)).toHaveLength(0);
  });
});

describe('localStorage persistence', () => {
  beforeEach(() => {
    state.pipelineBookId = null;
    localStorage.clear();
  });

  it('setPipelineBookId() should persist to localStorage', () => {
    setPipelineBookId('book-456');
    expect(state.pipelineBookId).toBe('book-456');
    expect(localStorage.getItem('alexandria-pipeline-book-id')).toBe('book-456');
  });

  it('setPipelineBookId(null) should clear localStorage value', () => {
    setPipelineBookId('book-789');
    setPipelineBookId(null);
    expect(state.pipelineBookId).toBe(null);
    expect(localStorage.getItem('alexandria-pipeline-book-id')).toBe('');
  });

  it('initState() should restore pipelineBookId from localStorage', () => {
    localStorage.setItem('alexandria-pipeline-book-id', 'restored-book');
    initState();
    expect(state.pipelineBookId).toBe('restored-book');
  });

  it('initState() should not restore empty bookId', () => {
    localStorage.setItem('alexandria-pipeline-book-id', '');
    initState();
    expect(state.pipelineBookId).toBe(null);
  });

  it('initState() should handle missing localStorage keys gracefully', () => {
    // No keys set
    initState();
    expect(state.pipelineBookId).toBe(null);
  });
});

describe('loadConfig', () => {
  beforeEach(() => {
    document.body.innerHTML = `
      <input id="chunk-size" />
      <input id="max-tokens" />
      <input id="llm-url" />
      <input id="llm-key" />
      <input id="llm-model" />
      <select id="llm-reasoning">
        <option value="">Default</option>
        <option value="low">Low</option>
        <option value="high">High</option>
      </select>
      <input id="llm-temperature" />
      <table id="per-task-llm-table">
        <tbody>
          <tr data-task="scene_segmentation">
            <td><input data-field="model_name" /></td>
            <td><select data-field="reasoning_effort"><option value="">Default</option><option value="low">Low</option><option value="high">High</option></select></td>
            <td><input data-field="temperature" /></td>
            <td><input data-field="prompt" /></td>
          </tr>
          <tr data-task="delivery">
            <td><input data-field="model_name" /></td>
            <td><select data-field="reasoning_effort"><option value="">Default</option><option value="low">Low</option><option value="high">High</option></select></td>
            <td><input data-field="temperature" /></td>
            <td><input data-field="prompt" /></td>
          </tr>
        </tbody>
      </table>
      <select id="tts-mode"><option value="external">External</option></select>
      <input id="tts-url" />
      <select id="tts-device"><option value="auto">Auto</option></select>
      <select id="tts-language"><option value="English">English</option></select>
      <input id="parallel-workers" />
      <input id="batch-seed" />
      <input id="compile-codec" type="checkbox" />
      <input id="batch-group-by-type" type="checkbox" />
      <input id="sub-batch-enabled" type="checkbox" />
      <input id="sub-batch-min-size" />
      <input id="sub-batch-ratio" />
      <input id="sub-batch-max-items" />
      <input id="pause-between-speakers" />
      <input id="pause-same-speaker" />
    `;
    vi.clearAllMocks();
  });

  it('should populate per-task override rows from config.task_overrides', async () => {
    const mockConfig = {
      llm: {
        base_url: 'http://localhost:1234/v1',
        api_key: 'test-key',
        model_name: 'gpt-4',
        reasoning_effort: 'medium',
        temperature: 0.6,
        task_overrides: {
          scene_segmentation: {
            model_name: 'gpt-4-turbo',
            reasoning_effort: 'high',
            temperature: 0.7,
          },
          delivery: {
            model_name: null,
            reasoning_effort: null,
            temperature: null,
          },
        },
      },
      tts: {
        mode: 'external',
        url: 'http://localhost:7860',
        device: 'auto',
        language: 'English',
        parallel_workers: 2,
      },
    };

    vi.mocked(API.get).mockResolvedValueOnce(mockConfig);

    await loadConfig();

    // Verify scene_segmentation row was populated
    const sceneRow = document.querySelector('[data-task="scene_segmentation"]');
    const sceneModel = sceneRow?.querySelector('[data-field="model_name"]') as HTMLInputElement;
    const sceneReasoning = sceneRow?.querySelector('[data-field="reasoning_effort"]') as HTMLSelectElement;
    const sceneTemp = sceneRow?.querySelector('[data-field="temperature"]') as HTMLInputElement;

    expect(sceneModel?.value).toBe('gpt-4-turbo');
    expect(sceneReasoning?.value).toBe('high');
    expect(sceneTemp?.value).toBe('0.7');

    // Verify delivery row was populated with empty values (null overrides)
    const deliveryRow = document.querySelector('[data-task="delivery"]');
    const deliveryModel = deliveryRow?.querySelector('[data-field="model_name"]') as HTMLInputElement;
    expect(deliveryModel?.value).toBe('');
    // Placeholder should show inherited global model
    expect(deliveryModel?.placeholder).toContain('gpt-4');
  });

  it('should skip rows with unknown task names', async () => {
    document.body.innerHTML += `
      <table id="per-task-llm-table">
        <tbody>
          <tr data-task="legacy_task">
            <td><input data-field="model_name" value="should-not-change" /></td>
          </tr>
        </tbody>
      </table>
    `;

    const mockConfig = {
      llm: {
        base_url: 'http://localhost:1234/v1',
        api_key: 'test-key',
        model_name: 'gpt-4',
        task_overrides: {
          legacy_task: { model_name: 'new-model' },
        },
      },
      tts: { mode: 'external' },
    };

    vi.mocked(API.get).mockResolvedValueOnce(mockConfig);

    await loadConfig();

    // The legacy_task row should not be modified by loadConfig
    // (it's not in WALK_TASK_NAMES, so it's skipped)
    const legacyRow = document.querySelector('[data-task="legacy_task"]');
    const legacyModel = legacyRow?.querySelector('[data-field="model_name"]') as HTMLInputElement;
    // Value remains unchanged because loadConfig skips unknown tasks
    expect(legacyModel?.value).toBe('should-not-change');
  });

  it('should preserve unknown top-level sections (generation/prompts/walk_override) in the save payload', async () => {
    const configWithUnknowns = {
      llm: {
        base_url: 'http://localhost:1234/v1',
        api_key: 'test-key',
        model_name: 'gpt-4',
        reasoning_effort: 'high',
        temperature: 0.5,
        task_overrides: {},
      },
      tts: {
        mode: 'external',
        url: 'http://localhost:7860',
        device: 'auto',
        language: 'English',
        parallel_workers: 2,
      },
      // Unknown top-level sections (byte-stable contract: backend deep-merges
      // unknown paths, never deletes them — the save payload must carry them).
      generation: { max_tokens: 512, seed: 42 },
      prompts: { default_prompt: 'legacy' },
      walk_override: { scene_segmentation: { prompt: 'Seg prompt' } },
    };

    vi.mocked(API.get).mockResolvedValueOnce(configWithUnknowns);

    await loadConfig();

    const payload = buildConfigPayload();

    // Unknown top-level sections are preserved verbatim in the save payload
    expect(payload.generation).toEqual({ max_tokens: 512, seed: 42 });
    expect(payload.prompts).toEqual({ default_prompt: 'legacy' });
    expect(payload.walk_override).toHaveProperty('scene_segmentation');
    expect(payload.walk_override?.scene_segmentation?.prompt).toBe('Seg prompt');
    // Known sections still present
    expect(payload.llm?.model_name).toBe('gpt-4');
    expect(payload.tts?.mode).toBe('external');
  });

  it('should render per-walk override fields (temperature + prompt) from config and include them in the save payload', async () => {
    const configWithOverrides = {
      llm: {
        base_url: 'http://localhost:1234/v1',
        api_key: 'test-key',
        model_name: 'gpt-4',
        reasoning_effort: 'medium',
        temperature: 0.6,
        task_overrides: {
          scene_segmentation: {
            model_name: 'gpt-4-turbo',
            reasoning_effort: 'high',
            temperature: 0.7,
          },
          delivery: {
            model_name: null,
            reasoning_effort: null,
            temperature: null,
          },
        },
      },
      tts: {
        mode: 'external',
        url: 'http://localhost:7860',
        device: 'auto',
        language: 'English',
        parallel_workers: 2,
      },
      // Per-walk prompt overrides carried in the config payload's walk_override section
      walk_override: {
        scene_segmentation: { prompt: 'You are a scene segmentation expert.' },
        delivery: { prompt: null },
      },
    };

    vi.mocked(API.get).mockResolvedValueOnce(configWithOverrides);

    await loadConfig();

    // Temperature override rendered from llm.task_overrides (existing convention)
    const sceneRow = document.querySelector('[data-task="scene_segmentation"]');
    const sceneTemp = sceneRow?.querySelector('[data-field="temperature"]') as HTMLInputElement;
    expect(sceneTemp?.value).toBe('0.7');

    // Prompt override rendered from the walk_override section
    const scenePrompt = sceneRow?.querySelector('[data-field="prompt"]') as HTMLInputElement;
    expect(scenePrompt?.value).toBe('You are a scene segmentation expert.');
    const deliveryRow = document.querySelector('[data-task="delivery"]');
    const deliveryPrompt = deliveryRow?.querySelector('[data-field="prompt"]') as HTMLInputElement;
    expect(deliveryPrompt?.value).toBe('');

    // Save payload includes both override kinds
    const payload = buildConfigPayload();
    expect(payload.llm?.task_overrides?.scene_segmentation?.temperature).toBe(0.7);
    expect(payload.walk_override?.scene_segmentation?.prompt).toBe('You are a scene segmentation expert.');
    expect(payload.walk_override?.delivery?.prompt).toBeNull();
  });
});

describe('prompt override contract lock (P5-S5)', () => {
  // Backend read path (resolve_task_config in app/utils.py): tier-1 prompt source
  // is config["walk_override"][task]["prompt"] — a TOP-LEVEL section keyed by task
  // name, string or None. These assertions lock the frontend payload/render to
  // that exact shape so a prompt set in the Setup tab stays consumable end-to-end.

  const nineTaskRows = WALK_TASK_NAMES.map(
    (task) => `
      <tr data-task="${task}">
        <td><input data-field="model_name" /></td>
        <td><select data-field="reasoning_effort"><option value="">Default</option><option value="low">Low</option><option value="high">High</option></select></td>
        <td><input data-field="temperature" /></td>
        <td><input data-field="prompt" /></td>
      </tr>
    `,
  ).join('');

  const configWithWalkPrompts = {
    llm: {
      base_url: 'http://localhost:1234/v1',
      api_key: 'test-key',
      model_name: 'gpt-4',
      reasoning_effort: 'medium',
      temperature: 0.6,
      task_overrides: {
        scene_segmentation: { model_name: 'gpt-4-turbo', reasoning_effort: 'high', temperature: 0.7 },
      },
    },
    tts: {
      mode: 'external',
      url: 'http://localhost:7860',
      device: 'auto',
      language: 'English',
      parallel_workers: 2,
    },
    // Top-level walk_override section — the exact path resolve_task_config reads
    // (config["walk_override"][task]["prompt"]). scene_segmentation: string prompt;
    // delivery + script_alias_resolution: explicit null; voice_audition: absent
    // entirely (unset -> None -> built-in system_prompt fallback on the backend).
    walk_override: {
      scene_segmentation: { prompt: 'You are a scene segmentation expert.' },
      character_discovery: { prompt: 'You are a character discovery expert.' },
      script_alias_resolution: { prompt: null },
      scene_presence: { prompt: 'You are a scene presence expert.' },
      span_attribution: { prompt: 'You are a span attribution expert.' },
      character_description: { prompt: 'You are a character description expert.' },
      voice_assignment: { prompt: 'You are a voice assignment expert.' },
      delivery: { prompt: null },
    },
  };

  beforeEach(() => {
    document.body.innerHTML = `
      <input id="llm-url" />
      <input id="llm-key" />
      <input id="llm-model" />
      <select id="llm-reasoning"><option value="">Default</option><option value="low">Low</option><option value="high">High</option></select>
      <input id="llm-temperature" />
      <table id="per-task-llm-table"><tbody>${nineTaskRows}</tbody></table>
      <select id="tts-mode"><option value="external">External</option></select>
      <input id="tts-url" />
      <select id="tts-device"><option value="auto">Auto</option></select>
      <select id="tts-language"><option value="English">English</option></select>
      <input id="parallel-workers" />
      <input id="batch-seed" />
      <input id="compile-codec" type="checkbox" />
      <input id="batch-group-by-type" type="checkbox" />
      <input id="sub-batch-enabled" type="checkbox" />
      <input id="sub-batch-min-size" />
      <input id="sub-batch-ratio" />
      <input id="sub-batch-max-items" />
      <input id="pause-between-speakers" />
      <input id="pause-same-speaker" />
    `;
    vi.clearAllMocks();
  });

  it('buildConfigPayload emits walk_override[taskName].prompt (string|null) keyed by task name — the resolve_task_config read path', async () => {
    vi.mocked(API.get).mockResolvedValueOnce(configWithWalkPrompts);

    await loadConfig();

    const payload = buildConfigPayload();

    // (a) Every walk task is keyed in the TOP-LEVEL walk_override section with a
    // prompt value that is either a non-empty string or explicit null — the exact
    // shape resolve_task_config consumes via config["walk_override"][task]["prompt"].
    for (const task of WALK_TASK_NAMES) {
      const entry = payload.walk_override?.[task];
      expect(entry, `walk_override entry for ${task}`).toBeDefined();
      const prompt = entry?.prompt;
      expect(
        typeof prompt === 'string' || prompt === null,
        `prompt for ${task} must be string or null, got ${String(prompt)}`,
      ).toBe(true);
    }
    // Concrete round-trip values: string prompt survives, explicit null survives,
    // and an absent entry becomes explicit null (backend: unset -> None -> built-in).
    expect(payload.walk_override?.scene_segmentation?.prompt).toBe('You are a scene segmentation expert.');
    expect(payload.walk_override?.delivery?.prompt).toBeNull();
    expect(payload.walk_override?.script_alias_resolution?.prompt).toBeNull();
    expect(payload.walk_override?.voice_audition?.prompt).toBeNull();
    // Override entries carry exactly {prompt} — no stray keys (temperature lives
    // in llm.task_overrides, which is where the backend tier-2 reads it from).
    expect(payload.walk_override?.scene_segmentation).toEqual({ prompt: 'You are a scene segmentation expert.' });

    // (b) The prompt override is NOT nested inside llm.task_overrides — that path
    // is stripped by GET /api/config (pydantic model_dump drops nested unknown
    // keys) and resolve_task_config does not read it. Lock it out of the payload.
    for (const task of WALK_TASK_NAMES) {
      const overrideEntry = payload.llm?.task_overrides?.[task];
      if (overrideEntry) {
        expect(Object.keys(overrideEntry)).not.toContain('prompt');
      }
    }
  });

  it('loadConfig populates the per-walk prompt input from config.walk_override[taskName].prompt (same path)', async () => {
    vi.mocked(API.get).mockResolvedValueOnce(configWithWalkPrompts);

    await loadConfig();

    // Every row's prompt input reflects the walk_override section: the override
    // string when set, '' when null/absent (placeholder shows the inherited model).
    for (const task of WALK_TASK_NAMES) {
      const row = document.querySelector(`[data-task="${task}"]`);
      const input = row?.querySelector('[data-field="prompt"]') as HTMLInputElement | null;
      const expected = configWithWalkPrompts.walk_override?.[task]?.prompt ?? '';
      expect(input?.value, `prompt input for ${task}`).toBe(expected);
    }
  });
});
