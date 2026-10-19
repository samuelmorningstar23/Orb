/**
 * Bed board tile action: Record admission assessment (adt-17b, ruling ADT-S5
 * (a), a build default for Samuel to confirm).
 *
 * A ward clerk's admission stores every clinical field as not yet assessed
 * and records no allergy, so the bed board flags 'Admission assessment due'
 * on the bed. A doctor clears it here: what the patient said about allergies
 * (allergies named, no known allergies, or why they could not be asked), the
 * presenting complaint, the diagnosis and the code status, in one write on
 * the server with an audit row. 'No known allergies' is only ever recorded
 * here, by a doctor who asked; the server refuses it while an allergy is on
 * the chart.
 *
 * Offered only on a tile the board marks 'Admission assessment due'
 * (actionMarker), with a patient the doctor may see (actionNeedsOccupant), to
 * a role holding patient.write (the doctors). The server checks all of it
 * again. ONE SCREEN PER STAY: keyed by the admission.
 */
import { useId, useState } from 'react'

import {
  ALLERGY_STATUS_WORDS,
  ASSESSMENT_DUE_MARKER,
  CODE_STATUS_WORDS,
  clerkFailureWords,
  recordAdmissionAssessment,
  type AdmissionAssessmentResult,
  type AllergyStatus,
  type CodeStatus,
} from '../../../api/adt/clerk'
import { assertOnlineForWrite, isOfflineWriteRefused } from '../../../lib/offline/writeGuard'
import '../EndStay.css'
import type { TileActionProps } from './index'

export const actionId = 'admission-assessment'
export const actionLabel = 'Record admission assessment'
export const actionOrder = 15
export const actionPermission = 'patient.write'
export const actionStatuses = 'occupied,occupied_on_leave,occupied_absconded,boarder'
export const actionNeedsOccupant = 1
export const actionMarker = 'Admission assessment due'

/** Allergens typed one per line or separated by commas or semicolons. */
function allergenList(text: string): string[] {
  return text.split(/[\n,;]+/).map((s) => s.trim()).filter(Boolean)
}

export default function AssessmentAction({ tile, ward, onDone, onClose }: TileActionProps) {
  const occupant = tile.occupant
  if (!occupant || !(tile.markers ?? []).includes(ASSESSMENT_DUE_MARKER)) {
    return (
      <div className="es">
        <p className="es__note">No admission assessment is due for anyone you can see in this bed.</p>
        <div className="es__actions">
          <button type="button" className="es__btn" onClick={onClose}>Close</button>
        </div>
      </div>
    )
  }
  return (
    <AssessmentForm
      key={occupant.admission_id}
      admissionId={occupant.admission_id}
      name={occupant.name}
      mrn={occupant.mrn}
      place={`${ward.code}, bed ${tile.bed_label ?? tile.unit_code}`}
      onDone={onDone}
      onClose={onClose}
    />
  )
}

interface AssessmentFormProps {
  admissionId: string
  name: string | null
  mrn: string | null
  place: string
  onDone: () => void
  onClose: () => void
}

const ALLERGY_CHOICES: readonly AllergyStatus[] = ['allergies_recorded', 'none_known', 'unable_to_obtain']
const CODE_CHOICES: readonly CodeStatus[] = ['full_code', 'dnr', 'dni', 'comfort']

function AssessmentForm({ admissionId, name, mrn, place, onDone, onClose }: AssessmentFormProps) {
  const madeId = useId()
  const headingId = `assessment-${madeId}`
  const [status, setStatus] = useState<AllergyStatus | null>(null)
  const [allergies, setAllergies] = useState('')
  const [reason, setReason] = useState('')
  const [complaint, setComplaint] = useState('')
  const [diagnosis, setDiagnosis] = useState('')
  const [code, setCode] = useState<CodeStatus | ''>('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<AdmissionAssessmentResult | null>(null)

  const named = allergenList(allergies)
  const allergyReady = status === 'none_known'
    || (status === 'allergies_recorded' && named.length > 0)
    || (status === 'unable_to_obtain' && reason.trim().length >= 3)
  const ready = allergyReady && complaint.trim() !== '' && code !== '' && !submitting

  const submit = async () => {
    if (!ready || !status || !code) return
    setSubmitting(true)
    setError(null)
    try {
      assertOnlineForWrite('record the admission assessment')
      setResult(await recordAdmissionAssessment(admissionId, {
        allergy_status: status,
        ...(status === 'allergies_recorded' ? { allergies: named } : {}),
        ...(status === 'unable_to_obtain' ? { reason: reason.trim() } : {}),
        chief_complaint: complaint.trim(),
        ...(diagnosis.trim() ? { initial_diagnosis: diagnosis.trim() } : {}),
        code_status: code,
      }))
    } catch (err) {
      setError(isOfflineWriteRefused(err) ? err.message : clerkFailureWords(err))
    } finally {
      setSubmitting(false)
    }
  }

  if (result) {
    return (
      <section className="es" aria-labelledby={headingId} data-testid="assessment-result">
        <h2 id={headingId} className="es__title">Admission assessment recorded</h2>
        <div className="es__box es__box--ok" role="status">
          <p className="es__box-text">Allergies: {result.assessment.status_label}.</p>
          {result.charted.length > 0 && (
            <p className="es__box-text">Charted: {result.charted.join(', ')}.</p>
          )}
          <p className="es__box-text">The bed board no longer flags an admission assessment due.</p>
        </div>
        <div className="es__actions">
          <button type="button" className="es__btn es__btn--primary" onClick={onDone}>Done</button>
        </div>
      </section>
    )
  }

  return (
    <section className="es" aria-labelledby={headingId} data-testid="assessment">
      <h2 id={headingId} className="es__title">Admission assessment</h2>
      <p className="es__lead">
        The ward clerk admitted this patient with the clinical details not yet assessed. Ask about
        allergies yourself: nothing recorded so far means nobody has asked, not none.
      </p>

      <dl className="es__readback" aria-label="The patient">
        <div className="es__readback-item">
          <dt className="es__term">Name</dt>
          <dd className="es__value">{name || 'Not recorded'}</dd>
        </div>
        <div className="es__readback-item">
          <dt className="es__term">MRN</dt>
          <dd className="es__value">{mrn || 'Not on file'}</dd>
        </div>
        <div className="es__readback-item">
          <dt className="es__term">Ward and bed</dt>
          <dd className="es__value">{place}</dd>
        </div>
      </dl>

      <fieldset className="es__fieldset">
        <legend className="es__legend">Allergies</legend>
        <div className="es__choices">
          {ALLERGY_CHOICES.map((s) => (
            <label key={s} className={`es__choice${status === s ? ' es__choice--on' : ''}`}>
              <input
                type="radio"
                className="es__choice-input"
                name={`${headingId}-allergy`}
                value={s}
                checked={status === s}
                onChange={() => setStatus(s)}
                disabled={submitting}
              />
              {ALLERGY_STATUS_WORDS[s]}
            </label>
          ))}
        </div>
      </fieldset>

      {status === 'allergies_recorded' && (
        <div className="es__field">
          <label className="es__label" htmlFor={`${headingId}-allergies`}>Allergies the patient named</label>
          <textarea
            id={`${headingId}-allergies`}
            className="es__textarea"
            value={allergies}
            maxLength={2000}
            onChange={(e) => setAllergies(e.target.value)}
            disabled={submitting}
          />
          <span className="es__hint">One per line, or separated by commas.</span>
        </div>
      )}

      {status === 'unable_to_obtain' && (
        <div className="es__field">
          <label className="es__label" htmlFor={`${headingId}-reason`}>Why they could not be obtained</label>
          <textarea
            id={`${headingId}-reason`}
            className="es__textarea"
            value={reason}
            maxLength={500}
            onChange={(e) => setReason(e.target.value)}
            disabled={submitting}
          />
        </div>
      )}

      <div className="es__field">
        <label className="es__label" htmlFor={`${headingId}-complaint`}>Presenting complaint</label>
        <textarea
          id={`${headingId}-complaint`}
          className="es__textarea"
          value={complaint}
          maxLength={500}
          onChange={(e) => setComplaint(e.target.value)}
          disabled={submitting}
        />
      </div>

      <div className="es__field">
        <label className="es__label" htmlFor={`${headingId}-diagnosis`}>Diagnosis (optional)</label>
        <input
          id={`${headingId}-diagnosis`}
          className="es__input"
          value={diagnosis}
          maxLength={500}
          onChange={(e) => setDiagnosis(e.target.value)}
          disabled={submitting}
        />
      </div>

      <div className="es__field">
        <label className="es__label" htmlFor={`${headingId}-code`}>Code status</label>
        <select
          id={`${headingId}-code`}
          className="es__input"
          value={code}
          onChange={(e) => setCode(e.target.value as CodeStatus | '')}
          disabled={submitting}
        >
          <option value="">Choose</option>
          {CODE_CHOICES.map((c) => <option key={c} value={c}>{CODE_STATUS_WORDS[c]}</option>)}
        </select>
      </div>

      {error && <p className="es__error-text" role="alert">{error}</p>}

      <div className="es__actions">
        <button type="button" className="es__btn" onClick={onClose} disabled={submitting}>Cancel</button>
        <button type="button" className="es__btn es__btn--primary" onClick={() => { void submit() }}
          disabled={!ready}>
          {submitting ? 'Recording' : 'Record assessment'}
        </button>
      </div>
    </section>
  )
}
