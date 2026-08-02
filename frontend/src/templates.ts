/**
 * HTML template functions for the Alexandria frontend
 * Ported from app/static/index.html lines 1691-1838, 1968-1978, 1990-2049, 3664-3697
 */

import { escapeHtml } from './utils';
import { state, AVAILABLE_VOICES, type Voice, type Chunk, type DsbRow } from './state';

/**
 * Create HTML for a voice configuration card
 * @param voice - Voice object with name and config
 * @param index - Index for radio button naming
 * @returns HTML string for the voice card
 */
export function createVoiceCard(voice: Voice, index: number): string {
  const config = voice.config || {};
  const voiceType = config.type || 'custom';

  return `
    <div class="card voice-card mb-3" data-voice="${escapeHtml(voice.name)}">
      <div class="card-body">
        <div class="row">
          <div class="col-md-3">
            <h5 class="card-title">${escapeHtml(voice.name)} ${config.alias_of ? `<span class="badge bg-info ms-2" title="Alias of ${escapeHtml(config.alias_of)}">${escapeHtml(config.alias_of)}</span>` : ''}</h5>
            <div class="form-text small text-muted mt-1">Alias of:</div>
            <select class="form-select form-select-sm alias-select mt-1">
              <option value="">-- None --</option>
              ${(() => {
                const names = state.voicesNames.filter(n => n !== voice.name);
                return names.map(n => `<option value="${escapeHtml(n)}" ${config.alias_of === n ? 'selected' : ''}>${escapeHtml(n)}</option>`).join('');
              })()}
            </select>
          </div>
          <div class="col-md-9">
            <div class="mb-2">
              <div class="form-check form-check-inline">
                <input class="form-check-input voice-type" type="radio" name="type_${index}" value="custom" ${voiceType === 'custom' ? 'checked' : ''} data-action="toggle-voice-type">
                <label class="form-check-label">Custom Voice</label>
              </div>
              <div class="form-check form-check-inline">
                <input class="form-check-input voice-type" type="radio" name="type_${index}" value="builtin_lora" ${voiceType === 'builtin_lora' ? 'checked' : ''} data-action="toggle-voice-type">
                <label class="form-check-label">Built-in Voice</label>
              </div>
              <div class="form-check form-check-inline">
                <input class="form-check-input voice-type" type="radio" name="type_${index}" value="clone" ${voiceType === 'clone' ? 'checked' : ''} data-action="toggle-voice-type">
                <label class="form-check-label">Voice Clone</label>
              </div>
              <div class="form-check form-check-inline">
                <input class="form-check-input voice-type" type="radio" name="type_${index}" value="lora" ${voiceType === 'lora' ? 'checked' : ''} data-action="toggle-voice-type">
                <label class="form-check-label">LoRA Voice</label>
              </div>
              <div class="form-check form-check-inline">
                <input class="form-check-input voice-type" type="radio" name="type_${index}" value="design" ${voiceType === 'design' ? 'checked' : ''} data-action="toggle-voice-type">
                <label class="form-check-label">Voice Design</label>
              </div>
            </div>

            <!-- Custom Options -->
            <div class="custom-opts" style="display: ${voiceType === 'custom' ? 'block' : 'none'}">
              <div class="row g-2">
                <div class="col-md-6">
                  <select class="form-select voice-select">
                    ${AVAILABLE_VOICES.map(v => `<option value="${v}" ${config.voice === v ? 'selected' : ''}>${v}</option>`).join('')}
                  </select>
                </div>
                <div class="col-md-6">
                  <input type="text" class="form-control character-style" placeholder="Character style (e.g. refined aristocratic tone, heavy Scottish accent)" value="${escapeHtml(config.character_style || config.default_style || '')}">
                </div>
              </div>
            </div>

            <!-- Built-in LoRA Options -->
            <div class="builtin-lora-opts" style="display: ${voiceType === 'builtin_lora' ? 'block' : 'none'}">
              <div class="row g-2">
                <div class="col-md-6">
                  <select class="form-select builtin-lora-select">
                    <option value="">-- Select built-in voice --</option>
                    ${(() => {
                      const models = state.loraModels.filter(m => m.builtin);
                      const males = models.filter(m => m.gender === 'male');
                      const females = models.filter(m => m.gender === 'female');
                      let html = '';
                      if (males.length) {
                        html += '<optgroup label="Male">';
                        html += males.map(m => `<option value="${escapeHtml(m.id)}" ${config.adapter_id === m.id ? 'selected' : ''} ${m.downloaded === false ? 'disabled' : ''}>${escapeHtml(m.name)}${m.downloaded === false ? ' (not downloaded)' : ''} — ${escapeHtml(m.description || '')}</option>`).join('');
                        html += '</optgroup>';
                      }
                      if (females.length) {
                        html += '<optgroup label="Female">';
                        html += females.map(m => `<option value="${escapeHtml(m.id)}" ${config.adapter_id === m.id ? 'selected' : ''} ${m.downloaded === false ? 'disabled' : ''}>${escapeHtml(m.name)}${m.downloaded === false ? ' (not downloaded)' : ''} — ${escapeHtml(m.description || '')}</option>`).join('');
                        html += '</optgroup>';
                      }
                      return html;
                    })()}
                  </select>
                </div>
                <div class="col-md-6">
                  <input type="text" class="form-control builtin-lora-style" placeholder="Character style (e.g. refined aristocratic tone, heavy Scottish accent)" value="${escapeHtml(voiceType === 'builtin_lora' ? (config.character_style || '') : '')}">
                </div>
              </div>
              <small class="text-muted mt-1 d-block">Grayed-out voices need to be downloaded first. Go to the <strong>Training</strong> tab to download them.</small>
            </div>

            <!-- Clone Options -->
            <div class="clone-opts" style="display: ${voiceType === 'clone' ? 'block' : 'none'}">
              <div class="row g-2 mb-2 align-items-center">
                <div class="col">
                  <select class="form-select designed-voice-select" data-action="designed-voice-select">
                    <option value="">-- Select voice or enter path manually --</option>
                    ${state.cloneVoices.length ? `<optgroup label="Uploaded Voices">
                      ${state.cloneVoices.map(v => `<option value="clone:${v.id}" ${config.ref_audio && config.ref_audio.includes(v.filename) ? 'selected' : ''}>${v.name}</option>`).join('')}
                    </optgroup>` : ''}
                    ${state.designedVoices.length ? `<optgroup label="Designed Voices">
                      ${state.designedVoices.map(v => `<option value="design:${v.id}" ${config.ref_audio && config.ref_audio.includes(v.filename) ? 'selected' : ''}>${v.name}</option>`).join('')}
                    </optgroup>` : ''}
                    <option value="__manual__" ${config.ref_audio && !state.cloneVoices.some(v => config.ref_audio!.includes(v.filename)) && !state.designedVoices.some(v => config.ref_audio!.includes(v.filename)) && config.ref_audio ? 'selected' : ''}>Custom path...</option>
                  </select>
                </div>
                <div class="col-auto">
                  <button class="btn btn-sm btn-outline-primary" data-action="upload-clone-voice" title="Upload audio file"><i class="fas fa-upload"></i> Upload</button>
                  <input type="file" class="clone-voice-file-input" accept=".wav,.mp3,.flac,.ogg" style="display:none" data-action="handle-clone-voice-upload">
                </div>
              </div>
              <input type="text" class="form-control ref-text mb-2" placeholder="Reference Text" value="${config.ref_text || ''}">
              <div class="input-group">
                <input type="text" class="form-control ref-audio" placeholder="Path to audio file" value="${config.ref_audio || ''}" ${config.ref_audio && (state.cloneVoices.some(v => config.ref_audio!.includes(v.filename)) || state.designedVoices.some(v => config.ref_audio!.includes(v.filename))) ? 'readonly' : ''}>
                <button class="btn btn-sm btn-outline-secondary clone-play-btn" data-action="play-clone-voice" title="Play reference audio" style="display:${config.ref_audio ? 'inline-block' : 'none'}"><i class="fas fa-play"></i></button>
                <button class="btn btn-sm btn-outline-danger clone-delete-btn" data-action="delete-clone-voice" title="Delete uploaded voice" style="display:${config.ref_audio && state.cloneVoices.some(v => config.ref_audio!.includes(v.filename)) ? 'inline-block' : 'none'}"><i class="fas fa-trash"></i></button>
              </div>
            </div>

            <!-- LoRA Options -->
            <div class="lora-opts" style="display: ${voiceType === 'lora' ? 'block' : 'none'}">
              <div class="row g-2">
                <div class="col-md-6">
                  <select class="form-select lora-adapter-select">
                    <option value="">-- Select trained adapter --</option>
                    ${state.loraModels.map(m => `<option value="${m.id}" ${config.adapter_id === m.id ? 'selected' : ''}>${m.name}</option>`).join('')}
                  </select>
                </div>
                <div class="col-md-6">
                  <input type="text" class="form-control lora-character-style" placeholder="Character style (e.g. refined aristocratic tone, heavy Scottish accent)" value="${voiceType === 'lora' ? (config.character_style || '') : ''}">
                </div>
              </div>
            </div>

            <!-- Voice Design Options -->
            <div class="design-opts" style="display: ${voiceType === 'design' ? 'block' : 'none'}">
              <input type="text" class="form-control design-description mb-1" placeholder="Base voice description (e.g. Young strong soldier)" value="${config.description || ''}">
              <span class="text-muted small">Per-line instruct is appended to this description as delivery/emotion direction</span>
              <div class="mt-2">
                <button type="button" class="btn btn-sm btn-outline-primary" data-action="open-voice-design-editor">
                  <i class="fas fa-wand-magic-sparkles me-1"></i>Re-design Voice
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  `;
}

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
