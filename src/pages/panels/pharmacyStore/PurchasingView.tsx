/**
 * Pharmacy store, Purchasing (grn-7): drug purchase orders from draft to the
 * vendor, and reorder suggestions turned into a draft.
 *
 * Discovered by the panel through ./viewRegistry.ts; shown to anyone holding
 * pharmacy_store.read. Raising and submitting a draft needs the write tier;
 * approving and sending to the vendor need the approve tier; the person who
 * raised an order never approves it. The server refuses anyone else whatever
 * this screen shows, and every refusal is shown in its own words.
 *
 * THE SERVER DECIDES what may be ordered: a vendor with a valid drug licence,
 * a classified drug with a pack size and a GST rate, a narcotic or
 * psychotropic into the NDPS vault. The form offers what the server says is
 * orderable and shows why the rest is not; it never works the rules out
 * itself. A rate is always typed by a person: a suggestion offers the last
 * rate paid, never an invented one.
 *
 * No patient data is read or shown. Order dates are calendar keys
 * (formatClinicalDateKey). Money is integer paise, typed and shown in rupees
 * through src/lib/money.
 */
import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react'

import { getUser } from '../../../api/client'
import { toPharmacyStoreError } from '../../../api/pharmacyStore'
import {
  LICENCE_LABELS,
  PURCHASE_ORDER_STATUS_LABELS,
  cancelPurchaseOrder,
  createPurchaseOrder,
  createPurchaseOrderFromSuggestions,
  getPurchaseOrder,
  getPurchasingOptions,
  getReorderSuggestions,
  listPurchaseOrders,
  movePurchaseOrder,
  type LicenceStatus,
  type PurchaseOrder,
  type PurchaseOrderAction,
  type PurchaseOrderStatus,
  type PurchasingOptions,
  type PurchasingWarning,
  type ReorderSuggestion,
} from '../../../api/pharmacyStore/purchasing'
import { formatClinicalDateKey } from '../../../lib/datetime'
import { formatInr, toPaise } from '../../../lib/money'
import { PHARMACY_STORE_APPROVE, PHARMACY_STORE_WRITE, hasPermission } from '../../../lib/roles'
import type { PharmacyStoreViewProps } from './viewRegistry'
import './stockAndPurchasing.css'

export const viewId = 'purchasing'
export const viewLabel = 'Purchasing'
export const viewOrder = 20
export const viewPermission = 'pharmacy_store.read'

type Section = 'orders' | 'new' | 'suggestions'

type Fail = (err: unknown) => string

function useFail(onModuleOff: () => void): Fail {
  return useCallback((err: unknown) => {
    const failure = toPharmacyStoreError(err)
    if (failure.isModuleDisabled) onModuleOff()
    return failure.message || 'Something went wrong. Nothing was saved.'
  }, [onModuleOff])
}

const STATUS_FILTERS: { value: PurchaseOrderStatus | ''; label: string }[] = [
  { value: '', label: 'All' },
  { value: 'draft', label: 'Drafts' },
  { value: 'pending_approval', label: 'Awaiting approval' },
  { value: 'approved', label: 'Approved' },
  { value: 'issued', label: 'Sent to vendor' },
  { value: 'cancelled', label: 'Cancelled' },
]

function statusTone(status: PurchaseOrderStatus): string {
  if (status === 'cancelled') return 'psw-chip--danger'
  if (status === 'pending_approval' || status === 'draft') return 'psw-chip--warn'
  return 'psw-chip--ok'
}

function licenceTone(status: LicenceStatus): string {
  if (status === 'valid') return 'psw-chip--ok'
  if (status === 'expiring') return 'psw-chip--warn'
  return 'psw-chip--danger'
}

function paise(value: number | null | undefined): string {
  return value === null || value === undefined ? 'Not recorded' : formatInr(value)
}

/** Rupees as typed ("45" or "45.50") to whole paise, or null when unreadable. */
function rupeesToPaise(text: string): number | null {
  const t = text.trim()
  if (!t) return null
  try {
    const value = toPaise(t)
    return value >= 0 ? value : null
  } catch {
    return null
  }
}

function paiseToRupeesText(value: number | null | undefined): string {
  if (value === null || value === undefined) return ''
  return formatInr(value, { symbol: false }).replace(/,/g, '')
}

interface DraftLine {
  key: number
  drugId: string
  packs: string
  rate: string
}

interface DraftForm {
  vendorCode: string
  storeCode: string
  expectedDate: string
  interstate: boolean
  remarks: string
  lines: DraftLine[]
  /** Started from reorder suggestions: saved through the suggestions route,
   *  so the order records where it came from. */
  fromSuggestions: boolean
}

let lineKey = 0
function blankLine(drugId = '', packs = '', rate = ''): DraftLine {
  lineKey += 1
  return { key: lineKey, drugId, packs, rate }
}

function blankForm(): DraftForm {
  return { vendorCode: '', storeCode: '', expectedDate: '', interstate: false, remarks: '',
    lines: [blankLine()], fromSuggestions: false }
}

export default function PurchasingView({ onModuleOff }: PharmacyStoreViewProps) {
  const [section, setSection] = useState<Section>('orders')
  const [openId, setOpenId] = useState<number | null>(null)
  const [draft, setDraft] = useState<DraftForm>(blankForm)
  const [notice, setNotice] = useState('')
  const fail = useFail(onModuleOff)
  const canWrite = hasPermission(PHARMACY_STORE_WRITE)
  const canApprove = hasPermission(PHARMACY_STORE_APPROVE)

  function onCreated(order: PurchaseOrder, warnings: PurchasingWarning[]) {
    setDraft(blankForm())
    setOpenId(order.id)
    setNotice(['Draft saved.', ...warnings.map(w => w.message)].join(' '))
    setSection('orders')
  }

  function startFromSuggestions(rows: ReorderSuggestion[]) {
    setDraft({
      ...blankForm(),
      storeCode: rows[0]?.store_code ?? '',
      vendorCode: rows.find(r => r.last_vendor_code)?.last_vendor_code ?? '',
      lines: rows.map(r => blankLine(r.drug_id, r.suggested_packs ? String(r.suggested_packs) : '',
        paiseToRupeesText(r.last_rate_paise))),
      fromSuggestions: true,
    })
    setNotice('')
    setSection('new')
  }

  const sections: { id: Section; label: string }[] = [
    { id: 'orders', label: 'Orders' },
    ...(canWrite ? [{ id: 'new' as Section, label: 'New order' }] : []),
    { id: 'suggestions', label: 'Reorder suggestions' },
  ]

  return (
    <div className="psw-view">
      <nav className="psw-sections" aria-label="Purchasing sections">
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
      {section === 'orders' && (
        <OrdersSection
          fail={fail}
          canWrite={canWrite}
          canApprove={canApprove}
          openId={openId}
          setOpenId={setOpenId}
        />
      )}
      {section === 'new' && canWrite && (
        <NewOrderSection fail={fail} draft={draft} setDraft={setDraft} onCreated={onCreated} />
      )}
      {section === 'suggestions' && (
        <SuggestionsSection fail={fail} canWrite={canWrite} onStart={startFromSuggestions} />
      )}
    </div>
  )
}

// ═════════════════════════════════════════════════════════════════════════════
//  Orders
// ═════════════════════════════════════════════════════════════════════════════

function OrdersSection({ fail, canWrite, canApprove, openId, setOpenId }: {
  fail: Fail
  canWrite: boolean
  canApprove: boolean
  openId: number | null
  setOpenId: (id: number | null) => void
}) {
  const [status, setStatus] = useState<PurchaseOrderStatus | ''>('')
  const [orders, setOrders] = useState<PurchaseOrder[] | null>(null)
  const [error, setError] = useState('')

  const load = useCallback(() => {
    listPurchaseOrders({ status }).then(
      res => { setError(''); setOrders(res.purchase_orders) },
      err => setError(fail(err)),
    )
  }, [fail, status])

  useEffect(() => { load() }, [load])

  return (
    <section className="psw-card" aria-labelledby="psw-orders-title">
      <div className="psw-card__head">
        <h2 id="psw-orders-title" className="psw-card__title">Purchase orders</h2>
        <div className="psw-toolbar">
          <label htmlFor="psw-order-status" className="psw-visually-hidden">Show orders</label>
          <select
            id="psw-order-status"
            value={status}
            onChange={e => setStatus(e.target.value as PurchaseOrderStatus | '')}
          >
            {STATUS_FILTERS.map(f => <option key={f.value} value={f.value}>{f.label}</option>)}
          </select>
        </div>
      </div>
      {error && <p className="psw-error" role="alert">{error}</p>}
      {orders === null && !error && <p className="psw-note" role="status">Loading orders</p>}
      {orders && orders.length === 0 && <p className="psw-note">No purchase orders here yet.</p>}
      {orders && orders.length > 0 && (
        <div className="psw-table-scroll">
          <table className="psw-table">
            <thead>
              <tr>
                <th scope="col">Order</th>
                <th scope="col">Vendor</th>
                <th scope="col">Store</th>
                <th scope="col">Status</th>
                <th scope="col" className="psw-num">Total</th>
                <th scope="col"><span className="psw-visually-hidden">Actions</span></th>
              </tr>
            </thead>
            <tbody>
              {orders.map(o => (
                <tr key={o.id} className={o.id === openId ? 'psw-row--open' : undefined}>
                  <td>
                    <span className="psw-strong">{o.po_number ?? 'Not numbered yet'}</span>
                    <span className="psw-sub">{formatClinicalDateKey(o.order_date)}</span>
                  </td>
                  <td>{o.vendor_name ?? o.vendor_code}</td>
                  <td>{o.store_code}</td>
                  <td>
                    <span className={`psw-chip ${statusTone(o.status)}`}>
                      {PURCHASE_ORDER_STATUS_LABELS[o.status] ?? o.status}
                    </span>
                  </td>
                  <td className="psw-num">{formatInr(o.total_paise)}</td>
                  <td className="psw-actions-cell">
                    <button
                      type="button"
                      className="psw-btn psw-btn--quiet"
                      aria-expanded={o.id === openId}
                      onClick={() => setOpenId(o.id === openId ? null : o.id)}
                    >
                      {o.id === openId ? 'Close' : 'Open'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {openId !== null && (
        <OrderDetail
          key={openId}
          poId={openId}
          fail={fail}
          canWrite={canWrite}
          canApprove={canApprove}
          onChanged={load}
        />
      )}
    </section>
  )
}

function OrderDetail({ poId, fail, canWrite, canApprove, onChanged }: {
  poId: number
  fail: Fail
  canWrite: boolean
  canApprove: boolean
  onChanged: () => void
}) {
  const [order, setOrder] = useState<PurchaseOrder | null>(null)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const [cancelling, setCancelling] = useState(false)
  const [reason, setReason] = useState('')
  const me = getUser()?.id ?? null

  useEffect(() => {
    let live = true
    getPurchaseOrder(poId).then(
      res => { if (live) { setError(''); setOrder(res.purchase_order) } },
      err => { if (live) setError(fail(err)) },
    )
    return () => { live = false }
  }, [poId, fail])

  function act(run: () => Promise<{ purchase_order: PurchaseOrder; warnings: PurchasingWarning[] }>,
    done: string) {
    setBusy(true)
    setError('')
    setMessage('')
    run().then(
      res => {
        setOrder(res.purchase_order)
        setMessage([done, ...res.warnings.map(w => w.message)].join(' '))
        setCancelling(false)
        setReason('')
        onChanged()
      },
      err => setError(fail(err)),
    ).finally(() => setBusy(false))
  }

  function move(action: PurchaseOrderAction, done: string) {
    act(() => movePurchaseOrder(poId, action), done)
  }

  function onCancel(e: FormEvent) {
    e.preventDefault()
    if (!reason.trim()) {
      setError('Say why the order is being cancelled.')
      return
    }
    act(() => cancelPurchaseOrder(poId, reason.trim()), 'Order cancelled.')
  }

  if (!order) {
    return (
      <div className="psw-detail">
        {error ? <p className="psw-error" role="alert">{error}</p>
          : <p className="psw-note" role="status">Loading the order</p>}
      </div>
    )
  }

  const raisedByMe = me !== null && order.created_by === me
  const beforeApproval = order.status === 'draft' || order.status === 'pending_approval'
  const cancellable = ['draft', 'pending_approval', 'approved', 'issued'].includes(order.status)
  const mayCancel = cancellable && (canApprove || (canWrite && beforeApproval))
  const licence = order.vendor_licence

  return (
    <div className="psw-detail" aria-labelledby="psw-order-title">
      <h3 id="psw-order-title" className="psw-card__title">
        {order.po_number ? `Order ${order.po_number}` : 'Order not numbered yet'}
      </h3>
      {order.po_number && order.number_series_configured === false && (
        <p className="psw-notice psw-notice--warn">
          This number comes from a series the site has not set up yet. Ask the
          administrator to configure the pharmacy order series before orders go out.
        </p>
      )}
      <dl className="psw-facts">
        <div><dt>Status</dt><dd>{PURCHASE_ORDER_STATUS_LABELS[order.status] ?? order.status}</dd></div>
        <div><dt>Vendor</dt><dd>{order.vendor_name ?? order.vendor_code}</dd></div>
        <div>
          <dt>Drug licence</dt>
          <dd>
            {licence ? (
              <span className={`psw-chip ${licenceTone(licence.status)}`}>
                {LICENCE_LABELS[licence.status]}
                {licence.valid_to ? `, to ${formatClinicalDateKey(licence.valid_to)}` : ''}
              </span>
            ) : 'Not on file'}
          </dd>
        </div>
        <div><dt>Deliver to</dt><dd>{order.store_name ?? order.store_code}</dd></div>
        <div><dt>Order date</dt><dd>{formatClinicalDateKey(order.order_date)}</dd></div>
        <div><dt>Raised by</dt><dd>{order.created_by_name ?? order.created_by ?? 'Not recorded'}</dd></div>
        {order.approved_by && (
          <div><dt>Approved by</dt><dd>{order.approved_by_name ?? order.approved_by}</dd></div>
        )}
        {order.cancel_reason && (
          <div><dt>Cancelled because</dt><dd>{order.cancel_reason}</dd></div>
        )}
        <div><dt>Tax</dt><dd>{order.is_interstate ? 'Interstate (IGST)' : 'Within the state (CGST and SGST)'}</dd></div>
      </dl>

      <div className="psw-table-scroll">
        <table className="psw-table">
          <thead>
            <tr>
              <th scope="col">Drug</th>
              <th scope="col" className="psw-num">Packs</th>
              <th scope="col" className="psw-num">Units</th>
              <th scope="col" className="psw-num">Rate per pack</th>
              <th scope="col" className="psw-num">GST</th>
              <th scope="col" className="psw-num">Line total</th>
            </tr>
          </thead>
          <tbody>
            {(order.lines ?? []).map(line => (
              <tr key={line.id}>
                <td>
                  <span className="psw-strong">{line.description ?? line.item_code}</span>
                  <span className="psw-sub">
                    {line.units_per_pack ? `${line.units_per_pack} per ${line.uom ?? 'pack'}` : 'Pack size not recorded'}
                  </span>
                </td>
                <td className="psw-num">{line.quantity_packs}</td>
                <td className="psw-num">{line.quantity_units ?? 'Not recorded'}</td>
                <td className="psw-num">{formatInr(line.unit_rate_paise)}</td>
                <td className="psw-num">{`${line.gst_rate_bp / 100}%`}</td>
                <td className="psw-num">{formatInr(line.total_paise)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <dl className="psw-facts">
        <div><dt>Taxable value</dt><dd>{paise(order.taxable_value_paise)}</dd></div>
        <div><dt>GST</dt><dd>{paise(order.tax_paise)}</dd></div>
        <div><dt>Round off</dt><dd>{paise(order.rounding_adjustment_paise)}</dd></div>
        <div><dt>Total</dt><dd><strong>{paise(order.total_paise)}</strong></dd></div>
      </dl>

      {error && <p className="psw-error" role="alert">{error}</p>}
      {message && <p className="psw-ok" role="status">{message}</p>}

      <div className="psw-buttons">
        {order.status === 'draft' && canWrite && (
          <button type="button" className="psw-btn psw-btn--primary" disabled={busy}
            onClick={() => move('submit', 'Sent for approval.')}>
            Send for approval
          </button>
        )}
        {order.status === 'pending_approval' && canApprove && !raisedByMe && (
          <button type="button" className="psw-btn psw-btn--primary" disabled={busy}
            onClick={() => move('approve', 'Order approved.')}>
            Approve
          </button>
        )}
        {order.status === 'approved' && canApprove && (
          <button type="button" className="psw-btn psw-btn--primary" disabled={busy}
            onClick={() => move('issue', 'Order sent to the vendor.')}>
            Send to vendor
          </button>
        )}
        {mayCancel && !cancelling && (
          <button type="button" className="psw-btn psw-btn--quiet" disabled={busy}
            onClick={() => { setCancelling(true); setError('') }}>
            Cancel order
          </button>
        )}
      </div>
      {order.status === 'pending_approval' && canApprove && raisedByMe && (
        <p className="psw-hint">You raised this order, so someone else approves it.</p>
      )}
      {order.status === 'pending_approval' && !canApprove && (
        <p className="psw-hint">Waiting for a person with approval rights.</p>
      )}
      {cancelling && (
        <form className="psw-form" onSubmit={onCancel}>
          <div className="psw-field psw-field--wide">
            <label htmlFor="psw-cancel-reason">Why is it being cancelled?</label>
            <textarea id="psw-cancel-reason" rows={2} value={reason}
              onChange={e => setReason(e.target.value)} />
          </div>
          <div className="psw-buttons">
            <button type="submit" className="psw-btn" disabled={busy}>Cancel the order</button>
            <button type="button" className="psw-btn psw-btn--quiet" disabled={busy}
              onClick={() => { setCancelling(false); setReason('') }}>
              Keep it
            </button>
          </div>
        </form>
      )}
    </div>
  )
}

// ═════════════════════════════════════════════════════════════════════════════
//  New order
// ═════════════════════════════════════════════════════════════════════════════

function NewOrderSection({ fail, draft, setDraft, onCreated }: {
  fail: Fail
  draft: DraftForm
  setDraft: (next: DraftForm | ((prev: DraftForm) => DraftForm)) => void
  onCreated: (order: PurchaseOrder, warnings: PurchasingWarning[]) => void
}) {
  const [options, setOptions] = useState<PurchasingOptions | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let live = true
    getPurchasingOptions().then(
      res => { if (live) setOptions(res) },
      err => { if (live) setError(fail(err)) },
    )
    return () => { live = false }
  }, [fail])

  const vendor = (options?.vendors ?? []).find(v => v.code === draft.vendorCode) ?? null
  const drugsById = useMemo(
    () => new Map((options?.drugs ?? []).map(d => [d.id, d])),
    [options],
  )

  function patchLine(key: number, patch: Partial<DraftLine>) {
    setDraft(prev => ({ ...prev, lines: prev.lines.map(l => (l.key === key ? { ...l, ...patch } : l)) }))
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault()
    setError('')
    if (!draft.vendorCode) { setError('Choose the vendor.'); return }
    if (!draft.storeCode) { setError('Choose the store the order goes to.'); return }
    const lines = []
    for (const [i, line] of draft.lines.entries()) {
      const n = i + 1
      if (!line.drugId) { setError(`Line ${n}: choose a drug.`); return }
      if (!/^\d+$/.test(line.packs.trim()) || Number(line.packs) < 1) {
        setError(`Line ${n}: packs is a whole number, at least 1.`)
        return
      }
      const rate = rupeesToPaise(line.rate)
      if (rate === null) { setError(`Line ${n}: enter the rate per pack in rupees.`); return }
      lines.push({ helix_medication_id: line.drugId, packs: Number(line.packs.trim()), unit_rate_paise: rate })
    }
    const common = {
      vendor_code: draft.vendorCode,
      store_code: draft.storeCode,
      expected_date: draft.expectedDate || null,
      is_interstate: draft.interstate,
      remarks: draft.remarks.trim() || null,
    }
    setBusy(true)
    const request = draft.fromSuggestions
      ? createPurchaseOrderFromSuggestions({ ...common, items: lines })
      : createPurchaseOrder({ ...common, lines })
    request.then(
      res => onCreated(res.purchase_order, res.warnings),
      err => setError(fail(err)),
    ).finally(() => setBusy(false))
  }

  if (!options) {
    return (
      <section className="psw-card" aria-labelledby="psw-new-title">
        <h2 id="psw-new-title" className="psw-card__title">New purchase order</h2>
        {error ? <p className="psw-error" role="alert">{error}</p>
          : <p className="psw-note" role="status">Loading vendors, stores and drugs</p>}
      </section>
    )
  }

  return (
    <section className="psw-card" aria-labelledby="psw-new-title">
      <h2 id="psw-new-title" className="psw-card__title">New purchase order</h2>
      {draft.fromSuggestions && (
        <p className="psw-notice">
          Started from reorder suggestions. Check the packs and type the rate you agreed
          with the vendor.
        </p>
      )}
      {!options.number_series_configured && (
        <p className="psw-hint">
          The pharmacy order number series is not set up yet; orders get a placeholder
          number when they are sent.
        </p>
      )}
      <form className="psw-form" onSubmit={onSubmit} noValidate>
        <div className="psw-fields">
          <div className="psw-field">
            <label htmlFor="psw-vendor">Vendor</label>
            <select id="psw-vendor" value={draft.vendorCode}
              onChange={e => setDraft(prev => ({ ...prev, vendorCode: e.target.value }))}>
              <option value="">Choose a vendor</option>
              {options.vendors.map(v => (
                <option key={v.code} value={v.code} disabled={!v.orderable}>
                  {v.name}{v.orderable ? '' : ` (${LICENCE_LABELS[v.licence_status].toLowerCase()})`}
                </option>
              ))}
            </select>
            {vendor && (
              <span className={`psw-chip ${licenceTone(vendor.licence_status)}`}>
                {LICENCE_LABELS[vendor.licence_status]}
                {vendor.drug_licence_valid_to ? `, to ${formatClinicalDateKey(vendor.drug_licence_valid_to)}` : ''}
              </span>
            )}
          </div>
          <div className="psw-field">
            <label htmlFor="psw-store">Deliver to</label>
            <select id="psw-store" value={draft.storeCode}
              onChange={e => setDraft(prev => ({ ...prev, storeCode: e.target.value }))}>
              <option value="">Choose a store</option>
              {options.stores.map(s => (
                <option key={s.code} value={s.code}>{s.name} ({s.kind_label})</option>
              ))}
            </select>
          </div>
          <div className="psw-field">
            <label htmlFor="psw-expected">Expected by</label>
            <input id="psw-expected" type="date" value={draft.expectedDate}
              onChange={e => setDraft(prev => ({ ...prev, expectedDate: e.target.value }))} />
          </div>
          <label className="psw-check">
            <input type="checkbox" checked={draft.interstate}
              onChange={e => setDraft(prev => ({ ...prev, interstate: e.target.checked }))} />
            Vendor is in another state (IGST)
          </label>
        </div>
        {options.stores.length === 0 && (
          <p className="psw-notice psw-notice--warn">
            No store can take deliveries yet. Set up the dispensing store, a sub-store or
            the NDPS vault in Masters first.
          </p>
        )}

        <ul className="psw-lines" aria-label="Order lines">
          {draft.lines.map((line, i) => {
            const chosen = drugsById.get(line.drugId)
            return (
              <li key={line.key} className="psw-line">
                <div className="psw-field">
                  <label htmlFor={`psw-drug-${line.key}`}>Drug, line {i + 1}</label>
                  <select id={`psw-drug-${line.key}`} value={line.drugId}
                    onChange={e => patchLine(line.key, { drugId: e.target.value })}>
                    <option value="">Choose a drug</option>
                    {options.drugs.map(d => (
                      <option key={d.id} value={d.id} disabled={!d.orderable}>
                        {d.name}{d.orderable ? '' : ` (${d.problems[0]?.message ?? 'not orderable'})`}
                      </option>
                    ))}
                  </select>
                  {chosen && (
                    <span className="psw-sub">
                      {chosen.pack_size_units ? `${chosen.pack_size_units} per ${chosen.purchase_uom ?? 'pack'}` : ''}
                      {chosen.ndps ? '. Narcotic or psychotropic: order into the NDPS vault.' : ''}
                    </span>
                  )}
                </div>
                <div className="psw-field">
                  <label htmlFor={`psw-packs-${line.key}`}>Packs</label>
                  <input id={`psw-packs-${line.key}`} inputMode="numeric" value={line.packs}
                    onChange={e => patchLine(line.key, { packs: e.target.value })} />
                </div>
                <div className="psw-field">
                  <label htmlFor={`psw-rate-${line.key}`}>Rate per pack, rupees</label>
                  <input id={`psw-rate-${line.key}`} inputMode="decimal" value={line.rate}
                    onChange={e => patchLine(line.key, { rate: e.target.value })} />
                </div>
                <button type="button" className="psw-btn psw-btn--quiet"
                  disabled={draft.lines.length === 1}
                  onClick={() => setDraft(prev => ({ ...prev, lines: prev.lines.filter(l => l.key !== line.key) }))}>
                  Remove
                </button>
              </li>
            )
          })}
        </ul>
        <button type="button" className="psw-btn psw-btn--quiet"
          onClick={() => setDraft(prev => ({ ...prev, lines: [...prev.lines, blankLine()] }))}>
          Add a line
        </button>
        <div className="psw-field psw-field--wide">
          <label htmlFor="psw-remarks">Remarks</label>
          <textarea id="psw-remarks" rows={2} value={draft.remarks}
            onChange={e => setDraft(prev => ({ ...prev, remarks: e.target.value }))} />
        </div>
        {error && <p className="psw-error" role="alert">{error}</p>}
        <div className="psw-buttons">
          <button type="submit" className="psw-btn psw-btn--primary" disabled={busy}>
            Save draft
          </button>
        </div>
      </form>
    </section>
  )
}

// ═════════════════════════════════════════════════════════════════════════════
//  Reorder suggestions
// ═════════════════════════════════════════════════════════════════════════════

function SuggestionsSection({ fail, canWrite, onStart }: {
  fail: Fail
  canWrite: boolean
  onStart: (rows: ReorderSuggestion[]) => void
}) {
  const [rows, setRows] = useState<ReorderSuggestion[] | null>(null)
  const [error, setError] = useState('')
  const [picked, setPicked] = useState<Set<string>>(new Set())

  useEffect(() => {
    let live = true
    getReorderSuggestions().then(
      res => { if (live) { setError(''); setRows(res.suggestions) } },
      err => { if (live) setError(fail(err)) },
    )
    return () => { live = false }
  }, [fail])

  const rowKey = (r: ReorderSuggestion) => `${r.store_id ?? 'none'}:${r.drug_id}`
  const chosen = (rows ?? []).filter(r => picked.has(rowKey(r)))
  const oneStore = new Set(chosen.map(r => r.store_code)).size <= 1

  function toggle(r: ReorderSuggestion) {
    setPicked(prev => {
      const next = new Set(prev)
      const key = rowKey(r)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  return (
    <section className="psw-card" aria-labelledby="psw-sugg-title">
      <h2 id="psw-sugg-title" className="psw-card__title">Reorder suggestions</h2>
      <p className="psw-hint">
        Drugs at or below their reorder level, counting usable stock, stock on its way
        between stores and stock already on order.
      </p>
      {error && <p className="psw-error" role="alert">{error}</p>}
      {rows === null && !error && <p className="psw-note" role="status">Loading suggestions</p>}
      {rows && rows.length === 0 && <p className="psw-note">Nothing needs ordering now.</p>}
      {rows && rows.length > 0 && (
        <div className="psw-table-scroll">
          <table className="psw-table">
            <thead>
              <tr>
                {canWrite && <th scope="col"><span className="psw-visually-hidden">Choose</span></th>}
                <th scope="col">Drug</th>
                <th scope="col">Store</th>
                <th scope="col">Level</th>
                <th scope="col" className="psw-num">Usable</th>
                <th scope="col" className="psw-num">On order</th>
                <th scope="col" className="psw-num">Suggested packs</th>
                <th scope="col" className="psw-num">Last rate</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(r => (
                <tr key={rowKey(r)}>
                  {canWrite && (
                    <td>
                      <label className="psw-pick">
                        <input
                          type="checkbox"
                          aria-label={`Order ${r.drug_name}`}
                          disabled={!r.orderable}
                          checked={picked.has(rowKey(r))}
                          onChange={() => toggle(r)}
                        />
                      </label>
                    </td>
                  )}
                  <td>
                    <span className="psw-strong">{r.drug_name}</span>
                    {!r.orderable && (
                      <span className="psw-sub psw-chip--danger">
                        {r.problems.map(p => p.message).join('. ')}
                      </span>
                    )}
                    {r.note && r.orderable && <span className="psw-sub">{r.note}</span>}
                  </td>
                  <td>{r.store_label}</td>
                  <td>{r.level_label}</td>
                  <td className="psw-num">{r.stock_units}</td>
                  <td className="psw-num">{r.on_order_units + r.in_transit_units}</td>
                  <td className="psw-num">{r.suggested_packs ?? 'Not set'}</td>
                  <td className="psw-num">
                    {r.last_rate_paise === null ? 'Never ordered' : formatInr(r.last_rate_paise)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {canWrite && rows && rows.length > 0 && (
        <div className="psw-buttons">
          <button
            type="button"
            className="psw-btn psw-btn--primary"
            disabled={chosen.length === 0 || !oneStore}
            onClick={() => onStart(chosen)}
          >
            Start an order from these
          </button>
          {!oneStore && <span className="psw-hint">Choose drugs for one store at a time.</span>}
        </div>
      )}
    </section>
  )
}
