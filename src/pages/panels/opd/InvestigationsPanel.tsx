/**
 * Investigations on an outpatient visit (opd-11).
 *
 * The doctor finds a laboratory test or imaging in the code list (GET
 * /api/opd/terminology/search?kind=investigation), picks a priority and
 * orders it on THIS visit: POST /api/opd/visits/{visit_id}/orders, which the
 * server writes through orders_service with the visit on the order. Only the
 * code and the priority are sent; the order's words and type come from the
 * code list on the server. No medicine, procedure or bedside test is offered,
 * and order sets are not offered for outpatients.
 *
 * The list shows each test's state and its result once back; an order not
 * yet taken in by the laboratory can be cancelled with a reason. The
 * investigation slip prints the tests with the ordering doctor's name and
 * registration number.
 */
import { useCallback, useEffect, useId, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

import {
  cancelInvestigation,
  errorCode,
  fetchVisitOrders,
  fetchVisitPrint,
  orderInvestigation,
  searchTerminology,
  type OpdOrder,
  type OrderPriority,
  type TerminologyHit,
  type VisitPrint,
} from '../../../api/opd'
import OpdConsultationPrint from '../../../components/print/OpdConsultationPrint'
import { formatClinicalDateTime } from '../../../lib/datetime'
import './ConsultationEditor.css'

interface InvestigationsPanelProps {
  visitId: string
  /** False on a visit that takes no new orders (finished, cancelled, the
   *  patient admitted): the list still shows, nothing new is offered. */
  canOrder: boolean
}

const SEARCH_DEBOUNCE_MS = 250

const STATUS_WORD: Record<string, string> = {
  draft: 'Not sent',
  active: 'Ordered',
  in_progress: 'In progress',
  completed: 'Done',
  resulted: 'Result back',
  cancelled: 'Cancelled',
}
const PRIORITY_WORD: Record<string, string> = { routine: 'Routine', urgent: 'Urgent', stat: 'STAT' }
const TYPE_WORD: Record<string, string> = { lab: 'Laboratory', imaging: 'Imaging' }

function messageOf(err: unknown, fallback: string): string {
  const detail = (err as { detail?: { message?: unknown } } | null)?.detail
  if (detail && typeof detail.message === 'string') return detail.message
  if (err instanceof Error && err.message) return err.message
  return fallback
}

function CancelForm({ order, onCancelled, onClose, visitId }: {
  order: OpdOrder
  visitId: string
  onCancelled: (o: OpdOrder) => void
  onClose: () => void
}) {
  const id = useId()
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const submit = async () => {
    if (busy || reason.trim().length < 3) return
    setBusy(true)
    setError('')
    try {
      onCancelled(await cancelInvestigation(visitId, order.id, reason.trim()))
    } catch (err) {
      setError(messageOf(err, 'The order was not cancelled.'))
      setBusy(false)
    }
  }
  return (
    <div className="opd-inv__cancel">
      <div className="opd-field">
        <label htmlFor={`${id}-reason`}>Why cancel {order.description}?</label>
        <input id={`${id}-reason`} type="text" value={reason} maxLength={500} disabled={busy}
          onChange={e => setReason(e.target.value)} />
      </div>
      {error && <p className="opd-error" role="alert">{error}</p>}
      <div className="opd-row__actions">
        <button type="button" className="opd-btn opd-btn--quiet" onClick={onClose} disabled={busy}>
          Keep the order
        </button>
        <button type="button" className="opd-btn" disabled={busy || reason.trim().length < 3}
          onClick={() => { void submit() }}>
          {busy ? 'Cancelling…' : 'Cancel the order'}
        </button>
      </div>
    </div>
  )
}

export default function InvestigationsPanel({ visitId, canOrder }: InvestigationsPanelProps) {
  const id = useId()
  const [orders, setOrders] = useState<OpdOrder[] | null>(null)
  const [loadFailed, setLoadFailed] = useState(false)
  const [query, setQuery] = useState('')
  const [answer, setAnswer] = useState<{ q: string; results: TerminologyHit[]; failed: boolean } | null>(null)
  const [priority, setPriority] = useState<OrderPriority>('routine')
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [cancelling, setCancelling] = useState<string | null>(null)
  const [print, setPrint] = useState<VisitPrint | null>(null)
  const seq = useRef(0)
  const alive = useRef(true)
  const typed = query.trim()
  const current = answer && answer.q === typed ? answer : null

  useEffect(() => {
    alive.current = true
    return () => { alive.current = false }
  }, [])

  const load = useCallback(async () => {
    try {
      const found = await fetchVisitOrders(visitId)
      if (!alive.current) return
      setOrders(Array.isArray(found?.orders) ? found.orders : [])
      setLoadFailed(false)
    } catch {
      if (!alive.current) return
      setLoadFailed(true)
    }
  }, [visitId])

  // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch trigger: the setState is the loader's own result, not state derived from props.
  useEffect(() => { void load() }, [load])

  useEffect(() => {
    const mine = ++seq.current
    if (typed.length < 2) return undefined
    const timer = setTimeout(() => {
      searchTerminology(typed, 'investigation')
        .then(found => {
          if (mine !== seq.current) return
          setAnswer({ q: typed, results: Array.isArray(found?.results) ? found.results : [], failed: false })
        })
        .catch(() => {
          if (mine !== seq.current) return
          setAnswer({ q: typed, results: [], failed: true })
        })
    }, SEARCH_DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [typed])

  const order = async (hit: TerminologyHit) => {
    if (busy) return
    setBusy(hit.code)
    setError('')
    setNotice('')
    try {
      const created = await orderInvestigation(visitId, hit, priority)
      if (!alive.current) return
      setOrders(prev => [...(prev ?? []), created])
      setNotice(`${created.description} ordered.`)
      setQuery('')
      setPriority('routine')
    } catch (err) {
      if (!alive.current) return
      setError(errorCode(err) === 'already_ordered'
        ? 'This test is already ordered on this visit.'
        : messageOf(err, 'The test was not ordered.'))
    } finally {
      if (alive.current) setBusy('')
    }
  }

  const printSlip = async () => {
    setError('')
    try {
      const data = await fetchVisitPrint(visitId, 'investigations')
      if (alive.current) setPrint(data)
    } catch (err) {
      if (alive.current) setError(messageOf(err, 'The slip could not be printed.'))
    }
  }

  const live = (orders ?? []).filter(o => o.status !== 'cancelled')

  return (
    <section className="opd-block opd-inv" aria-label="Investigations on this visit">
      <h4 className="opd-subhead">Investigations on this visit</h4>
      {orders === null && !loadFailed && <p className="opd-empty">Loading the investigations…</p>}
      {loadFailed && (
        <div className="opd-row__actions">
          <p className="opd-hint">The investigations on this visit could not be loaded.</p>
          <button type="button" className="opd-btn opd-btn--quiet" onClick={() => { void load() }}>
            Try again
          </button>
        </div>
      )}
      {orders !== null && orders.length === 0 && <p className="opd-empty">None ordered.</p>}
      {orders !== null && orders.length > 0 && (
        <ul className="opd-inv__list">
          {orders.map(o => (
            <li key={o.id} className="opd-inv__item">
              <div className="opd-inv__what">
                <span className={o.status === 'cancelled' ? 'opd-inv__name opd-inv__name--cancelled' : 'opd-inv__name'}>
                  {o.description}
                </span>
                <span className="opd-hint">
                  {[TYPE_WORD[o.order_type] ?? o.order_type, PRIORITY_WORD[o.priority] ?? o.priority,
                    STATUS_WORD[o.status] ?? o.status,
                    o.created_at ? formatClinicalDateTime(o.created_at) : null].filter(Boolean).join(' · ')}
                </span>
                {o.result_text && <span className="opd-line">Result: {o.result_text}</span>}
              </div>
              {canOrder && o.status === 'active' && cancelling !== o.id && (
                <button type="button" className="opd-btn opd-btn--quiet"
                  aria-label={`Cancel ${o.description}`} onClick={() => setCancelling(o.id)}>
                  Cancel
                </button>
              )}
              {cancelling === o.id && (
                <CancelForm
                  order={o}
                  visitId={visitId}
                  onClose={() => setCancelling(null)}
                  onCancelled={moved => {
                    setCancelling(null)
                    setOrders(prev => (prev ?? []).map(x => (x.id === moved.id ? { ...x, ...moved } : x)))
                    setNotice(`${moved.description} cancelled.`)
                  }}
                />
              )}
            </li>
          ))}
        </ul>
      )}

      {canOrder && (
        <div className="opd-inv__find">
          <div className="opd-field-row">
            <div className="opd-field">
              <label htmlFor={`${id}-find`}>Order a test</label>
              <input id={`${id}-find`} type="search" value={query} maxLength={120} autoComplete="off"
                placeholder="Laboratory test or imaging" onChange={e => setQuery(e.target.value)} />
            </div>
            <div className="opd-field">
              <label htmlFor={`${id}-priority`}>Priority</label>
              <select id={`${id}-priority`} value={priority}
                onChange={e => setPriority(e.target.value as OrderPriority)}>
                <option value="routine">Routine</option>
                <option value="urgent">Urgent</option>
                <option value="stat">STAT</option>
              </select>
            </div>
          </div>
          {current && current.results.length > 0 && (
            <ul className="opd-dx__results" aria-label="Tests that match">
              {current.results.map(r => (
                <li key={`${r.code_system}-${r.code}`}>
                  <button type="button" className="opd-dx__result" disabled={busy !== ''}
                    onClick={() => { void order(r) }}
                    aria-label={`Order ${r.display}`}>
                    <span className="opd-dx__result-text">{r.display}</span>
                    <span className="opd-dx__result-code">
                      {TYPE_WORD[r.order_type ?? ''] ?? r.order_type} · {r.code_system} {r.code}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
          {current && !current.failed && current.results.length === 0 && (
            <p className="opd-hint">No laboratory test or imaging matches.</p>
          )}
          {current?.failed && <p className="opd-hint">The test list could not be searched. Try again.</p>}
          <p className="opd-hint">
            Laboratory tests and imaging only. Medicines are not ordered here.
          </p>
        </div>
      )}

      {error && <p className="opd-error" role="alert">{error}</p>}
      {notice && <p className="opd-hint">{notice}</p>}

      {live.length > 0 && (
        <div className="opd-row__actions">
          <button type="button" className="opd-btn" onClick={() => { void printSlip() }}>
            Print investigation slip
          </button>
        </div>
      )}

      {print && createPortal(
        <OpdConsultationPrint print={print} onAfterPrint={() => setPrint(null)} />,
        document.body,
      )}
    </section>
  )
}
