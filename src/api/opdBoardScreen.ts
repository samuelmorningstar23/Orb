/**
 * The waiting-room token board SCREEN (opd-9): /display/opd, a device on
 * the wall with no session.
 *
 * It reads the key from the URL FRAGMENT (#key=...), which a browser never
 * sends to a server or writes into a proxy log, and sends it only in the
 * X-Orb-Board-Key header of GET /api/frontdesk/opd/board, with credentials
 * omitted so no staff session in the same browser rides along. The answer is
 * token, doctor and room only (ruling OPD-N7). Nothing here imports the staff
 * API client: the screen holds no session to send.
 */
export const BOARD_KEY_HEADER = 'X-Orb-Board-Key'
export const BOARD_PATH = '/display/opd'
/** Used until the board answers with its own interval (ruling: every 10 s). */
export const DEFAULT_BOARD_POLL_SECONDS = 10

export interface BoardDoctor {
  doctor_name: string
  room: string | null
  /** The token being called in, or null. */
  calling: number | null
  /** The next tokens in seeing order, at most three. */
  next: number[]
}

export interface BoardSnapshot {
  board_label: string | null
  department: string
  visit_date: string
  generated_at: string
  poll_seconds: number
  doctors: BoardDoctor[]
}

export type BoardRead =
  | { kind: 'ok'; snapshot: BoardSnapshot }
  | { kind: 'refused' }
  | { kind: 'off' }
  | { kind: 'unreachable' }

/** The board key in a location hash ('#key=...'), or null. Never the query string. */
export function boardKeyFromHash(hash: string | null | undefined): string | null {
  const raw = (hash ?? '').replace(/^#/, '')
  if (!raw) return null
  const key = new URLSearchParams(raw).get('key')
  return key && key.trim() ? key.trim() : null
}

/** The address a board screen opens: the key in the fragment only. */
export function boardLink(origin: string, key: string): string {
  return `${origin.replace(/\/+$/, '')}${BOARD_PATH}#key=${encodeURIComponent(key)}`
}

/** One read of the board. Never throws; never puts the key anywhere but the header. */
export async function readBoard(
  key: string,
  fetchImpl: typeof fetch = fetch,
  base = '/api',
): Promise<BoardRead> {
  let response: Response
  try {
    response = await fetchImpl(`${base}/frontdesk/opd/board`, {
      method: 'GET',
      headers: { [BOARD_KEY_HEADER]: key, Accept: 'application/json' },
      credentials: 'omit',
      cache: 'no-store',
    })
  } catch {
    return { kind: 'unreachable' }
  }
  if (response.status === 401) return { kind: 'refused' }
  if (response.status === 404) return { kind: 'off' }
  if (!response.ok) return { kind: 'unreachable' }
  try {
    const snapshot = (await response.json()) as BoardSnapshot
    if (!snapshot || !Array.isArray(snapshot.doctors)) return { kind: 'unreachable' }
    return { kind: 'ok', snapshot }
  } catch {
    return { kind: 'unreachable' }
  }
}
