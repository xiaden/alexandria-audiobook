/**
 * Global application state
 * Ported from app/static/index.html scattered window._ caches and let declarations
 */

/** Voice information from /api/pipeline/voices */
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

/** Chunk information (legacy state, no live endpoint) */
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
  /** Audio chunks (legacy state, no live endpoint) */
  chunks: Chunk[];
  /** Available voices (from /api/pipeline/voices) */
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
  /** Pipeline: current book ID from successful onboard (shared across tabs) */
  pipelineBookId: string | null;
  /** Pipeline: current render job ID (set after render completes, used for merge/download) */
  pipelineRenderJobId: string | null;
  /** Workbench: combined Characters & Scenes workbench state (loaded on tab open) */
  workbench: WorkbenchState | null;
  /** Workbench: cached per-walk config resolution (GET /workbench/{book_id}/config) */
  workbenchConfig: WorkbenchConfig | null;
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
  pipelineBookId: null,
  pipelineRenderJobId: null,
  workbench: null,
  workbenchConfig: null,
};

// ---------------------------------------------------------------------------
// localStorage persistence helpers
// ---------------------------------------------------------------------------

const LS_PIPELINE_BOOK_ID = 'alexandria-pipeline-book-id';

/**
 * Save a value to localStorage. Silently ignores errors (e.g. private mode,
 * quota exceeded) so callers never need try/catch.
 */
function saveKey(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* localStorage unavailable; value still applies for this session */
  }
}

/**
 * Load a value from localStorage. Returns null if the key is missing or
 * localStorage is unavailable.
 */
function loadKey(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

/**
 * Set pipelineBookId in state and persist to localStorage.
 */
export function setPipelineBookId(bookId: string | null): void {
  state.pipelineBookId = bookId;
  saveKey(LS_PIPELINE_BOOK_ID, bookId ?? '');
}

/**
 * Restore persisted pipeline state from localStorage.
 * Call once on page load before any tab init runs.
 */
export function initState(): void {
  const storedBookId = loadKey(LS_PIPELINE_BOOK_ID);
  if (storedBookId && storedBookId !== '') {
    state.pipelineBookId = storedBookId;
  }
}

/** Available built-in voices for custom voice selection */
export const AVAILABLE_VOICES = [
  'Aiden', 'Dylan', 'Eric', 'Ono_anna', 'Ryan', 'Serena', 'Sohee', 'Uncle_fu', 'Vivian'
];

// ---------------------------------------------------------------------------
// Workbench — combined Characters & Scenes (walks 2b/2c/2d)
// ---------------------------------------------------------------------------

/**
 * Workbench state read-model (GET /api/pipeline/workbench/{book_id}).
 * Mirrors WorkbenchStateDTO from CONTRACTS. Anchor fields are immutable source
 * identity; `position` fields are presentation metadata only.
 */
export interface WorkbenchState {
  book_id: string;
  generation_revision: number;
  /** Chapter→scene→paragraph→span hierarchy built from durable ids only. */
  scenes: WorkbenchChapter[];
  /** Characters referenced by manual or generated presence. aliases is a JSON string. */
  characters: WorkbenchCharacter[];
  /** Alias-merge rows. */
  aliases: WorkbenchAliasMerge[];
  /** Scene presence (manual + generated). */
  presence: WorkbenchPresence[];
  /** Confidence review items (decision:/junction:/walkitem: id forms). */
  review_items: WorkbenchReviewItem[];
  /** Per-walk DB overrides (config overrides applied by the user). */
  overrides: WorkbenchOverride[];
  /** Effective config per walk: {walk_name: {values, sources}}. */
  effective_config: Record<string, WorkbenchEffectiveConfig>;
  /** Conflicts: manual-vs-generated disagreement. */
  conflicts: WorkbenchConflict[];
  /** Recent runs of walks 2b/2c/2d. */
  runs: WorkbenchRun[];
}

/** A chapter in the workbench scene hierarchy. */
export interface WorkbenchChapter {
  chapter_id: string;
  position: number;
  scenes: WorkbenchScene[];
}

/** A scene in the workbench scene hierarchy. */
export interface WorkbenchScene {
  scene_id: string;
  position: number;
  paragraphs: WorkbenchParagraph[];
}

/** A paragraph containing spans with immutable span identity. */
export interface WorkbenchParagraph {
  paragraph_id: string;
  position: number;
  spans: WorkbenchSpan[];
}

/** A span of evidence text. `id` is the durable identity; `position` display-only. */
export interface WorkbenchSpan {
  id: string;
  span_type: string;
  text: string;
  instruct: string;
  position: number;
}

/** A character from the workbench character ledger. */
export interface WorkbenchCharacter {
  id: string;
  name: string;
  /** JSON-encoded string of alias names, per the pipeline voices contract. */
  aliases: string;
  voice_assignment_id: string | null;
  description?: string | null;
}

/** An alias-merge row from the 2c alias resolution ledger. */
export interface WorkbenchAliasMerge {
  merge_id: string;
  canonical_id: string;
  member_id: string;
  status: 'active' | string;
  merge_revision: number;
  created_ms: number;
  canonical_name: string;
  member_name: string;
}

/** A scene-presence row. source distinguishes manual vs generated. */
export interface WorkbenchPresence {
  scene_id: string;
  character_id: string;
  relation_type: 'present' | 'speaker' | 'absent' | string;
  source: string;
  confidence: number | null;
  human_override: boolean;
  revision: number;
}

/** A confidence review item in the workbench queue. */
export interface WorkbenchReviewItem {
  item_id: string;
  kind: string;
  target_table: string;
  target_id: string;
  status: string;
  decision_id: string | null;
  source_run_id: string | null;
  anchor?: {
    book_id?: string;
    scene_id?: string;
    chapter_id?: string;
    paragraph_id?: string;
    span_id?: string;
    start_offset?: number;
    end_offset?: number;
  } | null;
  neighbors?: { before: string[]; after: string[] };
  character_id?: string;
  character_name?: string;
  confidence?: number;
  junction_table?: string;
  related_entity_id?: string;
  reason?: string;
  walk_name?: string;
}

/** A per-walk config override row. */
export interface WorkbenchOverride {
  override_id: string;
  walk_name: string;
  key: string;
  value: unknown;
  revision: number;
}

/** Effective config resolution per walk (GET /workbench/{book_id}/config). */
export interface WorkbenchEffectiveConfig {
  values: Record<string, unknown>;
  sources: Record<string, string>;
}

/** A manual-vs-generated disagreement conflict. */
export interface WorkbenchConflict {
  code: string;
  current_revision: number;
  current_value: unknown;
  requested_value: unknown;
  decision_id: string | null;
  item_id: string | null;
}

/** A run of a workbench walk. */
export interface WorkbenchRun {
  run_id: string;
  walk_name: string;
  status: string;
  heartbeat_ms: number;
  created_ms: number;
  finished_ms: number | null;
  error: string | null;
}

/**
 * Per-walk config edit model (GET /workbench/{book_id}/config). Field-level
 * effective-source provenance per CONTRACTS precedence: DB row → task
 * override → global → hardcoded (prompt: DB → top-level walk override → task
 * override → llm.prompt → hardcoded). Raw prompts/secrets are omitted by the
 * backend; validation_errors surface rejection reasons for edited values.
 */
export interface WorkbenchConfig {
  global: Record<string, unknown> | null;
  task_overrides: Record<string, Record<string, unknown>>;
  top_level_walk_override: Record<string, Record<string, unknown>>;
  db_overrides: Record<string, Record<string, unknown>>;
  /** effective[walk][key] = resolved value */
  effective: Record<string, Record<string, unknown>>;
  /** source[walk][key] = provenance string */
  source: Record<string, Record<string, string>>;
  validation_errors: string[];
}

/** Walk names supported by the workbench, in canonical (sorted) order. */
export const WORKBENCH_WALK_NAMES = [
  'walk_2b_character_discovery',
  'walk_2c_alias_resolution',
  'walk_2d_scene_presence',
] as const;

export type WorkbenchWalkName = (typeof WORKBENCH_WALK_NAMES)[number];

/** Human-readable labels for workbench walks. */
export const WORKBENCH_WALK_LABELS: Record<string, string> = {
  walk_2b_character_discovery: '2b · Character discovery',
  walk_2c_alias_resolution: '2c · Alias resolution',
  walk_2d_scene_presence: '2d · Scene presence',
};

/**
 * Invalidation mapping for reruns: which downstream walks a rerun invalidates.
 * 2b→2c+2d, 2c→2d, 2d→none.
 */
export const RERUN_INVALIDATION: Record<string, string[]> = {
  walk_2b_character_discovery: ['walk_2c_alias_resolution', 'walk_2d_scene_presence'],
  walk_2c_alias_resolution: ['walk_2d_scene_presence'],
  walk_2d_scene_presence: [],
};

// ---------------------------------------------------------------------------
// Workbench selectors (pure, testable)
// ---------------------------------------------------------------------------

/** Return the active workbench state, or null if not loaded / no book. */
export function selectWorkbench(): WorkbenchState | null {
  return state.workbench && state.pipelineBookId ? state.workbench : null;
}

/**
 * Select review items by status filter. Recognized statuses: pending,
 * accepted, rejected, protected, conflict. Unknown filters return everything.
 */
export function selectReviewItems(
  wb: WorkbenchState | null,
  statusFilter: string,
): WorkbenchReviewItem[] {
  if (!wb) return [];
  if (!statusFilter || statusFilter === 'all') return wb.review_items;
  const normalized = statusFilter.toLowerCase();
  if (normalized === 'protected') {
    // Protected decisions are terminal/manual resolutions still in force.
    return wb.review_items.filter((r) => r.decision_id != null && r.status === 'resolved');
  }
  if (normalized === 'conflict') {
    const conflictItemIds = new Set(
      wb.conflicts.map((c) => c.item_id).filter((x): x is string => !!x),
    );
    return wb.review_items.filter((r) => conflictItemIds.has(r.item_id));
  }
  return wb.review_items.filter((r) => r.status.toLowerCase() === normalized);
}

/** Presence rows for a given scene. */
export function selectScenePresence(
  wb: WorkbenchState | null,
  sceneId: string | null,
): WorkbenchPresence[] {
  if (!wb || !sceneId) return [];
  return wb.presence.filter((p) => p.scene_id === sceneId);
}

/** Character by id, or null. */
export function selectCharacter(
  wb: WorkbenchState | null,
  characterId: string,
): WorkbenchCharacter | null {
  if (!wb) return null;
  return wb.characters.find((c) => c.id === characterId) ?? null;
}

/** All active alias-merge members (status 'active'). */
export function selectActiveAliases(wb: WorkbenchState | null): WorkbenchAliasMerge[] {
  if (!wb) return [];
  return wb.aliases.filter((a) => a.status === 'active');
}

/**
 * Canonical characters (those that are not an active merge member) for the
 * alias conversion picker.
 */
export function selectCanonicalCharacters(wb: WorkbenchState | null): WorkbenchCharacter[] {
  if (!wb) return [];
  const memberIds = new Set(selectActiveAliases(wb).map((a) => a.member_id));
  return wb.characters.filter((c) => !memberIds.has(c.id));
}

/** Effective config sources for a given walk, with labels for display. */
export function selectEffectiveSources(
  wb: WorkbenchState | null,
  walkName: string,
): Record<string, string> {
  if (!wb) return {};
  const cfg = wb.effective_config[walkName];
  return cfg ? cfg.sources : {};
}

/**
 * Short display label for a config source provenance string. Non-color state:
 * the label text itself carries meaning (used alongside badges).
 */
export function sourceLabel(source: string | undefined | null): string {
  if (!source) return 'default';
  const s = String(source);
  if (s === 'db') return 'custom';
  if (s === 'task_override' || s === 'task') return 'task override';
  if (s === 'top_level_walk_override' || s === 'walk_override') return 'walk override';
  if (s === 'global' || s === 'llm') return 'global';
  if (s === 'hardcoded' || s === 'builtin' || s === 'default') return 'default';
  return s;
}
