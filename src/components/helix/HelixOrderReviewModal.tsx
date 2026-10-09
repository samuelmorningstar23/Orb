import { Fragment, useCallback, useEffect, useId, useRef, useState, useSyncExternalStore } from 'react'
import styles from './Helix.module.css'
import {
  getHelixOrderSafety,
  isSafetyAckRequired,
  transitionHelixOrder,
  type HelixOrderSafety,
  type MedicationSafety,
  type SafetyAck,
  type SafetyAlert,
} from '../../api/client'
import {
  dispenseHelixOrder,
  getHelixDispenseOptions,
  getHelixOrderTimeline,
  type DispenseLot,
  type HelixDispenseOptions,
  type HelixOrderTimeline,
} from '../../api/pharmacy'
import {
  linkedOrderNotice,
  medOrderEventWords,
  MED_ORDER_STATUS_WORDS,
  type HelixLinkedOrder,
} from '../../api/medorders'
import { useFocusTrap } from '../../hooks/useFocusTrap'
import { formatClinicalDateKey, formatClinicalDateTime } from '../../lib/datetime'
import { currentFeatureFlags, currentModuleFlags, subscribeModuleFlags } from '../../shellConfig'
import { getStatusBadge } from './statusBadge'

interface HelixOrder {
  id: string
  patient_name: string
  medication_name: string
  dosage: string
  frequency: string
  route: string
  status: string
  prescribed_by_name: string
  created_at: string
  ward?: string
  bed?: string
  substitute_name?: string
  substitute_medication_id?: string
  pharmacist_notes?: string
  /** Set only by the ward, through POST /helix/orders/{id}/receive. */
  received_by?: string | null
  received_at?: string | null
  /** The medication order this supply row was requested for (medorders-5b),
   *  and its state as the list read it. Absent on an unlinked row. */
  med_order_id?: string | null
  linked_order?: HelixLinkedOrder | null
}

interface CatalogItem {
  id: string
  name: string
  generic_name: string
  form: string
  strength: string
  /** 'draft' (waiting for a second pharmacist), 'active' or 'retired'
   *  (grn-6). Only an active drug is offered as a substitute; the server
   *  refuses any other whatever is chosen. Absent reads as active. */
  identity_status?: string
}

// ─── Allowed next actions per state ───
function getNextActions(status: string): { target: string; label: string; btnClass: string }[] {
  switch (status) {
    case 'pending':
      return [{ target: 'reviewing', label: 'Begin Review', btnClass: 'btnPrimary' }]
    case 'reviewing':
      return [
        { target: 'verified', label: 'Verify', btnClass: 'btnPrimary' },
        { target: 'rejected', label: 'Reject', btnClass: 'btnDanger' },
        { target: 'substitute_suggested', label: 'Suggest Substitute', btnClass: 'btnSecondary' },
      ]
    case 'substitute_suggested':
      return [{ target: 'reviewing', label: 'Return to Review', btnClass: 'btnSecondary' }]
    case 'verified':
      return [
        { target: 'scheduled', label: 'Schedule', btnClass: 'btnPrimary' },
        { target: 'reviewing', label: 'Return to Review', btnClass: 'btnSecondary' },
        { target: 'rejected', label: 'Reject', btnClass: 'btnDanger' },
      ]
    case 'scheduled':
      // Dispensing decrements a real lot, FIFO by expiry, and writes a stock
      // movement. It was labelled 'Trigger Mock Dispense' until 2026-09-07,
      // which described a demonstration script rather than what the button does.
      // Return to Review and Reject are the way on from a stockout found here:
      // substitute from review, or tell the prescriber why not.
      return [
        { target: 'dispensed', label: 'Dispense', btnClass: 'btnPrimary' },
        { target: 'reviewing', label: 'Return to Review', btnClass: 'btnSecondary' },
        { target: 'rejected', label: 'Reject', btnClass: 'btnDanger' },
      ]
    case 'dispensed':
      // Nothing for pharmacy to do. The next step is the WARD signing for the
      // drug, on the nurse's Deliveries tab, under `helix.receive` — a
      // permission the pharmacist deliberately does not hold. A pharmacist
      // closing their own dispatch is the control this step exists to provide.
      return []
    default:
      return []
  }
}

// Statuses with a step still ahead that commits a drug to the patient
// (substitute, verify, dispense), so the screen is worth showing.
const SCREEN_AHEAD = new Set(['pending', 'reviewing', 'substitute_suggested', 'verified', 'scheduled'])

function severityTextColor(sev: string): string {
  if (sev === 'severe') return 'var(--status-danger-text)'
  if (sev === 'moderate') return 'var(--status-warn-text)'
  return 'var(--text-secondary)'
}

function AlertList({ alerts }: { alerts: SafetyAlert[] }) {
  return (
    <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'flex', flexDirection: 'column', gap: 6 }}>
      {alerts.map((a, i) => (
        <li
          key={i}
          style={{
            fontSize: '0.8125rem',
            lineHeight: 1.4,
            color: severityTextColor(a.severity),
            fontWeight: a.severity === 'severe' ? 700 : 500,
          }}
        >
          <span style={{ textTransform: 'capitalize' }}>{a.type || 'alert'}, {a.severity}:</span> {a.message}
        </li>
      ))}
    </ul>
  )
}

/** The drug-safety screen for the drug this order would dispense. Mirrors the
 *  Verify Queue's rule: a failed or partial screen never reads as all clear. */
function SafetyScreenView({ screen }: { screen: MedicationSafety & { screen_failed?: boolean } }) {
  const alerts = [...(screen.allergy_alerts || []), ...(screen.interaction_alerts || [])]
  const failed = !!screen.screen_failed
  const unscreened = !failed && screen.interaction_screened === false
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      {failed && (
        <div style={{ fontSize: '0.8125rem', fontWeight: 700, color: 'var(--status-danger-text)' }}>
          Screening unavailable
        </div>
      )}
      {unscreened && (
        <div style={{ fontSize: '0.8125rem', fontWeight: 700, color: 'var(--status-warn-text)' }}>
          Interactions not screened
        </div>
      )}
      {(failed || unscreened) && screen.coverage_note && (
        <div style={{ fontSize: '0.75rem', lineHeight: 1.4, color: 'var(--text-secondary)' }}>
          {screen.coverage_note}
        </div>
      )}
      {!failed && alerts.length > 0 && <AlertList alerts={alerts} />}
      {!failed && alerts.length === 0 && (
        unscreened
          ? <div style={{ fontSize: '0.8125rem', color: 'var(--text-secondary)' }}>No allergy alerts.</div>
          : <div style={{ fontSize: '0.8125rem', color: 'var(--status-ok-text)' }}>No allergy or interaction alerts.</div>
      )}
    </div>
  )
}

// ─── The medication order behind a linked supply row (medorders-5c) ───

/** A supply step in words, for the history. */
function supplyStepWords(fromStatus: string, toStatus: string): string {
  switch (toStatus) {
    case 'reviewing':
      return fromStatus === 'pending' ? 'Pharmacy began the review' : 'Returned to review'
    case 'verified': return 'Supply verified'
    case 'rejected': return 'Supply rejected'
    case 'substitute_suggested': return 'Substitute suggested'
    case 'scheduled': return 'Dispense scheduled'
    case 'dispensed': return 'Dispensed'
    case 'delivered': return 'Received on the ward'
    default: return `Moved to ${getStatusBadge(toStatus).label.toLowerCase()}`
  }
}

/** The order's ledger events that this supply's own steps already say. */
const SUPPLY_EVENT_KINDS = new Set([
  'supply_reviewing', 'supply_verified', 'supply_rejected', 'dispensed', 'received',
])

interface HistoryEntry { key: string; at: string; text: string; who: string | null }

/** One list, oldest first: this supply's steps and the order's own life (an
 *  earlier supply of the same order included). An order event that records
 *  one of this supply's steps is left out, because the step is listed. */
function historyEntries(timeline: HelixOrderTimeline): HistoryEntry[] {
  const entries: HistoryEntry[] = timeline.transitions.map(t => ({
    key: `t-${t.id}`,
    at: t.created_at,
    text: supplyStepWords(t.from_status, t.to_status),
    who: t.actor_name,
  }))
  for (const e of timeline.order_events) {
    if (SUPPLY_EVENT_KINDS.has(e.kind) && e.ref_type === 'helix_order' && e.ref_id === timeline.order_id) {
      continue
    }
    const words = medOrderEventWords(e.kind)
    entries.push({
      key: `e-${e.id}`,
      at: e.created_at,
      text: e.reason ? `${words}: ${e.reason}` : words,
      who: e.actor_name,
    })
  }
  const time = (v: string) => {
    const t = Date.parse(v)
    return Number.isNaN(t) ? 0 : t
  }
  return entries.sort((a, b) => time(a.at) - time(b.at))
}

function OrderHistory({ timeline, error }: { timeline: HelixOrderTimeline | null; error: string }) {
  if (error) {
    return <div role="alert" style={{ fontSize: '0.8125rem', fontWeight: 600, color: 'var(--status-danger-text)' }}>{error}</div>
  }
  if (!timeline) {
    return <div style={{ fontSize: '0.8125rem', color: 'var(--text-secondary)' }}>Loading the history…</div>
  }
  const entries = historyEntries(timeline)
  if (entries.length === 0) {
    return <div style={{ fontSize: '0.8125rem', color: 'var(--text-secondary)' }}>Nothing recorded yet.</div>
  }
  return (
    <ol className={styles.timeline} aria-label="Order history" style={{ listStyle: 'none', margin: 0, paddingLeft: 0 }}>
      {entries.map(entry => (
        <li key={entry.key} className={styles.timelineStep}>
          <span className={styles.timelineDot} aria-hidden="true" />
          <span className={styles.timelineContent}>
            <span className={styles.timelineLabel}>
              {entry.text}{entry.who ? `, by ${entry.who}` : ''}
            </span>
            <span className={styles.timelineTime}>{formatClinicalDateTime(entry.at)}</span>
          </span>
        </li>
      ))}
    </ol>
  )
}

function LinkedOrderSection({ order, lifecycleOn }: { order: HelixLinkedOrder; lifecycleOn: boolean }) {
  return (
    <div className={styles.modalSection}>
      <div className={styles.modalSectionTitle}>Medication order</div>
      <div className={styles.modalRow}>
        <span className={styles.modalLabel}>Order status</span>
        <span className={styles.modalValue}>{MED_ORDER_STATUS_WORDS[order.status] ?? order.status}</span>
      </div>
      <div role="status" style={{ fontSize: '0.8125rem', lineHeight: 1.4, color: 'var(--text-secondary)' }}>
        {linkedOrderNotice(order, lifecycleOn)}
      </div>
    </div>
  )
}

// ─── Dispense quantity and batch (grn-14) ───

/** The refusals the dispense step can give for a quantity or a batch. After
 *  any of them the batch list is read again: the shelf may have changed. */
const DISPENSE_REFUSALS = new Set([
  'bad_quantity', 'quantity_out_of_range', 'lot_not_found', 'lot_wrong_drug',
  'lot_not_dispensable', 'insufficient_lot_stock',
])

function unitsWords(n: number): string {
  return n === 1 ? '1 unit' : `${n} units`
}

function lotLabel(lot: DispenseLot): string {
  const parts = [`Batch ${lot.lot_number || 'not numbered'}`]
  const expiry = formatClinicalDateKey(lot.expiration_date)
  if (expiry) parts.push(`expires ${expiry}`)
  parts.push(unitsWords(lot.quantity))
  if (lot.store_name) parts.push(lot.store_name)
  return parts.join(', ')
}

/** Why the chosen quantity and batch cannot be sent, or '' when they can. */
function dispenseProblem(options: HelixDispenseOptions, quantity: string, lotId: string): string {
  const units = Number(quantity)
  if (!/^\d+$/.test(quantity.trim()) || units < 1 || units > options.max_units) {
    return options.max_units === 1
      ? 'This hospital dispenses 1 unit at a time.'
      : `Enter a whole number of units from 1 to ${options.max_units}.`
  }
  if (options.lots.length === 0) {
    return `No batch of ${options.drug_name} can be dispensed today.`
  }
  const lot = options.lots.find(l => l.id === lotId)
  if (lotId && !lot) return 'That batch is no longer on the list. Choose again.'
  if (lot && lot.quantity < units) {
    const name = lot.lot_number ? `Batch ${lot.lot_number}` : 'This batch'
    return `${name} holds ${unitsWords(lot.quantity)}. A dispense is never split between batches: choose another batch or fewer units.`
  }
  if (!lot && !options.lots.some(l => l.quantity >= units)) {
    return `No single batch holds ${unitsWords(units)}. Dispense fewer units.`
  }
  return ''
}

interface Props {
  order: HelixOrder
  catalog: CatalogItem[]
  onClose: () => void
  onTransition: () => void
}

// Statuses where the pharmacist can still write a note (a rejection needs one).
const NOTES_OPEN = new Set(['pending', 'reviewing', 'substitute_suggested', 'verified', 'scheduled'])

export default function HelixOrderReviewModal({ order: openedOrder, catalog, onClose, onTransition }: Props) {
  /** The order as the server last described it. Starts as the one the list
   *  opened, and is swapped for the current one when a step comes back 409
   *  because someone moved the order on in the meantime. */
  const [order, setOrder] = useState<HelixOrder>(openedOrder)
  const [stateNotice, setStateNotice] = useState('')
  // After a 409 the list behind the modal is stale too, so closing reloads it.
  const listStale = useRef(false)
  const close = useCallback(() => {
    if (listStale.current) onTransition()
    else onClose()
  }, [onClose, onTransition])
  const [notes, setNotes] = useState('')
  const [substituteId, setSubstituteId] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [screen, setScreen] = useState<HelixOrderSafety | null>(null)
  const [screenError, setScreenError] = useState('')
  /** Open when a transition came back 428: severe alerts need a reason. */
  const [ackFor, setAckFor] = useState<{ target: string; label: string; safety: MedicationSafety | null } | null>(null)
  const [ackReason, setAckReason] = useState('')

  const badge = getStatusBadge(order.status)
  const actions = getNextActions(order.status)
  const modalRef = useFocusTrap<HTMLDivElement>(close)
  const substituteHintId = useId()
  const reasonHintId = useId()
  const screenAhead = SCREEN_AHEAD.has(order.status)
  const previewId = order.status === 'reviewing' && substituteId ? substituteId : undefined

  // Re-screened whenever the pharmacist picks a different substitute, so the
  // alert is on screen before they propose it, not only as a refusal after.
  useEffect(() => {
    if (!screenAhead) return
    let cancelled = false
    getHelixOrderSafety(order.id, previewId)
      .then(r => {
        if (cancelled) return
        setScreen(r)
        setScreenError('')
      })
      .catch(() => {
        if (cancelled) return
        setScreen(null)
        setScreenError('The safety screen did not load. Do not assume this drug was checked.')
      })
    return () => { cancelled = true }
  }, [order.id, previewId, screenAhead])

  // ── The medication order behind a linked supply row (medorders-5c) ──
  // Only a row linked to an order reads its history: an unlinked row (every
  // row on a site that never used the order store) asks the server nothing new.
  const linked = Boolean(order.med_order_id || order.linked_order)
  const lifecycleOn = useSyncExternalStore(subscribeModuleFlags, currentFeatureFlags).med_order_lifecycle === true
  const [timeline, setTimeline] = useState<HelixOrderTimeline | null>(null)
  const [timelineError, setTimelineError] = useState('')
  const [timelineRead, setTimelineRead] = useState(0)
  useEffect(() => {
    if (!linked) return
    let cancelled = false
    getHelixOrderTimeline(order.id)
      .then(t => {
        if (cancelled) return
        setTimeline(t)
        setTimelineError('')
      })
      .catch(() => {
        if (cancelled) return
        setTimeline(null)
        setTimelineError('The order history did not load.')
      })
    return () => { cancelled = true }
  }, [order.id, linked, timelineRead])
  const linkedOrder = timeline?.linked_order ?? order.linked_order ?? null

  // ── Dispense quantity and batch (grn-14), with the pharmacy store on ──
  const storeOn = useSyncExternalStore(subscribeModuleFlags, currentModuleFlags)['pharmacy-store'] === true
  const choosingDispense = storeOn && order.status === 'scheduled'
  const [dispenseOptions, setDispenseOptions] = useState<HelixDispenseOptions | null>(null)
  const [dispenseOptionsError, setDispenseOptionsError] = useState('')
  const [optionsRead, setOptionsRead] = useState(0)
  const [quantity, setQuantity] = useState('1')
  const [lotId, setLotId] = useState('')
  const quantityId = useId()
  const lotSelectId = useId()
  const dispenseHintId = useId()
  useEffect(() => {
    if (!choosingDispense) return
    let cancelled = false
    getHelixDispenseOptions(order.id)
      .then(o => {
        if (cancelled) return
        setDispenseOptions(o)
        setDispenseOptionsError('')
        // A batch that left the list is not kept as the choice.
        setLotId(prev => (prev && !o.lots.some(l => l.id === prev) ? '' : prev))
      })
      .catch(() => {
        if (cancelled) return
        setDispenseOptions(null)
        setDispenseOptionsError('The batches did not load. Dispensing takes the earliest expiry, one unit.')
      })
    return () => { cancelled = true }
  }, [order.id, choosingDispense, optionsRead])
  const shapingDispense = choosingDispense && dispenseOptions?.enabled === true
  const dispenseBlocked = shapingDispense ? dispenseProblem(dispenseOptions!, quantity, lotId) : ''

  const handleTransition = async (targetStatus: string, label: string, safetyAck?: SafetyAck) => {
    setLoading(true)
    setError('')
    try {
      if (targetStatus === 'dispensed' && shapingDispense) {
        await dispenseHelixOrder(order.id, {
          dispense_quantity_units: Number(quantity),
          lot_id: lotId || null,
          ...(notes ? { pharmacist_notes: notes } : {}),
          ...(safetyAck ? { safety_ack: safetyAck } : {}),
        })
        setAckFor(null)
        onTransition()
        return
      }
      const payload: Parameters<typeof transitionHelixOrder>[1] = { target_status: targetStatus }
      if (notes) payload.pharmacist_notes = notes
      if (targetStatus === 'substitute_suggested' && substituteId) {
        payload.substitute_medication_id = substituteId
      }
      if (targetStatus === 'scheduled') {
        payload.scheduled_at = new Date().toISOString()
      }
      if (safetyAck) payload.safety_ack = safetyAck
      await transitionHelixOrder(order.id, payload)
      setAckFor(null)
      onTransition()
    } catch (e: unknown) {
      const err = e as Error & {
        status?: number
        detail?: { error?: string; message?: string; order?: Partial<HelixOrder> }
      }
      const code = err?.detail?.error
      if (isSafetyAckRequired(e)) {
        setAckFor({ target: targetStatus, label, safety: e.safety || null })
        setAckReason('')
      } else if (code && DISPENSE_REFUSALS.has(code)) {
        // The shelf refused the quantity or the batch. Say why in the
        // server's words and read the batches again.
        setError(err.message || 'The dispense was refused.')
        setOptionsRead(n => n + 1)
      } else if (linked && err?.status === 409 && code !== 'order_state_changed') {
        // The medication order behind this supply changed (a new version, or
        // an allergy or medicine charted since the screen loaded). The body's
        // `order` is the medication order, not this supply row, so it is not
        // merged in: the order is read again and the list reloads on close.
        setError(err.message || 'The medication order changed. Check it again.')
        setTimelineRead(n => n + 1)
        listStale.current = true
      } else if (err?.status === 409 && err.detail?.order) {
        // Someone else moved the order on. Show where it is now, with the steps
        // that are open from there, rather than a refusal the pharmacist can
        // do nothing with.
        setOrder(prev => ({ ...prev, ...err.detail!.order }))
        setStateNotice(err.detail.message || 'This order changed since you opened it. The current state is shown.')
        setSubstituteId('')
        setAckFor(null)
        listStale.current = true
      } else {
        setError(err?.message || 'Transition failed')
      }
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className={styles.modalOverlay} onClick={close}>
      <div className={styles.modalPanel} ref={modalRef} role="dialog" aria-modal="true" aria-labelledby="helix-order-review-title" onClick={e => e.stopPropagation()}>
        {/* Header */}
        <div className={styles.modalHeader}>
          <h3 className={styles.modalTitle} id="helix-order-review-title">Order Review</h3>
          <button className={styles.modalClose} onClick={close} aria-label="Close">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
            </svg>
          </button>
        </div>

        {/* Body */}
        <div className={styles.modalBody}>
          {stateNotice && (
            <div role="status" style={{ fontSize: '0.8125rem', fontWeight: 600, color: 'var(--status-warn-text)' }}>
              {stateNotice}
            </div>
          )}
          {/* Status */}
          <div className={styles.modalSection}>
            <div className={styles.modalSectionTitle}>Current Status</div>
            <span className={`${styles.badge} ${badge.className}`}>{badge.label}</span>
          </div>

          <div className={styles.modalDivider} />

          {/* Order Details */}
          <div className={styles.modalSection}>
            <div className={styles.modalSectionTitle}>Order Details</div>
            <div className={styles.modalRow}>
              <span className={styles.modalLabel}>Patient</span>
              <span className={styles.modalValue}>{order.patient_name}</span>
            </div>
            <div className={styles.modalRow}>
              <span className={styles.modalLabel}>Ward / Bed</span>
              <span className={styles.modalValue}>{order.ward || '—'} / {order.bed || '—'}</span>
            </div>
            <div className={styles.modalRow}>
              <span className={styles.modalLabel}>Medication</span>
              <span className={styles.modalValue}>{order.medication_name}</span>
            </div>
            <div className={styles.modalRow}>
              <span className={styles.modalLabel}>Dosage</span>
              <span className={styles.modalValue}>{order.dosage || '—'}</span>
            </div>
            <div className={styles.modalRow}>
              <span className={styles.modalLabel}>Frequency</span>
              <span className={styles.modalValue}>{order.frequency || '—'}</span>
            </div>
            <div className={styles.modalRow}>
              <span className={styles.modalLabel}>Route</span>
              <span className={styles.modalValue}>{order.route || '—'}</span>
            </div>
            <div className={styles.modalRow}>
              <span className={styles.modalLabel}>Prescriber</span>
              <span className={styles.modalValue}>{order.prescribed_by_name || 'System'}</span>
            </div>
            {order.substitute_name && (
              <div className={styles.modalRow}>
                <span className={styles.modalLabel}>Substitute</span>
                <span className={styles.modalValue} style={{ color: 'var(--status-info-text)' }}>{order.substitute_name}</span>
              </div>
            )}
            {order.pharmacist_notes && (
              <div className={styles.modalRow}>
                <span className={styles.modalLabel}>Notes</span>
                <span className={styles.modalValue} style={{ fontSize: '0.8125rem', color: 'var(--text-secondary)' }}>{order.pharmacist_notes}</span>
              </div>
            )}
          </div>

          {/* The medication order this supply was requested for, and its
              history: shown only for a supply row linked to one. */}
          {linked && (
            <>
              <div className={styles.modalDivider} />
              {linkedOrder && <LinkedOrderSection order={linkedOrder} lifecycleOn={lifecycleOn} />}
              <div className={styles.modalSection}>
                <div className={styles.modalSectionTitle}>Order history</div>
                <OrderHistory timeline={timeline} error={timelineError} />
              </div>
            </>
          )}

          {/* Drug safety: the allergy and interaction screen for the drug
              that would actually be dispensed, substitute included. */}
          {screenAhead && (
            <>
              <div className={styles.modalDivider} />
              <div className={styles.modalSection} aria-live="polite">
                <div className={styles.modalSectionTitle}>
                  Drug safety{screen ? `: ${screen.drug_name}${screen.is_substitute ? ' (substitute)' : ''}` : ''}
                </div>
                {screenError ? (
                  <div role="alert" style={{ fontSize: '0.8125rem', fontWeight: 600, color: 'var(--status-danger-text)' }}>
                    {screenError}
                  </div>
                ) : screen ? (
                  <SafetyScreenView screen={screen.safety} />
                ) : (
                  <div style={{ fontSize: '0.8125rem', color: 'var(--text-secondary)' }}>Screening…</div>
                )}
              </div>
            </>
          )}

          {/* Substitution chooser (only when reviewing) */}
          {order.status === 'reviewing' && (
            <>
              <div className={styles.modalDivider} />
              <div className={styles.modalSection}>
                <div className={styles.modalSectionTitle}>Substitute Medication (optional)</div>
                <select
                  className={styles.selectInput}
                  value={substituteId}
                  onChange={e => setSubstituteId(e.target.value)}
                >
                  <option value="">(Select substitute)</option>
                  {catalog.filter(c => c.name !== order.medication_name
                    && (c.identity_status ?? 'active') === 'active').map(c => (
                    <option key={c.id} value={c.id}>{c.name} ({c.strength})</option>
                  ))}
                </select>
              </div>
            </>
          )}

          {/* Dispense quantity and batch (grn-14): only with the pharmacy
              store on, and only when the server says this site may choose. */}
          {choosingDispense && (dispenseOptionsError || shapingDispense) && (
            <>
              <div className={styles.modalDivider} />
              <div className={styles.modalSection}>
                <div className={styles.modalSectionTitle}>Dispense</div>
                {dispenseOptionsError ? (
                  <div role="alert" style={{ fontSize: '0.8125rem', fontWeight: 600, color: 'var(--status-warn-text)' }}>
                    {dispenseOptionsError}
                  </div>
                ) : dispenseOptions && (
                  <>
                    <label htmlFor={quantityId} style={{ fontSize: '0.8125rem', color: 'var(--text-secondary)' }}>
                      Units to dispense (at most {dispenseOptions.max_units})
                    </label>
                    <input
                      id={quantityId}
                      className={styles.selectInput}
                      type="number"
                      inputMode="numeric"
                      min={1}
                      max={dispenseOptions.max_units}
                      step={1}
                      value={quantity}
                      onChange={e => setQuantity(e.target.value)}
                      aria-describedby={dispenseBlocked ? dispenseHintId : undefined}
                    />
                    <label htmlFor={lotSelectId} style={{ fontSize: '0.8125rem', color: 'var(--text-secondary)' }}>
                      Batch of {dispenseOptions.drug_name}
                    </label>
                    <select
                      id={lotSelectId}
                      className={styles.selectInput}
                      value={lotId}
                      onChange={e => setLotId(e.target.value)}
                      aria-describedby={dispenseBlocked ? dispenseHintId : undefined}
                    >
                      <option value="">Earliest expiry first</option>
                      {dispenseOptions.lots.map(lot => (
                        <option key={lot.id} value={lot.id}>{lotLabel(lot)}</option>
                      ))}
                    </select>
                    {dispenseBlocked && (
                      <div id={dispenseHintId} style={{ fontSize: '0.75rem', lineHeight: 1.4, color: 'var(--status-warn-text)' }}>
                        {dispenseBlocked}
                      </div>
                    )}
                  </>
                )}
              </div>
            </>
          )}

          {/* Notes input (for review/reject/substitute) */}
          {NOTES_OPEN.has(order.status) && (
            <>
              <div className={styles.modalDivider} />
              <div className={styles.modalSection}>
                <div className={styles.modalSectionTitle} id="helix-pharmacist-notes-title">Pharmacist Notes</div>
                <textarea
                  aria-labelledby="helix-pharmacist-notes-title"
                  className={styles.textarea}
                  value={notes}
                  onChange={e => setNotes(e.target.value)}
                  placeholder="Verification notes, the reason for rejecting, or why a substitute (a rejection needs a reason; the prescriber is told)"
                />
              </div>
            </>
          )}

          {/* Severe alert: proceeding needs a reason, and the override is
              audited server-side against this pharmacist. */}
          {ackFor && (
            <>
              <div className={styles.modalDivider} />
              <div className={styles.modalSection} role="alert">
                <div className={styles.modalSectionTitle} style={{ color: 'var(--status-danger-text)' }}>
                  Severe safety alert
                </div>
                {ackFor.safety
                  ? <SafetyScreenView screen={ackFor.safety} />
                  : <div style={{ fontSize: '0.8125rem', color: 'var(--status-danger-text)' }}>Severe drug-safety alert.</div>}
                {/* The server refused the last reason (a severe allergy clears
                    only for a prescriber). Say so, or the box just comes back
                    empty and reads as if nothing was sent. */}
                {ackFor.safety?.ack_refused && (
                  <div style={{ fontSize: '0.8125rem', fontWeight: 600, color: 'var(--status-danger-text)' }}>
                    Not accepted: {ackFor.safety.ack_refused}
                  </div>
                )}
                {ackFor.safety?.override_requires_permission && !ackFor.safety?.ack_refused && (
                  <div style={{ fontSize: '0.8125rem', color: 'var(--text-secondary)' }}>
                    Only a prescriber can override an allergy block. A reason here is accepted only
                    when the prescriber has already overridden this allergy for this patient.
                  </div>
                )}
                <label htmlFor="helix-ack-reason" style={{ fontSize: '0.8125rem', color: 'var(--text-secondary)' }}>
                  Reason for proceeding (recorded with your name)
                </label>
                <textarea
                  id="helix-ack-reason"
                  className={styles.textarea}
                  value={ackReason}
                  onChange={e => setAckReason(e.target.value)}
                  placeholder="For example: allergy reviewed with the prescriber, test dose tolerated"
                />
                <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                  <button
                    type="button"
                    className={styles.btnDanger}
                    disabled={loading || !ackReason.trim()}
                    style={loading || !ackReason.trim() ? {
                      background: 'var(--btn-disabled-bg)',
                      color: 'var(--btn-disabled-text)',
                      borderColor: 'var(--btn-disabled-border)',
                      cursor: 'not-allowed',
                    } : undefined}
                    onClick={() => handleTransition(ackFor.target, ackFor.label, { acknowledged: true, reason: ackReason.trim() })}
                  >
                    Override and {ackFor.label.toLowerCase()}
                  </button>
                  <button type="button" className={styles.btnSecondary} onClick={() => setAckFor(null)} disabled={loading}>
                    Cancel
                  </button>
                </div>
              </div>
            </>
          )}

          {error && (
            <div role="alert" style={{ color: 'var(--status-danger-text)', fontSize: '0.8125rem', padding: '8px 0' }}>{error}</div>
          )}
        </div>

        {/* Awaiting the ward. Said plainly, because an order with no buttons and
            no explanation reads as a broken screen rather than as a step that
            belongs to somebody else. */}
        {order.status === 'dispensed' && (
          <div className={styles.awaitingReceipt} role="status">
            {order.received_by
              ? 'Received on the ward.'
              : 'In transit. The ward records receipt when the drug arrives; pharmacy cannot sign for its own dispatch.'}
          </div>
        )}

        {/* Actions */}
        {actions.length > 0 && !ackFor && (
          <div className={styles.modalActions}>
            {actions.map(action => {
              // A disabled button with no reason reads as broken, so the one
              // precondition the pharmacist can fix is said next to it.
              const needsSubstitute = action.target === 'substitute_suggested' && !substituteId
              // The server refuses a rejection with no reason (the prescriber
              // is told why), so the button says so before it is pressed.
              const needsReason = action.target === 'rejected' && !notes.trim()
              // A quantity or batch the shelf cannot meet (grn-14): the reason
              // is already said under the batch picker.
              const needsDispenseFix = action.target === 'dispensed' && !!dispenseBlocked
              const hintId = needsSubstitute ? substituteHintId : needsReason ? reasonHintId
                : needsDispenseFix ? dispenseHintId : undefined
              return (
                <Fragment key={action.target}>
                  <button
                    className={styles[action.btnClass] || styles.btnSecondary}
                    onClick={() => handleTransition(action.target, action.label)}
                    disabled={loading || needsSubstitute || needsReason || needsDispenseFix}
                    aria-describedby={hintId}
                  >
                    {loading ? 'Processing...' : action.label}
                  </button>
                  {(needsSubstitute || needsReason) && (
                    <span
                      id={hintId}
                      style={{ alignSelf: 'center', fontSize: '0.75rem', color: 'var(--text-secondary)' }}
                    >
                      {needsSubstitute ? 'Choose a substitute first' : 'Write the reason in the notes first'}
                    </span>
                  )}
                </Fragment>
              )
            })}
          </div>
        )}
      </div>
    </div>
  )
}
