import { useCallback, useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { apiFetch } from '../api/client'
import { useFocusTrap } from '../hooks/useFocusTrap'
import './MfaEnrollmentModal.css'

interface Props {
  onClose: () => void
  /**
   * Short-lived enrolment ticket from the login 403 (X-MFA-Enrollment-Ticket).
   *
   * When set, the modal runs in PRE-LOGIN mode: there is no session yet, so both
   * enrol calls authenticate with this ticket instead. The ticket is scoped to
   * `mfa_enroll` and only /enroll/begin and /enroll/confirm accept it, so it
   * cannot be used to reach anything else. Without this mode an account in
   * ORB_MFA_REQUIRED_ROLES that has no TOTP factor can never sign in — and on a
   * fresh appliance that is the first and only admin.
   */
  enrollTicket?: string
  /** Called after a successful confirm, so the caller can resume its own flow. */
  onEnrolled?: () => void
}

/** Shape returned by POST /api/auth/mfa/enroll/begin. */
interface EnrollBeginResponse {
  secret: string
  otpauth_uri: string
  issuer: string
  /**
   * Server-rendered SVG QR of otpauth_uri as a `data:image/svg+xml;base64,`
   * URI. OPTIONAL by contract: an appliance without the qrcode package omits
   * the field entirely, and the modal falls back to manual key entry alone.
   */
  qr_svg?: string
}

type View = 'loading' | 'enroll' | 'enrolled' | 'disable' | 'disabled' | 'error'

/** Format a base32 secret into space-separated groups of 4 for legibility.
 *  Guards against a missing/malformed secret so a bad enroll response can never
 *  throw during render (the modal is mounted outside a panel error boundary). */
function groupSecret(secret: string | undefined | null): string {
  if (!secret) return ''
  return (secret.match(/.{1,4}/g) || [secret]).join(' ')
}

/**
 * Self-contained MFA (TOTP) management modal.
 *
 * On open it calls enroll/begin. If MFA is not yet enabled it walks the user
 * through enrolment (show secret + otpauth provisioning URI → confirm a live
 * 6-digit code). If the backend reports MFA is already enabled it flips to the
 * disable flow (current password + a live code, per the backend contract).
 *
 * QR note: the QR is rendered SERVER-side (an SVG data: URI in the enroll/begin
 * payload, from the approved pure-Python `qrcode` package), so the web bundle
 * still takes no new npm dependency. The field is optional — when the server
 * omits it (qrcode package absent) the modal falls back to exactly the manual
 * key-entry flow below, which is always present.
 */
export default function MfaEnrollmentModal({ onClose, enrollTicket, onEnrolled }: Props) {
  // Per-call Authorization override; buildHeaders leaves an explicit one alone.
  const ticketAuth = enrollTicket
    ? { Authorization: `Bearer ${enrollTicket}` }
    : undefined
  const [view, setView] = useState<View>('loading')
  const [enroll, setEnroll] = useState<EnrollBeginResponse | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [copied, setCopied] = useState<'secret' | 'uri' | null>(null)

  // Enrolment confirm code.
  const [confirmCode, setConfirmCode] = useState('')
  // Disable flow inputs.
  const [disablePassword, setDisablePassword] = useState('')
  const [disableCode, setDisableCode] = useState('')

  const modalRef = useFocusTrap<HTMLDivElement>(useCallback(() => onClose(), [onClose]))

  // Begin enrolment on open. A 400 "MFA already enabled" means this account is
  // already enrolled, so we present the disable flow instead.
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const data = await apiFetch<EnrollBeginResponse>('/auth/mfa/enroll/begin', {
          method: 'POST',
          headers: ticketAuth,
        })
        if (cancelled) return
        setEnroll(data)
        setView('enroll')
      } catch (err: any) {
        if (cancelled) return
        const msg = String(err?.message || '')
        if (msg === 'MFA already enabled' && !enrollTicket) {
          setView('disable')
        } else if (msg === 'MFA already enabled') {
          // Pre-login: the account enrolled elsewhere while this screen was open.
          // Offering the disable flow here would be wrong (it needs a session),
          // so send them back to sign in with the code they now have.
          setError('This account is already enrolled. Sign in with your authenticator code.')
          setView('error')
        } else {
          setError(msg || 'Could not start MFA setup.')
          setView('error')
        }
      }
    })()
    return () => { cancelled = true }
  }, [])

  const copy = async (value: string, which: 'secret' | 'uri') => {
    try {
      await navigator.clipboard?.writeText(value)
      setCopied(which)
      setTimeout(() => setCopied(null), 1500)
    } catch {
      /* clipboard unavailable — the value is selectable on screen */
    }
  }

  const handleConfirm = async (e: FormEvent) => {
    e.preventDefault()
    const code = confirmCode.trim()
    if (code.length < 6) {
      setError('Enter the 6-digit code from your authenticator app.')
      return
    }
    setBusy(true)
    setError('')
    try {
      await apiFetch('/auth/mfa/enroll/confirm', {
        method: 'POST',
        body: JSON.stringify({ totp_code: code }),
        headers: ticketAuth,
      })
      setView('enrolled')
      onEnrolled?.()
    } catch (err: any) {
      setError(String(err?.message || 'Could not verify the code.'))
    } finally {
      setBusy(false)
    }
  }

  const handleDisable = async (e: FormEvent) => {
    e.preventDefault()
    if (!disablePassword) {
      setError('Enter your password.')
      return
    }
    const code = disableCode.trim()
    if (code.length < 6) {
      setError('Enter the 6-digit code from your authenticator app.')
      return
    }
    setBusy(true)
    setError('')
    try {
      await apiFetch('/auth/mfa/disable', {
        method: 'POST',
        body: JSON.stringify({ password: disablePassword, totp_code: code }),
      })
      setView('disabled')
    } catch (err: any) {
      setError(String(err?.message || 'Could not disable MFA.'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="mfa-modal-backdrop" onClick={onClose}>
      <div
        className="mfa-modal glass-card"
        ref={modalRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="mfa-modal-title"
        onClick={e => e.stopPropagation()}
      >
        <div className="mfa-modal__header">
          <h2 className="mfa-modal__title" id="mfa-modal-title">
            {view === 'disable' || view === 'disabled'
              ? 'Two-factor authentication'
              : 'Set up two-factor authentication'}
          </h2>
          <button className="mfa-modal__close" onClick={onClose} aria-label="Close">&times;</button>
        </div>

        <div className="mfa-modal__body">
          {error && <div className="mfa-modal__error" role="alert">{error}</div>}

          {view === 'loading' && (
            <p className="mfa-modal__muted">Preparing your authenticator setup…</p>
          )}

          {view === 'error' && (
            <div className="mfa-modal__actions">
              <button type="button" className="mfa-btn mfa-btn--primary" onClick={onClose}>
                Close
              </button>
            </div>
          )}

          {view === 'enroll' && enroll && (
            <form onSubmit={handleConfirm}>
              <ol className="mfa-modal__steps">
                <li>Open your authenticator app (Google Authenticator, Authy, 1Password…).</li>
                {enroll.qr_svg ? (
                  <li>Scan the QR code below, or add the account with <strong>manual key entry</strong> using the setup key.</li>
                ) : (
                  <li>Add a new account using <strong>manual key entry</strong> and paste the setup key below.</li>
                )}
                <li>Enter the 6-digit code the app shows to confirm.</li>
              </ol>

              {enroll.qr_svg && (
                <div className="mfa-modal__qr">
                  <img
                    className="mfa-modal__qr-img"
                    src={enroll.qr_svg}
                    alt="QR code for authenticator enrolment, encodes the same setup key shown below"
                    width={200}
                    height={200}
                  />
                  <p className="mfa-modal__hint">Can&rsquo;t scan? Use the setup key below.</p>
                </div>
              )}

              <label className="mfa-modal__label">Setup key</label>
              <div className="mfa-modal__secret">
                <code className="mfa-modal__secret-value">{groupSecret(enroll.secret)}</code>
                <button
                  type="button"
                  className="mfa-btn mfa-btn--ghost"
                  onClick={() => copy(enroll.secret, 'secret')}
                >
                  {copied === 'secret' ? 'Copied' : 'Copy'}
                </button>
              </div>

              <label className="mfa-modal__label">Provisioning URI</label>
              <div className="mfa-modal__secret">
                <code className="mfa-modal__secret-value mfa-modal__secret-value--uri">{enroll.otpauth_uri}</code>
                <button
                  type="button"
                  className="mfa-btn mfa-btn--ghost"
                  onClick={() => copy(enroll.otpauth_uri, 'uri')}
                >
                  {copied === 'uri' ? 'Copied' : 'Copy'}
                </button>
              </div>
              <p className="mfa-modal__hint">
                Account: <strong>{enroll.issuer}</strong> · algorithm SHA1 · 6 digits · 30s period.
              </p>

              <label className="mfa-modal__label" htmlFor="mfa-confirm-code">
                Verification code
              </label>
              <input
                id="mfa-confirm-code"
                className="mfa-modal__code-input"
                type="text"
                inputMode="numeric"
                autoComplete="one-time-code"
                pattern="[0-9]*"
                maxLength={6}
                placeholder="000000"
                value={confirmCode}
                onChange={e => setConfirmCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
                autoFocus
              />

              <div className="mfa-modal__actions">
                <button type="button" className="mfa-btn mfa-btn--ghost" onClick={onClose} disabled={busy}>
                  Cancel
                </button>
                <button type="submit" className="mfa-btn mfa-btn--primary" disabled={busy}>
                  {busy ? 'Verifying…' : 'Enable MFA'}
                </button>
              </div>
            </form>
          )}

          {view === 'enrolled' && (
            <>
              <p className="mfa-modal__success">✓ Two-factor authentication is now enabled.</p>
              <p className="mfa-modal__muted">
                You'll be asked for a code from your authenticator app each time you sign in.
              </p>
              {enrollTicket && (
                // The server's replay guard (consume_totp) burns the code just
                // used to confirm enrolment, so reusing it to sign in fails with
                // "Invalid MFA code" — which reads like a broken setup rather
                // than working anti-replay. Say so before they try it.
                <p className="mfa-modal__muted">
                  <strong>Wait for your authenticator to show the next code</strong> before signing
                  in: the code you just used cannot be reused.
                </p>
              )}
              <div className="mfa-modal__actions">
                <button type="button" className="mfa-btn mfa-btn--primary" onClick={onClose}>
                  {enrollTicket ? 'Continue to sign in' : 'Done'}
                </button>
              </div>
            </>
          )}

          {view === 'disable' && (
            <form onSubmit={handleDisable}>
              <p className="mfa-modal__muted">
                MFA is currently <strong>enabled</strong> for your account. To turn it off, confirm
                your password and a current authenticator code.
              </p>

              <label className="mfa-modal__label" htmlFor="mfa-disable-password">Password</label>
              <input
                id="mfa-disable-password"
                className="mfa-modal__input"
                type="password"
                autoComplete="current-password"
                value={disablePassword}
                onChange={e => setDisablePassword(e.target.value)}
                autoFocus
              />

              <label className="mfa-modal__label" htmlFor="mfa-disable-code">Authenticator code</label>
              <input
                id="mfa-disable-code"
                className="mfa-modal__code-input"
                type="text"
                inputMode="numeric"
                autoComplete="one-time-code"
                pattern="[0-9]*"
                maxLength={6}
                placeholder="000000"
                value={disableCode}
                onChange={e => setDisableCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
              />

              <div className="mfa-modal__actions">
                <button type="button" className="mfa-btn mfa-btn--ghost" onClick={onClose} disabled={busy}>
                  Cancel
                </button>
                <button type="submit" className="mfa-btn mfa-btn--danger" disabled={busy}>
                  {busy ? 'Disabling…' : 'Disable MFA'}
                </button>
              </div>
            </form>
          )}

          {view === 'disabled' && (
            <>
              <p className="mfa-modal__success">Two-factor authentication has been disabled.</p>
              <div className="mfa-modal__actions">
                <button type="button" className="mfa-btn mfa-btn--primary" onClick={onClose}>
                  Done
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
