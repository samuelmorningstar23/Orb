import { useCallback, useEffect, useState } from 'react'
import {
  getOrdersAwaitingReceipt,
  receiveHelixOrder,
  type AwaitingReceiptOrder,
} from '../../../api/client'
import {
  medOrderChip,
  subscribeMedOrderUpdates,
  type HelixLinkedOrder,
} from '../../../api/medorders'
import { useFeatureFlags } from '../../../shellConfig'
import { clockTime } from './time'
import WardReturnsSection from './WardReturnsSection'
import WardStockSection from './WardStockSection'

/** A delivery the server tied to its medication order (medorders-10): only a
 *  supply row linked to an order carries `linked_order`. */
type Delivery = AwaitingReceiptOrder & { linked_order?: HelixLinkedOrder | null }

/** What the ward should know about the order behind a tray, or null when the
 *  order is live and nothing needs saying beyond its status. */
function orderWarning(order: HelixLinkedOrder): string | null {
  if (order.status === 'held') {
    return 'The prescriber has put this medicine on hold. Sign for what arrived, and do not ' +
      'give it until the order is resumed.'
  }
  if (['stopped', 'completed', 'cancelled', 'rejected'].includes(order.status)) {
    return 'This order has ended. Sign for what arrived, then send it back to the pharmacy.'
  }
  return null
}

/* ═══════════════════════════════════════════════════
   DELIVERIES — the ward end of a pharmacy dispense

   The last hop of the medication custody chain, and until 2026-09-07 it did
   not exist. `dispensed -> delivered` was gated on `helix.write`, so the
   pharmacist who sent the drug also clicked the button that said the ward had
   received it. Two steps, one desk. The row recorded neither who took delivery
   nor when.

   This screen is the other half of that. A nurse signs for what physically
   arrived, in her own name, and the server refuses the write if the account
   receiving is the account that dispensed (helix_service.SelfReceiptRefused).

   ─── WHAT THIS SCREEN IS NOT ───
   It is not a pharmacy view. A nurse holds `helix.receive` and no other Helix
   permission, and the endpoint behind it returns only orders for patients
   already on her ward — no inventory, no formulary, no other ward's orders.

   ─── WARD STOCK (grn-21) ───
   Below the deliveries, ./WardStockSection shows the ward's own stock store
   when the pharmacy store and ward stock are switched on: transfers on their
   way, what the ward holds against its stock list, and a top-up request.
   That is a different chain from this one (stock for the ward, not a dose
   for a patient) and helix.receive alone reaches none of it.

   ─── THE ONE RULE FROM NursePanel THAT BINDS HARDEST HERE ───
   A section that FAILED must never render as empty. "Nothing is in transit"
   and "we could not find out what is in transit" are opposite facts about
   drugs that may be sitting in a tray outside the door, and they must never
   share a shape.
   ═══════════════════════════════════════════════════ */

export default function DeliveriesSheet() {
  const [orders, setOrders] = useState<Delivery[] | null>(null)
  const [failed, setFailed] = useState(false)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [flash, setFlash] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  // When the last read SUCCEEDED, on this device's clock: the endpoint sends no
  // timestamp, and the calm state owes the nurse a time, not a double negative.
  const [checkedAt, setCheckedAt] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const res = await getOrdersAwaitingReceipt()
      setOrders(res.orders || [])
      setFailed(false)
      setCheckedAt(new Date().toISOString())
    } catch {
      // `orders` is deliberately left at whatever it was. Falling back to []
      // would paint the calm "nothing in transit" state over a failed read,
      // which is the one lie this screen must not tell.
      setFailed(true)
    }
  }, [])

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch trigger, not derived state.
    void load()
  }, [load])

  /* With the order store on (medorders-10), a pharmacy step on a linked order
     pushes 'medorders:update' to the ward: a fresh dispense appears here
     without waiting for the nurse to come back to the tab. */
  const storeOn = useFeatureFlags().med_order_store === true
  useEffect(() => {
    if (!storeOn) return
    return subscribeMedOrderUpdates(() => { void load() })
  }, [storeOn, load])

  const receive = async (order: AwaitingReceiptOrder) => {
    setBusyId(order.id)
    setError(null)
    try {
      await receiveHelixOrder(order.id)
      setFlash(`Receipt recorded for ${order.medication_name || 'this medication'}.`)
      await load()
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'Could not record receipt.'
      setError(msg)
    } finally {
      setBusyId(null)
    }
  }

  const describe = (o: AwaitingReceiptOrder) => {
    // Most formulary rows already carry the strength in the name ("Paracetamol
    // 650mg Oral Tablet"), so appending it unconditionally read as
    // "Paracetamol 650mg Oral Tablet 650mg". Append only what is missing.
    const name = o.medication_name || 'Medication'
    const strength = (o.strength || '').trim()
    return strength && !name.toLowerCase().includes(strength.toLowerCase())
      ? `${name} ${strength}`
      : name
  }

  const regimen = (o: AwaitingReceiptOrder) => {
    // "REVIEW REQUIRED" is what the Relay handoff writes when Sage extracted a
    // drug but no reliable dose (services/sage_handoff_service). Show it as the
    // instruction it is, not as if it were a dose.
    const parts = [o.dosage, o.route, o.frequency].filter(Boolean) as string[]
    return parts.length ? parts.join(' · ') : 'No dose recorded'
  }

  return (
    <div className="nurse-deliveries">
      {flash && <div className="nurse-note nurse-note--ok" role="status">{flash}</div>}
      {error && <div className="nurse-note nurse-note--danger" role="alert">{error}</div>}

      {failed && (
        <div className="orb-state orb-state--error" role="alert">
          <div className="orb-state__content">
            <p className="orb-state__title">Could not load deliveries.</p>
            <p className="orb-state__detail">
              This is a display failure. It does not mean nothing is in transit, and
              medication may still be waiting to be signed for.
            </p>
            <button type="button" className="nurse-btn nurse-btn--secondary" onClick={() => void load()}>
              Retry
            </button>
          </div>
        </div>
      )}

      {!failed && orders !== null && orders.length === 0 && (
        <div className="orb-state orb-state--empty orb-state--calm">
          <div className="orb-state__content">
            <p className="orb-state__title">Nothing in transit.</p>
            <p className="orb-state__detail">
              Pharmacy has no dispensed medication waiting to be signed for on this ward.
            </p>
            <p className="orb-state__reassure">
              Checked at <span className="orb-home__clock">{clockTime(checkedAt)}</span>.
            </p>
          </div>
        </div>
      )}

      {orders !== null && orders.length > 0 && (
        <ul className="orb-list nurse-deliveries__list">
          {orders.map(o => (
            <li key={o.id} className="orb-row nurse-delivery">
              <div className="nurse-delivery__main">
                {/* The drug in the tray, which is the substitute when pharmacy
                    swapped it. The prescribed drug is named beside it so the
                    nurse knows the swap was deliberate, not a picking error. */}
                <span className="nurse-delivery__drug">
                  {describe(o)}
                  {o.original_medication_name ? ` (substituted for ${o.original_medication_name})` : ''}
                </span>
                <span className="nurse-delivery__regimen">{regimen(o)}</span>
                {o.original_medication_name && o.pharmacist_notes && (
                  <span className="nurse-delivery__regimen">Pharmacist note: {o.pharmacist_notes}</span>
                )}
                {/* The medication order this tray belongs to (medorders-10),
                    named by its status in words, and what to do when it is no
                    longer live. Only a tray linked to an order carries one. */}
                {o.linked_order && (
                  <span className="nurse-delivery__regimen">
                    For the medication order: {medOrderChip(o.linked_order).label.toLowerCase()}
                  </span>
                )}
                {o.linked_order && orderWarning(o.linked_order) && (
                  <span className="ward-stock__problem" role="note">
                    {orderWarning(o.linked_order)}
                  </span>
                )}
                <span className="nurse-delivery__patient">
                  {[o.patient_name, o.bed && `Bed ${o.bed}`, o.ward].filter(Boolean).join(' · ')}
                </span>
              </div>
              <div className="nurse-delivery__side">
                {/* WHO SENT IT, named. The nurse is attesting against a specific
                    dispatch, and the separation of duties this step exists for
                    is only legible if she can see whose dispatch it is. */}
                <span className="nurse-delivery__from">
                  Dispensed by {o.dispensed_by_name || 'pharmacy'}
                  {o.dispensed_at ? <> at <span className="orb-home__clock">{clockTime(o.dispensed_at)}</span></> : null}
                </span>
                <button
                  type="button"
                  className="nurse-btn nurse-btn--primary"
                  onClick={() => void receive(o)}
                  disabled={busyId === o.id}
                >
                  {busyId === o.id ? 'Recording…' : 'Confirm receipt'}
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}

      <p className="nurse-footnote">
        Confirming receipt records your name and the time against this order. Pharmacy
        cannot record receipt of its own dispatch, so this is the only place the chain
        closes.
      </p>

      {/* The ward's own stock store (grn-21). Renders nothing unless the
          pharmacy store and ward stock are both switched on and this account
          holds ward_stock.read, so with either switch off this tab is
          unchanged. */}
      <WardStockSection />

      {/* Medicines going back to the pharmacy (grn-15). Renders nothing
          unless the pharmacy store is switched on and this account holds
          ward_return.record. */}
      <WardReturnsSection />
    </div>
  )
}
