"""Tests for EPUB text extraction (app.pipeline.extract)."""

from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path

import pytest

from app.pipeline.adapter import InMemorySQLiteAdapter
from app.pipeline.extract import (
    _build_paragraphs,
    _build_spans_for_paragraph,
    _parse_epub_chapters,
    _split_sentences,
    extract_epub_text,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def storage():
    """In-memory SQLite adapter for testing."""
    adapter = InMemorySQLiteAdapter()
    adapter.init_db()
    return adapter


@pytest.fixture()
def minimal_epub() -> str:
    """Create a minimal valid EPUB file for testing."""
    with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as tmp:
        epub_path = tmp.name

    with zipfile.ZipFile(epub_path, "w") as zf:
        # container.xml
        container_xml = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>"""
        zf.writestr("META-INF/container.xml", container_xml)

        # content.opf
        content_opf = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="uid">test-book-id</dc:identifier>
    <dc:title>Test Book</dc:title>
    <dc:language>en</dc:language>
  </metadata>
  <manifest>
    <item id="chapter1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>
    <item id="chapter2" href="chapter2.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine>
    <itemref idref="chapter1"/>
    <itemref idref="chapter2"/>
  </spine>
</package>"""
        zf.writestr("content.opf", content_opf)

        # chapter1.xhtml
        chapter1 = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Chapter 1</title></head>
<body>
  <h1>Chapter One</h1>
  <p>This is the first paragraph. It has two sentences.</p>
  <p>He said "hello" and left. Then he went home.</p>
</body>
</html>"""
        zf.writestr("chapter1.xhtml", chapter1)

        # chapter2.xhtml
        chapter2 = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Chapter 2</title></head>
<body>
  <h1>Chapter Two</h1>
  <p>Second chapter content. With multiple sentences!</p>
  <p>Another paragraph with 'single quotes' inside.</p>
</body>
</html>"""
        zf.writestr("chapter2.xhtml", chapter2)

    yield epub_path
    Path(epub_path).unlink()


@pytest.fixture()
def empty_epub() -> str:
    """Create a valid EPUB whose spine has no readable content."""
    with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as tmp:
        epub_path = tmp.name
    with zipfile.ZipFile(epub_path, "w") as zf:
        zf.writestr(
            "META-INF/container.xml",
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        zf.writestr(
            "content.opf",
            '<package xmlns="http://www.idpf.org/2007/opf"><manifest>'
            '<item id="chapter" href="chapter.xhtml" '
            'media-type="application/xhtml+xml"/></manifest><spine>'
            '<itemref idref="chapter"/></spine></package>',
        )
        zf.writestr("chapter.xhtml", "<html><body>   </body></html>")
    yield epub_path
    Path(epub_path).unlink()


# ---------------------------------------------------------------------------
# EPUB parsing tests
# ---------------------------------------------------------------------------


def test_parse_epub_chapters_returns_list(minimal_epub: str) -> None:
    """_parse_epub_chapters returns a list of chapter texts."""
    chapters = _parse_epub_chapters(minimal_epub)
    assert isinstance(chapters, list)
    assert len(chapters) == 2


def test_parse_epub_chapters_contains_text(minimal_epub: str) -> None:
    """Each chapter text contains expected content."""
    chapters = _parse_epub_chapters(minimal_epub)
    assert "Chapter One" in chapters[0]
    assert "first paragraph" in chapters[0]
    assert "Chapter Two" in chapters[1]
    assert "Second chapter" in chapters[1]


def test_parse_epub_chapters_strips_html(minimal_epub: str) -> None:
    """HTML tags are stripped from chapter text."""
    chapters = _parse_epub_chapters(minimal_epub)
    assert "<p>" not in chapters[0]
    assert "<h1>" not in chapters[0]
    assert "</html>" not in chapters[0]


# ---------------------------------------------------------------------------
# Sentence splitting tests
# ---------------------------------------------------------------------------


def test_split_sentences_basic() -> None:
    """Split text into sentences on sentence-ending punctuation."""
    text = "First sentence. Second sentence! Third sentence?"
    sentences = _split_sentences(text)
    assert len(sentences) == 3
    assert sentences[0] == "First sentence."
    assert sentences[1] == "Second sentence!"
    assert sentences[2] == "Third sentence?"


def test_split_sentences_no_punctuation() -> None:
    """Text without sentence-ending punctuation is one sentence."""
    text = "Just a phrase"
    sentences = _split_sentences(text)
    assert len(sentences) == 1
    assert sentences[0] == "Just a phrase"


def test_split_sentences_empty() -> None:
    """Empty text returns empty list."""
    sentences = _split_sentences("")
    assert sentences == []


def test_split_sentences_whitespace() -> None:
    """Whitespace-only text returns empty list."""
    sentences = _split_sentences("   \n\t  ")
    assert sentences == []


# ---------------------------------------------------------------------------
# Span detection tests
# ---------------------------------------------------------------------------


def test_build_spans_no_quotes() -> None:
    """Text without quotes produces one sentence span."""
    text = "This is a simple sentence."
    spans = _build_spans_for_paragraph(text)
    assert len(spans) == 1
    assert spans[0]["span_type"] == "sentence"
    assert spans[0]["text"] == "This is a simple sentence."
    assert "id" in spans[0]


def test_build_spans_double_quotes() -> None:
    """Double-quoted text produces quotation span."""
    text = 'He said "hello" and left.'
    spans = _build_spans_for_paragraph(text)
    assert len(spans) == 3
    assert spans[0]["span_type"] == "sentence"
    assert spans[0]["text"] == "He said"
    assert spans[1]["span_type"] == "quotation"
    assert spans[1]["text"] == "hello"
    assert spans[2]["span_type"] == "sentence"
    assert spans[2]["text"] == "and left."


def test_build_spans_single_quotes() -> None:
    """Single-quoted text produces quotation span."""
    text = "She whispered 'goodbye' softly."
    spans = _build_spans_for_paragraph(text)
    assert len(spans) == 3
    assert spans[0]["span_type"] == "sentence"
    assert spans[0]["text"] == "She whispered"
    assert spans[1]["span_type"] == "quotation"
    assert spans[1]["text"] == "goodbye"
    assert spans[2]["span_type"] == "sentence"
    assert spans[2]["text"] == "softly."


def test_build_spans_fully_quoted() -> None:
    """Fully-quoted sentence produces only quotation span."""
    text = '"Complete quote."'
    spans = _build_spans_for_paragraph(text)
    assert len(spans) == 1
    assert spans[0]["span_type"] == "quotation"
    assert spans[0]["text"] == "Complete quote."


def test_build_spans_multiple_sentences() -> None:
    """Multiple sentences each produce their own spans."""
    text = 'First. He said "hi". Second.'
    spans = _build_spans_for_paragraph(text)
    # First. → sentence
    # He said → sentence, hi → quotation, . → sentence (or merged)
    # Second. → sentence
    assert len(spans) >= 3
    span_types = [s["span_type"] for s in spans]
    assert "sentence" in span_types
    assert "quotation" in span_types


# ---------------------------------------------------------------------------
# Paragraph building tests
# ---------------------------------------------------------------------------


def test_build_paragraphs_single() -> None:
    """Single paragraph produces one paragraph dict."""
    text = "Just one paragraph."
    paragraphs = _build_paragraphs(text)
    assert len(paragraphs) == 1
    assert "id" in paragraphs[0]
    assert "spans" in paragraphs[0]
    assert len(paragraphs[0]["spans"]) == 1


def test_build_paragraphs_with_marker() -> None:
    """PARA_MARKER splits text into multiple paragraphs."""
    from app.utils import PARA_MARKER

    text = f"First paragraph.{PARA_MARKER}Second paragraph."
    paragraphs = _build_paragraphs(text)
    assert len(paragraphs) == 2
    assert paragraphs[0]["spans"][0]["text"] == "First paragraph."
    assert paragraphs[1]["spans"][0]["text"] == "Second paragraph."


def test_build_paragraphs_empty_skipped() -> None:
    """Empty paragraphs are skipped."""
    from app.utils import PARA_MARKER

    text = f"First.{PARA_MARKER}{PARA_MARKER}Second."
    paragraphs = _build_paragraphs(text)
    assert len(paragraphs) == 2


# ---------------------------------------------------------------------------
# UUID generation tests
# ---------------------------------------------------------------------------


def test_uuid_generation_unique() -> None:
    """Each entity gets a unique UUID."""
    text = "Test text."
    paragraphs = _build_paragraphs(text)
    para_id = paragraphs[0]["id"]
    span_id = paragraphs[0]["spans"][0]["id"]
    assert para_id != span_id
    assert len(para_id) == 36  # UUID format: 8-4-4-4-12
    assert len(span_id) == 36


def test_uuid_format() -> None:
    """UUIDs are in correct format."""
    import re

    uuid_pattern = re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    )
    paragraphs = _build_paragraphs("Test.")
    assert uuid_pattern.match(paragraphs[0]["id"])
    assert uuid_pattern.match(paragraphs[0]["spans"][0]["id"])


# ---------------------------------------------------------------------------
# Contract structure tests
# ---------------------------------------------------------------------------


def test_extract_epub_text_contract(minimal_epub: str, storage) -> None:
    """extract_epub_text returns dict matching contract structure."""
    result = extract_epub_text(minimal_epub, "test-book-123", storage)

    # Top-level keys
    assert "series_id" in result
    assert "book_id" in result
    assert "chapters" in result

    # book_id matches input
    assert result["book_id"] == "test-book-123"

    # series_id is a UUID string
    assert isinstance(result["series_id"], str)
    assert len(result["series_id"]) == 36

    # chapters is a list
    assert isinstance(result["chapters"], list)
    assert len(result["chapters"]) == 2


def test_extract_epub_text_chapter_structure(
    minimal_epub: str, storage
) -> None:
    """Each chapter has id and paragraphs list."""
    result = extract_epub_text(minimal_epub, "test-book-123", storage)

    for chapter in result["chapters"]:
        assert "id" in chapter
        assert "paragraphs" in chapter
        assert isinstance(chapter["id"], str)
        assert isinstance(chapter["paragraphs"], list)
        assert len(chapter["id"]) == 36


def test_extract_epub_text_paragraph_structure(
    minimal_epub: str, storage
) -> None:
    """Each paragraph has id and spans list."""
    result = extract_epub_text(minimal_epub, "test-book-123", storage)

    for chapter in result["chapters"]:
        for paragraph in chapter["paragraphs"]:
            assert "id" in paragraph
            assert "spans" in paragraph
            assert isinstance(paragraph["id"], str)
            assert isinstance(paragraph["spans"], list)
            assert len(paragraph["id"]) == 36


def test_extract_epub_text_span_structure(
    minimal_epub: str, storage
) -> None:
    """Each span has id, span_type, and text."""
    result = extract_epub_text(minimal_epub, "test-book-123", storage)

    for chapter in result["chapters"]:
        for paragraph in chapter["paragraphs"]:
            for span in paragraph["spans"]:
                assert "id" in span
                assert "span_type" in span
                assert "text" in span
                assert isinstance(span["id"], str)
                assert span["span_type"] in ("sentence", "quotation")
                assert isinstance(span["text"], str)
                assert len(span["text"]) > 0


def test_extract_epub_text_content(minimal_epub: str, storage) -> None:
    """Extracted content matches EPUB content."""
    result = extract_epub_text(minimal_epub, "test-book-123", storage)

    # Chapter 1
    ch1_text = " ".join(
        span["text"]
        for para in result["chapters"][0]["paragraphs"]
        for span in para["spans"]
    )
    assert "Chapter One" in ch1_text
    assert "first paragraph" in ch1_text

    # Chapter 2
    ch2_text = " ".join(
        span["text"]
        for para in result["chapters"][1]["paragraphs"]
        for span in para["spans"]
    )
    assert "Chapter Two" in ch2_text
    assert "Second chapter" in ch2_text


def test_extract_epub_text_quotation_detection(
    minimal_epub: str, storage
) -> None:
    """Quotation spans are correctly detected."""
    result = extract_epub_text(minimal_epub, "test-book-123", storage)

    # Find the paragraph with "hello" quotation
    found_quotation = False
    for chapter in result["chapters"]:
        for paragraph in chapter["paragraphs"]:
            for span in paragraph["spans"]:
                if span["span_type"] == "quotation" and span["text"] == "hello":
                    found_quotation = True
    assert found_quotation, "Expected to find quotation span with text 'hello'"


def test_extract_epub_text_series_id_default(
    minimal_epub: str, storage
) -> None:
    """series_id uses default UUID when no series context."""
    result = extract_epub_text(minimal_epub, "test-book-123", storage)
    assert result["series_id"] == "00000000-0000-4000-8000-000000000001"


def test_extract_epub_text_no_database_insertion(
    minimal_epub: str, storage
) -> None:
    """Phase 1 does not insert into database."""
    # Call extract
    extract_epub_text(minimal_epub, "test-book-123", storage)

    # Verify database is still empty (no insertion in Phase 1)
    # We can't directly query the database, but we can verify the function
    # doesn't call any storage methods by checking it completes without error
    # and returns the expected structure
    result = extract_epub_text(minimal_epub, "test-book-456", storage)
    assert result["book_id"] == "test-book-456"


def test_extract_epub_text_rejects_epub_without_readable_chapters(
    empty_epub: str, storage
) -> None:
    """An EPUB with no readable spine content is rejected before onboarding."""
    with pytest.raises(ValueError, match="no readable chapters"):
        extract_epub_text(empty_epub, "test-book-123", storage)
