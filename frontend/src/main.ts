/**
 * Alexandria Audiobook — Main entry point
 * Imports all tab modules and initializes them on DOMContentLoaded.
 */

// Import shared modules to ensure they're included in the build
import './api';
import './state';
import './utils';
import './templates';

// Import tab modules
import { initSetup } from './tabs/setup';
import { initScript } from './tabs/script';
import { initVoices } from './tabs/voices';
import { initDesigner } from './tabs/designer';
import { initPreparer } from './tabs/preparer';
import { initDatasetBuilder } from './tabs/dataset-builder';
import { initTraining } from './tabs/training';
import { initEditor } from './tabs/editor';
import { initAudio } from './tabs/audio';

// Initialize all tabs after DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    initSetup();
    initScript();
    initVoices();
    initDesigner();
    initPreparer();
    initDatasetBuilder();
    initTraining();
    initEditor();
    initAudio();
});
