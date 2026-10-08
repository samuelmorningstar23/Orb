/**
 * Pharmacy store, Receipts (grn-8): a delivery from a vendor, recorded as a
 * draft against the order it answers, then posted to the shelf.
 *
 * Discovered by the panel through ./viewRegistry.ts; shown to anyone holding
 * pharmacy_store.read. Recording a delivery and posting it need the write
 * tier. A batch with less shelf life left than the hospital asks for is posted
 * only by a person with approval rights who did not receive it, with a reason
 * (GRN-R16). An expired batch is never received. The server refuses anyone
 * else whatever this screen shows, and every refusal is shown in its own words.
 *
 * THE EXPIRY IS TYPED TWICE (ruling GRN-RS6 option (b), held for Samuel). The
 * second box is never filled from the first and refuses a paste, so a slip of
 * the finger on one of them is caught before an expired batch can reach the
 * shelf. The server compares the two as well.
 *
 * THE SERVER DECIDES. The hospital's date, the minimum shelf life, the batch
 * and MRP checks, over-receipt and the NDPS vault rule are the server's. The
 * hints on the form (days left, short shelf life, a rate above the MRP) are
 * worked out from the date and the minimum the server sent with the order
 * list, and the server checks them again on save and on post.
 *
 * No patient data is read or shown. Expiry, invoice and manufacturing dates
 * are calendar keys (formatClinicalDateKey); received and posted times are
 * stored UTC instants (formatClinicalDateTime). Money is integer paise, typed
 * and shown in rupees through src/lib/money.
 */
import { useCallback, useEffect, useState, type FormEvent } from 'react'

import { getUser } from '../../../api/client'
import { toPharmacyStoreError } from '../../../api/pharmacyStore'
import {
  GOODS_RECEIPT_STATUS_LABELS,
  cancelGoodsReceipt,
  createGoodsReceipt,
  getGoodsReceipt,
  getReceivableOrders,
  listGoodsReceipts,
  newReceiptKey,
  postGoodsReceipt,
  type GoodsReceipt,
  type GoodsReceiptStatus,
  type Receivable,
  type ReceivableLine,
  type ReceivableOrder,
  type ReceiptLineInput,
  type ReceiptResult,
  type ReceiptWarning,
} from '../../../api/pharmacyStore/receipts'
import { formatClinicalDateKey, formatClinicalDateTime } from '../../../lib/datetime'
import { formatInr, toPaise } from '../../../lib/money'
import { PHARMACY_STORE_APPROVE, PHARMACY_STORE_WRITE, hasPermission } from '../../../lib/roles'
import type { PharmacyStoreViewProps } from './viewRegistry'
import './stockAndPurchasing.css'

export const viewId = 'receipts'
export const viewLabel = 'Receipts'
export const viewOrder = 30
export const viewPermission = 'pharmacy_store.read'

type Section = 'receipts' | 'new'

type Fail = (err: unknown) => string

function useFail(onModuleOff: () => void): Fail {
  return useCallback((err: unknown) => {
    const failure = toPharmacyStoreError(err)
    if (failure.isModuleDisabled) onModuleOff()
    return failure.message || 'Something went wrong. Nothing was saved.'
  }, [onModuleOff])
}

const STATUS_FILTERS: { value: GoodsReceiptStatus | ''; label: string }[] = [
  { value: '', label: 'All' },
  { value: 'draft', label: 'Drafts' },
  { value: 'posted', label: 'Posted' },
  { value: 'cancelled', label: 'Cancelled' },
]

function statusTone(status: GoodsReceiptStatus): string {
  if (status === 'cancelled') return 'psw-chip--danger'
  if (status === 'draft') return 'psw-chip--warn'
  if (status === 'posted') return 'psw-chip--ok'
  return ''
}

function statusLabel(status: GoodsReceiptStatus): string {
  return GOODS_RECEIPT_STATUS_LABELS[status] ?? status
}

function paise(value: number | null | undefined, unset = 'Not recorded'): string {
  return value === null || value === undefined ? unset : formatInr(value)
}

/** Rupees as typed ("45" or "45.50") to whole paise; null when blank, 'bad'
 *  when unreadable or negative. */
function rupees(text: string): number | null | 'bad' {
  const t = text.trim()
  if (!t) return null
  try {
    const value = toPaise(t)
    return value >= 0 ? value : 'bad'
  } catch {
    return 'bad'
  }
}

/** A whole number typed in a box: 0 when blank, null when unreadable. */
function whole(text: string): number | null {
  const t = text.trim()
  if (!t) return 0
  return /^\d+$/.test(t) ? Number(t) : null
}

/** Whole days from one calendar key to another (both YYYY-MM-DD), or null.
 *  Calendar arithmetic only; nothing here is shown as a date. */
function daysBetween(fromKey: string, toKey: string): number | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(fromKey) || !/^\d{4}-\d{2}-\d{2}$/.test(toKey)) return null
  const a = Date.parse(`${fromKey}T00:00:00Z`)
  const b = Date.parse(`${toKey}T00:00:00Z`)
  if (Number.isNaN(a) || Number.isNaN(b)) return null
  return Math.round((b - a) / 86_400_000)
}

function dayWord(n: number): string {
  return n === 1 ? 'day' : 'days'
}

function resultMessage(done: string, warnings: ReceiptWarning[]): string {
  return [done, ...warnings.map(w => w.message)].join(' ')
}

export default function ReceiptsView({ onModuleOff }: PharmacyStoreViewProps) {
  const [section, setSection] = useState<Section>('receipts')
  const [openId, setOpenId] = useState<number | null>(null)
  const [notice, setNotice] = useState('')
  const fail = useFail(onModuleOff)
  const canWrite = hasPermission(PHARMACY_STORE_WRITE)
  const canApprove = hasPermission(PHARMACY_STORE_APPROVE)

  function onCreated(res: ReceiptResult) {
    setOpenId(res.goods_receipt.id)
    setNotice(resultMessage(
      res.replayed
        ? 'This delivery was already recorded. Here is the draft it made.'
        : 'Draft saved. Nothing is on the shelf until it is posted.',
      res.warnings))
    setSection('receipts')
  }

  const sections: { id: Section; label: string }[] = [
    { id: 'receipts', label: 'Receipts' },
    ...(canWrite ? [{ id: 'new' as Section, label: 'Record a delivery' }] : []),
  ]

  return (
    <div className="psw-view">
      <nav className="psw-sections" aria-label="Receipts sections">
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
      {section === 'receipts' && (
        <ReceiptsSection
          fail={fail}
          canWrite={canWrite}
          canApprove={canApprove}
          openId={openId}
          setOpenId={setOpenId}
        />
      )}
      {section === 'new' && canWrite && <NewReceiptSection fail={fail} onCreated={onCreated} />}
    </div>
  )
}

// ═════════════════════════════════════════════════════════════════════════════
//  Receipts
// ═════════════════════════════════════════════════════════════════════════════

function ReceiptsSection({ fail, canWrite, canApprove, openId, setOpenId }: {
  fail: Fail
  canWrite: boolean
  canApprove: boolean
  openId: number | null
  setOpenId: (id: number | null) => void
}) {
  const [status, setStatus] = useState<GoodsReceiptStatus | ''>('')
  const [receipts, setReceipts] = useState<GoodsReceipt[] | null>(null)
  const [error, setError] = useState('')

  const load = useCallback(() => {
    listGoodsReceipts({ status }).then(
      res => { setError(''); setReceipts(res.goods_receipts) },
      err => setError(fail(err)),
    )
  }, [fail, status])

  useEffect(() => { load() }, [load])

  return (
    <section className="psw-card" aria-labelledby="psw-receipts-title">
      <div className="psw-card__head">
        <h2 id="psw-receipts-title" className="psw-card__title">Goods receipts</h2>
        <div className="psw-toolbar">
          <label htmlFor="psw-receipt-status" className="psw-visually-hidden">Show receipts</label>
          <select
            id="psw-receipt-status"
            value={status}
            onChange={e => setStatus(e.target.value as GoodsReceiptStatus | '')}
          >
            {STATUS_FILTERS.map(f => <option key={f.value} value={f.value}>{f.label}</option>)}
          </select>
        </div>
      </div>
      {error && <p className="psw-error" role="alert">{error}</p>}
      {receipts === null && !error && <p className="psw-note" role="status">Loading receipts</p>}
      {receipts && receipts.length === 0 && <p className="psw-note">No goods receipts here yet.</p>}
      {receipts && receipts.length > 0 && (
        <div className="psw-table-scroll">
          <table className="psw-table">
            <thead>
              <tr>
                <th scope="col">Receipt</th>
                <th scope="col">Order</th>
                <th scope="col">Vendor</th>
                <th scope="col">Store</th>
                <th scope="col">Status</th>
                <th scope="col" className="psw-num">Total</th>
                <th scope="col"><span className="psw-visually-hidden">Actions</span></th>
              </tr>
            </thead>
            <tbody>
              {receipts.map(r => (
                <tr key={r.id} className={r.id === openId ? 'psw-row--open' : undefined}>
                  <td>
                    <span className="psw-strong">{r.grn_number ?? 'Not numbered yet'}</span>
                    <span className="psw-sub">
                      {r.received_at ? formatClinicalDateTime(r.received_at) : 'Not recorded'}
                    </span>
                  </td>
                  <td>{r.po_number ?? 'Not numbered yet'}</td>
                  <td>{r.vendor_name ?? 'Not recorded'}</td>
                  <td>{r.store_code ?? 'Not recorded'}</td>
                  <td>
                    <span className={`psw-chip ${statusTone(r.status)}`}>{statusLabel(r.status)}</span>
                  </td>
                  <td className="psw-num">{formatInr(r.total_paise)}</td>
                  <td className="psw-actions-cell">
                    <button
                      type="button"
                      className="psw-btn psw-btn--quiet"
                      aria-expanded={r.id === openId}
                      onClick={() => setOpenId(r.id === openId ? null : r.id)}
                    >
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
        <ReceiptDetail
          key={openId}
          grnId={openId}
          fail={fail}
          canWrite={canWrite}
          canApprove={canApprove}
          onChanged={load}
        />
      )}
    </section>
  )
}

function ReceiptDetail({ grnId, fail, canWrite, canApprove, onChanged }: {
  grnId: number
  fail: Fail
  canWrite: boolean
  canApprove: boolean
  onChanged: () => void
}) {
  const [doc, setDoc] = useState<GoodsReceipt | null>(null)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const [cancelling, setCancelling] = useState(false)
  const [cancelReason, setCancelReason] = useState('')
  const [shortReason, setShortReason] = useState('')
  const me = getUser()?.id ?? null

  useEffect(() => {
    let live = true
    getGoodsReceipt(grnId).then(
      res => { if (live) { setError(''); setDoc(res.goods_receipt) } },
      err => { if (live) setError(fail(err)) },
    )
    return () => { live = false }
  }, [grnId, fail])

  function act(run: () => Promise<ReceiptResult>, done: (res: ReceiptResult) => string) {
    setBusy(true)
    setError('')
    setMessage('')
    run().then(
      res => {
        setDoc(res.goods_receipt)
        setMessage(resultMessage(done(res), res.warnings))
        setCancelling(false)
        setCancelReason('')
        setShortReason('')
        onChanged()
      },
      err => setError(fail(err)),
    ).finally(() => setBusy(false))
  }

  function onPost() {
    act(() => postGoodsReceipt(grnId),
      res => (res.replayed ? 'This receipt was already posted. Nothing moved again.'
        : 'Posted to stock.'))
  }

  function onAcceptShort(e: FormEvent) {
    e.preventDefault()
    if (!shortReason.trim()) {
      setError('Give the reason for accepting stock with short expiry.')
      return
    }
    act(() => postGoodsReceipt(grnId, shortReason.trim()),
      res => (res.replayed ? 'This receipt was already posted. Nothing moved again.'
        : 'Short expiry accepted and posted to stock.'))
  }

  function onCancel(e: FormEvent) {
    e.preventDefault()
    if (!cancelReason.trim()) {
      setError('Say why the draft is being cancelled.')
      return
    }
    act(() => cancelGoodsReceipt(grnId, cancelReason.trim()), () => 'Draft cancelled.')
  }

  if (!doc) {
    return (
      <div className="psw-detail">
        {error ? <p className="psw-error" role="alert">{error}</p>
          : <p className="psw-note" role="status">Loading the receipt</p>}
      </div>
    )
  }

  const isDraft = doc.status === 'draft'
  const receivedByMe = me !== null && doc.received_by === me
  const minDays = doc.min_shelf_life_days ?? 0
  const expired = isDraft && doc.has_expired_line === true
  const needsApproval = isDraft && !expired && doc.needs_approval === true
  const invoiceDiffers = (doc.warnings ?? []).some(w => w.code === 'invoice_total_mismatch')

  return (
    <div className="psw-detail" aria-labelledby="psw-receipt-title">
      <h3 id="psw-receipt-title" className="psw-card__title">
        {doc.grn_number ? `Receipt ${doc.grn_number}` : 'Receipt not numbered yet'}
      </h3>
      {doc.grn_number && doc.number_series_configured === false && (
        <p className="psw-notice psw-notice--warn">
          This number comes from a series the site has not set up yet. Ask the
          administrator to configure the pharmacy receipt series.
        </p>
      )}
      <dl className="psw-facts">
        <div><dt>Status</dt><dd>{statusLabel(doc.status)}</dd></div>
        <div><dt>Order</dt><dd>{doc.po_number ?? 'Not numbered yet'}</dd></div>
        <div><dt>Vendor</dt><dd>{doc.vendor_name ?? doc.vendor_code ?? 'Not recorded'}</dd></div>
        <div><dt>Received into</dt><dd>{doc.store_name ?? doc.store_code ?? 'Not recorded'}</dd></div>
        <div>
          <dt>Vendor invoice</dt>
          <dd>
            {doc.vendor_invoice_number ?? 'Not recorded'}
            {doc.vendor_invoice_date ? `, ${formatClinicalDateKey(doc.vendor_invoice_date)}` : ''}
          </dd>
        </div>
        <div>
          <dt>Invoice total</dt>
          <dd>{paise(doc.vendor_invoice_total_paise, 'Not given')}</dd>
        </div>
        <div>
          <dt>Received by</dt>
          <dd>
            {doc.received_by_name ?? doc.received_by ?? 'Not recorded'}
            {doc.received_at ? `, ${formatClinicalDateTime(doc.received_at)}` : ''}
          </dd>
        </div>
        {doc.posted_at && (
          <div>
            <dt>Posted by</dt>
            <dd>{doc.posted_by_name ?? doc.posted_by ?? 'Not recorded'}, {formatClinicalDateTime(doc.posted_at)}</dd>
          </div>
        )}
        {doc.cancel_reason && (
          <div><dt>Cancelled because</dt><dd>{doc.cancel_reason}</dd></div>
        )}
      </dl>

      <div className="psw-table-scroll">
        <table className="psw-table">
          <thead>
            <tr>
              <th scope="col">Drug</th>
              <th scope="col">Batch</th>
              <th scope="col">Expiry</th>
              <th scope="col" className="psw-num">Packs</th>
              <th scope="col" className="psw-num">Units</th>
              <th scope="col" className="psw-num">MRP per pack</th>
              <th scope="col" className="psw-num">Rate per pack</th>
              <th scope="col" className="psw-num">Cost per unit</th>
              <th scope="col" className="psw-num">Line total</th>
            </tr>
          </thead>
          <tbody>
            {(doc.lines ?? []).map(line => (
              <tr key={line.id}>
                <td>
                  <span className="psw-strong">{line.description ?? line.item_code}</span>
                  <span className="psw-sub">
                    {line.units_per_pack ? `${line.units_per_pack} per ${line.uom ?? 'pack'}` : 'Pack size not recorded'}
                  </span>
                  {line.rejected_packs > 0 && (
                    <span className="psw-sub">
                      {line.rejected_packs} rejected{line.rejection_reason ? `: ${line.rejection_reason}` : ''}
                    </span>
                  )}
                </td>
                <td>{line.batch_no ?? 'None'}</td>
                <td>
                  {line.expiry_date ? formatClinicalDateKey(line.expiry_date) : 'None'}
                  {isDraft && line.days_to_expiry !== null && (
                    <span className={`psw-sub${line.expired || line.short_dated ? ' psw-chip--danger' : ''}`}>
                      {line.expired ? 'Expired'
                        : `${line.days_to_expiry} ${dayWord(line.days_to_expiry)} left`}
                    </span>
                  )}
                </td>
                <td className="psw-num">
                  {line.packs}
                  {line.free_packs > 0 && <span className="psw-sub">and {line.free_packs} free</span>}
                </td>
                <td className="psw-num">{line.units}</td>
                <td className="psw-num">{paise(line.mrp_pack_paise)}</td>
                <td className="psw-num">
                  {formatInr(line.unit_rate_paise)}
                  {line.rate_above_mrp && <span className="psw-sub psw-chip--warn">Above the MRP</span>}
                </td>
                <td className="psw-num">{paise(line.unit_cost_paise)}</td>
                <td className="psw-num">{formatInr(line.total_paise)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <dl className="psw-facts">
        <div><dt>Taxable value</dt><dd>{paise(doc.taxable_value_paise)}</dd></div>
        <div><dt>GST</dt><dd>{paise(doc.tax_paise)}</dd></div>
        <div><dt>Receipt total</dt><dd><strong>{paise(doc.total_paise)}</strong></dd></div>
      </dl>
      {isDraft && (doc.warnings ?? []).length > 0 && (
        <ul className="psw-notice psw-notice--warn" aria-label="Warnings">
          {(doc.warnings ?? []).map((w, i) => <li key={`${w.code}-${i}`}>{w.message}</li>)}
        </ul>
      )}
      {!isDraft && invoiceDiffers && (
        <p className="psw-hint">The vendor's invoice total differs from this receipt's total.</p>
      )}
      {expired && (
        <p className="psw-notice psw-notice--warn">
          A batch on this receipt has expired since it was recorded. Cancel the draft and
          record the delivery again without it.
        </p>
      )}

      {error && <p className="psw-error" role="alert">{error}</p>}
      {message && <p className="psw-ok" role="status">{message}</p>}

      {isDraft && !expired && !needsApproval && (
        <div className="psw-buttons">
          {canWrite ? (
            <button type="button" className="psw-btn psw-btn--primary" disabled={busy} onClick={onPost}>
              Post to stock
            </button>
          ) : (
            <p className="psw-hint">A pharmacist posts this receipt to stock.</p>
          )}
        </div>
      )}
      {needsApproval && canApprove && !receivedByMe && (
        <form className="psw-form" onSubmit={onAcceptShort} aria-label="Accept short expiry">
          <p className="psw-hint">
            A batch on this receipt has less than the {minDays} {dayWord(minDays)} of shelf life
            the hospital asks for. Accept it only with a reason, which is kept on the record.
          </p>
          <div className="psw-field psw-field--wide">
            <label htmlFor="psw-short-reason">Why is this stock accepted?</label>
            <textarea id="psw-short-reason" rows={2} value={shortReason}
              onChange={e => setShortReason(e.target.value)} />
          </div>
          <div className="psw-buttons">
            <button type="submit" className="psw-btn psw-btn--primary" disabled={busy}>
              Accept short expiry and post
            </button>
          </div>
        </form>
      )}
      {needsApproval && canApprove && receivedByMe && (
        <p className="psw-hint">
          You received this delivery, so someone else with approval rights accepts its short expiry.
        </p>
      )}
      {needsApproval && !canApprove && (
        <p className="psw-hint">
          A batch on this receipt has less than the {minDays} {dayWord(minDays)} of shelf life the
          hospital asks for. A person with approval rights must accept it, with a reason, before
          it is posted.
        </p>
      )}

      {isDraft && canWrite && !cancelling && (
        <div className="psw-buttons">
          <button type="button" className="psw-btn psw-btn--quiet" disabled={busy}
            onClick={() => { setCancelling(true); setError('') }}>
            Cancel draft
          </button>
        </div>
      )}
      {cancelling && (
        <form className="psw-form" onSubmit={onCancel} aria-label="Cancel the draft">
          <div className="psw-field psw-field--wide">
            <label htmlFor="psw-receipt-cancel-reason">Why is the draft being cancelled?</label>
            <textarea id="psw-receipt-cancel-reason" rows={2} value={cancelReason}
              onChange={e => setCancelReason(e.target.value)} />
          </div>
          <div className="psw-buttons">
            <button type="submit" className="psw-btn" disabled={busy}>Cancel the draft</button>
            <button type="button" className="psw-btn psw-btn--quiet" disabled={busy}
              onClick={() => { setCancelling(false); setCancelReason('') }}>
              Keep it
            </button>
          </div>
        </form>
      )}
    </div>
  )
}

// ═════════════════════════════════════════════════════════════════════════════
//  Record a delivery
// ═════════════════════════════════════════════════════════════════════════════

interface LineForm {
  packs: string
  freePacks: string
  rejectedPacks: string
  rejectionReason: string
  batch: string
  expiry: string
  expiryAgain: string
  mrp: string
  mfg: string
}

function blankLine(): LineForm {
  return { packs: '', freePacks: '', rejectedPacks: '', rejectionReason: '', batch: '',
    expiry: '', expiryAgain: '', mrp: '', mfg: '' }
}

interface ReceiptForm {
  invoiceNumber: string
  invoiceDate: string
  invoiceTotal: string
  remarks: string
  lines: Record<number, LineForm>
}

function blankForm(order: ReceivableOrder | null): ReceiptForm {
  const lines: Record<number, LineForm> = {}
  for (const line of order?.lines ?? []) lines[line.line_no] = blankLine()
  return { invoiceNumber: '', invoiceDate: '', invoiceTotal: '', remarks: '', lines }
}

/** The hints a line shows as it is typed: days left on the hospital's date,
 *  short shelf life, and a rate above the MRP. The server decides again. */
function lineHints(line: ReceivableLine, form: LineForm, today: string, minDays: number): {
  expired: boolean
  short: boolean
  daysLeft: number | null
  rateAboveMrp: boolean
} {
  const daysLeft = form.expiry ? daysBetween(today, form.expiry) : null
  const mrp = rupees(form.mrp)
  return {
    expired: daysLeft !== null && daysLeft < 0,
    short: daysLeft !== null && daysLeft >= 0 && daysLeft < minDays,
    daysLeft,
    rateAboveMrp: typeof mrp === 'number' && line.unit_rate_paise !== null
      && line.unit_rate_paise > mrp,
  }
}

function NewReceiptSection({ fail, onCreated }: {
  fail: Fail
  onCreated: (res: ReceiptResult) => void
}) {
  const [receivable, setReceivable] = useState<Receivable | null>(null)
  const [orderId, setOrderId] = useState<number | null>(null)
  const [form, setForm] = useState<ReceiptForm>(() => blankForm(null))
  // One key per delivery being recorded: a retried save after a lost reply
  // answers with the draft the first save made, never a second draft.
  const [requestKey, setRequestKey] = useState<string>(newReceiptKey)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let live = true
    getReceivableOrders().then(
      res => { if (live) { setError(''); setReceivable(res) } },
      err => { if (live) setError(fail(err)) },
    )
    return () => { live = false }
  }, [fail])

  const order = (receivable?.orders ?? []).find(o => o.id === orderId) ?? null

  function chooseOrder(value: string) {
    const id = value ? Number(value) : null
    const next = (receivable?.orders ?? []).find(o => o.id === id) ?? null
    setOrderId(next ? next.id : null)
    setForm(blankForm(next))
    setRequestKey(newReceiptKey())
    setError('')
  }

  function patchLine(lineNo: number, patch: Partial<LineForm>) {
    setForm(prev => ({ ...prev, lines: { ...prev.lines, [lineNo]: { ...prev.lines[lineNo], ...patch } } }))
    setError('')
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault()
    setError('')
    if (!order || !receivable) { setError('Choose the order this delivery is against.'); return }
    if (!form.invoiceNumber.trim()) { setError("Enter the vendor's invoice number."); return }
    if (!form.invoiceDate) { setError("Enter the vendor's invoice date."); return }
    const invoiceTotal = rupees(form.invoiceTotal)
    if (invoiceTotal === 'bad') { setError('Enter the invoice total in rupees, or leave it blank.'); return }

    const lines: ReceiptLineInput[] = []
    for (const line of order.lines) {
      const f = form.lines[line.line_no] ?? blankLine()
      const name = line.description ?? `Line ${line.line_no}`
      const packs = whole(f.packs)
      const free = whole(f.freePacks)
      const rejected = whole(f.rejectedPacks)
      if (packs === null || free === null || rejected === null) {
        setError(`${name}: packs are whole numbers.`)
        return
      }
      if (packs + free + rejected === 0) continue
      if (free > 0 && packs === 0) {
        setError(`${name}: free packs come with paid packs on the same line.`)
        return
      }
      if (packs + rejected > line.outstanding_packs) {
        setError(`${name}: ${packs + rejected} packs is more than the ${line.outstanding_packs} still to come on the order.`)
        return
      }
      if (rejected > 0 && !f.rejectionReason.trim()) {
        setError(`${name}: say why packs were rejected.`)
        return
      }
      const input: ReceiptLineInput = { po_line_no: line.line_no, packs, free_packs: free,
        rejected_packs: rejected }
      if (rejected > 0) input.rejection_reason = f.rejectionReason.trim()
      if (packs > 0) {
        if (!f.batch.trim()) { setError(`${name}: enter the batch number on the pack.`); return }
        if (!f.expiry || !f.expiryAgain) {
          setError(`${name}: enter the expiry date, and type it again in the second box.`)
          return
        }
        if (f.expiry !== f.expiryAgain) {
          setError(`${name}: the two expiry dates do not match. Check the pack and type the expiry again.`)
          return
        }
        const mrp = rupees(f.mrp)
        if (mrp === null || mrp === 'bad' || mrp === 0) {
          setError(`${name}: enter the MRP printed on the pack, in rupees.`)
          return
        }
        input.batch_no = f.batch.trim()
        input.expiry_date = f.expiry
        input.expiry_date_confirm = f.expiryAgain
        input.mrp_pack_paise = mrp
        if (f.mfg) input.mfg_date = f.mfg
      }
      lines.push(input)
    }
    if (lines.length === 0) { setError('Enter what arrived on at least one line.'); return }

    setBusy(true)
    createGoodsReceipt({
      purchase_order_id: order.id,
      lines,
      vendor_invoice_number: form.invoiceNumber.trim(),
      vendor_invoice_date: form.invoiceDate,
      vendor_invoice_total_paise: invoiceTotal,
      remarks: form.remarks.trim() || null,
    }, requestKey).then(
      res => {
        setRequestKey(newReceiptKey())
        setOrderId(null)
        setForm(blankForm(null))
        onCreated(res)
      },
      err => setError(fail(err)),
    ).finally(() => setBusy(false))
  }

  if (!receivable) {
    return (
      <section className="psw-card" aria-labelledby="psw-delivery-title">
        <h2 id="psw-delivery-title" className="psw-card__title">Record a delivery</h2>
        {error ? <p className="psw-error" role="alert">{error}</p>
          : <p className="psw-note" role="status">Loading orders with packs still to come</p>}
      </section>
    )
  }

  const minDays = receivable.min_shelf_life_days

  return (
    <section className="psw-card" aria-labelledby="psw-delivery-title">
      <h2 id="psw-delivery-title" className="psw-card__title">Record a delivery</h2>
      <p className="psw-hint">
        A delivery is saved as a draft and nothing reaches the shelf until it is posted. Type
        each expiry date twice from the pack. Batches with less than {minDays} {dayWord(minDays)} of
        shelf life left need a person with approval rights to accept them.
      </p>
      {receivable.orders.length === 0 && (
        <p className="psw-note">No order sent to a vendor has packs still to come.</p>
      )}
      {receivable.orders.length > 0 && (
        <form className="psw-form" onSubmit={onSubmit} noValidate>
          <div className="psw-fields">
            <div className="psw-field">
              <label htmlFor="psw-grn-order">Order</label>
              <select id="psw-grn-order" value={orderId ?? ''} onChange={e => chooseOrder(e.target.value)}>
                <option value="">Choose an order</option>
                {receivable.orders.map(o => (
                  <option key={o.id} value={o.id}>
                    {o.po_number ?? 'Not numbered yet'}, {o.vendor_name ?? o.vendor_code ?? 'vendor not recorded'}
                    {o.order_date ? `, ${formatClinicalDateKey(o.order_date)}` : ''}
                  </option>
                ))}
              </select>
              {order && <span className="psw-sub">Into {order.store_name ?? order.store_code}</span>}
            </div>
          </div>
          {order && (
            <>
              <div className="psw-fields">
                <div className="psw-field">
                  <label htmlFor="psw-grn-invoice">Vendor invoice number</label>
                  <input id="psw-grn-invoice" value={form.invoiceNumber}
                    onChange={e => setForm(prev => ({ ...prev, invoiceNumber: e.target.value }))} />
                </div>
                <div className="psw-field">
                  <label htmlFor="psw-grn-invoice-date">Invoice date</label>
                  <input id="psw-grn-invoice-date" type="date" value={form.invoiceDate}
                    onChange={e => setForm(prev => ({ ...prev, invoiceDate: e.target.value }))} />
                </div>
                <div className="psw-field">
                  <label htmlFor="psw-grn-invoice-total">Invoice total, rupees</label>
                  <input id="psw-grn-invoice-total" inputMode="decimal" value={form.invoiceTotal}
                    onChange={e => setForm(prev => ({ ...prev, invoiceTotal: e.target.value }))} />
                </div>
              </div>
              <ul className="psw-lines" aria-label="Delivery lines">
                {order.lines.map(line => (
                  <DeliveryLine
                    key={line.line_no}
                    line={line}
                    form={form.lines[line.line_no] ?? blankLine()}
                    today={receivable.today}
                    minDays={minDays}
                    onChange={patch => patchLine(line.line_no, patch)}
                  />
                ))}
              </ul>
              <div className="psw-field psw-field--wide">
                <label htmlFor="psw-grn-remarks">Remarks</label>
                <textarea id="psw-grn-remarks" rows={2} value={form.remarks}
                  onChange={e => setForm(prev => ({ ...prev, remarks: e.target.value }))} />
              </div>
            </>
          )}
          {error && <p className="psw-error" role="alert">{error}</p>}
          {order && (
            <div className="psw-buttons">
              <button type="submit" className="psw-btn psw-btn--primary" disabled={busy}>
                Save draft
              </button>
            </div>
          )}
        </form>
      )}
    </section>
  )
}

function DeliveryLine({ line, form, today, minDays, onChange }: {
  line: ReceivableLine
  form: LineForm
  today: string
  minDays: number
  onChange: (patch: Partial<LineForm>) => void
}) {
  const id = `psw-grn-${line.line_no}`
  const hints = lineHints(line, form, today, minDays)
  const name = line.description ?? `Line ${line.line_no}`
  return (
    <li className="psw-card" aria-label={name}>
      <div>
        <span className="psw-strong">{name}</span>
        <span className="psw-sub">
          {line.outstanding_packs} of {line.ordered_packs} {line.uom ?? 'packs'} still to come
          {line.units_per_pack ? `, ${line.units_per_pack} per ${line.uom ?? 'pack'}` : ''}
          {line.unit_rate_paise !== null ? `, ordered at ${formatInr(line.unit_rate_paise)} per pack` : ''}
        </span>
        {line.ndps && (
          <span className="psw-sub">Narcotic or psychotropic: received only into the NDPS vault.</span>
        )}
      </div>
      <div className="psw-fields">
        <div className="psw-field">
          <label htmlFor={`${id}-packs`}>Packs received</label>
          <input id={`${id}-packs`} inputMode="numeric" value={form.packs}
            onChange={e => onChange({ packs: e.target.value })} />
        </div>
        <div className="psw-field">
          <label htmlFor={`${id}-free`}>Free packs</label>
          <input id={`${id}-free`} inputMode="numeric" value={form.freePacks}
            onChange={e => onChange({ freePacks: e.target.value })} />
        </div>
        <div className="psw-field">
          <label htmlFor={`${id}-rejected`}>Packs rejected</label>
          <input id={`${id}-rejected`} inputMode="numeric" value={form.rejectedPacks}
            onChange={e => onChange({ rejectedPacks: e.target.value })} />
        </div>
        {whole(form.rejectedPacks) !== 0 && (
          <div className="psw-field psw-field--wide">
            <label htmlFor={`${id}-rejection`}>Why were packs rejected?</label>
            <input id={`${id}-rejection`} value={form.rejectionReason}
              onChange={e => onChange({ rejectionReason: e.target.value })} />
          </div>
        )}
        <div className="psw-field">
          <label htmlFor={`${id}-batch`}>Batch number</label>
          <input id={`${id}-batch`} value={form.batch} autoComplete="off"
            onChange={e => onChange({ batch: e.target.value })} />
        </div>
        <div className="psw-field">
          <label htmlFor={`${id}-expiry`}>Expiry date</label>
          <input id={`${id}-expiry`} type="date" value={form.expiry} autoComplete="off"
            onChange={e => onChange({ expiry: e.target.value })} />
        </div>
        <div className="psw-field">
          <label htmlFor={`${id}-expiry-again`}>Expiry date, typed again</label>
          <input id={`${id}-expiry-again`} type="date" value={form.expiryAgain} autoComplete="off"
            onPaste={e => e.preventDefault()}
            onDrop={e => e.preventDefault()}
            onChange={e => onChange({ expiryAgain: e.target.value })} />
          <span className="psw-sub">Type it from the pack; it cannot be pasted.</span>
        </div>
        <div className="psw-field">
          <label htmlFor={`${id}-mrp`}>MRP per pack, rupees</label>
          <input id={`${id}-mrp`} inputMode="decimal" value={form.mrp}
            onChange={e => onChange({ mrp: e.target.value })} />
        </div>
        <div className="psw-field">
          <label htmlFor={`${id}-mfg`}>Manufacturing date, if printed</label>
          <input id={`${id}-mfg`} type="date" value={form.mfg}
            onChange={e => onChange({ mfg: e.target.value })} />
        </div>
      </div>
      {form.expiry && form.expiryAgain && form.expiry !== form.expiryAgain && (
        <p className="psw-error" role="alert">The two expiry dates do not match. Check the pack.</p>
      )}
      {hints.expired && (
        <p className="psw-notice psw-notice--warn">
          This batch has expired. Expired stock is never received.
        </p>
      )}
      {hints.short && hints.daysLeft !== null && (
        <p className="psw-notice psw-notice--warn">
          {hints.daysLeft} {dayWord(hints.daysLeft)} of shelf life left, under the {minDays} the
          hospital asks for. Posting it needs a person with approval rights, with a reason.
        </p>
      )}
      {hints.rateAboveMrp && (
        <p className="psw-notice psw-notice--warn">The order's rate per pack is above this MRP.</p>
      )}
    </li>
  )
}
