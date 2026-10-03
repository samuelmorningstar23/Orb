/**
 * A fresh random id (Idempotency-Key, offline queue id, client draft id).
 *
 * crypto.randomUUID exists only in a SECURE context (https, or localhost). On a
 * ward tablet that opens Orb as http://<appliance-ip>, it is undefined, and
 * every unguarded call threw: Relay sends, observations, medication orders and
 * administrations all failed on the second device. This uses randomUUID when
 * it is there, then crypto.getRandomValues (available in insecure contexts
 * too), and only then Math.random, so an id is always produced.
 */
export function newId(): string {
  const c: Crypto | undefined = globalThis.crypto
  if (c && typeof c.randomUUID === 'function') {
    try {
      return c.randomUUID()
    } catch {
      // Some browsers expose the method and still refuse it outside a secure context.
    }
  }
  const bytes = new Uint8Array(16)
  if (c && typeof c.getRandomValues === 'function') {
    c.getRandomValues(bytes)
  } else {
    for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256)
  }
  // RFC 4122 version 4 layout, so the server sees the same shape either way.
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}
