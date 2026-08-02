/**
 * Setup tab module — Configuration panel with per-task LLM overrides
 * Ported from app/static/index.html lines 96-416 (HTML), 1251-1520 (JS)
 */

import * as API from '../api';
import { showToast } from '../utils';

/** Config shape returned by GET /api/config */
interface TaskOverride {
  model_name: string | null;
  reasoning_effort: string | null;
}

interface LLMConfig {
  base_url: string;
  api_key: string;
  model_name: string;
  reasoning_effort?: string | null;
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

interface PromptsConfig {
  system_prompt?: string;
  user_prompt?: string;
  review_system_prompt?: string;
  review_user_prompt?: string;
  persona_system_prompt?: string;
  persona_user_prompt?: string;
  persona_advanced_prompt?: string;
}

interface GenerationConfig {
  chunk_size?: number;
  max_tokens?: number;
  temperature?: number;
  top_p?: number;
  top_k?: number;
  min_p?: number;
  presence_penalty?: number;
  banned_tokens?: string[];
  merge_narrators?: boolean;
}

interface AppConfig {
  llm: LLMConfig;
  tts: TTSConfig;
  prompts?: PromptsConfig;
  generation?: GenerationConfig;
}

interface DefaultPrompts {
  system_prompt?: string;
  user_prompt?: string;
  review_system_prompt?: string;
  review_user_prompt?: string;
  persona_system_prompt?: string;
  persona_user_prompt?: string;
  persona_advanced_prompt?: string;
}

/**
 * Toggle TTS mode between local and external server.
 * Shows/hides relevant form groups based on selected mode.
 */
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
 * Fetches GET /api/config, populates LLM, TTS, prompts, and generation settings.
 * Also populates the per-task LLM override table.
 */
async function loadConfig(): Promise<void> {
  // Set defaults before fetching
  const chunkSizeEl = document.getElementById('chunk-size') as HTMLInputElement;
  const maxTokensEl = document.getElementById('max-tokens') as HTMLInputElement;
  if (chunkSizeEl) chunkSizeEl.value = '3000';
  if (maxTokensEl) maxTokensEl.value = '4096';

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

    // Populate per-task LLM overrides
    const taskRows = document.querySelectorAll<HTMLTableRowElement>('#per-task-llm-table tbody tr');
    taskRows.forEach(row => {
      const taskName = row.getAttribute('data-task');
      const taskConfig = config.llm?.task_overrides?.[taskName as string];
      const modelInput = row.querySelector<HTMLInputElement>('[data-field="model_name"]');
      const reasoningSelect = row.querySelector<HTMLSelectElement>('[data-field="reasoning_effort"]');
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

    // Load custom prompts if they exist and are non-empty
    if (config.prompts) {
      const promptFields: Array<[keyof PromptsConfig, string]> = [
        ['system_prompt', 'system-prompt'],
        ['user_prompt', 'user-prompt'],
        ['review_system_prompt', 'review-system-prompt'],
        ['review_user_prompt', 'review-user-prompt'],
        ['persona_system_prompt', 'persona-system-prompt'],
        ['persona_user_prompt', 'persona-user-prompt'],
        ['persona_advanced_prompt', 'persona-advanced-prompt'],
      ];
      for (const [key, id] of promptFields) {
        const val = config.prompts[key];
        if (val) {
          const el = document.getElementById(id) as HTMLTextAreaElement;
          if (el) el.value = val;
        }
      }
    }

    // If review/persona prompts are still empty, fetch defaults
    const reviewSystemEl = document.getElementById('review-system-prompt') as HTMLTextAreaElement;
    const reviewUserEl = document.getElementById('review-user-prompt') as HTMLTextAreaElement;
    const personaSystemEl = document.getElementById('persona-system-prompt') as HTMLTextAreaElement;
    const personaUserEl = document.getElementById('persona-user-prompt') as HTMLTextAreaElement;
    const personaAdvancedEl = document.getElementById('persona-advanced-prompt') as HTMLTextAreaElement;

    if (!reviewSystemEl?.value || !reviewUserEl?.value ||
        !personaSystemEl?.value || !personaUserEl?.value) {
      try {
        const defaults = await API.get<DefaultPrompts>('/api/default_prompts');
        if (!reviewSystemEl?.value && defaults.review_system_prompt) {
          reviewSystemEl.value = defaults.review_system_prompt;
        }
        if (!reviewUserEl?.value && defaults.review_user_prompt) {
          reviewUserEl.value = defaults.review_user_prompt;
        }
        if (!personaSystemEl?.value && defaults.persona_system_prompt) {
          personaSystemEl.value = defaults.persona_system_prompt;
        }
        if (!personaUserEl?.value && defaults.persona_user_prompt) {
          personaUserEl.value = defaults.persona_user_prompt;
        }
        if (!personaAdvancedEl?.value && defaults.persona_advanced_prompt) {
          personaAdvancedEl.value = defaults.persona_advanced_prompt;
        }
      } catch (e) {
        console.warn('Could not fetch default prompts', e);
      }
    }

    // Load generation settings
    if (config.generation) {
      if (config.generation.chunk_size && chunkSizeEl) {
        chunkSizeEl.value = String(config.generation.chunk_size);
      }
      if (config.generation.max_tokens && maxTokensEl) {
        maxTokensEl.value = String(config.generation.max_tokens);
      }
      const genNumberFields: Array<[keyof GenerationConfig, string]> = [
        ['temperature', 'temperature'],
        ['top_p', 'top-p'],
        ['top_k', 'top-k'],
        ['min_p', 'min-p'],
        ['presence_penalty', 'presence-penalty'],
      ];
      for (const [key, id] of genNumberFields) {
        const val = config.generation[key];
        if (val != null) {
          const el = document.getElementById(id) as HTMLInputElement;
          if (el) el.value = String(val);
        }
      }
      const bannedTokensEl = document.getElementById('banned-tokens') as HTMLInputElement;
      if (config.generation.banned_tokens && config.generation.banned_tokens.length > 0 && bannedTokensEl) {
        bannedTokensEl.value = config.generation.banned_tokens.join(', ');
      }
      const mergeNarratorsEl = document.getElementById('merge-narrators') as HTMLInputElement;
      if (mergeNarratorsEl) mergeNarratorsEl.checked = !!config.generation.merge_narrators;
    }
  } catch (e) {
    console.error('Failed to load config', e);
  }
}

/**
 * Reset prompts and generation settings to factory defaults.
 * Fetches GET /api/default_prompts and resets all prompt textareas
 * and generation setting inputs to their default values.
 */
async function resetPrompts(): Promise<void> {
  try {
    const defaults = await API.get<DefaultPrompts>('/api/default_prompts');

    const systemPromptEl = document.getElementById('system-prompt') as HTMLTextAreaElement;
    const userPromptEl = document.getElementById('user-prompt') as HTMLTextAreaElement;
    if (systemPromptEl) systemPromptEl.value = defaults.system_prompt || '';
    if (userPromptEl) userPromptEl.value = defaults.user_prompt || '';

    const reviewSystemEl = document.getElementById('review-system-prompt') as HTMLTextAreaElement;
    const reviewUserEl = document.getElementById('review-user-prompt') as HTMLTextAreaElement;
    const personaSystemEl = document.getElementById('persona-system-prompt') as HTMLTextAreaElement;
    const personaUserEl = document.getElementById('persona-user-prompt') as HTMLTextAreaElement;
    const personaAdvancedEl = document.getElementById('persona-advanced-prompt') as HTMLTextAreaElement;

    if (reviewSystemEl && defaults.review_system_prompt) reviewSystemEl.value = defaults.review_system_prompt;
    if (reviewUserEl && defaults.review_user_prompt) reviewUserEl.value = defaults.review_user_prompt;
    if (personaSystemEl && defaults.persona_system_prompt) personaSystemEl.value = defaults.persona_system_prompt;
    if (personaUserEl && defaults.persona_user_prompt) personaUserEl.value = defaults.persona_user_prompt;
    if (personaAdvancedEl && defaults.persona_advanced_prompt) personaAdvancedEl.value = defaults.persona_advanced_prompt;
  } catch (e) {
    console.error('Failed to fetch default prompts', e);
    showToast('Failed to load default prompts from server.', 'error');
  }

  // Reset generation settings to defaults
  const chunkSizeEl = document.getElementById('chunk-size') as HTMLInputElement;
  const maxTokensEl = document.getElementById('max-tokens') as HTMLInputElement;
  const temperatureEl = document.getElementById('temperature') as HTMLInputElement;
  const topPEl = document.getElementById('top-p') as HTMLInputElement;
  const topKEl = document.getElementById('top-k') as HTMLInputElement;
  const minPEl = document.getElementById('min-p') as HTMLInputElement;
  const presencePenaltyEl = document.getElementById('presence-penalty') as HTMLInputElement;
  const bannedTokensEl = document.getElementById('banned-tokens') as HTMLInputElement;
  const mergeNarratorsEl = document.getElementById('merge-narrators') as HTMLInputElement;

  if (chunkSizeEl) chunkSizeEl.value = '3000';
  if (maxTokensEl) maxTokensEl.value = '4096';
  if (temperatureEl) temperatureEl.value = '0.6';
  if (topPEl) topPEl.value = '0.8';
  if (topKEl) topKEl.value = '0';
  if (minPEl) minPEl.value = '0';
  if (presencePenaltyEl) presencePenaltyEl.value = '0';
  if (bannedTokensEl) bannedTokensEl.value = '';
  if (mergeNarratorsEl) mergeNarratorsEl.checked = false;
}

/**
 * Collect per-task LLM overrides from the table rows.
 * Iterates #per-task-llm-table tbody tr, reads data-task and data-field attributes,
 * builds task_overrides object with {model_name, reasoning_effort} per task.
 */
function collectTaskOverrides(): Record<string, TaskOverride> {
  const taskOverrides: Record<string, TaskOverride> = {};
  const overrideRows = document.querySelectorAll<HTMLTableRowElement>('#per-task-llm-table tbody tr');
  overrideRows.forEach(row => {
    const taskName = row.getAttribute('data-task');
    const modelInput = row.querySelector<HTMLInputElement>('[data-field="model_name"]');
    const reasoningSelect = row.querySelector<HTMLSelectElement>('[data-field="reasoning_effort"]');
    if (taskName) {
      taskOverrides[taskName] = {
        model_name: modelInput ? (modelInput.value.trim() || null) : null,
        reasoning_effort: reasoningSelect ? (reasoningSelect.value || null) : null,
      };
    }
  });
  return taskOverrides;
}

/**
 * Handle config form submission.
 * Collects all field values including per-task overrides, builds config object,
 * and POSTs to /api/config.
 */
async function handleConfigSubmit(e: Event): Promise<void> {
  e.preventDefault();

  const chunkSizeEl = document.getElementById('chunk-size') as HTMLInputElement;
  const maxTokensEl = document.getElementById('max-tokens') as HTMLInputElement;
  const parallelWorkersEl = document.getElementById('parallel-workers') as HTMLInputElement;

  let chunkSize = parseInt(chunkSizeEl?.value || '') || 3000;

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

  const systemPromptEl = document.getElementById('system-prompt') as HTMLTextAreaElement;
  const userPromptEl = document.getElementById('user-prompt') as HTMLTextAreaElement;
  const reviewSystemPromptEl = document.getElementById('review-system-prompt') as HTMLTextAreaElement;
  const reviewUserPromptEl = document.getElementById('review-user-prompt') as HTMLTextAreaElement;
  const personaSystemPromptEl = document.getElementById('persona-system-prompt') as HTMLTextAreaElement;
  const personaUserPromptEl = document.getElementById('persona-user-prompt') as HTMLTextAreaElement;
  const personaAdvancedPromptEl = document.getElementById('persona-advanced-prompt') as HTMLTextAreaElement;

  const temperatureEl = document.getElementById('temperature') as HTMLInputElement;
  const topPEl = document.getElementById('top-p') as HTMLInputElement;
  const topKEl = document.getElementById('top-k') as HTMLInputElement;
  const minPEl = document.getElementById('min-p') as HTMLInputElement;
  const presencePenaltyEl = document.getElementById('presence-penalty') as HTMLInputElement;
  const bannedTokensEl = document.getElementById('banned-tokens') as HTMLInputElement;
  const mergeNarratorsEl = document.getElementById('merge-narrators') as HTMLInputElement;

  const config = {
    llm: {
      base_url: llmUrlEl?.value || '',
      api_key: llmKeyEl?.value || '',
      model_name: llmModelEl?.value || '',
      reasoning_effort: llmReasoningEl?.value || null,
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
    prompts: {
      system_prompt: systemPromptEl?.value || '',
      user_prompt: userPromptEl?.value || '',
      review_system_prompt: reviewSystemPromptEl?.value || '',
      review_user_prompt: reviewUserPromptEl?.value || '',
      persona_system_prompt: personaSystemPromptEl?.value || '',
      persona_user_prompt: personaUserPromptEl?.value || '',
      persona_advanced_prompt: personaAdvancedPromptEl?.value || '',
    },
    generation: {
      chunk_size: chunkSize,
      max_tokens: parseInt(maxTokensEl?.value || '') || 4096,
      temperature: parseFloat(temperatureEl?.value || '0.6'),
      top_p: parseFloat(topPEl?.value || '0.8'),
      top_k: parseInt(topKEl?.value || '0'),
      min_p: parseFloat(minPEl?.value || '0'),
      presence_penalty: parseFloat(presencePenaltyEl?.value || '0'),
      banned_tokens: bannedTokensEl?.value
        ? bannedTokensEl.value.split(',').map(t => t.trim()).filter(t => t)
        : [],
      merge_narrators: mergeNarratorsEl?.checked || false,
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
 * Attaches event listeners for config form submission, TTS mode toggle,
 * prompt reset, and collapse chevron. Loads config on init.
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

    // Reset prompts button
    const resetBtn = document.querySelector('[onclick="resetPrompts()"]');
    if (resetBtn) {
      // Remove the inline onclick and use addEventListener instead
      resetBtn.removeAttribute('onclick');
      resetBtn.addEventListener('click', () => resetPrompts());
    }

    // Toggle chevron on collapse
    const promptSettings = document.getElementById('promptSettings');
    const promptChevron = document.getElementById('prompt-chevron');
    if (promptSettings && promptChevron) {
      promptSettings.addEventListener('show.bs.collapse', () => {
        promptChevron.classList.replace('fa-chevron-right', 'fa-chevron-down');
      });
      promptSettings.addEventListener('hide.bs.collapse', () => {
        promptChevron.classList.replace('fa-chevron-down', 'fa-chevron-right');
      });
    }

    // Load config on init
    loadConfig();
  });
}
