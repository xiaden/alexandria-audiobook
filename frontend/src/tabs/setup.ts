/**
 * Setup tab module — Configuration panel with per-task LLM overrides
 * Ported from app/static/index.html lines 96-416 (HTML), 1251-1520 (JS)
 */

import * as API from '../api';
import { showToast } from '../utils';

/**
 * Canonical walk task names matching WalkRunner.WALK_ORDER in runner.py.
 * These are the data-task attribute values used in the per-task LLM overrides
 * table. The short names (without walk_2x_ prefix) match what the backend
 * walks pass to resolve_task_llm().
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
}

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
    if (pauseBetweenSpeakersEl && config.tts.pause_between_speakers_ms != null) {
      pauseBetweenSpeakersEl.value = String(config.tts.pause_between_speakers_ms);
    }
    if (pauseSameSpeakerEl && config.tts.pause_same_speaker_ms != null) {
      pauseSameSpeakerEl.value = String(config.tts.pause_same_speaker_ms);
    }

    toggleTTSMode();
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
 * Handle config form submission.
 * Collects all field values including per-task overrides, builds config object
 * (llm + tts only), and POSTs to /api/config.
 */
async function handleConfigSubmit(e: Event): Promise<void> {
  e.preventDefault();

  const parallelWorkersEl = document.getElementById('parallel-workers') as HTMLInputElement;

  // Validate parallel workers
  let parallelWorkers = parseInt(parallelWorkersEl?.value || '') || 2;
  parallelWorkers = Math.max(1, parallelWorkers);
  if (parallelWorkersEl) parallelWorkersEl.value = String(parallelWorkers);

  // Collect per-task LLM overrides
  const taskOverrides = collectTaskOverrides();

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

  const config = {
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
      pause_between_speakers_ms: parseInt(pauseBetweenSpeakersEl?.value || '') || 500,
      pause_same_speaker_ms: parseInt(pauseSameSpeakerEl?.value || '') || 250,
    },
  };

  try {
    await API.post('/api/config', config);
    showToast('Configuration Saved!', 'success');
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
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

    // Load config on init
    loadConfig();
  });
}
