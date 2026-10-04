import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { useState, useEffect } from 'react'
import { getUser, clearToken, setUser as storeUser, deauthenticateSocket, clearOfflineQueue, logoutSession } from './api/client'
import { useIdleTimer } from './hooks/useIdleTimer'
import { clearNurseDrafts } from './lib/nurseDrafts'
import { clearOfflineCache } from './lib/offline/phiCache'
import { useOfflineSession } from './hooks/useOfflineSession'
import Login from './pages/Login'
import ClinicianShell from './pages/ClinicianShell'
import PatientShell from './pages/PatientShell'
import DischargeAccess from './pages/DischargeAccess'
import DowntimeMode from './pages/DowntimeMode'
import HandoffPage from './pages/HandoffPage'
import OpdTokenBoard from './pages/OpdTokenBoard'
import SessionExpiredModal from './components/SessionExpiredModal'
import { bootTheme, storedTheme } from './lib/theme'
import { AuthContext, type User } from './App'
import './App.css'

// The root component. It lives here, not in App.tsx, so that App.tsx exports
// no component: App.tsx holds the auth context, useAuth and the user types,
// which the whole app imports from '../App' and the tests mock that way, and a
// module that mixes a component with those breaks React fast refresh
// (react-refresh/only-export-components).

/** Inactivity before a session is dropped.
 *
 * A personal workstation LOCKS after 15 minutes and the same person re-auths.
 * A shared ward tablet is a different threat entirely — it sits on a trolley in
 * a corridor, and the next person to pick it up is usually somebody else — so a
 * PIN session is dropped completely after two minutes, returning the screen to
 * the PIN pad. Signing out rather than locking matters: a lock modal would ask
 * the NEXT nurse to re-authenticate as the PREVIOUS one, which is precisely the
 * shared-identity habit this feature exists to break. Two minutes is tolerable
 * only because signing back in is four digits.
 *
 * This is the comfort layer, not the control: the server mints PIN sessions with
 * a short expiry of their own, so a tampered client cannot hold one open. */
const IDLE_LOCK_MS = 15 * 60_000
const SHARED_DEVICE_IDLE_MS = 2 * 60_000

try {
  // bootTheme reads BOTH storage keys — see lib/theme. Reading only 'orb_theme'
  // here meant a saved choice was invisible at boot from the first toggle
  // onwards, because the toggle migrates that key away.
  document.documentElement.setAttribute('data-theme', bootTheme())
} catch (e) {
  console.error('Failed to initialize theme:', e)
}

function App() {
  // Restore user from localStorage on mount
  const [user, setUser] = useState<User | null>(() => {
    const stored = getUser()
    return stored ? stored as User : null
  })

  // True when the API client reported a 401 ("orb:session-expired") and the
  // user must re-authenticate before continuing.
  const [sessionExpired, setSessionExpired] = useState(false)

  // True when the workstation auto-locked after inactivity. Same blocking
  // re-auth flow as a 401, but triggered client-side by the idle timer.
  const [idleLocked, setIdleLocked] = useState(false)

  // Idle-lock applies only to logged-in staff (non-patient) users, and never
  // while an expiry re-auth or the idle lock itself is already up. Patients on
  // /bridge and the public routes (no user) are skipped: `enabled` gates the
  // timer entirely. Disabling on `idleLocked` also unmounts the timer so it
  // re-arms fresh once the user re-authenticates.
  // Declared before the idle timer below, which signs a shared tablet out
  // through logout().
  const login = (user: User) => {
    setUser(user)
    storeUser(user)
    // The JWT is NOT persisted — the session lives in the HttpOnly cookie set by
    // the server at login (see api/client.login), closing the localStorage
    // XSS-exfiltration hole.
  }

  const logout = () => {
    // FIRST, while the request can still carry this session's credentials:
    // revoke it on the server. Clearing only local state left the session
    // cookie valid, and the next page load reconnected the socket as this
    // user. Not awaited, so the screen clears at once and a dead or
    // unreachable server cannot hold anyone on it.
    void logoutSession()
    setUser(null)
    clearToken()
    deauthenticateSocket()
    // Nurse surfaces mirror in-progress work to localStorage so an interrupted
    // observation is never lost. On a shared ward tablet that draft must not
    // outlive the session: without this, the next nurse to sign in can read the
    // previous nurse's half-entered obs for a named patient. Clearing here
    // covers BOTH ways a session ends — this is also the function the expired
    // session and idle-lock modals call on sign-out.
    clearNurseDrafts()
    // Same reasoning, larger blast radius: the offline cache holds this nurse's
    // whole bay — names, diagnoses, NEWS2, drug charts — so the next person to
    // pick the tablet up could read all of it. Cleared here synchronously as
    // well as by useOfflineSession below, because this is the moment we KNOW
    // the session ended; the hook is the backstop for the ways it ends without
    // running this function.
    clearOfflineCache()
    // And the write side of the same story: anything queued offline is this
    // nurse's clinical text, and it would otherwise replay on the NEXT nurse's
    // session — her name on somebody else's note.
    clearOfflineQueue()
  }

  const idleEnabled =
    !!user && user.role !== 'patient' && !sessionExpired && !idleLocked
  const isSharedDevice = user?.authMethod === 'pin'
  useIdleTimer(
    isSharedDevice ? SHARED_DEVICE_IDLE_MS : IDLE_LOCK_MS,
    // A shared tablet SIGNS OUT (back to the PIN pad for whoever is next); a
    // personal workstation locks for the same person to re-open.
    () => { if (isSharedDevice) logout(); else setIdleLocked(true) },
    idleEnabled,
  )

  // Bind the offline PHI cache to whoever is signed in: wiped on sign-out, on a
  // switch to a different clinician, and on a cold boot that finds a cache from
  // a session no logout ever ended (a tablet whose battery died mid-round).
  useOfflineSession(user?.id ?? null)

  // Listen for session expiry from the API client (dispatched on any 401).
  useEffect(() => {
    const handleSessionExpired = () => {
      // Dedupe: concurrent 401s fire multiple events; only the first matters.
      setSessionExpired(prev => (prev ? prev : true))
    }
    window.addEventListener('orb:session-expired', handleSessionExpired)
    return () => window.removeEventListener('orb:session-expired', handleSessionExpired)
  }, [])

  // Theme auto-matching and listener initialization
  useEffect(() => {
    const mediaQuery = window.matchMedia('(prefers-color-scheme: light)')
    const handleSystemThemeChange = (e: MediaQueryListEvent) => {
      // Only follow the OS while the user has made no choice of their own —
      // and `storedTheme` is what actually knows that, across both keys.
      if (!storedTheme()) {
        document.documentElement.setAttribute('data-theme', e.matches ? 'light' : 'dark')
        window.dispatchEvent(new CustomEvent('theme-changed'))
      }
    }
    mediaQuery.addEventListener('change', handleSystemThemeChange)
    return () => mediaQuery.removeEventListener('change', handleSystemThemeChange)
  }, [])

  return (
    <AuthContext.Provider value={{ user, login, logout }}>
      {sessionExpired && user ? (
        // A real 401 outranks an idle lock — resolve the expiry first.
        <SessionExpiredModal
          user={user}
          onReauthed={() => setSessionExpired(false)}
          onSignOut={() => {
            setSessionExpired(false)
            setIdleLocked(false)
            // Clears token/user + realtime auth; routes then redirect to login.
            logout()
          }}
        />
      ) : idleLocked && user ? (
        <SessionExpiredModal
          user={user}
          reason="idle"
          onReauthed={() => setIdleLocked(false)}
          onSignOut={() => {
            setIdleLocked(false)
            // Clears token/user + realtime auth; routes then redirect to login.
            logout()
          }}
        />
      ) : null}
      <BrowserRouter basename={import.meta.env.BASE_URL === '/' ? undefined : import.meta.env.BASE_URL.replace(/\/$/, '')}>
        <Routes>
          <Route path="/" element={user ? <Navigate to={user.role === 'patient' ? '/bridge' : '/dashboard'} /> : <Navigate to="/login/staff" />} />
          <Route path="/login/:mode" element={user ? <Navigate to={user.role === 'patient' ? '/bridge' : '/dashboard'} /> : <Login />} />
          <Route path="/dashboard/*" element={user && user.role !== 'patient' ? <ClinicianShell /> : <Navigate to="/login/staff" />} />
          <Route path="/bridge/*" element={user && user.role === 'patient' ? <PatientShell /> : <Navigate to="/login/patient" />} />
          <Route path="/discharge/:token" element={<DischargeAccess />} />
          <Route path="/downtime/:patientId" element={user && user.role !== 'patient' ? <DowntimeMode /> : <Navigate to="/login/staff" />} />
          <Route path="/handoff/:patientId" element={user && user.role !== 'patient' ? <HandoffPage /> : <Navigate to="/login/staff" />} />
          {/* The waiting-room token board: a kiosk DEVICE, never a signed-in
              user, so it renders with or without a session and never redirects
              to the sign-in screen. It shows no patient data (ruling OPD-N7). */}
          <Route path="/display/opd" element={<OpdTokenBoard />} />
          <Route path="*" element={<Navigate to="/" />} />
        </Routes>
      </BrowserRouter>
    </AuthContext.Provider>
  )
}

export default App
