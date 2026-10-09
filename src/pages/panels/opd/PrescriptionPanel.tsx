/**
 * The outpatient prescription on a visit (opd-12).
 *
 * Each line is a medication order the consulting doctor writes and signs in
 * one step: POST /api/opd/visits/{visit_id}/prescriptions. It is for the
 * patient to take home: it never reaches the ward drug chart or the eMAR, and
 * the pharmacy dispenses it through its own lane.
 *
 *   * WHO: a prescriber (medication.prescribe) on an open visit, with the
 *     patient not admitted. Anyone with the clinic open sees the list.
 *   * THE ALLERGY REVIEW COMES FIRST (ruling OPD-S7): until a review lets the
 *     note be signed, no form is offered and the server refuses a line.
 *   * THE SCREEN: the server checks every line for allergies, interactions
 *     (the ward chart and this patient's other outpatient prescriptions) and
 *     the dose. A severe alert or a dose block stops the line (428) until the
 *     prescriber writes why (SafetyAlertModal); milder alerts are shown once
 *     after the line is written. The route is never filled in for the doctor.
 *   * CANCEL needs a reason; if a print already listed the line, the screen
 *     says to take that copy back.
 *   * PRINT shows the live lines with the doctor's registration number.
 *
 * With the medication-order store off, or on an Orb Ward Record computer
 * (the drug chart stays on paper), the server answers 404 and this panel
 * shows nothing at all.
 */
import { useCallback, useEffect, useId, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

import { isSafetyAckRequired, type MedicationSafety, type SafetyAck } from '../../../api/client'
import { errorCode } from '../../../api/opd'
import type { AllergyReviewState } from '../../../api/opdAllergyReview'
import {
  cancelPrescription,
  fetchPrescriptionPrint,
  fetchVisitPrescriptions,
  newLineKey,
  prescriptionLine,
  prescriptionsOff,
  writePrescription,
  type OpdPrescription,
  type PrescriptionInput,
  type PrescriptionPrint,
} from '../../../api/opdPrescriptions'
import OpdPrescriptionPrint from '../../../components/print/OpdPrescriptionPrint'
import SafetyAlertModal from '../../../components/vigil/SafetyAlertModal'
import { formatClinicalDateTime } from '../../../lib/datetime'
import { MEDICATION_PRESCRIBE, hasPermission } from '../../../lib/roles'
import './ConsultationEditor.css'

interface PrescriptionPanelProps {
  visitId: string
  /** False on a visit that takes no new lines (finished, cancelled, the
   *  patient admitted): the list still shows, nothing new is offered. */
  canWrite: boolean
  /** The consultation read's allergy review (ruling OPD-S7). */
  allergyReview: AllergyReviewState | null
  /** Open the allergy review dialog. */
  onReviewAllergies?: () => void
}

interface FormState {
  drug_name: string
  dose: string
  route: string
  frequency: string
  is_prn: boolean
  prn_indication: string
  duration_days: string
  quantity: string
  instructions: string
}

const EMPTY: FormState = {
  drug_name: '', dose: '', route: '', frequency: '', is_prn: false, prn_indication: '',
  duration_days: '', quantity: '', instructions: '',
}

const ROUTES = ['Oral', 'Sublingual', 'Topical', 'Inhaled', 'Eye drops', 'Ear drops', 'Nasal',
  'Rectal', 'Vaginal', 'Subcutaneous', 'Intramuscular']
const FREQUENCIES = ['OD', 'BD', 'TDS', 'QDS', 'HS', 'Once a week', 'SOS']
const MAX_DAYS = 365
const MAX_QUANTITY = 9999

function messageOf(err: unknown, fallback: string): string {
  const detail = (err as { detail?: { message?: unknown } } | null)?.detail
  if (detail && typeof detail.message === 'string') return detail.message
  if (err instanceof Error && err.message) return err.message
  return fallback
}

function wholeNumber(raw: string, label: string, max: number): number | undefined | string {
  const text = raw.trim()
  if (!text) return undefined
  const n = Number(text)
  if (!Number.isInteger(n) || n < 1 || n > max) return `${label} must be a whole number from 1 to ${max}.`
  return n
}

/** The form as the server takes it, or the sentence saying what is missing. */
function inputOf(form: FormState): PrescriptionInput | string {
  const drug = form.drug_name.trim()
  if (!drug) return 'Enter the medicine.'
  if (!form.dose.trim() || !form.route.trim() || !form.frequency.trim()) {
    return 'Enter the dose, the route and how often it is taken.'
  }
  if (form.is_prn && !form.prn_indication.trim()) {
    return 'Say when the patient should take it, for example for fever above 38.5.'
  }
  const days = wholeNumber(form.duration_days, 'Duration in days', MAX_DAYS)
  if (typeof days === 'string') return days
  const quantity = wholeNumber(form.quantity, 'Quantity', MAX_QUANTITY)
  if (typeof quantity === 'string') return quantity
  const out: PrescriptionInput = {
    drug_name: drug,
    dose: form.dose.trim(),
    route: form.route.trim(),
    frequency: form.frequency.trim(),
  }
  if (form.is_prn) {
    out.is_prn = true
    out.prn_indication = form.prn_indication.trim()
  }
  if (days !== undefined) out.duration_days = days
  if (quantity !== undefined) out.quantity = quantity
  if (form.instructions.trim()) out.instructions = form.instructions.trim()
  return out
}

function needsReview(safety: MedicationSafety | null | undefined): boolean {
  return !!safety && (safety.max_severity !== 'none' || safety.interaction_screened === false)
}

function CancelForm({ line, visitId, onCancelled, onClose }: {
  line: OpdPrescription
  visitId: string
  onCancelled: (line: OpdPrescription, printedBefore: boolean) => void
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
      const done = await cancelPrescription(visitId, line.id, reason.trim(), line.version)
      onCancelled(done.prescription, done.printed_before)
    } catch (err) {
      setError(messageOf(err, 'The medicine was not cancelled.'))
      setBusy(false)
    }
  }
  return (
    <div className="opd-inv__cancel">
      <div className="opd-field">
        <label htmlFor={`${id}-reason`}>Why cancel {line.drug_name}?</label>
        <input id={`${id}-reason`} type="text" value={reason} maxLength={500} disabled={busy}
          onChange={e => setReason(e.target.value)} />
      </div>
      {error && <p className="opd-error" role="alert">{error}</p>}
      <div className="opd-row__actions">
        <button type="button" className="opd-btn opd-btn--quiet" onClick={onClose} disabled={busy}>
          Keep the medicine
        </button>
        <button type="button" className="opd-btn" disabled={busy || reason.trim().length < 3}
          onClick={() => { void submit() }}>
          {busy ? 'Cancelling…' : 'Cancel the medicine'}
        </button>
      </div>
    </div>
  )
}

export default function PrescriptionPanel({
  visitId,
  canWrite,
  allergyReview,
  onReviewAllergies,
}: PrescriptionPanelProps) {
  const id = useId()
  const [lines, setLines] = useState<OpdPrescription[] | null>(null)
  const [off, setOff] = useState(false)
  const [loadFailed, setLoadFailed] = useState(false)
  const [form, setForm] = useState<FormState>(EMPTY)
  const [busy, setBusy] = useState<'' | 'write' | 'print'>('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [screen, setScreen] = useState<{ safety: MedicationSafety; mode: 'blocking' | 'info'; drug: string } | null>(null)
  const [pending, setPending] = useState<PrescriptionInput | null>(null)
  const [cancelling, setCancelling] = useState<string | null>(null)
  const [print, setPrint] = useState<PrescriptionPrint | null>(null)
  const key = useRef<string>(newLineKey())
  const alive = useRef(true)

  useEffect(() => {
    alive.current = true
    return () => { alive.current = false }
  }, [])

  const load = useCallback(async () => {
    try {
      const found = await fetchVisitPrescriptions(visitId)
      if (!alive.current) return
      setLines(Array.isArray(found?.prescriptions) ? found.prescriptions : [])
      setLoadFailed(false)
    } catch (err) {
      if (!alive.current) return
      if (prescriptionsOff(err)) setOff(true)
      else setLoadFailed(true)
    }
  }, [visitId])

  // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch trigger: the setState is the loader's own result, not state derived from props.
  useEffect(() => { void load() }, [load])

  if (off) return null

  const mayPrescribe = canWrite && hasPermission(MEDICATION_PRESCRIBE)
  const reviewed = !!allergyReview?.can_sign

  const edit = <K extends keyof FormState>(field: K, value: FormState[K]) => {
    // A changed line is a new request: it gets its own key.
    key.current = newLineKey()
    setForm(f => ({ ...f, [field]: value }))
  }

  const send = async (input: PrescriptionInput, ack?: SafetyAck) => {
    if (busy) return
    setBusy('write')
    setError('')
    setNotice('')
    try {
      const done = await writePrescription(visitId, input, key.current, ack)
      if (!alive.current) return
      key.current = newLineKey()
      setPending(null)
      setScreen(null)
      setForm(EMPTY)
      const line = done.prescription
      setLines(prev => {
        const list = prev ?? []
        return list.some(x => x.id === line.id) ? list : [...list, line]
      })
      const others = (done.duplicates ?? []).map(d => d.drug_name)
      setNotice(`${line.drug_name} prescribed.`
        + (others.length ? ` Already prescribed or on the chart: ${others.join(', ')}.` : ''))
      if (!ack && done.safety && needsReview(done.safety)) {
        setScreen({ safety: done.safety, mode: 'info', drug: line.drug_name })
      }
    } catch (err) {
      if (!alive.current) return
      if (isSafetyAckRequired(err)) {
        setPending(input)
        setScreen({ safety: err.safety, mode: 'blocking', drug: input.drug_name })
      } else if (errorCode(err) === 'allergy_review_required') {
        setError('Record the allergy review before prescribing: the patient’s allergies, no known '
          + 'allergies, or why they could not be asked.')
      } else {
        setError(messageOf(err, 'The medicine was not prescribed.'))
      }
    } finally {
      if (alive.current) setBusy('')
    }
  }

  const onSubmit = () => {
    const input = inputOf(form)
    if (typeof input === 'string') {
      setError(input)
      return
    }
    void send(input)
  }

  const onPrint = async () => {
    setBusy('print')
    setError('')
    try {
      const data = await fetchPrescriptionPrint(visitId)
      if (alive.current) setPrint(data)
    } catch (err) {
      if (alive.current) setError(messageOf(err, 'The prescription could not be printed.'))
    } finally {
      if (alive.current) setBusy('')
    }
  }

  const live = (lines ?? []).filter(l => l.live)

  return (
    <section className="opd-block opd-inv opd-rx" aria-label="Prescription for this visit">
      <h4 className="opd-subhead">Prescription</h4>
      <p className="opd-hint">
        Medicines for the patient to take home. They are not added to the ward drug chart.
      </p>
      {lines === null && !loadFailed && <p className="opd-empty">Loading the prescription…</p>}
      {loadFailed && (
        <div className="opd-row__actions">
          <p className="opd-hint">The prescription on this visit could not be loaded.</p>
          <button type="button" className="opd-btn opd-btn--quiet" onClick={() => { void load() }}>
            Try again
          </button>
        </div>
      )}
      {lines !== null && lines.length === 0 && <p className="opd-empty">No medicine prescribed.</p>}
      {lines !== null && lines.length > 0 && (
        <ul className="opd-inv__list" aria-label="Medicines on this visit">
          {lines.map(l => (
            <li key={l.id} className="opd-inv__item">
              <div className="opd-inv__what">
                <span className={l.live ? 'opd-inv__name' : 'opd-inv__name opd-inv__name--cancelled'}>
                  {l.drug_name}
                </span>
                <span className="opd-line">{prescriptionLine(l)}</span>
                {l.instructions && <span className="opd-line">{l.instructions}</span>}
                <span className="opd-hint">
                  {l.live
                    ? [l.status_label, l.prescriber_name,
                      l.prescribed_at ? formatClinicalDateTime(l.prescribed_at) : null]
                      .filter(Boolean).join(' · ')
                    : [l.status_label, l.ended_by_name,
                      l.ended_at ? formatClinicalDateTime(l.ended_at) : null,
                      l.cancel_reason ? `Reason: ${l.cancel_reason}` : null]
                      .filter(Boolean).join(' · ')}
                </span>
                {l.safety_overridden && (
                  <span className="opd-hint">A drug-safety alert was overridden with a reason.</span>
                )}
              </div>
              {mayPrescribe && l.live && cancelling !== l.id && (
                <button type="button" className="opd-btn opd-btn--quiet"
                  aria-label={`Cancel ${l.drug_name}`} onClick={() => setCancelling(l.id)}>
                  Cancel
                </button>
              )}
              {cancelling === l.id && (
                <CancelForm
                  line={l}
                  visitId={visitId}
                  onClose={() => setCancelling(null)}
                  onCancelled={(moved, printedBefore) => {
                    setCancelling(null)
                    setLines(prev => (prev ?? []).map(x => (x.id === moved.id ? moved : x)))
                    setNotice(printedBefore
                      ? `${moved.drug_name} cancelled. A printed prescription that lists it was `
                        + 'already made: take that copy back from the patient and print again.'
                      : `${moved.drug_name} cancelled.`)
                  }}
                />
              )}
            </li>
          ))}
        </ul>
      )}

      {mayPrescribe && lines !== null && !reviewed && (
        <div className="opd-cons__review" role="group" aria-label="Allergy review for prescribing">
          <p className="opd-hint">
            Medicines can be prescribed once the patient&rsquo;s allergies are recorded in an
            allergy review: their allergies, no known allergies, or why they could not be asked.
          </p>
          {onReviewAllergies && (
            <div className="opd-row__actions">
              <button type="button" className="opd-btn" onClick={onReviewAllergies}>
                Record the review now
              </button>
            </div>
          )}
        </div>
      )}

      {mayPrescribe && lines !== null && reviewed && (
        <form className="opd-inv__find" aria-label="Prescribe a medicine" noValidate
          onSubmit={e => { e.preventDefault(); onSubmit() }}>
          <div className="opd-field">
            <label htmlFor={`${id}-drug`}>Medicine</label>
            <input id={`${id}-drug`} type="text" value={form.drug_name} maxLength={200}
              autoComplete="off" disabled={busy !== ''}
              onChange={e => edit('drug_name', e.target.value)} />
          </div>
          <div className="opd-field-row">
            <div className="opd-field">
              <label htmlFor={`${id}-dose`}>Dose</label>
              <input id={`${id}-dose`} type="text" value={form.dose} maxLength={60}
                placeholder="For example 500 mg" disabled={busy !== ''}
                onChange={e => edit('dose', e.target.value)} />
            </div>
            <div className="opd-field">
              <label htmlFor={`${id}-route`}>Route</label>
              <input id={`${id}-route`} type="text" value={form.route} maxLength={40}
                list={`${id}-routes`} disabled={busy !== ''}
                onChange={e => edit('route', e.target.value)} />
              <datalist id={`${id}-routes`}>
                {ROUTES.map(r => <option key={r} value={r} />)}
              </datalist>
            </div>
            <div className="opd-field">
              <label htmlFor={`${id}-freq`}>How often</label>
              <input id={`${id}-freq`} type="text" value={form.frequency} maxLength={60}
                list={`${id}-freqs`} disabled={busy !== ''}
                onChange={e => edit('frequency', e.target.value)} />
              <datalist id={`${id}-freqs`}>
                {FREQUENCIES.map(f => <option key={f} value={f} />)}
              </datalist>
            </div>
          </div>
          <label className="opd-check">
            <input type="checkbox" checked={form.is_prn} disabled={busy !== ''}
              onChange={e => edit('is_prn', e.target.checked)} />
            Only when needed
          </label>
          {form.is_prn && (
            <div className="opd-field">
              <label htmlFor={`${id}-prn`}>When needed for</label>
              <input id={`${id}-prn`} type="text" value={form.prn_indication} maxLength={200}
                placeholder="For example fever above 38.5" disabled={busy !== ''}
                onChange={e => edit('prn_indication', e.target.value)} />
            </div>
          )}
          <div className="opd-field-row">
            <div className="opd-field">
              <label htmlFor={`${id}-days`}>Duration in days</label>
              <input id={`${id}-days`} type="number" inputMode="numeric" min={1} max={MAX_DAYS}
                step={1} value={form.duration_days} disabled={busy !== ''}
                onChange={e => edit('duration_days', e.target.value)} />
            </div>
            <div className="opd-field">
              <label htmlFor={`${id}-qty`}>Quantity</label>
              <input id={`${id}-qty`} type="number" inputMode="numeric" min={1} max={MAX_QUANTITY}
                step={1} value={form.quantity} disabled={busy !== ''}
                onChange={e => edit('quantity', e.target.value)} />
            </div>
          </div>
          <div className="opd-field">
            <label htmlFor={`${id}-instr`}>Instructions to the patient</label>
            <input id={`${id}-instr`} type="text" value={form.instructions} maxLength={500}
              placeholder="For example after food" disabled={busy !== ''}
              onChange={e => edit('instructions', e.target.value)} />
          </div>
          <div className="opd-row__actions">
            <button type="submit" className="opd-btn opd-btn--primary" disabled={busy !== ''}>
              {busy === 'write' ? 'Checking…' : 'Prescribe'}
            </button>
          </div>
        </form>
      )}

      {error && <p className="opd-error" role="alert">{error}</p>}
      {notice && <p className="opd-hint" role="status">{notice}</p>}

      {live.length > 0 && (
        <div className="opd-row__actions">
          <button type="button" className="opd-btn" disabled={busy !== ''} onClick={() => { void onPrint() }}>
            {busy === 'print' ? 'Preparing…' : 'Print prescription'}
          </button>
        </div>
      )}

      {screen && (
        <SafetyAlertModal
          safety={screen.safety}
          mode={screen.mode}
          drugName={screen.drug}
          busy={busy === 'write'}
          onAcknowledge={reason => {
            if (pending) void send(pending, { acknowledged: true, reason })
          }}
          placedNotice={screen.mode === 'info'
            ? 'This medicine is already on the prescription. Review the alerts below against a line that exists; cancel it from the list if it should not stand.'
            : undefined}
          onClose={() => { setScreen(null); setPending(null) }}
        />
      )}

      {print && createPortal(
        <OpdPrescriptionPrint print={print} onAfterPrint={() => setPrint(null)} />,
        document.body,
      )}
    </section>
  )
}
