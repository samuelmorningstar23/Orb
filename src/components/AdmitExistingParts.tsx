/**
 * The admission form's parts for a patient already on file (opd-17a): a
 * registered outpatient admitted from their clinic visit onto their own
 * record. AdmitPatientModal shows these in place of the new-patient fields
 * when it is opened with `existing`.
 *
 * What they never do, by ruling:
 *   * OPD-S1 (a): who the patient is comes from the record and cannot be
 *     edited, and the check is what the patient STATES, typed here and never
 *     filled in from the record. The stored date of birth is never shown.
 *   * OPD-S3 (a): the clinic's observations are listed with their time for
 *     reference and are not copied to the observation chart; there is nothing
 *     to type, because the ward charts a fresh set. No score, band or colour.
 *   * OPD-S4 (a): the patient's outpatient medicines are listed with their
 *     times for the admitting prescriber to reconcile one by one; none is
 *     carried onto the inpatient chart.
 */
import type {
  AdmittableConversionPreview,
  ConversionRefusal,
  ConversionVisitReference,
  StatedIdentityInput,
} from '../api/admitExisting'
import { formatClinicalDateKey, formatClinicalDateTime } from '../lib/datetime'
import { opdVitalsLine } from '../pages/panels/opd/opdVitals'
import './AdmitExistingParts.css'

function sexWord(sex: string | null): string | null {
  if (!sex) return null
  const s = sex.trim().toUpperCase()
  if (s === 'M') return 'Male'
  if (s === 'F') return 'Female'
  if (s === 'O') return 'Other'
  return sex
}

/** "58 y, Female, UHID 12-3456". */
function recordLine(patient: AdmittableConversionPreview['patient']): string {
  return [
    patient.age != null ? `${patient.age} y` : null,
    sexWord(patient.sex),
    patient.uhid ? `UHID ${patient.uhid}` : null,
  ].filter(Boolean).join(', ')
}

export function ConversionRefusalNote({ refusal }: { refusal: ConversionRefusal }) {
  return (
    <p role="alert" className="admit-modal__error admit-existing__refusal">
      {refusal.message}
    </p>
  )
}

/** A visit the patient cannot be admitted from: the server sends the visit and
 *  why, and nothing about the patient, so nothing about them is shown. */
export function RefusedVisitPanel({ visit, refusal }: {
  visit: ConversionVisitReference
  refusal: ConversionRefusal
}) {
  return (
    <div className="admit-modal__section">
      <ConversionRefusalNote refusal={refusal} />
      <p className="admit-existing__line">
        Visit to the {visit.department} clinic, token {visit.token_number}, of {formatClinicalDateKey(visit.visit_date)}.
      </p>
    </div>
  )
}

/** The record's identity, read-only, and what the patient states. */
export function ExistingIdentityPanel({
  preview,
  stated,
  onChange,
}: {
  preview: AdmittableConversionPreview
  stated: StatedIdentityInput
  onChange: (next: StatedIdentityInput) => void
}) {
  const set = (patch: Partial<StatedIdentityInput>) => onChange({ ...stated, ...patch })
  const p = preview.patient
  const v = preview.visit
  return (
    <div className="admit-modal__section">
      <section className="admit-existing__card" aria-label="Patient on file">
        <h3 className="admit-existing__name">{p.name}</h3>
        <p className="admit-existing__line">{recordLine(p)}</p>
        <p className="admit-existing__line">
          From the {v.department} clinic, token {v.token_number}, visit of {formatClinicalDateKey(v.visit_date)}
          {v.doctor_name ? `, ${v.doctor_name}` : ''}.
        </p>
        <p className="admit-existing__hint">
          The patient is admitted onto this record and keeps this hospital number. Name, age and
          sex are not changed here: correct them through registration details.
        </p>
      </section>

      <fieldset className="admit-existing__check">
        <legend>Confirm who the patient is</legend>
        <p className="admit-existing__hint">
          Ask the patient and type what they say. It is checked against the record when you
          admit; the record&rsquo;s own details are not shown.
        </p>
        <div className="admit-modal__grid">
          <div className="admit-form-group" style={{ gridColumn: '1 / -1' }}>
            <label htmlFor="admit-existing-name">Name, as the patient says it <span>*</span></label>
            <input id="admit-existing-name" type="text" className="admit-input" autoComplete="off"
              value={stated.name} onChange={e => set({ name: e.target.value })} />
          </div>
          <div className="admit-form-group">
            <label htmlFor="admit-existing-age-mode">They give</label>
            <select id="admit-existing-age-mode" className="admit-input" value={stated.ageMode}
              onChange={e => set({ ageMode: e.target.value as StatedIdentityInput['ageMode'] })}>
              <option value="dob">Date of birth</option>
              <option value="age">Age</option>
            </select>
          </div>
          {stated.ageMode === 'dob' ? (
            <div className="admit-form-group">
              <label htmlFor="admit-existing-dob">Date of birth <span>*</span></label>
              <input id="admit-existing-dob" type="date" className="admit-input"
                value={stated.dob} onChange={e => set({ dob: e.target.value })} />
            </div>
          ) : (
            <div className="admit-form-group">
              <label htmlFor="admit-existing-age">Age in years <span>*</span></label>
              <input id="admit-existing-age" type="number" min={0} max={130} step={1}
                className="admit-input" value={stated.age} onChange={e => set({ age: e.target.value })} />
            </div>
          )}
          <div className="admit-form-group">
            <label htmlFor="admit-existing-second-mode">And</label>
            <select id="admit-existing-second-mode" className="admit-input" value={stated.secondMode}
              onChange={e => set({ secondMode: e.target.value as StatedIdentityInput['secondMode'] })}>
              <option value="phone">Last 4 digits of their phone</option>
              <option value="uhid">Their UHID</option>
            </select>
          </div>
          {stated.secondMode === 'phone' ? (
            <div className="admit-form-group">
              <label htmlFor="admit-existing-last4">Last 4 digits <span>*</span></label>
              <input id="admit-existing-last4" type="text" inputMode="numeric" maxLength={4}
                className="admit-input" autoComplete="off" value={stated.last4}
                onChange={e => set({ last4: e.target.value })} />
            </div>
          ) : (
            <div className="admit-form-group">
              <label htmlFor="admit-existing-uhid">UHID <span>*</span></label>
              <input id="admit-existing-uhid" type="text" className="admit-input" autoComplete="off"
                value={stated.uhid} onChange={e => set({ uhid: e.target.value })} />
            </div>
          )}
        </div>
      </fieldset>
    </div>
  )
}

/** The clinic's observations, with their time, for reference only (OPD-S3). */
export function OpdObservationsReference({ preview }: { preview: AdmittableConversionPreview }) {
  return (
    <div className="admit-modal__section">
      <h3 className="admit-existing__heading">Observations from the clinic, for reference</h3>
      <p className="admit-existing__hint">
        These are not copied to the observation chart. The ward charts a fresh set of
        observations when the patient arrives.
      </p>
      {preview.opd_vitals.length === 0 ? (
        <p className="admit-existing__line">No observations were recorded at this visit.</p>
      ) : (
        <ul className="admit-existing__list">
          {preview.opd_vitals.map((v, i) => (
            <li key={`${v.measured_at ?? 'untimed'}-${i}`}>
              Measured {v.measured_at ? formatClinicalDateTime(v.measured_at) : 'at an unrecorded time'}
              {v.measured_by_name ? ` by ${v.measured_by_name}` : ''}: {opdVitalsLine(v.values)}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

/** The patient's outpatient medicines, with their times (OPD-S4). */
export function OpdMedicinesReference({ preview }: { preview: AdmittableConversionPreview }) {
  if (!preview.medicines_listed) return null
  return (
    <div className="admit-form-group" style={{ gridColumn: '1 / -1' }}>
      <h3 className="admit-existing__heading">Outpatient medicines, for reference</h3>
      <p className="admit-existing__hint">
        None of these is carried onto the inpatient medication chart. Prescribe each one again
        if the patient needs it on the ward.
      </p>
      {preview.opd_medicines.length === 0 ? (
        <p className="admit-existing__line">No outpatient medicines are on record.</p>
      ) : (
        <ul className="admit-existing__list">
          {preview.opd_medicines.map((m, i) => (
            <li key={`${m.drug_name}-${i}`}>
              {[m.drug_name, m.dose, m.route, m.is_prn ? 'when needed' : m.frequency]
                .filter(Boolean).join(' ')}
              {m.prescribed_at ? `, prescribed ${formatClinicalDateTime(m.prescribed_at)}` : ''}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

/** The allergies already on the chart, which stay as they are. */
export function ChartedAllergiesNote({ preview }: { preview: AdmittableConversionPreview }) {
  return (
    <div className="admit-form-group" style={{ gridColumn: '1 / -1' }}>
      <h3 className="admit-existing__heading">Allergies already on the chart</h3>
      {preview.allergies.length === 0 ? (
        <p className="admit-existing__line">
          None recorded. This is not the same as no known allergies: ask the patient.
        </p>
      ) : (
        <ul className="admit-existing__list">
          {preview.allergies.map(a => (
            <li key={a.substance}>
              {a.substance}
              {a.reaction ? `: ${a.reaction}` : ''}
              {a.severity && a.severity !== 'unknown' ? ` (${a.severity})` : ''}
            </li>
          ))}
        </ul>
      )}
      <p className="admit-existing__hint">They stay on the chart. Add any new ones below.</p>
    </div>
  )
}
