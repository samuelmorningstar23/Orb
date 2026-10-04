/**
 * Pharmacy store, Masters (grn-5): the drug master's commercial and regulatory
 * fields, drug classification, approvals, pharmacy stores, par levels and
 * vendors with their drug licences.
 *
 * Discovered by the panel through ./viewRegistry.ts; shown to anyone holding
 * pharmacy_store.read. The forms that change things appear only for the tier
 * that may use them (write: drug fields, classification, par levels, vendors;
 * approve: stores and deciding a downgrade), and the server refuses anyone else
 * whatever this screen shows.
 *
 * THE SERVER DECIDES. Whether a classification change applies now or waits for
 * a second approver is the server's answer, shown as it comes back. A drug's
 * name, generic name, form and strength are shown and never edited here.
 *
 * No patient data is read or shown. Dates are calendar keys
 * (formatClinicalDateKey) or stored UTC instants (formatClinicalDateTime).
 */
import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react'

import { getUser } from '../../../api/client'
import { toPharmacyStoreError } from '../../../api/pharmacyStore'
import {
  CLASSIFICATION_FIELD_LABELS,
  LICENCE_STATUS_LABELS,
  classifyDrug,
  createPharmacyStore,
  createPharmacyVendor,
  decideClassificationRequest,
  listClassificationRequests,
  listDrugs,
  listParLevels,
  listPharmacyStores,
  listPharmacyVendors,
  saveParLevel,
  setVendorDrugLicence,
  updateDrug,
  updatePharmacyStore,
  withdrawClassificationRequest,
  type AdoptionResult,
  type ClassificationChange,
  type ClassificationRequest,
  type DrugFieldChanges,
  type MastersOptions,
  type ParLevel,
  type PharmacyDrug,
  type PharmacyStoreRow,
  type PharmacyVendor,
} from '../../../api/pharmacyStore/masters'
import { formatClinicalDateKey, formatClinicalDateTime } from '../../../lib/datetime'
import { PHARMACY_STORE_APPROVE, PHARMACY_STORE_WRITE, hasPermission } from '../../../lib/roles'
import type { PharmacyStoreViewProps } from './viewRegistry'
import './MastersView.css'

export const viewId = 'masters'
export const viewLabel = 'Masters'
export const viewOrder = 90
export const viewPermission = 'pharmacy_store.read'

type Section = 'drugs' | 'approvals' | 'stores' | 'par-levels' | 'vendors'

const SECTIONS: { id: Section; label: string }[] = [
  { id: 'drugs', label: 'Drugs' },
  { id: 'approvals', label: 'Approvals' },
  { id: 'stores', label: 'Stores' },
  { id: 'par-levels', label: 'Par levels' },
  { id: 'vendors', label: 'Vendors' },
]

/** The store kinds a site may create without the ward-stock switch. */
const BASE_STORE_KINDS = ['dispensing', 'sub_store', 'ndps_vault', 'returns_quarantine']

type Fail = (err: unknown) => string

/** Turns any failure into words, and hands a switched-off module to the panel. */
function useFail(onModuleOff: () => void): Fail {
  return useCallback((err: unknown) => {
    const failure = toPharmacyStoreError(err)
    if (failure.isModuleDisabled) onModuleOff()
    return failure.message || 'Something went wrong. Nothing was saved.'
  }, [onModuleOff])
}

function wholeOrNull(text: string): number | null | 'bad' {
  const t = text.trim()
  if (!t) return null
  return /^\d+$/.test(t) ? Number(t) : 'bad'
}

function optionLabel(options: MastersOptions | null, field: string, value: string | null): string {
  if (field === 'is_essential_narcotic') return value === '1' ? 'Yes' : 'No'
  if (value === null || value === '') return options?.not_classified ?? 'Not classified'
  const list = field === 'drug_schedule' ? options?.drug_schedules : options?.ndps_categories
  return list?.find(o => o.value === value)?.label ?? value
}

function drugIdentity(d: PharmacyDrug): string {
  return [d.generic_name, d.strength, d.form].filter(Boolean).join(', ')
}

export default function MastersView({ onModuleOff, meta }: PharmacyStoreViewProps) {
  const [section, setSection] = useState<Section>('drugs')
  const fail = useFail(onModuleOff)
  const canWrite = hasPermission(PHARMACY_STORE_WRITE)
  const canApprove = hasPermission(PHARMACY_STORE_APPROVE)
  const features = (meta.features ?? {}) as Record<string, unknown>
  const wardStock = features.pharmacy_ward_stock === true

  return (
    <div className="psm-view">
      <nav className="psm-sections" aria-label="Masters sections">
        {SECTIONS.map(s => (
          <button
            key={s.id}
            type="button"
            className={`psm-section${s.id === section ? ' psm-section--on' : ''}`}
            aria-current={s.id === section ? 'page' : undefined}
            onClick={() => setSection(s.id)}
          >
            {s.label}
          </button>
        ))}
      </nav>
      {section === 'drugs' && <DrugsSection fail={fail} canWrite={canWrite} />}
      {section === 'approvals' && (
        <ApprovalsSection fail={fail} canWrite={canWrite} canApprove={canApprove} />
      )}
      {section === 'stores' && (
        <StoresSection fail={fail} canApprove={canApprove} wardStock={wardStock} />
      )}
      {section === 'par-levels' && <ParLevelsSection fail={fail} canWrite={canWrite} />}
      {section === 'vendors' && <VendorsSection fail={fail} canWrite={canWrite} />}
    </div>
  )
}

// ═════════════════════════════════════════════════════════════════════════════
//  Drugs
// ═════════════════════════════════════════════════════════════════════════════

function DrugsSection({ fail, canWrite }: { fail: Fail; canWrite: boolean }) {
  const [drugs, setDrugs] = useState<PharmacyDrug[] | null>(null)
  const [options, setOptions] = useState<MastersOptions | null>(null)
  const [search, setSearch] = useState('')
  const [error, setError] = useState('')
  const [openId, setOpenId] = useState<string | null>(null)

  const load = useCallback((term = '') => {
    listDrugs({ search: term || undefined }).then(
      res => { setError(''); setDrugs(res.drugs); setOptions(res.options) },
      err => setError(fail(err)),
    )
  }, [fail])

  useEffect(() => { load() }, [load])

  const open = drugs?.find(d => d.id === openId) ?? null

  function onSearch(e: FormEvent) {
    e.preventDefault()
    load(search.trim())
  }

  function onSaved(drug: PharmacyDrug) {
    setDrugs(list => (list ?? []).map(d => (d.id === drug.id ? drug : d)))
  }

  return (
    <section className="psm-card" aria-labelledby="psm-drugs-title">
      <div className="psm-card__head">
        <h2 id="psm-drugs-title" className="psm-card__title">Drug master</h2>
        <form className="psm-search" onSubmit={onSearch} role="search">
          <label htmlFor="psm-drug-search" className="psm-visually-hidden">Find a drug</label>
          <input
            id="psm-drug-search"
            value={search}
            onChange={e => setSearch(e.target.value)}
            placeholder="Find a drug by name"
          />
          <button type="submit" className="psm-btn">Find</button>
        </form>
      </div>
      {error && <p className="psm-error" role="alert">{error}</p>}
      {drugs === null && !error && <p className="psm-note" role="status">Loading drugs</p>}
      {drugs && drugs.length === 0 && <p className="psm-note">No drugs match.</p>}
      {drugs && drugs.length > 0 && (
        <div className="psm-table-scroll">
          <table className="psm-table">
            <thead>
              <tr>
                <th scope="col">Drug</th>
                <th scope="col">Schedule</th>
                <th scope="col">NDPS class</th>
                <th scope="col">Storage</th>
                <th scope="col">GST</th>
                <th scope="col"><span className="psm-visually-hidden">Actions</span></th>
              </tr>
            </thead>
            <tbody>
              {drugs.map(d => (
                <tr key={d.id} className={d.id === openId ? 'psm-row--open' : undefined}>
                  <td>
                    <span className="psm-strong">{d.name}</span>
                    <span className="psm-sub">{drugIdentity(d)}</span>
                    {d.is_active === 0 && <span className="psm-sub">Not in use</span>}
                  </td>
                  <td>{d.schedule_label}</td>
                  <td>{d.ndps_label}</td>
                  <td>{d.storage_label}</td>
                  <td className="psm-num">{d.gst_label}</td>
                  <td className="psm-actions-cell">
                    {d.pending_requests > 0 && (
                      <span className="psm-chip psm-chip--warn">Waiting for approval</span>
                    )}
                    <button type="button" className="psm-btn psm-btn--quiet"
                      onClick={() => setOpenId(d.id === openId ? null : d.id)}>
                      {d.id === openId ? 'Close' : 'Open'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {open && options && (
        <DrugEditor key={open.id} drug={open} options={options} canWrite={canWrite}
          fail={fail} onSaved={onSaved} />
      )}
    </section>
  )
}

interface FieldForm {
  pack: string
  purchaseUom: string
  dispenseUnit: string
  hsn: string
  gst: string
  manufacturer: string
  storage: string
  reorderLevel: string
  reorderQty: string
  leadTime: string
  active: boolean
}

function fieldForm(d: PharmacyDrug): FieldForm {
  return {
    pack: d.pack_size_units?.toString() ?? '',
    purchaseUom: d.purchase_uom ?? '',
    dispenseUnit: d.dispense_unit ?? '',
    hsn: d.hsn_code ?? '',
    gst: d.gst_rate_bp === null ? '' : String(d.gst_rate_bp / 100),
    manufacturer: d.manufacturer ?? '',
    storage: d.storage_condition ?? '',
    reorderLevel: d.reorder_level_units?.toString() ?? '',
    reorderQty: d.reorder_qty_units?.toString() ?? '',
    leadTime: d.lead_time_days?.toString() ?? '',
    active: d.is_active !== 0,
  }
}

/** The changed fields only, or a sentence saying what is wrong. */
function fieldChanges(d: PharmacyDrug, f: FieldForm): DrugFieldChanges | string {
  const out: DrugFieldChanges = {}
  const ints: [keyof DrugFieldChanges, string, number | null, string][] = [
    ['pack_size_units', f.pack, d.pack_size_units, 'Pack size'],
    ['reorder_level_units', f.reorderLevel, d.reorder_level_units, 'Reorder level'],
    ['reorder_qty_units', f.reorderQty, d.reorder_qty_units, 'Reorder quantity'],
    ['lead_time_days', f.leadTime, d.lead_time_days, 'Lead time'],
  ]
  for (const [key, text, current, label] of ints) {
    const value = wholeOrNull(text)
    if (value === 'bad') return `${label} must be a whole number.`
    if (value !== current) (out as Record<string, unknown>)[key] = value
  }
  const gstText = f.gst.trim()
  let gst: number | null = null
  if (gstText) {
    const pct = Number(gstText)
    if (!Number.isFinite(pct) || pct < 0) return 'GST rate must be a percentage.'
    gst = Math.round(pct * 100)
  }
  if (gst !== d.gst_rate_bp) out.gst_rate_bp = gst
  const texts: [keyof DrugFieldChanges, string, string | null][] = [
    ['purchase_uom', f.purchaseUom, d.purchase_uom],
    ['dispense_unit', f.dispenseUnit, d.dispense_unit],
    ['hsn_code', f.hsn, d.hsn_code],
    ['manufacturer', f.manufacturer, d.manufacturer],
    ['storage_condition', f.storage, d.storage_condition],
  ]
  for (const [key, text, current] of texts) {
    const value = text.trim() || null
    if (value !== current) (out as Record<string, unknown>)[key] = value
  }
  if (f.active !== (d.is_active !== 0)) out.is_active = f.active
  return out
}

function DrugEditor({ drug, options, canWrite, fail, onSaved }: {
  drug: PharmacyDrug
  options: MastersOptions
  canWrite: boolean
  fail: Fail
  onSaved: (d: PharmacyDrug) => void
}) {
  const [form, setForm] = useState<FieldForm>(() => fieldForm(drug))
  const [fieldMsg, setFieldMsg] = useState('')
  const [fieldErr, setFieldErr] = useState('')
  const [busy, setBusy] = useState(false)
  const [schedule, setSchedule] = useState(drug.drug_schedule ?? '')
  const [ndps, setNdps] = useState(drug.ndps_category ?? '')
  const [essential, setEssential] = useState(drug.is_essential_narcotic === 1)
  const [reason, setReason] = useState('')
  const [classMsg, setClassMsg] = useState('')
  const [classErr, setClassErr] = useState('')

  const set = (patch: Partial<FieldForm>) => setForm(f => ({ ...f, ...patch }))

  async function saveFields(e: FormEvent) {
    e.preventDefault()
    setFieldMsg(''); setFieldErr('')
    const changes = fieldChanges(drug, form)
    if (typeof changes === 'string') { setFieldErr(changes); return }
    if (Object.keys(changes).length === 0) { setFieldMsg('Nothing has changed.'); return }
    setBusy(true)
    try {
      const res = await updateDrug(drug.id, changes)
      onSaved(res.drug)
      setFieldMsg('Saved.')
    } catch (err) {
      setFieldErr(fail(err))
    } finally {
      setBusy(false)
    }
  }

  async function saveClassification(e: FormEvent) {
    e.preventDefault()
    setClassMsg(''); setClassErr('')
    const change: ClassificationChange = {}
    if (schedule && schedule !== (drug.drug_schedule ?? '')) change.drug_schedule = schedule
    if (ndps && ndps !== (drug.ndps_category ?? '')) change.ndps_category = ndps
    if (essential !== (drug.is_essential_narcotic === 1)) change.is_essential_narcotic = essential
    if (Object.keys(change).length === 0) { setClassMsg('Nothing has changed.'); return }
    if (reason.trim()) change.reason = reason.trim()
    setBusy(true)
    try {
      const res = await classifyDrug(drug.id, change)
      onSaved(res.drug)
      const words: string[] = []
      if (res.applied.length) {
        words.push(`Applied: ${res.applied.map(f => CLASSIFICATION_FIELD_LABELS[f] ?? f).join(', ')}.`)
      }
      if (res.requested.length) {
        words.push(`Sent for approval: ${res.requested.map(r => CLASSIFICATION_FIELD_LABELS[r.field] ?? r.field).join(', ')}. `
          + 'This change removes control, so another approver must approve it before it applies.')
      }
      setClassMsg(words.join(' '))
      setReason('')
    } catch (err) {
      setClassErr(fail(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="psm-editor">
      <div className="psm-identity">
        <h3 className="psm-editor__title">{drug.name}</h3>
        <p className="psm-sub">{drugIdentity(drug) || 'No generic name, strength or form recorded'}</p>
        <p className="psm-hint">
          The name, generic name, form and strength decide how allergies and interactions are
          screened, so they are not changed here.
        </p>
      </div>

      <form className="psm-form" onSubmit={saveFields} aria-label="Stock and purchase details">
        <h4 className="psm-form__title">Stock and purchase details</h4>
        <fieldset className="psm-fields" disabled={!canWrite || busy}>
          <Field id="psm-pack" label="Pack size (units)" value={form.pack} onChange={v => set({ pack: v })} inputMode="numeric" />
          <Field id="psm-puom" label="Purchase unit" value={form.purchaseUom} onChange={v => set({ purchaseUom: v })} />
          <Field id="psm-dunit" label="Dispense unit" value={form.dispenseUnit} onChange={v => set({ dispenseUnit: v })} />
          <Field id="psm-hsn" label="HSN code" value={form.hsn} onChange={v => set({ hsn: v })} inputMode="numeric" />
          <Field id="psm-gst" label="GST rate (%)" value={form.gst} onChange={v => set({ gst: v })} inputMode="decimal"
            placeholder={options.not_configured} />
          <Field id="psm-mfr" label="Manufacturer" value={form.manufacturer} onChange={v => set({ manufacturer: v })} />
          <div className="psm-field">
            <label htmlFor="psm-storage">Storage</label>
            <select id="psm-storage" value={form.storage} onChange={e => set({ storage: e.target.value })}>
              <option value="">{options.not_configured}</option>
              {options.storage_conditions.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
          </div>
          <Field id="psm-rl" label="Reorder level (units)" value={form.reorderLevel} onChange={v => set({ reorderLevel: v })} inputMode="numeric" />
          <Field id="psm-rq" label="Reorder quantity (units)" value={form.reorderQty} onChange={v => set({ reorderQty: v })} inputMode="numeric" />
          <Field id="psm-lead" label="Lead time (days)" value={form.leadTime} onChange={v => set({ leadTime: v })} inputMode="numeric" />
          <label className="psm-check">
            <input type="checkbox" checked={form.active} onChange={e => set({ active: e.target.checked })} />
            In use
          </label>
        </fieldset>
        {canWrite && <button type="submit" className="psm-btn psm-btn--primary" disabled={busy}>Save details</button>}
        {fieldErr && <p className="psm-error" role="alert">{fieldErr}</p>}
        {fieldMsg && <p className="psm-ok" role="status">{fieldMsg}</p>}
      </form>

      <form className="psm-form" onSubmit={saveClassification} aria-label="Classification">
        <h4 className="psm-form__title">Classification</h4>
        <p className="psm-hint">
          A change that adds control applies at once. A change that removes control waits for
          another approver, and is refused while the drug has stock in an NDPS vault.
        </p>
        <fieldset className="psm-fields" disabled={!canWrite || busy}>
          <div className="psm-field">
            <label htmlFor="psm-schedule">Schedule</label>
            <select id="psm-schedule" value={schedule} onChange={e => setSchedule(e.target.value)}>
              {!drug.drug_schedule && <option value="">{options.not_classified}</option>}
              {options.drug_schedules.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
          </div>
          <div className="psm-field">
            <label htmlFor="psm-ndps">NDPS class</label>
            <select id="psm-ndps" value={ndps} onChange={e => setNdps(e.target.value)}>
              {!drug.ndps_category && <option value="">{options.not_classified}</option>}
              {options.ndps_categories.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
          </div>
          <label className="psm-check">
            <input type="checkbox" checked={essential} onChange={e => setEssential(e.target.checked)} />
            Essential narcotic
          </label>
          <div className="psm-field psm-field--wide">
            <label htmlFor="psm-reason">Reason (needed when control is removed)</label>
            <textarea id="psm-reason" rows={2} value={reason} onChange={e => setReason(e.target.value)} />
          </div>
        </fieldset>
        {canWrite && <button type="submit" className="psm-btn psm-btn--primary" disabled={busy}>Save classification</button>}
        {classErr && <p className="psm-error" role="alert">{classErr}</p>}
        {classMsg && <p className="psm-ok" role="status">{classMsg}</p>}
      </form>
    </div>
  )
}

function Field({ id, label, value, onChange, inputMode, placeholder, type = 'text' }: {
  id: string
  label: string
  value: string
  onChange: (v: string) => void
  inputMode?: 'numeric' | 'decimal' | 'text'
  placeholder?: string
  type?: string
}) {
  return (
    <div className="psm-field">
      <label htmlFor={id}>{label}</label>
      <input id={id} type={type} value={value} inputMode={inputMode} placeholder={placeholder}
        onChange={e => onChange(e.target.value)} />
    </div>
  )
}

// ═════════════════════════════════════════════════════════════════════════════
//  Approvals
// ═════════════════════════════════════════════════════════════════════════════

function ApprovalsSection({ fail, canWrite, canApprove }: {
  fail: Fail
  canWrite: boolean
  canApprove: boolean
}) {
  const [requests, setRequests] = useState<ClassificationRequest[] | null>(null)
  const [options, setOptions] = useState<MastersOptions | null>(null)
  const [error, setError] = useState('')
  const [notes, setNotes] = useState<Record<number, string>>({})
  const [busyId, setBusyId] = useState<number | null>(null)
  const me = getUser()?.id ?? null

  const load = useCallback(() => {
    Promise.all([listClassificationRequests('pending'), listDrugs()]).then(
      ([req, drugs]) => { setError(''); setRequests(req.requests); setOptions(drugs.options) },
      err => setError(fail(err)),
    )
  }, [fail])

  useEffect(() => { load() }, [load])

  async function act(request: ClassificationRequest, action: 'approved' | 'refused' | 'withdraw') {
    setError('')
    setBusyId(request.id)
    try {
      if (action === 'withdraw') await withdrawClassificationRequest(request.id)
      else await decideClassificationRequest(request.id, action, notes[request.id]?.trim() || undefined)
      setRequests(list => (list ?? []).filter(r => r.id !== request.id))
    } catch (err) {
      setError(fail(err))
    } finally {
      setBusyId(null)
    }
  }

  return (
    <section className="psm-card" aria-labelledby="psm-approvals-title">
      <h2 id="psm-approvals-title" className="psm-card__title">Classification changes waiting for approval</h2>
      <p className="psm-hint">
        A change that removes control from a drug is applied only when someone other than the
        person who asked for it approves it.
      </p>
      {error && <p className="psm-error" role="alert">{error}</p>}
      {requests === null && !error && <p className="psm-note" role="status">Loading</p>}
      {requests && requests.length === 0 && <p className="psm-note">Nothing is waiting for approval.</p>}
      {requests && requests.length > 0 && (
        <ul className="psm-list">
          {requests.map(r => {
            const mine = me !== null && r.requested_by === me
            return (
              <li key={r.id} className="psm-item">
                <span className="psm-strong">{r.drug_name ?? r.helix_medication_id}</span>
                <span>
                  {CLASSIFICATION_FIELD_LABELS[r.field] ?? r.field}: {optionLabel(options, r.field, r.from_value)}
                  {' to '}{optionLabel(options, r.field, r.to_value)}
                </span>
                {r.reason && <span className="psm-sub">Reason: {r.reason}</span>}
                <span className="psm-sub">
                  Asked by {r.requested_by_name ?? r.requested_by}, {formatClinicalDateTime(r.requested_at)}
                </span>
                {mine && (
                  <span className="psm-sub">You asked for this change, so another approver must decide it.</span>
                )}
                <div className="psm-item__actions">
                  {canApprove && !mine && (
                    <>
                      <label htmlFor={`psm-note-${r.id}`} className="psm-visually-hidden">Note for this decision</label>
                      <input id={`psm-note-${r.id}`} className="psm-inline-input" placeholder="Note (needed to refuse)"
                        value={notes[r.id] ?? ''} onChange={e => setNotes(n => ({ ...n, [r.id]: e.target.value }))} />
                      <button type="button" className="psm-btn psm-btn--primary" disabled={busyId === r.id}
                        onClick={() => act(r, 'approved')}>Approve</button>
                      <button type="button" className="psm-btn" disabled={busyId === r.id}
                        onClick={() => act(r, 'refused')}>Refuse</button>
                    </>
                  )}
                  {canWrite && mine && (
                    <button type="button" className="psm-btn psm-btn--quiet" disabled={busyId === r.id}
                      onClick={() => act(r, 'withdraw')}>Withdraw</button>
                  )}
                </div>
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}

// ═════════════════════════════════════════════════════════════════════════════
//  Stores
// ═════════════════════════════════════════════════════════════════════════════

function adoptionWords(a: AdoptionResult): string {
  const parts = [`${a.adopted.length} lot${a.adopted.length === 1 ? '' : 's'} moved into the dispensing store`]
  const merged = a.merged.filter(m => m.units > 0).length
  if (merged) parts.push(`${merged} duplicate lot${merged === 1 ? '' : 's'} merged into the same batch`)
  if (a.conflicts.length) {
    parts.push(`${a.conflicts.length} batch${a.conflicts.length === 1 ? '' : 'es'} left where ${a.conflicts.length === 1 ? 'it was' : 'they were'} because the same batch is held in more than one status`)
  }
  return `${parts.join('; ')}.`
}

function StoresSection({ fail, canApprove, wardStock }: {
  fail: Fail
  canApprove: boolean
  wardStock: boolean
}) {
  const [stores, setStores] = useState<PharmacyStoreRow[] | null>(null)
  const [legacy, setLegacy] = useState(0)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const [form, setForm] = useState({ code: '', name: '', kind: 'dispensing', department: '', ward: '' })

  const load = useCallback(() => {
    listPharmacyStores().then(
      res => { setStores(res.stores); setLegacy(res.legacy_lots) },
      err => setError(fail(err)),
    )
  }, [fail])

  useEffect(() => { load() }, [load])

  const kinds = wardStock ? [...BASE_STORE_KINDS, 'ward_imprest', 'crash_cart'] : BASE_STORE_KINDS
  const wardKind = form.kind === 'ward_imprest' || form.kind === 'crash_cart'

  async function create(e: FormEvent) {
    e.preventDefault()
    setError(''); setMessage('')
    if (!form.code.trim() || !form.name.trim()) { setError('Enter a code and a name.'); return }
    setBusy(true)
    try {
      const res = await createPharmacyStore({
        code: form.code.trim(), name: form.name.trim(), pharmacy_kind: form.kind,
        department: form.department.trim() || undefined, ward: form.ward.trim() || undefined,
      })
      setMessage(`${res.store.name} created.${res.adoption ? ` ${adoptionWords(res.adoption)}` : ''}`)
      setForm({ code: '', name: '', kind: 'sub_store', department: '', ward: '' })
      load()
    } catch (err) {
      setError(fail(err))
    } finally {
      setBusy(false)
    }
  }

  async function toggle(store: PharmacyStoreRow) {
    setError(''); setMessage('')
    setBusy(true)
    try {
      const res = await updatePharmacyStore(store.code, { is_active: store.is_active === 0 })
      setMessage(`${res.store.name} ${res.store.is_active ? 'is in use again' : 'is closed'}.${res.adoption ? ` ${adoptionWords(res.adoption)}` : ''}`)
      load()
    } catch (err) {
      setError(fail(err))
    } finally {
      setBusy(false)
    }
  }

  const kindLabel = (k: string) => KIND_LABELS[k] ?? k
  const hasDispensing = (stores ?? []).some(s => s.pharmacy_kind === 'dispensing' && s.is_active)

  return (
    <section className="psm-card" aria-labelledby="psm-stores-title">
      <h2 id="psm-stores-title" className="psm-card__title">Pharmacy stores</h2>
      {legacy > 0 && !hasDispensing && (
        <p className="psm-notice">
          {legacy} lot{legacy === 1 ? ' is' : 's are'} not in any store yet. They are dispensed as
          before. Creating the dispensing store moves them into it.
        </p>
      )}
      {legacy > 0 && hasDispensing && (
        <p className="psm-notice">
          {legacy} lot{legacy === 1 ? ' was' : 's were'} left out of the dispensing store because the
          same batch is held in more than one status. {legacy === 1 ? 'It stays' : 'They stay'} as{' '}
          {legacy === 1 ? 'it was' : 'they were'}.
        </p>
      )}
      {error && <p className="psm-error" role="alert">{error}</p>}
      {message && <p className="psm-ok" role="status">{message}</p>}
      {stores === null && !error && <p className="psm-note" role="status">Loading stores</p>}
      {stores && stores.length === 0 && <p className="psm-note">No pharmacy stores yet.</p>}
      {stores && stores.length > 0 && (
        <div className="psm-table-scroll">
          <table className="psm-table">
            <thead>
              <tr>
                <th scope="col">Store</th>
                <th scope="col">Kind</th>
                <th scope="col">Department</th>
                <th scope="col">Units on hand</th>
                <th scope="col">Status</th>
                <th scope="col"><span className="psm-visually-hidden">Actions</span></th>
              </tr>
            </thead>
            <tbody>
              {stores.map(s => (
                <tr key={s.id}>
                  <td><span className="psm-strong">{s.name}</span><span className="psm-sub">{s.code}</span></td>
                  <td>{s.kind_label}</td>
                  <td>{s.department ?? 'Not set'}</td>
                  <td className="psm-num">{s.units_on_hand.toLocaleString('en-IN')}</td>
                  <td>{s.is_active ? 'In use' : 'Closed'}</td>
                  <td className="psm-actions-cell">
                    {canApprove && (
                      <button type="button" className="psm-btn psm-btn--quiet" disabled={busy}
                        onClick={() => toggle(s)}>{s.is_active ? 'Close' : 'Reopen'}</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {canApprove && (
        <form className="psm-form" onSubmit={create} aria-label="Add a pharmacy store">
          <h3 className="psm-form__title">Add a pharmacy store</h3>
          <fieldset className="psm-fields" disabled={busy}>
            <Field id="psm-store-code" label="Code" value={form.code} onChange={v => setForm(f => ({ ...f, code: v }))} />
            <Field id="psm-store-name" label="Name" value={form.name} onChange={v => setForm(f => ({ ...f, name: v }))} />
            <div className="psm-field">
              <label htmlFor="psm-store-kind">Kind</label>
              <select id="psm-store-kind" value={form.kind} onChange={e => setForm(f => ({ ...f, kind: e.target.value }))}>
                {kinds.map(k => <option key={k} value={k}>{kindLabel(k)}</option>)}
              </select>
            </div>
            <Field id="psm-store-dept" label={wardKind ? 'Department (needed)' : 'Department'} value={form.department}
              onChange={v => setForm(f => ({ ...f, department: v }))} />
            <Field id="psm-store-ward" label="Ward" value={form.ward} onChange={v => setForm(f => ({ ...f, ward: v }))} />
          </fieldset>
          <button type="submit" className="psm-btn psm-btn--primary" disabled={busy}>Add store</button>
        </form>
      )}
    </section>
  )
}

const KIND_LABELS: Record<string, string> = {
  dispensing: 'Dispensing pharmacy',
  sub_store: 'Sub-store',
  ndps_vault: 'NDPS vault',
  returns_quarantine: 'Returns quarantine',
  ward_imprest: 'Ward stock',
  crash_cart: 'Crash cart',
}

// ═════════════════════════════════════════════════════════════════════════════
//  Par levels
// ═════════════════════════════════════════════════════════════════════════════

function ParLevelsSection({ fail, canWrite }: { fail: Fail; canWrite: boolean }) {
  const [stores, setStores] = useState<PharmacyStoreRow[]>([])
  const [drugs, setDrugs] = useState<PharmacyDrug[]>([])
  const [levels, setLevels] = useState<ParLevel[] | null>(null)
  const [storeId, setStoreId] = useState('')
  const [form, setForm] = useState({ drug: '', par: '', reorder: '', max: '' })
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    Promise.all([listPharmacyStores(), listDrugs()]).then(
      ([s, d]) => { setStores(s.stores.filter(x => x.is_active)); setDrugs(d.drugs) },
      err => setError(fail(err)),
    )
  }, [fail])

  const load = useCallback((id: string) => {
    listParLevels(id ? Number(id) : undefined).then(
      res => setLevels(res.par_levels),
      err => setError(fail(err)),
    )
  }, [fail])

  useEffect(() => { load(storeId) }, [load, storeId])

  async function save(e: FormEvent) {
    e.preventDefault()
    setError(''); setMessage('')
    const par = wholeOrNull(form.par)
    const reorder = wholeOrNull(form.reorder)
    const max = wholeOrNull(form.max)
    if (!storeId || !form.drug) { setError('Choose a store and a drug.'); return }
    if (par === null || par === 'bad' || reorder === 'bad' || max === 'bad') {
      setError('Levels are whole numbers of units, and the par level is needed.')
      return
    }
    setBusy(true)
    try {
      await saveParLevel({ store_id: Number(storeId), helix_medication_id: form.drug, par_units: par,
        reorder_level_units: reorder, max_units: max })
      setMessage('Saved.')
      setForm({ drug: '', par: '', reorder: '', max: '' })
      load(storeId)
    } catch (err) {
      setError(fail(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="psm-card" aria-labelledby="psm-par-title">
      <h2 id="psm-par-title" className="psm-card__title">Par levels</h2>
      <div className="psm-field psm-field--narrow">
        <label htmlFor="psm-par-store">Store</label>
        <select id="psm-par-store" value={storeId} onChange={e => setStoreId(e.target.value)}>
          <option value="">Every store</option>
          {stores.map(s => <option key={s.id} value={String(s.id)}>{s.name}</option>)}
        </select>
      </div>
      {error && <p className="psm-error" role="alert">{error}</p>}
      {message && <p className="psm-ok" role="status">{message}</p>}
      {levels && levels.length === 0 && <p className="psm-note">No par levels set.</p>}
      {levels && levels.length > 0 && (
        <div className="psm-table-scroll">
          <table className="psm-table">
            <thead>
              <tr>
                <th scope="col">Store</th>
                <th scope="col">Drug</th>
                <th scope="col">Par</th>
                <th scope="col">Reorder at</th>
                <th scope="col">Maximum</th>
              </tr>
            </thead>
            <tbody>
              {levels.map(l => (
                <tr key={l.id}>
                  <td>{l.store_name}</td>
                  <td>{l.drug_name ?? l.helix_medication_id}</td>
                  <td className="psm-num">{l.par_units}</td>
                  <td className="psm-num">{l.reorder_level_units ?? 'No reorder level set'}</td>
                  <td className="psm-num">{l.max_units ?? 'Not set'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {canWrite && stores.length > 0 && (
        <form className="psm-form" onSubmit={save} aria-label="Set a par level">
          <h3 className="psm-form__title">Set a par level{storeId ? '' : ' (choose a store first)'}</h3>
          <fieldset className="psm-fields" disabled={busy || !storeId}>
            <div className="psm-field">
              <label htmlFor="psm-par-drug">Drug</label>
              <select id="psm-par-drug" value={form.drug} onChange={e => setForm(f => ({ ...f, drug: e.target.value }))}>
                <option value="">Choose a drug</option>
                {drugs.map(d => <option key={d.id} value={d.id}>{d.name}</option>)}
              </select>
            </div>
            <Field id="psm-par-par" label="Par (units)" value={form.par} onChange={v => setForm(f => ({ ...f, par: v }))} inputMode="numeric" />
            <Field id="psm-par-reorder" label="Reorder at (units)" value={form.reorder} onChange={v => setForm(f => ({ ...f, reorder: v }))} inputMode="numeric" />
            <Field id="psm-par-max" label="Maximum (units)" value={form.max} onChange={v => setForm(f => ({ ...f, max: v }))} inputMode="numeric" />
          </fieldset>
          <button type="submit" className="psm-btn psm-btn--primary" disabled={busy || !storeId}>Save par level</button>
        </form>
      )}
    </section>
  )
}

// ═════════════════════════════════════════════════════════════════════════════
//  Vendors
// ═════════════════════════════════════════════════════════════════════════════

const LICENCE_CHIP: Record<string, string> = {
  none: 'psm-chip--warn',
  expired: 'psm-chip--danger',
  expiring: 'psm-chip--warn',
  valid: 'psm-chip--ok',
}

function VendorsSection({ fail, canWrite }: { fail: Fail; canWrite: boolean }) {
  const [vendors, setVendors] = useState<PharmacyVendor[] | null>(null)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const [form, setForm] = useState({ code: '', name: '', gstin: '', phone: '', licence: '', validTo: '', ndps: '' })
  const [licenceFor, setLicenceFor] = useState<string | null>(null)
  const [licence, setLicence] = useState({ numbers: '', validTo: '', ndps: '' })

  const load = useCallback(() => {
    listPharmacyVendors().then(res => setVendors(res.vendors), err => setError(fail(err)))
  }, [fail])

  useEffect(() => { load() }, [load])

  const sorted = useMemo(() => vendors ?? [], [vendors])

  async function create(e: FormEvent) {
    e.preventDefault()
    setError(''); setMessage('')
    if (!form.code.trim() || !form.name.trim()) { setError('Enter a code and a name.'); return }
    setBusy(true)
    try {
      const res = await createPharmacyVendor({
        code: form.code.trim(), name: form.name.trim(), vendor_type: 'distributor',
        gstin: form.gstin.trim() || undefined, contact_phone: form.phone.trim() || undefined,
        drug_licence_numbers: form.licence.trim() || undefined,
        drug_licence_valid_to: form.validTo || undefined,
        ndps_licence_number: form.ndps.trim() || undefined,
      })
      setMessage(`${res.vendor.name} added.`)
      setForm({ code: '', name: '', gstin: '', phone: '', licence: '', validTo: '', ndps: '' })
      load()
    } catch (err) {
      setError(fail(err))
    } finally {
      setBusy(false)
    }
  }

  async function saveLicence(e: FormEvent, code: string) {
    e.preventDefault()
    setError(''); setMessage('')
    setBusy(true)
    try {
      const res = await setVendorDrugLicence(code, {
        drug_licence_numbers: licence.numbers.trim() || undefined,
        drug_licence_valid_to: licence.validTo || undefined,
        ndps_licence_number: licence.ndps.trim() || undefined,
      })
      setMessage(`Licence saved for ${res.vendor.name}.`)
      setLicenceFor(null)
      load()
    } catch (err) {
      setError(fail(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="psm-card" aria-labelledby="psm-vendors-title">
      <h2 id="psm-vendors-title" className="psm-card__title">Vendors and drug licences</h2>
      {error && <p className="psm-error" role="alert">{error}</p>}
      {message && <p className="psm-ok" role="status">{message}</p>}
      {vendors === null && !error && <p className="psm-note" role="status">Loading vendors</p>}
      {vendors && vendors.length === 0 && <p className="psm-note">No vendors yet.</p>}
      {sorted.length > 0 && (
        <ul className="psm-list">
          {sorted.map(v => (
            <li key={v.id} className="psm-item">
              <span className="psm-strong">{v.name}</span>
              <span className="psm-sub">{v.code}{v.gstin ? `, GSTIN ${v.gstin}` : ''}</span>
              <span>
                <span className={`psm-chip ${LICENCE_CHIP[v.licence_status] ?? ''}`}>
                  {LICENCE_STATUS_LABELS[v.licence_status]}
                </span>
                {v.drug_licence_valid_to && (
                  <span className="psm-sub">Valid to {formatClinicalDateKey(v.drug_licence_valid_to)}</span>
                )}
                {v.drug_licence_numbers && <span className="psm-sub">Licence {v.drug_licence_numbers}</span>}
                {v.ndps_licence_number && <span className="psm-sub">NDPS licence {v.ndps_licence_number}</span>}
              </span>
              {canWrite && licenceFor !== v.code && (
                <div className="psm-item__actions">
                  <button type="button" className="psm-btn psm-btn--quiet" onClick={() => {
                    setLicenceFor(v.code)
                    setLicence({ numbers: v.drug_licence_numbers ?? '', validTo: v.drug_licence_valid_to ?? '', ndps: v.ndps_licence_number ?? '' })
                  }}>Update licence</button>
                </div>
              )}
              {canWrite && licenceFor === v.code && (
                <form className="psm-form psm-form--inline" onSubmit={e => saveLicence(e, v.code)}
                  aria-label={`Drug licence for ${v.name}`}>
                  <fieldset className="psm-fields" disabled={busy}>
                    <Field id={`psm-lic-${v.id}`} label="Licence numbers" value={licence.numbers}
                      onChange={val => setLicence(l => ({ ...l, numbers: val }))} />
                    <Field id={`psm-lic-to-${v.id}`} label="Valid to" type="date" value={licence.validTo}
                      onChange={val => setLicence(l => ({ ...l, validTo: val }))} />
                    <Field id={`psm-lic-ndps-${v.id}`} label="NDPS licence" value={licence.ndps}
                      onChange={val => setLicence(l => ({ ...l, ndps: val }))} />
                  </fieldset>
                  <div className="psm-item__actions">
                    <button type="submit" className="psm-btn psm-btn--primary" disabled={busy}>Save licence</button>
                    <button type="button" className="psm-btn psm-btn--quiet" onClick={() => setLicenceFor(null)}>Cancel</button>
                  </div>
                </form>
              )}
            </li>
          ))}
        </ul>
      )}
      {canWrite && (
        <form className="psm-form" onSubmit={create} aria-label="Add a vendor">
          <h3 className="psm-form__title">Add a vendor</h3>
          <fieldset className="psm-fields" disabled={busy}>
            <Field id="psm-v-code" label="Code" value={form.code} onChange={v => setForm(f => ({ ...f, code: v }))} />
            <Field id="psm-v-name" label="Name" value={form.name} onChange={v => setForm(f => ({ ...f, name: v }))} />
            <Field id="psm-v-gstin" label="GSTIN" value={form.gstin} onChange={v => setForm(f => ({ ...f, gstin: v }))} />
            <Field id="psm-v-phone" label="Phone" value={form.phone} onChange={v => setForm(f => ({ ...f, phone: v }))} />
            <Field id="psm-v-lic" label="Drug licence numbers" value={form.licence} onChange={v => setForm(f => ({ ...f, licence: v }))} />
            <Field id="psm-v-to" label="Licence valid to" type="date" value={form.validTo} onChange={v => setForm(f => ({ ...f, validTo: v }))} />
            <Field id="psm-v-ndps" label="NDPS licence" value={form.ndps} onChange={v => setForm(f => ({ ...f, ndps: v }))} />
          </fieldset>
          <button type="submit" className="psm-btn psm-btn--primary" disabled={busy}>Add vendor</button>
        </form>
      )}
    </section>
  )
}
