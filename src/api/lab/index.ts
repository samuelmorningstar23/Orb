/**
 * Orb, Laboratory API: the shared base for every /api/lab call (lis-1).
 *
 * THIS FOLDER GROWS ONE FILE PER AREA. The catalogue and master screens use
 * ./master.ts; orders, specimens, results, criticals, reports, turnaround times
 * and analysers each arrive as their own file beside it, written by the
 * increment that builds that area, importing the helpers below and adding
 * nothing to this file.
 *
 * THE LABORATORY HAS THREE STATES, not two (ruling LIS-R21): 'on', 'draining'
 * (switched off with open work, so laboratory staff can still finish, cancel
 * or release it while new work is refused) and 'off'. A route that is not
 * served in the current state answers 404 `module_disabled`, which a screen
 * treats as a whole-panel state and never as an empty list.
 *
 * WHY A TYPED ERROR. The server's machine-readable `error` code is kept on
 * `LabApiError.code` (precision, overlap, retire_ranges_first, not_numeric,
 * permission_denied and so on) so a view branches on the code, never on
 * English. The calls go through `apiFetch`, so every laboratory screen gets
 * the same session refresh, cookie and CSRF handling as every other screen.
 *
 * NUMBERS. A laboratory value is decided at its reporting precision. Send a
 * typed value as the string the person typed ("5.10"), never as a parsed
 * float; the server checks it against the test's decimal places.
 */
import { apiFetch, errorDetailMessage } from '../client'

/** The server router's prefix under /api. */
export const LAB_API_PREFIX = '/lab'

/** The module id on /api/health and in the shell (HospitalModuleId). */
export const LAB_MODULE_ID = 'lab' as const

/** What module_states.lab can say. */
export type LabModuleState = 'on' | 'draining' | 'off'

const UNREACHABLE_MESSAGE =
  'Could not reach the laboratory. Check the connection and try again.'

/** A refusal from the laboratory API, with the server's code intact. */
export class LabApiError extends Error {
  /** HTTP status, or 0 when no reply arrived at all. */
  readonly status: number
  /** The server's `detail.error`, else `http_<status>`, else `network_error`. */
  readonly code: string
  /** The server's structured `detail`, untouched. */
  readonly detail: unknown

  constructor(message: string, status: number, code: string, detail: unknown = null) {
    super(message)
    this.name = 'LabApiError'
    this.status = status
    this.code = code
    this.detail = detail
  }

  /** The laboratory is switched off here, or draining and this call starts
   *  new work. */
  get isModuleDisabled(): boolean {
    return this.code === 'module_disabled'
  }

  /** No such record here. */
  get isNotFound(): boolean {
    return this.status === 404 && !this.isModuleDisabled
  }

  /** The caller lacks the permission (only a pathologist edits the catalogue). */
  get isForbidden(): boolean {
    return this.status === 403
  }

  /** The record moved on since it was read, or a range overlaps. */
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

/** Whatever `apiFetch` (or `fetch`) threw, as a LabApiError. */
export function toLabError(err: unknown): LabApiError {
  if (err instanceof LabApiError) return err
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
  return new LabApiError(message, status, code, detail)
}

export type LabQueryValue = string | number | boolean | null | undefined

/** `?a=1&b=x` from a params object. Empty, null and undefined values are left
 *  out, so an unset filter is never sent as the string "undefined". */
export function labQuery(params: Record<string, LabQueryValue> = {}): string {
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
    throw new Error(`laboratory paths start with "/" (got "${path}")`)
  }
  return `${LAB_API_PREFIX}${path}`
}

/** One call to /api/lab<path>. Throws LabApiError. */
export async function labRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  const target = areaPath(path)
  try {
    return await apiFetch<T>(target, init)
  } catch (err) {
    throw toLabError(err)
  }
}

export function labGet<T>(path: string, params?: Record<string, LabQueryValue>): Promise<T> {
  return labRequest<T>(`${path}${labQuery(params)}`)
}

export function labPost<T>(path: string, body?: unknown): Promise<T> {
  return labRequest<T>(path, { method: 'POST', body: JSON.stringify(body ?? {}) })
}

export function labPut<T>(path: string, body?: unknown): Promise<T> {
  return labRequest<T>(path, { method: 'PUT', body: JSON.stringify(body ?? {}) })
}

export function labPatch<T>(path: string, body?: unknown): Promise<T> {
  return labRequest<T>(path, { method: 'PATCH', body: JSON.stringify(body ?? {}) })
}

/** What GET /api/lab/meta answers. No patient data. */
export interface LabMeta {
  module?: string
  state?: LabModuleState
  features?: Record<string, boolean>
  settings?: Record<string, unknown>
  [key: string]: unknown
}

/** The module's own probe. A 404 `module_disabled` means switched off here. */
export function getLabMeta(): Promise<LabMeta> {
  return labGet<LabMeta>('/meta')
}
