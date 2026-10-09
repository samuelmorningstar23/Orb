/**
 * Pharmacy store, NDPS register (grn-13): narcotic and psychotropic drugs.
 *
 * Discovered by the panel through ./viewRegistry.ts and shown only to holders
 * of pharmacy_store.ndps. The server writes the register itself, inside every
 * stock movement of a controlled drug, while the register is on. This screen
 * shows the register against the shelf, the entries, the variances waiting
 * for reconciliation, and an export; and it makes the few entries a person
 * makes, each witnessed by a second pharmacist with their signing PIN.
 *
 * THE SWITCH IS SAID, NEVER HIDDEN. While the hospital has not chosen what
 * happens when an entry cannot be written during a dispense (ruling GRN-RS7)
 * the register is off, and the screen says so in words. Practice entries
 * written on a computer that is not the hospital's live system carry an
 * honesty label: they are not the statutory register.
 *
 * Patient names appear only on register entries, and only for patients in the
 * reader's department; the server audits every read that names one. Recorded
 * times are bare UTC from the server and go through formatClinicalDateTime.
 */
import { useCallback, useEffect, useState, type FormEvent } from 'react'

import { toPharmacyStoreError } from '../../../api/pharmacyStore'
import {
  NDPS_BALANCE_STATUS_LABELS,
  checkNdpsAgainstShelf,
  downloadNdpsRegister,
  getNdpsBalances,
  getNdpsOptions,
  getNdpsRegister,
  listNdpsVariances,
  openNdpsRegister,
  reconcileNdpsVariance,
  reportNdpsVarianceIncident,
  type NdpsBalanceRow,
  type NdpsBalanceStatus,
  type NdpsBalances,
  type NdpsEntry,
  type NdpsOptions,
  type NdpsVariance,
  type NdpsVarianceFilter,
} from '../../../api/pharmacyStore/ndps'
import { formatClinicalDateTime } from '../../../lib/datetime'
import type { PharmacyStoreViewProps } from './viewRegistry'
import WitnessFields from './WitnessFields'
import './stockAndPurchasing.css'

export const viewId = 'ndps'
export const viewLabel = 'NDPS register'
export const viewOrder = 80
export const viewPermission = 'pharmacy_store.ndps'

type Section = 'balances' | 'register' | 'variances'
type Fail = (err: unknown) => string

function useFail(onModuleOff: () => void): Fail {
  return useCallback((err: unknown) => {
    const failure = toPharmacyStoreError(err)
    if (failure.isModuleDisabled) onModuleOff()
    const text = failure.message || 'Something went wrong. Nothing was saved.'
    return text.charAt(0).toUpperCase() + text.slice(1)
  }, [onModuleOff])
}

function balanceTone(status: NdpsBalanceStatus): string {
  if (status === 'matches') return 'psw-chip--ok'
  if (status === 'not_opened') return 'psw-chip--warn'
  return 'psw-chip--danger'
}

function signed(units: number): string {
  return units > 0 ? `+${units}` : String(units)
}

function drugWords(name: string, strength: string | null, form: string | null): string {
  const lower = name.toLowerCase()
  const extra = [strength, form].filter((part): part is string =>
    Boolean(part) && !lower.includes(String(part).toLowerCase()))
  return extra.length ? `${name} (${extra.join(', ')})` : name
}

function switchWords(options: NdpsOptions): string {
  if (options.register_on) {
    return options.failure_mode === 'record'
      ? 'The register is on. If an entry cannot be written during a dispense, the dispense goes ahead and a variance is raised here for reconciliation the same day.'
      : 'The register is on. If an entry cannot be written during a dispense, the dispense is stopped until the register is put right.'
  }
  if (options.requested) {
    return 'The NDPS register was asked for on this site but stays off: the hospital has not yet chosen what happens when an entry cannot be written during a dispense. Nothing is entered until it does.'
  }
  return 'The NDPS register is off on this site. Nothing is entered until the hospital switches it on and chooses what happens when an entry cannot be written during a dispense.'
}

export default function NdpsView({ onModuleOff }: PharmacyStoreViewProps) {
  const [section, setSection] = useState<Section>('balances')
  const [options, setOptions] = useState<NdpsOptions | null>(null)
  const [optionsError, setOptionsError] = useState('')
  const fail = useFail(onModuleOff)

  useEffect(() => {
    let live = true
    getNdpsOptions().then(
      res => { if (live) { setOptionsError(''); setOptions(res) } },
      err => { if (live) setOptionsError(fail(err)) },
    )
    return () => { live = false }
  }, [fail])

  const sections: { id: Section; label: string }[] = [
    { id: 'balances', label: 'Balances' },
    { id: 'register', label: 'Register' },
    { id: 'variances', label: 'Variances' },
  ]

  return (
    <div className="psw-view">
      {optionsError && <p className="psw-error" role="alert">{optionsError}</p>}
      {options && (
        <p className={`psw-notice${options.register_on ? '' : ' psw-notice--warn'}`}>
          {switchWords(options)}
        </p>
      )}
      {options && options.lane !== 'live' && (
        <p className="psw-notice psw-notice--warn">
          Practice entries. This computer is not set up as the hospital&apos;s live system, so
          these entries are not the statutory register, and resetting the practice data clears
          them.
        </p>
      )}
      <nav className="psw-sections" aria-label="NDPS register sections">
        {sections.map(s => (
          <button
            key={s.id}
            type="button"
            className={`psw-section${s.id === section ? ' psw-section--on' : ''}`}
            aria-current={s.id === section ? 'page' : undefined}
            onClick={() => setSection(s.id)}
          >
            {s.label}
          </button>
        ))}
      </nav>
      {section === 'balances' && <BalancesSection fail={fail} options={options} />}
      {section === 'register' && <RegisterSection fail={fail} options={options} />}
      {section === 'variances' && <VariancesSection fail={fail} options={options} />}
    </div>
  )
}

// ═════════════════════════════════════════════════════════════════════════════
//  Balances: the register against the shelf
// ═════════════════════════════════════════════════════════════════════════════

function BalancesSection({ fail, options }: { fail: Fail; options: NdpsOptions | null }) {
  const [report, setReport] = useState<NdpsBalances | null>(null)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const [opening, setOpening] = useState(false)
  const [reason, setReason] = useState('')
  const [storeId, setStoreId] = useState('')
  const [witnessId, setWitnessId] = useState('')
  const [pin, setPin] = useState('')
  const registerOn = options?.register_on === true

  const load = useCallback(() => {
    getNdpsBalances().then(
      res => { setError(''); setReport(res) },
      err => setError(fail(err)),
    )
  }, [fail])

  useEffect(() => { load() }, [load])

  function onCheck() {
    setBusy(true)
    setError('')
    setMessage('')
    checkNdpsAgainstShelf().then(
      res => {
        setReport(res.balances)
        setMessage(res.raised.length === 0
          ? 'Checked. No new difference between the register and the shelf.'
          : `Checked. ${res.raised.length} difference${res.raised.length === 1 ? '' : 's'} raised as variances for reconciliation.`)
      },
      err => setError(fail(err)),
    ).finally(() => setBusy(false))
  }

  function onOpen(e: FormEvent) {
    e.preventDefault()
    if (!reason.trim()) { setError('Say why the register is being opened.'); return }
    if (!witnessId) { setError('Choose the pharmacist who witnesses this.'); return }
    if (!pin.trim()) { setError('The witness enters their signing PIN.'); return }
    setBusy(true)
    setError('')
    setMessage('')
    openNdpsRegister({ reason: reason.trim(), witness_id: witnessId, witness_pin: pin.trim(),
      store_id: storeId ? Number(storeId) : null }).then(
      res => {
        setReport(res.balances)
        const raised = res.opened.filter(o => o.variance_raised).length
        setMessage(`Register opened from the shelf for ${res.opened.length} drug${res.opened.length === 1 ? '' : 's'}.`
          + (raised === 0 ? ''
            : ` ${raised} difference${raised === 1 ? '' : 's'} from the old register raised as variances for reconciliation.`))
        setOpening(false)
        setReason('')
      },
      err => setError(fail(err)),
    ).finally(() => { setBusy(false); setPin('') })
  }

  const rows = report?.rows ?? []
  return (
    <section className="psw-card" aria-labelledby="psw-ndps-balances-title">
      <div className="psw-card__head">
        <h2 id="psw-ndps-balances-title" className="psw-card__title">Register against the shelf</h2>
        {registerOn && (
          <div className="psw-buttons">
            <button type="button" className="psw-btn" disabled={busy} onClick={onCheck}>
              Check against the shelf
            </button>
            <button type="button" className="psw-btn psw-btn--quiet" disabled={busy}
              onClick={() => { setError(''); setOpening(!opening) }}>
              Open the register
            </button>
          </div>
        )}
      </div>
      <p className="psw-hint">
        Each line is one controlled drug in one store. The register&apos;s balance runs from its
        latest opening and must equal what the stock ledger holds there.
      </p>
      {error && <p className="psw-error" role="alert">{error}</p>}
      {message && <p className="psw-ok" role="status">{message}</p>}
      {report && report.unplaced_units > 0 && (
        <p className="psw-notice psw-notice--warn">
          {`${report.unplaced_units} units of controlled drugs are on lots not yet in any pharmacy store. The register is kept per store: set up the dispensing store and the NDPS vault.`}
        </p>
      )}
      {opening && registerOn && (
        <form className="psw-form" onSubmit={onOpen} noValidate>
          <p className="psw-hint">
            Opening writes the balance the stock ledger holds now as the register&apos;s opening
            for each controlled drug, witnessed. Count the shelf first: a difference found
            by counting is a stock count, not an opening. Where the register already differs
            from the shelf, that difference is raised as a variance before the register opens
            again, and stays open until it is reconciled.
          </p>
          <div className="psw-fields">
            <div className="psw-field">
              <label htmlFor="psw-ndps-open-store">Store</label>
              <select id="psw-ndps-open-store" value={storeId}
                onChange={e => setStoreId(e.target.value)}>
                <option value="">Every pharmacy store</option>
                {(options?.stores ?? []).map(s => (
                  <option key={s.id} value={String(s.id)}>{s.name}</option>
                ))}
              </select>
            </div>
            <div className="psw-field psw-field--wide">
              <label htmlFor="psw-ndps-open-reason">Why is the register being opened?</label>
              <textarea id="psw-ndps-open-reason" rows={2} value={reason}
                onChange={e => setReason(e.target.value)} />
            </div>
          </div>
          <WitnessFields idPrefix="psw-ndps-open" witnesses={options?.witnesses ?? []}
            witnessId={witnessId} setWitnessId={setWitnessId} pin={pin} setPin={setPin} />
          <div className="psw-buttons">
            <button type="submit" className="psw-btn psw-btn--primary" disabled={busy}>
              Open the register
            </button>
            <button type="button" className="psw-btn psw-btn--quiet" disabled={busy}
              onClick={() => setOpening(false)}>
              Not now
            </button>
          </div>
        </form>
      )}
      {report === null && !error && <p className="psw-note" role="status">Loading the register</p>}
      {report && rows.length === 0 && (
        <p className="psw-note">No controlled drug is held or entered in any pharmacy store.</p>
      )}
      {rows.length > 0 && (
        <div className="psw-table-scroll">
          <table className="psw-table">
            <thead>
              <tr>
                <th scope="col">Drug</th>
                <th scope="col">Store</th>
                <th scope="col" className="psw-num">Register</th>
                <th scope="col" className="psw-num">Shelf</th>
                <th scope="col">Status</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r: NdpsBalanceRow) => (
                <tr key={`${r.store_id}-${r.drug_id}`}>
                  <td>
                    <span className="psw-strong">{drugWords(r.drug_name, r.strength, r.form)}</span>
                    {!r.controlled && <span className="psw-sub">No longer classified as controlled</span>}
                  </td>
                  <td>{r.store_name}</td>
                  <td className="psw-num">{r.register_units === null ? 'Not opened' : r.register_units}</td>
                  <td className="psw-num">{r.shelf_units}</td>
                  <td>
                    <span className={`psw-chip ${balanceTone(r.status)}`}>
                      {NDPS_BALANCE_STATUS_LABELS[r.status] ?? r.status_label}
                    </span>
                    {r.open_variances > 0 && (
                      <span className="psw-sub">{`${r.open_variances} open variance${r.open_variances === 1 ? '' : 's'}`}</span>
                    )}
                    {r.opened_at && <span className="psw-sub">{`Opened ${formatClinicalDateTime(r.opened_at)}`}</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

// ═════════════════════════════════════════════════════════════════════════════
//  The register's entries and the export
// ═════════════════════════════════════════════════════════════════════════════

function isDateKey(text: string): boolean {
  return /^\d{4}-\d{2}-\d{2}$/.test(text)
}

function RegisterSection({ fail, options }: { fail: Fail; options: NdpsOptions | null }) {
  const [storeId, setStoreId] = useState('')
  const [drugId, setDrugId] = useState('')
  const [entries, setEntries] = useState<NdpsEntry[] | null>(null)
  const [nextBefore, setNextBefore] = useState<number | null>(null)
  const [error, setError] = useState('')
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')

  const filter = { store_id: storeId ? Number(storeId) : null, drug_id: drugId || null }

  const load = useCallback((before: number | null) => {
    getNdpsRegister({ store_id: storeId ? Number(storeId) : null, drug_id: drugId || null,
      before_id: before }).then(
      res => {
        setError('')
        setEntries(prev => (before === null ? res.entries : [...(prev ?? []), ...res.entries]))
        setNextBefore(res.next_before_id)
      },
      err => setError(fail(err)),
    )
  }, [fail, storeId, drugId])

  useEffect(() => { load(null) }, [load])

  function onExport(e: FormEvent) {
    e.preventDefault()
    if ((from && !isDateKey(from)) || (to && !isDateKey(to))) {
      setError('Dates are written as year, month and day, for example 2026-10-01.')
      return
    }
    setBusy(true)
    setError('')
    setMessage('')
    downloadNdpsRegister({ ...filter, from: from || null, to: to || null }).then(
      blob => {
        const url = URL.createObjectURL(blob)
        const link = document.createElement('a')
        link.href = url
        link.download = 'ndps-register.csv'
        document.body.appendChild(link)
        link.click()
        link.remove()
        URL.revokeObjectURL(url)
        setMessage('Export ready. The export is recorded in the audit log.')
      },
      err => setError(fail(err)),
    ).finally(() => setBusy(false))
  }

  return (
    <section className="psw-card" aria-labelledby="psw-ndps-register-title">
      <div className="psw-card__head">
        <h2 id="psw-ndps-register-title" className="psw-card__title">Register entries</h2>
        <div className="psw-toolbar">
          <label htmlFor="psw-ndps-store" className="psw-visually-hidden">Store</label>
          <select id="psw-ndps-store" value={storeId} onChange={e => setStoreId(e.target.value)}>
            <option value="">Every store</option>
            {(options?.stores ?? []).map(s => (
              <option key={s.id} value={String(s.id)}>{s.name}</option>
            ))}
          </select>
          <label htmlFor="psw-ndps-drug" className="psw-visually-hidden">Drug</label>
          <select id="psw-ndps-drug" value={drugId} onChange={e => setDrugId(e.target.value)}>
            <option value="">Every controlled drug</option>
            {(options?.drugs ?? []).map(d => (
              <option key={d.id} value={d.id}>{d.name}</option>
            ))}
          </select>
        </div>
      </div>
      <p className="psw-hint">
        Newest first. An entry is never edited: a mistake is put right by a later entry.
        Patients outside your department are not named.
      </p>
      {error && <p className="psw-error" role="alert">{error}</p>}
      {message && <p className="psw-ok" role="status">{message}</p>}
      {entries === null && !error && <p className="psw-note" role="status">Loading entries</p>}
      {entries && entries.length === 0 && <p className="psw-note">No entries here yet.</p>}
      {entries && entries.length > 0 && (
        <div className="psw-table-scroll">
          <table className="psw-table">
            <thead>
              <tr>
                <th scope="col">Entry</th>
                <th scope="col">Drug and store</th>
                <th scope="col" className="psw-num">Units</th>
                <th scope="col" className="psw-num">Balance</th>
                <th scope="col">From or to</th>
                <th scope="col">Made by</th>
              </tr>
            </thead>
            <tbody>
              {entries.map(e => (
                <tr key={e.id}>
                  <td>
                    <span className="psw-strong">{e.entry_label}</span>
                    <span className="psw-sub">{`No. ${e.seq}, ${formatClinicalDateTime(e.occurred_at)}`}</span>
                    {e.lane !== 'live' && <span className="psw-sub">Practice entry</span>}
                  </td>
                  <td>
                    {drugWords(e.drug_name, e.strength, e.form)}
                    <span className="psw-sub">
                      {`${e.store_name ?? 'Store'}${e.batch_no ? `, batch ${e.batch_no}` : ''}`}
                    </span>
                  </td>
                  <td className="psw-num">{e.entry_type === 'opening' ? '' : signed(e.quantity_units)}</td>
                  <td className="psw-num">{e.balance_after_units}</td>
                  <td>
                    {e.counterparty || 'None'}
                    {e.patient_mrn && <span className="psw-sub">{`Hospital number ${e.patient_mrn}`}</span>}
                    {e.prescriber_name && <span className="psw-sub">{`Prescribed by ${e.prescriber_name}`}</span>}
                  </td>
                  <td>
                    {e.actor_name ?? e.actor_id}
                    {e.witness_id && (
                      <span className="psw-sub">{`Witness ${e.witness_name ?? e.witness_id}`}</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {nextBefore !== null && (
        <div className="psw-buttons">
          <button type="button" className="psw-btn psw-btn--quiet" onClick={() => load(nextBefore)}>
            Show older entries
          </button>
        </div>
      )}
      <form className="psw-form" onSubmit={onExport} noValidate aria-label="Export the register">
        <div className="psw-fields">
          <div className="psw-field">
            <label htmlFor="psw-ndps-from">First date</label>
            <input id="psw-ndps-from" type="date" value={from} onChange={e => setFrom(e.target.value)} />
          </div>
          <div className="psw-field">
            <label htmlFor="psw-ndps-to">Last date</label>
            <input id="psw-ndps-to" type="date" value={to} onChange={e => setTo(e.target.value)} />
          </div>
        </div>
        <p className="psw-hint">
          The export follows the store and drug chosen above. It is a working copy, not the
          statutory form layout.
        </p>
        <div className="psw-buttons">
          <button type="submit" className="psw-btn" disabled={busy}>Export as a spreadsheet file</button>
        </div>
      </form>
    </section>
  )
}

// ═════════════════════════════════════════════════════════════════════════════
//  Variances
// ═════════════════════════════════════════════════════════════════════════════

const VARIANCE_FILTERS: { value: NdpsVarianceFilter; label: string }[] = [
  { value: 'open', label: 'Open' },
  { value: 'reconciled', label: 'Reconciled' },
  { value: 'all', label: 'All' },
]

function VariancesSection({ fail, options }: { fail: Fail; options: NdpsOptions | null }) {
  const [status, setStatus] = useState<NdpsVarianceFilter>('open')
  const [rows, setRows] = useState<NdpsVariance[] | null>(null)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [closing, setClosing] = useState<number | null>(null)
  const [note, setNote] = useState('')
  const [witnessId, setWitnessId] = useState('')
  const [pin, setPin] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => {
    listNdpsVariances(status).then(
      res => { setError(''); setRows(res.variances) },
      err => setError(fail(err)),
    )
  }, [fail, status])

  useEffect(() => { load() }, [load])

  function onClose(e: FormEvent) {
    e.preventDefault()
    if (closing === null) return
    if (!note.trim()) { setError('Say what was found and what was done.'); return }
    if (!witnessId) { setError('Choose the pharmacist who witnesses this.'); return }
    if (!pin.trim()) { setError('The witness enters their signing PIN.'); return }
    setBusy(true)
    setError('')
    setMessage('')
    reconcileNdpsVariance(closing, { note: note.trim(), witness_id: witnessId,
      witness_pin: pin.trim() }).then(
      () => {
        setMessage('Variance reconciled.')
        setClosing(null)
        setNote('')
        load()
      },
      err => setError(fail(err)),
    ).finally(() => { setBusy(false); setPin('') })
  }

  function onReport(id: number) {
    setBusy(true)
    setError('')
    setMessage('')
    reportNdpsVarianceIncident(id).then(
      res => { setMessage(`Reported to the incident register as ${res.incident_ref}.`); load() },
      err => setError(fail(err)),
    ).finally(() => setBusy(false))
  }

  return (
    <section className="psw-card" aria-labelledby="psw-ndps-variances-title">
      <div className="psw-card__head">
        <h2 id="psw-ndps-variances-title" className="psw-card__title">Variances</h2>
        <div className="psw-toolbar">
          <label htmlFor="psw-ndps-variance-status" className="psw-visually-hidden">Show variances</label>
          <select id="psw-ndps-variance-status" value={status}
            onChange={e => setStatus(e.target.value as NdpsVarianceFilter)}>
            {VARIANCE_FILTERS.map(f => <option key={f.value} value={f.value}>{f.label}</option>)}
          </select>
        </div>
      </div>
      <p className="psw-hint">
        A variance is a difference the register could not account for. Reconcile it the same
        day, with a second pharmacist as witness. If the register still differs from the
        shelf, open it again before you reconcile: a reconciled variance no longer accounts
        for the difference, so the next check raises it again.
      </p>
      {error && <p className="psw-error" role="alert">{error}</p>}
      {message && <p className="psw-ok" role="status">{message}</p>}
      {rows === null && !error && <p className="psw-note" role="status">Loading variances</p>}
      {rows && rows.length === 0 && (
        <p className="psw-note">{status === 'open' ? 'No open variances.' : 'No variances here.'}</p>
      )}
      {rows && rows.length > 0 && (
        <ul className="psw-lines" aria-label="Variances">
          {rows.map(v => (
            <li key={v.id} className="psw-line">
              <span className="psw-strong">{`${v.drug_name}: ${v.variance_label}`}</span>
              <span className="psw-sub">
                {`${v.store_name ?? 'Store not recorded'}, raised ${formatClinicalDateTime(v.raised_at)} by ${v.raised_by_name ?? v.raised_by}`}
              </span>
              {(v.expected_units !== null || v.found_units !== null) && (
                <span className="psw-sub">
                  {`Expected ${v.expected_units ?? 'not known'}, found ${v.found_units ?? 'not known'}`}
                </span>
              )}
              {v.detail && <span className="psw-sub">{v.detail}</span>}
              {v.helix_order_id && <span className="psw-sub">{`Medicine order ${v.helix_order_id}`}</span>}
              {v.incident_ref && <span className="psw-sub">{`Incident ${v.incident_ref}`}</span>}
              {v.status === 'reconciled' ? (
                <span className="psw-sub psw-chip--ok">
                  {`Reconciled by ${v.reconciled_by_name ?? v.reconciled_by}: ${v.reconciliation_note ?? ''}`}
                </span>
              ) : (
                <div className="psw-buttons">
                  <button type="button" className="psw-btn" disabled={busy}
                    onClick={() => { setError(''); setClosing(closing === v.id ? null : v.id) }}>
                    Reconcile
                  </button>
                  {!v.incident_ref && (
                    <button type="button" className="psw-btn psw-btn--quiet" disabled={busy}
                      onClick={() => onReport(v.id)}>
                      Report as an incident
                    </button>
                  )}
                </div>
              )}
              {closing === v.id && (
                <form className="psw-form" onSubmit={onClose} noValidate>
                  <div className="psw-field psw-field--wide">
                    <label htmlFor={`psw-ndps-note-${v.id}`}>What was found and done?</label>
                    <textarea id={`psw-ndps-note-${v.id}`} rows={2} value={note}
                      onChange={e => setNote(e.target.value)} />
                  </div>
                  <WitnessFields idPrefix={`psw-ndps-close-${v.id}`}
                    witnesses={options?.witnesses ?? []} witnessId={witnessId}
                    setWitnessId={setWitnessId} pin={pin} setPin={setPin} />
                  <div className="psw-buttons">
                    <button type="submit" className="psw-btn psw-btn--primary" disabled={busy}>
                      Record the reconciliation
                    </button>
                  </div>
                </form>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
