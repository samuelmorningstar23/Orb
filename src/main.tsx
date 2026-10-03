import { StrictMode, Component, type ErrorInfo, type ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
// Self-hosted type system (bundled woff2 — no CDN, offline + CSP safe).
// Inter = all UI/body text; Fraunces = display serif for brand/page titles/hero
// moments; JetBrains Mono = data/IDs/codes. Imported before index.css so
// @font-face is ready. (Manrope + Space Grotesk retired in the flat redesign;
// Inter is imported again — index.css referenced it while only Manrope was
// bundled, so the whole app silently fell back to the system font.)
import '@fontsource-variable/inter/index.css'
import '@fontsource-variable/fraunces/index.css'
import '@fontsource-variable/jetbrains-mono/index.css'
import './index.css'
import './print.css'
import './i18n'
import App from './App.tsx'

// Website build only (vite.demo.config.ts): answer every API call from
// recordings so the site can embed the real product with no backend.
//
// The harness lives in ../demo/, OUTSIDE the app source tree, and this is the
// single line that reaches it. src/ is the product; nothing in it is written
// for a demonstration. Dead code in the normal build — VITE_DEMO_MODE is
// undefined there, so the dynamic import is dropped at build time.
if (import.meta.env.VITE_DEMO_MODE) {
  const { ready } = await import('../demo/shim')
  await ready
}

// ── Stale-deploy self-healing ──
// After a rebuild, this tab's hashed chunk files are deleted from dist and
// purged from the SW precache (cleanupOutdatedCaches), so lazy imports fail.
// Vite signals exactly this via 'vite:preloadError' — reload once to pick up
// the new index.html/hashes. Time-guarded (same key as PanelErrorBoundary's
// guard in ClinicianShell) to avoid a reload loop if the server is down.
window.addEventListener('vite:preloadError', ((event: Event) => {
  event.preventDefault() // suppress the raw rejection (avoids the error overlay)
  const KEY = 'orb:chunk-reload-at'
  const last = Number(sessionStorage.getItem(KEY) || 0)
  if (Date.now() - last < 30_000) return // already tried; let error boundaries show
  sessionStorage.setItem(KEY, String(Date.now()))
  window.location.reload()
}) as EventListener)

/**
 * Top-level error boundary. Without this, any uncaught render-time exception
 * (a malformed vitals/forecast payload, a bad timestamp, or a lazy chunk-load
 * failure) unmounts the entire clinical UI to a blank white screen with no
 * recovery path — a patient-safety hazard for a mid-shift clinician. This
 * catches the error and offers a reload affordance instead.
 */
interface ErrorBoundaryState {
  hasError: boolean
  error?: Error
}

class RootErrorBoundary extends Component<{ children: ReactNode }, ErrorBoundaryState> {
  state: ErrorBoundaryState = { hasError: false }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { hasError: true, error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('[Orb] Uncaught UI error:', error, info)
  }

  render() {
    if (this.state.hasError) {
      return (
        <div
          role="alert"
          style={{
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            minHeight: '100vh',
            gap: '1rem',
            padding: '2rem',
            textAlign: 'center',
            fontFamily: 'system-ui, sans-serif',
          }}
        >
          <h1 style={{ fontSize: '1.25rem', margin: 0 }}>Something went wrong</h1>
          <p style={{ maxWidth: 420, opacity: 0.8 }}>
            The interface hit an unexpected error. Your work may still be saved on the
            server. Reload to recover.
          </p>
          <button
            onClick={() => window.location.reload()}
            style={{
              padding: '0.6rem 1.4rem',
              borderRadius: 8,
              border: '1px solid currentColor',
              background: 'transparent',
              cursor: 'pointer',
              font: 'inherit',
            }}
          >
            Reload
          </button>
        </div>
      )
    }
    return this.props.children
  }
}

// Reuse a single root across HMR / double module-evaluation. Calling createRoot()
// twice on the same container triggers "container has already been passed to
// createRoot()" and double-mounts the app (split-screen artifacts). Caching the
// root on window makes re-evaluation re-render the existing root instead.
const container = document.getElementById('root')!
const w = window as unknown as { __orbRoot?: ReturnType<typeof createRoot> }
const root = w.__orbRoot ?? (w.__orbRoot = createRoot(container))
root.render(
  <StrictMode>
    <RootErrorBoundary>
      <App />
    </RootErrorBoundary>
  </StrictMode>,
)
