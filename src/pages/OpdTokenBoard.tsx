import { useEffect, useSyncExternalStore } from 'react'
import ModuleOffNotice from '../components/ModuleOffNotice'
import {
  currentFeatureFlags,
  currentHealthProbe,
  currentSwitchesSource,
  refreshModuleFlags,
  subscribeModuleFlags,
  type ModuleOffState,
} from '../shellConfig'

/**
 * THE OPD TOKEN BOARD KIOSK, /display/opd. PLACEHOLDER.
 *
 * The waiting-room screen that calls tokens (ruling OPD-N7: token, doctor and
 * room only, never a name) is built in a later wave of the HIS plan (opd-9).
 * This wave registers the route so that a display already pointed at it shows
 * a plain sentence rather than bouncing to the sign-in screen.
 *
 * What it deliberately does NOT do, and what opd-9 must keep:
 *   - it signs nobody in and reads no session: a board is a device, not a user;
 *   - it never reads the board key from the address. When the real board lands
 *     the key comes from the URL FRAGMENT (#key=...), which the browser never
 *     sends to a server or writes into a proxy log, and goes out only as a
 *     request header;
 *   - it fetches nothing but the unauthenticated /api/health, which carries no
 *     patient data.
 *
 * The notice follows the 'opd' feature flag: switched off, unreachable, or (when
 * the server says OPD is on) not yet available on this site.
 */
export default function OpdTokenBoard() {
  const features = useSyncExternalStore(subscribeModuleFlags, currentFeatureFlags)
  const probe = useSyncExternalStore(subscribeModuleFlags, currentHealthProbe)
  const source = useSyncExternalStore(subscribeModuleFlags, currentSwitchesSource)

  useEffect(() => { void refreshModuleFlags() }, [])

  /* The board draws no rail and no score, and its notice never guesses: it
     says 'checking' until the switches are known and 'unreachable' when the
     probe fails, so it needs no SwitchesGate. When the page carried the
     switches (9 October 2026) they are the server's answer already, and the
     board says 'off' on its first render instead of 'checking'. */
  const state: ModuleOffState = features.opd === true
    ? 'unavailable'
    : probe === 'pending' && source !== 'page' ? 'checking' : probe === 'failed' ? 'unreachable' : 'off'

  return (
    <main
      className="opd-token-board"
      style={{
        minHeight: '100vh',
        display: 'flex',
        flexDirection: 'column',
        gap: 'var(--space-4)',
        padding: 'var(--space-6)',
        background: 'var(--bg-primary)',
        color: 'var(--text-primary)',
      }}
    >
      <h1 style={{ margin: 0, fontSize: 'var(--text-xl)' }}>Token board</h1>
      <ModuleOffNotice subject="opd" state={state} />
    </main>
  )
}
