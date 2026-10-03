import { useState, useRef, useEffect } from 'react';
import type { FormEvent } from 'react';
import { login } from '../api/client';
import type { User } from '../App';
import { useFocusTrap } from '../hooks/useFocusTrap';
import './SessionExpiredModal.css';

interface Props {
  user: User;
  /** Called after a successful password re-entry (token has been refreshed). */
  onReauthed: () => void;
  /** Called when the user opts to sign out instead of re-authenticating. */
  onSignOut: () => void;
  /**
   * Why the overlay is up. "expired" (default) = a 401 ended the session;
   * "idle" = the workstation auto-locked after inactivity. Both use the same
   * blocking password re-entry + sign-out flow — only the copy differs.
   */
  reason?: 'expired' | 'idle';
}

/**
 * Blocking re-authentication overlay, shown when the API client dispatches
 * "orb:session-expired" (any 401). The user re-enters their password to mint
 * a fresh token without losing in-progress work; Escape does NOT close it —
 * the only ways out are re-auth or an explicit sign-out.
 */
export default function SessionExpiredModal({ user, onReauthed, onSignOut, reason = 'expired' }: Props) {
  const isIdle = reason === 'idle';
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  // No onClose passed: the trap cycles Tab/Shift+Tab inside the dialog and
  // Escape becomes a harmless no-op (blocking modal). Focus moves to the
  // first focusable element — the password input — on mount.
  const modalRef = useFocusTrap<HTMLDivElement>();

  // Refocus AFTER the failed-submit re-render re-enables the input — calling
  // focus() synchronously in the catch block is a no-op while the input still
  // carries the disabled attribute, which would drop keyboard focus entirely.
  useEffect(() => {
    if (error && !isSubmitting) inputRef.current?.focus();
  }, [error, isSubmitting]);

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (isSubmitting || !password) return;
    setIsSubmitting(true);
    setError(null);
    try {
      // The employee id is the stored user's id; login() persists the new
      // token + user and re-handshakes the realtime socket.
      await login(user.id, password);
      onReauthed();
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Re-authentication failed';
      // TOTP-enrolled users cannot complete a password-only re-auth; give them
      // a human path instead of the raw "mfa_required" detail.
      setError(/mfa_required/i.test(msg)
        ? 'Your account requires MFA: sign out and log in again with your authenticator code.'
        : msg);
      setIsSubmitting(false);
    }
  };

  return (
    <div className="session-expired-backdrop">
      <div
        ref={modalRef}
        className="session-expired-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="session-expired-title"
      >
        <h2 id="session-expired-title" className="session-expired-modal__title">
          {isIdle ? 'Screen locked' : 'Session expired'}
        </h2>
        <p className="session-expired-modal__subtitle">
          {isIdle ? (
            <>
              This workstation locked after a period of inactivity. Re-enter your
              password to continue as <strong>{user.name}</strong>.
            </>
          ) : (
            <>
              For your security, your session has ended. Re-enter your password to
              continue as <strong>{user.name}</strong>.
            </>
          )}
        </p>
        <form className="session-expired-modal__form" onSubmit={handleSubmit}>
          <label className="session-expired-modal__label" htmlFor="session-expired-password">
            Password
          </label>
          <input
            ref={inputRef}
            id="session-expired-password"
            className="session-expired-modal__input"
            type="password"
            value={password}
            onChange={e => setPassword(e.target.value)}
            autoComplete="current-password"
            autoFocus
            disabled={isSubmitting}
          />
          {error && (
            <div className="session-expired-modal__error" role="alert">
              {error}
            </div>
          )}
          <div className="session-expired-modal__actions">
            <button
              type="button"
              className="session-expired-modal__btn session-expired-modal__btn--secondary"
              onClick={onSignOut}
              disabled={isSubmitting}
            >
              Sign out
            </button>
            <button
              type="submit"
              className="session-expired-modal__btn session-expired-modal__btn--primary"
              disabled={isSubmitting || !password}
            >
              {isSubmitting ? 'Verifying…' : 'Continue'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
