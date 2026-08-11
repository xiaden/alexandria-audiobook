/**
 * Setup tab module — Configuration panel with per-task LLM overrides and
 * per-walk prompt overrides. Save preserves unknown top-level config sections
 * (generation/prompts/walk_override) verbatim — byte-stable round-trip.
 * Ported from app/static/index.html lines 96-416 (HTML), 1251-1520 (JS)
 */

import * as API from '../api';
import { showToast } from '../utils';
import { state } from '../state';

/** Pause bound (mirror of the backend validate_pause_ms / PAUSE_MAX_MS=10_000). */
const PAUSE_MAX_MS = 10000;

/**
 * Canonical walk task names matching WalkRunner.WALK_ORDER in runner.py.
 * These are the data-task attribute values used in the per-task LLM overrides
 * table. The short names (without walk_2x_ prefix) match what the backend
 * walks pass to resolve_task_config().
 */
export const WALK_TASK_NAMES: readonly string[] = [
  'scene_segmentation',
  'character_discovery',
  'script_alias_resolution',
  'scene_presence',
  'span_attribution',
  'character_description',
  'voice_audition',
  'voice_assignment',
  'delivery',
] as const;

/** Config shape returned by GET /api/config */
interface TaskOverride {
  model_name: string | null;
  reasoning_effort: string | null;
  temperature?: number | null;
}

interface LLMConfig {
  base_url: string;
  api_key: string;
  model_name: string;
  reasoning_effort?: string | null;
  temperature?: number;
  task_overrides?: Record<string, TaskOverride>;
}

interface TTSConfig {
  mode?: string;
  url?: string;
  device?: string;
  language?: string;
  parallel_workers?: number;
  batch_seed?: number | null;
  compile_codec?: boolean;
  batch_group_by_type?: boolean;
  sub_batch_enabled?: boolean;
  sub_batch_min_size?: number | null;
  sub_batch_ratio?: number | null;
  sub_batch_max_items?: number | null;
  pause_between_speakers_ms?: number | null;
  pause_same_speaker_ms?: number | null;
}

interface AppConfig {
  llm: LLMConfig;
  tts: TTSConfig;
  schema_version?: number;
  walk_override?: Record<string, WalkOverride>;
  /** Unknown top-level sections (generation, prompts, ...) preserved byte-stable. */
  [key: string]: unknown;
}

/** Per-walk override entry carried in the config payload's walk_override section (keyed by task name). */
interface WalkOverride {
  temperature?: number | null;
  prompt?: string | null;
}

/**
 * Full raw config from the last GET /api/config — retained so a save payload
 * can carry unknown top-level sections (generation/prompts/walk_override)
 * forward verbatim (byte-stable contract, CONTRACTS.md rule #11).
 */
let retainedConfig: AppConfig | null = null;

/** Human-readable label for a reasoning_effort value ('' / null = 'Default'). */
function reasoningLabel(value: string | null | undefined): string {
  switch (value) {
    case 'none': return 'None';
    case 'low': return 'Low';
    case 'medium': return 'Medium';
    case 'high': return 'High';
    default: return 'Default';
  }
}

/**
 * Toggle TTS mode between local and external server.
 * Shows/hides relevant form groups based on selected mode.
 */
function toggleTTSMode(): void {
  const mode = (document.getElementById('tts-mode') as HTMLSelectElement).value;
  const urlGroup = document.getElementById('tts-url-group');
  const deviceGroup = document.getElementById('tts-device-group');
  const localOptions = document.getElementById('tts-local-options');

  if (urlGroup) urlGroup.style.display = mode === 'external' ? '' : 'none';
  if (deviceGroup) deviceGroup.style.display = mode === 'local' ? '' : 'none';
  if (localOptions) localOptions.style.display = mode === 'local' ? '' : 'none';
}

/**
 * Load configuration from the server and populate all form fields.
 * Fetches GET /api/config and populates LLM settings, the per-task LLM
 * override table, and TTS settings.
 */
export async function loadConfig(): Promise<void> {
  try {
    const config = await API.get<AppConfig>('/api/config');

    // Retain the FULL raw config (unknown top-level sections included) so the
    // save payload can carry them forward — byte-stable round-trip contract.
    retainedConfig = config;

    // LLM settings
    const llmUrlEl = document.getElementById('llm-url') as HTMLInputElement;
    const llmKeyEl = document.getElementById('llm-key') as HTMLInputElement;
    const llmModelEl = document.getElementById('llm-model') as HTMLInputElement;
    if (llmUrlEl) llmUrlEl.value = config.llm.base_url;
    if (llmKeyEl) llmKeyEl.value = config.llm.api_key;
    if (llmModelEl) llmModelEl.value = config.llm.model_name;

    const llmReasoningEl = document.getElementById('llm-reasoning') as HTMLSelectElement;
    if (llmReasoningEl) llmReasoningEl.value = config.llm.reasoning_effort || '';

    const llmTemperatureEl = document.getElementById('llm-temperature') as HTMLInputElement;
    if (llmTemperatureEl && config.llm.temperature != null) {
      llmTemperatureEl.value = String(config.llm.temperature);
    }

    // Populate per-task LLM overrides for the 9 walk task names
    const taskRows = document.querySelectorAll<HTMLTableRowElement>('#per-task-llm-table tbody tr');
    taskRows.forEach(row => {
      const taskName = row.getAttribute('data-task');
      if (!taskName || !WALK_TASK_NAMES.includes(taskName)) return;
      const taskConfig = config.llm?.task_overrides?.[taskName as string];
      const modelInput = row.querySelector<HTMLInputElement>('[data-field="model_name"]');
      const reasoningSelect = row.querySelector<HTMLSelectElement>('[data-field="reasoning_effort"]');
      const temperatureInput = row.querySelector<HTMLInputElement>('[data-field="temperature"]');
      if (modelInput) {
        modelInput.value = (taskConfig && taskConfig.model_name) ? taskConfig.model_name : '';
        // Surface the inherited global model so per-task inheritance is visible
        modelInput.placeholder = `Inherit: ${config.llm.model_name}`;
      }
      if (reasoningSelect) {
        reasoningSelect.value = (taskConfig && taskConfig.reasoning_effort) ? taskConfig.reasoning_effort : '';
        // Label the Inherit option with the resolved global reasoning value
        const inheritOpt = reasoningSelect.querySelector<HTMLOptionElement>('option[value=""]');
        if (inheritOpt) inheritOpt.textContent = `Inherit (${reasoningLabel(config.llm.reasoning_effort)})`;
      }
      if (temperatureInput) {
        temperatureInput.value = (taskConfig && taskConfig.temperature != null) ? String(taskConfig.temperature) : '';
        // Surface the inherited global temperature so per-task inheritance is visible
        temperatureInput.placeholder = `Inherit: ${config.llm.temperature != null ? config.llm.temperature : 0.6}`;
      }
      // Per-walk prompt override from the config payload's walk_override section
      const promptInput = row.querySelector<HTMLInputElement>('[data-field="prompt"]');
      if (promptInput) {
        const walkOverride = config.walk_override?.[taskName as string];
        promptInput.value = (walkOverride && walkOverride.prompt) ? walkOverride.prompt : '';
        // Surface the inherited global model so per-walk inheritance is visible
        promptInput.placeholder = `Inherit: ${config.llm.model_name}`;
      }
    });

    // TTS settings
    const ttsModeEl = document.getElementById('tts-mode') as HTMLSelectElement;
    const ttsUrlEl = document.getElementById('tts-url') as HTMLInputElement;
    const ttsDeviceEl = document.getElementById('tts-device') as HTMLSelectElement;
    const ttsLanguageEl = document.getElementById('tts-language') as HTMLSelectElement;
    const parallelWorkersEl = document.getElementById('parallel-workers') as HTMLInputElement;
    const batchSeedEl = document.getElementById('batch-seed') as HTMLInputElement;

    if (ttsModeEl) ttsModeEl.value = config.tts.mode || 'external';
    if (ttsUrlEl) ttsUrlEl.value = config.tts.url || 'http://127.0.0.1:7860';
    if (ttsDeviceEl) ttsDeviceEl.value = config.tts.device || 'auto';
    if (ttsLanguageEl) ttsLanguageEl.value = config.tts.language || 'English';
    if (parallelWorkersEl) parallelWorkersEl.value = String(config.tts.parallel_workers || 2);
    if (batchSeedEl && config.tts.batch_seed != null) {
      batchSeedEl.value = String(config.tts.batch_seed);
    }

    const compileCodecEl = document.getElementById('compile-codec') as HTMLInputElement;
    const batchGroupByTypeEl = document.getElementById('batch-group-by-type') as HTMLInputElement;
    const subBatchEnabledEl = document.getElementById('sub-batch-enabled') as HTMLInputElement;
    const subBatchMinSizeEl = document.getElementById('sub-batch-min-size') as HTMLInputElement;
    const subBatchRatioEl = document.getElementById('sub-batch-ratio') as HTMLInputElement;
    const subBatchMaxItemsEl = document.getElementById('sub-batch-max-items') as HTMLInputElement;
    const pauseBetweenSpeakersEl = document.getElementById('pause-between-speakers') as HTMLInputElement;
    const pauseSameSpeakerEl = document.getElementById('pause-same-speaker') as HTMLInputElement;

    if (compileCodecEl) compileCodecEl.checked = !!config.tts.compile_codec;
    if (batchGroupByTypeEl) batchGroupByTypeEl.checked = !!config.tts.batch_group_by_type;
    if (subBatchEnabledEl) subBatchEnabledEl.checked = config.tts.sub_batch_enabled !== false;
    if (subBatchMinSizeEl && config.tts.sub_batch_min_size != null) {
      subBatchMinSizeEl.value = String(config.tts.sub_batch_min_size);
    }
    if (subBatchRatioEl && config.tts.sub_batch_ratio != null) {
      subBatchRatioEl.value = String(config.tts.sub_batch_ratio);
    }
    if (subBatchMaxItemsEl && config.tts.sub_batch_max_items != null) {
      subBatchMaxItemsEl.value = String(config.tts.sub_batch_max_items);
    }
    // Global pause fields — the config-wide defaults that are resolved into
    // the merged audio: pause_between_speakers_ms between different speakers
    // (default 500 ms), pause_same_speaker_ms when the same speaker continues
    // (default 250 ms). These are the defaults the book-level overrides below
    // fall back to (and render/export resolve them via precedence).
    if (pauseBetweenSpeakersEl && config.tts.pause_between_speakers_ms != null) {
      pauseBetweenSpeakersEl.value = String(config.tts.pause_between_speakers_ms);
    }
    if (pauseSameSpeakerEl && config.tts.pause_same_speaker_ms != null) {
      pauseSameSpeakerEl.value = String(config.tts.pause_same_speaker_ms);
    }

    toggleTTSMode();
    // Load the per-book pause overrides + resolved preview after the global
    // config populates the inputs (book overrides take precedence; blank =
    // inherit these config defaults).
    await loadBookPauseSettings();
  } catch (e) {
    console.error('Failed to load config', e);
  }
}

/**
 * Collect per-task LLM overrides from the table rows.
 * Iterates #per-task-llm-table tbody tr, reads data-task and data-field attributes,
 * builds task_overrides object with {model_name, reasoning_effort, temperature} per task.
 * Only collects overrides for known walk task names (WALK_TASK_NAMES).
 */
export function collectTaskOverrides(): Record<string, TaskOverride> {
  const taskOverrides: Record<string, TaskOverride> = {};
  const overrideRows = document.querySelectorAll<HTMLTableRowElement>('#per-task-llm-table tbody tr');
  overrideRows.forEach(row => {
    const taskName = row.getAttribute('data-task');
    if (!taskName || !WALK_TASK_NAMES.includes(taskName)) return;
    const modelInput = row.querySelector<HTMLInputElement>('[data-field="model_name"]');
    const reasoningSelect = row.querySelector<HTMLSelectElement>('[data-field="reasoning_effort"]');
    const temperatureInput = row.querySelector<HTMLInputElement>('[data-field="temperature"]');
    const override: TaskOverride = {
      model_name: modelInput ? (modelInput.value.trim() || null) : null,
      reasoning_effort: reasoningSelect ? (reasoningSelect.value || null) : null,
    };
    const tempRaw = temperatureInput ? temperatureInput.value.trim() : '';
    const temp = tempRaw !== '' ? parseFloat(tempRaw) : NaN;
    override.temperature = isNaN(temp) ? null : temp;
    taskOverrides[taskName] = override;
  });
  return taskOverrides;
}

/**
 * Collect per-walk prompt overrides from the table rows.
 * Iterates #per-task-llm-table tbody tr, reads the prompt input per task, and
 * builds the top-level walk_override section keyed by task name. This section is
 * an unknown top-level config path — the backend deep-merges it and GET returns
 * it verbatim, so it round-trips byte-stable (unlike nested keys inside llm,
 * which pydantic drops on GET). Only collects overrides for known walk task names.
 */
export function collectWalkOverrides(): Record<string, WalkOverride> {
  const walkOverrides: Record<string, WalkOverride> = {};
  const overrideRows = document.querySelectorAll<HTMLTableRowElement>('#per-task-llm-table tbody tr');
  overrideRows.forEach(row => {
    const taskName = row.getAttribute('data-task');
    if (!taskName || !WALK_TASK_NAMES.includes(taskName)) return;
    const promptInput = row.querySelector<HTMLInputElement>('[data-field="prompt"]');
    const override: WalkOverride = {
      // Explicit null for empty — matches the collectTaskOverrides convention and
      // lets the backend deep-merge clear a previously-set prompt on disk.
      prompt: promptInput ? (promptInput.value.trim() || null) : null,
    };
    walkOverrides[taskName] = override;
  });
  return walkOverrides;
}

/**
 * Build the config payload sent to POST /api/config.
 * Starts from the retained raw config (unknown top-level sections preserved
 * verbatim — the byte-stable contract), overlays the known llm/tts keys from the
 * current form values, and adds the per-walk prompt overrides under the
 * top-level walk_override section (keyed by task name).
 */
export function buildConfigPayload(): AppConfig {
  const parallelWorkersEl = document.getElementById('parallel-workers') as HTMLInputElement;

  // Validate parallel workers
  let parallelWorkers = parseInt(parallelWorkersEl?.value || '') || 2;
  parallelWorkers = Math.max(1, parallelWorkers);
  if (parallelWorkersEl) parallelWorkersEl.value = String(parallelWorkers);

  // Collect per-task LLM overrides
  const taskOverrides = collectTaskOverrides();
  const walkOverrides = collectWalkOverrides();

  const llmUrlEl = document.getElementById('llm-url') as HTMLInputElement;
  const llmKeyEl = document.getElementById('llm-key') as HTMLInputElement;
  const llmModelEl = document.getElementById('llm-model') as HTMLInputElement;
  const llmReasoningEl = document.getElementById('llm-reasoning') as HTMLSelectElement;
  const llmTemperatureEl = document.getElementById('llm-temperature') as HTMLInputElement;
  const ttsModeEl = document.getElementById('tts-mode') as HTMLSelectElement;
  const ttsUrlEl = document.getElementById('tts-url') as HTMLInputElement;
  const ttsDeviceEl = document.getElementById('tts-device') as HTMLSelectElement;
  const ttsLanguageEl = document.getElementById('tts-language') as HTMLSelectElement;
  const batchSeedEl = document.getElementById('batch-seed') as HTMLInputElement;
  const compileCodecEl = document.getElementById('compile-codec') as HTMLInputElement;
  const batchGroupByTypeEl = document.getElementById('batch-group-by-type') as HTMLInputElement;
  const subBatchEnabledEl = document.getElementById('sub-batch-enabled') as HTMLInputElement;
  const subBatchMinSizeEl = document.getElementById('sub-batch-min-size') as HTMLInputElement;
  const subBatchRatioEl = document.getElementById('sub-batch-ratio') as HTMLInputElement;
  const subBatchMaxItemsEl = document.getElementById('sub-batch-max-items') as HTMLInputElement;
  const pauseBetweenSpeakersEl = document.getElementById('pause-between-speakers') as HTMLInputElement;
  const pauseSameSpeakerEl = document.getElementById('pause-same-speaker') as HTMLInputElement;

  // Start from the retained raw config so unknown top-level sections survive;
  // known llm/tts keys are rebuilt from the current form values.
  const config: AppConfig = {
    ...(retainedConfig ?? {}),
    llm: {
      base_url: llmUrlEl?.value || '',
      api_key: llmKeyEl?.value || '',
      model_name: llmModelEl?.value || '',
      reasoning_effort: llmReasoningEl?.value || null,
      temperature: parseFloat(llmTemperatureEl?.value || '0.6'),
      task_overrides: taskOverrides,
    },
    tts: {
      mode: ttsModeEl?.value || 'external',
      url: ttsUrlEl?.value || '',
      device: ttsDeviceEl?.value || 'auto',
      language: ttsLanguageEl?.value || 'English',
      parallel_workers: parallelWorkers,
      batch_seed: batchSeedEl?.value ? parseInt(batchSeedEl.value) : null,
      compile_codec: compileCodecEl?.checked || false,
      batch_group_by_type: batchGroupByTypeEl?.checked || false,
      sub_batch_enabled: subBatchEnabledEl?.checked || false,
      sub_batch_min_size: parseInt(subBatchMinSizeEl?.value || '') || 4,
      sub_batch_ratio: parseFloat(subBatchRatioEl?.value || '') || 5,
      sub_batch_max_items: parseInt(subBatchMaxItemsEl?.value || '') || 0,
      // Global pause defaults — carried into the config that the render chain
      // resolves (tts_integration._build_chunks) into the merged audio; fall
      // back to the TTSConfig defaults (500/250 ms) when the input is empty.
      pause_between_speakers_ms: parseInt(pauseBetweenSpeakersEl?.value || '') || 500,
      pause_same_speaker_ms: parseInt(pauseSameSpeakerEl?.value || '') || 250,
    },
    walk_override: {
      ...(retainedConfig?.walk_override ?? {}),
      ...walkOverrides,
    },
  };
  return config;
}

/**
 * Read a pause (ms) input value into a nullable integer.
 *
 * Empty/blank → ``null`` (NULL = resolve the applicable default / inherit).
 * Non-empty must be an integer in 0..PAUSE_MAX_MS; anything invalid returns
 * ``null`` too (callers run validatePauseInputs() before submit, so the
 * invalid case is caught client-side and never persisted).
 */
function readPauseInput(value: string | undefined): number | null {
  if (value === undefined || value.trim() === '') return null;
  const n = Number(value);
  if (!Number.isInteger(n) || n < 0 || n > PAUSE_MAX_MS) return null;
  return n;
}

/**
 * Validate the pause inputs (global defaults + book overrides) client-side,
 * mirroring the backend 422 (validate_pause_ms / PAUSE_MAX_MS). Empty inputs
 * are allowed (inherit/fallback); any non-empty value must be an integer in
 * 0..10000. Returns false and toasts the first offending field.
 */
function validatePauseInputs(): boolean {
  const fields: Array<[string, string]> = [
    ['pause-between-speakers', 'Speaker change pause'],
    ['pause-same-speaker', 'Same speaker pause'],
    ['book-pause-between-speakers', 'Book speaker-change pause'],
    ['book-pause-same-speaker', 'Book same-speaker pause'],
  ];
  for (const [id, label] of fields) {
    const input = document.getElementById(id) as HTMLInputElement | null;
    if (!input || input.value.trim() === '') continue;
    const n = Number(input.value);
    if (!Number.isInteger(n) || n < 0 || n > PAUSE_MAX_MS) {
      showToast(`${label} must be a whole number between 0 and ${PAUSE_MAX_MS} ms`, 'error');
      return false;
    }
  }
  return true;
}

/**
 * Recompute the resolved-pause preview line from precedence:
 * book override → global config default (current global input) → 500/250 ms.
 */
function updatePauseResolvedPreview(): void {
  const preview = document.getElementById('pause-resolved-preview');
  if (!preview) return;
  const globalBetween = readPauseInput(
    (document.getElementById('pause-between-speakers') as HTMLInputElement | null)?.value,
  );
  const globalSame = readPauseInput(
    (document.getElementById('pause-same-speaker') as HTMLInputElement | null)?.value,
  );
  const bookBetween = readPauseInput(
    (document.getElementById('book-pause-between-speakers') as HTMLInputElement | null)?.value,
  );
  const bookSame = readPauseInput(
    (document.getElementById('book-pause-same-speaker') as HTMLInputElement | null)?.value,
  );
  const between = bookBetween ?? globalBetween ?? 500;
  const same = bookSame ?? globalSame ?? 250;
  preview.textContent = `Resolved: ${between} ms between speakers · ${same} ms same speaker`;
}

/**
 * Load the current book's pause overrides (GET /api/pipeline/book/{book_id}/
 * pause_settings) into the override inputs. With no book onboarded, clears the
 * overrides and shows the config-default preview. Failure is non-fatal.
 */
async function loadBookPauseSettings(): Promise<void> {
  const betweenEl = document.getElementById('book-pause-between-speakers') as HTMLInputElement | null;
  const sameEl = document.getElementById('book-pause-same-speaker') as HTMLInputElement | null;
  if (!state.pipelineBookId) {
    if (betweenEl) betweenEl.value = '';
    if (sameEl) sameEl.value = '';
    updatePauseResolvedPreview();
    return;
  }
  try {
    const settings = await API.get<{
      pause_between_speakers_ms: number | null;
      pause_same_speaker_ms: number | null;
    }>(`/api/pipeline/book/${state.pipelineBookId}/pause_settings`);
    if (betweenEl) {
      betweenEl.value = settings.pause_between_speakers_ms != null ? String(settings.pause_between_speakers_ms) : '';
    }
    if (sameEl) {
      sameEl.value = settings.pause_same_speaker_ms != null ? String(settings.pause_same_speaker_ms) : '';
    }
    updatePauseResolvedPreview();
  } catch {
    // Book-override load failure is non-fatal — keep the inputs at their
    // current (possibly empty = inherit) values and show the default preview.
    updatePauseResolvedPreview();
  }
}

/**
 * Handle config form submission.
 * Builds the config payload from the retained raw config merged with current
 * form values (unknown top-level sections preserved — byte-stable round-trip)
 * and POSTs it to /api/config. Also persists the per-book pause overrides via
 * PUT /api/pipeline/book/{book_id}/pause_settings when a book is onboarded.
 */
async function handleConfigSubmit(e: Event): Promise<void> {
  e.preventDefault();

  if (!validatePauseInputs()) return;

  const config = buildConfigPayload();

  try {
    await API.post('/api/config', config);
    // Persist the per-book pause overrides (P5-S1). Blank → null (inherit the
    // config default); explicit 0 → intentional no-gap. Partial-update via
    // model_fields_set: always send both keys so a cleared field reverts to
    // inherit.
    if (state.pipelineBookId) {
      await API.put(`/api/pipeline/book/${state.pipelineBookId}/pause_settings`, {
        pause_between_speakers_ms: readPauseInput(
          (document.getElementById('book-pause-between-speakers') as HTMLInputElement | null)?.value,
        ),
        pause_same_speaker_ms: readPauseInput(
          (document.getElementById('book-pause-same-speaker') as HTMLInputElement | null)?.value,
        ),
      });
    }
    showToast('Configuration Saved!', 'success');
    updatePauseResolvedPreview();
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    showToast('Error saving config: ' + msg, 'error');
  }
}

/**
 * Initialize the Setup tab.
 * Attaches event listeners for config form submission and the TTS mode
 * toggle. Loads config on init.
 */
export function initSetup(): void {
  document.addEventListener('DOMContentLoaded', () => {
    // Config form submit handler
    const configForm = document.getElementById('config-form');
    if (configForm) {
      configForm.addEventListener('submit', (e) => {
        handleConfigSubmit(e);
      });
    }

    // TTS mode toggle
    const ttsModeEl = document.getElementById('tts-mode');
    if (ttsModeEl) {
      ttsModeEl.addEventListener('change', () => toggleTTSMode());
    }

    // Live resolved-pause preview: recompute whenever a global default or a
    // book override input changes (P5-S1).
    ['pause-between-speakers', 'pause-same-speaker', 'book-pause-between-speakers', 'book-pause-same-speaker']
      .forEach(id => {
        const el = document.getElementById(id);
        if (el) el.addEventListener('input', () => updatePauseResolvedPreview());
      });

    // Load config on init
    loadConfig();
  });
}
