import { defineConfig, devices } from '@playwright/test'
import { existsSync } from 'node:fs'

/**
 * Orb Hospital OS — Playwright e2e configuration (A6)
 * =================================================================
 * Five "golden journey" specs live in ./e2e. They drive the real React shell
 * (production bundle) served by the real FastAPI backend with disposable data.
 * See e2e/README.md for the full run instructions and the
 * exact demo credentials.
 *
 * Chromium comes from Playwright's managed browser cache unless explicitly
 * overridden. PLAYWRIGHT_BROWSERS_PATH retains its standard Playwright meaning.
 */

const CHROMIUM_PATH = process.env.ORB_CHROMIUM_PATH ?? ''
const BASE_URL = process.env.ORB_E2E_BASE_URL ?? 'http://127.0.0.1:18000'

// Explicit manual mode is for a disposable test instance managed by the caller.
const MANAGE_SERVERS = process.env.ORB_E2E_NO_WEBSERVER !== '1'
const address = new URL(BASE_URL)
if (MANAGE_SERVERS && (!['127.0.0.1', 'localhost', '[::1]'].includes(address.hostname)
    || address.protocol !== 'http:' || address.pathname !== '/' || address.search || address.hash)) {
  throw new Error('Managed E2E requires a loopback http origin; use manual mode for another test instance')
}
const python = process.env.ORB_E2E_PYTHON ??
  (existsSync('server/.venv-dev/bin/python') ? 'server/.venv-dev/bin/python' : 'server/.venv/bin/python')
const shellQuote = (value: string) => `'${value.replace(/'/g, `'\\''`)}'`

export default defineConfig({
  testDir: './e2e',
  // Golden journeys are integration-heavy; give each a generous ceiling but not
  // so long that a hung login hangs CI forever.
  timeout: 60_000,
  expect: { timeout: 15_000 },
  fullyParallel: false, // shared backend DB — run serially to keep state legible
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: [['list'], ['html', { open: 'never', outputFolder: 'playwright-report' }]],

  use: {
    baseURL: BASE_URL,
    // Emit a full trace on the first retry / on failure so you can open it with
    // `npx playwright show-trace`. Traces land in test-results/.
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'off',
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
  },

  projects: [
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        launchOptions: {
          ...(CHROMIUM_PATH ? { executablePath: CHROMIUM_PATH } : {}),
          args: ['--no-sandbox', '--disable-dev-shm-usage'],
        },
      },
    },
  ],

  // Never attach automatically to an existing appliance. The harness copies
  // application source and seeds only a new temporary database.
  webServer: MANAGE_SERVERS
    ? {
        command: `${shellQuote(process.execPath)} node_modules/typescript/bin/tsc -b && ` +
          `${shellQuote(process.execPath)} node_modules/vite/bin/vite.js build && ` +
          `${shellQuote(python)} scripts/e2e_server.py --host ${shellQuote(address.hostname.replace('[', '').replace(']', ''))} ` +
          `--port ${address.port || '80'} --offline-modules`,
        url: `${BASE_URL.replace(/\/$/, '')}/api/ready`,
        reuseExistingServer: false,
        gracefulShutdown: { signal: 'SIGTERM', timeout: 20_000 },
        timeout: 180_000,
        stdout: 'pipe',
        stderr: 'pipe',
      }
    : undefined,
})
