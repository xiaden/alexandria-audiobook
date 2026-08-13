import { defineConfig } from 'vitest/config';
export default defineConfig({
  test: {
    environment: 'jsdom',
    environmentOptions: { jsdom: { url: 'http://localhost:3000/' } },
    include: ['tests/frontend/**/*.test.ts'],
    setupFiles: ['vitest.setup.ts'],
    maxWorkers: 1,
    minWorkers: 1,
    pool: 'forks',
    poolOptions: { forks: { singleFork: true } },
    hookTimeout: 120000,
    testTimeout: 60000,
  },
});
