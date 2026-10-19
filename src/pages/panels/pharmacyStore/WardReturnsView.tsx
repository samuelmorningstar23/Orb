/**
 * Pharmacy store, Ward returns (grn-15): medicines dispensed for a patient
 * that came back unused.
 *
 * Discovered by the panel through ./viewRegistry.ts; shown to pharmacy_store
 * .write holders (the pharmacist). Every answer names a patient, so the
 * read-only administrator tier reaches none of it, on the server as here.
 *
 * TWO JOBS.
 *   1. Inspect what is waiting in returns quarantine: write it off, with a
 *      reason (a narcotic or psychotropic return also needs a second
 *      pharmacist's signing PIN), or put it back into the batch it came from.
 *      Either decision is someone else's than the person who took the return
 *      in (a strict default listed for Samuel; the server refuses it too).
 *      Restock is ruling GRN-RS3, held for Samuel: the server refuses it on
 *      every site until he rules, and this screen says so instead of offering
 *      it. Where a site has it, only a sealed, in-date, non cold-chain, non-NDPS
 *      return goes back, inspected by someone other than the person who took it
 *      in; the server's reasons are shown when it cannot.
 *   2. Take a return in at the counter: a medicine the ward signed for, never
 *      more units than its dispense took minus what already came back.
 *
 * Units are whole dispense units. The credit shown is the candidate the
 * pharmacy charges (grn-16) will read: units at the batch MRP; nothing here
 * bills. Expiry dates are calendar keys (formatClinicalDateKey); recorded
 * times are UTC from the server and go through formatClinicalDateTime.
 */
import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react'

import { getUser } from '../../../api/client'
import { toPharmacyStoreError } from '../../../api/pharmacyStore'
import {
  WARD_RETURN_STATUS_LABELS,
  getReturnableOrders,
  getWardReturnOptions,
  inspectWardReturn,
  listWardReturns,
  newWardReturnKey,
  recordWardReturn,
  returnUnitsWords,
  type ReturnableBatch,
  type ReturnableOrder,
  type WardReturn,
  type WardReturnFilter,
  type WardReturnOptions,
} from '../../../api/pharmacyStore/returns'
import { formatClinicalDateKey, formatClinicalDateTime } from '../../../lib/datetime'
import { formatInr } from '../../../lib/money'
import type { PharmacyStoreViewProps } from './viewRegistry'
import WitnessFields from './WitnessFields'
import './stockAndPurchasing.css'

export const viewId = 'ward-returns'
export const viewLabel = 'Ward returns'
export const viewOrder = 88
export const viewPermission = 'pharmacy_store.write'

type Section = 'inspect' | 'counter'
type Fail = (err: unknown) => string

function useFail(onModuleOff: () => void): Fail {
  return useCallback((err: unknown) => {
    const failure = toPharmacyStoreError(err)
    if (failure.isModuleDisabled) onModuleOff()
    const text = failure.message || 'Something went wrong. Nothing was saved.'
    return text.charAt(0).toUpperCase() + text.slice(1)
  }, [onModuleOff])
}

const FILTERS: { value: WardReturnFilter; label: string }[] = [
  { value: 'pending_inspection', label: 'Waiting for inspection' },
  { value: 'written_off', label: 'Written off' },
  { value: 'restocked', label: 'Back on the shelf' },
  { value: '', label: 'All' },
]

function drugWords(r: { drug_name: string | null; strength: string | null }): string {
  const name = r.drug_name || 'Medicine'
  const strength = (r.strength || '').trim()
  return strength && !name.toLowerCase().includes(strength.toLowerCase()) ? `${name} ${strength}` : name
}

function patientWords(r: { patient_name: string | null; patient_bed?: string | null; bed?: string | null }): string {
  const bed = r.patient_bed ?? r.bed
  return [r.patient_name, bed && `Bed ${bed}`].filter(Boolean).join(' · ') || 'Patient not named'
}

function sealWords(seal: boolean | null): string {
  if (seal === true) return 'Sealed'
  if (seal === false) return 'Opened'
  return 'Not checked'
}

export default function WardReturnsView({ onModuleOff }: PharmacyStoreViewProps) {
  const [section, setSection] = useState<Section>('inspect')
  const [options, setOptions] = useState<WardReturnOptions | null>(null)
  const [optionsError, setOptionsError] = useState('')
  const [notice, setNotice] = useState('')
  const fail = useFail(onModuleOff)

  useEffect(() => {
    let live = true
    getWardReturnOptions().then(
      res => { if (live) { setOptionsError(''); setOptions(res) } },
      err => { if (live) setOptionsError(fail(err)) },
    )
    return () => { live = false }
  }, [fail])

  const sections: { id: Section; label: string }[] = [
    { id: 'inspect', label: 'Returns' },
    { id: 'counter', label: 'Take in a return' },
  ]

  return (
    <div className="psw-view">
      <nav className="psw-sections" aria-label="Ward return sections">
        {sections.map(s => (
          <button key={s.id} type="button"
            className={`psw-section${s.id === section ? ' psw-section--on' : ''}`}
            aria-current={s.id === section ? 'page' : undefined}
            onClick={() => { setNotice(''); setSection(s.id) }}>
            {s.label}
          </button>
        ))}
      </nav>
      {notice && <p className="psw-ok" role="status">{notice}</p>}
      {optionsError && <p className="psw-error" role="alert">{optionsError}</p>}
      {options && !options.restock_allowed && (
        <p className="psw-notice">
          This site does not put returned medicines back on the shelf. A return is written off,
          or sent back to the vendor from Vendor returns.
        </p>
      )}
      {options && options.stores.length === 0 && (
        <p className="psw-notice psw-notice--warn">
          There is no returns quarantine store yet, so no return can be taken in. Add one in
          Masters, Stores.
        </p>
      )}
      {section === 'inspect' && <ReturnsSection fail={fail} options={options} />}
      {section === 'counter' && (
        <CounterSection fail={fail} options={options}
          onTaken={message => { setNotice(message); setSection('inspect') }} />
      )}
    </div>
  )
}

// ═════════════════════════════════════════════════════════════════════════════
//  Returns and their inspection
// ═════════════════════════════════════════════════════════════════════════════

function ReturnsSection({ fail, options }: { fail: Fail; options: WardReturnOptions | null }) {
  const [status, setStatus] = useState<WardReturnFilter>('pending_inspection')
  const [rows, setRows] = useState<WardReturn[] | null>(null)
  const [error, setError] = useState('')
  const [openId, setOpenId] = useState<number | null>(null)

  const load = useCallback(() => {
    listWardReturns({ status }).then(
      res => { setError(''); setRows(res.ward_returns) },
      err => setError(fail(err)),
    )
  }, [fail, status])

  useEffect(() => { load() }, [load])

  const open = rows?.find(r => r.id === openId) ?? null

  return (
    <section className="psw-card" aria-labelledby="psw-wr-title">
      <div className="psw-card__head">
        <h2 id="psw-wr-title" className="psw-card__title">Returns from the wards</h2>
        <div className="psw-toolbar">
          <label htmlFor="psw-wr-status" className="psw-visually-hidden">Show returns</label>
          <select id="psw-wr-status" value={status}
            onChange={e => { setOpenId(null); setStatus(e.target.value as WardReturnFilter) }}>
            {FILTERS.map(f => <option key={f.value} value={f.value}>{f.label}</option>)}
          </select>
        </div>
      </div>
      {error && <p className="psw-error" role="alert">{error}</p>}
      {rows === null && !error && <p className="psw-note" role="status">Loading returns</p>}
      {rows && rows.length === 0 && <p className="psw-note">No returns here.</p>}
      {rows && rows.length > 0 && (
        <div className="psw-table-scroll">
          <table className="psw-table">
            <thead>
              <tr>
                <th scope="col">Medicine</th>
                <th scope="col">Patient</th>
                <th scope="col">Status</th>
                <th scope="col" className="psw-num">Units</th>
                <th scope="col"><span className="psw-visually-hidden">Actions</span></th>
              </tr>
            </thead>
            <tbody>
              {rows.map(r => (
                <tr key={r.id} className={r.id === openId ? 'psw-row--open' : undefined}>
                  <td>
                    <span className="psw-strong">{drugWords(r)}</span>
                    <span className="psw-sub">
                      {r.return_number ?? 'Not numbered yet'}
                      {r.controlled ? ' · Narcotic or psychotropic' : ''}
                    </span>
                  </td>
                  <td>{patientWords(r)}</td>
                  <td>
                    <span className={`psw-chip${r.status === 'pending_inspection' ? ' psw-chip--warn' : ''}`}>
                      {WARD_RETURN_STATUS_LABELS[r.status] ?? r.status_label}
                    </span>
                  </td>
                  <td className="psw-num">{r.quantity_units}</td>
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
      {open && (
        <ReturnDetail key={open.id} ret={open} fail={fail} options={options}
          onChanged={() => { load() }} />
      )}
    </section>
  )
}

type Mode = 'none' | 'write_off' | 'restock'

function ReturnDetail({ ret, fail, options, onChanged }: {
  ret: WardReturn
  fail: Fail
  options: WardReturnOptions | null
  onChanged: () => void
}) {
  const [doc, setDoc] = useState<WardReturn>(ret)
  const [mode, setMode] = useState<Mode>('none')
  const [reason, setReason] = useState('')
  const [sealConfirmed, setSealConfirmed] = useState(false)
  const [witnessId, setWitnessId] = useState('')
  const [pin, setPin] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const me = getUser()?.id ?? null
  const pending = doc.status === 'pending_inspection'
  const restockOn = !!options?.restock_allowed
  const tookItIn = me !== null && doc.received_by === me
  // Strict unless the server says otherwise: the receiver never restocks, and
  // writes off only once RETURN_WRITE_OFF_NEEDS_SECOND_PERSON is relaxed.
  const receiverMayWriteOff = options?.write_off_needs_second_person === false
  const mayWriteOff = !tookItIn || receiverMayWriteOff
  const mayRestock = !tookItIn && restockOn && !!doc.restock?.allowed
  const witnesses = (options?.witnesses ?? []).filter(w => w.id !== me)

  function start(next: Mode) {
    setError('')
    setMessage('')
    setReason('')
    setPin('')
    setSealConfirmed(false)
    setMode(next)
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault()
    if (mode === 'none') return
    if (!reason.trim()) { setError('Write the reason for this decision.'); return }
    if (mode === 'restock' && !sealConfirmed) {
      setError('Confirm the seal is intact, or write the return off.')
      return
    }
    if (mode === 'write_off' && doc.controlled && (!witnessId || !pin.trim())) {
      setError('A second pharmacist witnesses this write-off with their signing PIN.')
      return
    }
    setBusy(true)
    setError('')
    inspectWardReturn(doc.id, {
      decision: mode, reason: reason.trim(),
      seal_intact: mode === 'restock' ? true : null,
      ...(mode === 'write_off' && doc.controlled ? { witness_id: witnessId, witness_pin: pin.trim() } : {}),
    }).then(
      res => {
        setDoc(res.ward_return)
        setMessage(mode === 'restock' ? 'Back on the shelf, in the batch it came from.' : 'Written off.')
        setMode('none')
        onChanged()
      },
      err => setError(fail(err)),
    ).finally(() => { setBusy(false); setPin('') })
  }

  const credit = doc.credit
  return (
    <div className="psw-detail" aria-labelledby="psw-wr-detail-title">
      <h3 id="psw-wr-detail-title" className="psw-card__title">
        {doc.return_number ? `Return ${doc.return_number}` : 'Return not numbered yet'}
      </h3>
      <dl className="psw-facts">
        <div><dt>Medicine</dt><dd>{drugWords(doc)}</dd></div>
        <div><dt>Batch</dt><dd>{doc.batch_no ?? 'Not recorded'}</dd></div>
        <div><dt>Expiry</dt><dd>{formatClinicalDateKey(doc.expiry_date) || 'Not recorded'}</dd></div>
        <div><dt>Units</dt><dd>{returnUnitsWords(doc.quantity_units, doc.dispense_unit)}</dd></div>
        <div><dt>Patient</dt><dd>{patientWords(doc)}</dd></div>
        <div><dt>Seal when taken in</dt><dd>{sealWords(doc.seal_intact)}</dd></div>
        <div>
          <dt>Taken in by</dt>
          <dd>{doc.received_by_name ?? 'Not recorded'}{doc.occurred_at ? `, ${formatClinicalDateTime(doc.occurred_at)}` : ''}</dd>
        </div>
        <div><dt>Held in</dt><dd>{doc.store_name ?? doc.store_code ?? 'Not recorded'}</dd></div>
        <div>
          <dt>Credit at batch MRP</dt>
          <dd>{credit.amount_paise === null ? 'MRP not recorded for this batch' : formatInr(credit.amount_paise)}</dd>
        </div>
        {doc.note && <div><dt>Note</dt><dd>{doc.note}</dd></div>}
        {doc.inspected_by && (
          <div>
            <dt>Inspected by</dt>
            <dd>{doc.inspected_by_name ?? 'Not recorded'}{doc.inspected_at ? `, ${formatClinicalDateTime(doc.inspected_at)}` : ''}</dd>
          </div>
        )}
        {doc.inspection_note && <div><dt>Decision</dt><dd>{doc.inspection_note}</dd></div>}
      </dl>
      {doc.controlled && pending && (
        <p className="psw-notice">
          Narcotic or psychotropic. Writing it off needs a second pharmacist witnessing with their
          signing PIN.
        </p>
      )}
      {pending && restockOn && doc.restock && !doc.restock.allowed && (
        <div className="psw-notice psw-notice--warn">
          <p>It cannot go back on the shelf:</p>
          <ul>{doc.restock.reasons.map(r => <li key={r}>{r}</li>)}</ul>
        </div>
      )}
      {pending && tookItIn && !receiverMayWriteOff && (
        <p className="psw-hint">
          {restockOn && doc.restock?.allowed
            ? 'You took this return in, so another pharmacist inspects it: writes it off or puts it back on the shelf.'
            : 'You took this return in, so another pharmacist inspects it and writes it off.'}
        </p>
      )}
      {pending && tookItIn && receiverMayWriteOff && restockOn && doc.restock?.allowed && (
        <p className="psw-hint">
          You took this return in, so only another pharmacist can put it back on the shelf.
        </p>
      )}
      {error && <p className="psw-error" role="alert">{error}</p>}
      {message && <p className="psw-ok" role="status">{message}</p>}

      {/* The person who took a return in never restocks it, and writes it off
          only when the site has relaxed the second-person rule: otherwise a
          second person decides (the server refuses it too, same_person). */}
      {pending && mode === 'none' && mayWriteOff && (
        <div className="psw-buttons">
          <button type="button" className="psw-btn" disabled={busy} onClick={() => start('write_off')}>
            Write off
          </button>
          {mayRestock && (
            <button type="button" className="psw-btn psw-btn--primary" disabled={busy}
              onClick={() => start('restock')}>
              Put back on the shelf
            </button>
          )}
        </div>
      )}

      {mode !== 'none' && (
        <form className="psw-form" onSubmit={onSubmit} noValidate>
          <div className="psw-field psw-field--wide">
            <label htmlFor="psw-wr-reason">
              {mode === 'restock' ? 'What did the inspection find?' : 'Why is it written off?'}
            </label>
            <textarea id="psw-wr-reason" rows={2} value={reason}
              onChange={e => setReason(e.target.value)} disabled={busy} />
          </div>
          {mode === 'restock' && (
            <label className="psw-check">
              <input type="checkbox" checked={sealConfirmed}
                onChange={e => setSealConfirmed(e.target.checked)} disabled={busy} />
              The seal is intact
            </label>
          )}
          {mode === 'write_off' && doc.controlled && (
            <WitnessFields idPrefix="psw-wr-witness" witnesses={witnesses} witnessId={witnessId}
              setWitnessId={setWitnessId} pin={pin} setPin={setPin} />
          )}
          <div className="psw-buttons">
            <button type="submit" className="psw-btn psw-btn--primary" disabled={busy}>
              {busy ? 'Saving…' : mode === 'restock' ? 'Put back on the shelf' : 'Write off'}
            </button>
            <button type="button" className="psw-btn psw-btn--quiet" disabled={busy}
              onClick={() => setMode('none')}>
              Not now
            </button>
          </div>
        </form>
      )}
    </div>
  )
}

// ═════════════════════════════════════════════════════════════════════════════
//  Taking a return in at the counter
// ═════════════════════════════════════════════════════════════════════════════

interface Picked {
  order: ReturnableOrder
  batch: ReturnableBatch
}

function CounterSection({ fail, options, onTaken }: {
  fail: Fail
  options: WardReturnOptions | null
  onTaken: (message: string) => void
}) {
  const [orders, setOrders] = useState<ReturnableOrder[] | null>(null)
  const [error, setError] = useState('')
  const [search, setSearch] = useState('')
  const [picked, setPicked] = useState<Picked | null>(null)

  useEffect(() => {
    let live = true
    getReturnableOrders().then(
      res => { if (live) { setError(''); setOrders(res.orders) } },
      err => { if (live) setError(fail(err)) },
    )
    return () => { live = false }
  }, [fail])

  const shown = useMemo(() => {
    const q = search.trim().toLowerCase()
    const all = (orders ?? []).flatMap(order => order.batches
      .filter(b => b.returnable_units > 0)
      .map(batch => ({ order, batch })))
    if (!q) return all
    return all.filter(({ order, batch }) => [order.patient_name, order.bed, order.mrn, batch.drug_name,
      batch.batch_no].some(v => (v ?? '').toLowerCase().includes(q)))
  }, [orders, search])

  return (
    <section className="psw-card" aria-labelledby="psw-wr-counter-title">
      <div className="psw-card__head">
        <h2 id="psw-wr-counter-title" className="psw-card__title">Take in a return</h2>
        <div className="psw-toolbar">
          <label htmlFor="psw-wr-search" className="psw-visually-hidden">Find a patient or medicine</label>
          <input id="psw-wr-search" value={search} placeholder="Patient, bed or medicine"
            onChange={e => setSearch(e.target.value)} />
        </div>
      </div>
      {error && <p className="psw-error" role="alert">{error}</p>}
      {orders === null && !error && <p className="psw-note" role="status">Loading what can come back</p>}
      {orders !== null && shown.length === 0 && (
        <p className="psw-note">
          No medicine signed for on a ward in the last 30 days has units left to return
          {search.trim() ? ' that matches this search' : ''}.
        </p>
      )}
      {shown.length > 0 && (
        <div className="psw-table-scroll">
          <table className="psw-table">
            <thead>
              <tr>
                <th scope="col">Medicine</th>
                <th scope="col">Patient</th>
                <th scope="col" className="psw-num">Can come back</th>
                <th scope="col"><span className="psw-visually-hidden">Actions</span></th>
              </tr>
            </thead>
            <tbody>
              {shown.map(({ order, batch }) => {
                const on = picked?.order.id === order.id && picked.batch.lot_id === batch.lot_id
                return (
                  <tr key={`${order.id}:${batch.lot_id}`} className={on ? 'psw-row--open' : undefined}>
                    <td>
                      <span className="psw-strong">{drugWords(batch)}</span>
                      <span className="psw-sub">
                        Batch {batch.batch_no ?? 'not recorded'}
                        {batch.controlled ? ' · Narcotic or psychotropic' : ''}
                      </span>
                    </td>
                    <td>{patientWords(order)}</td>
                    <td className="psw-num">{batch.returnable_units}</td>
                    <td className="psw-actions-cell">
                      <button type="button" className="psw-btn psw-btn--quiet"
                        aria-expanded={on}
                        onClick={() => setPicked(on ? null : { order, batch })}>
                        {on ? 'Close' : 'Take in'}
                      </button>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
      {picked && (
        <TakeInForm key={`${picked.order.id}:${picked.batch.lot_id}`} picked={picked} options={options}
          fail={fail} onTaken={onTaken} />
      )}
    </section>
  )
}

function TakeInForm({ picked, options, fail, onTaken }: {
  picked: Picked
  options: WardReturnOptions | null
  fail: Fail
  onTaken: (message: string) => void
}) {
  const { order, batch } = picked
  const stores = options?.stores ?? []
  const [units, setUnits] = useState(String(batch.returnable_units))
  const [seal, setSeal] = useState<'' | 'yes' | 'no'>('')
  const [store, setStore] = useState(stores.length === 1 ? stores[0].code : '')
  const [note, setNote] = useState('')
  const [witnessId, setWitnessId] = useState('')
  const [pin, setPin] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [key] = useState(() => newWardReturnKey())
  const me = getUser()?.id ?? null
  const witnesses = (options?.witnesses ?? []).filter(w => w.id !== me)

  function onSubmit(e: FormEvent) {
    e.preventDefault()
    const raw = units.trim()
    const count = /^\d+$/.test(raw) ? Number(raw) : NaN
    if (!Number.isInteger(count) || count < 1 || count > batch.returnable_units) {
      setError(`Enter a whole number from 1 to ${batch.returnable_units}.`)
      return
    }
    if (stores.length > 1 && !store) { setError('Choose the returns store.'); return }
    if (batch.controlled && (!witnessId || !pin.trim())) {
      setError('A second pharmacist witnesses a controlled return with their signing PIN.')
      return
    }
    setBusy(true)
    setError('')
    recordWardReturn({
      helix_order_id: order.id, quantity_units: count, lot_id: batch.lot_id,
      seal_intact: seal === '' ? null : seal === 'yes', note: note.trim() || null,
      store_code: store || null,
      ...(batch.controlled ? { witness_id: witnessId, witness_pin: pin.trim() } : {}),
    }, key).then(
      res => onTaken(`Taken in: ${returnUnitsWords(count, batch.dispense_unit)} of ${drugWords(batch)}` +
        `${res.ward_return.return_number ? `, return ${res.ward_return.return_number}` : ''}. ` +
        'It waits for inspection.'),
      err => setError(fail(err)),
    ).finally(() => { setBusy(false); setPin('') })
  }

  return (
    <form className="psw-form psw-detail" onSubmit={onSubmit} noValidate>
      <h3 className="psw-card__title">{drugWords(batch)} for {patientWords(order)}</h3>
      <div className="psw-fields">
        <div className="psw-field">
          <label htmlFor="psw-wr-units">Units coming back</label>
          <input id="psw-wr-units" inputMode="numeric" value={units}
            onChange={e => setUnits(e.target.value)} disabled={busy} />
        </div>
        <div className="psw-field">
          <label htmlFor="psw-wr-seal">Seal</label>
          <select id="psw-wr-seal" value={seal}
            onChange={e => setSeal(e.target.value as '' | 'yes' | 'no')} disabled={busy}>
            <option value="">Not checked</option>
            <option value="yes">Sealed</option>
            <option value="no">Opened</option>
          </select>
        </div>
        {stores.length > 1 && (
          <div className="psw-field">
            <label htmlFor="psw-wr-store">Returns store</label>
            <select id="psw-wr-store" value={store} onChange={e => setStore(e.target.value)} disabled={busy}>
              <option value="">Choose the returns store</option>
              {stores.map(s => <option key={s.code} value={s.code}>{s.name}</option>)}
            </select>
          </div>
        )}
        <div className="psw-field psw-field--wide">
          <label htmlFor="psw-wr-note">Note (optional)</label>
          <input id="psw-wr-note" value={note} onChange={e => setNote(e.target.value)} disabled={busy}
            placeholder="Who brought it, and why it came back" />
        </div>
      </div>
      {batch.controlled && (
        <WitnessFields idPrefix="psw-wr-take-witness" witnesses={witnesses} witnessId={witnessId}
          setWitnessId={setWitnessId} pin={pin} setPin={setPin} />
      )}
      {error && <p className="psw-error" role="alert">{error}</p>}
      <div className="psw-buttons">
        <button type="submit" className="psw-btn psw-btn--primary" disabled={busy}>
          {busy ? 'Saving…' : 'Take in'}
        </button>
      </div>
    </form>
  )
}
