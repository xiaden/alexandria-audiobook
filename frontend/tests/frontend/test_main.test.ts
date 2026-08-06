/**
 * Spec-first tests for tab navigation (frontend/src/main.ts).
 *
 * Evidence-based requirement (parts README adjustment #1): nav links are plain
 * `<a data-tab="...">` with no click handler — only #setup-tab is ever visible
 * and every other pane sits in display:none DOM. These tests pin the fix:
 * clicking a nav link must show its pane, hide the others, and move the
 * .nav-link.active class — while preserving any existing per-tab listeners
 * (e.g. editor.ts's initEditor hook that calls loadSpans()/loadReviewItems()).
 *
 * A minimal jsdom DOM is built inline (nav links + panes) rather than loading
 * the real index.html, so the tests are hermetic and need no media stubs.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { switchTab, initTabNavigation } from '../../src/main';

// Mirrors the nav structure of frontend/index.html (L104-111) plus the
// `{tab}-tab` panes (setup visible by default, the rest display:none).
const NAV_HTML = `
  <nav>
    <a class="nav-link nav-pipeline active" data-tab="setup">1. Setup</a>
    <a class="nav-link nav-pipeline" data-tab="script">2. Script</a>
    <a class="nav-link nav-pipeline" data-tab="voices">3. Voices</a>
    <a class="nav-link nav-pipeline" data-tab="editor">4. Editor</a>
  </nav>
  <div id="setup-tab" class="tab-content">setup pane</div>
  <div id="script-tab" class="tab-content" style="display:none;">script pane</div>
  <div id="voices-tab" class="tab-content" style="display:none;">voices pane</div>
  <div id="editor-tab" class="tab-content" style="display:none;">editor pane</div>
`;

function pane(tabName: string): HTMLElement | null {
  return document.getElementById(`${tabName}-tab`);
}

function navLink(tabName: string): HTMLElement {
  return document.querySelector(`[data-tab="${tabName}"]`) as HTMLElement;
}

describe('Tab navigation', () => {
  beforeEach(() => {
    document.body.innerHTML = NAV_HTML;
  });

  afterEach(() => {
    document.body.innerHTML = '';
  });

  it('shows the clicked tab pane and hides the previously visible pane', () => {
    initTabNavigation();

    navLink('voices').click();

    // (a) target pane visible
    expect(pane('voices')?.style.display).not.toBe('none');
    // (b) previously visible pane hidden, along with all others
    expect(pane('setup')?.style.display).toBe('none');
    expect(pane('script')?.style.display).toBe('none');
    expect(pane('editor')?.style.display).toBe('none');
  });

  it('moves the .nav-link.active class to the clicked link', () => {
    initTabNavigation();

    navLink('voices').click();

    // (c) active class moved
    expect(navLink('voices').classList.contains('active')).toBe(true);
    expect(navLink('setup').classList.contains('active')).toBe(false);
    expect(navLink('script').classList.contains('active')).toBe(false);
  });

  it('switchTab() shows the pane for the given tab name and hides others', () => {
    switchTab('script');

    expect(pane('script')?.style.display).not.toBe('none');
    expect(pane('setup')?.style.display).toBe('none');
    expect(pane('voices')?.style.display).toBe('none');
    expect(navLink('script').classList.contains('active')).toBe(true);
    expect(navLink('setup').classList.contains('active')).toBe(false);
  });

  it('preserves per-tab load hooks — an existing listener on the editor nav link still fires', () => {
    // Simulate editor.ts initEditor()'s per-tab listener (loadSpans +
    // loadReviewItems on `[data-tab="editor"]`). The global handler must
    // coexist with it, not remove or suppress it.
    const editorLink = navLink('editor');
    const editorHook = vi.fn();
    editorLink.addEventListener('click', editorHook);

    initTabNavigation();
    editorLink.click();

    // (d) per-tab load hook contract preserved: the editor listener fires
    expect(editorHook).toHaveBeenCalledTimes(1);
    // and the global handler still toggled pane + active class
    expect(pane('editor')?.style.display).not.toBe('none');
    expect(pane('setup')?.style.display).toBe('none');
    expect(editorLink.classList.contains('active')).toBe(true);
  });

  it('fails silently when the target pane element is missing', () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});

    expect(() => switchTab('nonexistent-tab')).not.toThrow();
    expect(consoleError).not.toHaveBeenCalled();

    consoleError.mockRestore();
  });

  it('ignores clicks on non-nav elements', () => {
    initTabNavigation();

    pane('voices')?.click();

    // setup stays visible and active; nothing switched
    expect(pane('setup')?.style.display).not.toBe('none');
    expect(navLink('setup').classList.contains('active')).toBe(true);
  });

  it('calls preventDefault on data-tab link clicks (suppresses anchor default navigation)', () => {
    initTabNavigation();

    // Nav links carry href="#" so they are focusable; in a real browser the
    // anchor default navigation would rewrite the URL fragment and scroll to
    // top. jsdom does not implement anchor navigation, so spy on the event's
    // preventDefault (dispatched event bubbles to the document-level handler).
    const link = navLink('voices');
    const ev = new MouseEvent('click', { bubbles: true, cancelable: true });
    const preventDefault = vi.spyOn(ev, 'preventDefault');

    link.dispatchEvent(ev);

    expect(preventDefault).toHaveBeenCalledTimes(1);
    // and the tab switch still happens
    expect(pane('voices')?.style.display).not.toBe('none');

    preventDefault.mockRestore();
  });
});
