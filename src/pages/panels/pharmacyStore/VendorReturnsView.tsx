/**
 * Pharmacy store, Vendor returns (grn-22): stock going back to the vendor who
 * supplied it.
 *
 * Discovered by the panel through ./viewRegistry.ts; shown to anyone holding
 * pharmacy_store.read. Drafting and cancelling a draft need the write tier;
 * sending a return back needs the approve tier and is never done by the
 * person who drafted it; the vendor's credit note number is recorded by
 * either tier. The server refuses anyone else whatever this screen shows, and
 * every refusal is shown in its own words.
 *
 * WHAT MAY GO BACK is the server's call: lots held off the shelf
 * (quarantined, recalled, not of standard quality, a ward return waiting for
 * inspection), expired, or near expiry. The form lists only those, never more
 * units than a lot holds. A narcotic or psychotropic return needs the NDPS
 * permission to draft and, to send, a pharmacist other than the drafter as
 * witness with their own signing PIN.
 *
 * No patient data is read or shown. Expiry dates are calendar keys
 * (formatClinicalDateKey); recorded times are bare UTC from the server and go
 * through formatClinicalDateTime. Money is integer paise.
 */
import { useCallback, useEffect, useState, type FormEvent } from 'react'

import { getUser } from '../../../api/client'
import { toPharmacyStoreError } from '../../../api/pharmacyStore'
import {
  VENDOR_RETURN_STATUS_LABELS,
  cancelVendorReturn,
  createVendorReturn,
  getReturnableStock,
  getVendorReturn,
  getVendorReturnOptions,
  listVendorReturns,
  newVendorReturnKey,
  postVendorReturn,
  recordVendorCredit,
  type ReturnableStock,
  type VendorReturn,
  type VendorReturnFilter,
  type VendorReturnOptions,
  type VendorReturnResult,
  type VendorReturnStatus,
  type VendorReturnWarning,
} from '../../../api/pharmacyStore/vendorReturns'
import { formatClinicalDateKey, formatClinicalDateTime } from '../../../lib/datetime'
import { formatInr } from '../../../lib/money'
import {
  PHARMACY_STORE_APPROVE,
  PHARMACY_STORE_NDPS,
  PHARMACY_STORE_WRITE,
  hasPermission,
} from '../../../lib/roles'
import type { PharmacyStoreViewProps } from './viewRegistry'
import WitnessFields from './WitnessFields'
import './stockAndPurchasing.css'

export const viewId = 'vendor-returns'
export const viewLabel = 'Vendor returns'
export const viewOrder = 85
export const viewPermission = 'pharmacy_store.read'

type Section = 'returns' | 'new'
type Fail = (err: unknown) => string

function useFail(onModuleOff: () => void): Fail {
  return useCallback((err: unknown) => {
    const failure = toPharmacyStoreError(err)
    if (failure.isModuleDisabled) onModuleOff()
    const text = failure.message || 'Something went wrong. Nothing was saved.'
    return text.charAt(0).toUpperCase() + text.slice(1)
  }, [onModuleOff])
}

const STATUS_FILTERS: { value: VendorReturnFilter; label: string }[] = [
  { value: '', label: 'All' },
  { value: 'draft', label: 'Drafts' },
  { value: 'posted', label: 'Sent back' },
  { value: 'credited', label: 'Credit note received' },
  { value: 'cancelled', label: 'Cancelled' },
]

function statusTone(status: VendorReturnStatus): string {
  if (status === 'cancelled') return 'psw-chip--danger'
  if (status === 'credited') return 'psw-chip--ok'
  return 'psw-chip--warn'
}

function money(paise: number | null | undefined): string {
  return paise === null || paise === undefined ? 'Cost not recorded' : formatInr(paise)
}

function whoAndWhen(name: string | null, id: string | null, at: string | null): string {
  const who = name ?? id ?? 'Not recorded'
  const when = formatClinicalDateTime(at)
  return when ? `${who}, ${when}` : who
}

function isWholeNumber(text: string): boolean {
  return /^\d+$/.test(text.trim())
}

export default function VendorReturnsView({ onModuleOff }: PharmacyStoreViewProps) {
  const [section, setSection] = useState<Section>('returns')
  const [openId, setOpenId] = useState<number | null>(null)
  const [notice, setNotice] = useState('')
  const [options, setOptions] = useState<VendorReturnOptions | null>(null)
  const [optionsError, setOptionsError] = useState('')
  const fail = useFail(onModuleOff)
  const canWrite = hasPermission(PHARMACY_STORE_WRITE)

  useEffect(() => {
    let live = true
    getVendorReturnOptions().then(
      res => { if (live) { setOptionsError(''); setOptions(res) } },
      err => { if (live) setOptionsError(fail(err)) },
    )
    return () => { live = false }
  }, [fail])

  function onCreated(doc: VendorReturn, warnings: VendorReturnWarning[]) {
    setOpenId(doc.id)
    setNotice(['Draft saved. Somebody with approval rights sends it back.',
      ...warnings.map(w => w.message)].join(' '))
    setSection('returns')
  }

  const sections: { id: Section; label: string }[] = [
    { id: 'returns', label: 'Returns' },
    ...(canWrite ? [{ id: 'new' as Section, label: 'New return' }] : []),
  ]

  return (
    <div className="psw-view">
      <nav className="psw-sections" aria-label="Vendor return sections">
        {sections.map(s => (
          <button
            key={s.id}
            type="button"
            className={`psw-section${s.id === section ? ' psw-section--on' : ''}`}
            aria-current={s.id === section ? 'page' : undefined}
            onClick={() => { setNotice(''); setSection(s.id) }}
          >
            {s.label}
          </button>
        ))}
      </nav>
      {notice && <p className="psw-ok" role="status">{notice}</p>}
      {section === 'returns' && (
        <ReturnsSection fail={fail} witnesses={options?.witnesses ?? []} openId={openId}
          setOpenId={setOpenId} />
      )}
      {section === 'new' && canWrite && (
        <NewReturnSection fail={fail} options={options} optionsError={optionsError}
          onCreated={onCreated} />
      )}
    </div>
  )
}

// ═════════════════════════════════════════════════════════════════════════════
//  Returns list and one return
// ═════════════════════════════════════════════════════════════════════════════

function ReturnsSection({ fail, witnesses, openId, setOpenId }: {
  fail: Fail
  witnesses: { id: string; name: string }[]
  openId: number | null
  setOpenId: (id: number | null) => void
}) {
  const [status, setStatus] = useState<VendorReturnFilter>('')
  const [rows, setRows] = useState<VendorReturn[] | null>(null)
  const [error, setError] = useState('')

  const load = useCallback(() => {
    listVendorReturns({ status }).then(
      res => { setError(''); setRows(res.vendor_returns) },
      err => setError(fail(err)),
    )
  }, [fail, status])

  useEffect(() => { load() }, [load])

  return (
    <section className="psw-card" aria-labelledby="psw-vr-title">
      <div className="psw-card__head">
        <h2 id="psw-vr-title" className="psw-card__title">Returns to vendors</h2>
        <div className="psw-toolbar">
          <label htmlFor="psw-vr-status" className="psw-visually-hidden">Show returns</label>
          <select id="psw-vr-status" value={status}
            onChange={e => setStatus(e.target.value as VendorReturnFilter)}>
            {STATUS_FILTERS.map(f => <option key={f.value} value={f.value}>{f.label}</option>)}
          </select>
        </div>
      </div>
      {error && <p className="psw-error" role="alert">{error}</p>}
      {rows === null && !error && <p className="psw-note" role="status">Loading returns</p>}
      {rows && rows.length === 0 && <p className="psw-note">No returns here yet.</p>}
      {rows && rows.length > 0 && (
        <div className="psw-table-scroll">
          <table className="psw-table">
            <thead>
              <tr>
                <th scope="col">Return</th>
                <th scope="col">Vendor</th>
                <th scope="col">From</th>
                <th scope="col">Status</th>
                <th scope="col" className="psw-num">Units</th>
                <th scope="col"><span className="psw-visually-hidden">Actions</span></th>
              </tr>
            </thead>
            <tbody>
              {rows.map(r => (
                <tr key={r.id} className={r.id === openId ? 'psw-row--open' : undefined}>
                  <td>
                    <span className="psw-strong">{r.return_number ?? 'Not numbered yet'}</span>
                    <span className="psw-sub">{formatClinicalDateTime(r.created_at)}</span>
                  </td>
                  <td>{r.vendor_name ?? r.vendor_code}</td>
                  <td>{r.store_name ?? r.store_code}</td>
                  <td>
                    <span className={`psw-chip ${statusTone(r.status)}`}>
                      {VENDOR_RETURN_STATUS_LABELS[r.status] ?? r.status_label}
                    </span>
                    {r.controlled && <span className="psw-sub">Controlled stock</span>}
                  </td>
                  <td className="psw-num">{r.total_units}</td>
                  <td className="psw-actions-cell">
                    <button type="button" className="psw-btn psw-btn--quiet"
                      aria-expanded={r.id === openId}
                      onClick={() => setOpenId(r.id === openId ? null : r.id)}>
                      {r.id === openId ? 'Close' : 'Open'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {openId !== null && (
        <ReturnDetail key={openId} returnId={openId} fail={fail} witnesses={witnesses}
          onChanged={load} />
      )}
    </section>
  )
}

type Mode = 'none' | 'send' | 'credit' | 'cancel'

function ReturnDetail({ returnId, fail, witnesses, onChanged }: {
  returnId: number
  fail: Fail
  witnesses: { id: string; name: string }[]
  onChanged: () => void
}) {
  const [doc, setDoc] = useState<VendorReturn | null>(null)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const [mode, setMode] = useState<Mode>('none')
  const [text, setText] = useState('')
  const [witnessId, setWitnessId] = useState('')
  const [pin, setPin] = useState('')
  const me = getUser()?.id ?? null
  const canWrite = hasPermission(PHARMACY_STORE_WRITE)
  const canApprove = hasPermission(PHARMACY_STORE_APPROVE)
  const mayNdps = hasPermission(PHARMACY_STORE_NDPS)

  useEffect(() => {
    let live = true
    getVendorReturn(returnId).then(
      res => { if (live) { setError(''); setDoc(res.vendor_return) } },
      err => { if (live) setError(fail(err)) },
    )
    return () => { live = false }
  }, [returnId, fail])

  function startMode(next: Mode) {
    setError('')
    setMessage('')
    setText('')
    setPin('')
    setMode(next)
  }

  function act(run: () => Promise<VendorReturnResult>, done: string) {
    setBusy(true)
    setError('')
    setMessage('')
    run().then(
      res => {
        setDoc(res.vendor_return)
        const extra = res.warnings.map(w => w.message)
        setMessage([res.replayed ? 'This was already done; nothing new was recorded.' : done,
          ...extra].join(' '))
        setMode('none')
        setText('')
        onChanged()
      },
      err => setError(fail(err)),
    ).finally(() => { setBusy(false); setPin('') })
  }

  function onSend(e: FormEvent) {
    e.preventDefault()
    if (!doc) return
    let witness: { witness_id: string; witness_pin: string } | null = null
    if (doc.witness_required) {
      if (!witnessId) { setError('Choose the pharmacist who witnesses this.'); return }
      if (!pin.trim()) { setError('The witness enters their signing PIN.'); return }
      witness = { witness_id: witnessId, witness_pin: pin.trim() }
    }
    act(() => postVendorReturn(returnId, witness), 'Sent back. The units have left the shelf.')
  }

  function onCredit(e: FormEvent) {
    e.preventDefault()
    if (!text.trim()) { setError('Enter the credit note number from the vendor.'); return }
    act(() => recordVendorCredit(returnId, text.trim()), 'Credit note recorded.')
  }

  function onCancel(e: FormEvent) {
    e.preventDefault()
    if (!text.trim()) { setError('Say why the return is being cancelled.'); return }
    act(() => cancelVendorReturn(returnId, text.trim()), 'Return cancelled.')
  }

  if (!doc) {
    return (
      <div className="psw-detail">
        {error ? <p className="psw-error" role="alert">{error}</p>
          : <p className="psw-note" role="status">Loading the return</p>}
      </div>
    )
  }

  const draftedByMe = me !== null && doc.created_by === me
  const lines = doc.lines ?? []
  const witnessChoices = witnesses.filter(w => w.id !== doc.created_by)

  return (
    <div className="psw-detail" aria-labelledby="psw-vr-detail-title">
      <h3 id="psw-vr-detail-title" className="psw-card__title">
        {doc.return_number ? `Return ${doc.return_number}` : 'Return not numbered yet'}
      </h3>
      {doc.return_number && doc.number_series_configured === false && (
        <p className="psw-notice psw-notice--warn">
          This number comes from a series the site has not set up yet. Ask the administrator
          to configure the vendor return series.
        </p>
      )}
      {doc.controlled && (
        <p className="psw-notice">
          Controlled stock. Sending it back needs a second pharmacist, not the one who drafted
          it, as witness with their signing PIN.
        </p>
      )}
      <dl className="psw-facts">
        <div><dt>Status</dt><dd>{VENDOR_RETURN_STATUS_LABELS[doc.status] ?? doc.status_label}</dd></div>
        <div><dt>Vendor</dt><dd>{doc.vendor_name ?? doc.vendor_code}</dd></div>
        <div><dt>From</dt><dd>{doc.store_name ?? doc.store_code}</dd></div>
        <div><dt>Why</dt><dd>{doc.reason ?? 'Not recorded'}</dd></div>
        <div><dt>Value</dt><dd>{money(doc.total_paise)}</dd></div>
        <div><dt>Drafted by</dt><dd>{whoAndWhen(doc.created_by_name, doc.created_by, doc.created_at)}</dd></div>
        {doc.posted_by && (
          <div><dt>Sent back by</dt><dd>{whoAndWhen(doc.posted_by_name, doc.posted_by, doc.posted_at)}</dd></div>
        )}
        {doc.vendor_credit_note_ref && (
          <div><dt>Credit note</dt><dd>{doc.vendor_credit_note_ref}</dd></div>
        )}
        {doc.cancel_reason && <div><dt>Cancelled because</dt><dd>{doc.cancel_reason}</dd></div>}
        {doc.remarks && <div><dt>Remarks</dt><dd>{doc.remarks}</dd></div>}
      </dl>

      <div className="psw-table-scroll">
        <table className="psw-table">
          <thead>
            <tr>
              <th scope="col">Drug</th>
              <th scope="col">Batch</th>
              <th scope="col">Expiry</th>
              <th scope="col" className="psw-num">Units</th>
              <th scope="col" className="psw-num">Value</th>
            </tr>
          </thead>
          <tbody>
            {lines.map(line => (
              <tr key={line.id}>
                <td>
                  <span className="psw-strong">{line.drug_name}</span>
                  <span className="psw-sub">
                    {[line.reason, line.ndps ? 'Narcotic or psychotropic' : null].filter(Boolean).join('. ')}
                  </span>
                </td>
                <td>{line.batch_no ?? 'Not recorded'}</td>
                <td>{formatClinicalDateKey(line.expiry_date) || 'Not recorded'}</td>
                <td className="psw-num">{line.quantity_units}</td>
                <td className="psw-num">{money(line.value_paise)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {error && <p className="psw-error" role="alert">{error}</p>}
      {message && <p className="psw-ok" role="status">{message}</p>}

      {mode === 'none' && (
        <div className="psw-buttons">
          {doc.status === 'draft' && canApprove && !draftedByMe && (
            <button type="button" className="psw-btn psw-btn--primary" disabled={busy}
              onClick={() => {
                if (doc.witness_required) startMode('send')
                else act(() => postVendorReturn(returnId, null), 'Sent back. The units have left the shelf.')
              }}>
              Send back
            </button>
          )}
          {doc.status === 'posted' && (canWrite || canApprove) && (
            <button type="button" className="psw-btn" disabled={busy} onClick={() => startMode('credit')}>
              Record credit note
            </button>
          )}
          {doc.status === 'draft' && canWrite && (!doc.controlled || mayNdps) && (
            <button type="button" className="psw-btn psw-btn--quiet" disabled={busy}
              onClick={() => startMode('cancel')}>
              Cancel return
            </button>
          )}
        </div>
      )}
      {doc.status === 'draft' && draftedByMe && (
        <p className="psw-hint">You drafted this return, so somebody else with approval rights sends it back.</p>
      )}
      {doc.status === 'draft' && !canApprove && !draftedByMe && (
        <p className="psw-hint">Somebody with approval rights sends this return back.</p>
      )}

      {mode === 'send' && (
        <form className="psw-form" onSubmit={onSend} noValidate>
          <WitnessFields idPrefix="psw-vr-send" witnesses={witnessChoices} witnessId={witnessId}
            setWitnessId={setWitnessId} pin={pin} setPin={setPin} />
          <div className="psw-buttons">
            <button type="submit" className="psw-btn psw-btn--primary" disabled={busy}>Send back</button>
            <button type="button" className="psw-btn psw-btn--quiet" disabled={busy}
              onClick={() => setMode('none')}>
              Not now
            </button>
          </div>
        </form>
      )}
      {mode === 'credit' && (
        <form className="psw-form" onSubmit={onCredit} noValidate>
          <div className="psw-field">
            <label htmlFor="psw-vr-credit">Credit note number</label>
            <input id="psw-vr-credit" value={text} onChange={e => setText(e.target.value)} />
          </div>
          <div className="psw-buttons">
            <button type="submit" className="psw-btn psw-btn--primary" disabled={busy}>Record</button>
            <button type="button" className="psw-btn psw-btn--quiet" disabled={busy}
              onClick={() => setMode('none')}>
              Not now
            </button>
          </div>
        </form>
      )}
      {mode === 'cancel' && (
        <form className="psw-form" onSubmit={onCancel} noValidate>
          <div className="psw-field psw-field--wide">
            <label htmlFor="psw-vr-cancel">Why is it being cancelled?</label>
            <textarea id="psw-vr-cancel" rows={2} value={text} onChange={e => setText(e.target.value)} />
          </div>
          <div className="psw-buttons">
            <button type="submit" className="psw-btn" disabled={busy}>Cancel the return</button>
            <button type="button" className="psw-btn psw-btn--quiet" disabled={busy}
              onClick={() => { setMode('none'); setText('') }}>
              Keep it
            </button>
          </div>
        </form>
      )}
    </div>
  )
}

// ═════════════════════════════════════════════════════════════════════════════
//  New return
// ═════════════════════════════════════════════════════════════════════════════

interface Pick {
  on: boolean
  units: string
}

function NewReturnSection({ fail, options, optionsError, onCreated }: {
  fail: Fail
  options: VendorReturnOptions | null
  optionsError: string
  onCreated: (doc: VendorReturn, warnings: VendorReturnWarning[]) => void
}) {
  const [storeCode, setStoreCode] = useState('')
  const [vendorCode, setVendorCode] = useState('')
  const [stock, setStock] = useState<ReturnableStock | null>(null)
  const [picks, setPicks] = useState<Record<string, Pick>>({})
  const [reason, setReason] = useState('')
  const [remarks, setRemarks] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [requestKey, setRequestKey] = useState(newVendorReturnKey)
  const mayNdps = hasPermission(PHARMACY_STORE_NDPS)

  useEffect(() => {
    if (!storeCode) return
    let live = true
    getReturnableStock(storeCode).then(
      res => { if (live) { setError(''); setStock(res); setPicks({}) } },
      err => { if (live) setError(fail(err)) },
    )
    return () => { live = false }
  }, [storeCode, fail])

  function patch(lotId: string, next: Partial<Pick>) {
    setPicks(prev => {
      const base: Pick = prev[lotId] ?? { on: false, units: '' }
      return { ...prev, [lotId]: { ...base, ...next } }
    })
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault()
    if (!storeCode) { setError('Choose the store the stock goes back from.'); return }
    if (!vendorCode) { setError('Choose the vendor it goes back to.'); return }
    if (!reason.trim()) { setError('Say why the stock goes back.'); return }
    const lines: { lot_id: string; quantity_units: number }[] = []
    for (const lot of stock?.lots ?? []) {
      const pick = picks[lot.lot_id]
      if (!pick?.on) continue
      if (!isWholeNumber(pick.units) || Number(pick.units) < 1) {
        setError(`${lot.drug_name}, batch ${lot.batch_no}: units are a whole number of at least 1.`)
        return
      }
      if (Number(pick.units) > lot.quantity_units) {
        setError(`${lot.drug_name}, batch ${lot.batch_no}: the lot holds ${lot.quantity_units} units.`)
        return
      }
      lines.push({ lot_id: lot.lot_id, quantity_units: Number(pick.units) })
    }
    if (lines.length === 0) { setError('Tick at least one lot to send back.'); return }
    setBusy(true)
    setError('')
    createVendorReturn({ store_code: storeCode, vendor_code: vendorCode, reason: reason.trim(),
      remarks: remarks.trim() || null, lines, idempotency_key: requestKey }).then(
      res => {
        setRequestKey(newVendorReturnKey())
        onCreated(res.vendor_return, res.warnings)
      },
      err => setError(fail(err)),
    ).finally(() => setBusy(false))
  }

  return (
    <section className="psw-card" aria-labelledby="psw-vr-new-title">
      <h2 id="psw-vr-new-title" className="psw-card__title">New return to a vendor</h2>
      <p className="psw-hint">
        {`Only stock held off the shelf, expired, or within ${options?.near_expiry_days ?? 90} days of expiry goes back. Nothing leaves the shelf until the return is sent back.`}
      </p>
      {optionsError && <p className="psw-error" role="alert">{optionsError}</p>}
      <form className="psw-form" onSubmit={onSubmit} noValidate>
        <div className="psw-fields">
          <div className="psw-field">
            <label htmlFor="psw-vr-store">Store</label>
            <select id="psw-vr-store" value={storeCode}
              onChange={e => { setStock(null); setPicks({}); setStoreCode(e.target.value) }}>
              <option value="">Choose a store</option>
              {(options?.stores ?? []).map(s => <option key={s.code} value={s.code}>{s.name}</option>)}
            </select>
          </div>
          <div className="psw-field">
            <label htmlFor="psw-vr-vendor">Vendor</label>
            <select id="psw-vr-vendor" value={vendorCode} onChange={e => setVendorCode(e.target.value)}>
              <option value="">Choose a vendor</option>
              {(options?.vendors ?? []).map(v => <option key={v.code} value={v.code}>{v.name}</option>)}
            </select>
          </div>
        </div>
        {stock && stock.lots.length === 0 && (
          <p className="psw-note">Nothing in this store is held, expired or near expiry.</p>
        )}
        {stock && stock.lots.length > 0 && (
          <ul className="psw-lines" aria-label="Lots that may go back">
            {stock.lots.map(lot => {
              const pick = picks[lot.lot_id] ?? { on: false, units: '' }
              const blocked = lot.ndps && !mayNdps
              return (
                <li key={lot.lot_id} className="psw-line">
                  <label className="psw-check">
                    <input type="checkbox" checked={pick.on} disabled={blocked}
                      onChange={e => patch(lot.lot_id, { on: e.target.checked,
                        units: pick.units || String(lot.quantity_units) })} />
                    {`${lot.drug_name}, batch ${lot.batch_no}: ${lot.quantity_units} units. ${lot.why_label}`}
                  </label>
                  <span className="psw-sub">
                    {[lot.expiry_date ? `Expiry ${formatClinicalDateKey(lot.expiry_date)}` : null,
                      lot.vendor_name ? `From ${lot.vendor_name}` : 'No vendor on file',
                      lot.ndps ? 'Narcotic or psychotropic' : null].filter(Boolean).join('. ')}
                  </span>
                  {blocked && (
                    <span className="psw-sub">A pharmacist with the NDPS permission returns this lot.</span>
                  )}
                  {pick.on && (
                    <div className="psw-field">
                      <label htmlFor={`psw-vr-units-${lot.lot_id}`}>Units to send back</label>
                      <input id={`psw-vr-units-${lot.lot_id}`} inputMode="numeric" value={pick.units}
                        onChange={e => patch(lot.lot_id, { units: e.target.value })} />
                    </div>
                  )}
                </li>
              )
            })}
          </ul>
        )}
        <div className="psw-fields">
          <div className="psw-field psw-field--wide">
            <label htmlFor="psw-vr-reason">Why does it go back?</label>
            <textarea id="psw-vr-reason" rows={2} value={reason} onChange={e => setReason(e.target.value)} />
          </div>
          <div className="psw-field psw-field--wide">
            <label htmlFor="psw-vr-remarks">Remarks</label>
            <input id="psw-vr-remarks" value={remarks} onChange={e => setRemarks(e.target.value)} />
          </div>
        </div>
        {error && <p className="psw-error" role="alert">{error}</p>}
        <div className="psw-buttons">
          <button type="submit" className="psw-btn psw-btn--primary" disabled={busy}>Save draft</button>
        </div>
      </form>
    </section>
  )
}
