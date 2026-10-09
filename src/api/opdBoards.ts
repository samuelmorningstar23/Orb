/**
 * The waiting-room token board (opd-9).
 *
 * Two halves that never share a credential:
 *
 *   * THE SETTINGS (Front Desk, Display boards; schedule.manage, the
 *     administrator). GET, POST /api/frontdesk/opd/boards and POST
 *     .../{id}/revoke through apiFetch, on the staff session. Making a board
 *     answers its device key ONCE; it is never stored and never shown again.
 *
 *   * THE SCREEN (/display/opd, a device on the wall, no session). It reads
 *     the key from the URL FRAGMENT (#key=...), which a browser never sends to
 *     a server or writes into a proxy log, and sends it only in the
 *     X-Orb-Board-Key header of GET /api/frontdesk/opd/board, with
 *     credentials omitted so no staff session in the same browser rides along.
 *     The answer is token, doctor and room only (ruling OPD-N7).
 *
 * Every call exists only while the server's 'opd' feature is on (404
 * feature_disabled otherwise).
 */
import { apiFetch } from './client'

export { BOARD_PATH, boardLink } from './opdBoardScreen'

// ── The settings (schedule.manage) ───────────────────────────────────────────

export interface DisplayBoard {
  id: string
  department: string
  label: string
  created_by: string | null
  created_by_name: string | null
  created_at: string
  revoked_by: string | null
  revoked_by_name: string | null
  revoked_at: string | null
  active: boolean
}

export interface DisplayBoardList {
  boards: DisplayBoard[]
  departments: string[]
  poll_seconds: number
}

export async function listDisplayBoards(): Promise<DisplayBoardList> {
  return apiFetch<DisplayBoardList>('/frontdesk/opd/boards')
}

export async function createDisplayBoard(input: { department: string; label?: string }):
  Promise<{ board: DisplayBoard; key: string }> {
  return apiFetch('/frontdesk/opd/boards', {
    method: 'POST',
    body: JSON.stringify({
      department: input.department,
      ...(input.label && input.label.trim() ? { label: input.label.trim() } : {}),
    }),
  })
}

export async function revokeDisplayBoard(boardId: string, reason?: string): Promise<DisplayBoard> {
  return apiFetch(`/frontdesk/opd/boards/${encodeURIComponent(boardId)}/revoke`, {
    method: 'POST',
    body: JSON.stringify(reason && reason.trim() ? { reason: reason.trim() } : {}),
  })
}

/** Which switch a 404 'feature_disabled' names, or null for any other error. */
export function featureOffOf(err: unknown): string | null {
  const e = err as { status?: number; detail?: { error?: string; feature?: string } } | null
  if (!e || e.status !== 404 || e.detail?.error !== 'feature_disabled') return null
  return e.detail.feature ?? 'opd'
}
