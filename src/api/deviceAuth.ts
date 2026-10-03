/**
 * Orb — PIN-on-device identity (shared ward tablet).
 *
 * WHY THIS EXISTS. Typing a full password before every patient interaction is
 * what makes staff share a login, and a shared login destroys the audit trail:
 * every row says NUR001 when three different nurses touched the chart. So an
 * administrator enrols the tablet ONCE, and a person then signs in on top of
 * that with a short PIN. The session names the PERSON; the tablet is context.
 *
 * WHAT THE BROWSER HOLDS. Nothing readable. The device credential is exchanged
 * once (`bindDevice`) for an HttpOnly `orb_device` cookie, so the long-lived
 * secret is not sitting in localStorage where a single XSS could lift it off
 * the tablet — the same hole the session JWT was moved out of. Every call here
 * goes through `apiFetch`, which already sends `credentials: 'include'`.
 */

import { apiFetch, setUser, authenticateSocket, type LoginResponse } from './client';

/** What `GET /auth/device` reports about THIS browser. */
export interface DeviceStatus {
  enrolled: boolean;
  device_id?: string;
  label?: string;
  department?: string;
  /** Server-side session lifetime in minutes; the SPA idles out well inside it. */
  session_minutes?: number;
}

/**
 * Is this browser a bound ward tablet?
 *
 * Called by the login screen before any session exists, to decide whether to
 * offer the PIN pad at all. Never throws — a device that cannot reach the check
 * simply falls back to the password form, which always works.
 */
export async function getDeviceStatus(): Promise<DeviceStatus> {
  try {
    return await apiFetch<DeviceStatus>('/auth/device');
  } catch {
    return { enrolled: false };
  }
}

/**
 * One-time setup: hand the enrolment secret to the server, which validates it
 * and returns an HttpOnly cookie. After this the raw secret can be thrown away
 * — it is never stored client-side.
 */
export async function bindDevice(deviceSecret: string): Promise<DeviceStatus> {
  const data = await apiFetch<{ device_id: string; label: string; department?: string }>(
    '/auth/device/bind',
    { method: 'POST', body: JSON.stringify({ device_secret: deviceSecret.trim() }) },
  );
  return { enrolled: true, ...data };
}

/**
 * Sign in as a person with a PIN on the bound tablet.
 *
 * Mirrors `login()` in client.ts: the server sets the (short-lived) HttpOnly
 * session cookie, we cache the non-sensitive user for the UI, and the realtime
 * socket re-handshakes from the cookie. The JWT is deliberately NOT persisted.
 *
 * The device credential is NOT passed here — it rides as the HttpOnly cookie.
 * That is the property that makes a shoulder-surfed PIN worthless anywhere
 * else: typed into a personal laptop it carries no device credential and the
 * server refuses it outright.
 */
export async function pinLogin(employeeId: string, pin: string): Promise<LoginResponse> {
  const data = await apiFetch<LoginResponse>('/auth/pin-login', {
    method: 'POST',
    body: JSON.stringify({ employee_id: employeeId, pin }),
  });
  setUser(data.user);
  authenticateSocket();
  return data;
}

/** Set or change your own tablet PIN. Requires the current password. */
export async function setOwnDevicePin(password: string, pin: string): Promise<void> {
  await apiFetch('/auth/device-pin', {
    method: 'POST',
    body: JSON.stringify({ password, pin }),
  });
}
