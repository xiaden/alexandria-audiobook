"""Shared LLM helpers for walk modules.

Extracted from duplicated ``_call_llm`` implementations that shared an identical
OpenAI chat-completion wrapper differing only in the system prompt.
"""

from __future__ import annotations

import json
import logging
import re
import time
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.pipeline.walks.log_service import WalkLogSink

logger = logging.getLogger(__name__)

#: Thread-local sink seam (Part B). The runner sets the current run's walk-log
#: sink on this variable immediately before ``walk_module.execute(...)`` and
#: resets it in ``finally`` on every terminal path. Helpers read it to emit
#: ``llm``/``parse`` records; they never mutate it and never import a walk
#: module. Walk modules themselves never touch the seam (static-audit enforced).
WALK_LOG_SINK: ContextVar[WalkLogSink | None] = ContextVar(
    "walk_log_sink", default=None
)


def get_walk_log_sink() -> WalkLogSink | None:
    """Return the current thread's walk-log sink, or ``None`` when none is set.

    The runner sets the sink for the duration of a reserved walk's execution.
    Outside a running walk (or when the runner has no ``log_service``) this
    returns ``None``.
    """
    return WALK_LOG_SINK.get()


def chat_completion(
    client: Any,
    model_name: str,
    temperature: float,
    reasoning_effort: str | None,
    system_prompt: str,
    user_prompt: str,
) -> str:
    """Call the LLM and return the stripped response text.

    Parameters
    ----------
    client:
        An OpenAI-compatible client.
    model_name:
        The model identifier (e.g. ``"gpt-4o"``).
    temperature:
        Sampling temperature (0.0–2.0).
    reasoning_effort:
        Optional reasoning-effort hint forwarded as ``extra_body``.
    system_prompt:
        Walk-specific system prompt.
    user_prompt:
        The assembled user prompt (walk-specific text + data).

    Returns
    -------
    str
        The ``.content`` of the first choice, stripped of leading/trailing
        whitespace.
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    extra_body: dict[str, str] = {}
    if reasoning_effort is not None:
        extra_body["reasoning_effort"] = reasoning_effort

    response = client.chat.completions.create(
        model=model_name,
        messages=messages,
        temperature=temperature,
        extra_body=extra_body if extra_body else None,
    )

    text = response.choices[0].message.content.strip()

    # Optional per-run capture (Part B): emit an ``llm`` record when the runner
    # has attached a walk-log sink. Metadata the SDK omits is null (not dropped
    # keys); temperature/reasoning_effort come from the arguments and are null
    # only when the argument was null. The helper's return value is authoritative:
    # any sink failure is logged and swallowed, never propagated.
    sink = get_walk_log_sink()
    if sink is not None:
        try:
            first_choice = response.choices[0] if response.choices else None
            usage = getattr(response, "usage", None)
            usage_payload = None
            if usage is not None:
                usage_payload = {
                    "prompt_tokens": getattr(usage, "prompt_tokens", None),
                    "completion_tokens": getattr(usage, "completion_tokens", None),
                    "total_tokens": getattr(usage, "total_tokens", None),
                }
            sink.append(
                "llm",
                {
                    "timestamp": int(time.time() * 1000),
                    "model": getattr(response, "model", None),
                    "temperature": temperature,
                    "reasoning_effort": reasoning_effort,
                    "prompts": {"system": system_prompt, "user": user_prompt},
                    "response": text,
                    "finish_reason": (
                        getattr(first_choice, "finish_reason", None)
                        if first_choice is not None
                        else None
                    ),
                    "usage": usage_payload,
                },
            )
        except Exception:
            logger.warning("failed to emit llm record", exc_info=True)

    return text


def _emit_parse_record(success: bool, expected_type: str, **extra: Any) -> None:
    """Emit an optional ``parse`` sink record for one parser outcome.

    Best-effort: a missing sink means no record; a raising sink is logged and
    swallowed so the parser's decision/return is never altered.
    """
    sink = get_walk_log_sink()
    if sink is None:
        return
    try:
        payload: dict[str, Any] = {"success": success, "expected_type": expected_type}
        payload.update(extra)
        sink.append("parse", payload)
    except Exception:
        logger.warning("failed to emit parse record", exc_info=True)


def extract_json_from_llm_response(
    response_text: str, expected_type: str = "auto"
) -> dict | list | None:
    """Extract JSON from an LLM response text.

    Attempts to parse JSON from the response, with fallback to regex extraction
    if the response contains extra text around the JSON.

    Parameters
    ----------
    response_text:
        The raw LLM response text, which may contain JSON surrounded by
        explanatory text or markdown.
    expected_type:
        The expected JSON type: "auto" (accept dict or list), "dict", or "list".
        When "auto", tries dict regex first, then list regex as fallback.
        When "dict" or "list", only attempts the corresponding regex pattern.

    Returns
    -------
    dict | list | None
        The parsed JSON object, or None if parsing fails or the type doesn't
        match expected_type.

    Fallback Strategy
    -----------------
    1. Try ``json.loads(response_text)`` for direct parsing.
    2. If that fails, use regex to extract JSON:
       - For dict: ``re.search(r"\\{[\\s\\S]*\\}", response_text)``
       - For list: ``re.search(r"\\[[\\s\\S]*\\]", response_text)``
       - For auto: try dict regex first, then list regex.
    3. If all attempts fail, return None.

    Type Validation
    ---------------
    After successful extraction, validates that the result matches expected_type:
    - "auto": accepts dict or list.
    - "dict": returns None if result is not a dict.
    - "list": returns None if result is not a list.
    """
    if expected_type not in ("auto", "dict", "list"):
        logger.error(f"Invalid expected_type: {expected_type}")
        _emit_parse_record(False, expected_type, error="invalid_expected_type")
        return None

    # Step 1: Try direct JSON parsing
    try:
        result = json.loads(response_text)
        # Validate type if expected_type is specified
        if expected_type == "dict" and not isinstance(result, dict):
            logger.error(
                f"Expected dict but got {type(result).__name__}: {response_text[:200]}"
            )
            _emit_parse_record(False, expected_type, error="type_mismatch")
            return None
        if expected_type == "list" and not isinstance(result, list):
            logger.error(
                f"Expected list but got {type(result).__name__}: {response_text[:200]}"
            )
            _emit_parse_record(False, expected_type, error="type_mismatch")
            return None
        # For "auto", accept either dict or list
        _emit_parse_record(True, expected_type)
        return result
    except json.JSONDecodeError:
        # Step 2: Try regex fallback based on expected_type
        pass

    # Regex fallback patterns
    dict_pattern = r"\{[\s\S]*\}"
    list_pattern = r"\[[\s\S]*\]"

    if expected_type == "dict":
        patterns_to_try = [dict_pattern]
    elif expected_type == "list":
        patterns_to_try = [list_pattern]
    else:  # auto
        patterns_to_try = [dict_pattern, list_pattern]

    for pattern in patterns_to_try:
        match = re.search(pattern, response_text)
        if match:
            try:
                result = json.loads(match.group(0))
                # Double-check type (regex might match nested structures)
                if expected_type == "dict" and not isinstance(result, dict):
                    continue
                if expected_type == "list" and not isinstance(result, list):
                    continue
                _emit_parse_record(True, expected_type)
                return result
            except json.JSONDecodeError:
                # This regex match wasn't valid JSON, try next pattern
                continue

    # All attempts failed
    logger.error(f"Failed to extract JSON from LLM response: {response_text[:200]}")
    _emit_parse_record(False, expected_type, error="malformed")
    return None
