<p align="center">
  <img src="docs/screenshots/banner.png" alt="Alexandria Audiobook Generator" width="60%">
</p>

<h1 align="center">Alexandria 有声书生成器</h1>

<p align="center">
  [English](README.md) | 中文
</p>

> 新用户？从这里开始：阅读[快速入门](#快速入门)部分了解如何使用，或查看[安装](#安装)部分开始安装。

利用 AI 驱动的脚本标注和文本转语音技术，将任何书籍或小说转化为全配音有声书。内置 Qwen3-TTS 引擎，支持批量处理，并提供浏览器端编辑器，可逐行精调后导出。

[🎧 试听音频](docs/sample.mp3)

## 截图

<p align="center">
  <img src="docs/screenshots/1.png" width="45%">
  <img src="docs/screenshots/2.png" width="45%">
  <img src="docs/screenshots/3.png" width="45%">
  <img src="docs/screenshots/4.png" width="45%">
</p>

## 主要功能

### AI 驱动流水线
- **本地与云端 LLM 支持** - 使用本地 LLM 服务器（LM Studio、Ollama）或任何 OpenAI 兼容的云端 API
- **自动脚本标注** - 串行 9 步行走式 LLM 流水线（2a→2i）将书籍转换为带说话人、文本和 TTS 指令的 span 结构
- **置信度审校** - 自动接受高置信度结果，将低置信度项标出供人工审校（接受/拒绝/覆盖）
- **声音试听与分配** - 行走 2g/2h 自动为角色试听并分配声音
- **说话人别名** - 将多个说话人名称映射到同一声音（如 小林→林峰）

### 语音生成
- **内置 TTS 引擎** - 无需外部 TTS 服务器；Qwen3-TTS 权重自动下载（约 3.5 GB）
- **多语言支持** - 中文、英语、法语、德语、意大利语、日语、韩语、葡萄牙语、俄语、西班牙语，或自动检测
- **预置声音** - 9 个预先训练的声音，每个都支持指令情感/语气控制
- **声音克隆** - 使用 5-15 秒的参考音频克隆任何声音
- **声音设计器** - 用文字描述设计声音（Qwen3-TTS VoiceDesign 模型）
- **LoRA 声音训练** - 在声音设计器或克隆声音之上训练自定义 LoRA
- **内置 LoRA 预设** - 内置调优的 LoRA 预设
- **数据集构建器** - 为 LoRA 训练构建数据集
- **批量处理** - 批量合成音频，速度提升 3-6 倍
- **编解码器编译** - 使用 torch.compile 提升 3-4 倍速度
- **自然停顿** - 可配置的说话人切换暂停（默认 500 ms）与同说话人继续时的暂停（默认 250 ms），记录并传递至引擎，但当前引擎不会在合并音频中插入可听的静音

### Web UI 编辑器
- **简洁界面** - 核心流水线标签页（设置、脚本、声音、编辑器）加高级工具（设计器、预处理、数据集、训练）
- **跨度编辑器** - 编辑任意行的说话人、文本和指令
- **结构操作** - 在流水线编辑器中拆分、合并、移动或删除跨度
- **批量处理** - 优化批量渲染，子批处理充分利用 GPU
- **实时进度** - 所有操作的实时日志和状态跟踪

### 导出选项
- **M4B 有声书** - 带章节标记的 M4B（AAC），支持自动检测或逐块章节，适用于有声书播放器（Audiobookshelf、Apple Books、VLC 等）
- **原始语音块** - 以 ZIP 形式下载每行的 WAV/MP3 语音块

## 系统要求

- **Pinokio**（推荐）或手动安装
- **LLM 服务器** - 任意 OpenAI 兼容 API（LM Studio、Ollama、OpenAI、Together、Groq、DeepSeek 等）
- **GPU** - 最低 8GB 显存；推荐 16GB+（每个 TTS 模型约 3.4GB）
- **内存** - 推荐 16GB，最低 8GB
- **磁盘空间** - 约 20GB（虚拟环境 8GB + 模型权重 7GB + 工作区文件）

### GPU 兼容性

| 操作系统 | GPU | 兼容性 |
|---------|-----|--------|
| Windows | NVIDIA | ✅ 完整支持（CUDA 12.8、Flash Attention） |
| Linux | NVIDIA | ✅ 完整支持（CUDA 12.8、Flash Attention） |
| Linux | AMD | ✅ 完整支持（ROCm 6.3+） |
| Windows | AMD | ⚠️ 仅 CPU |
| macOS | Apple Silicon | ⚠️ 仅 CPU（不支持 MPS） |

> 无需外部 TTS 服务器。Qwen3-TTS 内置，首次生成时自动下载权重（约 3.5 GB/变体）。

## 安装

### 方式 A：Pinokio（推荐）

1. 安装 [Pinokio](https://pinokio.computer)
2. 进入 **Discover** 并搜索 "alexandria"
3. 点击 **Install** 并等待完成

### 方式 B：Google Colab

免费 T4 GPU，无需安装：

1. 打开 [Alexandria 有声书生成器 - Colab](https://colab.research.google.com/github/lazdavila/alexandria-audiobook/blob/main/colab.ipynb)
2. 点击 **Connect** → **Runtime Type** → **T4 GPU**
3. 点击 **Run All** 并按照笔记本中的说明设置 ngrok 公共 URL

## 首次启动 — 预期情况

### 1. 必须先启动 LLM 服务器

Alexandria 需要运行中的 LLM 服务器来生成脚本。默认端点：

| 服务器 | URL | 默认模型 |
|--------|-----|---------|
| LM Studio | `http://localhost:1234/v1` | 任意已加载模型 |
| Ollama | `http://localhost:11434/v1` | 任意已拉取模型 |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o` |

在 Setup 标签页中设置 Base URL 和 API Key，点击 **Save Configuration**。脚本生成时若未检测到服务器，会显示错误。

### 2. 首次 TTS 下载

首次生成音频时会自动下载 TTS 模型权重（每个变体约 3.5 GB），并在后台缓存。下载速度取决于网速。可通过 Pinokio 终端查看进度。

> **中国大陆用户：** 如 HuggingFace 下载缓慢，设置环境变量 `HF_ENDPOINT=https://hf-mirror.com`（可选 `HF_TOKEN`）后重新启动。

### 3. 首批生成预热

第一批批量生成比后续批次慢：
- **AMD GPU** - MIOpen 自动调优需要 30-60 秒（一次性）
- **Codec 编译** - torch.compile 首次运行时编译（一次性）

### 4. 显存决定

| 显存 | 推荐并行批处理大小 |
|------|------------------|
| 8GB | 小块（每次 2-5 块） |
| 16GB | 中等批次（10-20 块） |
| 24GB+ | 大块（40-60 块）+ Codec 编译 |

如果显存不足，请降低 Setup 标签页中的 **Parallel Workers**。

### 5. 出问题去哪看

所有日志（TTS、LLM 调用、错误）都打印到 **Pinokio 终端**。

## 快速入门

界面分为**核心流水线标签页**（绿色标签页）和**高级工具**（蓝色标签页）。

### 第 1 步：设置
配置 LLM 端点（Base URL、API Key、Model Name），TTS 模式设为 `local`，然后点击 **Save Configuration**。

### 第 2 步：脚本
在脚本标签页中上传 `.epub` 文件（仅支持 EPUB，服务端自动转换为纯文本）。点击 **Onboard Book** 导入书籍，然后点击 **Run All Walks** 运行完整的 9 步标注流水线（2a→2i）。行走进度显示在标签页中；任何行走均可取消或单独重新运行。如需重新导入（例如替换源文件），使用 **Re-onboard**。

### 第 3 步：声音
在声音标签页中，为每个检测到的角色从声音目录中选择声音。声音类型包括 Custom（9 个预置）、Clone（参考音频）、LoRA（训练适配器）和 Voice Design（文字描述）。使用**说话人别名**（Alias of 下拉菜单）将多个说话人名称映射到同一声音。更改通过流水线 API 自动保存。

### 第 4 步：编辑器
点击 **Render** 批量生成所有待处理 span 的音频，并实时显示进度。渲染完成后，可内联编辑任何 span（说话人/文本/指令）或使用结构操作拆分/合并/移动/删除跨度，然后点击 **Merge** 合成最终有声书，最后 **Download** 下载 M4B 文件。低置信度项目出现在审校列表中，可逐项接受、拒绝或覆盖。

### 高级工具

- **声音设计器** - 用文字描述设计自定义声音并预览
- **预处理** - 上传和准备 LoRA 训练数据集
- **数据集** - 构建带人工标注样本的 LoRA 训练数据集
- **训练** - 在声音设计器或克隆声音之上训练自定义 LoRA

## Web 界面

### 设置标签页

**LLM 设置：** Base URL、API Key、Model Name、Reasoning Effort、Temperature。

**TTS 设置：**
| 设置 | 说明 |
|------|------|
| TTS Mode | `local`（内置）或 `external`（远程 Gradio 服务器） |
| Device | `auto`、`cuda`、`cpu`、`mps` |
| Language | 语音语言（`auto` 检测或指定） |
| Parallel Workers | 并行合成的工作线程数（1-10） |
| Batch Seed | 批处理的随机种子（-1 = 随机） |
| Compile Codec | 使用 torch.compile 编译解码器（3-4 倍速度提升） |
| Batch Group by Type | 按声音类型分组批处理 |
| Sub-batching | 将大批拆分为子批次以节省显存 |
| Min Sub-batch Size | 最小子批大小（默认 4） |
| Length Ratio | 子批长度比例（默认 5） |
| Max Sub-batch Items | 单个子批次的最大项数 |
| Speaker Change Pause | 说话人切换暂停（记录并传递至引擎，但当前引擎不会在合并音频中插入可听的静音；默认 500 ms） |
| Same Speaker Pause | 同说话人继续时的暂停（记录并传递至引擎，但当前引擎不会在合并音频中插入可听的静音；默认 250 ms） |

### 脚本标签页

1. 上传 `.epub` 文件（仅支持 EPUB，服务端自动转换为纯文本）
2. 点击 **Onboard Book** - 从 EPUB 提取文本并建立书籍结构
3. 点击 **Run All Walks** - 运行完整标注流水线（2a→2i）
4. 行走状态显示在标签页中；任何行走均可取消，可单独重新运行，全部完成后进入编辑器审校

**Re-onboard** - 重新导入书籍（替换源文件并重建结构）。

### 声音标签页

- 角色列表（来自行走 2b 的角色发现）显示在左侧，每个角色带声音分配下拉菜单
- 点击分配下拉菜单中的声音名称，立即通过 `PUT /api/pipeline/characters/{id}/voice` 保存
- 声音目录管理：创建、编辑、删除声音；点击 **Preview** 播放声音预览
- **说话人别名：** 将多个说话人名称映射到同一声音（如 小林→林峰），自动进行传递解析和循环检测

### 声音设计器标签页

1. 描述要设计的声音（如 "低沉、洪亮的男声，带轻微回音"）
2. 点击 **Generate** - 声音由 Qwen3-TTS VoiceDesign 模型生成
3. 点击 **Preview** 试听，满意后 **Save to Library** 保存到声音库

### 训练标签页

**数据集：**
- **上传数据集** - 上传 ZIP 压缩的 WAV 文件（24kHz 单声道）和 `metadata.jsonl`
- **生成数据集** - 自动从克隆/设计声音生成训练数据集
- **数据集构建器** - 构建带人工标注样本的 LoRA 训练数据集

**训练配置：** 适配器名称、训练轮数、学习率、LoRA Rank、LoRA Alpha、语言、批量大小（+ 梯度累积）。建议从 15-30 轮开始，观察损失曲线。

### 数据集构建器标签页

1. 创建项目，包含声音描述和可选的全局种子
2. 添加并编辑样本（多行文本），批量生成音频
3. 预览生成的音频，取消整个批次，保存为数据集
4. 保存的数据集可在训练标签页中使用

### 编辑器标签页

- 显示所有 span 的表格，带状态指示器（TTS 进度）和置信度标签
- 内联编辑任意 span（说话人/文本/指令）
- 结构操作：拆分、合并、移动、删除 span
- **Render** - 批量渲染所有待处理 span，实时进度轮询
- 置信度审校：接受/拒绝/覆盖低置信度项
- **Merge** - 将渲染的音频块合并为单个 M4B 文件
- **Download** - 下载合并的 M4B 有声书

## 性能

### 推荐设置

| 设置 | 建议 |
|------|------|
| TTS Mode | `local` |
| Compile Codec | `true` |
| Parallel Workers | 20-60（显存允许时） |

### 基准测试

**AMD RX 7900 XTX（24GB）** 使用 `rocm` 栈：

| 模式 | 速度 |
|------|------|
| 标准 | ~1x |
| 批量（无 codec） | ~2x |
| 批量 + codec | 3-6x |

> 273 块的有声书（约 54 分钟音频）批量 + codec 约 16 分钟。

### ROCm AMD GPU 说明（Linux）

- 使用 `bfloat16` 以获得最佳性能和稳定性（AMD 不支持 float16）
- 首次生成时自动运行 MIOpen fast-find 优化
- **ROCm 7.x 降频问题：** 如生成速度缓慢，每次开机后运行一次：
  ```bash
  echo 5 | sudo tee /sys/class/drm/card1/device/pp_power_profile_mode
  ```
- 验证计算内核正在运行：`COMPUTE=ON`

## 脚本格式

流水线的标注行走输出结构化 JSON，其中包含 span，每个 span 具有：

- `speaker`（必需）- 说话人名称
- `text`（必需）- 对话文本
- `instruct`（可选）- TTS 的 2-3 句语音指令

示例：

```json
{
  "speaker": "NARRATOR",
  "text": "The morning sun crept across the dusty windowsill.",
  "instruct": "Soft, hushed tone; slow and contemplative."
}
```

## 输出文件

### M4B 有声书（推荐）

最终有声书为带章节标记的 `audiobook.m4b`（AAC 128kbps）。章节自动检测（基于脚本标题）或按块生成。下载后兼容 Audiobookshelf、Apple Books、VLC 等播放器。

### 原始语音块

每个渲染作业在作业目录中生成 `chunk_0000.wav`、`chunk_0001.wav` 等文件（按时间顺序编号）。下载端点服务 `audiobook.m4b`（合并后）或 `audiobook.zip`（未合并的语音块，ZIP 打包）。

## API 参考

Alexandria 使用 **Pipeline API**（`/api/pipeline/*`，25 个端点）作为唯一的脚本/声音/渲染界面。所有示例假设服务器运行在 `http://127.0.0.1:4200`。

### 配置

**获取当前配置：**
```bash
curl http://127.0.0.1:4200/api/config
```

**更新配置：**
```bash
curl -X POST http://127.0.0.1:4200/api/config \
  -H "Content-Type: application/json" \
  -d '{
    "llm": {
      "base_url": "http://localhost:1234/v1",
      "api_key": "your-api-key",
      "model_name": "qwen3-14b",
      "task_overrides": {}
    },
    "tts": {
      "mode": "local",
      "device": "auto",
      "language": "auto",
      "parallel_workers": 20,
      "batch_seed": -1,
      "compile_codec": true,
      "sub_batch_enabled": true,
      "sub_batch_min_size": 4,
      "sub_batch_ratio": 5,
      "pause_between_speakers_ms": 500,
      "pause_same_speaker_ms": 250
    }
  }'
```

### 流水线（脚本生成）

```bash
# 导入书籍
curl -X POST http://127.0.0.1:4200/api/pipeline/onboard \
  -F "file=@mybook.epub"

# 运行所有标注行走（2a→2i）
curl -X POST http://127.0.0.1:4200/api/pipeline/run_all_walks \
  -H "Content-Type: application/json" \
  -d '{"book_id": "123"}'

# 检查行走进度
curl http://127.0.0.1:4200/api/pipeline/walk_status/123

# 获取审校项
curl http://127.0.0.1:4200/api/pipeline/review/123

# 接受审校项
curl -X POST http://127.0.0.1:4200/api/pipeline/review/accept \
  -H "Content-Type: application/json" \
  -d '{"item_id": "..."}'
```

### 声音目录

```bash
# 列出所有声音
curl http://127.0.0.1:4200/api/pipeline/voices

# 创建自定义声音
curl -X POST http://127.0.0.1:4200/api/pipeline/voices \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Marcus",
    "type": "custom",
    "voice": "Ryan",
    "character_style": "Deep, authoritative"
  }'

# 预览声音
curl -X POST http://127.0.0.1:4200/api/pipeline/voices/<voice_id>/preview \
  -H "Content-Type: application/json" \
  -d '{"sample_text": "Hello world"}'

# 为角色分配声音
curl -X PUT http://127.0.0.1:4200/api/pipeline/characters/<character_id>/voice \
  -H "Content-Type: application/json" \
  -d '{"voice_assignment_id": "marcus"}'
```

### 角色账本

```bash
# 获取角色的全部角色及其分配
curl http://127.0.0.1:4200/api/pipeline/characters/123
```

### 渲染与下载

```bash
# 渲染有声书（批处理）
curl -X POST http://127.0.0.1:4200/api/pipeline/render \
  -H "Content-Type: application/json" \
  -d '{"book_id": "123", "use_batch": true}'
# → {"job_id": "abc", "status": "started"}

# 轮询渲染状态
curl http://127.0.0.1:4200/api/pipeline/render_status/abc

# 将语音块合并为 M4B
curl -X POST http://127.0.0.1:4200/api/pipeline/merge \
  -H "Content-Type: application/json" \
  -d '{"job_id": "abc"}'

# 下载合并的有声书
curl -L http://127.0.0.1:4200/api/pipeline/download/abc -o audiobook.m4b
```

### 声音设计器

```bash
# 预览声音设计
curl -X POST http://127.0.0.1:4200/api/voice_design/preview \
  -H "Content-Type: application/json" \
  -d '{"description": "Deep, resonant male voice"}'

# 保存到声音库
curl -X POST http://127.0.0.1:4200/api/voice_design/save \
  -H "Content-Type: application/json" \
  -d '{"name": "Marcus", "description": "Deep, resonant male voice"}'
```

### LoRA 训练

```bash
# 上传训练数据集（ZIP）
curl -X POST http://127.0.0.1:4200/api/lora/upload_dataset \
  -F "file=@dataset.zip" -F "name=my_dataset"

# 生成数据集
curl -X POST http://127.0.0.1:4200/api/lora/generate_dataset \
  -H "Content-Type: application/json" \
  -d '{"voice_id": "...", "lines": ["..."], "name": "my_dataset"}'

# 启动训练
curl -X POST http://127.0.0.1:4200/api/lora/train \
  -H "Content-Type: application/json" \
  -d '{"name": "my-lora", "dataset_id": "...", "epochs": 15, "learning_rate": 5e-6, "lora_r": 64, "lora_alpha": 128}'

# 列出训练好的模型
curl http://127.0.0.1:4200/api/lora/models
```

### 数据集构建器

```bash
# 列出项目
curl http://127.0.0.1:4200/api/dataset_builder/list

# 创建项目
curl -X POST http://127.0.0.1:4200/api/dataset_builder/create \
  -H "Content-Type: application/json" \
  -d '{"name": "marcus-dataset", "description": "...", "seed": -1}'

# 生成样本
curl -X POST http://127.0.0.1:4200/api/dataset_builder/generate_sample \
  -H "Content-Type: application/json" \
  -d '{"project_id": "...", "rows": [{"text": "Hello world"}]}'

# 保存项目为数据集
curl -X POST http://127.0.0.1:4200/api/dataset_builder/save \
  -H "Content-Type: application/json" \
  -d '{"project_id": "..."}'
```

## Python 集成

```python
import requests

BASE = "http://127.0.0.1:4200"

# 1. 导入书籍
with open("mybook.epub", "rb") as f:
    r = requests.post(f"{BASE}/api/pipeline/onboard", files={"file": f})
book_id = r.json()["book_id"]

# 2. 运行所有标注行走
requests.post(f"{BASE}/api/pipeline/run_all_walks", json={"book_id": book_id})

# 3. 等待行走完成
while True:
    status = requests.get(f"{BASE}/api/pipeline/walk_status/{book_id}").json()
    if all(w.get("status") == "completed" for w in status.values()):
        break
    time.sleep(2)

# 4.（可选）为角色分配声音
characters = requests.get(f"{BASE}/api/pipeline/characters/{book_id}").json()
for c in characters:
    requests.put(f"{BASE}/api/pipeline/characters/{c['id']}/voice",
                 json={"voice_assignment_id": "marcus"})

# 5. 渲染有声书
job = requests.post(f"{BASE}/api/pipeline/render",
                    json={"book_id": book_id, "use_batch": True}).json()
job_id = job["job_id"]

# 6. 轮询渲染状态
while True:
    st = requests.get(f"{BASE}/api/pipeline/render_status/{job_id}").json()
    if st["status"] == "completed":
        break
    time.sleep(2)

# 7. 合并并下载 M4B
requests.post(f"{BASE}/api/pipeline/merge", json={"job_id": job_id})
r = requests.get(f"{BASE}/api/pipeline/download/{job_id}")
with open("audiobook.m4b", "wb") as f:
    f.write(r.content)
```

## JavaScript 集成

```javascript
const BASE = "http://127.0.0.1:4200";

// 1. 导入书籍
const form = new FormData();
form.append("file", fileInput.files[0]);
const onboard = await fetch(`${BASE}/api/pipeline/onboard`, {
  method: "POST",
  body: form,
}).then((r) => r.json());
const bookId = onboard.book_id;

// 2. 运行所有标注行走
await fetch(`${BASE}/api/pipeline/run_all_walks`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ book_id: bookId }),
});

// 3. 等待行走完成
async function waitForWalks() {
  while (true) {
    const status = await fetch(`${BASE}/api/pipeline/walk_status/${bookId}`).then((r) => r.json());
    if (Object.values(status).every((w) => w.status === "completed")) break;
    await new Promise((r) => setTimeout(r, 2000));
  }
}
await waitForWalks();

// 4. 渲染
const { job_id: jobId } = await fetch(`${BASE}/api/pipeline/render`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ book_id: bookId, use_batch: true }),
}).then((r) => r.json());

// 5. 等待渲染完成
while (true) {
  const st = await fetch(`${BASE}/api/pipeline/render_status/${jobId}`).then((r) => r.json());
  if (st.status === "completed") break;
  await new Promise((r) => setTimeout(r, 2000));
}

// 6. 合并并下载
await fetch(`${BASE}/api/pipeline/merge`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ job_id: jobId }),
});
window.location.href = `${BASE}/api/pipeline/download/${jobId}`;
```

## 推荐 LLM 模型

| 模型 | 推荐用途 | 说明 |
|------|---------|------|
| Qwen3-next 80B-A3B-instruct | 最佳质量 | MoE，与 Alexandria 同款（Qwen 系列） |
| Gemma3 27B | 最佳开源 | 高质量标注 |
| Qwen2.5 | 推荐 | 广泛兼容 |
| Qwen3（非思维链） | 推荐 | 快速，质量高 |
| Llama 3.1 / 3.2 | 可选 | 质量不一 |
| Mistral / Mixtral | 可选 | 质量不一 |

**思维链模型**（DeepSeek-R1、GLM4-air 等）可能干扰 JSON 输出。如必须使用，建议选用非思维链变体或单独的端点用于标注行走。

## 常见问题

### 脚本生成失败

- 确认 LLM 服务器正在运行且可访问
- 确认 Setup 标签页中的模型名称正确
- 脚本生成日志会打印 JSON 解析错误（在 Pinokio 终端中查看）

### 模型下载失败或缓慢

中国大陆用户可设置 HuggingFace 镜像以加快下载：
```bash
export HF_ENDPOINT=https://hf-mirror.com
```
可选：`export HF_TOKEN=your_token` 用于 gated 模型。下载支持断点续传。

### TTS 生成失败

- 检查 Pinokio 终端中的错误信息
- 显存不足：确保 GPU 至少有 16GB（或使用 CPU 模式 `device: cpu`）
- 若使用外部 Gradio TTS 服务器，确认其正在运行
- 确认每个角色在声音标签页中都分配了有效的声音（声音配置存储在流水线的 `voice_config` 表中）
- 克隆声音：使用 5-15 秒清晰、干净的参考音频

### 生成速度慢

- 在 Setup 标签页中启用 **Compile Codec**
- 增加 **Parallel Workers**（如显存允许）
- 批量渲染会自动进行（`use_batch: true`）

### 显存不足 / OOM

- 降低 **Parallel Workers**（如 5-10）
- 使用 `device: cpu`（较慢但可运行）
- 减小子批次大小（Sub-batching）

### 音频损坏 / 输出文件很小（428 字节）

通常与 `ffmpeg`/`libmp3lame` 缺失或损坏有关：

```bash
# 如果使用 Pinokio conda 环境
conda install -c conda-forge ffmpeg
# 或
pip install imageio-ffmpeg
```

### 中文书籍处理提示

1. 将 TTS **Language** 设置为 `Chinese` 或 `Auto`
2. 在声音标签页中为每个角色分配中文声音
3. 标注行走内置中文对话约定（无需修改任何提示文件）

## 更多文档

- **[English README](README.md)** - 完整的英文文档，包含更多 API 参考和项目结构
- **Wiki** - 查看[文档](https://github.com/lazdavila/alexandria-audiobook/wiki)获取更多信息

## 致谢

- [Ayush Naphade](https://github.com/ayushnaphade) - PR#42 角色生成、说话人别名、上下文脚本审校（→ Lily）
- [Michii](https://github.com/michii) - PR#45 系统健康仪表板

## 许可证

[MIT](LICENSE)
