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
 *
 * Also with the switch on, each facility card has its document numbers
 * (multisite-7a): the series its bills, receipts and other documents are
 * numbered from, loaded when opened. An administrator of the facility edits a
 * prefix, the digits or whether it is the hospital's real series, and sets up
 * a second facility's own series. The server refuses any prefix that could
 * repeat a number and says why beside the field.
 *
 * And each facility card has its price lists (multisite-13), loaded when
 * opened: which list its patients are priced on when no payer names one (its
 * own default, else the group's), the lists that apply there with where each
 * applies, and the default price list editor. Choosing a group list other
 * than the group's default keeps it for that facility from now on, which only
 * an account with group-wide access may do; the server says why it refuses.
 */
import { Fragment, useCallback, useEffect, useState } from 'react'
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
import { listDocumentSeries, saveDocumentSeries } from '../../../api/documentSeries'
import {
  getFacilityPriceLists,
  isUnavailable,
  refusalText,
  setFacilityDefaultPriceList,
  setPriceListAppliesTo,
} from '../../../api/priceLists'
import type { DefaultSource, FacilityPriceLists, PriceList } from '../../../api/priceLists'
import type {
  DocumentSeries,
  DocumentSeriesChange,
  DocumentSeriesList,
} from '../../../api/documentSeries'
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

/* ── Document numbers (multisite-7a) ─────────────────────────────────────── */

interface SeriesDraft {
  code: string
  label: string
  /** Setting up a series this facility does not have yet. */
  creating: boolean
  prefix: string
  suffix: string
  digits: string
  real: boolean
}

const PAD_WIDTH_MIN = 1
const PAD_WIDTH_MAX = 12

/** The change to send: on an edit only what changed; on a set-up everything
 *  typed. A digits value that is not a whole number in range is refused here
 *  with the same words the server uses. */
function seriesChange(
  draft: SeriesDraft, current: DocumentSeries | null,
): DocumentSeriesChange | { digitsError: string } {
  const digits = Number(draft.digits)
  if (!/^\d+$/.test(draft.digits.trim()) || digits < PAD_WIDTH_MIN || digits > PAD_WIDTH_MAX) {
    return { digitsError: `Give a whole number of digits from ${PAD_WIDTH_MIN} to ${PAD_WIDTH_MAX}.` }
  }
  const next: DocumentSeriesChange = {
    prefix: draft.prefix, suffix: draft.suffix, pad_width: digits, is_configured: draft.real,
  }
  if (!current) return next
  const change: DocumentSeriesChange = {}
  if (next.prefix !== current.prefix) change.prefix = next.prefix
  if (next.suffix !== current.suffix) change.suffix = next.suffix
  if (next.pad_width !== current.pad_width) change.pad_width = next.pad_width
  if (next.is_configured !== current.is_configured) change.is_configured = next.is_configured
  return change
}

function SeriesSection({ facility }: { facility: Facility }) {
  const [open, setOpen] = useState(false)
  const [data, setData] = useState<DocumentSeriesList | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [hidden, setHidden] = useState(false)
  const [reloadKey, setReloadKey] = useState(0)
  const [draft, setDraft] = useState<SeriesDraft | null>(null)
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const [fieldErrors, setFieldErrors] = useState<Partial<Record<string, string>>>({})
  const [notice, setNotice] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    let alive = true
    listDocumentSeries(facility.id)
      .then(res => { if (alive) { setData(res); setLoadError(null) } })
      .catch(err => {
        if (!alive) return
        if (isSwitchedOff(err)) setHidden(true)
        else setLoadError(errorText(err, 'Could not load the document numbers.'))
      })
    return () => { alive = false }
  }, [open, facility.id, reloadKey])

  const begin = (next: SeriesDraft) => {
    setDraft(next)
    setFormError(null)
    setFieldErrors({})
    setNotice(null)
  }

  const save = async (e: FormEvent) => {
    e.preventDefault()
    if (!draft) return
    const current = draft.creating
      ? null
      : data?.series.find(s => s.series_code === draft.code) ?? null
    const change = seriesChange(draft, current)
    if ('digitsError' in change) {
      setFieldErrors({ pad_width: change.digitsError })
      setFormError('Some details need correcting before they can be saved.')
      return
    }
    if (!draft.creating && Object.keys(change).length === 0) {
      setDraft(null)
      setNotice('Nothing was changed.')
      return
    }
    setSaving(true)
    setFormError(null)
    setFieldErrors({})
    try {
      const res = await saveDocumentSeries(facility.id, draft.code, change)
      setDraft(null)
      setNotice(res.created
        ? `${res.series.label} are now numbered here. The next number is ${res.series.next_number_preview}.`
        : `Saved. The next number is ${res.series.next_number_preview}.`)
      setReloadKey(k => k + 1)
    } catch (err) {
      const errors = fieldErrorsOf(err)
      if (errors.length) {
        const byField: Partial<Record<string, string>> = {}
        for (const fe of errors) byField[fe.field] = byField[fe.field] ?? fe.message
        setFieldErrors(byField)
        setFormError('Some details need correcting before they can be saved.')
      } else {
        setFormError(errorText(err, 'Could not save the numbering.'))
      }
    } finally {
      setSaving(false)
    }
  }

  if (hidden) return null
  const titleId = `facility-${facility.id}-series-title`
  const field = (name: 'prefix' | 'suffix' | 'pad_width', label: string, hint: string,
                 value: string, onChange: (v: string) => void) => {
    const inputId = `facility-${facility.id}-series-${name}`
    const error = fieldErrors[name]
    const describedBy = [error ? `${inputId}-error` : null, `${inputId}-hint`].filter(Boolean).join(' ')
    return (
      <div className="admin-field">
        <label className="admin-field__label" htmlFor={inputId}>{label}</label>
        <input
          id={inputId} className="admin-filter-input" value={value}
          inputMode={name === 'pad_width' ? 'numeric' : undefined}
          spellCheck={false}
          onChange={e => onChange(e.target.value)}
          aria-invalid={error ? true : undefined} aria-describedby={describedBy}
        />
        <span className="admin-section__hint" id={`${inputId}-hint`}>{hint}</span>
        {error && <span className="admin-form__error" id={`${inputId}-error`}>{error}</span>}
      </div>
    )
  }

  return (
    <section
      className="glass-card" style={{ padding: 16 }} aria-labelledby={titleId}
      data-testid={`facility-series-${facility.id}`}
    >
      <div className="admin-filter-row" style={{ justifyContent: 'space-between', flexWrap: 'wrap', gap: 8 }}>
        <h3 className="admin-section__title" id={titleId}>
          Document numbers, {facility.display_name}
        </h3>
        <button
          type="button" className="admin-btn-ghost" aria-expanded={open}
          onClick={() => setOpen(o => !o)}
        >
          {open ? 'Hide document numbers' : 'Show document numbers'}
        </button>
      </div>
      {open && (
        <>
          <p className="admin-section__hint" style={{ marginTop: 4 }}>
            Each facility numbers its own bills, receipts and other documents. A prefix may not
            be the same as another facility&apos;s, begin with it or be the start of it, and may
            not begin a number already issued, so no number is ever given twice. Hospital
            numbers (UHID) are one series for the whole group.
          </p>
          {notice && <p className="admin-modal__text" role="status" style={{ marginTop: 8 }}>{notice}</p>}
          {loadError && (
            <div style={{ marginTop: 8 }}>
              <p className="admin-form__error" role="alert">{loadError}</p>
              <button type="button" className="admin-btn-ghost" style={{ marginTop: 8 }}
                      onClick={() => setReloadKey(k => k + 1)}>
                Try again
              </button>
            </div>
          )}
          {!data && !loadError && <p className="admin-modal__text" style={{ marginTop: 8 }}>Loading…</p>}
          {data && data.series.length === 0 && (
            <p className="admin-modal__text" style={{ marginTop: 8 }}>
              No document numbers are set up at this facility yet.
            </p>
          )}
          {data && data.series.length > 0 && (
            <div className="admin-table-wrap" style={{ marginTop: 8 }}>
              <table className="admin-table">
                <thead>
                  <tr>
                    <th scope="col">Document</th>
                    <th scope="col">Next number</th>
                    <th scope="col">Series</th>
                    <th scope="col"><span className="sr-only">Action</span></th>
                  </tr>
                </thead>
                <tbody>
                  {data.series.map(s => (
                    <tr key={s.series_code}>
                      <td>{s.label}{s.group_wide ? ' (whole group)' : ''}</td>
                      <td style={{ fontVariantNumeric: 'tabular-nums' }}>{s.next_number_preview}</td>
                      <td>
                        {s.is_configured
                          ? "The hospital's real series"
                          : 'Placeholder, not yet set by the hospital'}
                      </td>
                      <td>
                        {data.can_edit && !draft && (
                          <button
                            type="button" className="admin-btn-ghost"
                            onClick={() => begin({
                              code: s.series_code, label: s.label, creating: false,
                              prefix: s.prefix, suffix: s.suffix, digits: String(s.pad_width),
                              real: s.is_configured,
                            })}
                          >
                            Edit<span className="sr-only"> {s.label.toLowerCase()}</span>
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {data && data.can_edit && data.can_add.length > 0 && !draft && (
            <div style={{ marginTop: 12 }}>
              <p className="admin-modal__text">Not numbered at this facility yet:</p>
              <ul className="admin-modal__text" style={{ margin: '4px 0 0', paddingLeft: 20 }}
                  data-testid={`facility-series-to-add-${facility.id}`}>
                {data.can_add.map(a => (
                  <li key={a.series_code} style={{ marginTop: 4 }}>
                    {a.label}{' '}
                    <button
                      type="button" className="admin-btn-ghost"
                      onClick={() => begin({
                        code: a.series_code, label: a.label, creating: true,
                        prefix: '', suffix: '', digits: '6', real: false,
                      })}
                    >
                      Set up<span className="sr-only"> {a.label.toLowerCase()}</span>
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {draft && (
            <form className="admin-form" onSubmit={save} style={{ marginTop: 12 }} noValidate
                  aria-label={`${draft.creating ? 'Set up' : 'Edit'} numbering for ${draft.label.toLowerCase()}`}>
              <p className="admin-section__title">{draft.label}</p>
              <div className="admin-form__row">
                {field('prefix', 'Prefix', 'Up to 16 letters, digits or / . _ -',
                       draft.prefix, v => setDraft({ ...draft, prefix: v }))}
                {field('suffix', 'Suffix', 'Optional, up to 8 characters',
                       draft.suffix, v => setDraft({ ...draft, suffix: v }))}
                {field('pad_width', 'Digits', `The number is padded to this many digits (${PAD_WIDTH_MIN} to ${PAD_WIDTH_MAX})`,
                       draft.digits, v => setDraft({ ...draft, digits: v }))}
              </div>
              <label className="admin-field" style={{ flexDirection: 'row', alignItems: 'center', gap: 6 }}>
                <input
                  type="checkbox" checked={draft.real}
                  onChange={e => setDraft({ ...draft, real: e.target.checked })}
                />
                <span>This is the hospital&apos;s real series, not a placeholder</span>
              </label>
              {formError && <p className="admin-form__error" role="alert">{formError}</p>}
              <div className="admin-modal__actions">
                <button type="button" className="admin-btn-ghost" onClick={() => setDraft(null)} disabled={saving}>
                  Cancel
                </button>
                <button type="submit" className="admin-filter-btn" disabled={saving}>
                  {saving ? 'Saving…' : draft.creating ? 'Set up numbering' : 'Save numbering'}
                </button>
              </div>
            </form>
          )}
        </>
      )}
    </section>
  )
}

/* ── Price lists (multisite-13) ──────────────────────────────────────────── */

const SOURCE_WORDS: Record<DefaultSource, string> = {
  facility: "this facility's own default",
  group: "the group's default",
  first_active: 'the first list in use, because no list is marked as a default',
  none: '',
}

function planLabel(plan: PriceList): string {
  return `${plan.name} (${plan.code})`
}

/** The editor's choices: '' is the group's default; then this facility's own
 *  lists; then, for an account with group-wide access, the other group lists
 *  no payer prices on (choosing one keeps it for this facility). */
function defaultChoices(data: FacilityPriceLists): { value: string; label: string }[] {
  const groupDefault = data.plans.find(p => p.facility_id === null && p.is_default)
  const out = [{
    value: '',
    label: groupDefault ? `The group's default, ${planLabel(groupDefault)}` : "The group's default",
  }]
  for (const p of data.plans) {
    if (p.facility_id === data.facility.id) out.push({ value: String(p.id), label: planLabel(p) })
  }
  if (data.group_wide) {
    for (const p of data.plans) {
      if (p.facility_id === null && !p.is_default && p.used_by_payers === 0) {
        out.push({ value: String(p.id), label: `${planLabel(p)}, to be kept for this facility` })
      }
    }
  }
  return out
}

function currentChoice(data: FacilityPriceLists): string {
  return data.default_source === 'facility' && data.default_plan ? String(data.default_plan.id) : ''
}

function PriceListSection({ facility }: { facility: Facility }) {
  const [open, setOpen] = useState(false)
  const [data, setData] = useState<FacilityPriceLists | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [hidden, setHidden] = useState(false)
  const [reloadKey, setReloadKey] = useState(0)
  const [choice, setChoice] = useState('')
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    let alive = true
    getFacilityPriceLists(facility.id)
      .then(res => {
        if (!alive) return
        setData(res)
        setChoice(currentChoice(res))
        setLoadError(null)
      })
      .catch(err => {
        if (!alive) return
        if (isUnavailable(err)) setHidden(true)
        else setLoadError(errorText(err, 'Could not load the price lists.'))
      })
    return () => { alive = false }
  }, [open, facility.id, reloadKey])

  const save = async (e: FormEvent) => {
    e.preventDefault()
    if (!data) return
    if (choice === currentChoice(data)) {
      setNotice('Nothing was changed.')
      return
    }
    setSaving(true)
    setFormError(null)
    setNotice(null)
    try {
      const res = await setFacilityDefaultPriceList(facility.id, choice === '' ? null : Number(choice))
      setData(res)
      setChoice(currentChoice(res))
      const name = res.default_plan ? planLabel(res.default_plan) : 'no price list'
      setNotice(res.moved
        ? `Saved. ${name} now applies to this facility only and is its default.`
        : `Saved. Bills raised here from now on are priced on ${name}.`)
    } catch (err) {
      setFormError(refusalText(err, 'Could not save the default price list.'))
    } finally {
      setSaving(false)
    }
  }

  const makeGroupWide = async (plan: PriceList) => {
    setSaving(true)
    setFormError(null)
    setNotice(null)
    try {
      await setPriceListAppliesTo(plan.id, null)
      setNotice(`${planLabel(plan)} now applies to the whole group.`)
      setReloadKey(k => k + 1)
    } catch (err) {
      setFormError(refusalText(err, 'Could not change where the price list applies.'))
    } finally {
      setSaving(false)
    }
  }

  if (hidden) return null
  const titleId = `facility-${facility.id}-prices-title`
  const selectId = `facility-${facility.id}-default-price-list`
  return (
    <section
      className="glass-card" style={{ padding: 16 }} aria-labelledby={titleId}
      data-testid={`facility-price-lists-${facility.id}`}
    >
      <div className="admin-filter-row" style={{ justifyContent: 'space-between', flexWrap: 'wrap', gap: 8 }}>
        <h3 className="admin-section__title" id={titleId}>
          Price lists, {facility.display_name}
        </h3>
        <button
          type="button" className="admin-btn-ghost" aria-expanded={open}
          onClick={() => setOpen(o => !o)}
        >
          {open ? 'Hide price lists' : 'Show price lists'}
        </button>
      </div>
      {open && (
        <>
          <p className="admin-section__hint" style={{ marginTop: 4 }}>
            A price list applies to the whole group or to one facility. Unless a patient&apos;s
            payer names a list, a patient is priced on their facility&apos;s own default list,
            else on the group&apos;s default.
          </p>
          {notice && <p className="admin-modal__text" role="status" style={{ marginTop: 8 }}>{notice}</p>}
          {loadError && (
            <div style={{ marginTop: 8 }}>
              <p className="admin-form__error" role="alert">{loadError}</p>
              <button type="button" className="admin-btn-ghost" style={{ marginTop: 8 }}
                      onClick={() => setReloadKey(k => k + 1)}>
                Try again
              </button>
            </div>
          )}
          {!data && !loadError && <p className="admin-modal__text" style={{ marginTop: 8 }}>Loading…</p>}
          {data && (
            <p className="admin-modal__text" style={{ marginTop: 8 }} data-testid={`facility-price-default-${facility.id}`}>
              {data.default_plan
                ? `Patients billed here are priced on ${planLabel(data.default_plan)}, ${SOURCE_WORDS[data.default_source]}.`
                : 'No price list applies here yet. Items are billed at their own default rate, and an item without one is refused.'}
            </p>
          )}
          {data && data.plans.length > 0 && (
            <div className="admin-table-wrap" style={{ marginTop: 8 }}>
              <table className="admin-table">
                <thead>
                  <tr>
                    <th scope="col">Price list</th>
                    <th scope="col">Applies to</th>
                    <th scope="col">Default</th>
                    <th scope="col"><span className="sr-only">Action</span></th>
                  </tr>
                </thead>
                <tbody>
                  {data.plans.map(p => (
                    <tr key={p.id}>
                      <td>{planLabel(p)}</td>
                      <td>{p.applies_to}</td>
                      <td>
                        {p.id === data.default_plan?.id
                          ? 'Prices patients here'
                          : p.is_default && p.facility_id === null
                            ? "The group's default"
                            : ''}
                      </td>
                      <td>
                        {data.can_edit && data.group_wide && p.facility_id === facility.id && (
                          <button
                            type="button" className="admin-btn-ghost" disabled={saving}
                            onClick={() => { void makeGroupWide(p) }}
                          >
                            Make group-wide<span className="sr-only"> {planLabel(p)}</span>
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {data && data.can_edit && (
            <form className="admin-form" onSubmit={save} style={{ marginTop: 12 }} noValidate
                  aria-label={`Default price list of ${facility.display_name}`}>
              <div className="admin-field">
                <label className="admin-field__label" htmlFor={selectId}>Default price list</label>
                <select
                  id={selectId} className="admin-filter-input" value={choice}
                  onChange={e => setChoice(e.target.value)}
                  aria-describedby={data.group_wide ? `${selectId}-hint` : undefined}
                >
                  {defaultChoices(data).map(c => <option key={c.value} value={c.value}>{c.label}</option>)}
                </select>
                {data.group_wide && (
                  <span className="admin-section__hint" id={`${selectId}-hint`}>
                    Choosing a group list other than the group&apos;s default keeps it for this
                    facility from now on. A list a payer prices on stays with the whole group.
                  </span>
                )}
              </div>
              {formError && <p className="admin-form__error" role="alert">{formError}</p>}
              <div className="admin-modal__actions">
                <button type="submit" className="admin-filter-btn" disabled={saving}>
                  {saving ? 'Saving…' : 'Save default price list'}
                </button>
              </div>
            </form>
          )}
        </>
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

      {facilities.map(f => (
        <Fragment key={f.id}>
          <IdentityCard facility={f} onSaved={onSaved} />
          {multiSite && <SeriesSection facility={f} />}
          {multiSite && <PriceListSection facility={f} />}
        </Fragment>
      ))}

      {multiSite && <CreateCheckSection />}
    </div>
  )
}
