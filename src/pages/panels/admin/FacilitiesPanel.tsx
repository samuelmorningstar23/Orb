/**
 * Facilities (multisite-2): who the hospital is on its bills, receipts and
 * printed records.
 *
 * The identity card for facility 1 is always here, whatever the multi-site
 * switch says: legal name, registered address, GSTIN, PAN, GST state code and
 * the registration numbers (health facility registry, ABDM, NABH, clinical
 * establishment). Until the address and GSTIN are filled in, bills print
 * "Not configured" and say they are not GST tax invoices.
 *
 * Editing changes only documents numbered from then on: a bill or receipt that
 * already has a number keeps the details it was issued with (the server holds
 * them on the document itself).
 *
 * With the multi-site switch on (read from the platform flags shellConfig
 * adopts from /api/health), the screen lists every facility and says why a
 * second one cannot be added yet. Adding and closing facilities themselves come
 * with later steps of the multi-site work.
 */
import { useCallback, useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import {
  FACILITY_FIELDS,
  FACILITY_FIELD_LABELS,
  fieldErrorsOf,
  getCreateCheck,
  isSwitchedOff,
  listFacilities,
  updateFacility,
} from '../../../api/facilities'
import type {
  CreateCheck,
  Facility,
  FacilityField,
  FacilityIdentityPatch,
  FacilityList,
} from '../../../api/facilities'
import { formatClinicalDateKey, formatClinicalDateTime } from '../../../lib/datetime'
import { usePlatformFlags } from '../../../shellConfig'

type Draft = Record<FacilityField, string>

function errorText(err: unknown, fallback: string): string {
  return err instanceof Error && err.message ? err.message : fallback
}

function draftOf(facility: Facility): Draft {
  const out = {} as Draft
  for (const field of FACILITY_FIELDS) out[field] = facility[field] ?? ''
  return out
}

/** Only the fields that changed; a cleared field is sent as null. */
function patchOf(facility: Facility, draft: Draft): FacilityIdentityPatch {
  const patch: FacilityIdentityPatch = {}
  for (const field of FACILITY_FIELDS) {
    const next = draft[field].trim()
    const before = (facility[field] ?? '').trim()
    if (next !== before) patch[field] = next === '' ? null : next
  }
  return patch
}

const INPUT_TYPES: Partial<Record<FacilityField, string>> = {
  email: 'email',
  phone: 'tel',
  nabh_valid_until: 'date',
}

const UPPER_CASE_FIELDS: readonly FacilityField[] = ['gstin', 'pan', 'state_code']

/** Fields laid out in groups, so the form reads like the paper it is copied from. */
const FIELD_GROUPS: { title: string; fields: FacilityField[] }[] = [
  { title: 'Name', fields: ['name', 'short_name', 'legal_name'] },
  { title: 'Registered address', fields: ['address_line1', 'address_line2', 'city', 'district', 'state', 'pincode'] },
  { title: 'Tax', fields: ['gstin', 'pan', 'state_code'] },
  { title: 'Contact', fields: ['phone', 'email'] },
  {
    title: 'Registrations',
    fields: ['hfr_id', 'abdm_hip_id', 'nabh_accreditation_no', 'nabh_valid_until', 'clinical_establishment_reg_no'],
  },
  { title: 'Clock', fields: ['timezone'] },
]

const HINTS: Partial<Record<FacilityField, string>> = {
  name: 'Leave blank to use the name in the site settings file.',
  legal_name: 'As registered for GST. Bills print this as the supplier.',
  gstin: '15 characters. The GST state code and PAN are read from it.',
  timezone: "Leave blank to use this computer's timezone, which every facility keeps for now.",
}

function shown(value: string | null | undefined, empty = 'Not set'): string {
  return value && value.trim() ? value : empty
}

function IdentityRows({ facility }: { facility: Facility }) {
  const rows: [string, string][] = [
    ['Name', facility.name
      ? facility.name
      : facility.is_main && facility.site_name
        ? `${facility.site_name} (from the site settings file)`
        : 'Not set'],
    ['Code', facility.code],
    ['Legal name', shown(facility.legal_name)],
    ['Registered address', shown(facility.address)],
    ['GSTIN', shown(facility.gstin)],
    ['PAN', shown(facility.pan)],
    ['GST state code', facility.state_code
      ? `${facility.state_code}${facility.state_code_name ? `, ${facility.state_code_name}` : ''}`
      : 'Not set'],
    ['Phone', shown(facility.phone)],
    ['E-mail', shown(facility.email)],
    ['Health facility registry ID', shown(facility.hfr_id)],
    ['ABDM health information provider ID', shown(facility.abdm_hip_id)],
    ['NABH accreditation number', shown(facility.nabh_accreditation_no)],
    ['NABH accreditation valid until', facility.nabh_valid_until
      ? formatClinicalDateKey(facility.nabh_valid_until)
      : 'Not set'],
    ['Clinical establishment registration number', shown(facility.clinical_establishment_reg_no)],
    ['Timezone', shown(facility.timezone, "This computer's timezone")],
  ]
  if (facility.updated_at) {
    rows.push(['Last changed', formatClinicalDateTime(facility.updated_at) || facility.updated_at])
  }
  return (
    <dl
      style={{
        display: 'grid',
        gridTemplateColumns: 'minmax(140px, max-content) 1fr',
        gap: '6px 16px',
        margin: '12px 0 0',
      }}
    >
      {rows.map(([label, value]) => (
        <div key={label} style={{ display: 'contents' }}>
          <dt className="admin-field__label" style={{ alignSelf: 'baseline' }}>{label}</dt>
          <dd className="admin-modal__text" style={{ margin: 0, overflowWrap: 'anywhere' }}>{value}</dd>
        </div>
      ))}
    </dl>
  )
}

interface CardProps {
  facility: Facility
  onSaved: (next: Facility) => void
}

function IdentityCard({ facility, onSaved }: CardProps) {
  const [draft, setDraft] = useState<Draft | null>(null)
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const [fieldErrors, setFieldErrors] = useState<Partial<Record<string, string>>>({})
  const [notice, setNotice] = useState<string | null>(null)

  const startEdit = () => {
    setDraft(draftOf(facility))
    setFormError(null)
    setFieldErrors({})
    setNotice(null)
  }

  const save = async (e: FormEvent) => {
    e.preventDefault()
    if (!draft) return
    const patch = patchOf(facility, draft)
    if (Object.keys(patch).length === 0) {
      setDraft(null)
      setNotice('Nothing was changed.')
      return
    }
    setSaving(true)
    setFormError(null)
    setFieldErrors({})
    try {
      const next = await updateFacility(facility.id, patch)
      setDraft(null)
      setNotice('Saved. Bills and receipts numbered from now on carry these details.')
      onSaved(next)
    } catch (err) {
      const errors = fieldErrorsOf(err)
      if (errors.length) {
        const byField: Partial<Record<string, string>> = {}
        for (const fe of errors) byField[fe.field] = byField[fe.field] ?? fe.message
        setFieldErrors(byField)
        setFormError('Some details need correcting before they can be saved.')
      } else {
        setFormError(errorText(err, 'Could not save the details.'))
      }
    } finally {
      setSaving(false)
    }
  }

  const titleId = `facility-${facility.id}-title`
  return (
    <section
      className="glass-card"
      style={{ padding: 16 }}
      aria-labelledby={titleId}
      data-testid={`facility-card-${facility.id}`}
    >
      <div className="admin-filter-row" style={{ justifyContent: 'space-between', flexWrap: 'wrap', gap: 8 }}>
        <h3 className="admin-section__title" id={titleId}>
          {facility.display_name}
          {!facility.is_active && ' (closed)'}
        </h3>
        {facility.can_edit && !draft && (
          <button type="button" className="admin-btn-ghost" onClick={startEdit}>
            Edit details
          </button>
        )}
      </div>

      {facility.invoice_ready ? (
        <p className="admin-modal__text" style={{ marginTop: 8 }}>
          Bills and receipts carry this facility&apos;s legal name, registered address and GSTIN.
        </p>
      ) : (
        <p className="admin-modal__text" style={{ marginTop: 8 }} data-testid="facility-not-ready">
          Bills print &quot;Not configured&quot; until these are filled in:{' '}
          {facility.missing_for_invoice.join(', ')}. Until then a bill says it is not a GST tax invoice.
        </p>
      )}
      <p className="admin-section__hint" style={{ marginTop: 4 }}>
        A bill or receipt that already has a number keeps the details it was issued with.
        Changes here apply to documents numbered from now on.
      </p>
      {notice && <p className="admin-modal__text" role="status" style={{ marginTop: 8 }}>{notice}</p>}

      {!draft && <IdentityRows facility={facility} />}

      {draft && (
        <form className="admin-form" onSubmit={save} style={{ marginTop: 12 }}
              aria-label={`Details of ${facility.display_name}`} noValidate>
          {FIELD_GROUPS.map(group => (
            <fieldset key={group.title} style={{ border: 0, padding: 0, margin: 0 }}>
              <legend className="admin-section__title" style={{ marginBottom: 8 }}>{group.title}</legend>
              <div className="admin-form__row">
                {group.fields.map(field => {
                  const inputId = `facility-${facility.id}-${field}`
                  const errorId = `${inputId}-error`
                  const hintId = `${inputId}-hint`
                  const error = fieldErrors[field]
                  const describedBy = [error ? errorId : null, HINTS[field] ? hintId : null]
                    .filter(Boolean).join(' ') || undefined
                  return (
                    <div className="admin-field" key={field}>
                      <label className="admin-field__label" htmlFor={inputId}>
                        {FACILITY_FIELD_LABELS[field]}
                      </label>
                      <input
                        id={inputId}
                        className="admin-filter-input"
                        type={INPUT_TYPES[field] ?? 'text'}
                        value={draft[field]}
                        onChange={e => {
                          const raw = e.target.value
                          const value = UPPER_CASE_FIELDS.includes(field) ? raw.toUpperCase() : raw
                          setDraft({ ...draft, [field]: value })
                        }}
                        autoCapitalize={UPPER_CASE_FIELDS.includes(field) ? 'characters' : undefined}
                        spellCheck={UPPER_CASE_FIELDS.includes(field) ? false : undefined}
                        aria-invalid={error ? true : undefined}
                        aria-describedby={describedBy}
                      />
                      {HINTS[field] && (
                        <span className="admin-section__hint" id={hintId}>{HINTS[field]}</span>
                      )}
                      {error && <span className="admin-form__error" id={errorId}>{error}</span>}
                    </div>
                  )
                })}
              </div>
            </fieldset>
          ))}
          {formError && <p className="admin-form__error" role="alert">{formError}</p>}
          <div className="admin-modal__actions">
            <button type="button" className="admin-btn-ghost" onClick={() => setDraft(null)} disabled={saving}>
              Cancel
            </button>
            <button type="submit" className="admin-filter-btn" disabled={saving}>
              {saving ? 'Saving…' : 'Save details'}
            </button>
          </div>
        </form>
      )}
    </section>
  )
}

function CreateCheckSection() {
  const [check, setCheck] = useState<CreateCheck | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [hidden, setHidden] = useState(false)

  useEffect(() => {
    let alive = true
    getCreateCheck()
      .then(res => { if (alive) { setCheck(res); setError(null) } })
      .catch(err => {
        if (!alive) return
        if (isSwitchedOff(err)) setHidden(true)
        else setError(errorText(err, 'Could not check whether a facility can be added.'))
      })
    return () => { alive = false }
  }, [])

  if (hidden) return null
  return (
    <section className="glass-card" style={{ padding: 16 }} aria-labelledby="facility-add-title">
      <h3 className="admin-section__title" id="facility-add-title">Adding a facility</h3>
      {error && <p className="admin-form__error" role="alert" style={{ marginTop: 8 }}>{error}</p>}
      {!check && !error && <p className="admin-modal__text" style={{ marginTop: 8 }}>Checking…</p>}
      {check && check.allowed && (
        <p className="admin-modal__text" style={{ marginTop: 8 }}>
          Every condition for adding a facility is met.
        </p>
      )}
      {check && !check.allowed && (
        <>
          <p className="admin-modal__text" style={{ marginTop: 8 }}>
            A second facility cannot be added yet:
          </p>
          <ul className="admin-modal__text" style={{ margin: '4px 0 0', paddingLeft: 20 }}
              data-testid="facility-create-reasons">
            {check.reasons.map(r => <li key={r.code}>{r.message}</li>)}
          </ul>
        </>
      )}
    </section>
  )
}

export default function FacilitiesPanel() {
  const { multiSite, multiFacilityData } = usePlatformFlags()
  const [data, setData] = useState<FacilityList | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey(k => k + 1), [])

  useEffect(() => {
    let alive = true
    listFacilities()
      .then(res => { if (alive) { setData(res); setLoadError(null) } })
      .catch(err => { if (alive) setLoadError(errorText(err, 'Could not load the facility details.')) })
    return () => { alive = false }
  }, [reloadKey])

  const onSaved = (next: Facility) => {
    setData(current => current && {
      ...current,
      facilities: current.facilities.map(f => (f.id === next.id ? next : f)),
    })
  }

  const showAll = multiSite || multiFacilityData
  const facilities = (data?.facilities ?? []).filter(f => showAll || f.is_main)

  return (
    <div className="admin-section" aria-label="Facilities">
      <div className="admin-filter-bar glass-card" style={{ padding: '12px 16px' }}>
        <div className="admin-filter-row">
          <h2 className="admin-section__title">Facilities</h2>
          <span className="admin-page-info">
            Who the hospital is on its bills, receipts and printed records
          </span>
        </div>
      </div>

      {loadError && (
        <div className="glass-card" style={{ padding: 16 }}>
          <p className="admin-form__error" role="alert">{loadError}</p>
          <button type="button" className="admin-btn-ghost" style={{ marginTop: 8 }} onClick={reload}>
            Try again
          </button>
        </div>
      )}
      {!data && !loadError && <p className="admin-modal__text">Loading…</p>}

      {facilities.map(f => <IdentityCard key={f.id} facility={f} onSaved={onSaved} />)}

      {multiSite && <CreateCheckSection />}
    </div>
  )
}
