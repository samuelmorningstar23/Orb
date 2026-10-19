/**
 * Orb, Beds and admissions: the live bed board event (adt-19, HIS wave 9).
 *
 * After an ADT write commits (an admission, a bed move, a transfer, leave or
 * absconding, an end of stay), the server sends 'adt:changed' to the boards
 * that may show the ward (server/services/adt/live.py, server/ws/realtime.py).
 * The event is CONTENT-FREE: {ward_id} and nothing else, never a patient id,
 * name, bed or department. A board answers by re-reading GET /api/adt/board,
 * which applies the reader's own scope and is audited on the server; the
 * board's 30 second poll stays as the fallback when the socket is down.
 */
import { socket } from '../client'

/** The event name the server emits. */
export const ADT_CHANGED_EVENT = 'adt:changed'

/** The whole event body. */
export interface AdtChanged {
  /** The ward whose beds changed; null for an admission waiting for a bed on
   *  no ward yet. */
  ward_id: string | null
}

/** The ward an event names, or null. Anything that is not a non-blank string
 *  reads as no ward (the board then refetches, the safe way round). */
export function changedWard(payload: unknown): string | null {
  if (!payload || typeof payload !== 'object') return null
  const ward = (payload as { ward_id?: unknown }).ward_id
  return typeof ward === 'string' && ward.trim() ? ward : null
}

/** Listen for 'adt:changed'. Returns the unsubscribe. */
export function subscribeAdtChanged(handler: (event: AdtChanged) => void): () => void {
  const listener = (payload: unknown) => handler({ ward_id: changedWard(payload) })
  socket.on(ADT_CHANGED_EVENT, listener)
  return () => { socket.off(ADT_CHANGED_EVENT, listener) }
}

/** Listen for the socket coming back after it dropped (a reconnect), or
 *  connecting for the first time after the board was read, so a board can
 *  re-read the changes it missed. Returns the unsubscribe. */
export function subscribeSocketReconnect(handler: () => void): () => void {
  let dropped = (socket as { connected?: unknown }).connected === false
  const onDisconnect = () => { dropped = true }
  const onConnect = () => {
    if (dropped) {
      dropped = false
      handler()
    }
  }
  socket.on('disconnect', onDisconnect)
  socket.on('connect', onConnect)
  return () => {
    socket.off('disconnect', onDisconnect)
    socket.off('connect', onConnect)
  }
}
