/**
 * Front Desk, the registration details drawer (opd-6b).
 *
 * Opened from the Register view for the patient whose visit was just
 * registered. It reads GET /api/registration/patients/{id} (the server
 * scopes it: the caller's own department, a recent visit in the caller's
 * clinic, or a desk with no department; anything else answers not found,
 * which is all this drawer says), and the change history beside it.
 *
 *   * CORRECTION. Every change needs a reason and is sent against the
 *     row_version the drawer read; a colleague's change in between is
 *     refused (409 stale_row_version) and the drawer offers a reload. Fields
 *     need registration.write; the UHID is shown for correction to the
 *     administrator only, and the administrator corrects nothing else.
 *   * WHAT A CLINIC SEES. A reader let in only by an outpatient visit gets the
 *     date of birth, contact, address, next of kin and identity document
 *     back empty (hidden_fields); the drawer says they are not shown and
 *     offers no correction of them.
 *   * DATE OF BIRTH OR STATED AGE (ruling OPD-N5): one or the other, never an
 *     estimated date of birth; a stated age keeps the "no discharge link"
 *     hint.
 *   * IDENTITY DOCUMENT (ruling OPD-N10): its type and last 4 characters only.
 *     There is no field for a full Aadhaar or any other full number.
 *   * UHID CARD. Printed from what GET .../card returns, which the server
 *     audits as a card print.
 *
 * No allergy field (ruling OPD-S5). Never publishes the patient into the
 * active view contexts.
 */
import { useCallback, useEffect, useId, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'

import {
  correctRegistration,
  errorCode,
  errorDetail,
  errorStatus,
  fetchRegistrationChanges,
  fetchRegistrationRecord,
  fetchUhidCard,
  type CorrectableField,
  type IdentityChange,
  type RegistrationChanges,
  type RegistrationRecord,
  type UhidCard,
} from '../../../api/registration'
import UhidCardPrint from '../../../components/print/UhidCardPrint'
import { useFocusTrap } from '../../../hooks/useFocusTrap'
import { formatClinicalDateKey, formatClinicalDateTime } from '../../../lib/datetime'
import { REGISTRATION_WRITE, currentRole, hasPermission, type Permission } from '../../../lib/roles'
import './DemographicsDrawer.css'

interface DemographicsDrawerProps {
  patientId: string
  /** Shown in the title while the record loads. */
  patientName: string
  onClose: () => void
  /** Tests only: whether the signed-in user holds a permission. */
  holds?: (permission: string) => boolean
  /** Tests only: whether the signed-in user is the administrator. */
  isAdmin?: boolean
}

type EditKey = Exclude<CorrectableField, 'date_of_birth' | 'age_years'>

interface FieldDef {
  key: EditKey
  label: string
  type?: 'text' | 'tel' | 'email'
  max?: number
  options?: { value: string; label: string }[]
}

const SEX_OPTIONS = [
  { value: 'F', label: 'Female' },
  { value: 'M', label: 'Male' },
  { value: 'O', label: 'Other' },
]

/** The identity document kinds the server accepts (registration_service.
 *  ID_DOCUMENT_TYPES), recorded as the type and the last 4 characters. */
const ID_DOCUMENT_OPTIONS = [
  { value: 'aadhaar', label: 'Aadhaar' },
  { value: 'pan', label: 'PAN card' },
  { value: 'voter_id', label: 'Voter ID' },
  { value: 'passport', label: 'Passport' },
  { value: 'driving_licence', label: 'Driving licence' },
  { value: 'ration_card', label: 'Ration card' },
  { value: 'other', label: 'Other' },
]

const IDENTITY_FIELDS: FieldDef[] = [
  { key: 'name', label: 'Full name', max: 200 },
  { key: 'sex', label: 'Sex', options: SEX_OPTIONS },
]
const CONTACT_FIELDS: FieldDef[] = [
  { key: 'phone', label: 'Phone', type: 'tel', max: 30 },
  { key: 'alt_phone', label: 'Other phone', type: 'tel', max: 30 },
  { key: 'email', label: 'Email', type: 'email', max: 200 },
  { key: 'emergency_contact_name', label: 'Emergency contact', max: 200 },
  { key: 'emergency_contact_phone', label: 'Emergency contact phone', type: 'tel', max: 30 },
  { key: 'guardian_relation', label: 'Guardian relation', max: 200 },
  { key: 'guardian_name', label: 'Guardian name', max: 200 },
]
const ADDRESS_FIELDS: FieldDef[] = [
  { key: 'address_line', label: 'Address', max: 200 },
  { key: 'city', label: 'City or village', max: 200 },
  { key: 'district', label: 'District', max: 200 },
  { key: 'state_code', label: 'State', max: 200 },
  { key: 'pin_code', label: 'PIN code', max: 10 },
]
const OTHER_FIELDS: FieldDef[] = [
  { key: 'preferred_language', label: 'Preferred language', max: 200 },
  { key: 'marital_status', label: 'Marital status', max: 200 },
  { key: 'occupation', label: 'Occupation', max: 200 },
  { key: 'id_document_type', label: 'Identity document', options: ID_DOCUMENT_OPTIONS },
  { key: 'id_document_last4', label: 'Identity document, last 4 characters', max: 4 },
]
const ALL_FIELDS = [...IDENTITY_FIELDS, ...CONTACT_FIELDS, ...ADDRESS_FIELDS, ...OTHER_FIELDS]

const FIELD_LABEL: Record<string, string> = {
  ...Object.fromEntries(ALL_FIELDS.map(f => [f.key, f.label])),
  uhid: 'UHID',
  date_of_birth: 'Date of birth',
  age_years: 'Stated age',
}

function roleHolds(permission: string): boolean {
  return hasPermission(permission as Permission)
}

function valueOf(record: RegistrationRecord, key: EditKey): string {
  if (key === 'uhid') return record.uhid ?? ''
  if (key in record.demographics) {
    return (record.demographics as unknown as Record<string, string | null>)[key] ?? ''
  }
  const v = (record as unknown as Record<string, unknown>)[key]
  return v === null || v === undefined ? '' : String(v)
}

function shownValue(def: FieldDef, raw: string): string {
  if (!raw) return 'Not recorded'
  const option = def.options?.find(o => o.value === raw)
  return option ? option.label : raw
}

function messageOf(err: unknown, fallback: string): string {
  const message = errorDetail(err)?.message
  if (typeof message === 'string') return message
  if (err instanceof Error && err.message) return err.message
  return fallback
}

export default function DemographicsDrawer({
  patientId,
  patientName,
  onClose,
  holds = roleHolds,
  isAdmin = currentRole() === 'admin',
}: DemographicsDrawerProps) {
  const id = useId()
  const [record, setRecord] = useState<RegistrationRecord | null>(null)
  const [changes, setChanges] = useState<IdentityChange[] | null>(null)
  const [loadError, setLoadError] = useState('')
  const [notFound, setNotFound] = useState(false)
  const [editing, setEditing] = useState(false)
  const [values, setValues] = useState<Record<string, string>>({})
  const [ageMode, setAgeMode] = useState<'dob' | 'age'>('dob')
  const [dob, setDob] = useState('')
  const [age, setAge] = useState('')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [stale, setStale] = useState(false)
  const [saved, setSaved] = useState('')
  const [card, setCard] = useState<UhidCard | null>(null)
  const close = useCallback(() => { if (!busy) onClose() }, [busy, onClose])
  const ref = useFocusTrap<HTMLDivElement>(close)

  const canEditFields = holds(REGISTRATION_WRITE)
  const canCorrectUhid = isAdmin
  const hidden = useMemo(() => new Set(record?.hidden_fields ?? []), [record])

  const startForm = useCallback((r: RegistrationRecord) => {
    const next: Record<string, string> = {}
    for (const f of ALL_FIELDS) next[f.key] = valueOf(r, f.key)
    next.uhid = r.uhid ?? ''
    setValues(next)
    setAgeMode(r.date_of_birth ? 'dob' : r.age_stated ? 'age' : 'dob')
    setDob(r.date_of_birth ?? '')
    setAge(r.age_stated && r.age != null ? String(r.age) : '')
    setReason('')
  }, [])

  const load = useCallback(async () => {
    setLoadError('')
    setNotFound(false)
    try {
      const [r, history] = await Promise.all([
        fetchRegistrationRecord(patientId),
        fetchRegistrationChanges(patientId).catch(() => null),
      ])
      if (r?.patient_id !== patientId) {
        setLoadError('The record that came back is not this patient’s. Close and open it again.')
        return
      }
      setRecord(r)
      setChanges(Array.isArray(history?.changes) ? history.changes : null)
      startForm(r)
      setStale(false)
    } catch (err) {
      if (errorStatus(err) === 404) {
        setNotFound(true)
        return
      }
      setLoadError(messageOf(err, 'The record could not be loaded.'))
    }
  }, [patientId, startForm])

  // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch trigger: the setState is the loader's own result, not state derived from props.
  useEffect(() => { void load() }, [load])

  const editable = (key: string): boolean => {
    if (hidden.has(key)) return false
    if (key === 'uhid') return canCorrectUhid
    return canEditFields
  }

  const pending = (): RegistrationChanges => {
    if (!record) return {}
    const out: RegistrationChanges = {}
    for (const f of ALL_FIELDS) {
      if (!editable(f.key)) continue
      const before = valueOf(record, f.key).trim()
      const after = (values[f.key] ?? '').trim()
      if (after !== before) out[f.key] = after || null
    }
    if (editable('uhid') && (values.uhid ?? '').trim() !== (record.uhid ?? '').trim()) {
      out.uhid = (values.uhid ?? '').trim() || null
    }
    if (editable('date_of_birth') && canEditFields) {
      const hadDob = !!record.date_of_birth
      if (ageMode === 'dob') {
        if (dob && dob !== (record.date_of_birth ?? '')) out.date_of_birth = dob
      } else {
        const n = age.trim() === '' ? null : Number(age)
        const before = record.age_stated && record.age != null ? record.age : null
        if (hadDob) {
          out.date_of_birth = null
          out.age_years = n
        } else if (n !== before) {
          out.age_years = n
        }
      }
    }
    return out
  }

  const changed = editing ? Object.keys(pending()) : []
  const reasonOk = reason.trim().length >= 8
  const ready = !busy && !stale && changed.length > 0 && reasonOk

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    if (!record || !ready) return
    setBusy(true)
    setError('')
    setSaved('')
    try {
      const result = await correctRegistration(patientId, record.row_version, reason.trim(), pending())
      const history = await fetchRegistrationChanges(patientId).catch(() => null)
      setRecord(result.patient)
      startForm(result.patient)
      if (history && Array.isArray(history.changes)) setChanges(history.changes)
      setEditing(false)
      setSaved(`Corrected: ${result.changed_fields.map(f => FIELD_LABEL[f] ?? f).join(', ')}.`)
    } catch (err) {
      if (errorCode(err) === 'stale_row_version') {
        setStale(true)
        setError('Someone changed this record after you opened it. Reload it, then make your '
          + 'correction again.')
      } else {
        setError(messageOf(err, 'The correction was not saved.'))
      }
    } finally {
      setBusy(false)
    }
  }

  const printCard = async () => {
    setError('')
    try {
      setCard(await fetchUhidCard(patientId))
    } catch (err) {
      setError(messageOf(err, 'The card could not be printed.'))
    }
  }

  const fieldRow = (def: FieldDef) => {
    const raw = record ? valueOf(record, def.key) : ''
    const isHidden = hidden.has(def.key)
    if (editing && editable(def.key)) {
      const inputId = `${id}-${def.key}`
      return (
        <div className="dmg-field" key={def.key}>
          <label htmlFor={inputId}>{def.label}</label>
          {def.options ? (
            <select id={inputId} value={values[def.key] ?? ''} disabled={busy}
              onChange={e => setValues(v => ({ ...v, [def.key]: e.target.value }))}>
              {def.key !== 'sex' && <option value="">Not recorded</option>}
              {def.options.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
          ) : (
            <input id={inputId} type={def.type ?? 'text'} maxLength={def.max} autoComplete="off"
              value={values[def.key] ?? ''} disabled={busy}
              onChange={e => setValues(v => ({ ...v, [def.key]: e.target.value }))} />
          )}
        </div>
      )
    }
    return (
      <div className="dmg-row" key={def.key}>
        <dt>{def.label}</dt>
        <dd>{isHidden ? 'Not shown to your clinic' : shownValue(def, raw)}</dd>
      </div>
    )
  }

  const title = record?.name ?? patientName

  return createPortal(
    <div className="dmg-backdrop" onClick={close}>
      <div
        className="dmg-drawer"
        ref={ref}
        role="dialog"
        aria-modal="true"
        aria-labelledby={`${id}-title`}
        onClick={e => e.stopPropagation()}
      >
        <header className="dmg-head">
          <h2 className="dmg-title" id={`${id}-title`}>Registration details for {title}</h2>
          <button type="button" className="dmg-btn dmg-btn--quiet" onClick={close} disabled={busy}>
            Close
          </button>
        </header>

        {notFound && (
          <p className="dmg-error" role="alert">Patient not found.</p>
        )}
        {loadError && (
          <div className="dmg-actions">
            <p className="dmg-error" role="alert">{loadError}</p>
            <button type="button" className="dmg-btn" onClick={() => { void load() }}>Try again</button>
          </div>
        )}
        {!record && !notFound && !loadError && <p className="dmg-hint">Loading the record…</p>}

        {record && (
          <form className="dmg-body" aria-label="Registration details" onSubmit={submit}>
            <section className="dmg-section" aria-label="Identity">
              <h3 className="dmg-subhead">Identity</h3>
              <dl className="dmg-list">
                {editing && editable('uhid') ? null : (
                  <div className="dmg-row">
                    <dt>UHID</dt>
                    <dd>{record.uhid || 'Not recorded'}</dd>
                  </div>
                )}
                {IDENTITY_FIELDS.map(fieldRow)}
                {hidden.has('date_of_birth') ? (
                  <div className="dmg-row">
                    <dt>Date of birth</dt>
                    <dd>Not shown to your clinic</dd>
                  </div>
                ) : !(editing && canEditFields) ? (
                  <div className="dmg-row">
                    <dt>{record.date_of_birth ? 'Date of birth' : 'Stated age'}</dt>
                    <dd>
                      {record.date_of_birth
                        ? formatClinicalDateKey(record.date_of_birth)
                        : record.age != null
                          ? `${record.age} years${record.age_recorded_on
                            ? `, stated ${formatClinicalDateKey(record.age_recorded_on)}` : ''}`
                          : 'Not recorded'}
                    </dd>
                  </div>
                ) : null}
              </dl>
              {editing && editable('uhid') && (
                <div className="dmg-field">
                  <label htmlFor={`${id}-uhid`}>UHID (administrator only)</label>
                  <input id={`${id}-uhid`} type="text" maxLength={40} autoComplete="off"
                    value={values.uhid ?? ''} disabled={busy}
                    onChange={e => setValues(v => ({ ...v, uhid: e.target.value }))} />
                </div>
              )}
              {editing && canEditFields && !hidden.has('date_of_birth') && (
                <fieldset className="dmg-field">
                  <legend>Date of birth, or the age the patient states</legend>
                  <div className="dmg-choice">
                    <label>
                      <input type="radio" name={`${id}-age-mode`} checked={ageMode === 'dob'} disabled={busy}
                        onChange={() => setAgeMode('dob')} />
                      Date of birth
                    </label>
                    <label>
                      <input type="radio" name={`${id}-age-mode`} checked={ageMode === 'age'} disabled={busy}
                        onChange={() => setAgeMode('age')} />
                      Stated age
                    </label>
                  </div>
                  {ageMode === 'dob' ? (
                    <input aria-label="Date of birth" type="date" value={dob} disabled={busy}
                      onChange={e => setDob(e.target.value)} />
                  ) : (
                    <input aria-label="Stated age in years" type="number" min={0} max={130}
                      inputMode="numeric" value={age} disabled={busy} onChange={e => setAge(e.target.value)} />
                  )}
                </fieldset>
              )}
              {record.date_of_birth_hint && !hidden.has('date_of_birth') && (
                <p className="dmg-hint">{record.date_of_birth_hint}</p>
              )}
            </section>

            <section className="dmg-section" aria-label="Contact and next of kin">
              <h3 className="dmg-subhead">Contact and next of kin</h3>
              <dl className="dmg-list">{CONTACT_FIELDS.map(fieldRow)}</dl>
              {hidden.has('phone') && record.phone_last4 && (
                <p className="dmg-hint">Phone ending {record.phone_last4}.</p>
              )}
            </section>

            <section className="dmg-section" aria-label="Address">
              <h3 className="dmg-subhead">Address</h3>
              <dl className="dmg-list">{ADDRESS_FIELDS.map(fieldRow)}</dl>
            </section>

            <section className="dmg-section" aria-label="Other details">
              <h3 className="dmg-subhead">Other details</h3>
              <dl className="dmg-list">{OTHER_FIELDS.map(fieldRow)}</dl>
              <p className="dmg-hint">
                An identity document is recorded as its type and its last 4 characters only.
              </p>
              {record.abha.length > 0 && (
                <ul className="dmg-plain" aria-label="ABHA">
                  {record.abha.map(a => (
                    <li key={a.id}>
                      ABHA {a.abha_number ?? a.abha_address ?? ''} ({a.verification_label})
                    </li>
                  ))}
                </ul>
              )}
              {hidden.size > 0 && (
                <p className="dmg-hint">
                  Your clinic reached this record through an outpatient visit, so the date of birth,
                  contact, address and identity details are not shown and cannot be corrected here.
                </p>
              )}
            </section>

            <section className="dmg-section" aria-label="Registration">
              <h3 className="dmg-subhead">Registration</h3>
              <p className="dmg-hint">
                {record.registration.registered_at
                  ? `Registered ${formatClinicalDateTime(record.registration.registered_at)}`
                    + (record.registration.registered_by_name ? ` by ${record.registration.registered_by_name}` : '')
                    + '.'
                  : 'No registration date on file.'}
                {record.registration.updated_at
                  ? ` Last corrected ${formatClinicalDateTime(record.registration.updated_at)}`
                    + (record.registration.updated_by_name ? ` by ${record.registration.updated_by_name}` : '')
                    + '.'
                  : ''}
              </p>
            </section>

            {editing && (
              <div className="dmg-field">
                <label htmlFor={`${id}-reason`}>Why are these details being corrected? (at least 8 characters)</label>
                <textarea id={`${id}-reason`} rows={2} maxLength={1000} value={reason} disabled={busy}
                  onChange={e => setReason(e.target.value)} />
              </div>
            )}

            {error && <p className="dmg-error" role="alert">{error}</p>}
            {saved && <p className="dmg-ok" role="status">{saved}</p>}

            <div className="dmg-actions">
              {!editing && (canEditFields || canCorrectUhid) && (
                <button type="button" className="dmg-btn" onClick={() => { setSaved(''); setEditing(true) }}>
                  {canEditFields ? 'Correct details' : 'Correct the UHID'}
                </button>
              )}
              {editing && (
                <>
                  <button type="button" className="dmg-btn dmg-btn--quiet" disabled={busy}
                    onClick={() => { startForm(record); setEditing(false); setError(''); setStale(false) }}>
                    Cancel
                  </button>
                  {stale && (
                    <button type="button" className="dmg-btn" disabled={busy}
                      onClick={() => { setEditing(false); setError(''); void load() }}>
                      Reload the record
                    </button>
                  )}
                  <button type="submit" className="dmg-btn dmg-btn--primary" disabled={!ready}>
                    {busy ? 'Saving…' : 'Save correction'}
                  </button>
                </>
              )}
              {!editing && record.uhid && (
                <button type="button" className="dmg-btn" onClick={() => { void printCard() }}>
                  Print UHID card
                </button>
              )}
            </div>
          </form>
        )}

        {record && (
          <section className="dmg-section" aria-label="Change history">
            <h3 className="dmg-subhead">Change history</h3>
            {changes === null && <p className="dmg-hint">The change history could not be loaded.</p>}
            {changes !== null && changes.length === 0 && <p className="dmg-hint">No corrections yet.</p>}
            {changes !== null && changes.length > 0 && (
              <ul className="dmg-history">
                {changes.map(c => (
                  <li key={c.id} className="dmg-history__item">
                    <span className="dmg-history__what">
                      {FIELD_LABEL[c.field] ?? c.field}:{' '}
                      {c.values_hidden
                        ? 'values not shown to your clinic'
                        : `${c.old_value ?? 'blank'} to ${c.new_value ?? 'blank'}`}
                    </span>
                    <span className="dmg-hint">
                      {formatClinicalDateTime(c.changed_at)}
                      {c.changed_by_name ? `, ${c.changed_by_name}` : ''}. Reason: {c.reason}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </section>
        )}
      </div>
      {card && createPortal(
        <UhidCardPrint card={card} onAfterPrint={() => setCard(null)} />,
        document.body,
      )}
    </div>,
    document.body,
  )
}
