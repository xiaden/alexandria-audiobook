/**
 * Canonical walk-order contract for the audiobook pipeline.
 *
 * **This module MUST stay in sync with `app/pipeline/walks/order.py`.**
 * Both backend (Python) and frontend (TypeScript) reference these constants.
 * If you add, remove, or reorder walks, update BOTH files.
 *
 * The 9 walks form a strict serial dependency chain:
 *   2a → 2b → 2c → 2d → 2e → 2f → 2g → 2h → 2i
 *
 * Key constraints:
 * - Walk 2c (alias_resolution) operates at GLOBAL scope (entire book).
 * - Temperature: walks 2a–2f and 2h use 0.1; walks 2g and 2i use 0.3.
 */

/**
 * Canonical walk execution order.
 * Each walk depends on the output of all preceding walks.
 */
export const WALK_ORDER: readonly string[] = [
  'walk_2a_scene_segmentation',
  'walk_2b_character_discovery',
  'walk_2c_alias_resolution',
  'walk_2d_scene_presence',
  'walk_2e_span_attribution',
  'walk_2f_character_description',
  'walk_2g_voice_audition',
  'walk_2h_voice_assignment',
  'walk_2i_delivery',
] as const;

/**
 * Walk name → task name mapping.
 * Task names are used in LLM config overrides and logging.
 */
export const WALK_TASK_NAMES: Readonly<Record<string, string>> = {
  walk_2a_scene_segmentation: 'scene_segmentation',
  walk_2b_character_discovery: 'character_discovery',
  walk_2c_alias_resolution: 'script_alias_resolution',
  walk_2d_scene_presence: 'scene_presence',
  walk_2e_span_attribution: 'span_attribution',
  walk_2f_character_description: 'character_description',
  walk_2g_voice_audition: 'voice_audition',
  walk_2h_voice_assignment: 'voice_assignment',
  walk_2i_delivery: 'delivery',
};

/**
 * Walk name → human-readable display name.
 * Used in frontend UI (walk status badges, progress indicators) and API
 * error messages.
 */
export const WALK_DISPLAY_NAMES: Readonly<Record<string, string>> = {
  walk_2a_scene_segmentation: 'Scene Segmentation',
  walk_2b_character_discovery: 'Character Discovery',
  walk_2c_alias_resolution: 'Alias Resolution',
  walk_2d_scene_presence: 'Scene Presence',
  walk_2e_span_attribution: 'Span Attribution',
  walk_2f_character_description: 'Character Description',
  walk_2g_voice_audition: 'Voice Audition',
  walk_2h_voice_assignment: 'Voice Assignment',
  walk_2i_delivery: 'Delivery',
};
