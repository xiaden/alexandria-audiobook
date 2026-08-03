import os
import json
import time
import tempfile


# Structure markers for flattened EPUB text (emitted by app.py extract_epub_text /
# _HTMLTextExtractor, consumed by the pipeline chunker).
# PARA_MARKER is a soft boundary: consecutive paragraphs merge into one chunk up to
# max_size. CHAP_MARKER is a hard boundary: a chunk never spans two chapters.
# Both are stripped before text reaches the LLM.
PARA_MARKER = "<[para]>"
CHAP_MARKER = "<[chap]>"


def atomic_json_write(data, target_path, max_retries=5):
    """Atomically write JSON data using a temp file and os.replace.

    Includes retry logic with exponential backoff for Windows file locking
    (Access is denied / file in use errors).
    """
    directory = os.path.dirname(target_path) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        for attempt in range(max_retries):
            try:
                os.replace(tmp_path, target_path)
                return
            except OSError as e:
                if attempt < max_retries - 1 and (
                    e.errno == 5
                    or "Access is denied" in str(e)
                    or "being used by another process" in str(e)
                ):
                    delay = 0.05 * (2 ** attempt)
                    time.sleep(delay)
                    continue
                raise
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass

import re
import gc

# ── JSON Helpers ──

def load_json(path, default=None):
    """Load JSON from path, returning default on any failure."""
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError, OSError):
        return default


def save_json(path, data, atomic=True):
    """Write JSON, using atomic write by default."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if atomic:
        atomic_json_write(data, path)
    else:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)


# ── Config Loading ──

def find_config_path():
    """Return path to config.json, checking env var or default location."""
    return os.environ.get("ALEXANDRIA_CONFIG_PATH") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "config.json"
    )


def load_app_config(config_path=None):
    """Load the full app config.json, or None on failure."""
    if config_path is None:
        config_path = find_config_path()
    return load_json(config_path, default=None)


def load_llm_config(config_path=None):
    """Load LLM settings from config.json with fallback defaults."""
    config = load_app_config(config_path) or {}
    llm = config.get("llm", {})
    return {
        "base_url": llm.get("base_url", "http://localhost:11434/v1"),
        "api_key": llm.get("api_key", "local"),
        "model_name": llm.get("model_name", "richardyoung/qwen3-14b-abliterated:Q8_0"),
    }


def resolve_task_llm(task_name: str, config_path=None) -> dict:
    """Resolve per-task LLM config.

    Returns ``{'model_name': str, 'reasoning_effort': str|None, 'temperature': float}``.

    Resolution order: task-specific override -> global default -> hardcoded fallback.
    """
    # Hardcoded fallback defaults
    _FALLBACK_MODEL = "richardyoung/qwen3-14b-abliterated:Q8_0"
    _FALLBACK_REASONING = None
    _FALLBACK_TEMPERATURE = 0.6

    config = load_app_config(config_path) or {}
    llm = config.get("llm", {})

    # Global defaults from config (or fallback)
    global_model = llm.get("model_name", _FALLBACK_MODEL)
    global_reasoning = llm.get("reasoning_effort", _FALLBACK_REASONING)
    global_temperature = llm.get("temperature", _FALLBACK_TEMPERATURE)

    # Task-specific overrides
    task_overrides = llm.get("task_overrides", {})
    task_override = task_overrides.get(task_name, {}) if isinstance(task_overrides, dict) else {}

    # Resolve model_name: task override -> global -> hardcoded fallback
    override_model = task_override.get("model_name") if isinstance(task_override, dict) else None
    model = override_model if override_model else global_model

    # Resolve reasoning_effort: task override -> global -> None
    override_reasoning = task_override.get("reasoning_effort") if isinstance(task_override, dict) else None
    reasoning = override_reasoning if override_reasoning else global_reasoning

    # Resolve temperature: task override -> global -> hardcoded fallback.
    # Uses `is not None` (not truthiness) so an explicit 0.0 is honored.
    override_temperature = task_override.get("temperature") if isinstance(task_override, dict) else None
    temperature = override_temperature if override_temperature is not None else global_temperature

    return {"model_name": model, "reasoning_effort": reasoning, "temperature": temperature}


def load_generation_config(config_path=None):
    """Load generation settings from config.json with fallback defaults."""
    config = load_app_config(config_path) or {}
    return config.get("generation", {})


def load_prompts_config(config_path=None):
    """Load prompts section from config.json."""
    config = load_app_config(config_path) or {}
    return config.get("prompts", {})


def load_tts_config(config_path=None):
    """Load TTS settings from config.json with defaults."""
    config = load_app_config(config_path) or {}
    return config.get("tts", {})


def create_llm_client(config_path=None):
    """Load LLM config and return (OpenAI client, model_name)."""
    from openai import OpenAI
    llm = load_llm_config(config_path)
    return OpenAI(base_url=llm["base_url"], api_key=llm["api_key"]), llm["model_name"]


# ── LLM Response Logging ──

def log_llm_response(log_name, label, text, finish_reason=None, usage=None, attempt=1):
    """Append a formatted LLM response to logs/{log_name}."""
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, log_name)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"\n{'='*80}\n")
        f.write(f"{label} | attempt {attempt} | finish_reason={finish_reason}\n")
        if usage:
            f.write(f"tokens: prompt={getattr(usage, 'prompt_tokens', '?')} completion={getattr(usage, 'completion_tokens', '?')}\n")
        f.write(f"{'─'*80}\n")
        f.write(text)
        f.write(f"\n{'='*80}\n")


# ── JSON Cleaning / Repair ──

def clean_json_string(text):
    """Clean and extract valid JSON array from LLM response."""
    # Remove thinking tags and their content
    text = re.sub(r'<thinking>.*?</thinking>', '', text, flags=re.DOTALL)
    # Remove markdown code block fences
    text = re.sub(r'```json\s*', '', text)
    text = re.sub(r'```\s*', '', text)
    # Remove leading/trailing whitespace
    text = text.strip()
    # If the text already starts with [ and ends with ], return as-is
    if text.startswith('[') and text.endswith(']'):
        return text
    # Try to find a JSON array in the text
    array_match = re.search(r'\[.*\]', text, re.DOTALL)
    if array_match:
        return array_match.group()
    # Fall back: try to extract anything between the first { and the last }
    obj_match = re.search(r'\{.*\}', text, re.DOTALL)
    if obj_match:
        return f"[{obj_match.group()}]"
    return text


def repair_json_array(json_text):
    """Auto-repair common JSON formatting issues in LLM output within an array."""
    def _filter_entries(entries, max_attempts=5):
        """Remove entries with missing keys, return filtered list."""
        if not entries:
            return entries
        entries = [e for e in entries if e is not None]
        required_keys = ["speaker", "text", "start", "end"]
        for attempt in range(max_attempts):
            filtered = []
            removed = 0
            for entry in entries:
                if not isinstance(entry, dict):
                    removed += 1
                    continue
                # Check all required keys exist
                if not all(k in entry for k in required_keys):
                    removed += 1
                    continue
                # Check that text is meaningful
                text_val = entry.get("text", "")
                if not isinstance(text_val, str) or not text_val.strip():
                    removed += 1
                    continue
                # Ensure speaker is sensible
                speaker = str(entry.get("speaker", ""))
                if speaker.strip() and speaker != "None":
                    filtered.append(entry)
                else:
                    removed += 1
            if removed == 0:
                break
            entries = filtered
        return entries

    try:
        import json as _json
        entries = _json.loads(json_text)
        entries = _filter_entries(entries)
        return _json.dumps(entries, indent=2, ensure_ascii=False)
    except _json.JSONDecodeError:
        pass

    # Try to fix common issues
    text = json_text
    # Remove trailing commas before closing brackets
    text = re.sub(r',\s*\]', ']', text)
    text = re.sub(r',\s*\}', '}', text)
    # Fix single quotes to double quotes for property names
    text = re.sub(r"'([^']+)'", r'"\1"', text)
    # Try parsing again
    try:
        import json as _json
        entries = _json.loads(text)
        entries = _filter_entries(entries)
        return _json.dumps(entries, indent=2, ensure_ascii=False)
    except _json.JSONDecodeError:
        pass

    # Last resort: try extracting partial entries
    try:
        import json as _json
        partials = re.findall(r'\{[^{}]*\}', text)
        entries = []
        for p in partials:
            try:
                entry = _json.loads(p)
                entries.append(entry)
            except _json.JSONDecodeError:
                continue
        entries = _filter_entries(entries)
        if entries:
            return _json.dumps(entries, indent=2, ensure_ascii=False)
    except Exception:
        pass

    return json_text


def salvage_json_entries(json_text):
    """Salvage individual JSON entries from malformed array text."""
    try:
        import json as _json
        partials = re.findall(r'\{[^{}]*\}', json_text)
        entries = []
        for p in partials:
            try:
                entry = _json.loads(p)
                entries.append(entry)
            except _json.JSONDecodeError:
                continue
        return _json.dumps(entries, indent=2, ensure_ascii=False) if entries else json_text
    except Exception:
        return json_text


# ── GPU / System Utils ──

def clear_gpu_cache():
    """Free GPU memory: garbage-collect Python objects, then clear CUDA cache."""
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        pass


def resolve_device(device_str="auto"):
    """Resolve 'auto' device to the best available GPU/CPU."""
    if device_str != "auto":
        return device_str
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            return "mps"
    except ImportError:
        pass
    return "cpu"


def setup_rocm():
    """Apply ROCm-specific optimizations. No-op on NVIDIA/CPU."""
    try:
        import torch
    except ImportError:
        return
    if not (hasattr(torch.version, "hip") and torch.version.hip):
        return

    os.environ.setdefault("MIOPEN_FIND_MODE", "2")
    os.environ.setdefault("MIOPEN_LOG_LEVEL", "4")
    os.environ.setdefault("FLASH_ATTENTION_TRITON_AMD_ENABLE", "TRUE")

    try:
        from triton.compiler import compiler as triton_compiler
        if not hasattr(triton_compiler, "triton_key"):
            import triton
            triton_compiler.triton_key = lambda: f"pytorch-triton-rocm-{triton.__version__}"
    except ImportError:
        pass

    _patch_rdna_device_properties(torch)


def _patch_rdna_device_properties(torch_mod):
    """Monkey-patch torch.cuda.get_device_properties for RDNA2/3 GPUs."""
    if hasattr(torch_mod.cuda, '_rdna_props_patched'):
        return
    _rdna_corrections = {
        "7900 XTX": (96, 64), "7900 XT": (84, 64), "7900 GRE": (80, 64),
        "7800 XT": (60, 64), "7700 XT": (54, 64), "7600": (32, 64),
        "6950 XT": (80, 64), "6900 XT": (80, 64), "6800 XT": (72, 64),
        "6800": (60, 64), "6750 XT": (40, 64), "6700 XT": (40, 64),
        "6700": (36, 64), "6650 XT": (32, 64), "6600 XT": (32, 64), "6600": (28, 64),
    }
    original_fn = torch_mod.cuda.get_device_properties
    _cache = {}

    def _patched_get_device_properties(device=None):
        if device is None:
            device = torch_mod.cuda.current_device()
        key = int(device) if not isinstance(device, int) else device
        if key in _cache:
            return _cache[key]
        props = original_fn(device)
        correction = None
        for substr, vals in _rdna_corrections.items():
            if substr in props.name:
                correction = vals
                break
        if correction:
            from types import SimpleNamespace
            true_cus, true_warp = correction
            patched = SimpleNamespace()
            for attr in dir(props):
                if not attr.startswith('_'):
                    try:
                        setattr(patched, attr, getattr(props, attr))
                    except (AttributeError, RuntimeError):
                        pass
            patched.multi_processor_count = true_cus
            patched.warp_size = true_warp
            _cache[key] = patched
            return patched
        _cache[key] = props
        return props

    torch_mod.cuda.get_device_properties = _patched_get_device_properties
    torch_mod.cuda._rdna_props_patched = True


def resolve_local_model_path(model_id):
    """Check if a HuggingFace model is cached locally and return its snapshot path."""
    try:
        from huggingface_hub import try_to_load_from_cache
    except ImportError:
        return None
    result = try_to_load_from_cache(model_id, "config.json")
    if isinstance(result, str):
        return os.path.dirname(result)
    return None


def load_model_from_cache(model_cls, model_id, **load_kwargs):
    """Load a model, preferring local cache to avoid network issues.

    Checks if the model snapshot exists in the HF cache and loads from
    the local directory path directly, bypassing all HF Hub network calls.
    Falls back to normal download if local cache is missing or incomplete.
    """
    local_path = resolve_local_model_path(model_id)
    if local_path:
        print(f"  Loading from local cache: {local_path}")
        try:
            return model_cls.from_pretrained(local_path, **load_kwargs)
        except Exception as e:
            import traceback
            print(f"  Warning: Failed to load from local cache: {e}")
            traceback.print_exc()
            print(f"  Retrying with model ID (may download missing files)...")
    else:
        print(f"  Model not cached locally, downloading {model_id}...")
    return model_cls.from_pretrained(model_id, **load_kwargs)
