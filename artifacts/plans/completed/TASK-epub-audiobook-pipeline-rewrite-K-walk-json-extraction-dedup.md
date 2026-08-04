# Task: Walk JSON Extraction Deduplication

## Problem Statement
All 9 walk modules have identical JSON extraction code: try `json.loads(response_text)`, catch `JSONDecodeError`, fall back to `re.search(r"[\s\S]*", response_text)` + `json.loads(match.group(0))`. This pattern is duplicated 9 times across walk_2a through walk_2i. The `_llm_helpers.py` module already exists with a `chat_completion()` function but doesn't provide JSON extraction. This duplication increases maintenance burden and inconsistency risk.

## Dependencies
- None — this is a pure refactoring with no behavioral changes

## Phases

### Phase 1: Extract JSON extraction to shared helper
- [x] Add `extract_json_from_llm_response(response_text: str, expected_type: str = "auto") -> dict | list | None` to `app/pipeline/walks/_llm_helpers.py`
    **Note:** Added extract_json_from_llm_response(response_text, expected_type="auto") to app/pipeline/walks/_llm_helpers.py. Function tries json.loads first, falls back to regex extraction (dict pattern \{[\s\S]*\} and/or list pattern \[[\s\S]*\] based on expected_type). Returns None on failure. Validates type matches expected_type ("auto" accepts dict|list, "dict" rejects list, "list" rejects dict). Added json, logging, re imports and module-level logger. Ruff clean.
  **Notes:** Parameters: `response_text` (The raw LLM response text), `expected_type` ("auto" (try dict then list), "dict", or "list"). Returns: Parsed JSON object, or None if parsing fails. Logic: (1) Try `json.loads(response_text)`, (2) If fails, try `re.search(r"\{[\s\S]*\}", response_text)` for dict, (3) If fails, try `re.search(r"\[[\s\S]*\]", response_text)` for list, (4) If all fail, return None. Add docstring explaining the fallback strategy.
- [x] Add unit tests for `extract_json_from_llm_response` in `tests/pipeline/test_walk_helpers.py`
    **Note:** Created tests/pipeline/test_walk_helpers.py with 21 unit tests for extract_json_from_llm_response. Test classes: TestExtractJsonBasic (5 tests: valid dict/list/empty/nested), TestExtractJsonRegexFallback (7 tests: extra text before/after/both sides, markdown code blocks), TestExtractJsonFailures (5 tests: invalid JSON, empty string, non-JSON text, whitespace, partial JSON), TestExtractJsonTypeValidation (9 tests: expected_type enforces dict/list/auto, rejects mismatches with direct parse and regex fallback), TestExtractJsonAutoMode (2 tests: auto tries dict first then list). All tests follow pytest conventions, no fixtures needed (pure function tests). Ruff clean.
  **Notes:** Test 1: Valid JSON dict. Test 2: Valid JSON list. Test 3: JSON with extra text before/after. Test 4: Invalid JSON returns None. Test 5: Empty string returns None. Test 6: Non-JSON text returns None. Test 7: expected_type="dict" rejects list. Test 8: expected_type="list" rejects dict.
- [x] Run `pytest tests/pipeline/test_walk_helpers.py -v` and verify all tests pass
    **Note:** Ran pytest tests/pipeline/test_walk_helpers.py -v: all 28 tests passed in 0.44s. The extract_json_from_llm_response function is now the canonical JSON extraction implementation. All walks will use it in Phase 2.
  **Notes:** This is the canonical JSON extraction function. All walks will use it.

### Phase 2: Refactor walk modules to use shared helper
- [x] Refactor `walk_2a_scene_segmentation.py` to use `extract_json_from_llm_response(response_text, "list")`
    **Note:** Refactored walk_2a_scene_segmentation.py: replaced try/except json.loads + inline `import re` + re.search pattern in _parse_llm_response (lines 219-234) with `extract_json_from_llm_response(response_text, expected_type="list")`. Removed `import json` (not used elsewhere). Added `extract_json_from_llm_response` to import from `._llm_helpers`. Preserved downstream isinstance check and paragraph index mapping logic. Preserved f-string logger format. Ruff clean.
  **Notes:** Remove the try/except json.loads + re.search pattern. Replace with single call to helper. Verify the walk still produces the expected output structure.
- [x] Refactor `walk_2b_character_discovery.py` to use `extract_json_from_llm_response(response_text, "list")`
    **Note:** Refactored walk_2b_character_discovery.py: replaced try/except json.loads + re.search pattern in _parse_llm_response (lines 356-371) with `extract_json_from_llm_response(response_text, expected_type="list")`. Removed `import re` (only used in extraction). Kept `import json` (used for json.dumps(aliases) in _process_character). Added `extract_json_from_llm_response` to import from `._llm_helpers`. Preserved downstream isinstance check and field validation/normalization logic. Preserved f-string logger format. Ruff clean.
- [x] Refactor `walk_2c_alias_resolution.py` to use `extract_json_from_llm_response(response_text, "list")`
    **Note:** Refactored walk_2c_alias_resolution.py: replaced try/except json.loads + re.search pattern in _parse_llm_response (lines 257-274) with `extract_json_from_llm_response(response_text, expected_type="list")`. Removed `import re` (only used in extraction). Kept `import json` (used extensively in _build_alias_resolution_prompt and _consolidate_aliases for json.loads/json.dumps on aliases). Added `extract_json_from_llm_response` to import from `._llm_helpers`. Preserved downstream isinstance check and character_id validation against valid_ids set. Preserved %s logger format (unique among walks). Ruff clean.
- [x] Refactor `walk_2d_scene_presence.py` to use `extract_json_from_llm_response(response_text, "list")`
    **Note:** Refactored walk_2d_scene_presence.py: replaced try/except json.loads + re.search pattern in _parse_llm_response (lines 322-337) with `extract_json_from_llm_response(response_text, expected_type="list")`. Removed `import json` and `import re` (neither used elsewhere). Added `extract_json_from_llm_response` to import from `._llm_helpers`. Preserved downstream isinstance check and character_id/confidence validation logic. Preserved f-string logger format. Ruff clean.
- [x] Refactor `walk_2e_span_attribution.py` to use `extract_json_from_llm_response(response_text, "dict")`
    **Note:** Refactored walk_2e_span_attribution.py: replaced try/except json.loads + re.search pattern in _parse_llm_response (lines 360-375) with `extract_json_from_llm_response(response_text, expected_type="dict")`. Removed `import json` and `import re` (neither used elsewhere). Added `extract_json_from_llm_response` to import from `._llm_helpers`. Preserved downstream isinstance check and character_id type validation logic. Preserved f-string logger format. Ruff clean.
- [x] Refactor `walk_2f_character_description.py` to use `extract_json_from_llm_response(response_text, "dict")`
    **Note:** Refactored walk_2f_character_description.py: replaced try/except json.loads + re.search pattern in _parse_llm_response (lines 350-365) with `extract_json_from_llm_response(response_text, expected_type="dict")`. Removed `import re` (only used in extraction). Kept `import json` (used in _build_description_prompt for json.loads(character_aliases)). Added `extract_json_from_llm_response` to import from `._llm_helpers`. Preserved downstream isinstance check and description string validation logic. Preserved f-string logger format. Ruff clean.
- [x] Refactor `walk_2g_voice_audition.py` to use `extract_json_from_llm_response(response_text, "dict")`
    **Note:** Refactored walk_2g_voice_audition.py: replaced try/except json.loads + re.search pattern in _parse_llm_response (lines 378-393) with `extract_json_from_llm_response(response_text, expected_type="dict")`. Removed `import re` (only used in extraction). Kept `import json` (used in _store_voice_profile for json.dumps and _build_voice_audition_prompt for json.loads(character_aliases)). Added `extract_json_from_llm_response` to import from `._llm_helpers`. Preserved downstream isinstance check and voice_profile dict validation logic. Preserved f-string logger format. Ruff clean.
- [x] Refactor `walk_2h_voice_assignment.py` to use `extract_json_from_llm_response(response_text, "dict")`
    **Note:** Refactored walk_2h_voice_assignment.py: replaced try/except json.loads + re.search pattern in _parse_llm_response (lines 363-378) with `extract_json_from_llm_response(response_text, expected_type="dict")`. Removed `import re` (only used in extraction). Kept `import json` (used in _get_voice_profile for json.loads and _build_voice_assignment_prompt for json.dumps). Added `extract_json_from_llm_response` to import from `._llm_helpers`. Preserved downstream isinstance check and voice_config_id type validation logic. Preserved f-string logger format. Ruff clean.
- [x] Refactor `walk_2i_delivery.py` to use `extract_json_from_llm_response(response_text, "dict")`
    **Note:** Refactored walk_2i_delivery.py: replaced try/except json.loads + re.search pattern in _parse_llm_response (lines 416-431) with `extract_json_from_llm_response(response_text, expected_type="dict")`. Removed `import json` and `import re` (neither used elsewhere). Added `extract_json_from_llm_response` to import from `._llm_helpers`. Preserved downstream isinstance check and instruct string validation logic. Preserved f-string logger format. Ruff clean.
- [x] For each walk, verify the refactored code produces the same output as before
    **Note:** Verification complete. (1) All 196 walk tests pass (test_walk_2a through test_walk_2i, 1.98s). (2) No `re.search` calls remain in any walk_2*.py module. (3) All 9 walks import and call `extract_json_from_llm_response` from `._llm_helpers`. (4) Remaining `json.JSONDecodeError` occurrences (6 in walks 2c/2f/2g/2h) are all for parsing stored JSON strings (aliases, voice profiles from DB) — not LLM response extraction. (5) Ruff clean across all 9 files.
  **Notes:** This is a mechanical refactoring. No behavioral changes. Each walk's JSON structure is different, but the extraction logic is identical.

### Phase 3: Verification
- [x] Run `pytest tests/pipeline/test_walk_2a.py` through `test_walk_2i.py` and verify all tests pass
    **Note:** Ran all walk tests (test_walk_2a through test_walk_2i) plus test_walk_helpers.py: 224 tests passed in 2.44s. All walk modules correctly use the shared extract_json_from_llm_response helper with no behavioral regressions.
- [x] Run `pytest tests/pipeline/ -v` and verify all 592+ tests pass
    **Note:** Ran full pipeline test suite (pytest tests/pipeline/ -v): 630 tests passed in 7.14s, 1 warning (StarletteDeprecationWarning from FastAPI testclient — unrelated to this change). Zero failures. The JSON extraction deduplication refactoring is fully verified across the entire pipeline.
- [x] Verify no walk module has duplicated JSON extraction code (grep for `re.search.*\[` or `re.search.*\{`)
    **Note:** Searched for re.search(r'\[...') and re.search(r'\{...') patterns across all app/pipeline/walks/walk_2*.py files: zero matches found. No duplicated JSON extraction code remains in any walk module. The extraction logic exists solely in _llm_helpers.py.
- [x] Verify all walks import `extract_json_from_llm_response` from `_llm_helpers`
    **Completion:** Plan K complete. QA PASS Round 1. 630 tests pass, zero regressions. JSON extraction deduplicated from 9 walks into single extract_json_from_llm_response() helper in _llm_helpers.py. 11 files changed. No behavioral changes.
    **Note:** Searched for extract_json_from_llm_response across all walk_2*.py files: found 18 matches = 9 imports + 9 call sites, exactly one per walk module (2a through 2i). Each walk imports from ._llm_helpers and calls with the correct expected_type ("list" for 2a/2b/2c/2d, "dict" for 2e/2f/2g/2h/2i). Confirmed: all 9 walks use the shared helper.
  **Notes:** After this refactoring, the JSON extraction pattern should exist in exactly one place: `_llm_helpers.py`.

## Completion Criteria
- `extract_json_from_llm_response` function exists in `_llm_helpers.py` with unit tests
- All 9 walk modules use the shared helper
- No duplicated JSON extraction code remains in walk modules
- All walk tests pass
- No behavioral changes

## Negative Constraints
- Do NOT change the JSON extraction logic — only move it to a shared location
- Do NOT change the expected JSON structure for any walk
- Do NOT modify walk prompts or LLM call logic
- Do NOT skip tests that fail — the refactoring must preserve behavior
