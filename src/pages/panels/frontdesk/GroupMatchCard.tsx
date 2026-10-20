/**
 * Find a patient seen at another facility of the group (multisite-17).
 *
 * Shown on the front desk's OPD section only while the server reports
 * ORB_MULTI_SITE on and more than one facility on record, and only to a
 * central desk account (no department). The patient states four things; the
 * server compares them across the whole group and answers who matches, never
 * a clinical field. Picking a match and registering the visit here brings the
 * patient's one record into this facility's list, instead of a second record
 * that would split their allergy list. Every search is recorded with the
 * reason typed here, and searches are limited per hour.
 *
 * Quiet Clinical: the desk's own card, field and button classes
 * (FrontDeskPanel.css); no opacity, no new colour.
 */
import { useState } from 'react'
import {
  registerMatchedVisit,
  searchGroupIdentity,
  type GroupMatch,
  type GroupMatchResult,
} from '../../../api/groupMatch'
import type { ConsultingDoctor } from '../../../api/frontdesk'

const SEX_LABEL: Record<string, string> = { F: 'Female', M: 'Male', O: 'Other' }

function errorText(err: unknown): string {
  if (err instanceof Error && err.message) return err.message
  return 'Something went wrong. Nothing was saved.'
}

function sameClinic(a: string | null | undefined, b: string | null | undefined): boolean {
  return (a ?? '').trim().toLowerCase() === (b ?? '').trim().toLowerCase()
}

export interface GroupMatchCardProps {
  /** This facility's clinics (the server's transfer destinations list). */
  clinics: string[]
  /** This facility's consulting doctors (the server's roster). */
  doctors: ConsultingDoctor[]
  /** Called after a visit is registered, so the queue re-reads. */
  onRegistered: () => void
}

export default function GroupMatchCard({ clinics, doctors, onRegistered }: GroupMatchCardProps) {
  const [name, setName] = useState('')
  const [age, setAge] = useState('')
  const [dob, setDob] = useState('')
  const [sex, setSex] = useState('')
  const [phone, setPhone] = useState('')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [result, setResult] = useState<GroupMatchResult | null>(null)
  const [chosen, setChosen] = useState<GroupMatch | null>(null)
  const [clinic, setClinic] = useState('')
  const [doctorId, setDoctorId] = useState('')
  const [registered, setRegistered] = useState('')

  const ageNumber = age.trim() === '' ? null : Number(age)
  const ageOk = ageNumber === null || (Number.isInteger(ageNumber) && ageNumber >= 0 && ageNumber <= 130)
  const ready = name.trim().length >= 2 && (dob !== '' || (age.trim() !== '' && ageOk))
    && sex !== '' && phone.replace(/\D/g, '').length >= 10 && reason.trim().length >= 8

  const search = async (event: React.FormEvent) => {
    event.preventDefault()
    if (!ready || busy) return
    setBusy(true)
    setError('')
    setRegistered('')
    setChosen(null)
    try {
      const answer = await searchGroupIdentity({
        name: name.trim(),
        ageYears: dob ? null : ageNumber,
        dateOfBirth: dob || null,
        sex,
        phone,
        reason: reason.trim(),
      })
      setResult(answer)
    } catch (err) {
      setResult(null)
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  const clinicDoctors = clinic ? doctors.filter(d => sameClinic(d.department, clinic)) : []
  const chosenDoctor = clinicDoctors.some(d => d.id === doctorId) ? doctorId : ''

  const register = async () => {
    if (!chosen || !clinic || busy) return
    setBusy(true)
    setError('')
    try {
      const visit = await registerMatchedVisit({
        patientId: chosen.id,
        ticket: chosen.ticket,
        department: clinic,
        doctorId: chosenDoctor || undefined,
        stated: { name: name.trim(), ageYears: dob ? null : ageNumber, dateOfBirth: dob || null, phone },
        idempotencyKey: `group-match-${chosen.id}-${Date.now()}`,
      })
      setRegistered(`Visit registered for ${chosen.name} in ${visit.department}. Token ${visit.token_number}.`)
      setChosen(null)
      setResult(null)
      onRegistered()
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="fd-card fd-register" aria-label="Patient seen at another facility">
      <h3 className="fd-card__title">Patient seen at another facility</h3>
      <p className="fd-hint">
        If the patient says they were treated at another facility of the group, search for their
        record here instead of registering them again. Ask for all four details. Every search is
        recorded with your reason.
      </p>
      <form onSubmit={search}>
        <div className="fd-field">
          <label htmlFor="gm-name">Full name</label>
          <input id="gm-name" type="text" value={name} onChange={e => setName(e.target.value)}
            autoComplete="off" />
        </div>
        <div className="fd-field-row">
          <div className="fd-field">
            <label htmlFor="gm-age">Age in years</label>
            <input id="gm-age" type="number" inputMode="numeric" min={0} max={130} value={age}
              onChange={e => setAge(e.target.value)} disabled={dob !== ''} />
          </div>
          <div className="fd-field">
            <label htmlFor="gm-dob">Or date of birth</label>
            <input id="gm-dob" type="date" value={dob} onChange={e => setDob(e.target.value)} />
          </div>
        </div>
        <div className="fd-field-row">
          <div className="fd-field">
            <label htmlFor="gm-sex">Sex</label>
            <select id="gm-sex" value={sex} onChange={e => setSex(e.target.value)}>
              <option value="">Select…</option>
              <option value="F">Female</option>
              <option value="M">Male</option>
              <option value="O">Other</option>
            </select>
          </div>
          <div className="fd-field">
            <label htmlFor="gm-phone">Phone number</label>
            <input id="gm-phone" type="tel" inputMode="tel" value={phone}
              onChange={e => setPhone(e.target.value)} autoComplete="off" />
          </div>
        </div>
        <div className="fd-field">
          <label htmlFor="gm-reason">Why you are searching the whole group</label>
          <input id="gm-reason" type="text" value={reason} onChange={e => setReason(e.target.value)}
            placeholder="e.g. patient says they were treated at the main hospital" />
        </div>
        <div className="fd-actions">
          <button type="submit" className="fd-btn fd-btn--primary" disabled={!ready || busy}>
            {busy && !chosen ? 'Searching…' : 'Search all facilities'}
          </button>
        </div>
      </form>

      {error && <p className="fd-error" role="alert">{error}</p>}
      {registered && <p className="fd-hint" role="status">{registered}</p>}

      {result && (
        result.matches.length === 0 ? (
          <p className="fd-empty" role="status">
            No patient in the group matches all four details. Check them with the patient, or
            register them as a new patient.
          </p>
        ) : (
          <div className="fd-table-scroll">
            <table className="fd-table">
              <caption className="fd-hint">
                {result.matches.length === 1 ? 'One patient matches.' : `${result.matches.length} patients match.`}
                {result.truncated ? ' More may match: add a detail to narrow the search.' : ''}
              </caption>
              <thead>
                <tr>
                  <th scope="col">Patient</th>
                  <th scope="col">Age</th>
                  <th scope="col">Sex</th>
                  <th scope="col">Registered at</th>
                  <th scope="col"><span className="sr-only">Action</span></th>
                </tr>
              </thead>
              <tbody>
                {result.matches.map(m => (
                  <tr key={m.id}>
                    <td>
                      <span className="fd-patient-name">{m.name}</span>
                      {m.group_mrn && <span className="fd-patient-mrn"> UHID {m.group_mrn}</span>}
                    </td>
                    <td className="fd-nowrap">{m.age ?? 'Not known'}</td>
                    <td>{m.sex ? SEX_LABEL[m.sex] ?? m.sex : 'Not known'}</td>
                    <td className="fd-nowrap">{m.registered_facility.code ?? 'Not known'}</td>
                    <td className="fd-actions-cell">
                      <button type="button" className="fd-btn fd-btn--quiet"
                        onClick={() => { setChosen(m); setClinic(''); setDoctorId('') }}>
                        Register a visit here
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      )}

      {chosen && (
        <div className="fd-register" aria-label={`Register a visit for ${chosen.name}`}>
          <p className="fd-hint">
            Registering a visit here brings {chosen.name}&apos;s record into this facility&apos;s
            patient list.
          </p>
          <div className="fd-field-row">
            <div className="fd-field">
              <label htmlFor="gm-clinic">Clinic</label>
              <select id="gm-clinic" value={clinic} onChange={e => setClinic(e.target.value)}>
                <option value="">Select a clinic…</option>
                {clinics.map(c => <option key={c} value={c}>{c}</option>)}
              </select>
            </div>
            <div className="fd-field">
              <label htmlFor="gm-doctor">Consulting doctor</label>
              <select id="gm-doctor" value={chosenDoctor} onChange={e => setDoctorId(e.target.value)}>
                <option value="">{clinic ? 'Unassigned' : 'Choose a clinic first'}</option>
                {clinicDoctors.map(d => <option key={d.id} value={d.id}>{d.name}</option>)}
              </select>
            </div>
          </div>
          <div className="fd-actions">
            <button type="button" className="fd-btn fd-btn--quiet" onClick={() => setChosen(null)}
              disabled={busy}>
              Cancel
            </button>
            <button type="button" className="fd-btn fd-btn--primary" onClick={register}
              disabled={!clinic || busy}>
              {busy ? 'Registering…' : 'Register and issue token'}
            </button>
          </div>
        </div>
      )}
    </section>
  )
}
