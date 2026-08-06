/**
 * Spec-first tests for the audio-surface player (frontend/src/player.ts).
 *
 * Three blocks:
 *
 * 1. "vitest media stubs" — pins the jsdom media stubs added in Phase 2
 *    (frontend/vitest.setup.ts). jsdom implements HTMLMediaElement but its
 *    play()/pause() throw "Not implemented: HTMLMediaElement's play() method",
 *    so the setup installs recording mocks on HTMLMediaElement.prototype.
 *    These tests assert play()/pause()/currentTime work on a plain
 *    document.createElement('audio') WITHOUT a real media backend, and that
 *    the stubbed prototype play() records calls. These PASS in Phase 2.
 *
 * 2. "createPreviewPlayer() factory" — the contract implemented by Phase 3's
 *    frontend/src/player.ts. The module is loaded through a NON-LITERAL
 *    dynamic import (PLAYER_MODULE widened to `string`): TypeScript does not
 *    statically resolve non-literal specifiers, so nothing type-checks against
 *    the module path, while vitest's module runner resolves it at runtime.
 *    During Phase 2 that runtime resolution failed with "cannot find module"
 *    — the intended spec-first RED at that phase boundary. Phase 3 implemented
 *    src/player.ts to the contract below and these tests turned green.
 *
 * SPEC (locked here; Phase 3 must implement to it):
 *   - module exports `createPreviewPlayer(audio?: HTMLAudioElement)` — a
 *     factory returning a player object exposing
 *       play(url: string): Promise<void>   (stopThenPlay: stops current
 *                                           playback first; resolves when
 *                                           playback starts)
 *       playSequence(urls: string[]): Promise<void>
 *                                           (Phase 6: stopThenPlay applies to
 *                                           sequences too — stops current
 *                                           playback first, then queues the
 *                                           list and plays the URLs IN ORDER,
 *                                           auto-advancing on the element's
 *                                           'ended' event; an empty list is a
 *                                           no-op that resolves cleanly;
 *                                           stop() clears the queue so no
 *                                           further advancement happens)
 *       pause(): void
 *       seek(seconds: number): void        (moves the wrapped element's
 *                                           currentTime)
 *       stop(): Promise<void>              (stops playback: pauses, clears the
 *                                           src + tap-to-continue state; play()
 *                                           awaits it before loading a new URL)
 *                                           also clears any queued sequence per
 *                                           Phase 6)
 *   - the optional `audio` argument is dependency injection for tests: when
 *     provided, the player wraps THAT element (set src + drive play/pause/
 *     seek on it); when omitted the player creates its own element.
 *   - module exports `getPreviewPlayer()` — a singleton accessor returning
 *     the same shared player instance (wrapping one shared <audio> element)
 *     across calls.
 *   - AbortError / NotAllowedError from play() are treated as benign
 *     (swallowed) so tap-to-continue autoplay resumes without unhandled
 *     rejections (pinned by Phase 3's own tests).
 *
 * 3. "playSequence() queue" — Phase 6's sequence playback (bottom describe):
 *    queues span chunk URLs and plays them in order, auto-advancing on the
 *    element's 'ended' event; stop() clears the queue so no further
 *    advancement happens.
 */

import { describe, it, expect, vi } from 'vitest';

/** Player module specifier — widened to `string` so tsc skips static
 *  resolution (kept from the Phase 2 spec-first RED; now that src/player.ts
 *  exists it resolves at runtime). */
const PLAYER_MODULE: string = '../../src/player';

/** The player contract Phase 3 must implement (see docblock above). */
interface PreviewPlayer {
  play(url: string): Promise<void>;
  playSequence(urls: string[]): Promise<void>;
  pause(): void;
  seek(seconds: number): void;
  stop(): Promise<void>;
}

/** The player module shape Phase 3 must export. */
interface PlayerModule {
  createPreviewPlayer(audio?: HTMLAudioElement): PreviewPlayer;
  getPreviewPlayer(): PreviewPlayer;
}

/** Loads the player module via the non-literal dynamic import. Hoisted to
 *  module scope so every describe block (factory, singleton, Phase 6 queue)
 *  shares it. */
async function loadPlayerModule(): Promise<PlayerModule> {
  return (await import(PLAYER_MODULE)) as PlayerModule;
}

// ---------------------------------------------------------------------------
// Media stubs (Phase 2) — must PASS in this phase, no player import involved
// ---------------------------------------------------------------------------

describe('vitest media stubs (jsdom HTMLMediaElement)', () => {
  it('play() is callable on a plain <audio> element and resolves (jsdom does not implement it)', async () => {
    const audio = document.createElement('audio');
    expect(typeof audio.play).toBe('function');
    await expect(audio.play()).resolves.toBeUndefined();
  });

  it('the stubbed prototype play() records invocations', async () => {
    const playMock = vi.mocked(HTMLMediaElement.prototype.play);
    playMock.mockClear();
    const audio = document.createElement('audio');
    await audio.play();
    await audio.play();
    expect(playMock).toHaveBeenCalledTimes(2);
  });

  it('pause() is callable on a plain <audio> element and does not throw', () => {
    const audio = document.createElement('audio');
    expect(() => audio.pause()).not.toThrow();
  });

  it('currentTime can be read and assigned on a plain <audio> element', () => {
    const audio = document.createElement('audio');
    expect(audio.currentTime).toBe(0);
    audio.currentTime = 12.5;
    expect(audio.currentTime).toBe(12.5);
  });

  it('new Audio(url) works in jsdom and inherits the stubbed prototype play()', async () => {
    const audio = new Audio('/api/pipeline/export/chunk/job-1/0');
    expect(audio.src).toContain('/api/pipeline/export/chunk/job-1/0');
    await expect(audio.play()).resolves.toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// Player factory + singleton (Phase 3 spec) — green since src/player.ts landed
// ---------------------------------------------------------------------------

describe('createPreviewPlayer() factory (spec-first — src/player.ts lands in Phase 3)', () => {
  it('the factory returns a player exposing play/pause/seek/stop', async () => {
    const mod = await loadPlayerModule();
    const player = mod.createPreviewPlayer();
    expect(typeof player.play).toBe('function');
    expect(typeof player.pause).toBe('function');
    expect(typeof player.seek).toBe('function');
    expect(typeof player.stop).toBe('function');
  });

  it('play(url) loads the URL onto the injected audio element and starts playback', async () => {
    const playMock = vi.mocked(HTMLMediaElement.prototype.play);
    playMock.mockClear();
    const audio = document.createElement('audio');
    const mod = await loadPlayerModule();
    const player = mod.createPreviewPlayer(audio);

    await player.play('/api/pipeline/export/chunk/job-1/0');

    expect(audio.src).toContain('/api/pipeline/export/chunk/job-1/0');
    expect(playMock).toHaveBeenCalledTimes(1);
  });

  it('pause() pauses the injected audio element', async () => {
    const pauseMock = vi.mocked(HTMLMediaElement.prototype.pause);
    pauseMock.mockClear();
    const audio = document.createElement('audio');
    const mod = await loadPlayerModule();
    const player = mod.createPreviewPlayer(audio);

    player.pause();

    expect(pauseMock).toHaveBeenCalledTimes(1);
  });

  it('seek(seconds) moves the injected audio element currentTime', async () => {
    const audio = document.createElement('audio');
    const mod = await loadPlayerModule();
    const player = mod.createPreviewPlayer(audio);

    player.seek(42);

    expect(audio.currentTime).toBe(42);
  });

  it('getPreviewPlayer() returns the same singleton instance across calls', async () => {
    const mod = await loadPlayerModule();
    const first = mod.getPreviewPlayer();
    const second = mod.getPreviewPlayer();

    expect(first).toBe(second);
    expect(typeof first.play).toBe('function');
    expect(typeof first.stop).toBe('function');
  });

  it('getPreviewPlayer() and createPreviewPlayer() without an element are the same singleton instance', async () => {
    const mod = await loadPlayerModule();
    const viaFactory = mod.createPreviewPlayer();
    const viaAccessor = mod.getPreviewPlayer();

    expect(viaFactory).toBe(viaAccessor);
    expect(mod.getPreviewPlayer()).toBe(viaFactory);
    expect(mod.createPreviewPlayer()).toBe(viaFactory);
  });

  it('play(url) awaits the previous stop() before loading the next URL (stopThenPlay)', async () => {
    const audio = document.createElement('audio');
    const mod = await loadPlayerModule();
    const player = mod.createPreviewPlayer(audio);

    // First play completes normally (prototype pause mock resolves instantly).
    await player.play('/api/pipeline/export/chunk/job-1/0');

    // Gate the element's pause() so the stop() inside the second play() is
    // controllable: url2 must not start loading until that stop resolves.
    let releaseStop!: () => void;
    const stopGate = new Promise<void>((resolve) => { releaseStop = resolve; });
    audio.pause = (() => stopGate) as unknown as () => void;

    const playMock = vi.mocked(HTMLMediaElement.prototype.play);
    playMock.mockReset();

    const secondPlay = player.play('/api/pipeline/export/chunk/job-2/1');

    // The previous stop is still pending — url2 must NOT be loading yet.
    expect(audio.src).not.toContain('job-2');
    expect(playMock).not.toHaveBeenCalled();

    releaseStop();
    await secondPlay;

    // Only after the stop resolves does play() load url2 and start playback.
    expect(audio.src).toContain('job-2');
    expect(playMock).toHaveBeenCalledTimes(1);
  });

  it('play() swallows an AbortError rejection from the element (benign interruption)', async () => {
    const playMock = vi.mocked(HTMLMediaElement.prototype.play);
    playMock.mockReset();
    playMock.mockRejectedValueOnce(new DOMException('interrupted', 'AbortError'));

    const audio = document.createElement('audio');
    const mod = await loadPlayerModule();
    const player = mod.createPreviewPlayer(audio);

    // AbortError (e.g. a pause/stop interrupting the play attempt) must not
    // reject the play() promise.
    await expect(player.play('/api/pipeline/export/chunk/job-1/0')).resolves.toBeUndefined();
    expect(playMock).toHaveBeenCalledTimes(1);
    expect(audio.src).toContain('/api/pipeline/export/chunk/job-1/0');
  });

  it('play() treats a blocked autoplay (NotAllowedError) as benign and retries on the next user gesture (tap-to-continue)', async () => {
    const playMock = vi.mocked(HTMLMediaElement.prototype.play);
    playMock.mockReset();
    playMock.mockRejectedValueOnce(new DOMException('autoplay blocked', 'NotAllowedError'));

    const audio = document.createElement('audio');
    const mod = await loadPlayerModule();
    const player = mod.createPreviewPlayer(audio);

    // First attempt is blocked by the autoplay policy — play() must still
    // resolve (benign) and arm a tap-to-continue retry.
    await expect(player.play('/api/pipeline/export/chunk/job-1/0')).resolves.toBeUndefined();
    expect(playMock).toHaveBeenCalledTimes(1);

    // A user gesture (pointerdown) triggers the one-time retry.
    document.dispatchEvent(new Event('pointerdown'));
    await Promise.resolve();

    expect(playMock).toHaveBeenCalledTimes(2);
    expect(audio.src).toContain('job-1');

    // The retry is one-time: a second gesture does not start playback again.
    document.dispatchEvent(new Event('pointerdown'));
    await Promise.resolve();
    expect(playMock).toHaveBeenCalledTimes(2);
  });

  it('play() rethrows non-benign errors from the element (network/decode/404)', async () => {
    const playMock = vi.mocked(HTMLMediaElement.prototype.play);
    playMock.mockReset();
    playMock.mockRejectedValueOnce(new Error('boom'));

    const audio = document.createElement('audio');
    const mod = await loadPlayerModule();
    const player = mod.createPreviewPlayer(audio);

    // Only AbortError/NotAllowedError are benign — anything else (network,
    // decode, HTTP 404/500 surfaced via api.ts handleError) must propagate so
    // editor-pipeline's try/catch can toast it instead of leaving an unhandled
    // promise rejection from the click handler.
    await expect(player.play('/api/pipeline/export/chunk/job-1/0')).rejects.toThrow('boom');
    expect(audio.src).toContain('/api/pipeline/export/chunk/job-1/0');
  });
});

// ---------------------------------------------------------------------------
// Sequence playback queue (Plan E, Phase 6) — playSequence(urls) queues a list
// of chunk URLs and plays them in presentation order, auto-advancing on the
// element's 'ended' event. stop() clears the queue so no further advancement
// happens. stopThenPlay applies to sequences too: playSequence() tears down any
// current playback before starting, and play()/stop() cancel an in-flight
// sequence. An empty list is a no-op that resolves cleanly.
// ---------------------------------------------------------------------------

describe('playSequence() queue (spec-first — Phase 6)', () => {
  it('plays queued URLs in order, auto-advancing on ended events', async () => {
    const playMock = vi.mocked(HTMLMediaElement.prototype.play);
    playMock.mockClear();
    const audio = document.createElement('audio');
    const mod = await loadPlayerModule();
    const player = mod.createPreviewPlayer(audio);

    const sequence = player.playSequence(['/chunk/1', '/chunk/2', '/chunk/3']);
    await sequence;

    // The first queued URL loads and starts as soon as the sequence resolves.
    expect(audio.src).toContain('/chunk/1');
    expect(playMock).toHaveBeenCalledTimes(1);

    // Each 'ended' event on the element advances to the next queued URL.
    audio.dispatchEvent(new Event('ended'));
    expect(audio.src).toContain('/chunk/2');
    expect(playMock).toHaveBeenCalledTimes(2);

    audio.dispatchEvent(new Event('ended'));
    expect(audio.src).toContain('/chunk/3');
    expect(playMock).toHaveBeenCalledTimes(3);

    // The last chunk ending finishes the sequence — no further advancement.
    audio.dispatchEvent(new Event('ended'));
    expect(playMock).toHaveBeenCalledTimes(3);
  });

  it('stop() during a sequence clears the queue and stops cleanly', async () => {
    const playMock = vi.mocked(HTMLMediaElement.prototype.play);
    playMock.mockClear();
    const audio = document.createElement('audio');
    const mod = await loadPlayerModule();
    const player = mod.createPreviewPlayer(audio);

    await player.playSequence(['/chunk/1', '/chunk/2', '/chunk/3']);
    expect(audio.src).toContain('/chunk/1');

    await player.stop();

    // The queue is cleared and the element torn down: a late 'ended' event
    // must not advance to /chunk/2, and the src attribute is gone.
    expect(audio.hasAttribute('src')).toBe(false);
    audio.dispatchEvent(new Event('ended'));
    expect(audio.src).not.toContain('/chunk/2');
    expect(playMock).toHaveBeenCalledTimes(1);
  });

  it('playSequence([]) is a no-op and resolves cleanly', async () => {
    const playMock = vi.mocked(HTMLMediaElement.prototype.play);
    playMock.mockClear();
    const audio = document.createElement('audio');
    const mod = await loadPlayerModule();
    const player = mod.createPreviewPlayer(audio);

    await expect(player.playSequence([])).resolves.toBeUndefined();
    expect(playMock).not.toHaveBeenCalled();
    expect(audio.hasAttribute('src')).toBe(false);
  });

  it('playSequence() aborts cleanly when the first URL fails with a non-benign error', async () => {
    const playMock = vi.mocked(HTMLMediaElement.prototype.play);
    playMock.mockReset();
    playMock.mockRejectedValueOnce(new Error('boom'));

    const audio = document.createElement('audio');
    const mod = await loadPlayerModule();
    const player = mod.createPreviewPlayer(audio);

    // The first URL rejects hard — playSequence must reject and must NOT
    // leave the sequence armed: the 'ended' listener, queue and flag are all
    // torn down (mirroring the onSequenceEnded abort path).
    await expect(player.playSequence(['/chunk/1', '/chunk/2'])).rejects.toThrow('boom');

    // The 'ended' listener was removed: a late 'ended' event must not
    // advance to /chunk/2, and play() was never re-invoked.
    audio.dispatchEvent(new Event('ended'));
    expect(audio.src).not.toContain('/chunk/2');
    expect(playMock).toHaveBeenCalledTimes(1);
  });

  it('playSequence() stops current playback first (stopThenPlay applies to sequences)', async () => {
    const audio = document.createElement('audio');
    const mod = await loadPlayerModule();
    const player = mod.createPreviewPlayer(audio);

    // Establish playback on the element.
    await player.play('/chunk/prev');
    expect(audio.src).toContain('/chunk/prev');

    // Gate the element's pause() so the stop() inside playSequence() is
    // controllable: the sequence must not start loading until it resolves.
    let releaseStop!: () => void;
    const stopGate = new Promise<void>((resolve) => { releaseStop = resolve; });
    audio.pause = (() => stopGate) as unknown as () => void;

    const playMock = vi.mocked(HTMLMediaElement.prototype.play);
    playMock.mockReset();

    const sequence = player.playSequence(['/chunk/1', '/chunk/2']);

    // The teardown is still pending — nothing queued or loaded yet.
    expect(audio.src).not.toContain('/chunk/1');
    expect(playMock).not.toHaveBeenCalled();

    releaseStop();
    await sequence;

    // Only after the stop resolves does the first queued URL load.
    expect(audio.src).toContain('/chunk/1');
    expect(playMock).toHaveBeenCalledTimes(1);
  });

  it('playSequence is exposed on the shared singleton player', async () => {
    const mod = await loadPlayerModule();
    const singleton = mod.getPreviewPlayer();
    expect(typeof singleton.playSequence).toBe('function');
  });
});
