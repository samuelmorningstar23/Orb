/**
 * The OPD consultation note (opd-11, on the opd-10 server).
 *
 * One note per visit: complaint, history, examination, the vitals the doctor
 * measured with the time they were measured, diagnoses (free text with an
 * optional code, DiagnosisPicker), plan, advice, follow-up and referral.
 *
 *   * SAVE writes only the fields that changed, against the version the
 *     editor read; a colleague's later save is refused (409 stale_version)
 *     and the doctor reloads before saving again.
 *   * SIGN saves first if anything changed, then signs. It needs an allergy
 *     review (ruling OPD-S7, AllergyReviewPanel); a signed note never changes
 *     and takes addenda only. A signed note can be printed.
 *   * INVESTIGATIONS (InvestigationsPanel) are ordered on the visit itself.
 *   * THE PRESCRIPTION (PrescriptionPanel, opd-12) is written on the visit
 *     too, after the allergy review, and never reaches the ward drug chart.
 *
 * OPD VITALS ARE PLAIN VALUES (rulings OPD-S3, OPD-S6): typed with their time,
 * shown as words, never scored, banded or coloured, never sent to the vitals
 * chart, and no value prompts anyone.
 *
 * The clinic is never named to the server, and no patient is published into
 * the active view contexts.
 */
import { useId, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'

import {
  addConsultationAddendum,
  errorCode,
  fetchConsultation,
  fetchVisitPrint,
  saveConsultation,
  signConsultation,
  type ConsultationInput,
  type ConsultationView,
  type OpdConsultation,
  type OpdVitals,
  type VisitPrint,
} from '../../../api/opd'
import { reviewStateOf } from '../../../api/opdAllergyReview'
import OpdConsultationPrint from '../../../components/print/OpdConsultationPrint'
import { formatClinicalDateKey, formatClinicalDateTime, parseUTC } from '../../../lib/datetime'
import AllergyReviewPanel from './AllergyReviewPanel'
import DiagnosisPicker, { type DiagnosisDraft } from './DiagnosisPicker'
import InvestigationsPanel from './InvestigationsPanel'
import { OPD_VITAL_INPUTS, opdVitalsLine, type OpdVitalKey } from './opdVitals'
import PrescriptionPanel from './PrescriptionPanel'
import './ConsultationEditor.css'

interface ConsultationEditorProps {
  visitId: string
  patientName: string
  view: ConsultationView
  /** The note the server returned after a save, sign or addendum, or a
   *  re-read; the parent keeps it as the visit's note. */
  onChanged: (view: ConsultationView) => void
  /** Open the allergy review dialog. */
  onReviewAllergies?: () => void
  /** False when the visit is finished or cancelled: the note may still be
   *  written, nothing new is ordered. */
  visitOpen?: boolean
}

type TextKey = 'chief_complaint' | 'history' | 'examination' | 'plan' | 'advice'
  | 'follow_up_note' | 'referral_department'

const TEXT_FIELDS: { key: TextKey; label: string; rows: number; max: number }[] = [
  { key: 'chief_complaint', label: 'Complaint', rows: 2, max: 2000 },
  { key: 'history', label: 'History', rows: 3, max: 8000 },
  { key: 'examination', label: 'Examination', rows: 3, max: 8000 },
]
const PLAN_FIELDS: { key: TextKey; label: string; rows: number; max: number }[] = [
  { key: 'plan', label: 'Plan', rows: 3, max: 8000 },
  { key: 'advice', label: 'Advice to the patient', rows: 2, max: 4000 },
]

type VitalText = Partial<Record<OpdVitalKey, string>>

interface Draft {
  text: Record<TextKey, string>
  followUpDate: string
  diagnoses: DiagnosisDraft[]
  vitals: VitalText
  measuredAt: string
}

function pad(n: number): string {
  return String(n).padStart(2, '0')
}

/** A Date as the value of an <input type="datetime-local"> (local time). */
function toLocalInput(d: Date): string {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function draftOf(note: OpdConsultation | null): Draft {
  const text = {} as Record<TextKey, string>
  for (const f of [...TEXT_FIELDS, ...PLAN_FIELDS]) text[f.key] = note?.[f.key] ?? ''
  text.follow_up_note = note?.follow_up_note ?? ''
  text.referral_department = note?.referral_department ?? ''
  const vitals: VitalText = {}
  for (const v of OPD_VITAL_INPUTS) {
    const value = note?.vitals?.values?.[v.key]
    if (value != null) vitals[v.key] = String(value)
  }
  return {
    text,
    followUpDate: note?.follow_up_date ?? '',
    diagnoses: (note?.diagnoses ?? []).map(d => ({ ...d })),
    vitals,
    measuredAt: note?.vitals?.measured_at ? toLocalInput(parseUTC(note.vitals.measured_at)) : '',
  }
}

/** The vitals boxes that hold something, in a fixed order, as numbers where
 *  they are numbers (so "37.40" and "37.4" are the same value). */
function filledVitals(v: VitalText): Record<string, string | number> {
  const out: Record<string, string | number> = {}
  for (const input of OPD_VITAL_INPUTS) {
    const raw = (v[input.key] ?? '').trim()
    if (raw === '') continue
    const n = Number(raw)
    out[input.key] = Number.isFinite(n) ? n : raw
  }
  return out
}

function sameDiagnoses(a: DiagnosisDraft[], b: DiagnosisDraft[]): boolean {
  const key = (list: DiagnosisDraft[]) => JSON.stringify(list.map(d => [
    d.text.trim(), d.code ?? null, (d.code_system ?? null), !!d.add_to_problems]))
  return key(a) === key(b)
}

function messageOf(err: unknown, fallback: string): string {
  const detail = (err as { detail?: { message?: unknown } } | null)?.detail
  if (detail && typeof detail.message === 'string') return detail.message
  if (err instanceof Error && err.message) return err.message
  return fallback
}

/** The vitals typed, checked the way the server checks them; or the
 *  sentence saying what is wrong. */
function vitalsPayload(d: Draft): { values: OpdVitals['values']; measured_at: string } | null | string {
  const values: OpdVitals['values'] = {}
  for (const v of OPD_VITAL_INPUTS) {
    const raw = (d.vitals[v.key] ?? '').trim()
    if (raw === '') continue
    const n = Number(raw)
    if (!Number.isFinite(n)) return `${v.label} must be a number.`
    if (n < v.min || n > v.max) return `${v.label} ${raw} is outside ${v.min} to ${v.max}.`
    values[v.key] = n
  }
  if (Object.keys(values).length === 0) return null
  if (!d.measuredAt) return 'Say when the vitals were measured.'
  const when = new Date(d.measuredAt)
  if (Number.isNaN(when.getTime())) return 'The measured time is not a valid time.'
  if (when.getTime() > Date.now() + 5 * 60 * 1000) return 'The measured time is in the future.'
  return { values, measured_at: when.toISOString() }
}

function SignedNote({ note }: { note: OpdConsultation }) {
  const line = (label: string, value: string | null | undefined) =>
    value && value.trim() ? <p className="opd-line"><strong>{label}:</strong> {value}</p> : null
  return (
    <div className="opd-note">
      {line('Complaint', note.chief_complaint)}
      {line('History', note.history)}
      {line('Examination', note.examination)}
      {note.diagnoses.length > 0 && (
        <p className="opd-line">
          <strong>Diagnoses:</strong>{' '}
          {note.diagnoses.map(d => (d.code
            ? `${d.text} (${[d.code_system, d.code].filter(Boolean).join(' ')})`
            : d.text)).join('; ')}
        </p>
      )}
      {line('Plan', note.plan)}
      {line('Advice', note.advice)}
      {note.follow_up_date || note.follow_up_note
        ? line('Follow-up', [note.follow_up_date ? formatClinicalDateKey(note.follow_up_date) : null,
          note.follow_up_note].filter(Boolean).join('. '))
        : null}
      {line('Referred to', note.referral_department)}
    </div>
  )
}

export default function ConsultationEditor({
  visitId,
  patientName,
  view,
  onChanged,
  onReviewAllergies,
  visitOpen = true,
}: ConsultationEditorProps) {
  const id = useId()
  const note = view.consultation
  /* `base` is the note the draft was built from, and its row_version is the
     one a save sends: a note re-read from outside while the doctor has
     unsaved changes never becomes the base, so a colleague's later save is
     refused as stale instead of being overwritten unseen. */
  const [base, setBase] = useState<OpdConsultation | null>(note)
  const saved = useMemo(() => draftOf(base), [base])
  const [draft, setDraft] = useState<Draft>(saved)
  const [seen, setSeen] = useState<OpdConsultation | null>(note)
  const [busy, setBusy] = useState<'' | 'save' | 'sign' | 'addendum' | 'print' | 'reload'>('')
  const [error, setError] = useState('')
  const [stale, setStale] = useState(false)
  const [message, setMessage] = useState('')
  const [addendum, setAddendum] = useState('')
  const [print, setPrint] = useState<VisitPrint | null>(null)
  const review = reviewStateOf(view)
  const signed = note?.status === 'signed'
  const readOnly = signed || view.patient_admitted || view.visit_status === 'cancelled'
  const typedVitals = filledVitals(draft.vitals)
  const vitalsChanged = JSON.stringify(typedVitals) !== JSON.stringify(filledVitals(saved.vitals))
    || (draft.measuredAt !== saved.measuredAt && Object.keys(typedVitals).length > 0)

  const changes = (): ConsultationInput => {
    const out: ConsultationInput = {}
    for (const f of [...TEXT_FIELDS, ...PLAN_FIELDS, { key: 'follow_up_note' as TextKey },
      { key: 'referral_department' as TextKey }]) {
      if (draft.text[f.key].trim() !== saved.text[f.key].trim()) out[f.key] = draft.text[f.key].trim() || null
    }
    if (draft.followUpDate !== saved.followUpDate) out.follow_up_date = draft.followUpDate || null
    if (!sameDiagnoses(draft.diagnoses, saved.diagnoses)) {
      out.diagnoses = draft.diagnoses.filter(d => d.text.trim()).map(d => ({
        text: d.text.trim(),
        ...(d.code ? { code: d.code, code_system: d.code_system ?? undefined } : {}),
        add_to_problems: !!d.add_to_problems,
      }))
    }
    return out
  }

  const dirty = (): boolean => Object.keys(changes()).length > 0 || vitalsChanged

  /* A note the parent re-read (after an allergy review, say) is adopted only
     while nothing here is unsaved. */
  if (note !== seen) {
    setSeen(note)
    if (!dirty()) {
      setBase(note)
      setDraft(draftOf(note))
    }
  }

  /** The note the server holds now becomes the base of a fresh draft. */
  const adopt = (latest: OpdConsultation | null) => {
    setBase(latest)
    setDraft(draftOf(latest))
    setSeen(latest)
  }

  const setText = (key: TextKey, value: string) =>
    setDraft(d => ({ ...d, text: { ...d.text, [key]: value } }))
  const setVital = (key: OpdVitalKey, value: string) => setDraft(d => ({
    ...d,
    vitals: { ...d.vitals, [key]: value },
    measuredAt: d.measuredAt || (value.trim() ? toLocalInput(new Date()) : ''),
  }))

  const refusal = (err: unknown, fallback: string) => {
    const code = errorCode(err)
    if (code === 'stale_version') {
      setStale(true)
      setError('Someone saved this note after you opened it. Reload it, then make your change again.')
    } else if (code === 'allergy_review_required') {
      setError('Record the allergy review before signing: the patient’s allergies, no known '
        + 'allergies, or why they could not be asked.')
    } else if (code === 'patient_admitted') {
      setError('This patient is now admitted. Record it on the inpatient chart.')
    } else {
      setError(messageOf(err, fallback))
    }
  }

  /** Save what changed; returns the note the server holds afterwards. */
  const save = async (): Promise<OpdConsultation | null | false> => {
    const vitals = vitalsPayload(draft)
    if (typeof vitals === 'string') {
      setError(vitals)
      return false
    }
    const fields = changes()
    if (vitalsChanged) fields.vitals = vitals
    if (Object.keys(fields).length === 0) return base
    const updated = await saveConsultation(visitId, base ? base.row_version : null, fields)
    adopt(updated)
    onChanged({ ...view, consultation: updated })
    return updated
  }

  const onSave = async () => {
    if (busy || readOnly) return
    setBusy('save')
    setError('')
    setMessage('')
    try {
      const updated = await save()
      if (updated) setMessage(`Saved, version ${updated.row_version}.`)
    } catch (err) {
      refusal(err, 'The note was not saved.')
    } finally {
      setBusy('')
    }
  }

  const onSign = async () => {
    if (busy || readOnly) return
    setBusy('sign')
    setError('')
    setMessage('')
    try {
      const current = await save()
      if (current === false) return
      if (!current) {
        setError('Write the findings, a diagnosis or a plan before signing.')
        return
      }
      const done = await signConsultation(visitId, current.row_version)
      adopt(done)
      onChanged({ ...view, consultation: done })
    } catch (err) {
      refusal(err, 'The note was not signed.')
    } finally {
      setBusy('')
    }
  }

  const onReload = async () => {
    setBusy('reload')
    setError('')
    try {
      const fresh = await fetchConsultation(visitId)
      if (fresh?.visit_id !== visitId) throw new Error('The note that came back is not this visit’s.')
      setStale(false)
      adopt(fresh.consultation)
      onChanged(fresh)
    } catch (err) {
      setError(messageOf(err, 'The note could not be reloaded.'))
    } finally {
      setBusy('')
    }
  }

  const onAddendum = async () => {
    if (busy || !addendum.trim()) return
    setBusy('addendum')
    setError('')
    try {
      const updated = await addConsultationAddendum(visitId, addendum.trim())
      setAddendum('')
      adopt(updated)
      onChanged({ ...view, consultation: updated })
    } catch (err) {
      refusal(err, 'The addendum was not added.')
    } finally {
      setBusy('')
    }
  }

  const onPrint = async () => {
    setBusy('print')
    setError('')
    try {
      setPrint(await fetchVisitPrint(visitId, 'consultation'))
    } catch (err) {
      setError(messageOf(err, 'The note could not be printed.'))
    } finally {
      setBusy('')
    }
  }

  const savedVitals = note?.vitals ? opdVitalsLine(note.vitals.values) : ''

  return (
    <div className="opd-note opd-cons" aria-label={`Consultation note for ${patientName}`} role="group">
      <h4 className="opd-subhead">This visit&rsquo;s note</h4>
      {!note && <p className="opd-empty">No note has been started for this visit.</p>}
      {note && note.status === 'draft' && (
        <p className="opd-line">Draft, version {note.row_version}, last saved {formatClinicalDateTime(note.updated_at)}.</p>
      )}
      {note && signed && (
        <p className="opd-line">
          Signed by {note.signed_by_name ?? note.signed_by} on {formatClinicalDateTime(note.signed_at)}
          {note.addenda && note.addenda.length > 0 ? `, with ${note.addenda.length} addenda` : ''}.
        </p>
      )}
      {note?.vitals && savedVitals && (
        <p className="opd-line">
          Measured {formatClinicalDateTime(note.vitals.measured_at)}
          {note.vitals.measured_by_name ? ` by ${note.vitals.measured_by_name}` : ''}: {savedVitals}
        </p>
      )}
      {view.patient_admitted && (
        <p className="opd-hint">This patient is now admitted. The note is read only here.</p>
      )}

      {signed && note && <SignedNote note={note} />}

      {!readOnly && (
        <form className="opd-cons__form" aria-label="Edit the note" noValidate
          onSubmit={e => { e.preventDefault(); void onSave() }}>
          {TEXT_FIELDS.map(f => (
            <div className="opd-field" key={f.key}>
              <label htmlFor={`${id}-${f.key}`}>{f.label}</label>
              <textarea id={`${id}-${f.key}`} rows={f.rows} maxLength={f.max} value={draft.text[f.key]}
                disabled={busy !== ''} onChange={e => setText(f.key, e.target.value)} />
            </div>
          ))}

          <fieldset className="opd-cons__vitals">
            <legend className="opd-subhead">Vitals you measured</legend>
            <p className="opd-hint">
              Plain values with the time they were measured. They stay in this note and are not
              added to any vitals chart.
            </p>
            <div className="opd-cons__vital-grid">
              {OPD_VITAL_INPUTS.map(v => (
                <div className="opd-field" key={v.key}>
                  <label htmlFor={`${id}-v-${v.key}`}>{v.label} ({v.unit})</label>
                  <input id={`${id}-v-${v.key}`} type="number" inputMode="decimal" min={v.min} max={v.max}
                    step={v.step} value={draft.vitals[v.key] ?? ''} disabled={busy !== ''}
                    onChange={e => setVital(v.key, e.target.value)} />
                </div>
              ))}
            </div>
            <div className="opd-field">
              <label htmlFor={`${id}-measured`}>Measured at</label>
              <input id={`${id}-measured`} type="datetime-local" value={draft.measuredAt}
                disabled={busy !== ''}
                onChange={e => setDraft(d => ({ ...d, measuredAt: e.target.value }))} />
            </div>
          </fieldset>

          <div className="opd-cons__section">
            <h5 className="opd-subhead">Diagnoses</h5>
            <DiagnosisPicker value={draft.diagnoses} disabled={busy !== ''}
              onChange={next => setDraft(d => ({ ...d, diagnoses: next }))} />
          </div>

          {PLAN_FIELDS.map(f => (
            <div className="opd-field" key={f.key}>
              <label htmlFor={`${id}-${f.key}`}>{f.label}</label>
              <textarea id={`${id}-${f.key}`} rows={f.rows} maxLength={f.max} value={draft.text[f.key]}
                disabled={busy !== ''} onChange={e => setText(f.key, e.target.value)} />
            </div>
          ))}

          <div className="opd-field-row">
            <div className="opd-field">
              <label htmlFor={`${id}-fu-date`}>Follow-up date</label>
              <input id={`${id}-fu-date`} type="date" value={draft.followUpDate} disabled={busy !== ''}
                onChange={e => setDraft(d => ({ ...d, followUpDate: e.target.value }))} />
            </div>
            <div className="opd-field">
              <label htmlFor={`${id}-fu-note`}>Follow-up note</label>
              <input id={`${id}-fu-note`} type="text" maxLength={2000} value={draft.text.follow_up_note}
                disabled={busy !== ''} onChange={e => setText('follow_up_note', e.target.value)} />
            </div>
          </div>
          <div className="opd-field">
            <label htmlFor={`${id}-referral`}>Refer to another clinic</label>
            <input id={`${id}-referral`} type="text" maxLength={120} value={draft.text.referral_department}
              placeholder="The clinic's name, if you are referring" disabled={busy !== ''}
              onChange={e => setText('referral_department', e.target.value)} />
          </div>

          <AllergyReviewPanel review={review} onRecord={onReviewAllergies} />

          <div className="opd-row__actions">
            {stale && (
              <button type="button" className="opd-btn" onClick={() => { void onReload() }} disabled={busy !== ''}>
                {busy === 'reload' ? 'Reloading…' : 'Reload the note'}
              </button>
            )}
            <button type="submit" className="opd-btn" disabled={busy !== '' || stale || !dirty()}>
              {busy === 'save' ? 'Saving…' : 'Save draft'}
            </button>
            <button type="button" className="opd-btn opd-btn--primary"
              disabled={busy !== '' || stale || !review?.can_sign}
              onClick={() => { void onSign() }}>
              {busy === 'sign' ? 'Signing…' : 'Sign the note'}
            </button>
          </div>
        </form>
      )}

      {error && <p className="opd-error" role="alert">{error}</p>}
      {message && <p className="opd-hint">{message}</p>}

      {signed && note && (
        <div className="opd-cons__section">
          {note.addenda && note.addenda.length > 0 && (
            <ul className="opd-plain-list" aria-label="Addenda">
              {note.addenda.map(a => (
                <li key={a.id}>
                  {formatClinicalDateTime(a.created_at)}{a.author_name ? `, ${a.author_name}` : ''}: {a.body}
                </li>
              ))}
            </ul>
          )}
          {!view.patient_admitted && (
            <div className="opd-field">
              <label htmlFor={`${id}-addendum`}>Add an addendum</label>
              <textarea id={`${id}-addendum`} rows={2} maxLength={8000} value={addendum}
                disabled={busy !== ''} onChange={e => setAddendum(e.target.value)} />
            </div>
          )}
          <div className="opd-row__actions">
            {!view.patient_admitted && (
              <button type="button" className="opd-btn" disabled={busy !== '' || !addendum.trim()}
                onClick={() => { void onAddendum() }}>
                {busy === 'addendum' ? 'Adding…' : 'Add addendum'}
              </button>
            )}
            <button type="button" className="opd-btn" disabled={busy !== ''} onClick={() => { void onPrint() }}>
              {busy === 'print' ? 'Preparing…' : 'Print consultation'}
            </button>
          </div>
        </div>
      )}

      <InvestigationsPanel visitId={visitId}
        canOrder={visitOpen && !view.patient_admitted && view.visit_status !== 'cancelled'
          && view.visit_status !== 'completed'} />

      {/* opd-12: shows nothing where the medication-order store is off or the
          drug chart is not part of this computer (the server answers 404). */}
      <PrescriptionPanel visitId={visitId} allergyReview={review}
        onReviewAllergies={onReviewAllergies}
        canWrite={visitOpen && !view.patient_admitted && view.visit_status !== 'cancelled'
          && view.visit_status !== 'completed'} />

      {print && createPortal(
        <OpdConsultationPrint print={print} onAfterPrint={() => setPrint(null)} />,
        document.body,
      )}
    </div>
  )
}
