/**
 * AlertPermissionButton
 *
 * Icon-sized header control that governs critical-alert delivery. It sits beside
 * the theme toggle and reflects three states of the browser Notification API:
 *
 *   - "default"  (permission never requested) → click requests permission
 *   - "granted"  → click toggles the alert chime (mute / unmute)
 *   - "denied"   → disabled; tooltip explains it must be re-enabled in the browser
 *
 * Fully self-contained and theme-token styled (readable in light + dark).
 */

import { useEffect, useState } from 'react';
import {
  isAlertSoundMuted,
  toggleAlertSoundMuted,
} from '../services/criticalAlerts';

/* Line icons on the same 24-box grid, 1.6 stroke, as every other icon in the
   header. This control used to render 🔕 / 🔔 — the only emoji in the global
   chrome, sitting between five line-SVG buttons. An emoji also paints in its
   own colours, so the carefully chosen --accent-text / --text-secondary /
   --btn-disabled-text on this button were decorative: the glyph looked
   identical in all three states and in all three themes, and read as a
   permanent warning badge because that is simply what 🔕 looks like. */
const BellOn = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6"
       strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9" />
    <path d="M13.73 21a2 2 0 0 1-3.46 0" />
  </svg>
);

const BellOff = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6"
       strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M18 8a6 6 0 0 0-9.33-5" />
    <path d="M6.26 6.26A5.86 5.86 0 0 0 6 8c0 7-3 9-3 9h14" />
    <path d="M18 8c0 2.1.27 3.74.62 5" />
    <path d="M13.73 21a2 2 0 0 1-3.46 0" />
    <line x1="2" y1="2" x2="22" y2="22" />
  </svg>
);

type PermState = 'unsupported' | 'default' | 'granted' | 'denied';

function readPermission(): PermState {
  if (typeof window === 'undefined' || !('Notification' in window)) {
    return 'unsupported';
  }
  return Notification.permission as PermState;
}

export default function AlertPermissionButton() {
  const [perm, setPerm] = useState<PermState>(() => readPermission());
  const [muted, setMuted] = useState<boolean>(() => isAlertSoundMuted());

  // Keep permission state fresh if it changes elsewhere (e.g. another tab / the
  // browser UI). The Permissions API isn't universally available, so we also
  // re-read on window focus.
  useEffect(() => {
    const sync = () => setPerm(readPermission());
    window.addEventListener('focus', sync);
    return () => window.removeEventListener('focus', sync);
  }, []);

  if (perm === 'unsupported') return null;

  const handleClick = async () => {
    if (perm === 'default') {
      try {
        const result = await Notification.requestPermission();
        setPerm(result as PermState);
      } catch {
        setPerm(readPermission());
      }
      return;
    }
    if (perm === 'granted') {
      setMuted(toggleAlertSoundMuted());
      return;
    }
    // denied → nothing actionable in-app
  };

  const disabled = perm === 'denied';

  // The icon now carries the state. It previously did not: `muted` and unmuted
  // were BOTH 🔔, so the only difference between "this ward will be paged" and
  // "this ward will not be paged" was a subtle colour shift on an emoji that
  // ignores colour. Sound off is a slashed bell, sound on is a bell.
  let ringing: boolean;
  let label: string;
  if (perm === 'default') {
    ringing = false;
    label = 'Enable critical-alert notifications';
  } else if (perm === 'denied') {
    ringing = false;
    label = 'Notifications blocked: enable notifications in browser settings';
  } else if (muted) {
    ringing = false;
    label = 'Critical-alert sound muted: click to unmute';
  } else {
    ringing = true;
    label = 'Critical-alert sound on: click to mute';
  }

  const active = perm === 'granted' && !muted;

  return (
    <button
      type="button"
      // Same class as the theme toggle beside it: 34px, --radius-sm, hairline
      // border, transparent fill, 18px stroke icon. This button was 36px with a
      // 10px radius and a --glass-sheen fill behind a backdrop-filter — three
      // deviations from its five neighbours plus a blur the design system
      // retired, which made the one alert control in the product look like it
      // had been pasted in from another app.
      className={`shell__icon-toggle${active ? ' shell__icon-toggle--on' : ''}`}
      onClick={handleClick}
      disabled={disabled}
      title={label}
      aria-label={label}
      aria-pressed={perm === 'granted' ? !muted : undefined}
    >
      {ringing ? <BellOn /> : <BellOff />}
    </button>
  );
}
