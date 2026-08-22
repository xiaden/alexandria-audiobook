"""HuggingFace download utilities for built-in LoRA adapters."""
import json
import logging
import os
import shutil
import time

logger = logging.getLogger("AlexandriaUI")

BUILTIN_LORA_HF_REPO = "Finrandojin/Alexandria"

REQUIRED_ADAPTER_FILES = [
    "adapter_config.json",
    "adapter_model.safetensors",
    "ref_sample.wav",
    "training_meta.json",
]
OPTIONAL_ADAPTER_FILES = ["preview_sample.wav"]

# In-memory manifest cache
_manifest_cache = None
_manifest_cache_time = 0
_MANIFEST_TTL = 3600  # 1 hour


def fetch_builtin_manifest(builtin_dir, hf_repo=BUILTIN_LORA_HF_REPO):
    """Fetch manifest.json from HF repo, with local fallback and in-memory caching."""
    global _manifest_cache, _manifest_cache_time

    now = time.time()
    if _manifest_cache is not None and (now - _manifest_cache_time) < _MANIFEST_TTL:
        return _manifest_cache

    # Try remote
    try:
        from huggingface_hub import hf_hub_download
        cached_path = hf_hub_download(repo_id=hf_repo, filename="manifest.json")
        with open(cached_path, "r", encoding="utf-8") as f:
            entries = json.load(f)
        # Save local copy for offline fallback
        os.makedirs(builtin_dir, exist_ok=True)
        local_path = os.path.join(builtin_dir, "manifest.json")
        with open(local_path, "w", encoding="utf-8") as f:
            json.dump(entries, f, indent=2, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001 — remote fetch may fail many ways; falls back to local manifest
        logger.warning(f"Failed to fetch remote LoRA manifest, using local fallback: {e}")
        local_path = os.path.join(builtin_dir, "manifest.json")
        if os.path.exists(local_path):
            with open(local_path, "r", encoding="utf-8") as f:
                entries = json.load(f)
        else:
            entries = []

    _manifest_cache = entries
    _manifest_cache_time = now
    return entries


def download_builtin_adapter(adapter_id, builtin_dir, hf_repo=BUILTIN_LORA_HF_REPO):
    """Download a built-in LoRA adapter from HF to builtin_dir/adapter_id/.

    Args:
        adapter_id: Local adapter ID (e.g. "builtin_watson")
        builtin_dir: Path to the builtin_lora directory
        hf_repo: HF repo ID

    Returns:
        Path to the adapter directory on disk.

    Raises:
        RuntimeError: If a required file fails to download.
    """
    from huggingface_hub import hf_hub_download

    # Strip builtin_ prefix to get HF subfolder name
    hf_name = adapter_id.replace("builtin_", "", 1)
    adapter_dir = os.path.join(builtin_dir, adapter_id)
    os.makedirs(adapter_dir, exist_ok=True)

    for filename in REQUIRED_ADAPTER_FILES + OPTIONAL_ADAPTER_FILES:
        local_path = os.path.join(adapter_dir, filename)
        if os.path.exists(local_path):
            continue
        try:
            cached = hf_hub_download(
                repo_id=hf_repo,
                filename=f"{hf_name}/{filename}",
            )
            shutil.copy2(cached, local_path)
            logger.info(f"Downloaded {hf_name}/{filename} -> {local_path}")
        except Exception as e:  # noqa: BLE001 — HF download raises many types; required => RuntimeError, optional => warn
            if filename in REQUIRED_ADAPTER_FILES:
                raise RuntimeError(
                    f"Failed to download {hf_name}/{filename} for {adapter_id}: {e}"
                )
            logger.warning(f"Optional file {hf_name}/{filename} not available: {e}")

    return adapter_dir


def is_adapter_downloaded(adapter_id, builtin_dir):
    """Check if all required files exist for a built-in adapter."""
    adapter_dir = os.path.join(builtin_dir, adapter_id)
    return (
        os.path.isdir(adapter_dir)
        and all(
            os.path.exists(os.path.join(adapter_dir, f))
            for f in REQUIRED_ADAPTER_FILES
        )
    )
