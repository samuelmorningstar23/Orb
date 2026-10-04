/**
 * Orb, Pharmacy store API: the shared base for every /api/pharmacy-store call.
 *
 * THIS FOLDER GROWS ONE FILE PER AREA. Masters, purchasing, receipts, stock,
 * transfers, lots, counts, NDPS, vendor returns, ward stock and ward returns
 * each arrive as their own file beside this one (masters.ts, purchasing.ts and
 * so on), written by the cluster that builds that area. Each of them imports the
 * helpers below and adds nothing else to this file, so two areas built in the
 * same week never edit the same module.
 *
 * WHY A TYPED ERROR. Every refusal this module can produce is one a screen has
 * to treat differently: `module_disabled` is a whole-panel state, a 404 on a
 * document id is "not found", a 403 names a separation-of-duties rule, a 409 is
 * a stock or status conflict. The server's machine-readable `error` code is kept
 * on `PharmacyStoreApiError.code` so a view branches on the code, never on
 * English. The calls go through `apiFetch`, so a pharmacy store screen gets the
 * same session refresh, cookie and CSRF handling as every other screen.
 *
 * QUANTITIES. Drug stock is counted in whole DISPENSE UNITS (tablets, vials,
 * ampoules), integers on the wire. Money is integer paise. Nothing in this
 * folder parses a float.
 */
import { API_BASE, apiFetch, buildHeaders, errorDetailMessage, handleResponseError } from '../client'

/** The server router's prefix under /api. */
export const PHARMACY_STORE_API_PREFIX = '/pharmacy-store'

/** The module id on /api/health and in the shell (HospitalModuleId). */
export const PHARMACY_STORE_MODULE_ID = 'pharmacy-store' as const

const UNREACHABLE_MESSAGE =
  'Could not reach the pharmacy store. Check the connection and try again.'

/** A refusal from the pharmacy store API, with the server's code intact. */
export class PharmacyStoreApiError extends Error {
  /** HTTP status, or 0 when no reply arrived at all. */
  readonly status: number
  /** The server's `detail.error`, else `http_<status>`, else `network_error`. */
  readonly code: string
  /** The server's structured `detail`, untouched, for views that need more. */
  readonly detail: unknown

  constructor(message: string, status: number, code: string, detail: unknown = null) {
    super(message)
    this.name = 'PharmacyStoreApiError'
    this.status = status
    this.code = code
    this.detail = detail
  }

  /** This site has the pharmacy store switched off (ORB_PHARMACY_STORE). */
  get isModuleDisabled(): boolean {
    return this.code === 'module_disabled'
  }

  /** No such document here. A pharmacy id asked of a stores route, or another
   *  ward's id, also answers 404, by design. */
  get isNotFound(): boolean {
    return this.status === 404 && !this.isModuleDisabled
  }

  /** The caller lacks the permission, or a separation-of-duties rule refused. */
  get isForbidden(): boolean {
    return this.status === 403
  }

  /** A stock or status conflict: the document moved on since it was read. */
  get isConflict(): boolean {
    return this.status === 409
  }

  /** No reply at all: offline, or the server is down. */
  get isUnreachable(): boolean {
    return this.status === 0
  }
}

function detailCode(detail: unknown): string | null {
  if (detail && typeof detail === 'object') {
    const code = (detail as { error?: unknown }).error
    if (typeof code === 'string' && code.trim()) return code
  }
  return null
}

/** Whatever `apiFetch` (or `fetch`) threw, as a PharmacyStoreApiError. */
export function toPharmacyStoreError(err: unknown): PharmacyStoreApiError {
  if (err instanceof PharmacyStoreApiError) return err
  const raw = (err ?? {}) as {
    message?: unknown
    status?: unknown
    detail?: unknown
    permissions?: unknown
  }
  const status = typeof raw.status === 'number' ? raw.status : 0
  // apiFetch puts a 403's structured body on `permissions`, every other one on
  // `detail`.
  const detail = raw.detail ?? raw.permissions ?? null
  const code = detailCode(detail) ?? (status ? `http_${status}` : 'network_error')
  const message = status === 0
    ? UNREACHABLE_MESSAGE
    : (typeof raw.message === 'string' && raw.message.trim()
        ? raw.message
        : errorDetailMessage(detail, status))
  return new PharmacyStoreApiError(message, status, code, detail)
}

export type PharmacyStoreQueryValue = string | number | boolean | null | undefined

/** `?a=1&b=x` from a params object. Empty, null and undefined values are left
 *  out, so an unset filter is never sent as the string "undefined". */
export function pharmacyStoreQuery(params: Record<string, PharmacyStoreQueryValue> = {}): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue
    search.set(key, String(value))
  }
  const text = search.toString()
  return text ? `?${text}` : ''
}

function areaPath(path: string): string {
  if (!path.startsWith('/')) {
    throw new Error(`pharmacy store paths start with "/" (got "${path}")`)
  }
  return `${PHARMACY_STORE_API_PREFIX}${path}`
}

/** One call to /api/pharmacy-store<path>. Throws PharmacyStoreApiError. */
export async function pharmacyStoreRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  const target = areaPath(path)
  try {
    return await apiFetch<T>(target, init)
  } catch (err) {
    throw toPharmacyStoreError(err)
  }
}

export function pharmacyStoreGet<T>(
  path: string,
  params?: Record<string, PharmacyStoreQueryValue>,
): Promise<T> {
  return pharmacyStoreRequest<T>(`${path}${pharmacyStoreQuery(params)}`)
}

export function pharmacyStorePost<T>(path: string, body?: unknown): Promise<T> {
  return pharmacyStoreRequest<T>(path, { method: 'POST', body: JSON.stringify(body ?? {}) })
}

export function pharmacyStorePut<T>(path: string, body?: unknown): Promise<T> {
  return pharmacyStoreRequest<T>(path, { method: 'PUT', body: JSON.stringify(body ?? {}) })
}

export function pharmacyStorePatch<T>(path: string, body?: unknown): Promise<T> {
  return pharmacyStoreRequest<T>(path, { method: 'PATCH', body: JSON.stringify(body ?? {}) })
}

/** The window event App.tsx answers with the re-auth modal (see
 *  notifySessionExpired in ../client). */
const SESSION_EXPIRED_EVENT = 'orb:session-expired'

/** One single-flight session refresh after a 401, through the client's own
 *  raw-fetch helper (handleResponseError owns the refresh, and raises the
 *  re-auth modal itself when the session is dead). It always throws, so the
 *  answer is read from whether it raised the modal: true means the session was
 *  re-minted and the request may be sent once more. */
async function refreshAfter401(response: Response): Promise<boolean> {
  let expired = false
  const markExpired = () => { expired = true }
  window.addEventListener(SESSION_EXPIRED_EVENT, markExpired)
  try {
    await handleResponseError(response)
  } catch {
    // Expected: it throws whether or not the refresh worked.
  } finally {
    window.removeEventListener(SESSION_EXPIRED_EVENT, markExpired)
  }
  return !expired
}

function sessionExpiredError(): PharmacyStoreApiError {
  return new PharmacyStoreApiError('Session expired', 401, 'http_401')
}

/** A file from the pharmacy store (a CSV export), as a Blob. `apiFetch` reads
 *  every reply as JSON, so this one call uses `fetch` with the same headers
 *  and cookie. A refusal still arrives as a PharmacyStoreApiError.
 *
 *  A 401 follows apiFetch's session contract: one single-flight refresh, one
 *  retry, and a hard expiry (the re-auth modal) when the refresh fails or the
 *  retry 401s again. Never a second refresh, never a blind retry after a
 *  failed one. A download is a GET with no body, so it can be replayed. */
export async function pharmacyStoreDownload(
  path: string,
  params?: Record<string, PharmacyStoreQueryValue>,
): Promise<Blob> {
  const target = `${API_BASE}${areaPath(path)}${pharmacyStoreQuery(params)}`
  // Headers are rebuilt per attempt: a refresh rotates the CSRF cookie.
  const send = async (): Promise<Response> => {
    try {
      return await fetch(target, { credentials: 'include', headers: buildHeaders('GET') })
    } catch (err) {
      throw toPharmacyStoreError(err)
    }
  }
  let response = await send()
  if (response.status === 401) {
    if (!(await refreshAfter401(response))) throw sessionExpiredError()
    response = await send()
    if (response.status === 401) {
      window.dispatchEvent(new CustomEvent(SESSION_EXPIRED_EVENT))
      throw sessionExpiredError()
    }
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null) as { detail?: unknown } | null
    const detail = body?.detail ?? null
    throw new PharmacyStoreApiError(
      errorDetailMessage(detail, response.status),
      response.status,
      detailCode(detail) ?? `http_${response.status}`,
      detail,
    )
  }
  return response.blob()
}

/** What GET /api/pharmacy-store/meta answers. The panel relies only on the
 *  call succeeding; the fields are passed through to every view, which reads
 *  what it knows and ignores the rest. */
export interface PharmacyStoreMeta {
  module?: string
  enabled?: boolean
  [key: string]: unknown
}

/** The module's own probe. A 404 `module_disabled` means switched off here. */
export function getPharmacyStoreMeta(): Promise<PharmacyStoreMeta> {
  return pharmacyStoreGet<PharmacyStoreMeta>('/meta')
}
