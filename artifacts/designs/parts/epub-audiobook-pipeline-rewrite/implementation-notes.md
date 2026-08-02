# Implementation Notes

## Confidence Threshold

- **Default:** 0.7 auto-accept, < 0.5 auto-reject, 0.5-0.7 surface to user
- **Configurable:** `pipeline.confidence_threshold` in config.json
- **Per-walk override:** Optional — some walks may need stricter/looser thresholds (e.g., scene segmentation can be stricter, quotation attribution looser)

## Temperature Policy

- **0.1 for extraction** (factual tasks: scene segmentation, character discovery, alias resolution, quotation attribution, character description)
- **0.3 for creative** (interpretive tasks: delivery context, voice description)
- **Rationale:** Extraction should be deterministic; creative tasks benefit from slight variation

## Evidence Storage

- **Location:** `pipeline_state/annotations/` directory within project
- **Format:** JSON per walk (e.g., `walk_2a_scenes.json`, `walk_2d_attributions.json`)
- **Benefits:** Enables re-running individual walks, debugging, audit trail
- **Cleanup:** Old walk outputs overwritten on re-run (no versioning)

## Migration Coexistence

- **Old pipeline:** Writes `annotated_script.json` (current format)
- **New pipeline:** Writes `pipeline_state/` directory with walk outputs + final `script.json`
- **User toggle:** Setup tab has "Use new pipeline (experimental)" checkbox
- **Deprecation timeline:** TBD (see Open Questions in DD)

## Walk Parallelism

- **Sequential dependency:** Walks 2a-2f must run in order (each depends on prior)
- **Within-walk parallelism:** Each walk can process chapters/scenes in parallel
- **Example:** Walk 2b (Character Discovery) processes all chapters in parallel, then aggregates

## Frontend Integration

- **New tab:** "Annotation Review" between Extract and Voices
- **Shows:** Low-confidence items with neighbor context (±2 paragraphs)
- **Actions:** Accept, reject, edit, re-submit to LLM with corrected context
- **Character ledger editor:** View/edit canonical names, aliases, relationships
