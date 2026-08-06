/**
 * Alexandria Audiobook — Main entry point
 * Imports all tab modules and initializes them on DOMContentLoaded.
 */

// Import shared modules to ensure they're included in the build
import './api';
import './state';
import './utils';
import './templates';
import { initTheme } from './theme';
import { initState } from './state';

// Import tab modules
import { initSetup } from './tabs/setup';
import { initScript } from './tabs/script';
import { initVoices } from './tabs/voices';
import { initDesigner } from './tabs/designer';
import { initPreparer } from './tabs/preparer';
import { initDatasetBuilder } from './tabs/dataset-builder';
import { initTraining } from './tabs/training';
import { initEditor } from './tabs/editor';

// Initialize all tabs after DOM is ready
document.addEventListener('DOMContentLoaded', () => {
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
});
