/**
 * Session scoping for nurse in-progress drafts on a SHARED WARD TABLET.
 *
 * The nurse surfaces deliberately mirror in-progress work to localStorage — a
 * nurse who is pulled away mid-observation must not lose a reading, and that
 * feature stays. The problem is lifetime, not existence: localStorage outlives
 * the session, so without this module the next person to pick up the tablet can
 * read the previous nurse's half-entered observations for a named patient. That
 * is PHI persisting past the session on a shared device.
 *
 * Three bounds, so the draft survives an interruption but not a handover:
 *
 *   1. LOGOUT clears every `orb:nurse:*` key (clearNurseDrafts, called from the
 *      one logout path in App.tsx). Prefix-based on purpose: it covers the obs
 *      drafts, the handover notes, and anything added to this family later,
 *      without each surface having to remember to opt in.
 *   2. OWNER STAMP — a draft records who wrote it, and is refused to anyone
 *      else. This is the belt-and-braces bound for the case logout never runs:
 *      the tablet's battery dies, the browser is force-closed, the tab is
 *      killed. Ownership is checked on READ, which no crash can skip.
 *   3. AGE — a draft older than one long shift is stale by definition. Nobody
 *      returns to a half-entered set of obs 13 hours later, and by then the
 *      patient's real observations have been charted several times over.
 *
 * None of these bounds touch the within-session behaviour: same user, same
 * shift, reopening the sheet still gets their values back.
 */

/** Every nurse draft key starts with this. The logout sweep is prefix-based. */
export const NURSE_DRAFT_PREFIX = 'orb:nurse:'

/** One long shift. A draft older than this is never restored. */
export const NURSE_DRAFT_MAX_AGE_MS = 12 * 60 * 60 * 1000

export const obsDraftKey = (patientId: string) => `${NURSE_DRAFT_PREFIX}obs-draft:${patientId}`

/** Fields every nurse draft carries so it can be scoped to a session. */
export interface DraftEnvelope {
  /** User id of the nurse who wrote it. A different user never sees it. */
  ownerId?: string
  /** Epoch ms. Used for the age bound and for the "restored from…" note. */
  savedAt?: number
}

/**
 * Is this draft still the current user's, and still fresh?
 *
 * Deliberately strict: an unstamped draft (written before this scoping existed,
 * or by a build without it) is treated as somebody else's and discarded. The
 * cost of being wrong is one re-typed set of obs; the cost of the other
 * direction is showing one nurse another nurse's PHI.
 */
export function isOwnFreshDraft(
  draft: DraftEnvelope | null | undefined,
  ownerId: string | null | undefined,
  now: number = Date.now(),
): boolean {
  if (!draft || !ownerId) return false
  if (!draft.ownerId || draft.ownerId !== ownerId) return false
  if (typeof draft.savedAt !== 'number' || !Number.isFinite(draft.savedAt)) return false
  const age = now - draft.savedAt
  return age >= 0 && age <= NURSE_DRAFT_MAX_AGE_MS
}

/** Every `orb:nurse:*` key currently in localStorage. */
function nurseDraftKeys(): string[] {
  const keys: string[] = []
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i)
      if (key && key.startsWith(NURSE_DRAFT_PREFIX)) keys.push(key)
    }
  } catch {
    // A blocked or unavailable localStorage means there is nothing to clear.
  }
  return keys
}

/**
 * Drop every nurse draft on this device. Call on logout and on sign-out from an
 * expired session — the two moments a shared tablet changes hands.
 *
 * Returns how many keys were removed, so a caller can log or test it. Never
 * throws: a failure to clear must not block a nurse from signing out.
 */
export function clearNurseDrafts(): number {
  const keys = nurseDraftKeys()
  let removed = 0
  for (const key of keys) {
    try {
      localStorage.removeItem(key)
      removed++
    } catch {
      // Keep going: one unremovable key must not strand the rest.
    }
  }
  return removed
}
