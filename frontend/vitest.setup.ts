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
