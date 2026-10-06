/**
 * Orb Hospital OS — API Client
 * Handles all HTTP requests to the FastAPI backend.
 */

import { io } from 'socket.io-client';
import type { News2, ObsSchedule } from './nurse';
import { newId } from '../lib/id';
import { RELAY_PAGE_SIZE } from '../lib/relayPaging';

/** Dynamic API base — uses current origin so it works across localhost, LAN, and ngrok via proxy */
const API_HOST = window.location.origin;
export const API_BASE = `${API_HOST}/api`;

/**
 * Get a JWT from localStorage.
 *
 * SECURITY: browser sessions no longer PERSIST the JWT — `login()` stops storing
 * it, so this returns null in the normal browser flow and the session is carried
 * by the HttpOnly `orb_session` cookie (unreadable to JS, so an XSS or compromised
 * npm dependency can't exfiltrate a clinician session). This getter is retained
 * only so a persisted token — machine clients, or a legacy value — is still honored
 * as a Bearer header for backward compat (see buildHeaders). New browser code
 * should rely on the cookie + `credentials: 'include'`, never on a stored token.
 */
function getToken(): string | null {
  return localStorage.getItem('orb_token');
}

/**
 * Global WebSocket connection for real-time updates.
 *
 * The JWT is sent in the Socket.IO handshake `auth` payload so the server can
 * authenticate the connection the same way the REST layer does (Bearer token).
 * Without this, the realtime channel is reachable with no auth and the REST
 * Bearer enforcement is bypassed (any client could join any patient room).
 *
 * NOTE (cross-file dependency, server/ws/realtime.py — owned by another cluster):
 * the server's `connect(sid, environ, auth)` handler must read `auth['token']`,
 * verify the JWT, reject unauthenticated clients (raise ConnectionRefusedError),
 * and authorize join_channel / new_message / scribe events against the
 * authenticated user. Sending the token here is necessary but not sufficient on
 * its own; the server-side verification is required to actually close the hole.
 */
export const socket = io(API_HOST, {
  autoConnect: false,
  // withCredentials sends the HttpOnly session cookie with the handshake so the
  // server can authenticate the socket from it (see ws.realtime._authenticate).
  // The `auth.token` is still passed for machine/backward-compat clients that
  // hold a token; browser sessions authenticate purely via the cookie.
  withCredentials: true,
  auth: (cb) => cb({ token: getToken() }),
});
// Open the realtime channel when we have EITHER a persisted token or a session
// cookie (the normal browser case after login); unauthenticated visitors (login
// screen) should not hold an anonymous socket open.
if (getToken() || getCsrfToken()) socket.connect();

/** Re-attach the current token and (re)connect the socket after login. */
export function authenticateSocket(): void {
  // Refresh the handshake auth so a freshly stored token is used on (re)connect.
  socket.auth = { token: getToken() };
  if (socket.connected) {
    socket.disconnect();
  }
  socket.connect();
}

/** Disconnect the socket and drop its auth on logout, so the previous user's
 *  authenticated realtime session is not silently kept open. */
export function deauthenticateSocket(): void {
  socket.auth = { token: null };
  if (socket.connected) {
    socket.disconnect();
  }
}

/** Store JWT token */
export function setToken(token: string): void {
  localStorage.setItem('orb_token', token);
}

/** Clear token on logout */
export function clearToken(): void {
  localStorage.removeItem('orb_token');
  localStorage.removeItem('orb_user');
}

const LOGOUT_TIMEOUT_MS = 5000;

/**
 * End the session ON THE SERVER. Sign-out used to be local only: the HttpOnly
 * session cookie stayed valid, and on the next page load the orb_csrf cookie
 * made the socket reconnect as the previous user.
 *
 * POST /auth/logout revokes the user's sessions (token_version bump) and clears
 * both cookies. A raw fetch, not apiFetch: a 401 here means the session was
 * already dead, which is the outcome sign-out wants, so it must neither try a
 * refresh (that would re-mint the session being ended) nor latch the
 * expired-session modal. Never throws, because sign-out has to finish locally
 * even when the server is unreachable.
 *
 * Call it BEFORE clearing local state: the request headers (any stored Bearer
 * token, the CSRF token) are read synchronously when it starts. orb_csrf is
 * expired here only after the request settles, since the server's
 * double-submit check compares that cookie with the header.
 */
export async function logoutSession(): Promise<boolean> {
  const csrfAtStart = getCsrfToken();
  // Bounded, so a hung request cannot settle minutes later, after somebody
  // else has signed in on this device.
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), LOGOUT_TIMEOUT_MS);
  let ok = false;
  try {
    const r = await fetch(`${API_BASE}/auth/logout`, {
      method: 'POST',
      credentials: 'include',
      headers: buildHeaders('POST'),
      signal: controller.signal,
    });
    ok = r.ok;
  } catch {
    ok = false;
  } finally {
    clearTimeout(timer);
  }
  // The server clears it on success; this covers a dead or unreachable one.
  // Only the cookie this sign-out started with: a different value means a new
  // sign-in has already replaced it, and that session must keep its token.
  if (csrfAtStart && getCsrfToken() === csrfAtStart) {
    document.cookie = 'orb_csrf=; Max-Age=0; path=/; SameSite=Lax';
  }
  return ok;
}

/** Shape of the user object persisted to localStorage. Structurally compatible
 *  with App's `User` type (role kept as `string | null` so the union from App is
 *  assignable here without importing it and creating a circular module ref). */
export interface StoredUser {
  id: string;
  name: string;
  role: string | null;
  department?: string;
  registrationNumber?: string;
  /** How this session was authenticated. 'pin' means a shared ward tablet, which
   *  the app auto-signs-out far sooner — a tablet left on a trolley is the
   *  threat model. Not a credential and not trusted for authorization: the
   *  server enforces the short lifetime on the token itself. */
  authMethod?: 'password' | 'pin';
}

/** Store user data */
export function setUser(user: StoredUser): void {
  localStorage.setItem('orb_user', JSON.stringify(user));
}

/** Get stored user data */
export function getUser(): StoredUser | null {
  const raw = localStorage.getItem('orb_user');
  return raw ? (JSON.parse(raw) as StoredUser) : null;
}

/**
 * Session-expiry contract: when any API call gets a 401 (expired/invalid JWT),
 * we do NOT clear the token or reload the page — a reload would destroy unsaved
 * clinical input (draft notes, in-progress med orders). Instead we dispatch the
 * "orb:session-expired" CustomEvent on window; App.tsx listens for it and
 * renders a re-auth modal over the current screen so the clinician can sign
 * back in without losing work. The failing call still throws
 * Error("Session expired") so each caller's normal error path runs.
 */
function notifySessionExpired(): never {
  window.dispatchEvent(new CustomEvent('orb:session-expired'));
  throw new Error('Session expired');
}

/**
 * Endpoints where a 401 means "that credential was wrong", NOT "your session
 * died". They authenticate a caller who has no session yet, so routing them
 * through the expiry path is wrong twice over: it latches `sessionExpired` in
 * App, so the re-auth modal springs up over the dashboard the moment the user
 * DOES sign in successfully; and it replaces the server's message ("Invalid
 * staff ID or PIN", "This device is not enrolled") with a flat "Session
 * expired", which the sign-in screen then has no way to act on.
 */
const UNAUTHENTICATED_AUTH_PATHS = new Set([
  '/auth/login',
  '/auth/pin-login',
  '/auth/device',
  '/auth/device/bind',
]);

const UNSAFE_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);

/** Read the JS-readable CSRF token the server set alongside the HttpOnly session
 *  cookie at login. Echoed back in the X-CSRF-Token header on state-changing
 *  requests so the server's double-submit check passes (see auth.csrf_token_valid). */
export function getCsrfToken(): string | null {
  const m = document.cookie.match(/(?:^|;\s*)orb_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : null;
}

/**
 * Build request headers.
 *
 * Auth is primarily the HttpOnly `orb_session` cookie (sent automatically with
 * `credentials: 'include'`), so the JWT no longer lives in localStorage where an
 * XSS could read it. A Bearer header is still attached IF a token happens to be
 * present (machine clients / backward compat), but browser sessions rely on the
 * cookie. Cookie-authenticated mutations (POST/PUT/PATCH/DELETE) must carry the
 * CSRF token, so it is added for unsafe methods.
 */
/** Response header carrying the short-lived MFA self-enrolment ticket.
 *  Must match server/routes/auth_routes.py::MFA_ENROLL_TICKET_HEADER. */
export const MFA_ENROLL_TICKET_HEADER = 'X-MFA-Enrollment-Ticket';

export function buildHeaders(method: string = 'GET', extra?: Record<string, string>): Record<string, string> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json', ...(extra || {}) };
  const token = getToken();
  // An explicitly-passed Authorization wins over the ambient session token.
  // The MFA self-enrolment flow depends on this: it authenticates with a
  // short-lived enrolment TICKET while no session exists yet, and letting the
  // ambient token overwrite it would silently send the wrong credential.
  if (token && !headers['Authorization']) headers['Authorization'] = `Bearer ${token}`;
  if (UNSAFE_METHODS.has(method.toUpperCase())) {
    const csrf = getCsrfToken();
    if (csrf) headers['X-CSRF-Token'] = csrf;
  }
  return headers;
}

/** Auth headers for multipart/form-data uploads — like buildHeaders but WITHOUT
 *  a JSON Content-Type, so the browser sets the multipart boundary itself.
 *  Still attaches the CSRF token (uploads are cookie-authenticated POSTs). */
export function buildUploadHeaders(): Record<string, string> {
  const headers: Record<string, string> = {};
  const token = getToken();
  if (token) headers['Authorization'] = `Bearer ${token}`;
  const csrf = getCsrfToken();
  if (csrf) headers['X-CSRF-Token'] = csrf;
  return headers;
}

/**
 * Single-flight session refresh.
 *
 * POST /api/auth/refresh re-mints the HttpOnly session cookie (and rotates the
 * CSRF token) behind the CURRENT session, so a 401 that merely means "the JWT
 * aged out mid-shift" is repaired transparently instead of throwing the re-auth
 * modal over a clinician's unsaved work.
 *
 * REACTIVE-ONLY, deliberately. Proactive rotation would need the session's
 * expiry, and the client has no non-sensitive way to read one: the JWT lives in
 * an HttpOnly cookie (unreadable by design), cookie Max-Age is invisible to JS,
 * and persisting/decoding the login-response token would re-open the exact
 * localStorage hole this architecture closed (setToken still has zero callers).
 * So: any 401 → one refresh → one retry → hard session-expiry on failure.
 *
 * SINGLE-FLIGHT: a dashboard fires many requests in parallel, and when the
 * session lapses they all 401 together. Exactly one refresh goes to the server;
 * concurrent 401 handlers await the same promise. Resolves true when the
 * session was re-minted (retry your request — buildHeaders re-reads the ROTATED
 * CSRF cookie), false when the session is genuinely dead.
 */
let refreshInFlight: Promise<boolean> | null = null;

function refreshSession(): Promise<boolean> {
  if (!refreshInFlight) {
    refreshInFlight = (async () => {
      try {
        // Raw fetch, not apiFetch — a 401 here must never recurse into another
        // refresh attempt.
        const r = await fetch(`${API_BASE}/auth/refresh`, {
          method: 'POST',
          credentials: 'include',
          headers: buildHeaders('POST'),
        });
        return r.ok;
      } catch {
        return false;
      } finally {
        refreshInFlight = null;
      }
    })();
  }
  return refreshInFlight;
}

/** The message for an error response with no readable body: typically a proxy
 *  page while the server restarts. It used to read "Unknown error", which told
 *  a clinician nothing about what to do. */
function unreadableReply(status: number): string {
  return status >= 500
    ? `Orb is not answering properly (error ${status}). Try again shortly.`
    : `The request was refused (error ${status}).`;
}

/** "copay_percent_bp" -> "Copay percent": a request field named in words. The
 *  unit suffixes are storage details the person typing never saw. */
function fieldInWords(name: string): string {
  const words = name.replace(/_(bp|paise|id|iso)$/i, '').replace(/_/g, ' ').trim()
  return words.charAt(0).toUpperCase() + words.slice(1)
}

/**
 * The sentence to show for an error response's `detail`.
 *
 * Orb's own refusals send a string, or an object with a `message`. FastAPI's
 * request validation sends an ARRAY of `{loc, msg}`, which has no `message`,
 * so every typo in a form surfaced as "API error: 422". The first problem is
 * named here with its field in words; anything unreadable falls back to a
 * plain sentence with the status.
 */
export function errorDetailMessage(detail: unknown, status: number): string {
  if (typeof detail === 'string' && detail.trim()) return detail
  if (Array.isArray(detail) && detail.length > 0) {
    const first = detail[0] as { loc?: unknown[]; msg?: unknown }
    const raw = typeof first?.msg === 'string' ? first.msg.replace(/^value error,\s*/i, '').trim() : ''
    const loc = Array.isArray(first?.loc) ? first.loc : []
    const field = [...loc].reverse().find(
      (p): p is string => typeof p === 'string' && !['body', 'query', 'path', 'header'].includes(p),
    )
    if (raw && field) {
      const name = fieldInWords(field)
      if (/^field required$/i.test(raw)) return `${name} is required.`
      return `${name}: ${raw.charAt(0).toLowerCase()}${raw.slice(1)}${/[.!?]$/.test(raw) ? '' : '.'}`
    }
    if (raw) return raw
  }
  if (detail && typeof detail === 'object') {
    const message = (detail as { message?: unknown }).message
    if (typeof message === 'string' && message.trim()) return message
  }
  return unreadableReply(status)
}

/** The structured body of a 403. The RBAC layer sends `permission_denied`
 *  with the permissions involved; other refusals (`mfa_enrollment_required`,
 *  `allergy_override_not_permitted`, ...) carry their own fields. A plain
 *  string detail arrives here as `{ message }`. */
export interface ApiPermissionDetail {
  error?: string;
  message?: string;
  required?: string[];
  missing?: string[];
  [key: string]: unknown;
}

/** Thrown by apiFetch and handleResponseError on a 403. */
export interface ApiPermissionError extends Error {
  status: 403;
  permissions: ApiPermissionDetail;
  /** The MFA self-enrolment ticket, when the server issued one in the
   *  MFA_ENROLL_TICKET_HEADER response header (apiFetch only). */
  mfaEnrollTicket?: string;
}

/** Generic fetch wrapper with error handling */
export async function apiFetch<T>(path: string, options: RequestInit = {}): Promise<T> {
  // Headers are (re)built per attempt: a refresh rotates the CSRF cookie, so a
  // retried mutation must echo the NEW token, not a stale snapshot.
  const doFetch = () =>
    fetch(`${API_BASE}${path}`, {
      ...options,
      // Send the HttpOnly session cookie with every request.
      credentials: 'include',
      headers: buildHeaders((options.method as string) || 'GET', options.headers as Record<string, string>),
    });

  let response = await doFetch();

  if (response.status === 401 && !UNAUTHENTICATED_AUTH_PATHS.has(path)) {
    // The refresh endpoint itself 401ing IS the dead-session verdict — no
    // second refresh for it.
    if (path !== '/auth/refresh' && (await refreshSession())) {
      response = await doFetch();
    }
    if (response.status === 401) {
      notifySessionExpired();
    }
  }

  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: unreadableReply(response.status) }));

    // Structured 403 from RBAC layer
    if (response.status === 403) {
      const detail = typeof error.detail === 'object' ? error.detail : { message: error.detail };
      const permError = new Error(detail.message || 'Insufficient permissions') as ApiPermissionError;
      permError.status = 403;
      permError.permissions = detail;
      // MFA self-enrolment ticket. When a role in ORB_MFA_REQUIRED_ROLES signs in
      // with the right password but has no TOTP factor yet, the server answers
      // 403 mfa_enrollment_required AND issues a 10-minute, enrol-scoped JWT in
      // this header. It is the ONLY way that account can ever finish enrolling.
      // apiFetch throws away the Response, so without lifting the header here it
      // was unreachable from the UI — which hard-locked the first (and only)
      // admin out of a fresh appliance, with no second admin to "contact".
      const ticket = response.headers.get(MFA_ENROLL_TICKET_HEADER);
      if (ticket) permError.mfaEnrollTicket = ticket;
      throw permError;
    }

    // Drug-safety gate: 428 Precondition Required means the order hit severe
    // allergy/interaction alerts and needs an explicit, audited clinician
    // acknowledgement. Throw a typed error carrying the full safety payload so
    // the caller can render the alerts and re-submit with safety_ack.
    if (
      response.status === 428 &&
      typeof error.detail === 'object' &&
      error.detail?.error === 'drug_safety_acknowledgement_required'
    ) {
      const ackError = new Error(
        error.detail.message || 'Drug safety acknowledgement required'
      ) as SafetyAckRequiredError;
      ackError.code = 'safety_ack_required';
      ackError.status = 428;
      ackError.safety = error.detail.safety;
      throw ackError;
    }

    // Some endpoints (e.g. the drug-safety 503 "screen unavailable") return a
    // structured detail object, and request validation returns an array;
    // errorDetailMessage reads all three.
    // Carry the HTTP status so callers (notably the offline-queue replay) can
    // tell a permanent client error (4xx — will never succeed on retry) from a
    // transient one (network / 5xx) and stop retrying forever.
    const apiError: Error & { status?: number; detail?: unknown } = new Error(
      errorDetailMessage(error.detail, response.status),
    );
    apiError.status = response.status;
    // Keep the STRUCTURED body, not just its message. An order set refused by
    // the safety gates returns `{error:'order_set_blocked', blocked:[...],
    // items:[...]}` — the per-item verdicts are the entire clinical value of
    // that response, and flattening it to a sentence threw them away.
    apiError.detail = error.detail;
    throw apiError;
  }

  const data = await response.json();
  return data;
}

/** Helper to handle errors on raw fetch calls (e.g. streaming or uploads) */
export async function handleResponseError(response: Response): Promise<never> {
  if (response.status === 401) {
    // Streaming/upload callers can't be replayed from here (the request body is
    // gone), but the SESSION can still be repaired: refresh once, and only
    // declare the session dead when that fails. On success the caller's normal
    // error path runs and the user's next attempt rides the fresh session.
    if (await refreshSession()) {
      throw new Error('Session refreshed: please retry');
    }
    notifySessionExpired();
  }

  const error = await response.json().catch(() => ({ detail: unreadableReply(response.status) }));

  if (response.status === 403) {
    const detail = typeof error.detail === 'object' ? error.detail : { message: error.detail };
    const permError = new Error(detail.message || 'Insufficient permissions') as ApiPermissionError;
    permError.status = 403;
    permError.permissions = detail;
    throw permError;
  }

  throw new Error(errorDetailMessage(error.detail, response.status));
}


// ─── Server-Sent Events (shared streaming helpers) ───
// Before this, the read/decode/buffer-split loop and the `data:` reconstruction
// were copy-pasted into 7 streaming functions (Sage, Sage voice, Scribe, Bridge,
// Vigil analysis, Lens, Surgery risk), each with subtly different error handling.
// These two helpers centralize the transport+framing so each call site only owns
// its per-event field logic.

/** Read an SSE response body and yield each raw event (the text between the
 *  `\n\n` delimiters). Yields nothing if the body is not readable. */
export async function* readSSEEvents(response: Response): AsyncGenerator<string> {
  const reader = response.body?.getReader();
  if (!reader) return;
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const events = buffer.split('\n\n');
    buffer = events.pop() || '';
    for (const event of events) {
      if (event.trim()) yield event;
    }
  }
}

/** Reconstruct the data payload of a single SSE event: strip the `data: ` prefix
 *  from each line (keeping any non-prefixed lines verbatim, matching the prior
 *  permissive behavior), joined by newlines. */
export function parseSSEData(event: string): string {
  let data = '';
  for (const line of event.split('\n')) {
    data += (line.startsWith('data: ') ? line.slice(6) : line) + '\n';
  }
  return data.replace(/\n$/, '');
}


// ─── Offline Support ───
export interface OfflineRequest {
  id: string;
  url: string;
  method: string;
  /** The request body as the caller passed it (replayed verbatim if a
   *  string, JSON-encoded otherwise). */
  body: unknown;
  timestamp: number;
  /** Idempotency-Key sent with the replay so the server can dedupe a request it
   *  already processed (e.g. the response was lost in transit and we retry). */
  idempotencyKey?: string;
  /** WHO queued this write. A replay goes out on whatever session is signed in
   *  when the connection returns, so without this the previous nurse's note is
   *  filed under the next nurse's name — silently, and long after either of them
   *  could notice. Entries without it were queued by an older build and are
   *  refused rather than attributed to a guess. */
  userId?: string;
}

const OFFLINE_QUEUE_KEY = 'orb_offline_queue';

function getOfflineQueue(): OfflineRequest[] {
  const q = localStorage.getItem(OFFLINE_QUEUE_KEY);
  return q ? JSON.parse(q) : [];
}

function setOfflineQueue(queue: OfflineRequest[]) {
  localStorage.setItem(OFFLINE_QUEUE_KEY, JSON.stringify(queue));
}

/**
 * Drop every queued write. Called on sign-out and on user switch (see
 * hooks/useOfflineSession) — the queue holds free clinical text, patient ids and
 * dose payloads, so on a shared ward tablet it is PHI belonging to whoever is
 * walking away, and it must not survive them.
 *
 * @returns how many writes were discarded.
 */
export function clearOfflineQueue(): number {
  const n = getOfflineQueue().length;
  localStorage.removeItem(OFFLINE_QUEUE_KEY);
  return n;
}

/**
 * Drop queued writes that do NOT belong to `userId`; pass null to drop all.
 *
 * The queue is replayed on whatever session is signed in when the connection
 * returns, so an entry whose author is no longer at the tablet can never be sent
 * truthfully — it would be filed under the wrong clinician. It is also that
 * clinician's PHI sitting on a shared device. Both problems end the same way.
 *
 * @returns the discarded entries, so the caller can decide whether anyone needs
 *          telling (the replay path does; a sign-out does not).
 */
export function clearForeignOfflineQueue(userId: string | null): OfflineRequest[] {
  const queue = getOfflineQueue();
  const foreign = queue.filter(r => !userId || r.userId !== userId);
  if (foreign.length === 0) return [];
  const foreignIds = new Set(foreign.map(r => r.id));
  setOfflineQueue(queue.filter(r => !foreignIds.has(r.id)));
  return foreign;
}

function queueRequest(path: string, method: string, body: unknown, idempotencyKey?: string) {
  const queue = getOfflineQueue();
  queue.push({
    id: newId(),
    url: path,
    method,
    body,
    timestamp: Date.now(),
    idempotencyKey,
    userId: getUser()?.id,
  });
  setOfflineQueue(queue);
}

/**
 * Perform an idempotent write that survives going offline. The Idempotency-Key
 * is used for BOTH the direct request and any offline replay, so the server
 * dedupes the replay instead of double-applying the mutation. When offline the
 * request is queued and the caller's `offlineResult(key)` is returned as an
 * optimistic response; when online it goes straight through with the key header.
 *
 * `key` — pass a STABLE key when the caller can retry the same logical
 * mutation. Minting one per call (the default) protects the offline replay but
 * NOT a user-initiated retry: a bedside dose whose response was lost to a Wi-Fi
 * drop looks like a failure, the nurse taps Given again, a fresh key goes out
 * and the server charts a SECOND dose. Callers that expose a retry affordance
 * for a clinical write must mint one key per attempt and reuse it (see
 * AdministerModal).
 *
 * SAFETY: only route mutations here that are safe to apply WITHOUT a synchronous
 * server response — free-text clinical notes, relay messages, bedside charting of
 * an already-ordered/verified med. Do NOT route safety-gated writes (new
 * medication orders, CPOE) through this: a severe drug-interaction alert (428)
 * can't be acknowledged while offline, so those stay online-required and fail
 * loudly (see addMedication / createOrder).
 */
async function offlineMutate<T>(
  path: string,
  body: unknown,
  offlineResult: (idempotencyKey: string) => T,
  method: string = 'POST',
  key?: string,
): Promise<T> {
  const idempotencyKey = key ?? newId();
  if (!navigator.onLine) {
    queueRequest(path, method, body, idempotencyKey);
    return offlineResult(idempotencyKey);
  }
  return apiFetch<T>(path, {
    method,
    headers: { 'Idempotency-Key': idempotencyKey },
    body: JSON.stringify(body),
  });
}

/** A replay that failed with a permanent client error (4xx that won't self-heal:
 *  validation, gone, forbidden). 408/429 are treated as transient (retryable). */
function isPermanentReplayFailure(err: unknown): boolean {
  const status =
    typeof err === 'object' && err !== null && 'status' in err
      ? (err as { status?: unknown }).status
      : undefined;
  return typeof status === 'number' && status >= 400 && status < 500
    && status !== 408 && status !== 429;
}

/** Re-entrancy guard: the browser can fire 'online' several times in quick
 *  succession, and two interleaved replays would each rewrite the queue from a
 *  divergent view of it. Same-tab only — localStorage is shared across tabs, but
 *  the per-id read-modify-write below plus the server's Idempotency-Key dedupe
 *  bounds a cross-tab race to a harmless double-replay rather than data loss. */
let processingOfflineQueue = false;

export async function processOfflineQueue() {
  if (processingOfflineQueue) return;
  processingOfflineQueue = true;

  // OWNERSHIP, before anything goes on the wire.
  //
  // A replay is sent on the CURRENT session's cookie, so a write queued by the
  // last nurse would be filed against this one — her name on another
  // clinician's note or dose, with nothing on screen to reveal it. The same
  // rule phiCache applies to reads applies here: a queued write is replayed for
  // its author or not at all.
  //
  //   nobody signed in  HOLD. The 'online' event can land on the login screen;
  //                     there is no identity to attribute the write to and no
  //                     one to tell it was dropped. Sign-out and sign-in clear
  //                     the queue, which is where that decision belongs.
  //   another user      DROP, and say so. The tablet has changed hands, so this
  //                     write can never be replayed truthfully.
  //   no author stamp   DROP. Queued by a build that did not record one; it is
  //                     refused rather than attributed to a guess.
  const currentUserId = getUser()?.id ?? null;
  if (!currentUserId) {
    processingOfflineQueue = false;
    return;
  }
  const foreign = clearForeignOfflineQueue(currentUserId);
  if (foreign.length > 0) {
    // Same channel a permanently-failed replay uses: the shell renders it as
    // "something you did offline was NOT saved", which is exactly true.
    window.dispatchEvent(new CustomEvent('orb:offline-sync-failed', { detail: { failed: foreign } }));
  }

  // Process each request and only remove it from the persisted queue once the
  // server has confirmed success. Previously the whole queue was cleared up
  // front, so any replay failure (validation / 4xx / 5xx / partial connectivity)
  // silently dropped a clinical note or medication that the UI had already
  // reported as "saved" — a documentation-gap / patient-safety hazard.
  const failed: OfflineRequest[] = [];
  // Transient failures from THIS run: they stay in the persisted queue for the
  // next online/sync event, but are skipped by the follow-up pass below so a
  // still-unreachable server can't spin the loop.
  const deferred = new Set<string>();

  try {
    // Outer loop picks up anything queued WHILE we were replaying (connectivity
    // flapping is exactly when that happens), which would otherwise sit until
    // the next 'online' event.
    for (;;) {
      const queue = getOfflineQueue().filter((r) => !deferred.has(r.id));
      if (queue.length === 0) break;

      for (const req of queue) {
        try {
          await apiFetch(req.url, {
            method: req.method,
            // Replay with the same Idempotency-Key the original attempt carried, so
            // the server can dedupe if it already processed this request but the
            // response was lost (prevents duplicate med orders / notes).
            headers: req.idempotencyKey ? { 'Idempotency-Key': req.idempotencyKey } : undefined,
            body: typeof req.body === 'string' ? req.body : JSON.stringify(req.body),
          });
        } catch (e) {
          if (isPermanentReplayFailure(e)) {
            // A 4xx that will never succeed on retry (validation error, the order is
            // no longer active, permission revoked). Retrying forever would both spin
            // and hide the failure — the UI already reported this action as "saved".
            // Drop it from the queue and surface it so a clinician can re-do it,
            // rather than silently discarding a note/administration or looping.
            console.error('Queued request permanently failed, dropping:', req.url, e);
            failed.push(req);
          } else {
            console.error('Failed to sync queued request, will retry later:', req.url, e);
            // Keep transient failures (network / 5xx) to retry on the next online/sync
            // event. The shared Idempotency-Key makes a retry safe even if the server
            // already received the original but the response was lost. It is already
            // persisted, so nothing needs writing here.
            deferred.add(req.id);
            continue;
          }
        }
        // Persist after each item so a crash mid-loop never loses confirmed-or-pending
        // work. This is a read-modify-write against CURRENT storage keyed by request
        // id: a request queued during the await above exists only in localStorage, and
        // writing back any array derived from the pre-loop snapshot would erase it.
        setOfflineQueue(getOfflineQueue().filter((r) => r.id !== req.id));
      }
    }
  } finally {
    processingOfflineQueue = false;
  }

  // Surface permanently-failed replays so the app can warn the clinician that a
  // queued action did NOT go through (App-level listener renders a toast).
  if (failed.length > 0) {
    window.dispatchEvent(new CustomEvent('orb:offline-sync-failed', { detail: { failed } }));
  }
}

// Listen for network restoration
window.addEventListener('online', processOfflineQueue);

// ─── Auth ───
export interface LoginResponse {
  token: string;
  user: {
    id: string;
    name: string;
    role: string;
    department?: string;
    registration_number?: string;
  };
}

export async function login(employeeId: string, password: string): Promise<LoginResponse> {
  const data = await apiFetch<LoginResponse>('/auth/login', {
    method: 'POST',
    body: JSON.stringify({ employee_id: employeeId, password }),
  });
  // NOTE: we deliberately do NOT persist data.token — the session lives in the
  // HttpOnly `orb_session` cookie the server set on this response, unreadable to
  // JS. Storing the JWT in localStorage is exactly the XSS-exfiltration hole this
  // change closes. The (non-sensitive) user object is still cached for the UI.
  setUser(data.user);
  // Re-handshake the realtime socket; it now authenticates from the cookie.
  authenticateSocket();
  return data;
}

// ─── Vigil ───
export interface Patient {
  id: string;
  name: string;
  mrn: string;
  bed: string;
  ward: string;
  admission_date: string;
  diagnosis: string;
  age: number;
  sex: string;
  /** NEWS2 of the patient's latest vitals row, scored by the server; null when
   *  nothing is recorded. The board shows this, never `risk_score`. */
  news2?: News2 | null;
  /** When that vitals row was observed. A live tick older than this is stale. */
  news2_at?: string | null;
  /** The same moment as `news2`: its 0-1 INDEX (not a probability), band and
   *  drivers, kept for the round queue and the patient picker. null when
   *  nothing is recorded. */
  risk_score: number | null;
  risk_level: string | null;
  risk_drivers: string[];
  attending_doctor_name?: string;
  admission_department?: string;
}

export async function getPatients(): Promise<Patient[]> {
  return apiFetch('/vigil/patients');
}

/** One stored vitals row as the chart and the sparkline receive it. `source`
 *  is the row's provenance ('simulated' for the simulator), null on a
 *  database that predates the column. */
export interface ChartVitalsRow {
  timestamp: string;
  heart_rate: number | null;
  systolic_bp: number | null;
  diastolic_bp: number | null;
  spo2: number | null;
  temperature: number | null;
  respiratory_rate: number | null;
  source: string | null;
}

/** NEWS2 of the chart's newest vitals row (vigil_service.risk_for_row): the
 *  full score, or the no-vitals stub (score 0, stable, no breakdown) when
 *  nothing is recorded. The CfC overlay adds the `cfc` fields when a trained
 *  checkpoint is loaded. */
export type PatientRisk = Pick<News2, 'score' | 'level' | 'drivers' | 'scoring_model'>
  & Partial<Omit<News2, 'score' | 'level' | 'drivers' | 'scoring_model'>>
  & {
    cfc?: Record<string, { risk_by_horizon: unknown; peak_risk: number }>;
    cfc_primary_risk?: number;
    cfc_seq_len?: number;
  };

/** Recent-vs-earlier vitals direction (vigil_service.get_vitals_trend). */
export interface VitalsTrend {
  trend: 'deteriorating' | 'improving' | 'stable' | 'insufficient_data';
  /** Why there is no trend, on 'insufficient_data' only. */
  reason?: string;
  hr_delta?: number | null;
  sbp_delta?: number | null;
  spo2_delta?: number | null;
}

/** A `patients` row as the chart serves it, plus what the route joins on.
 *  Every column of the row is sent (`p.*`), so columns added by later
 *  migrations arrive too; the named ones are what the chart reads. */
export interface ChartPatientRecord {
  id: string;
  name: string;
  mrn: string;
  bed: string | null;
  ward: string | null;
  admission_date: string | null;
  diagnosis: string | null;
  status: string;
  age: number | null;
  date_of_birth?: string | null;
  sex: string | null;
  attending_doctor_id: string | null;
  attending_doctor_name: string | null;
  on_scale_2?: number | null;
  comorbidities: string[];
  /** The admission this chart belongs to: the active one, else the newest. */
  admission_id: string | null;
  [column: string]: unknown;
}

/** A `clinical_documents` row (a note, a ward round, a lab report). */
export interface ClinicalDocumentRow {
  id: number;
  patient_id: string;
  doc_type: string;
  title: string;
  content: string;
  author_id: string | null;
  created_at: string;
  /** 'simulated' for a simulator's result. */
  source?: string | null;
  idempotency_key?: string | null;
}

/** A `scribe_notes` row. The JSON columns arrive as stored TEXT here; the
 *  verification route is the one that parses them. */
export interface ScribeNoteRow {
  id: number;
  patient_id: string | null;
  author_id: string;
  raw_transcript: string;
  structured_note: string | null;
  note_format: 'soap' | 'discharge' | 'handoff' | 'raw' | null;
  audio_path: string | null;
  verification_json: string | null;
  pipeline_meta_json: string | null;
  created_at: string;
  status?: string;
  version?: number;
  supersedes_note_id?: number | null;
  signed_by?: string | null;
  signed_at?: string | null;
  finalized_at?: string | null;
  rejected_by?: string | null;
  rejected_at?: string | null;
  rejection_reason?: string | null;
  last_edited_by?: string | null;
  last_edited_at?: string | null;
  ai_generated?: number;
  unverified_ack_by?: string | null;
  unverified_ack_at?: string | null;
}

/** A `lens_analyses` row, with `image_url` (the authenticated /uploads path
 *  the screen loads; `image_path` is a location on the appliance's disk). */
export interface LensAnalysisRow {
  id: number;
  patient_id: string | null;
  author_id: string;
  image_path: string;
  image_url: string | null;
  image_type: string | null;
  clinical_context: string | null;
  /** The model's raw read. */
  analysis: string;
  findings_json: string | null;
  created_at: string;
  reviewed_by?: string | null;
  reviewed_at?: string | null;
  reviewed_text?: string | null;
  /** 'simulated' for a simulator's report (migration 46), so the chart can
   *  label it; null for a clinician's own. Every route sends it (SELECT *). */
  source?: string | null;
}

/** A `medications` row on the chart. `unverified` is true for an
 *  AI-extracted or agentic order no pharmacist has verified yet. */
export interface ChartMedicationRow {
  id: number;
  patient_id: string;
  drug_name: string;
  dosage: string;
  route: string;
  frequency: string;
  status: 'active' | 'discontinued' | 'prn';
  prescribed_by: string | null;
  notes: string | null;
  started_at: string;
  source?: string | null;
  verified_by?: string | null;
  verified_at?: string | null;
  unverified: boolean;
}

/** One structured lab result (services/lab_observations.chart_lab_rows),
 *  oldest first. */
export interface ChartLabObservation {
  id: number;
  observation_name: string;
  value_numeric: number | null;
  unit: string | null;
  reference_range: string | null;
  abnormal_flag: string | null;
  status: string | null;
  source: string | null;
  observed_at: string;
  lab_result_id: number | null;
}

/** GET /vigil/patients/{id}: the whole chart in one read. */
export interface PatientDetail {
  patient: ChartPatientRecord;
  risk: PatientRisk;
  trend: VitalsTrend;
  obs_schedule: ObsSchedule;
  lab_observations: ChartLabObservation[];
  problems: { problem: string; icd10_code: string | null; onset_date: string | null; source: string }[];
  site_name: string | null;
  /** Newest first: vitals[0] is the set the command bar shows. */
  vitals: ChartVitalsRow[];
  /** True when vitals[0] came from the simulator. */
  simulated_vitals: boolean;
  notes: ClinicalDocumentRow[];
  scribe_notes: ScribeNoteRow[];
  risk_history: { score: number; level: string; drivers: string[]; timestamp: string }[];
  lens_analyses: LensAnalysisRow[];
  medications: ChartMedicationRow[];
}

export async function getPatientDetail(patientId: string): Promise<PatientDetail> {
  return apiFetch(`/vigil/patients/${patientId}`);
}

/** Where this system's vital signs come from. `simulated` is true whenever
 *  the simulator is writing readings (ORB_INGESTION_MODE is not 'ingestion'),
 *  and the shell then carries the honesty label. No patient data. */
export async function getVitalsFeed(): Promise<{ mode: string; simulated: boolean }> {
  return apiFetch('/vigil/vitals-feed');
}

/** One care phase of a projected clinical course. */
export interface TimelinePhase {
  name: string;
  focus: string;
  /** Free text, e.g. "Day 1-3". */
  typical_days: string;
}

/** GET /vigil/patients/{id}/timeline-projection (services/timeline_service). */
export interface TimelineProjection {
  patient_name: string;
  diagnosis: string;
  age: number | null;
  sex: string | null;
  comorbidities: string[];
  expected_los_days: number | null;
  phases: TimelinePhase[];
  discharge_criteria: string[];
  rationale: string;
  /** Derived from the patient's live risk level, not from the calendar. */
  current_phase_index: number;
  /** 'llm' or 'heuristic' (or the cached row's own source). */
  source: string;
  /** The Ollama model that generated it; null for a heuristic projection. */
  engine: string | null;
}

/** Sage-deduced expected clinical course (phases + expected LOS), cached server-side per patient. */
export async function getTimelineProjection(patientId: string, refresh = false): Promise<TimelineProjection> {
  return apiFetch(`/vigil/patients/${patientId}/timeline-projection${refresh ? '?refresh=true' : ''}`);
}

export async function getPatientVitals(patientId: string, hours: number = 6): Promise<ChartVitalsRow[]> {
  return apiFetch(`/vigil/patients/${patientId}/vitals?hours=${hours}`);
}

/** GET /forecast/patient/{id} (forecast_service.predict_patient_los). The
 *  heuristic fallback says so in `prediction_source`, and `model_trained` is
 *  false while the network has no trained checkpoint. */
export interface PatientForecast {
  predicted_los_days: number;
  discharge_probability: number;
  acuity_trajectory: string;
  confidence: number;
  /** YYYY-MM-DD. */
  estimated_discharge_date: string;
  prediction_source?: 'lstm' | 'heuristic';
  model_trained?: boolean;
}

/**
 * Fetch the per-patient forecast. Routed through apiFetch so it uses the same
 * centralized auth headers and 401/403 handling as every other call (previously
 * VigilPanel did a hand-rolled fetch reading the token straight from localStorage
 * and swallowing all errors, bypassing session-expiry handling).
 * Returns null when no forecast is available rather than throwing.
 */
export async function getPatientForecast(patientId: string): Promise<PatientForecast | null> {
  try {
    return await apiFetch(`/forecast/patient/${patientId}`);
  } catch (e) {
    console.warn('[getPatientForecast] failed:', e);
    return null;
  }
}

/** First observations taken at the admission desk. ACVPU and oxygen are null
 *  when not assessed, never a guess: NEWS2 then reports the set incomplete. */
export interface AdmissionVitals {
  hr?: number;
  bp_sys?: number;
  bp_dia?: number;
  spo2?: number;
  temperature?: number;
  rr?: number;
  consciousness?: string | null;
  on_oxygen?: boolean | null;
}

/** POST /admissions/admit body (admission_routes.AdmissionRequest). */
export interface AdmissionRequest {
  name: string;
  age?: number | null;
  /** Optional, but it is the second factor on the discharge link. */
  date_of_birth?: string | null;
  sex?: string | null;
  phone?: string | null;
  emergency_contact_name?: string | null;
  emergency_contact_phone?: string | null;
  address?: string | null;
  mrn?: string | null;
  admission_source: string;
  admission_type: string;
  department: string;
  ward: string;
  bed: string;
  attending_doctor_id: string;
  referring_clinician?: string | null;
  chief_complaint: string;
  initial_diagnosis?: string | null;
  triage_severity?: number | null;
  comorbidities?: string[];
  past_medical_history?: string | null;
  past_surgical_history?: string | null;
  allergies?: string[];
  current_medications?: string[];
  family_history?: string | null;
  social_history?: string | null;
  vitals?: AdmissionVitals | null;
  admitting_notes?: string | null;
  nursing_notes?: string | null;
  isolation_precautions?: string[];
  code_status?: string | null;
}

/** What a successful admission returns (admission_service.admit_patient). */
export interface AdmissionResult {
  success: boolean;
  patient_id: string;
  admission_id: string;
  channel_id: string;
  mrn: string;
  /** The patient's Bridge login and one-time password. Returned exactly once:
   *  only the hash is stored, so it can never be retrieved again. */
  patient_login_id: string;
  temporary_password: string;
  /** False when no date of birth was recorded, so no discharge link can open. */
  date_of_birth_recorded: boolean;
  /** Null, or the sentence saying this bed was already held. */
  bed_warning: string | null;
}

export async function admitPatient(data: AdmissionRequest): Promise<AdmissionResult> {
  return apiFetch('/admissions/admit', {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

// ─── Relay ───
export interface Channel {
  id: string;
  name: string;
  type: string;
  patient_id?: string;
  mrn?: string;
  diagnosis?: string;
  ward?: string;
  bed?: string;
  risk_score?: number;
  risk_level?: 'critical' | 'elevated' | 'stable';
  unread_count: number;
  last_message?: string;
  last_activity?: string;
  alert_count?: number;
  has_alert: boolean;
}

export interface Message {
  id: number;
  channel_id: string;
  sender_id: string;
  sender_name: string;
  sender_role: string;
  content: string;
  message_type: string;
  timestamp: string;
  // Provenance: true for automated/AI bot posts (simulation vitals/labs, Lens AI,
  // Vigil alerts). The UI badges these so bot chatter is never mistaken for a
  // real clinician message. `source` is 'clinician' | 'bot' | 'simulated' | ...
  // ('simulated': a simulator's lab or imaging result, badged as one).
  is_bot?: boolean;
  source?: string;
  /** Wrong-room warning attached to THIS send, when the detector fired.
   *  Present only on the POST response — the durable copy comes from
   *  getRoomFitFlags(). See services/room_fit_service.py. */
  room_fit?: RoomFitFlag;
  /** CLIENT-ONLY correlation token. Set on an optimistic bubble so the socket
   *  echo of the same send can be matched to it before the POST response comes
   *  back with the real id. Never sent to or returned by the server. */
  client_key?: string;
}

/** A message the wrong-room detector flagged as possibly belonging to another
 *  patient. Room-visible and durable: whoever reads this thread next week needs
 *  to know a foreign clinical fact may have landed in it. */
export interface RoomFitFlag {
  id?: number;
  /** Present on the POST-response copy; `id` on the durable copy. */
  flag_id?: number;
  message_id: number;
  channel_id?: string;
  band: 'flag' | 'review' | 'clear';
  stage: 'deterministic' | 'model';
  score?: number;
  suspected_patient_id?: string | null;
  suspected_patient_name?: string | null;
  suggested_channel_id?: string | null;
  suggested_channel_name?: string | null;
  reasons?: string[];
  markers?: string[];
  suppressors?: string[];
  matched_surface?: string | null;
  ambiguous?: boolean;
  resolution: 'open' | 'confirmed_correct' | 'redirected' | 'auto_cleared';
  resolved_by?: string | null;
  resolved_by_name?: string | null;
  resolved_at?: string | null;
  resolve_reason?: string | null;
  redirected_message_id?: number | null;
  created_at?: string;
}

export interface SageAmbientMemory {
  room_id: string;
  patient_id: string;
  patient_summary: string;
  discussion_summary: string;
  current_concern: string;
  likely_next_steps: string[];
  unresolved_questions: string[];
  active_intents: {
    action: string;
    target: string;
    rationale?: string;
    confidence: number;
    created_at: string;
    updated_at: string;
  }[];
  risk_flags: {
    detail: string;
    rationale?: string;
    confidence: number;
    created_at: string;
    updated_at: string;
  }[];
  last_processed_at: string;
  updated_at: string;
}

/** One alert from the drug-safety screen that ran when the card was confirmed. */
export interface SageHandoffAlert {
  type: 'allergy' | 'interaction' | string;
  severity: string;
  match?: string | null;
  message: string;
}

/** The drug-safety screen behind a medication handoff (Helix lane only). */
export interface SageHandoffSafety {
  blocking: boolean;
  screen_failed: boolean;
  alerts: SageHandoffAlert[];
  coverage_note?: string | null;
}

export interface SageHandoff {
  id: string;
  /** 'blocked': the drug-safety screen stopped the order; nothing was charted. */
  status: 'pending' | 'accepted' | 'failed' | 'unsupported' | 'blocked';
  target_module: string;
  error_summary?: string;
  downstream_ref_id?: string;
  safety?: SageHandoffSafety | null;
}

export interface SageAction {
  id: string;
  room_id: string;
  patient_id: string;
  action_type: string;
  target: string;
  rationale?: string;
  confidence: number;
  state: 'suggested' | 'acknowledged' | 'dismissed' | 'confirmed' | 'failed' | 'superseded';
  version: number;
  source_msg_id?: number;
  created_at: string;
  updated_at: string;
  handoff?: SageHandoff;
}

export async function getChannels(): Promise<Channel[]> {
  return apiFetch('/relay/channels');
}

export async function getAlerts(): Promise<Message[]> {
  return apiFetch('/relay/alerts');
}

/** Resolve or escalate an alert. `status` is the alert's new status; the
 *  full updated message arrives on the socket as `message:update`. */
export async function resolveAlert(messageId: number, action: 'confirm' | 'escalate'): Promise<{ status: string }> {
  return apiFetch(`/relay/alerts/${messageId}/resolve`, {
    method: 'POST',
    body: JSON.stringify({ action })
  });
}

/** The newest page of a thread, or with `beforeId` the page of older messages
 *  just before that message ("Show earlier messages"). Oldest first. */
export async function getMessages(channelId: string, beforeId?: number): Promise<Message[]> {
  const query = beforeId != null
    ? `?limit=${RELAY_PAGE_SIZE}&before_id=${beforeId}`
    : `?limit=${RELAY_PAGE_SIZE}`;
  return apiFetch(`/relay/channels/${channelId}/messages${query}`);
}

/** Mark all messages in a channel as read by the current user (clears the unread badge). */
export async function markChannelRead(channelId: string): Promise<{ status: string; marked_read: number }> {
  return apiFetch(`/relay/channels/${channelId}/read`, { method: 'POST' });
}

/** Wrong-room flags on a channel's messages (durable, room-visible). */
export async function getRoomFitFlags(channelId: string): Promise<RoomFitFlag[]> {
  return apiFetch(`/relay/channels/${channelId}/room_fit`);
}

/** Close a wrong-room warning.
 *  `confirmed_correct` is the OVERRIDE — the clinician looked and says the
 *  message belongs here. `redirected` copies the text into the right room and
 *  leaves the original in place, annotated. */
export async function resolveRoomFit(
  flagId: number,
  decision: 'confirmed_correct' | 'redirected',
  opts?: { reason?: string; targetChannelId?: string },
): Promise<{ status: string; flag: RoomFitFlag; redirected_message?: Message | null }> {
  return apiFetch(`/relay/room_fit/${flagId}/resolve`, {
    method: 'POST',
    body: JSON.stringify({
      decision,
      reason: opts?.reason,
      target_channel_id: opts?.targetChannelId,
    }),
  });
}

export async function getSageMemory(channelId: string): Promise<SageAmbientMemory | null> {
  return apiFetch(`/relay/channels/${channelId}/sage_memory`);
}

export async function getSageActions(channelId: string): Promise<SageAction[]> {
  return apiFetch(`/relay/channels/${channelId}/sage_actions`);
}

export async function transitionSageAction(channelId: string, actionId: string, state: string, version: number, reason?: string): Promise<{status: string, action?: SageAction, handoff?: SageHandoff}> {
  return apiFetch(`/relay/channels/${channelId}/sage_actions/${actionId}/transition`, {
    method: 'POST',
    body: JSON.stringify({ state, version, reason }),
  });
}

// ── Sage Proactive (surveillance insights) ──
export interface SageInsight {
  id: string;
  patient_id: string;
  patient_name?: string;
  insight_type: string;
  severity: 'critical' | 'warning' | 'info';
  title: string;
  detail?: string;
  suggested_action_type?: string | null;
  suggested_action_target?: string | null;
  state: string;
  created_at: string;
}

export async function getSageInsights(): Promise<{ insights: SageInsight[] }> {
  return apiFetch('/sage/insights');
}

export async function transitionInsight(id: string, state: 'dismissed' | 'acted'): Promise<SageInsight> {
  return apiFetch(`/sage/insights/${id}/transition`, {
    method: 'POST',
    body: JSON.stringify({ state }),
  });
}

export async function actOnInsight(id: string): Promise<{ status: string; action_id: string; channel_id: string }> {
  return apiFetch(`/sage/insights/${id}/act`, { method: 'POST', body: JSON.stringify({}) });
}

export async function sendMessage(channelId: string, content: string): Promise<Message> {
  const user = getUser();
  // Relay messages are safe to send offline: they carry no drug-safety gate, and
  // the server dedupes the replay by Idempotency-Key so a lost-response retry
  // can't double-post. Offline we return an optimistic message (negative id marks
  // it pending) so the sender sees their text immediately; it syncs on reconnect.
  return offlineMutate<Message>(
    `/relay/channels/${channelId}/messages`,
    { content, message_type: 'text' },
    () => ({
      id: -Date.now(),
      channel_id: channelId,
      sender_id: user?.id ?? 'me',
      sender_name: user?.name ?? 'You',
      sender_role: user?.role ?? '',
      content,
      message_type: 'text',
      timestamp: new Date().toISOString(),
      source: 'clinician',
    }),
  );
}

// ─── Sage ───
/** One retrieved guideline document on a Sage answer. `cited` means the
 *  model named it; `attribution` says how it came to be shown. */
export interface SageSourceRef {
  document_id?: string;
  title?: string;
  source?: string;
  source_type?: string;
  /** The document's trust tier, an integer: 1-2 the hospital's own policy and
   *  formulary, 3+ external guidance. Null when the source carries none. */
  trust_tier?: number | null;
  is_synthetic?: boolean;
  category?: string;
  fusion_score?: number;
  cited?: boolean;
  attribution?: 'model_cited' | 'system_attributed' | 'context_only';
}

/** The model route a Sage turn took (`route_info` on the server). The
 *  provider layer adds keys over time, so only the read ones are named. */
export interface SageProviderInfo {
  lane?: string;
  provider?: string;
  model?: string;
  preferred_model?: string | null;
  downgraded?: boolean;
  thinking_tier?: string | null;
  thinking_requested?: boolean;
  [key: string]: unknown;
}

/** Timing on a Sage answer, in ms. Null means not measured. */
export interface SageStreamTiming {
  retrieval_ms?: number;
  ttft_ms?: number | null;
  first_content_ms?: number | null;
  total_ms?: number | null;
}

/** The parsed sections of a full Sage answer (the `structured_response`
 *  event). Every section is the model's text, sanitised server-side. */
export interface SageStructuredResponse {
  concise_answer?: string;
  patient_facts_summary?: string;
  local_policy_guidance?: string;
  external_guidance?: string;
  clinical_assessment?: string;
  caveats?: string;
  escalation?: string;
  display_mode?: string;
  action_intent?: string;
  cited_sources?: string[];
}

/** What `querySage` hands its `onMetadata` callback: the stream's `metadata`
 *  event, or (as a separate call) the `structured_response` event wrapped in
 *  `_structured_response`. Fields are optional because the refusal and
 *  out-of-scope paths send a shorter event. */
export interface SageStreamMetadata {
  action_state?: Record<string, unknown> | null;
  sources?: SageSourceRef[];
  citation_basis?: string;
  cited_count?: number;
  attributed_count?: number;
  attributed_sources?: string[];
  confidence?: string;
  confidence_basis?: string;
  escalation?: boolean;
  display_mode?: string;
  query_scope?: string;
  patient_context_used?: boolean;
  patient_id?: string | null;
  patient_name?: string | null;
  patient_source?: string | null;
  data_completeness?: { missing?: string[]; [key: string]: unknown } | null;
  local_policy_found?: boolean;
  provider?: SageProviderInfo | null;
  timing?: SageStreamTiming;
  _structured_response?: SageStructuredResponse;
}

/** `viewContext` is the wire-format description of what the clinician is
 *  looking at (built by buildWireViewContext in contexts/ActiveViewContext:
 *  { tab, patient_id, patient_name, selection }), sent with every Sage turn so
 *  the assistant is aware of the current screen without the user naming it.
 *  The backend whitelists/clamps the fields and treats them as DATA only. */
export async function querySage(
  query: string,
  patientId?: string,
  onChunk?: (chunk: string) => void,
  signal?: AbortSignal,
  onMetadata?: (metadata: SageStreamMetadata) => void,
  viewContext?: Record<string, unknown>,
  onStage?: (stage: 'retrieving' | 'generating', retrievalMs?: number) => void,
  /** THINKING MODE, per request. `true`/omitted = the careful model.
   *
   *  Deliberately a PARAMETER rather than a global read of the composer's
   *  toggle: `querySage` is also used programmatically (ScribePanel's
   *  demographics extraction), and a clinician's chat preference must not
   *  silently re-route a structured extraction over patient documentation to
   *  the model that fabricated patient data on 3 of 8 chart turns. Only the
   *  Sage chat surface passes its toggle; every other caller gets careful. */
  thinking?: boolean,
  /** THREAD id. The server scopes the six turns of history it prepends to the
   *  prompt to this value, so "New Chat" (which mints a fresh one) genuinely
   *  starts a new conversation instead of only clearing the panel. Omit it and
   *  the turn gets NO prior context — which is what every programmatic caller
   *  (Scribe demographics extraction, ward brief) wants. */
  conversationId?: string
): Promise<string> {
  // Merge of two branches: keep the page-aware `view_context` payload (from the
  // Sage streaming work) AND the cookie/CSRF auth (from Deferred A) — no Bearer
  // header; the session cookie authenticates via credentials:'include'.
  // Always sent explicitly. An omitted field also means "careful" on the server
  // (`resolve_thinking_flag`), but sending the real value keeps the audit record
  // unambiguous about what the clinician chose. The server's safety floor can
  // still override it — see server/services/thinking_mode.py.
  const body: Record<string, unknown> = {
    query,
    patient_id: patientId,
    thinking: thinking ?? true,
  };
  if (viewContext) body.view_context = viewContext;
  if (conversationId) body.conversation_id = conversationId;
  const response = await fetch(`${API_BASE}/sage/query`, {
    method: 'POST',
    credentials: 'include',
    headers: buildHeaders('POST'),
    body: JSON.stringify(body),
    signal,
  });

  if (!response.ok) await handleResponseError(response);

  let fullResponse = '';

  for await (const event of readSSEEvents(response)) {
    try {
      const data = JSON.parse(parseSSEData(event));
      // Progress stages: the backend brackets its two long phases (guideline
      // retrieval, then local-model generation) so the UI can show honest
      // status instead of minutes of anonymous typing dots.
      if (data.stage !== undefined) {
        onStage?.(data.stage, data.retrieval_ms);
      }
      if (data.content !== undefined) {
        fullResponse += data.content;
        onChunk?.(fullResponse);
      }
      if (data.metadata !== undefined) {
        onMetadata?.(data.metadata);
      }
      if (data.structured_response !== undefined) {
        onMetadata?.({ _structured_response: data.structured_response });
      }
    } catch {
      // Not JSON (an SSE framing artifact): skip the event. A throw from one
      // of the callbacks above is swallowed here too.
    }
  }

  return fullResponse;
}

// ─── Sage AI governance / ambient intelligence ───

export interface SageAiPosture {
  provider_mode: string;
  hosted_enabled: boolean;
  hosted_configured: boolean;
  hosted_host_allowlisted: boolean;
  phi_egress_approved: boolean;
  local_model: string;
  /** Whether ON-APPLIANCE transcription can run. A capability fact, and every
   *  dictation control needs it so it can say what it can do BEFORE the
   *  clinician speaks, rather than failing after thirty seconds of dictation.
   *  It is the only speech recognition there is: Orb is strictly offline. */
  local_asr_available?: boolean;
}

export async function getSageAiPosture(): Promise<SageAiPosture> {
  return apiFetch('/sage/ai-posture');
}

/** Speculative context prefetch: call when a clinician OPENS a patient so
 *  retrieval leaves Sage's time-to-first-token critical path. */
export async function prefetchSageContext(
  patientId: string,
  hint?: string,
): Promise<{ status?: 'warmed' | 'skipped'; patient_id?: string; error?: string }> {
  return apiFetch('/sage/prefetch', {
    method: 'POST',
    body: JSON.stringify({ patient_id: patientId, hint }),
  });
}

export interface WardBrief {
  snapshot: {
    /** Null for a department-scoped caller: a hospital-wide count describes
     *  wards they cannot see. */
    census: number | null;
    risk_distribution: Record<string, number>;
    watchlist: Array<{ patient_id: string; name: string; bed: string; ward: string; level: string; score: number }>;
    recent_events: Array<{ topic: string; event_type: string; patient_id: string | null; timestamp: string }>;
    /** Null for a scoped caller, for the same reason as census. */
    active_alerts: number | null;
    as_of: string;
    /** The caller's department when the brief is scoped to it. */
    scope?: string;
  } | null;
  narrative: string | null;
  narrative_engine: string | null;
  generated_at: string | null;
}

export async function getWardBrief(): Promise<WardBrief> {
  return apiFetch('/sage/ward-brief');
}

/** One stored Sage turn. `metadata` carries only what a reloaded thread
 *  needs (the speed footer and the patient label), and is absent on a turn
 *  stored before timing was recorded or whose timing was never measured. */
export interface SageHistoryMessage {
  role: string;
  content: string;
  conversation_id: string | null;
  timestamp: string;
  metadata?: {
    timing?: SageVoiceTiming;
    provider?: { thinking_tier: string };
    patient_name?: string;
  };
}

/** This user's recent turns across threads, oldest first. */
export async function getSageHistory(): Promise<{ messages: SageHistoryMessage[] }> {
  return apiFetch('/sage/history');
}

export async function clearSageHistory(): Promise<void> {
  await apiFetch('/sage/history', { method: 'DELETE' });
}

// ─── Sage Voice Agent ───
/** Timing on a voice answer (sage_routes._answer_timing). Null means not
 *  measured: the refusal paths never ran the model. */
export interface SageVoiceTiming {
  ttft_ms: number | null;
  total_ms: number | null;
  tokens: number | null;
}

/** One event on the voice stream. */
interface SageVoiceEvent {
  token?: string;
  done?: boolean;
  timing?: SageVoiceTiming;
  error?: string;
}

export async function querySageVoice(
  query: string,
  patientId?: string,
  onToken?: (token: string) => void,
  signal?: AbortSignal,
  conversationHistory?: Array<{ query: string; response: string }>,
  viewContext?: Record<string, unknown>,
): Promise<{ fullResponse: string; timing?: SageVoiceTiming }> {
  const body: Record<string, unknown> = { query, patient_id: patientId };
  if (conversationHistory && conversationHistory.length > 0) {
    body.conversation_history = conversationHistory;
  }
  if (viewContext) body.view_context = viewContext;
  const response = await fetch(`${API_BASE}/sage/voice`, {
    method: 'POST',
    credentials: 'include',
    headers: buildHeaders('POST'),
    body: JSON.stringify(body),
    signal,
  });

  if (!response.ok) await handleResponseError(response);

  let fullResponse = '';
  let timing: SageVoiceTiming | undefined = undefined;

  for await (const event of readSSEEvents(response)) {
    // The error check must sit OUTSIDE the parse try/catch — a catch that also
    // covers the throw swallows it, and the call then resolves as a successful
    // but empty answer.
    let data: SageVoiceEvent | null;
    try {
      data = JSON.parse(parseSSEData(event));
    } catch {
      continue; // SSE framing artifact / non-JSON chunk — skip it.
    }
    if (!data || typeof data !== 'object') continue;
    // The server emits `{"error": ...}` and closes the stream when the voice
    // agent is disabled or the model fails. Propagate it so the caller's error
    // path runs instead of Sage "answering" with silence.
    if (data.error) throw new Error(data.error);
    if (data.token !== undefined) {
      fullResponse += data.token;
      onToken?.(data.token);
    }
    if (data.done && data.timing) {
      timing = data.timing;
    }
  }

  return { fullResponse, timing };
}

// ─── Scribe ───
export async function transcribeAudio(audioBlob: Blob, hints?: string, skipCorrection?: boolean, language?: string): Promise<{ transcript: string; audio_path: string }> {
  const formData = new FormData();
  formData.append('audio', audioBlob, 'recording.webm');
  if (hints) {
    formData.append('hints', hints);
  }
  if (skipCorrection) {
    formData.append('skip_correction', 'true');
  }
  if (language) {
    formData.append('language', language);
  }

  const response = await fetch(`${API_BASE}/scribe/transcribe`, {
    method: 'POST',
    credentials: 'include',
    headers: buildUploadHeaders(),
    body: formData,
  });

  if (!response.ok) await handleResponseError(response);
  return response.json();
}

export async function transcribeChunk(audioBlob: Blob, chunkIndex: number, hints?: string): Promise<{ text: string; chunk_index: number }> {
  const formData = new FormData();
  formData.append('audio', audioBlob, `chunk_${chunkIndex}.webm`);
  formData.append('chunk_index', String(chunkIndex));
  if (hints) {
    formData.append('hints', hints);
  }

  const response = await fetch(`${API_BASE}/scribe/transcribe-chunk`, {
    method: 'POST',
    credentials: 'include',
    headers: buildUploadHeaders(),
    body: formData,
  });

  if (!response.ok) {
    if (response.status === 401) {
      notifySessionExpired();
    }
    return { text: '', chunk_index: chunkIndex };
  }
  return response.json();
}

/** One issue the verifier found: a claim the transcript does not support. */
export interface ScribeVerificationWarning {
  type?: string;
  section?: string;
  text: string;
  severity?: 'low' | 'medium' | 'high' | string;
}

/** The note verifier's report (scribe_routes._run_verification_async).
 *  `status` is 'not_requested', 'pending', 'complete' or 'failed'; the
 *  findings are present once it is complete, `error` once it failed. */
export interface ScribeVerification {
  status?: string;
  verified?: boolean;
  confidence?: number;
  /** 'unverifiable' when the verifier ran but could not reach a verdict. */
  verification_status?: string;
  warnings?: ScribeVerificationWarning[];
  omissions?: { text: string; severity?: string }[];
  sections_verified?: string[];
  error?: string;
  started_at?: string;
  completed_at?: string;
  failed_at?: string;
  elapsed_s?: number;
}

/** Stage timings and models behind a note (scribe_service.build_pipeline_meta). */
export interface ScribePipelineMeta {
  /** Seconds per stage, keyed by stage name. */
  timings?: Record<string, number>;
  models?: { asr?: string; correction?: string | null; draft?: string; verify?: string | null };
  stages_enabled?: { correction?: boolean; verify?: boolean };
}

/** One event on the /scribe/structure stream. */
interface ScribeStructureEvent {
  content?: string;
  done?: boolean;
  id?: number;
  format?: string;
  status?: string;
  pipeline?: ScribePipelineMeta;
  verification?: ScribeVerification;
  error?: string;
}

export async function structureNote(
  transcript: string,
  format: string,
  patientId?: string,
  audioPath?: string,
  verify?: boolean,
  onChunk?: (chunk: string) => void,
): Promise<{
  id: number;
  structured_note: string;
  format: string;
  verification?: ScribeVerification;
  pipeline?: ScribePipelineMeta;
  /** The saved note's status from the done event ('draft' today). */
  status?: string;
}> {
  const response = await fetch(`${API_BASE}/scribe/structure`, {
    method: 'POST',
    credentials: 'include',
    headers: buildHeaders('POST'),
    body: JSON.stringify({ transcript, format, patient_id: patientId, audio_path: audioPath, verify }),
  });

  if (!response.ok) await handleResponseError(response);

  let fullResponse = '';
  let noteId = 0;
  let verificationData = undefined;
  let pipelineData = undefined;
  let noteStatus: string | undefined = undefined;

  for await (const event of readSSEEvents(response)) {
    // Parse failures are skipped, but a real stream error must escape the loop —
    // it must not be thrown from inside a try whose catch swallows it, or the
    // caller gets { id: 0, structured_note: <truncated> } and reports it complete.
    let data: ScribeStructureEvent | null;
    try {
      data = JSON.parse(parseSSEData(event));
    } catch {
      continue; // JSON parse errors are ok, skip them
    }
    if (!data || typeof data !== 'object') continue;
    // Mid-stream `{"error": ...}`: the server aborted and saved no note, so the
    // caller must see a failure rather than a silently truncated note.
    if (data.error) throw new Error(data.error);
    if (data.content !== undefined) {
      fullResponse += data.content;
      onChunk?.(fullResponse);
    }
    if (data.done && data.id) {
      noteId = data.id;
      verificationData = data.verification;
      pipelineData = data.pipeline;
      noteStatus = data.status;
    }
  }

  return {
    id: noteId, structured_note: fullResponse, format,
    verification: verificationData, pipeline: pipelineData, status: noteStatus,
  };
}

/** The active Scribe pipeline (admin only): models and enabled stages. */
export interface ScribeConfig {
  asr: { model: string; provider: string; batch_size: number };
  correction: { enabled: boolean; model: string; provider: string };
  draft: { model: string; provider: string };
  verify: { enabled: boolean; model: string; provider: string };
}

export async function getScribeConfig(): Promise<ScribeConfig> {
  return apiFetch(`/scribe/config`);
}

/** Notes for a patient, or with no patient the caller's own notes. Newest
 *  first, at most 50. */
export async function getScribeNotes(patientId?: string): Promise<ScribeNoteRow[]> {
  const query = patientId ? `?patient_id=${patientId}` : '';
  return apiFetch(`/scribe/notes${query}`);
}

export async function getScribeNoteVerification(noteId: number): Promise<{
  note_id: number;
  verification: ScribeVerification | null;
  pipeline: ScribePipelineMeta | null;
}> {
  return apiFetch(`/scribe/notes/${noteId}/verification`);
}

export async function retryScribeNoteVerification(noteId: number): Promise<{ note_id: number; verification: ScribeVerification }> {
  return apiFetch(`/scribe/notes/${noteId}/verify`, { method: 'POST' });
}

export async function editScribeNoteDraft(noteId: number, structuredNote: string): Promise<{ ok: boolean; note_id: number }> {
  return apiFetch(`/scribe/notes/${noteId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ structured_note: structuredNote }),
  });
}

export async function signScribeNote(noteId: number, acknowledgeUnverified?: boolean): Promise<{ ok: boolean; note_id: number; status: string; superseded_note_id?: number }> {
  // acknowledge_unverified is only ever sent after the clinician completed the
  // explicit acknowledgment dialog — the first attempt must always omit it so
  // the server's fail-closed 428 gate stays the deciding authority.
  return apiFetch(`/scribe/notes/${noteId}/sign`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(acknowledgeUnverified ? { acknowledge_unverified: true } : {}),
  });
}

export interface ScribeAsrStatus {
  available: boolean;
  provider: string;
  model: string;
  reason: string | null;
  runbook: string;
}

export async function getScribeAsrStatus(): Promise<ScribeAsrStatus> {
  return apiFetch('/scribe/asr/status');
}

export async function rejectScribeNote(noteId: number, reason: string): Promise<{ ok: boolean; note_id: number; status: string }> {
  return apiFetch(`/scribe/notes/${noteId}/reject`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reason }),
  });
}

export async function reviseScribeNote(noteId: number): Promise<{ ok: boolean; new_note_id: number; version: number; status: string }> {
  return apiFetch(`/scribe/notes/${noteId}/revise`, { method: 'POST' });
}

// ─── Bridge ───
/** The patient's status in plain words (bridge_service.get_patient_status_simple).
 *  A login with no patient record gets only `status: 'unknown'` and a
 *  `message`. Vitals are charted text ("72 bpm"), never raw numbers. */
export interface BridgeStatus {
  status: string;
  message?: string;
  patient_name?: string;
  bed?: string | null;
  ward?: string | null;
  diagnosis_simple?: string;
  status_message?: string;
  vitals?: {
    heart_rate: string | null;
    blood_pressure: string | null;
    oxygen: string | null;
    temperature: string | null;
  } | null;
  attending_doctor?: string | null;
}

/** A medication as the patient portal lists it. */
export interface BridgeMedication {
  drug_name: string;
  dosage: string;
  route: string;
  frequency: string;
  status: string;
  started_at: string;
}

/** A clinical document as the portal shows it. `type_label` is the type in
 *  the patient's words; `source` is 'simulated' for a simulator's result. */
export interface BridgeDocument {
  doc_type: string;
  title: string;
  content: string;
  created_at: string;
  source?: string | null;
  type_label: string;
}

/** A discharge summary as the patient may see it: `summary` stays null until
 *  a clinician has reviewed the draft, and `message` says why. */
export interface BridgeDischargeView {
  summary: string | null;
  status: string;
  message: string | null;
  discharged_at: string;
}

/** GET /bridge/reports: everything on record for the signed-in patient. */
export interface BridgeReports {
  patient_id: string;
  medications: BridgeMedication[];
  clinical_documents: BridgeDocument[];
  labs: BridgeDocument[];
  discharge_summary: BridgeDischargeView | null;
  /** Clinician-reviewed reads only; `analysis` is the reviewed text. */
  imaging_reports: {
    image_type: string | null;
    clinical_context: string | null;
    analysis: string | null;
    reviewed_at: string | null;
    created_at: string;
  }[];
}

/** One entry on a discharge timeline (discharge_service.build_patient_timeline). */
export interface DischargeTimelineEntry {
  timestamp: string | null;
  type: string;
  title: string;
  detail: string;
}

export async function getBridgeStatus(): Promise<BridgeStatus> {
  return apiFetch('/bridge/status');
}

export async function getBridgeReports(): Promise<BridgeReports> {
  return apiFetch('/bridge/reports');
}

export async function getBridgeMedications(): Promise<{ medications: BridgeMedication[] }> {
  return apiFetch('/bridge/medications');
}

/** The patient's discharge summary. `discharge_summary` is the text, null
 *  until reviewed (or when there is none; `message` says which). */
export async function getBridgeDischarge(): Promise<{
  discharge_summary: string | null;
  status?: string;
  message: string | null;
  timeline?: DischargeTimelineEntry[] | null;
  discharged_at?: string;
}> {
  return apiFetch('/bridge/discharge');
}

export async function getBridgeDocuments(): Promise<{
  documents: { doc_type: string; title: string; content: string; created_at: string }[];
}> {
  return apiFetch('/bridge/documents');
}

export async function askBridge(
  question: string,
  onChunk?: (chunk: string) => void,
): Promise<string> {
  const response = await fetch(`${API_BASE}/bridge/ask`, {
    method: 'POST',
    credentials: 'include',
    headers: buildHeaders('POST'),
    body: JSON.stringify({ question }),
  });

  if (!response.ok) await handleResponseError(response);

  let fullResponse = '';

  for await (const event of readSSEEvents(response)) {
    try {
      const data = JSON.parse(parseSSEData(event));
      if (data.content !== undefined) {
        fullResponse += data.content;
        onChunk?.(fullResponse);
      }
    } catch {
      // Not JSON (an SSE framing artifact): skip the event. A throw from
      // onChunk is swallowed here too.
    }
  }

  return fullResponse;
}

// ─── Drug-Safety Gate (safety-ack protocol) ───
// Every medication-writing endpoint (Vigil med-add, CPOE med orders) runs the
// order-time allergy + interaction screen. Sub-severe results come back as
// `safety` on the success response; severe results 428 until re-submitted with
// an acknowledged SafetyAck (the override is audited server-side).

export interface SafetyAlert {
  severity: string;
  message: string;
  type?: string;
}

export interface MedicationSafety {
  drug?: string;
  allergy_alerts: SafetyAlert[];
  interaction_alerts: SafetyAlert[];
  max_severity: string;
  blocking: boolean;
  /** False when the drug maps to no known class — interactions NOT screened. */
  interaction_screened: boolean;
  coverage_note: string | null;
  /** The dose guard's verdict on the ordered dose, when the lane knew the dose
   *  (Vigil add-medication, Helix approve, Relay). A 'block' makes `blocking`
   *  true and clears only with a prescriber's written reason. */
  dose_check?: (DoseCheckSummary & { override_requires_permission?: string | null }) | null;
  /** Why the server refused the written reason on the last attempt, in its
   *  own words (for example a reason too short to be one). */
  ack_refused?: string | null;
}

/** Clinician acknowledgement re-submitted after reviewing severe alerts. */
export interface SafetyAck {
  acknowledged: true;
  reason: string;
}

/** Thrown by apiFetch on a 428 drug_safety_acknowledgement_required response. */
export interface SafetyAckRequiredError extends Error {
  code: 'safety_ack_required';
  status: 428;
  safety: MedicationSafety;
}

export function isSafetyAckRequired(err: unknown): err is SafetyAckRequiredError {
  return err instanceof Error && (err as SafetyAckRequiredError).code === 'safety_ack_required';
}

// ─── Enhanced Vigil ───
export async function getPatientNotes(patientId: string): Promise<ClinicalDocumentRow[]> {
  return apiFetch(`/vigil/patients/${patientId}/notes`);
}

/** What addClinicalNote resolves to: the server's `{status, id}` when online,
 *  or, when the note was queued offline, the note itself under an `offline-`
 *  id. */
export type AddClinicalNoteResult =
  | { status: string; id: number }
  | { id: string; patient_id: string; content: string; doc_type: string; title: string; created_at: string };

export async function addClinicalNote(patientId: string, content: string, docType: string = 'Progress Note'): Promise<AddClinicalNoteResult> {
  // Free-text notes carry no safety gate, so they are safe to queue offline and
  // sync on reconnect; the server dedupes the replay by Idempotency-Key.
  return offlineMutate<AddClinicalNoteResult>(
    `/vigil/patients/${patientId}/notes`,
    { content, doc_type: docType, title: 'Manual Entry' },
    () => ({
      id: 'offline-' + Date.now(), patient_id: patientId, content,
      doc_type: docType, title: 'Manual Entry', created_at: new Date().toISOString(),
    }),
  );
}

/** A medication order from the chart (POST /vigil/patients/{id}/medications).
 *  The server defaults `route` to 'Oral' and `status` to 'active'; the table
 *  accepts 'active', 'discontinued' or 'prn'. */
export interface MedicationOrderInput {
  drug_name: string;
  dosage: string;
  route?: string;
  frequency: string;
  status?: string;
}

export async function addMedication(
  patientId: string,
  medData: MedicationOrderInput,
  safetyAck?: SafetyAck,
): Promise<{ status: string; id: number; safety: MedicationSafety }> {
  // Medication orders are ONLINE-REQUIRED, unlike notes: the order-time drug-safety
  // gate (allergy + interaction screen) runs server-side and a severe interaction
  // returns a 428 the clinician must acknowledge. That acknowledgement can't happen
  // offline, so queuing an order optimistically would report "active" for a med the
  // server may reject on replay — a real prescribing hazard. Fail loudly instead.
  if (!navigator.onLine) {
    throw new Error('Medication orders require a connection for drug-safety screening. Reconnect and try again.');
  }
  // Re-submission after a 428 carries the clinician's acknowledgement so the
  // server can record the audited override and proceed.
  const body = safetyAck ? { ...medData, safety_ack: safetyAck } : medData;
  // Idempotency-Key still guards against a lost-response double-submit online.
  return apiFetch(`/vigil/patients/${patientId}/medications`, {
    method: 'POST',
    headers: { 'Idempotency-Key': newId() },
    body: JSON.stringify(body),
  });
}

// ─── Orders / CPOE ───
export interface CreateOrderPayload {
  patient_id: string;
  order_type: string;
  description: string;
  code?: string | null;
  code_system?: string | null;
  priority?: string;
}

/** Create an order. Medication orders pass the drug-safety gate: severe alerts
 *  throw the typed 428 error (see isSafetyAckRequired); re-submit with the
 *  acknowledged SafetyAck. Sub-severe screens come back as `safety`. */
export async function createOrder(
  payload: CreateOrderPayload,
  safetyAck?: SafetyAck,
): Promise<{ status: string; order: Record<string, unknown>; safety?: MedicationSafety }> {
  const body = safetyAck ? { ...payload, safety_ack: safetyAck } : payload;
  return apiFetch('/orders', {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

// ─── eMAR (bedside medication administration) ───
/** One alert from the server's allergy interlock, as
 *  `allergy_interlock.summarize()` shapes it. */
export interface AllergyAlert {
  substance?: string;
  reaction?: string | null;
  match?: string;
  decision?: string;
  effective_severity?: string;
  severity_basis?: string;
  message?: string;
}

/** WHAT THE INTERLOCK SAW AND DECIDED FOR THIS DOSE, stored on the
 *  administration row itself and served back with the history.
 *
 *  `null` on a record means the dose was never screened (a 'not_given' row, or
 *  one charted before the interlock existed) — which is NOT the same statement
 *  as "screened and clean", and the UI must not render them alike.
 *
 *  `override` is present only when the dose was given ACROSS a block. That is
 *  the field the chart surfaces render a marker from: a drug given despite a
 *  charted allergy has to be visible to whoever reads the record next, without
 *  them going to the audit log to find out. */
export interface AllergyCheck {
  decision?: 'allow' | 'warn' | 'block' | string;
  screen_failed?: boolean;
  allergies_on_file?: number | null;
  alerts?: AllergyAlert[];
  override?: {
    reason?: string;
    /** True when this dose inherited a reason typed for an earlier dose of the
     *  same order against the same allergy picture, rather than a fresh
     *  decision made now. */
    carried?: boolean;
    by?: string | null;
    at?: string | null;
  } | null;
  /** The permission needed to clear this block — `medication.prescribe` for a
   *  charted allergy, null when nothing needs clearing or when the screen
   *  merely failed. Sent by the server so no client hard-codes "ask a doctor". */
  override_requires_permission?: string | null;
  message?: string | null;
}

/** One finding from the dose guard, as drug_safety_service.summarize_dose_check
 *  shapes it. `decision` is 'block' (the dose was refused) or 'warn'. */
export interface DoseCheckFinding {
  kind?: string;
  decision?: 'allow' | 'warn' | 'block' | string;
  rule?: string | null;
  message?: string | null;
  threshold?: number | null;
  unit?: string | null;
}

/** THE DOSE GUARD'S VERDICT. Rides on a charted dose as
 *  `administration.dose_check` (a WARN the server let through) and on a refused
 *  one as `detail.dose` of the 428 `dose_acknowledgement_required`. */
export interface DoseCheckSummary {
  decision?: 'allow' | 'warn' | 'block' | string;
  unavailable?: boolean;
  unavailable_reason?: string | null;
  findings?: DoseCheckFinding[];
  message?: string | null;
}

export interface AdministrationRecord {
  id: number;
  medication_id: number;
  patient_id: string;
  drug_name: string;
  status: 'given' | 'not_given';
  dose_given: string | null;
  route_given: string | null;
  not_given_reason: string | null;
  administered_by: string;
  administered_by_name: string | null;
  administered_at: string;
  confirmation_method?: string;
  notes?: string | null;
  allergy_check?: AllergyCheck | null;
  dose_check?: DoseCheckSummary | null;
}

/** True when this dose was given ACROSS a charted allergy.
 *
 *  ONE derivation, exported, because the marker has to appear on every surface
 *  that shows an administration and a per-screen re-derivation is how one of
 *  them silently stops showing it. Deliberately strict: a WARN-grade screen that
 *  never stopped anything is not an override, and a screen that failed is
 *  reported by its own field rather than folded in here. */
export function givenAcrossAllergy(record: AdministrationRecord | null | undefined): boolean {
  return Boolean(record?.status === 'given' && record?.allergy_check?.override?.reason);
}

export interface AdministerPayload {
  status: 'given' | 'not_given';
  dose_given?: string;
  route_given?: string;
  not_given_reason?: string;
  confirmation_method: 'typed';
  notes?: string;
  /** PRESCRIBER-ONLY, and only after the server has already refused this dose
   *  with a 428. The written clinical justification for giving a drug the
   *  patient has a documented allergy to — a de-labelled allergy, a
   *  desensitisation step, a risk/benefit call under supervision.
   *
   *  The server is the authority on all of it: it enforces the minimum length
   *  and the junk-reason filter (`allergy_interlock.validate_override_reason`),
   *  and it refuses the field outright with a 403 unless the caller holds
   *  `medication.prescribe`. Anything this client checks before sending is a
   *  courtesy to the typist, never a control. */
  allergy_override_reason?: string;
}

/** Record a bedside administration (given / not given) of a charted med.
 *  Guard failures (inactive order, unverified AI med, missing not-given reason)
 *  surface as a 422 with a detail message.
 *
 *  428 → a SAFETY GATE stopped this dose and it can be cleared with a typed
 *  reason: `detail.allergy` (the allergy interlock) or `detail.dose` (the dose
 *  guard). 403 with `detail.error === 'allergy_override_not_permitted'` → the
 *  caller may record doses but may not clear a charted allergy; only a
 *  prescriber can, and no amount of retrying changes that.
 *
 *  `idempotencyKey` MUST be supplied by the caller and MUST be stable across
 *  retries of the same dose. Without it the key was minted per call, so the
 *  "retry after a failure" path the modal offers minted a fresh key and the
 *  server charted a second dose whenever the first request had actually
 *  succeeded and only its response was lost. */
export async function administerMedication(
  medId: number,
  payload: AdministerPayload,
  idempotencyKey?: string,
): Promise<{ status: string; administration: AdministrationRecord }> {
  // THE ONLINE GUARD ON THIS CALL IS LOAD-BEARING. DO NOT REMOVE IT.
  //
  // This function still routes through offlineMutate, but every caller must
  // first pass `assertOnlineForWrite` (components/vigil/AdministerModal.tsx) —
  // and the reason is no longer only about a nurse trusting an optimistic
  // record. TWO SERVER-SIDE DRUG-SAFETY GATES RUN ON THIS ENDPOINT, inside the
  // same transaction as the write:
  //
  //   * the ALLERGY INTERLOCK (server/services/allergy_interlock.py) screens the
  //     dose against the patient's LIVE allergy list at the moment it is given
  //     and refuses a severe match with a 428 — clearable only by a prescriber,
  //     with a written reason, which is recorded;
  //   * the DOSE GUARD (server/services/drug_safety_service.py) refuses an
  //     order-of-magnitude dose or a never-correct route on the same contract.
  //
  // Queueing an administration would send it to a server that is not there, and
  // the queue has no way to surface a 428 or a 403 to a nurse who has walked
  // away: the interlock's verdict would arrive, unread, minutes or hours after
  // the drug was already in the patient. An earlier version of this comment said
  // "no drug-safety gate runs here". That was true once, is false now, and was a
  // standing invitation to delete the guard and route doses through the queue —
  // which would turn the last line of defence into a message nobody reads.
  //
  // The Idempotency-Key remains what makes a RETRY safe once the connection is
  // back: a lost response cannot become a second charted dose.
  const user = getUser();
  return offlineMutate<{ status: string; administration: AdministrationRecord }>(
    `/emar/medications/${medId}/administer`,
    payload,
    () => ({
      status: 'queued_offline',
      administration: {
        id: -Date.now(),
        medication_id: medId,
        patient_id: '',
        drug_name: '',
        status: payload.status,
        dose_given: payload.dose_given ?? null,
        route_given: payload.route_given ?? null,
        not_given_reason: payload.not_given_reason ?? null,
        administered_by: user?.id ?? 'me',
        administered_by_name: user?.name ?? null,
        administered_at: new Date().toISOString(),
        confirmation_method: payload.confirmation_method,
        notes: payload.notes ?? null,
      },
    }),
    'POST',
    idempotencyKey,
  );
}

/** Administration history for a patient, newest first. */
export async function getAdministrations(patientId: string): Promise<AdministrationRecord[]> {
  const res = await apiFetch<{ administrations: AdministrationRecord[] } | AdministrationRecord[]>(
    `/emar/patients/${patientId}/administrations`,
  );
  return Array.isArray(res) ? res : (res.administrations ?? []);
}

// ─── Unified patient story (timeline) ───
/* Five types were added on 2026-08-08 when the story engine grew the streams it
   had been missing: lab/vitals OBSERVATIONS from the HL7 ingestion path, care
   BUNDLES, the PROBLEM list, ALLERGIES, and ORDER STATUS TRANSITIONS. Anything
   rendering a per-type label or colour must cover the whole union — an unknown
   type used to be a crash, not a fallback (PatientStory indexed TYPE_META
   directly). */
export type StoryType =
  | 'admission' | 'alert' | 'vitals' | 'note' | 'scribe' | 'imaging'
  | 'medication' | 'order' | 'administration' | 'discharge'
  | 'observation' | 'bundle' | 'problem' | 'allergy' | 'order_event';

export interface StoryEvent {
  timestamp: string | null;
  type: StoryType;
  title: string;
  detail: string;
  actor: string | null;
}

/** The story plus THIS clinician's "I have read up to here" watermark. */
export interface StoryFeed {
  events: StoryEvent[];
  /** ISO-8601 UTC, or null when this clinician has never reviewed this chart.
   *  null means "no marker at all" — deliberately NOT "everything is new". */
  last_reviewed_at: string | null;
  /** Events after the watermark, counted server-side over every row. */
  new_count: number;
}

/** The merged clinical narrative for a patient, newest first. `types` filters. */
export async function getPatientStory(patientId: string, types?: StoryType[]): Promise<StoryEvent[]> {
  const res = await getPatientStoryFeed(patientId, types);
  return res.events;
}

/** As getPatientStory, but keeps the review watermark that rides alongside it.
 *  Split so the ward round — which has its own overnight window and must not
 *  change with whose session it is — can keep the plain array. */
export async function getPatientStoryFeed(
  patientId: string,
  types?: StoryType[],
): Promise<StoryFeed> {
  const qs = types && types.length ? `?types=${types.join(',')}` : '';
  const res = await apiFetch<Partial<StoryFeed>>(`/vigil/patients/${patientId}/story${qs}`);
  return {
    events: res.events ?? [],
    last_reviewed_at: res.last_reviewed_at ?? null,
    new_count: res.new_count ?? 0,
  };
}

// ─── "What changed since I last looked" — per-clinician review watermarks ───

/** Move MY watermark on this chart forward. Never moves it backwards, so a
 *  replayed offline request cannot resurrect a badge over events already dealt
 *  with. `asOf` pins the mark to the newest event actually RENDERED, so an event
 *  that lands between the render and the click is not silently marked read. */
export async function markChartReviewed(
  patientId: string,
  asOf?: string | null,
): Promise<{ last_reviewed_at: string | null; previous: string | null; new_count: number }> {
  return apiFetch(`/chart/patients/${patientId}/reviewed`, {
    method: 'POST',
    body: JSON.stringify({ as_of: asOf ?? null }),
  });
}

/** {patientId: N} for admitted patients in my ward that I have reviewed before.
 *  Patients I have never opened are ABSENT rather than zero — a badge on every
 *  bed on the first login teaches clinicians to ignore the badge. */
export async function getUnseenCounts(): Promise<Record<string, number>> {
  const res = await apiFetch<{ counts?: Record<string, number> }>('/chart/unseen');
  return res.counts ?? {};
}

// ─── Discharge ───
/** A completed discharge. The drafted summary comes back for the discharging
 *  doctor to read; the patient sees it only once it is marked reviewed. */
export interface DischargeResult {
  status: 'success';
  summary_id: number;
  discharged_at: string;
  review_status: string;
  /** The secure patient link; null when no link was issued. */
  summary_url: string | null;
  /** The authenticated API path of the QR image, or null when none was made. */
  qr_code_url: string | null;
  secure_link_issued: boolean;
  /** Why no link was issued, when one was not. */
  secure_link_reason: string | null;
  summary: string;
  /** 'unavailable' when the drafting assistant was down and only the recorded
   *  facts were stored. */
  drafted_by: 'assistant' | 'unavailable';
  timeline: DischargeTimelineEntry[];
}

export async function dischargePatient(
  patientId: string,
): Promise<DischargeResult | { status: 'already_discharged'; patient_id: string }> {
  return apiFetch(`/vigil/patients/${patientId}/discharge`, { method: 'POST' });
}

/** The latest discharge summary, clinician-facing, so a draft is returned
 *  too (discharge_service.load_discharge_summary_view). */
export interface DischargeSummaryView {
  summary_id: number;
  summary: string;
  review_status: string;
  reviewed_by_name: string | null;
  reviewed_at: string | null;
  timeline: DischargeTimelineEntry[];
  qr_code_url: string | null;
  discharged_at: string;
}

export async function getDischargeSummary(patientId: string): Promise<DischargeSummaryView> {
  return apiFetch(`/vigil/patients/${patientId}/discharge-summary`);
}

/** One deterioration escalation chain, as the discharge screen lists it. */
export interface EscalationChain {
  id: string;
  patient_id: string;
  status: 'open' | 'acknowledged' | 'resolved' | string;
  raised_at: string;
  peak_news2?: number | null;
  current_tier?: number | null;
  tier_label?: string | null;
}

/** The patient's escalation chains that are still open or acknowledged. */
export async function getOpenEscalations(patientId: string): Promise<EscalationChain[]> {
  const res = await apiFetch<{ escalations?: EscalationChain[] }>(
    `/vigil/patients/${encodeURIComponent(patientId)}/escalations?include_resolved=false`,
  );
  return (res.escalations ?? []).filter(e => e.status !== 'resolved');
}

/** Close a chain with a reason (the server refuses a blank one). */
export async function resolveEscalation(
  escalationId: string, reason: string, category: string = 'patient_discharged',
): Promise<EscalationChain> {
  return apiFetch(`/vigil/escalations/${encodeURIComponent(escalationId)}/resolve`, {
    method: 'POST',
    body: JSON.stringify({ reason, category }),
  });
}

/**
 * Fetch the discharge QR PNG as an authenticated blob and return an object URL.
 *
 * The QR endpoint requires a JWT (it embeds the discharge access token, which is
 * PHI), but a plain <img src> does not send the Authorization header → the image
 * load 401s. We fetch with the Bearer header and hand back an object URL the
 * caller can drop into <img src>. The caller is responsible for revoking it.
 */
export async function getDischargeQrObjectUrl(qrUrl: string): Promise<string> {
  // qrUrl from the server is API-rooted (e.g. "/api/vigil/patients/X/qr").
  const path = qrUrl.replace(/^\/api/, '');
  const response = await fetch(`${API_BASE}${path}`, {
    credentials: 'include',
    headers: buildHeaders('GET'),
  });
  if (!response.ok) {
    if (response.status === 401) notifySessionExpired();
    throw new Error(`Failed to load discharge QR: ${response.status}`);
  }
  const blob = await response.blob();
  return URL.createObjectURL(blob);
}

/** One row of the discharged-patient list (ward-scoped, paginated). */
export interface DischargedPatient {
  id: string;
  name: string;
  mrn: string;
  bed: string | null;
  ward: string | null;
  diagnosis: string | null;
  age: number | null;
  sex: string | null;
  admission_date: string | null;
  discharged_at: string | null;
  summary: string | null;
  /** The latest stay, which a final bill is raised against. */
  admission_id: string | null;
  final_bill_raised: boolean;
  has_pending_charges: boolean;
}

export async function getDischargedPatients(): Promise<DischargedPatient[]> {
  return apiFetch('/vigil/discharged');
}

// ─── Lens ───
export async function analyzeLensImage(
  imageFile: File,
  options: { clinical_context?: string; patient_id?: string; image_type?: string },
  onChunk?: (chunk: string) => void,
): Promise<{ text: string; analysisId: number | null }> {
  const formData = new FormData();
  formData.append('image', imageFile);
  if (options.clinical_context) formData.append('clinical_context', options.clinical_context);
  if (options.patient_id) formData.append('patient_id', options.patient_id);
  if (options.image_type) formData.append('image_type', options.image_type);

  const response = await fetch(`${API_BASE}/lens/analyze`, {
    method: 'POST',
    credentials: 'include',
    headers: buildUploadHeaders(),
    body: formData,
  });

  if (!response.ok) await handleResponseError(response);

  let fullResponse = '';
  // Concrete id of the row the server just created, emitted as a final JSON SSE
  // event before [DONE]. Threading this through lets the caller file the reviewed
  // findings to THIS analysis rather than guessing the global newest one.
  let analysisId: number | null = null;

  for await (const event of readSSEEvents(response)) {
    const eventData = parseSSEData(event);
    if (eventData === '[DONE]' || eventData.includes('[DONE]')) continue;

    // The final metadata event is a JSON object carrying the analysis id.
    // Capture it and do NOT append it to the visible analysis text.
    if (eventData.startsWith('{')) {
      try {
        const meta = JSON.parse(eventData);
        if (meta && typeof meta.analysis_id === 'number') {
          analysisId = meta.analysis_id;
          continue;
        }
      } catch {
        // Not the metadata event — fall through and treat as analysis text.
      }
    }

    fullResponse += eventData;
    onChunk?.(fullResponse);
  }

  return { text: fullResponse, analysisId };
}

export async function getLensAnalyses(patientId?: string): Promise<LensAnalysisRow[]> {
  const query = patientId ? `?patient_id=${patientId}` : '';
  return apiFetch(`/lens/analyses${query}`);
}

export async function assignLensToPatient(analysisId: number, patientId: string, reviewedText?: string): Promise<{ status: string; patient_name: string }> {
  const formData = new FormData();
  formData.append('patient_id', patientId);
  if (reviewedText != null) formData.append('reviewed_text', reviewedText);

  const response = await fetch(`${API_BASE}/lens/analyses/${analysisId}/assign`, {
    method: 'PATCH',
    credentials: 'include',
    headers: buildUploadHeaders(),
    body: formData,
  });

  if (!response.ok) await handleResponseError(response);
  return response.json();
}

export async function getPatientsList(): Promise<{ id: string; name: string; bed: string }[]> {
  const patients = await getPatients();
  return patients.map(p => ({ id: p.id, name: p.name, bed: p.bed }));
}

// ─── Surgery ───
/** The `surgeries` columns every surgical list sends (`s.*`), plus the
 *  patient fields the lists join on. JSON columns arrive as stored TEXT here;
 *  only the detail route parses them. */
export interface SurgeryRow {
  id: string;
  patient_id: string;
  surgeon_id: string;
  procedure_name: string;
  diagnosis: string;
  /** YYYY-MM-DD, the hospital's local calendar day. */
  scheduled_date: string;
  scheduled_time: string;
  estimated_duration_min: number;
  operating_room: string;
  status: string;
  co_morbidities: string | null;
  surgical_checklist: string | null;
  instruments_required: string | null;
  staff_required: string | null;
  risk_assessment: string | null;
  possible_complications: string | null;
  /** A FRACTION (0-1), not a percentage. */
  success_probability: number | null;
  notes: string | null;
  created_at: string;
  started_at?: string | null;
  completed_at?: string | null;
  patient_name: string;
  mrn: string;
  age: number;
  sex: string;
}

/** A case on the week view, with the patient's latest risk band. */
export interface SurgeryScheduleCase extends SurgeryRow {
  surgeon_name: string | null;
  /** 'stable' when the patient has no risk row. */
  risk_level: string;
  risk_score: number | null;
}

/** One day of the week view. `is_today` is decided in the hospital's
 *  timezone. */
export interface SurgeryScheduleDay {
  date: string;
  /** The weekday name. */
  day_label: string;
  is_today: boolean;
  surgeries: SurgeryScheduleCase[];
}

export async function getSurgerySchedule(days: number = 7): Promise<SurgeryScheduleDay[]> {
  return apiFetch(`/surgery/schedule?days=${days}`);
}

/** Context imported with an emergency case (`notes.imported_context`). */
export interface SurgeryImportedContext {
  medications?: string[];
  vitals?: string[];
  recent_labs?: string[];
  scribe_notes?: string;
  [key: string]: unknown;
}

/** GET /surgery/{id}: one case in full, with its JSON columns parsed.
 *
 *  `bed`, `ward` and `admission_date` are the patient's, null when not
 *  recorded (the route did not select them at all until 4 October 2026, so the
 *  case header read "Bed undefined"); `risk_drivers` is the latest risk row's
 *  drivers, parsed. `surgical_checklist` is always a list of prep steps: the WHO
 *  checklist has its own column since migration 76 (it used to be stored here
 *  as an object, which crashed the panel), and the route sends [] for anything
 *  that is not a list. */
export interface SurgeryDetail {
  id: string;
  patient_id: string;
  surgeon_id: string;
  surgeon_name: string | null;
  patient_name: string;
  age: number;
  sex: string;
  mrn: string;
  bed: string | null;
  ward: string | null;
  patient_diagnosis: string;
  admission_date: string | null;
  procedure_name: string;
  diagnosis: string;
  scheduled_date: string;
  scheduled_time: string;
  estimated_duration_min: number;
  operating_room: string;
  status: string;
  co_morbidities: string[];
  /** The raw patient_profiles column the route falls back to. */
  comorbidities: string | null;
  allergies: string[];
  surgical_checklist: string[];
  instruments_required: string[];
  staff_required: { role: string; name: string }[];
  risk_assessment: string | null;
  possible_complications: string[];
  success_probability: number | null;
  /** 'stable' when the patient has no risk row. */
  risk_level: string;
  risk_score: number | null;
  risk_drivers: string[];
  notes: string | null;
  created_at: string;
  started_at?: string | null;
  completed_at?: string | null;
  /** Minutes from start to completion; null when not measured. */
  turnaround_min: number | null;
  imported_context?: SurgeryImportedContext;
}

export async function getSurgeryDetail(surgeryId: string): Promise<SurgeryDetail> {
  return apiFetch(`/surgery/${surgeryId}`);
}

// ─── WHO Surgical Safety Checklist ───
/** The static WHO 2009 checklist: three phases of items. */
export interface WhoChecklistTemplate {
  version: string;
  phases: {
    id: string;
    label: string;
    when: string;
    items: { id: string; text: string }[];
  }[];
}

/** A case's checklist: who attested each ticked item, and when. Unticking
 *  deletes the item's key. */
export interface WhoChecklistState {
  version: string;
  state: Record<string, { checked: boolean; by: string; at: string }>;
  progress: { checked: number; total: number };
  completed: boolean;
}

export async function getWhoChecklistTemplate(): Promise<WhoChecklistTemplate> {
  return apiFetch('/surgery/checklist/template');
}

export async function getWhoChecklist(surgeryId: string): Promise<WhoChecklistState> {
  return apiFetch(`/surgery/${surgeryId}/checklist`);
}

export async function setWhoChecklistItem(
  surgeryId: string,
  itemId: string,
  checked: boolean,
): Promise<WhoChecklistState> {
  return apiFetch(`/surgery/${surgeryId}/checklist`, {
    method: 'POST',
    body: JSON.stringify({ item_id: itemId, checked }),
  });
}

export async function getSurgeryRiskAssessment(
  surgeryId: string,
  onChunk?: (chunk: string) => void,
  signal?: AbortSignal,
): Promise<string> {
  const response = await fetch(`${API_BASE}/surgery/${surgeryId}/risk`, {
    method: 'GET',
    credentials: 'include',
    headers: buildUploadHeaders(),
    signal,
  });

  if (!response.ok) await handleResponseError(response);

  let fullResponse = '';

  for await (const event of readSSEEvents(response)) {
    const eventData = parseSSEData(event);
    if (eventData === '[DONE]' || eventData.includes('[DONE]')) continue;

    try {
      const data = JSON.parse(eventData);
      if (data.content !== undefined) {
        fullResponse += data.content;
        onChunk?.(fullResponse);
      }
    } catch {
      // Not JSON (an SSE framing artifact): skip the event. A throw from
      // onChunk is swallowed here too.
    }
  }

  return fullResponse;
}

export async function prepForSurgery(surgeryId: string): Promise<void> {
  await apiFetch(`/surgery/${surgeryId}/prep`, { method: 'POST' })
}

/** An emergency case awaiting the theatre team. */
export interface EmergencySurgeryCase extends SurgeryRow {
  bed: string | null;
  surgeon_name: string | null;
}

export async function getEmergencyQueue(): Promise<EmergencySurgeryCase[]> {
  return apiFetch(`/surgery/emergency-queue`)
}

export async function acknowledgeEmergency(surgeryId: string): Promise<void> {
  await apiFetch(`/surgery/emergency/${surgeryId}/acknowledge`, { method: 'POST' })
}

// ─── Surgery Cases ───
/** A `surgery_team_assignments` row. */
export interface SurgeryTeamAssignment {
  id: number;
  surgery_id: string;
  user_id: string;
  role_name: string;
  status: 'pending' | 'alerted' | 'confirmed' | 'declined';
  updated_at: string;
}

/** An active (not completed, not cancelled) case with its team. */
export interface SurgeryCase extends SurgeryRow {
  team: SurgeryTeamAssignment[];
}

export async function getSurgeryCases(): Promise<SurgeryCase[]> {
  return apiFetch('/surgery/cases')
}

export async function alertSurgicalTeam(surgeryId: string, team: { user_id: string; role: string }[]): Promise<{ status: string }> {
  return apiFetch(`/surgery/${surgeryId}/alert-team`, {
    method: 'POST',
    body: JSON.stringify({ team }),
  })
}

export async function surgicalTeamResponse(surgeryId: string, action: 'confirm' | 'decline'): Promise<{ status: string }> {
  return apiFetch(`/surgery/${surgeryId}/team-response`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
}

/** 'already_in_progress' (with `surgery_id`) when the case had started. */
export async function startSurgery(
  surgeryId: string,
): Promise<{ status: 'success'; started_at: string } | { status: 'already_in_progress'; surgery_id: string }> {
  return apiFetch(`/surgery/${surgeryId}/start`, { method: 'POST' })
}

/** 'already_completed' (with `surgery_id`) when the case had been completed. */
export async function completeSurgery(surgeryId: string): Promise<
  | {
      status: 'success';
      /** The operation note filed to the chart. */
      note_id: number;
      started_at: string | null;
      completed_at: string;
      turnaround_min: number | null;
    }
  | { status: 'already_completed'; surgery_id: string }
> {
  return apiFetch(`/surgery/${surgeryId}/complete`, { method: 'POST' })
}

// ─── Patient Creation ───
/** RETIRED: POST /vigil/patients answers 410 Gone and writes nothing, so this
 *  always throws. Admit through admitPatient. No caller remains. */
export async function createPatient(data: Record<string, unknown>): Promise<unknown> {
  return apiFetch('/vigil/patients', {
    method: 'POST',
    body: JSON.stringify(data),
  })
}

// ─── Admin / Governance ───
export interface AuditFilters {
  correlation_id?: string;
  patient_id?: string;
  action_type?: string;
  event_category?: string;
  outcome?: string;
  start_date?: string;
  end_date?: string;
  limit?: number;
  offset?: number;
}

export interface AuditLog {
  id: number;
  timestamp: string;
  correlation_id: string;
  event_category: string;
  action_type: string;
  outcome: string;
  actor_user_id: string | null;
  actor_role: string | null;
  patient_id: string | null;
  source_type: string | null;
  summary_message: string | null;
  metadata_json: string | null;
  target_type: string | null;
  target_id: string | null;
  request_path: string | null;
  ip_address: string | null;
}

export interface AuditLogsResponse {
  logs: AuditLog[];
  total: number;
  limit: number;
  offset: number;
}

/** Alerts fired in the last 24 h, from the shadow event log. `total: null`
 *  means UNKNOWN (the instrument is off or unreadable) and must never be
 *  shown as 0; `unknown_reason` says why. */
export interface AdminAlertCounts {
  total: number | null;
  known: boolean;
  unknown_reason: string | null;
  by_lane: Record<string, number>;
  by_level: Record<string, number>;
  source: string;
  /** The ingest lane's own audit rows, which are not a hospital-wide count. */
  audit_trail: { vigil: number; relay: number; lane: string; note: string };
}

/** Clinical rows written in the last 24 h. `total: null` means the figure is
 *  not known; `counted` is then a floor, rendered as "at least N". */
export interface AdminRecordCounts {
  total: number | null;
  counted: number;
  vitals_counted: number;
  observations_counted: number;
  by_source: Record<string, number>;
  complete: boolean;
  incomplete_reason: string | null;
  source: string;
}

/** GET /admin/overview: the governance snapshot. Counts are for 24 hours. */
export interface AdminOverviewData {
  ingestion_mode: string;
  audit_simulation_enabled: boolean;
  audit_total_24h: number;
  audit_categories: Record<string, number>;
  audit_outcomes: Record<string, number>;
  ingestion: {
    total: number;
    parsed: number;
    failed: number;
    duplicates: number;
    total_vitals: number;
    total_observations: number;
  };
  source_mix: Record<string, number>;
  alerts: AdminAlertCounts;
  records_written: AdminRecordCounts;
  recent_failures: {
    id: number;
    received_at: string;
    source_type: string;
    parse_error: string | null;
    correlation_id: string | null;
  }[];
  auth_events: { action: string; outcome: string; count: number }[];
}

export async function getAdminOverview(): Promise<AdminOverviewData> {
  return apiFetch('/admin/overview');
}

/** One `model_registry` row with its description. Integer flags are 0/1.
 *  `task`, `training_data` and `notes` are nullable columns, typed as the
 *  Model Governance screen reads them. */
export interface ModelRegistryEntry {
  id: number;
  model_key: string;
  version: string;
  task: string;
  training_data: string;
  validated: number;
  validation_metrics: Record<string, unknown> | string | null;
  calibration_stats: Record<string, unknown> | string | null;
  is_active: number;
  checkpoint_present: number;
  notes: string;
  registered_at: string;
  display_name?: string;
  role?: string | null;
  impacts?: string[];
}

export async function getModelRegistry(): Promise<{ models: ModelRegistryEntry[] }> {
  return apiFetch('/admin/models');
}

export async function getAuditLogs(filters: AuditFilters = {}): Promise<AuditLogsResponse> {
  const params = new URLSearchParams();
  if (filters.correlation_id) params.set('correlation_id', filters.correlation_id);
  if (filters.patient_id) params.set('patient_id', filters.patient_id);
  if (filters.action_type) params.set('action_type', filters.action_type);
  if (filters.event_category) params.set('event_category', filters.event_category);
  if (filters.outcome) params.set('outcome', filters.outcome);
  if (filters.start_date) params.set('start_date', filters.start_date);
  if (filters.end_date) params.set('end_date', filters.end_date);
  if (filters.limit) params.set('limit', String(filters.limit));
  if (filters.offset) params.set('offset', String(filters.offset));
  const qs = params.toString();
  return apiFetch(`/audit/logs${qs ? '?' + qs : ''}`);
}

/** One ingested payload as the Ingestion Monitor lists it. */
export interface IngestionEvent {
  id: number;
  received_at: string;
  source_type: string;
  source_name: string | null;
  parse_status: 'received' | 'parsed' | 'partially_parsed' | 'failed' | 'duplicate';
  parse_error: string | null;
  correlation_id: string | null;
  vitals_inserted: number | null;
  observations_inserted: number | null;
  processed_at: string | null;
}

export async function getIngestionStatus(
  limit: number = 20,
  status?: string,
): Promise<{ events: IngestionEvent[]; count: number; ingestion_mode: string }> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (status) params.set('status', status);
  return apiFetch(`/ingest/status?${params.toString()}`);
}


// ─── Discharge Access (Patient) ───
// Both calls THROW the parsed error body on failure (`{detail: {status:
// 'error', reason, ...}}`), not an Error, so the page can read `reason`.

/** A short-lived grant to read one discharge summary. */
export interface DischargeAccessGrant {
  status: 'success';
  access_grant: string;
  patient_id: string;
}

/** The discharge summary as the patient reads it through the secure link.
 *  `summary` is null until a clinician has reviewed the draft. */
export interface VerifiedDischargeSummary {
  status: 'success';
  summary: string | null;
  summary_status: 'draft' | 'reviewed';
  summary_message: string | null;
  reviewed_by_name: string | null;
  reviewed_at: string | null;
  discharged_at: string;
  timeline: DischargeTimelineEntry[];
  /** Empty when the patient row is gone. */
  patient: {
    mrn?: string;
    name?: string;
    age?: number | null;
    sex?: string | null;
    ward?: string | null;
    bed?: string | null;
    diagnosis?: string | null;
    admission_date?: string | null;
    attending_doctor_name?: string | null;
    admission_id?: string | null;
  };
  /** Null when the allergy list could not be read, which is not "none". */
  allergies: { substance: string; reaction: string | null; severity: string | null }[] | null;
  comorbidities: string[];
  /** The reconciled continued list, excluding orders still needing a
   *  pharmacist's verification. */
  medications: {
    name: string;
    drug_name: string;
    dosage: string;
    route: string;
    frequency: string;
    status: 'active' | 'prn';
  }[];
  unverified_medication_count: number;
  site_name: string | null;
}

export async function verifyDischargeAccess(token: string, dob: string): Promise<DischargeAccessGrant> {
  const response = await fetch(`${API_BASE}/discharge-access/verify`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token, dob }),
  });
  const data = await response.json();
  if (!response.ok) {
    throw data;
  }
  return data;
}

export async function getVerifiedDischargeSummary(token: string, accessGrant: string): Promise<VerifiedDischargeSummary> {
  const response = await fetch(`${API_BASE}/discharge-access/${token}/summary`, {
    headers: { 'Authorization': `Bearer ${accessGrant}` },
  });
  const data = await response.json();
  if (!response.ok) {
    throw data;
  }
  return data;
}
// ─── Helix Pharmacy ───
/** A `helix_patient_med_orders` row as stored. */
export interface HelixOrderRow {
  id: string;
  patient_id: string | null;
  medication_id: string | null;
  status: 'pending' | 'reviewing' | 'verified' | 'rejected' | 'substitute_suggested' | 'scheduled' | 'dispensed' | 'delivered';
  prescribed_by: string | null;
  dosage: string | null;
  frequency: string | null;
  route: string | null;
  substitute_medication_id: string | null;
  pharmacist_notes: string | null;
  reviewed_by: string | null;
  reviewed_at: string | null;
  scheduled_at: string | null;
  last_transition_id: string | null;
  /** Set only by the ward, through receiveHelixOrder. */
  received_by: string | null;
  received_at: string | null;
  created_at: string;
}

/** A Helix order as the pharmacy lists it: the row plus the names it shows. */
export interface HelixOrderListRow extends HelixOrderRow {
  medication_name: string;
  patient_name: string;
  prescribed_by_name: string | null;
  ward: string | null;
  bed: string | null;
  substitute_name: string | null;
}

/** One step in an order's history (`helix_transitions`). `payload` is the
 *  stored JSON TEXT. */
export interface HelixTransition {
  id: string;
  order_id: string;
  from_status: string;
  to_status: string;
  idempotency_key: string | null;
  actor_id: string | null;
  payload: string | null;
  created_at: string;
}

/** A transition or receipt that was applied, or (`already_applied`) one whose
 *  idempotency key had been used, returned as first recorded. `order` is the
 *  stored row, empty if it has since gone. */
export interface HelixTransitionResult {
  ok: true;
  order: Partial<HelixOrderRow>;
  transition: HelixTransition;
  already_applied: boolean;
}

export async function approveHelixOrder(payload: { patient_name: string; medication: string; dose?: string; route?: string; frequency?: string }): Promise<{
  status: string;
  order: HelixOrderRow;
  /** The drug-safety screen that ran on the order. */
  safety: MedicationSafety;
}> {
  return apiFetch('/helix/orders/approve', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export async function getHelixOrders(): Promise<{ orders: HelixOrderListRow[] }> {
  return apiFetch('/helix/orders');
}

/** Substituting, verifying and dispensing each pass the drug-safety gate on the
 *  drug that would actually leave the shelf. A severe alert throws a
 *  SafetyAckRequiredError (428); re-send with `safety_ack` to override, which
 *  the server audits. */
export async function transitionHelixOrder(orderId: string, payload: {
  target_status: string;
  idempotency_key?: string;
  pharmacist_notes?: string;
  substitute_medication_id?: string;
  scheduled_at?: string;
  safety_ack?: SafetyAck;
}): Promise<HelixTransitionResult & { safety?: MedicationSafety }> {
  return apiFetch(`/helix/orders/${orderId}/transition`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export interface HelixOrderSafety {
  order_id: string;
  medication_id: string;
  drug_name: string;
  is_substitute: boolean;
  safety: MedicationSafety & { screen_failed?: boolean };
}

/** The allergy and interaction screen for the drug an order would dispense.
 *  Pass `substituteId` to preview a substitute before proposing it. */
export async function getHelixOrderSafety(orderId: string, substituteId?: string): Promise<HelixOrderSafety> {
  const qs = substituteId ? `?substitute_medication_id=${encodeURIComponent(substituteId)}` : '';
  return apiFetch(`/helix/orders/${encodeURIComponent(orderId)}/safety${qs}`);
}

// ─── Ward receipt ───
// The other end of a dispense. Separate route and separate permission from the
// pharmacy transitions above (`helix.receive`, which the pharmacist does not
// hold), because the ward attesting that a drug arrived is a different act by a
// different person than pharmacy sending it.

export interface AwaitingReceiptOrder {
  id: string;
  patient_id: string;
  patient_name: string;
  /** The drug that was dispensed: the substitute when pharmacy set one. */
  medication_name: string | null;
  /** The prescribed drug, present only when a substitute was dispensed. */
  original_medication_name?: string | null;
  strength?: string | null;
  form?: string | null;
  dosage: string | null;
  route: string | null;
  frequency: string | null;
  ward: string | null;
  bed: string | null;
  pharmacist_notes?: string | null;
  dispensed_by: string | null;
  dispensed_by_name: string | null;
  dispensed_at: string | null;
}

export async function getOrdersAwaitingReceipt(): Promise<{ orders: AwaitingReceiptOrder[] }> {
  return apiFetch('/helix/orders/awaiting-receipt');
}

export async function receiveHelixOrder(orderId: string, idempotencyKey?: string): Promise<HelixTransitionResult> {
  return apiFetch(`/helix/orders/${orderId}/receive`, {
    method: 'POST',
    body: JSON.stringify({ idempotency_key: idempotencyKey }),
  });
}

/** A `helix_stock_movements` ledger row, newest first. The columns from
 *  `movement_type` on were added by the pharmacy store (migration 54) and are
 *  null on older rows. */
export interface HelixStockMovement {
  id: string;
  lot_id: string | null;
  medication_id: string | null;
  order_id: string | null;
  quantity_change: number | null;
  reason: string | null;
  timestamp: string;
  movement_type: string | null;
  store_id: number | null;
  balance_after_units: number | null;
  unit_cost_paise: number | null;
  reference_type: string | null;
  reference_id: string | null;
  actor_id: string | null;
  remarks: string | null;
  idempotency_key: string | null;
  occurred_at: string | null;
  seq: number | null;
  medication_name: string | null;
  lot_number: string | null;
}

export async function getHelixStockMovements(): Promise<{ movements: HelixStockMovement[] }> {
  return apiFetch('/helix/stock-movements');
}

/** An order's history, oldest first, with who made each step. */
export async function getHelixOrderTransitions(
  orderId: string,
): Promise<{ transitions: (HelixTransition & { actor_name: string | null })[] }> {
  return apiFetch(`/helix/orders/${orderId}/transitions`);
}
