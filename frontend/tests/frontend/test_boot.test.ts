/**
 * Regression test for the real browser boot flow (frontend/src/main.ts).
 *
 * Discovery (published image): the Script tab showed only the static
 * "Generation Logs" card — initScript()'s reveal of #pipeline-section never
 * ran. Root cause: main.ts registered a DOMContentLoaded handler that called
 * each tab init (initSetup, initScript, ...) DURING dispatch. Each tab init
 * registers its OWN DOMContentLoaded listener, and per the DOM spec a
 * listener added during dispatch is never invoked for the current event — so
 * every tab's wiring was dead.
 *
 * Fix: main.ts calls the tab inits at MODULE SCOPE. Module scripts execute
 * after parsing but before DOMContentLoaded fires, so each init's listener is
 * registered in time and fires normally.
 *
 * This test pins that flow: import main.ts, build the Script-tab DOM, dispatch
 * DOMContentLoaded, and assert the pipeline section is revealed. It fails on
 * the old (nested-listener) boot and passes on the module-scope boot.
 */

import { describe, it, expect, vi } from 'vitest';

// Mock the API and utils modules so the full main.ts boot never touches the
// network. A Proxy mock covers every API export the tab modules reference
// (get, post, getEffectiveWalkConfig, ...) without enumerating them. The get
// mock is path-aware: list endpoints return [] (loaders iterate with .map)
// and /api/config returns a config-shaped object (loadConfig reads
// config.llm.base_url etc.), so the dispatched tab inits do not throw.
vi.mock('../../src/api', () =>
  new Proxy({}, {
    get: () =>
      vi.fn().mockImplementation((path?: string) => {
        if (typeof path === 'string' && path.endsWith('/config')) {
          return Promise.resolve({
            llm: { base_url: '', api_key: '', model_name: '', reasoning_effort: '', temperature: 0.1, task_overrides: {} },
            tts: { provider: 'builtin', voice: '' },
          });
        }
        return Promise.resolve([]);
      }),
  }),
);

vi.mock('../../src/utils', () => ({
  showToast: vi.fn(),
  showConfirm: vi.fn(),
  escapeHtml: (s: unknown) => String(s),
}));

describe('main.ts boot flow (regression: nested DOMContentLoaded listeners)', () => {
  it('reveals #pipeline-section after DOMContentLoaded — the Script tab is not blank', async () => {
    // Fresh module registry so main.ts's module-scope inits run exactly once
    // against the DOM built below (mirrors a real page load).
    vi.resetModules();

    document.body.innerHTML = `
      <div id="script-tab" class="tab-content" style="display:none;">
        <div id="pipeline-section" style="display:none;">
          <button id="btn-onboard-epub"></button>
          <div id="walk-status-container"></div>
        </div>
      </div>
      <div id="workbench-tab" class="tab-content"></div>
    `;

    // Importing main.ts runs the tab inits at module scope (as in the real
    // browser, where this entry script is <script type="module">).
    await import('../../src/main');

    // The browser fires DOMContentLoaded after module scripts execute.
    document.dispatchEvent(new Event('DOMContentLoaded'));

    const section = document.getElementById('pipeline-section') as HTMLElement;
    expect(section).not.toBeNull();
    expect(section.style.display).not.toBe('none');
  });
});
