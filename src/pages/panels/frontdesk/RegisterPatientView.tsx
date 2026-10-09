/**
 * Front Desk, Register (opd-6a): register an outpatient visit, search first.
 *
 * Shown only while the 'registration' feature is on (the panel discovers this
 * file through ./viewRegistry.ts), and reused as the doctor's "Add walk-in" on
 * the Outpatients tab (opd-8).
 *
 *   1. FIND. The patient index (PatientIndexSearch): hospital-wide, minimal
 *      fields, every search audited. Registering a new patient is offered only
 *      after a search, because the same person registered twice has their
 *      allergies and history split across two records.
 *   2. CONFIRM (ruling OPD-S1). A record found by search is attached to a
 *      visit only after the patient states their name, date of birth or age,
 *      and phone's last 4 digits or UHID, and the server answers match
 *      (IdentityConfirmDialog). The stored date of birth is never shown.
 *   3. NEW PATIENT. Name, sex, a date of birth OR a stated age (with the "no
 *      date of birth, no discharge link" hint), phone, the hospital number in
 *      hospital mode, and an ABHA labelled unverified only when ABDM is on. A
 *      possible duplicate needs a written reason in the site's mode (ruling
 *      OPD-S2); a probable one, or the same UHID or ABHA, is not registered.
 *      There is no allergy field: allergies are a clinician's to record
 *      (ruling OPD-S5).
 *   4. THE VISIT. Clinic (only for an account with no department), doctor,
 *      visit type and complaint. The server re-checks the identity with the
 *      visit, so a new patient's visit carries what the desk just typed and
 *      the UHID the registration returned.
 *   5. AFTER THE VISIT (opd-6b). The token slip (OpdTokenSlipPrint), the UHID
 *      card (UhidCardPrint, read from the server, which audits it as a card
 *      print) and the registration details drawer (DemographicsDrawer) with
 *      reasoned corrections and their history.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'

import {
  fetchConsultingDoctors,
  type ConsultingDoctor,
  type OpdVisitType,
} from '../../../api/frontdesk'
import {
  duplicateCandidates,
  errorCode,
  errorDetail,
  fetchRegistrationConfig,
  fetchUhidCard,
  isFeatureDisabled,
  newRequestKey,
  registerNewPatient,
  registerVisitWithIdentity,
  type DuplicateCandidate,
  type IndexCard,
  type NewPatientInput,
  type RegisteredVisit,
  type RegistrationConfig,
  type StatedIdentity,
  type UhidCard,
} from '../../../api/registration'
import OpdTokenSlipPrint from '../../../components/print/OpdTokenSlipPrint'
import UhidCardPrint from '../../../components/print/UhidCardPrint'
import { REGISTRATION_WRITE, hasPermission, type Permission } from '../../../lib/roles'
import DemographicsDrawer from './DemographicsDrawer'
import IdentityConfirmDialog from './IdentityConfirmDialog'
import PatientIndexSearch from './PatientIndexSearch'
import { cardMeta } from './indexCard'
import type { FrontDeskViewProps } from './viewRegistry'
import './RegisterPatientView.css'

export const viewId = 'register'
export const viewLabel = 'Register'
export const viewOrder = 10
export const viewPermission = 'registration.read'

/** The server's own words (registration_service.NO_DOB_HINT). */
const NO_DOB_HINT = 'No date of birth on file, so a secure discharge link cannot be issued for '
  + 'this patient.'

export interface RegisterPatientViewProps extends FrontDeskViewProps {
  /** The Outpatients tab: the doctor offered first as the consultant. */
  defaultDoctorId?: string | null
  /** Called after a visit is registered (the Outpatients tab refetches). */
  onVisitRegistered?: (visit: RegisteredVisit) => void
  /** 'opd' when reused as the doctor's walk-in; changes wording only. */
  context?: 'desk' | 'opd'
  /** Tests only: whether the signed-in user holds a permission. */
  holds?: (permission: string) => boolean
}

interface ChosenPatient {
  patient_id: string
  name: string
  uhid: string | null
}

type Step =
  | { kind: 'find' }
  | { kind: 'confirm'; card: IndexCard }
  | { kind: 'new'; typed: string }
  | { kind: 'visit'; patient: ChosenPatient; stated: StatedIdentity; source: 'search' | 'new';
      card?: IndexCard; note?: string }
  | { kind: 'done'; visit: RegisteredVisit; patient: ChosenPatient }

function errorText(err: unknown): string {
  if (err instanceof Error && err.message) return err.message
  return 'Something went wrong. Nothing was saved.'
}

function roleHolds(permission: string): boolean {
  return hasPermission(permission as Permission)
}

function sameDepartment(a: string | null | undefined, b: string | null | undefined): boolean {
  return (a ?? '').trim().toLowerCase() === (b ?? '').trim().toLowerCase()
}

/** Typed text that looks like a name (not a number) starts the new form. */
function nameFrom(typed: string): string {
  return /\d/.test(typed) ? '' : typed.trim()
}

const BAND_LABEL: Record<DuplicateCandidate['band'], string> = {
  hard: 'Same UHID or ABHA',
  probable: 'Probable match',
  possible: 'Possible match',
}

// ── A new patient ────────────────────────────────────────────────────────────

interface Refusal {
  code: string
  message: string
  candidates: DuplicateCandidate[]
  /** A possible duplicate the site lets the desk register past with a reason. */
  overridable: boolean
}

function NewPatientForm({
  config,
  initialName,
  onRegistered,
  onChooseExisting,
  onCancel,
  onFeatureOff,
}: {
  config: RegistrationConfig
  initialName: string
  onRegistered: (patient: ChosenPatient, stated: StatedIdentity, note?: string) => void
  onChooseExisting: (card: IndexCard) => void
  onCancel: () => void
  onFeatureOff: () => void
}) {
  const [name, setName] = useState(initialName)
  const [sex, setSex] = useState('')
  const [ageMode, setAgeMode] = useState<'dob' | 'age'>('dob')
  const [dob, setDob] = useState('')
  const [age, setAge] = useState('')
  const [phone, setPhone] = useState('')
  const [uhid, setUhid] = useState('')
  const [abha, setAbha] = useState('')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [refusal, setRefusal] = useState<Refusal | null>(null)
  /* One key per registration on this form, so a session-refresh retry of the
     same request replays instead of registering the patient twice. */
  const [requestKey] = useState(() => newRequestKey('reg'))

  const hospitalMode = config.uhid_source === 'hospital'
  const ageNumber = age.trim() === '' ? NaN : Number(age)
  const ageOk = ageMode === 'dob'
    ? dob !== ''
    : Number.isInteger(ageNumber) && ageNumber >= 0 && ageNumber <= 130
  const ready = !busy && config.uhid_ready && name.trim() !== '' && sex !== '' && ageOk
    && (!hospitalMode || uhid.trim() !== '')
  const reasonOk = reason.trim().length >= (config.reason_min_chars || 8)

  /* What was typed changed: the candidates were about the old details. */
  const edit = (fn: () => void) => {
    fn()
    setRefusal(null)
    setError('')
  }

  const input = (): NewPatientInput => ({
    name: name.trim(),
    sex,
    ...(ageMode === 'dob' ? { date_of_birth: dob } : { age_years: ageNumber }),
    ...(phone.trim() ? { phone: phone.trim() } : {}),
    ...(hospitalMode && uhid.trim() ? { uhid: uhid.trim() } : {}),
    ...(config.abdm_enabled && abha.trim() ? { abha_number: abha.trim() } : {}),
  })

  const submit = async (withReview: boolean) => {
    if (!ready || (withReview && (!refusal?.overridable || !reasonOk))) return
    setBusy(true)
    setError('')
    const body = input()
    if (withReview && refusal) {
      body.duplicate_review = {
        decision: 'register_new',
        candidate_ids: refusal.candidates.map(c => c.patient_id),
        reason: reason.trim(),
      }
    }
    try {
      const result = await registerNewPatient(body, requestKey)
      const registered = result.patient
      const digits = phone.replace(/\D/g, '')
      const stated: StatedIdentity = {
        name: body.name,
        ...(ageMode === 'dob' ? { date_of_birth: dob } : { age_years: ageNumber }),
        ...(registered.uhid ? { uhid: registered.uhid }
          : digits.length >= 4 ? { phone_last4: digits.slice(-4) } : {}),
      }
      onRegistered(
        { patient_id: registered.patient_id, name: body.name, uhid: registered.uhid },
        stated,
        ageMode === 'age' ? NO_DOB_HINT : undefined,
      )
    } catch (err) {
      if (isFeatureDisabled(err)) {
        onFeatureOff()
        return
      }
      const code = errorCode(err)
      const message = String(errorDetail(err)?.message ?? errorText(err))
      if (code === 'duplicate_review_required' || code === 'duplicate_review_stale') {
        setRefusal({ code, message, candidates: duplicateCandidates(err), overridable: true })
      } else if (code === 'probable_duplicate' || code === 'existing_record'
        || code === 'duplicate_check_incomplete') {
        setRefusal({ code, message, candidates: duplicateCandidates(err), overridable: false })
      } else {
        setError(message)
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <form
      className="rpv-card"
      aria-label="Register a new patient"
      onSubmit={e => { e.preventDefault(); void submit(false) }}
    >
      <h3 className="rpv-card__title">Register a new patient</h3>
      {!config.uhid_ready && (
        <p className="rpv-error" role="alert">
          Patients cannot be registered yet: the hospital number setting on this site needs an
          administrator.
        </p>
      )}

      <div className="rpv-field-row">
        <div className="rpv-field">
          <label htmlFor="rpv-new-name">Full name</label>
          <input id="rpv-new-name" type="text" value={name} autoComplete="off" disabled={busy}
            onChange={e => edit(() => setName(e.target.value))} />
        </div>
        <div className="rpv-field">
          <label htmlFor="rpv-new-sex">Sex</label>
          <select id="rpv-new-sex" value={sex} disabled={busy}
            onChange={e => edit(() => setSex(e.target.value))}>
            <option value="">Choose…</option>
            <option value="F">Female</option>
            <option value="M">Male</option>
            <option value="O">Other</option>
          </select>
        </div>
      </div>

      <fieldset className="rpv-field">
        <legend>Date of birth, or the age the patient states</legend>
        <div className="rpv-choice">
          <label>
            <input type="radio" name="rpv-new-age" checked={ageMode === 'dob'} disabled={busy}
              onChange={() => edit(() => setAgeMode('dob'))} />
            Date of birth
          </label>
          <label>
            <input type="radio" name="rpv-new-age" checked={ageMode === 'age'} disabled={busy}
              onChange={() => edit(() => setAgeMode('age'))} />
            Stated age
          </label>
        </div>
        {ageMode === 'dob' ? (
          <input aria-label="Their date of birth" type="date" value={dob} disabled={busy}
            onChange={e => edit(() => setDob(e.target.value))} />
        ) : (
          <>
            <input aria-label="Stated age in years" type="number" min={0} max={130}
              inputMode="numeric" value={age} disabled={busy}
              onChange={e => edit(() => setAge(e.target.value))} />
            <p className="rpv-hint">{NO_DOB_HINT} Ask again for a date of birth when you can.</p>
          </>
        )}
      </fieldset>

      <div className="rpv-field-row">
        <div className="rpv-field">
          <label htmlFor="rpv-new-phone">Phone</label>
          <input id="rpv-new-phone" type="tel" value={phone} autoComplete="off" disabled={busy}
            onChange={e => edit(() => setPhone(e.target.value))} />
        </div>
        {hospitalMode ? (
          <div className="rpv-field">
            <label htmlFor="rpv-new-uhid">Hospital number (UHID)</label>
            <input id="rpv-new-uhid" type="text" value={uhid} autoComplete="off" disabled={busy}
              onChange={e => edit(() => setUhid(e.target.value))} />
          </div>
        ) : (
          <p className="rpv-hint">Orb issues the UHID when you register the patient.</p>
        )}
      </div>

      {config.abdm_enabled && (
        <div className="rpv-field">
          <label htmlFor="rpv-new-abha">ABHA number (unverified)</label>
          <input id="rpv-new-abha" type="text" value={abha} autoComplete="off" disabled={busy}
            onChange={e => edit(() => setAbha(e.target.value))} />
          <p className="rpv-hint">
            Recorded as unverified. {config.abha_notice}
          </p>
        </div>
      )}

      <p className="rpv-hint">
        Allergies are not recorded here. The doctor asks about them at the consultation.
      </p>

      {refusal && (
        <div className="rpv" role="alert" aria-label="Patients who may already be registered">
          <p className={refusal.overridable ? 'rpv-warn' : 'rpv-error'}>{refusal.message}</p>
          {refusal.candidates.length > 0 && (
            <ul className="rpv-results" aria-label="Possible existing records">
              {refusal.candidates.map(c => (
                <li key={c.patient_id} className="rpv-result">
                  <div className="rpv-result__who">
                    <span className="rpv-result__name">{c.name}</span>
                    <span className="rpv-result__meta">{cardMeta(c)}</span>
                    <span className="rpv-result__reasons">
                      {BAND_LABEL[c.band] ?? c.band}
                      {c.reason_labels.length ? `: ${c.reason_labels.join(', ')}` : ''}
                    </span>
                  </div>
                  <button type="button" className="rpv-btn" disabled={busy}
                    onClick={() => onChooseExisting(c)}
                    aria-label={`${c.name} is the patient`}>
                    This is the patient
                  </button>
                </li>
              ))}
            </ul>
          )}
          {refusal.overridable && (
            <>
              <div className="rpv-field">
                <label htmlFor="rpv-new-reason">
                  Why is this a different person? (at least {config.reason_min_chars || 8} characters)
                </label>
                <textarea id="rpv-new-reason" rows={2} value={reason} disabled={busy}
                  onChange={e => setReason(e.target.value)} />
              </div>
              <div className="rpv-actions">
                <button type="button" className="rpv-btn rpv-btn--primary"
                  disabled={!ready || !reasonOk} onClick={() => { void submit(true) }}>
                  {busy ? 'Registering…' : 'Register as a new patient'}
                </button>
              </div>
            </>
          )}
        </div>
      )}

      {error && <p className="rpv-error" role="alert">{error}</p>}

      <div className="rpv-actions">
        <button type="button" className="rpv-btn rpv-btn--quiet" onClick={onCancel} disabled={busy}>
          Back to search
        </button>
        {!refusal && (
          <button type="submit" className="rpv-btn rpv-btn--primary" disabled={!ready}>
            {busy ? 'Registering…' : 'Register patient'}
          </button>
        )}
      </div>
    </form>
  )
}

// ── The visit ────────────────────────────────────────────────────────────────

function VisitForm({
  patient,
  stated,
  source,
  note,
  doctors,
  scopedDepartment,
  defaultDoctorId,
  onDone,
  onRecheck,
  onCancel,
}: {
  patient: ChosenPatient
  stated: StatedIdentity
  source: 'search' | 'new'
  note?: string
  doctors: ConsultingDoctor[]
  scopedDepartment: string | null
  defaultDoctorId?: string | null
  onDone: (visit: RegisteredVisit) => void
  onRecheck?: () => void
  onCancel: () => void
}) {
  const mustChooseClinic = scopedDepartment === null
  const [department, setDepartment] = useState('')
  const [doctorId, setDoctorId] = useState(defaultDoctorId ?? '')
  const [visitType, setVisitType] = useState<OpdVisitType>('new')
  const [complaint, setComplaint] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [mismatch, setMismatch] = useState(false)
  const [requestKey] = useState(() => newRequestKey('opd'))

  const clinics = useMemo(() => {
    const names = new Set<string>()
    for (const d of doctors) if (d.department) names.add(d.department)
    return [...names].sort((a, b) => a.localeCompare(b))
  }, [doctors])
  const clinic = mustChooseClinic ? department : scopedDepartment
  const clinicDoctors = clinic ? doctors.filter(d => sameDepartment(d.department, clinic)) : []
  const chosenDoctor = clinicDoctors.some(d => d.id === doctorId) ? doctorId : ''
  const ready = !busy && (!mustChooseClinic || department !== '')

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    if (!ready) return
    setBusy(true)
    setError('')
    setMismatch(false)
    try {
      const visit = await registerVisitWithIdentity({
        patientId: patient.patient_id,
        department: mustChooseClinic ? department : undefined,
        doctorId: chosenDoctor || undefined,
        visitType,
        chiefComplaint: complaint.trim() || undefined,
        identity: stated,
        idempotencyKey: requestKey,
      })
      onDone(visit)
    } catch (err) {
      setMismatch(errorCode(err) === 'identity_not_confirmed')
      setError(String(errorDetail(err)?.message ?? errorText(err)))
      setBusy(false)
    }
  }

  return (
    <form className="rpv-card" aria-label="Register the visit" onSubmit={submit}>
      <h3 className="rpv-card__title">Visit for {patient.name}</h3>
      <p className="rpv-ok" role="status">
        {source === 'new'
          ? `Registered${patient.uhid ? `, UHID ${patient.uhid}` : ''}.`
          : `Identity confirmed${patient.uhid ? ` for UHID ${patient.uhid}` : ''}.`}
      </p>
      {note && <p className="rpv-hint">{note}</p>}

      <div className="rpv-field-row">
        {mustChooseClinic && (
          <div className="rpv-field">
            <label htmlFor="rpv-visit-clinic">Clinic</label>
            <select id="rpv-visit-clinic" value={department} disabled={busy}
              onChange={e => setDepartment(e.target.value)}>
              <option value="">Choose a clinic…</option>
              {clinics.map(c => <option key={c} value={c}>{c}</option>)}
            </select>
          </div>
        )}
        <div className="rpv-field">
          <label htmlFor="rpv-visit-doctor">Consulting doctor</label>
          <select id="rpv-visit-doctor" value={chosenDoctor} disabled={busy}
            onChange={e => setDoctorId(e.target.value)}>
            <option value="">{clinic ? 'Unassigned' : 'Choose a clinic first'}</option>
            {clinicDoctors.map(d => <option key={d.id} value={d.id}>{d.name}</option>)}
          </select>
        </div>
        <div className="rpv-field">
          <label htmlFor="rpv-visit-type">Visit type</label>
          <select id="rpv-visit-type" value={visitType} disabled={busy}
            onChange={e => setVisitType(e.target.value as OpdVisitType)}>
            <option value="new">New case</option>
            <option value="follow_up">Follow-up</option>
            <option value="review">Review</option>
          </select>
        </div>
      </div>
      <div className="rpv-field">
        <label htmlFor="rpv-visit-complaint">Chief complaint</label>
        <input id="rpv-visit-complaint" type="text" value={complaint} disabled={busy}
          placeholder="For example, cough for a week" onChange={e => setComplaint(e.target.value)} />
      </div>

      {error && <p className="rpv-error" role="alert">{error}</p>}

      <div className="rpv-actions">
        <button type="button" className="rpv-btn rpv-btn--quiet" onClick={onCancel} disabled={busy}>
          Start again
        </button>
        {mismatch && onRecheck && (
          <button type="button" className="rpv-btn" onClick={onRecheck} disabled={busy}>
            Check identity again
          </button>
        )}
        <button type="submit" className="rpv-btn rpv-btn--primary" disabled={!ready}>
          {busy ? 'Registering…' : 'Register visit and issue token'}
        </button>
      </div>
    </form>
  )
}

// ── The view ─────────────────────────────────────────────────────────────────

export default function RegisterPatientView({
  scopedDepartment,
  openView,
  onFeatureOff,
  defaultDoctorId = null,
  onVisitRegistered,
  context = 'desk',
  holds = roleHolds,
}: RegisterPatientViewProps) {
  const [step, setStep] = useState<Step>({ kind: 'find' })
  const [config, setConfig] = useState<RegistrationConfig | null>(null)
  const [configError, setConfigError] = useState('')
  const [doctors, setDoctors] = useState<ConsultingDoctor[]>([])
  /* opd-6b: the token slip, the UHID card and the registration details of
     the patient whose visit was just registered. */
  const [printing, setPrinting] = useState<{ kind: 'slip' } | { kind: 'card'; card: UhidCard } | null>(null)
  const [printError, setPrintError] = useState('')
  const [detailsOpen, setDetailsOpen] = useState(false)
  const canRegisterNew = holds(REGISTRATION_WRITE)

  /* The card's contents are read from the server, which audits the read as a
     card print. */
  const printCard = async (patientId: string) => {
    setPrintError('')
    try {
      const card = await fetchUhidCard(patientId)
      if (card?.patient_id !== patientId) {
        setPrintError('The card that came back is not this patient’s. Nothing was printed.')
        return
      }
      setPrinting({ kind: 'card', card })
    } catch (err) {
      setPrintError(String(errorDetail(err)?.message ?? errorText(err)))
    }
  }

  const loadSettings = useCallback(async () => {
    setConfigError('')
    try {
      const [cfg, roster] = await Promise.all([fetchRegistrationConfig(), fetchConsultingDoctors()])
      setConfig(cfg)
      setDoctors(Array.isArray(roster.doctors) ? roster.doctors : [])
    } catch (err) {
      if (isFeatureDisabled(err)) {
        onFeatureOff()
        return
      }
      setConfigError(errorText(err))
    }
  }, [onFeatureOff])

  // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch trigger: the setState is the loader's own result, not state derived from props.
  useEffect(() => { void loadSettings() }, [loadSettings])

  const reset = useCallback(() => {
    setStep({ kind: 'find' })
    setPrinting(null)
    setPrintError('')
    setDetailsOpen(false)
  }, [])
  const toVisit = (patient: ChosenPatient, stated: StatedIdentity, source: 'search' | 'new',
    card?: IndexCard, note?: string) => setStep({ kind: 'visit', patient, stated, source, card, note })

  const title = context === 'opd' ? 'Add a walk-in' : 'Register a visit'

  return (
    <div className="rpv">
      <div className="rpv-card">
        <h3 className="rpv-card__title">{title}</h3>
        <p className="rpv-lede">
          Search the patient index first. A returning patient confirms who they are before the
          visit is attached to their record; a new patient is registered once.
        </p>
        {configError && (
          <div className="rpv-actions">
            <p className="rpv-error" role="alert">{configError}</p>
            <button type="button" className="rpv-btn" onClick={() => { void loadSettings() }}>
              Try again
            </button>
          </div>
        )}
      </div>

      {(step.kind === 'find' || step.kind === 'confirm') && (
        <PatientIndexSearch
          minChars={config?.search_min_chars ?? 3}
          onChoose={card => setStep({ kind: 'confirm', card })}
          onRegisterNew={canRegisterNew && config ? typed => setStep({ kind: 'new', typed }) : undefined}
          onFeatureOff={onFeatureOff}
        />
      )}

      {step.kind === 'confirm' && (
        <IdentityConfirmDialog
          patient={step.card}
          onClose={reset}
          onConfirmed={stated => toVisit(
            { patient_id: step.card.patient_id, name: step.card.name, uhid: step.card.uhid },
            stated, 'search', step.card)}
        />
      )}

      {step.kind === 'new' && config && (
        <NewPatientForm
          config={config}
          initialName={nameFrom(step.typed)}
          onCancel={reset}
          onFeatureOff={onFeatureOff}
          onChooseExisting={card => setStep({ kind: 'confirm', card })}
          onRegistered={(patient, stated, note) => toVisit(patient, stated, 'new', undefined, note)}
        />
      )}

      {step.kind === 'visit' && (
        <VisitForm
          key={`${step.patient.patient_id}-${step.source}`}
          patient={step.patient}
          stated={step.stated}
          source={step.source}
          note={step.note}
          doctors={doctors}
          scopedDepartment={scopedDepartment}
          defaultDoctorId={defaultDoctorId}
          onCancel={reset}
          onRecheck={step.card ? () => setStep({ kind: 'confirm', card: step.card as IndexCard }) : undefined}
          onDone={visit => {
            setStep({ kind: 'done', visit, patient: step.patient })
            onVisitRegistered?.(visit)
          }}
        />
      )}

      {step.kind === 'done' && (
        <div className="rpv-card" role="status" aria-label="Visit registered">
          <h3 className="rpv-card__title">Visit registered</h3>
          <span className="rpv-token">Token {step.visit.token_number}</span>
          <p className="rpv-lede">
            {step.patient.name}
            {step.patient.uhid ? ` (UHID ${step.patient.uhid})` : ''} is in the queue for{' '}
            {step.visit.doctor_name ?? 'the next free doctor'} in {step.visit.department}.
          </p>
          <div className="rpv-actions">
            <button type="button" className="rpv-btn" onClick={() => setPrinting({ kind: 'slip' })}>
              Print token slip
            </button>
            {step.patient.uhid && (
              <button type="button" className="rpv-btn" onClick={() => { void printCard(step.patient.patient_id) }}>
                Print UHID card
              </button>
            )}
            <button type="button" className="rpv-btn" onClick={() => setDetailsOpen(true)}>
              Registration details
            </button>
          </div>
          {printError && <p className="rpv-error" role="alert">{printError}</p>}
          <div className="rpv-actions">
            {context === 'desk' && (
              <button type="button" className="rpv-btn" onClick={() => openView('opd')}>
                Open the OPD queue
              </button>
            )}
            <button type="button" className="rpv-btn rpv-btn--primary" onClick={reset}>
              Register another patient
            </button>
          </div>
        </div>
      )}

      {step.kind === 'done' && detailsOpen && (
        <DemographicsDrawer
          patientId={step.patient.patient_id}
          patientName={step.patient.name}
          holds={holds}
          onClose={() => setDetailsOpen(false)}
        />
      )}
      {step.kind === 'done' && printing?.kind === 'slip' && createPortal(
        <OpdTokenSlipPrint visit={step.visit} onAfterPrint={() => setPrinting(null)} />,
        document.body,
      )}
      {printing?.kind === 'card' && createPortal(
        <UhidCardPrint card={printing.card} onAfterPrint={() => setPrinting(null)} />,
        document.body,
      )}
    </div>
  )
}
