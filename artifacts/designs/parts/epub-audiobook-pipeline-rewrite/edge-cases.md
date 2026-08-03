# Edge Cases & Risks

## High-Risk Areas

1. **Walk 2d (Quotation Attribution) accuracy:** Highest-risk walk. Must handle:
   - Explicit attribution ("X said")
   - Implicit attribution (dialogue without speaker tag)
   - Multi-speaker scenes
   - Ambiguous pronouns
   - Confidence calibration critical — false high confidence worse than false low

2. **Ledger merge conflicts:** When alias resolution merges records:
   - Evidence lists concatenate, not overwrite
   - Canonical name selection: prefer most frequent, then longest, then first-appearing
   - Manual override via UI if auto-merge is wrong

3. **Voice reference text selection:**
   - Verbatim character speech, 8-30 words
   - Emotionally representative (score by emotion word density)
   - Cross-scene representativeness (avoid one-off emotional spikes)
   - Configurable per character if default selection is poor

4. **Audio QA regeneration:**
   - Same clone reference as original generation
   - Fresh correction context (never the context that created the mistake)
   - Per-line regeneration, not full scene re-render

5. **Span operation edge cases:**
   - **Split at word boundary:** Must handle mid-word splits gracefully (reject or snap to nearest boundary)
   - **Merge across scene boundaries:** Allowed, but logs warning — user may be merging distinct scenes
   - **Move to invalid position:** Reject if new position violates chapter ordering
   - **Delete with dependent annotations:** Orphan annotations are logged for human review, not silently dropped
   - **Content-overlap reconciliation failure:** If Jaccard < 0.6 and LCS < 50%, annotation is assigned to best-match span with reduced confidence (×0.8) and flagged for human review

## Migration Risks

6. **Frontend workflow change:** 5-step becomes 7-step (Setup→Extract→Annotate→Review→Voices→Editor→Result). User training required.

7. **Performance regression:** 6 sequential LLM passes adds latency vs current single pass. Mitigate via:
   - Within-walk parallelism (by chapter/scene)
   - Caching walk outputs (re-run only changed walks)
   - **Targeted re-attribution** (not full re-walk) — only affected scenes are re-processed on edit

8. **Backward compatibility bridge:** New pipeline writes both `pipeline_state/script.json` (new format) and `annotated_script.json` (legacy format). The legacy format is a deterministic transformation — code, not LLM. Risk: if the transformation code has a bug, downstream consumers (editor tab, M4B export, TTS rendering) receive malformed data. Mitigation: unit tests for the transformation function, comparing output against known-good legacy format samples.

9. **Config unification:** Multiple config resolution paths exist: `find_config_path()` (utils.py) resolves relative to utils.py location, `ProjectManager.__init__` (project.py:99) resolves via `ALEXANDRIA_CONFIG_PATH or os.path.join(root_dir, "app", "config.json")`, and `app.py CONFIG_PATH` uses a third resolution. These paths can diverge, causing subprocesses to read different config than the main process. Mitigation: unify all paths to `find_config_path()`, remove divergent fallbacks.

10. **Temperature departure from existing default:** Extraction walks use temperature 0.1, which is 6× below the existing 0.6 default in `GenerationConfig`. If users have tuned their global `llm.temperature` for the old pipeline, the new pipeline's per-task overrides take precedence. Risk: users may not realize the new pipeline uses different temperatures. Mitigation: document the temperature policy in the Setup tab UI, show per-walk temperatures in the Annotation Review tab.

11. **UUID presentation index confusion:** Users and agents operate on presentation indices (1..N), but annotations bind to UUIDs. If a user references "span 7" in a bug report, but the presentation has been renumbered since the operation, the UUID may not match the user's expectation. Mitigation: always show presentation indices in UI, log UUID→index mappings in operation log for debugging.
