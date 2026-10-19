import { useCallback, useEffect, useState } from 'react'
import type { AdministrationRecord } from '../../api/client'
import {
  fetchMedOrderEvents,
  ledgerAdministration,
  medOrderEventActorWords,
  medOrderEventDetailWords,
  medOrderEventWords,
  type MedOrderEvent,
} from '../../api/medorders'
import { formatClinicalDateTime } from '../../lib/datetime'
import AcrossAllergyMark from './AcrossAllergyMark'
import './MedOrderList.css'

/* ═══════════════════════════════════════════════════════════════════════════
   ONE MEDICATION ORDER'S HISTORY (medorders-10, W9-D; design gap W120)

   The order's own ledger, oldest first: written, signed, checked by a
   pharmacist, put on hold, resumed, supplied, received, each dose given or
   not given, stopped. Read from GET /api/medorders/orders/{id}/events, which
   is department-scoped and audited on the server (an order on another ward is
   a 404, the same answer as one that does not exist).

   Who did each step is said by role ("by a nurse"): the ledger records the
   person's id and role, and the chart never prints a login id. The reason is
   the one written at the time.

   A 'Dose given' step whose administration record was given across a
   charted allergy carries the 'Across charted allergy' chip and the written
   reason (AcrossAllergyMark): the event's ref_id is the record's id, looked
   up in the chart's own administrations.

   The rule every Orb list follows: a history that could not be read is never
   drawn as an empty one. "Nothing has happened to this order" and "we could
   not find out what happened" are opposite facts.
   ═══════════════════════════════════════════════════════════════════════════ */

interface Props {
  orderId: string
  /** The order's version on screen: a change reloads the history. */
  version?: number
  /** Heading for the region, for screen readers. */
  drugName?: string
  /** The patient's administrations (the chart already reads them): each
   *  'Dose given' step is joined to its record to mark an allergy override. */
  administrations?: AdministrationRecord[]
}

export default function MedOrderTimeline({ orderId, version, drugName, administrations }: Props) {
  const [events, setEvents] = useState<MedOrderEvent[] | null>(null)
  const [failed, setFailed] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const rows = await fetchMedOrderEvents(orderId)
      setEvents(rows)
      setFailed(null)
    } catch (err) {
      // `events` is left as it was: a failed refresh must not paint an empty history.
      setFailed(err instanceof Error && err.message ? err.message : 'The history could not be read.')
    }
  }, [orderId])

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch trigger, not derived state.
    void load()
  }, [load, version])

  return (
    <section className="medorder-timeline" aria-label={drugName ? `History of ${drugName}` : 'Order history'}>
      {failed && (
        <div className="medorder-timeline__failed" role="alert">
          <p>
            Could not load this order&apos;s history. {failed} This is a display failure, not an
            empty history.
          </p>
          <button type="button" className="medorder-btn medorder-btn--quiet" onClick={() => void load()}>
            Retry
          </button>
        </div>
      )}
      {events === null && !failed && (
        <p className="medorder-timeline__note" role="status">Loading the order&apos;s history</p>
      )}
      {events !== null && events.length === 0 && (
        <p className="medorder-timeline__note">Nothing is recorded on this order yet.</p>
      )}
      {events !== null && events.length > 0 && (
        <ol className="medorder-timeline__list">
          {events.map(event => {
            const detail = medOrderEventDetailWords(event)
            const record = event.kind === 'administered'
              ? ledgerAdministration(administrations, event) : undefined
            return (
              <li key={event.id} className="medorder-timeline__step">
                <span className="medorder-timeline__what">{medOrderEventWords(event.kind)}</span>
                <span className="medorder-timeline__who">
                  by {medOrderEventActorWords(event)}
                  {event.created_at ? <>, {formatClinicalDateTime(event.created_at)}</> : null}
                </span>
                {event.reason && (
                  <span className="medorder-timeline__why">Reason: {event.reason}</span>
                )}
                {detail && <span className="medorder-timeline__why">{detail}</span>}
                <AcrossAllergyMark record={record} />
              </li>
            )
          })}
        </ol>
      )}
    </section>
  )
}
