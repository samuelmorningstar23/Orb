/**
 * Orb — Billing.
 *
 * Three screens behind one tab, in the order a billing counter uses them:
 *
 *   BILLS      the invoice worklist: raise, issue, cancel with a reason, credit,
 *              and print a document.
 *   CHARGES    one patient's running bill: what has been captured, what the
 *              clinical record contains that has NOT been captured, and the
 *              discharge advisory. With the server's 'opd' feature on, the
 *              day's outpatients with lines to bill join the picker (opd-14);
 *              "Bill this visit" and the counter form bill them as outpatient
 *              bills, which never take a line recorded for a stay.
 *   PRICE LIST the masters: service items, tariff plans, packages.
 *
 * THREE RULES THIS FILE IS BUILT ON
 * ────────────────────────────────
 *
 * 1. AN UNSET PRICE IS RENDERED AS WORDS. Everything that can be unset goes
 *    through `formatInrOrUnset`, which prints "Not configured". There is no
 *    `?? 0` anywhere in this file, because "₹0.00" is a price — a free item —
 *    and a clerk will bill it.
 *
 * 2. MONEY IS PARSED ONCE, AT THE INPUT, AS TEXT. Every amount the user types
 *    goes through `toPaise()` from src/lib/money.ts (the mirror of the server's
 *    money.py) and travels as integer paise. `parseFloat` appears nowhere: a
 *    rupee amount that becomes a double has already lost.
 *
 * 3. THE DISCHARGE ADVISORY IS A WARNING, AND SAYS SO ON ITS FACE. It renders
 *    the amount, the reasons, and — in the same card, not a tooltip — the
 *    sentence "This does not prevent the discharge." A warning a clinician has
 *    to interpret as a block is a warning that delays care.
 *
 * Design: flat "Quiet Clinical". No opacity for de-emphasis (colour tokens do
 * that job), status FILLS use --status-x with --status-on-fill text, small
 * status TEXT uses --status-x-text, modals portal to document.body, and every
 * amount is rendered in tabular-lining figures so columns line up.
 */
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'
import { useFocusTrap } from '../../hooks/useFocusTrap'
import { useDataChanged } from '../../hooks/useDataChanged'
import { apiFetch } from '../../api/client'
import * as billing from '../../api/billing'
import type {
  BillingConfig, CaptureResult, Charge, DischargeAdvisory, FinancialSummary,
  Invoice, OpPatient, PrintView, ServiceItem, TariffPlan,
} from '../../api/billing'
import { MoneyError, formatInr, formatInrOrUnset, toPaise } from '../../lib/money'
import { formatClinicalDate, formatClinicalDateTime } from '../../lib/datetime'
import { humaniseEnum, plural } from '../../lib/humanise'
import { currentFeatureFlags, subscribeModuleFlags } from '../../shellConfig'
import './BillingPanel.css'

type TabId = 'bills' | 'charges' | 'prices'

interface WardPatient {
  id: string
  name: string
  mrn?: string
  ward?: string | null
  bed?: string | null
  admission_id?: string | null
  /** From the discharged list: no longer on a ward, still something to bill. */
  discharged?: boolean
  final_bill_raised?: boolean
  has_pending_charges?: boolean
  /** From the outpatient counter list (opd-14, the 'opd' feature): a walk-in
   *  with outpatient lines to bill and no stay. */
  outpatient?: boolean
  opVisits?: OpPatient['visits']
}

/** The message an apiFetch rejection carries, without ever showing "[object Object]". */
function errorText(err: unknown): string {
  if (err instanceof Error && err.message) return err.message
  return 'Something went wrong. Nothing was saved.'
}

/** The day a charge belongs to. A bare "YYYY-MM-DD" is already a local day and
 *  is printed as written; a full timestamp is UTC, and slicing its date put an
 *  OPD visit between 00:00 and 05:30 IST on the previous day. */
function chargeDay(raw: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(raw.trim())
  if (m) return formatClinicalDate(new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])))
  return formatClinicalDate(raw) || raw.slice(0, 10)
}

/* ── Small presentational pieces ─────────────────────────────────────────── */

/** Money, always in tabular-lining figures. `null` reads as words, never zero. */
function Money({ paise, unset, className }: {
  paise: number | null | undefined
  unset?: string
  className?: string
}) {
  return (
    <span className={`bill-money${className ? ` ${className}` : ''}`}>
      {formatInrOrUnset(paise, unset)}
    </span>
  )
}

const STATUS_TONE: Record<string, string> = {
  draft: 'neutral',
  issued: 'info',
  partially_paid: 'warn',
  paid: 'ok',
  cancelled: 'muted',
  void: 'muted',
  written_off: 'muted',
}

const STATUS_LABEL: Record<string, string> = {
  draft: 'Draft',
  issued: 'Issued',
  partially_paid: 'Part paid',
  paid: 'Paid',
  cancelled: 'Cancelled',
  void: 'Void',
  written_off: 'Written off',
}

function StatusChip({ status }: { status: string }) {
  return (
    <span className={`bill-chip bill-chip--${STATUS_TONE[status] || 'neutral'}`}>
      {STATUS_LABEL[status] || status}
    </span>
  )
}

/** A dialog. Portalled to <body> so the scrim covers the whole chrome.
 *  `printable` marks the dialog as the print reveal root (src/print.css hides
 *  everything at print time except the named layouts). */
function Modal({ title, onClose, children, wide, printable }: {
  title: string
  onClose: () => void
  children: React.ReactNode
  wide?: boolean
  printable?: boolean
}) {
  const ref = useFocusTrap<HTMLDivElement>(onClose)
  return createPortal(
    <div className="bill-modal-overlay" onClick={onClose}>
      <div
        ref={ref}
        className={`bill-modal${wide ? ' bill-modal--wide' : ''}${printable ? ' bill-print-layout' : ''}`}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onClick={e => e.stopPropagation()}
      >
        <div className="bill-modal__head">
          <h2 className="bill-modal__title">{title}</h2>
          <button type="button" className="bill-btn bill-btn--quiet" onClick={onClose}>
            Close
          </button>
        </div>
        <div className="bill-modal__body">{children}</div>
      </div>
    </div>,
    document.body,
  )
}

/**
 * A rupee input. Parses TEXT to integer paise on every keystroke and shows what
 * it parsed, so a typo is visible before it becomes a charge — never a float.
 */
function MoneyInput({ label, value, onChange, hint, id }: {
  label: string
  value: string
  onChange: (next: string) => void
  hint?: string
  id: string
}) {
  let parsed: number | null = null
  let error: string | null = null
  const trimmed = value.trim()
  if (trimmed) {
    try {
      parsed = toPaise(trimmed)
    } catch (err) {
      error = err instanceof MoneyError ? err.message : 'Not a rupee amount'
    }
  }
  return (
    <label className="bill-field" htmlFor={id}>
      <span className="bill-field__label">{label}</span>
      <input
        id={id}
        className="bill-input"
        inputMode="decimal"
        value={value}
        placeholder="0.00"
        /* The wrapping <label> also holds the live parse hint, so without this
           the field's accessible name would read "Default rate (optional)
           ₹1,234.55" and change on every keystroke. */
        aria-label={label}
        onChange={e => onChange(e.target.value)}
      />
      {error
        ? <span className="bill-field__error" role="alert">{error}</span>
        : <span className="bill-field__hint">
            {parsed === null ? (hint || 'Rupees, e.g. 1,250.50') : formatInr(parsed)}
          </span>}
    </label>
  )
}

/* ── Bills ───────────────────────────────────────────────────────────────── */

function InvoiceDetail({ invoiceId, onClose, onChanged }: {
  invoiceId: number
  onClose: () => void
  onChanged: () => void
}) {
  const [invoice, setInvoice] = useState<Invoice | null>(null)
  const [print, setPrint] = useState<PrintView | null>(null)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const res = await billing.getInvoice(invoiceId)
      setInvoice(res.invoice)
      setError(null)
    } catch (err) {
      setError(errorText(err))
    }
  }, [invoiceId])

  // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
  useEffect(() => { void load() }, [load])

  const act = async (fn: () => Promise<{ invoice: Invoice }>) => {
    setBusy(true)
    setError(null)
    try {
      const res = await fn()
      setInvoice(res.invoice)
      setReason('')
      onChanged()
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  const showPrint = async () => {
    try {
      setPrint(await billing.getPrintView(invoiceId))
    } catch (err) {
      setError(errorText(err))
    }
  }

  if (!invoice) {
    return (
      <Modal title="Invoice" onClose={onClose} wide>
        {error ? <p className="bill-error" role="alert">{error}</p>
               : <p className="bill-muted">Loading the invoice…</p>}
      </Modal>
    )
  }

  const reasonReady = reason.trim().length >= 3
  const canCancel = invoice.status === 'draft' || invoice.status === 'issued'
  const canCredit = ['issued', 'partially_paid', 'paid'].includes(invoice.status)
    && invoice.invoice_type !== 'credit_note'

  return (
    <Modal title={`Invoice ${invoice.number_display}`} onClose={onClose} wide printable>
      {error && <p className="bill-error" role="alert">{error}</p>}

      <div className="bill-doc" data-testid="invoice-document">
        {/* The supplier block heads the printed bill. Only shown once the
            print view is loaded, because that is where the server says what
            it has and what it does not. */}
        {print && (
          <dl className="bill-supplier" data-testid="bill-supplier">
            <div><dt>Hospital</dt><dd>{print.supplier.name}</dd></div>
            <div><dt>Address</dt><dd>{print.supplier.address}</dd></div>
            <div><dt>GSTIN</dt><dd>{print.supplier.gstin}</dd></div>
          </dl>
        )}
        <div className="bill-doc__head">
          <div>
            {/* Who issued the bill and when: a bill with no hospital and no
                date on its face cannot be checked against anything. */}
            <div className="bill-muted" data-testid="bill-hospital">
              {invoice.supplier_name || 'Hospital name not configured'}
            </div>
            <div className="bill-doc__number">{invoice.number_display}</div>
            <div className="bill-muted">
              {invoice.patient?.name || invoice.patient_id}
              {invoice.patient?.mrn ? ` · ${invoice.patient.mrn}` : ''}
            </div>
            <div className="bill-muted" data-testid="bill-date">
              Bill date: {invoice.issued_at ? formatClinicalDateTime(invoice.issued_at) : 'Not issued yet'}
            </div>
            {(invoice.period_from || invoice.period_to) && (
              <div className="bill-muted" data-testid="bill-period">
                Period: {invoice.period_from ? chargeDay(invoice.period_from) : 'Not recorded'} to{' '}
                {invoice.period_to ? chargeDay(invoice.period_to) : 'Not recorded'}
              </div>
            )}
          </div>
          <div className="bill-doc__status">
            <StatusChip status={invoice.status} />
            <span className="bill-muted">{humaniseEnum(invoice.invoice_type)}</span>
          </div>
        </div>

        {invoice.cancel_reason && (
          <p className="bill-notice bill-notice--muted">
            Cancelled: {invoice.cancel_reason}. The document and its number are kept;
            nothing was deleted.
          </p>
        )}

        <div className="bill-tablewrap">
          <table className="bill-table">
            <thead>
              <tr>
                <th scope="col">#</th>
                <th scope="col">Item</th>
                <th scope="col" className="bill-num">Qty</th>
                <th scope="col" className="bill-num">Rate</th>
                <th scope="col" className="bill-num">Taxable</th>
                <th scope="col" className="bill-num">GST</th>
                <th scope="col" className="bill-num">Amount</th>
              </tr>
            </thead>
            <tbody>
              {(invoice.lines || []).map(line => (
                <tr key={line.id}>
                  <td>{line.line_no}</td>
                  <td>
                    <div>{line.description}</div>
                    <div className="bill-muted bill-tiny">
                      {line.service_code}
                      {line.gst_treatment === 'exempt'
                        ? ' · GST exempt (healthcare)'
                        : ` · GST ${line.gst_rate_bp / 100}%`}
                    </div>
                  </td>
                  <td className="bill-num">{line.quantity}</td>
                  <td className="bill-num"><Money paise={line.unit_rate_paise} /></td>
                  <td className="bill-num"><Money paise={line.taxable_value_paise} /></td>
                  <td className="bill-num"><Money paise={line.tax_paise} /></td>
                  <td className="bill-num"><Money paise={line.total_paise} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <dl className="bill-totals">
          <div><dt>Taxable value</dt><dd><Money paise={invoice.taxable_value_paise} /></dd></div>
          {invoice.is_interstate
            ? <div><dt>IGST</dt><dd><Money paise={invoice.igst_paise} /></dd></div>
            : (<>
                <div><dt>CGST</dt><dd><Money paise={invoice.cgst_paise} /></dd></div>
                <div><dt>SGST</dt><dd><Money paise={invoice.sgst_paise} /></dd></div>
              </>)}
          {invoice.rounding_adjustment_paise !== 0 && (
            <div><dt>Round off</dt><dd><Money paise={invoice.rounding_adjustment_paise} /></dd></div>
          )}
          <div className="bill-totals__grand">
            <dt>Payable</dt><dd><Money paise={invoice.payable_paise} /></dd>
          </div>
          <div><dt>Paid</dt><dd><Money paise={invoice.amount_paid_paise} /></dd></div>
          <div><dt>Balance</dt><dd><Money paise={invoice.balance_paise} /></dd></div>
        </dl>

        {/* The receipts behind "Paid", so the figure can be checked. A
            reversed receipt stays listed with its status. */}
        <div data-testid="bill-receipts">
          <h3 className="bill-subhead">Receipts applied</h3>
          {(invoice.payments || []).length === 0 ? (
            <p className="bill-muted">No receipts against this bill.</p>
          ) : (
            <ul className="bill-list">
              {(invoice.payments || []).map(p => (
                <li key={p.id}>
                  <span>
                    {p.receipt_number || `Receipt ${p.id}`}
                    {p.received_at ? ` · ${formatClinicalDateTime(p.received_at)}` : ''}
                    {` · ${humaniseEnum(p.method)}`}
                    {p.direction === 'out' ? ' · money out' : ''}
                    {p.status !== 'received' && p.status !== 'cleared' ? ` · ${humaniseEnum(p.status).toLowerCase()}` : ''}
                  </span>
                  <span className="bill-money">{p.amount_display}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>

      {print && (
        <div className="bill-print" data-testid="print-view">
          <h3 className="bill-subhead bill-print__subhead">Printable document</h3>
          <p className="bill-notice bill-notice--warn">{print.supplier.notice}</p>
          {print.number_notice && (
            <p className="bill-notice bill-notice--warn">{print.number_notice}</p>
          )}
          <p className="bill-doc__words">{print.amount_in_words}</p>
          <p className="bill-muted bill-tiny">{print.gst_note}</p>
          <p className="bill-muted bill-tiny">Printed {formatClinicalDateTime(print.printed_at)}</p>
          {/* In .bill-actions so the button itself is left off the paper. */}
          <div className="bill-actions">
            <button type="button" className="bill-btn" onClick={() => window.print()}>
              Print this bill
            </button>
          </div>
        </div>
      )}

      <div className="bill-actions">
        {invoice.status === 'draft' && (
          <button
            type="button" className="bill-btn bill-btn--primary" disabled={busy}
            onClick={() => act(() => billing.issueInvoice(invoice.id))}
          >
            Issue invoice
          </button>
        )}
        <button type="button" className="bill-btn" onClick={showPrint}>
          Print view
        </button>
      </div>

      {(canCancel || canCredit) && (
        <div className="bill-reasonbox">
          <label className="bill-field" htmlFor="bill-reason">
            <span className="bill-field__label">
              Reason (required: an issued document is never removed silently)
            </span>
            <input
              id="bill-reason" className="bill-input" value={reason}
              onChange={e => setReason(e.target.value)}
              placeholder="e.g. charged to the wrong admission"
            />
          </label>
          <div className="bill-actions">
            {canCancel && (
              <button
                type="button" className="bill-btn bill-btn--danger"
                disabled={busy || !reasonReady}
                onClick={() => act(() => billing.cancelInvoice(invoice.id, reason))}
              >
                Cancel invoice
              </button>
            )}
            {canCredit && (
              <button
                type="button" className="bill-btn"
                disabled={busy || !reasonReady}
                onClick={() => act(() => billing.creditNote(invoice.id, reason))}
              >
                Raise credit note
              </button>
            )}
          </div>
          <p className="bill-muted bill-tiny">
            An invoice with money against it cannot be cancelled. It is reversed with
            a credit note, so the receipt still points at a document that exists.
          </p>
        </div>
      )}
    </Modal>
  )
}

function BillsTab() {
  const [invoices, setInvoices] = useState<Invoice[]>([])
  const [status, setStatus] = useState('')
  const [open, setOpen] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const res = await billing.listInvoices(status ? { status } : {})
      setInvoices(res.invoices)
      setError(null)
    } catch (err) {
      setError(errorText(err))
    } finally {
      setLoading(false)
    }
  }, [status])

  // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
  useEffect(() => { void load() }, [load])

  return (
    <section className="bill-section">
      <div className="bill-section__head">
        <h2 className="bill-subhead">Invoices</h2>
        <div className="bill-filters" role="group" aria-label="Filter invoices by status">
          {['', 'draft', 'issued', 'partially_paid', 'paid', 'cancelled'].map(s => (
            <button
              key={s || 'all'}
              type="button"
              className={`bill-filter${status === s ? ' bill-filter--on' : ''}`}
              aria-pressed={status === s}
              onClick={() => setStatus(s)}
            >
              {s ? STATUS_LABEL[s] : 'All'}
            </button>
          ))}
        </div>
      </div>

      {error && <p className="bill-error" role="alert">{error}</p>}
      {loading && <p className="bill-muted">Loading invoices…</p>}
      {!loading && invoices.length === 0 && (
        <p className="bill-muted">
          No invoices yet. Raise one from a patient's charges.
        </p>
      )}

      {invoices.length > 0 && (
        <div className="bill-tablewrap">
          <table className="bill-table">
            <thead>
              <tr>
                <th scope="col">Number</th>
                <th scope="col">Patient</th>
                <th scope="col">Type</th>
                <th scope="col">Status</th>
                <th scope="col" className="bill-num">Payable</th>
                <th scope="col" className="bill-num">Balance</th>
              </tr>
            </thead>
            <tbody>
              {invoices.map(inv => (
                <tr
                  key={inv.id}
                  className="bill-row"
                  tabIndex={0}
                  role="button"
                  onClick={() => setOpen(inv.id)}
                  onKeyDown={e => { if (e.key === 'Enter') setOpen(inv.id) }}
                >
                  <td>{inv.number_display}</td>
                  <td>{inv.patient_name || inv.patient_id}</td>
                  <td>{humaniseEnum(inv.invoice_type)}</td>
                  <td><StatusChip status={inv.status} /></td>
                  <td className="bill-num"><Money paise={inv.payable_paise} /></td>
                  <td className="bill-num"><Money paise={inv.balance_paise} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {open !== null && (
        <InvoiceDetail
          invoiceId={open}
          onClose={() => setOpen(null)}
          onChanged={() => { void load() }}
        />
      )}
    </section>
  )
}

/* ── Charges ─────────────────────────────────────────────────────────────── */

function AdvisoryCard({ advisory }: { advisory: DischargeAdvisory }) {
  const tone = advisory.advisory_status === 'outstanding' ? 'warn'
    : advisory.advisory_status === 'unknown' ? 'unknown' : 'ok'
  return (
    <div className={`bill-advisory bill-advisory--${tone}`} data-testid="discharge-advisory">
      <div className="bill-advisory__head">
        <span className="bill-advisory__label">Discharge advisory</span>
        <span className="bill-advisory__headline">{advisory.headline}</span>
      </div>
      {advisory.reasons.length > 0 && (
        <ul className="bill-advisory__reasons">
          {advisory.reasons.map(reason => <li key={reason}>{reason}</li>)}
        </ul>
      )}
      <p className="bill-advisory__note">
        {advisory.blocks_discharge
          ? 'This deployment enforces financial clearance. An override with a typed reason is required.'
          : 'Advisory only: this does not prevent the discharge.'}
      </p>
    </div>
  )
}

function AddChargeModal({ patientId, admissionId, items, onClose, onDone }: {
  patientId: string
  admissionId: string | null
  items: ServiceItem[]
  onClose: () => void
  onDone: () => void
}) {
  const [code, setCode] = useState('')
  const [qty, setQty] = useState('1')
  const [override, setOverride] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const selected = items.find(i => i.code === code)

  const submit = async () => {
    setBusy(true)
    setError(null)
    try {
      // "two" is not a number at all, which "must be greater than zero" did
      // not say; zero and below are a different mistake with their own words.
      const quantity = Number(qty.replace(/,/g, '').trim())
      if (!qty.trim() || !Number.isFinite(quantity)) {
        throw new Error('Quantity must be a number, e.g. 1 or 0.5')
      }
      const quantityMilli = Math.round(quantity * 1000)
      if (!Number.isSafeInteger(quantityMilli) || quantityMilli <= 0) {
        throw new Error('Quantity must be greater than zero')
      }
      await billing.postCharge(patientId, {
        service_code: code,
        quantity_milli: quantityMilli,
        admission_id: admissionId,
        unit_rate_paise: override.trim() ? toPaise(override.trim()) : undefined,
      })
      onDone()
      onClose()
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal title="Add a charge" onClose={onClose}>
      {error && <p className="bill-error" role="alert">{error}</p>}
      <label className="bill-field" htmlFor="charge-item">
        <span className="bill-field__label">Service item</span>
        <select
          id="charge-item" className="bill-input" value={code}
          onChange={e => setCode(e.target.value)}
        >
          <option value="">Choose an item…</option>
          {items.map(item => (
            <option key={item.code} value={item.code}>
              {item.name} ({item.code})
            </option>
          ))}
        </select>
      </label>
      {selected && (
        <p className="bill-muted">
          List rate:{' '}
          <Money paise={selected.rate_is_configured ? selected.default_rate_paise : null} />
          {selected.gst_treatment === 'exempt'
            ? ' · GST exempt (healthcare service)'
            : ` · GST ${selected.gst_rate_bp / 100}%`}
        </p>
      )}
      <label className="bill-field" htmlFor="charge-qty">
        <span className="bill-field__label">Quantity</span>
        <input
          id="charge-qty" className="bill-input" inputMode="decimal" value={qty}
          onChange={e => setQty(e.target.value)}
        />
      </label>
      <MoneyInput
        id="charge-override" label="Rate override (optional)" value={override}
        onChange={setOverride}
        hint="Leave blank to use the tariff. An override is recorded as one."
      />
      <div className="bill-actions">
        <button
          type="button" className="bill-btn bill-btn--primary"
          disabled={busy || !code} onClick={submit}
        >
          Add charge
        </button>
      </div>
    </Modal>
  )
}

interface CounterLine { key: number; code: string; qty: string }

/**
 * The outpatient counter form (opd-14): type what the patient is paying for,
 * get one issued bill. It posts through the existing counter route
 * (billing.createOpdInvoice), which prices each line from the tariff, refuses
 * an unpriced item rather than billing it at zero, and raises the bill over
 * exactly the lines it just posted, as an outpatient bill with no admission.
 */
function OpdCounterModal({ patientId, patientName, items, onClose, onDone }: {
  patientId: string
  patientName: string
  items: ServiceItem[]
  onClose: () => void
  onDone: (invoiceId: number | null) => void
}) {
  const [lines, setLines] = useState<CounterLine[]>([{ key: 1, code: '', qty: '1' }])
  const nextKey = useRef(2)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const update = (key: number, patch: Partial<CounterLine>) =>
    setLines(rows => rows.map(r => (r.key === key ? { ...r, ...patch } : r)))

  const submit = async () => {
    setBusy(true)
    setError(null)
    try {
      const body = lines.map((line, i) => {
        const quantity = Number(line.qty.replace(/,/g, '').trim())
        if (!line.qty.trim() || !Number.isFinite(quantity)) {
          throw new Error(`Line ${i + 1}: quantity must be a number, e.g. 1 or 0.5`)
        }
        const quantityMilli = Math.round(quantity * 1000)
        if (!Number.isSafeInteger(quantityMilli) || quantityMilli <= 0) {
          throw new Error(`Line ${i + 1}: quantity must be greater than zero`)
        }
        return { service_code: line.code, quantity_milli: quantityMilli }
      })
      const res = await billing.createOpdInvoice(patientId, { lines: body, issue: true })
      onDone(res.invoice.id)
      onClose()
    } catch (err) {
      setError(errorText(err))
      // A line the server priced before it refused a later one stays as a
      // pending charge: the running bill below is re-read so it shows.
      onDone(null)
    } finally {
      setBusy(false)
    }
  }

  const ready = lines.every(l => l.code && l.qty.trim())

  return (
    <Modal title={`Counter bill: ${patientName}`} onClose={onClose}>
      {error && <p className="bill-error" role="alert">{error}</p>}
      <p className="bill-muted">
        Each line is priced from the tariff. An item with no price is refused, never
        billed at ₹0. The bill is issued as an outpatient bill.
      </p>
      {lines.map((line, i) => {
        const selected = items.find(item => item.code === line.code)
        return (
          <fieldset className="bill-fieldset" key={line.key} data-testid="counter-line">
            <legend className="bill-field__label">Line {i + 1}</legend>
            <label className="bill-field" htmlFor={`counter-item-${line.key}`}>
              <span className="bill-field__label">Service item</span>
              <select
                id={`counter-item-${line.key}`} className="bill-input" value={line.code}
                onChange={e => update(line.key, { code: e.target.value })}
              >
                <option value="">Choose an item…</option>
                {items.map(item => (
                  <option key={item.code} value={item.code}>
                    {item.name} ({item.code})
                  </option>
                ))}
              </select>
            </label>
            {selected && (
              <p className="bill-muted bill-tiny">
                List rate:{' '}
                <Money paise={selected.rate_is_configured ? selected.default_rate_paise : null} />
              </p>
            )}
            <label className="bill-field" htmlFor={`counter-qty-${line.key}`}>
              <span className="bill-field__label">Quantity</span>
              <input
                id={`counter-qty-${line.key}`} className="bill-input" inputMode="decimal"
                value={line.qty} onChange={e => update(line.key, { qty: e.target.value })}
              />
            </label>
            {lines.length > 1 && (
              <div className="bill-actions">
                <button
                  type="button" className="bill-btn bill-btn--quiet" disabled={busy}
                  onClick={() => setLines(rows => rows.filter(r => r.key !== line.key))}
                >
                  Remove line {i + 1}
                </button>
              </div>
            )}
          </fieldset>
        )
      })}
      <div className="bill-actions">
        <button
          type="button" className="bill-btn" disabled={busy}
          onClick={() => {
            const key = nextKey.current++
            setLines(rows => [...rows, { key, code: '', qty: '1' }])
          }}
        >
          Add another line
        </button>
        <button
          type="button" className="bill-btn bill-btn--primary"
          disabled={busy || !ready} onClick={submit}
        >
          Bill and issue
        </button>
      </div>
    </Modal>
  )
}

/* The selected patient is OWNED BY THE PANEL, not by this tab.
   The panel unmounts whichever tab is not showing, so while `patientId` lived
   here a clerk part-way through capturing charges lost the patient every time
   they stepped over to Bills to check something — the picker came back reading
   "Choose a patient…" with the running bill gone. Lifting the id one level up
   keeps the selection alive across the round trip; the charges themselves are
   re-fetched on remount by the effect below, so nothing is served stale. */
function ChargesTab(
  { items, patientId, setPatientId }:
  { items: ServiceItem[]; patientId: string; setPatientId: (id: string) => void },
) {
  const [patients, setPatients] = useState<WardPatient[]>([])
  const [charges, setCharges] = useState<Charge[]>([])
  const [summary, setSummary] = useState<FinancialSummary | null>(null)
  const [advisory, setAdvisory] = useState<DischargeAdvisory | null>(null)
  const [capture, setCapture] = useState<CaptureResult | null>(null)
  const [adding, setAdding] = useState(false)
  const [openInvoice, setOpenInvoice] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  // The pending charge whose "Cancel charge" form is open, and its reason.
  const [cancelling, setCancelling] = useState<number | null>(null)
  const [cancelReason, setCancelReason] = useState('')

  /* Patients on the ward AND discharged patients with something still to
     bill: no final bill yet, or a charge still pending after it (an outpatient
     charge the stay's final bill does not take). The picker read the ward
     board alone, so the moment a patient was discharged they vanished from it
     and their final bill could not be raised here. Reloaded when a patient
     is admitted elsewhere in the shell, so a new admission can be billed. */
  const [patientsRev, setPatientsRev] = useState(0)
  useDataChanged(() => setPatientsRev(n => n + 1))
  /* Outpatients at the counter (opd-14): only while the server's 'opd'
     feature is on, as the shell's health probe last read it. Off, the picker
     asks for nothing new. */
  const features = useSyncExternalStore(subscribeModuleFlags, currentFeatureFlags)
  const opdOn = features.opd === true
  useEffect(() => {
    const ward = apiFetch<WardPatient[]>('/vigil/patients?limit=100')
      .then(rows => (Array.isArray(rows) ? rows : []))
      .catch(() => [] as WardPatient[])   // the picker is a convenience, not the path
    const discharged = apiFetch<WardPatient[]>('/vigil/discharged?limit=100')
      .then(rows => (Array.isArray(rows) ? rows : []))
      .catch(() => [] as WardPatient[])
    const outpatients = opdOn
      ? billing.listOpPatients()
        .then(res => (Array.isArray(res?.patients) ? res.patients : []))
        .catch(() => [] as OpPatient[])
      : Promise.resolve([] as OpPatient[])
    void Promise.all([ward, discharged, outpatients]).then(([onWard, gone, walkIns]) => {
      const seen = new Set(onWard.map(p => p.id))
      const unbilled: WardPatient[] = []
      for (const p of gone) {
        if ((p.final_bill_raised && !p.has_pending_charges) || seen.has(p.id)) continue
        seen.add(p.id)
        unbilled.push({ ...p, discharged: true })
      }
      /* A walk-in already listed (on a ward, or discharged with something to
         bill) keeps that entry and its stay; the rest are outpatients. */
      const counter: WardPatient[] = []
      for (const p of walkIns) {
        if (seen.has(p.id)) continue
        seen.add(p.id)
        counter.push({
          id: p.id, name: p.name, mrn: p.mrn ?? undefined, admission_id: null,
          outpatient: true, opVisits: p.visits,
        })
      }
      setPatients([...onWard, ...unbilled, ...counter])
    })
  }, [patientsRev, opdOn])

  /* The stay the bill belongs to. Every charge, capture and invoice raised
     here carries it, so an inpatient line is billed as inpatient: with no
     admission the server classifies a charge as outpatient. */
  const admissionId = useMemo(
    () => patients.find(p => p.id === patientId)?.admission_id ?? null,
    [patients, patientId],
  )
  const outpatient = useMemo(
    () => patients.find(p => p.id === patientId && p.outpatient) ?? null,
    [patients, patientId],
  )
  const isOutpatient = outpatient !== null
  const [counterOpen, setCounterOpen] = useState(false)

  /* An outpatient has no stay to discharge, so the discharge advisory is not
     asked for and not shown. */
  const load = useCallback(async (pid: string, admission: string | null, noStay = false) => {
    if (!pid) return
    setBusy(true)
    setError(null)
    try {
      const res = await billing.listCharges(pid)
      setCharges(res.charges)
      setSummary(res.summary)
      setAdvisory(noStay ? null : await billing.getDischargeAdvisory(pid, admission ?? undefined))
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }, [])

  // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
  useEffect(() => { if (patientId) void load(patientId, admissionId, isOutpatient) }, [patientId, admissionId, isOutpatient, load])

  const pending = useMemo(
    () => charges.filter(c => c.status === 'pending'), [charges],
  )

  /* What "Bill this visit" bills: the outpatient's pending OUTPATIENT lines,
     no admission. A line recorded for a stay is never one of them (the server
     refuses it on an outpatient bill as well). */
  const visitLines = useMemo(
    () => pending.filter(c => (c.admission_id ?? null) === null && c.encounter_type === 'op'),
    [pending],
  )
  const strayLines = useMemo(
    () => pending.filter(c => (c.admission_id ?? null) === null && c.encounter_type !== 'op'),
    [pending],
  )

  /* What "Interim bill" and "Final bill" will take, and what they will not.
     The server bills a stay over the charges linked to THAT stay (and an
     outpatient bill over unlinked ones), so a pending charge from another
     encounter (the OPD consultation that led to the admission, a desk charge
     posted before charges carried the stay) is listed on its own with a way
     to bill it, never left behind a button that skips it. */
  const stayPending = useMemo(
    () => pending.filter(c => (c.admission_id ?? null) === admissionId),
    [pending, admissionId],
  )
  const otherPending = useMemo(() => {
    const groups = new Map<string | null, Charge[]>()
    for (const c of pending) {
      const key = c.admission_id ?? null
      if (key === admissionId) continue
      groups.set(key, [...(groups.get(key) ?? []), c])
    }
    return [...groups.entries()].map(([admission, rows]) => ({ admission, rows }))
  }, [pending, admissionId])

  const runCapture = async (dryRun: boolean) => {
    setBusy(true)
    setError(null)
    try {
      const res = await billing.captureClinicalCharges(patientId, { dry_run: dryRun, admission_id: admissionId })
      setCapture(res)
      if (!dryRun) await load(patientId, admissionId, isOutpatient)
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  const cancelOne = async (chargeId: number) => {
    const reason = cancelReason.trim()
    if (reason.length < 3) return
    setBusy(true)
    setError(null)
    try {
      await billing.cancelCharge(chargeId, reason)
      setCancelling(null)
      setCancelReason('')
      await load(patientId, admissionId, isOutpatient)
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  /* With no `group` the bill sweeps the stay. A group is billed by id under
     its own encounter: outpatient when it carries no admission. */
  const raise = async (
    invoiceType: 'interim' | 'final',
    group?: { admission: string | null; rows: Charge[] },
  ) => {
    setBusy(true)
    setError(null)
    try {
      const res = await billing.createInvoice(patientId, group
        ? {
            invoice_type: invoiceType,
            charge_ids: group.rows.map(c => c.id),
            ...(group.admission ? { admission_id: group.admission } : {}),
          }
        : { invoice_type: invoiceType, admission_id: admissionId })
      setOpenInvoice(res.invoice.id)
      await load(patientId, admissionId, isOutpatient)
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  /* opd-14: the outpatient's own lines, by id, as an outpatient bill with no
     admission. The picker is not re-read here, so the patient stays selected
     with their running bill; the counter list drops a fully billed walk-in
     the next time the tab opens. */
  const billVisit = async () => {
    setBusy(true)
    setError(null)
    try {
      const res = await billing.createInvoice(patientId, {
        invoice_type: 'final',
        encounter_type: 'op',
        charge_ids: visitLines.map(c => c.id),
      })
      setOpenInvoice(res.invoice.id)
      await load(patientId, null, true)
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  const counterDone = (invoiceId: number | null) => {
    if (invoiceId !== null) setOpenInvoice(invoiceId)
    void load(patientId, null, true)
  }

  return (
    <section className="bill-section">
      <div className="bill-section__head">
        <h2 className="bill-subhead">Patient charges</h2>
        <label className="bill-field bill-field--inline" htmlFor="charges-patient">
          <span className="bill-field__label">Patient</span>
          <select
            id="charges-patient" className="bill-input" value={patientId}
            onChange={e => { setPatientId(e.target.value); setCapture(null); setCounterOpen(false) }}
          >
            <option value="">Choose a patient…</option>
            {patients.filter(p => !p.discharged && !p.outpatient).map(p => (
              <option key={p.id} value={p.id}>
                {p.name} ({p.mrn || p.id})
              </option>
            ))}
            {patients.some(p => p.discharged) && (
              <optgroup label="Discharged, still to be billed">
                {patients.filter(p => p.discharged).map(p => (
                  <option key={p.id} value={p.id}>
                    {p.name} ({p.mrn || p.id}), discharged
                  </option>
                ))}
              </optgroup>
            )}
            {patients.some(p => p.outpatient) && (
              <optgroup label="Outpatients today, still to be billed">
                {patients.filter(p => p.outpatient).map(p => (
                  <option key={p.id} value={p.id}>
                    {p.name} ({p.mrn || p.id}), outpatient
                  </option>
                ))}
              </optgroup>
            )}
          </select>
        </label>
      </div>

      {error && <p className="bill-error" role="alert">{error}</p>}
      {!patientId && (
        <p className="bill-muted">Choose a patient to see their running bill.</p>
      )}

      {patientId && summary && (
        <>
          <div className="bill-metrics">
            {/* Below zero the issued bills are owed TO the patient (a credit
                note on a paid bill). "Outstanding -₹5,750.50" read as a debt. */}
            <div className="bill-metric">
              <span className="bill-metric__label">
                {summary.outstanding_paise < 0 ? 'Credit due to patient on issued bills' : 'Outstanding on issued bills'}
              </span>
              <span className="bill-metric__value">
                <Money paise={Math.abs(summary.outstanding_paise)} />
              </span>
            </div>
            <div className="bill-metric">
              <span className="bill-metric__label">
                Captured, not yet billed ({summary.unbilled_charge_count})
              </span>
              <span className="bill-metric__value">
                {/* RULE 1 FOR THE CARD, same as the row cells below: an
                    unpriced pending charge sits in `unbilled_total_paise` as a
                    0 that is not a price. With every pending charge unpriced
                    the "total" is pure placeholder — words, never ₹0.00. With
                    only some unpriced the figure is real but partial, and the
                    note underneath says so. */}
                <Money paise={
                  summary.unpriced_charge_count > 0 &&
                  summary.unpriced_charge_count === summary.unbilled_charge_count
                    ? null
                    : summary.unbilled_total_paise
                } />
              </span>
              {summary.unpriced_charge_count > 0 && (
                <span className="bill-metric__note">
                  {summary.unpriced_charge_count} unpriced: this total is incomplete
                </span>
              )}
            </div>
            <div className="bill-metric">
              <span className="bill-metric__label">Advance held</span>
              <span className="bill-metric__value">
                <Money paise={summary.deposit_balance_paise} />
              </span>
            </div>
          </div>

          {advisory && <AdvisoryCard advisory={advisory} />}

          {outpatient && (
            <div className="bill-capture" data-testid="outpatient-visit">
              <h3 className="bill-subhead">Outpatient visit</h3>
              {(outpatient.opVisits ?? []).length > 0 ? (
                <ul className="bill-list">
                  {(outpatient.opVisits ?? []).map((v, i) => (
                    <li key={`${v.department}-${v.token_number}-${i}`}>
                      <span>
                        {v.department}, {v.doctor_name || 'no doctor assigned'}, token {v.token_number}
                      </span>
                      <span className="bill-muted">{humaniseEnum(v.status)}</span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="bill-muted">No outpatient visit recorded today.</p>
              )}
              <p className="bill-muted">
                {visitLines.length === 0
                  ? 'No outpatient charges are waiting to be billed.'
                  : `Bill this visit bills ${plural(visitLines.length, 'pending outpatient charge')} as an outpatient bill.`}
              </p>
              {strayLines.length > 0 && (
                <p className="bill-notice bill-notice--warn">
                  {plural(strayLines.length, 'pending charge')} with no admission{' '}
                  {strayLines.length === 1 ? 'is' : 'are'} recorded as inpatient, so this
                  visit's bill leaves {strayLines.length === 1 ? 'it' : 'them'} out.
                </p>
              )}
            </div>
          )}

          <div className="bill-actions">
            <button type="button" className="bill-btn" disabled={busy}
                    onClick={() => runCapture(true)}>
              Check the clinical record
            </button>
            <button type="button" className="bill-btn" disabled={busy}
                    onClick={() => runCapture(false)}>
              Pull chargeable events
            </button>
            <button type="button" className="bill-btn" disabled={busy}
                    onClick={() => setAdding(true)}>
              Add a charge
            </button>
            {outpatient ? (
              <>
                <button type="button" className="bill-btn" disabled={busy}
                        onClick={() => setCounterOpen(true)}>
                  Counter bill
                </button>
                <button type="button" className="bill-btn bill-btn--primary"
                        disabled={busy || visitLines.length === 0}
                        onClick={billVisit}>
                  Bill this visit
                </button>
              </>
            ) : (
              <>
                <button type="button" className="bill-btn bill-btn--primary"
                        disabled={busy || stayPending.length === 0}
                        onClick={() => raise('interim')}>
                  Interim bill
                </button>
                <button type="button" className="bill-btn bill-btn--primary"
                        disabled={busy || stayPending.length === 0}
                        onClick={() => raise('final')}>
                  Final bill
                </button>
              </>
            )}
          </div>

          {otherPending.map(group => (
            <div
              key={group.admission ?? 'outpatient'}
              className="bill-capture"
              data-testid="other-encounter-charges"
            >
              <p className="bill-notice bill-notice--warn">
                {plural(group.rows.length, 'pending charge')}{' '}
                {group.rows.length === 1
                  ? (group.admission === null
                    ? 'is not linked to this admission, for example an outpatient consultation before it'
                    : (admissionId ? 'belongs to another admission' : 'belongs to an admission'))
                  : (group.admission === null
                    ? 'are not linked to this admission, for example an outpatient consultation before it'
                    : (admissionId ? 'belong to another admission' : 'belong to an admission'))}.
                {outpatient
                  ? ` This visit's bill does not include ${group.rows.length === 1 ? 'it' : 'them'}.`
                  : ` The interim and final bills above do not include ${group.rows.length === 1 ? 'it' : 'them'}.`}
              </p>
              <ul className="bill-list">
                {group.rows.map(c => (
                  <li key={c.id}>
                    <span>{c.description}</span>
                    <Money paise={c.rate_is_configured ? c.total_paise : null} />
                  </li>
                ))}
              </ul>
              <div className="bill-actions">
                <button type="button" className="bill-btn" disabled={busy}
                        onClick={() => raise('final', group)}>
                  Bill these separately
                </button>
              </div>
            </div>
          ))}

          {capture && (
            <div className="bill-capture" data-testid="capture-result">
              <h3 className="bill-subhead">
                {capture.dry_run ? 'What the clinical record holds' : 'Pulled from the record'}
              </h3>
              {capture.posted.length === 0 && capture.skipped.length === 0 && (
                <p className="bill-muted">Nothing chargeable found for this patient.</p>
              )}
              {capture.posted.length > 0 && (
                <ul className="bill-list">
                  {capture.posted.map(c => (
                    <li key={`${c.source}-${c.source_ref}`}>
                      <span>{c.description}</span>
                      <Money paise={c.total_paise ?? null} unset="Priced on posting" />
                    </li>
                  ))}
                </ul>
              )}
              {capture.skipped.filter(s => s.reason !== 'already_charged').length > 0 && (
                <>
                  <p className="bill-notice bill-notice--warn">
                    {plural(capture.unpriceable_count, 'event')} cannot be billed yet.{' '}
                    {capture.unpriceable_count === 1 ? 'It is' : 'They are'} NOT on this bill and
                    {capture.unpriceable_count === 1 ? ' was' : ' were'} not posted at zero.
                  </p>
                  <ul className="bill-list">
                    {capture.skipped
                      .filter(s => s.reason !== 'already_charged')
                      .map(s => (
                        <li key={`${s.source}-${s.source_ref}`}>
                          <span>{s.description}</span>
                          <span className="bill-reason">
                            {s.reason === 'rate_not_configured' ? 'No price on this tariff'
                              : s.reason === 'no_service_item' ? 'No matching service item'
                              : 'No code to price on'}
                          </span>
                        </li>
                      ))}
                  </ul>
                </>
              )}
            </div>
          )}

          <div className="bill-tablewrap">
            <table className="bill-table">
              <thead>
                <tr>
                  <th scope="col">Item</th>
                  <th scope="col">Source</th>
                  <th scope="col">Date</th>
                  <th scope="col">Status</th>
                  <th scope="col" className="bill-num">Amount</th>
                  <th scope="col"><span className="sr-only">Action</span></th>
                </tr>
              </thead>
              <tbody>
                {charges.map(charge => (
                  <tr key={charge.id}>
                    <td>
                      <div>{charge.description}</div>
                      <div className="bill-muted bill-tiny">
                        {charge.service_code} · {charge.quantity} ×{' '}
                        {formatInrOrUnset(
                          charge.rate_is_configured ? charge.unit_rate_paise : null,
                        )}
                      </div>
                    </td>
                    <td>{humaniseEnum(charge.source)}</td>
                    {/* Charges arrive from two writers with two date shapes: the
                        manual path stores a plain date, the OPD path stores the
                        full consultation timestamp (frontdesk_service passes its
                        `service_date` straight through). Rendering the raw value
                        put "2026-08-12" and
                        "2026-08-12T06:20:45.408698+00:00" in the same column. A
                        charge date is a DAY on a bill; take the LOCAL day. */}
                    <td>{charge.service_date ? chargeDay(charge.service_date) : '—'}</td>
                    <td>
                      <span className={`bill-chip bill-chip--${
                        charge.status === 'billed' ? 'info'
                          : charge.status === 'cancelled' ? 'muted' : 'neutral'}`}>
                        {humaniseEnum(charge.status)}
                      </span>
                    </td>
                    {/* RULE 1, WHICH THIS CELL USED TO BREAK. An unpriced charge
                        really does carry total_paise = 0 in the spine — the
                        consultation happened and nobody has priced the item yet
                        (frontdesk_service._raise_consultation_charge: "UNPRICED
                        IS NOT FREE"). Printing that 0 as "₹0.00" turns "nobody
                        has said what this costs" into "this costs nothing", one
                        column away from a rate cell already reading "Not
                        configured", and a clerk bills the ₹0. The row's own
                        `rate_is_configured` is the flag; the amount follows it. */}
                    <td className="bill-num">
                      <Money paise={charge.rate_is_configured ? charge.total_paise : null} />
                    </td>
                    {/* A mis-posted charge is cancelled WITH a reason while it
                        is still unbilled; once it is on an invoice the invoice
                        is the record and the fix is a cancellation or credit
                        note there. The row is kept, with who and why. */}
                    <td>
                      {charge.status === 'pending' && cancelling !== charge.id && (
                        <button
                          type="button" className="bill-btn" disabled={busy}
                          onClick={() => { setCancelling(charge.id); setCancelReason('') }}
                        >
                          Cancel charge
                        </button>
                      )}
                      {charge.status === 'pending' && cancelling === charge.id && (
                        <div className="bill-reasonbox">
                          <label className="bill-field" htmlFor={`charge-cancel-${charge.id}`}>
                            <span className="bill-field__label">Reason (required)</span>
                            <input
                              id={`charge-cancel-${charge.id}`} className="bill-input"
                              value={cancelReason}
                              onChange={e => setCancelReason(e.target.value)}
                              placeholder="e.g. posted to the wrong patient"
                            />
                          </label>
                          <div className="bill-actions">
                            <button
                              type="button" className="bill-btn bill-btn--danger"
                              disabled={busy || cancelReason.trim().length < 3}
                              onClick={() => cancelOne(charge.id)}
                            >
                              Cancel this charge
                            </button>
                            <button
                              type="button" className="bill-btn" disabled={busy}
                              onClick={() => { setCancelling(null); setCancelReason('') }}
                            >
                              Keep it
                            </button>
                          </div>
                        </div>
                      )}
                      {charge.status === 'cancelled' && charge.cancel_reason && (
                        <span className="bill-muted bill-tiny">Cancelled: {charge.cancel_reason}</span>
                      )}
                    </td>
                  </tr>
                ))}
                {charges.length === 0 && (
                  <tr><td colSpan={6} className="bill-muted">No charges captured yet.</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </>
      )}

      {adding && (
        <AddChargeModal
          patientId={patientId}
          admissionId={admissionId}
          items={items}
          onClose={() => setAdding(false)}
          onDone={() => { void load(patientId, admissionId, isOutpatient) }}
        />
      )}
      {counterOpen && outpatient && (
        <OpdCounterModal
          patientId={patientId}
          patientName={outpatient.name}
          items={items}
          onClose={() => setCounterOpen(false)}
          onDone={counterDone}
        />
      )}
      {openInvoice !== null && (
        <InvoiceDetail
          invoiceId={openInvoice}
          onClose={() => setOpenInvoice(null)}
          onChanged={() => { void load(patientId, admissionId, isOutpatient) }}
        />
      )}
    </section>
  )
}

/* ── Price list ──────────────────────────────────────────────────────────── */

function NewItemModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [code, setCode] = useState('')
  const [name, setName] = useState('')
  const [category, setCategory] = useState('consultation')
  const [treatment, setTreatment] = useState<'exempt' | 'taxable'>('exempt')
  const [rateBp, setRateBp] = useState('1200')
  const [rate, setRate] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const submit = async () => {
    setBusy(true)
    setError(null)
    try {
      await billing.upsertServiceItem({
        code: code.trim(),
        name: name.trim(),
        category,
        gst_treatment: treatment,
        gst_rate_bp: treatment === 'taxable' ? Number(rateBp) : 0,
        // A BLANK price is null — "no price set" — never 0. Sending 0 here would
        // create a free item that looks priced.
        default_rate_paise: rate.trim() ? toPaise(rate.trim()) : null,
        // A new item: a code already on the list is refused and named here,
        // never overwritten (a blank price used to un-price it).
        create_only: true,
      })
      onDone()
      onClose()
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal title="New service item" onClose={onClose}>
      {error && <p className="bill-error" role="alert">{error}</p>}
      <label className="bill-field" htmlFor="item-code">
        <span className="bill-field__label">Code</span>
        <input id="item-code" className="bill-input" value={code}
               onChange={e => setCode(e.target.value)} />
      </label>
      <label className="bill-field" htmlFor="item-name">
        <span className="bill-field__label">Name</span>
        <input id="item-name" className="bill-input" value={name}
               onChange={e => setName(e.target.value)} />
      </label>
      <label className="bill-field" htmlFor="item-category">
        <span className="bill-field__label">Category</span>
        <select id="item-category" className="bill-input" value={category}
                onChange={e => setCategory(e.target.value)}>
          {['consultation', 'procedure', 'investigation', 'imaging', 'bed', 'nursing',
            'ot', 'pharmacy', 'consumable', 'implant', 'ambulance', 'other'].map(c => (
            <option key={c} value={c}>{humaniseEnum(c)}</option>
          ))}
        </select>
      </label>
      <fieldset className="bill-fieldset">
        <legend className="bill-field__label">GST</legend>
        <label className="bill-radio" htmlFor="gst-exempt">
          <input
            id="gst-exempt" type="radio" name="gst" checked={treatment === 'exempt'}
            onChange={() => setTreatment('exempt')}
          />
          <span>Exempt: a healthcare service by a clinical establishment</span>
        </label>
        <label className="bill-radio" htmlFor="gst-taxable">
          <input
            id="gst-taxable" type="radio" name="gst" checked={treatment === 'taxable'}
            onChange={() => setTreatment('taxable')}
          />
          <span>Taxable: pharmacy, consumables, implants, non-clinical supply</span>
        </label>
        {treatment === 'taxable' && (
          <label className="bill-field" htmlFor="item-gst-rate">
            <span className="bill-field__label">Rate</span>
            <select id="item-gst-rate" className="bill-input" value={rateBp}
                    onChange={e => setRateBp(e.target.value)}>
              {[['500', '5%'], ['1200', '12%'], ['1800', '18%'], ['2800', '28%']].map(
                ([bp, label]) => <option key={bp} value={bp}>{label}</option>)}
            </select>
          </label>
        )}
      </fieldset>
      <MoneyInput
        id="item-rate" label="Default rate (optional)" value={rate} onChange={setRate}
        hint="Leave blank for NO PRICE. It will read as “Not configured”, not ₹0."
      />
      <div className="bill-actions">
        <button type="button" className="bill-btn bill-btn--primary"
                disabled={busy || !code.trim() || !name.trim()} onClick={submit}>
          Save item
        </button>
      </div>
    </Modal>
  )
}

function SetRateModal({ item, plans, onClose, onDone }: {
  item: ServiceItem
  plans: TariffPlan[]
  onClose: () => void
  onDone: () => void
}) {
  const [planId, setPlanId] = useState(String(plans[0]?.id ?? ''))
  const [rate, setRate] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const submit = async () => {
    setBusy(true)
    setError(null)
    try {
      await billing.setTariffRate({
        tariff_plan_id: Number(planId),
        service_code: item.code,
        rate_paise: toPaise(rate.trim()),
      })
      onDone()
      onClose()
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal title={`Price: ${item.name}`} onClose={onClose}>
      {error && <p className="bill-error" role="alert">{error}</p>}
      <label className="bill-field" htmlFor="rate-plan">
        <span className="bill-field__label">Tariff plan</span>
        <select id="rate-plan" className="bill-input" value={planId}
                onChange={e => setPlanId(e.target.value)}>
          {plans.map(p => <option key={p.id} value={p.id}>{p.name} ({p.code})</option>)}
        </select>
      </label>
      <MoneyInput id="rate-amount" label="Rate" value={rate} onChange={setRate} />
      <div className="bill-actions">
        <button type="button" className="bill-btn bill-btn--primary"
                disabled={busy || !rate.trim()} onClick={submit}>
          Save rate
        </button>
      </div>
    </Modal>
  )
}

function PricesTab({ items, plans, reload }: {
  items: ServiceItem[]
  plans: TariffPlan[]
  reload: () => void
}) {
  const [creating, setCreating] = useState(false)
  const [pricing, setPricing] = useState<ServiceItem | null>(null)
  const [search, setSearch] = useState('')

  const shown = useMemo(() => {
    const needle = search.trim().toLowerCase()
    if (!needle) return items
    return items.filter(
      i => i.name.toLowerCase().includes(needle) || i.code.toLowerCase().includes(needle),
    )
  }, [items, search])

  const unpriced = items.filter(i => !i.rate_is_configured).length

  return (
    <section className="bill-section">
      <div className="bill-section__head">
        <h2 className="bill-subhead">Price list</h2>
        <div className="bill-actions">
          <input
            className="bill-input bill-input--search" value={search}
            aria-label="Search service items"
            placeholder="Search items…" onChange={e => setSearch(e.target.value)}
          />
          <button type="button" className="bill-btn bill-btn--primary"
                  onClick={() => setCreating(true)}>
            New item
          </button>
        </div>
      </div>

      {unpriced > 0 && (
        <p className="bill-notice bill-notice--warn">
          {plural(unpriced, 'item')} {unpriced === 1 ? 'has' : 'have'} no price.{' '}
          {unpriced === 1 ? 'It cannot' : 'They cannot'} be billed: Orb refuses to
          charge an unpriced item at ₹0.
        </p>
      )}

      <div className="bill-tablewrap">
        <table className="bill-table">
          <thead>
            <tr>
              <th scope="col">Code</th>
              <th scope="col">Item</th>
              <th scope="col">Category</th>
              <th scope="col">GST</th>
              <th scope="col" className="bill-num">Default rate</th>
              <th scope="col"></th>
            </tr>
          </thead>
          <tbody>
            {shown.map(item => (
              <tr key={item.id}>
                <td>{item.code}</td>
                <td>{item.name}</td>
                <td>{humaniseEnum(item.category)}</td>
                <td>
                  {item.gst_treatment === 'exempt'
                    ? <span className="bill-tag">Exempt</span>
                    : <span className="bill-tag">{item.gst_rate_bp / 100}%</span>}
                </td>
                <td className="bill-num">
                  <Money
                    paise={item.rate_is_configured ? item.default_rate_paise : null}
                  />
                </td>
                <td>
                  <button type="button" className="bill-btn bill-btn--quiet"
                          onClick={() => setPricing(item)}>
                    Set rate
                  </button>
                </td>
              </tr>
            ))}
            {shown.length === 0 && (
              <tr><td colSpan={6} className="bill-muted">No service items yet.</td></tr>
            )}
          </tbody>
        </table>
      </div>

      {creating && <NewItemModal onClose={() => setCreating(false)} onDone={reload} />}
      {pricing && (
        <SetRateModal
          item={pricing} plans={plans}
          onClose={() => setPricing(null)} onDone={reload}
        />
      )}
    </section>
  )
}

/* ── The panel ───────────────────────────────────────────────────────────── */

export default function BillingPanel() {
  const [tab, setTab] = useState<TabId>('bills')
  /* Owned here so it outlives ChargesTab's unmount on a tab round trip. */
  const [chargesPatientId, setChargesPatientId] = useState('')
  const [config, setConfig] = useState<BillingConfig | null>(null)
  const [items, setItems] = useState<ServiceItem[]>([])
  const [plans, setPlans] = useState<TariffPlan[]>([])
  const [error, setError] = useState<string | null>(null)
  const mounted = useRef(true)

  const loadMasters = useCallback(async () => {
    try {
      const [cfg, itemRes, planRes] = await Promise.all([
        billing.getBillingConfig(),
        billing.listServiceItems(),
        billing.listTariffPlans(),
      ])
      if (!mounted.current) return
      setConfig(cfg)
      setItems(itemRes.items)
      setPlans(planRes.plans)
      setError(null)
    } catch (err) {
      if (mounted.current) setError(errorText(err))
    }
  }, [])

  useEffect(() => {
    mounted.current = true
    // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
    void loadMasters()
    return () => { mounted.current = false }
  }, [loadMasters])

  return (
    <div className="orb-panel bill-panel">
      <header className="bill-header">
        <div>
          <h1 className="bill-title">Billing</h1>
          <p className="bill-muted">
            {config
              ? `${config.priced_service_item_count} of ${config.service_item_count} items priced · FY ${config.financial_year}`
              : 'Loading the price list…'}
          </p>
        </div>
        {config && (
          <p className={`bill-enforce bill-enforce--${
            config.discharge_financial_enforce ? 'on' : 'off'}`}>
            {config.discharge_financial_enforce
              ? 'Discharge financial clearance: ENFORCED at this site'
              : 'Discharge financial clearance: advisory only'}
          </p>
        )}
      </header>

      {error && <p className="bill-error" role="alert">{error}</p>}

      <nav className="bill-tabs" aria-label="Billing sections">
        {([['bills', 'Bills'], ['charges', 'Charges'], ['prices', 'Price list']] as
          [TabId, string][]).map(([id, label]) => (
          <button
            key={id}
            type="button"
            className={`bill-tab${tab === id ? ' bill-tab--on' : ''}`}
            aria-pressed={tab === id}
            onClick={() => setTab(id)}
          >
            {label}
          </button>
        ))}
      </nav>

      {tab === 'bills' && <BillsTab />}
      {tab === 'charges' && (
        <ChargesTab
          items={items}
          patientId={chargesPatientId}
          setPatientId={setChargesPatientId}
        />
      )}
      {tab === 'prices' && (
        <PricesTab items={items} plans={plans} reload={() => { void loadMasters() }} />
      )}
    </div>
  )
}
