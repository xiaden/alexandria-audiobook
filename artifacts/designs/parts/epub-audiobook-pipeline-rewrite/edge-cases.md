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

## Migration Risks

5. **Frontend workflow change:** 5-step becomes 7-step (Setup→Extract→Annotate→Review→Voices→Editor→Result). User training required.

6. **Performance regression:** 6 sequential LLM passes adds latency vs current single pass. Mitigate via:
   - Within-walk parallelism (by chapter/scene)
   - Caching walk outputs (re-run only changed walks)
   - Partial re-runs (re-run from edited walk onward, not full pipeline)

7. **Backward compatibility:** Old pipeline writes `annotated_script.json`, new writes `pipeline_state/`. Both formats must coexist during migration. User toggles in Setup tab.
