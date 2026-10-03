import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { VitePWA } from 'vite-plugin-pwa'

// https://vite.dev/config/
export default defineConfig({
  // Belt and braces for the no-console lint rule: any console.log/info/debug
  // that slips into app code is dropped from the production bundle by the
  // minifier, so a clinical browser never prints them. warn and error stay.
  esbuild: {
    pure: ['console.log', 'console.info', 'console.debug'],
  },
  build: {
    rollupOptions: {
      output: {
        // Split the big, rarely-changing vendor libraries into their own
        // long-cached chunks so an app-code change doesn't re-download them,
        // and the entry chunk stays lean. recharts already lands in a lazy
        // chunk (SizedChart); this covers the eager vendors in the entry.
        manualChunks(id) {
          if (!id.includes('node_modules')) return
          if (/[\\/]node_modules[\\/](react|react-dom|react-router|react-router-dom|scheduler)[\\/]/.test(id)) return 'vendor-react'
          if (/[\\/]node_modules[\\/]socket\.io-client[\\/]/.test(id) || /[\\/]node_modules[\\/]engine\.io/.test(id)) return 'vendor-socket'
          if (/[\\/]node_modules[\\/](react-markdown|remark|micromark|mdast|unist|hast|property-information|dompurify)/.test(id)) return 'vendor-markdown'
          if (/[\\/]node_modules[\\/]i18next/.test(id) || /[\\/]node_modules[\\/]react-i18next/.test(id)) return 'vendor-i18n'
        },
      },
    },
  },
  plugins: [
    react(),
    VitePWA({
      registerType: 'autoUpdate',
      devOptions: {
        enabled: false
      },
      workbox: {
        // Precache fonts too (woff2) so an installed / offline launch keeps the
        // self-hosted typography instead of falling back to system fonts.
        globPatterns: ['**/*.{js,css,html,ico,png,svg,woff2}'],
        navigateFallbackDenylist: [/^\/api/, /^\/uploads/, /^\/socket.io/],
      },
      includeAssets: ['favicon.svg', 'apple-touch-icon.png'],
      manifest: {
        name: 'Orb Hospital OS',
        short_name: 'Orb',
        description: 'Local-first hospital operating system',
        theme_color: '#0a0a0a',
        background_color: '#0a0a0a',
        display: 'standalone',
        start_url: '/',
        // Icons make the PWA installable on Chrome/Android (was missing entirely).
        icons: [
          { src: 'pwa-192.png', sizes: '192x192', type: 'image/png' },
          { src: 'pwa-512.png', sizes: '512x512', type: 'image/png' },
          { src: 'pwa-maskable-512.png', sizes: '512x512', type: 'image/png', purpose: 'maskable' },
        ],
      }
    })
  ],
  server: {
    host: '0.0.0.0',  // Bind to 0.0.0.0 (IPv4 explicitly) — accessible on LAN
    allowedHosts: true,
    watch: {
      // The visual-audit harness writes screenshots/reports while the app is
      // being driven — watching them causes full page reloads mid-audit.
      ignored: ['**/tests/visual/out*/**', '**/test-results/**', '**/playwright-report/**'],
    },
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/uploads': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/socket.io': {
        target: 'http://localhost:8000',
        ws: true,
        changeOrigin: true,
      },
    },
  },
})
