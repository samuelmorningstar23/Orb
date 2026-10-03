/** Relay thread reconciliation.
 *
 *  A sent message reaches the browser through THREE independent paths, and
 *  they race:
 *
 *    1. the optimistic bubble the composer paints immediately (temp id)
 *    2. the `message:new` socket broadcast, carrying the REAL server id
 *    3. the POST response, which swaps the optimistic entry to the real id
 *
 *  If (2) lands before (3) — which it usually does, because the broadcast is
 *  emitted before the HTTP response is serialised — the thread briefly holds
 *  the temp entry AND the real entry. Step (3) then rewrites the temp entry to
 *  the real id, producing two entries with the SAME id: React logs
 *  "Encountered two children with the same key" and the message renders twice,
 *  each copy carrying its own wrong-room warning card. The database has one
 *  row throughout; this is purely a client reconciliation defect.
 *
 *  The fix is two-part and lives here so it can be tested without a DOM:
 *    - `claimOptimistic` lets the socket echo recognise its own optimistic
 *      bubble by content+sender and replace it in place, so the doubled bubble
 *      never paints at all.
 *    - `mergeById` is the belt-and-braces invariant: whatever order the three
 *      paths land in, the list that reaches React has one entry per id.
 */

/** The minimum a message must have for these two functions to reconcile it.
 *
 *  No `[k: string]: unknown` index signature. It was here to say "carries other
 *  fields too", but TypeScript only accepts an index signature from a type that
 *  declares one, and `Message` in api/client.ts does not — so the real Relay
 *  message type was NOT assignable to this one, and every call site in
 *  RelayPanel.tsx was a type error (7 of them). Nothing in this module ever
 *  indexes a message by an arbitrary key: it reads the four named fields and
 *  spreads the rest through. The constraint only ever needed to name what it
 *  actually touches, and a generic `T extends MergeableMessage` already carries
 *  every extra field of the concrete type through to the return type. */
export interface MergeableMessage {
  id: number
  sender_id: string
  content: string
  timestamp: string
  client_key?: string
}

/** Collapse a message list to one entry per id.
 *
 *  First occurrence keeps its position (so the thread does not reorder under
 *  the reader), and later copies are merged over it — the server-authored copy
 *  therefore wins on every field it defines, while client-only fields such as
 *  `client_key` survive. */
export function mergeById<T extends MergeableMessage>(list: T[]): T[] {
  const at = new Map<number, number>()
  const out: T[] = []
  for (const m of list) {
    const i = at.get(m.id)
    if (i === undefined) {
      at.set(m.id, out.length)
      out.push(m)
    } else {
      out[i] = { ...out[i], ...m }
    }
  }
  return out
}

/** Replace a still-optimistic bubble with the server copy of the same send.
 *
 *  Returns the updated list, or `null` when nothing matched (the caller should
 *  then append normally — this is somebody else's message).
 *
 *  Matching is deliberately narrow: same sender, identical content, and the
 *  candidate must still be optimistic (`client_key` present, i.e. it has not
 *  yet been swapped to a real id). Two genuinely distinct sends of the same
 *  text are matched one-for-one because each claim clears the entry it used. */
export function claimOptimistic<T extends MergeableMessage>(
  list: T[],
  incoming: T,
): T[] | null {
  const idx = list.findIndex(
    m =>
      m.client_key !== undefined &&
      m.id !== incoming.id &&
      m.sender_id === incoming.sender_id &&
      m.content === incoming.content,
  )
  if (idx === -1) return null
  const next = list.slice()
  // Keep the optimistic entry's position: the composer already scrolled to it,
  // and re-sorting on the server timestamp would make it jump.
  next[idx] = { ...next[idx], ...incoming, client_key: undefined }
  return mergeById(next)
}
