# Walk Definitions

| Walk | Input | Output | LLM Task Name | Temp | Confidence |
|------|-------|--------|---------------|------|-----------|
| 2a: Scene Segmentation | Raw chapter text | Scene boundaries per paragraph | `scene_segmentation` | 0.1 | High: structural markers; Low: ambiguous |
| 2b: Character Discovery | Scenes + text | Raw character mentions with span_ids | `character_discovery` | 0.1 | High: explicit names; Low: pronouns |
| 2c: Alias Resolution | Character roster | Alias groups → canonical IDs | `alias_resolution` | 0.1 | High: exact; Low: fuzzy/partial |
| 2d: Quotation Attribution | Scenes + chars + aliases + text | Quotation → speaker (preliminary) | `quotation_attribution` | 0.1 | High: explicit "X said"; Low: implicit |
| 2e: Character Description | Narration near intros | Physical/social/origin traits | `character_description` | 0.1 | High: explicit; Low: inferred |
| 2f: Delivery Context | Scene-level text | Emotional/delivery annotations | `delivery_context` | 0.3 | High: explicit emotion; Low: subtext |

## Walk Execution

- **Sequential dependency:** 2a → 2b → 2c → 2d → 2e → 2f (each depends on prior output)
- **Within-walk parallelism:** Each walk can parallelize by chapter/scene where possible
- **Evidence storage:** JSON in `annotations/` directory. Each walk writes its own file. Enables re-running individual walks.
- **Walk re-execution:** If user edits character ledger after walks 2a-2f, downstream walks must be manually re-triggered (not auto-re-run)
