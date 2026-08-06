import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    environment: 'jsdom',
    environmentOptions: {
      // Explicit http(s) URL so jsdom exposes localStorage (absent on about:blank)
      jsdom: {
        url: 'http://localhost:3000/',
      },
    },
    include: ['tests/frontend/**/*.test.ts'],
    setupFiles: ['vitest.setup.ts'],
  },
});
