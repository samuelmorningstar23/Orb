import { useEffect, type ReactNode } from 'react'
import OrbLogo from './OrbLogo'
import { refreshModuleFlags, retryHealthProbe, useStartupState } from '../shellConfig'

/**
 * NO SCREEN IS DRAWN ON A GUESS (Samuel's ruling, 9 October 2026: fix the
 * startup flash).
 *
 * The client used to assume every clinical switch was on until /api/health
 * answered, so a Ward Record computer showed the full rail, Sage and the score
 * widgets for a moment after sign-in or a reload. This wraps the screens that
 * draw from the switches (ClinicianShell, DowntimeMode, HandoffPage) and draws
 * them only once the switches are known (shellConfig, SwitchesSource): from the
 * page the server served, from the probe, or from the copy this browser saved.
 *
 * Until then it draws a neutral frame: the Orb mark and one line, no rail item,
 * no panel, no score. When the probe fails and this browser has nothing saved,
 * it says the server could not be reached, with a Try again button, never the
 * full menu.
 *
 * It asks /api/health once on mount, as each of those screens used to do
 * itself. The answer still wins over the page's switches when it comes.
 */
export default function SwitchesGate({ children }: { children: ReactNode }) {
  const state = useStartupState()
  useEffect(() => { void refreshModuleFlags() }, [])
  if (state === 'ready') return <>{children}</>
  return <StartupFrame unreachable={state === 'unreachable'} />
}

export function StartupFrame({ unreachable }: { unreachable: boolean }) {
  return (
    <main
      className="orb-startup-frame"
      data-testid="orb-startup-frame"
      style={{
        minHeight: '100vh',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        gap: 'var(--space-4, 16px)',
        padding: 'var(--space-6, 24px)',
        background: 'var(--bg-primary)',
        color: 'var(--text-secondary, var(--text-primary))',
        textAlign: 'center',
      }}
    >
      <OrbLogo size={48} />
      {unreachable ? (
        <div role="alert" style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 12 }}>
          <p style={{ margin: 0, color: 'var(--text-primary)', fontSize: '1.05rem', fontWeight: 600 }}>
            Could not reach this computer&apos;s server
          </p>
          <button type="button" className="btn-primary" onClick={() => { void retryHealthProbe() }}>
            Try again
          </button>
        </div>
      ) : (
        <p role="status" aria-live="polite" style={{ margin: 0 }}>Starting Orb on this computer</p>
      )}
    </main>
  )
}
