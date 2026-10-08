/**
 * Pharmacy store, Transfers (grn-11): drug stock moved between the pharmacy's
 * own stores, the dispensing pharmacy, sub-stores and the NDPS vault.
 *
 * Discovered by the panel through ./viewRegistry.ts; shown to anyone holding
 * pharmacy_store.read. Raising, sending, receiving and cancelling need the
 * write tier. The server refuses anyone else whatever this screen shows, and
 * every refusal is shown in its own words.
 *
 * THE SERVER DECIDES. The plan (which lots, earliest expiry first) is the
 * server's; the form only asks which drugs and how many units. Units on their
 * way are counted by neither store. The person who sent a transfer is never
 * offered Receive. A short receipt needs a reason for every line that came up
 * short. A controlled transfer (the NDPS vault at either end, or a narcotic
 * or psychotropic drug) needs the NDPS permission, and its sending and its
 * receipt each need a second pharmacist as witness with their own signing PIN.
 *
 * No patient data is read or shown. Expiry dates are calendar keys
 * (formatClinicalDateKey); recorded times are bare UTC from the server and go
 * through formatClinicalDateTime.
 */
import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react'

import { getUser } from '../../../api/client'
import { toPharmacyStoreError } from '../../../api/pharmacyStore'
import {
  TRANSFER_STATUS_LABELS,
  TRANSFER_STORE_KIND_LABELS,
  cancelTransfer,
  createTransfer,
  dispatchTransfer,
  getStoreStock,
  getTransfer,
  getTransferOptions,
  listTransfers,
  newTransferRequestKey,
  receiveTransfer,
  replanTransfer,
  type ReceiptLine,
  type StoreStock,
  type Transfer,
  type TransferFilter,
  type TransferOptions,
  type TransferResult,
  type TransferStatus,
  type TransferWitness,
  type WitnessInput,
} from '../../../api/pharmacyStore/transfers'
import { formatClinicalDateKey, formatClinicalDateTime } from '../../../lib/datetime'
import { PHARMACY_STORE_NDPS, PHARMACY_STORE_WRITE, hasPermission } from '../../../lib/roles'
import type { PharmacyStoreViewProps } from './viewRegistry'
import './stockAndPurchasing.css'

export const viewId = 'transfers'
export const viewLabel = 'Transfers'
export const viewOrder = 50
export const viewPermission = 'pharmacy_store.read'

type Section = 'transfers' | 'new'

type Fail = (err: unknown) => string

function useFail(onModuleOff: () => void): Fail {
  return useCallback((err: unknown) => {
    const failure = toPharmacyStoreError(err)
    if (failure.isModuleDisabled) onModuleOff()
    const text = failure.message || 'Something went wrong. Nothing was saved.'
    // The server words a refusal as a clause; the screen shows it as a sentence.
    return text.charAt(0).toUpperCase() + text.slice(1)
  }, [onModuleOff])
}

const STATUS_FILTERS: { value: TransferFilter; label: string }[] = [
  { value: '', label: 'All' },
  { value: 'draft', label: 'Drafts' },
  { value: 'open', label: 'On their way' },
  { value: 'received', label: 'Received' },
  { value: 'cancelled', label: 'Cancelled' },
]

const IN_TRANSIT: readonly TransferStatus[] = ['dispatched', 'partially_received']

function statusTone(status: TransferStatus): string {
  if (status === 'cancelled') return 'psw-chip--danger'
  if (status === 'received') return 'psw-chip--ok'
  return 'psw-chip--warn'
}

function storeKindLabel(kind: string | null | undefined): string {
  return kind && kind in TRANSFER_STORE_KIND_LABELS
    ? TRANSFER_STORE_KIND_LABELS[kind as keyof typeof TRANSFER_STORE_KIND_LABELS]
    : 'Store'
}

function whoAndWhen(name: string | null, id: string | null, at: string | null): string {
  const who = name ?? id ?? 'Not recorded'
  const when = formatClinicalDateTime(at)
  return when ? `${who}, ${when}` : who
}

function isWholeNumber(text: string): boolean {
  return /^\d+$/.test(text.trim())
}

export default function TransfersView({ onModuleOff }: PharmacyStoreViewProps) {
  const [section, setSection] = useState<Section>('transfers')
  const [openId, setOpenId] = useState<number | null>(null)
  const [notice, setNotice] = useState('')
  const [options, setOptions] = useState<TransferOptions | null>(null)
  const [optionsError, setOptionsError] = useState('')
  const fail = useFail(onModuleOff)
  const canWrite = hasPermission(PHARMACY_STORE_WRITE)
  const mayNdps = hasPermission(PHARMACY_STORE_NDPS)

  useEffect(() => {
    let live = true
    getTransferOptions().then(
      res => { if (live) { setOptionsError(''); setOptions(res) } },
      err => { if (live) setOptionsError(fail(err)) },
    )
    return () => { live = false }
  }, [fail])

  function onCreated(transfer: Transfer) {
    setOpenId(transfer.id)
    setNotice('Draft saved. Pick the lots on the plan, then send it.')
    setSection('transfers')
  }

  const sections: { id: Section; label: string }[] = [
    { id: 'transfers', label: 'Transfers' },
    ...(canWrite ? [{ id: 'new' as Section, label: 'New transfer' }] : []),
  ]

  return (
    <div className="psw-view">
      <nav className="psw-sections" aria-label="Transfer sections">
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
      {section === 'transfers' && (
        <TransfersSection
          fail={fail}
          canWrite={canWrite}
          mayNdps={mayNdps}
          witnesses={options?.witnesses ?? []}
          openId={openId}
          setOpenId={setOpenId}
        />
      )}
      {section === 'new' && canWrite && (
        <NewTransferSection
          fail={fail}
          options={options}
          optionsError={optionsError}
          mayNdps={mayNdps}
          onCreated={onCreated}
        />
      )}
    </div>
  )
}

// ═════════════════════════════════════════════════════════════════════════════
//  Transfers list and one transfer
// ═════════════════════════════════════════════════════════════════════════════

function TransfersSection({ fail, canWrite, mayNdps, witnesses, openId, setOpenId }: {
  fail: Fail
  canWrite: boolean
  mayNdps: boolean
  witnesses: TransferWitness[]
  openId: number | null
  setOpenId: (id: number | null) => void
}) {
  const [status, setStatus] = useState<TransferFilter>('')
  const [rows, setRows] = useState<Transfer[] | null>(null)
  const [error, setError] = useState('')

  const load = useCallback(() => {
    listTransfers({ status }).then(
      res => { setError(''); setRows(res.transfers) },
      err => setError(fail(err)),
    )
  }, [fail, status])

  useEffect(() => { load() }, [load])

  return (
    <section className="psw-card" aria-labelledby="psw-transfers-title">
      <div className="psw-card__head">
        <h2 id="psw-transfers-title" className="psw-card__title">Transfers between stores</h2>
        <div className="psw-toolbar">
          <label htmlFor="psw-transfer-status" className="psw-visually-hidden">Show transfers</label>
          <select
            id="psw-transfer-status"
            value={status}
            onChange={e => setStatus(e.target.value as TransferFilter)}
          >
            {STATUS_FILTERS.map(f => <option key={f.value} value={f.value}>{f.label}</option>)}
          </select>
        </div>
      </div>
      <p className="psw-hint">
        Units on their way between stores are counted by neither store and cannot be
        dispensed until they are received.
      </p>
      {error && <p className="psw-error" role="alert">{error}</p>}
      {rows === null && !error && <p className="psw-note" role="status">Loading transfers</p>}
      {rows && rows.length === 0 && <p className="psw-note">No transfers here yet.</p>}
      {rows && rows.length > 0 && (
        <div className="psw-table-scroll">
          <table className="psw-table">
            <thead>
              <tr>
                <th scope="col">Transfer</th>
                <th scope="col">From</th>
                <th scope="col">To</th>
                <th scope="col">Status</th>
                <th scope="col" className="psw-num">Units</th>
                <th scope="col"><span className="psw-visually-hidden">Actions</span></th>
              </tr>
            </thead>
            <tbody>
              {rows.map(t => (
                <tr key={t.id} className={t.id === openId ? 'psw-row--open' : undefined}>
                  <td>
                    <span className="psw-strong">{t.transfer_number ?? 'Not numbered yet'}</span>
                    <span className="psw-sub">{formatClinicalDateTime(t.created_at)}</span>
                  </td>
                  <td>{t.from_store_name ?? t.from_store_code}</td>
                  <td>{t.to_store_name ?? t.to_store_code}</td>
                  <td>
                    <span className={`psw-chip ${statusTone(t.status)}`}>
                      {TRANSFER_STATUS_LABELS[t.status] ?? t.status_label}
                    </span>
                    {t.controlled && <span className="psw-sub">Controlled stock</span>}
                  </td>
                  <td className="psw-num">
                    {t.total_units}
                    {t.in_transit_units > 0 && (
                      <span className="psw-sub">{`${t.in_transit_units} on the way`}</span>
                    )}
                  </td>
                  <td className="psw-actions-cell">
                    <button
                      type="button"
                      className="psw-btn psw-btn--quiet"
                      aria-expanded={t.id === openId}
                      onClick={() => setOpenId(t.id === openId ? null : t.id)}
                    >
                      {t.id === openId ? 'Close' : 'Open'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {openId !== null && (
        <TransferDetail
          key={openId}
          transferId={openId}
          fail={fail}
          canWrite={canWrite}
          mayNdps={mayNdps}
          witnesses={witnesses}
          onChanged={load}
        />
      )}
    </section>
  )
}

type Mode = 'none' | 'send' | 'receive' | 'cancel'

interface ReceiptDraft {
  arrived: boolean
  units: string
  reason: string
}

function TransferDetail({ transferId, fail, canWrite, mayNdps, witnesses, onChanged }: {
  transferId: number
  fail: Fail
  canWrite: boolean
  mayNdps: boolean
  witnesses: TransferWitness[]
  onChanged: () => void
}) {
  const [transfer, setTransfer] = useState<Transfer | null>(null)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const [mode, setMode] = useState<Mode>('none')
  const [reason, setReason] = useState('')
  const [witnessId, setWitnessId] = useState('')
  const [witnessPin, setWitnessPin] = useState('')
  const [receipt, setReceipt] = useState<Record<number, ReceiptDraft>>({})
  const me = getUser()?.id ?? null

  useEffect(() => {
    let live = true
    getTransfer(transferId).then(
      res => { if (live) { setError(''); setTransfer(res.transfer) } },
      err => { if (live) setError(fail(err)) },
    )
    return () => { live = false }
  }, [transferId, fail])

  function startMode(next: Mode) {
    setError('')
    setMessage('')
    setWitnessPin('')
    setMode(next)
    if (next === 'receive' && transfer) {
      const drafts: Record<number, ReceiptDraft> = {}
      for (const line of transfer.lines ?? []) {
        if (line.received_units === null) {
          drafts[line.id] = { arrived: true, units: String(line.quantity_units), reason: '' }
        }
      }
      setReceipt(drafts)
    }
  }

  function act(run: () => Promise<TransferResult>, done: string) {
    setBusy(true)
    setError('')
    setMessage('')
    run().then(
      res => {
        setTransfer(res.transfer)
        setMessage(res.replayed ? 'This was already done; nothing new was recorded.' : done)
        setMode('none')
        setReason('')
        onChanged()
      },
      err => setError(fail(err)),
    ).finally(() => {
      setBusy(false)
      setWitnessPin('')
    })
  }

  function witnessInput(): WitnessInput | null | false {
    if (!transfer?.witness_required) return null
    if (!witnessId) { setError('Choose the pharmacist who witnesses this.'); return false }
    if (!witnessPin.trim()) { setError('The witness enters their signing PIN.'); return false }
    return { witness_id: witnessId, witness_pin: witnessPin.trim() }
  }

  function onSend(e: FormEvent) {
    e.preventDefault()
    const witness = witnessInput()
    if (witness === false) return
    act(() => dispatchTransfer(transferId, witness), 'Sent. The units are on their way.')
  }

  function onReceive(e: FormEvent) {
    e.preventDefault()
    if (!transfer) return
    const lines: ReceiptLine[] = []
    for (const line of transfer.lines ?? []) {
      const draft = receipt[line.id]
      if (!draft || !draft.arrived) continue
      if (!isWholeNumber(draft.units)) {
        setError(`Line ${line.line_no}: units received is a whole number.`)
        return
      }
      const units = Number(draft.units.trim())
      if (units > line.quantity_units) {
        setError(`Line ${line.line_no}: ${units} cannot arrive when ${line.quantity_units} were sent.`)
        return
      }
      if (units < line.quantity_units && !draft.reason.trim()) {
        setError(`Line ${line.line_no}: say what happened to the ${line.quantity_units - units} units that did not arrive.`)
        return
      }
      lines.push({ line_id: line.id, received_units: units,
        discrepancy_reason: draft.reason.trim() || null })
    }
    if (lines.length === 0) {
      setError('Tick at least one line that arrived.')
      return
    }
    const witness = witnessInput()
    if (witness === false) return
    act(() => receiveTransfer(transferId, lines, witness), 'Receipt recorded.')
  }

  function onCancel(e: FormEvent) {
    e.preventDefault()
    if (!reason.trim()) {
      setError('Say why the transfer is being cancelled.')
      return
    }
    act(() => cancelTransfer(transferId, reason.trim()), 'Transfer cancelled.')
  }

  function patchReceipt(lineId: number, patch: Partial<ReceiptDraft>) {
    setReceipt(prev => ({ ...prev, [lineId]: { ...prev[lineId], ...patch } }))
  }

  if (!transfer) {
    return (
      <div className="psw-detail">
        {error ? <p className="psw-error" role="alert">{error}</p>
          : <p className="psw-note" role="status">Loading the transfer</p>}
      </div>
    )
  }

  const lines = transfer.lines ?? []
  const inTransit = IN_TRANSIT.includes(transfer.status)
  const sentByMe = me !== null && transfer.dispatched_by === me
  const mayAct = canWrite && (!transfer.controlled || mayNdps)
  const pending = lines.filter(l => l.received_units === null)

  return (
    <div className="psw-detail" aria-labelledby="psw-transfer-title">
      <h3 id="psw-transfer-title" className="psw-card__title">
        {transfer.transfer_number ? `Transfer ${transfer.transfer_number}` : 'Transfer not numbered yet'}
      </h3>
      {transfer.transfer_number && transfer.number_series_configured === false && (
        <p className="psw-notice psw-notice--warn">
          This number comes from a series the site has not set up yet. Ask the
          administrator to configure the pharmacy transfer series.
        </p>
      )}
      {transfer.controlled && (
        <p className="psw-notice">
          Controlled stock. Only a pharmacist with the NDPS permission handles this transfer
          {transfer.witness_required
            ? ', and a second pharmacist witnesses both the sending and the receipt with their signing PIN.'
            : '.'}
        </p>
      )}
      <dl className="psw-facts">
        <div><dt>Status</dt><dd>{TRANSFER_STATUS_LABELS[transfer.status] ?? transfer.status_label}</dd></div>
        <div>
          <dt>From</dt>
          <dd>{`${transfer.from_store_name ?? transfer.from_store_code} (${storeKindLabel(transfer.from_store_kind)})`}</dd>
        </div>
        <div>
          <dt>To</dt>
          <dd>{`${transfer.to_store_name ?? transfer.to_store_code} (${storeKindLabel(transfer.to_store_kind)})`}</dd>
        </div>
        <div><dt>Raised by</dt><dd>{whoAndWhen(transfer.created_by_name, transfer.created_by, transfer.created_at)}</dd></div>
        {transfer.dispatched_by && (
          <div><dt>Sent by</dt><dd>{whoAndWhen(transfer.dispatched_by_name, transfer.dispatched_by, transfer.dispatched_at)}</dd></div>
        )}
        {transfer.received_by && (
          <div><dt>Last received by</dt><dd>{whoAndWhen(transfer.received_by_name, transfer.received_by, transfer.received_at)}</dd></div>
        )}
        {transfer.cancel_reason && (
          <div><dt>Cancelled because</dt><dd>{transfer.cancel_reason}</dd></div>
        )}
        {transfer.remarks && <div><dt>Remarks</dt><dd>{transfer.remarks}</dd></div>}
      </dl>

      <div className="psw-table-scroll">
        <table className="psw-table">
          <thead>
            <tr>
              <th scope="col">Drug</th>
              <th scope="col">Batch</th>
              <th scope="col">Expiry</th>
              <th scope="col" className="psw-num">Sent</th>
              <th scope="col" className="psw-num">Received</th>
            </tr>
          </thead>
          <tbody>
            {lines.map(line => (
              <tr key={line.id}>
                <td>
                  <span className="psw-strong">{line.drug_name}</span>
                  <span className="psw-sub">
                    {[line.strength, line.form].filter(Boolean).join(', ')}
                    {line.ndps ? `${line.strength || line.form ? '. ' : ''}Narcotic or psychotropic` : ''}
                  </span>
                </td>
                <td>{line.batch_no ?? 'Not recorded'}</td>
                <td>{formatClinicalDateKey(line.expiry_date) || 'Not recorded'}</td>
                <td className="psw-num">{line.quantity_units}</td>
                <td className="psw-num">
                  {line.received_units === null
                    ? (transfer.status === 'draft' || transfer.status === 'cancelled' ? 'Not sent' : 'On its way')
                    : line.received_units}
                  {line.shortfall_units !== null && line.shortfall_units > 0 && (
                    <span className="psw-sub psw-chip--danger">
                      {`${line.shortfall_units} short: ${line.discrepancy_reason ?? 'no reason recorded'}`}
                    </span>
                  )}
                  {line.to_lot_status && line.to_lot_status !== 'available' && (
                    <span className="psw-sub psw-chip--danger">
                      {`Held off the shelf: ${line.to_lot_status_label ?? 'not available'}`}
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {error && <p className="psw-error" role="alert">{error}</p>}
      {message && <p className="psw-ok" role="status">{message}</p>}

      {mode === 'none' && mayAct && (
        <div className="psw-buttons">
          {transfer.status === 'draft' && (
            <>
              <button type="button" className="psw-btn psw-btn--primary" disabled={busy}
                onClick={() => {
                  if (transfer.witness_required) startMode('send')
                  else act(() => dispatchTransfer(transferId, null), 'Sent. The units are on their way.')
                }}>
                Send
              </button>
              <button type="button" className="psw-btn" disabled={busy}
                onClick={() => act(() => replanTransfer(transferId), 'Planned again against the stock there is now.')}>
                Plan again
              </button>
              <button type="button" className="psw-btn psw-btn--quiet" disabled={busy}
                onClick={() => startMode('cancel')}>
                Cancel transfer
              </button>
            </>
          )}
          {inTransit && !sentByMe && pending.length > 0 && (
            <button type="button" className="psw-btn psw-btn--primary" disabled={busy}
              onClick={() => startMode('receive')}>
              Receive
            </button>
          )}
        </div>
      )}
      {inTransit && sentByMe && (
        <p className="psw-hint">You sent this transfer, so somebody else receives it.</p>
      )}
      {transfer.controlled && canWrite && !mayNdps && transfer.status !== 'received'
        && transfer.status !== 'cancelled' && (
        <p className="psw-hint">A pharmacist with the NDPS permission handles this transfer.</p>
      )}

      {mode === 'send' && (
        <form className="psw-form" onSubmit={onSend} noValidate>
          <WitnessFields
            idPrefix="psw-send"
            witnesses={witnesses}
            witnessId={witnessId}
            setWitnessId={setWitnessId}
            pin={witnessPin}
            setPin={setWitnessPin}
          />
          <div className="psw-buttons">
            <button type="submit" className="psw-btn psw-btn--primary" disabled={busy}>Send</button>
            <button type="button" className="psw-btn psw-btn--quiet" disabled={busy}
              onClick={() => setMode('none')}>
              Not now
            </button>
          </div>
        </form>
      )}

      {mode === 'receive' && (
        <form className="psw-form" onSubmit={onReceive} noValidate>
          <p className="psw-hint">
            Count what arrived. Untick a line that has not come yet; it stays on its way.
            If fewer units arrived than were sent, say what happened to the rest. Stock
            recalled, found not of standard quality or quarantined since it was sent is
            received held, off the shelf.
          </p>
          <ul className="psw-lines" aria-label="Lines to receive">
            {pending.map(line => {
              const draft = receipt[line.id] ?? { arrived: false, units: '', reason: '' }
              const short = isWholeNumber(draft.units) && Number(draft.units) < line.quantity_units
              return (
                <li key={line.id} className="psw-line">
                  <label className="psw-check">
                    <input type="checkbox" checked={draft.arrived}
                      onChange={e => patchReceipt(line.id, { arrived: e.target.checked })} />
                    {`${line.drug_name}, batch ${line.batch_no ?? 'not recorded'}: ${line.quantity_units} sent`}
                  </label>
                  <div className="psw-field">
                    <label htmlFor={`psw-recv-units-${line.id}`}>Units received</label>
                    <input id={`psw-recv-units-${line.id}`} inputMode="numeric" value={draft.units}
                      disabled={!draft.arrived}
                      onChange={e => patchReceipt(line.id, { units: e.target.value })} />
                  </div>
                  {draft.arrived && short && (
                    <div className="psw-field">
                      <label htmlFor={`psw-recv-reason-${line.id}`}>What happened to the rest?</label>
                      <input id={`psw-recv-reason-${line.id}`} value={draft.reason}
                        onChange={e => patchReceipt(line.id, { reason: e.target.value })} />
                    </div>
                  )}
                </li>
              )
            })}
          </ul>
          {transfer.witness_required && (
            <WitnessFields
              idPrefix="psw-recv"
              witnesses={witnesses}
              witnessId={witnessId}
              setWitnessId={setWitnessId}
              pin={witnessPin}
              setPin={setWitnessPin}
            />
          )}
          <div className="psw-buttons">
            <button type="submit" className="psw-btn psw-btn--primary" disabled={busy}>
              Record receipt
            </button>
            <button type="button" className="psw-btn psw-btn--quiet" disabled={busy}
              onClick={() => setMode('none')}>
              Not now
            </button>
          </div>
        </form>
      )}

      {mode === 'cancel' && (
        <form className="psw-form" onSubmit={onCancel}>
          <div className="psw-field psw-field--wide">
            <label htmlFor="psw-transfer-cancel-reason">Why is it being cancelled?</label>
            <textarea id="psw-transfer-cancel-reason" rows={2} value={reason}
              onChange={e => setReason(e.target.value)} />
          </div>
          <div className="psw-buttons">
            <button type="submit" className="psw-btn" disabled={busy}>Cancel the transfer</button>
            <button type="button" className="psw-btn psw-btn--quiet" disabled={busy}
              onClick={() => { setMode('none'); setReason('') }}>
              Keep it
            </button>
          </div>
        </form>
      )}
    </div>
  )
}

function WitnessFields({ idPrefix, witnesses, witnessId, setWitnessId, pin, setPin }: {
  idPrefix: string
  witnesses: TransferWitness[]
  witnessId: string
  setWitnessId: (id: string) => void
  pin: string
  setPin: (pin: string) => void
}) {
  return (
    <div className="psw-fields">
      <div className="psw-field">
        <label htmlFor={`${idPrefix}-witness`}>Witness</label>
        <select id={`${idPrefix}-witness`} value={witnessId}
          onChange={e => setWitnessId(e.target.value)}>
          <option value="">Choose a pharmacist</option>
          {witnesses.map(w => <option key={w.id} value={w.id}>{w.name}</option>)}
        </select>
      </div>
      <div className="psw-field">
        <label htmlFor={`${idPrefix}-pin`}>Witness signing PIN</label>
        <input id={`${idPrefix}-pin`} type="password" inputMode="numeric" autoComplete="off"
          value={pin} onChange={e => setPin(e.target.value)} />
      </div>
      {witnesses.length === 0 && (
        <p className="psw-notice psw-notice--warn">
          Nobody else here can witness a controlled transfer yet. A second pharmacist with
          the NDPS permission and a signing PIN is needed.
        </p>
      )}
    </div>
  )
}

// ═════════════════════════════════════════════════════════════════════════════
//  New transfer
// ═════════════════════════════════════════════════════════════════════════════

interface DraftLine {
  key: number
  drugId: string
  units: string
}

let lineKey = 0
function blankLine(): DraftLine {
  lineKey += 1
  return { key: lineKey, drugId: '', units: '' }
}

function NewTransferSection({ fail, options, optionsError, mayNdps, onCreated }: {
  fail: Fail
  options: TransferOptions | null
  optionsError: string
  mayNdps: boolean
  onCreated: (transfer: Transfer) => void
}) {
  const [fromCode, setFromCode] = useState('')
  const [toCode, setToCode] = useState('')
  const [lines, setLines] = useState<DraftLine[]>(() => [blankLine()])
  const [remarks, setRemarks] = useState('')
  const [loaded, setLoaded] = useState<{ code: string; stock: StoreStock | null; error: string }>(
    { code: '', stock: null, error: '' })
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [requestKey, setRequestKey] = useState(newTransferRequestKey)

  useEffect(() => {
    if (!fromCode) return
    let live = true
    getStoreStock(fromCode).then(
      res => { if (live) setLoaded({ code: fromCode, stock: res, error: '' }) },
      err => { if (live) setLoaded({ code: fromCode, stock: null, error: fail(err) }) },
    )
    return () => { live = false }
  }, [fromCode, fail])

  // What the chosen sending store holds; nothing while another store's answer
  // is the last one in.
  const current = fromCode && loaded.code === fromCode ? loaded : null
  const stock = current?.stock ?? null
  const stockError = current?.error ?? ''

  const drugsById = useMemo(
    () => new Map((stock?.drugs ?? []).map(d => [d.drug_id, d])),
    [stock],
  )

  if (!options) {
    return (
      <section className="psw-card" aria-labelledby="psw-new-transfer-title">
        <h2 id="psw-new-transfer-title" className="psw-card__title">New transfer</h2>
        {optionsError ? <p className="psw-error" role="alert">{optionsError}</p>
          : <p className="psw-note" role="status">Loading stores</p>}
      </section>
    )
  }

  const fromStore = options.stores.find(s => s.code === fromCode) ?? null
  const toStore = options.stores.find(s => s.code === toCode) ?? null
  const touchesVault = fromStore?.pharmacy_kind === 'ndps_vault' || toStore?.pharmacy_kind === 'ndps_vault'
  const anyNdps = lines.some(l => drugsById.get(l.drugId)?.ndps)

  function patchLine(key: number, patch: Partial<DraftLine>) {
    setLines(prev => prev.map(l => (l.key === key ? { ...l, ...patch } : l)))
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault()
    setError('')
    if (!fromCode) { setError('Choose the store the stock leaves from.'); return }
    if (!toCode) { setError('Choose the store the stock goes to.'); return }
    if (fromCode === toCode) { setError('A transfer goes from one store to another.'); return }
    const body = []
    const seen = new Set<string>()
    for (const [i, line] of lines.entries()) {
      const n = i + 1
      if (!line.drugId) { setError(`Line ${n}: choose a drug.`); return }
      if (seen.has(line.drugId)) { setError(`Line ${n}: that drug is already on the transfer.`); return }
      seen.add(line.drugId)
      if (!isWholeNumber(line.units) || Number(line.units) < 1) {
        setError(`Line ${n}: units is a whole number, at least 1.`)
        return
      }
      const usable = drugsById.get(line.drugId)?.usable_units
      if (usable !== undefined && Number(line.units) > usable) {
        setError(`Line ${n}: the store holds ${usable} usable units.`)
        return
      }
      body.push({ helix_medication_id: line.drugId, quantity_units: Number(line.units.trim()) })
    }
    setBusy(true)
    createTransfer({ from_store_code: fromCode, to_store_code: toCode, lines: body,
      remarks: remarks.trim() || null, idempotency_key: requestKey }).then(
      res => {
        setRequestKey(newTransferRequestKey())
        setLines([blankLine()])
        setRemarks('')
        onCreated(res.transfer)
      },
      err => setError(fail(err)),
    ).finally(() => setBusy(false))
  }

  return (
    <section className="psw-card" aria-labelledby="psw-new-transfer-title">
      <h2 id="psw-new-transfer-title" className="psw-card__title">New transfer</h2>
      <p className="psw-hint">
        Choose the drugs and how many units. The plan takes the store&apos;s usable lots,
        earliest expiry first; expired, quarantined and recalled stock never moves.
      </p>
      {options.stores.length < 2 && (
        <p className="psw-notice psw-notice--warn">
          A transfer needs two stores. Set up the dispensing pharmacy and a sub-store or the
          NDPS vault in Masters first.
        </p>
      )}
      <form className="psw-form" onSubmit={onSubmit} noValidate>
        <div className="psw-fields">
          <div className="psw-field">
            <label htmlFor="psw-transfer-from">From</label>
            <select id="psw-transfer-from" value={fromCode}
              onChange={e => { setFromCode(e.target.value); setLines([blankLine()]) }}>
              <option value="">Choose a store</option>
              {options.stores.map(s => (
                <option key={s.code} value={s.code}>{`${s.name} (${storeKindLabel(s.pharmacy_kind)})`}</option>
              ))}
            </select>
          </div>
          <div className="psw-field">
            <label htmlFor="psw-transfer-to">To</label>
            <select id="psw-transfer-to" value={toCode} onChange={e => setToCode(e.target.value)}>
              <option value="">Choose a store</option>
              {options.stores.filter(s => s.code !== fromCode).map(s => (
                <option key={s.code} value={s.code}>{`${s.name} (${storeKindLabel(s.pharmacy_kind)})`}</option>
              ))}
            </select>
          </div>
        </div>
        {(touchesVault || anyNdps) && (
          <p className="psw-notice">
            {mayNdps
              ? 'Controlled transfer. Narcotic and psychotropic drugs move only between the NDPS vault and the dispensing pharmacy, and a second pharmacist witnesses the sending and the receipt.'
              : 'Controlled transfer. Only a pharmacist with the NDPS permission can raise it.'}
          </p>
        )}
        {stockError && <p className="psw-error" role="alert">{stockError}</p>}
        {fromCode && stock === null && !stockError && (
          <p className="psw-note" role="status">Loading what this store holds</p>
        )}
        {stock && stock.drugs.length === 0 && (
          <p className="psw-note">This store holds no usable stock to send.</p>
        )}
        {stock && stock.drugs.length > 0 && (
          <>
            <ul className="psw-lines" aria-label="Transfer lines">
              {lines.map((line, i) => {
                const chosen = drugsById.get(line.drugId)
                return (
                  <li key={line.key} className="psw-line">
                    <div className="psw-field">
                      <label htmlFor={`psw-transfer-drug-${line.key}`}>{`Drug, line ${i + 1}`}</label>
                      <select id={`psw-transfer-drug-${line.key}`} value={line.drugId}
                        onChange={e => patchLine(line.key, { drugId: e.target.value })}>
                        <option value="">Choose a drug</option>
                        {stock.drugs.map(d => (
                          <option key={d.drug_id} value={d.drug_id} disabled={!d.classified}>
                            {`${d.drug_name}${d.classified ? '' : ' (not classified)'}`}
                          </option>
                        ))}
                      </select>
                      {chosen && (
                        <span className="psw-sub">
                          {`${chosen.usable_units} usable${chosen.earliest_expiry ? `, earliest expiry ${formatClinicalDateKey(chosen.earliest_expiry)}` : ''}${chosen.ndps ? '. Narcotic or psychotropic' : ''}`}
                        </span>
                      )}
                    </div>
                    <div className="psw-field">
                      <label htmlFor={`psw-transfer-units-${line.key}`}>Units</label>
                      <input id={`psw-transfer-units-${line.key}`} inputMode="numeric" value={line.units}
                        onChange={e => patchLine(line.key, { units: e.target.value })} />
                    </div>
                    <span aria-hidden="true" />
                    <button type="button" className="psw-btn psw-btn--quiet"
                      disabled={lines.length === 1}
                      onClick={() => setLines(prev => prev.filter(l => l.key !== line.key))}>
                      Remove
                    </button>
                  </li>
                )
              })}
            </ul>
            <button type="button" className="psw-btn psw-btn--quiet"
              disabled={lines.length >= options.max_drugs}
              onClick={() => setLines(prev => [...prev, blankLine()])}>
              Add a drug
            </button>
          </>
        )}
        <div className="psw-field psw-field--wide">
          <label htmlFor="psw-transfer-remarks">Remarks</label>
          <textarea id="psw-transfer-remarks" rows={2} value={remarks}
            onChange={e => setRemarks(e.target.value)} />
        </div>
        {error && <p className="psw-error" role="alert">{error}</p>}
        <div className="psw-buttons">
          <button type="submit" className="psw-btn psw-btn--primary" disabled={busy}>
            Save draft and plan
          </button>
        </div>
      </form>
    </section>
  )
}
