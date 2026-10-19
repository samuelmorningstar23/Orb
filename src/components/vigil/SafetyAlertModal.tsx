import { useCallback, useState } from 'react'
import { createPortal } from 'react-dom'
import { useFocusTrap } from '../../hooks/useFocusTrap'
import type { DoseCheckFinding, MedicationSafety, SafetyAlert } from '../../api/client'
import { hasPermission, MEDICATION_PRESCRIBE } from '../../lib/roles'
import './SafetyAlertModal.css'

/**
 * Drug-safety alert modal — the UI half of the order-time safety-ack protocol.
 *
 * Two modes:
 *   - 'blocking': the backend answered 428 (severe alerts). The clinician must
 *     supply a clinical justification (>= 10 chars) before "Acknowledge &
 *     Order" re-submits with the safety_ack; the override is audited
 *     server-side on the tamper-evident chain.
 *   - 'info': the order already succeeded but raised sub-severe alerts (or the
 *     drug was outside the interaction knowledge base). Review-only, and it
 *     says so FIRST (`placedNotice`): a prescriber reading an allergy warning
 *     must not take it for a warning about an order that does not exist yet.
 *
 * The dose guard's verdict rides on `safety.dose_check` from every lane that
 * knew the dose. A dose block clears only for a prescriber; anyone else is
 * told who can, with no reason box to type into.
 *
 * A severe ALLERGY alert is the same: the server refuses anyone without
 * medication.prescribe, so the screen offers them no reason box either.
 *
 * The same medicine already ordered rides on `safety.duplicate_check` when the
 * server's duplicate rule is 'acknowledge' (ruling MO-R6, medication-order
 * lifecycle on): listed in its own group, cleared with a written reason.
 */

/** The server's same-ingredient finding (services/medorders/screening.py). */
interface DuplicateCheck {
  decision?: string
  message?: string
  findings?: { description?: string; drug_name?: string; message?: string }[]
}

function duplicateFindings(safety: MedicationSafety): string[] {
  const check = (safety as MedicationSafety & { duplicate_check?: DuplicateCheck | null })
    .duplicate_check
  return (check?.findings ?? [])
    .map(f => f?.description || f?.drug_name || '')
    .filter(text => text.trim().length > 0)
}

function DuplicateGroup({ items }: { items: string[] }) {
  if (!items.length) return null
  return (
    <div className="safety-modal__group">
      <h3 className="safety-modal__group-title">Already ordered</h3>
      <ul className="safety-modal__list">
        {items.map((text, i) => (
          <li key={i} className="safety-modal__alert">
            <span className="safety-chip safety-chip--warn">Same medicine</span>
            <span className="safety-modal__alert-message">{text}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}
interface Props {
  safety: MedicationSafety
  mode: 'blocking' | 'info'
  /** Drug being ordered; falls back to safety.drug from the screen payload. */
  drugName?: string
  /** True while the acknowledged re-submission is in flight. */
  busy?: boolean
  onAcknowledge?: (reason: string) => void
  onClose: () => void
  /** Info mode: what already happened to the order, said before any alert. */
  placedNotice?: string
  /** Info mode: undo the order just placed. Offered only when the caller has a
   *  way to cancel it. */
  onCancelOrder?: () => void
  /** True while the cancel is in flight. */
  cancelBusy?: boolean
  /** Why the cancel failed, in the server's words. */
  cancelError?: string | null
}

const MIN_REASON_LENGTH = 10

function severityClass(severity: string): string {
  if (severity === 'severe' || severity === 'contraindicated') return 'safety-chip--danger'
  if (severity === 'mild' || severity === 'minor' || severity === 'low') return 'safety-chip--ok'
  // moderate / unknown / anything unmapped reads as caution, never as "fine".
  return 'safety-chip--warn'
}

function AlertGroup({ title, alerts }: { title: string; alerts: SafetyAlert[] }) {
  if (!alerts.length) return null
  return (
    <div className="safety-modal__group">
      <h3 className="safety-modal__group-title">{title}</h3>
      <ul className="safety-modal__list">
        {alerts.map((a, i) => (
          <li key={i} className="safety-modal__alert">
            <span className={`safety-chip ${severityClass(a.severity)}`}>{a.severity}</span>
            <span className="safety-modal__alert-message">{a.message}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

/** The dose guard's findings, refusals first, in its own words. */
function DoseGroup({ findings }: { findings: DoseCheckFinding[] }) {
  const shown = [
    ...findings.filter(f => f?.decision === 'block'),
    ...findings.filter(f => f?.decision !== 'block'),
  ].filter(f => f?.message?.trim())
  if (!shown.length) return null
  return (
    <div className="safety-modal__group">
      <h3 className="safety-modal__group-title">Dose check</h3>
      <ul className="safety-modal__list">
        {shown.map((f, i) => (
          <li key={i} className="safety-modal__alert">
            <span className={`safety-chip ${f.decision === 'block' ? 'safety-chip--danger' : 'safety-chip--warn'}`}>
              {f.decision === 'block' ? 'Stopped' : 'Check dose'}
            </span>
            <span className="safety-modal__alert-message">{f.message}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

export default function SafetyAlertModal({
  safety, mode, drugName, busy, onAcknowledge, onClose,
  placedNotice, onCancelOrder, cancelBusy, cancelError,
}: Props) {
  const [reason, setReason] = useState('')
  const blocking = mode === 'blocking'
  const working = !!busy || !!cancelBusy
  const close = useCallback(() => {
    if (!working) onClose()
  }, [working, onClose])
  const modalRef = useFocusTrap<HTMLDivElement>(close)

  const allergyAlerts = safety.allergy_alerts || []
  const interactionAlerts = safety.interaction_alerts || []
  const doseCheck = safety.dose_check ?? null
  const doseFindings = doseCheck?.findings ?? []
  const doseBlocked = doseCheck?.decision === 'block'
  const severeScreen = safety.max_severity === 'severe'
  const duplicates = duplicateFindings(safety)
  // Only the same medicine already ordered stops this order.
  const duplicateOnly = blocking && duplicates.length > 0 && !doseBlocked && !severeScreen
  // A severe allergy alert clears only for a prescriber, as the bedside does.
  const allergyBlocked = blocking && allergyAlerts.some(a => a.severity === 'severe')
  // A dose block names the permission that clears it. Without it there is no
  // reason box: the next step is the prescriber, not a typed sentence.
  const clearPermission = doseBlocked ? doseCheck?.override_requires_permission ?? MEDICATION_PRESCRIBE
    : allergyBlocked ? MEDICATION_PRESCRIBE : null
  // A permission this screen does not know offers no box (fail closed).
  const mayAcknowledge = !clearPermission
    || (clearPermission === MEDICATION_PRESCRIBE && hasPermission(MEDICATION_PRESCRIBE))
  const drug = drugName || safety.drug
  const canAcknowledge = !busy && mayAcknowledge && reason.trim().length >= MIN_REASON_LENGTH

  return createPortal(
    <div className="safety-modal-backdrop" onClick={close}>
      <div
        className={`safety-modal glass-strong ${blocking ? 'safety-modal--blocking' : ''}`}
        ref={modalRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="safety-modal-title"
        onClick={e => e.stopPropagation()}
      >
        <div className="safety-modal__header">
          <h2 className="safety-modal__title" id="safety-modal-title">
            {!blocking ? 'Drug-safety review'
              : doseBlocked && !severeScreen ? 'The dose check stopped this order'
                : duplicateOnly ? 'This medicine is already ordered'
                  : 'Severe drug-safety alerts'}
          </h2>
          <button type="button" className="safety-modal__close" onClick={close} aria-label="Close" disabled={working}>
            &times;
          </button>
        </div>

        <div className="safety-modal__body">
          {/* First, before any alert: the order already exists. */}
          {!blocking && placedNotice && (
            <p className="safety-modal__coverage" role="status">{placedNotice}</p>
          )}

          {drug && (
            <p className="safety-modal__drug">
              Screening result for <strong>{drug}</strong>
            </p>
          )}

          {blocking && (
            <p className="safety-modal__lede" role="alert">
              This order is blocked until the alerts below are reviewed. Acknowledging records
              your identity and justification on the audit chain.
            </p>
          )}

          <DoseGroup findings={doseFindings} />
          {doseCheck?.unavailable && (
            <div className="safety-modal__coverage" role="note">
              <span className="safety-chip safety-chip--warn">Not checked</span>
              <span>The dose check could not run. This dose has NOT been checked.</span>
            </div>
          )}
          <DuplicateGroup items={duplicates} />
          <AlertGroup title="Allergy alerts" alerts={allergyAlerts} />
          <AlertGroup title="Interaction alerts" alerts={interactionAlerts} />
          {allergyAlerts.length === 0 && interactionAlerts.length === 0 && duplicates.length === 0 && (
            <p className="safety-modal__none">No allergy or interaction alerts were raised.</p>
          )}

          {safety.interaction_screened === false && (
            <div className="safety-modal__coverage" role="note">
              <span className="safety-chip safety-chip--warn">Not screened</span>
              <span>
                {safety.coverage_note ||
                  'This drug is outside the interaction knowledge base. Drug–drug interactions were NOT screened.'}
              </span>
            </div>
          )}

          {blocking && safety.ack_refused && (
            <p className="safety-modal__lede" role="alert">
              Not accepted: {safety.ack_refused}
            </p>
          )}

          {blocking && !mayAcknowledge ? (
            <>
              {allergyBlocked && (
                <p className="safety-modal__lede" role="alert">
                  Only a prescriber can override an allergy block.
                </p>
              )}
              {doseBlocked && (
                <p className="safety-modal__lede" role="alert">
                  Only a prescriber can authorise a dose the dose check stopped. Change the dose,
                  or ask the prescriber.
                </p>
              )}
              <div className="safety-modal__actions">
                <button type="button" className="safety-btn safety-btn--primary" onClick={close}>
                  {doseBlocked ? 'Change the dose' : 'Close'}
                </button>
              </div>
            </>
          ) : blocking ? (
            <>
              <label className="safety-modal__label" htmlFor="safety-ack-reason">
                Clinical justification
              </label>
              <textarea
                id="safety-ack-reason"
                className="safety-modal__reason"
                rows={3}
                placeholder={doseBlocked
                  ? `Why is this dose correct for this patient? (min ${MIN_REASON_LENGTH} characters)`
                  : duplicateOnly
                    ? `Why does this patient need a second order for the same medicine? (min ${MIN_REASON_LENGTH} characters)`
                    : `Why is it clinically appropriate to proceed despite these alerts? (min ${MIN_REASON_LENGTH} characters)`}
                value={reason}
                onChange={e => setReason(e.target.value)}
                disabled={busy}
                autoFocus
              />
              <div className="safety-modal__actions">
                <button type="button" className="safety-btn safety-btn--ghost" onClick={close} disabled={busy}>
                  Cancel
                </button>
                <button
                  type="button"
                  className="safety-btn safety-btn--danger"
                  onClick={() => onAcknowledge?.(reason.trim())}
                  disabled={!canAcknowledge}
                >
                  {busy ? 'Submitting…' : 'Acknowledge & Order'}
                </button>
              </div>
            </>
          ) : (
            <>
              {cancelError && (
                <p className="safety-modal__lede" role="alert">
                  The order was not cancelled: {cancelError}
                </p>
              )}
              <div className="safety-modal__actions">
                {onCancelOrder && (
                  <button
                    type="button"
                    className="safety-btn safety-btn--ghost"
                    onClick={onCancelOrder}
                    disabled={working}
                  >
                    {cancelBusy ? 'Cancelling…' : 'Cancel this order'}
                  </button>
                )}
                <button type="button" className="safety-btn safety-btn--primary" onClick={close} disabled={working}>
                  {onCancelOrder ? 'Keep the order' : 'Close'}
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>,
    document.body,
  )
}
