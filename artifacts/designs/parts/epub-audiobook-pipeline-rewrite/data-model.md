# Data Model

## Span Model: UUID Identity with Presentation Indices

Spans are the immutable text units of the source EPUB. Identity is decoupled from presentation:

- **Storage:** Each span has a UUID (immutable identity). Text content never changes.
- **Presentation:** Sequential indices `1..N` derived from a mutable `seq` ordering field. Agents, the LLM, and humans operate ONLY on presentation indices.
- **Annotations:** Bind to UUIDs only — never to presentation indices.
- **Renumbering:** Free (recomputed per render from `seq` ordering). Non-cascading in identity space.

```python
@dataclass
class Span:
    span_id: str              # UUID v4, immutable identity
    text: str                 # Verbatim from source — NEVER rewritten
    seq: int                  # Mutable ordering field; presentation index = rank(seq)
    parent_span_id: str | None  # For hierarchical grouping (sentence → paragraph → scene)
    span_type: str            # "sentence" | "paragraph" | "scene" | "chapter"
    content_hash: str         # SHA-256 of normalized text, for overlap matching
    metadata: dict            # Chapter index, source position, etc.
```

### Presentation Index Computation

```python
def compute_presentation_indices(spans: list[Span]) -> dict[str, int]:
    """Returns {span_id: presentation_index}. Called per render."""
    sorted_spans = sorted(spans, key=lambda s: s.seq)
    return {span.span_id: idx + 1 for idx, span in enumerate(sorted_spans)}
```

Presentation indices are ephemeral — recomputed on every read. Storage never contains them.

## Span Operations (Edit Interface)

Agents/LLM/humans issue operations against presentation indices. The storage layer resolves index→UUID, executes, and renumbers.

| Operation | Semantics | Re-Attribution Scope |
|-----------|-----------|---------------------|
| `SPLIT(span_idx, position)` | Split span at word boundary. Creates 2 new UUIDs, reassigns annotations by content overlap. | Only the 2 new spans |
| `MERGE(span_idx_a, span_idx_b)` | Concatenate two adjacent spans. Creates 1 new UUID, merges annotations. | Only the merged span |
| `MOVE(span_idx, new_position)` | Change `seq` to reorder. Annotations ride along (bound to UUID). | None |
| `DELETE(span_idx)` | Remove span. Annotations orphaned (logged for review). | None |
| `RENAME(scene_id, new_name)` | Update scene label. Non-destructive — scene is aggregation of references. | None |

### Content-Overlap Reconciliation

When spans are split/merged, annotations from the old UUID must transfer to new UUIDs. Algorithm:

1. **Normalized-token Jaccard similarity:** Tokenize (lowercase, strip punctuation), compute `|A ∩ B| / |A ∪ B|`. Threshold: ≥ 0.6 → transfer annotation.
2. **Longest-common-substring fallback:** If Jaccard below threshold, check LCS ≥ 50% of shorter span's length → transfer.
3. **Partial overlap:** If a single old annotation spans multiple new spans, assign to the span with highest overlap. Log ambiguity for human review.

```python
def reconcile_annotations(old_span: Span, new_spans: list[Span], annotations: list[Annotation]) -> list[Annotation]:
    """Reassign annotations from old_span to new_spans by content overlap."""
    reassigned = []
    for ann in annotations:
        if ann.span_id != old_span.span_id:
            continue
        best_match = max(new_spans, key=lambda ns: token_jaccard(ann.source_text, ns.text))
        if token_jaccard(ann.source_text, best_match.text) >= 0.6:
            reassigned.append(Annotation(span_id=best_match.span_id, **ann.fields))
        else:
            reassigned.append(Annotation(span_id=best_match.span_id, **ann.fields, confidence=ann.confidence * 0.8))
            # Log for human review — annotation derived from different context
    return reassigned
```

**Caveat:** UUIDs make structural correction free but do NOT self-correct an annotation derived from wrong context. If the LLM attributed a quote to the wrong speaker because it lacked scene context, re-segmentation preserves that wrong attribution — targeted re-attribution or human correction is required.

## Immutability Scope

- **Immutable:** TEXT content. The LLM never rewrites source text.
- **Mutable:** Segmentation (span boundaries), scene assignments, presentation ordering.
- **Re-segmentation:** When segmentation changes, content-hash/overlap matching reconciles old→new spans so annotations ride along.

## Annotation Binding

Annotations bind to the finest stable unit (sentence or paragraph). Coarser units (scene) are aggregations of references to finer units, NOT positioned containers.

```python
@dataclass
class Annotation:
    annotation_id: str        # UUID
    span_id: str              # Binds to sentence/paragraph UUID (finest unit)
    annotation_type: str      # "scene_boundary" | "character_mention" | "speaker_attribution" | "delivery" | "alias"
    value: Any                # Type-specific payload
    confidence: float
    source_walk: str          # "2a" | "2b" | "2c" | "2d" | "2e" | "2f"
    evidence_text: str        # Verbatim text that triggered this annotation
```

Scene membership is derived: a scene is a set of span_id references, not a positioned container. Scene edits (rename, boundary move) are non-destructive to annotations.

## Character Ledger

```python
@dataclass
class CharacterRecord:
    canonical_id: str         # UUID
    canonical_name: str
    aliases: list[str]
    relationships: dict[str, str]
    evidence: CharacterEvidence
    confidence: float

@dataclass
class CharacterEvidence:
    narration_mentions: list[EvidenceSpan]
    dialogue_samples: list[EvidenceSpan]
    audible_traits: dict[str, Any] | None = None

@dataclass
class EvidenceSpan:
    span_id: str              # UUID reference (NOT presentation index)
    text: str                 # Verbatim excerpt
    evidence_type: str        # "narration" | "dialogue" | "description"
    confidence: float
```

## Script Line Schema (Output of Step 6)

```python
@dataclass
class ScriptLine:
    line_id: str              # UUID
    span_ids: list[str]       # UUID references to source spans
    speaker: str              # "NARRATOR" | canonical_id | "UNKNOWN"
    text: str                 # Verbatim from source (NEVER rewritten)
    delivery: str | None      # Emotional/delivery annotation
    confidence: float
    evidence_spans: list[str] # UUID references
    scene_id: str | None      # Derived from scene boundary annotations
```

## Walk Ordering as Correctness Guarantee

Segmentation walks (2a) are reviewed (confidence filter + human) BEFORE dependent walks (2b-2f) run. This ensures:
- Character discovery (2b) operates on stable scene boundaries
- Attribution (2d) operates on reviewed scenes + characters
- Scene is context, not source of truth — canonical ledger is primary truth

## Span ID Stability

- UUIDs are immutable per span content. Re-upload of same EPUB = full pipeline reset (new UUIDs).
- No diffing or versioning across uploads — acceptable tradeoff for simpler implementation.
- Within a session, UUIDs survive all structural edits (split/merge/move) via content-overlap reconciliation.
