/**
 * The read-only half of the offline contract.
 *
 * Orb's offline mode is READ-ONLY by deliberate product choice: a nurse keeps
 * seeing charts and drug rounds when the wi-fi drops, but cannot write until
 * she is back. The alternative — queuing writes on the tablet — means two
 * nurses can chart the same patient offline and something has to reconcile
 * them later. There is no safe automatic answer to that on a drug chart.
 *
 * So the whole job of this module is to make a refused write UNMISTAKABLE.
 * The dangerous failure is not the refusal; it is a write that looks like it
 * worked. A nurse who believes she charted a dose does not chart it again, and
 * the record simply never exists.
 */

/** Thrown when a write is attempted with no connection. */
export class OfflineWriteRefusedError extends Error {
  /** Discriminator that survives bundling and cross-realm errors, where
   *  `instanceof` quietly stops working. */
  readonly isOfflineWriteRefused = true as const
  readonly action: string

  constructor(action: string) {
    super(offlineRefusalMessage(action))
    this.name = 'OfflineWriteRefusedError'
    this.action = action
    // Restores the prototype chain when compiled down to ES5, where extending
    // a built-in otherwise breaks `instanceof`.
    Object.setPrototypeOf(this, OfflineWriteRefusedError.prototype)
  }
}

/**
 * The words a clinician sees. Deliberately says three things:
 * WHAT she tried to do, that the device is offline, and — the important part —
 * that nothing was saved.
 *
 * It must NEVER imply a queue or a later sync. Read-only was chosen precisely
 * so nothing sits on the tablet pretending it will land later, and "we'll send
 * it when you're back" is exactly the sentence that makes someone walk away
 * from an unrecorded dose.
 */
export function offlineRefusalMessage(action: string): string {
  return (
    `Cannot ${action}. This device is offline. ` +
    `Your entry has not been saved. Reconnect and enter it again.`
  )
}

/**
 * Gate every write behind this.
 *
 * @param action  What the clinician was trying to do, in their words —
 *                "record observations", "give a medication". It is shown to
 *                them, so it must read as an action, not as an endpoint name.
 */
export function assertOnlineForWrite(action: string): void {
  if (!isOnline()) throw new OfflineWriteRefusedError(action)
}

/** Type guard, used to tell this refusal apart from a genuine server error —
 *  they need different messages, because one is "try again later" and the
 *  other is "something is wrong". */
export function isOfflineWriteRefused(error: unknown): error is OfflineWriteRefusedError {
  if (!error || typeof error !== 'object') return false
  return (error as { isOfflineWriteRefused?: unknown }).isOfflineWriteRefused === true
}

/**
 * `navigator.onLine` is the only signal available synchronously, and it is
 * weak: it reports whether there is a network interface, not whether Orb's
 * appliance is reachable. A tablet on ward wi-fi with the server down reads
 * as ONLINE here.
 *
 * That is acceptable for this guard, because it fails in the safe direction —
 * the write proceeds and the real request surfaces the real error. What this
 * guard exists to catch is the unambiguous case: no network at all, where the
 * request would hang and then fail confusingly a long way from the action.
 */
function isOnline(): boolean {
  if (typeof navigator === 'undefined') return true
  return navigator.onLine !== false
}
