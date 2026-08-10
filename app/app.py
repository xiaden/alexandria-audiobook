import os
import sys
import json
import shutil
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict
from typing import List, Optional
import re
import time
import queue
import threading
import zipfile
import subprocess
import aiofiles
from utils import atomic_json_write, save_json

from hf_utils import fetch_builtin_manifest, download_builtin_adapter, is_adapter_downloaded

# Pipeline API router (Phase 1 of pipeline rewrite)
from app.pipeline.api import router as pipeline_router

# Tombstoning GC scheduler (Plan C phase 3) — hourly, off the hot request path.
# Started from the lifespan below; NEVER at module import time (importing
# app.app must not spawn threads — the tests import it directly).
from app.pipeline.adapter import (
    ConcurrentTransactionError,
    start_gc_scheduler,
    stop_gc_scheduler,
)

# TTS engine factory (replaces legacy project engine access)
from app.engine import get_tts_engine, reset_tts_engine

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AlexandriaUI")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """App lifecycle: start the hourly artifact-GC scheduler on startup.

    The sweep never runs on the hot request path: it lives in a daemon
    background thread started only when the ASGI server actually starts
    (uvicorn, or a TestClient used as a context manager).  The existing test
    suites construct ``TestClient(app)`` without entering the context
    manager, so the lifespan — and therefore the thread — never runs under
    ``pytest tests/pipeline``.  The scheduler defers its first sweep one full
    interval and honors the ``PIPELINE_GC_SCHEDULER=0`` opt-out.
    """
    start_gc_scheduler()
    yield
    stop_gc_scheduler()


app = FastAPI(title="Alexandria Audiobook", lifespan=lifespan)

# Paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)
CONFIG_PATH = os.environ.get("ALEXANDRIA_CONFIG_PATH") or os.path.join(BASE_DIR, "config.json")
UPLOADS_DIR = os.path.join(BASE_DIR, "uploads")
DESIGNED_VOICES_DIR = os.path.join(ROOT_DIR, "designed_voices")
CLONE_VOICES_DIR = os.path.join(ROOT_DIR, "clone_voices")
LORA_MODELS_DIR = os.path.join(ROOT_DIR, "lora_models")
LORA_DATASETS_DIR = os.path.join(ROOT_DIR, "lora_datasets")
BUILTIN_LORA_DIR = os.path.join(ROOT_DIR, "builtin_lora")
DATASET_BUILDER_DIR = os.path.join(ROOT_DIR, "dataset_builder")
PREPARER_SCRIPT_PATH = os.path.join(BASE_DIR, "alexandria_preparer.py")
PREPARER_OUTPUT_DIR = os.path.join(ROOT_DIR, "preparer_output")

os.makedirs(UPLOADS_DIR, exist_ok=True)
os.makedirs(DESIGNED_VOICES_DIR, exist_ok=True)
os.makedirs(CLONE_VOICES_DIR, exist_ok=True)
os.makedirs(LORA_MODELS_DIR, exist_ok=True)
os.makedirs(LORA_DATASETS_DIR, exist_ok=True)
os.makedirs(DATASET_BUILDER_DIR, exist_ok=True)
os.makedirs(PREPARER_OUTPUT_DIR, exist_ok=True)

# Mount static files with absolute path
STATIC_DIR = os.path.join(BASE_DIR, "static", "dist")
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Create voicelines directory if it doesn't exist to prevent startup error
VOICELINES_DIR = os.path.join(ROOT_DIR, "voicelines")
os.makedirs(VOICELINES_DIR, exist_ok=True)
app.mount("/voicelines", StaticFiles(directory=VOICELINES_DIR), name="voicelines")

# Designed voices directory for voice designer feature
app.mount("/designed_voices", StaticFiles(directory=DESIGNED_VOICES_DIR), name="designed_voices")

# Clone voices directory for user-uploaded reference audio
app.mount("/clone_voices", StaticFiles(directory=CLONE_VOICES_DIR), name="clone_voices")

# LoRA models directory for trained adapter test audio
app.mount("/lora_models", StaticFiles(directory=LORA_MODELS_DIR), name="lora_models")

# Built-in LoRA adapters directory
os.makedirs(BUILTIN_LORA_DIR, exist_ok=True)
app.mount("/builtin_lora", StaticFiles(directory=BUILTIN_LORA_DIR), name="builtin_lora")

# Dataset builder directory for preview audio
app.mount("/dataset_builder", StaticFiles(directory=DATASET_BUILDER_DIR), name="dataset_builder")

# CORS for development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount pipeline API router
app.include_router(pipeline_router)

# ConcurrentTransactionError -> 503 + Retry-After (CONTRACTS.md rule #4, Plan K).
# A concurrent pipeline storage write (owner-thread guard violation, or
# BEGIN IMMEDIATE timing out under contention) is transient and retryable:
# the client backs off for the advertised delay and re-dispatches its
# idempotent write. The 409 snapshot-block contract (POST /projects/load)
# keeps its own Retry-After in api_operations._RETRY_AFTER_SECONDS and is
# NOT affected by this handler — HTTPException(409) never reaches it.
_CONCURRENT_WRITE_RETRY_AFTER_SECONDS = 5


async def concurrent_transaction_error_handler(
    request: Request, exc: ConcurrentTransactionError
) -> JSONResponse:
    """Map a concurrent pipeline write to HTTP 503 + ``Retry-After``."""
    return JSONResponse(
        status_code=503,
        content={
            "detail": "Concurrent write in progress — retry after the advertised delay"
        },
        headers={"Retry-After": str(_CONCURRENT_WRITE_RETRY_AFTER_SECONDS)},
    )


app.add_exception_handler(ConcurrentTransactionError, concurrent_transaction_error_handler)

# --- System Helpers ---

def get_gpu_stats():
    """Get current GPU memory and utilization stats."""
    try:
        import torch
    except ImportError:
        return None

    if not torch.cuda.is_available():
        return None

    stats = {}
    try:
        # Memory stats (works for both NVIDIA and AMD ROCm)
        allocated = torch.cuda.memory_allocated() / 1e9  # GB
        reserved = torch.cuda.memory_reserved() / 1e9    # GB
        total = torch.cuda.get_device_properties(0).total_memory / 1e9  # GB

        stats['allocated_gb'] = allocated
        stats['reserved_gb'] = reserved
        stats['total_gb'] = total
        stats['allocated_percent'] = (allocated / total * 100) if total > 0 else 0

        # Try to get utilization via rocm-smi for AMD GPUs
        try:
            result = subprocess.run(
                ['/opt/rocm/bin/rocm-smi', '--showuse', '--json'],
                capture_output=True,
                text=True,
                timeout=2
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                for card_key, card_data in data.items():
                    if not isinstance(card_data, dict):
                        continue
                    for key in ('GPU use (%)', 'GPU Use (%)', 'GPU Activity'):
                        gpu_use_str = card_data.get(key)
                        if gpu_use_str is not None and gpu_use_str != 'N/A':
                            stats['utilization_percent'] = float(gpu_use_str)
                            break
                    break
        except Exception:
            stats['utilization_percent'] = None

    except Exception as e:
        logger.debug(f"Could not get GPU stats: {e}")
        return None

    return stats

def check_disk_space(path, required_gb):
    """Check if disk has enough space. Returns (has_space, free_gb)."""
    try:
        stat = shutil.disk_usage(path)
        free_gb = stat.free / (1024 ** 3)
        return free_gb >= required_gb, free_gb
    except Exception:
        return True, 0

@app.get("/api/system/stats")
async def get_system_stats():
    """Return GPU and Disk statistics."""
    gpu = get_gpu_stats()
    # Check root dir for disk space
    has_space, free_gb = check_disk_space(ROOT_DIR, 1.0) # 1GB threshold for generic warning
    
    return {
        "gpu": gpu,
        "disk": {
            "free_gb": round(free_gb, 2),
            "low_space": not has_space
        }
    }

# Data Models
class TaskLLMConfig(BaseModel):
    """Per-task LLM override. All fields optional — None means 'inherit global default'."""
    model_name: Optional[str] = None
    reasoning_effort: Optional[str] = None
    temperature: Optional[float] = None

class LLMTaskOverrides(BaseModel):
    """Per-task LLM configuration overrides for the 9 walk tasks.
    Each field is a TaskLLMConfig instance — all fields optional, inherit global."""
    scene_segmentation: TaskLLMConfig = TaskLLMConfig()
    character_discovery: TaskLLMConfig = TaskLLMConfig()
    script_alias_resolution: TaskLLMConfig = TaskLLMConfig()
    scene_presence: TaskLLMConfig = TaskLLMConfig()
    span_attribution: TaskLLMConfig = TaskLLMConfig()
    character_description: TaskLLMConfig = TaskLLMConfig()
    voice_audition: TaskLLMConfig = TaskLLMConfig()
    voice_assignment: TaskLLMConfig = TaskLLMConfig()
    delivery: TaskLLMConfig = TaskLLMConfig()

class LLMConfig(BaseModel):
    """LLM connection and model settings for the application.

    Holds the base URL, API key, and default model for all LLM calls,
    plus optional per-task overrides. Resolution order for each task is:
    task-specific override (``task_overrides.<task>``) → global default
    (``model_name`` / ``reasoning_effort`` / ``temperature`` here) →
    hardcoded fallback.

    Attributes:
        base_url: OpenAI-compatible API endpoint.
        api_key: API key for authentication.
        model_name: Default model identifier used when no task override is set.
        reasoning_effort: Global default reasoning depth passed to the provider
            (e.g. ``'low'``, ``'medium'``, ``'high'``). ``None`` means use the
            provider's default.
        temperature: Global default sampling temperature. Used when a task
            override does not set its own ``temperature``.
        task_overrides: Per-task model, reasoning, and temperature overrides
            keyed by task name (e.g. ``script_generation``,
            ``persona_discovery``).
    """
    base_url: str
    api_key: str
    model_name: str
    reasoning_effort: Optional[str] = None  # Global default reasoning effort
    temperature: float = 0.6  # Global default sampling temperature
    task_overrides: LLMTaskOverrides = LLMTaskOverrides()  # Per-task overrides

class TTSConfig(BaseModel):
    mode: str = "local"  # "local" or "external"
    url: str = "http://127.0.0.1:7860"  # external mode only
    device: str = "auto"  # local mode: "auto", "cuda:0", "cpu", etc.
    language: str = "English"  # TTS language
    parallel_workers: int = 2  # concurrent TTS workers
    batch_seed: Optional[int] = None  # Single seed for batch mode, None/-1 = random
    compile_codec: bool = False  # torch.compile the codec for ~3-4x batch throughput (slow first run)
    sub_batch_enabled: bool = True  # split batch by text length to reduce padding waste
    sub_batch_min_size: int = 4  # minimum chunks per sub-batch before allowing a split
    sub_batch_ratio: float = 5.0  # max longest/shortest length ratio before splitting
    sub_batch_max_items: int = 0  # hard cap on sequences per sub-batch (0 = auto from VRAM estimate)
    batch_group_by_type: bool = False  # group chunks by voice type for efficient batching
    pause_between_speakers_ms: int = 500  # silence (ms) between different speakers during merge
    pause_same_speaker_ms: int = 250  # silence (ms) when same speaker continues during merge

class AppConfig(BaseModel):
    # Validation-only schema (CONTRACTS.md rule #11): unknown top-level keys
    # (generation/prompts/...) are IGNORED here — they survive only as raw JSON
    # in config.json (DD cannot-restore #13) and the validation OUTPUT is never
    # serialized. Pydantic's default is already 'ignore'; this makes the
    # contract explicit and guards against a future model_config change.
    model_config = ConfigDict(extra="ignore")

    llm: LLMConfig
    tts: TTSConfig

class VoiceDesignPreviewRequest(BaseModel):
    description: str
    sample_text: str
    language: Optional[str] = None

class VoiceDesignSaveRequest(BaseModel):
    name: str
    description: str
    sample_text: str
    preview_file: str

class LoraTrainingRequest(BaseModel):
    name: str
    dataset_id: str
    epochs: int = 5
    lr: float = 5e-6
    batch_size: int = 1
    lora_r: int = 32
    lora_alpha: int = 128
    gradient_accumulation_steps: int = 8
    language: str = "english"

class LoraTestRequest(BaseModel):
    adapter_id: str
    text: str
    instruct: str = ""

class LoraDatasetSample(BaseModel):
    emotion: str = ""
    text: str

class LoraGenerateDatasetRequest(BaseModel):
    name: str
    description: str  # root voice description
    samples: Optional[List[LoraDatasetSample]] = None  # emotion+text pairs
    texts: Optional[List[str]] = None  # legacy: flat text list (no emotions)
    language: Optional[str] = None

class DatasetSampleGenRequest(BaseModel):
    description: str      # full voice description (root + emotion already combined by frontend)
    text: str
    dataset_name: str     # working directory name
    sample_index: int     # row number
    seed: int = -1        # -1 = random, >= 0 = manual seed

class DatasetBatchGenRequest(BaseModel):
    name: str
    description: str      # root voice description
    samples: List[LoraDatasetSample]
    indices: Optional[List[int]] = None  # which rows to generate (None = all)
    global_seed: int = -1 # -1 = random, >= 0 = same seed for all lines
    seeds: Optional[List[int]] = None  # per-line seeds (overrides global_seed)

class DatasetSaveRequest(BaseModel):
    name: str
    ref_index: int = 0    # which sample to use as ref.wav

class DatasetBuilderCreateRequest(BaseModel):
    name: str

class DatasetBuilderUpdateMetaRequest(BaseModel):
    name: str
    description: str = ""
    global_seed: str = ""

class DatasetBuilderUpdateRowsRequest(BaseModel):
    name: str
    rows: List[dict]  # [{emotion, text, seed}]

class PreparerConfig(BaseModel):
    audio_filename: str
    output_filename: str = "alexandria_dataset.zip"
    lang: str = "en"
    min_confidence: float = 0.85
    min_snr: int = 25

class BatchPreparerTask(BaseModel):
    audio_filename: str
    output_filename: str

class BatchPreparerRequest(BaseModel):
    tasks: List[BatchPreparerTask]
    lang: str = "en"
    min_confidence: float = 0.85
    min_snr: int = 25

# Global state for process tracking
process_state = {
    "script": {"running": False, "logs": []},
    "review": {"running": False, "logs": []},
    "lora_training": {"running": False, "logs": []},
    "dataset_gen": {"running": False, "logs": []},
    "dataset_builder": {"running": False, "logs": [], "cancel": False},
    "preparer": {"running": False, "logs": [], "cancel": False, "process": None, "status": "idle", "output_file": None},
    "batch_preparer": {"running": False, "logs": [], "cancel": False, "tasks": [], "current_task_idx": -1},
}

def run_process(command: List[str], task_name: str):
    """Run a subprocess and stream its output into process_state logs."""
    state = process_state[task_name]
    state["running"] = True
    state["logs"] = []

    logger.info(f"Starting task {task_name}: {' '.join(command)}")

    try:
        return_code = _stream_subprocess_to_logs(command, BASE_DIR, state)

        if state.get("cancel"):
            state["logs"].append(f"Task {task_name} cancelled.")
        elif return_code == 0:
            state["logs"].append(f"Task {task_name} completed successfully.")
        else:
            state["logs"].append(f"Task {task_name} failed with return code {return_code}.")

    except Exception as e:
        logger.error(f"Error running {task_name}: {e}")
        state["logs"].append(f"Error: {str(e)}")
    finally:
        state["process"] = None
        state["running"] = False



def _stream_subprocess_to_logs(command: List[str], cwd: str, state: dict, log_prefix: str = "", max_logs: int = 5000) -> int:
    """Run a subprocess, appending its merged stdout/stderr into state['logs'].

    Uses a reader thread + Queue so the drain loop can check state['cancel']
    between reads without any platform-specific I/O multiplexing (e.g. no
    select.select(), which does not work on Windows pipes).

    Returns the process exit code.
    """
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=cwd,
        env=os.environ.copy(),
    )

    if "process" in state:
        state["process"] = process

    log_queue: queue.Queue = queue.Queue()

    def _reader(stream, q):
        try:
            for line in stream:
                q.put(line)
        finally:
            q.put(None)

    reader = threading.Thread(target=_reader, args=(process.stdout, log_queue), daemon=True)
    reader.start()

    while True:
        try:
            line = log_queue.get(timeout=0.05)
        except queue.Empty:
            if state.get("cancel"):
                process.terminate()
            continue
        if line is None:
            break
        log_line = line.strip()
        if log_line:
            entry = f"{log_prefix}{log_line}" if log_prefix else log_line
            state["logs"].append(entry)
            if len(state["logs"]) > max_logs:
                state["logs"].pop(0)

    reader.join()
    process.wait()
    return process.returncode


# Endpoints

@app.get("/")
async def read_index():
    return FileResponse(
        os.path.join(STATIC_DIR, "index.html"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"}
    )

@app.get("/favicon.ico")
async def read_favicon():
    favicon_path = os.path.join(ROOT_DIR, "icon.png")
    if os.path.exists(favicon_path):
        return FileResponse(favicon_path, media_type="image/png")
    raise HTTPException(status_code=404, detail="Favicon not found")

@app.get("/api/config")
async def get_config():
    """Return the full application configuration as JSON.

    Reads ``config.json`` from disk (or builds a default when the file is
    missing) and materialises Pydantic defaults via
    ``AppConfig.model_validate`` so that new optional fields such as
    ``task_overrides`` and ``reasoning_effort`` are always present in the
    response — even when absent from the on-disk config.

    Unknown top-level keys (``generation``, ``prompts``, ``schema_version``,
    ...) present in the on-disk file are preserved in the response: the
    raw-JSON merge contract (rule #11) guarantees a byte-stable round-trip, so
    this endpoint must NOT drop keys the old ``model_validate -> model_dump``
    path stripped (the L4 data-loss bug).
    """
    default_config = {
        "llm": {
            "base_url": "http://localhost:11434/v1",
            "api_key": "local",
            "model_name": "richardyoung/qwen3-14b-abliterated:Q8_0"
        },
        "tts": {
            "mode": "local",
            "url": "http://127.0.0.1:7860",
            "device": "auto"
        }
    }

    if not os.path.exists(CONFIG_PATH):
        config = default_config
    else:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)

    # Materialize Pydantic defaults so new optional fields (task_overrides,
    # reasoning_effort) are present even when absent from config.json on disk.
    # This is the H1 fix — without it the frontend per-task table would receive
    # a config dict missing the task_overrides key entirely.
    validated = AppConfig.model_validate(config)
    result = validated.model_dump()

    # Preserve unknown top-level keys from the on-disk file (byte-stable
    # contract, rule #11) — they are not AppConfig fields and must round-trip
    # unchanged (DD cannot-restore #13).
    for key, value in config.items():
        if key not in result:
            result[key] = value

    return result


def _deep_merge_unknown(base: dict, overlay: dict, known: set) -> dict:
    """Deep-merge the unknown keys of ``overlay`` onto ``base`` (raw-JSON merge).

    Known keys (in ``known``, i.e. the AppConfig field names) replace the
    on-disk value verbatim — they come from the request body which was already
    validated through ``AppConfig``. Unknown keys are merged recursively:
    values from ``overlay`` win, but unknown keys already on disk that the body
    omits survive, and nested dicts merge key-wise. Returns a NEW dict;
    ``base`` is never mutated.
    """
    result = dict(base)
    for key, value in overlay.items():
        if key in known:
            result[key] = value
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge_unknown(result[key], value, known)
        else:
            result[key] = value
    return result


@app.post("/api/config")
async def save_config(request: Request, config: AppConfig):
    """Save the application configuration with a raw-JSON merge.

    The request body is parsed as raw JSON and deep-merged onto the current
    on-disk config: unknown top-level keys (``generation``, ``prompts``, ...)
    survive byte-stable instead of being wiped by ``AppConfig.model_dump()``
    (the L4 data-loss bug). Known keys are validated through ``AppConfig``
    (extra='ignore') and replace the on-disk section; the validation OUTPUT is
    never serialized. ``schema_version`` is stamped and the merged config is
    written atomically (tmp+rename) so a failure never leaves a partial file.
    """
    # Raw request body, including unknown top-level keys. ``config: AppConfig``
    # above has already parsed+validated the body (422 on malformed JSON or
    # invalid known values), so request.json() here is safe and returns the
    # exact bytes the client sent.
    raw = await request.json()

    # Merge base = current on-disk config (empty when the file is missing).
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            current = json.load(f)
    else:
        current = {}

    merged = _deep_merge_unknown(current, raw, known=set(AppConfig.model_fields))
    merged["schema_version"] = 1  # schema_version stamp (rule #11)

    save_json(CONFIG_PATH, merged)
    # Reset engine so it picks up new TTS settings on next use
    reset_tts_engine()
    return {"status": "saved"}


def _sanitize_name(name: str) -> str:
    """Make a string safe for use as a filename."""
    name = re.sub(r'[^\w\- ]', '', name).strip()
    name = re.sub(r'\s+', '_', name)
    return name.lower()


## ── Voice Designer ──────────────────────────────────────────────

DESIGNED_VOICES_MANIFEST = os.path.join(DESIGNED_VOICES_DIR, "manifest.json")

def _load_manifest(path):
    """Load a JSON manifest file, returning [] on missing or corrupt file."""
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass
    return []

def _save_manifest(path, manifest):
    """Write a JSON manifest file."""
    atomic_json_write(manifest, path)

@app.post("/api/voice_design/preview")
async def voice_design_preview(request: VoiceDesignPreviewRequest):
    """Generate a preview voice from a text description."""
    engine = get_tts_engine()
    if not engine:
        raise HTTPException(status_code=500, detail="Failed to initialize TTS engine")

    try:
        wav_path, sr = engine.generate_voice_design(
            description=request.description,
            sample_text=request.sample_text,
            language=request.language,
        )
        # Return relative URL for the static mount
        filename = os.path.basename(wav_path)
        return {"status": "ok", "audio_url": f"/designed_voices/previews/{filename}"}
    except Exception as e:
        logger.error(f"Voice design preview failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/voice_design/save")
async def voice_design_save(request: VoiceDesignSaveRequest):
    """Save a preview voice as a permanent designed voice."""
    previews_dir = os.path.join(DESIGNED_VOICES_DIR, "previews")
    preview_path = os.path.join(previews_dir, request.preview_file)

    if not os.path.exists(preview_path):
        raise HTTPException(status_code=404, detail="Preview file not found")

    safe_name = _sanitize_name(request.name)
    if not safe_name:
        raise HTTPException(status_code=400, detail="Invalid voice name")

    # Generate unique ID
    voice_id = f"{safe_name}_{int(time.time())}"
    dest_filename = f"{voice_id}.wav"
    dest_path = os.path.join(DESIGNED_VOICES_DIR, dest_filename)

    shutil.copy2(preview_path, dest_path)

    # Update manifest
    manifest = _load_manifest(DESIGNED_VOICES_MANIFEST)
    manifest.append({
        "id": voice_id,
        "name": request.name,
        "description": request.description,
        "sample_text": request.sample_text,
        "filename": dest_filename,
    })
    _save_manifest(DESIGNED_VOICES_MANIFEST, manifest)

    logger.info(f"Designed voice saved: '{request.name}' as {dest_filename}")
    return {"status": "saved", "voice_id": voice_id}

@app.get("/api/voice_design/list")
async def voice_design_list():
    """List all saved designed voices."""
    return _load_manifest(DESIGNED_VOICES_MANIFEST)

@app.delete("/api/voice_design/{voice_id}")
async def voice_design_delete(voice_id: str):
    """Delete a saved designed voice."""
    manifest = _load_manifest(DESIGNED_VOICES_MANIFEST)
    entry = next((v for v in manifest if v["id"] == voice_id), None)
    if not entry:
        raise HTTPException(status_code=404, detail="Voice not found")

    # Delete WAV file
    wav_path = os.path.join(DESIGNED_VOICES_DIR, entry["filename"])
    if os.path.exists(wav_path):
        os.remove(wav_path)

    # Remove from manifest
    manifest = [v for v in manifest if v["id"] != voice_id]
    _save_manifest(DESIGNED_VOICES_MANIFEST, manifest)

    logger.info(f"Designed voice deleted: {voice_id}")
    return {"status": "deleted", "voice_id": voice_id}

## ── Clone Voice Uploads ───────────────────────────────────────

CLONE_VOICES_MANIFEST = os.path.join(CLONE_VOICES_DIR, "manifest.json")
ALLOWED_AUDIO_EXTS = {".wav", ".mp3", ".flac", ".ogg"}

@app.get("/api/clone_voices/list")
async def clone_voices_list():
    """List all uploaded clone voices."""
    return _load_manifest(CLONE_VOICES_MANIFEST)

@app.post("/api/clone_voices/upload")
async def clone_voices_upload(file: UploadFile = File(...)):
    """Upload an audio file for voice cloning."""
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_AUDIO_EXTS:
        raise HTTPException(status_code=400, detail=f"Unsupported format. Use: {', '.join(ALLOWED_AUDIO_EXTS)}")

    base_name = os.path.splitext(file.filename)[0]
    safe_name = _sanitize_name(base_name)
    if not safe_name:
        raise HTTPException(status_code=400, detail="Invalid filename")

    voice_id = f"{safe_name}_{int(time.time())}"
    dest_filename = f"{voice_id}{ext}"
    dest_path = os.path.join(CLONE_VOICES_DIR, dest_filename)

    async with aiofiles.open(dest_path, "wb") as out_file:
        content = await file.read()
        await out_file.write(content)

    manifest = _load_manifest(CLONE_VOICES_MANIFEST)
    manifest.append({
        "id": voice_id,
        "name": base_name,
        "filename": dest_filename,
    })
    _save_manifest(CLONE_VOICES_MANIFEST, manifest)

    logger.info(f"Clone voice uploaded: '{base_name}' as {dest_filename}")
    return {"status": "uploaded", "voice_id": voice_id, "filename": dest_filename}

@app.delete("/api/clone_voices/{voice_id}")
async def clone_voices_delete(voice_id: str):
    """Delete an uploaded clone voice."""
    manifest = _load_manifest(CLONE_VOICES_MANIFEST)
    entry = next((v for v in manifest if v["id"] == voice_id), None)
    if not entry:
        raise HTTPException(status_code=404, detail="Clone voice not found")

    wav_path = os.path.join(CLONE_VOICES_DIR, entry["filename"])
    if os.path.exists(wav_path):
        os.remove(wav_path)

    manifest = [v for v in manifest if v["id"] != voice_id]
    _save_manifest(CLONE_VOICES_MANIFEST, manifest)

    logger.info(f"Clone voice deleted: {voice_id}")
    return {"status": "deleted", "voice_id": voice_id}

## ── LoRA Training ──────────────────────────────────────────────

LORA_MODELS_MANIFEST = os.path.join(LORA_MODELS_DIR, "manifest.json")

def _load_builtin_lora_manifest():
    """Load built-in LoRA manifest from HF (with local fallback). Returns ALL entries with download status."""
    entries = fetch_builtin_manifest(BUILTIN_LORA_DIR)
    result = []
    for entry in entries:
        entry = dict(entry)  # avoid mutating cached list
        local_id = entry["id"] if entry["id"].startswith("builtin_") else f"builtin_{entry['id']}"
        downloaded = is_adapter_downloaded(local_id, BUILTIN_LORA_DIR)
        entry["id"] = local_id
        entry["builtin"] = True
        entry["downloaded"] = downloaded
        entry["adapter_path"] = f"builtin_lora/{local_id}" if downloaded else None
        result.append(entry)
    return result

@app.post("/api/lora/upload_dataset")
async def lora_upload_dataset(file: UploadFile = File(...)):
    """Upload a ZIP containing WAV files and metadata.jsonl."""
    if not file.filename.endswith(".zip"):
        raise HTTPException(status_code=400, detail="File must be a .zip archive")

    # Derive dataset name from ZIP filename
    dataset_name = re.sub(r'[^\w\- ]', '', os.path.splitext(file.filename)[0]).strip()
    dataset_name = re.sub(r'\s+', '_', dataset_name).lower()
    if not dataset_name:
        raise HTTPException(status_code=400, detail="Invalid dataset name from filename")

    dataset_dir = os.path.join(LORA_DATASETS_DIR, dataset_name)
    if os.path.exists(dataset_dir):
        raise HTTPException(status_code=400, detail=f"Dataset '{dataset_name}' already exists")

    # Save ZIP temporarily, then extract
    tmp_path = os.path.join(LORA_DATASETS_DIR, f"_tmp_{dataset_name}.zip")
    try:
        async with aiofiles.open(tmp_path, "wb") as out_file:
            content = await file.read()
            await out_file.write(content)

        os.makedirs(dataset_dir, exist_ok=True)
        with zipfile.ZipFile(tmp_path, "r") as zf:
            zf.extractall(dataset_dir)

        # Check for metadata.jsonl (may be inside a subdirectory)
        metadata_path = os.path.join(dataset_dir, "metadata.jsonl")
        if not os.path.exists(metadata_path):
            # Check one level deep
            for entry in os.listdir(dataset_dir):
                candidate = os.path.join(dataset_dir, entry, "metadata.jsonl")
                if os.path.isdir(os.path.join(dataset_dir, entry)) and os.path.exists(candidate):
                    # Move contents up
                    nested = os.path.join(dataset_dir, entry)
                    for item in os.listdir(nested):
                        shutil.move(os.path.join(nested, item), os.path.join(dataset_dir, item))
                    os.rmdir(nested)
                    metadata_path = os.path.join(dataset_dir, "metadata.jsonl")
                    break

        if not os.path.exists(metadata_path):
            shutil.rmtree(dataset_dir)
            raise HTTPException(status_code=400, detail="ZIP must contain metadata.jsonl")

        # Count samples
        sample_count = 0
        with open(metadata_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    sample_count += 1

        logger.info(f"LoRA dataset uploaded: '{dataset_name}' ({sample_count} samples)")
        return {"status": "uploaded", "dataset_id": dataset_name, "sample_count": sample_count}

    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

@app.post("/api/lora/generate_dataset")
async def lora_generate_dataset(request: LoraGenerateDatasetRequest, background_tasks: BackgroundTasks):
    """Generate a LoRA training dataset using Voice Designer.

    Generates multiple audio samples with the same voice description,
    saving them as a ready-to-train dataset.
    """
    if process_state["dataset_gen"]["running"]:
        raise HTTPException(status_code=400, detail="Dataset generation already running")

    # Build unified sample list from either format
    sample_list = []
    if request.samples:
        for s in request.samples:
            if s.text.strip():
                sample_list.append({"emotion": s.emotion.strip(), "text": s.text.strip()})
    elif request.texts:
        for t in request.texts:
            if t.strip():
                sample_list.append({"emotion": "", "text": t.strip()})

    if not sample_list:
        raise HTTPException(status_code=400, detail="Provide at least one sample text")

    safe_name = _sanitize_name(request.name)
    if not safe_name:
        raise HTTPException(status_code=400, detail="Invalid dataset name")

    dataset_dir = os.path.join(LORA_DATASETS_DIR, safe_name)
    if os.path.exists(dataset_dir):
        raise HTTPException(status_code=400, detail=f"Dataset '{safe_name}' already exists")

    total = len(sample_list)
    root_description = request.description.strip()

    def task():
        process_state["dataset_gen"]["running"] = True
        process_state["dataset_gen"]["logs"] = [
            f"Generating {total} samples with VoiceDesign..."
        ]
        try:
            engine = get_tts_engine()
            if not engine:
                process_state["dataset_gen"]["logs"].append("Error: TTS engine not initialized")
                return

            os.makedirs(dataset_dir, exist_ok=True)
            metadata_lines = []
            completed = 0

            for i, sample in enumerate(sample_list):
                text = sample["text"]
                emotion = sample["emotion"]
                # Build full description: root + emotion if provided
                description = f"{root_description}, {emotion}" if emotion else root_description

                process_state["dataset_gen"]["logs"].append(
                    f"[{i+1}/{total}] {('[' + emotion + '] ' if emotion else '')}\"{ text[:60]}{'...' if len(text) > 60 else ''}\""
                )
                try:
                    wav_path, sr = engine.generate_voice_design(
                        description=description,
                        sample_text=text,
                        language=request.language,
                    )
                    # Copy to dataset dir with sequential name
                    dest_filename = f"sample_{i:03d}.wav"
                    dest_path = os.path.join(dataset_dir, dest_filename)
                    shutil.copy2(wav_path, dest_path)

                    # Save first successful sample as ref.wav for consistent speaker embedding
                    if completed == 0:
                        shutil.copy2(wav_path, os.path.join(dataset_dir, "ref.wav"))

                    metadata_lines.append(json.dumps({
                        "audio_filepath": dest_filename,
                        "text": text,
                        "ref_audio": "ref.wav",
                    }, ensure_ascii=False))
                    completed += 1
                    process_state["dataset_gen"]["logs"].append(
                        f"  Saved {dest_filename}"
                    )
                except Exception as e:
                    process_state["dataset_gen"]["logs"].append(
                        f"  Failed: {e}"
                    )

            # Write metadata.jsonl
            metadata_path = os.path.join(dataset_dir, "metadata.jsonl")
            with open(metadata_path, "w", encoding="utf-8") as f:
                f.write("\n".join(metadata_lines) + "\n")

            process_state["dataset_gen"]["logs"].append(
                f"Dataset '{safe_name}' complete: {completed}/{total} samples generated."
            )
            logger.info(f"LoRA dataset generated: '{safe_name}' ({completed} samples)")

        except Exception as e:
            process_state["dataset_gen"]["logs"].append(f"Error: {e}")
            logger.error(f"Dataset generation error: {e}")
            # Clean up partial dataset on failure
            if os.path.exists(dataset_dir):
                shutil.rmtree(dataset_dir)
        finally:
            process_state["dataset_gen"]["running"] = False

    background_tasks.add_task(task)
    return {"status": "started", "dataset_id": safe_name, "total": total}

@app.get("/api/lora/datasets")
async def lora_list_datasets():
    """List uploaded LoRA training datasets."""
    datasets = []
    if not os.path.exists(LORA_DATASETS_DIR):
        return datasets

    for name in sorted(os.listdir(LORA_DATASETS_DIR)):
        dataset_dir = os.path.join(LORA_DATASETS_DIR, name)
        if not os.path.isdir(dataset_dir):
            continue
        metadata_path = os.path.join(dataset_dir, "metadata.jsonl")
        sample_count = 0
        if os.path.exists(metadata_path):
            with open(metadata_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        sample_count += 1
        datasets.append({"dataset_id": name, "sample_count": sample_count})
    return datasets

@app.delete("/api/lora/datasets/{dataset_id}")
async def lora_delete_dataset(dataset_id: str):
    """Delete an uploaded dataset."""
    dataset_dir = os.path.join(LORA_DATASETS_DIR, dataset_id)
    if not os.path.isdir(dataset_dir):
        raise HTTPException(status_code=404, detail="Dataset not found")

    shutil.rmtree(dataset_dir)
    logger.info(f"LoRA dataset deleted: {dataset_id}")
    return {"status": "deleted", "dataset_id": dataset_id}

@app.post("/api/lora/train")
async def lora_start_training(request: LoraTrainingRequest, background_tasks: BackgroundTasks):
    """Start LoRA training as a subprocess."""
    if process_state["lora_training"]["running"]:
        raise HTTPException(status_code=400, detail="LoRA training already running")

    # Validate dataset exists
    dataset_dir = os.path.join(LORA_DATASETS_DIR, request.dataset_id)
    if not os.path.isdir(dataset_dir):
        raise HTTPException(status_code=400, detail=f"Dataset '{request.dataset_id}' not found")

    # Build output directory
    safe_name = _sanitize_name(request.name)
    if not safe_name:
        raise HTTPException(status_code=400, detail="Invalid adapter name")

    adapter_id = f"{safe_name}_{int(time.time())}"
    output_dir = os.path.join(LORA_MODELS_DIR, adapter_id)

    # Unload TTS engine to free GPU
    reset_tts_engine()
    from utils import clear_gpu_cache
    clear_gpu_cache()

    # Build subprocess command
    command = [
        sys.executable, "-u", "train_lora.py",
        "--data_dir", dataset_dir,
        "--output_dir", output_dir,
        "--epochs", str(request.epochs),
        "--lr", str(request.lr),
        "--batch_size", str(request.batch_size),
        "--lora_r", str(request.lora_r),
        "--lora_alpha", str(request.lora_alpha),
        "--gradient_accumulation_steps", str(request.gradient_accumulation_steps),
        "--language", request.language,
    ]

    def on_training_complete():
        """After training subprocess finishes, update manifest if adapter was saved."""
        run_process(command, "lora_training")

        # Check if training produced an adapter
        if os.path.isdir(output_dir) and os.path.exists(os.path.join(output_dir, "training_meta.json")):
            try:
                with open(os.path.join(output_dir, "training_meta.json"), "r") as f:
                    meta = json.load(f)

                manifest = _load_manifest(LORA_MODELS_MANIFEST)
                manifest.append({
                    "id": adapter_id,
                    "name": request.name,
                    "dataset_id": request.dataset_id,
                    "epochs": meta.get("epochs", request.epochs),
                    "final_loss": meta.get("final_loss"),
                    "sample_count": meta.get("num_samples"),
                    "lora_r": meta.get("lora_r"),
                    "lr": meta.get("lr"),
                    "created": time.time(),
                })
                _save_manifest(LORA_MODELS_MANIFEST, manifest)
                logger.info(f"LoRA adapter registered: {adapter_id}")
            except Exception as e:
                logger.error(f"Failed to update LoRA manifest: {e}")

    background_tasks.add_task(on_training_complete)
    return {"status": "started", "adapter_id": adapter_id}

@app.get("/api/lora/status")
async def lora_status():
    """Get LoRA training process status."""
    return {
        "logs": process_state["lora_training"]["logs"],
        "running": process_state["lora_training"]["running"],
    }

@app.get("/api/lora/models")
async def lora_list_models():
    """List all LoRA adapters (built-in + user-trained)."""
    models = _load_builtin_lora_manifest() + _load_manifest(LORA_MODELS_MANIFEST)
    for m in models:
        is_builtin = m.get("builtin", False)
        is_downloaded = m.get("downloaded", True)  # user-trained are always downloaded

        if not is_downloaded:
            m["preview_audio_url"] = None
            continue

        if is_builtin:
            adapter_dir = os.path.join(BUILTIN_LORA_DIR, m["id"])
            url_prefix = f"/builtin_lora/{m['id']}"
        else:
            adapter_dir = os.path.join(LORA_MODELS_DIR, m["id"])
            url_prefix = f"/lora_models/{m['id']}"
        preview_path = os.path.join(adapter_dir, "preview_sample.wav")
        m["preview_audio_url"] = f"{url_prefix}/preview_sample.wav" if os.path.exists(preview_path) else None
    return models

@app.delete("/api/lora/models/{adapter_id}")
async def lora_delete_model(adapter_id: str):
    """Delete a trained LoRA adapter. Built-in adapters cannot be deleted."""
    builtin = _load_builtin_lora_manifest()
    if any(m["id"] == adapter_id for m in builtin):
        raise HTTPException(status_code=403, detail="Built-in adapters cannot be deleted")
    manifest = _load_manifest(LORA_MODELS_MANIFEST)
    entry = next((m for m in manifest if m["id"] == adapter_id), None)
    if not entry:
        raise HTTPException(status_code=404, detail="Adapter not found")

    # Delete adapter directory
    adapter_dir = os.path.join(LORA_MODELS_DIR, adapter_id)
    if os.path.isdir(adapter_dir):
        shutil.rmtree(adapter_dir)

    # Remove from manifest
    manifest = [m for m in manifest if m["id"] != adapter_id]
    _save_manifest(LORA_MODELS_MANIFEST, manifest)

    logger.info(f"LoRA adapter deleted: {adapter_id}")
    return {"status": "deleted", "adapter_id": adapter_id}

@app.post("/api/lora/download/{adapter_id}")
async def lora_download_builtin(adapter_id: str):
    """Download a built-in LoRA adapter from HuggingFace."""
    manifest = fetch_builtin_manifest(BUILTIN_LORA_DIR)
    hf_name = adapter_id.replace("builtin_", "", 1)
    entry = next((e for e in manifest if e["id"] == hf_name or e["id"] == adapter_id), None)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Unknown built-in adapter: {adapter_id}")

    if is_adapter_downloaded(adapter_id, BUILTIN_LORA_DIR):
        return {"status": "already_downloaded", "adapter_id": adapter_id}

    try:
        download_builtin_adapter(adapter_id, BUILTIN_LORA_DIR)
        logger.info(f"Built-in adapter downloaded: {adapter_id}")
        return {"status": "downloaded", "adapter_id": adapter_id}
    except Exception as e:
        logger.error(f"Download failed for {adapter_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/lora/test")
async def lora_test_model(request: LoraTestRequest):
    """Generate test audio using a LoRA adapter (built-in or user-trained)."""
    # Check both manifests
    builtin = _load_builtin_lora_manifest()
    user_trained = _load_manifest(LORA_MODELS_MANIFEST)
    all_adapters = builtin + user_trained
    entry = next((m for m in all_adapters if m["id"] == request.adapter_id), None)
    if not entry:
        raise HTTPException(status_code=404, detail="Adapter not found")

    is_builtin = entry.get("builtin", False)
    if is_builtin:
        adapter_dir = os.path.join(BUILTIN_LORA_DIR, request.adapter_id)
        audio_url_prefix = f"/builtin_lora/{request.adapter_id}"
    else:
        adapter_dir = os.path.join(LORA_MODELS_DIR, request.adapter_id)
        audio_url_prefix = f"/lora_models/{request.adapter_id}"

    if not os.path.isdir(adapter_dir) and is_builtin:
        try:
            download_builtin_adapter(request.adapter_id, BUILTIN_LORA_DIR)
            adapter_dir = os.path.join(BUILTIN_LORA_DIR, request.adapter_id)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Auto-download failed: {e}")
    elif not os.path.isdir(adapter_dir):
        raise HTTPException(status_code=404, detail="Adapter files not found")

    engine = get_tts_engine()
    if not engine:
        raise HTTPException(status_code=500, detail="Failed to initialize TTS engine")

    try:
        output_filename = f"test_{request.adapter_id}_{int(time.time())}.wav"
        output_path = os.path.join(adapter_dir, output_filename)

        voice_data = {
            "type": "lora",
            "adapter_id": request.adapter_id,
            "adapter_path": adapter_dir,
        }
        voice_config = {"_lora_test_": voice_data}
        engine.generate_voice(
            text=request.text,
            instruct_text=request.instruct or "",
            speaker="_lora_test_",
            voice_config=voice_config,
            output_path=output_path,
        )

        return {
            "status": "ok",
            "audio_url": f"{audio_url_prefix}/{output_filename}",
        }
    except Exception as e:
        logger.error(f"LoRA test generation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

LORA_PREVIEW_TEXT = "The ancient library stood at the crossroads of two forgotten paths, its weathered stone walls covered in ivy that had been growing for centuries."

@app.post("/api/lora/preview/{adapter_id}")
async def lora_preview(adapter_id: str):
    """Generate or return cached preview audio for a LoRA adapter."""
    builtin = _load_builtin_lora_manifest()
    user_trained = _load_manifest(LORA_MODELS_MANIFEST)
    all_adapters = builtin + user_trained
    entry = next((m for m in all_adapters if m["id"] == adapter_id), None)
    if not entry:
        raise HTTPException(status_code=404, detail="Adapter not found")

    is_builtin = entry.get("builtin", False)
    if is_builtin:
        adapter_dir = os.path.join(BUILTIN_LORA_DIR, adapter_id)
        url_prefix = f"/builtin_lora/{adapter_id}"
    else:
        adapter_dir = os.path.join(LORA_MODELS_DIR, adapter_id)
        url_prefix = f"/lora_models/{adapter_id}"

    if not os.path.isdir(adapter_dir) and is_builtin:
        try:
            download_builtin_adapter(adapter_id, BUILTIN_LORA_DIR)
            adapter_dir = os.path.join(BUILTIN_LORA_DIR, adapter_id)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Auto-download failed: {e}")
    elif not os.path.isdir(adapter_dir):
        raise HTTPException(status_code=404, detail="Adapter files not found")

    preview_path = os.path.join(adapter_dir, "preview_sample.wav")

    # Return cached if exists
    if os.path.exists(preview_path):
        return {"status": "cached", "audio_url": f"{url_prefix}/preview_sample.wav"}

    # Generate preview
    engine = get_tts_engine()
    if not engine:
        raise HTTPException(status_code=500, detail="Failed to initialize TTS engine")

    try:
        voice_data = {
            "type": "lora",
            "adapter_id": adapter_id,
            "adapter_path": adapter_dir,
        }
        voice_config = {"_lora_preview_": voice_data}
        engine.generate_voice(
            text=LORA_PREVIEW_TEXT,
            instruct_text="",
            speaker="_lora_preview_",
            voice_config=voice_config,
            output_path=preview_path,
        )
        return {"status": "generated", "audio_url": f"{url_prefix}/preview_sample.wav"}
    except Exception as e:
        logger.error(f"LoRA preview generation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

## ── Dataset Builder ──────────────────────────────────────────

def _load_builder_state(name):
    """Load project state from dataset builder working directory."""
    state_path = os.path.join(DATASET_BUILDER_DIR, name, "state.json")
    if os.path.exists(state_path):
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                state = json.load(f)
            # Ensure new fields exist for backward compat
            state.setdefault("description", "")
            state.setdefault("global_seed", "")
            state.setdefault("samples", [])
            return state
        except Exception:
            pass
    return {"description": "", "global_seed": "", "samples": []}

def _save_builder_state(name, state):
    """Save per-sample state to dataset builder working directory."""
    work_dir = os.path.join(DATASET_BUILDER_DIR, name)
    os.makedirs(work_dir, exist_ok=True)
    with open(os.path.join(work_dir, "state.json"), "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

@app.get("/api/dataset_builder/list")
async def dataset_builder_list():
    """List existing dataset builder projects."""
    projects = []
    if os.path.isdir(DATASET_BUILDER_DIR):
        for name in sorted(os.listdir(DATASET_BUILDER_DIR)):
            state_path = os.path.join(DATASET_BUILDER_DIR, name, "state.json")
            if os.path.isfile(state_path):
                state = _load_builder_state(name)
                samples = state.get("samples", [])
                projects.append({
                    "name": name,
                    "description": state.get("description", ""),
                    "sample_count": len(samples),
                    "done_count": sum(1 for s in samples if s.get("status") == "done"),
                })
    return projects

@app.post("/api/dataset_builder/create")
async def dataset_builder_create(request: DatasetBuilderCreateRequest):
    """Create a new dataset builder project."""
    safe_name = _sanitize_name(request.name)
    if not safe_name:
        raise HTTPException(status_code=400, detail="Invalid dataset name")
    work_dir = os.path.join(DATASET_BUILDER_DIR, safe_name)
    if os.path.exists(work_dir):
        raise HTTPException(status_code=400, detail=f"Project '{safe_name}' already exists")
    _save_builder_state(safe_name, {"description": "", "global_seed": "", "samples": []})
    return {"name": safe_name}

@app.post("/api/dataset_builder/update_meta")
async def dataset_builder_update_meta(request: DatasetBuilderUpdateMetaRequest):
    """Update project description and global seed without touching samples."""
    safe_name = _sanitize_name(request.name)
    work_dir = os.path.join(DATASET_BUILDER_DIR, safe_name)
    if not os.path.exists(work_dir):
        raise HTTPException(status_code=404, detail="Project not found")
    state = _load_builder_state(safe_name)
    state["description"] = request.description
    state["global_seed"] = request.global_seed
    _save_builder_state(safe_name, state)
    return {"status": "ok"}

@app.post("/api/dataset_builder/update_rows")
async def dataset_builder_update_rows(request: DatasetBuilderUpdateRowsRequest):
    """Update row definitions, preserving existing generation status/audio."""
    safe_name = _sanitize_name(request.name)
    work_dir = os.path.join(DATASET_BUILDER_DIR, safe_name)
    if not os.path.exists(work_dir):
        raise HTTPException(status_code=404, detail="Project not found")
    state = _load_builder_state(safe_name)
    existing = state.get("samples", [])
    # Merge: keep status/audio_url from existing samples where text unchanged
    new_samples = []
    for i, row in enumerate(request.rows):
        sample = {
            "emotion": row.get("emotion", ""),
            "text": row.get("text", "").strip(),
            "seed": row.get("seed", ""),
            "status": "pending",
            "audio_url": None,
        }
        if i < len(existing):
            old = existing[i]
            # Preserve generation state if text unchanged (trimmed comparison)
            if old.get("text", "").strip() == sample["text"]:
                sample["status"] = old.get("status", "pending")
                sample["audio_url"] = old.get("audio_url")
        new_samples.append(sample)
    state["samples"] = new_samples
    _save_builder_state(safe_name, state)
    return {"status": "ok", "sample_count": len(new_samples)}

@app.post("/api/dataset_builder/generate_sample")
async def dataset_builder_generate_sample(request: DatasetSampleGenRequest):
    """Generate a single dataset sample using VoiceDesign."""
    engine = get_tts_engine()
    if not engine:
        raise HTTPException(status_code=500, detail="Failed to initialize TTS engine")

    work_dir = os.path.join(DATASET_BUILDER_DIR, request.dataset_name)
    os.makedirs(work_dir, exist_ok=True)

    try:
        wav_path, sr = engine.generate_voice_design(
            description=request.description,
            sample_text=request.text,
            seed=request.seed,
        )

        dest_filename = f"sample_{request.sample_index:03d}.wav"
        dest_path = os.path.join(work_dir, dest_filename)
        shutil.copy2(wav_path, dest_path)

        # Update state (cache-bust URL so browser loads fresh audio on regen)
        cache_bust = int(time.time())
        audio_url = f"/dataset_builder/{request.dataset_name}/{dest_filename}?t={cache_bust}"
        state = _load_builder_state(request.dataset_name)
        samples = state.get("samples", [])
        # Ensure list is large enough
        while len(samples) <= request.sample_index:
            samples.append({"status": "pending"})
        existing_sample = samples[request.sample_index] if request.sample_index < len(samples) else {}
        samples[request.sample_index] = {
            **existing_sample,
            "status": "done",
            "audio_url": audio_url,
            "text": request.text.strip(),
            "description": request.description,
        }
        state["samples"] = samples
        _save_builder_state(request.dataset_name, state)

        return {
            "status": "done",
            "sample_index": request.sample_index,
            "audio_url": audio_url,
        }
    except Exception as e:
        logger.error(f"Dataset builder sample generation failed: {e}")
        # Mark as error in state
        state = _load_builder_state(request.dataset_name)
        samples = state.get("samples", [])
        while len(samples) <= request.sample_index:
            samples.append({"status": "pending"})
        samples[request.sample_index] = {"status": "error", "error": str(e)}
        state["samples"] = samples
        _save_builder_state(request.dataset_name, state)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/dataset_builder/generate_batch")
async def dataset_builder_generate_batch(request: DatasetBatchGenRequest):
    """Batch generate dataset samples as a background task."""
    if process_state["dataset_builder"]["running"]:
        raise HTTPException(status_code=400, detail="Dataset generation already running")

    if not request.samples or len(request.samples) == 0:
        raise HTTPException(status_code=400, detail="No samples provided")

    safe_name = _sanitize_name(request.name)
    if not safe_name:
        raise HTTPException(status_code=400, detail="Invalid dataset name")

    work_dir = os.path.join(DATASET_BUILDER_DIR, safe_name)
    os.makedirs(work_dir, exist_ok=True)
    root_desc = request.description.strip()

    # Determine which indices to generate
    if request.indices is not None:
        to_generate = request.indices
    else:
        to_generate = list(range(len(request.samples)))

    total = len(to_generate)

    # Snapshot request data for the thread (request object may not survive)
    samples_snapshot = [(s.emotion.strip(), s.text.strip()) for s in request.samples]
    global_seed = request.global_seed
    per_seeds = request.seeds

    def task():
        process_state["dataset_builder"]["running"] = True
        process_state["dataset_builder"]["logs"] = []
        process_state["dataset_builder"]["cancel"] = False

        engine = get_tts_engine()
        if not engine:
            process_state["dataset_builder"]["logs"].append("[ERROR] Failed to initialize TTS engine")
            process_state["dataset_builder"]["running"] = False
            return

        state = _load_builder_state(safe_name)
        samples_state = state.get("samples", [])
        # Ensure list is large enough for all samples
        while len(samples_state) < len(samples_snapshot):
            samples_state.append({"status": "pending"})

        completed = 0
        for i, idx in enumerate(to_generate):
            if process_state["dataset_builder"]["cancel"]:
                process_state["dataset_builder"]["logs"].append(f"[CANCEL] Stopped at {completed}/{total}")
                break

            emotion, text = samples_snapshot[idx]
            description = f"{root_desc}, {emotion}" if emotion else root_desc

            # Mark as generating (preserve existing fields like emotion, seed)
            existing_s = samples_state[idx] if idx < len(samples_state) else {}
            samples_state[idx] = {**existing_s, "status": "generating", "text": text, "emotion": emotion, "description": description}
            state["samples"] = samples_state
            _save_builder_state(safe_name, state)

            process_state["dataset_builder"]["logs"].append(
                f"[{i+1}/{total}] {('[' + emotion + '] ' if emotion else '')}\"{text[:60]}{'...' if len(text) > 60 else ''}\""
            )

            try:
                # Resolve seed: per-line > global > random
                seed = -1
                if per_seeds and idx < len(per_seeds) and per_seeds[idx] >= 0:
                    seed = per_seeds[idx]
                elif global_seed >= 0:
                    seed = global_seed

                wav_path, sr = engine.generate_voice_design(
                    description=description,
                    sample_text=text,
                    seed=seed,
                )
                dest_filename = f"sample_{idx:03d}.wav"
                dest_path = os.path.join(work_dir, dest_filename)
                shutil.copy2(wav_path, dest_path)

                samples_state[idx] = {
                    **samples_state[idx],
                    "status": "done",
                    "audio_url": f"/dataset_builder/{safe_name}/{dest_filename}?t={int(time.time())}",
                    "text": text,
                    "emotion": emotion,
                    "description": description,
                }
                completed += 1
            except Exception as e:
                logger.error(f"Dataset builder sample {idx} failed: {e}")
                process_state["dataset_builder"]["logs"].append(f"  Error: {e}")
                samples_state[idx] = {**samples_state[idx], "status": "error", "error": str(e), "text": text, "emotion": emotion}

            state["samples"] = samples_state
            _save_builder_state(safe_name, state)

        process_state["dataset_builder"]["logs"].append(
            f"[DONE] Generated {completed}/{total} samples"
        )
        process_state["dataset_builder"]["running"] = False

    threading.Thread(target=task, daemon=True).start()
    return {"status": "started", "dataset_name": safe_name, "total": total}

@app.post("/api/dataset_builder/cancel")
async def dataset_builder_cancel():
    """Cancel ongoing batch dataset generation."""
    if process_state["dataset_builder"]["running"]:
        process_state["dataset_builder"]["cancel"] = True
        return {"status": "cancelling"}
    return {"status": "not_running"}

@app.get("/api/dataset_builder/status/{name}")
async def dataset_builder_status(name: str):
    """Get per-sample generation status for a dataset builder project."""
    state = _load_builder_state(name)
    return {
        "description": state.get("description", ""),
        "global_seed": state.get("global_seed", ""),
        "samples": state.get("samples", []),
        "running": process_state["dataset_builder"]["running"],
        "logs": process_state["dataset_builder"]["logs"],
    }

@app.post("/api/dataset_builder/save")
async def dataset_builder_save(request: DatasetSaveRequest):
    """Finalize dataset builder project as a training dataset."""
    safe_name = _sanitize_name(request.name)
    if not safe_name:
        raise HTTPException(status_code=400, detail="Invalid dataset name")

    work_dir = os.path.join(DATASET_BUILDER_DIR, safe_name)
    if not os.path.exists(work_dir):
        raise HTTPException(status_code=404, detail="Dataset builder project not found")

    state = _load_builder_state(safe_name)
    samples = state.get("samples", [])

    # Collect completed samples
    done_samples = [(i, s) for i, s in enumerate(samples) if s.get("status") == "done"]
    if not done_samples:
        raise HTTPException(status_code=400, detail="No completed samples to save")

    # Check ref_index is valid
    ref_idx = request.ref_index
    ref_sample = next((s for i, s in done_samples if i == ref_idx), None)
    if ref_sample is None:
        # Fall back to first completed sample
        ref_idx = done_samples[0][0]
        ref_sample = done_samples[0][1]

    # Create training dataset directory
    dataset_dir = os.path.join(LORA_DATASETS_DIR, safe_name)
    if os.path.exists(dataset_dir):
        raise HTTPException(status_code=400, detail=f"Dataset '{safe_name}' already exists in training datasets")

    os.makedirs(dataset_dir, exist_ok=True)

    try:
        metadata_lines = []
        for i, sample in done_samples:
            src_filename = f"sample_{i:03d}.wav"
            src_path = os.path.join(work_dir, src_filename)
            if not os.path.exists(src_path):
                continue

            dest_filename = f"sample_{i:03d}.wav"
            shutil.copy2(src_path, os.path.join(dataset_dir, dest_filename))

            metadata_lines.append(json.dumps({
                "audio_filepath": dest_filename,
                "text": sample.get("text", ""),
                "ref_audio": "ref.wav",
            }, ensure_ascii=False))

        # Copy ref sample and save its text for correct clone prompt alignment
        ref_src = os.path.join(work_dir, f"sample_{ref_idx:03d}.wav")
        if os.path.exists(ref_src):
            shutil.copy2(ref_src, os.path.join(dataset_dir, "ref.wav"))
        ref_text = ref_sample.get("text", "")
        with open(os.path.join(dataset_dir, "ref_text.txt"), "w", encoding="utf-8") as f:
            f.write(ref_text)

        # Write metadata
        with open(os.path.join(dataset_dir, "metadata.jsonl"), "w", encoding="utf-8") as f:
            f.write("\n".join(metadata_lines) + "\n")

        sample_count = len(metadata_lines)
        logger.info(f"Dataset saved: '{safe_name}' ({sample_count} samples, ref=sample_{ref_idx:03d})")

        return {
            "status": "saved",
            "dataset_id": safe_name,
            "sample_count": sample_count,
        }
    except Exception as e:
        # Clean up on failure
        if os.path.exists(dataset_dir):
            shutil.rmtree(dataset_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/dataset_builder/{name}")
async def dataset_builder_delete(name: str):
    """Discard a dataset builder working project."""
    work_dir = os.path.join(DATASET_BUILDER_DIR, name)
    if not os.path.exists(work_dir):
        raise HTTPException(status_code=404, detail="Dataset builder project not found")
    shutil.rmtree(work_dir, ignore_errors=True)
    logger.info(f"Dataset builder project discarded: {name}")
    return {"status": "deleted", "name": name}

# ── Preparer ─────────────────────────────────────────────────────────────────

@app.post("/api/preparer/start")
async def preparer_start(
    background_tasks: BackgroundTasks,
    config_json: str = Form(...),
    audio_file: UploadFile = File(...),
):
    """Upload audio and run the preparer to generate a voice training dataset."""
    if not os.path.exists(PREPARER_SCRIPT_PATH):
        raise HTTPException(
            status_code=503,
            detail="Preparer script not installed. Add app/alexandria_preparer.py to enable this feature.",
        )
    if process_state["preparer"]["running"]:
        raise HTTPException(status_code=400, detail="Preparer is already running.")

    try:
        config = PreparerConfig(**json.loads(config_json))
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Invalid config: {e}")

    has_space, free_gb = check_disk_space(ROOT_DIR, 2.0)
    if not has_space:
        raise HTTPException(status_code=400, detail=f"Insufficient disk space ({free_gb} GB free, 2 GB required).")

    audio_path = os.path.join(UPLOADS_DIR, config.audio_filename)
    async with aiofiles.open(audio_path, "wb") as f:
        while chunk := await audio_file.read(1024 * 1024):
            await f.write(chunk)

    def _run():
        state = process_state["preparer"]
        state["running"] = True
        state["logs"] = []
        state["cancel"] = False
        state["status"] = "running"
        state["output_file"] = None
        state["process"] = None

        cmd = [sys.executable, "-u", PREPARER_SCRIPT_PATH,
               "--audio", audio_path,
               "--output", os.path.join(PREPARER_OUTPUT_DIR, config.output_filename),
               "--lang", config.lang,
               "--min-confidence", str(config.min_confidence),
               "--min-snr", str(config.min_snr)]

        rc = _stream_subprocess_to_logs(cmd, BASE_DIR, state)

        if state.get("cancel"):
            state["status"] = "cancelled"
            state["logs"].append("Preparer cancelled.")
        elif rc == 0:
            state["status"] = "done"
            state["output_file"] = config.output_filename
            state["logs"].append("Preparer completed successfully.")
        else:
            state["status"] = "failed"
            state["logs"].append(f"Preparer failed (exit code {rc}).")

        state["running"] = False
        state["process"] = None

    background_tasks.add_task(_run)
    return {"status": "started"}


@app.post("/api/preparer/cancel")
async def preparer_cancel():
    state = process_state["preparer"]
    if not state["running"]:
        raise HTTPException(status_code=400, detail="No preparer is currently running.")
    proc = state.get("process")
    if proc:
        try:
            proc.terminate()
        except OSError:
            pass
    state["cancel"] = True
    return {"status": "cancel_requested"}


@app.get("/api/preparer/list")
async def preparer_list_outputs():
    """List completed dataset ZIP files available for download."""
    files = []
    if not os.path.exists(PREPARER_OUTPUT_DIR):
        return {"files": files}
    for fname in sorted(os.listdir(PREPARER_OUTPUT_DIR)):
        if not fname.endswith(".zip"):
            continue
        fpath = os.path.join(PREPARER_OUTPUT_DIR, fname)
        files.append({
            "filename": fname,
            "size_mb": round(os.path.getsize(fpath) / (1024 * 1024), 1),
            "modified": os.path.getmtime(fpath),
        })
    return {"files": files}


@app.get("/api/preparer/download/{filename:path}")
async def preparer_download(filename: str):
    """Download a generated dataset ZIP."""
    root = os.path.realpath(PREPARER_OUTPUT_DIR)
    file_path = os.path.realpath(os.path.join(PREPARER_OUTPUT_DIR, filename))
    if not file_path.startswith(root + os.sep) and file_path != root:
        raise HTTPException(status_code=400, detail="Invalid filename.")
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found.")
    return FileResponse(file_path, media_type="application/zip", filename=os.path.basename(file_path))


@app.post("/api/preparer/batch/start")
async def preparer_batch_start(request: BatchPreparerRequest, background_tasks: BackgroundTasks):
    """Process multiple audio files sequentially through the preparer script."""
    if not os.path.exists(PREPARER_SCRIPT_PATH):
        raise HTTPException(
            status_code=503,
            detail="Preparer script not installed. Add app/alexandria_preparer.py to enable this feature.",
        )
    if process_state["batch_preparer"]["running"]:
        raise HTTPException(status_code=400, detail="Batch preparer is already running.")

    has_space, free_gb = check_disk_space(ROOT_DIR, 5.0)
    if not has_space:
        raise HTTPException(status_code=400, detail=f"Insufficient disk space ({free_gb} GB free, 5 GB recommended).")

    def _run():
        state = process_state["batch_preparer"]
        state["running"] = True
        state["cancel"] = False
        state["logs"] = [f"Starting batch of {len(request.tasks)} tasks..."]
        state["tasks"] = [{"audio": t.audio_filename, "status": "pending"} for t in request.tasks]
        state["current_task_idx"] = -1

        for i, task in enumerate(request.tasks):
            if state["cancel"]:
                state["logs"].append("Batch cancelled.")
                break

            state["current_task_idx"] = i
            state["tasks"][i]["status"] = "running"

            audio_path = os.path.join(UPLOADS_DIR, task.audio_filename)
            if not os.path.exists(audio_path):
                state["logs"].append(f"[{i+1}/{len(request.tasks)}] Skipping — audio not found: {task.audio_filename}")
                state["tasks"][i]["status"] = "failed"
                continue

            state["logs"].append(f"--- [{i+1}/{len(request.tasks)}] {task.audio_filename} ---")

            cmd = [sys.executable, "-u", PREPARER_SCRIPT_PATH,
                   "--audio", audio_path,
                   "--output", os.path.join(PREPARER_OUTPUT_DIR, task.output_filename),
                   "--lang", request.lang,
                   "--min-confidence", str(request.min_confidence),
                   "--min-snr", str(request.min_snr)]

            rc = _stream_subprocess_to_logs(cmd, BASE_DIR, state, log_prefix=f"[{i+1}] ")

            if state.get("cancel"):
                state["tasks"][i]["status"] = "cancelled"
                break
            elif rc == 0:
                state["tasks"][i]["status"] = "done"
                state["logs"].append(f"[{i+1}] Done: {task.audio_filename}")
            else:
                state["tasks"][i]["status"] = "failed"
                state["logs"].append(f"[{i+1}] Failed (exit {rc}): {task.audio_filename}")

        state["running"] = False
        state["logs"].append("Batch processing finished.")

    background_tasks.add_task(_run)
    return {"status": "started", "task_count": len(request.tasks)}


@app.post("/api/preparer/batch/cancel")
async def preparer_batch_cancel():
    process_state["batch_preparer"]["cancel"] = True
    return {"status": "cancel_requested"}

@app.get("/api/preparer/status/{task_name}")
async def preparer_status(task_name: str):
    """Get preparer subprocess status for a task."""
    if task_name not in {"preparer", "batch_preparer"}:
        raise HTTPException(status_code=404, detail=f"Unknown task: {task_name}")
    state = process_state[task_name]
    # Exclude the live subprocess handle (Popen) — not JSON-serializable.
    return {k: v for k, v in state.items() if k != "process"}


if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("ALEXANDRIA_HOST", "127.0.0.1")
    port = int(os.environ.get("ALEXANDRIA_PORT", "4200"))
    uvicorn.run(app, host=host, port=port, access_log=False)
