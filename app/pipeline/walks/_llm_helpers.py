"""Shared LLM helpers for walk modules.

Extracted from duplicated ``_call_llm`` implementations that shared an identical
OpenAI chat-completion wrapper differing only in the system prompt.
"""

from __future__ import annotations

from typing import Any


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

    return response.choices[0].message.content.strip()
