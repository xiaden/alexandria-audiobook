<img width="475" height="467" alt="Alexandria Logo" src="https://github.com/user-attachments/assets/fa2c36d3-a5f3-49ab-9dfe-30933359dfbd" />

# Alexandria Audiobook Generator

English | [中文](README_CN.md)

> **A note to new users:** Alexandria has recently seen a sudden surge of attention and new users. As a small project, I may not be able to respond to every issue promptly. Before opening an issue, please read this README and the [Wiki](https://github.com/Finrandojin/alexandria-audiobook/wiki) thoroughly — most common questions are already answered there. Thank you for your patience!

Transform any book or novel into a fully-voiced audiobook using AI-powered script annotation and text-to-speech. Features a built-in Qwen3-TTS engine with batch processing and a browser-based editor for fine-tuning every line before final export.

## Example: [sample.mp3](https://github.com/user-attachments/files/25276110/sample.mp3)


## Screenshots

<img src="https://github.com/user-attachments/assets/874b5e30-56d2-4292-b754-4408fc53f5d6" width="30%"></img> <img src="https://github.com/user-attachments/assets/488cde02-6b93-47fa-874b-97a618ae482c" width="30%"></img> <img src="https://github.com/user-attachments/assets/4c0805a6-bb9d-42c1-a9ff-79bb29d0613c" width="30%"></img> <img src="https://github.com/user-attachments/assets/8e58a5bf-ed8f-4864-8545-1e3d9681b0cf" width="30%"></img> <img src="https://github.com/user-attachments/assets/531830da-8668-4189-a0dc-020e6661bfb6" width="30%"></img> 

## Features

### AI-Powered Pipeline
- **Local & Cloud LLM Support** - Use any OpenAI-compatible API (LM Studio, Ollama, OpenAI, etc.)
- **Automatic Script Annotation** - A serial 9-walk LLM pipeline (2a→2i) converts your book into structured spans with speakers, dialogue, and TTS instruct directions
- **Confidence Review** - Low-confidence annotations are flagged for human review (accept / reject / override) instead of silently propagating errors
- **Voice Audition & Assignment** - The pipeline generates a voice description for every character, auditions it against your voice catalog, and assigns voices automatically — one click from book to fully-voiced cast
- **Speaker Aliases** - Map multiple speaker names to the same voice (e.g. "YOUNG ELENA" → "ELENA") so variants share a single voice configuration

### Voice Generation
- **Built-in TTS Engine** - Qwen3-TTS runs locally with no external server required
- **External Server Mode** - Optionally connect to a remote Qwen3-TTS Gradio server
- **Multi-Language Support** - English, Chinese, French, German, Italian, Japanese, Korean, Portuguese, Russian, Spanish, or Auto-detect
- **Custom Voices** - 9 pre-trained voices with instruct-based emotion/tone control
- **Voice Cloning** - Clone any voice from a 5-15 second reference audio sample
- **Voice Designer** - Create new voices from text descriptions (e.g. "A warm, deep male voice with a calm and steady tone")
- **LoRA Voice Training** - Fine-tune the Base model on custom voice datasets to create persistent voice identities with instruct-following
- **Built-in LoRA Presets** - Pre-trained voice adapters included out of the box, ready to assign to characters
- **Dataset Builder** - Interactive tool for creating LoRA training datasets with per-sample text, emotion, and audio preview
- **Batch Processing** - Generate dozens of spans simultaneously with 3-6x real-time throughput
- **Codec Compilation** - Optional `torch.compile` optimization for 3-4x faster batch decoding
- **Non-verbal Sounds** - LLM writes natural vocalizations ("Ahh!", "Mmm...", "Haha!") with context-aware instruct directions
- **Natural Pauses** - Configurable silence between speakers (default 500ms) and same-speaker segments (default 250ms)

### Web UI Editor
- **Streamlined Interface** - Core pipeline tabs (Setup, Script, Voices, Editor) plus advanced tools (Designer, Preparer, Dataset, Training)
- **Span Editor** - Edit speaker, text, and instruct for any line
- **Structural Operations** - Split, merge, move, and delete spans directly in the editor
- **Batch Processing** - Optimized batch rendering with sub-batching for efficient GPU utilization
- **Live Progress** - Real-time walk status and render progress tracking
- **Audio Preview** - Preview voices and listen to rendered audio before final download

### Export Options
- **M4B Audiobook** - Chaptered M4B (AAC) with embedded chapter markers for audiobook players (Audiobookshelf, Apple Books, VLC, etc.)
- **Raw Chunks** - Download the rendered audio chunks as a ZIP for DAW editing or manual assembly

## Requirements

- [Pinokio](https://pinokio.computer/)
- LLM server (one of the following):
  - [LM Studio](https://lmstudio.ai/) (local) - recommended: Qwen3 or similar
  - [Ollama](https://ollama.ai/) (local)
  - [OpenAI API](https://platform.openai.com/) (cloud)
  - Any OpenAI-compatible API
- **GPU:** 8 GB VRAM minimum, 16 GB+ recommended — see compatibility table below
  - Each TTS model uses ~3.4 GB; remaining VRAM determines batch size
  - CPU mode available on all platforms but significantly slower
- **RAM:** 16 GB recommended (8 GB minimum)
- **Disk:** ~20 GB (8 GB venv/PyTorch, ~7 GB for model weights, working space for audio)

### GPU Compatibility

| GPU | OS | Status | Driver Requirement | Notes |
|-----|-----|--------|-------------------|-------|
| **NVIDIA** | Windows | Full support | Driver 550+ (CUDA 12.8) | Flash attention included for faster encoding |
| **NVIDIA** | Linux | Full support | Driver 550+ (CUDA 12.8) | Flash attention + triton included |
| **AMD** | Linux | Full support | ROCm 6.3+ | ROCm optimizations applied automatically |
| **AMD** | Windows | CPU only | N/A | GPU acceleration is not supported — the app runs in CPU mode. For GPU acceleration with AMD, use Linux |
| **Apple Silicon** | macOS | CPU only | N/A | MPS acceleration is not currently supported. Functional but slow |
| **Intel** | macOS | CPU only | N/A | |

> **Note:** No external TTS server is required. Alexandria includes a built-in Qwen3-TTS engine that loads models directly. Model weights are downloaded automatically on first use (~3.5 GB per model variant).

> **Documentation:** For in-depth guidance on voice types, LoRA training, batch generation, and more, see the [Wiki](https://github.com/Finrandojin/alexandria-audiobook/wiki).

## Installation

### Option A: Pinokio (Recommended)

1. Install [Pinokio](https://pinokio.computer/) if you haven't already
2. Open Alexandria on Pinokio: **[Install via Pinokio](https://beta.pinokio.co/apps/github-com-finrandojin-alexandria-audiobook)**
   - Or manually: in Pinokio, click **Download** and paste `https://github.com/Finrandojin/alexandria-audiobook`
3. Click **Install** to set up dependencies
4. Click **Start** to launch the web interface

### Option B: Google Colab (No Install Required)

No GPU or wrong OS? Run Alexandria on a free T4 GPU in your browser:

[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/Finrandojin/alexandria-audiobook/blob/main/alexandria_colab.ipynb)

Requires a free [ngrok account](https://dashboard.ngrok.com/signup) for the web UI tunnel. See the notebook for full instructions.

### Option C: Docker (NVIDIA GPU)

For integration into automated pipelines or server deployments:

```bash
git clone https://github.com/Finrandojin/alexandria-audiobook.git
cd alexandria-audiobook
docker compose up --build
```

Requires [Docker](https://docs.docker.com/get-docker/) with the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html). The web UI is available at `http://localhost:4200`. TTS models download on first use and are cached in a Docker volume. User data (uploads, voice configs, trained LoRA adapters, audio output) persists via bind mounts to the project directory.

## First Launch — What to Expect

If this is your first time running Alexandria, read this before anything else.

### 1. You Need an LLM Server Running First

Alexandria does **not** include an LLM — it connects to one over an API. Before generating a script, you must have one of these running:

| Server | Default URL | Install |
|--------|-------------|---------|
| [LM Studio](https://lmstudio.ai/) | `http://localhost:1234/v1` | Download, load a model, start server |
| [Ollama](https://ollama.ai/) | `http://localhost:11434/v1` | `ollama run qwen3` |
| [OpenAI API](https://platform.openai.com/) | `https://api.openai.com/v1` | Get an API key |

If the LLM server isn't running when you start the annotation walks, the generation will fail. Check the Pinokio terminal for error details.

### 2. First TTS Generation Downloads ~3.5 GB

The TTS models are **not** included in the install. They download automatically from Hugging Face the first time you generate audio. This is normal:

- **Each model variant is ~3.5 GB** (CustomVoice, Base/Clone, VoiceDesign)
- Only the variant you use gets downloaded (most users start with CustomVoice)
- Downloads happen in the background — **check the Pinokio terminal** for progress
- The web UI may appear frozen during this time. It is not — it's waiting for the download to finish
- After the first download, models are cached locally and load in seconds

> **Tip:** If the download seems stuck, check your internet connection. If it fails, restart the app and try again — it will resume from where it left off.

### 3. First Batch Has Extra Warmup Time

The very first batch generation in a session takes longer than subsequent ones:

- **MIOpen autotuning** (AMD GPUs): The GPU kernel optimizer runs once per session, adding 30-60 seconds
- **Codec compilation** (if enabled): One-time ~30-60 second warmup, then 3-4x faster for all remaining batches
- **This is expected.** After the first batch, generation speed stabilizes

### 4. VRAM Determines What You Can Do

| Available VRAM | What Works |
|---------------|------------|
| 8 GB | One model at a time, small batches (2-5 chunks), CPU offload may be needed |
| 16 GB | Comfortable for most use cases, batches of 10-20 chunks |
| 24 GB+ | Full speed, batches of 40-60 chunks with codec compilation |

- If you run out of VRAM, reduce **Parallel Workers** in the Setup tab
- Close other GPU applications (games, other AI tools) before generating
- Switching between voice types (Custom → Clone → LoRA) unloads and reloads models, which temporarily frees VRAM

### 5. Where to Look When Something Goes Wrong

The web UI shows high-level status, but **detailed logs are in the Pinokio terminal**:

- Click **Terminal** in the Pinokio sidebar to see real-time output
- Model loading, download progress, VRAM estimates, and errors all appear here
- If generation fails silently in the UI, the terminal will show why

For common issues and solutions, see [Troubleshooting](https://github.com/Finrandojin/alexandria-audiobook/wiki/Troubleshooting).

---

## Quick Start

The interface is split into a **core pipeline** (green tabs, numbered) and **advanced tools** (blue tabs, unnumbered). You only need the core pipeline to produce an audiobook.

### Core Pipeline

**Step 1 — Setup**
Configure your LLM connection and TTS engine. At minimum you need:
- **LLM Base URL**: `http://localhost:1234/v1` (LM Studio) or `http://localhost:11434/v1` (Ollama)
- **LLM API Key**: Your API key (use `local` for local servers)
- **LLM Model Name**: The model to use (e.g., `qwen2.5-14b`)
- **TTS Mode**: `local` (built-in, recommended) — loads models directly, no external server needed
- Click **Save Configuration** when done

**Step 2 — Script**
- Select your book file (EPUB only) using the file picker — it uploads and is onboarded into the pipeline automatically (converted to plain text server-side)
- Click **Run All Walks** — this runs the 9-walk LLM annotation pipeline (scene segmentation → character discovery → alias resolution → scene presence → span attribution → character description → voice audition → voice assignment → delivery) to build the annotated script
- Watch walk progress in real time; each walk's status is shown as it completes
- *(Optional)* Click **Re-onboard** if you need to reload the book from scratch

**Step 3 — Voices**
The pipeline's voice assignment walk (2h) automatically assigns a voice to every character from your voice catalog:
- Open the **Voices** tab to review the character list and each character's assigned voice
- Change any assignment via the dropdown — this saves to the character ledger immediately
- For each voice type: Custom Voice (easiest), Clone Voice, LoRA Voice, or Voice Design
- For Custom Voice, pick from 9 presets (Ryan, Serena, Aiden, etc.) and optionally set a character style (e.g., "Heavy Scottish accent")
- **Speaker Aliases** — Map one speaker to another character's voice config (e.g., set "YOUNG ELENA" as alias of "ELENA"). Aliased speakers use the target's voice config during generation
- See [Voice Types](https://github.com/Finrandojin/alexandria-audiobook/wiki/Voice-Types) for guidance on each type

**Step 4 — Editor**
- Click **Render** to generate audio for all spans in batch
- Edit any span's text, speaker, or instruct inline and regenerate it
- Use **Split / Merge / Move / Delete** operations to restructure the script
- Resolve low-confidence annotations flagged by the confidence review
- When satisfied, click **Merge** to combine everything into the final M4B, then **Download**

### Advanced Tools (Optional)

These tabs are for power users who want more control over voice creation:

- **Designer** — Create new voices from text descriptions (e.g., "A warm elderly woman with a gentle raspy voice"). Save them to use as clone references in the Voices tab
- **Preparer** — Batch-prepare voice datasets from uploaded audio (used as LoRA training data)
- **Dataset** — Build LoRA training datasets interactively, one sample at a time with audio preview
- **Training** — Train LoRA adapters on voice datasets to create persistent voice identities that follow instruct directions

## Web Interface

### Setup Tab
Configure connections to your LLM and TTS engine.

**LLM Settings:**
- **Base URL** - LLM server URL (LM Studio / Ollama / OpenAI / any OpenAI-compatible API)
- **API Key** - Your API key (`local` for local servers)
- **Model Name** - The model to use
- **Reasoning Effort** - Optional thinking-effort setting for reasoning models
- **Temperature** - LLM sampling temperature used for annotation walks

**TTS Settings:**
- **Mode** - `local` (built-in engine) or `external` (connect to Gradio server)
- **Device** - `auto` (recommended), `cuda`, `cpu`, or `mps`
- **Language** - TTS synthesis language: English (default), Chinese, French, German, Italian, Japanese, Korean, Portuguese, Russian, Spanish, or Auto (let the model detect)
- **Parallel Workers** - Batch size for fast batch rendering (higher = more VRAM usage)
- **Batch Seed** - Fixed seed for reproducible batch output (leave empty for random)
- **Compile Codec** - Enable `torch.compile` for 3-4x faster batch decoding (adds ~30-60s warmup on first generation)
- **Batch Group by Type** - Split batches by voice type to avoid mixed-type dispatch
- **Sub-batching** - Split batches by text length to reduce wasted GPU compute on padding (enabled by default)
- **Min Sub-batch Size** - Minimum chunks per sub-batch before allowing a split (default: 4)
- **Length Ratio** - Maximum longest/shortest text length ratio before forcing a sub-batch split (default: 5)
- **Max Sub-batch Items** - Upper bound on chunks per sub-batch
- **Speaker Change Pause** - Silence in milliseconds between different speakers during merge (default: 500)
- **Same Speaker Pause** - Silence in milliseconds when the same speaker continues during merge (default: 250)

### Script Tab
Upload an EPUB file and run the annotation walks. Onboarding is EPUB-only — the file is converted to plain text server-side on upload. The pipeline runs 9 serial LLM walks that convert your book into a structured span graph with:
- Speaker identification (NARRATOR vs character names)
- Dialogue text with natural vocalizations (written as pronounceable text, not tags)
- Style directions for TTS delivery
- Character descriptions, voice auditions, and voice assignments

- **Onboard** - Upload and load the book into the pipeline
- **Run All Walks** - Execute the 9-walk annotation DAG in sequence
- **Walk status** - Per-walk progress shown in real time; each walk can also be re-run individually
- **Cancel Walks** - Stop a running walk cycle
- **Re-onboard** - Reload the book and reset the pipeline state

### Voices Tab
The pipeline assigns a voice to every character during walk 2h. The Voices tab lets you review and adjust those assignments:

- **Character list** - Every character from the character ledger with its assigned voice
- **Assignment dropdown** - Change a character's voice from the catalog; saved immediately via `PUT /api/pipeline/characters/{id}/voice`
- **Voice catalog** - Create and manage voice configs (Custom, Clone, LoRA, Voice Design) that can be assigned to characters

**Speaker Aliases:**
Each voice config can be set as an alias of another. Setting a speaker as an alias of another speaker means it will use the target's voice configuration during audio generation. Useful for:
- Character name variants (e.g., "DR. SMITH" → "SMITH")
- Age variants (e.g., "YOUNG ELENA" → "ELENA")
- Reducing the number of voices to configure

Aliases resolve transitively (A → B → C uses C's config) with cycle detection.

**Custom Voice Mode:**
- Select from 9 pre-trained voices: Aiden, Dylan, Eric, Ono_anna, Ryan, Serena, Sohee, Uncle_fu, Vivian
- Set a character style that appends persistent traits to every TTS instruct (e.g., "Heavy Scottish accent", "Refined aristocratic tone")
- Optionally set a seed for reproducible output

**Clone Voice Mode:**
- Select a designed voice or enter a custom reference audio path
- Provide the exact transcript of the reference
- Note: Instruct directions are ignored for cloned voices

**LoRA Voice Mode:**
- Select a trained LoRA adapter from the Training tab
- Set a character style (same as Custom — appended to every instruct)
- Combines voice identity from training with instruct-following from the Base model

**Voice Design Mode:**
- Set a base voice description (e.g., "Young strong soldier")
- Each line's instruct is appended as delivery/emotion direction
- Generates voice on-the-fly using the VoiceDesign model — ideal for minor characters

### Voice Designer Tab
Create new voices from text descriptions without needing reference audio.

- **Describe a voice** in natural language (e.g., "A warm elderly woman with a gentle, raspy voice and a slight Southern drawl")
- **Preview** the voice with sample text before saving
- **Save to library** for use as clone voice references in the Voices tab
- Uses the Qwen3-TTS VoiceDesign model to synthesize voice characteristics from descriptions

### Training Tab
Train LoRA adapters on the Base model to create custom voice identities. Several built-in LoRA presets are included out of the box and appear alongside your trained adapters.

**Dataset:**
- **Upload ZIP** — WAV files (24kHz mono) + `metadata.jsonl` with `audio_filepath` and `text` fields
- **Generate Dataset** — Auto-generate training samples from a Voice Designer description with custom sample texts
- **Dataset Builder** — Interactive tool in its own tab (see below) for building datasets sample-by-sample with preview

**Training Configuration:**
- **Adapter Name** — Identifier for the trained model
- **Epochs** — Full passes over the dataset (15-30 recommended for 20+ samples)
- **Learning Rate** — Default 5e-6 (conservative). Higher trains faster but risks instability
- **LoRA Rank** — Adapter capacity. High (64+) locks voice identity strongly but can flatten delivery. Low (8-16) preserves expressiveness
- **LoRA Alpha** — Scaling factor. Effective strength = alpha / rank. Common starting point: alpha = 2x rank
- **Language** — Language for the codec prefix token. Match this to your training data's language (English, Chinese, Korean, Japanese, etc.). Mismatched language can cause the adapter to lose speaker identity
- **Batch Size / Grad Accum** — Batch 1 with gradient accumulation 8 is typical for 24GB cards

**Training tips:**
- Include samples with varied emotions (happy, sad, angry, calm) for expressive voices
- Neutral-only training data produces flat voices that resist instruct prompting
- The settings info panel in the UI explains each parameter's effect on voice quality

### Dataset Builder Tab
Build LoRA training datasets interactively, one sample at a time.

- **Create a project** with a voice description and optional global seed
- **Define samples** — Set text and emotion/style per row
- **Preview audio** — Generate and listen to individual samples or batch-generate all at once
- **Cancel batch** — Stop a running batch generation without losing completed samples
- **Save as dataset** — Export the project as a training-ready dataset that appears in the Training tab
- Designed voices and Voice Designer descriptions drive the audio generation via Qwen3-TTS VoiceDesign model

### Editor Tab
Fine-tune your audiobook before export:
- **View all spans** in a table with status indicators and confidence scores
- **Edit inline** - Click to modify speaker, text, or instruct
- **Structural operations** - Split, merge, move, or delete a span
- **Render** - Generate audio for all pending spans in batch (with progress polling)
- **Confidence review** - Accept, reject, or override low-confidence annotations
- **Merge** - Combine rendered chunks into the final M4B audiobook
- **Download** - Download the merged M4B (or the raw chunks as a ZIP)

## Performance

### Recommended Settings for Batch Generation

| Setting | Recommended | Notes |
|---------|-------------|-------|
| TTS Mode | `local` | Built-in engine, no external server |
| Compile Codec | `true` | 3-4x faster decoding after one-time warmup |
| Parallel Workers | 20-60 | Higher = more throughput, more VRAM |

### Benchmarks

Tested on AMD RX 7900 XTX (24 GB VRAM, ROCm 6.3/7.2):

| Configuration | Throughput |
|--------------|------------|
| Standard mode (sequential) | ~1x real-time |
| Batch mode, no codec compile | ~2x real-time |
| Batch mode + compile_codec | **3-6x real-time** |

A 273-chunk audiobook (~54 minutes of audio) generates in approximately 16 minutes with batch mode and codec compilation enabled.

### ROCm (AMD GPU) Notes

> **Linux only.** AMD GPU acceleration requires ROCm 6.3+ on Linux. AMD GPUs on Windows run in CPU mode — see [GPU Compatibility](#gpu-compatibility).

Alexandria automatically applies ROCm-specific optimizations when running on AMD GPUs:
- **MIOpen fast-find mode** - Prevents workspace allocation failures that cause slow GEMM fallback
- **Triton AMD flash attention** - Enables native flash attention for the whisper encoder
- **triton_key compatibility shim** - Fixes `torch.compile` on pytorch-triton-rocm

These are applied transparently and require no configuration.

> **ROCm 7.x GPU downclocking fix:** ROCm 7.x has a regression where the GPU's DPM controller aggressively downclocks the shader engine between autoregressive generation steps, causing batch generation to slow to a crawl or appear to hang. The fix is to set the GPU power profile to COMPUTE, which enforces a minimum clock frequency floor:
>
> ```bash
> echo 5 | sudo tee /sys/class/drm/card1/device/pp_power_profile_mode
> ```
>
> This needs to be run once per boot (it does not persist across reboots). You can add it to your system startup or run it manually before launching Alexandria. To verify it's active, check for `COMPUTE*` in the output of:
>
> ```bash
> cat /sys/class/drm/card1/device/pp_power_profile_mode
> ```
>
> ROCm 6.x users and NVIDIA users are not affected.

## Script Format

The annotated script is a JSON array with `speaker`, `text`, and `instruct` fields (one entry per span):

```json
[
  {"speaker": "NARRATOR", "text": "The door creaked open slowly.", "instruct": "Calm, even narration."},
  {"speaker": "ELENA", "text": "Ah! Who's there?", "instruct": "Startled and fearful, sharp whispered question, voice cracking with panic."},
  {"speaker": "MARCUS", "text": "Haha... did you miss me?", "instruct": "Menacing confidence, low smug drawl with a dark chuckle, savoring the moment."}
]
```

- **`instruct`** — 2-3 sentence TTS voice direction sent directly to the engine. Set tone, describe delivery, then give specific references. Example: "Devastated by grief, Sniffing between words and pausing to collect herself, end with a wracking sob."

### Non-verbal Sounds
Vocalizations are written as real pronounceable text that the TTS speaks directly — no bracket tags or special tokens. The LLM generates natural onomatopoeia with short instruct directions:
- Gasps: "Ah!", "Oh!" with instruct like "Fearful, sharp gasp."
- Sighs: "Haah...", "Hff..."
- Laughter: "Haha!", "Ahaha..."
- Crying: "Hic... sniff..."
- Exclamations: "Mmm...", "Hmm...", "Ugh..."

## Output Files

Rendering produces per-span audio chunks in a job-specific output directory, which the pipeline then merges into a single M4B:

**Final Audiobook:**
- `audiobook.m4b` - AAC audiobook with embedded chapter markers (Audiobookshelf, Apple Books, VLC, Haruna, and most audiobook players)

**Render job output (per render):**
- `chunk_0000.wav`, `chunk_0001.wav`, ... - One WAV per span, numbered in timeline order
- The `GET /api/pipeline/download/{job_id}` endpoint serves the merged `audiobook.m4b` when available, otherwise packages the chunks into `audiobook.zip` for manual assembly

## API Reference

Alexandria exposes a REST API for programmatic access:

### Configuration
```bash
# Get current config
curl http://127.0.0.1:4200/api/config

# Save config
curl -X POST http://127.0.0.1:4200/api/config \
  -H "Content-Type: application/json" \
  -d '{
    "llm": {"base_url": "...", "api_key": "...", "model_name": "..."},
    "tts": {
      "mode": "local",
      "device": "auto",
      "language": "English",
      "parallel_workers": 25,
      "batch_seed": 12345,
      "compile_codec": true,
      "sub_batch_enabled": true,
      "sub_batch_min_size": 4,
      "sub_batch_ratio": 5,
      "pause_between_speakers_ms": 500,
      "pause_same_speaker_ms": 250
    }
  }'
```

### Pipeline (Script Generation)
```bash
# Onboard an EPUB — extracts text, populates spine, returns book_id
curl -X POST http://127.0.0.1:4200/api/pipeline/onboard \
  -F "file=@mybook.epub"

# Run all annotation walks (replace <book_id> with the ID returned by onboard)
curl -X POST http://127.0.0.1:4200/api/pipeline/run_all_walks \
  -H "Content-Type: application/json" \
  -d '{"book_id": "<book_id>"}'

# Check walk status
curl http://127.0.0.1:4200/api/pipeline/walk_status/<book_id>

# Review items (low-confidence annotations needing human review)
curl http://127.0.0.1:4200/api/pipeline/review/<book_id>

# Accept a review item
curl -X POST http://127.0.0.1:4200/api/pipeline/review/accept \
  -H "Content-Type: application/json" \
  -d '{"item_id": "<item_id>"}'
```

### Voice Catalog
```bash
# List all voice configs
curl http://127.0.0.1:4200/api/pipeline/voices

# Create a voice config
curl -X POST http://127.0.0.1:4200/api/pipeline/voices \
  -H "Content-Type: application/json" \
  -d '{"name": "NARRATOR", "type": "custom", "voice": "Ryan", "character_style": "calm"}'

# Assign a voice to a character
curl -X PUT http://127.0.0.1:4200/api/pipeline/characters/<character_id>/voice \
  -H "Content-Type: application/json" \
  -d '{"voice_assignment_id": "<voice_id>"}'
```

### Character Ledger
```bash
# View characters extracted during pipeline walks (voices, roles, descriptions)
curl http://127.0.0.1:4200/api/pipeline/characters/<book_id>
```

### Voice Designer
```bash
# Preview a voice from text description
curl -X POST http://127.0.0.1:4200/api/voice_design/preview \
  -H "Content-Type: application/json" \
  -d '{"description": "A warm, deep male voice", "text": "Hello world."}'

# Save a designed voice
curl -X POST http://127.0.0.1:4200/api/voice_design/save \
  -H "Content-Type: application/json" \
  -d '{"name": "warm_narrator", "description": "A warm, deep male voice", "text": "Hello world."}'

# List saved designed voices
curl http://127.0.0.1:4200/api/voice_design/list

# Delete a designed voice
curl -X DELETE http://127.0.0.1:4200/api/voice_design/delete/voice_id_here
```

### LoRA Training
```bash
# Upload a training dataset (ZIP with WAV + metadata.jsonl)
curl -X POST http://127.0.0.1:4200/api/lora/upload_dataset \
  -F "file=@dataset.zip" -F "name=my_voice"

# Generate a dataset from Voice Designer description
curl -X POST http://127.0.0.1:4200/api/lora/generate_dataset \
  -H "Content-Type: application/json" \
  -d '{"name": "warm_voice", "description": "A warm male voice", "texts": ["Hello.", "Goodbye."]}'

# List uploaded datasets
curl http://127.0.0.1:4200/api/lora/datasets

# Delete a dataset
curl -X DELETE http://127.0.0.1:4200/api/lora/datasets/dataset_id_here

# Start LoRA training
curl -X POST http://127.0.0.1:4200/api/lora/train \
  -H "Content-Type: application/json" \
  -d '{"name": "narrator_warm", "dataset_id": "my_voice", "epochs": 25, "lr": "5e-6", "lora_r": 32, "lora_alpha": 64}'

# List trained adapters
curl http://127.0.0.1:4200/api/lora/models

# Test a trained adapter
curl -X POST http://127.0.0.1:4200/api/lora/test \
  -H "Content-Type: application/json" \
  -d '{"adapter_id": "narrator_warm_1234567890", "text": "Test line.", "instruct": "Calm narration."}'

# Delete an adapter
curl -X DELETE http://127.0.0.1:4200/api/lora/models/adapter_id_here

# Check LoRA training status
curl http://127.0.0.1:4200/api/lora/status
```

### Voice Dataset Preparer
```bash
# Check a preparer job's status (task_name: preparer | batch_preparer)
curl http://127.0.0.1:4200/api/preparer/status/preparer
curl http://127.0.0.1:4200/api/preparer/status/batch_preparer
```

### Dataset Builder
```bash
# List all dataset builder projects
curl http://127.0.0.1:4200/api/dataset_builder/list

# Create a new project
curl -X POST http://127.0.0.1:4200/api/dataset_builder/create \
  -H "Content-Type: application/json" \
  -d '{"name": "my_voice_dataset"}'

# Update project metadata (description and global seed)
curl -X POST http://127.0.0.1:4200/api/dataset_builder/update_meta \
  -H "Content-Type: application/json" \
  -d '{"name": "my_voice_dataset", "description": "A warm male narrator", "global_seed": "42"}'

# Update sample rows
curl -X POST http://127.0.0.1:4200/api/dataset_builder/update_rows \
  -H "Content-Type: application/json" \
  -d '{"name": "my_voice_dataset", "rows": [{"text": "Hello world.", "emotion": "cheerful"}]}'

# Generate a single sample preview
curl -X POST http://127.0.0.1:4200/api/dataset_builder/generate_sample \
  -H "Content-Type: application/json" \
  -d '{"name": "my_voice_dataset", "description": "A warm male voice", "sample_index": 0, "samples": [{"text": "Hello.", "emotion": "cheerful"}]}'

# Batch generate all samples
curl -X POST http://127.0.0.1:4200/api/dataset_builder/generate_batch \
  -H "Content-Type: application/json" \
  -d '{"name": "my_voice_dataset", "description": "A warm male voice", "samples": [{"text": "Hello.", "emotion": "cheerful"}]}'

# Check batch generation status
curl http://127.0.0.1:4200/api/dataset_builder/status/my_voice_dataset

# Cancel a running batch generation
curl -X POST http://127.0.0.1:4200/api/dataset_builder/cancel \
  -H "Content-Type: application/json" \
  -d '{"name": "my_voice_dataset"}'

# Save project as a training dataset
curl -X POST http://127.0.0.1:4200/api/dataset_builder/save \
  -H "Content-Type: application/json" \
  -d '{"name": "my_voice_dataset", "ref_sample_index": 0}'

# Delete a project
curl -X DELETE http://127.0.0.1:4200/api/dataset_builder/my_voice_dataset
```

### Rendering & Download
```bash
# Start a render job (replace <book_id> with your book)
curl -X POST http://127.0.0.1:4200/api/pipeline/render \
  -H "Content-Type: application/json" \
  -d '{"book_id": "<book_id>", "use_batch": true}'
# → {"job_id": "...", "status": "started"}

# Poll render status until completed
curl http://127.0.0.1:4200/api/pipeline/render_status/<job_id>

# Merge rendered chunks into audiobook.m4b
curl -X POST http://127.0.0.1:4200/api/pipeline/merge \
  -H "Content-Type: application/json" \
  -d '{"job_id": "<job_id>"}'

# Download the merged M4B (or audiobook.zip of raw chunks)
curl http://127.0.0.1:4200/api/pipeline/download/<job_id> --output audiobook.m4b
```

## Python Integration

```python
import requests
import time

BASE = "http://127.0.0.1:4200"

# Onboard EPUB — extracts text, populates spine, returns book_id
with open("mybook.epub", "rb") as f:
    onboard = requests.post(f"{BASE}/api/pipeline/onboard", files={"file": f}).json()
book_id = onboard["book_id"]

# Run all annotation walks
requests.post(f"{BASE}/api/pipeline/run_all_walks", json={"book_id": book_id})

# Poll until all walks complete
while True:
    statuses = requests.get(f"{BASE}/api/pipeline/walk_status/{book_id}").json()
    if all(s in ("completed", "error") for s in statuses.values()):
        break
    time.sleep(2)

# Assign a voice to a character (optional; walks 2g/2h already assign voices)
characters = requests.get(f"{BASE}/api/pipeline/characters/{book_id}").json()
narrator = next(c for c in characters if c["name"] == "NARRATOR")
requests.put(
    f"{BASE}/api/pipeline/characters/{narrator['id']}/voice",
    json={"voice_assignment_id": "NARRATOR"},
)

# Render the audiobook
job = requests.post(f"{BASE}/api/pipeline/render", json={"book_id": book_id, "use_batch": True}).json()
job_id = job["job_id"]

# Poll until render completes
while True:
    status = requests.get(f"{BASE}/api/pipeline/render_status/{job_id}").json()
    if status["status"] == "completed":
        break
    time.sleep(2)

# Merge into audiobook.m4b and download
requests.post(f"{BASE}/api/pipeline/merge", json={"job_id": job_id})
with open("audiobook.m4b", "wb") as f:
    f.write(requests.get(f"{BASE}/api/pipeline/download/{job_id}").content)
```

## JavaScript Integration

```javascript
const BASE = "http://127.0.0.1:4200";

// Onboard EPUB — extracts text, populates spine, returns book_id
const formData = new FormData();
formData.append("file", fileInput.files[0]);
const onboard = await fetch(`${BASE}/api/pipeline/onboard`, { method: "POST", body: formData });
const { book_id: bookId } = await onboard.json();

// Run all annotation walks
await fetch(`${BASE}/api/pipeline/run_all_walks`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ book_id: bookId }),
});

// Poll until all walks complete
async function waitForWalks(bookId) {
  while (true) {
    const res = await fetch(`${BASE}/api/pipeline/walk_status/${bookId}`);
    const statuses = await res.json();
    if (Object.values(statuses).every(s => s === "completed" || s === "error")) return statuses;
    await new Promise(r => setTimeout(r, 2000));
  }
}
await waitForWalks(bookId);

// Render the audiobook
const render = await fetch(`${BASE}/api/pipeline/render`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ book_id: bookId, use_batch: true }),
});
const { job_id: jobId } = await render.json();

// Poll until render completes
while (true) {
  const res = await fetch(`${BASE}/api/pipeline/render_status/${jobId}`);
  const status = await res.json();
  if (status.status === "completed") break;
  await new Promise(r => setTimeout(r, 2000));
}

// Merge into audiobook.m4b and download
await fetch(`${BASE}/api/pipeline/merge`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ job_id: jobId }),
});
window.location.href = `${BASE}/api/pipeline/download/${jobId}`;
```

## Recommended LLM Models

For script generation, non-thinking models work best:
- **Qwen3-next** (80B-A3B-instruct) - Excellent JSON output and instruct directions
- **Gemma3** (27B recommended) - Strong JSON output and instruct directions
- **Qwen2.5** (any size) - Reliable JSON output
- **Qwen3** (non-thinking variant)
- **Llama 3.1/3.2** - Good character distinction
- **Mistral/Mixtral** - Fast and reliable

**Thinking models** (DeepSeek-R1, GLM4-air, etc.) can interfere with JSON output. If you must use one, prefer a non-thinking variant or a separate endpoint for annotation walks.

## Troubleshooting

### Script generation fails
- Check LLM server is running and accessible
- Verify model name matches what's loaded
- Try a different model - some struggle with JSON output

### Model download fails or is very slow
- TTS models (~3.5 GB each) are downloaded from Hugging Face on first use
- If downloads are slow or fail due to network restrictions (common in mainland China), set a Hugging Face mirror before launching:
  - Set the environment variable `HF_ENDPOINT=https://hf-mirror.com` before starting the app
  - Or in Pinokio, add it to start.js `env` field: `env: { HF_ENDPOINT: "https://hf-mirror.com" }`
- If you hit rate limits, create a free [Hugging Face account](https://huggingface.co/join) and set `HF_TOKEN` to your access token
- Downloads resume automatically if interrupted — just restart the app

### TTS generation fails
- Check the Pinokio terminal for model loading errors
- Ensure sufficient VRAM (16+ GB recommended for bfloat16)
- For external mode, ensure the Gradio TTS server is running at the configured URL
- Verify every character has a valid voice assigned in the Voices tab (voice configs are stored in the pipeline's voice_config table)
- For clone voices, verify reference audio exists and transcript is accurate

### Slow batch generation
- Enable **Compile Codec** in Setup (adds warmup time but 3-4x faster after)
- Increase **Parallel Workers** (batch size) if VRAM allows
- If you see MIOpen warnings on AMD, these are handled automatically

### Out of memory errors
- Reduce **Parallel Workers** (batch size)
- Close other GPU-intensive applications
- Try `device: cpu` as a fallback (much slower)

### Broken or tiny MP3 files (428 bytes)
Conda's bundled ffmpeg on Windows often lacks the MP3 encoder (libmp3lame). Alexandria now detects this and automatically falls back to WAV, but if you want MP3 output:
- Install ffmpeg with MP3 support: `conda install -c conda-forge ffmpeg`
- Or remove conda's ffmpeg to use your system one: `conda remove ffmpeg`
- Verify with: `ffmpeg -encoders 2>/dev/null | grep mp3`

### Audio quality issues
- Use 5-15 second clear reference audio for cloning
- Avoid background noise in reference samples
- Try different seeds for custom voices

### Mojibake characters in output
- The system automatically fixes common encoding issues
- If problems persist, ensure your input text is UTF-8 encoded

## Development & Testing

Backend tests live in `tests/pipeline/` (pytest):

```bash
# Full backend suite (pipeline tests + guard suite)
pytest tests/pipeline -q

# With the app-level API tests (requires a live LLM server; environmental
# failures for missing TTS/LLM dependencies are expected)
pytest --import-mode=importlib app/test_api.py tests/pipeline -q
```

Frontend (in `frontend/`):

```bash
npm run build    # production build (outputs to ../app/static/dist)
npx tsc --noEmit # TypeScript type check

# Frontend unit tests — require vitest (installed in the pipeline-only
# cutover's final phase; not yet enabled if the install step was skipped)
npx vitest run
```

## Project Structure

```
Alexandria/
├── app/
│   ├── app.py                 # FastAPI server (config, voice_design, lora, dataset_builder, preparer)
│   ├── engine.py              # TTS engine factory (get_tts_engine / reset_tts_engine, module cache)
│   ├── tts.py                 # TTS engine (local + external backends)
│   ├── train_lora.py          # LoRA training subprocess script
│   ├── hf_utils.py            # Hugging Face model download utilities
│   ├── utils.py               # Shared utilities (LLM client, config resolution)
│   ├── pipeline/              # v3 Annotation Pipeline (SQLite-WAL, two-graph model)
│   │   ├── api.py             # Pipeline router entry point (includes sub-routers)
│   │   ├── api_onboard.py     # Onboard / re-onboard endpoints
│   │   ├── api_walks.py       # Walk execution + status endpoints
│   │   ├── api_operations.py  # Structural operations + span text editing
│   │   ├── api_review.py      # Confidence review endpoints
│   │   ├── api_export.py      # Export / render / merge / download endpoints
│   │   ├── api_characters.py  # Character ledger + voice assignment
│   │   ├── api_voices.py      # Voice catalog CRUD + preview
│   │   ├── adapter.py         # Pipeline adapter + schema init
│   │   ├── extract.py         # EPUB text extraction
│   │   ├── populate.py        # Spine population
│   │   ├── ledger.py          # Character ledger
│   │   ├── operations.py      # Operation executor (split/merge/move/delete)
│   │   ├── review.py          # Review manager (low-confidence items)
│   │   ├── assembly.py        # Script assembly + export
│   │   ├── tts_integration.py # TTS integration (render_audiobook)
│   │   ├── schema.py          # SQL schema + span_presentation VIEW
│   │   └── walks/             # 9-walk serial DAG (2a→2b→…→2i)
│   │       ├── walk_2a_scene_segmentation.py
│   │       ├── walk_2b_character_discovery.py
│   │       ├── walk_2c_alias_resolution.py
│   │       ├── walk_2d_scene_presence.py
│   │       ├── walk_2e_span_attribution.py
│   │       ├── walk_2f_character_description.py
│   │       ├── walk_2g_voice_audition.py
│   │       ├── walk_2h_voice_assignment.py
│   │       └── walk_2i_delivery.py
│   ├── config.json            # Runtime configuration (gitignored)
│   ├── static/index.html      # Web UI
│   ├── static/dist/           # Built frontend bundle (generated, never hand-edited)
│   └── requirements.txt       # Python dependencies
├── frontend/                  # Frontend source (TypeScript, bundled to app/static/dist)
│   └── src/tabs/              # script, voices, editor, setup, designer, preparer, dataset-builder, training
├── builtin_lora/              # Pre-trained LoRA voice presets
├── dataset_builder/           # Dataset builder project workspace (gitignored)
├── designed_voices/           # Saved Voice Designer outputs (gitignored)
├── lora_datasets/             # Uploaded/generated training datasets (gitignored)
├── lora_models/               # Trained LoRA adapters (gitignored)
├── data/                      # SQLite pipeline database (pipeline.db, gitignored)
├── install.js                 # Pinokio installer
├── start.js                   # Pinokio launcher
├── reset.js                   # Reset script
├── pinokio.js                 # Pinokio UI config
├── pinokio.json               # Pinokio metadata
└── README.md
```

## Acknowledgements

- [Ayush Naphade](https://github.com/aayushnaphade) — Persona generation, speaker alias resolution, and contextual script review ([PR #42](https://github.com/Finrandojin/alexandria-audiobook/pull/42)). Check out his project [Lily](https://lily.rayoneai.in/) — looking forward to seeing where it goes!
- [Michii](https://github.com/on22s) — System Health Dashboard with real-time GPU/disk monitoring ([PR #45](https://github.com/Finrandojin/alexandria-audiobook/pull/45)), cross-platform subprocess runner ([PR #46](https://github.com/Finrandojin/alexandria-audiobook/pull/46)), Voice Training Dataset Preparer tab ([PR #47](https://github.com/Finrandojin/alexandria-audiobook/pull/47))

## License

MIT

### Third-Party Licenses

- [qwen_tts](https://github.com/Qwen/Qwen3-TTS) — Apache License 2.0, Copyright Alibaba Qwen Team
