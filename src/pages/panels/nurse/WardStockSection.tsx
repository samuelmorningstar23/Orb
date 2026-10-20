import { useCallback, useEffect, useState, useSyncExternalStore } from 'react'
import { toPharmacyStoreError } from '../../../api/pharmacyStore'
import {
  WARD_INDENT_PRIORITY_LABELS,
  cancelWardIndent,
  getWardStock,
  getWardTransfersAwaiting,
  listWardIndents,
  raiseWardIndent,
  receiveWardTransfer,
  type WardAwaitingTransfer,
  type WardIndent,
  type WardIndentPriority,
  type WardStockDrug,
  type WardStockOverview,
  type WardStoreStock,
} from '../../../api/pharmacyStore/ward'
import { formatClinicalDateKey, formatClinicalDateTime } from '../../../lib/datetime'
import { WARD_STOCK_READ, hasPermission } from '../../../lib/roles'
import { currentFeatureFlags, currentModuleFlags, subscribeModuleFlags } from '../../../shellConfig'
import './WardStockSection.css'

/* ═══════════════════════════════════════════════════
   WARD STOCK: the ward's own imprest store (grn-21)

   Shown only while the pharmacy store module AND ward stock are both switched
   on (ORB_PHARMACY_STORE, ORB_PHARMACY_WARD_STOCK; ward stock stays off until
   ruling GRN-RS5 is confirmed) and only to an account holding ward_stock.read.
   With either switch off this renders nothing at all, so the Deliveries tab is
   exactly what it was.

   Three jobs, in the order a nurse meets them:
     1. What is on its way from the pharmacy, and confirming what arrived, in
        her own name. The server refuses the person who sent it.
     2. What the ward holds against its stock list (par levels).
     3. Asking the pharmacy for a top-up of drugs on that list.

   The server decides everything: which drugs a ward may hold (never NDPS
   stock, never a barred high-alert drug or concentrated electrolyte, never a
   drug off the list), how much, and who receives. Giving a dose from ward
   stock still needs an active order on the drug chart; nothing here changes
   the medicine round.

   The rule from the Deliveries sheet binds here too: a failed read never
   renders as an empty ward. "Nothing on its way" and "we could not find out"
   are opposite facts.
   ═══════════════════════════════════════════════════ */

function useWardStockShown(): boolean {
  const modules = useSyncExternalStore(subscribeModuleFlags, currentModuleFlags)
  const features = useSyncExternalStore(subscribeModuleFlags, currentFeatureFlags)
  // Short-circuit on purpose: the permission check reads the signed-in role,
  // which is only worth asking once both switches are on.
  return Boolean(modules['pharmacy-store'] && features.pharmacy_ward_stock
    && hasPermission(WARD_STOCK_READ))
}

function failureText(err: unknown): string {
  const failure = toPharmacyStoreError(err)
  const text = failure.message || 'Something went wrong. Nothing was saved.'
  return text.charAt(0).toUpperCase() + text.slice(1)
}

function unitsWord(n: number, unit?: string | null): string {
  const word = (unit || '').trim().toLowerCase()
  if (word) return `${n} ${word}`
  return `${n} ${n === 1 ? 'unit' : 'units'}`
}

export default function WardStockSection() {
  const shown = useWardStockShown()
  if (!shown) return null
  return <WardStockBody />
}

interface Loaded {
  overview: WardStockOverview
  awaiting: WardAwaitingTransfer[]
  indents: WardIndent[]
}

function WardStockBody() {
  const [data, setData] = useState<Loaded | null>(null)
  const [failed, setFailed] = useState<string | null>(null)
  const [flash, setFlash] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const [overview, awaiting, indents] = await Promise.all([
        getWardStock(), getWardTransfersAwaiting(), listWardIndents({ status: 'open' }),
      ])
      setData({ overview, awaiting: awaiting.transfers || [], indents: indents.indents || [] })
      setFailed(null)
    } catch (err) {
      // `data` is left as it was: a failed refresh must not paint a quiet ward.
      setFailed(failureText(err))
    }
  }, [])

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch trigger, not derived state.
    void load()
  }, [load])

  const done = async (message: string) => {
    setError(null)
    setFlash(message)
    await load()
  }

  return (
    <section className="ward-stock" aria-labelledby="ward-stock-title">
      <h2 className="orb-section-header__label" id="ward-stock-title">Ward stock</h2>
      {flash && <div className="nurse-note nurse-note--ok" role="status">{flash}</div>}
      {error && <div className="nurse-note nurse-note--danger" role="alert">{error}</div>}

      {failed && (
        <div className="orb-state orb-state--error" role="alert">
          <div className="orb-state__content">
            <p className="orb-state__title">Could not load ward stock.</p>
            <p className="orb-state__detail">
              {failed} This is a display failure. It does not mean nothing is on its way from
              the pharmacy.
            </p>
            <button type="button" className="nurse-btn nurse-btn--secondary"
                    onClick={() => void load()}>
              Retry
            </button>
          </div>
        </div>
      )}

      {data && (
        <>
          <AwaitingList transfers={data.awaiting} mayReceive={data.overview.may_receive}
                        onDone={done} onError={setError} />
          {data.overview.stores.length === 0 && !failed && (
            <div className="orb-state orb-state--empty">
              <div className="orb-state__content">
                <p className="orb-state__title">No ward store for your department.</p>
                <p className="orb-state__detail">
                  The pharmacy sets up a ward&apos;s stock store and its stock list. Ask them
                  if your ward should have one.
                </p>
              </div>
            </div>
          )}
          {data.overview.stores.map(store => (
            <StoreBlock key={store.store.id} store={store} overview={data.overview}
                        indents={data.indents.filter(i => i.store_id === store.store.id)}
                        onDone={done} onError={setError} />
          ))}
        </>
      )}

      <p className="nurse-footnote">
        Ward stock holds only drugs on this ward&apos;s stock list. Controlled drugs,
        high-alert medicines and concentrated electrolytes are never ward stock; the
        pharmacy dispenses them for each patient. A dose from ward stock still needs an
        active order on the drug chart.
      </p>
    </section>
  )
}

// ─── On its way from the pharmacy ───────────────────────────────────────────

function AwaitingList({ transfers, mayReceive, onDone, onError }: {
  transfers: WardAwaitingTransfer[]
  mayReceive: boolean
  onDone: (message: string) => Promise<void>
  onError: (message: string | null) => void
}) {
  return (
    <div className="ward-stock__block">
      <h3 className="ward-stock__heading">On its way from the pharmacy</h3>
      {transfers.length === 0 ? (
        <p className="nurse-section__hint">Nothing is on its way to this ward.</p>
      ) : (
        <ul className="orb-list ward-stock__list">
          {transfers.map(t => (
            <AwaitingTransfer key={t.id} transfer={t} mayReceive={mayReceive}
                              onDone={onDone} onError={onError} />
          ))}
        </ul>
      )}
    </div>
  )
}

function AwaitingTransfer({ transfer, mayReceive, onDone, onError }: {
  transfer: WardAwaitingTransfer
  mayReceive: boolean
  onDone: (message: string) => Promise<void>
  onError: (message: string | null) => void
}) {
  const pending = (transfer.lines || []).filter(line => line.received_units === null)
  const [counts, setCounts] = useState<Record<number, string>>(
    () => Object.fromEntries(pending.map(line => [line.id, String(line.quantity_units)])))
  const [reasons, setReasons] = useState<Record<number, string>>({})
  const [busy, setBusy] = useState(false)
  // A drug on this delivery was made a controlled drug after it left the
  // pharmacy: a pharmacist witnesses the count with their own signing PIN.
  const needsWitness = Boolean(transfer.witness_required)
  const witnesses = transfer.witnesses || []
  const [witnessId, setWitnessId] = useState('')
  const [witnessPin, setWitnessPin] = useState('')

  const short = (lineId: number, sent: number) => {
    const got = Number(counts[lineId])
    return Number.isInteger(got) && got < sent
  }
  const countsValid = pending.every(line => {
    const raw = (counts[line.id] ?? '').trim()
    if (!/^\d+$/.test(raw)) return false
    const got = Number(raw)
    if (got > line.quantity_units) return false
    return got === line.quantity_units || Boolean((reasons[line.id] || '').trim())
  })
  const valid = countsValid && (!needsWitness || (Boolean(witnessId) && Boolean(witnessPin)))

  const receive = async () => {
    setBusy(true)
    onError(null)
    try {
      const lines = pending.map(line => ({
        line_id: line.id,
        received_units: Number(counts[line.id]),
        discrepancy_reason: short(line.id, line.quantity_units)
          ? (reasons[line.id] || '').trim() : null,
      }))
      if (needsWitness) {
        await receiveWardTransfer(transfer.id, lines,
          { witness_id: witnessId, witness_pin: witnessPin })
      } else {
        await receiveWardTransfer(transfer.id, lines)
      }
      await onDone(needsWitness
        ? 'Receipt recorded in your name, witnessed. The controlled drug is held, not for use.'
        : 'Receipt recorded in your name.')
    } catch (err) {
      onError(failureText(err))
    } finally {
      // A PIN is never kept on screen after it was used, right or wrong.
      setWitnessPin('')
      setBusy(false)
    }
  }

  const label = transfer.transfer_number || `Transfer ${transfer.id}`
  return (
    <li className="orb-row ward-stock__transfer">
      <div className="ward-stock__transfer-head">
        <span className="nurse-delivery__drug">{label}</span>
        <span className="nurse-delivery__from">
          Sent by {transfer.dispatched_by_name || 'the pharmacy'}
          {transfer.dispatched_at ? `, ${formatClinicalDateTime(transfer.dispatched_at)}` : ''}
        </span>
      </div>
      <ul className="ward-stock__lines">
        {pending.map(line => (
          <li key={line.id} className="ward-stock__line">
            <span className="ward-stock__line-drug">
              {line.drug_name}
              <span className="ward-stock__muted">
                {' '}Batch {line.batch_no || 'not recorded'}
                {line.expiry_date ? `, expires ${formatClinicalDateKey(line.expiry_date)}` : ''}
              </span>
            </span>
            <span className="ward-stock__muted">Sent {unitsWord(line.quantity_units)}</span>
            {line.ndps && (
              <span className="ward-stock__problem">
                Now a controlled drug. It is held on the ward, not for use. Tell the pharmacy.
              </span>
            )}
            {mayReceive && (
              <label className="ward-stock__field">
                <span>Arrived</span>
                <input className="shift-input ward-stock__count" inputMode="numeric"
                       aria-label={`Units of ${line.drug_name} that arrived`}
                       value={counts[line.id] ?? ''}
                       onChange={e => setCounts(c => ({ ...c, [line.id]: e.target.value }))} />
              </label>
            )}
            {mayReceive && short(line.id, line.quantity_units) && (
              <label className="ward-stock__field ward-stock__field--wide">
                <span>What happened to the rest</span>
                <input className="shift-input" value={reasons[line.id] ?? ''}
                       aria-label={`Why fewer units of ${line.drug_name} arrived`}
                       onChange={e => setReasons(r => ({ ...r, [line.id]: e.target.value }))} />
              </label>
            )}
          </li>
        ))}
      </ul>
      {mayReceive && needsWitness && (
        <div className="ward-stock__witness">
          <p className="nurse-note nurse-note--warn">
            A drug on this delivery became a controlled drug after it was sent. A pharmacist
            witnesses your count with their own signing PIN. The person who sent it cannot.
          </p>
          {witnesses.length === 0 ? (
            <p className="nurse-section__hint">
              No pharmacist with a signing PIN can witness this yet. Ask the pharmacy.
            </p>
          ) : (
            <div className="ward-stock__actions">
              <label className="ward-stock__field">
                <span>Witness</span>
                <select className="shift-input" value={witnessId}
                        onChange={e => setWitnessId(e.target.value)}>
                  <option value="">Choose who witnesses</option>
                  {witnesses.map(w => <option key={w.id} value={w.id}>{w.name}</option>)}
                </select>
              </label>
              <label className="ward-stock__field">
                <span>Witness PIN</span>
                <input className="shift-input ward-stock__count" type="password"
                       inputMode="numeric" autoComplete="off" value={witnessPin}
                       onChange={e => setWitnessPin(e.target.value)} />
              </label>
            </div>
          )}
        </div>
      )}
      {mayReceive && (
        <div className="ward-stock__actions">
          <button type="button" className="nurse-btn nurse-btn--primary"
                  onClick={() => void receive()} disabled={busy || !valid}>
            {busy ? 'Recording…' : 'Confirm what arrived'}
          </button>
        </div>
      )}
    </li>
  )
}

// ─── One ward store ──────────────────────────────────────────────────────────

function StoreBlock({ store, overview, indents, onDone, onError }: {
  store: WardStoreStock
  overview: WardStockOverview
  indents: WardIndent[]
  onDone: (message: string) => Promise<void>
  onError: (message: string | null) => void
}) {
  const listed = store.drugs.filter(d => d.listed)
  const unlisted = store.drugs.filter(d => !d.listed)
  return (
    <div className="ward-stock__block">
      <h3 className="ward-stock__heading">
        {store.store.name}
        {store.store.ward ? <span className="ward-stock__muted"> · {store.store.ward}</span> : null}
      </h3>
      {!store.store.is_active && (
        <p className="nurse-note nurse-note--warn">This store is closed.</p>
      )}
      {listed.length === 0 ? (
        <p className="nurse-section__hint">
          No stock list is set for this store yet. The pharmacy sets it.
        </p>
      ) : (
        <ul className="orb-list ward-stock__list">
          {listed.map(drug => <StockRow key={drug.drug_id} drug={drug} />)}
        </ul>
      )}
      {unlisted.length > 0 && (
        <div className="ward-stock__unlisted">
          <p className="nurse-section__hint">Held here but not on the stock list:</p>
          <ul className="orb-list ward-stock__list">
            {unlisted.map(drug => <StockRow key={drug.drug_id} drug={drug} />)}
          </ul>
        </div>
      )}
      <OpenRequests indents={indents} onDone={onDone} onError={onError} />
      {overview.may_raise && store.store.is_active && listed.length > 0 && (
        // Keyed on what a top-up would ask for, so fresh numbers after a
        // reload replace the form's starting amounts.
        <TopUpForm key={store.drugs.map(d => `${d.drug_id}:${d.top_up_units}`).join('|')}
                   store={store} priorities={overview.priorities} onDone={onDone}
                   onError={onError} />
      )}
    </div>
  )
}

function StockRow({ drug }: { drug: WardStockDrug }) {
  const chips: { text: string; tone: 'warn' | 'danger' | 'muted' }[] = []
  if (drug.listed && drug.below_par) chips.push({ text: 'Below par', tone: 'warn' })
  if (drug.expired_units) chips.push({ text: `${drug.expired_units} expired`, tone: 'danger' })
  if (drug.held_units) chips.push({ text: `${drug.held_units} held, do not use`, tone: 'danger' })
  if (drug.below_zero_units) {
    chips.push({ text: `${drug.below_zero_units} given with none on the books, count this drug`, tone: 'warn' })
  }
  if (drug.in_transit_units) chips.push({ text: `${drug.in_transit_units} on its way`, tone: 'muted' })
  if (drug.requested_units) chips.push({ text: `${drug.requested_units} asked for`, tone: 'muted' })
  return (
    <li className="orb-row ward-stock__row">
      <div className="ward-stock__row-main">
        <span className="nurse-delivery__drug">{drug.drug_name}</span>
        <span className="ward-stock__muted">
          {drug.listed && drug.par_units !== null
            ? `${unitsWord(drug.on_hand_units, drug.dispense_unit)} on hand of ${drug.par_units}`
            : `${unitsWord(drug.on_hand_units, drug.dispense_unit)} on hand`}
          {drug.earliest_expiry ? `, earliest expiry ${formatClinicalDateKey(drug.earliest_expiry)}` : ''}
        </span>
        {drug.problem && <span className="ward-stock__problem">{drug.problem.message}</span>}
      </div>
      {chips.length > 0 && (
        <div className="ward-stock__chips">
          {chips.map(chip => (
            <span key={chip.text} className={`ward-stock__chip ward-stock__chip--${chip.tone}`}>
              {chip.text}
            </span>
          ))}
        </div>
      )}
    </li>
  )
}

// ─── Requests to the pharmacy ────────────────────────────────────────────────

function OpenRequests({ indents, onDone, onError }: {
  indents: WardIndent[]
  onDone: (message: string) => Promise<void>
  onError: (message: string | null) => void
}) {
  const [cancelling, setCancelling] = useState<number | null>(null)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  if (indents.length === 0) return null

  const cancel = async (indent: WardIndent) => {
    setBusy(true)
    onError(null)
    try {
      await cancelWardIndent(indent.id, reason.trim())
      setCancelling(null)
      setReason('')
      await onDone('Request withdrawn.')
    } catch (err) {
      onError(failureText(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="ward-stock__requests">
      <p className="nurse-section__hint">Requests to the pharmacy:</p>
      <ul className="orb-list ward-stock__list">
        {indents.map(indent => (
          <li key={indent.id} className="orb-row ward-stock__row">
            <div className="ward-stock__row-main">
              <span className="nurse-delivery__drug">
                {indent.indent_number || `Request ${indent.id}`}: {indent.status_label}
              </span>
              <span className="ward-stock__muted">
                {indent.lines.map(line => `${line.drug_name} ${line.approved_units ?? line.requested_units}`).join(', ')}
              </span>
              <span className="ward-stock__muted">
                Asked by {indent.requested_by_name || 'the ward'}
                {indent.requested_at ? `, ${formatClinicalDateTime(indent.requested_at)}` : ''}
              </span>
            </div>
            {indent.status === 'submitted' && (cancelling === indent.id ? (
              <div className="ward-stock__cancel">
                <input className="shift-input" value={reason} placeholder="Why it is no longer needed"
                       aria-label="Reason for withdrawing the request"
                       onChange={e => setReason(e.target.value)} />
                <button type="button" className="nurse-btn nurse-btn--secondary"
                        disabled={busy || !reason.trim()} onClick={() => void cancel(indent)}>
                  Withdraw
                </button>
                <button type="button" className="nurse-btn nurse-btn--ghost"
                        onClick={() => { setCancelling(null); setReason('') }}>
                  Keep it
                </button>
              </div>
            ) : (
              <button type="button" className="nurse-btn nurse-btn--ghost"
                      onClick={() => { setCancelling(indent.id); setReason('') }}>
                Withdraw request
              </button>
            ))}
          </li>
        ))}
      </ul>
    </div>
  )
}

function TopUpForm({ store, priorities, onDone, onError }: {
  store: WardStoreStock
  priorities: WardIndentPriority[]
  onDone: (message: string) => Promise<void>
  onError: (message: string | null) => void
}) {
  const askable = store.drugs.filter(d => d.listed && !d.problem && d.top_up_units > 0)
  const [amounts, setAmounts] = useState<Record<string, string>>(
    () => Object.fromEntries(askable.map(d => [d.drug_id, String(d.top_up_units)])))
  const [picked, setPicked] = useState<Record<string, boolean>>({})
  const [priority, setPriority] = useState<WardIndentPriority>('routine')
  const [busy, setBusy] = useState(false)

  if (askable.length === 0) {
    return (
      <p className="nurse-section__hint">
        Nothing on the list needs a top-up beyond what is on hand, on its way or already asked for.
      </p>
    )
  }

  const lines = askable
    .filter(d => picked[d.drug_id])
    .map(d => ({ helix_medication_id: d.drug_id, quantity_units: Number(amounts[d.drug_id]) }))
  const valid = lines.length > 0
    && lines.every(l => Number.isInteger(l.quantity_units) && l.quantity_units > 0)

  const send = async () => {
    setBusy(true)
    onError(null)
    try {
      await raiseWardIndent({ store_code: store.store.code, lines, priority })
      setPicked({})
      await onDone('Request sent to the pharmacy.')
    } catch (err) {
      onError(failureText(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="ward-stock__topup">
      <p className="nurse-section__hint">Ask the pharmacy for a top-up:</p>
      <ul className="ward-stock__lines">
        {askable.map(drug => (
          <li key={drug.drug_id} className="ward-stock__line">
            <label className="ward-stock__pick">
              <input type="checkbox" checked={Boolean(picked[drug.drug_id])}
                     onChange={e => setPicked(p => ({ ...p, [drug.drug_id]: e.target.checked }))} />
              <span>{drug.drug_name}</span>
            </label>
            <label className="ward-stock__field">
              <span>Units</span>
              <input className="shift-input ward-stock__count" inputMode="numeric"
                     aria-label={`Units of ${drug.drug_name} to ask for`}
                     value={amounts[drug.drug_id] ?? ''}
                     onChange={e => setAmounts(a => ({ ...a, [drug.drug_id]: e.target.value }))} />
            </label>
          </li>
        ))}
      </ul>
      <div className="ward-stock__actions">
        <label className="ward-stock__field">
          <span>Priority</span>
          <select className="shift-input" value={priority}
                  onChange={e => setPriority(e.target.value as WardIndentPriority)}>
            {priorities.map(p => (
              <option key={p} value={p}>{WARD_INDENT_PRIORITY_LABELS[p] ?? p}</option>
            ))}
          </select>
        </label>
        <button type="button" className="nurse-btn nurse-btn--primary"
                disabled={busy || !valid} onClick={() => void send()}>
          {busy ? 'Sending…' : 'Send request'}
        </button>
      </div>
    </div>
  )
}
