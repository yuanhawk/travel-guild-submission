import { defineConfig } from 'vitest/config';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { svelteTesting } from '@testing-library/svelte/vite';

// Unit/component tests (fast, chromium-free) — run on every push + locally.
// E2E (Playwright, browser-driven) lives under e2e/ and runs via `npm run test:e2e`.
//
// The svelte() plugin lets vitest compile *.svelte for COMPONENT tests; svelteTesting()
// wires the browser resolve-condition + auto-cleanup. Pure *.ts unit tests are unaffected
// and keep running in the default 'node' env — component tests opt into jsdom per-file
// via `// @vitest-environment jsdom`.
export default defineConfig({
  plugins: [svelte({ hot: false }), svelteTesting()],
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
    setupFiles: ['./vitest-setup.ts'],
  },
});
