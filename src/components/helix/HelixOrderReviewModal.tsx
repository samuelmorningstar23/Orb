import { Fragment, useCallback, useEffect, useId, useRef, useState } from 'react'
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
import { useFocusTrap } from '../../hooks/useFocusTrap'
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
}

interface CatalogItem {
  id: string
  name: string
  generic_name: string
  form: string
  strength: string
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

  const handleTransition = async (targetStatus: string, label: string, safetyAck?: SafetyAck) => {
    setLoading(true)
    setError('')
    try {
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
      const err = e as Error & { status?: number; detail?: { message?: string; order?: Partial<HelixOrder> } }
      if (isSafetyAckRequired(e)) {
        setAckFor({ target: targetStatus, label, safety: e.safety || null })
        setAckReason('')
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
                  {catalog.filter(c => c.name !== order.medication_name).map(c => (
                    <option key={c.id} value={c.id}>{c.name} ({c.strength})</option>
                  ))}
                </select>
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
              const hintId = needsSubstitute ? substituteHintId : needsReason ? reasonHintId : undefined
              return (
                <Fragment key={action.target}>
                  <button
                    className={styles[action.btnClass] || styles.btnSecondary}
                    onClick={() => handleTransition(action.target, action.label)}
                    disabled={loading || needsSubstitute || needsReason}
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
