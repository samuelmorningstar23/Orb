/**
 * Orb — Procurement & Stores.
 *
 * ╔════════════════════════════════════════════════════════════════════════╗
 * ║ MEDICATIONS ARE NOT HERE. Drug stock lives in Helix (Pharmacy).        ║
 * ╚════════════════════════════════════════════════════════════════════════╝
 *
 * Orb has two stock systems on purpose, and this screen says which one it is
 * before it says anything else. Helix holds drugs: batch, expiry and
 * dispensing against a prescription. Where the Pharmacy store module is
 * switched on, drugs are bought, received and classified (schedule and NDPS
 * class) there, and every unit still lands in Helix's ledger. This module
 * holds everything else the hospital buys: consumables, implants, reagents,
 * linen, PPE, instruments, housekeeping, stationery.
 *
 * The reason the sentence is on the screen and not only in a design document:
 * a storekeeper who cannot see where the boundary is will eventually create a
 * second row for a drug they can already see in Helix, and from that moment two
 * ledgers disagree about how much of something exists. The server refuses an
 * item that matches the Helix catalogue, but a refusal a person did not expect
 * reads as a bug. So the expectation is set first, in the header, on the first
 * paint, before any network round trip and still there if every fetch fails.
 *
 * WHAT THIS SCREEN IS ORGANISED AROUND. Not "here are your tables". A stores
 * manager opens the day on three questions — what has run out, what is about to
 * go off, and what is on its way — so those are the overview, in that order.
 * Everything else is a drill-down.
 *
 * MONEY. Integer paise end to end. The panel renders the server's own
 * `*_display` strings wherever the server computed the number, so the screen
 * can never disagree with the record it just wrote; anything derived here goes
 * through src/lib/money.ts. An item nobody has priced reads "Not configured" —
 * never "₹0.00", which is a price, and a price somebody will act on.
 *
 * QUANTITIES. Integer milli (thousandths of a unit) on the wire, converted at
 * the input boundary by `unitsToMilli`. There is no float multiply anywhere in
 * this file.
 */
import { type ReactNode, useCallback, useEffect, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  approvePurchaseOrder, cancelIndent, createGoodsReceipt, createIndent,
  createPurchaseOrder, decideIndent, formatQuantity, getConsumption,
  getIndent, getIndents, getItems, getMeta, getOverview, getPurchaseOrder,
  getPurchaseOrders, getStock, getStockLedger, getStores, getVendors,
  issuePurchaseOrder, ProcurementApiError, recordStockMovement, saveItem,
  saveVendor, submitIndent, submitPurchaseOrder, unitsToMilli,
  type ConsumptionReport, type Indent, type InventoryItem, type ProcurementMeta,
  type ProcurementOverview, type PurchaseOrder, type StockMovement,
  type StockRow, type Store, type Vendor,
} from '../../api/procurement'
import { formatInr, formatInrOrUnset, MoneyError, toPaise } from '../../lib/money'
import { formatClinicalDateTime } from '../../lib/datetime'
import { humaniseEnum, plural } from '../../lib/humanise'
import './ProcurementPanel.css'

/* ── The boundary sentence, duplicated on purpose ────────────────────────────
 * The server sends this same statement on /overview and /meta, and the panel
 * prefers the server's copy once it has one. This literal exists so the words
 * are on screen in the FIRST PAINT and still there if every request fails. The
 * one message that must never be missing cannot be one that depends on the
 * network being up. */
const BOUNDARY_NOTE =
  'Medications are not stocked here. Drug stock lives in Helix (Pharmacy), '
  + 'which tracks batch, expiry and dispensing against a prescription. '
  + 'This module covers non-pharmacy stores: consumables, implants, reagents, '
  + 'linen, PPE, instruments, housekeeping and stationery.'

/** Orb has no vendor connection. Every "issue", "order" and "receive" here is a
 *  RECORD of something a human did elsewhere — the same boundary PayerPanel and
 *  AbdmPanel state, stated the same way, because the consequence of assuming
 *  otherwise is the same: nobody actually sends the thing. */
const NO_TRANSMISSION_SHORT =
  'Orb does not send anything to a vendor. Issuing an order here records that '
  + 'your team sent it, by e-mail, on the vendor portal or on paper.'

type View = 'overview' | 'indents' | 'orders' | 'stock' | 'consumption' | 'masters'

const VIEWS: { id: View; label: string }[] = [
  { id: 'overview', label: 'Overview' },
  { id: 'indents', label: 'Indents' },
  { id: 'orders', label: 'Purchase orders' },
  { id: 'stock', label: 'Stock' },
  { id: 'consumption', label: 'Consumption' },
  { id: 'masters', label: 'Items & vendors' },
]

const STATUS_TONE: Record<string, string> = {
  draft: 'neutral', submitted: 'info', pending_approval: 'info',
  approved: 'ok', partially_approved: 'warn', rejected: 'danger',
  converted: 'ok', cancelled: 'neutral', closed: 'neutral',
  issued: 'info', partially_received: 'warn', received: 'ok', posted: 'ok',
  open: 'info', returned: 'warn',
}

const STATUS_LABEL: Record<string, string> = {
  draft: 'Draft', submitted: 'Awaiting approval', pending_approval: 'Awaiting approval',
  approved: 'Approved', partially_approved: 'Approved in part', rejected: 'Rejected',
  converted: 'Ordered', cancelled: 'Cancelled', closed: 'Closed',
  issued: 'With vendor', partially_received: 'Part received', received: 'Received',
  posted: 'Posted', open: 'Open', short_closed: 'Short closed', returned: 'Returned',
}

function label(status: string): string {
  return STATUS_LABEL[status] ?? humaniseEnum(status)
}

function tone(status: string): string {
  return STATUS_TONE[status] ?? 'neutral'
}

/* ── Small presentational pieces ─────────────────────────────────────────── */

function Badge({ status }: { status: string }) {
  return <span className={`proc-badge proc-badge--${tone(status)}`}>{label(status)}</span>
}

function Tile({ value, caption, tone: t }: { value: string; caption: string; tone?: string }) {
  return (
    <div className="proc-tile">
      <div className={`proc-tile__value${t ? ` proc-tile__value--${t}` : ''}`}>{value}</div>
      <div className="proc-tile__caption">{caption}</div>
    </div>
  )
}

function Empty({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="proc-empty">
      <div className="proc-empty__title">{title}</div>
      <div className="proc-empty__detail">{detail}</div>
    </div>
  )
}

/** A number nobody entered is not zero. Used for every rate on this screen. */
function Money({ paise, unset }: { paise: number | null | undefined; unset?: string }) {
  const missing = paise === null || paise === undefined
  return (
    <span className={missing ? 'proc-money proc-money--unset' : 'proc-money'}>
      {formatInrOrUnset(paise ?? null, unset ?? 'Not configured')}
    </span>
  )
}

/* ── The panel ───────────────────────────────────────────────────────────── */

export default function ProcurementPanel() {
  const [view, setView] = useState<View>('overview')
  const [meta, setMeta] = useState<ProcurementMeta | null>(null)
  const [stores, setStores] = useState<Store[]>([])
  const [storeCode, setStoreCode] = useState<string>('')
  const [moduleOff, setModuleOff] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [flash, setFlash] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [reloadToken, setReloadToken] = useState(0)

  const reload = useCallback(() => setReloadToken((n) => n + 1), [])

  /** One place where every failure lands, so a disabled module can never be
   *  mistaken for an empty store. */
  const handle = useCallback((err: unknown) => {
    if (err instanceof ProcurementApiError && err.isModuleDisabled) {
      setModuleOff(err.message)
      return
    }
    setError(err instanceof Error ? err.message : String(err))
  }, [])

  /** `onFail` lets a form claim its own error (returning true) so the sentence
   *  is shown where the person is looking, not on the panel behind a drawer. */
  const run = useCallback(async (
    fn: () => Promise<unknown>, note?: string, onFail?: (err: unknown) => boolean,
  ) => {
    setBusy(true)
    setError(null)
    try {
      await fn()
      if (note) setFlash(note)
      reload()
    } catch (err) {
      if (!onFail?.(err)) handle(err)
    } finally {
      setBusy(false)
    }
  }, [handle, reload])

  useEffect(() => {
    let alive = true
    Promise.all([getMeta(), getStores()])
      .then(([m, s]) => {
        if (!alive) return
        setMeta(m)
        setStores(s)
      })
      .catch(handle)
    return () => { alive = false }
  }, [handle])

  const boundary = meta?.pharmacy_boundary?.message || BOUNDARY_NOTE

  if (moduleOff) {
    return (
      <div className="proc-panel">
        <header className="proc-head">
          <div>
            <h1 className="proc-title">Procurement &amp; Stores</h1>
            <p className="proc-sub">Not available at this site</p>
          </div>
        </header>
        <div className="proc-notice proc-notice--off">{moduleOff}</div>
      </div>
    )
  }

  return (
    <div className="proc-panel">
      <header className="proc-head">
        <div>
          <h1 className="proc-title">Procurement &amp; Stores</h1>
          <p className="proc-sub">
            Vendors, non-pharmacy items, indents, purchase orders, goods receipt and stock
          </p>
        </div>
        <div className="proc-head__controls">
          <label className="proc-field proc-field--inline">
            <span className="proc-field__label">Store</span>
            <select
              className="proc-input"
              value={storeCode}
              onChange={(e) => setStoreCode(e.target.value)}
            >
              <option value="">All stores</option>
              {stores.map((s) => (
                <option key={s.code} value={s.code}>{s.name}</option>
              ))}
            </select>
          </label>
          <button type="button" className="proc-btn" onClick={reload} disabled={busy}>
            Refresh
          </button>
        </div>
      </header>

      {/* Rendered before any fetch resolves and never dismissible. */}
      <div className="proc-notice">{boundary}</div>

      <nav className="proc-tabs" aria-label="Procurement views">
        {VIEWS.map((v) => (
          <button
            key={v.id}
            type="button"
            className={`proc-tab${view === v.id ? ' proc-tab--on' : ''}`}
            aria-current={view === v.id ? 'page' : undefined}
            onClick={() => { setView(v.id); setError(null); setFlash(null) }}
          >
            {v.label}
          </button>
        ))}
      </nav>

      {error && (
        <div className="proc-alert proc-alert--danger" role="alert">
          {error}
        </div>
      )}
      {flash && (
        <div className="proc-alert proc-alert--ok" role="status">
          {flash}
        </div>
      )}

      {view === 'overview' && (
        <OverviewView storeCode={storeCode} token={reloadToken} onError={handle} />
      )}
      {view === 'indents' && (
        <IndentsView
          storeCode={storeCode} stores={stores} token={reloadToken}
          onError={handle} run={run} busy={busy}
        />
      )}
      {view === 'orders' && (
        <OrdersView
          storeCode={storeCode} token={reloadToken}
          onError={handle} run={run} busy={busy}
        />
      )}
      {view === 'stock' && (
        <StockView storeCode={storeCode} token={reloadToken} onError={handle}
                   run={run} busy={busy} />
      )}
      {view === 'consumption' && (
        <ConsumptionView storeCode={storeCode} token={reloadToken} onError={handle} />
      )}
      {view === 'masters' && (
        <MastersView meta={meta} token={reloadToken} onError={handle} run={run} busy={busy} />
      )}
    </div>
  )
}

/* ── Overview ────────────────────────────────────────────────────────────── */

function OverviewView(
  { storeCode, token, onError }:
  { storeCode: string; token: number; onError: (e: unknown) => void },
) {
  const [data, setData] = useState<ProcurementOverview | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let alive = true
    // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
    setLoading(true)
    getOverview(storeCode || undefined)
      .then((d) => { if (alive) setData(d) })
      .catch(onError)
      .finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [storeCode, token, onError])

  if (loading && !data) return <div className="proc-loading">Loading stores…</div>
  if (!data) return null

  return (
    <div className="proc-stack">
      <div className="proc-tiles">
        <Tile
          value={String(data.low_stock_count)} caption="At or below reorder level"
          tone={data.low_stock_count > 0 ? 'warn' : undefined}
        />
        <Tile
          value={String(data.expiring_count)}
          caption={data.expired_count > 0
            ? `Expiring in 90 days (${data.expired_count} already expired)`
            : 'Expiring in 90 days'}
          tone={data.expired_count > 0 ? 'danger' : (data.expiring_count > 0 ? 'warn' : undefined)}
        />
        <Tile
          value={String(data.open_purchase_order_count)} caption="Orders with vendors"
        />
        <Tile
          value={data.open_purchase_order_value_display}
          caption="Committed to vendors"
        />
        <Tile value={String(data.pending_indent_count)} caption="Indents awaiting approval" />
        <Tile value={String(data.active_item_count)} caption="Active items" />
      </div>

      <section className="proc-section">
        <h2 className="proc-section__title">Needs ordering</h2>
        {data.low_stock.length === 0 ? (
          <Empty
            title="Nothing is below its reorder level"
            detail="Items only appear here once a reorder level has been set on them. An item with no level set is not 'fine'. It is unconfigured."
          />
        ) : (
          <div className="proc-table-wrap">
            <table className="proc-table">
              <thead>
                <tr>
                  <th scope="col">Item</th>
                  <th scope="col">Store</th>
                  <th scope="col" className="proc-num">On hand</th>
                  <th scope="col" className="proc-num">Reorder at</th>
                  <th scope="col" className="proc-num">Short by</th>
                  <th scope="col" className="proc-num">Suggested order</th>
                  <th scope="col" className="proc-num">Last rate</th>
                </tr>
              </thead>
              <tbody>
                {data.low_stock.map((row) => (
                  <tr key={`${row.item_code}-${row.store_code ?? 'none'}`}>
                    <td>
                      <div className="proc-cell__primary">{row.item_name}</div>
                      <div className="proc-cell__secondary">{row.item_code}</div>
                    </td>
                    <td>{row.store_name ?? 'Never stocked'}</td>
                    <td className="proc-num proc-num--warn">{row.on_hand_display}</td>
                    <td className="proc-num">{row.reorder_level_display}</td>
                    <td className="proc-num">{row.shortfall_display}</td>
                    <td className="proc-num">{row.suggested_order_display}</td>
                    <td className="proc-num"><Money paise={row.last_purchase_rate_paise} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="proc-section">
        <h2 className="proc-section__title">Expiring soon</h2>
        {data.expiring.length === 0 ? (
          <Empty
            title="No batch is within 90 days of expiry"
            detail="Only items that carry a batch and an expiry date appear here. Linen and instruments do not expire and are deliberately absent."
          />
        ) : (
          <ul className="proc-list">
            {data.expiring.map((row) => (
              <li
                key={`${row.item_code}-${row.batch_no}-${row.expiry_date}`}
                className={`proc-row${row.is_expired ? ' proc-row--danger' : ' proc-row--warn'}`}
              >
                <div className="proc-row__body">
                  <div className="proc-row__primary">{row.item_name ?? row.item_code}</div>
                  <div className="proc-row__secondary">
                    Batch {row.batch_no} · {row.store_name ?? 'Unknown store'} · {row.balance_display} remaining
                  </div>
                </div>
                <div className="proc-row__trailing">
                  <div className={row.is_expired ? 'proc-row__value proc-row__value--danger'
                                                 : 'proc-row__value proc-row__value--warn'}>
                    {row.is_expired
                      ? `Expired ${Math.abs(row.days_to_expiry)}d ago`
                      : `${row.days_to_expiry}d left`}
                  </div>
                  <div className="proc-row__note">{row.value_at_risk_display}</div>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="proc-section">
        <h2 className="proc-section__title">On its way</h2>
        {data.open_purchase_orders.length === 0 ? (
          <Empty title="No orders are with a vendor"
                 detail="Orders appear here once they have been approved and issued." />
        ) : (
          <ul className="proc-list">
            {data.open_purchase_orders.map((po) => (
              <li key={po.id} className="proc-row">
                <div className="proc-row__body">
                  <div className="proc-row__primary">
                    {po.po_number ?? `Order #${po.id}`} · {po.vendor_name}
                  </div>
                  <div className="proc-row__secondary">
                    Expected {po.expected_date ?? 'date not set'}
                  </div>
                </div>
                <div className="proc-row__trailing">
                  <Badge status={po.status} />
                  <div className="proc-row__note">{formatInr(po.total_paise)}</div>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}

/* ── Indents ─────────────────────────────────────────────────────────────── */

function IndentsView(
  { storeCode, stores, token, onError, run, busy }: {
    storeCode: string; stores: Store[]; token: number
    onError: (e: unknown) => void
    run: (fn: () => Promise<unknown>, note?: string) => Promise<void>
    busy: boolean
  },
) {
  const [indents, setIndents] = useState<Indent[]>([])
  const [items, setItems] = useState<InventoryItem[]>([])
  const [open, setOpen] = useState<Indent | null>(null)
  const [showNew, setShowNew] = useState(false)

  useEffect(() => {
    let alive = true
    Promise.all([getIndents({ storeCode: storeCode || undefined }), getItems()])
      .then(([i, it]) => { if (alive) { setIndents(i); setItems(it) } })
      .catch(onError)
    return () => { alive = false }
  }, [storeCode, token, onError])

  return (
    <div className="proc-stack">
      <div className="proc-section__header">
        <h2 className="proc-section__title">Indents</h2>
        <button type="button" className="proc-btn proc-btn--primary"
                onClick={() => setShowNew(true)}>
          Raise an indent
        </button>
      </div>

      {indents.length === 0 ? (
        <Empty title="No indents yet"
               detail="A department raises an indent to ask the store for items. It is approved (in whole or in part) before a purchase order can be raised against it." />
      ) : (
        <ul className="proc-list">
          {indents.map((ind) => (
            <li key={ind.id}>
              <button
                type="button"
                className="proc-row proc-row--link"
                onClick={() => getIndent(ind.id).then(setOpen).catch(onError)}
              >
                <div className="proc-row__body">
                  <div className="proc-row__primary">
                    {ind.indent_number ?? `Draft #${ind.id}`}
                    {ind.department ? ` · ${ind.department}` : ''}
                  </div>
                  <div className="proc-row__secondary">
                    {ind.store_name ?? 'No store'} · {ind.priority}
                    {ind.required_by ? ` · needed by ${ind.required_by}` : ''}
                  </div>
                </div>
                <div className="proc-row__trailing">
                  <Badge status={ind.status} />
                </div>
              </button>
            </li>
          ))}
        </ul>
      )}

      {showNew && (
        <NewIndentDrawer
          stores={stores} items={items} defaultStore={storeCode}
          onClose={() => setShowNew(false)}
          onSubmit={(payload) => run(
            () => createIndent(payload).then(() => setShowNew(false)),
            'Indent raised.',
          )}
          busy={busy}
        />
      )}

      {open && (
        <IndentDrawer
          indent={open} busy={busy}
          onClose={() => setOpen(null)}
          onSubmitIndent={() => run(() => submitIndent(open.id).then(() => setOpen(null)),
                                    'Indent submitted for approval.')}
          onApprove={(approvals) => run(
            () => decideIndent(open.id, { approvals }).then(() => setOpen(null)),
            'Indent decided.',
          )}
          onReject={(reason) => run(
            () => decideIndent(open.id, { reject: true, reason }).then(() => setOpen(null)),
            'Indent rejected.',
          )}
          onCancel={(reason) => run(
            () => cancelIndent(open.id, reason).then(() => setOpen(null)),
            'Indent cancelled.',
          )}
        />
      )}
    </div>
  )
}

function Drawer({ title, subtitle, onClose, children }: {
  title: string; subtitle?: string; onClose: () => void; children: ReactNode
}) {
  // Portalled to document.body so no ancestor's overflow/transform can clip it.
  return createPortal(
    <div className="proc-drawer" role="dialog" aria-modal="true" aria-label={title}>
      <button type="button" className="proc-drawer__scrim" aria-label="Close" onClick={onClose} />
      <div className="proc-drawer__panel">
        <header className="proc-drawer__head">
          <div>
            <h2 className="proc-drawer__title">{title}</h2>
            {subtitle && <p className="proc-drawer__sub">{subtitle}</p>}
          </div>
          <button type="button" className="proc-btn" onClick={onClose}>Close</button>
        </header>
        <div className="proc-drawer__body">{children}</div>
      </div>
    </div>,
    document.body,
  )
}

function NewIndentDrawer({ stores, items, defaultStore, onClose, onSubmit, busy }: {
  stores: Store[]; items: InventoryItem[]; defaultStore: string
  onClose: () => void
  onSubmit: (payload: Parameters<typeof createIndent>[0]) => void
  busy: boolean
}) {
  const [store, setStore] = useState(defaultStore || stores[0]?.code || '')
  const [department, setDepartment] = useState('')
  const [priority, setPriority] = useState('routine')
  const [rows, setRows] = useState<{ code: string; units: string }[]>([{ code: '', units: '' }])
  const [problem, setProblem] = useState<string | null>(null)

  const submit = (submitNow: boolean) => {
    setProblem(null)
    const lines: Array<{ item_code: string; quantity_milli: number }> = []
    for (const row of rows) {
      if (!row.code && !row.units) continue
      if (!row.code) { setProblem('Every line needs an item.'); return }
      const units = Number(row.units)
      if (!row.units || !Number.isInteger(units) || units <= 0) {
        setProblem(`Quantity for ${row.code} must be a whole number greater than zero.`)
        return
      }
      lines.push({ item_code: row.code, quantity_milli: unitsToMilli(units) })
    }
    if (lines.length === 0) { setProblem('An indent needs at least one line.'); return }
    onSubmit({
      store_code: store || undefined,
      department: department || undefined,
      priority,
      submit: submitNow,
      lines,
    })
  }

  return (
    <Drawer title="Raise an indent"
            subtitle="Non-pharmacy items only: medications are requested through Pharmacy."
            onClose={onClose}>
      {problem && <div className="proc-alert proc-alert--danger" role="alert">{problem}</div>}
      <div className="proc-form-grid">
        <label className="proc-field">
          <span className="proc-field__label">Store</span>
          <select className="proc-input" value={store} onChange={(e) => setStore(e.target.value)}>
            <option value="">Not specified</option>
            {stores.map((s) => <option key={s.code} value={s.code}>{s.name}</option>)}
          </select>
        </label>
        <label className="proc-field">
          <span className="proc-field__label">Department</span>
          <input className="proc-input" value={department}
                 onChange={(e) => setDepartment(e.target.value)} placeholder="e.g. Theatre" />
        </label>
        <label className="proc-field">
          <span className="proc-field__label">Priority</span>
          <select className="proc-input" value={priority}
                  onChange={(e) => setPriority(e.target.value)}>
            <option value="routine">Routine</option>
            <option value="urgent">Urgent</option>
            <option value="emergency">Emergency</option>
          </select>
        </label>
      </div>

      <h3 className="proc-subhead">Lines</h3>
      {rows.map((row, index) => (
        <div className="proc-line-row" key={index}>
          <label className="proc-field">
            <span className="proc-field__label">Item</span>
            <select
              className="proc-input" value={row.code}
              onChange={(e) => setRows(rows.map((r, i) =>
                i === index ? { ...r, code: e.target.value } : r))}
            >
              <option value="">Choose an item…</option>
              {items.map((it) => (
                <option key={it.code} value={it.code}>{it.name} ({it.uom})</option>
              ))}
            </select>
          </label>
          <label className="proc-field proc-field--narrow">
            <span className="proc-field__label">Quantity</span>
            <input
              className="proc-input proc-input--num" inputMode="numeric" value={row.units}
              onChange={(e) => setRows(rows.map((r, i) =>
                i === index ? { ...r, units: e.target.value } : r))}
            />
          </label>
        </div>
      ))}
      <button type="button" className="proc-btn"
              onClick={() => setRows([...rows, { code: '', units: '' }])}>
        Add a line
      </button>

      <div className="proc-drawer__actions">
        <button type="button" className="proc-btn" disabled={busy}
                onClick={() => submit(false)}>
          Save as draft
        </button>
        <button type="button" className="proc-btn proc-btn--primary" disabled={busy}
                onClick={() => submit(true)}>
          Submit for approval
        </button>
      </div>
    </Drawer>
  )
}

function IndentDrawer({ indent, busy, onClose, onSubmitIndent, onApprove, onReject, onCancel }: {
  indent: Indent; busy: boolean
  onClose: () => void
  onSubmitIndent: () => void
  onApprove: (approvals: Record<number, number>) => void
  onReject: (reason: string) => void
  onCancel: (reason: string) => void
}) {
  const [approved, setApproved] = useState<Record<number, string>>(() =>
    Object.fromEntries(indent.lines.map((l) => [
      l.line_no, String(Math.trunc(l.quantity_milli / 1000)),
    ])))
  const [reason, setReason] = useState('')
  const [problem, setProblem] = useState<string | null>(null)

  const can = (target: string) => indent.legal_transitions.includes(target)

  const approve = () => {
    setProblem(null)
    const payload: Record<number, number> = {}
    for (const line of indent.lines) {
      const raw = approved[line.line_no]
      const units = Number(raw)
      if (raw === '' || !Number.isInteger(units) || units < 0) {
        setProblem(`Approved quantity on line ${line.line_no} must be a whole number.`)
        return
      }
      payload[line.line_no] = unitsToMilli(units)
    }
    onApprove(payload)
  }

  return (
    <Drawer
      title={indent.indent_number ?? `Draft indent #${indent.id}`}
      subtitle={`${label(indent.status)} · ${indent.store_name ?? 'No store'}`}
      onClose={onClose}
    >
      {problem && <div className="proc-alert proc-alert--danger" role="alert">{problem}</div>}
      {indent.rejected_reason && (
        <div className="proc-alert proc-alert--warn">{indent.rejected_reason}</div>
      )}

      <div className="proc-table-wrap">
        <table className="proc-table">
          <thead>
            <tr>
              <th scope="col">Item</th>
              <th scope="col" className="proc-num">Requested</th>
              <th scope="col" className="proc-num">Approved</th>
              <th scope="col">Status</th>
            </tr>
          </thead>
          <tbody>
            {indent.lines.map((line) => (
              <tr key={line.id}>
                <td>
                  <div className="proc-cell__primary">{line.description}</div>
                  <div className="proc-cell__secondary">{line.item_code}</div>
                </td>
                <td className="proc-num">{line.quantity_display}</td>
                <td className="proc-num">
                  {can('approved') || can('partially_approved') ? (
                    <input
                      className="proc-input proc-input--num proc-input--tiny"
                      inputMode="numeric"
                      aria-label={`Approved quantity for ${line.item_code}`}
                      value={approved[line.line_no] ?? ''}
                      onChange={(e) => setApproved({ ...approved, [line.line_no]: e.target.value })}
                    />
                  ) : (
                    line.approved_quantity_display
                  )}
                </td>
                <td><Badge status={line.status} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {(can('rejected') || can('cancelled')) && (
        <label className="proc-field">
          <span className="proc-field__label">Reason (required to reject or cancel)</span>
          <input className="proc-input" value={reason} onChange={(e) => setReason(e.target.value)} />
        </label>
      )}

      <div className="proc-drawer__actions">
        {can('submitted') && (
          <button type="button" className="proc-btn proc-btn--primary" disabled={busy}
                  onClick={onSubmitIndent}>
            Submit for approval
          </button>
        )}
        {(can('approved') || can('partially_approved')) && (
          <button type="button" className="proc-btn proc-btn--primary" disabled={busy}
                  onClick={approve}>
            Approve
          </button>
        )}
        {can('rejected') && (
          <button type="button" className="proc-btn proc-btn--danger"
                  disabled={busy || !reason.trim()}
                  onClick={() => onReject(reason.trim())}>
            Reject
          </button>
        )}
        {can('cancelled') && (
          <button type="button" className="proc-btn" disabled={busy || !reason.trim()}
                  onClick={() => onCancel(reason.trim())}>
            Cancel indent
          </button>
        )}
      </div>
      {indent.legal_transitions.length === 0 && (
        <p className="proc-hint">
          This indent is final. A rejected or closed indent is not reopened. Raise a new
          one, so the decision stays a fact somebody can point at.
        </p>
      )}
    </Drawer>
  )
}

/* ── Purchase orders ─────────────────────────────────────────────────────── */

function OrdersView(
  { storeCode, token, onError, run, busy }: {
    storeCode: string; token: number
    onError: (e: unknown) => void
    run: (fn: () => Promise<unknown>, note?: string) => Promise<void>
    busy: boolean
  },
) {
  const [orders, setOrders] = useState<PurchaseOrder[]>([])
  const [vendors, setVendors] = useState<Vendor[]>([])
  const [items, setItems] = useState<InventoryItem[]>([])
  const [open, setOpen] = useState<PurchaseOrder | null>(null)
  const [showNew, setShowNew] = useState(false)

  useEffect(() => {
    let alive = true
    Promise.all([getPurchaseOrders(), getVendors(), getItems()])
      .then(([o, v, it]) => { if (alive) { setOrders(o); setVendors(v); setItems(it) } })
      .catch(onError)
    return () => { alive = false }
  }, [token, onError])

  return (
    <div className="proc-stack">
      <div className="proc-section__header">
        <h2 className="proc-section__title">Purchase orders</h2>
        <button type="button" className="proc-btn proc-btn--primary"
                onClick={() => setShowNew(true)}>
          New purchase order
        </button>
      </div>

      {orders.length === 0 ? (
        <Empty title="No purchase orders"
               detail="An order is drafted, approved, then marked issued once your team has sent it to the vendor. Orb sends nothing itself. Goods can only be received against an issued order." />
      ) : (
        <ul className="proc-list">
          {orders.map((po) => (
            <li key={po.id}>
              <button
                type="button" className="proc-row proc-row--link"
                onClick={() => getPurchaseOrder(po.id).then(setOpen).catch(onError)}
              >
                <div className="proc-row__body">
                  <div className="proc-row__primary">
                    {po.po_number ?? `Draft #${po.id}`} · {po.vendor_name}
                  </div>
                  <div className="proc-row__secondary">
                    {po.order_date ?? 'No date'}
                    {po.is_interstate ? ' · inter-state (IGST)' : ' · intra-state (CGST+SGST)'}
                  </div>
                </div>
                <div className="proc-row__trailing">
                  <Badge status={po.status} />
                  <div className="proc-row__note">{formatInr(po.total_paise)}</div>
                </div>
              </button>
            </li>
          ))}
        </ul>
      )}

      {showNew && (
        <NewOrderDrawer
          vendors={vendors} items={items} storeCode={storeCode}
          onClose={() => setShowNew(false)} busy={busy}
          onSubmit={(payload) => run(
            () => createPurchaseOrder(payload).then(() => setShowNew(false)),
            'Purchase order drafted. It still needs approval and issue.',
          )}
        />
      )}

      {open && (
        <OrderDrawer
          po={open} busy={busy} onClose={() => setOpen(null)}
          run={run}
          onDone={() => setOpen(null)}
        />
      )}
    </div>
  )
}

function NewOrderDrawer({ vendors, items, storeCode, onClose, onSubmit, busy }: {
  vendors: Vendor[]; items: InventoryItem[]; storeCode: string
  onClose: () => void
  onSubmit: (payload: Parameters<typeof createPurchaseOrder>[0]) => void
  busy: boolean
}) {
  const [vendorCode, setVendorCode] = useState(vendors[0]?.code ?? '')
  const [interstate, setInterstate] = useState(false)
  const [rows, setRows] = useState([{ code: '', units: '', rate: '' }])
  const [problem, setProblem] = useState<string | null>(null)

  const vendor = vendors.find((v) => v.code === vendorCode)

  const submit = () => {
    setProblem(null)
    const lines: Array<{ item_code: string; quantity_milli: number; unit_rate_paise: number }> = []
    for (const row of rows) {
      if (!row.code && !row.units && !row.rate) continue
      if (!row.code) { setProblem('Every line needs an item.'); return }
      const units = Number(row.units)
      if (!row.units || !Number.isInteger(units) || units <= 0) {
        setProblem(`Quantity for ${row.code} must be a whole number greater than zero.`)
        return
      }
      let paise: number
      try {
        paise = toPaise(row.rate)
      } catch (err) {
        setProblem(err instanceof MoneyError
          ? `Rate for ${row.code}: ${err.message}`
          : `Rate for ${row.code} is not a rupee amount.`)
        return
      }
      if (paise < 0) { setProblem('A purchase rate cannot be negative.'); return }
      lines.push({ item_code: row.code, quantity_milli: unitsToMilli(units), unit_rate_paise: paise })
    }
    if (lines.length === 0) { setProblem('A purchase order needs at least one line.'); return }
    if (!vendorCode) { setProblem('Choose a vendor.'); return }
    onSubmit({
      vendor_code: vendorCode,
      store_code: storeCode || undefined,
      is_interstate: interstate,
      lines,
    })
  }

  return (
    <Drawer title="New purchase order" onClose={onClose}>
      {problem && <div className="proc-alert proc-alert--danger" role="alert">{problem}</div>}
      <div className="proc-form-grid">
        <label className="proc-field">
          <span className="proc-field__label">Vendor</span>
          <select className="proc-input" value={vendorCode}
                  onChange={(e) => setVendorCode(e.target.value)}>
            <option value="">Choose a vendor…</option>
            {vendors.map((v) => <option key={v.code} value={v.code}>{v.name}</option>)}
          </select>
        </label>
        <label className="proc-field proc-field--check">
          <input type="checkbox" checked={interstate}
                 onChange={(e) => setInterstate(e.target.checked)} />
          <span>
            Inter-state supply (IGST)
            <span className="proc-field__hint">
              {vendor?.state_code
                ? `Vendor state code ${vendor.state_code}. `
                : 'This vendor has no state code on file. '}
              Orb does not guess this: CGST+SGST and IGST are different tax heads on a
              real document, so the buyer states it.
            </span>
          </span>
        </label>
      </div>

      <h3 className="proc-subhead">Lines</h3>
      {rows.map((row, index) => (
        <div className="proc-line-row" key={index}>
          <label className="proc-field">
            <span className="proc-field__label">Item</span>
            <select className="proc-input" value={row.code}
                    onChange={(e) => setRows(rows.map((r, i) =>
                      i === index ? { ...r, code: e.target.value } : r))}>
              <option value="">Choose an item…</option>
              {items.map((it) => (
                <option key={it.code} value={it.code}>{it.name} ({it.uom})</option>
              ))}
            </select>
          </label>
          <label className="proc-field proc-field--narrow">
            <span className="proc-field__label">Quantity</span>
            <input className="proc-input proc-input--num" inputMode="numeric" value={row.units}
                   onChange={(e) => setRows(rows.map((r, i) =>
                     i === index ? { ...r, units: e.target.value } : r))} />
          </label>
          <label className="proc-field proc-field--narrow">
            <span className="proc-field__label">Rate (₹)</span>
            <input className="proc-input proc-input--num" inputMode="decimal" value={row.rate}
                   onChange={(e) => setRows(rows.map((r, i) =>
                     i === index ? { ...r, rate: e.target.value } : r))} />
          </label>
        </div>
      ))}
      <button type="button" className="proc-btn"
              onClick={() => setRows([...rows, { code: '', units: '', rate: '' }])}>
        Add a line
      </button>

      <div className="proc-drawer__actions">
        <button type="button" className="proc-btn proc-btn--primary" disabled={busy}
                onClick={submit}>
          Create draft order
        </button>
      </div>
    </Drawer>
  )
}

function OrderDrawer({ po, busy, onClose, run, onDone }: {
  po: PurchaseOrder; busy: boolean
  onClose: () => void
  run: (fn: () => Promise<unknown>, note?: string) => Promise<void>
  onDone: () => void
}) {
  const [receiving, setReceiving] = useState(false)
  const can = (target: string) => po.legal_transitions.includes(target)

  return (
    <Drawer
      title={po.po_number ?? `Draft order #${po.id}`}
      subtitle={`${label(po.status)} · ${po.vendor_name ?? 'Unknown vendor'}`}
      onClose={onClose}
    >
      <div className="proc-table-wrap">
        <table className="proc-table">
          <thead>
            <tr>
              <th scope="col">Item</th>
              <th scope="col" className="proc-num">Ordered</th>
              <th scope="col" className="proc-num">Received</th>
              <th scope="col" className="proc-num">Outstanding</th>
              <th scope="col" className="proc-num">Rate</th>
              <th scope="col" className="proc-num">Line total</th>
            </tr>
          </thead>
          <tbody>
            {po.lines.map((line) => (
              <tr key={line.id}>
                <td>
                  <div className="proc-cell__primary">{line.description}</div>
                  <div className="proc-cell__secondary">
                    {line.item_code}{line.hsn_sac ? ` · HSN ${line.hsn_sac}` : ''}
                  </div>
                </td>
                <td className="proc-num">{line.quantity_display}</td>
                <td className="proc-num">{line.received_quantity_display}</td>
                <td className={`proc-num${line.outstanding_quantity_milli > 0
                  ? ' proc-num--warn' : ''}`}>
                  {line.outstanding_quantity_display}
                </td>
                <td className="proc-num">{formatInr(line.unit_rate_paise)}</td>
                <td className="proc-num">{formatInr(line.total_paise)}</td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr>
              <th scope="row" colSpan={5}>Taxable value</th>
              <td className="proc-num">{formatInr(po.taxable_value_paise)}</td>
            </tr>
            <tr>
              <th scope="row" colSpan={5}>
                {po.is_interstate ? 'IGST' : 'CGST + SGST'}
              </th>
              <td className="proc-num">{formatInr(po.tax_paise)}</td>
            </tr>
            {po.rounding_adjustment_paise !== 0 && (
              <tr>
                <th scope="row" colSpan={5}>Round off</th>
                <td className="proc-num">{formatInr(po.rounding_adjustment_paise)}</td>
              </tr>
            )}
            <tr className="proc-table__total">
              <th scope="row" colSpan={5}>Order total</th>
              <td className="proc-num">{formatInr(po.total_paise)}</td>
            </tr>
          </tfoot>
        </table>
      </div>

      {/* THE SAME SENTENCE PAYER AND ABDM CARRY, for the same reason. This
          module has no vendor connection of any kind — issue_purchase_order()
          is a status change and nothing else (server/services/procurement_
          service.py). The button used to read "Issue to vendor" and the toast
          "Order issued to the vendor", which a purchaser can only read as "Orb
          sent it". The failure that follows is silent and is discovered at the
          next stockout. */}
      <p className="proc-note">{NO_TRANSMISSION_SHORT}</p>

      <div className="proc-drawer__actions">
        {can('pending_approval') && (
          <button type="button" className="proc-btn" disabled={busy}
                  onClick={() => run(() => submitPurchaseOrder(po.id).then(onDone),
                                     'Sent for approval.')}>
            Send for approval
          </button>
        )}
        {can('approved') && (
          <button type="button" className="proc-btn proc-btn--primary" disabled={busy}
                  onClick={() => run(() => approvePurchaseOrder(po.id).then(onDone),
                                     'Order approved.')}>
            Approve
          </button>
        )}
        {can('issued') && (
          <button type="button" className="proc-btn proc-btn--primary" disabled={busy}
                  onClick={() => run(() => issuePurchaseOrder(po.id).then(onDone),
                                     'Recorded as issued in Orb. Send the order to the vendor yourself.')}>
            Mark as issued to vendor
          </button>
        )}
        {po.can_receive && (
          <button type="button" className="proc-btn proc-btn--primary" disabled={busy}
                  onClick={() => setReceiving(true)}>
            Receive goods
          </button>
        )}
      </div>

      {receiving && (
        <ReceiveForm
          po={po} busy={busy}
          onCancel={() => setReceiving(false)}
          onSubmit={(payload) => run(
            () => createGoodsReceipt(payload).then(onDone),
            'Goods received and stock updated.',
          )}
        />
      )}
    </Drawer>
  )
}

function ReceiveForm({ po, busy, onCancel, onSubmit }: {
  po: PurchaseOrder; busy: boolean
  onCancel: () => void
  onSubmit: (payload: Parameters<typeof createGoodsReceipt>[0]) => void
  }) {
  const [rows, setRows] = useState(() =>
    po.lines
      .filter((l) => l.outstanding_quantity_milli > 0)
      .map((l) => ({
        lineNo: l.line_no,
        itemCode: l.item_code,
        description: l.description,
        outstanding: l.outstanding_quantity_milli,
        outstandingDisplay: l.outstanding_quantity_display,
        accepted: '',
        rejected: '',
        rejectionReason: '',
        batch: '',
        expiry: '',
      })))
  const [invoice, setInvoice] = useState('')
  const [problem, setProblem] = useState<string | null>(null)
  // A stable key for the whole attempt, so a double-tap on a slow connection
  // replays the receipt the server already made instead of posting it twice.
  const [idempotencyKey] = useState(
    () => `grn-${po.id}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`)

  const set = (index: number, patch: Partial<(typeof rows)[number]>) =>
    setRows(rows.map((r, i) => (i === index ? { ...r, ...patch } : r)))

  const submit = () => {
    setProblem(null)
    const lines: Parameters<typeof createGoodsReceipt>[0]['lines'] = []
    for (const row of rows) {
      const accepted = row.accepted === '' ? 0 : Number(row.accepted)
      const rejected = row.rejected === '' ? 0 : Number(row.rejected)
      if (!Number.isInteger(accepted) || !Number.isInteger(rejected)
          || accepted < 0 || rejected < 0) {
        setProblem(`Quantities for ${row.itemCode} must be whole numbers.`)
        return
      }
      if (accepted === 0 && rejected === 0) continue
      if (unitsToMilli(accepted + rejected) > row.outstanding) {
        setProblem(
          `${row.itemCode}: receiving more than the outstanding `
          + `${row.outstandingDisplay} would over-receive the order. `
          + 'Raise an amended purchase order for the excess.')
        return
      }
      if (rejected > 0 && !row.rejectionReason.trim()) {
        setProblem(`${row.itemCode}: say why the ${rejected} rejected were rejected.`)
        return
      }
      lines.push({
        po_line_no: row.lineNo,
        accepted_quantity_milli: unitsToMilli(accepted),
        rejected_quantity_milli: unitsToMilli(rejected),
        rejection_reason: row.rejectionReason.trim() || undefined,
        batch_no: row.batch.trim() || undefined,
        expiry_date: row.expiry || undefined,
      })
    }
    if (lines.length === 0) { setProblem('Enter what actually arrived.'); return }
    onSubmit({
      purchase_order_id: po.id,
      vendor_invoice_number: invoice.trim() || undefined,
      idempotency_key: idempotencyKey,
      lines,
    })
  }

  return (
    <section className="proc-receive">
      <h3 className="proc-subhead">Receive goods</h3>
      <p className="proc-hint">
        Enter what actually arrived. A part delivery is normal. The balance stays
        outstanding on the order. Rejected goods are recorded but do not enter stock.
      </p>
      {problem && <div className="proc-alert proc-alert--danger" role="alert">{problem}</div>}

      {rows.map((row, index) => (
        <div className="proc-receive__line" key={row.lineNo}>
          <div className="proc-receive__item">
            <div className="proc-cell__primary">{row.description}</div>
            <div className="proc-cell__secondary">
              {row.itemCode} · {row.outstandingDisplay} outstanding
            </div>
          </div>
          <div className="proc-receive__fields">
            <label className="proc-field proc-field--narrow">
              <span className="proc-field__label">Accepted</span>
              <input className="proc-input proc-input--num" inputMode="numeric"
                     aria-label={`Accepted quantity for ${row.itemCode}`}
                     value={row.accepted}
                     onChange={(e) => set(index, { accepted: e.target.value })} />
            </label>
            <label className="proc-field proc-field--narrow">
              <span className="proc-field__label">Rejected</span>
              <input className="proc-input proc-input--num" inputMode="numeric"
                     aria-label={`Rejected quantity for ${row.itemCode}`}
                     value={row.rejected}
                     onChange={(e) => set(index, { rejected: e.target.value })} />
            </label>
            <label className="proc-field proc-field--narrow">
              <span className="proc-field__label">Batch</span>
              <input className="proc-input" value={row.batch}
                     aria-label={`Batch number for ${row.itemCode}`}
                     onChange={(e) => set(index, { batch: e.target.value })} />
            </label>
            <label className="proc-field proc-field--narrow">
              <span className="proc-field__label">Expiry</span>
              <input className="proc-input" type="date" value={row.expiry}
                     aria-label={`Expiry date for ${row.itemCode}`}
                     onChange={(e) => set(index, { expiry: e.target.value })} />
            </label>
          </div>
          {Number(row.rejected) > 0 && (
            <label className="proc-field">
              <span className="proc-field__label">Why were they rejected?</span>
              <input className="proc-input" value={row.rejectionReason}
                     onChange={(e) => set(index, { rejectionReason: e.target.value })} />
            </label>
          )}
        </div>
      ))}

      <label className="proc-field">
        <span className="proc-field__label">Vendor invoice number (optional)</span>
        <input className="proc-input" value={invoice} onChange={(e) => setInvoice(e.target.value)} />
      </label>

      <div className="proc-drawer__actions">
        <button type="button" className="proc-btn" onClick={onCancel}>Cancel</button>
        <button type="button" className="proc-btn proc-btn--primary" disabled={busy}
                onClick={submit}>
          Post receipt
        </button>
      </div>
    </section>
  )
}

/* ── Stock ───────────────────────────────────────────────────────────────── */

function StockView({ storeCode, token, onError, run, busy }: {
  storeCode: string; token: number
  onError: (e: unknown) => void
  run: (fn: () => Promise<unknown>, note?: string) => Promise<void>
  busy: boolean
}) {
  const [rows, setRows] = useState<StockRow[]>([])
  const [lowOnly, setLowOnly] = useState(false)
  const [ledger, setLedger] = useState<{ row: StockRow; movements: StockMovement[] } | null>(null)
  const [issuing, setIssuing] = useState<StockRow | null>(null)

  useEffect(() => {
    let alive = true
    getStock({ storeCode: storeCode || undefined, lowOnly })
      .then((r) => { if (alive) setRows(r) })
      .catch(onError)
    return () => { alive = false }
  }, [storeCode, lowOnly, token, onError])

  const totalValue = useMemo(
    () => rows.reduce((sum, r) => sum + (r.value_paise ?? 0), 0), [rows])
  const unvalued = useMemo(() => rows.filter((r) => r.value_paise === null).length, [rows])

  return (
    <div className="proc-stack">
      <div className="proc-section__header">
        <h2 className="proc-section__title">Stock on hand</h2>
        <label className="proc-field proc-field--check">
          <input type="checkbox" checked={lowOnly}
                 onChange={(e) => setLowOnly(e.target.checked)} />
          <span>Only what needs ordering</span>
        </label>
      </div>

      <div className="proc-tiles">
        <Tile value={String(rows.length)} caption="Item / store positions" />
        <Tile
          value={unvalued === 0 ? formatInr(totalValue) : 'Partly unpriced'}
          caption={unvalued === 0
            ? 'Stock value'
            : `Stock value: ${plural(unvalued, 'item')} ${unvalued === 1 ? 'has' : 'have'} no purchase rate yet`}
          tone={unvalued === 0 ? undefined : 'warn'}
        />
      </div>

      {rows.length === 0 ? (
        <Empty title="Nothing to show"
               detail="Stock appears once an item has been received, or an opening balance has been recorded against it." />
      ) : (
        <div className="proc-table-wrap">
          <table className="proc-table">
            <thead>
              <tr>
                <th scope="col">Item</th>
                <th scope="col">Store</th>
                <th scope="col" className="proc-num">On hand</th>
                <th scope="col" className="proc-num">Reorder at</th>
                <th scope="col" className="proc-num">Last rate</th>
                <th scope="col" className="proc-num">Value</th>
                <th scope="col"><span className="proc-visually-hidden">Actions</span></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={`${row.item_code}-${row.store_code ?? 'none'}`}
                    className={row.is_low ? 'proc-tr--low' : undefined}>
                  <td>
                    <div className="proc-cell__primary">{row.item_name}</div>
                    <div className="proc-cell__secondary">{row.item_code} · {humaniseEnum(row.category)}</div>
                  </td>
                  <td>{row.store_name ?? 'Never stocked'}</td>
                  <td className={`proc-num${row.is_low ? ' proc-num--warn' : ''}`}>
                    {row.on_hand_display}
                    {row.is_low && <span className="proc-flag">Reorder</span>}
                  </td>
                  <td className="proc-num">{row.reorder_level_display}</td>
                  <td className="proc-num"><Money paise={row.last_purchase_rate_paise} /></td>
                  <td className="proc-num">
                    <Money paise={row.value_paise} unset="Rate not configured" />
                  </td>
                  <td className="proc-actions-cell">
                    <div className="proc-actions">
                      <button
                        type="button" className="proc-btn proc-btn--tiny"
                        onClick={() => getStockLedger(row.item_code, row.store_code ?? undefined)
                          .then((l) => setLedger({ row, movements: l.movements }))
                          .catch(onError)}
                      >
                        Ledger
                      </button>
                      {row.store_code && (
                        <button type="button" className="proc-btn proc-btn--tiny"
                                onClick={() => setIssuing(row)}>
                          Issue
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {ledger && (
        <Drawer
          title={`${ledger.row.item_name} ledger`}
          subtitle={`${ledger.row.on_hand_display} on hand in ${ledger.row.store_name ?? 'all stores'}`}
          onClose={() => setLedger(null)}
        >
          <p className="proc-hint">
            On hand is the sum of these movements. There is no separate balance that
            could disagree with them.
          </p>
          <div className="proc-table-wrap">
            <table className="proc-table">
              <thead>
                <tr>
                  <th scope="col">When</th>
                  <th scope="col">Type</th>
                  <th scope="col" className="proc-num">Change</th>
                  <th scope="col" className="proc-num">Balance</th>
                  <th scope="col">Reference</th>
                </tr>
              </thead>
              <tbody>
                {ledger.movements.map((m) => (
                  <tr key={m.id}>
                    <td className="proc-nowrap">{formatClinicalDateTime(m.moved_at)}</td>
                    <td>{label(m.movement_type)}</td>
                    <td className={`proc-num${m.quantity_milli < 0 ? ' proc-num--out' : ''}`}>
                      {m.quantity_display}
                    </td>
                    <td className="proc-num">{formatQuantity(m.balance_after_milli)}</td>
                    <td>
                      {m.reference_type ? `${humaniseEnum(m.reference_type)} ${m.reference_id ?? ''}`.trim() : '—'}
                      {m.batch_no ? ` · batch ${m.batch_no}` : ''}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Drawer>
      )}

      {issuing && (
        <IssueDrawer
          row={issuing} busy={busy} onClose={() => setIssuing(null)}
          onSubmit={(payload) => run(
            () => recordStockMovement(payload).then(() => setIssuing(null)),
            'Stock issued.',
          )}
        />
      )}
    </div>
  )
}

function IssueDrawer({ row, busy, onClose, onSubmit }: {
  row: StockRow; busy: boolean
  onClose: () => void
  onSubmit: (payload: Parameters<typeof recordStockMovement>[0]) => void
}) {
  const [units, setUnits] = useState('')
  const [department, setDepartment] = useState('')
  const [problem, setProblem] = useState<string | null>(null)

  const submit = () => {
    setProblem(null)
    const value = Number(units)
    if (!units || !Number.isInteger(value) || value <= 0) {
      setProblem('Quantity must be a whole number greater than zero.')
      return
    }
    if (unitsToMilli(value) > row.on_hand_milli) {
      setProblem(
        `Only ${row.on_hand_display} is on hand. Issuing more would take the store `
        + 'negative, which means the ledger and the shelf already disagree. Count '
        + 'the shelf and record an adjustment instead.')
      return
    }
    onSubmit({
      item_code: row.item_code,
      store_code: row.store_code as string,
      movement_type: 'issue',
      quantity_milli: unitsToMilli(value),
      department: department.trim() || undefined,
    })
  }

  return (
    <Drawer title={`Issue ${row.item_name}`}
            subtitle={`${row.on_hand_display} on hand in ${row.store_name}`}
            onClose={onClose}>
      {problem && <div className="proc-alert proc-alert--danger" role="alert">{problem}</div>}
      <div className="proc-form-grid">
        <label className="proc-field proc-field--narrow">
          <span className="proc-field__label">Quantity ({row.uom})</span>
          <input className="proc-input proc-input--num" inputMode="numeric" value={units}
                 onChange={(e) => setUnits(e.target.value)} />
        </label>
        <label className="proc-field">
          <span className="proc-field__label">To department</span>
          <input className="proc-input" value={department}
                 onChange={(e) => setDepartment(e.target.value)} placeholder="e.g. Theatre" />
        </label>
      </div>
      <div className="proc-drawer__actions">
        <button type="button" className="proc-btn proc-btn--primary" disabled={busy}
                onClick={submit}>
          Issue stock
        </button>
      </div>
    </Drawer>
  )
}

/* ── Consumption ─────────────────────────────────────────────────────────── */

function ConsumptionView({ storeCode, token, onError }: {
  storeCode: string; token: number; onError: (e: unknown) => void
}) {
  const [report, setReport] = useState<ConsumptionReport | null>(null)
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')

  useEffect(() => {
    let alive = true
    getConsumption({
      storeCode: storeCode || undefined,
      dateFrom: from || undefined,
      dateTo: to || undefined,
    })
      .then((r) => { if (alive) setReport(r) })
      .catch(onError)
    return () => { alive = false }
  }, [storeCode, from, to, token, onError])

  if (!report) return <div className="proc-loading">Loading consumption…</div>

  return (
    <div className="proc-stack">
      <div className="proc-section__header">
        <h2 className="proc-section__title">Consumption by department</h2>
        <div className="proc-head__controls">
          <label className="proc-field proc-field--inline">
            <span className="proc-field__label">From</span>
            <input className="proc-input" type="date" value={from}
                   onChange={(e) => setFrom(e.target.value)} />
          </label>
          <label className="proc-field proc-field--inline">
            <span className="proc-field__label">To</span>
            <input className="proc-input" type="date" value={to}
                   onChange={(e) => setTo(e.target.value)} />
          </label>
        </div>
      </div>

      <p className="proc-hint">
        Only issues to a department count. A transfer between two of the hospital&apos;s own
        stores is not consumption, and a write-off is a loss rather than a use.
      </p>

      {!report.value_is_complete && (
        <div className="proc-alert proc-alert--warn" role="status">
          {plural(report.unvalued_movement_count, 'movement')}{' '}
          {report.unvalued_movement_count === 1 ? 'has' : 'have'} no purchase rate on the item, so
          {report.unvalued_movement_count === 1 ? ' it contributes' : ' they contribute'} nothing to the
          totals below. The value shown is incomplete, not low.
        </div>
      )}

      <div className="proc-tiles">
        {report.departments.map((dept) => (
          <Tile key={dept.department} value={dept.value_display} caption={dept.department} />
        ))}
        <Tile value={report.total_value_display} caption="Total"
              tone={report.value_is_complete ? undefined : 'warn'} />
      </div>

      {report.lines.length === 0 ? (
        <Empty title="Nothing was issued in this period"
               detail="Consumption is recorded when a store issues stock to a department." />
      ) : (
        <div className="proc-table-wrap">
          <table className="proc-table">
            <thead>
              <tr>
                <th scope="col">Department</th>
                <th scope="col">Item</th>
                <th scope="col" className="proc-num">Quantity</th>
                <th scope="col" className="proc-num">Value</th>
              </tr>
            </thead>
            <tbody>
              {report.lines.map((line) => (
                <tr key={`${line.department}-${line.item_code}-${line.store_code}`}>
                  <td>{line.department}</td>
                  <td>
                    <div className="proc-cell__primary">{line.item_name ?? line.item_code}</div>
                    <div className="proc-cell__secondary">{line.item_code}</div>
                  </td>
                  <td className="proc-num">{line.quantity_display}</td>
                  <td className="proc-num">{line.value_display}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

/* ── Masters ─────────────────────────────────────────────────────────────── */

function MastersView({ meta, token, onError, run, busy }: {
  meta: ProcurementMeta | null; token: number
  onError: (e: unknown) => void
  run: (fn: () => Promise<unknown>, note?: string, onFail?: (err: unknown) => boolean) => Promise<void>
  busy: boolean
}) {
  const [items, setItems] = useState<InventoryItem[]>([])
  const [vendors, setVendors] = useState<Vendor[]>([])
  const [showItem, setShowItem] = useState(false)
  const [itemRefusal, setItemRefusal] = useState<string | null>(null)
  const [vendorRefusal, setVendorRefusal] = useState<string | null>(null)
  const [showVendor, setShowVendor] = useState(false)

  useEffect(() => {
    let alive = true
    Promise.all([getItems(), getVendors({ includeBlacklisted: true })])
      .then(([i, v]) => { if (alive) { setItems(i); setVendors(v) } })
      .catch(onError)
    return () => { alive = false }
  }, [token, onError])

  return (
    <div className="proc-stack">
      <div className="proc-section__header">
        <h2 className="proc-section__title">Items</h2>
        <button type="button" className="proc-btn proc-btn--primary"
                onClick={() => setShowItem(true)}>Add an item</button>
      </div>
      <p className="proc-hint">
        Non-pharmacy only. An item whose code or name matches a medicine in the Helix
        (Pharmacy) catalogue is refused, because a second stock record for a drug is how
        two systems end up disagreeing about how much of a controlled substance exists.
      </p>

      {items.length === 0 ? (
        <Empty title="No items yet"
               detail="Nothing can be indented, ordered or stocked until it exists in the item master." />
      ) : (
        <div className="proc-table-wrap">
          <table className="proc-table">
            <thead>
              <tr>
                <th scope="col">Item</th>
                <th scope="col">Category</th>
                <th scope="col">UOM</th>
                <th scope="col" className="proc-num">Reorder at</th>
                <th scope="col" className="proc-num">Last rate</th>
                <th scope="col">GST</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.code}>
                  <td>
                    <div className="proc-cell__primary">{item.name}</div>
                    <div className="proc-cell__secondary">{item.code}</div>
                  </td>
                  <td>{humaniseEnum(item.category)}{item.tracks_expiry ? ' · batch/expiry' : ''}</td>
                  <td>{item.uom}</td>
                  <td className="proc-num">
                    {item.reorder_level_milli === null
                      ? 'Not set'
                      : formatQuantity(item.reorder_level_milli, item.uom)}
                  </td>
                  <td className="proc-num"><Money paise={item.last_purchase_rate_paise} /></td>
                  <td>
                    {item.gst_treatment === 'taxable'
                      ? `${(item.gst_rate_bp / 100).toFixed(2)}%`
                      : label(item.gst_treatment)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="proc-section__header">
        <h2 className="proc-section__title">Vendors</h2>
        <button type="button" className="proc-btn proc-btn--primary"
                onClick={() => setShowVendor(true)}>Add a vendor</button>
      </div>

      {vendors.length === 0 ? (
        <Empty title="No vendors yet" detail="A purchase order needs a vendor." />
      ) : (
        <div className="proc-table-wrap">
          <table className="proc-table">
            <thead>
              <tr>
                <th scope="col">Vendor</th>
                <th scope="col">GSTIN</th>
                <th scope="col">State</th>
                <th scope="col">Terms</th>
                <th scope="col">Status</th>
              </tr>
            </thead>
            <tbody>
              {vendors.map((v) => (
                <tr key={v.code}>
                  <td>
                    <div className="proc-cell__primary">{v.name}</div>
                    <div className="proc-cell__secondary">{v.code}</div>
                  </td>
                  <td>{v.gstin ?? 'Not on file'}</td>
                  <td>{v.state_code ?? 'Not on file'}</td>
                  <td>{v.payment_terms_days !== null ? `${v.payment_terms_days} days` : 'Not set'}</td>
                  <td>
                    {v.is_blacklisted
                      ? <span className="proc-badge proc-badge--danger">Blacklisted</span>
                      : <span className="proc-badge proc-badge--ok">Active</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {showItem && (
        <NewItemDrawer
          meta={meta} busy={busy} refusal={itemRefusal}
          onClose={() => { setShowItem(false); setItemRefusal(null) }}
          onSubmit={(payload) => {
            setItemRefusal(null)
            return run(
              () => saveItem(payload).then(() => setShowItem(false)),
              'Item saved.',
              // A refusal is about the form in front of the storekeeper, so it
              // is said inside the drawer rather than on the panel behind it.
              (err) => {
                if (err instanceof ProcurementApiError && err.status < 500 && !err.isModuleDisabled) {
                  setItemRefusal(err.message)
                  return true
                }
                return false
              },
            )
          }}
        />
      )}
      {showVendor && (
        <NewVendorDrawer
          busy={busy} refusal={vendorRefusal}
          onClose={() => { setShowVendor(false); setVendorRefusal(null) }}
          onSubmit={(payload) => {
            setVendorRefusal(null)
            return run(
              () => saveVendor(payload).then(() => setShowVendor(false)),
              'Vendor saved.',
              // Same as the item drawer: a code already in use is said inside
              // the drawer, with what was typed still in it.
              (err) => {
                if (err instanceof ProcurementApiError && err.status < 500 && !err.isModuleDisabled) {
                  setVendorRefusal(err.message)
                  return true
                }
                return false
              },
            )
          }}
        />
      )}
    </div>
  )
}

function NewItemDrawer({ meta, busy, refusal, onClose, onSubmit }: {
  meta: ProcurementMeta | null; busy: boolean
  /** The server's sentence when it refused the last save. */
  refusal?: string | null
  onClose: () => void
  onSubmit: (payload: Record<string, unknown> & { code: string; name: string }) => void
}) {
  const categories = meta?.item_categories ?? ['consumable']
  const [code, setCode] = useState('')
  const [name, setName] = useState('')
  const [category, setCategory] = useState('consumable')
  const [uom, setUom] = useState('each')
  const [gstPercent, setGstPercent] = useState('12')
  const [exempt, setExempt] = useState(false)
  const [reorder, setReorder] = useState('')
  const [problem, setProblem] = useState<string | null>(null)

  const submit = () => {
    setProblem(null)
    if (!code.trim() || !name.trim()) { setProblem('An item needs a code and a name.'); return }
    let rateBp = 0
    if (!exempt) {
      const percent = Number(gstPercent)
      if (!Number.isFinite(percent) || percent < 0) {
        setProblem('GST rate must be a percentage.'); return
      }
      rateBp = Math.round(percent * 100)
    }
    let reorderQty: number | undefined
    if (reorder.trim()) {
      const value = Number(reorder)
      if (!Number.isInteger(value) || value < 0) {
        setProblem('Reorder level must be a whole number of units.'); return
      }
      reorderQty = value
    }
    onSubmit({
      code: code.trim().toUpperCase(),
      name: name.trim(),
      category,
      uom: uom.trim() || 'each',
      gst_treatment: exempt ? 'exempt' : 'taxable',
      gst_rate_bp: rateBp,
      reorder_level_qty: reorderQty,
      // An Add, not an edit: a code already in use is refused, not overwritten.
      create_only: true,
    })
  }

  return (
    <Drawer title="Add a non-pharmacy item"
            subtitle="Medications belong to Helix (Pharmacy). An item that matches one is refused."
            onClose={onClose}>
      {(problem || refusal) && (
        <div className="proc-alert proc-alert--danger" role="alert">{problem || refusal}</div>
      )}
      <div className="proc-form-grid">
        <label className="proc-field">
          <span className="proc-field__label">Code</span>
          <input className="proc-input" value={code} onChange={(e) => setCode(e.target.value)} />
        </label>
        <label className="proc-field">
          <span className="proc-field__label">Name</span>
          <input className="proc-input" value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <label className="proc-field">
          <span className="proc-field__label">Category</span>
          <select className="proc-input" value={category}
                  onChange={(e) => setCategory(e.target.value)}>
            {categories.map((c) => <option key={c} value={c}>{humaniseEnum(c)}</option>)}
          </select>
        </label>
        <label className="proc-field proc-field--narrow">
          <span className="proc-field__label">Unit</span>
          <input className="proc-input" value={uom} onChange={(e) => setUom(e.target.value)} />
        </label>
        <label className="proc-field proc-field--narrow">
          <span className="proc-field__label">Reorder at (units)</span>
          <input className="proc-input proc-input--num" inputMode="numeric" value={reorder}
                 onChange={(e) => setReorder(e.target.value)} />
          <span className="proc-field__hint">
            Left blank means no reorder alert: unconfigured, not &ldquo;fine&rdquo;.
          </span>
        </label>
        <label className="proc-field proc-field--check">
          <input type="checkbox" checked={exempt} onChange={(e) => setExempt(e.target.checked)} />
          <span>
            GST exempt
            <span className="proc-field__hint">
              Hospital services are GST-exempt in India; the consumables, reagents and
              implants a hospital buys generally are not.
            </span>
          </span>
        </label>
        {!exempt && (
          <label className="proc-field proc-field--narrow">
            <span className="proc-field__label">GST %</span>
            <input className="proc-input proc-input--num" inputMode="decimal" value={gstPercent}
                   onChange={(e) => setGstPercent(e.target.value)} />
          </label>
        )}
      </div>
      <div className="proc-drawer__actions">
        <button type="button" className="proc-btn proc-btn--primary" disabled={busy}
                onClick={submit}>Save item</button>
      </div>
    </Drawer>
  )
}

function NewVendorDrawer({ busy, refusal, onClose, onSubmit }: {
  busy: boolean
  /** The server's sentence when it refused the last save. */
  refusal?: string | null
  onClose: () => void
  onSubmit: (payload: Parameters<typeof saveVendor>[0]) => void
}) {
  const [code, setCode] = useState('')
  const [name, setName] = useState('')
  const [gstin, setGstin] = useState('')
  const [stateCode, setStateCode] = useState('')
  const [terms, setTerms] = useState('')
  const [problem, setProblem] = useState<string | null>(null)

  const submit = () => {
    setProblem(null)
    if (!code.trim() || !name.trim()) { setProblem('A vendor needs a code and a name.'); return }
    let days: number | undefined
    if (terms.trim()) {
      const value = Number(terms)
      if (!Number.isInteger(value) || value < 0) {
        setProblem('Payment terms must be a whole number of days.'); return
      }
      days = value
    }
    onSubmit({
      code: code.trim().toUpperCase(),
      name: name.trim(),
      gstin: gstin.trim() || null,
      state_code: stateCode.trim() || null,
      payment_terms_days: days ?? null,
      create_only: true,
    })
  }

  return (
    <Drawer title="Add a vendor" onClose={onClose}>
      {(problem || refusal) && (
        <div className="proc-alert proc-alert--danger" role="alert">{problem || refusal}</div>
      )}
      <div className="proc-form-grid">
        <label className="proc-field">
          <span className="proc-field__label">Code</span>
          <input className="proc-input" value={code} onChange={(e) => setCode(e.target.value)} />
        </label>
        <label className="proc-field">
          <span className="proc-field__label">Name</span>
          <input className="proc-input" value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <label className="proc-field">
          <span className="proc-field__label">GSTIN</span>
          <input className="proc-input" value={gstin} onChange={(e) => setGstin(e.target.value)} />
        </label>
        <label className="proc-field proc-field--narrow">
          <span className="proc-field__label">State code</span>
          <input className="proc-input" value={stateCode}
                 onChange={(e) => setStateCode(e.target.value)} />
          <span className="proc-field__hint">
            Used to show whether a supply is inter-state. Orb never guesses the tax heads
            from it.
          </span>
        </label>
        <label className="proc-field proc-field--narrow">
          <span className="proc-field__label">Payment terms (days)</span>
          <input className="proc-input proc-input--num" inputMode="numeric" value={terms}
                 onChange={(e) => setTerms(e.target.value)} />
        </label>
      </div>
      <p className="proc-hint">
        Orb deliberately does not store a full bank account number. It has no
        vendor-payment rail, and holding one on an appliance full of patient data would
        make it a target for no operational gain.
      </p>
      <div className="proc-drawer__actions">
        <button type="button" className="proc-btn proc-btn--primary" disabled={busy}
                onClick={submit}>Save vendor</button>
      </div>
    </Drawer>
  )
}
