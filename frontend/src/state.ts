/**
 * Global application state
 * Ported from app/static/index.html scattered window._ caches and let declarations
 */

/** Voice information from /api/voices */
export interface Voice {
  name: string;
  config?: VoiceConfig;
}

/** Voice configuration object */
export interface VoiceConfig {
  type?: 'custom' | 'builtin_lora' | 'clone' | 'lora' | 'design';
  alias_of?: string;
  // Custom voice fields
  voice?: string;
  character_style?: string;
  default_style?: string;
  seed?: string;
  // Clone voice fields
  ref_text?: string;
  ref_audio?: string;
  // LoRA fields
  adapter_id?: string;
  adapter_path?: string;
  // Design fields
  description?: string;
}

/** Chunk information from /api/chunks */
export interface Chunk {
  id: number;
  text: string;
  speaker: string;
  status: 'pending' | 'generating' | 'done' | 'error';
  audio_path?: string;
  instruct?: string;
  pause_after?: number;
}

/** Designed voice from /api/voice_design/list */
export interface DesignedVoice {
  id: string;
  name: string;
  filename: string;
  description?: string;
  sample_text?: string;
}

/** Clone voice from /api/clone_voices/list */
export interface CloneVoice {
  id: string;
  name: string;
  filename: string;
}

/** LoRA model from /api/lora/models */
export interface LoraModel {
  id: string;
  name: string;
  builtin?: boolean;
  gender?: 'male' | 'female';
  description?: string;
  downloaded?: boolean;
  adapter_path?: string;
  dataset_id?: string;
  epochs?: number;
  final_loss?: number;
  sample_count?: number;
  preview_audio_url?: string;
}

/** Dataset builder project from /api/dataset_builder/list */
export interface DsbProject {
  name: string;
  done_count: number;
  sample_count: number;
}

/** Dataset builder row */
export interface DsbRow {
  emotion: string;
  text: string;
  seed: number | string;
  status: 'pending' | 'generating' | 'done' | 'error';
  audio_url: string | null;
}

/** Application state interface */
export interface AppState {
  /** Current script entries (from /api/script) */
  currentScript: unknown[];
  /** Audio chunks (from /api/chunks) */
  chunks: Chunk[];
  /** Available voices (from /api/voices) */
  voices: Voice[];
  /** Designed voices cache (from /api/voice_design/list) */
  designedVoices: DesignedVoice[];
  /** Clone voices cache (from /api/clone_voices/list) */
  cloneVoices: CloneVoice[];
  /** LoRA models cache (from /api/lora/models) */
  loraModels: LoraModel[];
  /** Voice names for dropdowns */
  voicesNames: string[];
  /** Dataset builder projects (from /api/dataset_builder/list) */
  dsbProjects: DsbProject[];
  /** Dataset builder rows */
  dsbRows: DsbRow[];
  /** Current dataset builder project name */
  dsbCurrentProject: string;
  /** Editor: cached chunks for diff detection */
  cachedChunks: Chunk[];
  /** Editor: playing sequence flag */
  isPlayingSequence: boolean;
  /** Editor: rendering all flag */
  isRenderingAll: boolean;
}

/** Module-level state singleton */
export const state: AppState = {
  currentScript: [],
  chunks: [],
  voices: [],
  designedVoices: [],
  cloneVoices: [],
  loraModels: [],
  voicesNames: [],
  dsbProjects: [],
  dsbRows: [],
  dsbCurrentProject: '',
  cachedChunks: [],
  isPlayingSequence: false,
  isRenderingAll: false,
};

/** Available built-in voices for custom voice selection */
export const AVAILABLE_VOICES = [
  'Aiden', 'Dylan', 'Eric', 'Ono_anna', 'Ryan', 'Serena', 'Sohee', 'Uncle_fu', 'Vivian'
];
