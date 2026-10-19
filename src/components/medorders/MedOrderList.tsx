import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import {
  getUser,
  isSafetyAckRequired,
  type AdministrationRecord,
  type ChartMedicationRow,
  type MedicationSafety,
  type SafetyAck,
} from '../../api/client'
import {
  MED_ORDER_STEP_LABELS,
  MEDORDER_HOLD_STOP_ROUTES,
  REASON_REQUIRED_STEPS,
  cancelMedOrderDraft,
  errorCode,
  fetchPatientMedOrders,
  holdMedOrder,
  isLiveMedOrder,
  lastDoseWords,
  ledgerAdministration,
  medOrderChip,
  medOrderSteps,
  resumeMedOrder,
  signMedOrder,
  stopMedOrder,
  subscribeMedOrderUpdates,
  supplyLineWords,
  type LegacyMedication,
  type MedOrder,
  type MedOrderChip,
  type MedOrderStep,
  type MedOrderStepResult,
  type PatientMedOrders,
} from '../../api/medorders'
import { formatClinicalDateTime } from '../../lib/datetime'
import { hasPermission, MEDICATION_PRESCRIBE } from '../../lib/roles'
import { useFeatureFlags } from '../../shellConfig'
import SafetyAlertModal from '../vigil/SafetyAlertModal'
import AcrossAllergyMark from './AcrossAllergyMark'
import MedOrderTimeline from './MedOrderTimeline'
import './MedOrderList.css'

/* ═══════════════════════════════════════════════════════════════════════════
   THE CHART'S MEDICATION ORDERS (medorders-10, W9-D)

   Drawn in the chart's Medications window while the server's
   'med_order_store' feature is on; with it off the chart keeps its old table
   (VigilPanel). One row per order from GET /api/medorders/patient/{id}, plus
   each chart row the order record has not taken in yet.

   EACH ROW SAYS, IN WORDS:
     * the status, as a chip whose label is the status (Awaiting signature,
       Awaiting pharmacist, Prescribed, Verified by pharmacist, On hold,
       Stopped, Completed, Cancelled, Rejected by pharmacy). The chip's border
       adds a status colour; the word is always there, so colour never carries
       the status alone;
     * who prescribed it, who checked it, and on a hold who held it, when and
       why;
     * the supply line: where the pharmacy's supply stands, or 'Supply not
       linked' when none was ever requested (ward stock or the patient's own);
     * the last dose recorded at the bedside, and, when the last dose given
       was given across a charted allergy, the 'Across charted allergy' chip
       with the written reason (AcrossAllergyMark, never silent, exactly as
       the old table marked it; the history marks each such dose too).

   THE STEPS ARE THE SERVER'S. Which buttons appear comes from
   medOrderSteps(): with the lifecycle off none do (record mode); sign, hold,
   resume and stop are the prescriber's (ruling MO-R2 (a)); a draft is
   cancelled by whoever entered it or a prescriber. Hold, resume and stop are
   offered only once the server serves them (MEDORDER_HOLD_STOP_ROUTES,
   medorders-6a). Every step that needs a reason asks for one; the server
   checks the version on screen (a stale one is said and the list reloads);
   sign and resume can meet the safety screen's 428, which opens the same
   alert window the rest of the chart uses.

   Administer opens the chart's own AdministerModal for the order's ward row,
   exactly as the old table did, and only where the old table offered it: the
   eMAR keeps every check it has, and a held order is refused there.

   THE ADD FORM IS NOT HERE. Adding a medicine keeps posting to the chart's
   own route (ruling R010, held: who may enter a medicine order), and keeps its
   Discontinued option, which records a stopped order.

   Everything reloads on 'medorders:update' for this patient (ids only on the
   socket; the list reads the rest over REST), and whenever the chart's ward
   rows or administrations it hands in change (the chart's own Add form pushes
   nothing on the socket).
   ═══════════════════════════════════════════════════════════════════════════ */

interface Props {
  patientId: string
  /** The chart's ward rows: the Administer target and the legacy dose chips. */
  chartRows: ChartMedicationRow[]
  /** The patient's administrations, newest first (the chart already reads them). */
  administrations: AdministrationRecord[]
  /** The drug round runs on this computer (not the Orb Ward Record). */
  canAdminister: boolean
  onAdminister: (row: ChartMedicationRow) => void
  /** Hold, resume and stop are served. Defaults to MEDORDER_HOLD_STOP_ROUTES. */
  holdStopAvailable?: boolean
  /** After a step this list took, so the chart can reload its own rows. */
  onChanged?: () => void
}

interface StepForm {
  order: MedOrder
  step: MedOrderStep
  reason: string
}

interface SafetyPrompt {
  order: MedOrder
  step: 'sign' | 'resume'
  reason: string
  safety: MedicationSafety
}

const ENDED_STATUSES = new Set(['stopped', 'completed', 'cancelled', 'rejected'])

const STEP_DONE: Record<MedOrderStep, string> = {
  sign: 'Signed. It is on the ward chart and the drug round now.',
  hold: 'On hold. Doses are refused at the bedside until it is resumed.',
  resume: 'Resumed. Doses can be given again.',
  stop: 'Stopped. It is off the drug round.',
  cancel: 'Cancelled. It never reached the ward.',
}

const REASON_LABEL: Record<MedOrderStep, string> = {
  sign: '',
  hold: 'Why is it on hold?',
  resume: 'Why is it being resumed?',
  stop: 'Why is it being stopped?',
  cancel: 'Why is it being cancelled?',
}

function chipClass(chip: MedOrderChip): string {
  return chip.tone === 'neutral' ? 'medorder-chip' : `medorder-chip medorder-chip--${chip.tone}`
}

function regimenWords(o: { dose: string | null; route: string | null; frequency: string | null;
  is_prn: boolean; prn_indication?: string | null; priority?: string | null }): string {
  const parts = [o.dose, o.route, o.frequency].filter(p => p && String(p).trim()) as string[]
  if (o.is_prn) {
    parts.push(o.prn_indication && o.prn_indication.trim()
      ? `as required for ${o.prn_indication.trim()}` : 'as required')
  }
  if (o.priority === 'stat') parts.push('stat')
  if (o.priority === 'urgent') parts.push('urgent')
  return parts.length ? parts.join(' · ') : 'No dose recorded'
}

function whenWords(at: string | null | undefined): string {
  const when = formatClinicalDateTime(at)
  return when ? ` at ${when}` : ''
}

/** The last 'given' record on a ward row, from the chart's administrations. */
function lastGivenOnRow(records: AdministrationRecord[], medicationId: number | null):
AdministrationRecord | undefined {
  if (medicationId === null || medicationId === undefined) return undefined
  return records.find(a => a.medication_id === medicationId && a.status === 'given')
}

function refusalWords(err: unknown): string {
  const code = errorCode(err)
  const status = (err as { status?: number } | null)?.status
  switch (code) {
    case 'stale_version':
    case 'screen_stale':
      return 'This order changed since the list was loaded. The list is up to date now: check it and try again.'
    case 'prescriber_required':
      return 'Only a prescriber can do this.'
    case 'reason_required':
      return 'Give a reason first.'
    case 'cancel_not_offered':
      return 'A signed order is stopped, not cancelled.'
    case 'dose_route_and_frequency_required':
      return 'Add the dose, route and frequency in Orders before signing.'
    case 'transition_not_allowed':
      return 'This order cannot take that step from where it is now.'
    case 'not_draft_creator':
      return 'Only the person who entered this draft, or a prescriber, can cancel it.'
    case 'feature_disabled':
      return 'Medication order steps are switched off on this computer.'
    default:
      break
  }
  if (status === 404) return 'This step is not available on this computer.'
  return err instanceof Error && err.message ? err.message : 'Nothing was changed. Try again.'
}

export default function MedOrderList({
  patientId, chartRows, administrations, canAdminister, onAdminister,
  holdStopAvailable = MEDORDER_HOLD_STOP_ROUTES, onChanged,
}: Props) {
  const features = useFeatureFlags()
  const lifecycleOn = features.med_order_lifecycle === true
  const verifyBeforeDose = features.med_order_verify_before_dose === true
  const mayPrescribe = hasPermission(MEDICATION_PRESCRIBE)
  const userId = getUser()?.id ?? null

  const [data, setData] = useState<PatientMedOrders | null>(null)
  const [failed, setFailed] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [form, setForm] = useState<StepForm | null>(null)
  const [prompt, setPrompt] = useState<SafetyPrompt | null>(null)
  const [message, setMessage] = useState<{ orderId: string; text: string; ok: boolean } | null>(null)
  const [showEnded, setShowEnded] = useState(false)
  const [history, setHistory] = useState<Record<string, boolean>>({})

  // Several things start a read (the mount, a socket push, a dose, the chart's
  // own rows changing, a step), and two can overlap. Only the newest one
  // writes: an older answer landing last would put a superseded list back.
  const loadSeq = useRef(0)
  const load = useCallback(async () => {
    const seq = ++loadSeq.current
    try {
      const res = await fetchPatientMedOrders(patientId)
      if (seq !== loadSeq.current) return
      setData(res)
      setFailed(null)
    } catch (err) {
      if (seq !== loadSeq.current) return
      // `data` is left as it was: a failed refresh must not paint an empty chart.
      setFailed(err instanceof Error && err.message ? err.message : 'The orders could not be read.')
    }
  }, [patientId])

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch trigger, not derived state.
    void load()
    return subscribeMedOrderUpdates(update => {
      if (update.patient_id === patientId) void load()
    })
  }, [load, patientId])

  // A dose recorded from this chart reloads the administrations it hands in;
  // the last-dose lines follow (the first hand-in is the mount's own load).
  const seenAdministrations = useRef(administrations)
  useEffect(() => {
    if (seenAdministrations.current === administrations) return
    seenAdministrations.current = administrations
    void load()
  }, [administrations, load])

  // A medicine added from the chart's own Add form (or a stopped one recorded
  // there) posts to the chart's route, which pushes no 'medorders:update'; the
  // chart then reloads the ward rows it hands in. Follow them, or the window's
  // count reads one more than the list and the missing order invites a second
  // one. Compared by what the rows say, not by identity: the chart may hand in
  // a new array holding the same rows (an empty list is a new [] each render).
  const chartRowsKey = useMemo(() => chartRows
    .map(r => [r.id, r.status, r.unverified ? 1 : 0, r.drug_name, r.dosage, r.route, r.frequency].join('\u0001'))
    .join('\u0002'), [chartRows])
  const seenChartRowsKey = useRef(chartRowsKey)
  useEffect(() => {
    if (seenChartRowsKey.current === chartRowsKey) return
    seenChartRowsKey.current = chartRowsKey
    void load()
  }, [chartRowsKey, load])

  const rowsById = useMemo(() => new Map(chartRows.map(r => [r.id, r])), [chartRows])

  const orders = data?.orders ?? []
  const drafts = orders.filter(o => o.status === 'draft')
  const current = orders.filter(o => isLiveMedOrder(o) && o.status !== 'draft')
  const ended = orders.filter(o => ENDED_STATUSES.has(o.status))
  const legacyLive = (data?.legacy ?? []).filter(l => l.chart_status === 'active' || l.chart_status === 'prn')
  const legacyEnded = (data?.legacy ?? []).filter(l => !(l.chart_status === 'active' || l.chart_status === 'prn'))

  const finish = async (order: MedOrder, step: MedOrderStep, res: MedOrderStepResult | null) => {
    const same = (res?.duplicates ?? []).map(d => d.drug_name).filter(Boolean)
    const text = same.length
      ? `${STEP_DONE[step]} Already on this patient's chart: ${same.join(', ')}. Check it is not given twice.`
      : STEP_DONE[step]
    setMessage({ orderId: order.id, text, ok: true })
    setForm(null)
    setPrompt(null)
    await load()
    onChanged?.()
  }

  const run = async (order: MedOrder, step: MedOrderStep, reason: string, ack?: SafetyAck) => {
    setBusy(order.id)
    setMessage(null)
    try {
      let res: MedOrderStepResult | null = null
      if (step === 'sign') res = await signMedOrder(order.id, order.version, ack)
      else if (step === 'cancel') await cancelMedOrderDraft(order.id, reason, order.version)
      else if (step === 'hold') res = await holdMedOrder(order.id, reason, order.version)
      else if (step === 'resume') res = await resumeMedOrder(order.id, reason, order.version, ack)
      else res = await stopMedOrder(order.id, reason, order.version)
      await finish(order, step, res)
    } catch (err) {
      if ((step === 'sign' || step === 'resume') && isSafetyAckRequired(err)) {
        setPrompt({ order, step, reason, safety: err.safety })
        return
      }
      setPrompt(null)
      setMessage({ orderId: order.id, text: refusalWords(err), ok: false })
      const code = errorCode(err)
      if (code === 'stale_version' || code === 'screen_stale') void load()
    } finally {
      setBusy(null)
    }
  }

  const startStep = (order: MedOrder, step: MedOrderStep) => {
    setMessage(null)
    if (REASON_REQUIRED_STEPS.includes(step)) {
      setForm({ order, step, reason: '' })
    } else {
      void run(order, step, '')
    }
  }

  const submitForm = (e: FormEvent) => {
    e.preventDefault()
    if (!form) return
    const reason = form.reason.trim()
    if (!reason) {
      setMessage({ orderId: form.order.id, text: 'Give a reason first.', ok: false })
      return
    }
    void run(form.order, form.step, reason)
  }

  const renderOrder = (order: MedOrder) => {
    const chip = medOrderChip(order, { verifyBeforeDose })
    const steps = medOrderSteps(order, { mayPrescribe, userId, lifecycleOn, holdStopAvailable })
    const row = order.legacy_medication_id !== null ? rowsById.get(order.legacy_medication_id) : undefined
    const live = order.status === 'prescribed' || order.status === 'verified'
    const offerAdminister = canAdminister && live && !!row && row.status === 'active'
    const lastDose = lastDoseWords(order)
    const rowLastGiven = lastGivenOnRow(administrations, order.legacy_medication_id)
    const legacyGiven = lastDose ? undefined : rowLastGiven
    // The dose whose allergy override is marked: the ledger's last dose when it
    // is a given record the chart has read; otherwise the ward row's last given
    // dose, the record the old table marked (a later 'not given' must not hide
    // it). AcrossAllergyMark draws nothing unless givenAcrossAllergy() says so.
    const ledgerRecord = ledgerAdministration(administrations, order.last_dose)
    const markRecord = ledgerRecord?.status === 'given' ? ledgerRecord : rowLastGiven
    const formHere = form?.order.id === order.id ? form : null
    const note = message?.orderId === order.id ? message : null
    const historyOpen = !!history[order.id]
    return (
      <li key={order.id} className="medorder-row">
        <div className="medorder-row__head">
          <div className="medorder-row__drug">
            <span className="medorder-row__name">{order.drug_name}</span>
            <span className="medorder-row__regimen">{regimenWords(order)}</span>
          </div>
          <span className={chipClass(chip)}>{chip.label}</span>
        </div>

        {order.status === 'draft' ? (
          <span className="medorder-row__line">
            Entered by {order.entered_by_name || 'a colleague'}{whenWords(order.created_at)}.
            Nothing reaches the ward until a prescriber signs it.
          </span>
        ) : (
          order.prescriber_name && (
            <span className="medorder-row__line">
              Prescribed by {order.prescriber_name}{whenWords(order.prescribed_at)}
              {order.entered_by_role === 'nurse' && order.entered_by_name
                ? `, entered by ${order.entered_by_name} (a nurse)` : ''}
            </span>
          )
        )}
        {order.verified_by && (
          <span className="medorder-row__line">
            Checked by {order.verified_by_name || 'a pharmacist'}{whenWords(order.verified_at)}
          </span>
        )}
        {order.status === 'held' && (
          <span className="medorder-row__line medorder-row__line--danger">
            On hold since {formatClinicalDateTime(order.status_changed_at) || 'a time not recorded'}
            {order.status_changed_by_name ? `, by ${order.status_changed_by_name}` : ''}
            {order.status_reason ? `: ${order.status_reason}` : ''}. Do not give. A dose can
            still be recorded as not given (held).
          </span>
        )}
        {ENDED_STATUSES.has(order.status) && order.status_reason && (
          <span className="medorder-row__line">Reason: {order.status_reason}</span>
        )}
        {order.status !== 'draft' && !ENDED_STATUSES.has(order.status) && (
          <span className="medorder-row__line">{supplyLineWords(order)}</span>
        )}
        {lastDose && <span className="medorder-row__line">{lastDose}</span>}
        {legacyGiven && (
          <span className="medorder-row__line">
            Last dose given by {legacyGiven.administered_by_name || 'a colleague'}
            {whenWords(legacyGiven.administered_at)}
          </span>
        )}
        <AcrossAllergyMark record={markRecord} />

        {note && (
          <p className={note.ok ? 'medorder-ok' : 'medorder-error'} role={note.ok ? 'status' : 'alert'}>
            {note.text}
          </p>
        )}

        {formHere && (
          <form className="medorder-form" onSubmit={submitForm} noValidate>
            <label htmlFor={`medorder-reason-${order.id}`}>{REASON_LABEL[formHere.step]}</label>
            <textarea
              id={`medorder-reason-${order.id}`}
              rows={2}
              value={formHere.reason}
              onChange={e => setForm({ ...formHere, reason: e.target.value })}
              disabled={busy === order.id}
            />
            <div className="medorder-row__actions">
              <button type="submit" className="medorder-btn medorder-btn--primary"
                disabled={busy === order.id || !formHere.reason.trim()}>
                {busy === order.id ? 'Saving…' : MED_ORDER_STEP_LABELS[formHere.step]}
              </button>
              <button type="button" className="medorder-btn medorder-btn--quiet"
                disabled={busy === order.id} onClick={() => setForm(null)}>
                Not now
              </button>
            </div>
          </form>
        )}

        <div className="medorder-row__actions">
          {!formHere && steps.map(step => (
            <button
              key={step}
              type="button"
              className={`medorder-btn${step === 'sign' ? ' medorder-btn--primary' : ''}`}
              disabled={busy === order.id}
              onClick={() => startStep(order, step)}
            >
              {busy === order.id && step === 'sign' ? 'Signing…' : MED_ORDER_STEP_LABELS[step]}
            </button>
          ))}
          {offerAdminister && row && (
            <button
              type="button"
              className="medorder-btn"
              disabled={!!row.unverified}
              title={row.unverified ? 'Pharmacist verification required' : `Record administration of ${order.drug_name}`}
              onClick={() => onAdminister(row)}
            >
              Administer
            </button>
          )}
          <button
            type="button"
            className="medorder-btn medorder-btn--quiet"
            aria-expanded={historyOpen}
            onClick={() => setHistory(prev => ({ ...prev, [order.id]: !historyOpen }))}
          >
            {historyOpen ? 'Hide history' : 'History'}
          </button>
        </div>

        {historyOpen && (
          <MedOrderTimeline orderId={order.id} version={order.version} drugName={order.drug_name}
            administrations={administrations} />
        )}
      </li>
    )
  }

  const renderLegacy = (item: LegacyMedication) => {
    const chip = item.status ? medOrderChip({
      status: item.status, needs_verification: item.needs_verification,
      verified_by: item.verified_by,
    }, { verifyBeforeDose: false }) : { label: 'Status not known', tone: 'warn' as const }
    const row = rowsById.get(item.medication_id)
    const offerAdminister = canAdminister && !!row && row.status === 'active'
    const given = lastGivenOnRow(administrations, item.medication_id)
    return (
      <li key={`legacy-${item.medication_id}`} className="medorder-row">
        <div className="medorder-row__head">
          <div className="medorder-row__drug">
            <span className="medorder-row__name">{item.drug_name}</span>
            <span className="medorder-row__regimen">
              {regimenWords({ dose: item.dose, route: item.route, frequency: item.frequency,
                is_prn: item.is_prn })}
            </span>
          </div>
          <span className={chipClass(chip)}>{chip.label}</span>
        </div>
        <span className="medorder-row__line">
          Charted before the order record. It has no order history here yet.
        </span>
        {given && (
          <span className="medorder-row__line">
            Last dose given by {given.administered_by_name || 'a colleague'}{whenWords(given.administered_at)}
          </span>
        )}
        <AcrossAllergyMark record={given} />
        {offerAdminister && row && (
          <div className="medorder-row__actions">
            <button
              type="button"
              className="medorder-btn"
              disabled={!!row.unverified}
              title={row.unverified ? 'Pharmacist verification required' : `Record administration of ${item.drug_name}`}
              onClick={() => onAdminister(row)}
            >
              Administer
            </button>
          </div>
        )}
      </li>
    )
  }

  const endedCount = ended.length + legacyEnded.length

  return (
    <div className="medorder-list">
      {failed && (
        <div className="medorder-failed" role="alert">
          <span>
            Could not load the medication orders. {failed} This is a display failure: it does
            not mean the patient has no medicines. Check the drug chart before giving anything.
          </span>
          <button type="button" className="medorder-btn" onClick={() => void load()}>Retry</button>
        </div>
      )}
      {data === null && !failed && (
        <p className="medorder-note" role="status">Loading the medication orders</p>
      )}
      {data !== null && orders.length === 0 && (data.legacy ?? []).length === 0 && (
        <p className="medorder-note">No medicines are ordered for this patient.</p>
      )}
      {!lifecycleOn && data !== null && orders.length > 0 && (
        <p className="medorder-note">
          This list is for reading on this computer. Medicines are changed from the chart and
          Orders as before.
        </p>
      )}

      {drafts.length > 0 && (
        <section className="medorder-list__group" aria-labelledby="medorder-drafts">
          <h3 className="medorder-list__heading" id="medorder-drafts">
            Awaiting signature ({drafts.length})
          </h3>
          <ul className="medorder-list__items">{drafts.map(renderOrder)}</ul>
        </section>
      )}
      {(current.length > 0 || legacyLive.length > 0) && (
        <section className="medorder-list__group" aria-labelledby="medorder-current">
          <h3 className="medorder-list__heading" id="medorder-current">
            Current ({current.length + legacyLive.length})
          </h3>
          <ul className="medorder-list__items">
            {current.map(renderOrder)}
            {legacyLive.map(renderLegacy)}
          </ul>
        </section>
      )}
      {endedCount > 0 && (
        <section className="medorder-list__group" aria-labelledby="medorder-ended">
          <h3 className="medorder-list__heading" id="medorder-ended">Ended ({endedCount})</h3>
          <button
            type="button"
            className="medorder-btn medorder-btn--quiet"
            aria-expanded={showEnded}
            onClick={() => setShowEnded(v => !v)}
          >
            {showEnded ? 'Hide ended orders' : `Show ${endedCount} ended ${endedCount === 1 ? 'order' : 'orders'}`}
          </button>
          {showEnded && (
            <ul className="medorder-list__items">
              {ended.map(renderOrder)}
              {legacyEnded.map(renderLegacy)}
            </ul>
          )}
        </section>
      )}

      {prompt && (
        <SafetyAlertModal
          safety={prompt.safety}
          mode="blocking"
          drugName={prompt.order.drug_name}
          busy={busy === prompt.order.id}
          onAcknowledge={reason => void run(prompt.order, prompt.step, prompt.reason,
            { acknowledged: true, reason })}
          onClose={() => setPrompt(null)}
        />
      )}
    </div>
  )
}
