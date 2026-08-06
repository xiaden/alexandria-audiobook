/**
 * HTML template functions for the Alexandria frontend
 * Ported from app/static/index.html lines 1691-1838, 1968-1978, 1990-2049, 3664-3697
 */

import { escapeHtml } from './utils';
import { state, type Chunk, type DsbRow } from './state';

/**
 * Build speaker select dropdown HTML for a chunk
 * @param chunk - Chunk object with speaker field
 * @returns HTML string for the select element
 */
export function buildSpeakerSelect(chunk: Chunk): string {
  const current = (chunk.speaker || '').trim();
  const names = state.voicesNames;
  const normalized = [...new Set(names.map(n => (n || '').trim()).filter(Boolean))].sort((a, b) => a.localeCompare(b));
  const options = normalized.map(name => `<option value="${escapeHtml(name)}" ${name === current ? 'selected' : ''}>${escapeHtml(name)}</option>`).join('');
  const unknownOption = current && !normalized.includes(current)
    ? `<option value="${escapeHtml(current)}" selected>${escapeHtml(current)} (custom)</option>`
    : '';

  return `<select class="form-select form-select-sm" data-action="update-chunk-speaker" data-chunk-id="${chunk.id}">${unknownOption}${options}</select>`;
}

/**
 * Update a chunk row in the editor table (DOM manipulation)
 * @param chunk - Updated chunk data
 * @returns true if row was found and updated, false otherwise
 */
export function updateChunkRow(chunk: Chunk): boolean {
  const tr = document.querySelector(`tr[data-id="${chunk.id}"]`);
  if (!tr) return false;

  const statusColor = chunk.status === 'done' ? 'success' :
                    chunk.status === 'generating' ? 'warning' :
                    chunk.status === 'error' ? 'danger' : 'secondary';

  // Update status badge
  const badge = tr.querySelector('.badge');
  if (badge) {
    badge.className = `badge bg-${statusColor}`;
    badge.textContent = chunk.status;
  }

  // Update action area (button/progress)
  const actionContainer = tr.querySelector('.d-flex');
  if (actionContainer) {
    const existingBtn = actionContainer.querySelector('button');
    const existingProgress = actionContainer.querySelector('.progress');

    if (chunk.status === 'generating') {
      if (existingBtn && !existingProgress) {
        const progressBar = document.createElement('div');
        progressBar.className = 'progress';
        progressBar.style.width = '100px';
        progressBar.style.height = '20px';
        progressBar.innerHTML = '<div class="progress-bar progress-bar-striped progress-bar-animated bg-warning" role="progressbar" style="width: 100%"></div>';
        actionContainer.replaceChild(progressBar, existingBtn);
      }
    } else {
      if (existingProgress && !existingBtn) {
        const btn = document.createElement('button');
        btn.className = 'btn btn-sm btn-primary';
        btn.dataset.action = 'generate-chunk';
        btn.dataset.chunkId = String(chunk.id);
        btn.innerHTML = '<i class="fas fa-play"></i> Gen';
        actionContainer.replaceChild(btn, existingProgress);
      }
    }

    // Update audio player when status is done - always refresh src to bust cache
    if (chunk.status === 'done' && chunk.audio_path) {
      const existingAudio = actionContainer.querySelector('audio');
      const existingNoAudio = actionContainer.querySelector('.text-muted');
      const newSrc = `/${chunk.audio_path}?t=${Date.now()}`;

      if (existingNoAudio) {
        // No audio element yet, create one
        const audioHtml = `<audio class="chunk-audio" data-id="${chunk.id}" controls src="${newSrc}" style="width: 200px; height: 30px;" data-action="stop-others"></audio>`;
        existingNoAudio.outerHTML = audioHtml;
      } else if (existingAudio) {
        // Audio exists - just update the src with new cache-busting timestamp
        (existingAudio as HTMLAudioElement).src = newSrc;
        (existingAudio as HTMLAudioElement).load(); // Force reload
      }
    }
  }
  return true;
}

/**
 * Build HTML for a dataset builder row
 * @param row - Dataset row data
 * @param i - Row index
 * @returns HTML string for the table row
 */
export function dsbBuildRowHtml(row: DsbRow, i: number): string {
  const statusColor = row.status === 'done' ? 'success' :
                      row.status === 'generating' ? 'warning' :
                      row.status === 'error' ? 'danger' : 'secondary';
  const statusLabel = row.status || 'pending';

  let actionHtml = '';
  if (row.status === 'generating') {
    actionHtml = '<div class="progress" style="width:80px;height:20px;"><div class="progress-bar progress-bar-striped progress-bar-animated bg-warning" style="width:100%"></div></div>';
  } else {
    const genLabel = row.status === 'done' ? '<i class="fas fa-redo"></i>' : '<i class="fas fa-play"></i>';
  actionHtml = `<button class="btn btn-sm btn-primary" data-action="dsb-gen-sample" data-dsb-idx="${i}" title="${row.status === 'done' ? 'Regenerate' : 'Generate'}">${genLabel}</button>`;
  }

  let audioHtml = '';
  if (row.status === 'done' && row.audio_url) {
    audioHtml = `<audio controls src="${row.audio_url}" style="width:180px;height:28px;" data-action="dsb-stop-others" data-dsb-idx="${i}"></audio>`;
  }

  return `<tr data-dsb-idx="${i}" data-dsb-status="${row.status || 'pending'}" data-dsb-audio="${row.audio_url || ''}" class="${row.status === 'generating' ? 'table-info' : ''}">
    <td class="text-center align-middle">${i + 1}</td>
    <td><input type="text" class="form-control form-control-sm" value="${(row.emotion || '').replace(/"/g, '&quot;')}" data-action="dsb-update-row" data-dsb-idx="${i}" data-field="emotion" placeholder="e.g. Savagely sarcastic"></td>
    <td><textarea class="form-control form-control-sm" rows="2" data-action="dsb-update-row" data-dsb-idx="${i}" data-field="text" placeholder="Sample text...">${(row.text || '').replace(/</g, '&lt;')}</textarea></td>
    <td><input type="number" class="form-control form-control-sm" value="${row.seed ?? ''}" data-action="dsb-update-row" data-dsb-idx="${i}" data-field="seed" placeholder="-" style="width:65px;" min="-1"></td>
    <td class="text-center align-middle"><span class="badge bg-${statusColor}">${statusLabel}</span></td>
    <td class="align-middle">
      <div class="d-flex align-items-center gap-1">
        ${actionHtml}
        ${audioHtml}
        <button class="btn btn-sm btn-outline-danger ms-auto" data-action="dsb-remove-row" data-dsb-idx="${i}" title="Delete row"><i class="fas fa-trash"></i></button>
      </div>
    </td>
  </tr>`;
}
