/**
 * Audio/Result Tab
 * Ported from app/static/index.html lines 1025-1105 (HTML structure),
 * 2637-2734 (exportAudacity, exportM4B, M4B cover upload), 2737-2760 (pollLogs audio completion)
 *
 * TTS-only: no LLM calls. Handles final audiobook playback, MP3 download,
 * Audacity export, and M4B export with metadata options.
 */

import * as API from '../api';
import { escapeHtml } from '../utils';

/**
 * Export audiobook to Audacity project format (ZIP)
 * Posts to /api/export_audacity, polls /api/status/audacity_export,
 * and auto-downloads the resulting ZIP file on success.
 */
async function exportAudacity(): Promise<void> {
  const statusEl = document.getElementById('audacity-status');
  if (!statusEl) return;

  statusEl.innerHTML = '<span class="text-info"><i class="fas fa-spinner fa-spin me-1"></i>Exporting...</span>';

  try {
    await API.post('/api/export_audacity', {});

    const poll = setInterval(async () => {
      try {
        const status = await API.get<{ running: boolean; logs: string[] }>('/api/status/audacity_export');
        if (!status.running) {
          clearInterval(poll);
          if (status.logs.some(l => l.includes("complete"))) {
            statusEl.innerHTML = '<span class="text-success"><i class="fas fa-check me-1"></i>Done!</span>';
            // Auto-download the zip
            const a = document.createElement('a');
            a.href = `/api/export_audacity?t=${Date.now()}`;
            a.download = 'audacity_export.zip';
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            setTimeout(() => { statusEl.innerHTML = ''; }, 5000);
          } else {
            const lastLog = status.logs[status.logs.length - 1] || 'Unknown error';
            statusEl.innerHTML = `<span class="text-danger"><i class="fas fa-times me-1"></i>${escapeHtml(lastLog)}</span>`;
          }
        }
      } catch (e) {
        clearInterval(poll);
        const msg = e instanceof Error ? e.message : String(e);
        statusEl.innerHTML = `<span class="text-danger">Poll error: ${escapeHtml(msg)}</span>`;
      }
    }, 1000);
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    statusEl.innerHTML = `<span class="text-danger"><i class="fas fa-times me-1"></i>${escapeHtml(msg)}</span>`;
  }
}

/**
 * Handle M4B cover image file upload
 * Posts the file to /api/m4b_cover as FormData and updates status text.
 */
async function handleM4BCoverUpload(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  const statusEl = document.getElementById('m4b-cover-status');
  if (!file || !statusEl) return;

  const formData = new FormData();
  formData.append('file', file);
  try {
    const resp = await fetch('/api/m4b_cover', { method: 'POST', body: formData });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({ detail: resp.statusText }));
      throw new Error(body.detail || resp.statusText);
    }
    statusEl.textContent = 'Uploaded';
    statusEl.className = 'small text-success';
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    statusEl.textContent = msg;
    statusEl.className = 'small text-danger';
  }
}

/**
 * Export audiobook to M4B format with metadata
 * Posts to /api/merge_m4b with metadata fields, polls /api/status/m4b_export,
 * and auto-downloads the resulting M4B file on success.
 */
async function exportM4B(): Promise<void> {
  const statusEl = document.getElementById('m4b-status');
  if (!statusEl) return;

  const perChunk = (document.getElementById('m4b-per-chunk') as HTMLInputElement)?.checked ?? false;
  statusEl.innerHTML = '<span class="text-info"><i class="fas fa-spinner fa-spin me-1"></i>Exporting M4B...</span>';

  try {
    await API.post('/api/merge_m4b', {
      per_chunk_chapters: perChunk,
      title: (document.getElementById('m4b-title') as HTMLInputElement)?.value ?? '',
      author: (document.getElementById('m4b-author') as HTMLInputElement)?.value ?? '',
      narrator: (document.getElementById('m4b-narrator') as HTMLInputElement)?.value ?? '',
      year: (document.getElementById('m4b-year') as HTMLInputElement)?.value ?? '',
      description: (document.getElementById('m4b-description') as HTMLInputElement)?.value ?? ''
    });

    const poll = setInterval(async () => {
      try {
        const status = await API.get<{ running: boolean; logs: string[] }>('/api/status/m4b_export');
        if (!status.running) {
          clearInterval(poll);
          if (status.logs.some(l => l.includes("complete"))) {
            statusEl.innerHTML = '<span class="text-success"><i class="fas fa-check me-1"></i>Done!</span>';
            const a = document.createElement('a');
            a.href = `/api/audiobook_m4b?t=${Date.now()}`;
            a.download = 'audiobook.m4b';
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            setTimeout(() => { statusEl.innerHTML = ''; }, 5000);
          } else {
            const lastLog = status.logs[status.logs.length - 1] || 'Unknown error';
            statusEl.innerHTML = `<span class="text-danger"><i class="fas fa-times me-1"></i>${escapeHtml(lastLog)}</span>`;
          }
        }
      } catch (e) {
        clearInterval(poll);
        const msg = e instanceof Error ? e.message : String(e);
        statusEl.innerHTML = `<span class="text-danger">Poll error: ${escapeHtml(msg)}</span>`;
      }
    }, 1000);
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    statusEl.innerHTML = `<span class="text-danger"><i class="fas fa-times me-1"></i>${escapeHtml(msg)}</span>`;
  }
}

/**
 * Initialize the Audio/Result tab.
 * Attaches event listeners to export buttons and M4B cover upload.
 * Initializes event listeners for export buttons and M4B cover upload.
 *
 * Note: pollLogs('audio', 'audio-logs') is called from editor.ts mergeAudiobook()
 * and the pollLogs function in script.ts already handles audio completion
 * (loading the audio player when merge completes). Tab-switch polling is
 * handled by the tab navigation in the HTML (data-tab="audio" click triggers
 * the editor.ts merge flow which calls pollLogs).
 */
export function initAudio(): void {
  document.addEventListener('DOMContentLoaded', () => {
    // Audacity export button
    const audacityBtn = document.querySelector('button[onclick="exportAudacity()"]') as HTMLButtonElement | null;
    if (audacityBtn) {
      audacityBtn.removeAttribute('onclick');
      audacityBtn.addEventListener('click', exportAudacity);
    }

    // M4B export button
    const m4bBtn = document.querySelector('button[onclick="exportM4B()"]') as HTMLButtonElement | null;
    if (m4bBtn) {
      m4bBtn.removeAttribute('onclick');
      m4bBtn.addEventListener('click', exportM4B);
    }

    // M4B cover image upload
    const coverInput = document.getElementById('m4b-cover-input') as HTMLInputElement | null;
    if (coverInput) {
      coverInput.addEventListener('change', handleM4BCoverUpload);
    }
  });
}
