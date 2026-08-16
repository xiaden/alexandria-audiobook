/**
 * Alexandria Audiobook — Main entry point
 * Imports all tab modules and initializes them at module scope.
 */

// Import shared modules to ensure they're included in the build
import './api';
import './state';
import './utils';
import './templates';
import { initTheme } from './theme';
import { initState } from './state';

// ---------------------------------------------------------------------------
// Tab navigation
// ---------------------------------------------------------------------------
// Evidence-based fix (parts README adjustment #1): nav links are plain
// `<a data-tab="...">` with no click handler, so only #setup-tab is ever
// visible. The handlers below toggle pane visibility and the active class;
// they do NOT touch existing per-tab listeners (e.g. editor.ts's
// loadSpans/loadReviewItems hook on [data-tab="editor"]) — those coexist on
// the same elements and keep firing on click.

/**
 * Switch the visible tab to `tabName`.
 *
 * Shows the pane element `#${tabName}-tab`, hides every other `.tab-content`
 * pane, and moves the `.nav-link.active` class to the matching nav link.
 * Fails silently (no-op, no console error) if the pane element is missing.
 */
export function switchTab(tabName: string): void {
  const pane = document.getElementById(`${tabName}-tab`);
  if (!pane) return; // fail silently: no pane for this tab

  // Hide every tab pane, then reveal the target one.
  document.querySelectorAll<HTMLElement>('.tab-content').forEach((p) => {
    p.style.display = 'none';
  });
  pane.style.display = '';

  // Move the active class to the clicked tab's nav link.
  document.querySelectorAll<HTMLElement>('.nav-link[data-tab]').forEach((link) => {
    link.classList.remove('active');
    if (link.getAttribute('data-tab') === tabName) {
      link.classList.add('active');
    }
  });
}

let tabNavigationInitialized = false;

/**
 * Wire up the global tab-switch click handler.
 *
 * Listens on `document` (delegated) so it coexists with per-tab listeners
 * attached directly to nav links — e.g. the editor's load hook — without
 * removing, replacing, or suppressing them. Idempotent: safe to call more
 * than once.
 */
export function initTabNavigation(): void {
  if (tabNavigationInitialized) return;
  tabNavigationInitialized = true;
  document.addEventListener('click', (e) => {
    const target = e.target as HTMLElement | null;
    const link = target?.closest?.('[data-tab]');
    if (!link) return;
    const tabName = link.getAttribute('data-tab');
    if (!tabName) return;
    // Suppress the anchor default navigation (nav links carry href="#", which
    // would rewrite the URL fragment and scroll to top in a real browser).
    e.preventDefault();
    switchTab(tabName);
  });
}

// Import tab modules
import { initSetup } from './tabs/setup';
import { initScript } from './tabs/script';
import { initVoices } from './tabs/voices';
import { initDesigner } from './tabs/designer';
import { initPreparer } from './tabs/preparer';
import { initDatasetBuilder } from './tabs/dataset-builder';
import { initTraining } from './tabs/training';
import { initEditor } from './tabs/editor';
import { initProjects } from './tabs/projects';
import { initWorkbench } from './tabs/workbench';
import { initPromptConfig } from './tabs/prompt-config';

// Initialize all tabs at MODULE SCOPE, not inside a DOMContentLoaded handler.
//
// This entry script is a module (`<script type="module">`), so it executes
// after the HTML is parsed but BEFORE DOMContentLoaded fires. Each tab init
// registers its OWN `document.addEventListener('DOMContentLoaded', ...)`
// listener; calling the inits here registers those listeners in time for the
// event, so every tab's wiring runs normally.
//
// Previously these calls were wrapped in a DOMContentLoaded handler, which
// made the inits run DURING dispatch — their nested listeners were therefore
// registered mid-dispatch and per the DOM spec are never invoked for the
// current event. The result: initScript()'s reveal of #pipeline-section never
// ran, so the Script tab showed only its static (and since the old pipeline
// was deleted, empty) "Generation Logs" card.
initState();
initTheme();
initSetup();
initScript();
initVoices();
initDesigner();
initPreparer();
initDatasetBuilder();
initTraining();
initEditor();
initProjects();
initWorkbench();
initPromptConfig();
initTabNavigation();
