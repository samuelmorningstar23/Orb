import { useCallback, useEffect, useState, useSyncExternalStore, type FormEvent } from 'react'
import { toPharmacyStoreError } from '../../../api/pharmacyStore'
import {
  WARD_RETURN_STATUS_LABELS,
  getReturnableOrders,
  getWardReturnOptions,
  listWardReturns,
  newWardReturnKey,
  recordWardReturn,
  returnUnitsWords,
  type ReturnableBatch,
  type ReturnableOrder,
  type WardReturn,
  type WardReturnOptions,
} from '../../../api/pharmacyStore/returns'
import { formatClinicalDateTime } from '../../../lib/datetime'
import { WARD_RETURN_RECORD, hasPermission } from '../../../lib/roles'
import { currentModuleFlags, subscribeModuleFlags } from '../../../shellConfig'
import './WardStockSection.css'

/* ═══════════════════════════════════════════════════
   RETURN TO PHARMACY: medicines the ward no longer needs (grn-15)

   Shown only while the pharmacy store module is switched on
   (ORB_PHARMACY_STORE, off by default) and only to an account holding
   ward_return.record (the nurse, ruling GRN-R8 (a)). With the module off this
   renders nothing and asks the server nothing, so the Deliveries tab is
   exactly what it was.

   A return names a medicine this ward signed for (a DELIVERED order) and can
   never be more than that order's dispense took minus what already came back:
   the server refuses more, and refuses another ward's order as if it did not
   exist. The units wait in the pharmacy's returns quarantine, never usable,
   until a pharmacist inspects them.

   A narcotic or psychotropic medicine is not returned from here: it goes back
   to the pharmacy counter, where a second pharmacist witnesses it. The row
   says so instead of offering the button.

   The rule from the Deliveries sheet binds here too: a failed read never
   renders as an empty list.
   ═══════════════════════════════════════════════════ */

function useReturnsShown(): boolean {
  const modules = useSyncExternalStore(subscribeModuleFlags, currentModuleFlags)
  return Boolean(modules['pharmacy-store'] && hasPermission(WARD_RETURN_RECORD))
}

function failureText(err: unknown): string {
  const failure = toPharmacyStoreError(err)
  const text = failure.message || 'Something went wrong. Nothing was saved.'
  return text.charAt(0).toUpperCase() + text.slice(1)
}

function batchLabel(batch: ReturnableBatch): string {
  const name = batch.drug_name || 'Medicine'
  const strength = (batch.strength || '').trim()
  return strength && !name.toLowerCase().includes(strength.toLowerCase())
    ? `${name} ${strength}` : name
}

export default function WardReturnsSection() {
  const shown = useReturnsShown()
  if (!shown) return null
  return <WardReturnsBody />
}

interface Loaded {
  orders: ReturnableOrder[]
  returns: WardReturn[]
  options: WardReturnOptions
}

function WardReturnsBody() {
  const [data, setData] = useState<Loaded | null>(null)
  const [failed, setFailed] = useState<string | null>(null)
  const [flash, setFlash] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const [orders, returns, options] = await Promise.all([
        getReturnableOrders(), listWardReturns(), getWardReturnOptions(),
      ])
      setData({ orders: orders.orders || [], returns: returns.ward_returns || [], options })
      setFailed(null)
    } catch (err) {
      // `data` is left as it was: a failed refresh must not paint a quiet list.
      setFailed(failureText(err))
    }
  }, [])

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch trigger, not derived state.
    void load()
  }, [load])

  const done = async (message: string) => {
    setFlash(message)
    await load()
  }

  const recent = (data?.returns ?? []).slice(0, 10)

  return (
    <section className="ward-stock" aria-labelledby="ward-returns-title">
      <h2 className="orb-section-header__label" id="ward-returns-title">Return to pharmacy</h2>
      {flash && <div className="nurse-note nurse-note--ok" role="status">{flash}</div>}

      {failed && (
        <div className="orb-state orb-state--error" role="alert">
          <div className="orb-state__content">
            <p className="orb-state__title">Could not load returns.</p>
            <p className="orb-state__detail">
              {failed} This is a display failure. It does not mean there is nothing to send back.
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
          <div className="ward-stock__block">
            <h3 className="ward-stock__heading">Medicines that can go back</h3>
            {data.orders.length === 0 ? (
              <p className="nurse-section__hint">
                No medicine signed for on this ward in the last 30 days has units left to return.
              </p>
            ) : (
              <ul className="orb-list ward-stock__list">
                {data.orders.flatMap(order => order.batches
                  .filter(batch => batch.returnable_units > 0)
                  .map(batch => (
                    <ReturnRow key={`${order.id}:${batch.lot_id}`} order={order} batch={batch}
                               options={data.options} onDone={done} />
                  )))}
              </ul>
            )}
          </div>

          {recent.length > 0 && (
            <div className="ward-stock__block">
              <h3 className="ward-stock__heading">Sent back from this ward</h3>
              <ul className="orb-list ward-stock__list">
                {recent.map(r => (
                  <li key={r.id} className="orb-row ward-stock__row">
                    <div className="ward-stock__row-main">
                      <span className="nurse-delivery__drug">
                        {r.drug_name || 'Medicine'}: {returnUnitsWords(r.quantity_units, r.dispense_unit)}
                      </span>
                      <span className="ward-stock__muted">
                        {[r.patient_name, r.patient_bed && `Bed ${r.patient_bed}`]
                          .filter(Boolean).join(' · ')}
                        {r.occurred_at ? ` · ${formatClinicalDateTime(r.occurred_at)}` : ''}
                      </span>
                    </div>
                    <span className="ward-stock__chip">
                      {WARD_RETURN_STATUS_LABELS[r.status] ?? r.status_label}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}

      <p className="nurse-footnote">
        Returned medicines wait in the pharmacy&apos;s returns store and are never used again
        until a pharmacist has inspected them. Narcotic and psychotropic medicines go back to the
        pharmacy counter, where a second pharmacist witnesses them.
      </p>
    </section>
  )
}

function ReturnRow({ order, batch, options, onDone }: {
  order: ReturnableOrder
  batch: ReturnableBatch
  options: WardReturnOptions
  onDone: (message: string) => Promise<void>
}) {
  const [open, setOpen] = useState(false)
  const [units, setUnits] = useState(String(batch.returnable_units))
  const [seal, setSeal] = useState<'' | 'yes' | 'no'>('')
  const [note, setNote] = useState('')
  const [store, setStore] = useState(options.stores.length === 1 ? options.stores[0].code : '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // One key per return being recorded, kept across retries of the same return.
  const [key, setKey] = useState(() => newWardReturnKey())

  const raw = units.trim()
  const count = /^\d+$/.test(raw) ? Number(raw) : NaN
  const unitsValid = Number.isInteger(count) && count >= 1 && count <= batch.returnable_units
  const storeValid = options.stores.length <= 1 || Boolean(store)
  const noStore = options.stores.length === 0
  const controlled = batch.controlled && !options.may_record_controlled

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (!unitsValid || !storeValid) return
    setBusy(true)
    setError(null)
    try {
      await recordWardReturn({
        helix_order_id: order.id,
        quantity_units: count,
        lot_id: batch.lot_id,
        seal_intact: seal === '' ? null : seal === 'yes',
        note: note.trim() || null,
        store_code: store || null,
      }, key)
      setOpen(false)
      setKey(newWardReturnKey())
      await onDone(`Return recorded in your name: ${returnUnitsWords(count, batch.dispense_unit)} of ` +
        `${batchLabel(batch)}. Take it to the pharmacy.`)
    } catch (err) {
      setError(failureText(err))
    } finally {
      setBusy(false)
    }
  }

  const fieldId = `ward-return-${order.id}-${batch.lot_id}`
  return (
    <li className="orb-row ward-stock__transfer">
      <div className="ward-stock__transfer-head">
        <span className="nurse-delivery__drug">{batchLabel(batch)}</span>
        <span className="nurse-delivery__from">
          {returnUnitsWords(batch.returnable_units, batch.dispense_unit)} can go back
        </span>
      </div>
      <span className="nurse-delivery__patient">
        {[order.patient_name, order.bed && `Bed ${order.bed}`, order.ward].filter(Boolean).join(' · ')}
      </span>
      <span className="ward-stock__muted">
        Batch {batch.batch_no || 'not recorded'}
        {order.received_at ? ` · received ${formatClinicalDateTime(order.received_at)}` : ''}
        {order.received_by_name ? ` by ${order.received_by_name}` : ''}
      </span>
      {error && <div className="nurse-note nurse-note--danger" role="alert">{error}</div>}

      {controlled ? (
        <span className="ward-stock__problem">
          Narcotic or psychotropic: take it to the pharmacy counter, where a second pharmacist
          witnesses the return.
        </span>
      ) : noStore ? (
        <span className="ward-stock__problem">
          The pharmacy has no returns store set up yet, so returns cannot be recorded. Ask the
          pharmacy.
        </span>
      ) : !open ? (
        <div className="ward-stock__actions">
          <button type="button" className="nurse-btn nurse-btn--secondary"
                  onClick={() => { setError(null); setOpen(true) }}>
            Return to pharmacy
          </button>
        </div>
      ) : (
        <form className="ward-stock__requests" onSubmit={e => void submit(e)} noValidate>
          <label className="ward-stock__field" htmlFor={`${fieldId}-units`}>
            Units coming back
            <input id={`${fieldId}-units`} className="shift-input ward-stock__count" inputMode="numeric"
                   value={units} onChange={e => setUnits(e.target.value)} disabled={busy} />
          </label>
          {!unitsValid && raw !== '' && (
            <span className="ward-stock__problem">
              Enter a whole number from 1 to {batch.returnable_units}.
            </span>
          )}
          <div className="ward-stock__field ward-stock__field--wide" role="radiogroup"
               aria-labelledby={`${fieldId}-seal-label`}>
            <span id={`${fieldId}-seal-label`}>Is the pack still sealed?</span>
            {(['yes', 'no', ''] as const).map(value => (
              <label key={value || 'unknown'} className="ward-stock__pick">
                <input type="radio" name={`${fieldId}-seal`} value={value} disabled={busy}
                       checked={seal === value} onChange={() => setSeal(value)} />
                {value === 'yes' ? 'Sealed' : value === 'no' ? 'Opened' : 'Not checked'}
              </label>
            ))}
          </div>
          {options.stores.length > 1 && (
            <label className="ward-stock__field ward-stock__field--wide" htmlFor={`${fieldId}-store`}>
              Returns store
              <select id={`${fieldId}-store`} className="shift-input" value={store}
                      onChange={e => setStore(e.target.value)} disabled={busy}>
                <option value="">Choose the returns store</option>
                {options.stores.map(s => <option key={s.code} value={s.code}>{s.name}</option>)}
              </select>
            </label>
          )}
          <label className="ward-stock__field ward-stock__field--wide" htmlFor={`${fieldId}-note`}>
            Note (optional)
            <input id={`${fieldId}-note`} className="shift-input" value={note}
                   onChange={e => setNote(e.target.value)} disabled={busy}
                   placeholder="For example, course stopped on the ward round" />
          </label>
          <div className="ward-stock__actions">
            <button type="submit" className="nurse-btn nurse-btn--primary"
                    disabled={busy || !unitsValid || !storeValid}>
              {busy ? 'Recording…' : 'Record return'}
            </button>
            <button type="button" className="nurse-btn nurse-btn--ghost" disabled={busy}
                    onClick={() => setOpen(false)}>
              Not now
            </button>
          </div>
        </form>
      )}
    </li>
  )
}
