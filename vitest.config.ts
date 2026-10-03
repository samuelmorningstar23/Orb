import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

// Dedicated test config so the PWA/build plugins in vite.config.ts don't run
// under the test runner. Uses jsdom for component tests and jest-dom matchers.
export default defineConfig({
  plugins: [react()],
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    // demo/ holds the website demo's local copies of server rules (NEWS2), which
    // are tested against the same literal tables as the server's.
    include: ['src/**/*.{test,spec}.{ts,tsx}', 'demo/**/*.{test,spec}.{ts,tsx}'],
    css: false,
  },
})
