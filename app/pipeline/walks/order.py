"""Canonical walk-order contract for the audiobook pipeline.

Defines the execution order, task-name mapping, and display names for all
pipeline walks. This module is the single source of truth — both backend
(``WalkRunner``, API endpoints) and frontend (TypeScript constants) must
reference these definitions.

Walk DAG
--------
The 9 walks form a strict serial dependency chain::

    2a → 2b → 2c → 2d → 2e → 2f → 2g → 2h → 2i

Each walk consumes the output of the previous walk. No walk may execute
before its predecessor has completed successfully.

Key constraints:

- Walk 2c (``walk_2c_alias_resolution``) operates at GLOBAL scope — it
  processes the entire book's character set, not individual scenes.
- Temperature settings: walks 2a–2f and 2h use temperature 0.1 (deterministic
  extraction); walks 2g and 2i use temperature 0.3 (creative variation).

Adding or removing walks requires updating all three constants in this module,
the ``_VERIFICATIONS`` registry in ``runner.py``, and the frontend TypeScript
constants in ``frontend/src/pipeline/walks.ts``.
"""

from __future__ import annotations

# Canonical walk execution order.
# Each walk depends on the output of all preceding walks.
WALK_ORDER: list[str] = [
    "walk_2a_scene_segmentation",
    "walk_2b_character_discovery",
    "walk_2c_alias_resolution",
    "walk_2d_scene_presence",
    "walk_2e_span_attribution",
    "walk_2f_character_description",
    "walk_2g_voice_audition",
    "walk_2h_voice_assignment",
    "walk_2i_delivery",
]

# Walk name → task name mapping.
# Task names are used in LLM config overrides (``LLMTaskOverrides``) and
# logging. They match the keys in the config schema.
WALK_TASK_NAMES: dict[str, str] = {
    "walk_2a_scene_segmentation": "scene_segmentation",
    "walk_2b_character_discovery": "character_discovery",
    "walk_2c_alias_resolution": "script_alias_resolution",
    "walk_2d_scene_presence": "scene_presence",
    "walk_2e_span_attribution": "span_attribution",
    "walk_2f_character_description": "character_description",
    "walk_2g_voice_audition": "voice_audition",
    "walk_2h_voice_assignment": "voice_assignment",
    "walk_2i_delivery": "delivery",
}

# Walk name → human-readable display name.
# Used in frontend UI (walk status badges, progress indicators) and API
# error messages.
WALK_DISPLAY_NAMES: dict[str, str] = {
    "walk_2a_scene_segmentation": "Scene Segmentation",
    "walk_2b_character_discovery": "Character Discovery",
    "walk_2c_alias_resolution": "Alias Resolution",
    "walk_2d_scene_presence": "Scene Presence",
    "walk_2e_span_attribution": "Span Attribution",
    "walk_2f_character_description": "Character Description",
    "walk_2g_voice_audition": "Voice Audition",
    "walk_2h_voice_assignment": "Voice Assignment",
    "walk_2i_delivery": "Delivery",
}
