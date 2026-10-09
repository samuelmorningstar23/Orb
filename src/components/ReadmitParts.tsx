/**
 * The admission form's parts for readmitting a discharged patient onto their
 * own record (opd-17c). AdmitPatientModal shows them when the server says a
 * new admission matches a record that may be readmitted, and on the result
 * screen of any readmission.
 *
 * What they never do, by ruling:
 *   * OPD-S1 (a): who the patient is comes from the record and cannot be
 *     edited; the check is what the patient STATES, typed here and never
 *     filled in from the record. No date of birth is ever shown.
 *   * OPD-S8 (a): the earlier stay's medicines are listed only after the
 *     readmission stopped them, for the prescriber to prescribe again.
 *   * ADT-S9 (a): nothing here shows the earlier stay's observations.
 */
import type { StatedIdentityInput } from '../api/admitExisting'
import {
  previousStayMedicineWords,
  type ReadmissionFields,
  type ReadmitCandidate,
} from '../api/readmission'
import { formatClinicalDateKey, formatClinicalDateTime } from '../lib/datetime'
import './AdmitExistingParts.css'
import './ReadmitParts.css'

function sexWord(sex: string | null): string | null {
  if (!sex) return null
  const s = sex.trim().toUpperCase()
  if (s === 'M' || s === 'MALE') return 'Male'
  if (s === 'F' || s === 'FEMALE') return 'Female'
  if (s === 'O' || s === 'OTHER') return 'Other'
  return sex
}

/** "65 y, Female, UHID 12-3456". */
function candidateLine(c: ReadmitCandidate): string {
  return [
    c.age != null ? `${c.age} y` : null,
    sexWord(c.sex),
    c.uhid ? `UHID ${c.uhid}` : null,
  ].filter(Boolean).join(', ')
}

/** The records the refusal says may be readmitted, each with its button. */
export function ReadmitOffer({ candidates, onChoose }: {
  candidates: ReadmitCandidate[]
  onChoose: (c: ReadmitCandidate) => void
}) {
  if (candidates.length === 0) return null
  return (
    <section className="admit-existing__card" aria-label="Readmit onto an existing record">
      <h3 className="admit-existing__heading">This patient may already have a record</h3>
      <p className="admit-existing__hint">
        If this is a patient who was an inpatient here before, readmit them onto their own
        record. You confirm who they are on the next step.
      </p>
      <ul className="admit-existing__list readmit-offer__list">
        {candidates.map(c => (
          <li key={c.patient_id} className="readmit-offer__item">
            <span>
              <strong>{c.name}</strong>
              {candidateLine(c) ? `, ${candidateLine(c)}` : ''}
              {c.last_visit_date ? `, last seen ${formatClinicalDateKey(c.last_visit_date)}` : ''}
            </span>
            <button type="button" className="admit-btn readmit__action" onClick={() => onChoose(c)}>
              Readmit this patient
            </button>
          </li>
        ))}
      </ul>
    </section>
  )
}

/** The chosen record, read-only, and what the patient states. */
export function ReadmitIdentityPanel({ candidate, stated, onChange, onCancel }: {
  candidate: ReadmitCandidate
  stated: StatedIdentityInput
  onChange: (next: StatedIdentityInput) => void
  onCancel: () => void
}) {
  const set = (patch: Partial<StatedIdentityInput>) => onChange({ ...stated, ...patch })
  return (
    <div className="admit-modal__section">
      <section className="admit-existing__card" aria-label="Patient on file">
        <h3 className="admit-existing__name">{candidate.name}</h3>
        <p className="admit-existing__line">{candidateLine(candidate)}</p>
        <p className="admit-existing__hint">
          The patient is readmitted onto this record and keeps this hospital number. Any
          medicine still on the chart from the earlier stay is stopped and listed for you to
          prescribe again. The earlier stay&rsquo;s observations are not current: the ward
          charts a fresh set.
        </p>
        <div>
          <button type="button" className="admit-btn readmit__action" onClick={onCancel}>
            Not this patient
          </button>
        </div>
      </section>

      <fieldset className="admit-existing__check">
        <legend>Confirm who the patient is</legend>
        <p className="admit-existing__hint">
          Ask the patient and type what they say. It is checked against the record when you
          admit; the record&rsquo;s own details are not shown.
        </p>
        <div className="admit-modal__grid">
          <div className="admit-form-group" style={{ gridColumn: '1 / -1' }}>
            <label htmlFor="readmit-name">Name, as the patient says it <span>*</span></label>
            <input id="readmit-name" type="text" className="admit-input" autoComplete="off"
              value={stated.name} onChange={e => set({ name: e.target.value })} />
          </div>
          <div className="admit-form-group">
            <label htmlFor="readmit-age-mode">They give</label>
            <select id="readmit-age-mode" className="admit-input" value={stated.ageMode}
              onChange={e => set({ ageMode: e.target.value as StatedIdentityInput['ageMode'] })}>
              <option value="dob">Date of birth</option>
              <option value="age">Age</option>
            </select>
          </div>
          {stated.ageMode === 'dob' ? (
            <div className="admit-form-group">
              <label htmlFor="readmit-dob">Date of birth <span>*</span></label>
              <input id="readmit-dob" type="date" className="admit-input"
                value={stated.dob} onChange={e => set({ dob: e.target.value })} />
            </div>
          ) : (
            <div className="admit-form-group">
              <label htmlFor="readmit-age">Age in years <span>*</span></label>
              <input id="readmit-age" type="number" min={0} max={130} step={1}
                className="admit-input" value={stated.age} onChange={e => set({ age: e.target.value })} />
            </div>
          )}
          <div className="admit-form-group">
            <label htmlFor="readmit-second-mode">And</label>
            <select id="readmit-second-mode" className="admit-input" value={stated.secondMode}
              onChange={e => set({ secondMode: e.target.value as StatedIdentityInput['secondMode'] })}>
              <option value="phone">Last 4 digits of their phone</option>
              <option value="uhid">Their UHID</option>
            </select>
          </div>
          {stated.secondMode === 'phone' ? (
            <div className="admit-form-group">
              <label htmlFor="readmit-last4">Last 4 digits <span>*</span></label>
              <input id="readmit-last4" type="text" inputMode="numeric" maxLength={4}
                className="admit-input" autoComplete="off" value={stated.last4}
                onChange={e => set({ last4: e.target.value })} />
            </div>
          ) : (
            <div className="admit-form-group">
              <label htmlFor="readmit-uhid">UHID <span>*</span></label>
              <input id="readmit-uhid" type="text" className="admit-input" autoComplete="off"
                value={stated.uhid} onChange={e => set({ uhid: e.target.value })} />
            </div>
          )}
        </div>
      </fieldset>
    </div>
  )
}

/** For a clinic visit whose patient was an inpatient before: what admitting
 *  them does, in the server's words. */
export function ReadmissionNote({ note }: { note: string }) {
  return (
    <section className="admit-existing__card" aria-label="Readmission">
      <h3 className="admit-existing__heading">Readmission onto the same record</h3>
      <p className="admit-existing__line">{note}</p>
    </section>
  )
}

/** On the result screen: the earlier stay's medicines the readmission
 *  stopped, for the prescriber to prescribe again one by one. */
export function StoppedMedicinesNote({ result }: { result: ReadmissionFields }) {
  if (!result.readmission) return null
  const count = result.previous_stay_medicines_stopped ?? 0
  const listed = result.previous_stay_medicines ?? []
  return (
    <section className="admit-existing__card" aria-label="Medicines from the previous stay">
      <h3 className="admit-existing__heading">Medicines from the previous stay</h3>
      {count === 0 ? (
        <p className="admit-existing__line">No medicine was still on the chart from the earlier stay.</p>
      ) : result.previous_stay_medicines_listed ? (
        <>
          <p className="admit-existing__hint">
            These were stopped when the patient was readmitted. Prescribe each one again if the
            patient needs it.
          </p>
          <ul className="admit-existing__list">
            {listed.map(m => (
              <li key={m.medication_id}>
                {previousStayMedicineWords(m)}
                {m.started_at ? `, started ${formatClinicalDateTime(m.started_at)}` : ''}
                {m.was_on_hold ? ', was on hold' : ''}
              </li>
            ))}
          </ul>
        </>
      ) : (
        <p className="admit-existing__line">
          {count === 1 ? '1 medicine' : `${count} medicines`} from the earlier stay were stopped.
          The treating team sees the list in the patient&rsquo;s case room and prescribes each
          one again if it is needed.
        </p>
      )}
      <p className="admit-existing__hint">
        The earlier stay&rsquo;s observations are not current. Chart a fresh set when the
        patient arrives.
      </p>
    </section>
  )
}
