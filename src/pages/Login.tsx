import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useParams, useNavigate } from 'react-router-dom'
import { useAuth, type User, type UserRole } from '../App'
import { apiFetch, setUser, authenticateSocket, type ApiPermissionError, type LoginResponse } from '../api/client'
import { getDeviceStatus, bindDevice, pinLogin, type DeviceStatus } from '../api/deviceAuth'
import OrbLogo from '../components/OrbLogo'
import MfaEnrollmentModal from '../components/MfaEnrollmentModal'
import Aurora from '../components/Aurora'
import ClockWidget from '../components/ClockWidget'
import ThemeToggle from '../components/ThemeToggle'
import { useTheme } from '../lib/theme'
import './Login.css'

export default function Login() {
  const { mode } = useParams<{ mode: string }>()
  const navigate = useNavigate()
  const { login } = useAuth()
  const { t } = useTranslation()
  const { theme, toggleTheme } = useTheme()
  const isPatient = mode === 'patient'

  const [identifier, setIdentifier] = useState('')
  const [password, setPassword] = useState('')
  const [dob, setDob] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  // ── PIN-on-device ──────────────────────────────────────────────────────────
  // On an ENROLLED ward tablet the default is a short PIN, not a password: a
  // full password per patient interaction is exactly what drives staff to share
  // one login, and a shared login destroys attribution. The password form stays
  // one tap away — it is the fallback whenever a PIN is locked or unset, which
  // is why a PIN lockout can never strand a clinician mid-shift.
  const [device, setDevice] = useState<DeviceStatus | null>(null)
  const [usePin, setUsePin] = useState(false)
  const [pin, setPin] = useState('')
  // Tablet setup: paste the one-time enrolment secret an administrator issued.
  // Held in component state only, never persisted — the server exchanges it for
  // an HttpOnly cookie so the long-lived credential is out of JavaScript's reach.
  const [showDeviceSetup, setShowDeviceSetup] = useState(false)
  const [deviceSecret, setDeviceSecret] = useState('')

  // Ask the server whether THIS browser is a bound tablet. Never blocks the
  // password form: getDeviceStatus swallows its own errors, so an appliance that
  // cannot answer simply shows the ordinary login.
  useEffect(() => {
    if (isPatient) return
    let cancelled = false
    getDeviceStatus().then(status => {
      if (cancelled) return
      setDevice(status)
      if (status.enrolled) setUsePin(true)
    })
    return () => { cancelled = true }
  }, [isPatient])

  // MFA second-step state. When a correct password belongs to an MFA-enabled
  // account the backend replies 401 detail="mfa_required"; we then reveal a TOTP
  // field and resubmit the SAME credentials plus the code. The credentials are
  // held in `mfaCreds` rather than re-read from the form so the resubmit replays
  // exactly what the password step already verified.
  const [mfaRequired, setMfaRequired] = useState(false)
  const [totpCode, setTotpCode] = useState('')
  const [mfaCreds, setMfaCreds] = useState<{ id: string; pw: string } | null>(null)
  // Self-enrolment ticket from a 403 `mfa_enrollment_required`. Held only for
  // the life of this screen — it is a 10-minute, enrol-scoped credential and
  // deliberately never persisted.
  const [enrollTicket, setEnrollTicket] = useState<string | null>(null)
  // Latched by the modal's onEnrolled so onClose knows whether to advance to
  // the code step or just return to the password step.
  const [enrolled, setEnrolled] = useState(false)

  // The PIN pad is only ever shown on a bound tablet, for staff, and never
  // mid-MFA — an in-flight TOTP step owns the form until it resolves.
  const pinMode = usePin && !!device?.enrolled && !isPatient && !mfaRequired && !showDeviceSetup

  const resetMfa = () => {
    setMfaRequired(false)
    setMfaCreds(null)
    setTotpCode('')
    setEnrollTicket(null)
    setEnrolled(false)
  }

  /** Adopt a successful login response — shared by the password and PIN paths so
   *  the two cannot drift on role normalization or the post-login destination. */
  const applyLoginResponse = (data: LoginResponse, authMethod: 'password' | 'pin' = 'password') => {
    // Normalize the server role into the app's UserRole vocabulary. The backend
    // may emit 'surgeon'; the app models surgeons as doctors in the Surgery
    // department, so map it here instead of letting an unknown role fall through
    // to a stripped-down default tab set in ClinicianShell.
    const rawRole = String(data.user.role || '')
    const normalizedRole: UserRole =
      rawRole === 'surgeon' ? 'doctor' : (rawRole as UserRole)
    const department =
      rawRole === 'surgeon' && !data.user.department ? 'Surgery' : data.user.department

    const user: User = {
      id: data.user.id,
      name: data.user.name,
      role: normalizedRole,
      department,
      registrationNumber: data.user.registration_number,
      authMethod,
    }
    login(user)
    navigate(normalizedRole === 'patient' ? '/bridge' : '/dashboard')
  }

  const handlePinLogin = async (employeeId: string, code: string) => {
    setLoading(true)
    setError('')
    try {
      // The device credential is NOT sent from here — it rides as the HttpOnly
      // `orb_device` cookie. That is what makes a shoulder-surfed PIN worth
      // nothing on a personal laptop.
      applyLoginResponse(await pinLogin(employeeId, code), 'pin')
    } catch (err) {
      setPin('')
      const msg = String((err instanceof Error ? err.message : '') || 'Sign-in failed')
      if (msg.toLowerCase().includes('not enrolled')) {
        // The tablet lost (or never had) its binding. Say so and fall back to
        // the password form rather than letting them retype a PIN that cannot
        // work here.
        setDevice({ enrolled: false })
        setUsePin(false)
        setError('This tablet is not set up for PIN sign-in. Use your password.')
      } else {
        setError(msg)
      }
    } finally {
      setLoading(false)
    }
  }

  const handleBindDevice = async () => {
    setLoading(true)
    setError('')
    try {
      const status = await bindDevice(deviceSecret)
      setDevice(status)
      setDeviceSecret('')
      setShowDeviceSetup(false)
      setUsePin(true)
    } catch (err) {
      setError(String((err instanceof Error ? err.message : '') || 'That setup code was not accepted.'))
    } finally {
      setLoading(false)
    }
  }

  const handleLogin = async (employeeId: string, pw: string, code?: string) => {
    setLoading(true)
    setError('')
    try {
      // Call the login endpoint directly so we can include the optional TOTP
      // second factor. On success this mirrors what api/client `login()` does:
      // the server sets the HttpOnly session cookie, we cache the (non-sensitive)
      // user, and (re)authenticate the realtime socket from the cookie. The JWT is
      // deliberately NOT persisted to localStorage (XSS-exfiltration hole).
      const data = await apiFetch<LoginResponse>('/auth/login', {
        method: 'POST',
        body: JSON.stringify({
          employee_id: employeeId,
          password: pw,
          ...(code ? { totp_code: code } : {}),
        }),
      })
      setUser(data.user)
      authenticateSocket()
      applyLoginResponse(data)
    } catch (err) {
      const msg = String((err instanceof Error ? err.message : '') || 'Login failed')
      if (msg === 'mfa_required') {
        // Password verified — prompt for the authenticator code and remember the
        // credentials so the next submit can replay them with the code.
        setMfaCreds({ id: employeeId, pw })
        setMfaRequired(true)
        setTotpCode('')
        setError('')
      } else if (msg === 'Invalid MFA code') {
        setError('Invalid authenticator code: check the 6-digit code and try again.')
      } else if (msg === 'mfa_enrollment_required') {
        // The password was correct; this account just has no TOTP factor yet.
        // The 403 carries a short-lived, enrol-scoped ticket — enrol in place.
        // "Contact your administrator" was a dead end on a fresh appliance,
        // where this IS the only administrator.
        const ticket = (err as ApiPermissionError | undefined)?.mfaEnrollTicket
        resetMfa()
        if (ticket) {
          setMfaCreds({ id: employeeId, pw })
          setEnrollTicket(ticket)
          setError('')
        } else {
          // No ticket means the header never reached us (an older server, or a
          // cross-origin deployment without expose_headers). Say what to do
          // rather than implying someone else can fix it.
          setError(
            'This account needs an authenticator app before signing in, but the ' +
            'enrolment ticket did not reach the browser. Run the sign-in from the ' +
            'appliance URL itself, or see docs/DEPLOYMENT.md for MFA enrolment.'
          )
        }
      } else {
        // A credential (or other) failure returns to the password step.
        resetMfa()
        setError(msg)
      }
    } finally {
      setLoading(false)
    }
  }

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (showDeviceSetup) {
      if (deviceSecret.trim().length < 16) {
        setError('Paste the full setup code from the administrator.')
        return
      }
      handleBindDevice()
      return
    }
    if (pinMode) {
      if (!identifier.trim()) {
        setError('Enter your staff ID.')
        return
      }
      if (pin.length < 4) {
        setError('Enter your 4-digit PIN.')
        return
      }
      handlePinLogin(identifier.trim(), pin)
      return
    }
    if (mfaRequired && mfaCreds) {
      const code = totpCode.trim()
      if (code.length < 6) {
        setError('Enter the 6-digit code from your authenticator app.')
        return
      }
      handleLogin(mfaCreds.id, mfaCreds.pw, code)
      return
    }
    if (isPatient) {
      // The ID is asked for first: with both fields empty the screen used to
      // ask only for the password.
      if (!identifier.trim()) {
        setError('Please enter your patient ID.')
        return
      }
      // Require a real credential. Never default-fill a hardcoded password
      // ('patient123' was a server-valid shared secret — a PHI auth bypass).
      if (!dob.trim()) {
        setError('Please enter your password.')
        return
      }
      handleLogin(identifier, dob)
    } else {
      handleLogin(identifier, password)
    }
  }

  return (
    <div className="login">
      {/* The WebGL aurora + dot grid that the rest of the product sits on. It is
          purely decorative and paints on fixed canvases at z-index 0/1, which is
          why .login__content is already z-index 2. */}
      <Aurora />

      {/* Pre-login topbar. A ward tablet lives on this screen between shifts, so
          the wall-clock reading a nurse charts against has to be true here too —
          not only after sign-in. The theme switch sits beside it because the
          light/dark choice is a property of the DEVICE (a bright day ward, a
          night shift), and making someone sign in before they can dim the screen
          is the wrong order. */}
      <header className="login__topbar">
        <ClockWidget />
        <ThemeToggle
          theme={theme}
          onToggle={toggleTheme}
          className="login__icon-toggle"
          id="btn-login-theme-toggle"
        />
      </header>

      <div className="login__content">

        <div className="login__card glass-card animate-slide-up">
          <div className="login__card-header">
            <OrbLogo size={40} />
            <h1 className="login__card-title">
              {isPatient ? t('login.patientAccess') : t('login.clinicianLogin')}
            </h1>
            <p className="login__card-subtitle">
              {isPatient ? t('login.bridge') : t('login.clinicalPlatform')}
            </p>
            {/* Which tablet you are on. Named so a nurse can see at a glance that
                she is signing in on the ward device and not a stray browser. */}
            {device?.enrolled && !isPatient && (
              <p className="login__device-badge">{device.label}</p>
            )}
          </div>

          {error && <div className="login__error">{error}</div>}

          <form className="login__form" onSubmit={handleSubmit}>
            {showDeviceSetup ? (
              <div className="login__field">
                <label className="login__label" htmlFor="input-device-secret">
                  Tablet setup code
                </label>
                <textarea
                  className="login__input login__secret-input"
                  rows={3}
                  placeholder="Paste the one-time code from the administrator"
                  value={deviceSecret}
                  onChange={e => setDeviceSecret(e.target.value)}
                  id="input-device-secret"
                  autoFocus
                  aria-describedby="device-setup-hint"
                />
                <p className="login__hint" id="device-setup-hint">
                  An administrator enrols this tablet once and gives you this code.
                  It is exchanged for a stored credential and is not kept in the browser.
                </p>
              </div>
            ) : pinMode ? (
              <>
                <div className="login__field">
                  <label className="login__label" htmlFor="input-pin-id">
                    {t('login.employeeId')}
                  </label>
                  <input
                    className="login__input"
                    type="text"
                    placeholder="NUR001"
                    value={identifier}
                    onChange={e => setIdentifier(e.target.value)}
                    id="input-pin-id"
                    autoComplete="username"
                    autoFocus
                  />
                </div>
                <div className="login__field">
                  <label className="login__label" htmlFor="input-pin">PIN</label>
                  <input
                    className="login__input login__code-input"
                    type="password"
                    inputMode="numeric"
                    pattern="[0-9]*"
                    maxLength={8}
                    placeholder="••••"
                    value={pin}
                    onChange={e => setPin(e.target.value.replace(/\D/g, '').slice(0, 8))}
                    id="input-pin"
                    autoComplete="off"
                    aria-describedby="pin-hint"
                  />
                  <p className="login__hint" id="pin-hint">
                    Your PIN identifies you personally. Everything you do on this
                    tablet is recorded under your name, never share it.
                  </p>
                </div>
              </>
            ) : isPatient ? (
              <>
                <div className="login__field">
                  <label className="login__label">{t('login.patientId')}</label>
                  <input
                    className="login__input"
                    type="text"
                    placeholder="P001"
                    value={identifier}
                    onChange={e => setIdentifier(e.target.value)}
                    disabled={mfaRequired}
                    id="input-registration"
                  />
                </div>
                <div className="login__field">
                  <label className="login__label">{t('login.password')}</label>
                  <input
                    className="login__input"
                    type="password"
                    placeholder={t('login.passwordPlaceholder')}
                    value={dob}
                    onChange={e => setDob(e.target.value)}
                    disabled={mfaRequired}
                    id="input-dob"
                  />
                </div>
              </>
            ) : (
              <>
                <div className="login__field">
                  <label className="login__label">{t('login.employeeId')}</label>
                  <input
                    className="login__input"
                    type="text"
                    placeholder="DOC001"
                    value={identifier}
                    onChange={e => setIdentifier(e.target.value)}
                    disabled={mfaRequired}
                    id="input-employee-id"
                  />
                </div>
                <div className="login__field">
                  <label className="login__label">{t('login.password')}</label>
                  <input
                    className="login__input"
                    type="password"
                    placeholder={t('login.passwordPlaceholder')}
                    value={password}
                    onChange={e => setPassword(e.target.value)}
                    disabled={mfaRequired}
                    id="input-password"
                  />
                </div>
              </>
            )}

            {mfaRequired && (
              <div className="login__field login__field--mfa">
                <label className="login__label" htmlFor="input-totp">Authenticator code</label>
                <input
                  className="login__input login__code-input"
                  type="text"
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  pattern="[0-9]*"
                  maxLength={6}
                  placeholder="000000"
                  value={totpCode}
                  onChange={e => setTotpCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
                  id="input-totp"
                  autoFocus
                  aria-describedby="mfa-hint"
                />
                <p className="login__mfa-hint" id="mfa-hint">
                  Enter the 6-digit code from your authenticator app to finish signing in.
                </p>
              </div>
            )}

            <button className="login__submit" type="submit" id="btn-login" disabled={loading}>
              {loading
                ? t('login.signingIn')
                : showDeviceSetup
                  ? 'Set up this tablet'
                  : mfaRequired
                    ? 'Verify code'
                    : t('login.signIn')}
            </button>

            {mfaRequired && (
              <button
                type="button"
                className="login__mfa-cancel"
                onClick={() => { resetMfa(); setError('') }}
                disabled={loading}
              >
                Use a different account
              </button>
            )}

            {/* The password fallback is ALWAYS one tap away on an enrolled
                tablet. It is what makes a PIN lockout survivable: a locked PIN
                degrades to "use your password", never to "you are out of the
                record mid-shift". */}
            {!isPatient && !mfaRequired && !showDeviceSetup && device?.enrolled && (
              <button
                type="button"
                className="login__mfa-cancel"
                onClick={() => { setUsePin(!usePin); setPin(''); setPassword(''); setError('') }}
                disabled={loading}
                id="btn-toggle-pin"
              >
                {usePin ? 'Use my password instead' : 'Use my PIN'}
              </button>
            )}

            {/* Tablet enrolment. Offered on a browser that is not already bound
                — an enrolled ward tablet never advertises it.
                `showDeviceSetup ||` is the escape hatch, and it is load-bearing:
                the device-status fetch is async, so a tap on this button before
                it resolves opens the setup form on a browser that then turns out
                to BE enrolled. Gating purely on `!device?.enrolled` made the way
                back vanish at that moment and stranded the user in the setup form
                with only a page reload to get out. */}
            {!isPatient && !mfaRequired && (showDeviceSetup || !device?.enrolled) && (
              <button
                type="button"
                className="login__mfa-cancel"
                onClick={() => {
                  setShowDeviceSetup(!showDeviceSetup)
                  setDeviceSecret('')
                  setError('')
                }}
                disabled={loading}
                id="btn-device-setup"
              >
                {showDeviceSetup ? 'Back to sign in' : 'Set up this tablet for PIN sign-in'}
              </button>
            )}
          </form>
        </div>
      </div>

      {/* Pre-login MFA self-enrolment. Reached only via a 403
          `mfa_enrollment_required`, which also issues the ticket this runs on. */}
      {enrollTicket && (
        <MfaEnrollmentModal
          enrollTicket={enrollTicket}
          // Only RECORD success here — clearing the ticket would unmount the
          // modal instantly and the user would never see the success screen or
          // its "wait for the next code" warning. The transition happens on close.
          onEnrolled={() => setEnrolled(true)}
          onClose={() => {
            setEnrollTicket(null)
            if (enrolled) {
              // Hand them back to the code step with the credentials they
              // already proved, so they only type the NEXT code.
              setEnrolled(false)
              setMfaRequired(true)
              setTotpCode('')
              setError('')
            } else {
              // Closed without finishing: back to a clean password step. The
              // ticket is short-lived, so a fresh attempt re-issues one.
              setMfaCreds(null)
            }
          }}
        />
      )}
    </div>
  )
}
