/**
 * Singleton audio-surface player (Universal Upgrade cap1 — Plan E Phase 3).
 *
 * Contract locked by frontend/tests/frontend/test_player.test.ts:
 *
 *   createPreviewPlayer(audio?: HTMLAudioElement): PreviewPlayer
 *     Injectable factory. When `audio` is provided the player wraps THAT
 *     element (test/dependency injection). When omitted it returns the
 *     module-level singleton backed by one shared <audio> element, lazily
 *     created and NOT attached to the DOM — browsers play unattached audio
 *     fine, and jsdom needs no document.body for the media-stub tests.
 *
 *   getPreviewPlayer(): PreviewPlayer
 *     Module-level singleton accessor. Returns the same instance across
 *     calls; it is exactly createPreviewPlayer() without an element, so the
 *     singleton is constructed through the factory (per the DD test
 *     strategy: "audio singleton via injectable createPreviewPlayer()
 *     factory").
 *
 *   play(url): Promise<void>   — stopThenPlay
 *     Awaits the current stop() before loading the new src, so the next URL
 *     never starts loading until the previous playback has been torn down.
 *     AbortError (a pause/stop racing the play attempt) is benign and
 *     swallowed. NotAllowedError (autoplay blocked) is benign and arms a
 *     one-time tap-to-continue retry on the next pointerdown/keydown user
 *     gesture, resuming the blocked play inside the gesture's
 *     user-activation context. Other errors rethrow.
 *
 *   playSequence(urls): Promise<void>   — Phase 6 queue (additive)
 *     stopThenPlay applies to sequences too: awaits the current stop()
 *     (tearing down any in-flight sequence or single playback), then queues
 *     `urls` and plays them IN ORDER. Each URL loads through the same
 *     playInternal() path as play(). An 'ended' listener on the wrapped
 *     element auto-advances to the next queued URL; when the last URL ends
 *     the listener is removed and the sequence is done. An empty list is a
 *     no-op that resolves cleanly. Resolves once the first URL starts.
 *
 *   pause(): void              — pauses the wrapped element.
 *   seek(seconds): void        — moves the wrapped element's currentTime.
 *   stop(): Promise<void>      — pauses, drops pending tap-to-continue state,
 *                                clears the src, AND clears any queued
 *                                sequence (listener removed, no further
 *                                advancement). Resolves when the teardown is
 *                                done.
 *
 * The play path is centralized in playInternal() and each player holds its
 * element, so the queue hooks in without restructuring.
 */

/** Player surface consumed by the pipeline UI (Phase 4+) and tests. */
export interface PreviewPlayer {
  /** stopThenPlay: stops current playback first, then loads `url` and plays. */
  play(url: string): Promise<void>;
  /**
   * stopThenPlay sequence queue (Phase 6): stops current playback first, then
   * queues `urls` and plays them in order, auto-advancing on the element's
   * 'ended' event. Empty list resolves cleanly without playing. Resolves once
   * the first URL starts.
   */
  playSequence(urls: string[]): Promise<void>;
  /** Pause the wrapped element. */
  pause(): void;
  /** Move the wrapped element's currentTime to `seconds`. */
  seek(seconds: number): void;
  /**
   * Teardown: pause, drop pending tap-to-continue state, clear the src, and
   * clear any queued sequence (no further advancement).
   */
  stop(): Promise<void>;
}

/** The singleton instance; created lazily through createPreviewPlayer(). */
let singletonPlayer: PreviewPlayer | null = null;

/**
 * Create a preview player.
 *
 * With an `audio` argument this is a plain factory — every call produces a
 * fresh player wrapping that element (dependency injection for tests and for
 * Phase 4's editor-pipeline wiring). Without an argument it returns the
 * module-level singleton (one shared <audio> element, one player).
 */
export function createPreviewPlayer(audio?: HTMLAudioElement): PreviewPlayer {
  if (audio === undefined) {
    if (singletonPlayer) return singletonPlayer;
    const sharedAudio = document.createElement('audio');
    singletonPlayer = createPlayer(sharedAudio);
    return singletonPlayer;
  }
  return createPlayer(audio);
}

/**
 * Singleton accessor — returns the same shared player instance across calls
 * (backed by a single shared <audio> element).
 */
export function getPreviewPlayer(): PreviewPlayer {
  return createPreviewPlayer();
}

/** Build a player object driving the given element. */
function createPlayer(audio: HTMLAudioElement): PreviewPlayer {
  let tapToContinueCleanup: (() => void) | null = null;

  // Phase 6 sequence-playback state: whether a sequence is active and the
  // URLs still pending. The 'ended' listener is registered only while a
  // sequence is active, so single play() calls never auto-advance.
  let sequenceActive = false;
  let sequenceQueue: string[] = [];

  /** Remove any armed tap-to-continue listeners (idempotent). */
  function clearTapToContinue(): void {
    if (tapToContinueCleanup) {
      tapToContinueCleanup();
      tapToContinueCleanup = null;
    }
  }

  /**
   * Teardown half of stopThenPlay. Awaiting the element's pause() keeps the
   * stop awaitable and lets tests gate the transition (override pause() to
   * return a controllable promise); in browsers pause() is synchronous so
   * the await resolves immediately.
   */
  async function stop(): Promise<void> {
    // Sequence teardown first: no further advancement after stop, and a
    // late 'ended' (e.g. dispatched between stop and the next play) no-ops
    // because the listener is gone.
    sequenceActive = false;
    sequenceQueue = [];
    audio.removeEventListener('ended', onSequenceEnded);
    await audio.pause();
    clearTapToContinue();
    audio.removeAttribute('src');
  }

  /**
   * Auto-advance the active sequence when the current chunk finishes. Played
   * only while a sequence is active; the final URL ending ends the sequence
   * (listener removed, queue drained). A hard (non-benign) failure advancing
   * to the next URL aborts the sequence rather than leaving an unhandled
   * rejection.
   */
  function onSequenceEnded(): void {
    if (!sequenceActive) return;
    if (sequenceQueue.length === 0) {
      sequenceActive = false;
      audio.removeEventListener('ended', onSequenceEnded);
      return;
    }
    const next = sequenceQueue.shift() as string;
    void playInternal(next).catch((err: unknown) => {
      console.error('Sequence playback failed:', err);
      sequenceActive = false;
      sequenceQueue = [];
      audio.removeEventListener('ended', onSequenceEnded);
    });
  }

  /** Arm a one-time retry of a blocked autoplay on the next user gesture. */
  function registerTapToContinue(url: string): void {
    clearTapToContinue();
    const onGesture = (): void => {
      clearTapToContinue();
      // Retry inside the gesture's user-activation context.
      void playInternal(url).catch((err: unknown) => {
        console.error('Tap-to-continue retry failed:', err);
      });
    };
    document.addEventListener('pointerdown', onGesture);
    document.addEventListener('keydown', onGesture);
    tapToContinueCleanup = (): void => {
      document.removeEventListener('pointerdown', onGesture);
      document.removeEventListener('keydown', onGesture);
    };
  }

  /** Centralized load+play path (the sequence queue drives auto-advance;
   *  abort-clean on non-benign error). */
  async function playInternal(url: string): Promise<void> {
    audio.src = url;
    try {
      await audio.play();
    } catch (err) {
      if (errorName(err) === 'AbortError') {
        // Interrupted by a pause/stop racing the play attempt — benign.
        return;
      }
      if (errorName(err) === 'NotAllowedError') {
        // Autoplay blocked by the browser — benign; resume on user gesture.
        registerTapToContinue(url);
        return;
      }
      throw err;
    }
  }

  return {
    async play(url: string): Promise<void> {
      await stop();
      await playInternal(url);
    },
    async playSequence(urls: string[]): Promise<void> {
      // stopThenPlay applies to sequences: tear down any current playback or
      // in-flight sequence before queueing the new one.
      await stop();
      if (urls.length === 0) return; // empty sequence — no-op, resolves cleanly
      sequenceQueue = urls.slice();
      sequenceActive = true;
      audio.addEventListener('ended', onSequenceEnded);
      const first = sequenceQueue.shift() as string;
      try {
        await playInternal(first);
      } catch (err) {
        // A non-benign failure starting the FIRST URL must not leave the
        // sequence armed: mirror the onSequenceEnded abort path (listener
        // removed, queue drained, flag reset) so a late 'ended' cannot
        // advance a dead sequence and no state leaks until the next stop().
        sequenceActive = false;
        sequenceQueue = [];
        audio.removeEventListener('ended', onSequenceEnded);
        throw err;
      }
    },
    pause(): void {
      audio.pause();
    },
    seek(seconds: number): void {
      audio.currentTime = seconds;
    },
    stop,
  };
}

/**
 * Read the DOMException-style `name` off an unknown rejection value, without
 * depending on the error crossing realms (instanceof is unreliable for
 * errors thrown by the media element or re-created in tests).
 */
function errorName(err: unknown): string | undefined {
  if (err !== null && typeof err === 'object' && 'name' in err) {
    const name = (err as { name?: unknown }).name;
    return typeof name === 'string' ? name : undefined;
  }
  return undefined;
}
