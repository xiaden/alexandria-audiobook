/**
 * Deterministic media fixtures for the browser-journey tests
 * (TASK-voice-persona-prompt-parity C, P4-S1).
 *
 * The DD requires journey-level Vitest/jsdom tests to be deterministic: fixed
 * media fixtures and fake TTS-engine generate behavior — never real binaries,
 * never timing-dependent assertions. This module provides:
 *
 *   - `createWavBytes(durationMs, sampleRate)` — a tiny valid PCM WAV (44-byte
 *     RIFF header + deterministic sine-wave samples) built with a DataView. The
 *     byte content is a pure function of the arguments, so it is stable across
 *     runs (the determinism gate runs the suite twice).
 *   - `wavDurationMs(bytes)` — parses duration back out of the WAV header so
 *     tests can assert the fixture's metadata without hard-coding sample math.
 *   - `FakeGenerateEngine` — a fake TTS engine implementing a minimal "generate
 *     contract": given a voice id + sample text it returns stable audio bytes
 *     and duration. It can be flipped to `unavailable`, in which case
 *     `generate()` throws `EngineUnavailableError`. This is what Journey 5
 *     ("no unavailable-engine false-green") uses: when a capability is
 *     unavailable it must be surfaced as an error, never silently passed.
 *
 * No real engine, no binary assets, no global stubs — the module is pure and
 * safe to import from any jsdom test.
 */

/** Standard WAV "fmt " chunk size for PCM. */
export const WAV_HEADER_BYTES = 44;

/** Error thrown by a fake engine whose capability is unavailable. */
export class EngineUnavailableError extends Error {
  constructor(message = 'TTS engine unavailable') {
    super(message);
    this.name = 'EngineUnavailableError';
  }
}

/**
 * Generate a minimal valid PCM WAV as bytes.
 *
 * Content is a deterministic 440 Hz sine wave (scaled to a small amplitude so
 * the samples stay well inside int16 range). `durationMs` and `sampleRate`
 * control the length; the header is a plain little-endian RIFF/WAVE chunk.
 */
export function createWavBytes(
  durationMs: number,
  sampleRate = 8000,
  channels = 1,
  bitsPerSample = 16,
): Uint8Array {
  const sampleCount = Math.max(0, Math.floor((sampleRate * durationMs) / 1000));
  const bytesPerSample = bitsPerSample / 8;
  const dataBytes = sampleCount * channels * bytesPerSample;
  const blockAlign = channels * bytesPerSample;
  const byteRate = sampleRate * blockAlign;
  const total = WAV_HEADER_BYTES + dataBytes;

  const buffer = new ArrayBuffer(total);
  const dv = new DataView(buffer);

  // RIFF/WAVE header (little-endian PCM).
  writeAscii(dv, 0, 'RIFF');
  dv.setUint32(4, 36 + dataBytes, true);
  writeAscii(dv, 8, 'WAVE');
  writeAscii(dv, 12, 'fmt ');
  dv.setUint32(16, 16, true); // fmt chunk size (PCM)
  dv.setUint16(20, 1, true); // audio format: PCM
  dv.setUint16(22, channels, true);
  dv.setUint32(24, sampleRate, true);
  dv.setUint32(28, byteRate, true);
  dv.setUint16(32, blockAlign, true);
  dv.setUint16(34, bitsPerSample, true);
  writeAscii(dv, 36, 'data');
  dv.setUint32(40, dataBytes, true);

  // Deterministic sample content: 440 Hz sine at ~12% peak amplitude.
  const amplitude = Math.round(32767 * 0.12);
  const samplesPerChannel = sampleCount * channels;
  for (let i = 0; i < samplesPerChannel; i++) {
    const sample = Math.round(Math.sin((2 * Math.PI * 440 * i) / sampleRate) * amplitude);
    dv.setInt16(WAV_HEADER_BYTES + i * bytesPerSample, sample, true);
  }

  return new Uint8Array(buffer);
}

/** Parse the duration (ms) back out of a `createWavBytes` WAV. */
export function wavDurationMs(bytes: Uint8Array): number {
  const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const byteRate = dv.getUint32(28, true);
  const dataBytes = dv.getUint32(40, true);
  if (byteRate <= 0) return 0;
  return Math.round((dataBytes / byteRate) * 1000);
}

/** A stable, collision-free pseudo URL for a fixture's generated audio. */
export function fakeAudioUrl(voiceId: string, seed: number): string {
  return `blob:fake-${encodeURIComponent(voiceId)}-${seed}`;
}

/** Request shape a fake TTS generate seam consumes. */
export interface FakeGenerateRequest {
  voiceId: string;
  sampleText: string;
}

/** Result shape a fake TTS generate seam produces. */
export interface FakeGenerateResult {
  bytes: Uint8Array;
  durationMs: number;
  mediaType: string;
  /** A stable URL the audio surface can assign to an <audio> src. */
  audioUrl: string;
}

/**
 * Deterministic fake TTS engine.
 *
 * When available, `generate()` records the request and returns stable audio
 * bytes (fixed 2000 ms) plus a stable `audioUrl`. When `unavailable` is set to
 * true it throws `EngineUnavailableError` — the seam the journey test wires to
 * real UI error surfacing, so an unavailable capability is never a silent pass.
 */
export class FakeGenerateEngine {
  unavailable = false;
  /** Every generate() request, in call order (for assertion). */
  calls: FakeGenerateRequest[] = [];

  constructor(public readonly voiceId = 'voice-fake') {}

  async generate(request: FakeGenerateRequest): Promise<FakeGenerateResult> {
    this.calls.push(request);
    if (this.unavailable) {
      throw new EngineUnavailableError(
        `TTS engine unavailable for voice '${request.voiceId}' (capability not installed)`,
      );
    }
    const durationMs = 2000;
    const bytes = createWavBytes(durationMs, 8000);
    return {
      bytes,
      durationMs,
      mediaType: 'audio/wav',
      audioUrl: fakeAudioUrl(request.voiceId, this.calls.length),
    };
  }
}

/** Write an ASCII string into a DataView at the given byte offset. */
function writeAscii(dv: DataView, offset: number, text: string): void {
  for (let i = 0; i < text.length; i++) {
    dv.setUint8(offset + i, text.charCodeAt(i));
  }
}
