import { defineConfig } from 'vite';

/**
 * Vite build configuration for Alexandria Audiobook frontend.
 * Outputs to app/static/dist/ for Flask static serving.
 */
export default defineConfig({
  /** URL base path — matches Flask's /static/ route */
  base: '/static/',
  build: {
    /** Output directory relative to project root — Flask serves from app/static/ */
    outDir: '../app/static/dist',
    /** Clear output directory before each build to remove stale assets */
    emptyOutDir: true,
  },
});
