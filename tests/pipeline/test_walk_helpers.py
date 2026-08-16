"""Tests for shared walk helpers — extract_json_from_llm_response."""

from unittest.mock import patch

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


# ===========================================================================
# Part B (per-walk log streaming) — helper-instrumentation contract tests.
#
# These classes lock the Phase 3 helper instrumentation contracts from
# artifacts/designs/parts/per-walk-log-streaming/CONTRACTS.md (§ Part B):
# ``chat_completion`` emits an optional ``llm`` record and
# ``extract_json_from_llm_response`` emits an optional ``parse`` record through
# the ``WALK_LOG_SINK`` ContextVar. Both records are emitted only when a sink is
# attached; the helper's return value is preserved exactly and a raising sink is
# swallowed (never propagated).
# ===========================================================================

import types


class _Sink:
    """Minimal stand-in for a WalkLogSink capturing appended records."""

    def __init__(self):
        self.records = []

    def append(self, event, payload=None, *, terminal=False):
        self.records.append({"event": event, "data": payload, "terminal": terminal})

    def append_terminal(self, status, payload=None):
        return None

    def close_partial(self, status="aborted"):
        pass

    def close(self):
        pass


class _Message:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content, finish_reason):
        self.message = _Message(content)
        self.finish_reason = finish_reason


class _Response:
    def __init__(self, model, choice, usage):
        self.model = model
        self.choices = [choice]
        self.usage = usage


class _Completions:
    def __init__(self, response):
        self._response = response

    def create(self, **kwargs):
        return self._response


class _Chat:
    def __init__(self, response):
        self.completions = _Completions(response)


class _Client:
    def __init__(self, response):
        self.chat = _Chat(response)


def _set_sink():
    """Set a fresh ``_Sink`` on the ``WALK_LOG_SINK`` ContextVar
    and return ``(sink, token)`` so the caller resets it in ``finally``."""
    from app.pipeline.walks._llm_helpers import WALK_LOG_SINK

    sink = _Sink()
    token = WALK_LOG_SINK.set(sink)
    return sink, token


def _set_raising_sink():
    """Set a raising sink on ``WALK_LOG_SINK`` and return ``(sink, token)``.

    Used to verify a sink failure is swallowed (logged) and never propagates,
    so the helper's return value is preserved exactly."""
    from app.pipeline.walks._llm_helpers import WALK_LOG_SINK

    class _RaisingSink(_Sink):
        def append(self, event, payload=None, *, terminal=False):
            raise OSError("cannot write record")

        def append_terminal(self, status, payload=None):
            raise OSError("cannot write terminal")

    sink = _RaisingSink()
    token = WALK_LOG_SINK.set(sink)
    return sink, token


# ---------------------------------------------------------------------------
# P1-S5 — chat_completion emits an optional ``llm`` record
# ---------------------------------------------------------------------------


class TestChatCompletionLLMRecord:
    """The ``chat_completion`` ``llm`` record emission contract, driven through
    the ``WALK_LOG_SINK`` ContextVar. Emits a record when a sink is attached; the
    ``chat_completion`` return value is preserved exactly; the legacy
    ``app.utils.log_llm_response`` is never called; a raising sink is swallowed."""

    def test_chat_completion_emits_llm_record(self):
        from app.pipeline.walks._llm_helpers import WALK_LOG_SINK, chat_completion

        usage = types.SimpleNamespace(prompt_tokens=10, completion_tokens=20, total_tokens=30)
        response = _Response("gpt-4o", _Choice("  hi there  ", "stop"), usage)
        client = _Client(response)
        sink, token = _set_sink()
        try:
            result = chat_completion(client, "gpt-4o", 0.1, "low", "sys", "usr")
        finally:
            WALK_LOG_SINK.reset(token)
        assert result == "hi there"
        assert len(sink.records) == 1
        rec = sink.records[0]
        assert rec["event"] == "llm"
        data = rec["data"]
        assert data["model"] == "gpt-4o"
        assert data["temperature"] == 0.1
        assert data["reasoning_effort"] == "low"
        assert data["prompts"] == {"system": "sys", "user": "usr"}
        assert data["response"] == "hi there"
        assert data["finish_reason"] == "stop"
        assert data["usage"] == {
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "total_tokens": 30,
        }
        assert "timestamp" in data

    def test_chat_completion_null_sdk_fields_are_null(self):
        from app.pipeline.walks._llm_helpers import WALK_LOG_SINK, chat_completion

        # SDK omits model, finish_reason, usage -> null in the record.
        response = _Response(None, _Choice("  x  ", None), None)
        client = _Client(response)
        sink, token = _set_sink()
        try:
            chat_completion(client, "m", 0.2, None, "sys", "usr")
        finally:
            WALK_LOG_SINK.reset(token)
        data = sink.records[0]["data"]
        assert data["model"] is None
        assert data["finish_reason"] is None
        assert data["usage"] is None
        # temperature/reasoning_effort null ONLY when the arg was null.
        assert data["temperature"] == 0.2
        assert data["reasoning_effort"] is None

    def test_chat_completion_null_args_produce_null_temperature_and_effort(self):
        from app.pipeline.walks._llm_helpers import WALK_LOG_SINK, chat_completion

        response = _Response("m", _Choice("  ok  ", "stop"), None)
        client = _Client(response)
        sink, token = _set_sink()
        try:
            chat_completion(client, "m", None, None, "sys", "usr")
        finally:
            WALK_LOG_SINK.reset(token)
        data = sink.records[0]["data"]
        assert data["temperature"] is None
        assert data["reasoning_effort"] is None

    def test_chat_completion_preserves_return_value_and_never_logs_legacy(self):
        from app.pipeline.walks._llm_helpers import WALK_LOG_SINK, chat_completion

        response = _Response("m", _Choice("  exact   ", "stop"), None)
        client = _Client(response)
        sink, token = _set_sink()
        try:
            with patch("app.utils.log_llm_response") as mock_legacy:
                result = chat_completion(client, "m", 0.5, "low", "sys", "usr")
        finally:
            WALK_LOG_SINK.reset(token)
        assert result == "exact"
        mock_legacy.assert_not_called()
        assert len(sink.records) == 1

    def test_chat_completion_without_sink_emits_nothing(self):
        from app.pipeline.walks._llm_helpers import chat_completion

        response = _Response("m", _Choice("  ok  ", "stop"), None)
        client = _Client(response)
        result = chat_completion(client, "m", 0.5, None, "sys", "usr")
        assert result == "ok"

    def test_chat_completion_raising_sink_preserves_return(self):
        """A sink whose append raises is swallowed (never propagates) and the
        EXACT original return value is preserved."""
        from app.pipeline.walks._llm_helpers import (
            WALK_LOG_SINK,
            chat_completion,
            get_walk_log_sink,
        )

        response = _Response("m", _Choice("  exact   ", "stop"), None)
        client = _Client(response)
        _, token = _set_raising_sink()
        try:
            result = chat_completion(client, "m", 0.5, "low", "sys", "usr")
        finally:
            WALK_LOG_SINK.reset(token)
        assert result == "exact"
        assert get_walk_log_sink() is None


# ---------------------------------------------------------------------------
# P1-S6 — extract_json_from_llm_response emits an optional ``parse`` record
# ---------------------------------------------------------------------------


class TestExtractJsonParseRecord:
    """The ``extract_json_from_llm_response`` ``parse`` record emission
    contract. Every parser decision (direct success, regex success, invalid
    expected type, type mismatch, malformed) must emit a ``parse`` record
    preserving the exact original return value; a raising sink is swallowed."""

    def test_direct_success_emits_parse_record(self):
        from app.pipeline.walks._llm_helpers import (
            WALK_LOG_SINK,
            extract_json_from_llm_response,
        )

        sink, token = _set_sink()
        try:
            result = extract_json_from_llm_response('{"a": 1}', expected_type="auto")
        finally:
            WALK_LOG_SINK.reset(token)
        assert result == {"a": 1}
        assert len(sink.records) == 1
        rec = sink.records[0]
        assert rec["event"] == "parse"
        data = rec["data"]
        assert data["success"] is True
        assert data["expected_type"] == "auto"

    def test_regex_success_emits_parse_record(self):
        from app.pipeline.walks._llm_helpers import (
            WALK_LOG_SINK,
            extract_json_from_llm_response,
        )

        sink, token = _set_sink()
        try:
            result = extract_json_from_llm_response(
                'Here is the result:\n{"a": 1}', expected_type="auto"
            )
        finally:
            WALK_LOG_SINK.reset(token)
        assert result == {"a": 1}
        data = sink.records[0]["data"]
        assert data["success"] is True
        assert data["expected_type"] == "auto"

    def test_invalid_expected_type_emits_failure_record(self):
        from app.pipeline.walks._llm_helpers import (
            WALK_LOG_SINK,
            extract_json_from_llm_response,
        )

        sink, token = _set_sink()
        try:
            result = extract_json_from_llm_response('{"a": 1}', expected_type="string")
        finally:
            WALK_LOG_SINK.reset(token)
        assert result is None
        data = sink.records[0]["data"]
        assert data["success"] is False
        assert data["expected_type"] == "string"

    def test_type_mismatch_emits_failure_record(self):
        from app.pipeline.walks._llm_helpers import (
            WALK_LOG_SINK,
            extract_json_from_llm_response,
        )

        sink, token = _set_sink()
        try:
            result = extract_json_from_llm_response('[1, 2, 3]', expected_type="dict")
        finally:
            WALK_LOG_SINK.reset(token)
        assert result is None
        data = sink.records[0]["data"]
        assert data["success"] is False
        assert data["expected_type"] == "dict"

    def test_malformed_emits_failure_record(self):
        from app.pipeline.walks._llm_helpers import (
            WALK_LOG_SINK,
            extract_json_from_llm_response,
        )

        sink, token = _set_sink()
        try:
            result = extract_json_from_llm_response(
                "this is not json at all {{{", expected_type="auto"
            )
        finally:
            WALK_LOG_SINK.reset(token)
        assert result is None
        data = sink.records[0]["data"]
        assert data["success"] is False

    def test_parse_raising_sink_preserves_return(self):
        """A raising sink on a malformed parse outcome is swallowed and the
        EXACT original return value (None) is preserved."""
        from app.pipeline.walks._llm_helpers import (
            WALK_LOG_SINK,
            extract_json_from_llm_response,
            get_walk_log_sink,
        )

        _, token = _set_raising_sink()
        try:
            result = extract_json_from_llm_response(
                "this is not json at all {{{", expected_type="auto"
            )
        finally:
            WALK_LOG_SINK.reset(token)
        assert result is None
        assert get_walk_log_sink() is None
