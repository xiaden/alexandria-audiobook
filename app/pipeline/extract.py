"""EPUB text extraction for the audiobook pipeline.

Reads an EPUB file (ZIP → container.xml → OPF → spine → XHTML), extracts
plain text using marker-based chapter/paragraph boundaries, and returns a
structured spine dict with sentence and quotation spans.

This is Phase 1 of the pipeline rewrite — it produces the structured dict
but does NOT insert into the database (that happens in Phase 2).
"""

from __future__ import annotations

import re
import uuid
import zipfile
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from typing import TYPE_CHECKING

from app.utils import PARA_MARKER

if TYPE_CHECKING:
    from app.pipeline.adapter import PipelineStorage

# Fixed series UUID for EPUBs with no series context.
_DEFAULT_SERIES_ID = "00000000-0000-4000-8000-000000000001"

# Sentence boundary: split on whitespace following sentence-ending punctuation.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

# Quotation detection: double-quoted or single-quoted segments.
_QUOTATION_RE = re.compile(r""""([^"]*)"|'([^']*)'""")


# ---------------------------------------------------------------------------
# HTML → text extraction
# ---------------------------------------------------------------------------


class _HTMLTextExtractor(HTMLParser):
    """Strip HTML tags, inserting PARA_MARKER between block-level elements."""

    BLOCK_TAGS = frozenset({
        "p", "div", "h1", "h2", "h3", "h4", "h5", "h6",
        "li", "blockquote", "br", "hr", "tr", "section", "article",
    })
    SKIP_TAGS = frozenset({"style", "script"})

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._pending_newline = False
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        elif tag in self.BLOCK_TAGS:
            self._pending_newline = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self.SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        if self._pending_newline and self._parts:
            self._parts.append("\n" + PARA_MARKER + "\n")
            self._pending_newline = False
        self._parts.append(data)

    def get_text(self) -> str:
        return "".join(self._parts)


# ---------------------------------------------------------------------------
# EPUB parsing helpers
# ---------------------------------------------------------------------------


def _read_opf(
    zf: zipfile.ZipFile,
) -> tuple[dict[str, str], list[str]]:
    """Parse EPUB container and OPF; return (manifest, spine_ids)."""
    container_xml = zf.read("META-INF/container.xml")
    container = ET.fromstring(container_xml)
    ns = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
    rootfile_el = container.find(".//c:rootfile", ns)
    if rootfile_el is None:
        raise ValueError("Invalid EPUB: no rootfile found in container.xml")
    opf_path = rootfile_el.get("full-path")
    if opf_path is None:
        raise ValueError("Invalid EPUB: rootfile missing full-path attribute")

    opf_xml = zf.read(opf_path)
    opf = ET.fromstring(opf_xml)
    _opf_ns_match = re.match(r"\{(.+?)\}", opf.tag)
    opf_ns = _opf_ns_match.group(0) if _opf_ns_match else ""
    opf_dir = opf_path.rsplit("/", 1)[0] + "/" if "/" in opf_path else ""

    manifest: dict[str, str] = {}
    for item in opf.findall(f".//{opf_ns}item"):
        item_id = item.get("id")
        href = item.get("href")
        media_type = item.get("media-type", "")
        if item_id and href and "html" in media_type:
            manifest[item_id] = opf_dir + href

    spine_ids = [
        itemref.get("idref")
        for itemref in opf.findall(f".//{opf_ns}itemref")
        if itemref.get("idref")
    ]
    return manifest, spine_ids


def _parse_epub_chapters(epub_path: str) -> list[str]:
    """Parse EPUB and return chapter texts in spine order.

    Each chapter text contains PARA_MARKER between block-level elements.
    """
    with zipfile.ZipFile(epub_path, "r") as zf:
        manifest, spine_ids = _read_opf(zf)
        chapters: list[str] = []
        for item_id in spine_ids:
            href = manifest.get(item_id)
            if href is None:
                continue
            try:
                html_bytes = zf.read(href)
            except KeyError:
                continue
            extractor = _HTMLTextExtractor()
            extractor.feed(html_bytes.decode("utf-8", errors="replace"))
            text = extractor.get_text().strip()
            if text:
                chapters.append(text)
    return chapters


# ---------------------------------------------------------------------------
# Sentence splitting and span detection
# ---------------------------------------------------------------------------


def _split_sentences(text: str) -> list[str]:
    """Split paragraph text into sentences using regex (no nltk dependency)."""
    raw = _SENTENCE_SPLIT_RE.split(text.strip())
    return [s.strip() for s in raw if s.strip()]


def _sentence_to_spans(sentence: str) -> list[dict[str, str]]:
    """Convert a sentence into sentence and quotation spans."""
    spans: list[dict[str, str]] = []
    last_end = 0
    for match in _QUOTATION_RE.finditer(sentence):
        before = sentence[last_end:match.start()].strip()
        if before:
            spans.append(_make_span("sentence", before))
        quoted = match.group(1) if match.group(1) is not None else match.group(2)
        if quoted:
            spans.append(_make_span("quotation", quoted))
        last_end = match.end()
    after = sentence[last_end:].strip()
    if after:
        spans.append(_make_span("sentence", after))
    if not spans:
        spans.append(_make_span("sentence", sentence.strip()))
    return spans


def _build_spans_for_paragraph(text: str) -> list[dict[str, str]]:
    """Split paragraph text into sentence/quotation spans."""
    spans: list[dict[str, str]] = []
    for sentence in _split_sentences(text):
        spans.extend(_sentence_to_spans(sentence))
    return spans


# ---------------------------------------------------------------------------
# Paragraph and chapter construction
# ---------------------------------------------------------------------------


def _make_span(span_type: str, text: str) -> dict[str, str]:
    """Create a span dict with a fresh UUID."""
    return {"id": str(uuid.uuid4()), "span_type": span_type, "text": text}


def _build_paragraphs(chapter_text: str) -> list[dict]:
    """Split chapter text on PARA_MARKER and build paragraph dicts."""
    paragraphs: list[dict] = []
    for para_text in chapter_text.split(PARA_MARKER):
        text = para_text.strip()
        if not text:
            continue
        spans = _build_spans_for_paragraph(text)
        paragraphs.append({"id": str(uuid.uuid4()), "spans": spans})
    return paragraphs


def _build_chapters(chapter_texts: list[str]) -> list[dict]:
    """Build chapter dicts from raw chapter texts."""
    chapters: list[dict] = []
    for chapter_text in chapter_texts:
        paragraphs = _build_paragraphs(chapter_text)
        chapters.append({"id": str(uuid.uuid4()), "paragraphs": paragraphs})
    return chapters


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_epub_text(
    epub_path: str,
    book_id: str,
    storage: PipelineStorage,
) -> dict:
    """Extract EPUB text and return a structured spine dict.

    Parameters
    ----------
    epub_path:
        Filesystem path to the EPUB file.
    book_id:
        Identifier for the book (assigned by the caller).
    storage:
        Pipeline storage adapter (unused in Phase 1; reserved for Phase 2
        database insertion).

    Returns
    -------
    dict with keys:
        series_id : str — fixed default series UUID
        book_id : str — the provided book_id
        chapters : list of {id, paragraphs: [{id, spans: [{id, span_type, text}]}]}
    """
    chapter_texts = _parse_epub_chapters(epub_path)
    chapters = _build_chapters(chapter_texts)
    return {
        "series_id": _DEFAULT_SERIES_ID,
        "book_id": book_id,
        "chapters": chapters,
    }
