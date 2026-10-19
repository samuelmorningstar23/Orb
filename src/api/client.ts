import { readSSEEvents, parseSSEData } from './sse';
export { readSSEEvents, parseSSEData } from './sse';
import type {
  StoredUser,
  ApiPermissionError,
  OfflineRequest,
  LoginResponse,
  Patient,
  ChartVitalsRow,
  ClinicalDocumentRow,
  ScribeNoteRow,
  LensAnalysisRow,
  PatientDetail,
  TimelineProjection,
  PatientForecast,
  AdmissionRequest,
  AdmissionResult,
  Channel,
  Message,
  RoomFitFlag,
  SageAmbientMemory,
  SageHandoff,
  SageAction,
  SageInsight,
  SageStreamMetadata,
  SageAiPosture,
  WardBrief,
  SageHistoryMessage,
  SageVoiceTiming,
  ScribeVerification,
  ScribePipelineMeta,
  ScribeConfig,
  ScribeAsrStatus,
  BridgeStatus,
  BridgeMedication,
  BridgeReports,
  DischargeTimelineEntry,
  MedicationSafety,
  SafetyAck,
  SafetyAckRequiredError,
  AddClinicalNoteResult,
  MedicationOrderInput,
  CreateOrderPayload,
  AdministrationRecord,
  AdministerPayload,
  StoryType,
  StoryEvent,
  StoryFeed,
  DischargeResult,
  DischargeSummaryView,
  EscalationChain,
  DischargedPatient,
  SurgeryScheduleDay,
  SurgeryDetail,
  WhoChecklistTemplate,
  WhoChecklistState,
  EmergencySurgeryCase,
  SurgeryCase,
  AuditFilters,
  AuditLogsResponse,
  AdminOverviewData,
  ModelRegistryEntry,
  IngestionEvent,
  DischargeAccessGrant,
  VerifiedDischargeSummary,
  HelixOrderRow,
  HelixOrderListRow,
  HelixTransition,
  HelixTransitionResult,
  HelixOrderSafety,
  AwaitingReceiptOrder,
  HelixStockMovement
} from './contracts';
export type {
  StoredUser,
  ApiPermissionDetail,
  ApiPermissionError,
  OfflineRequest,
  LoginResponse,
  Patient,
  ChartVitalsRow,
  PatientRisk,
  VitalsTrend,
  ChartPatientRecord,
  ClinicalDocumentRow,
  ScribeNoteRow,
  LensAnalysisRow,
  ChartMedicationRow,
  ChartLabObservation,
  PatientDetail,
  TimelinePhase,
  TimelineProjection,
  PatientForecast,
  AdmissionVitals,
  AdmissionRequest,
  AdmissionResult,
  Channel,
  Message,
  RoomFitFlag,
  SageAmbientMemory,
  SageHandoffAlert,
  SageHandoffSafety,
  SageHandoff,
  SageAction,
  SageInsight,
  SageSourceRef,
  SageProviderInfo,
  SageStreamTiming,
  SageStructuredResponse,
  SageStreamMetadata,
  SageAiPosture,
  WardBrief,
  SageHistoryMessage,
  SageVoiceTiming,
  ScribeVerificationWarning,
  ScribeVerification,
  ScribePipelineMeta,
  ScribeConfig,
  ScribeAsrStatus,
  BridgeStatus,
  BridgeMedication,
  BridgeDocument,
  BridgeDischargeView,
  BridgeReports,
  DischargeTimelineEntry,
  SafetyAlert,
  MedicationSafety,
  SafetyAck,
  SafetyAckRequiredError,
  AddClinicalNoteResult,
  MedicationOrderInput,
  CreateOrderPayload,
  AllergyAlert,
  AllergyCheck,
  DoseCheckFinding,
  DoseCheckSummary,
  AdministrationRecord,
  AdministerPayload,
  StoryType,
  StoryEvent,
  StoryFeed,
  DischargeResult,
  DischargeSummaryView,
  EscalationChain,
  DischargedPatient,
  SurgeryRow,
  SurgeryScheduleCase,
  SurgeryScheduleDay,
  SurgeryImportedContext,
  SurgeryDetail,
  WhoChecklistTemplate,
  WhoChecklistState,
  EmergencySurgeryCase,
  SurgeryTeamAssignment,
  SurgeryCase,
  AuditFilters,
  AuditLog,
  AuditLogsResponse,
  AdminAlertCounts,
  AdminRecordCounts,
  AdminOverviewData,
  ModelRegistryEntry,
  IngestionEvent,
  DischargeAccessGrant,
  VerifiedDischargeSummary,
  HelixOrderRow,
  HelixOrderListRow,
  HelixTransition,
  HelixTransitionResult,
  HelixOrderSafety,
  AwaitingReceiptOrder,
  HelixStockMovement
} from './contracts';
/**
 * Orb Hospital OS — API Client
 * Handles all HTTP requests to the FastAPI backend.
 */

import { io } from 'socket.io-client';
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


/** Reconstruct the data payload of a single SSE event: strip the `data: ` prefix
 *  from each line (keeping any non-prefixed lines verbatim, matching the prior
 *  permissive behavior), joined by newlines. */


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

export async function getPatients(): Promise<Patient[]> {
  return apiFetch('/vigil/patients');
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

/** Sage-deduced expected clinical course (phases + expected LOS), cached server-side per patient. */
export async function getTimelineProjection(patientId: string, refresh = false): Promise<TimelineProjection> {
  return apiFetch(`/vigil/patients/${patientId}/timeline-projection${refresh ? '?refresh=true' : ''}`);
}

export async function getPatientVitals(patientId: string, hours: number = 6): Promise<ChartVitalsRow[]> {
  return apiFetch(`/vigil/patients/${patientId}/vitals?hours=${hours}`);
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

export async function admitPatient(data: AdmissionRequest): Promise<AdmissionResult> {
  return apiFetch('/admissions/admit', {
    method: 'POST',
    body: JSON.stringify(data),
  });
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

export async function getWardBrief(): Promise<WardBrief> {
  return apiFetch('/sage/ward-brief');
}

/** This user's recent turns across threads, oldest first. */
export async function getSageHistory(): Promise<{ messages: SageHistoryMessage[] }> {
  return apiFetch('/sage/history');
}

export async function clearSageHistory(): Promise<void> {
  await apiFetch('/sage/history', { method: 'DELETE' });
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

export function isSafetyAckRequired(err: unknown): err is SafetyAckRequiredError {
  return err instanceof Error && (err as SafetyAckRequiredError).code === 'safety_ack_required';
}

// ─── Enhanced Vigil ───
export async function getPatientNotes(patientId: string): Promise<ClinicalDocumentRow[]> {
  return apiFetch(`/vigil/patients/${patientId}/notes`);
}

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

export async function dischargePatient(
  patientId: string,
): Promise<DischargeResult | { status: 'already_discharged'; patient_id: string }> {
  return apiFetch(`/vigil/patients/${patientId}/discharge`, { method: 'POST' });
}

export async function getDischargeSummary(patientId: string): Promise<DischargeSummaryView> {
  return apiFetch(`/vigil/patients/${patientId}/discharge-summary`);
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

export async function getSurgerySchedule(days: number = 7): Promise<SurgeryScheduleDay[]> {
  return apiFetch(`/surgery/schedule?days=${days}`);
}

export async function getSurgeryDetail(surgeryId: string): Promise<SurgeryDetail> {
  return apiFetch(`/surgery/${surgeryId}`);
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

export async function getEmergencyQueue(): Promise<EmergencySurgeryCase[]> {
  return apiFetch(`/surgery/emergency-queue`)
}

export async function acknowledgeEmergency(surgeryId: string): Promise<void> {
  await apiFetch(`/surgery/emergency/${surgeryId}/acknowledge`, { method: 'POST' })
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

export async function getAdminOverview(): Promise<AdminOverviewData> {
  return apiFetch('/admin/overview');
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

export async function getIngestionStatus(
  limit: number = 20,
  status?: string,
): Promise<{ events: IngestionEvent[]; count: number; ingestion_mode: string }> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (status) params.set('status', status);
  return apiFetch(`/ingest/status?${params.toString()}`);
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

/** The allergy and interaction screen for the drug an order would dispense.
 *  Pass `substituteId` to preview a substitute before proposing it. */
export async function getHelixOrderSafety(orderId: string, substituteId?: string): Promise<HelixOrderSafety> {
  const qs = substituteId ? `?substitute_medication_id=${encodeURIComponent(substituteId)}` : '';
  return apiFetch(`/helix/orders/${encodeURIComponent(orderId)}/safety${qs}`);
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

export async function getHelixStockMovements(): Promise<{ movements: HelixStockMovement[] }> {
  return apiFetch('/helix/stock-movements');
}

/** An order's history, oldest first, with who made each step. */
export async function getHelixOrderTransitions(
  orderId: string,
): Promise<{ transitions: (HelixTransition & { actor_name: string | null })[] }> {
  return apiFetch(`/helix/orders/${orderId}/transitions`);
}
