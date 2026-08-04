"""Tests for shared walk helpers — extract_json_from_llm_response."""

from app.pipeline.walks._llm_helpers import extract_json_from_llm_response


# ---------------------------------------------------------------------------
# extract_json_from_llm_response — basic parsing
# ---------------------------------------------------------------------------


class TestExtractJsonBasic:
    """Test direct JSON parsing (no regex fallback needed)."""

    def test_valid_json_dict(self):
        """A clean JSON dict should parse directly."""
        text = '{"character_id": "abc-123", "confidence": 0.9}'
        result = extract_json_from_llm_response(text)
        assert result == {"character_id": "abc-123", "confidence": 0.9}

    def test_valid_json_list(self):
        """A clean JSON list should parse directly."""
        text = '[{"paragraph_ids": ["P1", "P2"], "confidence": 0.8}]'
        result = extract_json_from_llm_response(text)
        assert result == [{"paragraph_ids": ["P1", "P2"], "confidence": 0.8}]

    def test_valid_json_empty_dict(self):
        """An empty JSON dict should parse."""
        assert extract_json_from_llm_response("{}") == {}

    def test_valid_json_empty_list(self):
        """An empty JSON list should parse."""
        assert extract_json_from_llm_response("[]") == []

    def test_valid_json_nested(self):
        """Nested JSON should parse correctly."""
        text = '{"data": {"nested": [1, 2, 3]}, "meta": "ok"}'
        result = extract_json_from_llm_response(text)
        assert result == {"data": {"nested": [1, 2, 3]}, "meta": "ok"}


# ---------------------------------------------------------------------------
# extract_json_from_llm_response — regex fallback
# ---------------------------------------------------------------------------


class TestExtractJsonRegexFallback:
    """Test regex extraction when response has extra text around JSON."""

    def test_dict_with_extra_text_before(self):
        """Dict JSON preceded by explanatory text should be extracted."""
        text = 'Here is the result:\n{"character_id": "abc-123"}'
        result = extract_json_from_llm_response(text)
        assert result == {"character_id": "abc-123"}

    def test_dict_with_extra_text_after(self):
        """Dict JSON followed by explanatory text should be extracted."""
        text = '{"confidence": 0.9}\nThat is my analysis.'
        result = extract_json_from_llm_response(text)
        assert result == {"confidence": 0.9}

    def test_dict_with_extra_text_both_sides(self):
        """Dict JSON surrounded by text should be extracted."""
        text = 'Sure! Here is the JSON:\n{"key": "value"}\nHope this helps!'
        result = extract_json_from_llm_response(text)
        assert result == {"key": "value"}

    def test_list_with_extra_text_before(self):
        """List JSON preceded by explanatory text should be extracted."""
        text = 'Here are the scenes:\n[{"paragraph_ids": ["P1"]}]'
        result = extract_json_from_llm_response(text, expected_type="list")
        assert result == [{"paragraph_ids": ["P1"]}]

    def test_list_with_extra_text_after(self):
        """List JSON followed by explanatory text should be extracted."""
        text = '[{"id": "1"}, {"id": "2"}]\nThese are the results.'
        result = extract_json_from_llm_response(text, expected_type="list")
        assert result == [{"id": "1"}, {"id": "2"}]

    def test_list_with_markdown_code_block(self):
        """List JSON inside markdown code fences should be extracted."""
        text = "```json\n[{\"name\": \"John\"}]\n```"
        result = extract_json_from_llm_response(text, expected_type="list")
        assert result == [{"name": "John"}]

    def test_dict_with_markdown_code_block(self):
        """Dict JSON inside markdown code fences should be extracted."""
        text = "```json\n{\"key\": \"value\"}\n```"
        result = extract_json_from_llm_response(text, expected_type="dict")
        assert result == {"key": "value"}


# ---------------------------------------------------------------------------
# extract_json_from_llm_response — failure cases
# ---------------------------------------------------------------------------


class TestExtractJsonFailures:
    """Test that invalid inputs return None."""

    def test_invalid_json_returns_none(self):
        """Completely invalid JSON should return None."""
        text = "this is not json at all {{{"
        result = extract_json_from_llm_response(text)
        assert result is None

    def test_empty_string_returns_none(self):
        """Empty string should return None."""
        assert extract_json_from_llm_response("") is None

    def test_non_json_text_returns_none(self):
        """Plain English text with no JSON should return None."""
        text = "I'm sorry, I couldn't process that request."
        assert extract_json_from_llm_response(text) is None

    def test_whitespace_only_returns_none(self):
        """Whitespace-only string should return None."""
        assert extract_json_from_llm_response("   \n\t  ") is None

    def test_partial_json_returns_none(self):
        """Truncated JSON should return None."""
        text = '{"key": "val'
        assert extract_json_from_llm_response(text) is None


# ---------------------------------------------------------------------------
# extract_json_from_llm_response — type validation
# ---------------------------------------------------------------------------


class TestExtractJsonTypeValidation:
    """Test expected_type parameter enforces type constraints."""

    def test_expected_type_dict_accepts_dict(self):
        """expected_type='dict' should accept a dict."""
        text = '{"key": "value"}'
        result = extract_json_from_llm_response(text, expected_type="dict")
        assert result == {"key": "value"}

    def test_expected_type_dict_rejects_list(self):
        """expected_type='dict' should reject a list and return None."""
        text = '[{"key": "value"}]'
        result = extract_json_from_llm_response(text, expected_type="dict")
        assert result is None

    def test_expected_type_list_accepts_list(self):
        """expected_type='list' should accept a list."""
        text = '[{"key": "value"}]'
        result = extract_json_from_llm_response(text, expected_type="list")
        assert result == [{"key": "value"}]

    def test_expected_type_list_rejects_dict(self):
        """expected_type='list' should reject a dict and return None."""
        text = '{"key": "value"}'
        result = extract_json_from_llm_response(text, expected_type="list")
        assert result is None

    def test_expected_type_auto_accepts_dict(self):
        """expected_type='auto' should accept a dict."""
        text = '{"key": "value"}'
        result = extract_json_from_llm_response(text, expected_type="auto")
        assert result == {"key": "value"}

    def test_expected_type_auto_accepts_list(self):
        """expected_type='auto' should accept a list."""
        text = '[1, 2, 3]'
        result = extract_json_from_llm_response(text, expected_type="auto")
        assert result == [1, 2, 3]

    def test_expected_type_dict_rejects_list_with_regex_fallback(self):
        """expected_type='dict' with text containing a list should return None
        even when regex fallback is needed."""
        text = "Here is the data:\n[1, 2, 3]\nDone."
        result = extract_json_from_llm_response(text, expected_type="dict")
        assert result is None

    def test_expected_type_list_rejects_dict_with_regex_fallback(self):
        """expected_type='list' with text containing only a dict should return None
        even when regex fallback is needed."""
        text = "Here is the data:\n{\"key\": \"value\"}\nDone."
        result = extract_json_from_llm_response(text, expected_type="list")
        assert result is None

    def test_invalid_expected_type_returns_none(self):
        """An invalid expected_type value should return None."""
        text = '{"key": "value"}'
        result = extract_json_from_llm_response(text, expected_type="string")
        assert result is None


# ---------------------------------------------------------------------------
# extract_json_from_llm_response — auto mode regex ordering
# ---------------------------------------------------------------------------


class TestExtractJsonAutoMode:
    """Test that auto mode tries dict regex first, then list regex."""

    def test_auto_prefers_dict_when_both_present(self):
        """When response contains both dict and list patterns, auto mode
        should find the dict first (greedy regex)."""
        # Note: regex is greedy, so \{[\s\S]*\} will match from first { to last }
        # This test verifies the function handles mixed content
        text = '{"wrapper": [1, 2, 3]}'
        result = extract_json_from_llm_response(text, expected_type="auto")
        # json.loads succeeds directly, returning the dict
        assert isinstance(result, dict)
        assert result == {"wrapper": [1, 2, 3]}

    def test_auto_falls_back_to_list_when_no_dict(self):
        """When response has no dict pattern, auto mode should find list."""
        text = "Results:\n[1, 2, 3]"
        result = extract_json_from_llm_response(text, expected_type="auto")
        assert result == [1, 2, 3]
