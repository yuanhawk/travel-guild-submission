import { defineConfig, devices } from '@playwright/test';

// E2E (browser-driven). Chromium is installed on demand
// (`npx playwright install`). Every spec mocks /negotiate_text via
// page.route so it's deterministic + needs no live backend.
export default defineConfig({
  testDir: './e2e',
  // A "staging/" subdirectory (targeting a live deployment, not included in
  // this showcase repo) would run via a separate config -- kept out of the
  // default run here.
  testIgnore: '**/staging/**',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? 'github' : 'list',
  use: {
    baseURL: process.env.E2E_BASE_URL ?? 'http://localhost:5173',
    trace: 'on-first-retry',
  },
  webServer: {
    command: 'npm run dev -- --port 5173 --strictPort',
    url: 'http://localhost:5173',
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
  },
  projects: [
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        // MapLibre needs WebGL. Headless Chromium has no GPU, so enable the
        // SwiftShader software renderer (ANGLE backend). --enable-unsafe-swiftshader
        // is required since Chromium 121 to allow SwiftShader WebGL in headless.
        // Works in any headless CI, not just this box.
        launchOptions: {
          args: [
            '--use-gl=angle',
            '--use-angle=swiftshader',
            '--enable-unsafe-swiftshader',
            '--ignore-gpu-blocklist',
            '--enable-webgl',
          ],
        },
      },
    },
  ],
});
