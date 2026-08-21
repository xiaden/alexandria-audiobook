/**
 * Training tab module — LoRA training UI (dataset upload, training, model management)
 * Ported from app/static/index.html lines 3214-3517 (JS logic)
 * HTML: lines 694-877 (training-tab)
 */

import * as API from '../api';
import { state, LoraModel } from '../state';
import { showToast, showConfirm, escapeHtml } from '../utils';

/** Module-level state for training tab */
let loraPoller: ReturnType<typeof setInterval> | null = null;

/** LoRA dataset from /api/lora/datasets */
interface LoraDataset {
  dataset_id: string;
  sample_count: number;
}

/**
 * Load the list of LoRA training datasets and populate the dropdown + list.
 * Fetches GET /api/lora/datasets and renders both the select dropdown and
 * the dataset list with delete buttons.
 */
async function loadLoraDatasets(): Promise<void> {
  try {
    const datasets = await API.get<LoraDataset[]>('/api/lora/datasets');
    const listEl = document.getElementById('lora-datasets-list');
    const selectEl = document.getElementById('lora-dataset-select') as HTMLSelectElement;
    if (!listEl || !selectEl) return;

    // Update dropdown
    const currentVal = selectEl.value;
    selectEl.innerHTML = '<option value="">-- Select dataset --</option>' +
      datasets.map(d => `<option value="${escapeHtml(d.dataset_id)}">${escapeHtml(d.dataset_id)} (${d.sample_count} samples)</option>`).join('');
    if (currentVal) selectEl.value = currentVal;

    // Update list
    if (!datasets.length) {
      listEl.innerHTML = '<span class="text-muted">No datasets uploaded yet.</span>';
      return;
    }
    listEl.innerHTML = datasets.map(d => `
      <div class="d-flex justify-content-between align-items-center py-1">
        <span><strong>${escapeHtml(d.dataset_id)}</strong> <small class="text-muted">(${d.sample_count} samples)</small></span>
        <button class="btn btn-sm btn-outline-danger" data-action="delete-dataset" data-id="${escapeHtml(d.dataset_id)}"><i class="fas fa-trash"></i></button>
      </div>
    `).join('');
  } catch (e) {
    console.error('Failed to load LoRA datasets:', e);
  }
}

/**
 * Handle LoRA dataset ZIP upload.
 * Reads #lora-dataset-file input, validates .zip extension, uploads via POST /api/lora/upload_dataset.
 */
async function uploadLoraDataset(): Promise<void> {
  const fileInput = document.getElementById('lora-dataset-file') as HTMLInputElement;
  if (!fileInput || !fileInput.files?.length) {
    showToast('Select a ZIP file first.', 'warning');
    return;
  }

  const file = fileInput.files[0];
  if (!file.name.endsWith('.zip')) {
    showToast('File must be a .zip archive.', 'warning');
    return;
  }

  const formData = new FormData();
  formData.append('file', file);

  try {
    const res = await fetch('/api/lora/upload_dataset', { method: 'POST', body: formData });
    if (!res.ok) {
      const err = await res.json();
      showToast(err.detail || 'Upload failed.', 'error');
      return;
    }
    const result = await res.json();
    showToast(`Dataset "${result.dataset_id}" uploaded (${result.sample_count} samples).`, 'success');
    fileInput.value = '';
    loadLoraDatasets();
  } catch (e) {
    showToast('Upload error: ' + (e as Error).message, 'error');
  }
}

/**
 * Delete a LoRA dataset by ID.
 * Uses raw fetch DELETE to /api/lora/datasets/{datasetId}.
 */
async function deleteLoraDataset(datasetId: string): Promise<void> {
  if (!await showConfirm(`Delete dataset "${datasetId}"?`)) return;
  try {
    const res = await fetch(`/api/lora/datasets/${encodeURIComponent(datasetId)}`, { method: 'DELETE' });
    if (!res.ok) {
      const err = await res.json();
      showToast(err.detail || 'Failed to delete.', 'error');
      return;
    }
    loadLoraDatasets();
  } catch (e) {
    showToast('Error deleting dataset: ' + (e as Error).message, 'error');
  }
}

/**
 * Start LoRA training with the configured parameters.
 * Reads form fields (adapter name, dataset, epochs, lr, batch size, rank, alpha, grad accum, language),
 * POSTs to /api/lora/train, then starts polling for training progress.
 */
async function startLoraTraining(): Promise<void> {
  const name = (document.getElementById('lora-adapter-name') as HTMLInputElement)?.value.trim();
  const datasetId = (document.getElementById('lora-dataset-select') as HTMLSelectElement)?.value;
  if (!name) { showToast('Enter an adapter name.', 'warning'); return; }
  if (!datasetId) { showToast('Select a dataset.', 'warning'); return; }

  const request = {
    name: name,
    dataset_id: datasetId,
    epochs: parseInt((document.getElementById('lora-epochs') as HTMLInputElement)?.value) || 5,
    lr: parseFloat((document.getElementById('lora-lr') as HTMLInputElement)?.value) || 5e-6,
    batch_size: parseInt((document.getElementById('lora-batch-size') as HTMLInputElement)?.value) || 1,
    lora_r: parseInt((document.getElementById('lora-rank') as HTMLInputElement)?.value) || 32,
    lora_alpha: parseInt((document.getElementById('lora-alpha') as HTMLInputElement)?.value) || 128,
    gradient_accumulation_steps: parseInt((document.getElementById('lora-grad-accum') as HTMLInputElement)?.value) || 8,
    language: (document.getElementById('lora-language') as HTMLSelectElement)?.value || 'english'
  };

  const btn = document.getElementById('btn-lora-train') as HTMLButtonElement;
  const statusEl = document.getElementById('lora-train-status');
  if (btn) btn.disabled = true;
  if (statusEl) statusEl.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i>Starting...';

  try {
    await API.post('/api/lora/train', request);
    const progressSection = document.getElementById('lora-progress-section');
    if (progressSection) progressSection.style.display = 'block';
    if (statusEl) statusEl.innerHTML = '<span class="text-info">Training in progress...</span>';
    pollLoraTraining();
  } catch (e) {
    showToast('Failed to start training: ' + (e as Error).message, 'error');
    if (btn) btn.disabled = false;
    if (statusEl) statusEl.innerHTML = '';
  }
}

/**
 * Poll LoRA training status every 2 seconds.
 * Fetches GET /api/lora/status, updates progress bar, epoch/loss displays, and logs.
 * Stops polling when training is no longer running.
 */
function pollLoraTraining(): void {
  const logsEl = document.getElementById('lora-train-logs');
  const progressBar = document.getElementById('lora-progress-bar');
  const epochDisplay = document.getElementById('lora-epoch-display');
  const lossDisplay = document.getElementById('lora-loss-display');
  if (!logsEl || !progressBar || !epochDisplay || !lossDisplay) return;

  if (loraPoller) clearInterval(loraPoller);

  loraPoller = setInterval(async () => {
    try {
      const status = await API.get<{
        logs: string[];
        running: boolean;
        status: 'idle' | 'running' | 'succeeded' | 'failed' | 'cancelled';
      }>('/api/lora/status');
      logsEl.innerText = status.logs.join('\n');
      logsEl.scrollTop = logsEl.scrollHeight;

      // Parse latest metrics from log lines
      for (let i = status.logs.length - 1; i >= 0; i--) {
        const line = status.logs[i];
        const epochMatch = line.match(/\[EPOCH\]\s*(\d+)\/(\d+)\s+avg_loss=([\d.]+)/);
        if (epochMatch) {
          const epoch = parseInt(epochMatch[1]);
          const maxEpoch = parseInt(epochMatch[2]);
          const loss = epochMatch[3];
          const pct = Math.round((epoch / maxEpoch) * 100);
          epochDisplay.innerText = `${epoch}/${maxEpoch}`;
          lossDisplay.innerText = loss;
          progressBar.style.width = `${pct}%`;
          progressBar.innerText = `${pct}%`;
          break;
        }
        const trainMatch = line.match(/\[TRAIN\]\s*epoch=(\d+)\/(\d+)\s+step=\d+\/\d+\s+loss=([\d.]+)/);
        if (trainMatch) {
          const epoch = parseInt(trainMatch[1]);
          const maxEpoch = parseInt(trainMatch[2]);
          const loss = trainMatch[3];
          const pct = Math.round(((epoch - 1) / maxEpoch) * 100);
          epochDisplay.innerText = `${epoch}/${maxEpoch}`;
          lossDisplay.innerText = loss;
          progressBar.style.width = `${pct}%`;
          progressBar.innerText = `${pct}%`;
          break;
        }
      }

      if (!status.running) {
        if (loraPoller) clearInterval(loraPoller);
        loraPoller = null;
        const btn = document.getElementById('btn-lora-train') as HTMLButtonElement;
        const statusEl = document.getElementById('lora-train-status');
        if (btn) btn.disabled = false;

        // The backend status is authoritative; log markers are only a
        // compatibility fallback for older server responses.
        const hasTerminalStatus = ['succeeded', 'failed', 'cancelled'].includes(status.status);
        const isDone = hasTerminalStatus
          ? status.status === 'succeeded'
          : status.logs.some(l => l.includes('[DONE]'));
        const isError = hasTerminalStatus
          ? status.status === 'failed'
          : status.logs.some(l => l.includes('[ERROR]'));

        if (isDone) {
          if (statusEl) statusEl.innerHTML = '<span class="text-success"><i class="fas fa-check me-1"></i>Training complete!</span>';
          progressBar.style.width = '100%';
          progressBar.innerText = '100%';
          progressBar.classList.remove('progress-bar-animated');
          progressBar.classList.replace('bg-info', 'bg-success');
          loadLoraModels();
        } else if (isError) {
          if (statusEl) statusEl.innerHTML = '<span class="text-danger"><i class="fas fa-times me-1"></i>Training failed</span>';
          progressBar.classList.remove('progress-bar-animated');
          progressBar.classList.replace('bg-info', 'bg-danger');
        } else {
          if (statusEl) statusEl.innerHTML = '<span class="text-warning">Training stopped</span>';
        }
      }
    } catch (e) {
      console.error('LoRA poll error:', e);
      if (loraPoller) clearInterval(loraPoller);
      loraPoller = null;
    }
  }, 2000);
}

/**
 * Load the list of trained LoRA models and render the models table.
 * Fetches GET /api/lora/models, updates state.loraModels cache, renders table with
 * action buttons (preview, test, delete, download), and populates the test adapter dropdown.
 */
async function loadLoraModels(): Promise<void> {
  try {
    const models = await API.get<LoraModel[]>('/api/lora/models');
    state.loraModels = models;
    const container = document.getElementById('lora-models-list');
    const testForm = document.getElementById('lora-test-form');
    if (!container || !testForm) return;

    if (!models.length) {
      container.innerHTML = '<p class="text-muted mb-0">No adapters available.</p>';
      testForm.style.display = 'none';
      return;
    }

    container.innerHTML = `
      <table class="table table-sm table-hover mb-0">
        <thead><tr><th>Name</th><th>Dataset</th><th>Epochs</th><th>Final Loss</th><th>Samples</th><th style="width:240px">Actions</th></tr></thead>
        <tbody>
          ${models.map(m => `
            <tr${m.builtin ? ' class="table-light"' : ''}>
              <td><strong>${escapeHtml(m.name)}</strong>${m.builtin ? ` <span class="badge bg-secondary">built-in</span>${m.downloaded === false ? ' <span class="badge bg-warning text-dark">not downloaded</span>' : ''}` : ''}</td>
              <td>${escapeHtml(m.dataset_id || (m.builtin ? '--' : '--'))}</td>
              <td>${m.epochs || '--'}</td>
              <td>${m.final_loss != null ? m.final_loss.toFixed(4) : '--'}</td>
              <td>${m.sample_count || '--'}</td>
              <td>
                ${m.builtin && m.downloaded === false ? `
                  <button class="btn btn-sm btn-outline-warning" data-action="download-adapter" data-id="${escapeHtml(m.id)}" title="Download from HuggingFace"><i class="fas fa-download me-1"></i>Download</button>
                ` : `
                  <button class="btn btn-sm ${m.preview_audio_url ? 'btn-outline-success' : 'btn-outline-secondary'} me-1" data-action="play-preview" data-id="${escapeHtml(m.id)}" id="lora-preview-btn-${escapeHtml(m.id)}" title="${m.preview_audio_url ? 'Play preview' : 'Generate and play preview (first time may take a moment)'}"><i class="fas fa-volume-up"></i></button>
                  <button class="btn btn-sm btn-outline-primary me-1" data-action="test-model" data-id="${escapeHtml(m.id)}" title="Generate test line with custom text"><i class="fas fa-flask me-1"></i>Test</button>
                  ${m.builtin ? '' : `<button class="btn btn-sm btn-outline-danger" data-action="delete-model" data-id="${escapeHtml(m.id)}" title="Delete"><i class="fas fa-trash"></i></button>`}
                `}
              </td>
            </tr>
          `).join('')}
        </tbody>
      </table>`;

    // Populate test dropdown
    const dropdown = document.getElementById('lora-test-adapter') as HTMLSelectElement;
    if (dropdown) {
      const prevVal = dropdown.value;
      dropdown.innerHTML = models.filter(m => m.downloaded !== false).map(m =>
        `<option value="${escapeHtml(m.id)}">${escapeHtml(m.name)}</option>`
      ).join('');
      if (prevVal && models.some(m => m.id === prevVal)) dropdown.value = prevVal;
    }
    testForm.style.display = '';
  } catch (e) {
    console.error('Failed to load LoRA models:', e);
  }
}

/**
 * Play or generate a preview audio for a LoRA adapter.
 * POSTs to /api/lora/preview/{adapterId}, plays the returned audio, and updates the button style.
 */
async function playLoraPreview(adapterId: string): Promise<void> {
  const btn = document.getElementById(`lora-preview-btn-${adapterId}`) as HTMLButtonElement;
  if (!btn) return;
  const origHtml = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';

  try {
    const result = await API.post<{ audio_url: string }>(`/api/lora/preview/${encodeURIComponent(adapterId)}`, {});
    const audio = new Audio(`${result.audio_url}?t=${Date.now()}`);
    audio.play();
    // Update button now that preview is cached
    btn.title = 'Play preview';
    btn.classList.replace('btn-outline-secondary', 'btn-outline-success');
  } catch (e) {
    showToast('Preview failed: ' + (e as Error).message, 'error');
  } finally {
    btn.disabled = false;
    btn.innerHTML = origHtml;
  }
}

/**
 * Select a LoRA adapter for testing — populate the test form and focus the text input.
 */
function testLoraModel(adapterId: string): void {
  const dropdown = document.getElementById('lora-test-adapter') as HTMLSelectElement;
  const testForm = document.getElementById('lora-test-form');
  const textInput = document.getElementById('lora-test-text') as HTMLInputElement;
  if (dropdown) dropdown.value = adapterId;
  if (testForm) testForm.style.display = '';
  if (textInput) textInput.focus();
}

/**
 * Run a test generation with the selected LoRA adapter.
 * Reads adapter ID, text, and instruct from the test form, POSTs to /api/lora/test,
 * and plays the returned audio.
 */
async function runLoraTest(): Promise<void> {
  const adapterId = (document.getElementById('lora-test-adapter') as HTMLSelectElement)?.value;
  const text = (document.getElementById('lora-test-text') as HTMLInputElement)?.value.trim();
  const instruct = (document.getElementById('lora-test-instruct') as HTMLInputElement)?.value.trim();
  if (!adapterId) { showToast('Select an adapter.', 'warning'); return; }
  if (!text) { showToast('Enter text to synthesize.', 'warning'); return; }

  const statusEl = document.getElementById('lora-test-status');
  if (statusEl) statusEl.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i>Generating...';

  try {
    const result = await API.post<{ audio_url: string }>('/api/lora/test', {
      adapter_id: adapterId,
      text: text,
      instruct: instruct
    });

    if (statusEl) statusEl.innerHTML = '';
    const audioDiv = document.getElementById('lora-test-audio');
    if (audioDiv) audioDiv.innerHTML = `<audio controls autoplay src="${result.audio_url}?t=${Date.now()}"></audio>`;
  } catch (e) {
    if (statusEl) statusEl.innerHTML = `<span class="text-danger">Failed: ${(e as Error).message}</span>`;
  }
}

/**
 * Delete a trained LoRA adapter.
 * Uses raw fetch DELETE to /api/lora/models/{adapterId}.
 */
async function deleteLoraModel(adapterId: string): Promise<void> {
  if (!await showConfirm('Delete this trained adapter? This cannot be undone.')) return;
  try {
    const res = await fetch(`/api/lora/models/${encodeURIComponent(adapterId)}`, { method: 'DELETE' });
    if (!res.ok) {
      const err = await res.json();
      showToast(err.detail || 'Failed to delete.', 'error');
      return;
    }
    loadLoraModels();
  } catch (e) {
    showToast('Error deleting adapter: ' + (e as Error).message, 'error');
  }
}

/**
 * Download a built-in LoRA adapter from HuggingFace.
 * POSTs to /api/lora/download/{adapterId} and refreshes the models list.
 */
async function downloadBuiltinAdapter(adapterId: string): Promise<void> {
  const btn = document.getElementById(`lora-dl-btn-${adapterId}`) as HTMLButtonElement;
  if (!btn) return;
  const origHtml = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i>Downloading...';

  try {
    await API.post(`/api/lora/download/${encodeURIComponent(adapterId)}`, {});
    showToast('Adapter downloaded successfully.', 'success');
    loadLoraModels();
  } catch (e) {
    showToast('Download failed: ' + (e as Error).message, 'error');
    btn.disabled = false;
    btn.innerHTML = origHtml;
  }
}

/**
 * Initialize the Training tab.
 * Removes inline onclick handlers from HTML and attaches addEventListener.
 * Loads initial datasets and models on DOMContentLoaded.
 */
export function initTraining(): void {
  document.addEventListener('DOMContentLoaded', () => {
    // Upload dataset button
    const uploadBtn = document.querySelector('[onclick="uploadLoraDataset()"]');
    if (uploadBtn) {
      uploadBtn.removeAttribute('onclick');
      uploadBtn.addEventListener('click', () => uploadLoraDataset());
    }

    // Dataset list — event delegation for delete buttons
    const datasetsList = document.getElementById('lora-datasets-list');
    if (datasetsList) {
      datasetsList.addEventListener('click', (e) => {
        const target = e.target as HTMLElement;
        const btn = target.closest('[data-action="delete-dataset"]') as HTMLElement;
        if (btn) {
          const id = btn.getAttribute('data-id');
          if (id) deleteLoraDataset(id);
        }
      });
    }

    // Start training button
    const trainBtn = document.getElementById('btn-lora-train');
    if (trainBtn) {
      trainBtn.removeAttribute('onclick');
      trainBtn.addEventListener('click', () => startLoraTraining());
    }

    // Models list — event delegation for action buttons
    const modelsList = document.getElementById('lora-models-list');
    if (modelsList) {
      modelsList.addEventListener('click', (e) => {
        const target = e.target as HTMLElement;
        const btn = target.closest('[data-action]') as HTMLElement;
        if (!btn) return;
        const action = btn.getAttribute('data-action');
        const id = btn.getAttribute('data-id');
        if (!id) return;

        switch (action) {
          case 'play-preview':
            playLoraPreview(id);
            break;
          case 'test-model':
            testLoraModel(id);
            break;
          case 'delete-model':
            deleteLoraModel(id);
            break;
          case 'download-adapter':
            downloadBuiltinAdapter(id);
            break;
        }
      });
    }

    // Refresh models button
    const refreshBtn = document.querySelector('[onclick="loadLoraModels()"]');
    if (refreshBtn) {
      refreshBtn.removeAttribute('onclick');
      refreshBtn.addEventListener('click', () => loadLoraModels());
    }

    // Test generate button
    const testBtn = document.querySelector('[onclick="runLoraTest()"]');
    if (testBtn) {
      testBtn.removeAttribute('onclick');
      testBtn.addEventListener('click', () => runLoraTest());
    }

    // Initial load
    loadLoraDatasets();
    loadLoraModels();
  });
}
