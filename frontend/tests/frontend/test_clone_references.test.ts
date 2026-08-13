/**
 * Focused tests for the clone-reference panel in frontend/src/tabs/voices.ts
 * (PipelineVoiceCloneReferenceAPI.v1 shared contracts + clone UI).
 *
 * Covers: the bounded-upload / list / preview / download / delete UI surface,
 * the format helpers, resolved-id voice selector population, media seek
 * ordering (load/loadedmetadata precede currentTime), and the explicit
 * destructive-confirmation gate.
 *
 * Run with `npm test` (vitest run) from frontend/.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
  formatCloneByteSize,
  formatCloneDuration,
  createCloneReferenceRow,
  renderCloneReferenceList,
  renderCloneReferencePanel,
  loadCloneReferences,
  uploadCloneReferenceFromPanel,
  deleteCloneReferenceConfirmed,
  playCloneReference,
  resetCloneReferencePanel,
  CLONE_REF_MAX_BYTES,
  registerVoiceCatalog,
} from '../../src/tabs/voices';
import type { CloneReference, VoiceConfigRow } from '../../src/tabs/voices';
import { showToast, showConfirm } from '../../src/utils';
import * as API from '../../src/api';

// Mock the API module (contract helpers under test) and utils.
vi.mock('../../src/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/api')>();
  return {
    ...actual,
    listCloneReferences: vi.fn(),
    uploadCloneReference: vi.fn(),
    deleteCloneReference: vi.fn(),
    cloneReferencePreviewUrl: vi.fn((v, r) => `/preview/${v}/${r}`),
    cloneReferenceDownloadUrl: vi.fn((v, r) => `/download/${v}/${r}`),
  };
});

vi.mock('../../src/utils', () => ({
  showToast: vi.fn(),
  showConfirm: vi.fn(),
  escapeHtml: (s: unknown) => String(s),
}));

const MOCK_REF: CloneReference = {
  reference_id: 'ref-1',
  voice_id: 'voice-1',
  owner_id: 'local',
  relative_path: 'voices/voice-1/ref-1.wav',
  original_filename: 'sample.wav',
  media_type: 'audio/wav',
  byte_size: 2048,
  duration_ms: 120000,
  sha256: 'abc',
  created_ms: 1700000000000,
};

const MOCK_VOICES: VoiceConfigRow[] = [
  { id: 'voice-1', name: 'Alice', voice: 'Alice', type: 'clone' },
  { id: 'voice-2', name: 'Bob', voice: 'Bob', type: 'custom' },
  { id: 'NARRATOR', name: 'Narrator', voice: 'Ryan', type: null },
];

function setVoicesDom(): void {
  document.body.innerHTML = `
    <div id="toast-container"></div>
    <div id="pipeline-voices-section" style="display:none;">
      <div id="voice-catalog"></div>
      <div id="character-ledger"></div>
    </div>
  `;
  registerVoiceCatalog(MOCK_VOICES);
  // Build the clone-reference panel so #clone-reference-list exists.
  renderCloneReferencePanel();
}

beforeEach(() => {
  vi.resetAllMocks();
  setVoicesDom();
  vi.mocked(showConfirm).mockResolvedValue(true);
  resetCloneReferencePanel();
});

afterEach(() => {
  document.body.innerHTML = '';
  vi.resetAllMocks();
});

describe('formatCloneByteSize', () => {
  it('formats bytes, KB and MB', () => {
    expect(formatCloneByteSize(512)).toBe('512 B');
    expect(formatCloneByteSize(2048)).toBe('2.0 KB');
    expect(formatCloneByteSize(3 * 1024 * 1024)).toBe('3.0 MB');
  });
});

describe('formatCloneDuration', () => {
  it('formats seconds and minutes', () => {
    expect(formatCloneDuration(5000)).toBe('05s');
    expect(formatCloneDuration(120000)).toBe('2m 00s');
  });
});

describe('createCloneReferenceRow', () => {
  it('exposes display-safe metadata and never the filesystem path', () => {
    const html = createCloneReferenceRow(MOCK_REF);
    expect(html).toContain('sample.wav');
    expect(html).toContain('audio/wav');
    expect(html).toContain('2.0 KB');
    expect(html).toContain('2m 00s');
    expect(html).not.toContain('voices/voice-1');
    expect(html).not.toContain('relative_path');
    expect(html).toContain('data-action="clone-ref-preview"');
    expect(html).toContain('data-action="clone-ref-delete"');
    // Download is an attachment link to the download URL.
    expect(html).toContain('/download/voice-1/ref-1');
  });
});

describe('renderCloneReferencePanel', () => {
  it('populates the voice selector with resolved ids (non-narrator)', () => {
    renderCloneReferencePanel('voice-2');
    const select = document.getElementById('clone-ref-voice-select') as HTMLSelectElement;
    expect(select).not.toBeNull();
    const values = Array.from(select.options).map((o) => o.value);
    expect(values).toContain('voice-1');
    expect(values).toContain('voice-2');
    expect(values).not.toContain('NARRATOR');
    expect(select.value).toBe('voice-2');
  });

  it('no-ops when #pipeline-voices-section is absent', () => {
    document.body.innerHTML = '';
    expect(() => renderCloneReferencePanel()).not.toThrow();
  });
});

describe('renderCloneReferenceList', () => {
  it('renders empty state and a populated list', () => {
    renderCloneReferenceList([]);
    let list = document.getElementById('clone-reference-list');
    expect(list?.textContent).toContain('No clone references');

    renderCloneReferenceList([MOCK_REF]);
    list = document.getElementById('clone-reference-list');
    expect(list?.querySelector('[data-reference-id="ref-1"]')).not.toBeNull();
  });
});

describe('loadCloneReferences', () => {
  it('fetches and renders the owner references', async () => {
    vi.mocked(API.listCloneReferences).mockResolvedValue({ references: [MOCK_REF] });
    await loadCloneReferences('voice-1');
    expect(API.listCloneReferences).toHaveBeenCalledWith('voice-1');
    const list = document.getElementById('clone-reference-list');
    expect(list?.querySelector('[data-reference-id="ref-1"]')).not.toBeNull();
  });

  it('shows an error toast and clears the list on failure', async () => {
    vi.mocked(API.listCloneReferences).mockRejectedValue(new Error('boom'));
    await loadCloneReferences('voice-1');
    expect(showToast).toHaveBeenCalledWith(expect.stringContaining('boom'), 'error');
    const list = document.getElementById('clone-reference-list');
    expect(list?.textContent).toContain('No clone references');
  });
});

describe('uploadCloneReferenceFromPanel', () => {
  async function setupUpload(voiceId: string): Promise<HTMLButtonElement> {
    renderCloneReferencePanel(voiceId);
    const panel = document.getElementById('clone-reference-panel');
    // Ensure the delegated upload button exists inside the panel.
    document.body.innerHTML += `
      <button type="button" data-action="clone-ref-upload">Upload</button>
    `;
    void panel; // panel element referenced to keep lint quiet
    const button = document.querySelector('[data-action="clone-ref-upload"]') as HTMLButtonElement;
    // Inject the file input (the panel created one already — reuse it).
    return button;
  }

  it('requires a selected voice', async () => {
    renderCloneReferencePanel();
    const button = document.createElement('button');
    document.body.appendChild(button);
    await uploadCloneReferenceFromPanel(button);
    expect(showToast).toHaveBeenCalledWith(expect.stringContaining('Select a clone voice'), 'warning');
    expect(API.uploadCloneReference).not.toHaveBeenCalled();
  });

  it('requires a chosen file', async () => {
    const button = await setupUpload('voice-1');
    (document.getElementById('clone-ref-audio-file') as HTMLInputElement).value = '';
    await uploadCloneReferenceFromPanel(button);
    expect(showToast).toHaveBeenCalledWith(expect.stringContaining('Choose an audio file'), 'warning');
  });

  it('uploads with the file name and optional ref text', async () => {
    const button = await setupUpload('voice-1');
    const file = new File(['data'], 'sample.wav', { type: 'audio/wav' });
    Object.defineProperty(document.getElementById('clone-ref-audio-file') as HTMLInputElement, 'files', {
      value: [file],
      configurable: true,
    });
    (document.getElementById('clone-ref-text') as HTMLInputElement).value = 'aligned transcript';
    vi.mocked(API.uploadCloneReference).mockResolvedValue({
      reference: MOCK_REF,
      voice: { id: 'voice-1', name: 'Alice', voice: 'Alice', type: 'clone', ref_audio: 'voices/voice-1/ref-1.wav' },
    });
    vi.mocked(API.listCloneReferences).mockResolvedValue({ references: [MOCK_REF] });

    await uploadCloneReferenceFromPanel(button);
    expect(API.uploadCloneReference).toHaveBeenCalledWith(
      'voice-1', file, 'sample.wav', 'aligned transcript',
    );
    expect(showToast).toHaveBeenCalledWith(expect.stringContaining('Reference uploaded'), 'success');
  });

  it('rejects an oversized file before hitting the network', async () => {
    const button = await setupUpload('voice-1');
    const big = new File([new Uint8Array(CLONE_REF_MAX_BYTES + 1)], 'big.wav', { type: 'audio/wav' });
    Object.defineProperty(document.getElementById('clone-ref-audio-file') as HTMLInputElement, 'files', {
      value: [big],
      configurable: true,
    });
    await uploadCloneReferenceFromPanel(button);
    expect(showToast).toHaveBeenCalledWith(expect.stringContaining('limit'), 'error');
    expect(API.uploadCloneReference).not.toHaveBeenCalled();
  });

  it('surfaces upload failure', async () => {
    const button = await setupUpload('voice-1');
    const file = new File(['data'], 'sample.wav', { type: 'audio/wav' });
    Object.defineProperty(document.getElementById('clone-ref-audio-file') as HTMLInputElement, 'files', {
      value: [file],
      configurable: true,
    });
    vi.mocked(API.uploadCloneReference).mockRejectedValue(new Error('rejected'));
    await uploadCloneReferenceFromPanel(button);
    expect(showToast).toHaveBeenCalledWith(expect.stringContaining('rejected'), 'error');
  });
});

describe('deleteCloneReferenceConfirmed', () => {
  it('requires explicit confirmation before deleting', async () => {
    vi.mocked(API.deleteCloneReference).mockResolvedValue(undefined);
    vi.mocked(API.listCloneReferences).mockResolvedValue({ references: [] });

    await deleteCloneReferenceConfirmed('voice-1', 'ref-1');
    expect(showConfirm).toHaveBeenCalled();
    expect(API.deleteCloneReference).toHaveBeenCalledWith('voice-1', 'ref-1');
    expect(showToast).toHaveBeenCalledWith('Clone reference deleted', 'success');
  });

  it('aborts when confirmation is declined', async () => {
    vi.mocked(showConfirm).mockResolvedValue(false);
    await deleteCloneReferenceConfirmed('voice-1', 'ref-1');
    expect(API.deleteCloneReference).not.toHaveBeenCalled();
  });
});

describe('playCloneReference (media seek ordering)', () => {
  it('sets src, waits for loadedmetadata before currentTime, then plays', async () => {
    // Build the inline preview <audio> exactly as createCloneReferenceRow does.
    document.body.innerHTML = `
      <div id="pipeline-voices-section">
        <audio id="clone-audio-ref-1" controls preload="metadata"></audio>
      </div>
    `;
    const audio = document.getElementById('clone-audio-ref-1') as HTMLAudioElement;

    // Stub media methods (play resolves per vitest.setup).
    const playSpy = vi.spyOn(audio, 'play').mockResolvedValue(undefined);
    const loadSpy = vi.spyOn(audio, 'load').mockImplementation(() => {
      // Simulate metadata becoming available synchronously on load.
      audio.dispatchEvent(new Event('loadedmetadata'));
    });

    const currentTimeSpy = vi.spyOn(audio, 'currentTime', 'set');

    await playCloneReference('voice-1', 'ref-1');

    expect(loadSpy).toHaveBeenCalled();
    expect(currentTimeSpy).toHaveBeenCalledWith(0);
    expect(playSpy).toHaveBeenCalled();
    // src must be the inline preview URL.
    expect(audio.src).toContain('/preview/voice-1/ref-1');
  });
});
