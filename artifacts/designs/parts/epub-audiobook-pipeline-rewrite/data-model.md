# Data Model

## Span Hierarchy (Immutable Source)

```python
@dataclass(frozen=True)
class SpanID:
    book_id: str
    chapter_idx: int
    scene_idx: int
    paragraph_idx: int
    sentence_idx: int
    
    def to_path(self) -> str:
        return f"{self.book_id}/ch{self.chapter_idx}/sc{self.scene_idx}/p{self.paragraph_idx}/s{self.sentence_idx}"
```

## Character Ledger

```python
@dataclass
class CharacterRecord:
    canonical_id: str
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
    span_id: SpanID
    text: str
    evidence_type: str
    confidence: float
```

## Script Line Schema (Output of Step 6)

```python
@dataclass
class ScriptLine:
    line_id: str
    span_ids: list[SpanID]
    speaker: str          # "NARRATOR" | canonical_id | "UNKNOWN"
    text: str             # verbatim from source (NEVER rewritten)
    delivery: str | None
    confidence: float
    evidence_spans: list[SpanID]
```

## Span ID Stability

- Derived from EPUB structure (chapter/paragraph/sentence indices)
- Re-upload of same EPUB = full pipeline reset (no diffing)
- Acceptable tradeoff: simpler implementation, no versioning complexity
