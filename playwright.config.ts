import { defineConfig, devices } from '@playwright/test'

/**
 * Orb Hospital OS — Playwright e2e configuration (A6)
 * =================================================================
 * Five "golden journey" specs live in ./e2e. They drive the real React shell
 * (Vite dev server on :5173) which proxies /api + /socket.io to the FastAPI
 * backend on :8000. See e2e/README.md for the full run instructions and the
 * exact demo credentials.
 *
 * Browser: this environment ships a PRE-INSTALLED Chromium at
 *   /opt/pw-browsers/chromium
 * but its build revision does NOT match the one Playwright 1.61 would download
 * (downloads are blocked here). We therefore pin `executablePath` to the
 * pre-installed binary instead of relying on Playwright's managed browsers.
 * If you run this on a normal machine where `npx playwright install chromium`
 * succeeded, unset ORB_CHROMIUM_PATH (below) and Playwright will use its own.
 */

// Allow overriding the browser binary. Defaults to the pre-installed Chromium
// in this sandbox; on a normal host, leave it empty to use Playwright's managed
// browser (run `npx playwright install chromium` first).
const CHROMIUM_PATH =
  process.env.ORB_CHROMIUM_PATH ??
  (process.env.PLAYWRIGHT_BROWSERS_PATH ? '/opt/pw-browsers/chromium' : '')

const BASE_URL = process.env.ORB_E2E_BASE_URL ?? 'http://localhost:5173'
const BACKEND_URL = process.env.ORB_E2E_BACKEND_URL ?? 'http://localhost:8000'

// Set ORB_E2E_NO_WEBSERVER=1 if you prefer to boot the backend + vite yourself
// (see e2e/README.md). Otherwise Playwright boots BOTH for you and reuses any
// instance that is already listening.
const MANAGE_SERVERS = process.env.ORB_E2E_NO_WEBSERVER !== '1'

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
          // Pin the pre-installed Chromium when present (sandbox); empty string
          // means "use Playwright's managed browser".
          ...(CHROMIUM_PATH ? { executablePath: CHROMIUM_PATH } : {}),
          args: ['--no-sandbox', '--disable-dev-shm-usage'],
        },
      },
    },
  ],

  // Boot BOTH servers. Playwright supports an array of webServers; it waits for
  // each `url` to respond before starting the tests, and (because
  // reuseExistingServer is true) it will happily attach to instances you've
  // already started manually rather than spawning duplicates.
  webServer: MANAGE_SERVERS
    ? [
        {
          // FastAPI + Socket.IO backend (simulation ingestion, dev profile).
          // The ASGI object exported by server/main.py is `socket_app`.
          command:
            'cd server && . .venv-dev/bin/activate && ' +
            'ORB_DEPLOYMENT_PROFILE=dev ORB_INGESTION_MODE=simulation ' +
            'python -m uvicorn main:socket_app --port 8000',
          url: `${BACKEND_URL}/docs`,
          reuseExistingServer: true,
          timeout: 120_000,
          stdout: 'pipe',
          stderr: 'pipe',
        },
        {
          // Vite dev server (proxies /api + /socket.io -> :8000).
          command: 'npm run dev',
          url: BASE_URL,
          reuseExistingServer: true,
          timeout: 120_000,
          stdout: 'pipe',
          stderr: 'pipe',
        },
      ]
    : undefined,
})
