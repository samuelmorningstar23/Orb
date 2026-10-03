import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { fileURLToPath } from 'node:url'

/**
 * Website build: the real front end, no backend.
 *
 * Used by the website (orbsuite.com) to embed the running product. Every
 * /api request is answered by demo/shim.ts from recordings of the real
 * server, and socket.io-client is replaced by demo/fakeSocket.ts, which
 * replays the recorded realtime frames. Nothing in the clinical code changes;
 * the shim is imported first by main.tsx only when VITE_DEMO_MODE is set.
 *
 * THE HARNESS LIVES OUTSIDE src/. src/ is the product and contains no code
 * written for a demonstration; everything that exists to show Orb rather than
 * to run it is in demo/, reachable only through this config.
 *
 *   npx vite build --config vite.demo.config.ts --outDir ../Orb-website/public/demo
 */
export default defineConfig({
  base: '/demo/',
  define: {
    'import.meta.env.VITE_DEMO_MODE': JSON.stringify('1'),
  },
  resolve: {
    alias: {
      'socket.io-client': fileURLToPath(new URL('./demo/fakeSocket.ts', import.meta.url)),
    },
  },
  plugins: [
    react(),
    {
      // The dev-only error overlay in index.html keys off ports 5173/5174; the
      // website's dev server is 5174, so strip it from the demo build.
      name: 'demo-strip-dev-overlay',
      transformIndexHtml(html) {
        return html
          .replace(/<script id="error-catcher">[\s\S]*?<\/script>/, '')
          .replace(/<title>Orb<\/title>/, '<title>Orb demo</title>')
      },
    },
  ],
  build: {
    emptyOutDir: true,
    sourcemap: false,
    chunkSizeWarningLimit: 1500,
  },
})
