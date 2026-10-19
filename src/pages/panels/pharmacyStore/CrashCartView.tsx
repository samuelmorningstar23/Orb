/**
 * Pharmacy store, Crash carts (grn-23): every crash cart against its list, its
 * seal and its checks, expiry inside the cart, the top-up after use, the
 * pharmacy's decision on a check that found a difference, and which carts a
 * batch went to.
 *
 * Discovered by the panel through ./viewRegistry.ts; shown to anyone holding
 * pharmacy_store.read. Behind ward stock as well as the module
 * (ORB_PHARMACY_WARD_STOCK, off until Samuel confirms ruling GRN-RS5): with it
 * off this view says so and asks the server nothing.
 *
 * A crash cart is a ward store. It is filled by ordinary ward requests (the
 * Ward stock view decides and sends them) for drugs on its list only, and
 * Helix never dispenses from it. This view adds the cart's own record.
 *
 * THE SERVER DECIDES. Who may check a cart, that a full check counts every
 * batch in it and ends with a new seal, that a count differing from the books
 * needs a note and waits for the pharmacy, that the person who counted never
 * confirms it, that units found short leave the books only on confirmation and
 * units found over are never added, and what is due. Every refusal is shown
 * in the server's words. The buttons offered follow the server's may_* answers.
 *
 * No patient data is read or shown. Expiry dates are calendar keys
 * (formatClinicalDateKey); recorded times are bare UTC from the server and go
 * through formatClinicalDateTime.
 */
import { useCallback, useEffect, useState } from 'react'

import { getUser } from '../../../api/client'
import { toPharmacyStoreError } from '../../../api/pharmacyStore'
import {
  decideCartCheck,
  getCartBatchTrace,
  getCartCheck,
  getCrashCarts,
  listCartChecks,
  newCartCheckKey,
  recordCartCheck,
  requestCartTopUp,
  type CartBatchTrace,
  type CartCheck,
  type CartCheckKind,
  type CartCheckStatus,
  type CartLotEntry,
  type CrashCart,
  type CrashCartOverview,
} from '../../../api/pharmacyStore/crashCart'
import { WARD_INDENT_PRIORITY_LABELS, type WardIndentPriority } from '../../../api/pharmacyStore/ward'
import { formatClinicalDateKey, formatClinicalDateTime } from '../../../lib/datetime'
import type { PharmacyStoreViewProps } from './viewRegistry'
import './stockAndPurchasing.css'

export const viewId = 'crash-carts'
export const viewLabel = 'Crash carts'
export const viewOrder = 110
export const viewPermission = 'pharmacy_store.read'

type Section = 'carts' | 'record' | 'batch'
type Fail = (err: unknown) => string
type SealState = 'intact' | 'broken' | 'none'

function useFail(onModuleOff: () => void): Fail {
  return useCallback((err: unknown) => {
    const failure = toPharmacyStoreError(err)
    if (failure.isModuleDisabled) onModuleOff()
    const text = failure.message || 'Something went wrong. Nothing was saved.'
    return text.charAt(0).toUpperCase() + text.slice(1)
  }, [onModuleOff])
}

function isWhole(text: string): boolean {
  return /^\d+$/.test(text.trim())
}

function units(n: number): string {
  return `${n} unit${n === 1 ? '' : 's'}`
}

function statusTone(check: CartCheck): string {
  if (check.status === 'submitted') return 'psw-chip--warn'
  if (check.status === 'rejected') return 'psw-chip--danger'
  return 'psw-chip--ok'
}

function checkLine(check: CartCheck): string {
  const who = check.counted_by_name ?? check.counted_by ?? 'Not recorded'
  const when = check.checked_at ? formatClinicalDateTime(check.checked_at) : ''
  return `${check.kind_label} by ${who}${when ? `, ${when}` : ''}`
}

export default function CrashCartView({ meta, onModuleOff }: PharmacyStoreViewProps) {
  const features = (meta.features ?? {}) as Record<string, unknown>
  const on = features.pharmacy_ward_stock === true
  const [section, setSection] = useState<Section>('carts')
  const fail = useFail(onModuleOff)

  if (!on) {
    return (
      <div className="psw-view">
        <p className="psw-notice" role="status">
          Ward stock is switched off on this site, so there are no crash carts to look
          after here. Crash carts are switched on with ward stock.
        </p>
      </div>
    )
  }

  const sections: { id: Section; label: string }[] = [
    { id: 'carts', label: 'Carts' },
    { id: 'record', label: 'Check record' },
    { id: 'batch', label: 'Batch in carts' },
  ]

  return (
    <div className="psw-view">
      <nav className="psw-sections" aria-label="Crash cart sections">
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
      {section === 'carts' && <CartsSection fail={fail} />}
      {section === 'record' && <RecordSection fail={fail} />}
      {section === 'batch' && <BatchSection fail={fail} />}
    </div>
  )
}

// ═════════════════════════════════════════════════════════════════════════════
//  Carts
// ═════════════════════════════════════════════════════════════════════════════

function CartsSection({ fail }: { fail: Fail }) {
  const [data, setData] = useState<CrashCartOverview | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const load = useCallback(() => {
    getCrashCarts().then(
      res => { setError(''); setData(res) },
      err => setError(fail(err)),
    )
  }, [fail])

  useEffect(() => { load() }, [load])

  const carts = data?.carts ?? []
  const tiles = [
    { label: 'Checks due', value: carts.filter(c => c.seal_check_due || c.full_check_due).length,
      tone: 'warn' },
    { label: 'Waiting for the pharmacy', value: carts.filter(c => c.pending_check).length,
      tone: 'warn' },
    { label: 'Expired items', value: carts.reduce((n, c) => n + c.expired.length, 0),
      tone: 'danger' },
    { label: `Expiring within ${data?.near_expiry_days ?? 0} days`,
      value: carts.reduce((n, c) => n + c.expiring.length, 0), tone: 'warn' },
  ]

  return (
    <section className="psw-card" aria-labelledby="psw-carts-title">
      <div className="psw-card__head">
        <h2 id="psw-carts-title" className="psw-card__title">Crash carts</h2>
      </div>
      <p className="psw-hint">
        A crash cart holds only the drugs on its list (par levels in Masters) and is
        filled by ward requests. Helix never dispenses from it.
      </p>
      {notice && <p className="psw-ok" role="status">{notice}</p>}
      {error && <p className="psw-error" role="alert">{error}</p>}
      {data === null && !error && <p className="psw-note" role="status">Loading crash carts</p>}
      {data && carts.length === 0 && (
        <p className="psw-note">No crash cart is set up yet. Add one in Masters.</p>
      )}
      {data && carts.length > 0 && (
        <ul className="psw-tiles" aria-label="Crash carts at a glance">
          {tiles.map(t => (
            <li key={t.label}
                className={`psw-tile${t.value > 0 ? ` psw-tile--${t.tone}` : ''}`}>
              <span className="psw-tile__value">{t.value}</span>
              <span className="psw-tile__label">{t.label}</span>
            </li>
          ))}
        </ul>
      )}
      {data && carts.map(cart => (
        <CartCard key={cart.store.id} cart={cart} overview={data} fail={fail}
                  onChanged={(done: string) => { setNotice(done); load() }} />
      ))}
    </section>
  )
}

function CartCard({ cart, overview, fail, onChanged }: {
  cart: CrashCart
  overview: CrashCartOverview
  fail: Fail
  onChanged: (done: string) => void
}) {
  const [checking, setChecking] = useState(false)
  const name = cart.store.name
  const sealWords = cart.seal_state === 'sealed'
    ? `Sealed with ${cart.seal}`
    : cart.seal_state === 'broken'
      ? `Seal ${cart.seal ?? ''} found broken or changed since it was applied`
      : 'No seal on record'
  const lots: (CartLotEntry & { kind: string })[] = [
    ...cart.expired.map(e => ({ ...e, kind: 'Expired: take it out and replace it' })),
    ...cart.expiring.map(e => ({ ...e,
      kind: `Expires in ${e.days_left ?? 0} day${e.days_left === 1 ? '' : 's'}` })),
    ...cart.held.map(e => ({ ...e, kind: `Held (${e.status_label}): take it out` })),
  ]

  return (
    <div className="psw-detail" aria-label={`Crash cart ${name}`}>
      <p className="psw-strong">
        {name}
        {cart.store.department ? `, ${cart.store.department}` : ''}
        {!cart.store.is_active ? ' (closed)' : ''}
      </p>
      <div className="psw-toolbar">
        {cart.seal_check_due && <span className="psw-chip psw-chip--warn">Seal check due</span>}
        {cart.full_check_due && <span className="psw-chip psw-chip--warn">Full check due</span>}
        {cart.pending_check && (
          <span className="psw-chip psw-chip--warn">Check waiting for the pharmacy</span>
        )}
        {cart.expired.length > 0 && (
          <span className="psw-chip psw-chip--danger">Expired items in the cart</span>
        )}
        {cart.below_par > 0 && <span className="psw-chip psw-chip--warn">Below par</span>}
        {!cart.seal_check_due && !cart.full_check_due && !cart.pending_check
          && cart.expired.length === 0 && cart.below_par === 0 && (
          <span className="psw-chip psw-chip--ok">Checked and complete</span>
        )}
      </div>
      <dl className="psw-facts">
        <div><dt>Seal</dt><dd>{sealWords}</dd></div>
        <div>
          <dt>Last check</dt>
          <dd>
            {cart.last_check ? `${checkLine(cart.last_check)}. ${cart.last_check.outcome}`
              : 'Never checked'}
          </dd>
        </div>
        <div>
          <dt>Last full check</dt>
          <dd>{cart.last_full_check ? checkLine(cart.last_full_check) : 'None on record'}</dd>
        </div>
      </dl>
      {cart.full_check_due && cart.due_reasons.length > 0 && (
        <ul className="psw-hint" aria-label={`Why a full check of ${name} is due`}>
          {cart.due_reasons.map(r => <li key={r}>{r}</li>)}
        </ul>
      )}
      {cart.not_allowed.map(d => (
        <p key={d.drug_id} className="psw-error">{`${d.drug_name}: ${d.message}`}</p>
      ))}

      {lots.length > 0 && (
        <div className="psw-table-scroll">
          <table className="psw-table" aria-label={`Expiry and held items in ${name}`}>
            <thead>
              <tr>
                <th scope="col">Drug</th>
                <th scope="col">Batch</th>
                <th scope="col">Expiry</th>
                <th scope="col" className="psw-num">Units</th>
                <th scope="col">What to do</th>
              </tr>
            </thead>
            <tbody>
              {lots.map(l => (
                <tr key={`${l.kind}-${l.lot_id}`}>
                  <td>{l.drug_name}</td>
                  <td>{l.batch_no ?? 'Not recorded'}</td>
                  <td>{l.expiry_date ? formatClinicalDateKey(l.expiry_date) : 'Not recorded'}</td>
                  <td className="psw-num">{l.units}</td>
                  <td>{l.kind}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {cart.pending_check && (
        <PendingCheck key={cart.pending_check.id} summary={cart.pending_check}
                      mayConfirm={overview.may_confirm} fail={fail} onChanged={onChanged} />
      )}

      <TopUp cart={cart} overview={overview} fail={fail} onChanged={onChanged} />

      {overview.may_check && cart.store.is_active && (checking ? (
        <CheckForm cart={cart} fail={fail} onCancel={() => setChecking(false)}
                   onDone={(done: string) => { setChecking(false); onChanged(done) }} />
      ) : (
        <div className="psw-buttons">
          <button type="button" className="psw-btn" onClick={() => setChecking(true)}>
            Record a check
          </button>
        </div>
      ))}
    </div>
  )
}

// ── Top-up after use ──────────────────────────────────────────────────────────

function TopUp({ cart, overview, fail, onChanged }: {
  cart: CrashCart
  overview: CrashCartOverview
  fail: Fail
  onChanged: (done: string) => void
}) {
  const [priority, setPriority] = useState<WardIndentPriority>('urgent')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  if (cart.top_up.length === 0) return null

  const ask = async () => {
    setBusy(true)
    setError('')
    try {
      const res = await requestCartTopUp(cart.store.code, { priority })
      const left = res.left_out.length
        ? ` Already asked for: ${res.left_out.map(l => l.drug_name).join(', ')}.` : ''
      onChanged(`Top-up asked for ${cart.store.name}. Someone in the pharmacy decides it.${left}`)
    } catch (err) {
      setError(fail(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="psw-form" aria-label={`Top-up for ${cart.store.name}`}>
      <p className="psw-strong">Top-up after use</p>
      <ul className="psw-lines">
        {cart.top_up.map(t => (
          <li key={t.drug_id} className="psw-line">
            <span>{t.drug_name}</span>
            <span className="psw-sub">{units(t.units)}</span>
          </li>
        ))}
      </ul>
      {error && <p className="psw-error" role="alert">{error}</p>}
      {overview.may_raise && cart.store.is_active && (
        <div className="psw-fields">
          <div className="psw-field">
            <label htmlFor={`psw-cart-priority-${cart.store.id}`}>Priority</label>
            <select id={`psw-cart-priority-${cart.store.id}`} value={priority}
                    onChange={e => setPriority(e.target.value as WardIndentPriority)}>
              {overview.priorities.map(p => (
                <option key={p} value={p}>{WARD_INDENT_PRIORITY_LABELS[p] ?? p}</option>
              ))}
            </select>
          </div>
          <div className="psw-buttons">
            <button type="button" className="psw-btn psw-btn--primary" disabled={busy}
                    onClick={() => void ask()}>
              Ask for the top-up
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

// ── A check waiting for the pharmacy ─────────────────────────────────────────

function PendingCheck({ summary, mayConfirm, fail, onChanged }: {
  summary: CartCheck
  mayConfirm: boolean
  fail: Fail
  onChanged: (done: string) => void
}) {
  const me = getUser()?.id ?? null
  const [check, setCheck] = useState<CartCheck | null>(null)
  const [error, setError] = useState('')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let alive = true
    getCartCheck(summary.id).then(
      res => { if (alive) setCheck(res.check) },
      err => { if (alive) setError(fail(err)) },
    )
    return () => { alive = false }
  }, [summary.id, fail])

  const mine = me !== null && summary.counted_by === me
  const differing = (check?.lines ?? []).filter(l => l.short_units > 0 || l.over_units > 0)

  const decide = async (confirm: boolean) => {
    setBusy(true)
    setError('')
    try {
      await decideCartCheck(summary.id, { confirm, reason: reason.trim() || null })
      onChanged(confirm
        ? 'Check confirmed. Units found short are off the books.'
        : 'Check refused. The cart is to be counted again.')
    } catch (err) {
      setError(fail(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="psw-form" aria-label="Check waiting for the pharmacy">
      <p className="psw-strong">Check waiting for the pharmacy</p>
      <p className="psw-note">{`${checkLine(summary)}. ${summary.outcome}`}</p>
      {summary.note && <p className="psw-note">{`Note: ${summary.note}`}</p>}
      {error && <p className="psw-error" role="alert">{error}</p>}
      {differing.length > 0 && (
        <div className="psw-table-scroll">
          <table className="psw-table">
            <thead>
              <tr>
                <th scope="col">Drug</th>
                <th scope="col">Batch</th>
                <th scope="col" className="psw-num">On the books</th>
                <th scope="col" className="psw-num">Counted</th>
                <th scope="col">Difference</th>
              </tr>
            </thead>
            <tbody>
              {differing.map(l => (
                <tr key={l.id}>
                  <td>{l.drug_name}{l.found_extra && <span className="psw-sub">Not on the books</span>}</td>
                  <td>{l.batch_no ?? 'Not recorded'}</td>
                  <td className="psw-num">{l.system_units}</td>
                  <td className="psw-num">{l.counted_units ?? 'Not counted'}</td>
                  <td>{l.short_units > 0 ? `${units(l.short_units)} short` : `${units(l.over_units)} over`}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {mine && (
        <p className="psw-note">You counted this cart, so someone else in the pharmacy confirms it.</p>
      )}
      {mayConfirm && !mine && (
        <>
          <p className="psw-hint">
            Confirming takes the units found short off the cart&apos;s books. Units found
            over are never added: take them out of the cart.
          </p>
          <div className="psw-field psw-field--wide">
            <label htmlFor={`psw-cart-reason-${summary.id}`}>Reason (needed to refuse)</label>
            <input id={`psw-cart-reason-${summary.id}`} value={reason}
                   onChange={e => setReason(e.target.value)} />
          </div>
          <div className="psw-buttons">
            <button type="button" className="psw-btn psw-btn--primary" disabled={busy}
                    onClick={() => void decide(true)}>
              Confirm the check
            </button>
            <button type="button" className="psw-btn" disabled={busy || !reason.trim()}
                    onClick={() => void decide(false)}>
              Refuse and count again
            </button>
          </div>
        </>
      )}
    </div>
  )
}

// ── Recording a check ─────────────────────────────────────────────────────────

function CheckForm({ cart, fail, onCancel, onDone }: {
  cart: CrashCart
  fail: Fail
  onCancel: () => void
  onDone: (done: string) => void
}) {
  const sid = cart.store.id
  const [kind, setKind] = useState<CartCheckKind>('seal')
  const [seal, setSeal] = useState<SealState>('intact')
  const [found, setFound] = useState('')
  const [applied, setApplied] = useState('')
  const [note, setNote] = useState('')
  const [counts, setCounts] = useState<Record<string, string>>({})
  const [key] = useState(() => newCartCheckKey())
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const lots = cart.drugs.flatMap(d => d.lots.map(l => ({ ...l, drug_name: d.drug_name })))
  const allCounted = lots.every(l => isWhole(counts[l.lot_id] ?? ''))
  const differs = lots.some(l => isWhole(counts[l.lot_id] ?? '')
    && Number(counts[l.lot_id]) !== l.units)
  const sealOk = seal === 'none' || found.trim() !== ''
  const ready = sealOk && (kind === 'seal'
    || (allCounted && applied.trim() !== '' && (!differs || note.trim() !== '')))

  const record = async () => {
    setBusy(true)
    setError('')
    try {
      const res = await recordCartCheck(cart.store.code, {
        kind,
        seal_found: seal === 'none' ? null : found.trim(),
        seal_intact: seal === 'intact',
        ...(kind === 'full' ? {
          seal_applied: applied.trim(),
          lines: lots.map(l => ({ lot_id: l.lot_id, counted_units: Number(counts[l.lot_id]) })),
        } : {}),
        note: note.trim() || null,
        idempotency_key: key,
      })
      onDone(`${res.check.kind_label} recorded. ${res.check.outcome}`)
    } catch (err) {
      setError(fail(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="psw-form" aria-label={`Record a check of ${cart.store.name}`}>
      <div className="psw-toolbar" role="radiogroup" aria-label="Kind of check">
        {(['seal', 'full'] as const).map(k => (
          <label key={k} className="psw-check">
            <input type="radio" name={`psw-cart-kind-${sid}`} checked={kind === k}
                   onChange={() => setKind(k)} />
            {k === 'seal' ? 'Seal check' : 'Full check (count and seal again)'}
          </label>
        ))}
      </div>
      <div className="psw-toolbar" role="radiogroup" aria-label="The seal as found">
        {([['intact', 'Seal intact'], ['broken', 'Seal broken'], ['none', 'No seal']] as const)
          .map(([value, label]) => (
            <label key={value} className="psw-check">
              <input type="radio" name={`psw-cart-seal-${sid}`} checked={seal === value}
                     onChange={() => setSeal(value)} />
              {label}
            </label>
          ))}
      </div>
      <div className="psw-fields">
        {seal !== 'none' && (
          <div className="psw-field">
            <label htmlFor={`psw-cart-found-${sid}`}>Seal number found</label>
            <input id={`psw-cart-found-${sid}`} value={found} maxLength={40}
                   onChange={e => setFound(e.target.value)} />
          </div>
        )}
        {kind === 'full' && (
          <div className="psw-field">
            <label htmlFor={`psw-cart-applied-${sid}`}>New seal number</label>
            <input id={`psw-cart-applied-${sid}`} value={applied} maxLength={40}
                   onChange={e => setApplied(e.target.value)} />
          </div>
        )}
      </div>
      {kind === 'full' && (lots.length === 0 ? (
        <p className="psw-note">The books show nothing in this cart. Record the new seal.</p>
      ) : (
        <div className="psw-table-scroll">
          <table className="psw-table">
            <thead>
              <tr>
                <th scope="col">Drug</th>
                <th scope="col">Batch</th>
                <th scope="col">Expiry</th>
                <th scope="col" className="psw-num">On the books</th>
                <th scope="col">Counted</th>
              </tr>
            </thead>
            <tbody>
              {lots.map(l => (
                <tr key={l.lot_id}>
                  <td>
                    {l.drug_name}
                    {l.status_label !== 'available' && <span className="psw-sub">{l.status_label}</span>}
                  </td>
                  <td>{l.batch_no ?? 'Not recorded'}</td>
                  <td>{l.expiry_date ? formatClinicalDateKey(l.expiry_date) : 'Not recorded'}</td>
                  <td className="psw-num">{l.units}</td>
                  <td>
                    <label htmlFor={`psw-cart-count-${l.lot_id}`} className="psw-visually-hidden">
                      {`Units of ${l.drug_name} batch ${l.batch_no ?? 'not recorded'} counted`}
                    </label>
                    <input id={`psw-cart-count-${l.lot_id}`} inputMode="numeric"
                           value={counts[l.lot_id] ?? ''}
                           onChange={e => setCounts(c => ({ ...c, [l.lot_id]: e.target.value }))} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
      <div className="psw-field psw-field--wide">
        <label htmlFor={`psw-cart-note-${sid}`}>
          {kind === 'full' && differs ? 'What happened (needed when the count differs)' : 'Note'}
        </label>
        <input id={`psw-cart-note-${sid}`} value={note} onChange={e => setNote(e.target.value)} />
      </div>
      {kind === 'full' && differs && (
        <p className="psw-hint">
          The count differs from the books. The check waits for someone in the pharmacy to
          confirm it; the top-up can be asked for straight away.
        </p>
      )}
      {error && <p className="psw-error" role="alert">{error}</p>}
      <div className="psw-buttons">
        <button type="button" className="psw-btn psw-btn--primary" disabled={busy || !ready}
                onClick={() => void record()}>
          Record the check
        </button>
        <button type="button" className="psw-btn psw-btn--quiet" disabled={busy} onClick={onCancel}>
          Cancel
        </button>
      </div>
    </div>
  )
}

// ═════════════════════════════════════════════════════════════════════════════
//  The check record
// ═════════════════════════════════════════════════════════════════════════════

const RECORD_FILTERS: { value: CartCheckStatus | ''; label: string }[] = [
  { value: '', label: 'All checks' },
  { value: 'submitted', label: 'Waiting for the pharmacy' },
  { value: 'rejected', label: 'Count again' },
]

function RecordSection({ fail }: { fail: Fail }) {
  const [filter, setFilter] = useState<CartCheckStatus | ''>('')
  const [rows, setRows] = useState<CartCheck[] | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let alive = true
    listCartChecks({ status: filter, limit: 100 }).then(
      res => { if (alive) { setError(''); setRows(res.checks) } },
      err => { if (alive) setError(fail(err)) },
    )
    return () => { alive = false }
  }, [fail, filter])

  return (
    <section className="psw-card" aria-labelledby="psw-cart-record-title">
      <div className="psw-card__head">
        <h2 id="psw-cart-record-title" className="psw-card__title">Check record</h2>
        <div className="psw-toolbar">
          <label htmlFor="psw-cart-record-filter" className="psw-visually-hidden">Show checks</label>
          <select id="psw-cart-record-filter" value={filter}
                  onChange={e => { setRows(null); setFilter(e.target.value as CartCheckStatus | '') }}>
            {RECORD_FILTERS.map(f => <option key={f.value} value={f.value}>{f.label}</option>)}
          </select>
        </div>
      </div>
      {error && <p className="psw-error" role="alert">{error}</p>}
      {rows === null && !error && <p className="psw-note" role="status">Loading checks</p>}
      {rows && rows.length === 0 && <p className="psw-note">No checks here.</p>}
      {rows && rows.length > 0 && (
        <div className="psw-table-scroll">
          <table className="psw-table">
            <thead>
              <tr>
                <th scope="col">Check</th>
                <th scope="col">Cart</th>
                <th scope="col">Finding</th>
                <th scope="col">Status</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(c => (
                <tr key={c.id}>
                  <td>
                    <span className="psw-strong">{c.kind_label}</span>
                    <span className="psw-sub">{checkLine(c).replace(`${c.kind_label} by `, '')}</span>
                  </td>
                  <td>
                    {c.store_name ?? c.store_code}
                    {c.department && <span className="psw-sub">{c.department}</span>}
                  </td>
                  <td>
                    {c.outcome}
                    {c.note && <span className="psw-sub">{`Note: ${c.note}`}</span>}
                  </td>
                  <td>
                    <span className={`psw-chip ${statusTone(c)}`}>{c.status_label}</span>
                    {c.decided_by_name && (
                      <span className="psw-sub">
                        {`${c.decided_by_name}${c.decided_at ? `, ${formatClinicalDateTime(c.decided_at)}` : ''}`}
                      </span>
                    )}
                    {c.decision_reason && <span className="psw-sub">{c.decision_reason}</span>}
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
//  Which carts a batch went to
// ═════════════════════════════════════════════════════════════════════════════

function BatchSection({ fail }: { fail: Fail }) {
  const [drugId, setDrugId] = useState('')
  const [batch, setBatch] = useState('')
  const [trace, setTrace] = useState<CartBatchTrace | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const look = async () => {
    setBusy(true)
    setError('')
    setTrace(null)
    try {
      setTrace(await getCartBatchTrace(drugId.trim(), batch.trim()))
    } catch (err) {
      setError(fail(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="psw-card" aria-labelledby="psw-cart-batch-title">
      <div className="psw-card__head">
        <h2 id="psw-cart-batch-title" className="psw-card__title">Batch in carts</h2>
      </div>
      <p className="psw-hint">
        For a recall: every crash cart a batch was sent to, what is still in the cart, and
        what left it. Who received a dose from a cart is not recorded here.
      </p>
      <div className="psw-fields">
        <div className="psw-field">
          <label htmlFor="psw-cart-batch-drug">Drug code</label>
          <input id="psw-cart-batch-drug" value={drugId} onChange={e => setDrugId(e.target.value)} />
        </div>
        <div className="psw-field">
          <label htmlFor="psw-cart-batch-no">Batch</label>
          <input id="psw-cart-batch-no" value={batch} onChange={e => setBatch(e.target.value)} />
        </div>
        <div className="psw-buttons">
          <button type="button" className="psw-btn psw-btn--primary"
                  disabled={busy || !drugId.trim() || !batch.trim()} onClick={() => void look()}>
            Find the carts
          </button>
        </div>
      </div>
      {error && <p className="psw-error" role="alert">{error}</p>}
      {trace && trace.carts.length === 0 && (
        <p className="psw-note">{`No crash cart was sent batch ${trace.batch_no} of ${trace.drug_name}.`}</p>
      )}
      {trace && trace.carts.length > 0 && (
        <div className="psw-table-scroll">
          <table className="psw-table" aria-label={`Crash carts sent batch ${trace.batch_no}`}>
            <thead>
              <tr>
                <th scope="col">Cart</th>
                <th scope="col" className="psw-num">Sent</th>
                <th scope="col" className="psw-num">On its way</th>
                <th scope="col" className="psw-num">Still in the cart</th>
                <th scope="col" className="psw-num">Left the cart</th>
              </tr>
            </thead>
            <tbody>
              {trace.carts.map(c => (
                <tr key={c.store_id}>
                  <td>
                    {c.store_name}
                    <span className="psw-sub">{c.label}</span>
                  </td>
                  <td className="psw-num">{c.sent_units}</td>
                  <td className="psw-num">{c.in_transit_units}</td>
                  <td className="psw-num">{c.in_cart_units}</td>
                  <td className="psw-num">{c.unattributed_units}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
