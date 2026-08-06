import { vi } from 'vitest';

/**
 * Vitest global setup (jsdom).
 *
 * Node 22+ ships an experimental global `localStorage` getter that returns
 * undefined unless `--localstorage-file` is passed. Because that key already
 * exists on the Node global, vitest's jsdom `populateGlobal` skips overriding
 * it (getWindowKeys only overrides pre-existing keys that are in its allowlist),
 * so `globalThis.localStorage` stays Node's dead getter instead of jsdom's
 * real Storage. Force jsdom's implementation (reachable via vitest's
 * `global.jsdom` handle) onto globalThis so `state.ts` saveKey/loadKey
 * persistence tests work without Node CLI flags.
 */
const jsdomWindow = (globalThis as { jsdom?: { window: Window & typeof globalThis } }).jsdom?.window;
if (jsdomWindow && jsdomWindow.localStorage) {
  Object.defineProperty(globalThis, 'localStorage', {
    value: jsdomWindow.localStorage,
    configurable: true,
    writable: true,
  });
  if (jsdomWindow.sessionStorage) {
    Object.defineProperty(globalThis, 'sessionStorage', {
      value: jsdomWindow.sessionStorage,
      configurable: true,
      writable: true,
    });
  }
}

// ---------------------------------------------------------------------------
// Media stubs (evidence-based adjustment #2: DD claims stubs exist in
// vitest.setup.ts; research found only MockAudio inside test_voices.test.ts).
//
// jsdom implements HTMLMediaElement but play()/pause() throw "Not implemented:
// HTMLMediaElement's play() method". The DD test strategy (line 138) requires
// playable audio under jsdom for the singleton-player tests, so install
// recording mocks on HTMLMediaElement.prototype ONCE here. currentTime get/set
// works natively in jsdom (verified) — left untouched. `Audio` is provided by
// jsdom (verified: `typeof Audio === 'function'`, new Audio(url) returns an
// HTMLMediaElement) — no global Audio stub needed; instances inherit the
// stubbed prototype play().
//
// Installed with Object.defineProperty, NOT vi.stubGlobal: test_voices.test.ts
// stubs the Audio global with its own MockAudio via vi.stubGlobal and calls
// vi.unstubAllGlobals() in afterEach — plain property definitions here are
// invisible to that bookkeeping, so the cycle restores OUR stubs (captured as
// the "original" at stub time), never jsdom's throwing originals.
// ---------------------------------------------------------------------------
const mediaProto = (
  globalThis as { HTMLMediaElement?: { prototype: HTMLMediaElement } }
).HTMLMediaElement?.prototype;
if (mediaProto) {
  // play(): a real <audio>.play() returns Promise<void>; jsdom throws instead.
  // The mock resolves (so await play() works) and records calls for assertions.
  Object.defineProperty(mediaProto, 'play', {
    value: vi.fn(() => Promise.resolve()),
    writable: true,
    configurable: true,
    enumerable: true,
  });
  Object.defineProperty(mediaProto, 'pause', {
    value: vi.fn(),
    writable: true,
    configurable: true,
    enumerable: true,
  });
}
