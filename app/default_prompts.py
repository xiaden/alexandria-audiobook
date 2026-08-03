import os

_PROMPTS_FILE = os.path.join(os.path.dirname(__file__), "..", "default_prompts.txt")


_prompt_cache = {"mtime": None, "prompts": None}


def load_default_prompts():
    """Read default_prompts.txt from disk and return (system_prompt, user_prompt).

    Uses an mtime-based cache to pick up edits without restarting the app,
    avoiding redundant disk reads when the file hasn't changed.
    """
    try:
        mtime = os.path.getmtime(_PROMPTS_FILE)
    except FileNotFoundError:
        raise RuntimeError(
            f"default_prompts.txt not found at {os.path.abspath(_PROMPTS_FILE)}. "
            "This file is required for LLM prompt defaults."
        )

    if _prompt_cache["mtime"] == mtime and _prompt_cache["prompts"] is not None:
        return _prompt_cache["prompts"]

    try:
        with open(_PROMPTS_FILE, "r", encoding="utf-8") as f:
            raw = f.read()
    except Exception as e:
        raise RuntimeError(f"Error reading default_prompts.txt: {e}")

    parts = raw.split("---SEPARATOR---", maxsplit=1)
    if len(parts) != 2:
        raise RuntimeError(
            "default_prompts.txt is malformed: expected exactly one '---SEPARATOR---' delimiter."
        )

    prompts = (parts[0].strip(), parts[1].strip())
    _prompt_cache["mtime"] = mtime
    _prompt_cache["prompts"] = prompts
    return prompts


# Cached at import time — used by script generation (subprocess, fresh each run)
DEFAULT_SYSTEM_PROMPT, DEFAULT_USER_PROMPT = load_default_prompts()
